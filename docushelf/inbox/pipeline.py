"""Processing pipeline for inbox items.

:meth:`Pipeline.process` takes an item in ``eingang/arbeit`` through the stages
of :data:`~docushelf.inbox.items.STAGES`::

    received → normalized → deblanked → ocr_done → analyzed → ready

Each stage reads only what earlier stages produced, writes its own result
(intermediate files in ``work/``), and is recorded in ``item.json`` before the
next one starts. An interrupted run therefore continues with the stage that did
not finish; if an intermediate file has gone missing, processing falls back to
the stage that produces it. The originals in ``original/`` are never modified.

Finished items move to ``eingang/bereit``, failed ones to ``eingang/fehler``
together with the error. Every step leaves a German line in the item log.

Protected PDFs (digitally signed, or encrypted with a user password; see
:func:`docushelf.pdf.normalize.pdf_protection`) must never be rewritten. A
single such PDF passes through byte for byte: blank page removal and OCR are
skipped, text that cannot be read counts as no text, and
``item.context["pdf_protection"]`` says ``"signed"`` or ``"encrypted"``.

Moves between the inbox states and changes to items in ``bereit`` hold
``services.archive_lock``, like filing and deleting do in
:mod:`docushelf.archive.ops`, so they never interleave with a filing
transaction. The long-running stages themselves hold no lock.

Automatic filing (``settings.auto_file``, off by default): when an item from
an eligible source reaches ``bereit``, an ``auto_file`` job follows – right
away, or after the language model's answer (or failure) when the model is
enabled and ``wait_for_llm`` is set. :meth:`Pipeline.auto_file` asks
:func:`docushelf.suggest.auto.decide` and either files the item as user
"automatisch" or leaves it in ``bereit`` with the reason in
``item.context["auto"]``. A failed automatic filing is never retried on its
own, and an item a person retried or recomputed by hand
(``options["no_auto"]``) is never filed automatically afterwards.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import shutil
import stat
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import quote

from ..errors import Conflict, DocuShelfError, NotFound, ProcessingError
from ..models import Analysis, FilingRequest
from ..pdf.text import extract_pages, page_count
from ..util import fsops
from ..util.ids import is_item_id
from ..util.names import OWN_TEMP_PREFIX, casefold_key, check_relative
from ..util.timeutil import format_date_de, now_iso, parse_iso_datetime
from .items import STAGES, InboxItem

log = logging.getLogger(__name__)

# German names of the stages, used in item logs and error messages.
STAGE_LABELS = {
    "received": "Eingang",
    "normalized": "Umwandlung in PDF",
    "deblanked": "Leerseitenerkennung",
    "ocr_done": "Texterkennung (OCR)",
    "analyzed": "Analyse",
    "ready": "Ablagevorschläge",
}

# The file each stage reads, and the stage to fall back to when it is missing.
_STAGE_INPUT = {
    "deblanked": ("work/normalized.pdf", "received"),
    "ocr_done": ("work/deblanked.pdf", "normalized"),
    "analyzed": ("document.pdf", "deblanked"),
    "ready": ("text.json", "ocr_done"),
}

# States of ``item.filing`` that mean a filing transaction has not finished yet.
_FILING_UNFINISHED = ("publishing", "published")

# Key in item.context for protected PDFs, and what the item log says about them.
PROTECTION_KEY = "pdf_protection"
_PROTECTION_NOTES = {
    "signed": "PDF ist digital signiert",
    "encrypted": "PDF ist passwortgeschützt",
}

NOTIFY_TITLE = "DocuShelf: Verarbeitung fehlgeschlagen"

# Automatic filing: job kind, the user recorded for its filings, and the key in
# the payload of an ``llm_suggest`` job whose end (success or failure) queues it.
AUTO_JOB = "auto_file"
AUTO_USER = "automatisch"
AUTO_AFTER_LLM = "auto_file"
AUTO_MODES = ("off", "rules", "confident", "always")
_AUTO_MODE_ALIASES = {"aus": "off", "regeln": "rules", "sicher": "confident", "immer": "always"}


# ---------------------------------------------------------------------------
# Collaborators, imported lazily (they pull in heavy libraries and are
# implemented in their own modules).


def _normalize_to_pdf(files: list[Path], out: Path) -> int:
    from ..pdf.normalize import normalize_to_pdf

    return normalize_to_pdf(files, out)


def _detect_blank_pages(pdf: Path, *, threshold: float, text_pages: list[str] | None) -> list[int]:
    from ..pdf.normalize import detect_blank_pages

    return detect_blank_pages(pdf, threshold=threshold, text_pages=text_pages)


def _remove_pages(src: Path, dst: Path, pages: list[int]) -> None:
    from ..pdf.normalize import remove_pages

    remove_pages(src, dst, pages)


def _pdf_protection(path: Path) -> str | None:
    from ..pdf.normalize import pdf_protection

    return pdf_protection(path)


def _run_ocr(src: Path, dst: Path, settings: Any, *, log: Any = None) -> Any:
    from ..pdf.ocr import run_ocr

    return run_ocr(src, dst, settings, log=log)


def _analyze(pages: list[str], context: dict[str, Any], *, known_senders: list[str] | None) -> Analysis:
    from ..analysis.extract import analyze

    return analyze(pages, context, known_senders=known_senders)


def _decide(item: InboxItem, *, settings: Any, folder_exists: Callable[[str], bool]) -> Any:
    """The automatic filing decision for a ready item (``suggest.auto.decide``)."""
    from ..suggest.auto import decide

    return decide(item, settings=settings, folder_exists=folder_exists)


def _source_allowed(source: str, sources: Sequence[str]) -> bool:
    """Whether items from ``source`` may be filed automatically. Without the
    decision module nothing is filed automatically, so nothing is eligible."""
    try:
        from ..suggest.auto import source_allowed
    except ImportError:
        return False
    return bool(source_allowed(source, sources))


def _describe_sources(sources: Sequence[str]) -> list[str]:
    """German labels of the signals behind a decision ("Ähnlichkeit", "KI" …)."""
    try:
        from ..suggest.auto import describe_sources
    except ImportError:
        return [str(source) for source in sources]
    return [str(label) for label in describe_sources(list(sources))]


def _decision_class() -> Any:
    try:
        from ..suggest.auto import AutoDecision
    except ImportError:
        return _HeldDecision
    return AutoDecision


@dataclass
class _HeldDecision:
    """Stand-in for ``suggest.auto.AutoDecision`` while that module is not available."""

    action: str = "hold"
    request: FilingRequest | None = None
    confidence: float = 0.0
    review: bool = False
    sources: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Small helpers


def error_message(exc: BaseException) -> str:
    """A German, user-facing description of an exception."""
    if isinstance(exc, DocuShelfError):
        text = str(exc).strip() or "Verarbeitung fehlgeschlagen"
    elif isinstance(exc, PermissionError):
        text = f"Keine Berechtigung: {exc.filename or exc}"
    elif isinstance(exc, FileNotFoundError):
        text = f"Datei nicht gefunden: {exc.filename or exc}"
    elif isinstance(exc, OSError):
        text = f"Dateisystemfehler: {exc.strerror or exc}"
    elif isinstance(exc, MemoryError):
        text = "Nicht genügend Arbeitsspeicher"
    else:
        detail = str(exc).strip()
        text = f"Unerwarteter Fehler ({type(exc).__name__})" + (f": {detail}" if detail else "")
    return text if len(text) <= 1000 else text[:997] + "…"


def _count(number: int, singular: str, plural: str) -> str:
    return f"{number} {singular if number == 1 else plural}"


def _seconds(value: float) -> str:
    if value < 10:
        return f"{value:.1f} s".replace(".", ",")
    return f"{value:.0f} s"


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _copy_file(src: Path, dst: Path) -> None:
    """Replace ``dst`` atomically with a durable copy of ``src``."""
    tmp = fsops.temp_name_for(dst)
    fsops.copy_file_fsync(src, tmp)
    try:
        os.replace(tmp, dst)
    except BaseException:
        _unlink(tmp)
        raise


def _suggestion_dict(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if hasattr(value, "to_dict"):
        return dict(value.to_dict())
    if isinstance(value, dict):
        return dict(value)
    return None


def _order_key(received_at: str | None, item_id: str) -> tuple[float, str]:
    parsed: datetime | None = parse_iso_datetime(received_at)
    return (parsed.timestamp() if parsed else float("inf"), item_id)


@dataclass(frozen=True)
class _ItemSummary:
    id: str
    state: str
    origin_hash: str
    order: tuple[float, str]
    title: str


class _InboxSummaries:
    """Origin hashes of inbox items for duplicate checks, cached by item.json signature."""

    def __init__(self, layout: Any):
        self._layout = layout
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[tuple[int, int], _ItemSummary]] = {}

    def list(self, states: tuple[str, ...]) -> list[_ItemSummary]:
        result: list[_ItemSummary] = []
        with self._lock:
            for state in states:
                base = self._layout.state_dir(state)
                try:
                    names = os.listdir(base)
                except FileNotFoundError:
                    continue
                seen: set[str] = set()
                for name in names:
                    if not is_item_id(name):
                        continue
                    path = base / name / "item.json"
                    try:
                        st = os.stat(path)
                    except OSError:
                        continue
                    key = str(path)
                    seen.add(key)
                    signature = (st.st_size, st.st_mtime_ns)
                    cached = self._cache.get(key)
                    if cached is not None and cached[0] == signature:
                        result.append(cached[1])
                        continue
                    try:
                        item = InboxItem.from_dict(fsops.read_json(path), directory=path.parent, state=state)
                    except Exception:
                        continue
                    summary = _ItemSummary(
                        id=item.id,
                        state=state,
                        origin_hash=item.origin_hash,
                        order=_order_key(item.received_at, item.id),
                        title=item.display_name,
                    )
                    self._cache[key] = (signature, summary)
                    result.append(summary)
                prefix = str(base) + os.sep
                for key in [k for k in self._cache if k.startswith(prefix) and k not in seen]:
                    del self._cache[key]
        return result


@dataclass
class _Run:
    """Results passed from one stage to the next within a single run."""

    pages: list[str] | None = None
    analysis: Analysis | None = None


# ---------------------------------------------------------------------------


class Pipeline:
    def __init__(self, services: Any):
        self.services = services
        self._summaries = _InboxSummaries(services.layout)
        # Items being processed right now. The job queue never runs two jobs for
        # one item, but process() may also be called directly.
        self._active: set[str] = set()
        self._active_lock = threading.Lock()
        self._handlers = {
            "normalized": self._stage_normalize,
            "deblanked": self._stage_deblank,
            "ocr_done": self._stage_ocr,
            "analyzed": self._stage_analyze,
            "ready": self._stage_suggest,
        }

    @property
    def settings(self) -> Any:
        return self.services.settings

    @property
    def inbox(self) -> Any:
        return self.services.inbox

    @property
    def index(self) -> Any:
        return self.services.index

    @property
    def _lock(self) -> Any:
        return self.services.archive_lock

    # -- processing --------------------------------------------------------------------
    def process(self, item_id: str) -> InboxItem:
        """Run the remaining stages of an item in ``arbeit`` and move it to ``bereit``.

        A transient ``ProcessingError`` propagates (the caller retries later);
        any other error moves the item to ``fehler``. Raises ``NotFound`` if the
        item is not (or no longer) in ``arbeit`` and ``Conflict`` if it is being
        processed already.
        """
        with self._active_lock:
            if item_id in self._active:
                raise Conflict("Das Dokument wird bereits verarbeitet")
            self._active.add(item_id)
        try:
            return self._process(item_id)
        finally:
            with self._active_lock:
                self._active.discard(item_id)

    def _process(self, item_id: str) -> InboxItem:
        item = self.inbox.load(item_id, states=("arbeit",))
        if item.stage not in STAGES:
            self._log(item, f"Unbekannte Verarbeitungsstufe „{item.stage}“ – Verarbeitung beginnt von vorn")
            item.stage = "received"
        item.attempts += 1
        item.error = None
        if item.stage == "received":
            self._log(item, "Verarbeitung gestartet")
        elif item.stage != "ready":
            self._log(item, f"Verarbeitung wird nach Stufe „{STAGE_LABELS[item.stage]}“ fortgesetzt")
        self._save(item)
        run = _Run()
        try:
            self._run_stages(item, run)
            self._finish(item)
        except NotFound:
            raise
        except ProcessingError as exc:
            if not item.dir.is_dir():
                raise NotFound(item_id) from exc
            if exc.transient:
                label = STAGE_LABELS.get(self._failed_stage(item, exc), "Verarbeitung")
                self._log(item, f"Vorübergehender Fehler bei {label}: {error_message(exc)}")
                raise
            return self.fail(item_id, exc)
        except Exception as exc:
            if not item.dir.is_dir():
                raise NotFound(item_id) from exc
            log.exception("processing of inbox item %s failed", item_id)
            return self.fail(item_id, exc)
        return item

    def _run_stages(self, item: InboxItem, run: _Run) -> None:
        # Each iteration either completes a stage or falls back to an earlier one;
        # the bound only protects against a stage that never produces its output.
        for _ in range(len(STAGES) * 4):
            if item.stage == "ready":
                return
            target = STAGES[STAGES.index(item.stage) + 1]
            needed = _STAGE_INPUT.get(target)
            if needed is not None and not (item.dir / needed[0]).is_file():
                if not item.dir.is_dir():
                    raise NotFound(item.id)
                self._log(item, f"Zwischenergebnis {needed[0]} fehlt – {STAGE_LABELS[target]} wird neu vorbereitet")
                item.stage = needed[1]
                continue
            if target == "ready" and item.analysis is None:
                item.stage = "ocr_done"
                continue
            self._handlers[target](item, run)
            item.stage = target
            self._save(item)
        raise ProcessingError("Die Verarbeitung kommt nicht voran", stage=item.stage)

    def _finish(self, item: InboxItem) -> None:
        with self._lock:
            shutil.rmtree(item.work_dir, ignore_errors=True)
            self.inbox.move(item, "bereit")
            item.add_history("processed", pages=item.page_count, suggestions=len(item.suggestions))
            self._log(item, "Bereit zur Ablage")
            self._save(item)
        self._queue_follow_ups(item)

    def _queue_follow_ups(self, item: InboxItem) -> None:
        """Queue what follows processing: the language model and/or the automatic filing.

        With the model enabled and ``wait_for_llm`` set, the ``llm_suggest``
        job carries the request to file automatically afterwards (the worker
        queues ``auto_file`` when it ends, successfully or not). Otherwise
        ``auto_file`` is queued first, so a single worker thread decides
        before it spends time on the model.
        """
        llm = bool(self.settings.llm)
        auto = self._auto_eligible(item)
        wait = auto and llm and bool(getattr(_auto_config(self.settings), "wait_for_llm", True))
        queued = False
        if auto and not wait:
            queued = self._enqueue(AUTO_JOB, item.id) or queued
        if llm:
            queued = self._enqueue("llm_suggest", item.id, {AUTO_AFTER_LLM: True} if wait else None) or queued
        if queued:
            self._kick_worker()

    def _enqueue(self, kind: str, item_id: str, payload: dict[str, Any] | None = None) -> bool:
        try:
            return bool(self.index.enqueue(kind, item_id, payload))
        except Exception:
            log.exception("cannot queue the %s job for %s", kind, item_id)
            return False

    # -- stages ----------------------------------------------------------------------------
    def _stage_normalize(self, item: InboxItem, run: _Run) -> None:
        originals = item.original_paths()
        if not originals:
            raise ProcessingError("Keine Originaldatei vorhanden", stage="normalized")
        missing = [path.name for path in originals if not path.is_file()]
        if missing:
            raise ProcessingError(f"Originaldatei fehlt: {', '.join(missing)}", stage="normalized")
        out = self._work_dir(item) / "normalized.pdf"
        _unlink(out)
        started = time.monotonic()
        pages = _normalize_to_pdf(originals, out)
        if not out.is_file():
            raise ProcessingError("Bei der Umwandlung ist keine PDF-Datei entstanden", stage="normalized")
        # Only a single PDF is taken over as it is; merged documents are new files.
        protection = _pdf_protection(out) if len(originals) == 1 else None
        if protection:
            item.context[PROTECTION_KEY] = protection
        else:
            item.context.pop(PROTECTION_KEY, None)
        item.pages_removed = []
        if protection == "encrypted":
            # Without the password not even the pages can be counted.
            item.page_count = max(0, int(pages or 0))
            self._log(
                item,
                "Passwortgeschütztes PDF unverändert übernommen – ohne Passwort sind Vorschau, "
                "Leerseitenerkennung und Texterkennung nicht möglich",
            )
            return
        item.page_count = int(pages) if pages else page_count(out)
        if protection == "signed":
            self._log(
                item,
                "Digital signiertes PDF unverändert übernommen, damit die Signatur gültig bleibt "
                f"({_count(item.page_count, 'Seite', 'Seiten')})",
            )
            return
        self._log(
            item,
            f"PDF erstellt: {_count(item.page_count, 'Seite', 'Seiten')} aus "
            f"{_count(len(originals), 'Datei', 'Dateien')} ({_seconds(time.monotonic() - started)})",
        )

    def _stage_deblank(self, item: InboxItem, run: _Run) -> None:
        work = self._work_dir(item)
        src = work / "normalized.pdf"
        dst = work / "deblanked.pdf"
        _unlink(dst)
        item.pages_removed = []
        protection = _protection(item)
        if protection:
            _copy_file(src, dst)
            self._log(item, f"Leerseitenerkennung übersprungen ({_PROTECTION_NOTES[protection]})")
            return
        if not self.settings.blank_page_removal:
            _copy_file(src, dst)
            self._log(item, "Leerseitenerkennung übersprungen (ausgeschaltet)")
            return
        if item.options.get("skip_blank_removal"):
            _copy_file(src, dst)
            self._log(item, "Leerseitenerkennung übersprungen (für dieses Dokument ausgeschaltet)")
            return
        total = item.page_count or page_count(src)
        started = time.monotonic()
        try:
            try:
                text_pages: list[str] | None = extract_pages(src)
            except Exception:
                text_pages = None
            found = _detect_blank_pages(src, threshold=self.settings.blank_page_threshold, text_pages=text_pages)
            blank = sorted({int(page) for page in found if 1 <= int(page) <= total})
            if blank and len(blank) < total:
                _remove_pages(src, dst, blank)
        except ProcessingError as exc:
            if exc.transient:
                raise
            self._blank_detection_failed(item, src, dst, exc)
            return
        except Exception as exc:
            log.warning("blank page detection for %s failed", item.id, exc_info=True)
            self._blank_detection_failed(item, src, dst, exc)
            return
        if not blank:
            _copy_file(src, dst)
            self._log(item, f"Keine Leerseiten gefunden ({_seconds(time.monotonic() - started)})")
            return
        if len(blank) >= total:
            _copy_file(src, dst)
            self._log(item, "Alle Seiten wirken leer – es werden keine Seiten entfernt")
            return
        if not dst.is_file():
            raise ProcessingError("Beim Entfernen der Leerseiten ist keine PDF-Datei entstanden", stage="deblanked")
        item.pages_removed = blank
        item.page_count = total - len(blank)
        self._log(
            item,
            f"{_count(len(blank), 'Leerseite', 'Leerseiten')} entfernt (Seite {', '.join(map(str, blank))}), "
            f"{_count(item.page_count, 'Seite verbleibt', 'Seiten verbleiben')}",
        )

    def _blank_detection_failed(self, item: InboxItem, src: Path, dst: Path, exc: BaseException) -> None:
        _unlink(dst)
        _copy_file(src, dst)
        item.pages_removed = []
        self._log(item, f"Leerseitenerkennung fehlgeschlagen ({error_message(exc)}) – alle Seiten bleiben erhalten")

    def _stage_ocr(self, item: InboxItem, run: _Run) -> None:
        work = self._work_dir(item)
        src = work / "deblanked.pdf"
        protection = _protection(item)
        if protection:
            _copy_file(src, item.document_path)
            self._log(item, f"Texterkennung übersprungen ({_PROTECTION_NOTES[protection]})")
            return
        if item.options.get("skip_ocr"):
            _copy_file(src, item.document_path)
            self._log(item, "Texterkennung übersprungen (für dieses Dokument ausgeschaltet)")
            return
        out = work / "ocr.pdf"
        _unlink(out)
        started = time.monotonic()
        result = _run_ocr(src, out, self.settings, log=lambda line: self._log(item, str(line)))
        produced = Path(getattr(result, "output", None) or out)
        if not produced.is_file():
            raise ProcessingError("Die Texterkennung hat keine PDF-Datei erzeugt", stage="ocr_done")
        if produced == out:
            os.replace(out, item.document_path)
        else:
            _copy_file(produced, item.document_path)
        duration = getattr(result, "duration_s", None)
        if duration is None:
            duration = time.monotonic() - started
        for warning in getattr(result, "warnings", None) or []:
            self._log(item, f"Hinweis der Texterkennung: {warning}")
        if getattr(result, "performed", True):
            self._log(
                item, f"OCR abgeschlossen ({_count(item.page_count, 'Seite', 'Seiten')}, {_seconds(float(duration))})"
            )
        else:
            reason = getattr(result, "skipped_reason", None) or "keine Angabe"
            self._log(item, f"OCR übersprungen: {reason}")

    def _stage_analyze(self, item: InboxItem, run: _Run) -> None:
        pages, unreadable = self._read_item_pages(item)
        self.inbox.write_text(item, pages)
        if not unreadable:
            item.page_count = len(pages)
        chars = sum(len(page) for page in pages)
        if unreadable:
            self._log(item, f"Text kann nicht gelesen werden ({_PROTECTION_NOTES[unreadable]})")
        elif chars:
            self._log(
                item,
                f"Text gelesen: {chars:,} Zeichen auf {_count(len(pages), 'Seite', 'Seiten')}".replace(",", "."),
            )
        else:
            self._log(item, "Kein Text im Dokument gefunden")
        analysis = self._analyze_pages(item, pages)
        item.analysis = analysis.to_dict()
        self._log(item, _describe_analysis(analysis))
        item.duplicate_of = self._find_duplicate(item)
        if item.duplicate_of:
            self._log(item, _describe_duplicate(item.duplicate_of))
        run.pages = pages
        run.analysis = analysis

    def _stage_suggest(self, item: InboxItem, run: _Run) -> None:
        pages = run.pages if run.pages is not None else self.inbox.read_text(item)
        analysis = run.analysis or Analysis.from_dict(item.analysis)
        item.suggestions = self._compute_suggestions(item, pages, analysis)
        self._log(item, _describe_suggestions(item.suggestions))

    # -- stage helpers ------------------------------------------------------------------------
    def _work_dir(self, item: InboxItem) -> Path:
        # Never with parents=True: an item directory that vanished must not reappear.
        try:
            item.work_dir.mkdir(exist_ok=True)
        except FileNotFoundError:
            raise NotFound(item.id) from None
        return item.work_dir

    def _read_pages(self, path: Path) -> list[str]:
        try:
            return extract_pages(path)
        except FileNotFoundError:
            raise
        except Exception as exc:
            raise ProcessingError(f"Das PDF kann nicht gelesen werden: {exc}", stage="analyzed") from exc

    def _read_item_pages(self, item: InboxItem) -> tuple[list[str], str | None]:
        """Text of the item's document.pdf.

        A protected PDF whose text cannot be read (no password) has no text;
        the second value then names the protection. For other PDFs an
        unreadable file is an error.
        """
        try:
            return self._read_pages(item.document_path), None
        except ProcessingError:
            protection = _protection(item)
            if not protection:
                raise
            log.info("text of protected inbox item %s cannot be read", item.id, exc_info=True)
            return [], protection

    def _analyze_pages(self, item: InboxItem, pages: list[str]) -> Analysis:
        known: list[str] | None = None
        suggester = getattr(self.services, "suggester", None)
        if suggester is not None:
            try:
                known = list(suggester.known_senders())
            except ProcessingError as exc:
                if exc.transient:
                    raise
                log.warning("known senders unavailable", exc_info=True)
            except Exception:
                log.warning("known senders unavailable", exc_info=True)
        try:
            context = dict(item.context)
            if self.settings.owner_names and not context.get("owner_names"):
                context["owner_names"] = list(self.settings.owner_names)
            result = _analyze(pages, context, known_senders=known)
        except ProcessingError as exc:
            if exc.transient:
                raise
            self._log(item, f"Analyse fehlgeschlagen: {error_message(exc)}")
            return Analysis()
        except Exception as exc:
            log.exception("analysis of inbox item %s failed", item.id)
            self._log(item, f"Analyse fehlgeschlagen: {error_message(exc)}")
            return Analysis()
        if isinstance(result, dict):
            return Analysis.from_dict(result)
        return result if isinstance(result, Analysis) else Analysis()

    def _compute_suggestions(self, item: InboxItem, pages: list[str], analysis: Analysis) -> list[dict[str, Any]]:
        suggester = getattr(self.services, "suggester", None)
        if suggester is None:
            return []
        try:
            result = suggester.suggest(item, pages, analysis)
        except ProcessingError as exc:
            if exc.transient:
                raise
            self._log(item, f"Vorschläge konnten nicht berechnet werden: {error_message(exc)}")
            return []
        except Exception as exc:
            log.exception("suggestions for inbox item %s failed", item.id)
            self._log(item, f"Vorschläge konnten nicht berechnet werden: {error_message(exc)}")
            return []
        suggestions = []
        for value in result or []:
            data = _suggestion_dict(value)
            if data is not None:
                suggestions.append(data)
        return suggestions

    def _find_duplicate(self, item: InboxItem) -> dict[str, Any] | None:
        """An archived document or an earlier inbox item with the same origin hash."""
        digest = item.origin_hash
        if not digest:
            return None
        try:
            records = self.index.by_origin_hash(digest)
        except Exception:
            log.warning("duplicate lookup in the index failed", exc_info=True)
            records = []
        if records:
            record = sorted(records, key=lambda r: r.path)[0]
            return {"kind": "archive", "id": record.id, "path": record.path, "title": record.title}
        mine = _order_key(item.received_at, item.id)
        earlier = [
            other
            for other in self._summaries.list(("arbeit", "bereit"))
            if other.id != item.id and other.origin_hash == digest and other.order < mine
        ]
        if not earlier:
            return None
        first = min(earlier, key=lambda other: other.order)
        return {"kind": "inbox", "id": first.id, "title": first.title, "state": first.state}

    def _failed_stage(self, item: InboxItem, exc: BaseException) -> str:
        stage = getattr(exc, "stage", None)
        if stage in STAGES:
            return stage
        if item.stage not in STAGES:
            return "normalized"
        position = STAGES.index(item.stage)
        return STAGES[min(position + 1, len(STAGES) - 1)]

    # -- failures and retries -------------------------------------------------------------------
    def fail(self, item_id: str, exc: BaseException) -> InboxItem:
        """Record the error in item.json, move the item to ``fehler`` and send a notification."""
        message = error_message(exc)
        with self._lock:
            item = self.inbox.load(item_id, states=("arbeit", "bereit", "fehler"))
            stage = self._failed_stage(item, exc)
            label = STAGE_LABELS.get(stage, stage)
            item.error = {"stage": stage, "label": label, "message": message, "at": now_iso()}
            if isinstance(exc, ProcessingError) and exc.transient:
                item.error["transient"] = True
            item.add_history("failed", stage=stage, message=message)
            self._log(item, f"Fehler bei {label}: {message}")
            # Saved before the move: after a crash in between, the item is simply processed again.
            self._save(item)
            self.inbox.move(item, "fehler")
        log.warning("inbox item %s failed at %s: %s", item_id, stage, message)
        try:
            self.services.notifier.send(NOTIFY_TITLE, f"„{item.display_name}“ ({item.source}): {label} – {message}")
        except Exception:
            log.warning("failure notification could not be sent", exc_info=True)
        return item

    def retry(
        self,
        item_id: str,
        *,
        skip_blank_removal: bool | None = None,
        skip_ocr: bool | None = None,
    ) -> InboxItem:
        """Process an item from ``fehler`` or ``bereit`` again from the start.

        Retrying is a person's decision: while automatic filing is on, the
        item is marked ``options["no_auto"]`` and waits for that person
        afterwards instead of being filed automatically.
        """
        with self._lock:
            item = self.inbox.find(item_id)
            if item is None:
                raise NotFound(item_id)
            if item.state == "erledigt":
                raise Conflict("Das Dokument wurde bereits abgelegt")
            if item.state == "arbeit":
                raise Conflict("Das Dokument wird gerade verarbeitet")
            if _filing_unfinished(item):
                raise Conflict("Die Ablage dieses Dokuments ist nicht abgeschlossen")
            previous_state = item.state
            # Moved first: if anything below is interrupted, the item is processed
            # again from its recorded stage instead of being stuck.
            self.inbox.move(item, "arbeit")
            if skip_blank_removal is not None:
                item.options["skip_blank_removal"] = bool(skip_blank_removal)
            if skip_ocr is not None:
                item.options["skip_ocr"] = bool(skip_ocr)
            marked = self._mark_manual(item)
            # A decision about the previous result says nothing about the next one.
            item.context.pop("auto", None)
            item.stage = "received"
            item.error = None
            item.analysis = None
            item.suggestions = []
            item.duplicate_of = None
            item.page_count = 0
            item.pages_removed = []
            item.add_history(
                "retry",
                previous_state=previous_state,
                skip_blank_removal=item.options.get("skip_blank_removal") or None,
                skip_ocr=item.options.get("skip_ocr") or None,
            )
            flags = []
            if item.options.get("skip_blank_removal"):
                flags.append("ohne Leerseitenerkennung")
            if item.options.get("skip_ocr"):
                flags.append("ohne OCR")
            self._log(item, "Erneute Verarbeitung angestoßen" + (f" ({', '.join(flags)})" if flags else ""))
            if marked:
                self._log(item, "Wird danach nicht automatisch abgelegt (von Hand neu verarbeitet)")
            self._save(item)
            self._remove_derived(item)
        try:
            self.index.enqueue("process", item.id)
        except Exception:
            log.exception("cannot queue inbox item %s; it is picked up again on the next start", item.id)
        self._kick_worker()
        return item

    def _remove_derived(self, item: InboxItem) -> None:
        for path in (item.document_path, item.text_path):
            try:
                _unlink(path)
            except OSError:
                log.warning("cannot remove %s", path, exc_info=True)
        shutil.rmtree(item.work_dir, ignore_errors=True)
        try:
            for stray in item.dir.glob(OWN_TEMP_PREFIX + "*.part"):
                _unlink(stray)
        except OSError:
            pass

    # -- suggestions for ready items --------------------------------------------------------------
    def refresh_suggestions(self, item_id: str, *, manual: bool = True) -> InboxItem:
        """Recompute analysis, duplicate check and suggestions of an item in ``bereit``.

        Suggestions of the language model are kept (they are expensive to compute).
        ``manual`` means a person asked for it (web page, API); while
        automatic filing is on, such an item is marked ``options["no_auto"]``,
        so an automatic filing still pending never overrides that person.
        Queued refreshes (``refresh`` jobs) pass ``manual=False``.
        """
        item = self._load_ready(item_id)
        pages = self.inbox.read_text(item)
        extracted = False
        if not pages and item.document_path.is_file():
            pages, _unreadable = self._read_item_pages(item)
            extracted = True
        analysis = self._analyze_pages(item, pages)
        duplicate = self._find_duplicate(item)
        suggestions = self._compute_suggestions(item, pages, analysis)
        with self._lock:
            fresh = self._load_ready(item_id)
            if _filing_unfinished(fresh):
                raise Conflict("Das Dokument wird gerade abgelegt")
            if extracted:
                self.inbox.write_text(fresh, pages)
            kept = [s for s in fresh.suggestions if s.get("source") == "llm"]
            fresh.analysis = analysis.to_dict()
            fresh.duplicate_of = duplicate
            fresh.suggestions = suggestions + kept
            fresh.add_history("refresh", suggestions=len(suggestions))
            self._log(fresh, "Vorschläge neu berechnet: " + _describe_suggestions(suggestions))
            if manual and self._mark_manual(fresh):
                self._log(fresh, "Wird nicht mehr automatisch abgelegt (von Hand bearbeitet)")
            self._save(fresh)
        return fresh

    def queue_refresh(self, *, only_stale: bool = False) -> int:
        """Queue a suggestion refresh for the items in ``bereit``; returns how many were queued.

        With ``only_stale`` only items whose first suggestion points to a folder
        that does not exist in the index right now, whatever the suggestion
        said when it was made: a folder renamed or moved away (in the app or
        in the Finder) as well as a proposed new folder, whose suggestion may
        have to follow such a change too. Items whose filing has not finished
        are left alone.
        """
        index = self.services.index
        known: set[str] | None = None
        if only_stale:
            try:
                known = {_folder_key(path) for path in index.list_folders()}
            except Exception:
                log.warning("cannot list folders to find stale suggestions", exc_info=True)
                return 0
        queued = 0
        for item in self.inbox.list("bereit"):
            if _filing_unfinished(item):
                continue
            if known is not None:
                folder = _folder_key((item.top_suggestion or {}).get("folder"))
                if not folder or folder in known:
                    continue
            if index.enqueue("refresh", item.id):
                queued += 1
        if queued:
            worker = getattr(self.services, "worker", None)
            if worker is not None:
                try:
                    worker.kick()
                except Exception:
                    log.warning("cannot wake the worker", exc_info=True)
        return queued

    def run_llm(self, item_id: str) -> InboxItem:
        """Ask the local language model and append its suggestion (source "llm")."""
        item = self._load_ready(item_id)
        suggester = getattr(self.services, "suggester", None)
        if not self.settings.llm or suggester is None:
            return item
        pages = self.inbox.read_text(item)
        analysis = Analysis.from_dict(item.analysis)
        started = time.monotonic()
        notes: list[str] = []
        try:
            answer = suggester.llm_suggest(item, pages, analysis, log=notes.append)
        except TypeError:  # suggesters without the log keyword
            answer = suggester.llm_suggest(item, pages, analysis)
        data = _suggestion_dict(answer)
        elapsed = _seconds(time.monotonic() - started)
        if data is not None:
            data["source"] = "llm"
        with self._lock:
            # The model may take minutes; work on the current state of the item.
            fresh = self._load_ready(item_id)
            if _filing_unfinished(fresh):
                return fresh
            fresh.suggestions = [s for s in fresh.suggestions if s.get("source") != "llm"]
            if data is not None:
                fresh.suggestions.append(data)
                folder = data.get("folder") or "–"
                marker = " (neuer Ordner)" if data.get("new_folder") else ""
                self._log(fresh, f"KI-Vorschlag: „{folder}“{marker} ({elapsed})")
            else:
                reason = f": {notes[-1]}" if notes else ""
                self._log(fresh, f"Kein KI-Vorschlag erhalten ({elapsed}){reason}")
            fresh.add_history("llm", folder=(data or {}).get("folder"))
            self._save(fresh)
        return fresh

    def _load_ready(self, item_id: str) -> InboxItem:
        item = self.inbox.find(item_id)
        if item is None:
            raise NotFound(item_id)
        if item.state == "erledigt":
            raise Conflict("Das Dokument wurde bereits abgelegt")
        if item.state != "bereit":
            raise Conflict("Vorschläge gibt es nur für fertig verarbeitete Dokumente im Eingang")
        return item

    # -- automatic filing -----------------------------------------------------------------------------
    def auto_file(self, item_id: str) -> Any:
        """File a ready item without asking, if the decision rules allow it; returns the decision.

        The item is loaded and decided on under the archive lock, so no person
        can change it in between. ``file``: filed as user "automatisch"; the
        decision goes into the filing journal, the audit log (``auto_file``)
        and ``item.context["auto"]`` (see :meth:`ArchiveOps.file_item`), and a
        notification is sent if that is wanted. ``hold``: the item stays in
        ``bereit`` and ``item.context["auto"]`` plus the item log say why. A
        filing that fails is a ``hold`` as well and is not retried on its own:
        errors of the decision or the filing are never raised. Raises NotFound
        or Conflict if the item is not (or no longer) waiting in ``bereit``.
        """
        config = _auto_config(self.settings)
        with self._lock:
            item = self._load_ready(item_id)
            if _filing_unfinished(item):
                raise Conflict("Das Dokument wird gerade abgelegt")
            if _auto_mode(config) == "off":
                # Switched off after the job was queued: the item simply waits, untouched.
                return _held(["Automatische Ablage ist ausgeschaltet"])
            decision = self._auto_decision(item)
            if getattr(decision, "action", None) == "file" and getattr(decision, "request", None) is not None:
                veto = _veto(item)
                if veto:
                    decision = _held([veto], like=decision)
            if getattr(decision, "action", None) != "file" or getattr(decision, "request", None) is None:
                self._record_hold(item, decision)
                return decision
            self._log(item, _describe_auto_filing(decision))
            try:
                record = self.services.ops.file_item(
                    item_id, decision.request, user=AUTO_USER, auto=_filing_record(decision)
                )
            except Exception as exc:
                return self._auto_filing_failed(item_id, decision, exc)
        self._notify_auto_filed(record, decision)
        return decision

    def _auto_decision(self, item: InboxItem) -> Any:
        try:
            return _decide(item, settings=self.settings, folder_exists=self._folder_exists)
        except ImportError:
            return _held(["Automatische Ablage ist nicht verfügbar"])
        except Exception as exc:
            log.exception("automatic filing decision for inbox item %s failed", item.id)
            return _held([f"Entscheidung nicht möglich: {error_message(exc)}"])

    def _folder_exists(self, folder: str) -> bool:
        """Whether ``folder`` is an existing archive folder, matched like filing matches it
        (on-disk spelling, case, spacing). Symbolic links do not count."""
        from ..archive.ops import resolve_folder

        layout = self.services.layout
        try:
            rel = check_relative(resolve_folder(layout.archive, str(folder or "")))
            path = layout.archive_path(rel)
            return stat.S_ISDIR(os.lstat(path).st_mode)
        except (DocuShelfError, OSError, ValueError):
            return False

    def _record_hold(self, item: InboxItem, decision: Any) -> None:
        """Note in item.json and the item log why the item was not filed automatically."""
        reasons = _texts(getattr(decision, "reasons", None)) or ["kein Grund angegeben"]
        item.context["auto"] = {
            "decision": "hold",
            "confidence": _confidence(decision),
            "reasons": reasons,
            "at": now_iso(),
        }
        item.add_history("auto_hold", reason=reasons[0])
        self._log(item, "Nicht automatisch abgelegt: " + "; ".join(reasons))
        self._save(item)

    def _auto_filing_failed(self, item_id: str, decision: Any, exc: BaseException) -> Any:
        """The item stays in ``bereit`` with the reason; no automatic second attempt."""
        message = error_message(exc)
        if isinstance(exc, DocuShelfError):
            log.warning("automatic filing of inbox item %s failed: %s", item_id, message)
        else:
            log.error("automatic filing of inbox item %s failed", item_id, exc_info=exc)
        held = _held([f"Ablage fehlgeschlagen: {message}"], like=decision)
        item = self.inbox.find(item_id)
        if item is None or item.state != "bereit" or item.filing:
            # Gone, filed after all, or a journal that recover() completes: nothing to note here.
            return held
        try:
            self._record_hold(item, held)
        except Exception:
            log.exception("cannot record the failed automatic filing of inbox item %s", item_id)
        return held

    def _notify_auto_filed(self, record: Any, decision: Any) -> None:
        """One push notification per automatically filed document (if wanted and configured)."""
        config = _auto_config(self.settings)
        if not getattr(config, "notify", True) or not getattr(self.settings, "notify_url", None):
            return
        review = bool(getattr(decision, "review", False))
        title = getattr(record, "title", None) or getattr(getattr(decision, "request", None), "title", "") or "Dokument"
        message = f"{getattr(record, 'folder', '') or 'Archiv'} · {round(_confidence(decision) * 100)} %"
        if review:
            message += " – bitte prüfen"
        click = None
        base_url = str(getattr(self.settings, "base_url", "") or "").rstrip("/")
        if base_url and getattr(record, "id", None):
            click = f"{base_url}/dokument/{quote(str(record.id), safe='')}"
        try:
            self.services.notifier.send(
                f"Abgelegt: {title}", message, tags="warning" if review else "file_folder", click=click
            )
        except Exception:
            log.warning("notification about an automatic filing could not be sent", exc_info=True)

    def _auto_eligible(self, item: InboxItem) -> bool:
        """Automatic filing is on and items from the item's source may be filed automatically."""
        config = _auto_config(self.settings)
        if _auto_mode(config) == "off":
            return False
        try:
            return _source_allowed(item.source, tuple(getattr(config, "sources", None) or ()))
        except Exception:
            log.exception("cannot check the source of inbox item %s for automatic filing", item.id)
            return False

    def _mark_manual(self, item: InboxItem) -> bool:
        """Mark an item a person worked on, so it is never filed automatically afterwards.

        Only while automatic filing is on (otherwise the item stays exactly as
        it was). Returns True if the mark is new.
        """
        if _auto_mode(_auto_config(self.settings)) == "off" or item.options.get("no_auto"):
            return False
        item.options["no_auto"] = True
        return True

    # -- persistence helpers ----------------------------------------------------------------------
    def _save(self, item: InboxItem) -> None:
        """Save item.json, unless the item directory has been moved or deleted meanwhile.

        ``InboxStore.save`` creates missing parent directories, so saving an
        item that was deleted in the meantime would bring back an empty ghost item.
        """
        if not item.dir.is_dir():
            raise NotFound(item.id)
        self.inbox.save(item)

    def _log(self, item: InboxItem, message: str) -> None:
        if item.dir.is_dir():
            self.inbox.log(item, message)

    def _kick_worker(self) -> None:
        worker = getattr(self.services, "worker", None)
        if worker is None:
            return
        try:
            worker.kick()
        except Exception:
            log.exception("cannot wake the worker")


