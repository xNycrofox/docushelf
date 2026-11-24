"""Decisions for automatic filing ("Automatische Ablage").

:func:`decide` looks at an inbox item that is ready – at its suggestions from
rules, the import, similar documents, heuristics and the language model – and
says whether it may be filed without asking, where to, under which name, and
how sure that is. It files nothing itself; the pipeline does that and keeps a
record of every decision.

The checks, in this order:

1. **hold** if automatic filing is off, the item's source is not enabled, the
   item is not ready, it may be a duplicate, there is no suggestion, or a
   person already retried or edited it (``options["no_auto"]``).
2. A rule marked ``auto: true`` → **file** with its suggestion (confidence 1.0;
   a folder that does not exist yet only if new folders are allowed).
3. Mode ``rules`` → **hold**.
4. The suggestions are combined per folder (case-insensitive): every source
   counts with a weight, ``combined = 1 − Π(1 − weight · score)``, so
   independent sources agreeing raise the confidence.
5. A runner-up folder within 0.1 of the best costs 20 %.
6. A folder that does not exist yet: **hold** unless new folders are allowed;
   10 % less unless a rule or the import named it.
7. Title, date, sender and tags (see :func:`decide`); no usable title → **hold**.
8. Mode ``confident`` files at or above the threshold; mode ``always`` files
   everything and marks decisions below the threshold for review. The
   confidence is rounded to three places first, so the stored value is the
   one that decided.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..config import AutoFileConfig
from ..errors import PathError
from ..models import FilingRequest
from ..util.names import casefold_key, format_filename
from ..util.timeutil import parse_date, parse_iso_datetime

log = logging.getLogger(__name__)

# How much each source's score counts (rule 4).
SOURCE_WEIGHTS = {"rule": 1.0, "import": 1.0, "similarity": 1.0, "llm": 0.9, "heuristic": 0.5}
UNKNOWN_SOURCE_WEIGHT = 0.5
AMBIGUITY_MARGIN = 0.1
AMBIGUITY_FACTOR = 0.8
NEW_FOLDER_FACTOR = 0.9
RECEIVED_DATE_FACTOR = 0.85
# Sources that a wildcard never covers: they must be listed by name.
EXPLICIT_SOURCES = ("paperless", "unbekannt")
SOURCE_LABELS = {
    "rule": "Regel",
    "import": "Paperless",
    "similarity": "Ähnlichkeit",
    "llm": "KI",
    "heuristic": "Heuristik",
}
_SOURCE_ORDER = ("rule", "import", "similarity", "llm", "heuristic")
# Sources a folder counts as "decided" by: no penalty for a new folder.
_DECIDING_SOURCES = {"rule", "import"}
# Whose title wins (rule 7); anything else is the composed fallback title.
_TITLE_ORDER = ("rule", "import", "llm", "similarity")
# Whose date is looked at first.
_DATE_ORDER = ("import", "rule", "llm", "similarity", "heuristic")
# Dates that were checked against the document (an import keeps the date decided there).
_CHECKED_DATE_SOURCES = {"import", "llm"}
MAX_TITLE = 150
MAX_REASONS = 6


@dataclass
class AutoDecision:
    """The outcome of :func:`decide`.

    ``action`` is ``"file"`` (``request`` says how) or ``"hold"`` (the item
    stays in the inbox; ``reasons[0]`` says why). ``review`` marks a document
    filed although its confidence is below the threshold (mode ``always``).
    ``sources`` are the signals that support the folder (``rule``, ``import``,
    ``similarity``, ``llm``, ``heuristic``); ``reasons`` are short German texts
    for the inbox and the item log.
    """

    action: str
    request: FilingRequest | None
    confidence: float
    review: bool = False
    sources: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form (for item.json and the audit log)."""
        request = None
        if self.request is not None:
            request = {
                "folder": self.request.folder,
                "title": self.request.title,
                "doc_date": self.request.doc_date.isoformat() if self.request.doc_date else None,
                "tags": list(self.request.tags),
                "sender": self.request.sender,
            }
        return {
            "action": self.action,
            "confidence": self.confidence,
            "review": self.review,
            "sources": list(self.sources),
            "reasons": list(self.reasons),
            "request": request,
        }


