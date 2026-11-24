"""JSON API under ``/api/v1``.

Every endpoint needs a logged-in browser session or an API token
(``Authorization: Bearer dsk_…``). Managing tokens and deleting trash entries
for good need the browser session. Requests that change something and rely on
the session cookie must carry the session's CSRF token in the ``X-CSRF-Token``
header. Errors are answered as ``{"detail": "<German message>"}`` with
404 (not found), 400 (invalid input or path), 409 (conflict), 422 (processing
failed), 401/403 (authentication).

Numbers in URLs are 1-based: ``/inbox/{id}/original/1`` is the first original
file, ``/documents/{id}/pages/1.png`` the first page. Page images accept
``?w=<pixels>`` (64–2000); with ``?v=<content_hash>`` matching the current
content, document page images may be cached by the browser for a long time.
Pages of files that cannot be rendered (password protected, damaged) are
answered with a placeholder image (``image/svg+xml``) instead of an error.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import shutil
import stat
import tempfile
import threading
import unicodedata
from contextlib import aclosing
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from .. import __version__
from ..errors import DocuShelfError, NotFound, PathError
from ..models import FilingRequest, MetadataUpdate
from ..util.names import check_relative
from ..util.timeutil import now_iso, parse_date
from .security import (
    User,
    get_services,
    get_settings,
    household_key,
    require_session_user,
    require_user,
    verify_csrf,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_user), Depends(verify_csrf)])

MB = 1024 * 1024
MAX_JSON_BYTES = 2 * MB
# Shown instead of a page image when a file cannot be rendered.
PAGE_PLACEHOLDER = Path(__file__).resolve().parent.parent / "static" / "page-unavailable.svg"
MAX_UPLOAD_FILES = 200
MAX_BATCH_ITEMS = 1000
DEFAULT_PAGE_WIDTH = 300
ACTIVE_STATES = ("arbeit", "bereit", "fehler")
ALL_STATES = ("arbeit", "bereit", "fehler", "erledigt")
_FORM_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")
_SUFFIX_RE = re.compile(r"\.[a-z0-9]{1,8}")
_TRUE = {"1", "true", "yes", "on", "ja"}
_FALSE = {"0", "false", "no", "off", "nein", ""}

# Only these types are shown in the browser. Everything else is offered as a
# download, so that an HTML or SVG file in the archive never runs as part of
# this site.
INLINE_TYPES = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/tiff",
        "image/bmp",
        "image/heic",
        "image/heif",
        "text/plain",
    }
)
mimetypes.add_type("image/heic", ".heic")
mimetypes.add_type("image/heif", ".heif")
mimetypes.add_type("image/webp", ".webp")


# ---------------------------------------------------------------------------
# Helpers


def _services(request: Request) -> Any:
    return get_services(request)


def _audit(services: Any, user: str | None, action: str, **details: Any) -> None:
    audit = getattr(services, "audit", None)
    if audit is None:
        return
    try:
        audit.write(user, action, **details)
    except Exception:
        log.exception("cannot write the audit log")


def _media_type(request: Request) -> str:
    return request.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _content_length(request: Request) -> int | None:
    value = request.headers.get("content-length", "").strip()
    return int(value) if value.isdigit() else None


async def read_body(request: Request, limit: int = MAX_JSON_BYTES) -> bytes:
    """The request body, refused with 413 above ``limit`` bytes."""
    too_large = f"Die Anfrage ist zu groß (höchstens {max(1, limit // MB)} MB)."
    declared = _content_length(request)
    if declared is not None and declared > limit:
        raise HTTPException(status_code=413, detail=too_large)
    chunks: list[bytes] = []
    size = 0
    async with aclosing(request.stream()) as stream:
        async for chunk in stream:
            size += len(chunk)
            if size > limit:
                raise HTTPException(status_code=413, detail=too_large)
            chunks.append(chunk)
    return b"".join(chunks)


async def request_data(request: Request) -> dict[str, Any]:
    """Dependency: the JSON object of the request (or its form fields); ``{}`` without body."""
    media_type = _media_type(request)
    if media_type in _FORM_TYPES:
        form = await request.form()
        return {key: value for key, value in form.items() if key != "csrf_token" and isinstance(value, str)}
    raw = await read_body(request)
    if not raw.strip():
        return {}
    if media_type and media_type != "application/json" and not media_type.endswith("+json"):
        raise HTTPException(status_code=415, detail="Bitte die Daten als JSON senden (Content-Type: application/json).")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Der Inhalt ist kein gültiges JSON.") from None
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Es wird ein JSON-Objekt erwartet.")
    return data


def _as_bool(value: Any, field: str = "") -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise HTTPException(status_code=400, detail=f"„{field or value}“ muss wahr oder falsch sein.")


def _optional_bool(data: dict[str, Any], key: str) -> bool | None:
    return _as_bool(data[key], key) if key in data and data[key] is not None else None


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"„{field}“ muss ein Text sein.")
    return value


def _date_value(value: Any, field: str):
    parsed = parse_date(value)
    if parsed is None:
        raise HTTPException(status_code=400, detail=f"Ungültiges Datum für „{field}“: {value!r} (erwartet JJJJ-MM-TT).")
    return parsed


def _tags(value: Any) -> list[str]:
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        items = value
    else:
        raise HTTPException(status_code=400, detail="„tags“ muss eine Liste von Texten sein.")
    return [item.strip() for item in items if item.strip()]


def _relative(value: Any, field: str = "path") -> str:
    return check_relative(_text(value, field))


def _load_item(services: Any, item_id: str) -> Any:
    try:
        return services.inbox.load(item_id)
    except NotFound:
        raise NotFound("Eingangsdokument nicht gefunden.") from None


def _load_record(services: Any, doc_id: str) -> Any:
    record = services.index.get(doc_id) if doc_id else None
    if record is None:
        raise NotFound("Dokument nicht gefunden.")
    return record


def _original_names(item: Any) -> list[str]:
    return [rel.split("/", 1)[-1] for rel in item.original_files]


def item_summary(item: Any) -> dict[str, Any]:
    """The fields of an inbox item that lists need."""
    return {
        "id": item.id,
        "state": item.state,
        "stage": item.stage,
        "source": item.source,
        "received_at": item.received_at,
        "display_name": item.display_name,
        "original_files": _original_names(item),
        "page_count": item.page_count,
        "pages_removed": list(item.pages_removed),
        "attempts": item.attempts,
        "error": item.error,
        "duplicate_of": item.duplicate_of,
        "filing": item.filing,
        "has_document": item.document_path.is_file(),
        "top_suggestion": item.top_suggestion,
        "suggestion_count": len(item.suggestions),
    }


def item_detail(item: Any, inbox: Any = None) -> dict[str, Any]:
    """Everything about an inbox item (``item.json`` plus derived fields and the log)."""
    data = item.to_dict()
    data.update(
        state=item.state,
        display_name=item.display_name,
        original_names=_original_names(item),
        has_document=item.document_path.is_file(),
    )
    if inbox is not None:
        data["log"] = inbox.read_log(item)
    return data


_GERMAN_ASCII = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss", "ẞ": "SS"})


def _ascii_name(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.translate(_GERMAN_ASCII))
    text = text.encode("ascii", "ignore").decode("ascii")
    return re.sub(r'["%;\\]', "_", text).strip()


def content_disposition(kind: str, filename: str) -> str:
    """``inline``/``attachment`` with an ASCII ``filename`` and the exact name as RFC 5987 ``filename*``."""
    name = unicodedata.normalize("NFC", filename or "")
    name = "".join(char for char in name if char.isprintable()).replace("/", "-").replace("\\", "-").strip()
    name = name or "dokument"
    stem, ext = os.path.splitext(name)
    if not ext and _SUFFIX_RE.fullmatch(stem.lower()):
        stem, ext = "", stem  # ".pdf" is only an extension
    elif not _SUFFIX_RE.fullmatch(ext.lower()):
        stem, ext = name, ""
    fallback_stem = _ascii_name(stem).lstrip(".")
    if not fallback_stem.strip(" ._-"):
        fallback_stem = "dokument"
    return f"{kind}; filename=\"{fallback_stem}{ext}\"; filename*=UTF-8''{quote(name, safe='')}"


def _etag(st: os.stat_result) -> str:
    return f'"{st.st_mtime_ns:x}-{st.st_size:x}"'


def _not_modified(request: Request, etag: str) -> bool:
    header = request.headers.get("if-none-match")
    if not header:
        return False
    candidates = {part.strip().removeprefix("W/") for part in header.split(",")}
    return "*" in candidates or etag in candidates


def _send_file(
    request: Request,
    path: Path,
    *,
    media_type: str,
    headers: dict[str, str],
) -> Response:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        raise NotFound("Datei nicht gefunden.") from None
    if not stat.S_ISREG(st.st_mode):
        raise NotFound("Datei nicht gefunden.")
    etag = _etag(st)
    headers = {**headers, "ETag": etag}
    if _not_modified(request, etag):
        return Response(status_code=304, headers=headers)
    return FileResponse(path, media_type=media_type, headers=headers, stat_result=st)


def file_response(
    request: Request,
    path: Path,
    filename: str,
    *,
    download: bool = False,
    media_type: str | None = None,
) -> Response:
    guessed = media_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    inline = not download and guessed in INLINE_TYPES
    served = guessed if guessed in INLINE_TYPES else "application/octet-stream"
    headers = {
        "Content-Disposition": content_disposition("inline" if inline else "attachment", filename),
        "Cache-Control": "private, no-cache",
    }
    return _send_file(request, path, media_type=served, headers=headers)


_state_lock = threading.Lock()


def _per_household(request: Request, name: str, create: Callable[[], Any]) -> Any:
    """Application state kept apart per household (``app.state.<name>``).

    With a single household it is one object; with several, one per household
    in ``app.state.<name>_by_household``.
    """
    state = request.app.state
    key = household_key(request)
    if key is None:
        value = getattr(state, name, None)
        if value is None:
            with _state_lock:
                value = getattr(state, name, None)
                if value is None:
                    value = create()
                    setattr(state, name, value)
        return value
    with _state_lock:
        values = getattr(state, name + "_by_household", None)
        if values is None:
            values = {}
            setattr(state, name + "_by_household", values)
        value = values.get(key)
        if value is None:
            value = values[key] = create()
        return value


def _thumbnails(request: Request) -> Any:
    from ..pdf.render import ThumbnailCache

    services = _services(request)
    return _per_household(request, "thumbnails", lambda: ThumbnailCache(services.layout.cache))


def _page_png(request: Request, pdf: Path, page: int, width: int, key: str, *, version: str | None = None) -> Response:
    """The PNG of one page, or a placeholder image if the file cannot be rendered
    (password protected, damaged). A page that does not exist stays a 404."""
    if page < 1:
        raise NotFound(f"Seite {page} existiert nicht.")
    try:
        png = _thumbnails(request).get(pdf, page, width, key=key)
    except NotFound:
        raise
    except DocuShelfError as exc:
        log.info("no page preview for %s (page %d): %s", pdf.name, page, exc)
        return _send_file(
            request, PAGE_PLACEHOLDER, media_type="image/svg+xml", headers={"Cache-Control": "private, no-cache"}
        )
    long_lived = bool(version) and version == key
    headers = {"Cache-Control": "private, max-age=31536000, immutable" if long_lived else "private, no-cache"}
    return _send_file(request, png, media_type="image/png", headers=headers)


def _inside(base: Path, rel: str) -> Path:
    """``base/rel`` for a relative path that must not leave ``base``."""
    rel = check_relative(rel)
    path = base / rel
    root = os.path.realpath(base)
    resolved = os.path.realpath(path)
    if not resolved.startswith(root + os.sep):
        raise PathError("Ungültiger Dateipfad")
    return path


def _first_original_pdf(item: Any) -> Path | None:
    for rel in item.original_files[:1]:
        path = _inside(item.dir, rel)
        try:
            with open(path, "rb") as handle:
                if handle.read(1024).lstrip(b"\x00\t\r\n ").startswith(b"%PDF-"):
                    return path
        except OSError:
            return None
    return None


def _request_scan(services: Any, reason: str) -> None:
    watcher = getattr(services, "watcher", None)
    if watcher is None:
        return
    try:
        watcher.request_scan(reason)
    except Exception:
        log.warning("cannot request a reconciliation pass", exc_info=True)


def _call(fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        return fn()
    except Exception:
        log.debug("status part not available", exc_info=True)
        return default


# ---------------------------------------------------------------------------
# Background jobs (rebuild, verifying reconciliation)


class BackgroundJobs:
    """Runs long maintenance tasks in a thread, one at a time per name."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._state: dict[str, dict[str, Any]] = {}
        self.cancel = threading.Event()

    def running(self, name: str) -> bool:
        thread = self._threads.get(name)
        return thread is not None and thread.is_alive()

    def start(self, name: str, fn: Callable[[], Any]) -> bool:
        with self._lock:
            if self.running(name):
                return False
            state = {"running": True, "started_at": now_iso(), "finished_at": None, "result": None, "error": None}
            self._state[name] = state
            thread = threading.Thread(target=self._run, args=(name, fn, state), name=f"docushelf-{name}", daemon=True)
            self._threads[name] = thread
            thread.start()
        return True

    def _run(self, name: str, fn: Callable[[], Any], state: dict[str, Any]) -> None:
        try:
            result = fn()
            state["result"] = result.to_dict() if hasattr(result, "to_dict") else result
        except Exception as exc:
            log.exception("%s failed", name)
            state["error"] = str(exc) or type(exc).__name__
        finally:
            state["running"] = False
            state["finished_at"] = now_iso()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {name: dict(state) for name, state in self._state.items()}

    def stop(self, timeout: float = 5.0) -> None:
        self.cancel.set()
        for name, thread in list(self._threads.items()):
            if thread.is_alive():
                thread.join(timeout)
                if thread.is_alive():
                    log.warning("%s is still running while the application stops", name)


