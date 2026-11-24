"""Filing suggestions for inbox items.

Several sources propose a target folder, each with its own confidence:

==============  =====  ===================================================
source          score  basis
==============  =====  ===================================================
import          0.95   decision taken over from another system (Paperless);
                       the folder is rendered anew from the current
                       ``paperless-import.yaml``
rule            1.0    a rule from ``config/rules.yaml`` matched
rule (weak)     0.9    as above, but no sender was detected and the rule's
                       sender was only found in the letterhead
similarity      ≤0.94  folders of the most similar archived documents
heuristic       ≤0.65  folders holding documents of the same sender, folder
                       names matching the sender or the document type
heuristic (new) 0.15   a new folder, only if no existing folder fits
llm             ≤0.55  optional local language model (separate job)
==============  =====  ===================================================

Suggestions for the same folder are merged (reasons combined). An import
suggestion for a folder that does not exist steps behind an existing folder
that very similar documents already live in, or the same sender's folder under
another name next to it – typically the folder was renamed in Finder. A folder
that the current mapping file renders differently than at import time is a
deliberate change and stays first. The first suggestion pre-fills the inbox
form, so every suggestion carries a title, a date and the sender whenever
anything is known about them.

The engine returns :class:`RankedSuggestion` objects. Besides what the inbox
shows they record what automatic filing (:mod:`docushelf.suggest.auto`) needs:
the score of every source that proposes the folder, whether a rule marked
``auto: true`` is among them, and where title and date come from.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field, fields, replace
from datetime import date
from email.utils import parsedate_to_datetime
from pathlib import PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..analysis import doctypes
from ..analysis.extract import entity_key, short_sender
from ..errors import PathError
from ..models import Analysis, Suggestion
from ..util.names import casefold_key, sanitize_folder, title_from_original_name
from ..util.textnorm import normalize
from ..util.timeutil import format_date_de, parse_date, parse_iso_datetime
from .llm import EXAMPLE_FOLDERS, EXAMPLES_PER_FOLDER, MAX_SIMILAR, LlmClient, LlmRequest, SimilarDocument
from .rules import clean_subject, render_title
from .similarity import SimilarityModel, sender_reason

log = logging.getLogger(__name__)

MAX_SUGGESTIONS = 5
MAX_TITLE = 150
# An existing folder with at least this score "fits"; otherwise a new folder is proposed too.
FITTING_SCORE = 0.35
RULE_SCORE = 1.0
# A rule whose sender was only found in the letterhead: below an import (0.95) and
# below very similar documents (similarity is capped at 0.94).
WEAK_RULE_SCORE = 0.9
# Similarity at which an existing folder beats an import suggestion for a missing folder.
STRONG_SIMILARITY = 0.9
_SOURCE_RANK = {"rule": 5, "import": 4, "similarity": 3, "llm": 2, "heuristic": 1}
# Sources whose score is a fixed statement ("a rule matched"), not an estimate.
_FIXED_SCORE_SOURCES = {"rule", "import"}
# First words of sender names that say nothing about the sender itself.
_GENERIC_SENDER_WORDS = {
    "die", "der", "das", "deutsche", "deutscher", "stadt", "landeshauptstadt", "gemeinde", "praxis", "dr", "med",
    "prof", "hausverwaltung", "firma", "kanzlei", "ihre", "ihr", "fa", "gmbh", "ag",
}
_USER_NAMED_SOURCES = ("upload", "paperless")
# Item context passed on to the language model.
_LLM_CONTEXT_KEYS = ("mail_subject", "mail_from", "forwarded_from", "title")


@dataclass
class RankedSuggestion(Suggestion):
    """A suggestion with the details automatic filing relies on.

    Serialized with ``to_dict()`` like every suggestion (the extra keys end up
    in ``item.json``); :meth:`Suggestion.from_dict` ignores them.

    - ``rule_auto``: a rule marked ``auto: true`` proposes this folder.
    - ``signals``: the score of every source proposing this folder, before the
      small bonus for agreement – e.g. ``{"rule": 1.0, "similarity": 0.82}``.
    - ``title_source``: where the title comes from (``rule``, ``import``,
      ``similarity``, ``llm``); None for the fallback title (original file
      name, document type and sender, mail subject).
    - ``date_source``: ``import``, ``llm`` (checked against the text),
      ``analysis``, ``mail`` or ``received``.
    - ``confidence``: the language model's own assessment (``llm`` only).
    """

    rule_auto: bool = False
    signals: dict[str, float] = field(default_factory=dict)
    title_source: str | None = None
    date_source: str | None = None
    confidence: float | None = None


def _clamp(value: Any) -> float:
    try:
        return round(max(0.0, min(1.0, float(value or 0.0))), 3)
    except (TypeError, ValueError):
        return 0.0


def _ranked(suggestion: Suggestion, **details: Any) -> RankedSuggestion:
    """``suggestion`` as a :class:`RankedSuggestion` (``details`` set on it)."""
    if isinstance(suggestion, RankedSuggestion):
        ranked = suggestion
    else:
        values = {item.name: getattr(suggestion, item.name) for item in fields(Suggestion)}
        values["tags"] = list(values.get("tags") or [])
        values["reasons"] = list(values.get("reasons") or [])
        ranked = RankedSuggestion(**values)
    for key, value in details.items():
        setattr(ranked, key, value)
    if not ranked.signals and ranked.folder:
        ranked.signals = {ranked.source: _clamp(ranked.score)}
    return ranked


def _signals(suggestion: Suggestion) -> dict[str, float]:
    found = getattr(suggestion, "signals", None)
    if isinstance(found, Mapping) and found:
        return {str(source): _clamp(score) for source, score in found.items()}
    return {suggestion.source: _clamp(suggestion.score)} if suggestion.folder else {}


@dataclass
class _Base:
    """Values every suggestion falls back to."""

    title: str
    doc_date: date | None
    sender: str | None
    date_source: str | None = None


def _as_analysis(analysis: Analysis | Mapping[str, Any] | None) -> Analysis:
    if isinstance(analysis, Analysis):
        return analysis
    return Analysis.from_dict(dict(analysis or {}))


def _mail_date(value: Any) -> date | None:
    if not value:
        return None
    parsed = parse_iso_datetime(str(value)) if isinstance(value, str) else None
    if parsed:
        return parsed.date()
    direct = parse_date(value)
    if direct:
        return direct
    try:
        return parsedate_to_datetime(str(value)).date()
    except (TypeError, ValueError, IndexError):
        return None


def _original_name(item: Any) -> str | None:
    files = list(getattr(item, "original_files", None) or [])
    if not files:
        return None
    return str(files[0]).split("/", 1)[-1]


def _limit_title(title: str | None) -> str | None:
    if not title:
        return None
    value = re.sub(r"\s+", " ", str(title)).strip()
    if len(value) > MAX_TITLE:
        value = value[:MAX_TITLE].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return value or None


def composed_title(doc_type: str | None, sender: str | None) -> str | None:
    """"Rechnung Muster Elektronik" from document type and sender (legal form dropped)."""
    short = short_sender(sender)
    if short and len(short) > 50:
        short = short[:50].rsplit(" ", 1)[0]
    if doc_type and short:
        return f"{doc_type} {short}"
    return doc_type or short or None


def _paperless_reason(context: Mapping[str, Any]) -> str:
    data = context.get("paperless")
    parts: list[str] = []
    if isinstance(data, Mapping):
        def pick(*keys: str) -> str | None:
            for key in keys:
                value = data.get(key)
                if isinstance(value, Mapping):
                    value = value.get("name")
                if value:
                    return str(value)
            return None

        doc_type = pick("document_type", "document_type_name", "documenttype", "type")
        correspondent = pick("correspondent", "correspondent_name")
        storage_path = pick("storage_path", "storage_path_name", "storagepath")
        if doc_type:
            parts.append(f"Typ {doc_type}")
        if correspondent:
            parts.append(f"Korrespondent {correspondent}")
        if storage_path:
            parts.append(f"Speicherpfad {storage_path}")
    return "aus Paperless: " + ", ".join(parts) if parts else "aus Paperless übernommen"


class SuggestionEngine:
    """Combines rules, similarity, heuristics (and optionally an LLM) into ranked suggestions."""

    def __init__(self, services: Any):
        self.services = services
        self.similarity = SimilarityModel(services)
        self._llm: LlmClient | None = None
        self._llm_lock = threading.Lock()

    # -- public API ------------------------------------------------------------
    def suggest(
        self, item: Any, pages: Sequence[str], analysis: Analysis | Mapping[str, Any] | None
    ) -> list[Suggestion]:
        """Ranked suggestions (at most five, best first) for an inbox item."""
        analysis = _as_analysis(analysis)
        pages = [str(page or "") for page in (pages or [])]
        context = dict(getattr(item, "context", None) or {})
        source = str(getattr(item, "source", "") or "")
        imported = self._import_suggestion(context)
        base = self._base(item, analysis, context, source, imported)
        folders = self._folders()

        candidates: list[RankedSuggestion] = []
        if imported is not None and imported.folder:
            candidates.append(imported)
        candidates += self._step("rules", lambda: self._from_rules(item, analysis, pages, context, source, base))
        similar = [
            _ranked(s, title_source="similarity" if s.title else None)
            for s in self._step("similarity", lambda: self._from_similarity(pages, base))
        ]
        same_sender = [_ranked(s) for s in self._step("sender folders", lambda: self._from_sender_folders(base))]
        candidates += similar + same_sender
        candidates += [
            _ranked(s) for s in self._step("folder names", lambda: self._from_folder_names(analysis, base, folders))
        ]

        merged = _merge(candidates)
        for suggestion in merged:
            self._finish(suggestion, base, folders)
        if imported is not None and not _remapped(imported, context):
            _yield_to_existing_folder(merged, similar, same_sender)
        decided = any(s.source in _FIXED_SCORE_SOURCES for s in merged)
        fits = any(s.folder and not s.new_folder and s.score >= FITTING_SCORE for s in merged)
        if not decided and not fits:
            # Only now a new folder is proposed (a rule or an import may name a new folder themselves).
            proposal = self._step("new folder", lambda: self._new_folder(analysis, base, folders))
            for suggestion in proposal:
                if not any(casefold_key(s.folder or "") == casefold_key(suggestion.folder or "") for s in merged):
                    suggestion = _ranked(suggestion)
                    self._finish(suggestion, base, folders)
                    merged.append(suggestion)
        merged.sort(key=lambda s: (s.score, _SOURCE_RANK.get(s.source, 0)), reverse=True)
        return merged[:MAX_SUGGESTIONS]

    def llm_suggest(
        self,
        item: Any,
        pages: Sequence[str],
        analysis: Analysis | Mapping[str, Any] | None,
        *,
        log: Callable[[str], Any] | None = None,
    ) -> Suggestion | None:
        """Ask the language model (if configured). Slow; meant for a background job.

        ``log`` receives a German line with the reason when there is no suggestion
        (for the item log).
        """
        config = getattr(self.services.settings, "llm", None)
        if not config:
            return None
        analysis = _as_analysis(analysis)
        context = dict(getattr(item, "context", None) or {})
        request = self.llm_request(item, pages, analysis)
        result = self._llm_client(config).classify(request)
        found = result.suggestion
        if found is None:
            if log is not None and result.error:
                try:
                    log(f"KI: {result.error}")
                except Exception:  # pragma: no cover - logging must not break the job
                    logging.getLogger(__name__).exception("cannot write the item log")
            return None
        suggestion = _ranked(
            found,
            title_source="llm" if found.title else None,
            date_source="llm" if found.doc_date else None,
            confidence=result.confidence,
        )
        base = self._base(item, analysis, context, str(getattr(item, "source", "") or ""),
                          self._import_suggestion(context))
        self._finish(suggestion, base, self._folders())
        return suggestion

    def llm_request(
        self, item: Any, pages: Sequence[str], analysis: Analysis | Mapping[str, Any] | None
    ) -> LlmRequest:
        """What the language model gets to see: text, folders with document counts,
        candidate folders with example file names, similar documents, detected facts."""
        analysis = _as_analysis(analysis)
        pages = [str(page or "") for page in (pages or [])]
        context = dict(getattr(item, "context", None) or {})
        text = "\n\n".join(pages)
        folders = self._folders()
        neighbours = self._step("similar documents", lambda: self._neighbours(text))
        candidates = self.candidate_folders(item, pages, analysis, neighbours=neighbours, folders=folders)
        similar = []
        for doc_id, folder, title, score in neighbours[:MAX_SIMILAR]:
            if folder:
                similar.append(SimilarDocument(name=self._document_name(doc_id, title), folder=folder,
                                               score=round(float(score), 3)))
        llm_context = {key: context[key] for key in _LLM_CONTEXT_KEYS if context.get(key)}
        original = _original_name(item)
        if original:
            llm_context["original_name"] = original
        return LlmRequest(
            text=text,
            folders=sorted(folders.values(), key=str.casefold),
            counts=self._folder_counts(),
            candidates=candidates,
            examples=self.example_filenames(candidates[:EXAMPLE_FOLDERS]),
            similar=similar,
            analysis=analysis,
            context=llm_context,
        )

    def candidate_folders(
        self,
        item: Any,
        pages: Sequence[str],
        analysis: Analysis | Mapping[str, Any] | None,
        *,
        neighbours: Sequence[tuple[str, str, str, float]] | None = None,
        folders: Mapping[str, str] | None = None,
    ) -> list[str]:
        """Existing folders that probably fit, best first (spelled as on disk).

        The folders of the item's suggestions (rules, similar documents, the
        sender's folders – computed now if the item has none yet), then the
        folders of the most similar documents.
        """
        folders = self._folders() if folders is None else folders
        result: list[str] = []

        def add(folder: Any) -> None:
            existing = folders.get(casefold_key(str(folder or "")))
            if existing and existing not in result:
                result.append(existing)

        stored = [s for s in (getattr(item, "suggestions", None) or []) if isinstance(s, Mapping)]
        if stored:
            entries = [(s.get("folder"), s.get("source")) for s in stored]
        else:
            entries = [(s.folder, s.source) for s in self.suggest(item, pages, analysis)]
        for folder, source in entries:
            if source != "llm":
                add(folder)
        if neighbours is None:
            text = "\n\n".join(str(page or "") for page in (pages or []))
            neighbours = self._step("similar documents", lambda: self._neighbours(text))
        for _doc_id, folder, _title, _score in neighbours:
            add(folder)
        return result

    def example_filenames(
        self, folders: Iterable[str], *, per_folder: int = EXAMPLES_PER_FOLDER
    ) -> dict[str, list[str]]:
        """Names of the newest documents (by document date) in each folder, to show the naming style."""
        result: dict[str, list[str]] = {}
        for folder in folders:
            if not folder or folder in result:
                continue
            try:
                records = self.services.index.list_in_folder(folder, order="date_desc", limit=per_folder)
            except Exception:  # pragma: no cover - index unavailable
                log.exception("cannot list documents of %s", folder)
                continue
            names = [record.filename for record in records if record.filename]
            if names:
                result[folder] = names
        return result

    def llm_status(self) -> dict[str, Any] | None:
        """The configured language model and how its last request went (None if not configured).

        ``{"provider", "model", "host", "endpoint", "reachable", "ok", "error", "at", "duration_s"}``;
        the last four are None until the first request.
        """
        config = getattr(self.services.settings, "llm", None)
        if not config:
            return None
        client = self._llm_client(config)
        status = dict(client.last_status or {})
        return {
            "provider": client.provider,
            "model": config.model,
            "host": client.host,
            "endpoint": client.endpoint,
            "reachable": status.get("reachable"),
            "ok": status.get("ok"),
            "error": status.get("error"),
            "at": status.get("at"),
            "duration_s": status.get("duration_s"),
        }

    def llm_probe(self) -> dict[str, Any] | None:
        """Run the connection test of :meth:`LlmClient.probe` (None if no model is configured)."""
        config = getattr(self.services.settings, "llm", None)
        if not config:
            return None
        return self._llm_client(config).probe()

    def known_senders(self) -> list[str]:
        """Senders the user confirmed before (archive) plus senders named in rules."""
        names: list[str] = []
        seen: set[str] = set()

        def add(values: Iterable[str]) -> None:
            for value in values:
                key = normalize(value or "")
                if len(key) >= 2 and key not in seen:
                    seen.add(key)
                    names.append(str(value).strip())

        try:
            add(name for name, _count in self.services.index.senders())
        except Exception:  # pragma: no cover - index unavailable
            log.exception("cannot read senders from the index")
        rules = getattr(self.services, "rules", None)
        if rules is not None:
            try:
                add(value for rule in rules.load() for value in rule.match.sender)
            except Exception:  # pragma: no cover - defensive
                log.exception("cannot read rule senders")
        return names

    # -- building blocks ---------------------------------------------------------
    @staticmethod
    def _step(name: str, func) -> list[Suggestion]:
        """One source of suggestions; a failure there must not take the others down."""
        try:
            return list(func() or [])
        except Exception:
            log.exception("suggestion source failed: %s", name)
            return []

    def _llm_client(self, config) -> LlmClient:
        with self._llm_lock:
            if self._llm is None or self._llm.config != config:
                self._llm = LlmClient(config)
            return self._llm

    def _folders(self) -> dict[str, str]:
        """casefold key -> folder as spelled in the archive."""
        try:
            return {casefold_key(folder): folder for folder in self.services.index.list_folders()}
        except Exception:  # pragma: no cover - index unavailable
            log.exception("cannot list folders")
            return {}

    def _folder_counts(self) -> dict[str, int]:
        """Number of documents directly in each folder."""
        try:
            return dict(self.services.index.folder_counts())
        except Exception:  # pragma: no cover - index unavailable
            log.exception("cannot count documents per folder")
            return {}

    def _neighbours(self, text: str) -> list[tuple[str, str, str, float]]:
        """The most similar archived documents: (doc_id, folder, title, similarity)."""
        if not getattr(self.services.settings, "similarity_enabled", True):
            return []
        normalized = normalize(text or "")
        if not normalized:
            return []
        return list(self.similarity.neighbours(normalized, limit=MAX_SIMILAR))

    def _document_name(self, doc_id: str, title: str | None) -> str:
        """File name (without extension) of an archived document, else its title."""
        try:
            record = self.services.index.get(doc_id)
        except Exception:  # pragma: no cover - index unavailable
            record = None
        if record is not None and record.filename:
            return PurePosixPath(record.filename).stem
        return str(title or doc_id)

    def _import_suggestion(self, context: Mapping[str, Any]) -> RankedSuggestion | None:
        data = context.get("import_suggestion")
        if not isinstance(data, Mapping):
            return None
        suggestion = None
        for candidate in (self._rerendered_import(context), data):
            if candidate is None:
                continue
            try:
                suggestion = Suggestion.from_dict(dict(candidate))
                break
            except TypeError:
                log.warning("ignoring malformed import suggestion")
        if suggestion is None:
            return None
        suggestion.source = "import"
        suggestion.score = 0.95
        if suggestion.folder:
            try:
                suggestion.folder = sanitize_folder(str(suggestion.folder)) or None
            except PathError:
                suggestion.folder = None
        if not suggestion.reasons:
            suggestion.reasons = [_paperless_reason(context)]
        return _ranked(
            suggestion,
            title_source="import" if suggestion.title else None,
            date_source="import" if parse_date(suggestion.doc_date) else None,
        )

    def _rerendered_import(self, context: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """The import suggestion with its folder rendered from the current mapping file,
        so a changed ``paperless-import.yaml`` reaches items waiting in the inbox."""
        if not isinstance(context.get("paperless"), Mapping):
            return None
        config_dir = getattr(getattr(self.services, "layout", None), "config", None)
        if config_dir is None:
            return None
        try:
            from ..importers.paperless import recompute_import_suggestion
        except (ImportError, AttributeError):
            return None
        try:
            result = recompute_import_suggestion(context, config_dir)
        except Exception:  # the importer promises not to raise; stay on the stored suggestion
            log.exception("cannot render the import suggestion again")
            return None
        return result if isinstance(result, Mapping) else None

    def _base(
        self,
        item: Any,
        analysis: Analysis,
        context: Mapping[str, Any],
        source: str,
        imported: Suggestion | None,
    ) -> _Base:
        doc_date: date | None = None
        date_source: str | None = None
        for source_name, compute in (
            ("import", lambda: parse_date(imported.doc_date if imported else None)),
            ("analysis", lambda: parse_date(analysis.doc_date)),
            ("mail", lambda: _mail_date(context.get("mail_date"))),
            ("received", lambda: _mail_date(getattr(item, "received_at", None))),
        ):
            doc_date = compute()
            if doc_date is not None:
                date_source = source_name
                break
        sender = (imported.sender if imported else None) or analysis.sender
        title = None
        if imported and imported.title:
            title = imported.title
        if not title and context.get("title"):
            title = str(context["title"])
        if not title:
            original = None
            name = _original_name(item)
            if name:
                original = title_from_original_name(name)
            composed = composed_title(analysis.doc_type, sender)
            user_named = source in _USER_NAMED_SOURCES or source.startswith("watch")
            ordered = [original, composed] if user_named else [composed, original]
            title = next((value for value in ordered if value), None)
        if not title:
            title = clean_subject(str(context.get("mail_subject") or "")) or None
        if not title:
            title = f"Dokument vom {format_date_de(doc_date)}" if doc_date else "Dokument"
        return _Base(title=_limit_title(title) or "Dokument", doc_date=doc_date, sender=sender,
                     date_source=date_source)

    def _from_rules(
        self,
        item: Any,
        analysis: Analysis,
        pages: Sequence[str],
        context: Mapping[str, Any],
        source: str,
        base: _Base,
    ) -> list[RankedSuggestion]:
        rules = getattr(self.services, "rules", None)
        if rules is None:
            return []
        matcher = getattr(rules, "matches", None)
        if callable(matcher):
            matches = [(hit.rule, hit.reasons, hit.page1_only) for hit in matcher(analysis, pages, context, source)]
        else:
            matches = [(rule, reasons, False) for rule, reasons in rules.match(analysis, pages, context, source)]
        if not matches:
            return []
        render_analysis = replace(analysis, sender=base.sender)
        render_context = dict(context)
        original = _original_name(item)
        if original:
            render_context.setdefault("original_name", original)
        result = []
        # Rules whose sender was only found in the letterhead come after full matches
        # (lower score); among equals the order of the file decides.
        for rule, reasons, page1_only in matches:
            title = None
            if rule.action.title:
                title = render_title(rule.action.title, analysis=render_analysis, context=render_context,
                                     doc_date=base.doc_date) or None
            tags: list[str] = []
            for template in rule.action.tags:
                tag = render_title(template, analysis=render_analysis, context=render_context, doc_date=base.doc_date)
                if tag and tag.casefold() not in {t.casefold() for t in tags}:
                    tags.append(tag)
            result.append(
                _ranked(
                    Suggestion(
                        folder=rule.action.folder,
                        title=title,
                        tags=tags,
                        score=WEAK_RULE_SCORE if page1_only else RULE_SCORE,
                        source="rule",
                        reasons=list(reasons),
                        rule_name=rule.name,
                    ),
                    rule_auto=bool(getattr(rule, "auto", False)),
                    title_source="rule" if title else None,
                )
            )
        return result

    def _from_similarity(self, pages: Sequence[str], base: _Base) -> list[Suggestion]:
        if not getattr(self.services.settings, "similarity_enabled", True):
            return []
        text = normalize("\n".join(pages))
        if not text:
            return []
        return self.similarity.suggest(text, sender=base.sender, limit=3, doc_date=base.doc_date)

    def _from_sender_folders(self, base: _Base) -> list[Suggestion]:
        """Folders that already hold documents of this sender (spelled with or without legal form)."""
        if not base.sender:
            return []
        key = entity_key(base.sender)
        index = self.services.index
        spellings = {base.sender}
        spellings.update(name for name, _count in index.senders() if entity_key(name) == key)
        counts: dict[str, int] = {}
        for spelling in spellings:
            for folder, count in index.sender_folders(spelling):
                if folder:
                    counts[folder] = counts.get(folder, 0) + count
        rows = sorted(counts.items(), key=lambda entry: entry[1], reverse=True)
        total = sum(count for _, count in rows)
        result = []
        for folder, count in rows[:3]:
            share = count / total if total else 0.0
            score = 0.35 + 0.3 * share * min(1.0, count / 3)
            result.append(
                Suggestion(folder=folder, score=round(score, 3), source="heuristic", reasons=[sender_reason(count)])
            )
        return result

    def _from_folder_names(self, analysis: Analysis, base: _Base, folders: Mapping[str, str]) -> list[Suggestion]:
        """Existing folders named after the sender or the document type."""
        sender = base.sender
        sender_key = normalize(short_sender(sender) or "")
        sender_words = [word for word in sender_key.split() if word not in _GENERIC_SENDER_WORDS and len(word) >= 3]
        sender_first = sender_words[0] if sender_words else ""
        doc_type = analysis.doc_type
        type_words = set(doctypes.folder_words(doc_type))
        if doc_type:
            type_words.add(normalize(doc_type))

        def sender_hit(component: str) -> bool:
            if not sender_key or not component:
                return False
            if component == sender_key or component.startswith(sender_key + " "):
                return True
            if len(component) >= 4 and component not in _GENERIC_SENDER_WORDS and sender_key.startswith(component + " "):
                return True
            return bool(sender_first) and len(sender_first) >= 4 and component == sender_first

        def type_hit(component: str) -> bool:
            if not component or not type_words:
                return False
            if component in type_words:
                return True
            name = normalize(doc_type or "")
            return bool(name) and component.startswith(name) and len(component) - len(name) <= 3

        scored: list[tuple[float, int, str, list[str]]] = []
        for folder in folders.values():
            parts = [normalize(part) for part in folder.split("/")]
            leaf, ancestors = parts[-1], parts[:-1]
            leaf_sender, leaf_type = sender_hit(leaf), type_hit(leaf)
            any_sender = leaf_sender or any(sender_hit(part) for part in ancestors)
            ancestor_type = any(type_hit(part) for part in ancestors)
            score = 0.0
            reasons: list[str] = []
            if any_sender:
                score = 0.5 if leaf_sender else 0.4
                reasons.append(f"Absender: {sender}")
                if leaf_type or ancestor_type:
                    score += 0.15
                    reasons.append(f"Dokumenttyp: {doc_type}")
            elif leaf_type:
                score = 0.35
                reasons.append(f"Dokumenttyp: {doc_type}")
            if score:
                scored.append((score, len(parts), folder, reasons))
        scored.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        return [
            Suggestion(folder=folder, score=round(score, 3), source="heuristic", reasons=reasons)
            for score, _depth, folder, reasons in scored[:3]
        ]

    def _new_folder(self, analysis: Analysis, base: _Base, folders: Mapping[str, str]) -> list[Suggestion]:
        """A clearly marked new folder, for documents nothing in the archive fits."""
        short = short_sender(base.sender)
        if short and len(short) > 40:
            short = short[:40].rsplit(" ", 1)[0]
        template = doctypes.new_folder_template(analysis.doc_type) if analysis.doc_type else (
            doctypes.DEFAULT_NEW_FOLDER if short else doctypes.FALLBACK_NEW_FOLDER
        )
        if short:
            raw = template.replace("{sender}", short)
        else:
            raw = template.replace("/{sender}", "").replace("{sender}", "") or doctypes.FALLBACK_NEW_FOLDER
        try:
            folder = sanitize_folder(raw)
        except PathError:
            folder = sanitize_folder(template.replace("/{sender}", "").replace("{sender}", "")) or "Sonstiges"
        if not folder:
            return []
        existing = folders.get(casefold_key(folder))
        if existing:
            reasons = []
            if base.sender:
                reasons.append(f"Absender: {base.sender}")
            if analysis.doc_type:
                reasons.append(f"Dokumenttyp: {analysis.doc_type}")
            return [Suggestion(folder=existing, score=0.3, source="heuristic", reasons=reasons or ["passender Ordner"])]
        return [
            Suggestion(
                folder=folder,
                score=0.15,
                source="heuristic",
                reasons=["neuer Ordner – kein vorhandener Ordner passt"],
                new_folder=True,
            )
        ]

    def _finish(self, suggestion: Suggestion, base: _Base, folders: Mapping[str, str]) -> None:
        """Fill in title, date and sender; use the archive's spelling of the folder; mark new folders."""
        ranked = isinstance(suggestion, RankedSuggestion)
        if suggestion.folder:
            existing = folders.get(casefold_key(suggestion.folder))
            suggestion.new_folder = existing is None
            if existing is not None:
                suggestion.folder = existing
        else:
            suggestion.new_folder = False
        title = _limit_title(suggestion.title)
        if not title and ranked:
            suggestion.title_source = None
        suggestion.title = title or base.title
        if not suggestion.doc_date and base.doc_date:
            suggestion.doc_date = base.doc_date.isoformat()
            if ranked:
                suggestion.date_source = base.date_source
        if not suggestion.sender and base.sender:
            suggestion.sender = base.sender
        suggestion.score = _clamp(suggestion.score)
        if ranked:
            suggestion.signals = _signals(suggestion)


