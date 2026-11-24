"""The settings page "Eingangskanäle" and the iPhone shortcut routes.

Single household (env only) and household mode: who may open and change the
page, CSRF, validation errors that keep the input, channels.yaml with .bak,
passwords in config/secrets (mode 600, never sent back), the IMAP connection
test against an in-memory server, and the shortcut download / phone token.
"""

from __future__ import annotations

import stat
from types import SimpleNamespace

import pytest
import yaml

from docushelf.channels.store import parse_channels
from docushelf.config import load_settings
from docushelf.households import Registry
from docushelf.services import Services
from docushelf.web import routes_channels, routes_ui
from docushelf.web.app import create_app
from tests.test_imap import FakeImapServer
from web_support import FakeProvider, client_for, login_local, make_app, oidc_login, session_data

PAGE = "/einstellungen/kanaele"


@pytest.fixture(autouse=True)
def _no_ocr_probe(monkeypatch):
    monkeypatch.setattr(routes_ui, "_ocr_info", lambda s: routes_ui._OcrInfo(False, None, None, [], []))


@pytest.fixture
def single(settings):
    services = Services(settings)
    app = make_app(services)
    yield SimpleNamespace(app=app, services=services, layout=services.layout)
    services.stop()
    services.index.close()


def admin_client(app):
    client = client_for(app)
    assert login_local(client).status_code == 303
    return client


def csrf(client) -> str:
    return session_data(client)["csrf"]


def post(client, path, data=None, **kwargs):
    form = {"csrf_token": csrf(client), **(data or {})}
    return client.post(PAGE + path, data=form, follow_redirects=False, **kwargs)


MAILBOX = {
    "name": "familie",
    "host": "imap.example.org",
    "user": "docs@example.org",
    "password": "pw-sehr-geheim-123",
    "tls": "ssl",
    "port": "993",
    "allowed_senders": "erika@example.org, @stadtwerke.example",
    "poll_seconds": "120",
    "enabled": "1",
    "aktion": "speichern",
}


def channels_file(layout):
    return layout.config / "channels.yaml"


# ---------------------------------------------------------------------------
# Access


def test_page_needs_a_login(single):
    response = client_for(single.app).get(PAGE, follow_redirects=False)
    assert response.status_code in (302, 303, 307)
    assert "/login" in response.headers["location"]


def test_local_admin_sees_the_page_and_the_link_in_the_settings(single):
    client = admin_client(single.app)
    page = client.get(PAGE)
    assert page.status_code == 200
    assert "Eingangskanäle" in page.text and "Mail-Postfächer (IMAP)" in page.text
    assert "Voreinstellungen aus den Umgebungsvariablen" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert 'href="/einstellungen/kanaele"' in client.get("/einstellungen").text


def test_sso_user_without_admin_rights_gets_403(settings, data_dir):
    provider = FakeProvider()
    settings = settings.with_overrides(**provider.settings(), allow_all_oidc_users=True, admin_groups=("software-admins",))
    services = Services(settings)
    app = make_app(services)
    try:
        def login(groups):
            client = client_for(app)
            provider.claims.update({"sub": "u", "preferred_username": "anna", "email": "anna@example.test", "groups": groups})
            with provider.running():
                assert oidc_login(client, provider).status_code == 303
            return client

        anna = login(["familie"])
        assert anna.get(PAGE).status_code == 403
        assert 'href="/einstellungen/kanaele"' not in anna.get("/einstellungen").text
        response = anna.post(PAGE + "/smtp/schalten", data={"csrf_token": csrf(anna), "an": "1"}, follow_redirects=False)
        assert response.status_code == 403
        assert not channels_file(services.layout).exists()

        boss = login(["Software-Admins"])
        assert boss.get(PAGE).status_code == 200
    finally:
        services.stop()
        services.index.close()