def _jobs(request: Request) -> BackgroundJobs:
    """The maintenance jobs of the request's household."""
    return _per_household(request, "background_jobs", BackgroundJobs)


def stop_jobs(app: Any) -> None:
    """Called when the application shuts down."""
    jobs = getattr(app.state, "background_jobs", None)
    if jobs is not None:
        jobs.stop()
    with _state_lock:
        per_household = list((getattr(app.state, "background_jobs_by_household", None) or {}).values())
    for jobs in per_household:
        jobs.stop()


# ---------------------------------------------------------------------------
# Status


@router.get("/status")
def get_status(request: Request, user: User = Depends(require_user)) -> dict[str, Any]:
    services = _services(request)
    settings = get_settings(request)
    # Mailbox and automatic filing can differ per household.
    own = getattr(services, "settings", None) or settings

    def ocr() -> dict[str, Any]:
        from ..pdf.ocr import ocr_available, ocr_version

        return {"enabled": bool(settings.ocr_enabled), "available": ocr_available(), "version": ocr_version()}

    result: dict[str, Any] = {
        "version": __version__,
        "user": user.to_dict(),
        "inbox": _call(lambda: services.inbox.counts()),
        "index": _call(lambda: services.index.stats()),
        "worker": _call(lambda: services.worker.status()),
        "intake": _call(lambda: services.intake.status()),
        "watcher": _call(lambda: services.watcher.status()),
        "jobs": _jobs(request).status(),
        "ocr": _call(ocr),
        "rules_error": _call(lambda: services.rules.error),
        "tokens_error": _call(lambda: services.tokens.error),
        "features": {
            "oidc": bool(settings.oidc_enabled),
            "local_login": bool(settings.local_login_enabled),
            "imap": own.imap is not None,
            "smtp": settings.smtp is not None,
            "llm": settings.llm is not None,
            "auto_file": str(getattr(getattr(own, "auto_file", None), "mode", None) or "off"),
        },
        "max_upload_mb": settings.max_upload_mb,
    }
    if household_key(request) is not None:
        household = services.household
        result["household"] = {"id": household.id, "name": household.name}
    return result


