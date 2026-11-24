"""The settings area: overview, subpages, saving, locks, secrets, restart and access.

One household with SSO (the provider set through the environment, so its
fields are locked) and the local emergency account as admin; the household
tests at the end use households.yaml with two households.
"""

from __future__ import annotations

import re
import stat
import time
from types import SimpleNamespace

import httpx
import pytest
import respx
import yaml

from docushelf.config import load_settings
from docushelf.households import Registry
from docushelf.settings_file import SettingsFile
from docushelf.web import routes_settings, routes_ui
from docushelf.web.app import create_app
from test_ui import assert_clean_page, csrf_of, flash_texts
from web_support import FakeProvider, client_for, login_local, oidc_login, session_data

ADMIN_PAGES = (
    "/einstellungen/haushalte",
    "/einstellungen/kanaele",
    "/einstellungen/ablage",
    "/einstellungen/ki",
    "/einstellungen/verarbeitung",
    "/einstellungen/benachrichtigungen",
    "/einstellungen/anmeldung",
)
MEMBER_PAGES = ("/einstellungen", "/einstellungen/handy", "/einstellungen/protokoll", "/einstellungen/system")


def _ocr(monkeypatch):
    monkeypatch.setattr(routes_ui, "_ocr_info", lambda s: routes_ui._OcrInfo(True, True, "16.0.0", ["deu", "eng"], []))


@pytest.fixture
def site(env, monkeypatch):
    provider = FakeProvider()
    env.update(
        {
            "DOCUSHELF_OIDC_ISSUER": provider.issuer,
            "DOCUSHELF_OIDC_CLIENT_ID": provider.client_id,
            "DOCUSHELF_OIDC_CLIENT_SECRET": provider.client_secret,
            "DOCUSHELF_ALLOWED_GROUPS": "familie",
        }
    )
    _ocr(monkeypatch)
    settings = load_settings(env)
    app = create_app(settings)
    app.state.settings_env = env
    data = SimpleNamespace(
        app=app, env=env, provider=provider, services=app.state.services, store=SettingsFile(settings.root_dir, env=env)
    )
    yield data
    app.state.registry.close()


def admin(site):
    client = client_for(site.app)
    assert login_local(client).status_code == 303
    return client


def sso(site, name="anna", groups=("familie",)):
    client = client_for(site.app)
    site.provider.claims.update(
        {"sub": f"user-{name}", "preferred_username": name, "email": f"{name}@example.test", "groups": list(groups)}
    )
    with site.provider.running():
        response = oidc_login(client, site.provider)
    assert response.status_code == 303 and "error=" not in response.headers["location"], response.headers["location"]
    return client


def csrf(client) -> str:
    return session_data(client)["csrf"]


def save(client, page_key, section, data, *, confirm=False):
    payload = {"csrf_token": csrf(client), **data}
    if confirm:
        payload["bestaetigt"] = "1"
    return client.post(f"/einstellungen/speichern/{page_key}/{section}", data=payload, follow_redirects=False)


def card(html: str, key: str) -> str:
    found = re.search(rf'<section class="card settings-card" id="{key}".*?</section>', html, re.S)
    assert found, key
    return found.group(0)


def file_values(site) -> dict:
    return yaml.safe_load(site.store.path.read_text(encoding="utf-8")) or {}


PROCESSING = {
    "ocr_enabled": "1",
    "ocr_languages": "deu+eng",
    "ocr_output_type": "pdfa-2",
    "ocr_jobs": "2",
    "ocr_parallel": "1",
    "ocr_timeout": "900",
    "ocr_extra_args": "",
}


# ---------------------------------------------------------------------------
# Rendering and access


def test_every_subpage_renders_for_an_admin(site):
    client = admin(site)
    for url in MEMBER_PAGES + ADMIN_PAGES:
        response = client.get(url)
        assert response.status_code == 200, url
        assert response.headers["cache-control"] == "no-store"
        assert_clean_page(response.text, csrf=csrf_of(response.text))
        assert 'class="settings-nav"' in response.text and 'aria-current="page"' in response.text
    overview = client.get("/einstellungen").text
    for url in ADMIN_PAGES:
        assert f'href="{url}"' in overview, url
    assert "Ein Haushalt: „Zuhause“" in overview and "Texterkennung an (deu+eng)" in overview


