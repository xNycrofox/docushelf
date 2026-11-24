"""Helpers for the HTML pages: flash messages, form parsing and view models.

Everything here is independent of FastAPI routing so it can be tested on its
own. Texts that reach the browser are German.
"""

from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import threading
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote, urlencode, urlsplit

from starlette.requests import Request
from starlette.responses import RedirectResponse

from ..errors import DocuShelfError, NotFound
from ..models import Analysis, DateCandidate, FilingRequest, Suggestion
from ..util.ids import is_item_id
from ..util.names import casefold_key
from ..util.timeutil import now, now_iso, parse_date, parse_iso_datetime

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Flash messages (kept in the signed session cookie, so they must stay small)

FLASH_KEY = "_flashes"
MAX_FLASHES = 6
MAX_FLASH_CHARS = 400
FLASH_CATEGORIES = ("success", "info", "warning", "error")


def _session(request: Request) -> dict[str, Any] | None:
    if "session" not in request.scope:
        return None
    try:
        return request.session
    except AssertionError:  # pragma: no cover - SessionMiddleware missing
        return None


def flash(
    request: Request,
    message: str,
    category: str = "success",
    *,
    link: str | None = None,
    link_label: str | None = None,
    action: str | None = None,
    action_label: str | None = None,
) -> None:
    """Queue a message for the next rendered page.

    ``link`` adds a plain link, ``action`` a small POST button (e.g. "Rückgängig").
    Both must be local paths.
    """
    session = _session(request)
    if session is None:
        return
    if category not in FLASH_CATEGORIES:
        category = "info"
    text = (message or "").strip()
    if len(text) > MAX_FLASH_CHARS:
        text = text[: MAX_FLASH_CHARS - 1] + "…"
    entry: dict[str, Any] = {"message": text, "category": category}
    if link and is_local_path(link):
        entry["link"] = link
        entry["link_label"] = link_label or "Anzeigen"
    if action and is_local_path(action):
        entry["action"] = action
        entry["action_label"] = action_label or "Rückgängig"
    queued = list(session.get(FLASH_KEY) or [])
    queued.append(entry)
    session[FLASH_KEY] = queued[-MAX_FLASHES:]


def pop_flashes(request: Request) -> list[dict[str, Any]]:
    """Return and remove all queued messages."""
    session = _session(request)
    if session is None:
        return []
    queued = session.pop(FLASH_KEY, None) or []
    return [entry for entry in queued if isinstance(entry, dict) and entry.get("message")]


def redirect(url: str, *, status_code: int = 303) -> RedirectResponse:
    """Redirect after a POST (303 See Other makes the browser use GET)."""
    return RedirectResponse(url, status_code=status_code)


# ---------------------------------------------------------------------------
# One-time secrets (API tokens are shown exactly once, never stored in a cookie)


class OneTimeStore:
    """Keeps short-lived values in memory until they are read once."""

    def __init__(self, ttl: float = 300.0):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._values: dict[str, tuple[float, Any]] = {}

    def put(self, value: Any) -> str:
        key = secrets.token_urlsafe(16)
        now = time.monotonic()
        with self._lock:
            self._expire(now)
            self._values[key] = (now + self.ttl, value)
        return key

    def take(self, key: str | None) -> Any | None:
        if not key:
            return None
        with self._lock:
            self._expire(time.monotonic())
            entry = self._values.pop(key, None)
        return entry[1] if entry else None

    def _expire(self, now: float) -> None:
        for key in [k for k, (expires, _) in self._values.items() if expires <= now]:
            del self._values[key]


one_time = OneTimeStore()

# ---------------------------------------------------------------------------
# URLs and small text helpers


def is_local_path(value: str | None) -> bool:
    """True for paths on this site ("/eingang"), never for other hosts ("//x", "/\\x")."""
    if not value or not value.startswith("/"):
        return False
    if value.startswith("//") or value.startswith("/\\"):
        return False
    return not any(ch in value for ch in ("\r", "\n", "\t", "\x00"))


def safe_next(value: str | None, default: str = "/eingang") -> str:
    return value if is_local_path(value) else default


def archive_url(folder: str | None) -> str:
    folder = (folder or "").strip("/")
    return "/archiv/" + quote(folder, safe="/") if folder else "/archiv"


def quote_id(value: str) -> str:
    """A document id as a single URL path component."""
    return quote(str(value or ""), safe="")


def document_url(doc_id: str, page: int | None = None) -> str:
    url = "/dokument/" + quote_id(doc_id)
    return f"{url}#page={page}" if page else url


def search_url(**params: Any) -> str:
    clean: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, (list, tuple)):
            clean.extend((key, str(v)) for v in value if str(v))
        else:
            clean.append((key, str(value)))
    return "/suche" + ("?" + urlencode(clean) if clean else "")


def folder_label(folder: str | None) -> str:
    folder = (folder or "").strip("/")
    return folder.rsplit("/", 1)[-1] if folder else "Archiv"


def breadcrumbs(folder: str | None) -> list[dict[str, str]]:
    """``Finanzen/Bank`` → Archiv › Finanzen › Bank with a link for each step."""
    crumbs = [{"name": "Archiv", "path": "", "url": "/archiv"}]
    parts = [p for p in (folder or "").split("/") if p]
    for i in range(len(parts)):
        path = "/".join(parts[: i + 1])
        crumbs.append({"name": parts[i], "path": path, "url": archive_url(path)})
    return crumbs


def plural(count: int, singular: str, plural_form: str) -> str:
    return f"{format_number(count)} {singular if count == 1 else plural_form}"


def format_number(value: Any) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:,}".replace(",", ".")


def format_iban(value: str) -> str:
    compact = re.sub(r"\s+", "", value or "").upper()
    return " ".join(compact[i : i + 4] for i in range(0, len(compact), 4))


def error_text(exc: BaseException, fallback: str) -> str:
    """A German message for the user. Bare ids (``NotFound("2026…")``) are replaced by ``fallback``."""
    if isinstance(exc, DocuShelfError):
        message = str(exc).strip()
        if not message:
            return fallback
        if isinstance(exc, NotFound) and not _looks_like_sentence(message):
            return fallback
        return message
    if isinstance(exc, PermissionError):
        return f"{fallback} (keine Berechtigung)"
    if isinstance(exc, OSError):
        detail = exc.strerror or ""
        return f"{fallback} ({detail})" if detail else fallback
    return fallback


def _looks_like_sentence(message: str) -> bool:
    return " " in message and any(ch.isalpha() for ch in message)


_MARK_RE = re.compile(r"(</?mark>)")


def safe_snippet(snippet_html: str) -> str:
    """Re-escape a search snippet, keeping only ``<mark>`` tags.

    The searcher already escapes its snippets; this is a second line of defence
    so nothing but highlighting can ever reach the page.
    """
    text = html.unescape(snippet_html or "")
    parts = _MARK_RE.split(text)
    out: list[str] = []
    open_marks = 0
    for part in parts:
        if part == "<mark>":
            out.append(part)
            open_marks += 1
        elif part == "</mark>":
            if open_marks:
                out.append(part)
                open_marks -= 1
        else:
            out.append(html.escape(part, quote=False))
    out.extend("</mark>" for _ in range(open_marks))
    return "".join(out)


def text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Form parsing


def form_value(form: Mapping[str, Any], key: str, default: str = "") -> str:
    value = form.get(key)
    if value is None or not isinstance(value, str):
        return default
    return value.strip()


def form_flag(form: Mapping[str, Any], key: str) -> bool:
    return form_value(form, key).lower() in ("1", "true", "on", "yes", "ja")


def parse_date_input(value: str | None) -> date | None:
    """Accept ``2026-11-23`` (date inputs) and ``23.11.2026``. Raises ValueError for garbage."""
    text = (value or "").strip()
    if not text:
        return None
    match = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{2}|\d{4})", text)
    if match:
        day, month, year = (int(g) for g in match.groups())
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            raise ValueError(f"„{text}“ ist kein gültiges Datum.") from None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        parsed = parse_date(text)
        if parsed is not None:
            return parsed
    raise ValueError(f"„{text}“ ist kein gültiges Datum.")


def parse_tags(value: str | Iterable[str] | None) -> list[str]:
    """Comma separated (or a list) → clean, de-duplicated tags in input order."""
    if value is None:
        return []
    raw: list[str] = []
    if isinstance(value, str):
        raw = re.split(r"[,;\n]", value)
    else:
        for entry in value:
            raw.extend(re.split(r"[,;\n]", str(entry)))
    tags: list[str] = []
    seen: set[str] = set()
    for tag in raw:
        clean = " ".join(tag.split())
        if not clean:
            continue
        key = casefold_key(clean)
        if key in seen:
            continue
        seen.add(key)
        tags.append(clean)
    return tags