# ---------------------------------------------------------------------------
# Log texts


def _filing_unfinished(item: InboxItem) -> bool:
    return bool(item.filing) and (item.filing or {}).get("state") in _FILING_UNFINISHED


def _protection(item: InboxItem) -> str | None:
    """"signed" or "encrypted" for a protected PDF (set by the first stage), else None."""
    value = item.context.get(PROTECTION_KEY)
    return value if value in _PROTECTION_NOTES else None


def _folder_key(folder: Any) -> str:
    """Comparison key of a folder path: NFC, case-insensitive, without outer slashes."""
    return casefold_key(str(folder or "").strip().strip("/"))


def _describe_analysis(analysis: Analysis) -> str:
    parts = []
    if analysis.doc_type:
        parts.append(analysis.doc_type)
    if analysis.sender:
        parts.append(f"Absender „{analysis.sender}“")
    if analysis.doc_date:
        parts.append(f"Datum {format_date_de(analysis.doc_date)}")
    if analysis.total_amount:
        parts.append(f"Betrag {analysis.total_amount}")
    if analysis.ibans:
        parts.append(_count(len(analysis.ibans), "IBAN", "IBANs"))
    references = sum(len(values) for values in (analysis.references or {}).values())
    if references:
        parts.append(_count(references, "Referenznummer", "Referenznummern"))
    if not parts:
        return "Analyse: keine Angaben erkannt"
    return "Analyse: " + ", ".join(parts)


