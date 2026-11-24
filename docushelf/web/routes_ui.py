"""HTML pages and form actions of the web interface.

Pages are rendered on the server; JavaScript only enhances them (drag and
drop, live progress, comboboxes). Every form posts to a route here that checks
the login and the CSRF token, does its work through the service container and
redirects with a flash message (POST/redirect/GET). Input errors re-render the
page with what the user typed, so nothing has to be entered twice.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from ..errors import ConfigError, Conflict, DocuShelfError, NotFound, PathError
from ..households import MAX_MEMBER_LENGTH
from ..models import Analysis, MetadataUpdate
from ..util.ids import is_item_id
from ..util.names import check_relative, format_filename, sanitize_folder
from ..util.timeutil import parse_date
from . import security, views
from .security import User, get_services, require_user, verify_csrf
from .templating import render
from .views import flash, form_flag, form_value, redirect

log = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)

INBOX_PAGE_SIZE = 500
ARCHIVE_PAGE_SIZE = 200
SEARCH_PAGE_SIZE = 25
MAX_PREVIEW_PAGES = 60
LOG_TAIL_LINES = 60
TEXT_PREVIEW_CHARS = 6000
ARCHIVE_SORTS = {
    "date_desc": "Neueste zuerst",
    "date_asc": "Älteste zuerst",
    "title": "Titel A–Z",
    "title_desc": "Titel Z–A",
}
# How long the settings page waits for the language model test; a slower
# answer is kept and shown on the next visit.
LLM_PROBE_WAIT = 95.0
DEFAULT_UPLOAD_EXTENSIONS = frozenset(
    {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".heif", ".webp", ".gif", ".bmp"}
)
NEW_TOKEN_KEY = "_new_token"

# Managing API tokens needs a browser login; a token must not be able to mint more tokens.
require_session_user = getattr(security, "require_session_user", require_user)


# ---------------------------------------------------------------------------
# Helpers


def _username(user: Any) -> str | None:
    return getattr(user, "username", None) or None


def _page_number(value: Any) -> int:
    try:
        return max(1, min(int(value), 10_000))
    except (TypeError, ValueError):
        return 1


def _error_page(request: Request, user: Any, status_code: int, title: str, message: str) -> HTMLResponse:
    return render(
        request,
        "error.html",
        {"user": user, "status_code": status_code, "title": title, "message": message},
        status_code=status_code,
    )


def _not_found(request: Request, user: Any, message: str) -> HTMLResponse:
    return _error_page(request, user, 404, "Nicht gefunden", message)


async def _with_form(request: Request, handler: Callable[..., Response], *args: Any) -> Response:
    """Parse the submitted form and run the (blocking) handler in the thread pool."""
    form = await request.form()
    try:
        return await run_in_threadpool(handler, request, form, *args)
    finally:
        await form.close()  # removes spooled upload files right away


def _ready_ids(services: Any) -> list[str]:
    """Ids of the items waiting in ``bereit``, oldest first (same order as the inbox page)."""
    base = services.layout.ready
    try:
        names = os.listdir(base)
    except FileNotFoundError:
        return []
    return sorted(name for name in names if is_item_id(name) and (base / name).is_dir())


def _next_ready_id(services: Any, item_id: str) -> str | None:
    """The item to show after ``item_id`` was filed or deleted."""
    remaining = [i for i in _ready_ids(services) if i != item_id]
    later = [i for i in remaining if i > item_id]
    if later:
        return later[0]
    return remaining[0] if remaining else None


def _after(services: Any, form: Mapping[str, Any], item_id: str, default: str) -> str:
    after = form_value(form, "after")
    if after == "next":
        next_id = _next_ready_id(services, item_id)
        return f"/eingang/{next_id}" if next_id else "/eingang"
    return views.safe_next(after, default)


def _running_items(services: Any) -> set[str]:
    worker = getattr(services, "worker", None)
    if worker is None:
        return set()
    try:
        status = worker.status()
    except Exception:
        log.debug("cannot read worker status", exc_info=True)
        return set()
    return {
        str(job.get("key"))
        for job in status.get("current_jobs") or []
        if isinstance(job, Mapping) and job.get("kind") == "process"
    }


def _folders(services: Any) -> list[str]:
    try:
        return list(services.index.list_folders())
    except Exception:
        log.warning("cannot list folders", exc_info=True)
        return []


def _tag_names(services: Any) -> list[str]:
    try:
        return [tag for tag, _count in services.index.tags()]
    except Exception:
        log.warning("cannot list tags", exc_info=True)
        return []


def _sender_names(services: Any, limit: int = 300) -> list[str]:
    try:
        return [sender for sender, _count in services.index.senders()[:limit]]
    except Exception:
        log.warning("cannot list senders", exc_info=True)
        return []


def _log_tail(services: Any, item: Any, lines: int = LOG_TAIL_LINES) -> str:
    try:
        text = services.inbox.read_log(item)
    except Exception:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _originals(item: Any) -> list[dict[str, Any]]:
    files = []
    for number, rel in enumerate(item.original_files or [], start=1):
        name = rel.split("/", 1)[-1]
        files.append(
            {"number": number, "name": name, "url": f"/api/v1/inbox/{item.id}/original/{number}?download=1"}
        )
    return files


def _filename_preview(title: str, doc_date: Any, ext: str = ".pdf") -> str:
    try:
        return format_filename(title, parse_date(doc_date), ext) if title else ""
    except Exception:
        return ""


def _audit(services: Any, user: Any, action: str, **details: Any) -> None:
    audit = getattr(services, "audit", None)
    if audit is None:
        return
    try:
        audit.write(_username(user), action, **details)
    except Exception:
        log.warning("cannot write audit entry %s", action, exc_info=True)


def _supported_extensions() -> frozenset[str]:
    try:
        from ..pdf.normalize import SUPPORTED_EXT
    except Exception:  # pragma: no cover - optional dependency missing
        return DEFAULT_UPLOAD_EXTENSIONS
    return frozenset(SUPPORTED_EXT)


def _upload_accept() -> str:
    extensions = sorted(_supported_extensions())
    return ",".join(["application/pdf", "image/*", *extensions])


# ---------------------------------------------------------------------------
# Start


@router.get("/")
def home() -> RedirectResponse:
    return RedirectResponse("/eingang", status_code=302)


# ---------------------------------------------------------------------------
# Inbox


@router.get("/eingang", response_class=HTMLResponse)
def inbox_page(request: Request, seite: int = 1, user: User = Depends(require_user)) -> HTMLResponse:
    services = get_services(request)
    inbox = services.inbox
    page = _page_number(seite)
    ready = inbox.list("bereit")
    start = (page - 1) * INBOX_PAGE_SIZE
    exists = views.folder_checker(services.index)
    auto = _auto_overview(services)
    cards = [
        views.inbox_card(item, folder_exists=exists, auto=auto.enabled) for item in ready[start : start + INBOX_PAGE_SIZE]
    ]
    processing = [views.inbox_card(item) for item in inbox.list("arbeit")]
    counts = dict(inbox.counts())
    counts.setdefault("bereit", len(ready))
    return render(
        request,
        "inbox.html",
        {
            "user": user,
            "cards": cards,
            "processing": processing,
            "running": _running_items(services),
            "counts": counts,
            "page": page,
            "has_more": len(ready) > start + INBOX_PAGE_SIZE,
            "max_upload_mb": services.settings.max_upload_mb,
            "upload_accept": _upload_accept(),
            "auto": auto,
            "active_tab": "eingang",
        },
    )


def _auto_overview(services: Any, filings: list[views.AutoFiling] | None = None) -> views.AutoOverview:
    try:
        return views.auto_overview(services, filings)
    except Exception:
        log.warning("cannot summarize the automatic filings", exc_info=True)
        return views.AutoOverview(config=views.auto_settings(getattr(services, "settings", None)))


@router.get("/eingang/automatisch", response_class=HTMLResponse)
def inbox_auto_page(request: Request, unsicher: str = "", user: User = Depends(require_user)) -> HTMLResponse:
    """What automatic filing did in the last weeks; every decision can be confirmed or corrected."""
    services = get_services(request)
    filings = views.auto_filings(services)
    auto = _auto_overview(services, filings)
    only_uncertain = unsicher.strip().lower() in ("1", "ja", "true", "on")
    shown = [f for f in filings if f.pending] if only_uncertain else filings
    return render(
        request,
        "inbox_auto.html",
        {
            "user": user,
            "filings": filings,
            "shown": shown,
            "only_uncertain": only_uncertain,
            "auto": auto,
            "counts": dict(services.inbox.counts()),
            "window_days": views.AUTO_WINDOW_DAYS,
            "active_tab": "automatisch",
        },
    )


@router.post("/eingang/automatisch/{doc_id}/passt")
async def inbox_auto_confirm(
    request: Request, doc_id: str, user: User = Depends(require_user), _csrf: Any = Depends(verify_csrf)
) -> Response:
    return await _with_form(request, _confirm_auto_filing, user, doc_id)


def _confirm_auto_filing(request: Request, form: Any, user: Any, doc_id: str) -> Response:
    services = get_services(request)
    target = views.safe_next(form_value(form, "after"), "/eingang/automatisch")
    filing = views.find_auto_filing(services, doc_id, days=views.AUTO_CONFIRM_DAYS)
    if filing is None:
        flash(request, "Zu diesem Dokument gibt es keine automatische Ablage (mehr).", "warning")
        return redirect(target)
    if filing.reviewed:
        flash(request, f"„{filing.current_title}“ ist schon geprüft.", "info")
        return redirect(target)
    views.confirm_auto_filing(services, filing, user=_username(user))
    flash(request, f"Danke! „{filing.current_title}“ ist als geprüft markiert.")
    return redirect(target)


@router.get("/eingang/fehler", response_class=HTMLResponse)
def inbox_errors_page(request: Request, user: User = Depends(require_user)) -> HTMLResponse:
    services = get_services(request)
    failed = []
    for item in services.inbox.list("fehler"):
        card = views.inbox_card(item)
        failed.append(
            {
                "card": card,
                "item": item,
                "error": item.error or {},
                "log": _log_tail(services, item, 25),
                "originals": _originals(item),
            }
        )
    return render(request, "inbox_errors.html", {"user": user, "failed": failed})


@router.get("/eingang/{item_id}", response_class=HTMLResponse)
def inbox_item_page(request: Request, item_id: str, user: User = Depends(require_user)) -> HTMLResponse:
    services = get_services(request)
    item = services.inbox.find(item_id) if is_item_id(item_id) else None
    if item is None:
        return _not_found(
            request, user, "Dieses Dokument ist nicht (mehr) im Eingang – vielleicht wurde es schon abgelegt."
        )
    return _render_item(request, user, services, item)


def _default_form(item: Any, analysis: Analysis) -> dict[str, Any]:
    top = item.top_suggestion or {}
    title = str(top.get("title") or item.context.get("title") or "").strip()
    if not title:
        title = views.item_title(item)
    doc_date = parse_date(top.get("doc_date") or analysis.doc_date)
    tags = top.get("tags") or []
    return {
        "folder": str(top.get("folder") or ""),
        "title": title,
        "doc_date": doc_date.isoformat() if doc_date else "",
        "sender": str(top.get("sender") or analysis.sender or ""),
        "tags": ", ".join(str(t) for t in tags),
        "create_rule": False,
    }


def _render_item(
    request: Request,
    user: Any,
    services: Any,
    item: Any,
    *,
    form: dict[str, Any] | None = None,
    errors: list[str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    analysis = Analysis.from_dict(item.analysis)
    context: dict[str, Any] = {
        "user": user,
        "item": item,
        "title": views.item_title(item),
        "originals": _originals(item),
        "log": _log_tail(services, item),
        "errors": errors or [],
    }
    if item.state == "bereit":
        auto_on = views.auto_settings(services.settings).enabled
        protection = views.item_protection(item)
        ids = _ready_ids(services)
        position = ids.index(item.id) + 1 if item.id in ids else None
        values = form or _default_form(item, analysis)
        exists = views.folder_checker(services.index)
        context.update(
            {
                "form": values,
                "filename_preview": _filename_preview(values.get("title", ""), values.get("doc_date")),
                "suggestions": [
                    views.suggestion_view(s, folder_exists=exists) for s in item.suggestions or [] if isinstance(s, Mapping)
                ],
                "dates": views.date_choices(analysis),
                "facts": views.analysis_facts(analysis, item.context),
                "keywords": list(analysis.keywords or [])[:10],
                "duplicate": views.duplicate_link(item.duplicate_of),
                "protection_notice": views.protection_notice(protection),
                # Without the password not even the pages can be counted: one placeholder page instead.
                "preview_unavailable": protection == "encrypted" and not item.page_count,
                "auto_hold": views.auto_hold(item) if auto_on else None,
                "folders": _folders(services),
                "tags": _tag_names(services),
                "senders": _sender_names(services),
                "position": position,
                "total": len(ids),
                "prev_id": ids[position - 2] if position and position > 1 else None,
                "next_id": ids[position] if position and position < len(ids) else None,
                "preview_pages": min(int(item.page_count or 0), MAX_PREVIEW_PAGES),
            }
        )
    elif item.state == "arbeit":
        context["step"] = views.processing_step(item.stage)
        context["running"] = item.id in _running_items(services)
    elif item.state == "erledigt":
        filing = item.filing if isinstance(item.filing, Mapping) else {}
        doc_id = filing.get("record_id") or filing.get("doc_id")
        record = None
        if doc_id:
            try:
                record = services.index.get(doc_id)
            except Exception:
                record = None
        context["filed"] = {"record": record, "path": filing.get("path"), "at": filing.get("finished_at")}
        context["auto_done"] = views.auto_done(item)
    return render(request, "inbox_item.html", context, status_code=status_code)


@router.post("/eingang/hochladen")
async def inbox_upload(
    request: Request, user: User = Depends(require_user), _csrf: Any = Depends(verify_csrf)
) -> Response:
    return await _with_form(request, _store_upload, user)


class _TooLarge(Exception):
    pass


def _detect_kind(path: Path) -> str | None:
    """"pdf", "image" or None, judged by the content like the processing pipeline does."""
    try:
        from ..pdf.normalize import detect_kind
    except Exception:  # pragma: no cover - optional dependency missing
        ext = path.suffix.lower()
        return "pdf" if ext == ".pdf" else ("image" if ext in DEFAULT_UPLOAD_EXTENSIONS else None)
    return detect_kind(path)


def _stage_upload(upload: UploadFile, target: Path, budget: int) -> int:
    """Copy an uploaded file to ``target`` in chunks; raises _TooLarge past ``budget`` bytes."""
    written = 0
    upload.file.seek(0)
    with open(target, "xb") as handle:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > budget:
                raise _TooLarge
            handle.write(chunk)
    return written


def _store_upload(request: Request, form: Any, user: Any) -> Response:
    """Fallback for browsers without JavaScript (the page itself uploads through the API).

    Files are staged in a temporary directory, checked by content and then
    written as one batch into the upload drop zone. With "combine" all images
    become one document; PDFs always stay documents of their own.
    """
    from ..channels.sendung import write_sendung

    services = get_services(request)
    uploads = [f for f in form.getlist("files") if isinstance(f, UploadFile) and f.filename]
    if not uploads:
        flash(request, "Bitte mindestens eine Datei auswählen.", "error")
        return redirect("/eingang")
    max_mb = int(services.settings.max_upload_mb)
    budget = max_mb * 1024 * 1024
    skipped: list[str] = []
    with tempfile.TemporaryDirectory(prefix="docushelf-upload-") as tmp:
        staged: list[tuple[str, Path, str]] = []
        for position, upload in enumerate(uploads, start=1):
            name = (upload.filename or "datei").replace("\\", "/").rsplit("/", 1)[-1] or "datei"
            suffix = os.path.splitext(name)[1].lower()
            target = Path(tmp) / f"{position:04d}{suffix if re.fullmatch(r'\.[a-z0-9]{1,8}', suffix) else ''}"
            try:
                size = _stage_upload(upload, target, budget)
            except _TooLarge:
                flash(request, f"Die Dateien sind zu groß (zusammen höchstens {max_mb} MB).", "error")
                return redirect("/eingang")
            except OSError as exc:
                log.exception("cannot stage upload %s", name)
                flash(request, views.error_text(exc, "Die Dateien konnten nicht gespeichert werden."), "error")
                return redirect("/eingang")
            budget -= size
            kind = _detect_kind(target) if size else None
            if kind is None:
                skipped.append(name)
                continue
            staged.append((name, target, kind))
        if not staged:
            names = ", ".join(skipped[:5])
            flash(request, f"Diese Dateien lassen sich nicht verarbeiten (leer oder kein PDF/Bild): {names}", "error")
            return redirect("/eingang")

        combine = form_flag(form, "combine")
        images = [(name, path) for name, path, kind in staged if kind == "image"]
        if combine and len(images) > 1:
            documents = [[(name, path)] for name, path, kind in staged if kind != "image"] + [images]
        else:
            documents = [[(name, path)] for name, path, _kind in staged]
        try:
            write_sendung(
                services.layout.source_dir("upload"),
                documents,
                context={"uploader": _username(user), "original_names": [name for name, _p, _k in staged]},
                options={"combine": True} if any(len(document) > 1 for document in documents) else {},
            )
        except (OSError, ValueError, DocuShelfError) as exc:
            log.exception("cannot store upload")
            flash(request, views.error_text(exc, "Die Dateien konnten nicht gespeichert werden."), "error")
            return redirect("/eingang")
    intake = getattr(services, "intake", None)
    if intake is not None:
        try:
            intake.trigger()
        except Exception:
            log.warning("cannot wake the intake", exc_info=True)
    _audit(services, user, "upload", files=len(staged))
    flash(request, f"{views.plural(len(staged), 'Datei', 'Dateien')} hochgeladen – wird jetzt verarbeitet.")
    if skipped:
        flash(request, f"Übersprungen (leer oder kein PDF/Bild): {', '.join(skipped[:5])}", "warning")
    return redirect("/eingang")


@router.post("/eingang/sammelablage")
async def inbox_file_batch(
    request: Request, user: User = Depends(require_user), _csrf: Any = Depends(verify_csrf)
) -> Response:
    return await _with_form(request, _file_batch, user)


def _file_batch(request: Request, form: Any, user: Any) -> Response:
    """File the selected items with their top suggestion each (fallback without JavaScript).

    Each checkbox carries the fingerprint of the suggestion the page showed
    (``id:fingerprint``, ``:dup`` for items shown as possible duplicates); items
    whose suggestion changed since are left in the inbox.
    """
    services = get_services(request)
    selection = views.parse_batch_selection(form.getlist("items"))
    if not selection:
        flash(request, "Bitte zuerst Dokumente auswählen.", "info")
        return redirect("/eingang")
    filed = 0
    failures: list[str] = []
    for item_id, (expect, duplicate_seen) in selection.items():
        item = services.inbox.find(item_id)
        if item is None or item.state != "bereit":
            failures.append(f"{item_id}: nicht mehr im Eingang")
            continue
        title = views.item_title(item)
        refusal = views.batch_refusal(item, expect, duplicate_seen=duplicate_seen)
        if refusal:
            failures.append(f"„{title}“: {refusal}")
            continue
        request_data = views.filing_request_from_suggestion(item.top_suggestion)
        if request_data is None:
            failures.append(f"„{title}“: kein vollständiger Vorschlag")
            continue
        try:
            services.ops.file_item(item_id, request_data, user=_username(user))
            filed += 1
        except DocuShelfError as exc:
            failures.append(f"„{title}“: {views.error_text(exc, 'Ablage fehlgeschlagen')}")
        except Exception:
            log.exception("batch filing of %s failed", item_id)
            failures.append(f"„{title}“: unerwarteter Fehler")
    if filed:
        flash(request, f"{views.plural(filed, 'Dokument', 'Dokumente')} abgelegt.")
    if failures:
        shown = "; ".join(failures[:3])
        more = f" (und {len(failures) - 3} weitere)" if len(failures) > 3 else ""
        flash(request, f"Nicht abgelegt: {shown}{more}", "error")
    return redirect("/eingang")


@router.post("/eingang/{item_id}/ablegen")
async def inbox_file(
    request: Request, item_id: str, user: User = Depends(require_user), _csrf: Any = Depends(verify_csrf)
) -> Response:
    return await _with_form(request, _file_item, user, item_id)


def _form_values(form: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "folder": form_value(form, "folder"),
        "title": form_value(form, "title"),
        "doc_date": form_value(form, "doc_date"),
        "sender": form_value(form, "sender"),
        "tags": form_value(form, "tags"),
        "create_rule": form_flag(form, "create_rule"),
    }


def _file_item(request: Request, form: Any, user: Any, item_id: str) -> Response:
    services = get_services(request)
    item = services.inbox.find(item_id) if is_item_id(item_id) else None
    if item is None:
        flash(request, "Dieses Dokument ist nicht mehr im Eingang.", "warning")
        return redirect("/eingang")
    if item.state != "bereit":
        flash(request, "Das Dokument kann gerade nicht abgelegt werden.", "warning")
        return redirect(f"/eingang/{item_id}")

    values = _form_values(form)
    try:
        filing = views.filing_request_from_form(form)
        if not filing.title:
            raise ValueError("Bitte einen Titel angeben.")
        if not filing.folder:
            raise ValueError("Bitte einen Ordner angeben.")
        sanitize_folder(filing.folder)
    except (ValueError, PathError) as exc:
        return _render_item(request, user, services, item, form=values, errors=[str(exc)], status_code=400)

    try:
        record = services.ops.file_item(item_id, filing, user=_username(user))
    except NotFound as exc:
        flash(request, views.error_text(exc, "Dieses Dokument ist nicht mehr im Eingang."), "warning")
        return redirect("/eingang")
    except DocuShelfError as exc:
        fresh = services.inbox.find(item_id) or item
        if fresh.state != "bereit":
            flash(request, views.error_text(exc, "Ablegen fehlgeschlagen."), "error")
            return redirect(f"/eingang/{item_id}")
        message = views.error_text(exc, "Ablegen fehlgeschlagen.")
        return _render_item(request, user, services, fresh, form=values, errors=[message], status_code=409)

    flash(
        request,
        f"„{record.title}“ liegt jetzt in {record.folder or 'Archiv'}.",
        link=views.document_url(record.id),
        link_label="Ansehen",
    )
    if filing.create_rule:
        done = services.inbox.find(item_id)
        journal = done.filing if done is not None and isinstance(done.filing, Mapping) else {}
        if journal.get("rule_error"):
            flash(request, f"Regel nicht angelegt: {journal['rule_error']}", "warning")
        elif journal.get("rule_name"):
            flash(request, f"Regel „{journal['rule_name']}“ angelegt.", "info", link="/regeln", link_label="Regeln")
            _flash_rules_backup(request, services)
    next_id = _next_ready_id(services, item_id)
    return redirect(f"/eingang/{next_id}" if next_id else "/eingang")


@router.post("/eingang/{item_id}/loeschen")
async def inbox_delete(
    request: Request, item_id: str, user: User = Depends(require_user), _csrf: Any = Depends(verify_csrf)
) -> Response:
    return await _with_form(request, _delete_item, user, item_id)


def _delete_item(request: Request, form: Any, user: Any, item_id: str) -> Response:
    services = get_services(request)
    item = services.inbox.find(item_id) if is_item_id(item_id) else None
    if item is None:
        flash(request, "Dieses Dokument ist nicht mehr im Eingang.", "warning")
        return redirect("/eingang")
    title = views.item_title(item)
    target = _after(services, form, item_id, "/eingang")
    try:
        entry_id = services.ops.delete_inbox_item(item_id, user=_username(user))
    except DocuShelfError as exc:
        flash(request, views.error_text(exc, "Löschen fehlgeschlagen."), "error")
        return redirect(f"/eingang/{item_id}")
    flash(
        request,
        f"„{title}“ liegt jetzt im Papierkorb.",
        action=f"/papierkorb/{entry_id}/wiederherstellen",
        action_label="Rückgängig",
    )
    return redirect(target)


@router.post("/eingang/{item_id}/neu-verarbeiten")
async def inbox_retry(
    request: Request, item_id: str, user: User = Depends(require_user), _csrf: Any = Depends(verify_csrf)
) -> Response:
    return await _with_form(request, _retry_item, user, item_id)


def _retry_item(request: Request, form: Any, user: Any, item_id: str) -> Response:
    services = get_services(request)
    if not is_item_id(item_id):
        flash(request, "Dieses Dokument ist nicht mehr im Eingang.", "warning")
        return redirect("/eingang")
    try:
        services.pipeline.retry(
            item_id,
            skip_blank_removal=form_flag(form, "skip_blank_removal"),
            skip_ocr=form_flag(form, "skip_ocr"),
        )
    except NotFound as exc:
        flash(request, views.error_text(exc, "Dieses Dokument ist nicht mehr im Eingang."), "warning")
        return redirect("/eingang")
    except DocuShelfError as exc:
        flash(request, views.error_text(exc, "Neu verarbeiten ist gerade nicht möglich."), "error")
        return redirect(f"/eingang/{item_id}")
    _audit(services, user, "retry", item=item_id)
    flash(request, "Das Dokument wird neu verarbeitet.", "info")
    return redirect(views.safe_next(form_value(form, "after"), f"/eingang/{item_id}"))


@router.post("/eingang/vorschlaege")
async def inbox_refresh_all(
    request: Request, user: User = Depends(require_user), _csrf: Any = Depends(verify_csrf)
) -> Response:
    return await _with_form(request, _refresh_all, user)


def _refresh_all(request: Request, form: Any, user: Any) -> Response:
    services = get_services(request)
    try:
        queued = services.pipeline.queue_refresh()
    except DocuShelfError as exc:
        flash(request, views.error_text(exc, "Vorschläge konnten nicht neu berechnet werden."), "error")
        return redirect("/eingang")
    if queued:
        flash(
            request,
            f"Vorschläge für {views.plural(queued, 'Dokument', 'Dokumente')} werden neu berechnet. "
            "Das dauert einen Moment – die Seite neu laden, um das Ergebnis zu sehen.",
            "info",
        )
    else:
        flash(request, "Es gibt nichts neu zu berechnen.", "info")
    return redirect("/eingang")


@router.post("/eingang/{item_id}/vorschlaege")
async def inbox_refresh(
    request: Request, item_id: str, user: User = Depends(require_user), _csrf: Any = Depends(verify_csrf)
) -> Response:
    return await _with_form(request, _refresh_item, user, item_id)


def _refresh_item(request: Request, form: Any, user: Any, item_id: str) -> Response:
    services = get_services(request)
    if not is_item_id(item_id):
        flash(request, "Dieses Dokument ist nicht mehr im Eingang.", "warning")
        return redirect("/eingang")
    try:
        item = services.pipeline.refresh_suggestions(item_id)
    except NotFound as exc:
        flash(request, views.error_text(exc, "Dieses Dokument ist nicht mehr im Eingang."), "warning")
        return redirect("/eingang")
    except DocuShelfError as exc:
        flash(request, views.error_text(exc, "Vorschläge konnten nicht berechnet werden."), "error")
        return redirect(f"/eingang/{item_id}")
    count = len(getattr(item, "suggestions", None) or [])
    flash(request, f"Vorschläge neu berechnet ({views.plural(count, 'Vorschlag', 'Vorschläge')}).", "info")
    return redirect(f"/eingang/{item_id}")


# ---------------------------------------------------------------------------
# Archive


@router.get("/archiv", response_class=HTMLResponse)
def archive_root(
    request: Request,
    sort: str = "date_desc",
    seite: int = 1,
    ansicht: str | None = None,
    user: User = Depends(require_user),
) -> Response:
    return _archive_page(request, user, "", sort, seite, ansicht)


@router.get("/archiv/{path:path}", response_class=HTMLResponse)
def archive_folder(
    request: Request,
    path: str,
    sort: str = "date_desc",
    seite: int = 1,
    ansicht: str | None = None,
    user: User = Depends(require_user),
) -> Response:
    return _archive_page(request, user, path, sort, seite, ansicht)


def _switch_view(request: Request, folder: str, value: str) -> Response:
    """``?ansicht=liste`` without JavaScript: remember the choice in a cookie and show the folder again."""
    params = [(key, val) for key, val in request.query_params.multi_items() if key != "ansicht"]
    target = views.archive_url(folder) + ("?" + urlencode(params) if params else "")
    response = redirect(target)
    view = (value or "").strip().lower()
    if view in views.VIEWS:
        settings = get_services(request).settings
        response.set_cookie(
            views.VIEW_COOKIE,
            view,
            max_age=views.VIEW_COOKIE_MAX_AGE,
            path="/",
            samesite="lax",
            secure=bool(getattr(settings, "cookies_secure", False)),
            httponly=False,  # the page script switches the view without a reload and sets it too
        )
    return response


def _newest_date(services: Any, folder: str) -> Any:
    """Date of the newest document in ``folder`` and its subfolders (None if none has a date)."""
    try:
        newest = services.index.list_in_folder(folder, recursive=True, order="date_desc", limit=1)
    except Exception:
        log.debug("cannot read the newest document of %s", folder, exc_info=True)
        return None
    return newest[0].doc_date if newest else None


def _documents_page(index: Any, folder: str, order: str, page: int, total: int) -> tuple[list[Any], bool]:
    """One page of the documents directly in ``folder`` and whether more pages follow."""
    if order == "title_desc":
        # Reverse alphabetical order: the matching slice of the A–Z order, read backwards.
        end = max(0, total - (page - 1) * ARCHIVE_PAGE_SIZE)
        start = max(0, end - ARCHIVE_PAGE_SIZE)
        docs = index.list_in_folder(folder, order="title", limit=end - start, offset=start) if end > start else []
        return list(reversed(docs)), start > 0
    docs = index.list_in_folder(folder, order=order, limit=ARCHIVE_PAGE_SIZE + 1, offset=(page - 1) * ARCHIVE_PAGE_SIZE)
    return docs[:ARCHIVE_PAGE_SIZE], len(docs) > ARCHIVE_PAGE_SIZE


def _folder_exists(services: Any, folder: str) -> bool:
    if folder == "":
        return True
    try:
        if services.index.folder_exists(folder):
            return True
    except Exception:
        log.debug("folder lookup failed", exc_info=True)
    try:
        path = services.layout.archive_path(folder)
    except PathError:
        return False
    return path.is_dir() and not path.is_symlink()


def _archive_page(request: Request, user: Any, path: str, sort: str, seite: Any, ansicht: str | None = None) -> Response:
    services = get_services(request)
    try:
        folder = check_relative((path or "").strip("/"))
    except PathError:
        return _not_found(request, user, "Diesen Ordner gibt es nicht.")
    if not _folder_exists(services, folder):
        return _not_found(request, user, f"Den Ordner „{folder}“ gibt es nicht (mehr) im Archiv.")
    if ansicht is not None:
        return _switch_view(request, folder, ansicht)
    index = services.index
    folders = _folders(services)
    if folder and folder not in folders:
        folders.append(folder)
    try:
        counts = dict(index.folder_counts())
    except Exception:
        counts = {}
    totals = views.recursive_counts(folders, counts)
    children_of: dict[str, int] = {}
    for entry in folders:
        parent = entry.rsplit("/", 1)[0] if "/" in entry else ""
        children_of[parent] = children_of.get(parent, 0) + 1
    subfolders = [
        {
            "path": entry,
            "name": entry.rsplit("/", 1)[-1],
            "url": views.archive_url(entry),
            "total": totals.get(entry, 0),
            "children": children_of.get(entry, 0),
        }
        for entry in folders
        if (entry.rsplit("/", 1)[0] if "/" in entry else "") == folder and entry != folder
    ]
    order = sort if sort in ARCHIVE_SORTS else "date_desc"
    page = _page_number(seite)
    direct = int(counts.get(folder, 0))
    docs, has_more = _documents_page(index, folder, order, page, direct)
    # The list shows folders first, then documents; folders only once, on the first page.
    list_folders: list[dict[str, Any]] = []
    if page == 1:
        rows = [{**sub, "newest": _newest_date(services, sub["path"])} for sub in subfolders]
        list_folders = views.sort_folder_rows(rows, order)
    return render(
        request,
        "archive.html",
        {
            "user": user,
            "folder": folder,
            "folder_name": views.folder_label(folder),
            "parent": folder.rsplit("/", 1)[0] if "/" in folder else "",
            "subfolders": subfolders,
            "docs": docs,
            "direct_count": direct,
            "total_count": totals.get(folder, direct),
            "tree": views.folder_tree(folders, counts, folder),
            "is_empty": not subfolders and direct == 0,
            "sort": order,
            "sorts": ARCHIVE_SORTS,
            "page": page,
            "has_more": has_more,
            "folders": folders,
            "view": views.archive_view(request.cookies.get(views.VIEW_COOKIE)),
            "list_folders": list_folders,
            "list_sorting": views.list_sorting(order),
        },
    )


@router.post("/archiv/ordner/neu")
async def folder_create(request: Request, user: User = Depends(require_user), _csrf: Any = Depends(verify_csrf)) -> Response:
    return await _with_form(request, _create_folder, user)


def _create_folder(request: Request, form: Any, user: Any) -> Response:
    services = get_services(request)
    parent = form_value(form, "parent").strip("/")
    name = form_value(form, "name")
    back = views.archive_url(parent)
    if not name:
        flash(request, "Bitte einen Namen für den neuen Ordner angeben.", "error")
        return redirect(back)
    rel = f"{parent}/{name}" if parent else name
    try:
        created = services.ops.create_folder(rel, user=_username(user))
    except DocuShelfError as exc:
        flash(request, views.error_text(exc, "Der Ordner konnte nicht angelegt werden."), "error")
        return redirect(back)
    flash(request, f"Ordner „{created}“ angelegt.")
    return redirect(views.archive_url(created))


@router.post("/archiv/ordner/umbenennen")
async def folder_rename(request: Request, user: User = Depends(require_user), _csrf: Any = Depends(verify_csrf)) -> Response:
    return await _with_form(request, _rename_folder, user)


def _rename_folder(request: Request, form: Any, user: Any) -> Response:
    services = get_services(request)
    path = form_value(form, "path").strip("/")
    name = form_value(form, "name")
    back = views.archive_url(path)
    if not path:
        flash(request, "Der Archivordner selbst kann nicht umbenannt werden.", "error")
        return redirect("/archiv")
    if not name:
        flash(request, "Bitte einen neuen Namen angeben.", "error")
        return redirect(back)
    try:
        renamed = services.ops.rename_folder(path, name, user=_username(user))
    except DocuShelfError as exc:
        flash(request, views.error_text(exc, "Der Ordner konnte nicht umbenannt werden."), "error")
        return redirect(back)
    flash(request, f"Ordner umbenannt in „{views.folder_label(renamed)}“.")
    return redirect(views.archive_url(renamed))


@router.post("/archiv/ordner/loeschen")
async def folder_delete(request: Request, user: User = Depends(require_user), _csrf: Any = Depends(verify_csrf)) -> Response:
    return await _with_form(request, _delete_folder, user)


def _delete_folder(request: Request, form: Any, user: Any) -> Response:
    services = get_services(request)
    path = form_value(form, "path").strip("/")
    if not path:
        flash(request, "Der Archivordner selbst kann nicht gelöscht werden.", "error")
        return redirect("/archiv")
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    try:
        services.ops.delete_folder(path, user=_username(user))
    except DocuShelfError as exc:
        flash(request, views.error_text(exc, "Der Ordner konnte nicht gelöscht werden."), "error")
        return redirect(views.archive_url(path))
    flash(request, f"Ordner „{views.folder_label(path)}“ gelöscht.")
    return redirect(views.archive_url(parent))


# ---------------------------------------------------------------------------
# Documents

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif")


@router.get("/dokument/{doc_id}", response_class=HTMLResponse)
def document_page(request: Request, doc_id: str, user: User = Depends(require_user)) -> HTMLResponse:
    services = get_services(request)
    record = services.index.get(doc_id)
    if record is None:
        return _not_found(
            request, user, "Dieses Dokument gibt es nicht (mehr). Vielleicht wurde es verschoben oder gelöscht."
        )
    return _render_document(request, user, services, record)


def _file_details(path: Path) -> tuple[str | None, list[dict[str, Any]]]:
    """Protection state ("signed"/"encrypted") and further XMP metadata of an archived PDF."""
    protection = views.pdf_protection(path)
    if protection == "encrypted":
        return protection, []
    try:
        from ..pdf.xmp import read_meta

        extra = read_meta(path).extra
    except Exception:
        log.debug("cannot read the metadata of %s", path, exc_info=True)
        extra = {}
    return protection, views.document_extras(extra)


def _render_document(
    request: Request,
    user: Any,
    services: Any,
    record: Any,
    *,
    form: dict[str, Any] | None = None,
    original: Mapping[str, str] | None = None,
    errors: list[str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """The document page. ``original`` are the values the edit form was first
    rendered with (kept across re-renders after input errors)."""
    try:
        path = services.layout.archive_path(record.path)
        on_disk = path.is_file()
    except PathError:
        path, on_disk = None, False
    try:
        pages = list(services.index.get_pages(record.id)) if record.is_pdf else []
    except Exception:
        pages = []
    text_pages = [
        {"number": number, "text": text[:TEXT_PREVIEW_CHARS], "cut": len(text) > TEXT_PREVIEW_CHARS}
        for number, text in enumerate(pages[:MAX_PREVIEW_PAGES], start=1)
        if text and text.strip()
    ]
    protection, extras = _file_details(path) if record.is_pdf and on_disk and path is not None else (None, [])
    # Signed and encrypted PDFs are never rewritten: only title, date and folder can change.
    editable_meta = record.is_pdf and protection is None
    current = views.document_form(record)
    values = form or current
    shown_fields = views.EDIT_FIELDS if editable_meta else ("title", "doc_date", "folder")
    baseline = {**current, **(original or {})}
    file_url = f"/api/v1/documents/{views.quote_id(record.id)}/file"
    auto_notice = None
    try:
        filing = views.find_auto_filing(services, record.id)
    except Exception:
        log.debug("cannot look up the automatic filing of %s", record.id, exc_info=True)
        filing = None
    if filing is not None and not filing.reviewed:
        auto_notice = filing
    return render(
        request,
        "document.html",
        {
            "user": user,
            "doc": record,
            "form": values,
            "original": {key: baseline[key] for key in shown_fields},
            "editable_meta": editable_meta,
            "protection_notice": views.protection_notice(protection, archived=True),
            "extras": extras,
            "errors": errors or [],
            "on_disk": on_disk,
            "file_url": file_url,
            "page_url": f"/api/v1/documents/{views.quote_id(record.id)}/pages",
            "page_version": (record.content_hash or "")[:12],
            "is_image": record.ext in IMAGE_EXTENSIONS,
            "preview_pages": min(int(record.page_count or 0), MAX_PREVIEW_PAGES),
            "text_pages": text_pages,
            "text_status": views.TEXT_STATUS_LABELS.get(record.text_status, record.text_status),
            "provisional": record.id.startswith("h-"),
            "folders": _folders(services),
            "tags": _tag_names(services),
            "senders": _sender_names(services),
            "auto_notice": auto_notice,
        },
        status_code=status_code,
    )


@router.post("/dokument/{doc_id}/bearbeiten")
async def document_update(
    request: Request, doc_id: str, user: User = Depends(require_user), _csrf: Any = Depends(verify_csrf)
) -> Response:
    return await _with_form(request, _update_document, user, doc_id)


def _update_document(request: Request, form: Any, user: Any, doc_id: str) -> Response:
    services = get_services(request)
    record = services.index.get(doc_id)
    if record is None:
        flash(request, "Dieses Dokument gibt es nicht mehr.", "warning")
        return redirect("/archiv")
    current_form = views.document_form(record)
    # Tags and sender only exist in PDFs; fields missing from the form stay as they are.
    fields = views.EDIT_FIELDS if record.is_pdf else ("title", "doc_date", "folder")
    present = [key for key in fields if key in form]
    values = {key: form_value(form, key) if key in form else current_form[key] for key in views.EDIT_FIELDS}
    # The values the page was rendered with (hidden "orig_*" inputs); None for forms without them.
    rendered = None
    if "orig_title" in form:
        rendered = {
            key: form_value(form, f"orig_{key}") if f"orig_{key}" in form else current_form[key] for key in fields
        }
    current = {key: views.edit_value(key, current_form[key]) for key in fields}
    try:
        original = {key: views.edit_value(key, rendered[key]) for key in fields} if rendered is not None else None
    except ValueError:
        original = None  # unusable hidden values: compare with the current state, as forms without them do
    try:
        posted = {key: views.edit_value(key, values[key]) for key in present}
        if "title" in posted and not posted["title"]:
            raise ValueError("Bitte einen Titel angeben.")
        plan = views.plan_document_edit(posted, original, current)
        if "folder" in plan.changes:
            # Only a new folder is checked: the current one may be spelled in ways the app would not create.
            sanitize_folder(plan.changes["folder"])
    except (ValueError, PathError) as exc:
        return _render_document(
            request, user, services, record, form=values, original=rendered, errors=[str(exc)], status_code=400
        )
    if plan.conflicts:
        # Someone else changed what the user changed: show the current state with the user's input and ask again.
        merged = {**current_form, **{key: values[key] for key in plan.touched}}
        return _render_document(
            request,
            user,
            services,
            record,
            form=merged,
            errors=[views.edit_conflict_message(plan.conflicts)],
            status_code=409,
        )
    changes = plan.changes
    if not changes:
        flash(request, "Es gab nichts zu ändern.", "info")
        return redirect(views.document_url(record.id))
    update = MetadataUpdate(
        title=changes.get("title"),
        doc_date=changes.get("doc_date"),
        clear_date="doc_date" in changes and changes["doc_date"] is None,
        tags=changes.get("tags"),
        sender=changes.get("sender"),
        # As typed (NFC, trimmed): existing folders are matched by their real name.
        folder=changes.get("folder"),
    )
    try:
        updated = services.ops.update_document(record.id, update, user=_username(user))
    except NotFound as exc:
        flash(request, views.error_text(exc, "Dieses Dokument gibt es nicht mehr."), "warning")
        return redirect(views.archive_url(record.folder))
    except Conflict as exc:
        flash(request, views.error_text(exc, "Die Datei wurde zwischenzeitlich geändert."), "error")
        return redirect(views.document_url(record.id))
    except DocuShelfError as exc:
        message = views.error_text(exc, "Speichern fehlgeschlagen.")
        return _render_document(
            request, user, services, record, form=values, original=rendered, errors=[message], status_code=400
        )
    if updated.folder != record.folder:
        flash(request, f"Gespeichert und nach „{updated.folder or 'Archiv'}“ verschoben.")
    else:
        flash(request, "Änderungen gespeichert.")
    return redirect(views.document_url(updated.id))


@router.post("/dokument/{doc_id}/loeschen")
async def document_delete(
    request: Request, doc_id: str, user: User = Depends(require_user), _csrf: Any = Depends(verify_csrf)
) -> Response:
    return await _with_form(request, _delete_document, user, doc_id)


def _delete_document(request: Request, form: Any, user: Any, doc_id: str) -> Response:
    services = get_services(request)
    record = services.index.get(doc_id)
    if record is None:
        flash(request, "Dieses Dokument gibt es nicht mehr.", "warning")
        return redirect("/archiv")
    try:
        entry_id = services.ops.delete_document(record.id, user=_username(user))
    except DocuShelfError as exc:
        flash(request, views.error_text(exc, "Löschen fehlgeschlagen."), "error")
        return redirect(views.document_url(record.id))
    flash(
        request,
        f"„{record.title}“ liegt jetzt im Papierkorb.",
        action=f"/papierkorb/{entry_id}/wiederherstellen",
        action_label="Rückgängig",
    )
    return redirect(views.archive_url(record.folder))


# ---------------------------------------------------------------------------
# Search


@router.get("/suche", response_class=HTMLResponse)
def search_page(
    request: Request,
    q: str = "",
    ordner: str = "",
    ab: str = "",
    bis: str = "",
    tag: list[str] = Query(default=[]),
    absender: str = "",
    seite: int = 1,
    user: User = Depends(require_user),
) -> HTMLResponse:
    from ..index.search import SearchQuery

    services = get_services(request)
    page = _page_number(seite)
    notes: list[str] = []
    try:
        date_from = views.parse_date_input(ab)
    except ValueError:
        date_from = None
        notes.append(f"„{ab}“ ist kein gültiges Datum und wurde ignoriert.")
    try:
        date_to = views.parse_date_input(bis)
    except ValueError:
        date_to = None
        notes.append(f"„{bis}“ ist kein gültiges Datum und wurde ignoriert.")
    tags = views.parse_tags(tag)
    query = SearchQuery(
        text=q.strip(),
        folder=ordner.strip("/ ") or None,
        date_from=date_from,
        date_to=date_to,
        tags=tags,
        sender=absender.strip() or None,
        limit=SEARCH_PAGE_SIZE,
        offset=(page - 1) * SEARCH_PAGE_SIZE,
    )
    result = None
    hits: list[dict[str, Any]] = []
    corrections: list[dict[str, Any]] = []
    recent: list[Any] = []
    if query.is_empty():
        try:
            recent = list(services.index.recent(8))
        except Exception:
            recent = []
    else:
        try:
            result = services.searcher.search(query)
        except Exception:
            log.exception("search failed")
            notes.append("Die Suche ist gerade nicht verfügbar. Bitte gleich noch einmal versuchen.")
        if result is not None:
            hits = [_hit_view(hit) for hit in result.hits]
            corrections = _correction_links(q, result.corrections, request)
    filters_active = bool(query.folder or query.date_from or query.date_to or query.tags or query.sender)
    return render(
        request,
        "search.html",
        {
            "user": user,
            "q": q,
            "filters": {
                "ordner": ordner,
                "ab": date_from.isoformat() if date_from else "",
                "bis": date_to.isoformat() if date_to else "",
                "tags": ", ".join(tags),
                "absender": absender,
            },
            "filters_active": filters_active,
            "result": result,
            "hits": hits,
            "corrections": corrections,
            "notes": notes,
            "recent": recent,
            "page": page,
            "has_more": result is not None and result.total > page * SEARCH_PAGE_SIZE,
            "page_size": SEARCH_PAGE_SIZE,
            "folders": _folders(services),
            "tags": _tag_names(services),
            "senders": _sender_names(services),
        },
    )


def _hit_view(hit: Any) -> dict[str, Any]:
    doc = hit.doc
    pages = list(hit.pages or [])
    return {
        "doc": doc,
        "url": views.document_url(doc.id, pages[0] if pages else None),
        "snippets": [
            {"page": s.page, "html": s.html, "url": views.document_url(doc.id, s.page if s.page else None)}
            for s in hit.snippets or []
        ],
        "matched": [views.MATCH_LABELS.get(m, m) for m in hit.matched_in or []],
        "pages": pages,
    }


def _correction_links(q: str, corrections: Mapping[str, list[str]], request: Request) -> list[dict[str, Any]]:
    links = []
    params = dict(request.query_params)
    params.pop("seite", None)
    for term, alternatives in (corrections or {}).items():
        options = []
        for alternative in list(alternatives or [])[:3]:
            pattern = re.compile(re.escape(term), re.IGNORECASE)
            replaced = pattern.sub(alternative, q, count=1) if pattern.search(q) else alternative
            options.append({"word": alternative, "url": views.search_url(**{**params, "q": replaced})})
        links.append({"term": term, "options": options})
    return links


# ---------------------------------------------------------------------------
# Trash


@router.get("/papierkorb", response_class=HTMLResponse)
def trash_page(request: Request, user: User = Depends(require_user)) -> HTMLResponse:
    services = get_services(request)
    entries = []
    for entry in services.trash.list():
        expires = None
        try:
            expires = services.trash.expires_at(entry)
        except Exception:
            expires = None
        entries.append({"entry": entry, "expires": expires})
    return render(
        request,
        "trash.html",
        {"user": user, "entries": entries, "retention_days": services.settings.trash_retention_days},
    )


@router.post("/papierkorb/{entry_id}/wiederherstellen")
async def trash_restore(
    request: Request, entry_id: str, user: User = Depends(require_user), _csrf: Any = Depends(verify_csrf)
) -> Response:
    return await _with_form(request, _restore_entry, user, entry_id)


def _restore_entry(request: Request, form: Any, user: Any, entry_id: str) -> Response:
    services = get_services(request)
    try:
        restored = services.trash.restore(entry_id, user=_username(user))
    except DocuShelfError as exc:
        flash(request, views.error_text(exc, "Wiederherstellen fehlgeschlagen."), "error")
        return redirect("/papierkorb")
    if hasattr(restored, "path") and hasattr(restored, "folder"):
        url = views.document_url(restored.id)
        message = f"„{restored.title}“ ist wieder in {restored.folder or 'Archiv'}."
    else:
        url = f"/eingang/{restored.id}"
        message = f"„{views.item_title(restored)}“ ist wieder im Eingang."
    target = views.safe_next(form_value(form, "after"), url)
    # A link is only useful when the page shown next is not the restored object itself.
    flash(request, message, link=url if target != url else None)
    return redirect(target)


@router.post("/papierkorb/{entry_id}/loeschen")
async def trash_purge(
    request: Request, entry_id: str, user: User = Depends(require_session_user), _csrf: Any = Depends(verify_csrf)
) -> Response:
    # Deleting for good needs a browser login, never just an API token.
    return await _with_form(request, _purge_entry, user, entry_id)


def _purge_entry(request: Request, form: Any, user: Any, entry_id: str) -> Response:
    services = get_services(request)
    try:
        entry = services.trash.get(entry_id)
        services.trash.purge(entry_id, user=_username(user))
    except DocuShelfError as exc:
        flash(request, views.error_text(exc, "Endgültiges Löschen fehlgeschlagen."), "error")
        return redirect("/papierkorb")
    flash(request, f"„{entry.title}“ wurde endgültig gelöscht.")
    return redirect("/papierkorb")


# ---------------------------------------------------------------------------
# Rules

RULE_FIELDS = ("sender", "keywords_any", "reference", "iban", "mail_from", "doc_type")


@router.get("/regeln", response_class=HTMLResponse)
def rules_page(request: Request, user: User = Depends(require_user)) -> HTMLResponse:
    return _render_rules(request, user, get_services(request))


def _render_rules(
    request: Request,
    user: Any,
    services: Any,
    *,
    new_rule: dict[str, Any] | None = None,
    rule_error: str | None = None,
    yaml_text: str | None = None,
    yaml_error: str | None = None,
    yaml_conflict: bool = False,
    status_code: int = 200,
) -> HTMLResponse:
    store = services.rules
    try:
        rules = store.load()
    except Exception:
        log.exception("cannot load rules")
        rules = []
    raw = store.raw_text()
    try:
        from ..analysis.doctypes import NAMES

        doc_types = sorted(NAMES, key=str.casefold)
    except Exception:
        doc_types = []
    return render(
        request,
        "rules.html",
        {
            "user": user,
            "rules": [views.rule_summary(rule) for rule in rules],
            "file_error": getattr(store, "error", None),
            "new_rule": new_rule or {},
            "rule_error": rule_error,
            "yaml_text": raw if yaml_text is None else yaml_text,
            "yaml_hash": views.text_hash(raw),
            "yaml_error": yaml_error,
            "yaml_conflict": yaml_conflict,
            "doc_types": doc_types,
            "folders": _folders(services),
            "rules_path": str(getattr(store, "path", "")),
            "auto": views.auto_settings(services.settings),
        },
        status_code=status_code,
    )


@router.post("/regeln/neu")
async def rules_add(request: Request, user: User = Depends(require_user), _csrf: Any = Depends(verify_csrf)) -> Response:
    return await _with_form(request, _add_rule, user)


def _rule_name(match: Mapping[str, list[str]], folder: str) -> str:
    for key in ("sender", "keywords_any", "reference", "iban", "mail_from", "doc_type"):
        if match.get(key):
            return f"{match[key][0]} → {folder}"[:80]
    return folder[:80] or "Neue Regel"


def _add_rule(request: Request, form: Any, user: Any) -> Response:
    from ..suggest.rules import Rule

    services = get_services(request)
    values: dict[str, Any] = {key: form_value(form, key) for key in ("name", "folder", "title", "tags", *RULE_FIELDS)}
    values["auto"] = form_flag(form, "auto")
    match = {key: views.parse_list(values[key]) for key in RULE_FIELDS if views.parse_list(values[key])}
    action: dict[str, Any] = {"folder": values["folder"]}
    if values["title"]:
        action["title"] = values["title"]
    tags = views.parse_tags(values["tags"])
    if tags:
        action["tags"] = tags
    try:
        if not match:
            raise ConfigError("Bitte mindestens eine Bedingung angeben (z. B. einen Absender).")
        if not values["folder"]:
            raise ConfigError("Bitte einen Zielordner angeben.")
        name = values["name"] or _rule_name(match, values["folder"])
        rule = Rule.from_dict({"name": name, "match": match, "action": action, "auto": bool(values["auto"])})
        services.rules.add(rule)
    except (ConfigError, PathError, NotFound) as exc:
        message = views.error_text(exc, "Die Regel konnte nicht gespeichert werden.")
        return _render_rules(request, user, services, new_rule=values, rule_error=message, status_code=400)
    _audit(services, user, "create_rule", name=rule.name, folder=rule.action.folder)
    flash(
        request,
        f"Regel „{rule.name}“ gespeichert. Sie gilt für alle neuen Dokumente – für die wartenden "
        "im Eingang über „Vorschläge neu berechnen“.",
    )
    _flash_rules_backup(request, services)
    return redirect("/regeln")


def _flash_rules_backup(request: Request, services: Any) -> None:
    """Tell the user where the commented version went when a structured change rewrote rules.yaml."""
    backup = getattr(getattr(services, "rules", None), "last_backup", None)
    if backup is None:
        return
    name = Path(str(backup)).name or "rules.yaml.bak"
    flash(
        request,
        f"Die Regeldatei wurde dabei neu geschrieben. Die vorige Fassung mit deinen Kommentaren liegt als {name} daneben.",
        "info",
    )


@router.post("/regeln/{index}/loeschen")
async def rules_delete(
    request: Request, index: int, user: User = Depends(require_user), _csrf: Any = Depends(verify_csrf)
) -> Response:
    return await _with_form(request, _delete_rule, user, index)


def _delete_rule(request: Request, form: Any, user: Any, index: int) -> Response:
    services = get_services(request)
    expected = form_value(form, "name")
    try:
        rules = services.rules.load()
        if not 0 <= index < len(rules) or (expected and rules[index].name != expected):
            flash(request, "Die Regeln wurden inzwischen geändert. Bitte noch einmal versuchen.", "warning")
            return redirect("/regeln")
        name = rules[index].name
        services.rules.delete(index)
    except DocuShelfError as exc:
        flash(request, views.error_text(exc, "Die Regel konnte nicht gelöscht werden."), "error")
        return redirect("/regeln")
    _audit(services, user, "delete_rule", name=name)
    flash(request, f"Regel „{name}“ gelöscht.")
    _flash_rules_backup(request, services)
    return redirect("/regeln")


@router.post("/regeln/yaml")
async def rules_save_yaml(request: Request, user: User = Depends(require_user), _csrf: Any = Depends(verify_csrf)) -> Response:
    return await _with_form(request, _save_rules_yaml, user)


def _save_rules_yaml(request: Request, form: Any, user: Any) -> Response:
    services = get_services(request)
    raw_value = form.get("yaml")
    text = raw_value if isinstance(raw_value, str) else ""
    base_hash = form_value(form, "base_hash")
    store = services.rules
    if base_hash and base_hash != views.text_hash(store.raw_text()) and not form_flag(form, "force"):
        return _render_rules(
            request,
            user,
            services,
            yaml_text=text,
            yaml_error="Die Regeldatei wurde inzwischen geändert (z. B. beim Ablegen mit „Regel anlegen“). "
            "Prüfe den Text und speichere mit „Trotzdem speichern“, um die neue Fassung zu überschreiben.",
            yaml_conflict=True,
            status_code=409,
        )
    try:
        rules = store.save_raw_text(text)
    except ConfigError as exc:
        return _render_rules(request, user, services, yaml_text=text, yaml_error=str(exc), status_code=400)
    except OSError as exc:
        return _render_rules(
            request, user, services, yaml_text=text,
            yaml_error=views.error_text(exc, "Die Regeldatei konnte nicht gespeichert werden."), status_code=500,
        )
    _audit(services, user, "rules_update", rules=len(rules))
    flash(request, f"Regeln gespeichert ({views.plural(len(rules), 'Regel', 'Regeln')}).")
    return redirect("/regeln")


# ---------------------------------------------------------------------------
# Settings


@dataclass
class _OcrInfo:
    enabled: bool
    available: bool | None
    version: str | None
    languages: list[str]
    missing_languages: list[str]


def _ocr_info(settings: Any) -> _OcrInfo:
    wanted = [lang for lang in str(settings.ocr_languages or "").split("+") if lang]
    try:
        from ..pdf import ocr

        available = bool(ocr.ocr_available())
        version = ocr.ocr_version() if available else None
        languages = ocr.tesseract_languages() if available else []
    except Exception:
        log.debug("cannot determine OCR status", exc_info=True)
        return _OcrInfo(bool(settings.ocr_enabled), None, None, [], [])
    missing = [lang for lang in wanted if languages and lang not in languages]
    return _OcrInfo(bool(settings.ocr_enabled), available, version, languages, missing)


def _status_of(component: Any) -> dict[str, Any] | None:
    if component is None or not hasattr(component, "status"):
        return None
    try:
        status = component.status()
    except Exception:
        log.debug("cannot read status of %s", type(component).__name__, exc_info=True)
        return None
    return dict(status) if isinstance(status, Mapping) else None


def _sources(services: Any) -> list[dict[str, Any]]:
    settings = services.settings
    result = []
    for source in settings.sources:
        try:
            path = source.path or services.layout.source_dir(source.name)
        except Exception:
            path = source.path
        result.append(
            {
                "name": source.name,
                "label": views.source_label(source.name),
                "icon": views.source_icon(source.name),
                "path": str(path) if path else "",
                "stable_seconds": source.stable_seconds,
                "external": source.path is not None,
                "remove_after_import": source.remove_after_import,
            }
        )
    return result


@router.get("/einstellungen", response_class=HTMLResponse)
def settings_page(request: Request, user: User = Depends(require_user)) -> HTMLResponse:
    services = get_services(request)
    settings = services.settings
    try:
        stats = dict(services.index.stats())
    except Exception:
        log.exception("cannot read index statistics")
        stats = {}
    tokens_store = getattr(services, "tokens", None)
    tokens: list[dict[str, Any]] = []
    if tokens_store is not None:
        try:
            tokens = [dict(t) for t in tokens_store.list()]
        except Exception:
            log.exception("cannot list API tokens")
    for token in tokens:
        token.pop("hash", None)
    try:
        audit_entries = [views.audit_entry(e) for e in services.audit.tail(40)]
    except Exception:
        audit_entries = []
    new_token = None
    session = request.scope.get("session")
    if isinstance(session, dict) and NEW_TOKEN_KEY in session:
        new_token = views.one_time.take(session.pop(NEW_TOKEN_KEY, None))
    watcher = getattr(services, "watcher", None)
    probe_running, probe = _llm_probe(request).snapshot()
    smtp = _status_of(getattr(services, "smtp", None))
    smtp_config = settings.smtp
    smtp_recipients: list[str] = []
    household = None
    if security.multi_household(request):
        # One SMTP receiver serves every household; mail is routed by recipient.
        registry = security.get_registry(request)
        smtp = _status_of(getattr(registry, "smtp", None))
        smtp_config = getattr(request.app.state.settings, "smtp", None)
        household = services.household
        smtp_recipients = list(household.smtp_recipients)
    response = render(
        request,
        "settings.html",
        {
            "user": user,
            "stats": stats,
            "counts": dict(services.inbox.counts()),
            "watcher": _status_of(watcher),
            "worker": _status_of(getattr(services, "worker", None)),
            "intake": _status_of(getattr(services, "intake", None)),
            "imap": _status_of(getattr(services, "imap", None)),
            "smtp": smtp,
            "smtp_config": smtp_config,
            "smtp_recipients": smtp_recipients,
            "current_household": household,
            "ocr": _ocr_info(settings),
            "settings": settings,
            "sources": _sources(services),
            "tokens": tokens,
            "tokens_available": tokens_store is not None,
            "new_token": new_token,
            "audit": audit_entries,
            "auto": _auto_overview(services),
            "llm": views.llm_info(settings),
            "probe": probe,
            "probe_answer": views.probe_answer((probe or {}).get("answer")),
            "probe_running": probe_running,
            "member_admin": _member_overview(request, user),
            "max_member_length": MAX_MEMBER_LENGTH,
        },
    )
    if new_token:
        response.headers["Cache-Control"] = "no-store"
    return response


def _member_overview(request: Request, user: Any) -> list[dict[str, Any]] | None:
    """Every household with its members for the admin section, or None (not an admin)."""
    if not security.is_household_admin(request, user):
        return None
    registry = security.get_registry(request)
    return [
        {"id": h.id, "name": h.name, "users": list(h.users), "groups": list(h.groups)}
        for h in registry.households
    ]


_probe_lock = threading.Lock()


def _llm_probe(request: Request) -> views.LlmProbe:
    """The connection test of this application (one per process, kept in the app state)."""
    state = request.app.state
    probe = getattr(state, "llm_probe", None)
    if probe is None:
        with _probe_lock:
            probe = getattr(state, "llm_probe", None)
            if probe is None:
                probe = state.llm_probe = views.LlmProbe()
    return probe


@router.post("/einstellungen/sprachmodell/testen")
async def settings_llm_test(request: Request, user: User = Depends(require_user), _csrf: Any = Depends(verify_csrf)) -> Response:
    return await _with_form(request, _test_llm, user)


def _test_llm(request: Request, form: Any, user: Any) -> Response:
    """Classify a built-in sample letter with the configured model and show answer and duration."""
    services = get_services(request)
    config = getattr(services.settings, "llm", None)
    back = "/einstellungen#ki"
    if config is None:
        flash(request, "Das Sprachmodell ist ausgeschaltet – es gibt nichts zu testen.", "info")
        return redirect(back)
    try:
        configured = float(getattr(config, "timeout", LLM_PROBE_WAIT) or LLM_PROBE_WAIT)
    except (TypeError, ValueError):
        configured = LLM_PROBE_WAIT
    # A little longer than one request may take, but never longer than a page should load.
    wait = min(max(configured, 5.0) + 5.0, LLM_PROBE_WAIT)
    status, result = _llm_probe(request).run(config, timeout=wait)
    if status == "busy":
        flash(request, "Ein Test läuft noch. Lade die Seite gleich neu, um das Ergebnis zu sehen.", "info")
    elif status == "running":
        flash(
            request,
            f"Nach {int(wait)} s kam noch keine Antwort – vielleicht wird das Modell gerade geladen. "
            "Der Test läuft weiter; lade die Seite gleich neu, um das Ergebnis zu sehen.",
            "warning",
        )
    elif result and result.get("ok"):
        flash(request, f"Das Sprachmodell antwortet ({views.format_seconds(result.get('duration_s'))}).")
    else:
        error = (result or {}).get("error") or "unbekannter Fehler"
        flash(request, f"Das Sprachmodell antwortet nicht: {error}", "error")
    return redirect(back)


@router.post("/haushalt")
async def household_switch(
    request: Request, user: User = Depends(require_session_user), _csrf: Any = Depends(verify_csrf)
) -> Response:
    return await _with_form(request, _switch_household, user)


def _switch_household(request: Request, form: Any, user: Any) -> Response:
    """Switch the browser session to another household the user belongs to."""
    household = security.switch_household(request, form_value(form, "id").strip())
    if household is None:
        return _not_found(request, user, "Diesen Haushalt gibt es nicht oder er ist für dich nicht freigegeben.")
    flash(request, f"Du arbeitest jetzt im Haushalt „{household.name}“.")
    return redirect("/eingang")


@router.post("/einstellungen/haushalte/{household_id}/mitglieder")
async def household_member_add(
    request: Request, household_id: str, user: User = Depends(require_session_user), _csrf: Any = Depends(verify_csrf)
) -> Response:
    return await _with_form(request, _change_members, user, household_id, "add")


@router.post("/einstellungen/haushalte/{household_id}/mitglieder/entfernen")
async def household_member_remove(
    request: Request, household_id: str, user: User = Depends(require_session_user), _csrf: Any = Depends(verify_csrf)
) -> Response:
    return await _with_form(request, _change_members, user, household_id, "remove")


def _change_members(request: Request, form: Any, user: Any, household_id: str, change: str) -> Response:
    """Add a user to or remove a user from a household (admins only, written to households.yaml)."""
    back = "/einstellungen#haushalte"
    if not security.multi_household(request):
        return _not_found(request, user, "Haushalte gibt es in dieser Installation nicht.")
    if not security.is_household_admin(request, user):
        return _error_page(
            request, user, 403, "Keine Berechtigung", "Haushaltsmitglieder können nur Administratoren ändern."
        )
    registry = security.get_registry(request)
    name = form_value(form, "user")
    try:
        if change == "add":
            household = registry.change_members(household_id, add=name)
        else:
            household = registry.change_members(household_id, remove=name)
    except DocuShelfError as exc:
        flash(request, views.error_text(exc, "Die Mitglieder konnten nicht geändert werden."), "error")
        return redirect(back)
    name = name.strip()
    try:
        services = registry.services(household.id)
    except Exception:
        log.exception("cannot open household %s for the audit log", household.id)
        services = None
    if change == "add":
        _audit(services, user, "household_members", added=name, household=household.id)
        flash(request, f"„{name}“ gehört jetzt zum Haushalt „{household.name}“.")
    else:
        _audit(services, user, "household_members", removed=name, household=household.id)
        flash(request, f"„{name}“ wurde aus dem Haushalt „{household.name}“ entfernt.")
    return redirect(back)


@router.post("/einstellungen/abgleich")
async def settings_reconcile(request: Request, user: User = Depends(require_user), _csrf: Any = Depends(verify_csrf)) -> Response:
    return await _with_form(request, _reconcile, user)


def _reconcile(request: Request, form: Any, user: Any) -> Response:
    services = get_services(request)
    watcher = getattr(services, "watcher", None)
    try:
        if watcher is not None and getattr(watcher, "running", False):
            watcher.request_scan("Abgleich über die Weboberfläche")
            flash(request, "Der Abgleich läuft im Hintergrund. Änderungen erscheinen in wenigen Sekunden.", "info")
        else:
            stats = services.reconciler.scan(full=True)
            flash(
                request,
                "Abgleich abgeschlossen: "
                f"{stats.added} neu, {stats.updated} geändert, {stats.moved} verschoben, {stats.removed} entfernt"
                + (f", {stats.errors} Fehler" if stats.errors else "")
                + ".",
            )
    except DocuShelfError as exc:
        flash(request, views.error_text(exc, "Der Abgleich ist fehlgeschlagen."), "error")
        return redirect("/einstellungen")
    _audit(services, user, "reconcile")
    return redirect("/einstellungen")


@router.post("/einstellungen/index-neu-aufbauen")
async def settings_rebuild(request: Request, user: User = Depends(require_user), _csrf: Any = Depends(verify_csrf)) -> Response:
    return await _with_form(request, _rebuild, user)


def _rebuild(request: Request, form: Any, user: Any) -> Response:
    services = get_services(request)
    try:
        stats = services.rebuild(user=_username(user))
    except DocuShelfError as exc:
        flash(request, views.error_text(exc, "Der Index konnte nicht neu aufgebaut werden."), "error")
        return redirect("/einstellungen")
    count = views.plural(int(getattr(stats, "added", 0) or 0), "Dokument", "Dokumente")
    seconds = (getattr(stats, "duration_ms", 0) or 0) / 1000
    duration = f" in {seconds:.1f} s".replace(".", ",") if seconds >= 0.1 else ""
    flash(request, f"Index neu aufgebaut: {count}{duration}.")
    return redirect("/einstellungen")


@router.post("/einstellungen/tokens")
async def settings_token_create(
    request: Request, user: User = Depends(require_session_user), _csrf: Any = Depends(verify_csrf)
) -> Response:
    return await _with_form(request, _create_token, user)


def _create_token(request: Request, form: Any, user: Any) -> Response:
    services = get_services(request)
    store = getattr(services, "tokens", None)
    if store is None:
        flash(request, "API-Tokens sind nicht verfügbar.", "error")
        return redirect("/einstellungen#tokens")
    name = " ".join(form_value(form, "name").split())[:80]
    if not name:
        flash(request, "Bitte einen Namen für den Token angeben (z. B. „iPhone Kurzbefehl“).", "error")
        return redirect("/einstellungen#tokens")
    try:
        # Tied to the account: the token stops working once the owner is no longer allowed in.
        token, info = store.create(name, _username(user), owner=user)
    except DocuShelfError as exc:
        flash(request, views.error_text(exc, "Der Token konnte nicht erstellt werden."), "error")
        return redirect("/einstellungen#tokens")
    info = info if isinstance(info, Mapping) else {}
    key = views.one_time.put({"token": token, "name": info.get("name", name)})
    request.session[NEW_TOKEN_KEY] = key
    _audit(services, user, "token_create", token=info.get("id"), name=info.get("name", name))
    flash(request, f"Token „{name}“ erstellt. Er wird nur dieses eine Mal angezeigt.")
    return redirect("/einstellungen#tokens")


@router.post("/einstellungen/tokens/{token_id}/widerrufen")
async def settings_token_revoke(
    request: Request, token_id: str, user: User = Depends(require_session_user), _csrf: Any = Depends(verify_csrf)
) -> Response:
    return await _with_form(request, _revoke_token, user, token_id)


def _revoke_token(request: Request, form: Any, user: Any, token_id: str) -> Response:
    services = get_services(request)
    store = getattr(services, "tokens", None)
    if store is None:
        flash(request, "API-Tokens sind nicht verfügbar.", "error")
        return redirect("/einstellungen#tokens")
    name = form_value(form, "name") or token_id
    try:
        result = store.revoke(token_id)
    except DocuShelfError as exc:
        flash(request, views.error_text(exc, "Der Token konnte nicht widerrufen werden."), "error")
        return redirect("/einstellungen#tokens")
    if result is False:
        flash(request, "Diesen Token gibt es nicht mehr.", "warning")
        return redirect("/einstellungen#tokens")
    if isinstance(result, Mapping) and result.get("name"):
        name = str(result["name"])
    _audit(services, user, "token_revoke", token=token_id, name=name)
    flash(request, f"Token „{name}“ widerrufen. Skripte mit diesem Token haben keinen Zugriff mehr.")
    return redirect("/einstellungen#tokens")


__all__ = ["router"]
