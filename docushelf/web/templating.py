"""Server-side rendering of the HTML pages with Jinja2.

:func:`render` is used by every HTML route (and by the error handlers and the
login page). It adds what the layout needs: the logged-in user, the CSRF token,
the inbox counts for the navigation badge, flash messages and the app version.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape
from starlette.requests import Request
from starlette.responses import HTMLResponse

from .. import __version__
from ..util.timeutil import now, parse_date, parse_iso_datetime, timezone
from . import views

log = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

NAV_ITEMS = (
    # key, label, url, icon, short label for the tab bar on phones
    ("eingang", "Eingang", "/eingang", "inbox", "Eingang"),
    ("archiv", "Archiv", "/archiv", "archive", "Archiv"),
    ("suche", "Suche", "/suche", "search", "Suche"),
    ("papierkorb", "Papierkorb", "/papierkorb", "trash", "Papierkorb"),
    ("regeln", "Regeln", "/regeln", "signpost", "Regeln"),
    ("einstellungen", "Einstellungen", "/einstellungen", "sliders", "System"),
)

# ---------------------------------------------------------------------------
# Filters


def _as_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime(value.year, value.month, value.day)
    elif isinstance(value, (int, float)):
        result = datetime.fromtimestamp(value, tz=timezone())
    else:
        result = parse_iso_datetime(str(value))
        if result is None:
            parsed = parse_date(value)
            if parsed is None:
                return None
            result = datetime(parsed.year, parsed.month, parsed.day)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone())
    return result.astimezone(timezone())


def date_de(value: Any) -> str:
    """``23.11.2026`` for dates, datetimes and ISO strings; '' for nothing."""
    if isinstance(value, datetime) or (isinstance(value, str) and "T" in value):
        moment = _as_datetime(value)
        return moment.strftime("%d.%m.%Y") if moment else ""
    parsed = parse_date(value)
    return parsed.strftime("%d.%m.%Y") if parsed else ""


def datetime_de(value: Any) -> str:
    """``23.11.2026, 14:05``."""
    moment = _as_datetime(value)
    if moment is None:
        return ""
    if not isinstance(value, datetime) and isinstance(value, date):
        return moment.strftime("%d.%m.%Y")
    return moment.strftime("%d.%m.%Y, %H:%M")


def iso(value: Any) -> str:
    """Machine-readable value for ``<time datetime>``."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    moment = _as_datetime(value)
    return moment.isoformat(timespec="seconds") if moment else ""


def filesize(value: Any) -> str:
    """``1,4 MB`` (binary units, German decimal comma)."""
    try:
        size = float(value)
    except (TypeError, ValueError):
        return ""
    if size < 1024:
        return f"{int(size)} Bytes" if size != 1 else "1 Byte"
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024.0
        if size < 1024 or unit == "TB":
            text = f"{size:.1f}" if size < 10 else f"{size:.0f}"
            return f"{text.replace('.', ',')} {unit}"
    return ""  # pragma: no cover


