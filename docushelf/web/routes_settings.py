"""The settings area: an overview and one subpage per topic.

``/einstellungen`` shows what is set up, with one card per subpage. The
subpages (``/einstellungen/<seite>``) share a navigation on the left (a row of
tabs on phones). Everyone logged in sees the overview, their tokens and the
phone setup, the audit log and the index of their household; everything that
concerns the whole installation is shown to admins only
(:func:`security.is_channel_admin`).

Saving works the same on every subpage: each card (a
:class:`~.settings_fields.Section`) is one form posting to
``/einstellungen/speichern/<seite>/<abschnitt>``. The values are checked, then
written – installation-wide settings to ``config/settings.yaml`` (see
:mod:`docushelf.settings_file`), values of one household to its entry in
``households.yaml`` when there are several households – and every change is
audited (``settings_change``). What the running server can pick up applies at
once; everything else shows a banner until the server was restarted
("Neustart nötig, damit … wirkt" with a button "Jetzt neu starten").
Settings set through environment variables are shown, but locked.
"""

from __future__ import annotations

import logging
import platform
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from .. import __version__
from ..errors import ConfigError, DocuShelfError
from ..households import MAX_MEMBER_LENGTH
from ..settings_file import SettingsFile, apply_live, env_name
from ..util.timeutil import now
from . import security, views
from . import settings_fields as sf
from .security import User, get_services, require_user, verify_csrf
from .templating import environment, render, reltime
from .views import flash, form_value, redirect

log = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)

BASE = "/einstellungen"
AUDIT_ACTION = "settings_change"
NEW_TOKEN_KEY = "_new_token"
# How long the language model test may hold the page; a slower answer is shown later.
LLM_PROBE_WAIT = 95.0
# Delay between the answer of "Jetzt neu starten" and the shutdown, so the page arrives.
RESTART_DELAY = 1.0
AUDIT_PAGE_SIZE = 100
AUDIT_MAX = 2000
AUDIT_PERIODS = (("7", "7 Tage"), ("30", "30 Tage"), ("90", "90 Tage"), ("365", "1 Jahr"), ("0", "alles"))

require_session_user = getattr(security, "require_session_user", require_user)


# ---------------------------------------------------------------------------
# Access and shared state


def is_admin(request: Request, user: Any) -> bool:
    """Whether ``user`` may see and change installation-wide settings (browser login only)."""
    try:
        return security.is_channel_admin(request, user)
    except Exception:
        log.debug("cannot determine whether the user is an admin", exc_info=True)
        return False


def settings_store(request: Request) -> SettingsFile:
    """``config/settings.yaml`` of this installation (environment: ``app.state.settings_env``)."""
    state = request.app.state
    settings = security.get_settings(request)
    return SettingsFile(settings.root_dir, env=getattr(state, "settings_env", None))


def _running(request: Request) -> Any:
    return security.get_settings(request)


def _saved(request: Request, store: SettingsFile) -> tuple[Any, dict[str, Any], str | None]:
    """The settings as saved (what a restart would load), the file's values and an error text."""
    try:
        values = store.read()
    except ConfigError as exc:
        return None, {}, str(exc)
    try:
        return store.load(values), values, None
    except ConfigError as exc:
        return None, values, str(exc)


def _multi(request: Request) -> bool:
    return security.multi_household(request)


def pending_restart(request: Request, store: SettingsFile | None = None, saved: Any = None) -> list[str]:
    """Titles of the sections whose saved values differ from what the server runs with."""
    store = store or settings_store(request)
    if saved is None:
        saved, _values, error = _saved(request, store)
        if saved is None:
            return []
    running = _running(request)
    multi = _multi(request)
    titles: list[str] = []
    for _page, sec in sf.all_sections():
        if sec.live or sec.title in titles:
            continue
        for f in sf.visible_fields(sec, multi=multi):
            if f.value_of(running) != f.value_of(saved):
                titles.append(sec.title)
                break
    return titles


def restart_text(titles: list[str]) -> str:
    """„Neustart nötig, damit die Änderung an „KI“ wirkt.“"""
    quoted = [f"„{t}“" for t in titles]
    if len(quoted) == 1:
        return f"Neustart nötig, damit die Änderung an {quoted[0]} wirkt."
    joined = ", ".join(quoted[:-1]) + " und " + quoted[-1]
    return f"Neustart nötig, damit die Änderungen an {joined} wirken."


def _restart_handler(request: Request) -> Callable[[], None] | None:
    return getattr(request.app.state, "restart_handler", None)


def _audit(services: Any, user: Any, action: str, **details: Any) -> None:
    audit = getattr(services, "audit", None)
    if audit is None:
        return
    try:
        audit.write(getattr(user, "username", None) or None, action, **details)
    except Exception:
        log.warning("cannot write audit entry %s", action, exc_info=True)


def _error_page(request: Request, user: Any, status_code: int, title: str, message: str) -> HTMLResponse:
    return render(
        request,
        "error.html",
        {"user": user, "status_code": status_code, "title": title, "message": message},
        status_code=status_code,
    )


def _forbidden(request: Request, user: Any) -> HTMLResponse:
    return _error_page(
        request, user, 403, "Keine Berechtigung", "Diese Einstellungen können nur Administratoren sehen und ändern."
    )


async def _with_form(request: Request, handler: Callable[..., Response], *args: Any) -> Response:
    form = await request.form()
    try:
        return await run_in_threadpool(handler, request, form, *args)
    finally:
        await form.close()


# ---------------------------------------------------------------------------
# Building blocks of every subpage


@dataclass
class FormState:
    """A form that comes back with an error (or a message) and the values that were typed."""

    section: str | None = None
    values: dict[str, Any] = field(default_factory=dict)
    message: str | None = None
    ok: bool = False


def _nav(request: Request, user: Any, active: str) -> list[dict[str, Any]]:
    admin = is_admin(request, user)
    return [
        {"key": p.key, "label": p.label, "icon": p.icon, "url": p.url, "active": p.key == active}
        for p in sf.PAGES
        if admin or not p.admin
    ]


