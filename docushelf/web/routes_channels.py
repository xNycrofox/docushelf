"""Settings page "Eingangskanäle": inbound channels of the household in the browser.

Admins (see :func:`security.is_channel_admin`) switch channels on and off and
configure them: IMAP mailboxes (with a connection test), scan-to-mail
addresses per household and per member, scanner subfolders per member for the
SMB/FTP drop zones and watch folders. Every change is written to the
household's ``config/channels.yaml`` through its channel manager
(``services.channels``), applied at once (only this household's fetchers and
intake restart) and recorded in the audit log (``channels_change``).

Passwords are written to ``config/secrets/`` and never sent back to the
browser: the page only says whether one is stored.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from ..channels import origin
from ..channels.manager import imap_config
from ..channels.store import (
    SCANNER_KINDS,
    ChannelConfig,
    Endpoint,
    Recipient,
    SmtpChannel,
    build_mailbox,
    build_watch,
    clean_address,
    clean_folder,
    clean_name,
    clean_owner,
)
from ..errors import ConfigError, DocuShelfError
from ..inbox.intake import layout_protected_paths
from . import security, views
from .security import User, get_services, verify_csrf
from .templating import environment, render
from .views import flash, form_value, redirect

log = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)

PAGE = "/einstellungen/kanaele"
AUDIT_ACTION = "channels_change"
# Tests replace this with a fake server's connect function.
IMAP_CONNECTION_FACTORY: Callable[..., Any] | None = None

# Managing channels needs a browser login, like managing tokens.
require_session_user = getattr(security, "require_session_user", security.require_user)

_SCANNER_HELP = {
    "smb": {
        "title": "Scanner (SMB-Freigabe)",
        "profile": "smb",
        "share": "scan",
        "account": "scanner (Passwort in deploy/samba.env)",
    },
    "ftp": {
        "title": "Scanner (FTP)",
        "profile": "ftp",
        "share": "",
        "account": "FTP_USER aus der .env (Standard: scanner)",
    },
}


# ---------------------------------------------------------------------------
# Template helpers (also used by the settings page and the inbox)


def _channels_admin(request: Request) -> bool:
    try:
        return security.is_channel_admin(request, security.current_user(request))
    except Exception:
        log.debug("cannot determine whether the user manages channels", exc_info=True)
        return False


def _channel_summary(request: Request) -> dict[str, Any] | None:
    """What the settings page shows of the household's channels (None: no channel manager)."""
    try:
        services = security.household_services(request)
    except Exception:
        return None
    manager = getattr(services, "channels", None)
    config = getattr(manager, "config", None)
    if config is None:
        return None
    smtp = config.smtp
    return {
        "from_file": bool(manager.from_file),
        "mailboxes": len(getattr(manager, "fetchers", {}) or {}),
        "recipients": [r.address for r in smtp.recipients] if smtp.enabled else [],
    }


environment.globals.setdefault("channels_admin", _channels_admin)
environment.globals.setdefault("channel_summary", _channel_summary)
environment.filters.setdefault("origin_label", origin.origin_label)


# ---------------------------------------------------------------------------
# Helpers


def _manager(request: Request) -> Any:
    return getattr(get_services(request), "channels", None)


def _guard(request: Request, user: Any) -> Response | None:
    """403 page for non-admins, 404 page when the household has no channel manager."""
    if not security.is_channel_admin(request, user):
        return _error(
            request,
            user,
            403,
            "Keine Berechtigung",
            "Eingangskanäle können nur Administratoren ändern.",
        )
    if _manager(request) is None:
        return _error(request, user, 404, "Nicht verfügbar", "Eingangskanäle lassen sich hier nicht einstellen.")
    return None


def _error(request: Request, user: Any, status_code: int, title: str, message: str) -> HTMLResponse:
    return render(
        request,
        "error.html",
        {"user": user, "status_code": status_code, "title": title, "message": message},
        status_code=status_code,
    )


def _members(services: Any) -> list[str]:
    household = getattr(services, "household", None)
    return sorted(dict.fromkeys(str(u) for u in getattr(household, "users", ()) or ()))