def _unit(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def reltime(value: Any, *, reference: datetime | None = None) -> str:
    """Relative German time: "gerade eben", "vor 5 Minuten", "gestern, 14:05", "in 3 Tagen", "am 12.03.2025"."""
    date_only = isinstance(value, date) and not isinstance(value, datetime)
    if isinstance(value, str) and len(value.strip()) == 10:
        date_only = True
    moment = _as_datetime(value)
    if moment is None:
        return ""
    current = reference or now()
    if date_only:
        days = (moment.date() - current.date()).days
        if days == 0:
            return "heute"
        if days == -1:
            return "gestern"
        if days == 1:
            return "morgen"
        if -7 < days < 0:
            return f"vor {-days} Tagen"
        if 0 < days < 60:
            return f"in {days} Tagen"
        return f"am {moment:%d.%m.%Y}"

    seconds = (current - moment).total_seconds()
    if seconds < 0:
        ahead = -seconds
        if ahead < 60:
            return "gleich"
        if ahead < 3600:
            return "in " + _unit(math.ceil(ahead / 60), "Minute", "Minuten")
        if ahead < 86400:
            return "in " + _unit(round(ahead / 3600), "Stunde", "Stunden")
        days = (moment.date() - current.date()).days
        if days == 1:
            return f"morgen, {moment:%H:%M}"
        if days < 60:
            return "in " + _unit(days, "Tag", "Tagen")
        return f"am {moment:%d.%m.%Y}"
    if seconds < 45:
        return "gerade eben"
    if seconds < 90:
        return "vor einer Minute"
    if seconds < 3600:
        return f"vor {round(seconds / 60)} Minuten"
    days = (current.date() - moment.date()).days
    if days == 0:
        hours = round(seconds / 3600)
        return "vor einer Stunde" if hours <= 1 else f"vor {hours} Stunden"
    if days == 1:
        return f"gestern, {moment:%H:%M}"
    if days < 7:
        return f"vor {days} Tagen"
    return f"am {moment:%d.%m.%Y}"


def percent(value: Any) -> str:
    try:
        return f"{round(float(value) * 100)} %"
    except (TypeError, ValueError):
        return ""


def urlpath(value: Any) -> str:
    """Percent-encode a folder path for use in a URL (slashes stay)."""
    return quote(str(value or ""), safe="/")


def urlpart(value: Any) -> str:
    """Percent-encode a single URL component."""
    return quote(str(value or ""), safe="")


# ---------------------------------------------------------------------------
# Static files and icons


class _StaticVersions:
    """Short content hashes for cache busting, refreshed when a file changes."""

    def __init__(self, directory: Path):
        self.directory = directory
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[tuple[int, int], str]] = {}

    def url(self, name: str) -> str:
        path = self.directory / name
        try:
            st = os.stat(path)
        except OSError:
            return f"/static/{name}"
        signature = (st.st_mtime_ns, st.st_size)
        with self._lock:
            cached = self._cache.get(name)
            if cached and cached[0] == signature:
                return f"/static/{name}?v={cached[1]}"
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:10]
        except OSError:
            return f"/static/{name}"
        with self._lock:
            self._cache[name] = (signature, digest)
        return f"/static/{name}?v={digest}"


_static_versions = _StaticVersions(STATIC_DIR)


def static_url(name: str) -> str:
    return _static_versions.url(name)


def icon(name: str, cls: str = "", *, label: str | None = None) -> Markup:
    """An icon from the SVG sprite. Decorative unless ``label`` is given."""
    classes = "icon" + (f" {cls}" if cls else "")
    href = f"{static_url('icons.svg')}#{name}"
    if label:
        aria = f' role="img" aria-label="{escape(label)}"'
    else:
        aria = ' aria-hidden="true" focusable="false"'
    return Markup(f'<svg class="{escape(classes)}"{aria}><use href="{escape(href)}"></use></svg>')


# ---------------------------------------------------------------------------
# Environment


def _create_environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml", "svg"]),
        trim_blocks=True,
        lstrip_blocks=True,
        auto_reload=True,
    )
    env.filters.update(
        date_de=date_de,
        datetime_de=datetime_de,
        iso=iso,
        filesize=filesize,
        reltime=reltime,
        percent=percent,
        seconds=views.format_seconds,
        urlpath=urlpath,
        urlpart=urlpart,
        number=views.format_number,
        iban=views.format_iban,
        folder_label=views.folder_label,
        source_label=views.source_label,
        source_icon=views.source_icon,
        snippet=lambda value: Markup(views.safe_snippet(value)),
        script_json=lambda value: Markup(views.json_for_script(value)),
    )
    env.globals.update(
        static_url=static_url,
        icon=icon,
        plural=views.plural,
        archive_url=views.archive_url,
        document_url=views.document_url,
        search_url=views.search_url,
        breadcrumbs=views.breadcrumbs,
        app_name="DocuShelf",
    )
    return env


environment = _create_environment()


# ---------------------------------------------------------------------------
# Context