# ---------------------------------------------------------------------------
# Helpers for callers


def source_allowed(source: str | None, sources: Sequence[str]) -> bool:
    """True if items from ``source`` may be filed automatically.

    Entries are source names (``upload``, ``smb``, ``watch-scanner`` …) or
    prefixes with a trailing ``*`` (``watch-*``, ``*``). ``paperless`` and
    ``unbekannt`` are only covered when listed by name.
    """
    name = (str(source or "").strip() or "unbekannt").casefold()
    for entry in sources or ():
        pattern = str(entry or "").strip().casefold()
        if not pattern:
            continue
        if pattern == name:
            return True
        if pattern.endswith("*") and name not in EXPLICIT_SOURCES and name.startswith(pattern[:-1]):
            return True
    return False


def describe_sources(sources: Iterable[str] | None) -> list[str]:
    """German labels for signal names: Regel, Paperless, Ähnlichkeit, KI, Heuristik."""
    labels: list[str] = []
    for source in sources or ():
        label = SOURCE_LABELS.get(str(source), str(source))
        if label and label not in labels:
            labels.append(label)
    return labels


def confidence_percent(value: float | None) -> int:
    """A confidence as a whole percentage, rounded down (0.749 is 74 %, never "75 %" below a 75 % threshold)."""
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        return 0
    if math.isnan(number):
        return 0
    return max(0, min(100, math.floor(number * 100 + 1e-9)))


# ---------------------------------------------------------------------------
# Reading suggestions


def _number(value: Any) -> float:
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(number):
        return 0.0
    return max(0.0, min(1.0, number))


def _folder_key(folder: Any) -> str:
    return casefold_key(str(folder or "").strip().strip("/"))


def _signals(suggestion: Mapping[str, Any]) -> dict[str, float]:
    """Score per source for the suggestion's folder."""
    found = suggestion.get("signals")
    if isinstance(found, Mapping) and found:
        return {str(source): _number(score) for source, score in found.items()}
    source = str(suggestion.get("source") or "heuristic")
    return {source: _number(suggestion.get("score"))}


def _text(value: Any, limit: int = 200) -> str | None:
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:limit].strip() or None


def _clean_title(value: Any) -> str | None:
    """A title that gives a valid file name, or None."""
    title = _text(value, MAX_TITLE * 2)
    if not title:
        return None
    if len(title) > MAX_TITLE:
        title = title[:MAX_TITLE].rsplit(" ", 1)[0].rstrip(" ,;:-") or title[:MAX_TITLE]
    try:
        format_filename(title, None)
    except PathError:
        return None
    return title


def _tags(values: Any) -> list[str]:
    result: list[str] = []
    for value in values or []:
        tag = _text(value, 80)
        if tag and tag.casefold() not in {t.casefold() for t in result}:
            result.append(tag)
    return result


@dataclass
class _Folder:
    """All suggestions for one folder."""

    folder: str
    members: list[dict[str, Any]] = field(default_factory=list)
    signals: dict[str, float] = field(default_factory=dict)

    @property
    def combined(self) -> float:
        remaining = 1.0
        for source, score in self.signals.items():
            weight = SOURCE_WEIGHTS.get(source, UNKNOWN_SOURCE_WEIGHT)
            remaining *= 1.0 - max(0.0, min(1.0, weight * score))
        return 1.0 - remaining

    @property
    def sources(self) -> list[str]:
        ordered = [source for source in _SOURCE_ORDER if source in self.signals]
        return ordered + sorted(source for source in self.signals if source not in _SOURCE_ORDER)

    def by_source(self, order: Sequence[str]) -> list[dict[str, Any]]:
        """Members sorted by their source in ``order`` (others last)."""
        rank = {source: index for index, source in enumerate(order)}
        return sorted(self.members, key=lambda s: rank.get(str(s.get("source")), len(order)))