def test_members_see_their_own_pages_only(site):
    client = sso(site)
    for url in MEMBER_PAGES:
        response = client.get(url)
        assert response.status_code == 200, url
        for admin_url in ADMIN_PAGES:
            assert f'href="{admin_url}"' not in response.text, (url, admin_url)
    for url in ADMIN_PAGES:
        response = client.get(url)
        assert response.status_code == 403 and "Keine Berechtigung" in response.text, url
    system = client.get("/einstellungen/system").text
    assert "Jetzt abgleichen" in system and "Installation" not in system and "/einstellungen/neustart" not in system
    assert "API-Tokens" in client.get("/einstellungen/handy").text
    # Saving and restarting are refused as well; nothing is written.
    assert save(client, "verarbeitung", "texterkennung", PROCESSING).status_code == 403
    response = client.post("/einstellungen/neustart", data={"csrf_token": csrf(client)}, follow_redirects=False)
    assert response.status_code == 403
    assert not site.store.path.exists()


def test_admins_from_the_settings_see_everything(site):
    site.store.save({"admin_users": ["anna"]})
    site.app.state.settings = site.app.state.settings.with_overrides(admin_users=("anna",))
    client = sso(site)
    for url in ADMIN_PAGES:
        assert client.get(url).status_code == 200, url


def test_a_post_without_csrf_token_is_refused(site):
    client = admin(site)
    response = client.post("/einstellungen/speichern/verarbeitung/texterkennung", data=PROCESSING, follow_redirects=False)
    assert response.status_code == 403
    assert not site.store.path.exists()


def test_unknown_sections_are_404(site):
    client = admin(site)
    assert save(client, "verarbeitung", "gibtsnicht", {}).status_code == 404
    assert save(client, "gibtsnicht", "x", {}).status_code == 404


# ---------------------------------------------------------------------------
# Saving


def test_saving_a_section_that_needs_a_restart(site):
    client = admin(site)
    page = client.get("/einstellungen/verarbeitung").text
    assert "Neustart nötig" not in page
    section = card(page, "texterkennung")
    assert 'name="ocr_jobs" value="2"' in section and "nach Neustart" in section

    response = save(client, "verarbeitung", "texterkennung", {**PROCESSING, "ocr_jobs": "4", "ocr_languages": "deu+fra"})
    assert response.status_code == 303
    assert response.headers["location"] == "/einstellungen/verarbeitung?gespeichert=texterkennung#texterkennung"
    assert file_values(site) == {"ocr_jobs": 4, "ocr_languages": "deu+fra"}
    # The running server keeps its values until the restart.
    assert site.services.settings.ocr_jobs == 2

    page = client.get(response.headers["location"]).text
    assert "Neustart nötig, damit die Änderung an „Texterkennung (OCR)“ wirkt." in page
    assert "Jetzt neu starten" not in page  # no restart handler outside "docushelf serve"
    assert "Die Änderung wirkt nach einem Neustart" in card(page, "texterkennung")
    assert 'name="ocr_jobs" value="4"' in page
    overview = client.get("/einstellungen").text
    assert "Neustart nötig" in overview

    entry = site.services.audit.tail(1)[0]
    assert entry["action"] == "settings_change" and entry["user"] == "admin"
    assert entry["keys"] == ["ocr_jobs", "ocr_languages"] and "Verarbeitung › Texterkennung (OCR)" in entry["summary"]
    assert "Einstellungen geändert" in client.get("/einstellungen/protokoll").text

    # A second save keeps the previous file as .bak; saving the same again changes nothing.
    first = site.store.path.read_text(encoding="utf-8")
    assert save(client, "verarbeitung", "texterkennung", {**PROCESSING, "ocr_jobs": "6"}).status_code == 303
    assert site.store.backup_path.read_text(encoding="utf-8") == first
    response = save(client, "verarbeitung", "texterkennung", {**PROCESSING, "ocr_jobs": "6"})
    assert "unveraendert=texterkennung" in response.headers["location"]
    assert "es hat sich nichts geändert" in client.get(response.headers["location"]).text
    # Back to the defaults: the entries disappear from the file again.
    save(client, "verarbeitung", "texterkennung", PROCESSING)
    assert file_values(site) == {}
    assert "Neustart nötig" not in client.get("/einstellungen/verarbeitung").text