def _check_owner(services: Any, owner: str | None) -> str | None:
    """An owner must be a member of the household when the household lists its members."""
    if not owner:
        return None
    members = {m.casefold() for m in _members(services)}
    if members and owner.casefold() not in members:
        household = getattr(services, "household", None)
        name = getattr(household, "name", "") or "diesem Haushalt"
        raise ConfigError(
            f"„{owner}“ ist kein Mitglied von {name}. Trag einen der Benutzer des Haushalts ein "
            "(oder lass das Feld leer)."
        )
    return owner


def _flag(form: Mapping[str, Any], key: str) -> bool:
    return form_value(form, key).strip().lower() in {"1", "on", "true", "ja", "an"}


def _audit(services: Any, user: Any, summary: str, **details: Any) -> None:
    audit = getattr(services, "audit", None)
    if audit is None:
        return
    try:
        audit.write(getattr(user, "username", None), AUDIT_ACTION, summary=summary, **details)
    except Exception:
        log.exception("cannot write the audit log")


def _default_folder(owner: str | None) -> str:
    """``erika.mustermann@example.org`` → ``erika.mustermann`` (valid as a scanner folder)."""
    text = (owner or "").split("@", 1)[0].strip().lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text).strip("-._")
    return text[:64]


def _smtp_server(request: Request, services: Any) -> tuple[Any, dict[str, Any] | None]:
    """The SMTP receiver configuration and status (global with several households)."""
    if security.multi_household(request):
        registry = security.get_registry(request)
        config = getattr(getattr(request.app.state, "settings", None), "smtp", None)
        receiver = getattr(registry, "smtp", None)
    else:
        config = getattr(services.settings, "smtp", None)
        receiver = getattr(services, "smtp", None)
    status = None
    if receiver is not None and hasattr(receiver, "status"):
        try:
            status = dict(receiver.status())
        except Exception:
            log.debug("cannot read the SMTP status", exc_info=True)
    return config, status


def _address_elsewhere(request: Request, services: Any, address: str) -> str | None:
    """The name of another household that already receives mail for ``address``."""
    if not security.multi_household(request):
        return None
    registry = security.get_registry(request)
    own = getattr(getattr(services, "household", None), "id", None)
    try:
        household = registry.by_smtp_recipient(address)
    except Exception:
        return None
    if household is not None and household.id != own:
        return household.name
    return None


# System directories a watch folder set in the browser may never be (or lie in): the intake
# would copy files out of them and, with removal on, try to delete them.
_SYSTEM_ROOTS = ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64", "/proc", "/root", "/run",
                 "/sbin", "/sys", "/usr", "/var/lib", "/var/run")


def _watch_conflict(request: Request, services: Any, path: Path) -> str | None:
    """Why a watch folder path cannot be used (system or data directories), or None."""
    import os

    real = os.path.realpath(path)
    # Both the path as given and where it leads count (on some systems /etc is /private/etc).
    candidates = {os.path.normpath(str(path)), real, real.removeprefix("/private")}
    for candidate in candidates:
        if candidate == os.path.sep or any(
            candidate == root or candidate.startswith(root + os.path.sep) for root in _SYSTEM_ROOTS
        ):
            return f"Der Ordner {path} ist ein Systemordner und kann nicht überwacht werden."
    package = os.path.realpath(Path(__file__).resolve().parent.parent)
    if real == package or real.startswith(package + os.path.sep) or package.startswith(real + os.path.sep):
        return f"Der Ordner {path} enthält das Programm DocuShelf selbst und kann nicht überwacht werden."
    protected = list(layout_protected_paths(services.layout))
    if security.multi_household(request):
        registry = security.get_registry(request)
        own = getattr(getattr(services, "household", None), "id", None)
        for household in registry.households:
            if household.id == own:
                continue
            try:
                protected.extend(layout_protected_paths(registry.services(household.id).layout))
            except Exception:
                continue
    for directory in protected:
        other = os.path.realpath(directory)
        try:
            common = os.path.commonpath([real, other])
        except ValueError:
            continue
        if common in (real, other):
            return f"Der Ordner {path} überschneidet sich mit den Daten von DocuShelf ({directory}) – bitte einen anderen wählen."
    return None


# ---------------------------------------------------------------------------
# The page