_YEAR_RE = re.compile(r"(?:19|20)\d{2}")


def _other_period(folder: str, other: str) -> bool:
    """"Steuern/2023" next to "Steuern/2024": the same structure for another year."""
    parts, other_parts = casefold_key(folder).split("/"), casefold_key(other).split("/")
    if len(parts) != len(other_parts):
        return False
    differing = [(a, b) for a, b in zip(parts, other_parts) if a != b]
    return bool(differing) and all(_YEAR_RE.fullmatch(a) and _YEAR_RE.fullmatch(b) for a, b in differing)


def _remapped(imported: Suggestion, context: Mapping[str, Any]) -> bool:
    """True if the current mapping file gives another folder than the one stored at import."""
    stored = context.get("import_suggestion")
    folder = stored.get("folder") if isinstance(stored, Mapping) else None
    try:
        stored_key = casefold_key(sanitize_folder(str(folder or "")))
    except PathError:
        return True
    return stored_key != casefold_key(imported.folder or "")


def _sender_named_in(component: str, sender: str | None) -> bool:
    """True if a folder name contains a distinctive word of the sender's name."""
    words = {
        word for word in normalize(short_sender(sender) or "").split()
        if word not in _GENERIC_SENDER_WORDS and len(word) >= 3
    }
    return bool(words & set(normalize(component).split()))