def parse_list(value: str | None) -> list[str]:
    """Comma or line separated values for rule conditions."""
    return [part.strip() for part in re.split(r"[,\n]", value or "") if part.strip()]


# ---------------------------------------------------------------------------
# Editing archived documents
#
# The edit form posts the values the page was rendered with as hidden
# "orig_<field>" inputs. Only fields the user actually changed are applied, so
# a stale page never reverts what happened to the file in the meantime (a move
# in the Finder, someone else's edit). When the user changed a field that was
# also changed elsewhere, the page asks again instead of overwriting.

EDIT_FIELDS = ("title", "doc_date", "sender", "tags", "folder")
EDIT_LABELS = {"title": "Titel", "doc_date": "Datum", "sender": "Absender", "tags": "Tags", "folder": "Ordner"}


def document_form(record: Any) -> dict[str, str]:
    """The edit form values of an archived document, as the page shows them."""
    return {
        "title": record.title or "",
        "doc_date": record.doc_date.isoformat() if record.doc_date else "",
        "sender": record.sender or "",
        "tags": ", ".join(record.tags or []),
        "folder": record.folder or "",
    }


def folder_key(value: str | None) -> str:
    """A folder as typed, comparable with an existing folder.

    Only Unicode normalization (NFC) and surrounding blanks and slashes are
    removed; the spelling of the folder names themselves (double spaces, a
    trailing dot as in "e.V.") stays, because folders made in the Finder may
    use it.
    """
    return unicodedata.normalize("NFC", value or "").strip().strip("/").strip()


def edit_value(field_name: str, raw: str | None) -> Any:
    """A form value in comparable form. Raises ValueError (German) for an invalid date."""
    text = raw or ""
    if field_name in ("title", "sender"):
        return " ".join(text.split())
    if field_name == "doc_date":
        return parse_date_input(text)
    if field_name == "tags":
        return parse_tags(text)
    if field_name == "folder":
        return folder_key(text)
    raise KeyError(field_name)


@dataclass
class EditPlan:
    changes: dict[str, Any]  # field -> value to store
    touched: set[str]  # fields the user changed on the page
    conflicts: dict[str, Any]  # field -> current value, changed elsewhere since the page was rendered