def _describe_duplicate(duplicate: dict[str, Any]) -> str:
    if duplicate.get("kind") == "archive":
        return f"Mögliches Duplikat: bereits im Archiv als „{duplicate.get('path')}“"
    return f"Mögliches Duplikat: gleiches Original wie „{duplicate.get('title')}“ im Eingang"


def _describe_suggestions(suggestions: list[dict[str, Any]]) -> str:
    if not suggestions:
        return "Keine Ablagevorschläge gefunden"
    top = suggestions[0]
    folder = top.get("folder") or "–"
    marker = " (neuer Ordner)" if top.get("new_folder") else ""
    count = _count(len(suggestions), "Ablagevorschlag", "Ablagevorschläge")
    return f"{count}, bester: „{folder}“{marker}"


# ---------------------------------------------------------------------------
# Automatic filing helpers


def _auto_config(settings: Any) -> Any:
    """``settings.auto_file``, or None where it does not exist (treated as "off")."""
    return getattr(settings, "auto_file", None)


def _auto_mode(config: Any) -> str:
    """The mode of an AutoFileConfig as one of AUTO_MODES; anything unknown is "off"."""
    if config is None:
        return "off"
    mode = str(getattr(config, "mode", "off") or "off").strip().lower()
    mode = _AUTO_MODE_ALIASES.get(mode, mode)
    return mode if mode in AUTO_MODES else "off"