def layout_context(request: Request, user: Any, active: str) -> dict[str, Any]:
    """What ``settings/_base.html`` needs: navigation, admin flag, restart banner."""
    admin = is_admin(request, user)
    context: dict[str, Any] = {
        "user": user,
        "settings_nav": _nav(request, user, active),
        "settings_page": sf.page(active),
        "settings_admin": admin,
        "restart_pending": [],
        "restart_message": "",
        "restart_available": False,
        "settings_error": None,
    }
    if admin:
        store = settings_store(request)
        saved, _values, error = _saved(request, store)
        pending = pending_restart(request, store, saved) if saved is not None else []
        context.update(
            {
                "restart_pending": pending,
                "restart_message": restart_text(pending) if pending else "",
                "restart_available": _restart_handler(request) is not None,
                "settings_error": error,
            }
        )
    return context


def section_views(
    request: Request,
    page_key: str,
    *,
    form: FormState | None = None,
    only: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """The forms of a page's sections with current values, locks and the message of the last save."""
    found = sf.page(page_key)
    if found is None:
        return []
    store = settings_store(request)
    saved, values, _error = _saved(request, store)
    services = security.household_services(request)
    multi = _multi(request)
    done = request.query_params.get("gespeichert")
    unchanged = request.query_params.get("unveraendert")
    result = []
    for sec in found.sections:
        if only is not None and sec.key not in only:
            continue
        typed = form is not None and form.section == sec.key
        household_scope = sec.scope == "household" and multi
        visible = sf.visible_fields(sec, multi=multi)
        if not visible:
            continue
        fields = []
        for f in visible:
            locked = None if household_scope else store.env_source(f.key)
            view: dict[str, Any] = {"field": f, "locked": locked, "id": f"f-{sec.key}-{f.key}", "value": ""}
            if f.kind == "secret":
                view["has_secret"] = store.has_secret(f.key, values)
            elif typed and not locked:
                view["value"] = form.values.get(f.key, False if f.kind == "bool" else ([] if f.kind == "sources" else ""))
            elif household_scope:
                current = getattr(services, "settings", None)
                view["value"] = sf.form_text(f, f.value_of(current))
            elif locked:
                env_value = store.env_value(f.key)
                view["value"] = sf.form_text(f, env_value if env_value is not None else f.value_of(saved))
            else:
                view["value"] = sf.form_text(f, values.get(f.key, f.default))
            if f.kind == "sources":
                chosen = list(view["value"] or [])
                known = {key for key, _ in f.choices}
                view["extra"] = ", ".join(v for v in chosen if v not in known)
                if typed and not locked:
                    view["extra"] = form.values.get(f.key + "__extra", view["extra"])
            fields.append(view)
        household = getattr(services, "household", None)
        message, ok = (form.message, form.ok) if typed else (None, False)
        if not typed and done == sec.key:
            message, ok = saved_text(sec), True
        elif not typed and unchanged == sec.key:
            message, ok = "Gespeichert – es hat sich nichts geändert.", True
        result.append(
            {
                "section": sec,
                "fields": fields,
                "action": f"{BASE}/speichern/{found.key}/{sec.key}",
                "household": household if household_scope else None,
                "all_locked": bool(fields) and all(v["locked"] for v in fields),
                "message": message,
                "ok": ok,
                "typed": typed,
            }
        )
    return result


def saved_text(sec: sf.Section) -> str:
    if sec.live:
        return "Gespeichert. Die Änderung gilt ab sofort."
    return "Gespeichert. Die Änderung wirkt nach einem Neustart (siehe Hinweis oben)."


def _render(
    request: Request,
    user: Any,
    page_key: str,
    template: str,
    context: Mapping[str, Any],
    *,
    form: FormState | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    values = layout_context(request, user, page_key)
    if values["settings_admin"]:
        values["sections"] = section_views(request, page_key, form=form)
    else:
        values["sections"] = []
    values["multi"] = _multi(request)
    values.update(context)
    response = render(request, template, values, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# Overview


def _stats(services: Any) -> dict[str, Any]:
    try:
        return dict(services.index.stats())
    except Exception:
        log.exception("cannot read index statistics")
        return {}


def _counts(services: Any) -> dict[str, int]:
    try:
        return dict(services.inbox.counts())
    except Exception:
        return {}


def _channel_status(services: Any) -> str:
    manager = getattr(services, "channels", None)
    config = getattr(manager, "config", None)
    if config is None:
        sources = [s.name for s in getattr(services.settings, "sources", ())]
        return f"{len(sources)} Eingänge" if sources else "nur Hochladen"
    parts = []
    boxes = [b for b in config.imap if b.enabled]
    if boxes:
        parts.append(views.plural(len(boxes), "Postfach", "Postfächer"))
    if config.smtp.enabled and config.smtp.recipients:
        parts.append("Scan per Mail an")
    for scanner in config.scanners:
        if scanner.enabled:
            parts.append(f"{scanner.kind.upper()} an")
    watch = [w for w in config.watch if w.enabled]
    if watch:
        parts.append(views.plural(len(watch), "Watch-Ordner", "Watch-Ordner"))
    return " · ".join(parts) or "nur Hochladen"


def _overview_status(request: Request, services: Any, key: str) -> str:
    settings = services.settings
    registry = security.get_registry(request)
    try:
        if key == "haushalte":
            households = list(getattr(registry, "households", []) or [])
            if _multi(request):
                members = sum(len(h.users) for h in households)
                return f"{views.plural(len(households), 'Haushalt', 'Haushalte')} · {views.plural(members, 'Mitglied', 'Mitglieder')}"
            name = getattr(getattr(services, "household", None), "name", None) or settings.household_name
            return f"Ein Haushalt: „{name}“"
        if key == "kanaele":
            return _channel_status(services)
        if key == "ablage":
            auto = views.auto_settings(settings)
            trash = settings.trash_retention_days
            return f"Automatisch: {auto.summary} · Papierkorb {trash} Tage" if trash > 0 else f"Automatisch: {auto.summary}"
        if key == "ki":
            info = views.llm_info(settings)
            return f"{info.provider_label} · {info.model}" if info.enabled else "aus"
        if key == "verarbeitung":
            ocr = f"Texterkennung an ({settings.ocr_languages})" if settings.ocr_enabled else "Texterkennung aus"
            blank = "Leerseiten werden entfernt" if settings.blank_page_removal else "Leerseiten bleiben"
            return f"{ocr} · {blank}"
        if key == "benachrichtigungen":
            url = getattr(settings, "notify_url", None)
            return f"an {views.endpoint_host(url) or 'eingerichtet'}" if url else "aus"
        if key == "handy":
            tokens = getattr(services, "tokens", None)
            count = len(tokens.list()) if tokens is not None else 0
            return views.plural(count, "API-Token", "API-Tokens") if count else "noch keine Tokens"
        if key == "anmeldung":
            sso = "SSO eingerichtet" if settings.oidc_enabled else "ohne SSO"
            local = "Notfallzugang aktiv" if settings.local_login_enabled else "kein Notfallzugang"
            return f"{sso} · {local}"
        if key == "protokoll":
            entries = services.audit.tail(1)
            if entries:
                return f"letzter Eintrag {reltime(entries[0].get('at'))}"
            return "noch keine Einträge"
        if key == "system":
            stats = _stats(services)
            return f"DocuShelf {__version__} · {views.plural(int(stats.get('documents') or 0), 'Dokument', 'Dokumente')} im Index"
    except Exception:
        log.debug("cannot build the status of %s", key, exc_info=True)
    return ""


@router.get(BASE, response_class=HTMLResponse)
def overview_page(request: Request, user: User = Depends(require_user)) -> HTMLResponse:
    services = get_services(request)
    admin = is_admin(request, user)
    cards = [
        {"page": p, "status": _overview_status(request, services, p.key)}
        for p in sf.PAGES
        if p.key != "uebersicht" and (admin or not p.admin)
    ]
    return _render(
        request,
        user,
        "uebersicht",
        "settings/overview.html",
        {"stats": _stats(services), "counts": _counts(services), "cards": cards},
    )


# ---------------------------------------------------------------------------
# Saving a section


def _form_values(fields: tuple[sf.Field, ...], form: Any) -> dict[str, Any]:
    """What was typed, to show it again after an error (never secrets)."""
    values: dict[str, Any] = {}
    for f in fields:
        if f.kind == "secret":
            continue
        if f.kind == "bool":
            values[f.key] = form_value(form, f.key).lower() in ("1", "on", "true", "ja")
        elif f.kind == "sources":
            values[f.key] = [str(v) for v in form.getlist(f.key)] if hasattr(form, "getlist") else []
            values[f.key + "__extra"] = form_value(form, f.key + "__extra")
        else:
            raw = form.get(f.key)
            values[f.key] = raw if isinstance(raw, str) else ""
    return values


def _would_be_admin(request: Request, user: Any, new: Any) -> bool:
    if getattr(user, "method", None) == "local":
        return True
    if getattr(user, "method", None) != "oidc":
        return False
    names = {str(n).strip().casefold() for n in new.admin_users or ()}
    groups = {str(g).strip().casefold() for g in new.admin_groups or ()}
    if _multi(request):
        registry = security.get_registry(request)
        names |= set(getattr(registry, "admins", ()) or ())
        groups |= set(getattr(registry, "admin_groups", ()) or ())
    username = (user.username or "").strip().casefold()
    email = (user.email or "").strip().casefold()
    if (username and username in names) or (email and email in names):
        return True
    return any(str(g).strip().casefold() in groups for g in user.groups or ())


def _lockout_check(request: Request, user: Any) -> Callable[[Any], None]:
    """Refuse login settings with which nobody (or not the user making the change) could get in."""

    def check(new: Any) -> None:
        if not new.oidc_enabled and not new.local_login_enabled:
            raise ConfigError(
                "Ohne Anmeldedienst und ohne Notfallzugang könnte sich niemand mehr anmelden. Es wurde nichts geändert."
            )
        if getattr(user, "method", None) != "oidc":
            return
        if not new.oidc_enabled:
            raise ConfigError(
                "Du bist per SSO angemeldet – ohne Anmeldedienst kämst du nicht mehr hinein. "
                "Bitte das über den lokalen Notfallzugang ändern."
            )
        if not _multi(request) and not security.oidc_user_allowed(
            new, username=user.username, email=user.email, groups=user.groups or ()
        ):
            raise ConfigError("Mit diesen Regeln dürftest du selbst nicht mehr hinein. Es wurde nichts geändert.")
        if not _would_be_admin(request, user, new):
            raise ConfigError(
                "Damit würdest du dir selbst die Administratorrechte entziehen. Es wurde nichts geändert."
            )

    return check


_CHECKS: dict[str, Callable[[Request, Any], Callable[[Any], None]]] = {
    "oidc": _lockout_check,
    "zugang": _lockout_check,
    "sitzung": _lockout_check,
}

# Settings attributes behind the live keys (several keys of auto_file form one object).
_LIVE_ATTRS = {key: ("auto_file" if key.startswith("auto_file") else key) for key in (
    "auto_file", "auto_file_threshold", "auto_file_sources", "auto_file_new_folders", "auto_file_wait_for_llm",
    "auto_file_notify", "notify_url", "owner_names", "trash_retention_days", "keep_originals_days",
)}
_AUTO_KEYS = {
    "auto_file": "mode",
    "auto_file_threshold": "threshold",
    "auto_file_sources": "sources",
    "auto_file_new_folders": "new_folders",
    "auto_file_wait_for_llm": "wait_for_llm",
    "auto_file_notify": "notify",
}


def _containers(request: Request) -> list[Any]:
    registry = security.get_registry(request)
    created = getattr(registry, "created", None)
    if callable(created):
        return list(created())
    services = getattr(request.app.state, "services", None)
    return [services] if services is not None else []


def _apply_global_live(request: Request, new: Any, keys: list[str]) -> None:
    """Settings that apply at once: running containers, the registry and the app take them over."""
    attrs = sorted({_LIVE_ATTRS[key] for key in keys if key in _LIVE_ATTRS})
    if not attrs:
        return
    changes = {attr: getattr(new, attr) for attr in attrs}
    if _multi(request):
        # With several households only installation-wide values are global; the rest is per household.
        changes = {k: v for k, v in changes.items() if k in ("trash_retention_days", "keep_originals_days")}
        if not changes:
            return
    apply_live(_containers(request), changes)
    state = request.app.state
    if getattr(state, "settings", None) is not None:
        state.settings = state.settings.with_overrides(**changes)
    registry = security.get_registry(request)
    if registry is not None and hasattr(getattr(registry, "settings", None), "with_overrides"):
        try:
            registry.settings = registry.settings.with_overrides(**changes)
        except AttributeError:
            pass


def _save_household(request: Request, sec: sf.Section, parsed: dict[str, Any]) -> list[str]:
    """Write a household section to the household's entry of households.yaml; returns what changed."""
    services = get_services(request)
    household = services.household
    registry = security.get_registry(request)
    if sec.key == "automatisch":
        entry = {
            "mode": parsed.get("auto_file") or "off",
            "threshold": parsed.get("auto_file_threshold") if parsed.get("auto_file_threshold") is not None else 0.75,
            "sources": list(parsed.get("auto_file_sources") or []),
            "new_folders": bool(parsed.get("auto_file_new_folders")),
            "wait_for_llm": bool(parsed.get("auto_file_wait_for_llm")),
            "notify": bool(parsed.get("auto_file_notify")),
        }
        current = services.settings.auto_file
        before = {key: getattr(current, key) for key in entry}
        before["sources"] = list(before["sources"])
        if household.auto_file is not None and before == entry:
            return []
        registry.update_household(household.id, {"auto_file": entry})
        return ["auto_file"]
    if sec.key == "namen":
        names = parsed.get("owner_names") or None
        if list(household.owner_names or ()) == list(names or ()) and (household.owner_names is None) == (names is None):
            return []
        registry.update_household(household.id, {"owner_names": names})
        return ["owner_names"]
    if sec.key == "ziel":
        url = parsed.get("notify_url") or None
        if household.notify_url == url:
            return []
        registry.update_household(household.id, {"notify_url": url})
        return ["notify_url"]
    raise ConfigError("Dieser Abschnitt gehört nicht zu einem Haushalt.")


def _save_section(request: Request, form: Any, user: Any, page_key: str, section_key: str) -> Response:
    found = sf.page(page_key)
    sec = sf.section(page_key, section_key)
    if found is None or sec is None:
        return _error_page(request, user, 404, "Nicht gefunden", "Diesen Abschnitt der Einstellungen gibt es nicht.")
    if not is_admin(request, user):
        return _forbidden(request, user)
    multi = _multi(request)
    fields = sf.visible_fields(sec, multi=multi)
    household_scope = sec.scope == "household" and multi
    store = settings_store(request)
    typed = _form_values(fields, form)
    parsed: dict[str, Any] = {}
    changes: dict[str, Any] = {}
    secrets: dict[str, str | None] = {}
    try:
        if sec.confirm and form_value(form, "bestaetigt").lower() not in ("1", "on", "true", "ja"):
            raise ConfigError("Bitte bestätige zuerst, dass die Anmeldung danach noch funktioniert.")
        for f in fields:
            if not household_scope and store.env_source(f.key):
                continue
            if f.kind == "secret":
                if form_value(form, f.key + "__entfernen").lower() in ("1", "on", "true", "ja"):
                    secrets[f.key] = None
                else:
                    raw = form.get(f.key)
                    if isinstance(raw, str) and raw.strip():
                        secrets[f.key] = raw.strip()
                continue
            value = sf.parse_field(f, form)
            if f.check is not None and value is not None:
                f.check(value)
            parsed[f.key] = value
            changes[f.key] = sf.stored_value(f, value)
        if changes.get("llm_provider") == "anthropic" and "llm_url" in changes:
            # The Anthropic API has a fixed address: a stored one (e.g. left over from Ollama) is dropped.
            changes["llm_url"] = None
            parsed["llm_url"] = None
        if household_scope:
            changed = _save_household(request, sec, parsed)
            new = None
        else:
            check = _CHECKS.get(sec.key)
            new, changed = store.save(changes, secrets=secrets, check=check(request, user) if check else None)
    except DocuShelfError as exc:
        state = FormState(sec.key, typed, views.error_text(exc, "Die Einstellungen konnten nicht gespeichert werden."))
        return _render_page(request, user, page_key, form=state, status_code=400)

    if not changed:
        return redirect(f"{found.url}?{urlencode({'unveraendert': sec.key})}#{sec.key}")
    labels = [f.label for f in fields if f.key in changed]
    services = security.household_services(request)
    details: dict[str, Any] = {"summary": f"{found.label} › {sec.title}: {', '.join(labels)}", "keys": changed}
    if household_scope:
        details["household"] = getattr(getattr(services, "household", None), "id", None)
    _audit(services, user, AUDIT_ACTION, **details)
    if new is not None and sec.live:
        _apply_global_live(request, new, changed)
    # The confirmation is shown in the card itself: the page jumps to it, a message at the top would be out of view.
    return redirect(f"{found.url}?{urlencode({'gespeichert': sec.key})}#{sec.key}")


@router.post(BASE + "/speichern/{page_key}/{section_key}")
async def save_section(
    request: Request,
    page_key: str,
    section_key: str,
    user: User = Depends(require_session_user),
    _csrf: Any = Depends(verify_csrf),
) -> Response:
    return await _with_form(request, _save_section, user, page_key, section_key)


def _render_page(request: Request, user: Any, page_key: str, *, form: FormState | None = None, status_code: int = 200) -> Response:
    renderers: dict[str, Callable[..., Response]] = {
        "haushalte": _households_page,
        "ablage": _filing_page,
        "ki": _llm_page,
        "verarbeitung": _processing_page,
        "benachrichtigungen": _notify_page,
        "handy": _phone_page,
        "anmeldung": _login_page,
        "system": _system_page,
    }
    if page_key == "kanaele":
        from .routes_channels import render_channels

        return render_channels(request, user, settings_form=form, status_code=status_code)
    handler = renderers.get(page_key)
    if handler is None:
        return _error_page(request, user, 404, "Nicht gefunden", "Diese Seite gibt es nicht.")
    return handler(request, user, form=form, status_code=status_code)


def _admin_page(handler: Callable[..., Response]) -> Callable[..., Response]:
    """GET route for an admin-only subpage."""

    def endpoint(request: Request, user: User = Depends(require_session_user)) -> Response:
        if not is_admin(request, user):
            return _forbidden(request, user)
        return handler(request, user)

    endpoint.__name__ = "settings" + handler.__name__
    return endpoint


# ---------------------------------------------------------------------------
# Haushalte


def _households_page(request: Request, user: Any, *, form: FormState | None = None, status_code: int = 200) -> Response:
    registry = security.get_registry(request)
    services = get_services(request)
    multi = _multi(request)
    households = []
    root = Path(_running(request).root_dir)
    for h in getattr(registry, "households", []) or []:
        try:
            relative = Path(h.path).relative_to(root).as_posix()
        except ValueError:
            relative = str(h.path)
        households.append(
            {
                "id": h.id,
                "name": h.name,
                "path": str(h.path),
                "folder": "im Datenverzeichnis selbst" if relative in ("", ".") else f"Ordner {relative}",
                "users": list(h.users),
                "groups": list(h.groups),
                "access": h.access,
                "current": h.id == getattr(getattr(services, "household", None), "id", None),
            }
        )
    settings = _running(request)
    store = settings_store(request)
    context = {
        "households": households,
        "admins": list(getattr(registry, "admins", ()) or ()),
        "admin_groups": list(getattr(registry, "admin_groups", ()) or ()),
        "env_admins": list(settings.admin_users or ()),
        "env_admin_groups": list(settings.admin_groups or ()),
        "allowed_users": list(settings.allowed_users or ()),
        "allowed_groups": list(settings.allowed_groups or ()),
        "allow_all": bool(settings.allow_all_oidc_users),
        "households_file": str(Path(settings.root_dir) / "config" / "households.yaml"),
        "max_member_length": MAX_MEMBER_LENGTH,
        "settings_file_path": str(store.path),
    }
    return _render(request, user, "haushalte", "settings/households.html", context, form=form, status_code=status_code)


router.add_api_route(BASE + "/haushalte", _admin_page(_households_page), methods=["GET"], response_class=HTMLResponse)


def _member_change(request: Request, form: Any, user: Any, household_id: str, change: str, field_name: str) -> Response:
    """Add or remove a user or group of a household (admins only, written to households.yaml)."""
    back = f"{BASE}/haushalte#haushalt-{household_id}"
    if not _multi(request):
        return _error_page(request, user, 404, "Nicht gefunden", "Haushalte gibt es in dieser Installation nicht.")
    if not security.is_household_admin(request, user):
        return _error_page(
            request, user, 403, "Keine Berechtigung", "Haushaltsmitglieder können nur Administratoren ändern."
        )
    registry = security.get_registry(request)
    name = form_value(form, "user" if field_name == "users" else "group")
    try:
        if change == "add":
            household = registry.change_members(household_id, add=name, field=field_name)
        else:
            household = registry.change_members(household_id, remove=name, field=field_name)
    except DocuShelfError as exc:
        flash(request, views.error_text(exc, "Die Mitglieder konnten nicht geändert werden."), "error")
        return redirect(back)
    try:
        services = registry.services(household.id)
    except Exception:
        log.exception("cannot open household %s for the audit log", household.id)
        services = None
    what = "Gruppe " if field_name == "groups" else ""
    key = "added" if change == "add" else "removed"
    details = {key: name, "household": household.id}
    if field_name == "groups":
        details["field"] = "groups"
    _audit(services, user, "household_members", **details)
    if change == "add":
        flash(request, f"{what}„{name}“ gehört jetzt zum Haushalt „{household.name}“.")
    else:
        flash(request, f"{what}„{name}“ wurde aus dem Haushalt „{household.name}“ entfernt.")
    return redirect(back)


def _member_route(path: str, change: str, field_name: str) -> None:
    async def endpoint(
        request: Request, household_id: str, user: User = Depends(require_session_user), _csrf: Any = Depends(verify_csrf)
    ) -> Response:
        return await _with_form(request, _member_change, user, household_id, change, field_name)

    endpoint.__name__ = f"household_{field_name}_{change}"
    router.add_api_route(BASE + "/haushalte/{household_id}/" + path, endpoint, methods=["POST"])


_member_route("mitglieder", "add", "users")
_member_route("mitglieder/entfernen", "remove", "users")
_member_route("gruppen", "add", "groups")
_member_route("gruppen/entfernen", "remove", "groups")


def _rename_household(request: Request, form: Any, user: Any, household_id: str) -> Response:
    back = f"{BASE}/haushalte#haushalt-{household_id}"
    if not _multi(request):
        return _error_page(request, user, 404, "Nicht gefunden", "Haushalte gibt es in dieser Installation nicht.")
    if not security.is_household_admin(request, user):
        return _forbidden(request, user)
    name = " ".join(form_value(form, "name").split())
    registry = security.get_registry(request)
    try:
        if not name or len(name) > 80:
            raise ConfigError("Bitte einen Namen mit höchstens 80 Zeichen angeben.")
        old = registry.get(household_id).name
        if old == name:
            flash(request, "Der Name ist unverändert.", "info")
            return redirect(back)
        household = registry.update_household(household_id, {"name": name})
    except DocuShelfError as exc:
        flash(request, views.error_text(exc, "Der Haushalt konnte nicht umbenannt werden."), "error")
        return redirect(back)
    try:
        services = registry.services(household.id)
    except Exception:
        services = None
    _audit(services, user, AUDIT_ACTION, summary=f"Haushalt umbenannt: „{old}“ → „{name}“", keys=["name"], household=household.id)
    flash(request, f"Der Haushalt heißt jetzt „{name}“.")
    return redirect(back)


@router.post(BASE + "/haushalte/{household_id}/name")
async def household_rename(
    request: Request, household_id: str, user: User = Depends(require_session_user), _csrf: Any = Depends(verify_csrf)
) -> Response:
    return await _with_form(request, _rename_household, user, household_id)


def _admin_change(request: Request, form: Any, user: Any, change: str) -> Response:
    back = f"{BASE}/haushalte#administratoren"
    if not _multi(request):
        return _error_page(request, user, 404, "Nicht gefunden", "Haushalte gibt es in dieser Installation nicht.")
    if not security.is_household_admin(request, user):
        return _forbidden(request, user)
    kind = "admin_groups" if form_value(form, "art") == "gruppe" else "admins"
    name = form_value(form, "name")
    registry = security.get_registry(request)
    try:
        if change == "add":
            registry.change_admins(add=name, field=kind, keep=user)
        else:
            registry.change_admins(remove=name, field=kind, keep=user)
    except DocuShelfError as exc:
        flash(request, views.error_text(exc, "Die Administratoren konnten nicht geändert werden."), "error")
        return redirect(back)
    label = "Administratorgruppe" if kind == "admin_groups" else "Administrator"
    verb = "hinzugefügt" if change == "add" else "entfernt"
    _audit(get_services(request), user, AUDIT_ACTION, summary=f"{label} „{name}“ {verb}", keys=[kind])
    flash(request, f"{label} „{name}“ {verb}. Das gilt ab sofort.")
    return redirect(back)


@router.post(BASE + "/haushalte/administratoren")
async def household_admin_add(request: Request, user: User = Depends(require_session_user), _csrf: Any = Depends(verify_csrf)) -> Response:
    return await _with_form(request, _admin_change, user, "add")


@router.post(BASE + "/haushalte/administratoren/entfernen")
async def household_admin_remove(request: Request, user: User = Depends(require_session_user), _csrf: Any = Depends(verify_csrf)) -> Response:
    return await _with_form(request, _admin_change, user, "remove")


# ---------------------------------------------------------------------------
# Ablage, KI, Verarbeitung, Benachrichtigungen, Anmeldung


def _filing_page(request: Request, user: Any, *, form: FormState | None = None, status_code: int = 200) -> Response:
    services = get_services(request)
    from .routes_ui import _auto_overview

    context = {
        "auto": _auto_overview(services),
        "llm": views.llm_info(services.settings),
        "current_household": services.household if _multi(request) else None,
    }
    return _render(request, user, "ablage", "settings/filing.html", context, form=form, status_code=status_code)


router.add_api_route(BASE + "/ablage", _admin_page(_filing_page), methods=["GET"], response_class=HTMLResponse)


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


def _probe_config(request: Request) -> tuple[Any, bool]:
    """The model to test: the saved one while it waits for a restart, else the running one."""
    services = get_services(request)
    running = getattr(services.settings, "llm", None)
    store = settings_store(request)
    saved, _values, _error = _saved(request, store)
    llm_titles = {sf.LLM_SECTION.title, sf.LLM_LIMITS_SECTION.title}
    if saved is not None and saved.llm is not None and llm_titles & set(pending_restart(request, store, saved)):
        return saved.llm, True
    return running, False


def _llm_page(request: Request, user: Any, *, form: FormState | None = None, status_code: int = 200) -> Response:
    services = get_services(request)
    probe_running, probe = _llm_probe(request).snapshot()
    config, saved = _probe_config(request)
    context = {
        "llm": views.llm_info(services.settings),
        "probe": probe,
        "probe_answer": views.probe_answer((probe or {}).get("answer")),
        "probe_running": probe_running,
        "probe_target": views.llm_info(SimpleSettings(config)) if saved else None,
        "can_probe": config is not None,
    }
    return _render(request, user, "ki", "settings/llm.html", context, form=form, status_code=status_code)


@dataclass
class SimpleSettings:
    llm: Any


router.add_api_route(BASE + "/ki", _admin_page(_llm_page), methods=["GET"], response_class=HTMLResponse)


@router.post(BASE + "/sprachmodell/testen")
async def settings_llm_test(request: Request, user: User = Depends(require_session_user), _csrf: Any = Depends(verify_csrf)) -> Response:
    return await _with_form(request, _test_llm, user)


def _test_llm(request: Request, form: Any, user: Any) -> Response:
    """Classify a built-in sample letter with the configured model and show answer and duration."""
    if not is_admin(request, user):
        return _forbidden(request, user)
    config, _saved_config = _probe_config(request)
    back = f"{BASE}/ki#status"
    if config is None:
        flash(request, "Das Sprachmodell ist ausgeschaltet – es gibt nichts zu testen.", "info")
        return redirect(back)
    try:
        configured = float(getattr(config, "timeout", LLM_PROBE_WAIT) or LLM_PROBE_WAIT)
    except (TypeError, ValueError):
        configured = LLM_PROBE_WAIT
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


def _processing_page(request: Request, user: Any, *, form: FormState | None = None, status_code: int = 200) -> Response:
    services = get_services(request)
    from .routes_ui import _ocr_info, _status_of

    context = {
        "ocr": _ocr_info(services.settings),
        "worker": _status_of(getattr(services, "worker", None)),
        "settings": services.settings,
    }
    return _render(request, user, "verarbeitung", "settings/processing.html", context, form=form, status_code=status_code)


router.add_api_route(BASE + "/verarbeitung", _admin_page(_processing_page), methods=["GET"], response_class=HTMLResponse)


def _notify_page(request: Request, user: Any, *, form: FormState | None = None, status_code: int = 200) -> Response:
    services = get_services(request)
    context = {
        "notify_url": getattr(services.settings, "notify_url", None),
        "notify_host": views.endpoint_host(getattr(services.settings, "notify_url", None)),
        "auto": views.auto_settings(services.settings),
        "current_household": services.household if _multi(request) else None,
    }
    return _render(request, user, "benachrichtigungen", "settings/notify.html", context, form=form, status_code=status_code)


router.add_api_route(BASE + "/benachrichtigungen", _admin_page(_notify_page), methods=["GET"], response_class=HTMLResponse)


def _test_notify(request: Request, form: Any, user: Any) -> Response:
    if not is_admin(request, user):
        return _forbidden(request, user)
    back = f"{BASE}/benachrichtigungen#ziel"
    services = get_services(request)
    notifier = getattr(services, "notifier", None)
    if notifier is None or not getattr(notifier, "url", None):
        flash(request, "Es ist keine Benachrichtigungs-Adresse eingerichtet.", "info")
        return redirect(back)
    household = getattr(getattr(services, "household", None), "name", None)
    message = "Wenn du das liest, kommen Benachrichtigungen von DocuShelf an." + (f" (Haushalt „{household}“)" if household else "")
    error = notifier.send_now("DocuShelf: Test", message)
    if error:
        flash(request, f"Die Test-Benachrichtigung ging nicht durch: {error}", "error")
    else:
        flash(request, f"Test-Benachrichtigung an {views.endpoint_host(notifier.url)} geschickt.")
    return redirect(back)


@router.post(BASE + "/benachrichtigungen/testen")
async def settings_notify_test(request: Request, user: User = Depends(require_session_user), _csrf: Any = Depends(verify_csrf)) -> Response:
    return await _with_form(request, _test_notify, user)


def _login_page(request: Request, user: Any, *, form: FormState | None = None, status_code: int = 200) -> Response:
    settings = _running(request)
    store = settings_store(request)
    context = {
        "running": settings,
        "local_user": settings.admin_user,
        "local_enabled": settings.local_login_enabled,
        "local_locked": store.env_source("admin_password_hash") or store.env_source("admin_password"),
        "cookies_secure": settings.cookies_secure,
        "secret_key_file": str(Path(settings.root_dir) / "config" / "secret.key"),
        "redirect_uri": f"{settings.base_url}/auth/callback",
        "you": user,
    }
    return _render(request, user, "anmeldung", "settings/login.html", context, form=form, status_code=status_code)


router.add_api_route(BASE + "/anmeldung", _admin_page(_login_page), methods=["GET"], response_class=HTMLResponse)


# ---------------------------------------------------------------------------
# Handy & Tokens (everyone)


def _phone_page(request: Request, user: Any, *, form: FormState | None = None, status_code: int = 200) -> Response:
    services = get_services(request)
    tokens_store = getattr(services, "tokens", None)
    tokens: list[dict[str, Any]] = []
    if tokens_store is not None:
        try:
            tokens = [dict(t) for t in tokens_store.list()]
        except Exception:
            log.exception("cannot list API tokens")
    for token in tokens:
        token.pop("hash", None)
    new_token = None
    session = request.scope.get("session")
    if isinstance(session, dict) and NEW_TOKEN_KEY in session:
        new_token = views.one_time.take(session.pop(NEW_TOKEN_KEY, None))
    context = {
        "tokens": tokens,
        "tokens_available": tokens_store is not None,
        "new_token": new_token,
        "current_household": services.household if _multi(request) else None,
        "settings": services.settings,
    }
    return _render(request, user, "handy", "settings/phone.html", context, form=form, status_code=status_code)


@router.get(BASE + "/handy", response_class=HTMLResponse)
def phone_page(request: Request, user: User = Depends(require_user)) -> Response:
    return _phone_page(request, user)


@router.post(BASE + "/tokens")
async def settings_token_create(
    request: Request, user: User = Depends(require_session_user), _csrf: Any = Depends(verify_csrf)
) -> Response:
    return await _with_form(request, _create_token, user)


TOKENS = BASE + "/handy#tokens"


def _create_token(request: Request, form: Any, user: Any) -> Response:
    services = get_services(request)
    store = getattr(services, "tokens", None)
    if store is None:
        flash(request, "API-Tokens sind nicht verfügbar.", "error")
        return redirect(TOKENS)
    name = " ".join(form_value(form, "name").split())[:80]
    if not name:
        flash(request, "Bitte einen Namen für den Token angeben (z. B. „iPhone Kurzbefehl“).", "error")
        return redirect(TOKENS)
    try:
        # Tied to the account: the token stops working once the owner is no longer allowed in.
        token, info = store.create(name, getattr(user, "username", None), owner=user)
    except DocuShelfError as exc:
        flash(request, views.error_text(exc, "Der Token konnte nicht erstellt werden."), "error")
        return redirect(TOKENS)
    info = info if isinstance(info, Mapping) else {}
    key = views.one_time.put({"token": token, "name": info.get("name", name)})
    request.session[NEW_TOKEN_KEY] = key
    _audit(services, user, "token_create", token=info.get("id"), name=info.get("name", name))
    flash(request, f"Token „{name}“ erstellt. Er wird nur dieses eine Mal angezeigt.")
    return redirect(TOKENS)


@router.post(BASE + "/tokens/{token_id}/widerrufen")
async def settings_token_revoke(
    request: Request, token_id: str, user: User = Depends(require_session_user), _csrf: Any = Depends(verify_csrf)
) -> Response:
    return await _with_form(request, _revoke_token, user, token_id)


def _revoke_token(request: Request, form: Any, user: Any, token_id: str) -> Response:
    services = get_services(request)
    store = getattr(services, "tokens", None)
    if store is None:
        flash(request, "API-Tokens sind nicht verfügbar.", "error")
        return redirect(TOKENS)
    name = form_value(form, "name") or token_id
    try:
        result = store.revoke(token_id)
    except DocuShelfError as exc:
        flash(request, views.error_text(exc, "Der Token konnte nicht widerrufen werden."), "error")
        return redirect(TOKENS)
    if result is False:
        flash(request, "Diesen Token gibt es nicht mehr.", "warning")
        return redirect(TOKENS)
    if isinstance(result, Mapping) and result.get("name"):
        name = str(result["name"])
    _audit(services, user, "token_revoke", token=token_id, name=name)
    flash(request, f"Token „{name}“ widerrufen. Skripte mit diesem Token haben keinen Zugriff mehr.")
    return redirect(TOKENS)


# ---------------------------------------------------------------------------
# Protokoll (everyone: the audit log of the own household)


def _audit_page(request: Request, user: Any) -> Response:
    services = get_services(request)
    params = request.query_params
    action = (params.get("aktion") or "").strip()
    who = (params.get("wer") or "").strip()
    text = (params.get("suche") or "").strip()
    period = params.get("zeitraum") or "30"
    if period not in {key for key, _ in AUDIT_PERIODS}:
        period = "30"
    try:
        limit = max(AUDIT_PAGE_SIZE, min(int(params.get("anzahl") or AUDIT_PAGE_SIZE), AUDIT_MAX))
    except ValueError:
        limit = AUDIT_PAGE_SIZE
    since = now() - timedelta(days=int(period)) if period != "0" else None
    actions = [key for key, label in views.AUDIT_LABELS.items() if key == action or label == action] if action else None
    rows: list[dict[str, Any]] = []
    more = False
    error = None
    audit = getattr(services, "audit", None)
    try:
        entries = audit.entries(actions, since) if audit is not None else []
    except Exception:
        log.exception("cannot read the audit log")
        entries, error = [], "Das Protokoll konnte nicht gelesen werden."
    needle = text.casefold()
    who_key = who.casefold()
    for entry in entries:
        row = views.audit_entry(entry)
        if who_key and who_key not in str(row["user"]).casefold():
            continue
        if needle and needle not in f"{row['label']} {row['details']}".casefold():
            continue
        if len(rows) >= limit:
            more = True
            break
        rows.append(row)
    labels = sorted({label for label in views.AUDIT_LABELS.values()})
    query = {"aktion": action, "wer": who, "suche": text, "zeitraum": period}
    more_url = f"{BASE}/protokoll?" + urlencode({**{k: v for k, v in query.items() if v}, "anzahl": min(limit * 2, AUDIT_MAX)})
    context = {
        "rows": rows,
        "more": more and limit < AUDIT_MAX,
        "more_url": more_url,
        "filters": query,
        "filtered": bool(action or who or text),
        "action_labels": labels,
        "periods": AUDIT_PERIODS,
        "error": error,
        "current_household": services.household if _multi(request) else None,
    }
    return _render(request, user, "protokoll", "settings/audit.html", context)


@router.get(BASE + "/protokoll", response_class=HTMLResponse)
def audit_page(request: Request, user: User = Depends(require_user)) -> Response:
    return _audit_page(request, user)


# ---------------------------------------------------------------------------
# System (index for everyone; installation, general settings and restart for admins)


def _versions() -> list[dict[str, str]]:
    rows = [{"label": "DocuShelf", "value": __version__}, {"label": "Python", "value": platform.python_version()}]
    try:
        from ..pdf import ocr

        version = ocr.ocr_version() if ocr.ocr_available() else None
        rows.append({"label": "OCRmyPDF", "value": version or "nicht installiert"})
    except Exception:
        log.debug("cannot determine the OCR version", exc_info=True)
    return rows


def _system_page(request: Request, user: Any, *, form: FormState | None = None, status_code: int = 200) -> Response:
    services = get_services(request)
    from .routes_ui import _status_of

    settings = _running(request)
    admin = is_admin(request, user)
    store = settings_store(request)
    paths = []
    if admin:
        root = Path(settings.root_dir)
        paths = [
            {"label": "Datenverzeichnis", "value": str(root)},
            {"label": "Archiv dieses Haushalts", "value": str(services.settings.archive_dir)},
            {"label": "Einstellungen", "value": str(store.path)},
            {"label": "Geheimnisse", "value": str(store.secrets_dir)},
        ]
        if _multi(request):
            paths.append({"label": "Haushalte", "value": str(root / "config" / "households.yaml")})
    context = {
        "stats": _stats(services),
        "counts": _counts(services),
        "watcher": _status_of(getattr(services, "watcher", None)),
        "settings": services.settings,
        "versions": _versions() if admin else [],
        "paths": paths,
        "households_count": len(getattr(security.get_registry(request), "households", []) or []),
    }
    return _render(request, user, "system", "settings/system.html", context, form=form, status_code=status_code)


@router.get(BASE + "/system", response_class=HTMLResponse)
def system_page(request: Request, user: User = Depends(require_user)) -> Response:
    return _system_page(request, user)


SYSTEM = BASE + "/system"


@router.post(BASE + "/abgleich")
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
        return redirect(SYSTEM)
    _audit(services, user, "reconcile")
    return redirect(SYSTEM)


@router.post(BASE + "/index-neu-aufbauen")
async def settings_rebuild(request: Request, user: User = Depends(require_user), _csrf: Any = Depends(verify_csrf)) -> Response:
    return await _with_form(request, _rebuild, user)


def _rebuild(request: Request, form: Any, user: Any) -> Response:
    services = get_services(request)
    try:
        stats = services.rebuild(user=getattr(user, "username", None))
    except DocuShelfError as exc:
        flash(request, views.error_text(exc, "Der Index konnte nicht neu aufgebaut werden."), "error")
        return redirect(SYSTEM)
    count = views.plural(int(getattr(stats, "added", 0) or 0), "Dokument", "Dokumente")
    seconds = (getattr(stats, "duration_ms", 0) or 0) / 1000
    duration = f" in {seconds:.1f} s".replace(".", ",") if seconds >= 0.1 else ""
    flash(request, f"Index neu aufgebaut: {count}{duration}.")
    return redirect(SYSTEM)


def _restart(request: Request, form: Any, user: Any) -> Response:
    if not is_admin(request, user):
        return _forbidden(request, user)
    handler = _restart_handler(request)
    if handler is None:
        flash(
            request,
            "DocuShelf kann sich hier nicht selbst neu starten (es läuft nicht über „docushelf serve“). "
            "Bitte den Dienst bzw. Container von Hand neu starten.",
            "warning",
        )
        return redirect(SYSTEM + "#neustart")
    pending = pending_restart(request)
    _audit(
        security.household_services(request),
        user,
        "restart",
        summary=("für: " + ", ".join(pending)) if pending else "ohne ausstehende Änderungen",
    )
    log.info("restart requested in the web interface by %s", getattr(user, "username", None))
    timer = threading.Timer(RESTART_DELAY, handler)
    timer.daemon = True
    timer.start()
    back = form_value(form, "zurueck")
    target = back if views.is_local_path(back) and back.startswith(BASE) else BASE
    return render(request, "settings/restarting.html", {"user": user, "target": target, "pending": pending})


@router.post(BASE + "/neustart")
async def settings_restart(request: Request, user: User = Depends(require_session_user), _csrf: Any = Depends(verify_csrf)) -> Response:
    return await _with_form(request, _restart, user)


# Template helpers
environment.globals.setdefault("settings_env_name", env_name)


__all__ = ["layout_context", "pending_restart", "router", "section_views", "settings_store"]