def _services(request: Request) -> Any:
    """The service container of the request's household, or None."""
    try:
        from .security import household_services
    except ImportError:  # pragma: no cover - web-core not installed
        return getattr(request.app.state, "services", None)
    try:
        return household_services(request)
    except Exception:
        log.debug("cannot determine the household of the request", exc_info=True)
        return None


def _households(request: Request) -> tuple[Any, list[Any]]:
    """The household of the request and the households to switch between (several only)."""
    try:
        from .security import current_household, multi_household, switchable_households
    except ImportError:  # pragma: no cover - web-core not installed
        return None, []
    try:
        if not multi_household(request):
            return None, []
        return current_household(request), switchable_households(request)
    except Exception:
        log.debug("cannot determine the households of the request", exc_info=True)
        return None, []


def _current_user(request: Request) -> Any:
    try:
        from .security import current_user
    except ImportError:  # pragma: no cover - web-core not installed
        return None
    try:
        return current_user(request)
    except Exception:
        log.debug("cannot determine the current user", exc_info=True)
        return None


def _csrf_token(request: Request) -> str:
    if "session" not in request.scope:
        return ""
    try:
        from .security import csrf_token
    except ImportError:  # pragma: no cover - web-core not installed
        return ""
    try:
        return csrf_token(request)
    except Exception:
        log.debug("cannot create a CSRF token", exc_info=True)
        return ""


def inbox_counts(services: Any) -> dict[str, int]:
    inbox = getattr(services, "inbox", None)
    if inbox is None:
        return {}
    try:
        return dict(inbox.counts())
    except Exception:
        log.warning("cannot count inbox items", exc_info=True)
        return {}


def auto_pending(services: Any) -> int:
    """Uncertain automatic filings nobody checked yet (a small dot next to "Eingang")."""
    if services is None or getattr(services, "audit", None) is None:
        return 0
    try:
        return int(views.auto_pending_count(services))
    except Exception:
        log.warning("cannot count the automatic filings to check", exc_info=True)
        return 0


def build_nav(path: str, counts: Mapping[str, int], *, review: int = 0) -> list[dict[str, Any]]:
    entries = []
    for key, label, url, icon_name, short in NAV_ITEMS:
        active = path == url or path.startswith(url + "/")
        if key == "archiv" and path.startswith("/dokument/"):
            active = True
        entry: dict[str, Any] = {
            "key": key,
            "label": label,
            "short": short,
            "url": url,
            "icon": icon_name,
            "active": active,
        }
        if key == "eingang":
            entry["badge"] = int(counts.get("bereit", 0) or 0)
            entry["alert"] = int(counts.get("fehler", 0) or 0)
            entry["review"] = int(review or 0)
        entries.append(entry)
    return entries


def base_context(request: Request, provided: Mapping[str, Any] | None = None) -> dict[str, Any]:
    provided = provided or {}
    user = provided["user"] if "user" in provided else _current_user(request)
    services = _services(request)
    counts = inbox_counts(services) if user else {}
    review = auto_pending(services) if user else 0
    household, households = _households(request) if user else (None, [])
    path = request.url.path
    query = request.url.query
    return {
        "request": request,
        "user": user,
        "csrf_token": _csrf_token(request),
        "app_version": __version__,
        "inbox_counts": counts,
        "auto_review_count": review,
        "nav": build_nav(path, counts, review=review) if user else [],
        # Several households only: the current one and the ones to switch to.
        "household": household,
        "households": households,
        "flashes": views.pop_flashes(request),
        "current_path": path,
        "current_url": path + (f"?{query}" if query else ""),
    }


def render(
    request: Request,
    name: str,
    context: Mapping[str, Any] | None = None,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    """Render ``templates/<name>`` into an HTML response."""
    values = base_context(request, context)
    if context:
        values.update(context)
    html = environment.get_template(name).render(values)
    return HTMLResponse(html, status_code=status_code)


__all__ = [
    "STATIC_DIR",
    "TEMPLATE_DIR",
    "base_context",
    "build_nav",
    "date_de",
    "datetime_de",
    "environment",
    "filesize",
    "icon",
    "reltime",
    "render",
    "static_url",
]