def _page_context(request: Request, services: Any, manager: Any) -> dict[str, Any]:
    config: ChannelConfig = manager.config
    mailboxes = []
    for box in config.imap:
        mailboxes.append(
            {
                "box": box,
                "has_password": manager.has_password(box.name),
                "status": manager.fetcher_status(box.name) if box.enabled else None,
                "senders": ", ".join(box.allowed_senders),
            }
        )
    smtp_config, smtp_status = _smtp_server(request, services)
    layout = services.layout
    scanners = []
    for kind in SCANNER_KINDS:
        scanner = config.scanner(kind)
        try:
            root = layout.source_dir(kind)
        except Exception:
            root = None
        endpoints = [
            {
                "endpoint": point,
                "path": str(root / point.folder) if root else "",
                "exists": bool(root and (root / point.folder).is_dir()),
            }
            for point in scanner.endpoints
        ]
        scanners.append(
            {
                "kind": kind,
                "scanner": scanner,
                "root": str(root) if root else "",
                "endpoints": endpoints,
                "help": _SCANNER_HELP[kind],
            }
        )
    watch = [
        {"folder": folder, "available": folder.path.is_dir()}
        for folder in config.watch
    ]
    settings = getattr(request.app.state, "settings", None) or services.settings
    return {
        "channel_household": getattr(services, "household", None),
        "multi": security.multi_household(request),
        "from_file": manager.from_file,
        "store_path": str(manager.store.path),
        "mailboxes": mailboxes,
        "smtp": config.smtp,
        "smtp_config": smtp_config,
        "smtp_status": smtp_status,
        "scanners": scanners,
        "watch": watch,
        "members": _members(services),
        "server_host": _server_host(settings),
    }


def _server_host(settings: Any) -> str:
    base = str(getattr(settings, "base_url", "") or "")
    host = re.sub(r"^[a-z]+://", "", base).split("/", 1)[0].split(":", 1)[0]
    return host or "server"