def _renamed_sender_folder(folder: str, imported: Suggestion) -> bool:
    """"Finanzen/Rechnungen/Telekom" next to the missing "Finanzen/Rechnungen/Telekom
    Deutschland GmbH": the sender's folder under another name."""
    parent, _, leaf = folder.rpartition("/")
    import_parent, _, import_leaf = (imported.folder or "").rpartition("/")
    return (
        casefold_key(parent) == casefold_key(import_parent)
        and casefold_key(leaf) != casefold_key(import_leaf)
        and _sender_named_in(leaf, imported.sender)
        and _sender_named_in(import_leaf, imported.sender)
    )


def _yield_to_existing_folder(
    merged: list[Suggestion], similar: Sequence[Suggestion], same_sender: Sequence[Suggestion]
) -> None:
    """Put an import suggestion for a missing folder behind an existing folder that fits.

    The folder of an import suggestion was rendered from the mapping file when the
    document was imported; after that folder was renamed (in Finder, say) it no
    longer exists. An existing folder holding very similar documents, or the same
    sender's folder under another name next to it, then wins – but never another
    year of the same structure ("Steuern/2023" for "Steuern/2024"). Callers skip
    this for a folder the current mapping file renders differently: that is a
    deliberate change of the structure.
    """
    imported = next((s for s in merged if s.source == "import"), None)
    if imported is None or not imported.new_folder or not imported.folder:
        return
    wanted = {casefold_key(s.folder) for s in similar if s.folder and s.score >= STRONG_SIMILARITY}
    wanted |= {casefold_key(s.folder) for s in same_sender if s.folder and _renamed_sender_folder(s.folder, imported)}
    alternatives = [
        s for s in merged
        if s is not imported and s.folder and not s.new_folder and casefold_key(s.folder) in wanted
        and not _other_period(s.folder, imported.folder)
    ]
    if not alternatives:
        return
    best = max(alternatives, key=lambda s: s.score)
    if best.score <= imported.score:
        imported.score = round(max(0.0, best.score - 0.01), 3)
        imported.reasons.append(f"Ordner gibt es noch nicht – „{best.folder}“ passt besser")
        signals = getattr(imported, "signals", None)
        if isinstance(signals, dict):
            signals["import"] = imported.score