def test_invalid_input_comes_back_with_the_typed_values(site):
    client = admin(site)
    response = save(client, "verarbeitung", "texterkennung", {**PROCESSING, "ocr_jobs": "viele", "ocr_languages": "deu+fra"})
    assert response.status_code == 400
    section = card(response.text, "texterkennung")
    assert "Kerne pro Lauf: bitte eine Zahl eingeben." in section and 'value="viele"' in section
    assert 'value="deu+fra"' in section and "data-settings-message" in section
    assert_clean_page(response.text, csrf=csrf_of(response.text))
    for bad, message in (
        ({"ocr_jobs": "0"}, "mindestens 1"),
        ({"ocr_languages": "deutsch; rm"}, "Tesseract-Kürzel"),
        ({"ocr_output_type": "docx"}, "angebotenen Möglichkeiten"),
        ({"ocr_extra_args": '--clean "offen'}, "Anführungszeichen"),
    ):
        response = save(client, "verarbeitung", "texterkennung", {**PROCESSING, **bad})
        assert response.status_code == 400 and message in response.text, bad
    response = save(client, "system", "allgemein", {"timezone": "Mars/Olympus", "log_level": "info"})
    assert response.status_code == 400 and "keine bekannte Zeitzone" in response.text
    response = save(client, "anmeldung", "sitzung", {"base_url": "http://testserver", "session_max_age": "14",
                                                     "trusted_proxies": "irgendwo"}, confirm=True)
    assert response.status_code == 400 and "keine IP-Adresse" in response.text
    assert not site.store.path.exists()


def test_the_loader_of_the_server_checks_every_save(site):
    client = admin(site)
    response = save(client, "ki", "sprachmodell", {"llm_enabled": "1", "llm_provider": "openai", "llm_url": "", "llm_model": ""})
    assert response.status_code == 400 and "LLM_URL fehlt" in response.text
    assert not site.store.path.exists()


def test_settings_from_the_environment_are_locked(site, env):
    env["DOCUSHELF_OCR_LANGUAGES"] = "deu"
    site.app.state.settings_env = env
    client = admin(site)
    page = client.get("/einstellungen/verarbeitung").text
    row = re.search(r'<div class="form-row form-row-text is-locked">.*?</div>\s*</div>', page, re.S).group(0)
    assert 'name="ocr_languages" value="deu"' in row and " disabled" in row
    assert "festgelegt über Umgebungsvariable" in row and "DOCUSHELF_OCR_LANGUAGES" in row
    # The OIDC provider comes from the environment as well.
    login = client.get("/einstellungen/anmeldung").text
    assert "DOCUSHELF_OIDC_ISSUER" in card(login, "oidc") and "DOCUSHELF_OIDC_CLIENT_SECRET" in card(login, "oidc")
    # A locked key is never written, whatever the form says.
    assert save(client, "verarbeitung", "texterkennung", {**PROCESSING, "ocr_languages": "eng", "ocr_jobs": "3"}).status_code == 303
    assert file_values(site) == {"ocr_jobs": 3}