def _texts(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    try:
        return [text for text in (" ".join(str(value).split()) for value in values) if text]
    except TypeError:
        return []


def _confidence(decision: Any) -> float:
    try:
        value = float(getattr(decision, "confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return round(min(1.0, max(0.0, value)), 3)


def _held(reasons: list[str], *, like: Any = None) -> Any:
    """A "hold" decision with ``reasons``; confidence and sources are taken from ``like``."""
    if like is not None and dataclasses.is_dataclass(like) and not isinstance(like, type):
        try:
            return dataclasses.replace(like, action="hold", request=None, review=False, reasons=list(reasons))
        except (TypeError, ValueError):
            pass
    sources = _texts(getattr(like, "sources", None))
    return _decision_class()(
        action="hold", request=None, confidence=_confidence(like), review=False, sources=sources, reasons=list(reasons)
    )


def _veto(item: InboxItem) -> str | None:
    """What forbids filing ``item`` automatically, whatever a decision says (or None).

    Mirrors the first rule of the decision, as a last line of defence: a
    person's work and possible duplicates are never filed without asking.
    """
    if item.options.get("no_auto"):
        return "von Hand bearbeitet"
    if item.duplicate_of:
        return "mögliches Duplikat"
    return None


def _filing_record(decision: Any) -> dict[str, Any]:
    """What an automatic filing keeps of its decision (journal, audit log, item.json)."""
    return {
        "decision": "file",
        "confidence": _confidence(decision),
        "review": bool(getattr(decision, "review", False)),
        "sources": _texts(getattr(decision, "sources", None)),
        "reasons": _texts(getattr(decision, "reasons", None)),
        "at": now_iso(),
    }


def _describe_auto_filing(decision: Any) -> str:
    request = getattr(decision, "request", None)
    folder = getattr(request, "folder", None) or "Archiv"
    labels = _texts(_safe_source_labels(_texts(getattr(decision, "sources", None))))
    detail = f"{round(_confidence(decision) * 100)} %"
    if labels:
        detail += " – " + ", ".join(labels)
    line = f"Automatische Ablage nach „{folder}“ ({detail})"
    if getattr(decision, "review", False):
        line += ", unsicher – bitte prüfen"
    reasons = _texts(getattr(decision, "reasons", None))
    if reasons:
        line += ". Begründung: " + "; ".join(reasons)
    return line


def _safe_source_labels(sources: list[str]) -> list[str]:
    try:
        return _describe_sources(sources)
    except Exception:
        log.debug("cannot describe the sources %s", sources, exc_info=True)
        return sources