# ---------------------------------------------------------------------------
# Upload


def _client_filename(raw: str | None, position: int) -> str:
    name = unicodedata.normalize("NFC", (raw or "").replace("\\", "/").rsplit("/", 1)[-1])
    name = "".join(char for char in name if char.isprintable()).strip()
    return name or f"datei-{position}"


def _upload_size(upload: UploadFile) -> int:
    if upload.size is not None:
        return int(upload.size)
    handle = upload.file
    position = handle.tell()
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    handle.seek(position)
    return size


def _name_list(names: list[str]) -> str:
    shown = ", ".join(f"„{name}“" for name in names[:5])
    return shown + (f" und {len(names) - 5} weitere" if len(names) > 5 else "")


def store_upload(services: Any, uploads: list[tuple[str, Any]], *, combine: bool, user: str) -> dict[str, Any]:
    """Write uploaded files as one batch into the upload drop zone (``_sendung.json`` last).

    ``uploads`` are ``(original name, file object)`` pairs. With ``combine`` all
    files become one document, otherwise every file is its own document.
    Unsupported or empty files reject the whole upload; nothing is written then.
    """
    from ..channels.sendung import write_sendung
    from ..pdf.normalize import detect_kind

    with tempfile.TemporaryDirectory(prefix="docushelf-upload-") as tmp:
        staged: list[tuple[str, Path]] = []
        empty: list[str] = []
        unsupported: list[str] = []
        for position, (name, fileobj) in enumerate(uploads, start=1):
            suffix = os.path.splitext(name)[1].lower()
            path = Path(tmp) / f"{position:04d}{suffix if _SUFFIX_RE.fullmatch(suffix) else ''}"
            fileobj.seek(0)
            with open(path, "xb") as handle:
                shutil.copyfileobj(fileobj, handle, MB)
            if path.stat().st_size == 0:
                empty.append(name)
            elif detect_kind(path) is None:
                unsupported.append(name)
            else:
                staged.append((name, path))
        if empty:
            raise HTTPException(status_code=400, detail=f"Leere Datei: {_name_list(empty)}.")
        if unsupported:
            raise HTTPException(
                status_code=415,
                detail=f"Dateityp nicht unterstützt: {_name_list(unsupported)}. Möglich sind PDF-Dateien und Bilder "
                "(JPEG, PNG, TIFF, HEIC, WebP …).",
            )
        if combine:
            documents = [[(name, path) for name, path in staged]]
        else:
            documents = [[(name, path)] for name, path in staged]
        context = {"uploader": user, "original_names": [name for name, _path in staged]}
        options = {"combine": True} if combine and len(staged) > 1 else {}
        source_dir = services.layout.source_dir("upload")
        source_dir.mkdir(parents=True, exist_ok=True)
        batch_dir = write_sendung(source_dir, documents, context=context, options=options)
    return {"batch_id": batch_dir.name, "documents": len(documents), "files": [name for name, _path in staged]}