def test_secrets_are_stored_apart_and_never_shown(site, data_dir):
    client = admin(site)
    response = save(
        client, "ki", "sprachmodell",
        {"llm_enabled": "1", "llm_provider": "openai", "llm_url": "http://llama:8080/v1", "llm_model": "m", "llm_api_key": "sk-sehr-geheim-42"},
    )
    assert response.status_code == 303
    secret = data_dir / "config" / "secrets" / "llm-api-key"
    assert secret.read_text(encoding="utf-8").strip() == "sk-sehr-geheim-42"
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert file_values(site)["llm_api_key_file"] == "secrets/llm-api-key"
    for url in MEMBER_PAGES + ADMIN_PAGES:
        assert "sk-sehr-geheim-42" not in client.get(url).text, url
    section = card(client.get("/einstellungen/ki").text, "sprachmodell")
    assert "gesetzt" in section and "Gespeicherten Wert entfernen" in section
    assert "sk-sehr-geheim-42" not in site.store.path.read_text(encoding="utf-8")
    assert all("sk-sehr-geheim-42" not in str(e) for e in site.services.audit.tail(20))

    # Saving without a new value keeps it; an error page does not echo a typed secret.
    keep = {"llm_enabled": "1", "llm_provider": "openai", "llm_url": "http://llama:8080/v1", "llm_model": "m2"}
    assert save(client, "ki", "sprachmodell", keep).status_code == 303 and secret.exists()
    response = save(client, "ki", "sprachmodell", {**keep, "llm_url": "kaputt", "llm_api_key": "sk-neu-99"})
    assert response.status_code == 400 and "sk-neu-99" not in response.text
    assert secret.read_text(encoding="utf-8").strip() == "sk-sehr-geheim-42"
    # Removing deletes the file.
    assert save(client, "ki", "sprachmodell", {**keep, "llm_api_key__entfernen": "1"}).status_code == 303
    assert not secret.exists() and "llm_api_key_file" not in file_values(site)


def test_live_settings_apply_at_once(site):
    client = admin(site)
    data = {
        "auto_file": "confident",
        "auto_file_threshold": "80",
        "auto_file_sources": ["upload", "smb", "watch-*"],
        "auto_file_sources__extra": "paperless",
        "auto_file_new_folders": "1",
        "auto_file_notify": "1",
    }
    response = save(client, "ablage", "automatisch", data)
    assert response.status_code == 303
    config = site.services.settings.auto_file
    assert config.mode == "confident" and config.threshold == 0.8 and not config.wait_for_llm
    assert config.sources == ("upload", "smb", "watch-*", "paperless")
    assert site.services.channels.base_settings.auto_file == config
    assert site.app.state.settings.auto_file == config
    stored = file_values(site)
    assert stored["auto_file"] == "confident" and stored["auto_file_threshold"] == 0.8
    assert stored["auto_file_wait_for_llm"] is False and "auto_file_new_folders" not in stored  # default
    page = client.get(response.headers["location"]).text
    assert "Neustart nötig" not in page and "Die Änderung gilt ab sofort" in card(page, "automatisch")
    assert 'value="confident" checked' in page and 'value="paperless"' in page

    assert save(client, "benachrichtigungen", "ziel", {"notify_url": "https://ntfy.example.test/docushelf"}).status_code == 303
    assert site.services.notifier.url == "https://ntfy.example.test/docushelf"
    assert save(client, "ablage", "aufbewahrung", {"trash_retention_days": "60", "keep_originals_days": "7"}).status_code == 303
    assert site.services.settings.trash_retention_days == 60 and site.services.settings.keep_originals_days == 7
    assert save(client, "ablage", "namen", {"owner_names": "Erika Mustermann\nMax Mustermann"}).status_code == 303
    assert site.services.settings.owner_names == ("Erika Mustermann", "Max Mustermann")
    assert "Neustart nötig" not in client.get("/einstellungen").text


