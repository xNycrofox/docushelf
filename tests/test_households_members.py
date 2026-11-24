"""Managing household members in the web interface.

Two households: "familie-a" (data root, group familie-a) and "eltern" (group
eltern, user opa). Admins are the local emergency account, the ``admins`` of
households.yaml (berta) and ``DOCUSHELF_ADMIN_USERS``. A change is written back
to households.yaml (with a .bak) and applies to the running registry at once.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
import yaml

from docushelf import households as households_module
from docushelf.config import load_settings
from docushelf.errors import ConfigError
from docushelf.households import Registry
from docushelf.web import routes_ui
from docushelf.web.app import SESSION_COOKIE, create_app
from web_support import FakeProvider, client_for, login_local, oidc_login, session_data, signed_session

FILE_TEXT = """\
# Kommentar, der beim Speichern verloren gehen darf
admins: [Berta]
households:
  - id: familie-a
    name: "Familie A"
    path: "."
    groups: [familie-a]
    owner_names: ["Erika Mustermann"]
    smtp_recipients: [scan-a@example.test]
  - id: eltern
    name: "Eltern"
    groups: [eltern]
    users: [opa]
    smtp_recipients: [scan-eltern@example.test]
"""

USERS = {
    "anna": ["familie-a"],
    "opa": [],
    "berta": ["familie-a"],
    "carl": ["familie-a"],
    "zoe": ["sonstwer"],
}


def _setup(env, data_dir, monkeypatch, *, text: str = FILE_TEXT, **overrides):
    path = data_dir / "config" / "households.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    provider = FakeProvider()
    settings = load_settings(env).with_overrides(**provider.settings(), **overrides)
    monkeypatch.setattr(routes_ui, "_ocr_info", lambda s: routes_ui._OcrInfo(False, None, None, [], []))
    registry = Registry(settings)
    app = create_app(settings, registry=registry)
    return SimpleNamespace(app=app, registry=registry, provider=provider, path=path, settings=settings)


@pytest.fixture
def setup(env, data_dir, monkeypatch):
    data = _setup(env, data_dir, monkeypatch)
    yield data
    data.registry.stop()
    data.registry.close()


def sso(setup, name: str, *, expect_ok: bool = True):
    client = client_for(setup.app)
    setup.provider.claims.update(
        {
            "sub": f"user-{name}",
            "preferred_username": name,
            "email": f"{name}@example.test",
            "name": name.title(),
            "groups": USERS[name],
        }
    )
    with setup.provider.running():
        response = oidc_login(client, setup.provider)
    assert response.status_code == 303
    if expect_ok:
        assert "error=" not in response.headers["location"], response.headers["location"]
    return client, response


def admin(setup):
    client = client_for(setup.app)
    assert login_local(client).status_code == 303
    return client


def csrf(client) -> str:
    return session_data(client)["csrf"]


def add(client, household: str, user: str):
    return client.post(
        f"/einstellungen/haushalte/{household}/mitglieder",
        data={"csrf_token": csrf(client), "user": user},
        follow_redirects=False,
    )


def remove(client, household: str, user: str):
    return client.post(
        f"/einstellungen/haushalte/{household}/mitglieder/entfernen",
        data={"csrf_token": csrf(client), "user": user},
        follow_redirects=False,
    )


def file_data(setup):
    return yaml.safe_load(setup.path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Who may manage members


def test_admins_see_the_section_and_others_do_not(setup):
    page = admin(setup).get("/einstellungen").text
    assert 'id="haushalte"' in page
    assert "/einstellungen/haushalte/familie-a/mitglieder" in page
    assert "/einstellungen/haushalte/eltern/mitglieder/entfernen" in page
    assert 'name="user" value="opa"' in page
    assert "Gruppen kommen aus dem Anmeldedienst" in page and "Kommentare in der Datei gehen" in page

    berta, _ = sso(setup, "berta")  # listed as "Berta" under admins
    assert 'id="haushalte"' in berta.get("/einstellungen").text

    anna, _ = sso(setup, "anna")
    page = anna.get("/einstellungen").text
    assert 'id="haushalte"' not in page and "/mitglieder" not in page


def test_non_admins_get_403_and_nothing_changes(setup):
    before = setup.path.read_text(encoding="utf-8")
    anna, _ = sso(setup, "anna")
    response = add(anna, "eltern", "anna")
    assert response.status_code == 403
    assert remove(anna, "eltern", "opa").status_code == 403
    assert setup.path.read_text(encoding="utf-8") == before
    assert not setup.path.with_name("households.yaml.bak").exists()
    assert [h.users for h in setup.registry.households] == [(), ("opa",)]


def test_admin_requests_need_the_csrf_token(setup):
    client = admin(setup)
    response = client.post("/einstellungen/haushalte/eltern/mitglieder", data={"user": "x"}, follow_redirects=False)
    assert response.status_code == 403
    assert setup.registry.get("eltern").users == ("opa",)


def test_admins_from_the_environment(env, data_dir, monkeypatch):
    env["DOCUSHELF_ADMIN_USERS"] = "nobody, CARL@example.test"
    data = _setup(env, data_dir, monkeypatch)
    try:
        assert data.settings.admin_users == ("nobody", "carl@example.test")
        carl, _ = sso(data, "carl")  # matched by e-mail address
        assert 'id="haushalte"' in carl.get("/einstellungen").text
        assert add(carl, "eltern", "carl").status_code == 303
        assert data.registry.get("eltern").users == ("opa", "carl")
    finally:
        data.registry.stop()
        data.registry.close()


def test_a_token_cannot_manage_members(setup):
    token, _entry = setup.registry.services("familie-a").tokens.create("Skript", "admin")
    client = client_for(setup.app)
    response = client.post(
        "/einstellungen/haushalte/eltern/mitglieder",
        data={"user": "x"},
        headers={"Authorization": f"Bearer {token}"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert setup.registry.get("eltern").users == ("opa",)


def test_single_household_hides_the_section(services):
    from web_support import make_app

    client = client_for(make_app(services))
    assert login_local(client).status_code == 303
    assert 'id="haushalte"' not in client.get("/einstellungen").text
    response = add(client, "default", "someone")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Changing members


def test_adding_a_member_writes_the_file_and_a_backup(setup):
    before = setup.path.read_text(encoding="utf-8")
    client = admin(setup)
    response = add(client, "eltern", "  Zoe@Example.test  ")
    assert response.status_code == 303 and response.headers["location"] == "/einstellungen#haushalte"
    page = client.get("/einstellungen").text
    assert "„Zoe@Example.test“ gehört jetzt zum Haushalt „Eltern“." in page

    data = file_data(setup)
    assert list(data) == ["admins", "households"] and data["admins"] == ["Berta"]
    assert [h["id"] for h in data["households"]] == ["familie-a", "eltern"]
    assert data["households"][1]["users"] == ["opa", "Zoe@Example.test"]
    assert data["households"][1]["smtp_recipients"] == ["scan-eltern@example.test"]
    assert data["households"][0]["owner_names"] == ["Erika Mustermann"]
    assert data["households"][0]["path"] == "."
    assert setup.path.with_name("households.yaml.bak").read_text(encoding="utf-8") == before

    assert setup.registry.get("eltern").users == ("opa", "zoe@example.test")
    entry = setup.registry.services("eltern").audit.tail(1)[0]
    assert entry["action"] == "household_members" and entry["added"] == "Zoe@Example.test"
    assert entry["user"] == "admin"
    assert not any(e["action"] == "household_members" for e in setup.registry.services("familie-a").audit.tail(20))


def test_removing_a_member(setup):
    client = admin(setup)
    assert remove(client, "eltern", "OPA").status_code == 303
    assert "users" in file_data(setup)["households"][1]
    assert file_data(setup)["households"][1]["users"] == []
    assert setup.registry.get("eltern").users == ()
    entry = setup.registry.services("eltern").audit.tail(1)[0]
    assert entry["action"] == "household_members" and entry["removed"] == "OPA"
    assert "„OPA“ wurde aus dem Haushalt „Eltern“ entfernt." in client.get("/einstellungen").text


@pytest.mark.parametrize(
    "value, message",
    [
        ("", "Bitte einen Benutzernamen"),
        ("   ", "Bitte einen Benutzernamen"),
        ("x" * 201, "zu lang"),
        ("a\nb", "Steuerzeichen"),
        ("OPA", "ist schon Mitglied"),
    ],
)
def test_invalid_or_duplicate_input_changes_nothing(setup, value, message):
    before = setup.path.read_text(encoding="utf-8")
    client = admin(setup)
    response = add(client, "eltern", value)
    assert response.status_code == 303
    assert message in client.get("/einstellungen").text
    assert setup.path.read_text(encoding="utf-8") == before
    assert not setup.path.with_name("households.yaml.bak").exists()
    assert setup.registry.get("eltern").users == ("opa",)


def test_a_member_may_be_in_several_households_and_200_chars_are_fine(setup):
    client = admin(setup)
    assert add(client, "familie-a", "opa").status_code == 303
    long_name = "y" * 200
    assert add(client, "eltern", long_name).status_code == 303
    assert setup.registry.get("familie-a").users == ("opa",)
    assert setup.registry.get("eltern").users == ("opa", long_name)


def test_removing_someone_not_listed_and_unknown_households(setup):
    client = admin(setup)
    assert remove(client, "eltern", "niemand").status_code == 303
    assert "steht nicht (mehr) bei den Benutzern" in client.get("/einstellungen").text
    assert add(client, "gibtsnicht", "x").status_code == 303
    assert "gibt es nicht" in client.get("/einstellungen").text
    assert not setup.path.with_name("households.yaml.bak").exists()


def test_an_invalid_file_is_not_replaced(setup):
    broken = FILE_TEXT.replace("    groups: [eltern]\n", "    grups: [eltern]\n")
    setup.path.write_text(broken, encoding="utf-8")
    client = admin(setup)
    assert add(client, "eltern", "zoe").status_code == 303
    page = client.get("/einstellungen").text
    assert "derzeit ungültig" in page and "grups" in page
    assert setup.path.read_text(encoding="utf-8") == broken
    assert not setup.path.with_name("households.yaml.bak").exists()
    assert setup.registry.get("eltern").users == ("opa",)


def test_a_result_that_fails_validation_is_not_written(setup, monkeypatch):
    before = setup.path.read_text(encoding="utf-8")
    original = households_module.parse_households
    calls = []

    def failing_second_time(raw, settings, *, env):
        calls.append(1)
        if len(calls) == 2:
            raise ConfigError("households.yaml: geprüft und abgelehnt.")
        return original(raw, settings, env=env)

    monkeypatch.setattr(households_module, "parse_households", failing_second_time)
    client = admin(setup)
    assert add(client, "eltern", "zoe").status_code == 303
    assert "geprüft und abgelehnt" in client.get("/einstellungen").text
    assert len(calls) == 2
    assert setup.path.read_text(encoding="utf-8") == before
    assert not setup.path.with_name("households.yaml.bak").exists()
    assert setup.registry.get("eltern").users == ("opa",)


def test_other_changes_to_the_file_survive_and_households_must_stay_the_same(setup):
    client = admin(setup)
    added = FILE_TEXT + "  - id: neu\n    name: Neu\n"
    setup.path.write_text(added, encoding="utf-8")
    assert add(client, "eltern", "zoe").status_code == 303
    assert "neu starten" in client.get("/einstellungen").text
    assert setup.path.read_text(encoding="utf-8") == added

    edited = FILE_TEXT.replace("groups: [eltern]", "groups: [eltern, enkel]")
    setup.path.write_text(edited, encoding="utf-8")
    assert add(client, "eltern", "zoe").status_code == 303
    assert setup.registry.get("eltern").groups == ("eltern", "enkel")
    assert file_data(setup)["households"][1]["groups"] == ["eltern", "enkel"]


# ---------------------------------------------------------------------------
# Effect without restart


def test_an_added_user_can_log_in_and_switch_at_once(setup):
    _client, response = sso(setup, "zoe", expect_ok=False)
    assert "error=denied" in response.headers["location"]

    anna, _ = sso(setup, "anna")
    assert 'action="/haushalt"' not in anna.get("/eingang").text
    response = anna.post("/haushalt", data={"csrf_token": csrf(anna), "id": "eltern"}, follow_redirects=False)
    assert response.status_code == 404

    client = admin(setup)
    assert add(client, "eltern", "zoe@example.test").status_code == 303
    assert add(client, "eltern", "Anna").status_code == 303

    zoe, _ = sso(setup, "zoe")
    assert session_data(zoe)["household"] == "eltern"
    assert zoe.get("/eingang").status_code == 200

    assert 'action="/haushalt"' in anna.get("/eingang").text
    response = anna.post("/haushalt", data={"csrf_token": csrf(anna), "id": "eltern"}, follow_redirects=False)
    assert response.status_code == 303 and session_data(anna)["household"] == "eltern"


def test_a_removed_user_loses_access_with_the_next_request(setup):
    opa, _ = sso(setup, "opa")
    assert opa.get("/eingang").status_code == 200
    assert remove(admin(setup), "eltern", "opa").status_code == 303
    response = opa.get("/eingang", follow_redirects=False)
    assert response.status_code == 403 and "Kein Haushalt freigegeben" in response.text
    _client, response = sso(setup, "opa", expect_ok=False)
    assert "error=denied" in response.headers["location"]


def test_a_removed_member_of_two_households_falls_back(setup):
    client = admin(setup)
    assert add(client, "eltern", "anna").status_code == 303
    anna, _ = sso(setup, "anna")
    response = anna.post("/haushalt", data={"csrf_token": csrf(anna), "id": "eltern"}, follow_redirects=False)
    assert response.status_code == 303
    assert remove(client, "eltern", "anna").status_code == 303
    assert anna.get("/eingang").status_code == 200
    assert session_data(anna)["household"] == "familie-a"


def test_an_existing_session_becomes_admin_when_listed(setup):
    session = {
        "user": {"username": "anna", "name": "Anna", "email": None, "groups": ["familie-a"], "method": "oidc"},
        "auth_at": int(time.time()),
        "csrf": "x" * 32,
    }
    anna = client_for(setup.app)
    anna.cookies.set(SESSION_COOKIE, signed_session(setup.app.state.session_secret, session), domain="testserver.local")
    assert 'id="haushalte"' not in anna.get("/einstellungen").text
    text = setup.path.read_text(encoding="utf-8").replace("admins: [Berta]", "admins: [Berta, anna]")
    setup.path.write_text(text, encoding="utf-8")
    assert add(admin(setup), "eltern", "zoe").status_code == 303  # reloads the admins as well
    assert 'id="haushalte"' in anna.get("/einstellungen").text


# ---------------------------------------------------------------------------
# The admins entry


def test_admins_entry_is_validated(env, data_dir):
    path = data_dir / "config" / "households.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    settings = load_settings(env)
    path.write_text(FILE_TEXT.replace("admins: [Berta]", "admins: {berta: true}"), encoding="utf-8")
    with pytest.raises(ConfigError, match="admins muss eine Liste sein"):
        households_module.load_households(settings)
    path.write_text(FILE_TEXT.replace("admins: [Berta]", "admin: [Berta]"), encoding="utf-8")
    with pytest.raises(ConfigError, match="unbekannter Eintrag „admin“"):
        households_module.load_households(settings)


def test_is_admin(setup):
    registry = setup.registry

    def user(name, method="oidc", email=None):
        return SimpleNamespace(username=name, email=email, method=method, groups=())

    assert registry.admins == ("berta",)
    assert registry.is_admin(user("admin", "local"))
    assert registry.is_admin(user("BERTA"))
    assert registry.is_admin(user("x", email="Berta"))
    assert not registry.is_admin(user("berta", "token"))
    assert not registry.is_admin(user("anna"))
    assert not registry.is_admin(None)


def test_the_file_keeps_its_permissions(setup):
    setup.path.chmod(0o600)
    assert add(admin(setup), "eltern", "zoe").status_code == 303
    assert setup.path.stat().st_mode & 0o777 == 0o600
    assert setup.path.with_name("households.yaml.bak").stat().st_mode & 0o777 == 0o600
