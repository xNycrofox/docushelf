"""Sending documents from phones: installable web app with a share target.

Installed from the browser (Android, desktop Chrome and Edge), DocuShelf
appears in the system share sheet. The browser then posts the shared files to
``POST /teilen`` as the manifest's ``share_target`` describes:

* With the service worker (``static/sw.js``) active, that request never
  reaches the server. The worker keeps the files in the Cache Storage
  (``docushelf-teilen``) and opens ``GET /teilen``, where ``static/share.js``
  lists them and uploads them through ``/api/v1/upload`` with the session's
  CSRF token – after a login, if the session has expired in the meantime.
* Without the worker (e.g. the very first share right after installing),
  ``POST /teilen`` arrives here and is stored like a normal upload. A share
  sheet cannot send a CSRF token; the browser's ``Sec-Fetch-Site`` header takes
  its place: only ``none`` (opened by the user or the system) and
  ``same-origin`` are accepted. Other sites cannot set that header.

``GET /manifest.webmanifest`` and ``GET /sw.js`` (registered in ``web/app.py``)
need no login: browsers fetch the manifest without cookies, and neither file
contains anything private.
"""

from __future__ import annotations

import json
import logging
import os
import unicodedata
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from ..errors import DocuShelfError
from . import auth
from .routes_api import MAX_UPLOAD_FILES, store_upload
from .security import User, current_user, get_services, get_settings, login_url

log = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
SERVICE_WORKER = STATIC_DIR / "sw.js"
MB = 1024 * 1024

SHARE_PATH = "/teilen"
# Cache Storage of the shared files; the same name is used in static/sw.js and static/share.js.
SHARE_CACHE = "docushelf-teilen"
SHARE_FIELD = "files"
SHARE_ACCEPT = (
    "application/pdf",
    "image/*",
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".heic",
    ".tif",
    ".tiff",
    ".webp",
)
# Sec-Fetch-Site values that only come from the browser itself or from pages of this site.
TRUSTED_FETCH_SITES = frozenset({"none", "same-origin"})

MANIFEST_MEDIA_TYPE = "application/manifest+json"
# The paper background of the light palette (static/app.css, --bg), as in base.html.
BACKGROUND_COLOR = "#f6f2ea"
THEME_COLOR = "#f6f2ea"
ICONS = (
    ("icon-192.png", 192, "any"),
    ("icon-512.png", 512, "any"),
    ("icon-maskable-192.png", 192, "maskable"),
    ("icon-maskable-512.png", 512, "maskable"),
)

# Messages for ``/teilen?hinweis=<code>``: only codes travel in the URL, never text.
NOTICES: dict[str, tuple[str, str]] = {
    "anmelden": (
        "Bitte melde dich an. Danach kannst du die Dateien noch einmal mit DocuShelf teilen.",
        "warning",
    ),
    "abgelehnt": (
        "Die geteilten Dateien wurden aus Sicherheitsgründen nicht übernommen. "
        "Bitte teile sie noch einmal über das Teilen-Menü deines Geräts.",
        "warning",
    ),
    "leer": (
        "Es sind keine Dateien angekommen. DocuShelf nimmt PDF-Dateien und Bilder an, keine Texte oder Links.",
        "warning",
    ),
    "fehler": (
        "Die geteilten Dateien konnten nicht zwischengespeichert werden. Bitte teile sie noch einmal.",
        "error",
    ),
}


# ---------------------------------------------------------------------------
# Manifest and service worker


def manifest_data() -> dict[str, Any]:
    """The web app manifest (W3C Web App Manifest with the Web Share Target extension)."""
    return {
        "id": "/",
        "name": "DocuShelf",
        "short_name": "DocuShelf",
        "description": "Dokumentenarchiv in normalen Ordnern: Eingang, Ablage und Volltextsuche.",
        "lang": "de",
        "dir": "ltr",
        "start_url": "/eingang",
        "scope": "/",
        "display": "standalone",
        "background_color": BACKGROUND_COLOR,
        "theme_color": THEME_COLOR,
        "icons": [
            {"src": f"/static/icons/{name}", "sizes": f"{size}x{size}", "type": "image/png", "purpose": purpose}
            for name, size, purpose in ICONS
        ],
        "share_target": {
            "action": SHARE_PATH,
            "method": "POST",
            "enctype": "multipart/form-data",
            "params": {
                "title": "title",
                "text": "text",
                "files": [{"name": SHARE_FIELD, "accept": list(SHARE_ACCEPT)}],
            },
        },
    }


def manifest(request: Request) -> Response:
    """``GET /manifest.webmanifest`` (no login: browsers fetch it without cookies)."""
    body = json.dumps(manifest_data(), indent=2) + "\n"
    return Response(body, media_type=MANIFEST_MEDIA_TYPE, headers={"Cache-Control": "no-cache"})