def test_the_notification_test(site):
    client = admin(site)
    response = client.post("/einstellungen/benachrichtigungen/testen", data={"csrf_token": csrf(client)}, follow_redirects=False)
    assert response.status_code == 303
    assert "keine Benachrichtigungs-Adresse" in " ".join(flash_texts(client.get("/einstellungen").text))
    save(client, "benachrichtigungen", "ziel", {"notify_url": "https://ntfy.example.test/docushelf"})
    with respx.mock() as router:
        route = router.post("https://ntfy.example.test/docushelf").mock(return_value=httpx.Response(200))
        client.post("/einstellungen/benachrichtigungen/testen", data={"csrf_token": csrf(client)}, follow_redirects=False)
        assert route.called and "DocuShelf" in route.calls[0].request.headers["Title"]
    assert "Test-Benachrichtigung an https://ntfy.example.test geschickt." in client.get("/einstellungen").text
    with respx.mock() as router:
        router.post("https://ntfy.example.test/docushelf").mock(return_value=httpx.Response(403))
        client.post("/einstellungen/benachrichtigungen/testen", data={"csrf_token": csrf(client)}, follow_redirects=False)
    assert "HTTP 403" in client.get("/einstellungen").text


# ---------------------------------------------------------------------------
# Login settings: confirmation and lock-out protection


def test_login_sections_need_a_confirmation(site):
    client = admin(site)
    data = {"admin_users": "berta", "admin_groups": "", "allowed_users": "", "allowed_groups": "familie"}
    response = save(client, "anmeldung", "zugang", data)
    assert response.status_code == 400 and "Bitte bestätige zuerst" in response.text
    assert not site.store.path.exists()
    assert save(client, "anmeldung", "zugang", data, confirm=True).status_code == 303
    assert file_values(site) == {"admin_users": ["berta"]}  # allowed_groups comes from the environment
    assert "„Wer darf hinein“" in client.get("/einstellungen/anmeldung").text


def test_nobody_locks_themselves_out(site):
    site.store.save({"admin_users": ["anna"]})
    site.app.state.settings = site.app.state.settings.with_overrides(admin_users=("anna",))
    client = sso(site)
    response = save(client, "anmeldung", "zugang", {"admin_users": "berta"}, confirm=True)
    assert response.status_code == 400 and "Administratorrechte entziehen" in response.text
    assert file_values(site) == {"admin_users": ["anna"]}
    assert save(client, "anmeldung", "zugang", {"admin_users": "anna, berta"}, confirm=True).status_code == 303
    assert file_values(site) == {"admin_users": ["anna", "berta"]}


def test_sso_settings_cannot_disable_every_login(env, monkeypatch):
    _ocr(monkeypatch)
    settings = load_settings({**env, "DOCUSHELF_ADMIN_PASSWORD": ""}).with_overrides(admin_password="pw")
    app = create_app(settings)
    app.state.settings_env = {k: v for k, v in env.items() if k != "DOCUSHELF_ADMIN_PASSWORD"}
    try:
        client = client_for(app)
        assert login_local(client, password="pw").status_code == 303
        store = SettingsFile(settings.root_dir, env=app.state.settings_env)
        store.save({"oidc_issuer": "https://idp.example.test/", "oidc_client_id": "docushelf"})
        data = {"oidc_issuer": "", "oidc_client_id": "", "oidc_label": "Mit SSO anmelden", "oidc_scopes": "openid email profile"}
        response = save(client, "anmeldung", "oidc", data, confirm=True)
        assert response.status_code == 400 and "könnte sich niemand mehr anmelden" in response.text
        assert store.read()["oidc_issuer"] == "https://idp.example.test/"
    finally:
        app.state.registry.close()


# ---------------------------------------------------------------------------
# Restart


def test_restart_button(site, monkeypatch):
    calls = []
    site.app.state.restart_handler = lambda: calls.append("stop")
    monkeypatch.setattr(routes_settings, "RESTART_DELAY", 0.0)
    client = admin(site)
    save(client, "verarbeitung", "betrieb", {"worker_threads": "2", "max_upload_mb": "200", "reconcile_interval": "6"})
    page = client.get("/einstellungen/verarbeitung").text
    banner = re.search(r'<div class="restart-banner".*?</form>', page, re.S).group(0)
    assert "„Leistung &amp; Grenzen“" in banner and 'action="/einstellungen/neustart"' in banner and "data-confirm" in banner

    response = client.post(
        "/einstellungen/neustart", data={"csrf_token": csrf(client), "zurueck": "/einstellungen/verarbeitung"}
    )
    assert response.status_code == 200 and "DocuShelf startet neu" in response.text
    assert 'content="12;url=/einstellungen/verarbeitung"' in response.text
    for _ in range(100):
        if calls:
            break
        time.sleep(0.01)
    assert calls == ["stop"]
    entry = site.services.audit.tail(1)[0]
    assert entry["action"] == "restart" and "Leistung & Grenzen" in entry["summary"]
    # A foreign target is not followed.
    response = client.post("/einstellungen/neustart", data={"csrf_token": csrf(client), "zurueck": "https://example.org/"})
    assert 'url=/einstellungen"' in response.text