def _groups(suggestions: Sequence[Mapping[str, Any]]) -> list[_Folder]:
    """Suggestions combined per folder (case-insensitive), best first."""
    groups: dict[str, _Folder] = {}
    for suggestion in suggestions:
        folder = str(suggestion.get("folder") or "").strip().strip("/")
        if not folder:
            continue
        key = _folder_key(folder)
        group = groups.get(key)
        if group is None:
            group = groups[key] = _Folder(folder=folder)
        elif group.members and all(m.get("new_folder") for m in group.members) and not suggestion.get("new_folder"):
            group.folder = folder  # the spelling of a suggestion that knew the folder exists
        group.members.append(dict(suggestion))
        for source, score in _signals(suggestion).items():
            group.signals[source] = max(group.signals.get(source, 0.0), score)
    order = list(groups.values())
    order.sort(key=lambda g: g.combined, reverse=True)
    return order


# ---------------------------------------------------------------------------
# The decision


def _config(settings: Any) -> AutoFileConfig:
    config = getattr(settings, "auto_file", None)
    return config if isinstance(config, AutoFileConfig) else AutoFileConfig()


def _analysis(item: Any) -> Mapping[str, Any]:
    analysis = getattr(item, "analysis", None)
    if isinstance(analysis, Mapping):
        return analysis
    if analysis is not None and hasattr(analysis, "to_dict"):
        return analysis.to_dict()
    return {}


def _received_date(item: Any) -> date | None:
    """When the document arrived: the date of the mail, else the time it was received."""
    context = getattr(item, "context", None) or {}
    mail_date = context.get("mail_date") if isinstance(context, Mapping) else None
    if mail_date:
        parsed = parse_iso_datetime(str(mail_date)) if isinstance(mail_date, str) else None
        if parsed is not None:
            return parsed.date()
        try:
            return parsedate_to_datetime(str(mail_date)).date()
        except (TypeError, ValueError, IndexError):
            pass
    received = getattr(item, "received_at", None)
    parsed = parse_iso_datetime(str(received)) if received else None
    return parsed.date() if parsed is not None else parse_date(received)


def _percent_label(confidence: float) -> str:
    return f"unsicher ({confidence_percent(confidence)} %)"


def _rule_reasons(suggestion: Mapping[str, Any]) -> list[str]:
    name = _text(suggestion.get("rule_name"), 80) or "ohne Namen"
    reasons = [f"automatische Regel „{name}“"]
    for reason in suggestion.get("reasons") or []:
        text = _text(reason)
        if text and not text.startswith("Regel „") and text not in reasons:
            reasons.append(text)
    return reasons[:4]


def _support_reasons(group: _Folder) -> list[str]:
    """Short reasons of the sources that support the folder, strongest first."""
    reasons: list[str] = []

    def strength(member: Mapping[str, Any]) -> float:
        return max(
            (SOURCE_WEIGHTS.get(source, UNKNOWN_SOURCE_WEIGHT) * score for source, score in _signals(member).items()),
            default=0.0,
        )

    for member in sorted(group.members, key=strength, reverse=True):
        texts = [_text(reason) for reason in member.get("reasons") or []]
        texts = [text for text in texts if text]
        if member.get("source") == "llm":
            texts = [f"KI: {texts[0]}"] if texts else ["Vorschlag der KI"]
        for text in texts[:3]:
            if text not in reasons:
                reasons.append(text)
    return reasons[:4]


def _choose_title(group: _Folder, suggestions: Sequence[Mapping[str, Any]]) -> str | None:
    """Rule title, then import, language model, similar document, then the composed title."""
    candidates: dict[str, str] = {}
    for member in group.members:
        source = member.get("title_source")
        title = _clean_title(member.get("title"))
        if title and source in _TITLE_ORDER and source != "llm":
            candidates.setdefault(str(source), title)
    for suggestion in suggestions:
        if suggestion.get("source") == "llm" and suggestion.get("title_source") == "llm":
            title = _clean_title(suggestion.get("title"))
            if title:
                candidates.setdefault("llm", title)
                break
    for source in _TITLE_ORDER:
        if source in candidates:
            return candidates[source]
    for member in group.by_source(_SOURCE_ORDER):
        title = _clean_title(member.get("title"))
        if title:
            return title
    return None