@router.post("/upload", status_code=202)
async def upload(request: Request, user: User = Depends(require_user)) -> JSONResponse:
    services = _services(request)
    settings = get_settings(request)
    limit = max(1, int(settings.max_upload_mb)) * MB
    too_large = f"Die Dateien sind zu groß (höchstens {settings.max_upload_mb} MB pro Upload)."
    if _media_type(request) != "multipart/form-data":
        raise HTTPException(status_code=415, detail="Dateien bitte als multipart/form-data im Feld „files“ senden.")
    declared = _content_length(request)
    if declared is not None and declared > limit + MB:
        raise HTTPException(status_code=413, detail=too_large)
    async with request.form() as form:
        uploads = [
            (_client_filename(value.filename, position), value)
            for position, (key, value) in enumerate(form.multi_items(), start=1)
            if key in ("files", "file") and isinstance(value, UploadFile)
        ]
        if not uploads:
            raise HTTPException(status_code=400, detail="Es wurde keine Datei übermittelt (Feld „files“).")
        if len(uploads) > MAX_UPLOAD_FILES:
            raise HTTPException(status_code=400, detail=f"Zu viele Dateien auf einmal (höchstens {MAX_UPLOAD_FILES}).")
        if sum(_upload_size(fileobj) for _name, fileobj in uploads) > limit:
            raise HTTPException(status_code=413, detail=too_large)
        combine = _as_bool(form.get("combine") if isinstance(form.get("combine"), str) else None, "combine")
        result = await run_in_threadpool(
            store_upload,
            services,
            [(name, value.file) for name, value in uploads],
            combine=combine,
            user=user.username,
        )
    intake = getattr(services, "intake", None)
    if intake is not None:
        try:
            intake.trigger()
        except Exception:
            log.warning("cannot wake the intake; the upload is picked up at the next poll", exc_info=True)
    _audit(services, user.username, "upload", batch=result["batch_id"], files=len(result["files"]))
    return JSONResponse(result, status_code=202)


# ---------------------------------------------------------------------------
# Inbox