def test_restart_without_a_server_says_so(site):
    client = admin(site)
    response = client.post("/einstellungen/neustart", data={"csrf_token": csrf(client)}, follow_redirects=False)
    assert response.status_code == 303
    assert "kann sich hier nicht selbst neu starten" in client.get("/einstellungen").text


# ---------------------------------------------------------------------------
# Audit log


def test_audit_log_filters(site):
    audit = site.services.audit
    audit.write("anna", "file", title="Stromrechnung", path="Wohnung/Strom/a.pdf")
    audit.write("berta", "file", title="Kontoauszug", path="Finanzen/Bank/b.pdf")
    audit.write("berta", "token_create", name="Handy")
    client = admin(site)
    page = client.get("/einstellungen/protokoll").text
    assert "„Stromrechnung“" in page and "„Kontoauszug“" in page and "API-Token erstellt" in page
    page = client.get("/einstellungen/protokoll?wer=berta").text
    assert "„Stromrechnung“" not in page and "„Kontoauszug“" in page
    page = client.get("/einstellungen/protokoll?aktion=Abgelegt&suche=strom").text
    assert "„Stromrechnung“" in page and "„Kontoauszug“" not in page and "API-Token erstellt" not in page.split("audit-list")[-1]
    page = client.get("/einstellungen/protokoll?suche=gibtsnicht").text
    assert "Keine Einträge passen zu diesem Filter." in page


# ---------------------------------------------------------------------------
# Channels page and its server section


def test_channel_page_in_the_settings_layout(site):
    client = admin(site)
    page = client.get("/einstellungen/kanaele").text
    assert 'class="settings-nav"' in page and "Mail-Postfächer (IMAP)" in page and 'id="empfang"' in page
    data = {"smtp_enabled": "1", "smtp_recipient": "scan@example.test", "smtp_allowed_networks": "192.168.1.0/24",
            "smtp_max_message_mb": "50", "smtp_user": "", "smtp_password": "smtp-geheim", "stable_seconds": "5",
            "remote_stable_seconds": "10"}
    response = save(client, "kanaele", "empfang", data)
    assert response.status_code == 303, response.text
    stored = file_values(site)
    assert stored["smtp_enabled"] is True and stored["smtp_allowed_networks"] == ["192.168.1.0/24"]
    assert "smtp-geheim" not in site.store.path.read_text(encoding="utf-8")
    page = client.get("/einstellungen/kanaele").text
    assert "Neustart nötig" in page and "smtp-geheim" not in page
    response = save(client, "kanaele", "empfang", {**data, "smtp_recipient": "", "smtp_password": ""})
    assert response.status_code == 400 and "SMTP_RECIPIENT fehlt" in response.text
    assert 'class="settings-nav"' in response.text


# ---------------------------------------------------------------------------
# Several households


HOUSEHOLDS = """\
admins: [berta]
households:
  - id: familie-a
    name: "Familie A"
    path: "."
    groups: [familie-a]
  - id: eltern
    name: "Eltern"
    groups: [eltern]
    users: [opa]
"""