def _choose_date(group: _Folder, item: Any) -> tuple[date | None, bool]:
    """The document date and whether only the received date was left.

    The date of the folder's suggestions counts if it agrees with the date the
    analysis found, or if the language model (checked against the text) or the
    import supplied it; otherwise the analysis date, otherwise the received date.
    """
    analysis_date = parse_date(_analysis(item).get("doc_date"))
    for member in group.by_source(_DATE_ORDER):
        value = parse_date(member.get("doc_date"))
        if value is None:
            continue
        if analysis_date is not None and value == analysis_date:
            return value, False
        if member.get("date_source") in _CHECKED_DATE_SOURCES:
            return value, False
    if analysis_date is not None:
        return analysis_date, False
    return _received_date(item), True


def _choose_sender(suggestions: Sequence[Mapping[str, Any]], item: Any) -> str | None:
    """The sender confirmed in an import, else the detected one, else the language model's."""
    for suggestion in suggestions:
        if suggestion.get("source") == "import" and _text(suggestion.get("sender")):
            return _text(suggestion.get("sender"))
    detected = _text(_analysis(item).get("sender"))
    if detected:
        return detected
    for suggestion in suggestions:
        if suggestion.get("source") == "llm" and _text(suggestion.get("sender")):
            return _text(suggestion.get("sender"))
    return None


def _choose_tags(group: _Folder) -> list[str]:
    """Tags of rules (and of an import); tags of the language model are never used automatically."""
    tags: list[str] = []
    for member in group.members:
        if member.get("source") == "llm" or not (_DECIDING_SOURCES & set(_signals(member))):
            continue
        for tag in _tags(member.get("tags")):
            if tag.casefold() not in {t.casefold() for t in tags}:
                tags.append(tag)
    return tags