def test_api_tokens_cannot_use_the_page(single):
    token, _ = single.services.tokens.create("skript", "admin")
    client = client_for(single.app)
    response = client.get(PAGE, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    response = client.post(PAGE + "/smtp/schalten", data={"an": "1"}, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert not channels_file(single.layout).exists()


def test_changes_need_the_csrf_token(single):
    client = admin_client(single.app)
    response = client.post(PAGE + "/imap", data=MAILBOX, follow_redirects=False)
    assert response.status_code == 403
    response = client.post(PAGE + "/imap", data={**MAILBOX, "csrf_token": "falsch"}, follow_redirects=False)
    assert response.status_code == 403
    assert not channels_file(single.layout).exists()


# ---------------------------------------------------------------------------
# Mailboxes, secrets and the file


def test_new_mailbox_writes_the_file_and_a_private_password_file(single):
    client = admin_client(single.app)
    response = post(client, "/imap", MAILBOX)
    assert response.status_code == 303 and response.headers["location"].endswith("#postfaecher")

    path = channels_file(single.layout)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    box = data["imap"][0]
    assert box["name"] == "familie" and box["host"] == "imap.example.org" and box["poll_seconds"] == 120
    assert box["allowed_senders"] == ["erika@example.org", "@stadtwerke.example"]
    assert "pw-sehr-geheim-123" not in path.read_text(encoding="utf-8")

    secret = single.layout.config / "secrets" / "imap-familie"
    assert secret.read_text(encoding="utf-8") == "pw-sehr-geheim-123"
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert stat.S_IMODE(secret.parent.stat().st_mode) == 0o700

    page = client.get(PAGE).text
    assert "pw-sehr-geheim-123" not in page
    assert "gesetzt – leer lassen, um es zu behalten" in page
    assert "Gespeichert in" in page

    # The running household fetches the new mailbox right away.
    manager = single.services.channels
    assert manager.from_file and list(manager.fetchers) == ["familie"]
    assert single.services.settings.imap.host == "imap.example.org"
    assert single.services.settings.imap.password == "pw-sehr-geheim-123"

    entries = single.services.audit.tail(5)
    assert entries[0]["action"] == "channels_change"
    assert entries[0]["summary"] == "Postfach „familie“ angelegt"
    assert "pw-sehr-geheim-123" not in str(entries)


def test_saving_again_keeps_the_password_and_writes_a_backup(single):
    client = admin_client(single.app)
    assert post(client, "/imap", MAILBOX).status_code == 303
    first = channels_file(single.layout).read_text(encoding="utf-8")

    edit = {**MAILBOX, "original": "familie", "password": "", "poll_seconds": "300"}
    assert post(client, "/imap", edit).status_code == 303
    backup = single.layout.config / "channels.yaml.bak"
    assert backup.read_text(encoding="utf-8") == first
    assert yaml.safe_load(channels_file(single.layout).read_text())["imap"][0]["poll_seconds"] == 300
    assert (single.layout.config / "secrets" / "imap-familie").read_text() == "pw-sehr-geheim-123"

    # Renaming moves the password along and removes the old one.
    renamed = {**edit, "name": "post"}
    assert post(client, "/imap", renamed).status_code == 303
    assert (single.layout.config / "secrets" / "imap-post").read_text() == "pw-sehr-geheim-123"
    assert not (single.layout.config / "secrets" / "imap-familie").exists()
    assert list(single.services.channels.fetchers) == ["post"]

    # The overview of the settings counts the mailboxes.
    assert post(client, "/imap", {**MAILBOX, "name": "erika"}).status_code == 303
    assert "2 Postfächer" in client.get("/einstellungen").text


def test_invalid_input_is_shown_in_german_and_keeps_what_was_typed(single):
    client = admin_client(single.app)
    response = post(client, "/imap", {**MAILBOX, "host": "kein host!", "folder": "Posteingang Spezial"})
    assert response.status_code == 400
    assert "kein gültiger Servername" in response.text
    assert 'value="Posteingang Spezial"' in response.text
    assert "pw-sehr-geheim-123" not in response.text  # the password is never sent back
    assert not channels_file(single.layout).exists()

    response = post(client, "/imap", {**MAILBOX, "password": ""})
    assert response.status_code == 400 and "Bitte das Passwort des Postfachs eingeben" in response.text

    response = post(client, "/imap", {**MAILBOX, "processed_folder": "INBOX"})
    assert response.status_code == 400 and "darf nicht der abgerufene Ordner" in response.text

    response = post(client, "/imap", {**MAILBOX, "allowed_senders": "kein absender"})
    assert response.status_code == 400 and "weder eine E-Mail-Adresse noch eine Domain" in response.text

    response = post(client, "/imap", {**MAILBOX, "name": "Familie A"})
    assert response.status_code == 400 and "Kleinbuchstaben" in response.text
    assert not channels_file(single.layout).exists()


def test_duplicate_names_are_refused(single):
    client = admin_client(single.app)
    assert post(client, "/imap", MAILBOX).status_code == 303
    response = post(client, "/imap", MAILBOX)
    assert response.status_code == 400 and "gibt es schon" in response.text


def test_toggle_and_remove_a_mailbox(single):
    client = admin_client(single.app)
    assert post(client, "/imap", MAILBOX).status_code == 303
    manager = single.services.channels

    assert post(client, "/imap/familie/schalten", {"an": "0"}).status_code == 303
    assert manager.config.mailbox("familie").enabled is False and manager.fetchers == {}
    assert single.services.settings.imap is None
    assert post(client, "/imap/familie/schalten", {"an": "1"}).status_code == 303
    assert list(manager.fetchers) == ["familie"]

    assert post(client, "/imap/familie/entfernen").status_code == 303
    assert manager.config.imap == () and manager.fetchers == {}
    assert not (single.layout.config / "secrets" / "imap-familie").exists()
    summaries = [e.get("summary") for e in single.services.audit.tail(10)]
    assert "Postfach „familie“ entfernt" in summaries and "Postfach „familie“ ausgeschaltet" in summaries


def test_owner_must_be_a_member_when_the_household_lists_members(settings):
    settings = settings.with_overrides(allowed_users=("erika", "max"))
    services = Services(settings)
    app = make_app(services)
    try:
        client = admin_client(app)
        response = post(client, "/smtp/adressen", {"address": "scan-zoe@docushelf.local", "owner": "zoe"})
        assert response.status_code == 400 and "kein Mitglied" in response.text
        assert 'value="scan-zoe@docushelf.local"' in response.text
        assert post(client, "/smtp/adressen", {"address": "scan-erika@docushelf.local", "owner": "Erika"}).status_code == 303
        assert '<option value="erika">' in client.get(PAGE).text
    finally:
        services.stop()
        services.index.close()


def test_imap_connection_test_uses_the_fake_server(single, monkeypatch):
    server = FakeImapServer(username="docs@example.org", password="pw-sehr-geheim-123")
    server.deliver(b"Subject: x\r\n\r\nhallo")
    monkeypatch.setattr(routes_channels, "IMAP_CONNECTION_FACTORY", server.connect)
    client = admin_client(single.app)

    response = post(client, "/imap", {**MAILBOX, "aktion": "testen"})
    assert response.status_code == 200
    assert "Verbindung klappt" in response.text and "mit 1 Nachricht" in response.text
    assert not channels_file(single.layout).exists()  # a test saves nothing
    assert "pw-sehr-geheim-123" not in response.text
    # Nothing was changed in the mailbox: no folders created, no mail moved.
    assert set(server.folders) == {"INBOX"} and len(server.folders["INBOX"]) == 1

    response = post(client, "/imap", {**MAILBOX, "aktion": "testen", "password": "falsch"})
    assert "Anmeldung am IMAP-Server fehlgeschlagen" in response.text

    # A stored password is used when the field stays empty.
    assert post(client, "/imap", MAILBOX).status_code == 303
    response = post(client, "/imap", {**MAILBOX, "original": "familie", "password": "", "aktion": "testen"})
    assert "Verbindung klappt" in response.text

    response = post(client, "/imap", {**MAILBOX, "folder": "Fehlt", "aktion": "testen"})
    assert "lässt sich nicht öffnen" in response.text


# ---------------------------------------------------------------------------
# SMTP, scanner folders, watch folders


def test_smtp_addresses_and_server_hint(single):
    client = admin_client(single.app)
    page = client.get(PAGE).text
    assert "Der SMTP-Empfänger läuft nicht" in page and "DOCUSHELF_SMTP_ENABLED=true" in page

    assert post(client, "/smtp/adressen", {"address": "Scan-Erika@Docushelf.local", "owner": "erika"}).status_code == 303
    smtp = single.services.channels.config.smtp
    assert smtp.enabled and smtp.recipients[0].address == "scan-erika@docushelf.local"
    assert smtp.recipients[0].owner == "erika"

    response = post(client, "/smtp/adressen", {"address": "keine-adresse"})
    assert response.status_code == 400 and "keine E-Mail-Adresse" in response.text
    response = post(client, "/smtp/adressen", {"address": "scan-erika@docushelf.local"})
    assert response.status_code == 400 and "schon eingetragen" in response.text

    assert post(client, "/smtp/schalten", {"an": "0"}).status_code == 303
    assert single.services.channels.smtp_route("scan-erika@docushelf.local") is None
    assert post(client, "/smtp/adressen/entfernen", {"address": "scan-erika@docushelf.local"}).status_code == 303
    assert single.services.channels.config.smtp.recipients == ()


def test_scanner_endpoints_create_folders_with_the_drop_zone_mode(single):
    smb = single.layout.source_dir("smb")
    smb.mkdir(parents=True, exist_ok=True)
    smb.chmod(0o1733)
    client = admin_client(single.app)
    assert post(client, "/scanner/smb/endpunkte", {"owner": "erika.mustermann@example.org"}).status_code == 303
    folder = smb / "erika.mustermann"
    assert folder.is_dir() and stat.S_IMODE(folder.stat().st_mode) == 0o1733
    source = next(s for s in single.services.settings.sources if s.name == "smb")
    assert source.endpoints[0].owner == "erika.mustermann@example.org"

    page = client.get(PAGE).text
    assert "\\\\testserver\\scan\\erika.mustermann" in page
    assert "docker compose --profile smb up -d" in page

    assert post(client, "/scanner/smb/endpunkte/schalten", {"folder": "erika.mustermann", "an": "0"}).status_code == 303
    source = next(s for s in single.services.settings.sources if s.name == "smb")
    assert source.endpoints[0].enabled is False

    response = post(client, "/scanner/smb/endpunkte", {"folder": "../etc"})
    assert response.status_code == 400 and "ungültig" in response.text
    response = post(client, "/scanner/smb/endpunkte", {"folder": "Erika.Mustermann"})
    assert response.status_code == 400 and "schon" in response.text

    assert post(client, "/scanner/ftp/schalten", {"an": "0"}).status_code == 303
    assert "ftp" not in [s.name for s in single.services.settings.sources]
    assert post(client, "/scanner/smb/endpunkte/entfernen", {"folder": "erika.mustermann"}).status_code == 303
    assert folder.is_dir()  # the folder itself stays
    assert post(client, "/scanner/xyz/schalten", {"an": "1"}).status_code == 303


def test_watch_folders(single, tmp_path):
    outside = tmp_path / "nas"
    outside.mkdir()
    client = admin_client(single.app)
    data = {"name": "nas", "path": str(outside), "enabled": "1"}
    assert post(client, "/watch", data).status_code == 303
    source = next(s for s in single.services.settings.sources if s.name == "watch-nas")
    assert source.path == outside and source.remove_after_import is False

    response = post(client, "/watch", {"name": "archiv", "path": str(single.layout.archive / "x"), "enabled": "1"})
    assert response.status_code == 400 and "überschneidet sich" in response.text
    response = post(client, "/watch", {"name": "rel", "path": "relativ/pfad"})
    assert response.status_code == 400 and "absolut" in response.text
    for system in ("/", "/etc", "/proc/self", "/usr/lib"):
        response = post(client, "/watch", {"name": "sys", "path": system, "enabled": "1"})
        assert response.status_code == 400 and "Systemordner" in response.text, system

    assert post(client, "/watch/nas/schalten", {"an": "0"}).status_code == 303
    assert "watch-nas" not in [s.name for s in single.services.settings.sources]
    assert post(client, "/watch/nas/entfernen").status_code == 303
    assert single.services.channels.config.watch == ()


def test_every_written_file_is_valid_for_the_loader(single, tmp_path):
    client = admin_client(single.app)
    post(client, "/imap", MAILBOX)
    post(client, "/smtp/adressen", {"address": "scan@docushelf.local"})
    post(client, "/scanner/smb/endpunkte", {"owner": "max"})
    parsed = parse_channels(channels_file(single.layout).read_text(encoding="utf-8"))
    assert parsed == single.services.channels.config


# ---------------------------------------------------------------------------
# Several households


HOUSEHOLDS = """\
admins: [berta]
households:
  - id: familie-a
    name: "Familie A"
    path: "."
    groups: [familie-a]
    users: [erika]
    smtp_recipients: [scan-a@example.test]
  - id: eltern
    name: "Eltern"
    groups: [eltern]
    users: [opa]
    smtp_recipients: [scan-eltern@example.test]
"""


@pytest.fixture
def multi(env, data_dir):
    path = data_dir / "config" / "households.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HOUSEHOLDS, encoding="utf-8")
    provider = FakeProvider()
    smtp = {"DOCUSHELF_SMTP_ENABLED": "true", "DOCUSHELF_SMTP_PORT": "0", "DOCUSHELF_SMTP_ALLOWED_NETWORKS": "127.0.0.1/32"}
    settings = load_settings({**env, **smtp}).with_overrides(**provider.settings())
    registry = Registry(settings)
    app = create_app(settings, registry=registry)
    yield SimpleNamespace(app=app, registry=registry, provider=provider)
    registry.stop()
    registry.close()


def sso(multi, name, groups):
    client = client_for(multi.app)
    multi.provider.claims.update(
        {"sub": f"user-{name}", "preferred_username": name, "email": f"{name}@example.test", "groups": groups}
    )
    with multi.provider.running():
        assert oidc_login(client, multi.provider).status_code == 303
    return client


def test_household_admins_manage_their_current_household_only(multi):
    erika = sso(multi, "erika", ["familie-a"])
    assert erika.get(PAGE).status_code == 403

    berta = sso(multi, "berta", ["familie-a"])
    page = berta.get(PAGE)
    assert page.status_code == 200 and "Familie A" in page.text
    assert "scan-a@example.test" in page.text  # the default from households.yaml
    assert "scan-eltern@example.test" not in page.text

    data = {"csrf_token": csrf(berta), "household": "familie-a", "address": "scan-erika@example.test", "owner": "erika"}
    assert berta.post(PAGE + "/smtp/adressen", data=data, follow_redirects=False).status_code == 303
    channels = multi.registry.services("familie-a").channels
    assert channels.from_file
    assert [r.address for r in channels.config.smtp.recipients] == ["scan-a@example.test", "scan-erika@example.test"]
    assert multi.registry.by_smtp_recipient("scan-erika@example.test").id == "familie-a"
    assert "Scan per Mail an" in berta.get("/einstellungen").text
    assert not (multi.registry.services("eltern").layout.config / "channels.yaml").exists()

    # An address of another household is refused.
    data = {"csrf_token": csrf(berta), "address": "scan-eltern@example.test"}
    response = berta.post(PAGE + "/smtp/adressen", data=data, follow_redirects=False)
    assert response.status_code == 400 and "gehört schon zum Haushalt „Eltern“" in response.text

    # The owner must be a member of the household.
    data = {"csrf_token": csrf(berta), "address": "scan-opa@example.test", "owner": "opa"}
    response = berta.post(PAGE + "/smtp/adressen", data=data, follow_redirects=False)
    assert response.status_code == 400 and "kein Mitglied" in response.text

    entries = multi.registry.services("familie-a").audit.tail(3)
    assert entries[0]["action"] == "channels_change"


def test_page_of_a_stale_household_is_refused(multi):
    admin = client_for(multi.app)
    assert login_local(admin).status_code == 303
    data = {"csrf_token": csrf(admin), "household": "eltern", "an": "0"}
    response = admin.post(PAGE + "/smtp/schalten", data=data, follow_redirects=False)
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# iPhone shortcut


def test_shortcut_download_and_link(single, settings):
    client = admin_client(single.app)
    assert client_for(single.app).get("/kurzbefehl", follow_redirects=False).status_code in (302, 303, 307)

    response = client.get("/kurzbefehl", follow_redirects=False)
    assert response.status_code == 404 and "noch nicht hinterlegt" in response.text
    assert "Kurzbefehl installieren" not in client.get("/einstellungen/handy").text

    folder = settings.root_dir / "config" / "shortcuts"
    folder.mkdir(parents=True)
    (folder / "DocuShelf senden.shortcut").write_bytes(b"signed-shortcut")
    response = client.get("/kurzbefehl", follow_redirects=False)
    assert response.status_code == 200 and response.content == b"signed-shortcut"
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert "Kurzbefehl installieren" in client.get("/einstellungen/handy").text


def test_shortcut_redirects_to_the_icloud_link(settings):
    services = Services(settings)
    app = make_app(services, ios_shortcut_url="https://www.icloud.com/shortcuts/abc123")
    try:
        client = admin_client(app)
        response = client.get("/kurzbefehl", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "https://www.icloud.com/shortcuts/abc123"
    finally:
        services.stop()
        services.index.close()


def test_shortcut_url_must_be_https(env):
    from docushelf.errors import ConfigError

    with pytest.raises(ConfigError, match="https-Link"):
        load_settings({**env, "DOCUSHELF_IOS_SHORTCUT_URL": "http://example.org/x"})
    assert load_settings({**env, "DOCUSHELF_IOS_SHORTCUT_URL": "https://www.icloud.com/shortcuts/x"}).ios_shortcut_url


def test_phone_token_is_created_for_the_user_and_shown_once(single):
    client = admin_client(single.app)
    page = client.get("/einstellungen/handy").text
    assert "Token für dieses Handy erstellen" in page
    assert "Serveradresse für den Kurzbefehl" in page and 'value="http://testserver"' in page

    response = client.post("/kurzbefehl/token", data={"csrf_token": csrf(client)}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/einstellungen/handy#handy"
    tokens = single.services.tokens.list()
    assert [t["name"] for t in tokens] == ["iPhone-Kurzbefehl"] and tokens[0]["user"] == "admin"

    first = client.get("/einstellungen/handy")
    assert 'id="phone-token-value"' in first.text and 'id="new-token-value"' not in first.text
    assert first.headers["cache-control"] == "no-store"
    assert 'id="phone-token-value"' not in client.get("/einstellungen/handy").text  # only once

    assert client.post("/kurzbefehl/token", data={}, follow_redirects=False).status_code == 403
    token, _ = single.services.tokens.create("skript", "admin")
    response = client_for(single.app).post("/kurzbefehl/token", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