@pytest.fixture
def multi(env, data_dir, monkeypatch):
    (data_dir / "config").mkdir(exist_ok=True)
    path = data_dir / "config" / "households.yaml"
    path.write_text(HOUSEHOLDS, encoding="utf-8")
    _ocr(monkeypatch)
    provider = FakeProvider()
    env.update({"DOCUSHELF_OIDC_ISSUER": provider.issuer, "DOCUSHELF_OIDC_CLIENT_ID": provider.client_id,
                "DOCUSHELF_OIDC_CLIENT_SECRET": provider.client_secret})
    settings = load_settings(env)
    registry = Registry(settings)
    app = create_app(settings, registry=registry)
    app.state.settings_env = env
    yield SimpleNamespace(app=app, registry=registry, path=path, provider=provider)
    registry.stop()
    registry.close()


def multi_admin(multi, household="familie-a"):
    client = client_for(multi.app)
    assert login_local(client).status_code == 303
    if household != "familie-a":
        client.post("/haushalt", data={"csrf_token": csrf(client), "id": household})
    return client


def test_household_values_go_to_households_yaml(multi):
    client = multi_admin(multi, "eltern")
    before = multi.path.read_text(encoding="utf-8")
    page = client.get("/einstellungen/ablage").text
    assert "Gilt für den Haushalt <strong>Eltern</strong>" in card(page, "automatisch")
    data = {"auto_file": "always", "auto_file_threshold": "70", "auto_file_sources": ["upload"], "auto_file_notify": "1"}
    response = save(client, "ablage", "automatisch", data)
    assert response.status_code == 303
    entry = yaml.safe_load(multi.path.read_text(encoding="utf-8"))["households"][1]
    assert entry["auto_file"] == {"mode": "always", "threshold": 0.7, "sources": ["upload"], "new_folders": False,
                                  "wait_for_llm": False, "notify": True}
    assert (multi.path.parent / "households.yaml.bak").read_text(encoding="utf-8") == before
    eltern = multi.registry.services("eltern")
    assert eltern.settings.auto_file.mode == "always" and eltern.settings.auto_file.threshold == 0.7
    assert multi.registry.services("familie-a").settings.auto_file.mode == "off"
    assert multi.registry.get("eltern").auto_file["mode"] == "always"
    assert not (multi.path.parent / "settings.yaml").exists()
    assert "Neustart nötig" not in client.get("/einstellungen/ablage").text
    entry = eltern.audit.tail(1)[0]
    assert entry["action"] == "settings_change" and entry["household"] == "eltern"

    assert save(client, "benachrichtigungen", "ziel", {"notify_url": "https://ntfy.example.test/eltern"}).status_code == 303
    assert eltern.notifier.url == "https://ntfy.example.test/eltern"
    assert multi.registry.services("familie-a").notifier.url is None
    assert save(client, "ablage", "namen", {"owner_names": "Opa Mustermann"}).status_code == 303
    assert eltern.settings.owner_names == ("Opa Mustermann",)
    assert yaml.safe_load(multi.path.read_text(encoding="utf-8"))["households"][1]["owner_names"] == ["Opa Mustermann"]


def test_admins_and_groups_and_names_of_households(multi):
    client = multi_admin(multi)
    page = client.get("/einstellungen/haushalte").text
    assert 'id="administratoren"' in page and 'id="haushalt-eltern"' in page and "berta" in page

    def post(url, data):
        return client.post(url, data={"csrf_token": csrf(client), **data}, follow_redirects=False)

    assert post("/einstellungen/haushalte/administratoren", {"art": "gruppe", "name": "software-admins"}).status_code == 303
    assert post("/einstellungen/haushalte/eltern/gruppen", {"group": "grosseltern"}).status_code == 303
    assert post("/einstellungen/haushalte/eltern/gruppen/entfernen", {"group": "eltern"}).status_code == 303
    assert post("/einstellungen/haushalte/eltern/name", {"name": "Oma & Opa"}).status_code == 303
    data = yaml.safe_load(multi.path.read_text(encoding="utf-8"))
    assert data["admin_groups"] == ["software-admins"]
    assert data["households"][1]["groups"] == ["grosseltern"] and data["households"][1]["name"] == "Oma & Opa"
    assert multi.registry.admin_groups == ("software-admins",)
    assert multi.registry.get("eltern").groups == ("grosseltern",) and multi.registry.get("eltern").name == "Oma & Opa"
    assert multi.registry.services("eltern").settings.household_name == "Oma & Opa"
    assert post("/einstellungen/haushalte/administratoren/entfernen", {"art": "person", "name": "berta"}).status_code == 303
    assert "admins" not in yaml.safe_load(multi.path.read_text(encoding="utf-8"))
    page = client.get("/einstellungen/haushalte").text
    assert "Administrator „berta“ entfernt" in page and "Oma &amp; Opa" in page