def decide(item: Any, *, settings: Any, folder_exists: Callable[[str], bool]) -> AutoDecision:
    """Whether and how ``item`` (an :class:`~docushelf.inbox.items.InboxItem` in ``bereit``)
    may be filed automatically. ``folder_exists(folder)`` tells whether a folder
    exists in the archive right now. Never files anything; see the module docstring.
    """
    config = _config(settings)

    def hold(reason: str, *, confidence: float = 0.0, sources: Sequence[str] = (),
             more: Sequence[str] = ()) -> AutoDecision:
        reasons = [reason] + [text for text in more if text and text != reason]
        return AutoDecision("hold", None, round(confidence, 3), False, list(sources), reasons[:MAX_REASONS])

    def exists(folder: str) -> bool | None:
        try:
            return bool(folder_exists(folder))
        except Exception:
            log.exception("cannot check whether folder %r exists", folder)
            return None

    # 1. Reasons never to file automatically.
    if config.mode == "off":
        return hold("automatische Ablage ist ausgeschaltet")
    source = str(getattr(item, "source", "") or "unbekannt")
    if not source_allowed(source, config.sources):
        return hold(f"Eingang „{source}“ ist für die automatische Ablage nicht freigegeben")
    if getattr(item, "state", None) != "bereit":
        return hold("Dokument ist nicht bereit")
    if getattr(item, "duplicate_of", None):
        return hold("mögliches Duplikat")
    suggestions = [dict(s) for s in (getattr(item, "suggestions", None) or []) if isinstance(s, Mapping)]
    if not suggestions:
        return hold("kein Ablagevorschlag")
    options = getattr(item, "options", None) or {}
    if isinstance(options, Mapping) and options.get("no_auto"):
        return hold("von Hand bearbeitet")
    groups = _groups(suggestions)

    # 2. A rule marked for automatic filing decides.
    auto_rule = next((s for s in suggestions if s.get("rule_auto") and _folder_key(s.get("folder"))), None)
    if auto_rule is not None:
        group = next(g for g in groups if g.members and _folder_key(g.folder) == _folder_key(auto_rule["folder"]))
        folder = group.folder
        reasons = _rule_reasons(auto_rule)
        present = exists(folder)
        if present is None:
            return hold("Ordner konnte nicht geprüft werden", confidence=1.0, sources=group.sources)
        if not present:
            if not config.new_folders:
                return hold(f"Ordner „{folder}“ gibt es noch nicht", confidence=1.0, sources=group.sources,
                            more=reasons)
            reasons.append(f"neuer Ordner „{folder}“")
        title = _clean_title(auto_rule.get("title"))
        if not title:
            return hold("kein brauchbarer Titel", confidence=1.0, sources=group.sources, more=reasons)
        request = FilingRequest(
            folder=folder,
            title=title,
            doc_date=parse_date(auto_rule.get("doc_date")) or _received_date(item),
            tags=_tags(auto_rule.get("tags")),
            sender=_text(auto_rule.get("sender")),
        )
        return AutoDecision("file", request, 1.0, False, group.sources, reasons[:MAX_REASONS])

    # 3. Only rules may file in this mode.
    if config.mode == "rules":
        return hold("keine automatische Regel passt")

    # 4. Combine the sources per folder.
    if not groups:
        return hold("kein Ordnervorschlag")
    best = groups[0]
    confidence = best.combined
    sources = best.sources
    notes: list[str] = []

    # 5. Two folders that fit almost equally well.
    if len(groups) > 1 and best.combined - groups[1].combined <= AMBIGUITY_MARGIN + 1e-9:
        confidence *= AMBIGUITY_FACTOR
        notes.append("zwei Ordner kommen in Frage")

    # 6. A folder that does not exist yet.
    present = exists(best.folder)
    if present is None:
        return hold("Ordner konnte nicht geprüft werden", confidence=confidence, sources=sources)
    if not present:
        if not config.new_folders:
            return hold(f"Ordner „{best.folder}“ gibt es noch nicht", confidence=confidence, sources=sources,
                        more=_support_reasons(best))
        if not (_DECIDING_SOURCES & set(best.signals)):
            confidence *= NEW_FOLDER_FACTOR
        notes.append(f"neuer Ordner „{best.folder}“")

    # 7. Title, date, sender, tags.
    title = _choose_title(best, suggestions)
    if not title:
        return hold("kein brauchbarer Titel", confidence=confidence, sources=sources)
    doc_date, received = _choose_date(best, item)
    if received:
        confidence *= RECEIVED_DATE_FACTOR
        notes.append("kein Datum erkannt – Eingangsdatum verwendet")
    request = FilingRequest(
        folder=best.folder,
        title=title,
        doc_date=doc_date,
        tags=_choose_tags(best),
        sender=_choose_sender(suggestions, item),
    )
    # Rounded before it is compared with the threshold: the stored value is the one that decided.
    confidence = round(max(0.0, min(1.0, confidence)), 3)
    support = _support_reasons(best)
    if not present:
        # "neuer Ordner „…“" below says it with the folder's name.
        support = [reason for reason in support if not reason.startswith("neuer Ordner")]
    reasons = support + notes

    # 8. Mode decides what happens below the threshold.
    if confidence >= config.threshold:
        return AutoDecision("file", request, confidence, False, sources, reasons[:MAX_REASONS])
    if config.mode == "always":
        return AutoDecision("file", request, confidence, True, sources,
                            [_percent_label(confidence), *reasons][:MAX_REASONS])
    return hold(_percent_label(confidence), confidence=confidence, sources=sources, more=reasons)


__all__ = [
    "AutoDecision",
    "EXPLICIT_SOURCES",
    "SOURCE_LABELS",
    "SOURCE_WEIGHTS",
    "confidence_percent",
    "decide",
    "describe_sources",
    "source_allowed",
]