def _states(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ACTIVE_STATES
    if raw.strip().lower() == "all":
        return ALL_STATES
    states = tuple(dict.fromkeys(part.strip().lower() for part in raw.split(",") if part.strip()))
    unknown = [state for state in states if state not in ALL_STATES]
    if unknown or not states:
        raise HTTPException(
            status_code=400,
            detail=f"Unbekannter Zustand: {', '.join(unknown) or raw}. Möglich: {', '.join(ALL_STATES)}, all.",
        )
    return states


@router.get("/inbox")
def list_inbox(
    request: Request,
    state: str | None = None,
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    inbox = _services(request).inbox
    items = [item for name in _states(state) for item in inbox.iter_state(name)]
    return {
        "items": [item_summary(item) for item in items[offset : offset + limit]],
        "total": len(items),
        "counts": inbox.counts(),
    }


@router.post("/inbox/file-batch")
def file_batch(request: Request, data: dict[str, Any] = Depends(request_data), user: User = Depends(require_user)) -> dict[str, Any]:
    """File several ready items, each with its top suggestion (optionally with overrides).

    An item may carry ``expect``, the fingerprint of the suggestion the client
    showed (``views.suggestion_fingerprint``). Such an item is refused if its
    top suggestion changed since, or if it became a possible duplicate and
    ``accept_duplicate`` is not set.
    """
    services = _services(request)
    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise HTTPException(status_code=400, detail="„items“ muss eine Liste von Eingangsdokumenten sein.")
    if len(raw_items) > MAX_BATCH_ITEMS:
        raise HTTPException(status_code=400, detail=f"Höchstens {MAX_BATCH_ITEMS} Dokumente auf einmal.")
    requests: dict[str, dict[str, Any]] = {}
    for entry in raw_items:
        if isinstance(entry, str):
            requests.setdefault(entry, {})
        elif isinstance(entry, dict) and isinstance(entry.get("id"), str):
            requests.setdefault(entry["id"], {k: v for k, v in entry.items() if k != "id"})
        else:
            raise HTTPException(status_code=400, detail="Jeder Eintrag in „items“ braucht eine id.")
    filed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for item_id, overrides in requests.items():
        expect = overrides.pop("expect", None)
        accept_duplicate = overrides.pop("accept_duplicate", False)
        try:
            if expect is not None and not isinstance(expect, str):
                raise HTTPException(status_code=400, detail="„expect“ muss ein Text sein.")
            accept_duplicate = _as_bool(accept_duplicate, "accept_duplicate")
            item = _load_item(services, item_id)
            if expect:
                # Part of the web interface; imported here so the API also works without it.
                from .views import batch_refusal

                refusal = batch_refusal(item, expect, duplicate_seen=accept_duplicate)
                if refusal:
                    failed.append({"item": item_id, "error": refusal})
                    continue
            filing = filing_request(item, overrides, require_suggestion=True)
            record = services.ops.file_item(item_id, filing, user=user.username)
        except HTTPException as exc:
            failed.append({"item": item_id, "error": str(exc.detail)})
        except DocuShelfError as exc:
            failed.append({"item": item_id, "error": str(exc) or type(exc).__name__})
        except Exception:
            log.exception("filing %s failed", item_id)
            failed.append({"item": item_id, "error": "Interner Fehler beim Ablegen. Details stehen im Server-Log."})
        else:
            filed.append({"item": item_id, "document": record.to_dict()})
    return {"filed": filed, "failed": failed, "filed_count": len(filed), "failed_count": len(failed)}


@router.get("/inbox/{item_id}")
def get_inbox_item(request: Request, item_id: str) -> dict[str, Any]:
    services = _services(request)
    return item_detail(_load_item(services, item_id), services.inbox)


def filing_request(item: Any, data: dict[str, Any], *, require_suggestion: bool = False) -> FilingRequest:
    """Build the filing request from the body; missing values come from a suggestion.

    ``suggestion`` selects a suggestion by position (0 = best); otherwise the
    best one fills in whatever the body does not contain.
    """
    base: dict[str, Any] | None = None
    if "suggestion" in data and data["suggestion"] is not None:
        try:
            base = item.suggestions[int(data["suggestion"])]
        except (ValueError, TypeError, IndexError):
            raise HTTPException(status_code=400, detail="Diesen Vorschlag gibt es nicht.") from None
    elif require_suggestion or "folder" not in data or "title" not in data:
        base = item.top_suggestion
    if require_suggestion and not data.get("folder") and not (base and base.get("folder") is not None):
        raise HTTPException(status_code=400, detail="Für dieses Dokument gibt es keinen Ablagevorschlag.")
    merged: dict[str, Any] = {}
    if base:
        merged = {
            "folder": base.get("folder"),
            "title": base.get("title"),
            "doc_date": base.get("doc_date"),
            "tags": list(base.get("tags") or []),
            "sender": base.get("sender"),
        }
    if "folder" in data:
        merged["folder"] = _text(data["folder"], "folder")
    if "title" in data:
        merged["title"] = _text(data["title"], "title")
    for key in ("doc_date", "date"):
        if key in data:
            merged["doc_date"] = None if data[key] in (None, "") else _date_value(data[key], key).isoformat()
    if "tags" in data:
        merged["tags"] = _tags(data["tags"])
    if "sender" in data:
        merged["sender"] = None if data["sender"] is None else _text(data["sender"], "sender")
    if "create_rule" in data:
        merged["create_rule"] = _as_bool(data["create_rule"], "create_rule")
    if merged.get("folder") is None:
        raise HTTPException(status_code=400, detail="Bitte einen Zielordner angeben.")
    if not str(merged.get("title") or "").strip():
        raise HTTPException(status_code=400, detail="Bitte einen Titel angeben.")
    if merged.get("doc_date") and parse_date(merged["doc_date"]) is None:
        merged["doc_date"] = None
    return FilingRequest.from_mapping(merged)


@router.post("/inbox/{item_id}/file")
def file_inbox_item(
    request: Request,
    item_id: str,
    data: dict[str, Any] = Depends(request_data),
    user: User = Depends(require_user),
) -> dict[str, Any]:
    services = _services(request)
    item = _load_item(services, item_id)
    record = services.ops.file_item(item_id, filing_request(item, data), user=user.username)
    return {"item": item_id, "document": record.to_dict()}


@router.post("/inbox/{item_id}/retry")
def retry_inbox_item(request: Request, item_id: str, data: dict[str, Any] = Depends(request_data)) -> dict[str, Any]:
    services = _services(request)
    _load_item(services, item_id)
    item = services.pipeline.retry(
        item_id,
        skip_blank_removal=_optional_bool(data, "skip_blank_removal"),
        skip_ocr=_optional_bool(data, "skip_ocr"),
    )
    return item_summary(item)


@router.post("/inbox/{item_id}/refresh")
def refresh_inbox_item(request: Request, item_id: str) -> dict[str, Any]:
    services = _services(request)
    _load_item(services, item_id)
    return item_detail(services.pipeline.refresh_suggestions(item_id))


@router.delete("/inbox/{item_id}")
def delete_inbox_item(request: Request, item_id: str, user: User = Depends(require_user)) -> dict[str, Any]:
    services = _services(request)
    _load_item(services, item_id)
    entry_id = services.ops.delete_inbox_item(item_id, user=user.username)
    return {"item": item_id, "trash_id": entry_id}


def _pdf_name(item: Any) -> str:
    stem = os.path.splitext(item.display_name)[0].strip() or item.id
    return stem + ".pdf"


@router.get("/inbox/{item_id}/document")
def inbox_document(request: Request, item_id: str, download: bool = False) -> Response:
    item = _load_item(_services(request), item_id)
    if not item.document_path.is_file():
        raise NotFound("Das Dokument ist noch nicht verarbeitet.")
    return file_response(request, item.document_path, _pdf_name(item), download=download, media_type="application/pdf")


@router.get("/inbox/{item_id}/original/{number}")
def inbox_original(request: Request, item_id: str, number: int, download: bool = False) -> Response:
    item = _load_item(_services(request), item_id)
    if number < 1 or number > len(item.original_files):
        raise NotFound("Originaldatei nicht gefunden.")
    rel = item.original_files[number - 1]
    return file_response(request, _inside(item.dir, rel), rel.rsplit("/", 1)[-1], download=download)


@router.get("/inbox/{item_id}/pages/{page}.png")
def inbox_page(request: Request, item_id: str, page: int, w: int = DEFAULT_PAGE_WIDTH) -> Response:
    item = _load_item(_services(request), item_id)
    pdf: Path | None = item.document_path if item.document_path.is_file() else _first_original_pdf(item)
    if pdf is None:
        raise NotFound("Für dieses Dokument gibt es noch keine Vorschau.")
    try:
        mtime = os.stat(pdf).st_mtime_ns
    except FileNotFoundError:
        raise NotFound("Für dieses Dokument gibt es noch keine Vorschau.") from None
    kind = "d" if pdf == item.document_path else "o"
    return _page_png(request, pdf, page, w, f"{item.id}-{kind}{mtime}")


# ---------------------------------------------------------------------------
# Search


@router.get("/search")
def search(
    request: Request,
    q: str = "",
    folder: str | None = None,
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    tag: list[str] = Query(default=[]),
    sender: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    from ..index.search import parse_query

    query = parse_query(q or "")
    if folder and folder.strip("/ "):
        query.folder = check_relative(folder)
    if date_from:
        query.date_from = _date_value(date_from, "from")
    if date_to:
        query.date_to = _date_value(date_to, "to")
    if query.date_from and query.date_to and query.date_from > query.date_to:
        query.date_from, query.date_to = query.date_to, query.date_from
    query.tags.extend(value.strip() for value in tag if value.strip())
    if sender and sender.strip():
        query.sender = sender.strip()
    query.limit = limit
    query.offset = offset
    return _services(request).searcher.search(query).to_dict()


@router.get("/suggest")
def suggest(request: Request, prefix: str = "", limit: int = Query(8, ge=1, le=50)) -> dict[str, Any]:
    return {"prefix": prefix, "terms": _services(request).searcher.suggest_terms(prefix, limit)}


# ---------------------------------------------------------------------------
# Documents


def metadata_update(data: dict[str, Any]) -> MetadataUpdate:
    update = MetadataUpdate()
    if "title" in data:
        update.title = _text(data["title"], "title")
    for key in ("doc_date", "date"):
        if key in data:
            if data[key] in (None, ""):
                update.clear_date = True
            else:
                update.doc_date = _date_value(data[key], key)
    if _as_bool(data.get("clear_date"), "clear_date"):
        update.clear_date = True
        update.doc_date = None
    if "tags" in data:
        update.tags = [] if data["tags"] is None else _tags(data["tags"])
    if "sender" in data:
        # "" (or null) removes the sender.
        update.sender = "" if data["sender"] is None else _text(data["sender"], "sender")
    if "folder" in data:
        update.folder = _text(data["folder"], "folder")
    return update


@router.get("/documents/{doc_id}")
def get_document(request: Request, doc_id: str, text: bool = False) -> dict[str, Any]:
    services = _services(request)
    record = _load_record(services, doc_id)
    data = record.to_dict()
    if text:
        data["pages"] = services.index.get_pages(record.id)
    return data


@router.patch("/documents/{doc_id}")
def update_document(
    request: Request,
    doc_id: str,
    data: dict[str, Any] = Depends(request_data),
    user: User = Depends(require_user),
) -> dict[str, Any]:
    services = _services(request)
    _load_record(services, doc_id)
    record = services.ops.update_document(doc_id, metadata_update(data), user=user.username)
    return record.to_dict()


@router.delete("/documents/{doc_id}")
def delete_document(request: Request, doc_id: str, user: User = Depends(require_user)) -> dict[str, Any]:
    services = _services(request)
    _load_record(services, doc_id)
    return {"document": doc_id, "trash_id": services.ops.delete_document(doc_id, user=user.username)}


def _document_path(services: Any, record: Any) -> Path:
    path = services.layout.archive_path(record.path)
    if not path.is_file():
        _request_scan(services, f"missing {record.path}")
        raise NotFound("Die Datei liegt nicht mehr im Archiv. Der Index wird aktualisiert.")
    return path


@router.get("/documents/{doc_id}/file")
def document_file(request: Request, doc_id: str, download: bool = False) -> Response:
    services = _services(request)
    record = _load_record(services, doc_id)
    return file_response(request, _document_path(services, record), record.filename, download=download)


@router.get("/documents/{doc_id}/pages/{page}.png")
def document_page(request: Request, doc_id: str, page: int, w: int = DEFAULT_PAGE_WIDTH, v: str | None = None) -> Response:
    services = _services(request)
    record = _load_record(services, doc_id)
    if not record.is_pdf:
        raise NotFound("Für diesen Dateityp gibt es keine Seitenvorschau.")
    return _page_png(request, _document_path(services, record), page, w, record.content_hash, version=v)


# ---------------------------------------------------------------------------
# Folders


@router.get("/folders")
def list_folders(request: Request, parent: str | None = None) -> dict[str, Any]:
    index = _services(request).index
    counts = index.folder_counts()
    paths = index.subfolders(check_relative(parent)) if parent is not None else index.list_folders()
    folders = [
        {
            "path": path,
            "name": path.rsplit("/", 1)[-1],
            "parent": path.rsplit("/", 1)[0] if "/" in path else "",
            "documents": counts.get(path, 0),
        }
        for path in paths
    ]
    return {"folders": folders, "root_documents": counts.get("", 0)}


@router.post("/folders", status_code=201)
def create_folder(request: Request, data: dict[str, Any] = Depends(request_data), user: User = Depends(require_user)) -> dict[str, Any]:
    value = data.get("path", data.get("folder"))
    if value is None:
        raise HTTPException(status_code=400, detail="Bitte den Ordner im Feld „path“ angeben.")
    return {"path": _services(request).ops.create_folder(_text(value, "path"), user=user.username)}


@router.patch("/folders")
def rename_folder(request: Request, data: dict[str, Any] = Depends(request_data), user: User = Depends(require_user)) -> dict[str, Any]:
    if "path" not in data or "name" not in data:
        raise HTTPException(status_code=400, detail="Bitte „path“ (bisheriger Ordner) und „name“ (neuer Name) angeben.")
    old = _relative(data["path"])
    new = _services(request).ops.rename_folder(old, _text(data["name"], "name"), user=user.username)
    return {"path": new, "old_path": old}


@router.delete("/folders")
def delete_folder(
    request: Request,
    path: str | None = None,
    data: dict[str, Any] = Depends(request_data),
    user: User = Depends(require_user),
) -> dict[str, Any]:
    value = path if path is not None else data.get("path")
    if value is None:
        raise HTTPException(status_code=400, detail="Bitte den Ordner im Parameter „path“ angeben.")
    folder = _relative(value)
    _services(request).ops.delete_folder(folder, user=user.username)
    return {"path": folder, "deleted": True}


# ---------------------------------------------------------------------------
# Trash


def trash_entry(entry: Any, trash: Any) -> dict[str, Any]:
    expires = None
    expires_at = getattr(trash, "expires_at", None)
    if expires_at is not None:
        value = _call(lambda: expires_at(entry))
        expires = value.isoformat(timespec="seconds") if value is not None else None
    return {
        "id": entry.id,
        "kind": entry.kind,
        "deleted_at": entry.deleted_at,
        "deleted_by": entry.deleted_by,
        "title": entry.title,
        "original_path": entry.original_path,
        "folder": getattr(entry, "folder", None),
        "item_id": entry.item_id,
        "doc_id": entry.doc_id,
        "files": [Path(path).name for path in entry.files],
        "expires_at": expires,
        "meta": entry.meta,
    }


@router.get("/trash")
def list_trash(request: Request) -> dict[str, Any]:
    trash = _services(request).trash
    return {"items": [trash_entry(entry, trash) for entry in trash.list()]}


@router.post("/trash/{entry_id}/restore")
def restore_trash_entry(request: Request, entry_id: str, user: User = Depends(require_user)) -> dict[str, Any]:
    restored = _services(request).trash.restore(entry_id, user=user.username)
    if hasattr(restored, "original_files"):
        return {"kind": "inbox", "item": item_summary(restored)}
    return {"kind": "document", "document": restored.to_dict()}


@router.delete("/trash/{entry_id}")
def purge_trash_entry(request: Request, entry_id: str, user: User = Depends(require_session_user)) -> dict[str, Any]:
    """Delete a trash entry for good. Needs a browser login: a lost or leaked API
    token can at most move documents into the trash, never destroy them."""
    _services(request).trash.purge(entry_id, user=user.username)
    return {"id": entry_id, "purged": True}


# ---------------------------------------------------------------------------
# Rules


def _rules_payload(store: Any) -> dict[str, Any]:
    rules = store.load()
    return {"text": store.raw_text(), "rules": [rule.to_dict() for rule in rules], "error": store.error}


@router.get("/rules")
def get_rules(request: Request) -> dict[str, Any]:
    return _rules_payload(_services(request).rules)


@router.put("/rules")
async def put_rules(request: Request, user: User = Depends(require_user)) -> dict[str, Any]:
    """Replace ``rules.yaml``: JSON ``{"text": "<yaml>"}`` or the YAML itself as text."""
    services = _services(request)
    media_type = _media_type(request)
    is_json = media_type == "application/json" or media_type.endswith("+json")
    is_text = not media_type or media_type.startswith("text/") or media_type in ("application/yaml", "application/x-yaml")
    if not (is_json or is_text):
        raise HTTPException(status_code=415, detail="Bitte JSON ({\"text\": …}) oder YAML-Text senden.")
    raw = await read_body(request)
    if is_json:
        try:
            data = json.loads(raw or b"{}")
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="Der Inhalt ist kein gültiges JSON.") from None
        text = data.get("text") if isinstance(data, dict) else None
        if not isinstance(text, str):
            raise HTTPException(status_code=400, detail="„text“ mit dem Inhalt der Regeldatei fehlt.")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="Die Regeldatei muss UTF-8-kodiert sein.") from None
    store = services.rules
    rules = await run_in_threadpool(store.save_raw_text, text)
    _audit(services, user.username, "rules_update", rules=len(rules))
    return await run_in_threadpool(_rules_payload, store)


# ---------------------------------------------------------------------------
# Maintenance


@router.post("/admin/reconcile")
def reconcile(request: Request, wait: bool = False, verify: bool = False, user: User = Depends(require_user)) -> Response:
    """Compare the index with the archive. ``verify`` re-reads every file; ``wait`` returns the result."""
    services = _services(request)
    if wait:
        stats = services.reconciler.scan(full=True, verify=verify)
        return JSONResponse({"result": stats.to_dict()})
    if verify:
        jobs = _jobs(request)
        if not jobs.start("reconcile", lambda: services.reconciler.scan(full=True, verify=True, cancel=jobs.cancel)):
            raise HTTPException(status_code=409, detail="Ein Abgleich läuft bereits.")
        return JSONResponse({"started": True}, status_code=202)
    _request_scan(services, f"manual request by {user.username}")
    return JSONResponse({"queued": True}, status_code=202)


@router.post("/admin/rebuild")
def rebuild(request: Request, wait: bool = False, user: User = Depends(require_user)) -> Response:
    """Throw the index away and rebuild it from the files (in the background unless ``wait``)."""
    services = _services(request)
    jobs = _jobs(request)
    if jobs.running("rebuild"):
        raise HTTPException(status_code=409, detail="Der Index wird bereits neu aufgebaut.")
    if wait:
        stats = services.rebuild(user=user.username)
        return JSONResponse({"result": stats.to_dict()})
    if not jobs.start("rebuild", lambda: services.rebuild(user=user.username)):
        raise HTTPException(status_code=409, detail="Der Index wird bereits neu aufgebaut.")
    return JSONResponse({"started": True}, status_code=202)


# ---------------------------------------------------------------------------
# API tokens


@router.get("/tokens")
def list_tokens(request: Request) -> dict[str, Any]:
    store = _services(request).tokens
    return {"tokens": store.list(), "error": store.error}


@router.post("/tokens", status_code=201)
def create_token(
    request: Request,
    data: dict[str, Any] = Depends(request_data),
    user: User = Depends(require_session_user),
) -> JSONResponse:
    services = _services(request)
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(status_code=400, detail="Bitte einen Namen für das Token angeben.")
    # The token keeps working only while its owner would still be allowed in.
    token, entry = services.tokens.create(name, user.username, owner=user)
    _audit(services, user.username, "token_create", token=entry["id"], name=entry["name"])
    # The token is shown only now; it must not end up in any cache.
    return JSONResponse({"token": token, "entry": entry}, status_code=201, headers={"Cache-Control": "no-store"})


@router.delete("/tokens/{token_id}")
def revoke_token(request: Request, token_id: str, user: User = Depends(require_session_user)) -> dict[str, Any]:
    services = _services(request)
    entry = services.tokens.revoke(token_id)
    _audit(services, user.username, "token_revoke", token=entry["id"], name=entry["name"])
    return {"revoked": True, "entry": entry}


# ---------------------------------------------------------------------------
# Automatic filing


@router.get("/auto")
def list_auto_filings(
    request: Request,
    days: int = Query(60, ge=1, le=400),
    uncertain: bool = False,
) -> dict[str, Any]:
    """Recent automatic filings, newest first – the data of the page "Automatisch abgelegt".

    ``uncertain`` keeps only the uncertain filings nobody has checked yet.
    Each entry says how it was decided (confidence, signals, reasons), whether
    the document still exists (``current``) and whether a person checked it
    (``review``).
    """
    # Part of the web interface; imported here so the API also works without it.
    from . import views

    services = _services(request)
    filings = views.auto_filings(services, days=days)
    overview = views.auto_overview(services, filings)
    shown = [filing for filing in filings if filing.pending] if uncertain else filings
    return {
        "mode": overview.config.mode,
        "threshold": overview.config.threshold,
        "days": days,
        "summary": {"total": overview.total, "recent": overview.recent, "pending": overview.pending},
        "entries": [filing.to_dict() for filing in shown],
    }


@router.post("/auto/{doc_id}/ok")
def confirm_auto_filing(request: Request, doc_id: str, user: User = Depends(require_user)) -> dict[str, Any]:
    """Confirm an automatic filing ("Passt"). Confirming it again changes nothing."""
    from . import views

    services = _services(request)
    filing = views.find_auto_filing(services, doc_id, days=views.AUTO_CONFIRM_DAYS)
    if filing is None:
        raise NotFound("Zu diesem Dokument gibt es keine automatische Ablage.")
    if filing.reviewed:
        return {"doc_id": filing.doc_id, "status": "ok", "already": True, "review": filing.review}
    review = views.confirm_auto_filing(services, filing, user=user.username)
    return {"doc_id": filing.doc_id, "status": "ok", "already": False, "review": review}


# ---------------------------------------------------------------------------
# Audit log


@router.get("/audit")
def audit_log(request: Request, limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
    return {"entries": _services(request).audit.tail(limit)}