def test_an_admin_cannot_remove_their_own_rights(multi):
    client = client_for(multi.app)
    multi.provider.claims.update({"sub": "u-berta", "preferred_username": "berta", "email": "berta@example.test",
                                  "groups": ["familie-a"]})
    with multi.provider.running():
        assert oidc_login(client, multi.provider).status_code == 303
    before = multi.path.read_text(encoding="utf-8")
    response = client.post("/einstellungen/haushalte/administratoren/entfernen",
                           data={"csrf_token": csrf(client), "art": "person", "name": "berta"}, follow_redirects=False)
    assert response.status_code == 303
    assert "Administratorrechte entziehen" in client.get("/einstellungen/haushalte").text
    assert multi.path.read_text(encoding="utf-8") == before


def test_household_pages_for_members_without_admin_rights(multi):
    client = client_for(multi.app)
    multi.provider.claims.update({"sub": "u-anna", "preferred_username": "anna", "email": "anna@example.test",
                                  "groups": ["familie-a"]})
    with multi.provider.running():
        assert oidc_login(client, multi.provider).status_code == 303
    assert client.get("/einstellungen/ablage").status_code == 403
    assert client.get("/einstellungen/handy").status_code == 200
    response = save(client, "ablage", "automatisch", {"auto_file": "always"})
    assert response.status_code == 403
    assert "auto_file" not in multi.path.read_text(encoding="utf-8")


def test_anthropic_uses_its_fixed_address_and_is_shown_as_cloud(site, env):
    from docushelf.config import ANTHROPIC_URL
    from docushelf.web import views

    client = admin(site)
    # An address left over from Ollama stays in the file until the section is saved …
    site.store.save({"llm_enabled": True, "llm_provider": "ollama", "llm_url": "http://ollama:11434"})
    assert file_values(site)["llm_url"] == "http://ollama:11434"
    # … and is dropped once Anthropic is chosen: the Anthropic API has a fixed address.
    response = save(client, "ki", "sprachmodell", {"llm_enabled": "1", "llm_provider": "anthropic",
                                                   "llm_url": "http://ollama:11434", "llm_model": ""})
    assert response.status_code == 303
    values = file_values(site)
    assert values["llm_provider"] == "anthropic" and "llm_url" not in values

    # The loader ignores a stored address for Anthropic, so the page never shows a local one.
    settings = load_settings({**env, "DOCUSHELF_LLM_ENABLED": "true", "DOCUSHELF_LLM_PROVIDER": "anthropic",
                              "DOCUSHELF_LLM_URL": "http://ollama:11434"})
    assert settings.llm.url == ANTHROPIC_URL
    info = views.llm_info(settings)
    assert info.provider_label == "Anthropic" and info.cloud and info.local is False
    assert info.endpoint == "https://api.anthropic.com" and info.context_tokens is None


def test_the_language_model_page_shows_a_cloud_provider_as_cloud(site, env):
    site.services.settings = load_settings({**env, "DOCUSHELF_LLM_ENABLED": "true", "DOCUSHELF_LLM_PROVIDER": "anthropic",
                                            "DOCUSHELF_LLM_API_KEY": "sk-x", "DOCUSHELF_LLM_URL": "http://ollama:11434"})
    status = card(admin(site).get("/einstellungen/ki").text, "status")
    assert "Anthropic" in status and "Cloud" in status and "api.anthropic.com" in status
    assert "ollama:11434" not in status and "im lokalen Netz" not in status