def _merge(candidates: Sequence[Suggestion]) -> list[RankedSuggestion]:
    """One suggestion per folder; reasons and missing values are taken from the others.

    ``signals`` keeps the score of every source; the title and the date say
    which suggestion they were taken from.
    """
    groups: dict[str, list[RankedSuggestion]] = {}
    for suggestion in candidates:
        if not suggestion.folder:
            continue
        groups.setdefault(casefold_key(suggestion.folder), []).append(_ranked(suggestion))
    merged: list[RankedSuggestion] = []
    for group in groups.values():
        group = sorted(group, key=lambda s: (s.score, _SOURCE_RANK.get(s.source, 0)), reverse=True)
        best = replace(group[0], tags=list(group[0].tags), reasons=list(group[0].reasons),
                       signals=dict(group[0].signals))
        sources = {s.source for s in group}
        signals: dict[str, float] = {}
        for member in group:
            for source, score in _signals(member).items():
                signals[source] = max(signals.get(source, 0.0), score)
        for other in group[1:]:
            for reason in other.reasons:
                if reason not in best.reasons:
                    best.reasons.append(reason)
            if not best.title and other.title:
                best.title, best.title_source = other.title, other.title_source
            if not best.doc_date and other.doc_date:
                best.doc_date, best.date_source = other.doc_date, other.date_source
            best.sender = best.sender or other.sender
            if other.rule_auto and not best.rule_auto:
                # The rule marked for automatic filing names the suggestion.
                best.rule_auto, best.rule_name = True, other.rule_name or best.rule_name
            best.rule_name = best.rule_name or other.rule_name
            for tag in other.tags:
                if tag.casefold() not in {t.casefold() for t in best.tags}:
                    best.tags.append(tag)
        best.signals = signals
        if len(sources) > 1 and best.source not in _FIXED_SCORE_SOURCES:
            # Independent sources agreeing on a folder raise the confidence a little.
            best.score = min(0.94, best.score + 0.05 * (len(sources) - 1))
        merged.append(best)
    return merged


__all__ = ["RankedSuggestion", "SuggestionEngine", "composed_title"]