def service_worker(request: Request) -> Response:
    """``GET /sw.js`` – served from the root, so the worker's scope is the whole site.

    ``no-cache`` makes browsers look for a new version on every check instead of
    keeping an old worker for up to a day.
    """
    return FileResponse(SERVICE_WORKER, media_type="text/javascript", headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------------------
# The share page


def _notice(code: str | None) -> tuple[str, str] | None:
    return NOTICES.get((code or "").strip().lower())


def _flash(request: Request, message: str, category: str, **options: Any) -> None:
    """A message for the next page (kept in the session by the web interface)."""
    try:
        from . import views
    except ImportError:  # pragma: no cover - the web interface is not installed
        return
    views.flash(request, message, category, **options)


@router.get(SHARE_PATH)
def share_page(request: Request, hinweis: str | None = None) -> Response:
    """The page that shows and uploads the files the service worker has kept."""
    notice = _notice(hinweis)
    user = current_user(request)
    if user is None:
        # A GET carries the session cookie, so the message lands in the visitor's own session.
        if notice:
            _flash(request, notice[0], notice[1])
        return RedirectResponse(login_url(SHARE_PATH), status_code=303)
    get_services(request)  # with several households: refuses accounts without household
    settings = get_settings(request)
    context = {
        "user": user,
        "notice": {"message": notice[0], "category": notice[1]} if notice else None,
        "max_upload_mb": settings.max_upload_mb,
        "share_cache": SHARE_CACHE,
        "base_url": settings.base_url,
    }
    return auth.render_page(request, "share.html", context, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# Shares that reach the server (no service worker yet)


def trusted_fetch_site(request: Request) -> bool:
    """Whether the browser marks the request as user-initiated or coming from this site."""
    return request.headers.get("sec-fetch-site", "").strip().lower() in TRUSTED_FETCH_SITES


def _file_name(raw: str | None, position: int) -> str:
    """The file name the client sent, without folders and control characters."""
    name = unicodedata.normalize("NFC", (raw or "").replace("\\", "/").rsplit("/", 1)[-1])
    name = "".join(char for char in name if char.isprintable()).strip()
    return name or f"datei-{position}"


def _size(upload: UploadFile) -> int:
    if upload.size is not None:
        return int(upload.size)
    handle = upload.file
    position = handle.tell()
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    handle.seek(position)
    return size


@router.post(SHARE_PATH)
async def share_post(request: Request) -> Response:
    """Files posted by the share sheet while no service worker handles them."""
    user: User | None = current_user(request)
    if user is not None and user.method == "token":
        user = None  # a share sheet never sends an API token; such clients use /api/v1/upload
    trusted = trusted_fetch_site(request)
    if user is None or not trusted:
        site = (request.headers.get("sec-fetch-site") or "-")[:40]
        log.info("share refused (Sec-Fetch-Site %s, %s)", site, "logged in" if user else "not logged in")
        if trusted:
            # Same-origin or user-initiated requests carry the session cookie: it is safe to
            # leave the message in this session.
            _flash(request, *NOTICES["anmelden"])
            return RedirectResponse(login_url(SHARE_PATH), status_code=303)
        # The session stays untouched: a cross-site request may arrive without the cookie,
        # and a new session would replace the one stored in the browser. The share page
        # (a GET with the cookie) shows the message or leads to the login.
        return RedirectResponse(f"{SHARE_PATH}?hinweis=abgelehnt", status_code=303)

    services = get_services(request)
    settings = get_settings(request)
    limit = max(1, int(settings.max_upload_mb)) * MB
    async with request.form() as form:
        parts = [value for key, value in form.multi_items() if key == SHARE_FIELD and isinstance(value, UploadFile)]
        uploads = [(_file_name(part.filename, position), part) for position, part in enumerate(parts, start=1)]
        if not uploads:
            _flash(request, *NOTICES["leer"])
            return RedirectResponse(SHARE_PATH, status_code=303)
        if len(uploads) > MAX_UPLOAD_FILES:
            _flash(request, f"Zu viele Dateien auf einmal (höchstens {MAX_UPLOAD_FILES}).", "error")
            return RedirectResponse(SHARE_PATH, status_code=303)
        if sum(_size(part) for _name, part in uploads) > limit:
            _flash(
                request, f"Die Dateien sind zu groß (zusammen höchstens {settings.max_upload_mb} MB).", "error"
            )
            return RedirectResponse(SHARE_PATH, status_code=303)
        try:
            result = await run_in_threadpool(
                store_upload,
                services,
                [(name, part.file) for name, part in uploads],
                combine=False,
                user=user.username,
            )
        except HTTPException as exc:
            # Unsupported or empty files: the upload API explains which ones.
            _flash(request, str(exc.detail), "error")
            return RedirectResponse(SHARE_PATH, status_code=303)
        except (OSError, ValueError, DocuShelfError):
            log.exception("cannot store shared files")
            _flash(request, "Die Dateien konnten nicht gespeichert werden. Details stehen im Server-Log.", "error")
            return RedirectResponse(SHARE_PATH, status_code=303)

    intake = getattr(services, "intake", None)
    if intake is not None:
        try:
            intake.trigger()
        except Exception:
            log.warning("cannot wake the intake; the shared files are picked up at the next poll", exc_info=True)
    audit = getattr(services, "audit", None)
    if audit is not None:
        try:
            audit.write(user.username, "upload", batch=result["batch_id"], files=len(result["files"]), via="teilen")
        except Exception:
            log.exception("cannot write the audit log")
    count = len(result["files"])
    _flash(
        request,
        f"{count} {'Datei' if count == 1 else 'Dateien'} übernommen – {'sie wird' if count == 1 else 'sie werden'} "
        "jetzt verarbeitet.",
        "success",
        link="/eingang",
        link_label="Zum Eingang",
    )
    return RedirectResponse(SHARE_PATH, status_code=303)