def _render(
    request: Request,
    user: Any,
    *,
    section: str | None = None,
    values: Mapping[str, Any] | None = None,
    message: str | None = None,
    ok: bool = False,
    status_code: int = 200,
    settings_form: Any = None,
) -> HTMLResponse:
    from .routes_settings import layout_context, section_views

    services = get_services(request)
    manager = services.channels
    context = layout_context(request, user, "kanaele")
    context.update(_page_context(request, services, manager))
    context.update(
        {
            "user": user,
            "form_section": section,
            "form_values": dict(values or {}),
            "form_message": message,
            "form_ok": ok,
            "sections": section_views(request, "kanaele", form=settings_form),
            "multi": security.multi_household(request),
        }
    )
    response = render(request, "channels.html", context, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


def render_channels(request: Request, user: Any, *, settings_form: Any = None, status_code: int = 200) -> Response:
    """The page with a message for one of its settings sections (used by the settings area)."""
    denied = _guard(request, user)
    if denied is not None:
        return denied
    return _render(request, user, settings_form=settings_form, status_code=status_code)


@router.get(PAGE, response_class=HTMLResponse)
def channels_page(request: Request, user: User = Depends(require_session_user)) -> Response:
    denied = _guard(request, user)
    if denied is not None:
        return denied
    return _render(request, user)


async def _with_form(request: Request, handler: Callable[..., Response], *args: Any) -> Response:
    from starlette.concurrency import run_in_threadpool

    form = await request.form()
    try:
        return await run_in_threadpool(handler, request, form, *args)
    finally:
        await form.close()


def _apply(
    request: Request,
    user: Any,
    form: Mapping[str, Any],
    *,
    section: str,
    anchor: str,
    change: Callable[[ChannelConfig], ChannelConfig],
    summary: str,
    passwords: Mapping[str, str] | None = None,
    renamed: Mapping[str, str] | None = None,
    **details: Any,
) -> Response:
    """Run a change through the manager; errors re-render the page with the input kept."""
    services = get_services(request)
    try:
        services.channels.update(change, passwords=passwords, renamed=renamed)
    except DocuShelfError as exc:
        values = {key: value for key, value in form.items() if key not in ("password", "csrf_token")}
        return _render(
            request,
            user,
            section=section,
            values=values,
            message=views.error_text(exc, "Die Eingangskanäle konnten nicht gespeichert werden."),
            status_code=400,
        )
    _audit(services, user, summary, **details)
    flash(request, f"{summary}. Die Änderung gilt ab sofort.")
    return redirect(f"{PAGE}#{anchor}")


def _post(path: str, handler: Callable[..., Response]) -> None:
    async def endpoint(
        request: Request, user: User = Depends(require_session_user), _csrf: Any = Depends(verify_csrf)
    ) -> Response:
        denied = _guard(request, user)
        if denied is not None:
            return denied
        return await _with_form(request, handler, user, dict(request.path_params))

    endpoint.__name__ = "channels_" + re.sub(r"\W+", "_", path).strip("_")
    router.add_api_route(PAGE + path, endpoint, methods=["POST"])


# ---------------------------------------------------------------------------
# Mailboxes (IMAP)


def _mailbox_from_form(services: Any, form: Mapping[str, Any]) -> Any:
    name = clean_name(form_value(form, "name").strip().lower(), "Name des Postfachs")
    values = {
        "host": form_value(form, "host"),
        "user": form_value(form, "user"),
        "port": form_value(form, "port"),
        "tls": form_value(form, "tls") or "ssl",
        "folder": form_value(form, "folder"),
        "processed_folder": form_value(form, "processed_folder"),
        "rejected_folder": form_value(form, "rejected_folder"),
        "no_attachment_folder": form_value(form, "no_attachment_folder"),
        "allowed_senders": form_value(form, "allowed_senders"),
        "poll_seconds": form_value(form, "poll_seconds"),
        "owner": form_value(form, "owner"),
        "enabled": "1" if _flag(form, "enabled") else "0",
    }
    box = build_mailbox(values, name=name, where=f"Postfach „{name}“")
    _check_owner(services, box.owner)
    return box


def _save_mailbox(request: Request, form: Any, user: Any, params: dict[str, str]) -> Response:
    services = get_services(request)
    manager = services.channels
    original = form_value(form, "original").strip()
    password = form_value(form, "password")
    section = f"imap:{original or 'neu'}"
    values = {key: value for key, value in form.items() if key not in ("password", "csrf_token")}
    try:
        box = _mailbox_from_form(services, form)
        if form_value(form, "aktion") == "testen":
            return _test_mailbox(request, user, box, password or None, original, section, values)
        existing = manager.config.mailbox(box.name)
        if existing is not None and box.name != original:
            raise ConfigError(f"Ein Postfach „{box.name}“ gibt es schon – bitte einen anderen Namen wählen.")
        if original and manager.config.mailbox(original) is None:
            raise ConfigError(f"Das Postfach „{original}“ gibt es nicht mehr. Bitte die Seite neu laden.")
        if box.enabled and not password and not manager.has_password(original or box.name):
            raise ConfigError("Bitte das Passwort des Postfachs eingeben.")
    except DocuShelfError as exc:
        return _render(request, user, section=section, values=values, message=views.error_text(exc, "Ungültige Eingabe."), status_code=400)
    if len(password) > 1000 or any(ord(ch) < 32 for ch in password):
        return _render(request, user, section=section, values=values, message="Das Passwort ist ungültig.", status_code=400)

    def change(config: ChannelConfig) -> ChannelConfig:
        boxes = list(config.imap)
        if original:
            boxes = [box if old.name == original else old for old in boxes]
        else:
            boxes.append(box)
        return replace(config, imap=tuple(boxes))

    renamed = {box.name: original} if original and original != box.name else None
    verb = "angelegt" if not original else "gespeichert"
    return _apply(
        request,
        user,
        form,
        section=section,
        anchor="postfaecher",
        change=change,
        summary=f"Postfach „{box.name}“ {verb}",
        passwords={box.name: password} if password else None,
        renamed=renamed,
        channel="imap",
        name=box.name,
        password_changed=bool(password),
    )


def _test_mailbox(
    request: Request,
    user: Any,
    box: Any,
    password: str | None,
    original: str,
    section: str,
    values: Mapping[str, Any],
) -> Response:
    from ..channels.imap import probe_connection

    manager = get_services(request).channels
    if not password:
        try:
            password = manager.password(original or box.name)
        except ConfigError:
            password = None
    if not password:
        return _render(
            request, user, section=section, values=values, message="Für den Test bitte das Passwort eingeben.", status_code=400
        )
    result = probe_connection(imap_config(box, password), connection_factory=IMAP_CONNECTION_FACTORY)
    return _render(request, user, section=section, values=values, message=result.message, ok=result.ok)


def _toggle_mailbox(request: Request, form: Any, user: Any, params: dict[str, str]) -> Response:
    name = params.get("name", "")
    manager = get_services(request).channels
    box = manager.config.mailbox(name)
    if box is None:
        flash(request, f"Das Postfach „{name}“ gibt es nicht (mehr).", "error")
        return redirect(f"{PAGE}#postfaecher")
    enabled = _flag(form, "an")

    def change(config: ChannelConfig) -> ChannelConfig:
        return replace(config, imap=tuple(replace(b, enabled=enabled) if b.name == name else b for b in config.imap))

    if enabled and not manager.has_password(name):
        flash(request, f"Für das Postfach „{name}“ ist kein Passwort gespeichert. Bitte erst eins eingeben.", "error")
        return redirect(f"{PAGE}#postfach-{name}")
    return _apply(
        request,
        user,
        form,
        section=f"imap:{name}",
        anchor="postfaecher",
        change=change,
        summary=f"Postfach „{name}“ {'eingeschaltet' if enabled else 'ausgeschaltet'}",
        channel="imap",
        name=name,
    )


def _remove_mailbox(request: Request, form: Any, user: Any, params: dict[str, str]) -> Response:
    name = params.get("name", "")

    def change(config: ChannelConfig) -> ChannelConfig:
        if config.mailbox(name) is None:
            raise ConfigError(f"Das Postfach „{name}“ gibt es nicht (mehr).")
        return replace(config, imap=tuple(b for b in config.imap if b.name != name))

    return _apply(
        request,
        user,
        form,
        section=f"imap:{name}",
        anchor="postfaecher",
        change=change,
        summary=f"Postfach „{name}“ entfernt",
        channel="imap",
        name=name,
    )


# ---------------------------------------------------------------------------
# Scan per mail (SMTP)


def _toggle_smtp(request: Request, form: Any, user: Any, params: dict[str, str]) -> Response:
    enabled = _flag(form, "an")

    def change(config: ChannelConfig) -> ChannelConfig:
        return replace(config, smtp=replace(config.smtp, enabled=enabled))

    return _apply(
        request,
        user,
        form,
        section="smtp",
        anchor="scan-per-mail",
        change=change,
        summary=f"Scan per Mail {'eingeschaltet' if enabled else 'ausgeschaltet'}",
        channel="smtp",
    )


def _add_recipient(request: Request, form: Any, user: Any, params: dict[str, str]) -> Response:
    services = get_services(request)
    values = {key: value for key, value in form.items() if key != "csrf_token"}
    try:
        address = clean_address(form_value(form, "address"))
        owner = _check_owner(services, clean_owner(form_value(form, "owner")))
        elsewhere = _address_elsewhere(request, services, address)
        if elsewhere:
            raise ConfigError(f"Die Adresse {address} gehört schon zum Haushalt „{elsewhere}“.")
    except DocuShelfError as exc:
        return _render(request, user, section="smtp", values=values, message=views.error_text(exc, "Ungültige Eingabe."), status_code=400)

    def change(config: ChannelConfig) -> ChannelConfig:
        if config.smtp.find(address) is not None:
            raise ConfigError(f"Die Adresse {address} ist schon eingetragen.")
        recipients = (*config.smtp.recipients, Recipient(address, owner))
        enabled = config.smtp.enabled or not config.smtp.recipients
        return replace(config, smtp=SmtpChannel(enabled=enabled, recipients=recipients))

    return _apply(
        request,
        user,
        form,
        section="smtp",
        anchor="scan-per-mail",
        change=change,
        summary=f"Scan-Adresse {address} hinzugefügt" + (f" (für {owner})" if owner else ""),
        channel="smtp",
        address=address,
    )


def _remove_recipient(request: Request, form: Any, user: Any, params: dict[str, str]) -> Response:
    address = (form_value(form, "address") or "").strip().lower()

    def change(config: ChannelConfig) -> ChannelConfig:
        if config.smtp.find(address) is None:
            raise ConfigError(f"Die Adresse {address} ist nicht (mehr) eingetragen.")
        recipients = tuple(r for r in config.smtp.recipients if r.address != address)
        return replace(config, smtp=replace(config.smtp, recipients=recipients))

    return _apply(
        request,
        user,
        form,
        section="smtp",
        anchor="scan-per-mail",
        change=change,
        summary=f"Scan-Adresse {address} entfernt",
        channel="smtp",
        address=address,
    )


# ---------------------------------------------------------------------------
# Scanner folders (SMB/FTP)


def _scanner_kind(params: Mapping[str, str]) -> str:
    kind = params.get("kind", "")
    if kind not in SCANNER_KINDS:
        raise ConfigError("Diesen Scanner-Kanal gibt es nicht.")
    return kind


def _toggle_scanner(request: Request, form: Any, user: Any, params: dict[str, str]) -> Response:
    try:
        kind = _scanner_kind(params)
    except ConfigError as exc:
        flash(request, str(exc), "error")
        return redirect(PAGE)
    enabled = _flag(form, "an")

    def change(config: ChannelConfig) -> ChannelConfig:
        return config.with_scanner(replace(config.scanner(kind), enabled=enabled))

    return _apply(
        request,
        user,
        form,
        section=f"scanner:{kind}",
        anchor=f"scanner-{kind}",
        change=change,
        summary=f"{_SCANNER_HELP[kind]['title']} {'eingeschaltet' if enabled else 'ausgeschaltet'}",
        channel=kind,
    )


def _add_endpoint(request: Request, form: Any, user: Any, params: dict[str, str]) -> Response:
    services = get_services(request)
    values = {key: value for key, value in form.items() if key != "csrf_token"}
    try:
        kind = _scanner_kind(params)
        owner = _check_owner(services, clean_owner(form_value(form, "owner")))
        folder_raw = form_value(form, "folder").strip() or _default_folder(owner)
        if not folder_raw:
            raise ConfigError("Bitte eine Person oder einen Ordnernamen angeben.")
        folder = clean_folder(folder_raw, "Ordner")
    except DocuShelfError as exc:
        return _render(
            request,
            user,
            section=f"scanner:{params.get('kind', '')}",
            values=values,
            message=views.error_text(exc, "Ungültige Eingabe."),
            status_code=400,
        )

    def change(config: ChannelConfig) -> ChannelConfig:
        scanner = config.scanner(kind)
        if scanner.find(folder) is not None:
            raise ConfigError(f"Den Ordner „{folder}“ gibt es bei {scanner.label} schon.")
        endpoints = (*scanner.endpoints, Endpoint(folder=folder, owner=owner, enabled=True))
        return config.with_scanner(replace(scanner, enabled=True, endpoints=endpoints))

    return _apply(
        request,
        user,
        form,
        section=f"scanner:{kind}",
        anchor=f"scanner-{kind}",
        change=change,
        summary=f"Scanner-Ordner „{folder}“ ({kind.upper()}) angelegt" + (f" für {owner}" if owner else ""),
        channel=kind,
        folder=folder,
    )


def _endpoint_change(request: Request, form: Any, user: Any, params: dict[str, str], *, remove: bool) -> Response:
    try:
        kind = _scanner_kind(params)
    except ConfigError as exc:
        flash(request, str(exc), "error")
        return redirect(PAGE)
    folder = form_value(form, "folder").strip()
    enabled = _flag(form, "an")

    def change(config: ChannelConfig) -> ChannelConfig:
        scanner = config.scanner(kind)
        if scanner.find(folder) is None:
            raise ConfigError(f"Den Ordner „{folder}“ gibt es bei {scanner.label} nicht (mehr).")
        if remove:
            endpoints = tuple(e for e in scanner.endpoints if e.folder.casefold() != folder.casefold())
        else:
            endpoints = tuple(
                replace(e, enabled=enabled) if e.folder.casefold() == folder.casefold() else e for e in scanner.endpoints
            )
        return config.with_scanner(replace(scanner, endpoints=endpoints))

    if remove:
        summary = f"Scanner-Ordner „{folder}“ ({kind.upper()}) entfernt"
    else:
        summary = f"Scanner-Ordner „{folder}“ ({kind.upper()}) {'eingeschaltet' if enabled else 'ausgeschaltet'}"
    return _apply(
        request,
        user,
        form,
        section=f"scanner:{kind}",
        anchor=f"scanner-{kind}",
        change=change,
        summary=summary,
        channel=kind,
        folder=folder,
    )


def _toggle_endpoint(request: Request, form: Any, user: Any, params: dict[str, str]) -> Response:
    return _endpoint_change(request, form, user, params, remove=False)


def _remove_endpoint(request: Request, form: Any, user: Any, params: dict[str, str]) -> Response:
    return _endpoint_change(request, form, user, params, remove=True)


# ---------------------------------------------------------------------------
# Watch folders


def _save_watch(request: Request, form: Any, user: Any, params: dict[str, str]) -> Response:
    services = get_services(request)
    original = form_value(form, "original").strip()
    section = f"watch:{original or 'neu'}"
    values = {key: value for key, value in form.items() if key != "csrf_token"}
    try:
        folder = build_watch(
            {
                "name": form_value(form, "name").strip(),
                "path": form_value(form, "path"),
                "remove_after_import": "1" if _flag(form, "remove_after_import") else "0",
                "enabled": "1" if _flag(form, "enabled") else "0",
            },
            where="Watch-Ordner",
        )
        conflict = _watch_conflict(request, services, folder.path)
        if conflict:
            raise ConfigError(conflict)
        manager = services.channels
        if manager.config.watch_folder(folder.name) is not None and folder.name != original:
            raise ConfigError(f"Einen Watch-Ordner „{folder.name}“ gibt es schon.")
    except DocuShelfError as exc:
        return _render(request, user, section=section, values=values, message=views.error_text(exc, "Ungültige Eingabe."), status_code=400)

    def change(config: ChannelConfig) -> ChannelConfig:
        folders = list(config.watch)
        if original:
            if config.watch_folder(original) is None:
                raise ConfigError(f"Den Watch-Ordner „{original}“ gibt es nicht mehr. Bitte die Seite neu laden.")
            folders = [folder if old.name == original else old for old in folders]
        else:
            folders.append(folder)
        return replace(config, watch=tuple(folders))

    return _apply(
        request,
        user,
        form,
        section=section,
        anchor="watch-ordner",
        change=change,
        summary=f"Watch-Ordner „{folder.name}“ {'gespeichert' if original else 'angelegt'}",
        channel="watch",
        name=folder.name,
        path=str(folder.path),
    )


def _toggle_watch(request: Request, form: Any, user: Any, params: dict[str, str]) -> Response:
    name = params.get("name", "")
    enabled = _flag(form, "an")

    def change(config: ChannelConfig) -> ChannelConfig:
        if config.watch_folder(name) is None:
            raise ConfigError(f"Den Watch-Ordner „{name}“ gibt es nicht (mehr).")
        return replace(config, watch=tuple(replace(w, enabled=enabled) if w.name == name else w for w in config.watch))

    return _apply(
        request,
        user,
        form,
        section=f"watch:{name}",
        anchor="watch-ordner",
        change=change,
        summary=f"Watch-Ordner „{name}“ {'eingeschaltet' if enabled else 'ausgeschaltet'}",
        channel="watch",
        name=name,
    )


def _remove_watch(request: Request, form: Any, user: Any, params: dict[str, str]) -> Response:
    name = params.get("name", "")

    def change(config: ChannelConfig) -> ChannelConfig:
        if config.watch_folder(name) is None:
            raise ConfigError(f"Den Watch-Ordner „{name}“ gibt es nicht (mehr).")
        return replace(config, watch=tuple(w for w in config.watch if w.name != name))

    return _apply(
        request,
        user,
        form,
        section=f"watch:{name}",
        anchor="watch-ordner",
        change=change,
        summary=f"Watch-Ordner „{name}“ entfernt",
        channel="watch",
        name=name,
    )


_post("/imap", _save_mailbox)
_post("/imap/{name}/schalten", _toggle_mailbox)
_post("/imap/{name}/entfernen", _remove_mailbox)
_post("/smtp/schalten", _toggle_smtp)
_post("/smtp/adressen", _add_recipient)
_post("/smtp/adressen/entfernen", _remove_recipient)
_post("/scanner/{kind}/schalten", _toggle_scanner)
_post("/scanner/{kind}/endpunkte", _add_endpoint)
_post("/scanner/{kind}/endpunkte/schalten", _toggle_endpoint)
_post("/scanner/{kind}/endpunkte/entfernen", _remove_endpoint)
_post("/watch", _save_watch)
_post("/watch/{name}/schalten", _toggle_watch)
_post("/watch/{name}/entfernen", _remove_watch)


__all__ = ["PAGE", "render_channels", "router"]