def plan_document_edit(
    posted: Mapping[str, Any],
    original: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> EditPlan:
    """Decide what an edit form submission changes (all values from :func:`edit_value`).

    ``posted`` holds the submitted fields, ``original`` the values the page
    was rendered with (None for forms without them: then every submitted value
    counts as wanted) and ``current`` the document as it is now. An empty
    folder field keeps the folder.
    """
    base = current if original is None else original
    plan = EditPlan(changes={}, touched=set(), conflicts={})
    for name, value in posted.items():
        if name == "folder" and not value:
            continue
        if name not in base or value == base[name]:
            continue
        plan.touched.add(name)
        if value == current.get(name):
            continue  # already like that
        if original is not None and current.get(name) != original.get(name):
            plan.conflicts[name] = current.get(name)
        else:
            plan.changes[name] = value
    return plan


def edit_conflict_message(conflicts: Mapping[str, Any]) -> str:
    parts = []
    for name in EDIT_FIELDS:
        if name not in conflicts:
            continue
        value = conflicts[name]
        if isinstance(value, date):
            shown = value.strftime("%d.%m.%Y")
        elif isinstance(value, list):
            shown = ", ".join(value)
        else:
            shown = str(value or "")
        parts.append(f"{EDIT_LABELS[name]} jetzt „{shown}“" if shown else f"{EDIT_LABELS[name]} jetzt leer")
    return (
        f"Die Datei wurde inzwischen geändert ({'; '.join(parts)}). Deine Eingaben stehen noch im Formular – "
        "prüfe sie und speichere erneut, um sie zu übernehmen."
    )


def filing_request_from_form(form: Mapping[str, Any]) -> FilingRequest:
    """Build the filing request from the inbox detail form. Raises ValueError (German)."""
    return FilingRequest(
        folder=form_value(form, "folder"),
        title=" ".join(form_value(form, "title").split()),
        doc_date=parse_date_input(form_value(form, "doc_date")),
        tags=parse_tags(form_value(form, "tags")),
        sender=" ".join(form_value(form, "sender").split()) or None,
        create_rule=form_flag(form, "create_rule"),
    )


FINGERPRINT_FIELDS = ("folder", "title", "doc_date", "tags", "sender")
STALE_SUGGESTION = "Der Vorschlag hat sich inzwischen geändert – bitte die Seite neu laden und prüfen."
NEW_DUPLICATE = "Das Dokument ist inzwischen als mögliches Duplikat markiert – bitte zuerst prüfen."


def suggestion_fingerprint(top: Mapping[str, Any] | None) -> str:
    """Short fingerprint of what batch filing would use: folder, title, date, tags and sender.

    The inbox page sends it along with each selected item, so an item whose
    suggestion changed after the page was rendered is not filed blindly.
    """
    top = top or {}
    data = {key: top.get(key) for key in FINGERPRINT_FIELDS}
    text = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


def batch_refusal(item: Any, expect: str | None, *, duplicate_seen: bool = False) -> str | None:
    """Why a batch selection made on an older page must not be filed; None if it may.

    ``expect`` is the fingerprint shown on the page (no check without it),
    ``duplicate_seen`` whether the page already marked the item as a possible
    duplicate.
    """
    if not expect:
        return None
    if suggestion_fingerprint(getattr(item, "top_suggestion", None)) != str(expect).strip().lower():
        return STALE_SUGGESTION
    if getattr(item, "duplicate_of", None) and not duplicate_seen:
        return NEW_DUPLICATE
    return None


def parse_batch_selection(values: Iterable[Any]) -> dict[str, tuple[str | None, bool]]:
    """Checkbox values of the inbox list: ``id``, ``id:fingerprint`` or ``id:fingerprint:dup``.

    Returns ``{item_id: (fingerprint or None, shown as duplicate)}`` in input
    order; anything that is not an item id is dropped.
    """
    selection: dict[str, tuple[str | None, bool]] = {}
    for value in values:
        if not isinstance(value, str):
            continue
        item_id, _, rest = value.strip().partition(":")
        if not is_item_id(item_id) or item_id in selection:
            continue
        expect, _, flag = rest.partition(":")
        selection[item_id] = (expect or None, flag == "dup")
    return selection


def filing_request_from_suggestion(suggestion: Mapping[str, Any] | None) -> FilingRequest | None:
    """The request "take the suggestion as it is" (batch filing); None if it is incomplete."""
    if not suggestion:
        return None
    folder = str(suggestion.get("folder") or "").strip()
    title = str(suggestion.get("title") or "").strip()
    if not folder or not title:
        return None
    return FilingRequest(
        folder=folder,
        title=title,
        doc_date=parse_date(suggestion.get("doc_date")),
        tags=parse_tags(suggestion.get("tags") or []),
        sender=(str(suggestion.get("sender")).strip() or None) if suggestion.get("sender") else None,
    )


# ---------------------------------------------------------------------------
# Labels

SOURCE_LABELS = {
    "upload": ("Upload", "upload"),
    "smb": ("Scanner (SMB)", "printer"),
    "ftp": ("Scanner (FTP)", "printer"),
    "mail": ("E-Mail", "mail"),
    "smtp": ("Scan per Mail", "mail"),
    "paperless": ("Paperless-Import", "archive"),
    "unbekannt": ("Unbekannt", "file"),
}


def source_label(source: str | None) -> str:
    source = source or "unbekannt"
    if source in SOURCE_LABELS:
        return SOURCE_LABELS[source][0]
    if source.startswith("watch-"):
        return f"Ordner „{source[6:]}“"
    return source


def source_icon(source: str | None) -> str:
    source = source or "unbekannt"
    if source in SOURCE_LABELS:
        return SOURCE_LABELS[source][1]
    if source.startswith("watch-"):
        return "folder"
    return "file"


SUGGESTION_SOURCES = {
    "rule": ("Regel", "signpost"),
    "similarity": ("Ähnliche Dokumente", "layers"),
    "llm": ("KI (lokal)", "sparkle"),
    "import": ("Paperless", "archive"),
    "heuristic": ("Vermutung", "sparkle"),
}

# Pipeline stages after which the named step runs (the item stores the last completed stage).
PIPELINE_STEPS = (
    ("received", "Umwandlung in PDF"),
    ("normalized", "Leerseiten entfernen"),
    ("deblanked", "Texterkennung (OCR)"),
    ("ocr_done", "Analyse"),
    ("analyzed", "Ablagevorschläge"),
)

TEXT_STATUS_LABELS = {
    "ok": "Text vorhanden",
    "empty": "Kein Text erkannt",
    "error": "Text konnte nicht gelesen werden",
    "unsupported": "Kein PDF – nur Name und Ordner sind durchsuchbar",
    "pending": "Wird noch eingelesen",
}

MATCH_LABELS = {
    "title": "Titel",
    "sender": "Absender",
    "folder": "Ordner",
    "tags": "Tags",
    "filename": "Dateiname",
    "text": "Text",
}


def processing_step(stage: str | None) -> dict[str, Any]:
    """Progress of an item that is being processed: step number, total and label."""
    stages = [name for name, _ in PIPELINE_STEPS]
    try:
        done = stages.index(stage or "received")
    except ValueError:
        done = len(stages) if stage == "ready" else 0
    current = PIPELINE_STEPS[min(done, len(PIPELINE_STEPS) - 1)][1]
    return {
        "done": done,
        "total": len(PIPELINE_STEPS),
        "number": min(done + 1, len(PIPELINE_STEPS)),
        "label": current,
        "steps": [
            {"label": label, "state": "done" if i < done else ("current" if i == done else "todo")}
            for i, (_name, label) in enumerate(PIPELINE_STEPS)
        ],
    }


# ---------------------------------------------------------------------------
# View models


@dataclass
class SuggestionView:
    folder: str
    title: str
    doc_date: date | None
    sender: str
    tags: list[str]
    score: float
    source: str
    source_label: str
    source_icon: str
    reasons: list[str]
    new_folder: bool
    rule_name: str | None

    @property
    def score_pct(self) -> int:
        return max(0, min(100, int(round(self.score * 100))))

    @property
    def folder_label(self) -> str:
        return folder_label(self.folder)

    @property
    def folder_parent(self) -> str:
        return self.folder.rsplit("/", 1)[0] if "/" in self.folder else ""

    @property
    def data(self) -> dict[str, Any]:
        """Values the filing form takes over when the suggestion is chosen."""
        return {
            "folder": self.folder,
            "title": self.title,
            "date": self.doc_date.isoformat() if self.doc_date else "",
            "sender": self.sender,
            "tags": list(self.tags),
        }


def folder_checker(index: Any) -> Callable[[str], bool]:
    """``exists(folder)`` against the current index, compared the way the archive resolves names.

    Suggestions are computed when a document arrives; the archive may have
    changed since (e.g. a folder renamed in the Finder), so "new folder" is
    decided when the page is shown.
    """
    try:
        known = {path.casefold() for path in index.list_folders()}
    except Exception:
        return lambda folder: True
    return lambda folder: not folder or folder.casefold() in known


def suggestion_view(data: Mapping[str, Any], *, folder_exists: Callable[[str], bool] | None = None) -> SuggestionView:
    suggestion = Suggestion.from_dict(dict(data))
    label, icon = SUGGESTION_SOURCES.get(suggestion.source, (suggestion.source or "Vorschlag", "sparkle"))
    try:
        score = float(suggestion.score or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    return SuggestionView(
        folder=str(suggestion.folder or ""),
        title=str(suggestion.title or ""),
        doc_date=parse_date(suggestion.doc_date),
        sender=str(suggestion.sender or ""),
        tags=[str(t) for t in suggestion.tags or []],
        score=score,
        source=suggestion.source,
        source_label=label,
        source_icon=icon,
        reasons=[str(r) for r in suggestion.reasons or [] if str(r).strip()],
        new_folder=(not folder_exists(str(suggestion.folder or ""))) if folder_exists else bool(suggestion.new_folder),
        rule_name=suggestion.rule_name,
    )


@dataclass
class InboxCard:
    id: str
    state: str
    title: str
    folder: str
    new_folder: bool
    doc_date: date | None
    sender: str
    source: str
    source_label: str
    source_icon: str
    received_at: str
    page_count: int
    pages_removed: int
    duplicate: dict[str, Any] | None
    fileable: bool
    suggestion_source: str | None
    stage: str
    step: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    # suggestion_fingerprint() of the top suggestion, sent back by batch filing.
    fingerprint: str = ""
    # "signed" / "encrypted" for PDFs that are filed unchanged (item.context["pdf_protection"]).
    protection: str | None = None
    # Why automatic filing left the item here (see auto_hold()); only while automatic filing is on.
    auto_hold: dict[str, Any] | None = None
    # The item's intake context, for the channel origin ("von Erika · Scanner").
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def url(self) -> str:
        return f"/eingang/{self.id}"

    @property
    def preview_unavailable(self) -> bool:
        """A password-protected PDF whose pages cannot even be counted: no page image to ask for."""
        return self.protection == "encrypted" and not self.page_count

    @property
    def protection_label(self) -> str | None:
        return PROTECTION_LABELS.get(self.protection or "")

    @property
    def protection_notice(self) -> str | None:
        return protection_notice(self.protection)

    @property
    def folder_label(self) -> str:
        return folder_label(self.folder)


def item_title(item: Any) -> str:
    top = getattr(item, "top_suggestion", None) or {}
    context = getattr(item, "context", None) or {}
    return str(top.get("title") or context.get("title") or getattr(item, "display_name", "") or item.id)


def inbox_card(item: Any, *, folder_exists: Callable[[str], bool] | None = None, auto: bool = False) -> InboxCard:
    """The card of an inbox item. ``auto``: automatic filing is on, so a held-back decision is shown."""
    top = item.top_suggestion or {}
    folder = str(top.get("folder") or "")
    request = filing_request_from_suggestion(top)
    analysis = item.analysis or {}
    return InboxCard(
        context=dict(item.context or {}) if isinstance(getattr(item, "context", None), dict) else {},
        id=item.id,
        state=item.state,
        title=item_title(item),
        folder=folder,
        new_folder=(not folder_exists(folder)) if folder_exists and folder else bool(top.get("new_folder")),
        doc_date=parse_date(top.get("doc_date") or analysis.get("doc_date")),
        sender=str(top.get("sender") or analysis.get("sender") or ""),
        source=item.source,
        source_label=source_label(item.source),
        source_icon=source_icon(item.source),
        received_at=item.received_at,
        page_count=int(item.page_count or 0),
        pages_removed=len(item.pages_removed or []),
        duplicate=item.duplicate_of if isinstance(item.duplicate_of, dict) else None,
        fileable=request is not None and item.state == "bereit",
        suggestion_source=top.get("source"),
        stage=item.stage,
        step=processing_step(item.stage),
        error=item.error if isinstance(item.error, dict) else None,
        fingerprint=suggestion_fingerprint(top),
        protection=item_protection(item),
        auto_hold=auto_hold(item) if auto and item.state == "bereit" else None,
    )


# ---------------------------------------------------------------------------
# Signed and password-protected PDFs (never rewritten, see pdf/xmp.py)

PROTECTION_LABELS = {"signed": "signiert", "encrypted": "passwortgeschützt"}
_INBOX_NOTICES = {
    "signed": "Diese PDF ist digital signiert. Damit die Signatur gültig bleibt, wird sie unverändert abgelegt: "
    "Titel und Datum stehen dann nur im Dateinamen, Tags und Absender lassen sich nicht in der Datei speichern.",
    "encrypted": "Diese PDF ist mit einem Passwort geschützt und wird unverändert abgelegt: Titel und Datum stehen "
    "dann nur im Dateinamen, Tags und Absender lassen sich nicht in der Datei speichern. Ohne das Passwort gibt es "
    "keine Vorschau und keinen durchsuchbaren Text.",
}
_ARCHIVE_NOTICES = {
    "signed": "Diese PDF ist digital signiert. DocuShelf verändert sie nicht, damit die Signatur gültig bleibt: "
    "Titel, Datum und Ordner kannst du ändern (die Datei wird nur umbenannt oder verschoben), Tags und Absender "
    "lassen sich nicht speichern.",
    "encrypted": "Diese PDF ist verschlüsselt. DocuShelf verändert sie nicht: Titel, Datum und Ordner kannst du "
    "ändern (die Datei wird nur umbenannt oder verschoben), Tags und Absender lassen sich nicht speichern.",
}


def item_protection(item: Any) -> str | None:
    """"signed" or "encrypted" if the pipeline marked the item's PDF as protected."""
    context = getattr(item, "context", None) or {}
    value = context.get("pdf_protection") if isinstance(context, Mapping) else None
    return value if value in PROTECTION_LABELS else None


def protection_notice(kind: str | None, *, archived: bool = False) -> str | None:
    """The German hint for a protected PDF in the inbox (or, with ``archived``, in the archive)."""
    return (_ARCHIVE_NOTICES if archived else _INBOX_NOTICES).get(kind or "")


def pdf_protection(path: Any) -> str | None:
    """"encrypted" or "signed" for an archived PDF that DocuShelf never rewrites, else None.

    Read-only check: any encryption, or a digital signature (``/SigFlags``
    bit 1 in the AcroForm, a signature field with a value, or ``/Perms`` in
    the catalog).
    """
    try:
        import pikepdf
    except ImportError:  # pragma: no cover - pikepdf is a dependency
        return None
    try:
        with pikepdf.open(path) as pdf:
            if pdf.is_encrypted:
                return "encrypted"
            return "signed" if _has_signature(pdf) else None
    except pikepdf.PasswordError:
        return "encrypted"
    except Exception:
        log.debug("cannot check %s for signatures", path, exc_info=True)
        return None


def _has_signature(pdf: Any) -> bool:
    import pikepdf

    root = pdf.Root
    if "/Perms" in root:
        return True
    form = root.get("/AcroForm")
    if not isinstance(form, pikepdf.Dictionary):
        return False
    try:
        if int(form.get("/SigFlags", 0)) & 1:
            return True
    except (TypeError, ValueError):
        pass
    fields = form.get("/Fields")
    pending = list(fields) if isinstance(fields, pikepdf.Array) else []
    checked = 0
    while pending and checked < 5000:
        node = pending.pop()
        checked += 1
        if not isinstance(node, pikepdf.Dictionary):
            continue
        if node.get("/FT") == pikepdf.Name.Sig and node.get("/V") is not None:
            return True
        kids = node.get("/Kids")
        if isinstance(kids, pikepdf.Array):
            pending.extend(kids)
    return False


# ---------------------------------------------------------------------------
# Further metadata of archived files (DocMeta.extra, e.g. from Paperless-ngx)

_EXTRA_FIELDS = (
    # XMP property, label, icon, how several entries are separated
    ("PaperlessNotes", "Notizen", "edit", "notes"),
    ("PaperlessFields", "Eigene Felder", "hash", "fields"),
    ("ArchiveSerialNumber", "Archivnummer (ASN)", "hash", None),
    ("PaperlessId", "Paperless-ID", "archive", None),
    ("MailSubject", "Mail-Betreff", "mail", None),
    ("MailFrom", "Mail von", "mail", None),
)
# Notes are joined with " | ", custom fields ("Name: Wert") with "; ". A "; "
# inside a value is kept when it is not followed by another "Name:".
_NOTE_SPLIT_RE = re.compile(r"\s+\|\s+")
_FIELD_SPLIT_RE = re.compile(r";\s+(?=[^;:]{1,80}:\s)")


def document_extras(extra: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Readable entries for the document page from ``read_meta(path).extra``."""
    extra = extra or {}
    entries: list[dict[str, Any]] = []
    for key, label, icon_name, kind in _EXTRA_FIELDS:
        value = extra.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        if kind == "notes":
            values = _NOTE_SPLIT_RE.split(value.strip())
        elif kind == "fields":
            values = _FIELD_SPLIT_RE.split(value.strip())
        else:
            values = [value.strip()]
        values = [v.strip() for v in values if v.strip()]
        if values:
            entries.append({"key": key, "label": label, "icon": icon_name, "values": values})
    return entries


def duplicate_link(duplicate: Mapping[str, Any] | None) -> dict[str, str] | None:
    """Where the original of a suspected duplicate can be seen."""
    if not duplicate:
        return None
    kind = duplicate.get("kind")
    target = str(duplicate.get("id") or "")
    if kind == "archive" and target:
        label = str(duplicate.get("path") or duplicate.get("title") or "Dokument im Archiv")
        return {"url": document_url(target), "label": label, "kind": "im Archiv"}
    if kind == "inbox" and is_item_id(target):
        label = str(duplicate.get("title") or "Dokument im Eingang")
        return {"url": f"/eingang/{target}", "label": label, "kind": "im Eingang"}
    return None


def date_choices(analysis: Analysis, limit: int = 4) -> list[dict[str, str]]:
    """Detected dates as chips: label, value (ISO) and German display."""
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    candidates: list[DateCandidate] = sorted(analysis.dates or [], key=lambda c: -float(c.score or 0))
    for candidate in candidates:
        parsed = parse_date(candidate.value)
        if parsed is None or candidate.value in seen:
            continue
        seen.add(candidate.value)
        result.append(
            {
                "value": parsed.isoformat(),
                "display": parsed.strftime("%d.%m.%Y"),
                "label": candidate.label or "",
            }
        )
        if len(result) >= limit:
            break
    return result


def analysis_facts(analysis: Analysis, context: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Readable facts about an inbox item for the detail page."""
    context = context or {}
    facts: list[dict[str, Any]] = []

    def add(label: str, values: Any, icon: str, *, mono: bool = False) -> None:
        if values is None or values == "" or values == []:
            return
        if not isinstance(values, (list, tuple)):
            values = [values]
        shown = [str(v) for v in values if str(v).strip()]
        if shown:
            facts.append({"label": label, "shown": shown, "icon": icon, "mono": mono})

    add("Dokumenttyp", analysis.doc_type, "file-text")
    add("Absender", analysis.sender, "building")
    if analysis.total_amount:
        add("Betrag", analysis.total_amount, "euro")
    others = [a for a in analysis.amounts or [] if a != analysis.total_amount]
    add("Weitere Beträge" if analysis.total_amount else "Beträge", others[:5], "euro")
    add("IBAN", [format_iban(i) for i in analysis.ibans or []][:3], "card", mono=True)
    for label, values in (analysis.references or {}).items():
        add(str(label), list(values)[:3], "hash", mono=True)

    if context.get("mail_subject"):
        add("Mail-Betreff", context.get("mail_subject"), "mail")
    sender_name = context.get("mail_from_name")
    sender_addr = context.get("mail_from")
    if sender_addr or sender_name:
        shown = f"{sender_name} <{sender_addr}>" if sender_name and sender_addr else (sender_addr or sender_name)
        add("Mail von", shown, "mail")
    add("Weitergeleitet von", context.get("forwarded_from"), "mail")
    add("Hochgeladen von", context.get("uploader") or context.get("uploaded_by"), "user")
    add("Herkunft", context.get("source_path"), "folder")

    paperless = context.get("paperless")
    if isinstance(paperless, Mapping):
        parts = []
        if paperless.get("id") is not None:
            parts.append(f"#{paperless.get('id')}")
        if paperless.get("correspondent"):
            parts.append(str(paperless.get("correspondent")))
        if paperless.get("document_type"):
            parts.append(str(paperless.get("document_type")))
        add("Paperless", " · ".join(parts), "archive")
        add("Paperless-Tags", list(paperless.get("tags") or [])[:8], "tag")
        add("Archivnummer (ASN)", paperless.get("archive_serial_number"), "hash", mono=True)
        notes = []
        for note in paperless.get("notes") or []:
            if isinstance(note, Mapping):
                text = note.get("note") or note.get("text")
                if text:
                    notes.append(str(text))
        add("Notizen", notes[:3], "edit")
        custom = []
        for entry in paperless.get("custom_fields") or []:
            if not isinstance(entry, Mapping) or not entry.get("name"):
                continue
            value = entry.get("value")
            if isinstance(value, (list, tuple)):
                value = ", ".join(str(v) for v in value)
            if value not in (None, ""):
                custom.append(f"{entry['name']}: {value}")
        add("Eigene Felder", custom[:8], "hash")
    return facts


@dataclass
class TreeNode:
    path: str
    name: str
    count: int  # documents directly in the folder
    total: int  # documents including all subfolders
    children: list["TreeNode"] = field(default_factory=list)
    open: bool = False
    current: bool = False

    @property
    def url(self) -> str:
        return archive_url(self.path)


def recursive_counts(folders: Iterable[str], counts: Mapping[str, int]) -> dict[str, int]:
    """Documents per folder including subfolders."""
    totals = {folder: 0 for folder in folders}
    totals.setdefault("", 0)
    for folder, count in counts.items():
        parts = [p for p in (folder or "").split("/") if p]
        totals[""] = totals.get("", 0) + count
        for i in range(1, len(parts) + 1):
            prefix = "/".join(parts[:i])
            totals[prefix] = totals.get(prefix, 0) + count
    return totals


def folder_tree(folders: Iterable[str], counts: Mapping[str, int], current: str = "") -> list[TreeNode]:
    """Nested folders for the archive sidebar; the branch of ``current`` is open."""
    folder_list = sorted(set(folders), key=natural_path_key)
    totals = recursive_counts(folder_list, counts)
    nodes: dict[str, TreeNode] = {}
    roots: list[TreeNode] = []
    current_key = casefold_key(current or "")
    for path in folder_list:
        if not path:
            continue
        parent, _, name = path.rpartition("/")
        node = TreeNode(
            path=path,
            name=name,
            count=int(counts.get(path, 0)),
            total=int(totals.get(path, 0)),
            current=casefold_key(path) == current_key,
            open=bool(current_key) and (current_key + "/").startswith(casefold_key(path) + "/"),
        )
        nodes[path] = node
        if parent and parent in nodes:
            nodes[parent].children.append(node)
        else:
            roots.append(node)
    return roots


# ---------------------------------------------------------------------------
# Audit log

AUDIT_LABELS = {
    "file": "Abgelegt",
    "auto_file": "Automatisch abgelegt",
    "channels_change": "Eingangskanäle geändert",
    "auto_review": "Automatische Ablage bestätigt",
    "update": "Bearbeitet",
    "move": "Verschoben",
    "delete": "In den Papierkorb",
    "delete_inbox": "Aus dem Eingang gelöscht",
    "restore": "Wiederhergestellt",
    "purge": "Endgültig gelöscht",
    "purge_expired": "Papierkorb automatisch geleert",
    "purge_done": "Erledigte Eingänge aufgeräumt",
    "create_folder": "Ordner angelegt",
    "rename_folder": "Ordner umbenannt",
    "delete_folder": "Ordner gelöscht",
    "create_rule": "Regel angelegt",
    "delete_rule": "Regel gelöscht",
    "rules_update": "Regeln bearbeitet",
    "rebuild": "Index neu aufgebaut",
    "reconcile": "Abgleich angestoßen",
    "external_move": "Im Dateisystem verschoben",
    "external_delete": "Im Dateisystem gelöscht",
    "import_paperless": "Paperless-Import",
    "upload": "Hochgeladen",
    "retry": "Neu verarbeitet",
    "login": "Angemeldet",
    "logout": "Abgemeldet",
    "login_denied": "Anmeldung abgelehnt",
    "token_create": "API-Token erstellt",
    "token_revoke": "API-Token widerrufen",
    "household_members": "Haushaltsmitglieder geändert",
    "settings_change": "Einstellungen geändert",
    "restart": "Neustart angefordert",
    "auto_file": "Automatisch abgelegt",
    "auto_review": "Automatische Ablage geprüft",
}


def audit_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """One audit log line in readable form."""
    action = str(entry.get("action") or "")
    details: list[str] = []
    title = entry.get("title")
    if title:
        details.append(f"„{title}“")
    if action in ("external_move",):
        if entry.get("from") and entry.get("to"):
            details.append(f"{entry.get('from')} → {entry.get('to')}")
    elif action == "rename_folder":
        if entry.get("folder") and entry.get("new_folder"):
            details.append(f"{entry.get('folder')} → {entry.get('new_folder')}")
    else:
        previous = entry.get("previous_path")
        path = entry.get("path") or entry.get("folder")
        if previous and path:
            details.append(f"{previous} → {path}")
        elif path:
            details.append(str(path))
    if entry.get("name") and action in ("create_rule", "delete_rule", "token_create", "token_revoke"):
        details.append(f"„{entry.get('name')}“")
    if action in ("channels_change", "settings_change", "restart") and entry.get("summary"):
        details.append(str(entry.get("summary")))
    if action == "household_members":
        group = "Gruppe " if entry.get("field") == "groups" else ""
        if entry.get("added"):
            details.append(f"hinzugefügt: {group}{entry.get('added')}")
        if entry.get("removed"):
            details.append(f"entfernt: {group}{entry.get('removed')}")
    if entry.get("method") and action in ("login", "login_denied"):
        details.append("per SSO" if entry.get("method") == "oidc" else "lokal")
    for key, label in (
        ("count", "Anzahl"),
        ("documents", "Dokumente"),
        ("imported", "importiert"),
        ("files", "Dateien"),
        ("rules", "Regeln"),
    ):
        value = entry.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            details.append(f"{label}: {format_number(value)}")
    if action == "auto_file":
        confidence = _fraction(entry.get("confidence"))
        if confidence is not None:
            details.append(f"Sicherheit {percent_down(confidence)} %" + (" – unsicher" if entry.get("review") else ""))
    if entry.get("recovered"):
        details.append("nach Unterbrechung abgeschlossen")
    return {
        "at": entry.get("at"),
        "user": entry.get("user") or "system",
        "action": action,
        "label": AUDIT_LABELS.get(action, action.replace("_", " ").capitalize() or "Aktion"),
        "details": " · ".join(details),
    }


def rule_summary(rule: Any) -> dict[str, Any]:
    """A rule in words for the rules page."""
    labels = {
        "sender": "erkannter Absender enthält",
        "keywords_any": "eines der Stichwörter",
        "keywords_all": "alle Stichwörter",
        "iban": "IBAN",
        "reference": "Nummer",
        "source": "Eingangskanal",
        "mail_from": "Mail von",
        "doc_type": "Dokumenttyp",
    }
    conditions = []
    match = getattr(rule, "match", None)
    raw = match.conditions() if match is not None and hasattr(match, "conditions") else {}
    for key, values in raw.items():
        shown = [format_iban(v) if key == "iban" else (source_label(v) if key == "source" else v) for v in values]
        conditions.append({"label": labels.get(key, key), "shown": shown})
    action = getattr(rule, "action", None)
    return {
        "name": getattr(rule, "name", ""),
        "conditions": conditions,
        "folder": getattr(action, "folder", "") if action else "",
        "title": getattr(action, "title", None) if action else None,
        "tags": list(getattr(action, "tags", []) or []) if action else [],
        "auto": bool(getattr(rule, "auto", False)),
    }


def json_for_script(value: Any) -> str:
    """JSON that is safe inside ``<script type="application/json">``."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _fraction(value: Any) -> float | None:
    """A number in 0..1 (confidence values), or None."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return min(1.0, max(0.0, number))


def percent_down(value: float) -> int:
    """A confidence as whole percent, rounded down: 0.749 shows as 74 %, never as a 75 % it did not reach."""
    return max(0, min(100, math.floor(value * 100 + 1e-9)))


def format_seconds(value: Any) -> str:
    """``3,2 s`` (German decimal comma)."""
    try:
        seconds = max(0.0, float(value))
    except (TypeError, ValueError):
        return ""
    text = f"{seconds:.1f}" if seconds < 100 else f"{seconds:.0f}"
    return text.replace(".", ",") + " s"


# ---------------------------------------------------------------------------
# Automatic filing
#
# Every automatic decision is written to the audit log ("auto_file"); a person
# confirming it adds an "auto_review" entry. Saving a change on the document
# page ("update", "move") counts as checked as well. The audit log lives on the
# data volume, so this history survives an index rebuild; the pages read it
# through a small cache that is valid as long as the log files are unchanged.

AUTO_USER = "automatisch"
AUTO_WINDOW_DAYS = 60
AUTO_RECENT_DAYS = 7
# A confirmation may refer to a filing older than the list shows.
AUTO_CONFIRM_DAYS = 400
AUTO_ACTIONS = frozenset({"auto_file", "auto_review", "update", "move"})
AUTO_MODES = ("off", "rules", "confident", "always")
_AUTO_MODE_ALIASES = {"aus": "off", "regeln": "rules", "sicher": "confident", "immer": "always"}
DEFAULT_AUTO_SOURCES = ("upload", "smb", "ftp", "mail", "smtp", "watch-*")
SIGNAL_LABELS = {
    "rule": "Regel",
    "import": "Paperless",
    "similarity": "Ähnlichkeit",
    "llm": "KI",
    "heuristic": "Heuristik",
}
_REVIEW_LABELS = {"ok": "bestätigt", "changed": "geändert"}


@dataclass(frozen=True)
class AutoSettings:
    """``Settings.auto_file`` as the pages show it."""

    mode: str = "off"
    threshold: float = 0.75
    sources: tuple[str, ...] = DEFAULT_AUTO_SOURCES
    new_folders: bool = True
    wait_for_llm: bool = True
    notify: bool = True

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @property
    def threshold_pct(self) -> int:
        return round(self.threshold * 100)

    @property
    def summary(self) -> str:
        """Short state for the inbox: "an (sicher ab 75 %)"."""
        pct = self.threshold_pct
        return {
            "off": "aus",
            "rules": "nur für Regeln mit „automatisch“",
            "confident": f"an (sicher ab {pct} %)",
            "always": f"an für alle Dokumente (unter {pct} % zum Prüfen markiert)",
        }.get(self.mode, "aus")

    @property
    def mode_label(self) -> str:
        """The mode in words for the settings page."""
        pct = self.threshold_pct
        return {
            "off": "aus – alles wartet im Eingang auf dich",
            "rules": "nur Regeln, die als „automatisch“ markiert sind",
            "confident": f"sicher – automatisch ab {pct} % Sicherheit, alles andere wartet im Eingang",
            "always": f"immer – alles wird abgelegt, unter {pct} % zum Prüfen markiert",
        }.get(self.mode, "aus")

    @property
    def source_labels(self) -> list[str]:
        labels = []
        for pattern in self.sources:
            if pattern.endswith("*"):
                prefix = pattern[:-1]
                if prefix == "watch-":
                    label = "alle Watch-Ordner"
                elif prefix.startswith("watch-"):
                    label = f"Watch-Ordner „{prefix[6:]}…“"
                else:
                    label = f"{prefix}…"
            else:
                label = source_label(pattern)
            if label not in labels:
                labels.append(label)
        return labels


def auto_settings(settings: Any) -> AutoSettings:
    """The automatic filing settings; defaults ("off") when the configuration has none."""
    config = getattr(settings, "auto_file", None)
    if config is None:
        return AutoSettings()
    mode = str(getattr(config, "mode", "off") or "off").strip().lower()
    mode = _AUTO_MODE_ALIASES.get(mode, mode)
    if mode not in AUTO_MODES:
        mode = "off"
    threshold = _fraction(getattr(config, "threshold", 0.75))
    sources = getattr(config, "sources", DEFAULT_AUTO_SOURCES)
    if isinstance(sources, str):
        sources = [part.strip() for part in sources.split(",")]
    return AutoSettings(
        mode=mode,
        threshold=0.75 if threshold is None else threshold,
        sources=tuple(str(s) for s in sources or () if str(s).strip()),
        new_folders=bool(getattr(config, "new_folders", True)),
        wait_for_llm=bool(getattr(config, "wait_for_llm", True)),
        notify=bool(getattr(config, "notify", True)),
    )


def signal_labels(sources: Iterable[Any] | None) -> list[str]:
    """German names of the signals behind a decision ("Regel", "Ähnlichkeit", "KI" …)."""
    names = [str(s) for s in sources or [] if s]
    if not names:
        return []
    try:
        from ..suggest.auto import describe_sources
    except Exception:
        describe_sources = None
    if describe_sources is not None:
        try:
            described = [str(label) for label in describe_sources(names) if label]
        except Exception:
            log.debug("cannot describe the sources %s", names, exc_info=True)
        else:
            if described:
                return described
    labels: list[str] = []
    for name in names:
        label = SIGNAL_LABELS.get(name, name)
        if label not in labels:
            labels.append(label)
    return labels


class _AuditCache:
    """Audit entries read recently, per log directory and start, valid while the files are unchanged."""

    def __init__(self, size: int = 16):
        self.size = size
        self._lock = threading.Lock()
        self._values: OrderedDict[tuple[Any, ...], tuple[tuple[Any, ...], list[tuple[datetime, dict[str, Any]]]]] = (
            OrderedDict()
        )

    def get(self, key: tuple[Any, ...], signature: tuple[Any, ...]) -> list[tuple[datetime, dict[str, Any]]] | None:
        with self._lock:
            cached = self._values.get(key)
            if cached is None or cached[0] != signature:
                return None
            self._values.move_to_end(key)
            return cached[1]

    def put(self, key: tuple[Any, ...], signature: tuple[Any, ...], entries: list[tuple[datetime, dict[str, Any]]]) -> None:
        with self._lock:
            self._values[key] = (signature, entries)
            self._values.move_to_end(key)
            while len(self._values) > self.size:
                self._values.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


_audit_cache = _AuditCache()


def _audit_signature(directory: Path) -> tuple[Any, ...] | None:
    """Name, mtime and size of every monthly log file (None if the directory cannot be read)."""
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return ()
    except OSError:
        return None
    parts = []
    for name in names:
        if not (name.startswith("audit-") and name.endswith(".jsonl")):
            continue
        try:
            st = os.stat(directory / name)
        except FileNotFoundError:
            continue
        except OSError:
            return None
        parts.append((name, st.st_mtime_ns, st.st_size))
    return tuple(parts)


def _read_audit(audit: Any, actions: frozenset[str], start: datetime) -> list[tuple[datetime, dict[str, Any]]]:
    reader = getattr(audit, "entries", None)
    raw: Iterable[Any] = []
    if callable(reader):
        raw = reader(actions=set(actions), since=start)
    else:  # an audit log without the query: newest entries only
        raw = audit.tail(5000)
    found: list[tuple[datetime, dict[str, Any]]] = []
    for entry in raw:
        if not isinstance(entry, Mapping) or entry.get("action") not in actions:
            continue
        at = parse_iso_datetime(entry.get("at")) if isinstance(entry.get("at"), str) else None
        if at is None or at < start:
            continue
        found.append((at, dict(entry)))
    found.sort(key=lambda pair: pair[0], reverse=True)
    return found


def audit_entries(audit: Any, actions: Iterable[str], since: datetime) -> list[tuple[datetime, dict[str, Any]]]:
    """``(time, entry)`` of the given actions written since ``since``, newest first.

    The log is read from the start of ``since``'s month and kept until one of
    its files changes, so pages can ask on every request.
    """
    wanted = frozenset(actions)
    start = since.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    directory = getattr(audit, "directory", None)
    signature = _audit_signature(Path(directory)) if directory is not None else None
    key = (str(directory), wanted, start.isoformat())
    cached = _audit_cache.get(key, signature) if signature is not None else None
    if cached is None:
        cached = _read_audit(audit, wanted, start)
        if signature is not None:
            _audit_cache.put(key, signature, cached)
    return [(at, entry) for at, entry in cached if at >= since]


def _text_items(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if item is not None and str(item).strip()]


@dataclass
class AutoFiling:
    """One automatic filing from the audit log, with what happened to it since."""

    doc_id: str
    at: str
    when: datetime
    user: str
    title: str
    folder: str
    path: str
    confidence: float
    uncertain: bool  # filed below the threshold ("unsicher")
    sources: list[str]
    signals: list[str]
    reasons: list[str]
    item: str | None = None
    # How a person checked it: {"at", "user", "status": "ok" | "changed"}.
    review: dict[str, Any] | None = None
    # The document as it is now; None if it is gone (only meaningful once looked_up).
    record: Any = None
    looked_up: bool = False

    @property
    def confidence_pct(self) -> int:
        return percent_down(self.confidence)

    @property
    def reviewed(self) -> bool:
        return self.review is not None

    @property
    def gone(self) -> bool:
        """The document is no longer in the archive (deleted, or moved without its id)."""
        return self.looked_up and self.record is None

    @property
    def pending(self) -> bool:
        """Filed as uncertain and nobody has looked at it yet."""
        return self.uncertain and not self.reviewed and not self.gone

    @property
    def url(self) -> str:
        return document_url(self.record.id if self.record is not None else self.doc_id)

    @property
    def current_title(self) -> str:
        return str(getattr(self.record, "title", "") or self.title) if self.record is not None else self.title

    @property
    def current_folder(self) -> str:
        return str(getattr(self.record, "folder", "") or "") if self.record is not None else self.folder

    @property
    def current_path(self) -> str:
        return str(getattr(self.record, "path", "") or "") if self.record is not None else self.path

    @property
    def moved(self) -> bool:
        return self.record is not None and bool(self.path) and self.current_path != self.path

    @property
    def label(self) -> str:
        """``82 % – Ähnlichkeit, KI``."""
        text = f"{self.confidence_pct} %"
        return f"{text} – {', '.join(self.signals)}" if self.signals else text

    @property
    def review_label(self) -> str:
        if self.review is None:
            return ""
        return _REVIEW_LABELS.get(str(self.review.get("status") or "ok"), "geprüft")

    def to_dict(self) -> dict[str, Any]:
        current = None
        if self.record is not None:
            current = {"path": self.current_path, "folder": self.current_folder, "title": self.current_title}
        return {
            "doc_id": self.doc_id,
            "at": self.at,
            "user": self.user,
            "title": self.title,
            "folder": self.folder,
            "path": self.path,
            "confidence": self.confidence,
            "uncertain": self.uncertain,
            "sources": list(self.sources),
            "signals": list(self.signals),
            "reasons": list(self.reasons),
            "item": self.item,
            "exists": not self.gone,
            "current": current,
            "review": dict(self.review) if self.review else None,
            "reviewed": self.reviewed,
            "pending": self.pending,
            "url": self.url,
        }


def _auto_filing(when: datetime, entry: Mapping[str, Any], config: AutoSettings, review: dict[str, Any] | None) -> AutoFiling:
    confidence = _fraction(entry.get("confidence"))
    confidence = 0.0 if confidence is None else confidence
    flag = entry.get("review")
    uncertain = flag if isinstance(flag, bool) else confidence < config.threshold
    path = str(entry.get("path") or "").strip("/")
    folder = entry.get("folder")
    if not isinstance(folder, str):
        folder = path.rsplit("/", 1)[0] if "/" in path else ""
    title = str(entry.get("title") or "").strip()
    if not title and path:
        title = path.rsplit("/", 1)[-1]
    sources = _text_items(entry.get("sources"))
    item = entry.get("item")
    return AutoFiling(
        doc_id=str(entry.get("doc_id") or "").strip(),
        at=str(entry.get("at") or ""),
        when=when,
        user=str(entry.get("user") or AUTO_USER),
        title=title or "Dokument",
        folder=folder.strip("/"),
        path=path,
        confidence=confidence,
        uncertain=uncertain,
        sources=sources,
        signals=signal_labels(sources),
        reasons=_text_items(entry.get("reasons")),
        item=str(item) if item else None,
        review=review,
    )


def look_up_records(services: Any, filings: Iterable[AutoFiling]) -> None:
    """Attach the current index record to each filing (None: no longer in the archive)."""
    index = getattr(services, "index", None)
    if index is None:
        return
    for filing in filings:
        if filing.looked_up:
            continue
        try:
            filing.record = index.get(filing.doc_id)
        except Exception:
            log.debug("cannot look up document %s", filing.doc_id, exc_info=True)
            continue
        filing.looked_up = True


def auto_filings(
    services: Any,
    *,
    days: int = AUTO_WINDOW_DAYS,
    lookup: bool = True,
    reference: datetime | None = None,
) -> list[AutoFiling]:
    """The automatic filings of the last ``days`` days, newest first."""
    audit = getattr(services, "audit", None)
    if audit is None:
        return []
    since = (reference or now()) - timedelta(days=days)
    try:
        entries = audit_entries(audit, AUTO_ACTIONS, since)
    except Exception:
        log.warning("cannot read the automatic filings from the audit log", exc_info=True)
        return []
    config = auto_settings(getattr(services, "settings", None))
    reviews: dict[str, dict[str, Any]] = {}
    edits: dict[str, list[tuple[datetime, dict[str, Any]]]] = {}
    filed: list[tuple[datetime, dict[str, Any]]] = []
    for when, entry in entries:  # newest first
        doc_id = str(entry.get("doc_id") or "").strip()
        if not doc_id:
            continue
        action = entry.get("action")
        if action == "auto_file":
            filed.append((when, entry))
        elif action == "auto_review":
            reviews.setdefault(
                doc_id,
                {"at": entry.get("at"), "user": entry.get("user") or "system", "status": str(entry.get("status") or "ok")},
            )
        elif str(entry.get("user") or "") not in ("", "system", AUTO_USER):
            edits.setdefault(doc_id, []).append((when, entry))
    result: list[AutoFiling] = []
    seen: set[str] = set()
    for when, entry in filed:
        doc_id = str(entry.get("doc_id")).strip()
        if doc_id in seen:
            continue
        seen.add(doc_id)
        review = reviews.get(doc_id)
        if review is None:
            later = [edit for at, edit in edits.get(doc_id, []) if at >= when]
            if later:
                first = later[-1]  # edits are newest first: the last one followed the filing first
                review = {"at": first.get("at"), "user": first.get("user"), "status": "changed"}
        result.append(_auto_filing(when, entry, config, review))
    if lookup:
        look_up_records(services, result)
    return result


def find_auto_filing(services: Any, doc_id: str, *, days: int = AUTO_WINDOW_DAYS) -> AutoFiling | None:
    """The automatic filing of document ``doc_id`` (with its current record), or None."""
    doc_id = str(doc_id or "").strip()
    if not doc_id:
        return None
    for filing in auto_filings(services, days=days, lookup=False):
        if filing.doc_id == doc_id:
            look_up_records(services, [filing])
            return filing
    return None


def confirm_auto_filing(services: Any, filing: AutoFiling, *, user: str | None) -> dict[str, Any]:
    """Record that a person checked an automatic filing ("Passt")."""
    services.audit.write(
        user,
        "auto_review",
        doc_id=filing.doc_id,
        status="ok",
        title=filing.current_title or None,
        path=filing.current_path or None,
    )
    filing.review = {"at": now_iso(), "user": user or "system", "status": "ok"}
    return dict(filing.review)


@dataclass
class AutoOverview:
    """Automatic filing at a glance: settings and what happened recently."""

    config: AutoSettings
    total: int = 0  # filings in the list window
    recent: int = 0  # filings of the last AUTO_RECENT_DAYS days
    pending: int = 0  # uncertain ones nobody checked yet

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @property
    def show_tabs(self) -> bool:
        return self.enabled or self.total > 0


def auto_overview(
    services: Any, filings: list[AutoFiling] | None = None, *, reference: datetime | None = None
) -> AutoOverview:
    config = auto_settings(getattr(services, "settings", None))
    if filings is None:
        filings = auto_filings(services, lookup=False, reference=reference)
    look_up_records(services, [f for f in filings if f.uncertain and not f.reviewed])
    recent_since = (reference or now()) - timedelta(days=AUTO_RECENT_DAYS)
    return AutoOverview(
        config=config,
        total=len(filings),
        recent=sum(1 for f in filings if f.when >= recent_since),
        pending=sum(1 for f in filings if f.pending),
    )


def auto_pending_count(services: Any) -> int:
    """Uncertain automatic filings nobody has checked yet (the dot in the navigation)."""
    candidates = [f for f in auto_filings(services, lookup=False) if f.uncertain and not f.reviewed]
    if not candidates:
        return 0
    look_up_records(services, candidates)
    return sum(1 for f in candidates if f.pending)


def auto_hold(item: Any) -> dict[str, Any] | None:
    """Why automatic filing left an inbox item for the user (``item.context["auto"]``), or None."""
    context = getattr(item, "context", None) or {}
    data = context.get("auto") if isinstance(context, Mapping) else None
    if not isinstance(data, Mapping) or data.get("decision") != "hold":
        return None
    reasons = _text_items(data.get("reasons"))
    confidence = _fraction(data.get("confidence"))
    main = next((reason for reason in reasons if reason.casefold().startswith("unsicher")), None)
    if main is None and reasons:
        main = reasons[-1]
    if main is None:
        main = f"unsicher ({percent_down(confidence)} %)" if confidence is not None else "zurückgehalten"
    return {
        "label": f"Nicht automatisch: {main}",
        "reason": main,
        "reasons": reasons,
        "confidence": confidence,
        "at": data.get("at"),
    }


def auto_done(item: Any) -> dict[str, Any] | None:
    """How a filed inbox item was filed automatically (``item.context["auto"]``), or None."""
    context = getattr(item, "context", None) or {}
    data = context.get("auto") if isinstance(context, Mapping) else None
    if not isinstance(data, Mapping) or data.get("decision") != "file":
        return None
    confidence = _fraction(data.get("confidence"))
    signals = signal_labels(_text_items(data.get("sources")))
    pct = percent_down(confidence) if confidence is not None else None
    label = f"{pct} %" if pct is not None else ""
    if signals:
        label = f"{label} – {', '.join(signals)}" if label else ", ".join(signals)
    return {
        "label": label,
        "review": bool(data.get("review")),
        "reasons": _text_items(data.get("reasons")),
        "doc_id": data.get("doc_id"),
    }


# ---------------------------------------------------------------------------
# Language model (settings page)

LLM_PROVIDERS = {"ollama": "Ollama", "openai": "OpenAI-kompatibel"}
_LOCAL_SUFFIXES = (".localhost", ".local", ".lan", ".internal", ".home.arpa", ".intranet")
_SHARED_NETWORK = ipaddress.ip_network("100.64.0.0/10")
_ANSWER_FIELDS = (
    ("ordner", "Ordner"),
    ("neuer_ordner", "Neuer Ordner"),
    ("titel", "Titel"),
    ("datum", "Datum"),
    ("absender", "Absender"),
    ("dokumenttyp", "Dokumenttyp"),
    ("tags", "Tags"),
    ("sicherheit", "Sicherheit"),
    ("begruendung", "Begründung"),
)
MAX_ANSWER_FIELDS = 12
MAX_ANSWER_CHARS = 240


def endpoint_host(url: str | None) -> str:
    """``http://ollama:11434`` – scheme, host and port only (no user, path or query)."""
    try:
        parts = urlsplit(str(url or "").strip())
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return ""
    if not host:
        return ""
    if ":" in host:
        host = f"[{host}]"
    shown = host + (f":{port}" if port else "")
    return f"{parts.scheme}://{shown}" if parts.scheme else shown


def endpoint_is_local(url: str | None) -> bool | None:
    """Whether the endpoint is on this machine or in the local network (None: unknown)."""
    try:
        host = urlsplit(str(url or "").strip()).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES) or ("." not in host and ":" not in host):
        return True  # names without a domain are containers or machines of the local network
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or (address.version == 4 and address in _SHARED_NETWORK)
    )


@dataclass(frozen=True)
class LlmInfo:
    enabled: bool
    provider: str = ""
    provider_label: str = ""
    endpoint: str = ""
    local: bool | None = None
    model: str = ""
    api_key: bool = False
    context_tokens: int | None = None
    timeout: int | None = None


def llm_info(settings: Any) -> LlmInfo:
    """What the settings page shows about the language model; never the API key itself."""
    config = getattr(settings, "llm", None)
    if config is None:
        return LlmInfo(enabled=False)
    provider = str(getattr(config, "provider", "ollama") or "ollama").strip().lower()
    url = str(getattr(config, "url", "") or "")
    context_tokens = getattr(config, "context_tokens", None)
    timeout = getattr(config, "timeout", None)
    return LlmInfo(
        enabled=True,
        provider=provider,
        provider_label=LLM_PROVIDERS.get(provider, provider),
        endpoint=endpoint_host(url),
        local=endpoint_is_local(url),
        model=str(getattr(config, "model", "") or ""),
        api_key=bool(getattr(config, "api_key", None)),
        context_tokens=int(context_tokens) if isinstance(context_tokens, int) and provider == "ollama" else None,
        timeout=int(timeout) if isinstance(timeout, (int, float)) else None,
    )


def _answer_value(key: str, value: Any) -> str:
    if value is None or value == "":
        return "–"
    if isinstance(value, bool):
        return "ja" if value else "nein"
    if key == "sicherheit" and isinstance(value, (int, float)):
        fraction = _fraction(value)
        return f"{percent_down(fraction)} %" if fraction is not None else "–"
    if isinstance(value, (list, tuple)):
        text = ", ".join(str(v) for v in value if v not in (None, "")) or "–"
    elif isinstance(value, Mapping):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= MAX_ANSWER_CHARS else text[: MAX_ANSWER_CHARS - 1] + "…"


def probe_answer(answer: Any) -> list[dict[str, str]]:
    """The model's answer to the test letter as label/value pairs (known keys first)."""
    if not isinstance(answer, Mapping):
        return []
    rows: list[dict[str, str]] = []
    known = {key for key, _label in _ANSWER_FIELDS}
    for key, label in _ANSWER_FIELDS:
        if key in answer:
            rows.append({"label": label, "value": _answer_value(key, answer[key])})
    for key, value in answer.items():
        if str(key) not in known:
            rows.append({"label": str(key), "value": _answer_value(str(key), value)})
    return rows[:MAX_ANSWER_FIELDS]


class LlmProbe:
    """The connection test of the language model ("Verbindung testen").

    The test runs in a thread of its own; the page waits at most a given time
    for it. A slower answer (a model that is still being loaded) is kept and
    shown the next time the settings page is opened. Only one test runs at a time.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running = False
        self.last: dict[str, Any] | None = None

    def snapshot(self) -> tuple[bool, dict[str, Any] | None]:
        with self._lock:
            return self.running, (dict(self.last) if self.last else None)

    def run(self, config: Any, *, timeout: float, probe: Callable[[Any], dict[str, Any]] | None = None) -> tuple[str, dict[str, Any] | None]:
        """("done", result), ("running", None) after ``timeout`` or ("busy", None) if a test is still running."""
        with self._lock:
            if self.running:
                return "busy", None
            self.running = True
        done = threading.Event()
        worker = probe or probe_llm

        def target() -> None:
            try:
                result = worker(config)
            except Exception as exc:  # the probe must never take the page down
                log.warning("language model test failed", exc_info=True)
                result = _probe_result(config, ok=False, error=f"Test fehlgeschlagen: {exc}")
            result = dict(result)
            result["at"] = now_iso()
            with self._lock:
                self.last = result
                self.running = False
            done.set()

        thread = threading.Thread(target=target, name="docushelf-llm-test", daemon=True)
        thread.start()
        if not done.wait(max(0.1, float(timeout))):
            return "running", None
        with self._lock:
            return "done", dict(self.last) if self.last else None


def _probe_result(config: Any, *, ok: bool, error: str | None = None, duration: float = 0.0) -> dict[str, Any]:
    return {
        "ok": ok,
        "provider": str(getattr(config, "provider", "ollama") or "ollama"),
        "model": str(getattr(config, "model", "") or ""),
        "duration_s": round(duration, 2),
        "answer": None,
        "error": error,
    }


def probe_llm(config: Any) -> dict[str, Any]:
    """``LlmClient(config).probe()`` – a tiny classification of a built-in sample letter."""
    started = time.monotonic()
    try:
        from ..suggest.llm import LlmClient

        client = LlmClient(config)
        probe = getattr(client, "probe", None)
        if not callable(probe):
            return _probe_result(config, ok=False, error="Diese Version kann die Verbindung noch nicht testen.")
        result = probe()
    except Exception as exc:
        log.warning("language model test failed", exc_info=True)
        return _probe_result(config, ok=False, error=f"Test fehlgeschlagen: {exc}", duration=time.monotonic() - started)
    if not isinstance(result, Mapping):
        return _probe_result(config, ok=False, error="Der Test lieferte kein Ergebnis.", duration=time.monotonic() - started)
    data = _probe_result(config, ok=bool(result.get("ok")), duration=time.monotonic() - started)
    data.update({key: value for key, value in result.items() if key not in ("ok",)})
    try:
        data["duration_s"] = round(float(result.get("duration_s")), 2)
    except (TypeError, ValueError):
        data["duration_s"] = round(time.monotonic() - started, 2)
    if not data["ok"] and not data.get("error"):
        data["error"] = "Keine verwertbare Antwort."
    return data


# ---------------------------------------------------------------------------
# Archive: tiles or list ("Kacheln" / "Liste")

VIEW_COOKIE = "ds_ansicht"
VIEWS = ("kacheln", "liste")
DEFAULT_VIEW = "kacheln"
VIEW_COOKIE_MAX_AGE = 365 * 24 * 3600


# Sort order of the archive, remembered like the view. By name first: folders are
# often numbered ("01 Finanzen", "02 Wohnung") to set their order on purpose.
SORT_COOKIE = "ds_sortierung"
DEFAULT_SORT = "title"
SORT_COOKIE_MAX_AGE = VIEW_COOKIE_MAX_AGE

_DIGITS = re.compile(r"(\d+)")


def archive_view(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in VIEWS else DEFAULT_VIEW


def natural_key(name: str) -> tuple[Any, ...]:
    """Sort key that compares names like people do: case and accents do not
    matter and numbers count by value, so "2 B" < "10 A" and "Ärzte" sits with "A".
    Equal values keep a stable order ("02" before "2")."""
    folded = unicodedata.normalize("NFKD", casefold_key(name or ""))
    plain = "".join(ch for ch in folded if not unicodedata.combining(ch))
    parts = _DIGITS.split(plain)
    # split() alternates text and digit runs, so equal positions always hold equal types.
    return tuple((int(part), part) if index % 2 else part for index, part in enumerate(parts))


def natural_path_key(path: str) -> tuple[tuple[Any, ...], ...]:
    """``natural_key`` for each part of a folder path, so children follow their parent."""
    return tuple(natural_key(part) for part in (path or "").split("/"))


def sort_folder_rows(rows: list[dict[str, Any]], order: str) -> list[dict[str, Any]]:
    """Folders of the list view in the order of the documents: by name or by their newest document."""
    by_name = sorted(rows, key=lambda row: natural_key(str(row.get("name") or "")))
    if order == "title_desc":
        return list(reversed(by_name))
    if order in ("date_desc", "date_asc"):
        dated = [row for row in by_name if row.get("newest")]
        undated = [row for row in by_name if not row.get("newest")]
        dated.sort(key=lambda row: row["newest"], reverse=order == "date_desc")
        return dated + undated
    return by_name


def list_sorting(order: str) -> dict[str, dict[str, str | None]]:
    """Target and state of the sortable column headers of the list view (name and date)."""
    return {
        "name": {
            "next": "title_desc" if order == "title" else "title",
            "state": "ascending" if order == "title" else ("descending" if order == "title_desc" else None),
        },
        "date": {
            "next": "date_asc" if order == "date_desc" else "date_desc",
            "state": "descending" if order == "date_desc" else ("ascending" if order == "date_asc" else None),
        },
    }
