"""Households core: households.yaml, settings per household, registry, OCR slots, CLI selection."""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from docushelf import cli
from docushelf.config import load_settings
from docushelf.errors import ConfigError, NotFound
from docushelf.households import (
    Household,
    Registry,
    default_household,
    load_households,
    settings_for,
)
from docushelf.pdf import ocr
from docushelf.services import Services
from docushelf.web.security import User


def write_households(data_dir: Path, households) -> Path:
    path = data_dir / "config" / "households.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = households if isinstance(households, str) else yaml.safe_dump({"households": households})
    path.write_text(content, encoding="utf-8")
    return path


TWO = [
    {"id": "familie-a", "name": "Familie A", "path": ".", "groups": ["familie-a"],
     "smtp_recipients": ["scan-a@docushelf.local"]},
    {"id": "eltern", "name": "Eltern", "groups": ["docushelf-eltern"], "users": ["Opa@Example.org"],
     "smtp_recipients": ["Scan <SCAN-Eltern@docushelf.local>"]},
]


@pytest.fixture
def two(env, data_dir):
    write_households(data_dir, TWO)
    return load_settings(env)


def oidc(username="x", email=None, groups=()):
    return User(username=username, name=username, email=email, groups=tuple(groups), method="oidc")


def local():
    return User(username="admin", name="admin", method="local")


# -- single household (no households.yaml) -------------------------------------------------------------


def test_fallback_is_one_default_household_in_the_data_root(settings, env):
    households = load_households(settings)
    assert len(households) == 1
    household = households[0]
    assert (household.id, household.name, household.path) == ("default", "Zuhause", settings.data_dir)
    assert settings_for(settings, household) is settings
    named = load_settings({**env, "DOCUSHELF_HOUSEHOLD_NAME": "Familie Muster"})
    assert load_households(named)[0].name == "Familie Muster"


def test_fallback_registry_behaves_like_before(env):
    settings = load_settings({**env, "DOCUSHELF_ALLOWED_GROUPS": "familie", "DOCUSHELF_ALLOWED_USERS": "Anna"})
    registry = Registry(settings)
    try:
        assert registry.default is registry.households[0] and not registry.multi
        services = registry.services("default")
        assert services is registry.services("default")
        assert services.settings is settings and services.household.id == "default"
        assert services.layout.data == settings.data_dir
        with pytest.raises(NotFound):
            registry.services("eltern")
        # the classic DOCUSHELF_ALLOWED_* rules decide
        assert registry.for_user(oidc("anna")) == [registry.default]
        assert registry.for_user(oidc("bob", groups=["familie"])) == [registry.default]
        assert registry.for_user(oidc("bob", groups=["andere"])) == []
        assert registry.for_user(local()) == [registry.default]
        assert registry.for_user(None) == []
        # every SMTP recipient belongs to the only household
        assert registry.by_smtp_recipient("irgendwer@example.org") is registry.default
    finally:
        registry.close()


def test_services_without_household_get_the_default_one(settings):
    services = Services(settings)
    try:
        assert services.household == default_household(settings)
    finally:
        services.index.close()


# -- parsing ----------------------------------------------------------------------------------------------


def test_full_example_is_parsed(env, data_dir, tmp_path):
    secret = tmp_path / "imap-secret"
    secret.write_text("geheim-a\n", encoding="utf-8")
    write_households(data_dir, [
        {
            "id": "familie-a",
            "name": "Familie A",
            "path": ".",
            "groups": ["familie-a"],
            "users": [],
            "owner_names": ["Erika Mustermann"],
            "notify_url": None,
            "auto_file": {"mode": "immer", "threshold": "80 %"},
            "imap": {"host": "imap.example.org", "user": "dokumente@example.org", "password_file": str(secret),
                     "folder": "INBOX", "allowed_senders": ["Erika@Example.org"], "port": 143, "ssl": False},
            "smtp_recipients": ["scan-familie-a@docushelf.local"],
            "watch_folders": {"nas": "/watch/familie-a"},
        },
        {"id": "eltern", "name": "Eltern", "groups": "docushelf-eltern"},
    ])
    first, second = load_households(load_settings(env))
    assert first.path == data_dir
    assert first.owner_names == ("Erika Mustermann",) and first.notify_url is None
    assert first.auto_file == {"mode": "always", "threshold": 0.8}
    assert first.imap["password"] == "geheim-a" and first.imap["allowed_senders"] == ("erika@example.org",)
    assert first.watch_folders == {"nas": Path("/watch/familie-a")}
    assert second.path == data_dir / "haushalte" / "eltern"
    assert second.groups == ("docushelf-eltern",) and second.owner_names is None and second.imap is None
    assert "geheim" not in repr(first)


@pytest.mark.parametrize(
    "households, message",
    [
        ("- id: a\n", "households:"),
        ({"households": []}, "mindestens einem Haushalt"),
        ({"households": [{"id": "a"}], "haushalte": []}, "unbekannter Eintrag „haushalte“"),
        ([{"id": "a", "grups": ["x"]}], "unbekannter Eintrag „grups“"),
        ([{"name": "ohne id"}], "„id“ fehlt"),
        ([{"id": "Familie A"}], "id ist ungültig"),
        ([{"id": "-a"}], "id ist ungültig"),
        ([{"id": "a" * 33}], "id ist ungültig"),
        ([{"id": "a"}, {"id": "a"}], "doppelt"),
        ([{"id": "a", "path": "x"}, {"id": "b", "path": "./x/"}], "dasselbe Verzeichnis"),
        ([{"id": "a", "path": "."}, {"id": "b", "path": "x/.."}], "nur ein Haushalt"),
        ([{"id": "a", "path": "/srv/a"}], "relativ"),
        ([{"id": "a", "path": "../a"}], "hinausführen"),
        ([{"id": "a", "path": "a"}, {"id": "b", "path": "a/archiv/b"}], "„archiv“ des Haushalts „a“"),
        ([{"id": "a", "path": "a"}, {"id": "b", "path": "a/eingang"}], "„eingang“"),
        ([{"id": "a", "path": "index/a"}], "des Datenverzeichnisses"),
        ([{"id": "a", "path": "."}, {"id": "b", "path": "papierkorb/b"}], "„papierkorb“"),
        ([{"id": "a", "groups": {"x": 1}}], "Liste"),
        ([{"id": "a", "imap": {"user": "u", "password": "p"}}], "imap.host fehlt"),
        ([{"id": "a", "imap": {"host": "h", "user": "u"}}], "genau eine"),
        ([{"id": "a", "imap": {"host": "h", "user": "u", "password": "p", "password_env": "X"}}], "genau eine"),
        ([{"id": "a", "imap": {"host": "h", "user": "u", "password": "p", "pasword": "q"}}], "„pasword“"),
        ([{"id": "a", "imap": {"host": "h", "user": "u", "password_env": "DOCUSHELF_TEST_NOT_SET"}}],
         "DOCUSHELF_TEST_NOT_SET"),
        ([{"id": "a", "imap": {"host": "h", "user": "u", "password_file": "/nicht/da"}}], "/nicht/da"),
        ([{"id": "a", "imap": {"host": "h", "user": "u", "password": "p", "port": 0}}], "imap.port"),
        ([{"id": "a", "auto_file": {"mode": "manchmal"}}], "auto_file.mode"),
        ([{"id": "a", "auto_file": {"modus": "always"}}], "„modus“"),
        ([{"id": "a", "auto_file": {"threshold": 2}}], "auto_file.threshold"),
        ([{"id": "a", "watch_folders": {"nas": "relativ/pfad"}}], "absoluter Pfad"),
        ([{"id": "a", "watch_folders": ["nas=/x"]}], "watch_folders"),
        ([{"id": "a", "smtp_recipients": ["kein-at"]}], "keine E-Mail-Adresse"),
        ([{"id": "a", "smtp_recipients": ["s@x.de"]}, {"id": "b", "smtp_recipients": ["S@X.de"]}], "s@x.de"),
    ],
)
def test_invalid_households_are_rejected(env, data_dir, households, message):
    write_households(data_dir, households if not isinstance(households, dict) else yaml.safe_dump(households))
    with pytest.raises(ConfigError) as raised:
        load_households(load_settings(env))
    assert message in str(raised.value)
    assert "households.yaml" in str(raised.value)


def test_secrets_never_appear_in_errors_or_logs(env, data_dir, monkeypatch, caplog):
    monkeypatch.setenv("FAMILIE_A_IMAP", "streng-geheim")
    write_households(data_dir, [
        {"id": "a", "path": ".", "imap": {"host": "h", "user": "u", "password_env": "FAMILIE_A_IMAP"}},
        {"id": "b", "imap": {"host": "h", "user": "u", "password": "auch-geheim", "port": "kaputt"}},
    ])
    with pytest.raises(ConfigError) as raised:
        load_households(load_settings(env))
    assert "auch-geheim" not in str(raised.value)
    write_households(data_dir, [
        {"id": "a", "path": ".", "imap": {"host": "h", "user": "u", "password_env": "FAMILIE_A_IMAP"}},
    ])
    caplog.set_level("DEBUG")
    registry = Registry(load_settings(env))
    try:
        household = registry.get("a")
        assert registry.settings_for("a").imap.password == "streng-geheim"
        assert "streng-geheim" not in repr(household)
        assert "streng-geheim" not in repr(registry.settings_for("a"))
        registry.services("a")
    finally:
        registry.close()
    assert "streng-geheim" not in caplog.text


# -- settings per household ---------------------------------------------------------------------------------


def test_settings_for_overrides(env, data_dir, monkeypatch):
    monkeypatch.setenv("ELTERN_IMAP", "pw-eltern")
    env = {
        **env,
        "DOCUSHELF_OWNER_NAMES": "Erika Muster",
        "DOCUSHELF_NOTIFY_URL": "https://ntfy.example/global",
        "DOCUSHELF_AUTO_FILE": "rules",
        "DOCUSHELF_AUTO_FILE_SOURCES": "upload,smb",
        "DOCUSHELF_IMAP_ENABLED": "true",
        "DOCUSHELF_IMAP_HOST": "imap.global",
        "DOCUSHELF_IMAP_USER": "global",
        "DOCUSHELF_IMAP_PASSWORD": "pw-global",
        "DOCUSHELF_WATCH_FOLDERS": "scanner=/watch/global",
        "DOCUSHELF_SMTP_ENABLED": "true",
    }
    write_households(data_dir, [
        {"id": "familie-a", "path": "."},
        {"id": "eltern", "name": "Eltern", "owner_names": ["Opa Muster", "Oma Muster"],
         "notify_url": "https://ntfy.example/eltern", "auto_file": {"mode": "always", "threshold": 0.9},
         "imap": {"host": "imap.eltern", "user": "eltern", "password_env": "ELTERN_IMAP", "poll_seconds": 300},
         "watch_folders": {"nas": "/watch/eltern"}},
    ])
    settings = load_settings(env)
    assert settings.smtp is not None and settings.smtp.recipient == ""  # recipients come from households.yaml
    root_household, eltern = load_households(settings)

    root = settings_for(settings, root_household)
    assert root.data_dir == data_dir and root.archive_dir == data_dir / "archiv"
    assert root.imap.host == "imap.global" and root.smtp is None
    assert [s.name for s in root.sources if s.path is not None] == ["watch-scanner"]
    assert root.owner_names == ("Erika Muster",) and root.auto_file.mode == "rules"
    assert root.root_dir == data_dir and root.data_root is None

    other = settings_for(settings, eltern)
    assert other.data_dir == data_dir / "haushalte" / "eltern"
    assert other.archive_dir == data_dir / "haushalte" / "eltern" / "archiv"
    assert other.owner_names == ("Opa Muster", "Oma Muster")
    assert other.notify_url == "https://ntfy.example/eltern"
    assert (other.auto_file.mode, other.auto_file.threshold) == ("always", 0.9)
    assert other.auto_file.sources == ("upload", "smb")  # not overridden: global value
    assert (other.imap.host, other.imap.username, other.imap.password) == ("imap.eltern", "eltern", "pw-eltern")
    assert other.imap.poll_seconds == 300 and other.imap.folder == "INBOX"
    assert other.smtp is None
    watch = [s for s in other.sources if s.path is not None]
    assert [(s.name, s.path) for s in watch] == [("watch-nas", Path("/watch/eltern"))]
    assert {s.name for s in other.sources if s.path is None} == {"upload", "smb", "ftp", "mail", "smtp"}
    assert other.root_dir == data_dir and other.household_name == "Eltern"


def test_separate_archive_dir_only_counts_with_one_household(env, data_dir, tmp_path):
    archive = tmp_path / "nas" / "archiv"
    env = {**env, "DOCUSHELF_ARCHIVE_DIR": str(archive)}
    write_households(data_dir, [{"id": "a", "path": "."}])
    settings = load_settings(env)
    assert settings_for(settings, load_households(settings)[0]).archive_dir == archive
    write_households(data_dir, [{"id": "a", "path": "."}, {"id": "b"}])
    settings = load_settings(env)
    first, second = load_households(settings)
    assert settings_for(settings, first).archive_dir == data_dir / "archiv"
    assert settings_for(settings, second).archive_dir == data_dir / "haushalte" / "b" / "archiv"


# -- registry ------------------------------------------------------------------------------------------------


def test_registry_with_two_households(two, data_dir):
    registry = Registry(two)
    try:
        assert registry.multi and registry.default is None
        assert [h.id for h in registry.households] == ["familie-a", "eltern"]
        a, b = registry.services("familie-a"), registry.services("eltern")
        assert registry.services("familie-a") is a
        assert a.household.id == "familie-a" and b.household.name == "Eltern"
        assert a.layout.data == data_dir and b.layout.data == data_dir / "haushalte" / "eltern"
        assert a.layout.tokens_file != b.layout.tokens_file
        assert (data_dir / "haushalte" / "eltern" / "eingang" / "quellen" / "smtp").is_dir()
        assert a.smtp is None and b.smtp is None
        with pytest.raises(NotFound):
            registry.services("fremd")
        with pytest.raises(NotFound):
            registry.get("fremd")
    finally:
        registry.close()


def test_registry_membership(two):
    registry = Registry(two)
    try:
        ids = lambda households: [h.id for h in households]  # noqa: E731
        assert ids(registry.for_user(oidc("anna", groups=["Familie-A"]))) == ["familie-a"]
        assert ids(registry.for_user(oidc("opa"))) == []
        assert ids(registry.for_user(oidc("x", email="opa@example.org"))) == ["eltern"]
        assert ids(registry.for_user(oidc("opa@example.org", groups=["familie-a"]))) == ["familie-a", "eltern"]
        assert ids(registry.for_user(oidc("niemand", groups=["andere"]))) == []
        assert ids(registry.for_user(local())) == ["familie-a", "eltern"]
        assert registry.for_user(User(username="t", name="t", method="token")) == []
    finally:
        registry.close()


def test_smtp_recipient_lookup(two):
    from docushelf.channels.smtp import registry_resolver

    registry = Registry(two)
    try:
        assert registry.by_smtp_recipient("scan-a@docushelf.local").id == "familie-a"
        assert registry.by_smtp_recipient("Scanner <SCAN-ELTERN@docushelf.local>").id == "eltern"
        assert registry.by_smtp_recipient("unbekannt@docushelf.local") is None
        assert registry.by_smtp_recipient("") is None
        resolve = registry_resolver(registry, "scan@docushelf.local")
        assert resolve("scan-eltern@docushelf.local") is registry.services("eltern")
        # the global recipient keeps working for the household in the data root
        assert resolve("scan@docushelf.local") is registry.services("familie-a")
        assert resolve("fremd@example.org") is None
    finally:
        registry.close()


class FakeServices:
    events: list[str] = []
    fail_start: set[str] = set()

    def __init__(self, settings, *, household):
        self.settings = settings
        self.household = household
        self.layout = SimpleNamespace(archive=Path("/x"), inbox=Path("/x"), trash=Path("/x"),
                                      config=Path("/x"), audit=Path("/x"), index=Path("/x"))
        self.intake = SimpleNamespace(protected_paths=())
        self.index = SimpleNamespace(close=lambda: self.events.append(f"close {household.id}"))

    def recover(self):
        self.events.append(f"recover {self.household.id}")

    def start(self):
        if self.household.id in self.fail_start:
            raise RuntimeError("kaputt")
        self.events.append(f"start {self.household.id}")

    def stop(self):
        self.events.append(f"stop {self.household.id}")


def test_registry_lifecycle_with_fake_services(two):
    FakeServices.events = []
    FakeServices.fail_start = set()
    registry = Registry(two, services_factory=FakeServices)
    registry.recover()
    registry.start()
    registry.start()  # idempotent
    registry.stop()
    assert FakeServices.events == [
        "recover familie-a", "recover eltern", "start familie-a", "start eltern", "stop eltern", "stop familie-a",
    ]
    FakeServices.events = []
    FakeServices.fail_start = {"eltern"}
    registry = Registry(two, services_factory=FakeServices)
    with pytest.raises(RuntimeError):
        registry.start()
    assert FakeServices.events == ["start familie-a", "stop familie-a"]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_registry_starts_and_stops_everything(env, data_dir):
    write_households(data_dir, TWO)
    settings = load_settings({
        **env,
        "DOCUSHELF_SMTP_ENABLED": "true",
        "DOCUSHELF_SMTP_HOST": "127.0.0.1",
        "DOCUSHELF_SMTP_PORT": str(free_port()),
        "DOCUSHELF_SMTP_ALLOWED_NETWORKS": "127.0.0.1/32",
    })
    registry = Registry(settings)
    registry.start()
    try:
        assert registry.smtp is not None and registry.smtp.running and registry.smtp.multi_household
        a, b = registry.services("familie-a"), registry.services("eltern")
        assert a._started and b._started
        assert a.smtp is None and b.smtp is None
        # watch folders of one household must stay out of the other's data
        assert b.layout.archive in a.intake.protected_paths
        assert a.layout.archive in b.intake.protected_paths
    finally:
        registry.stop()
    assert registry.smtp is None
    assert not registry.services("familie-a")._started and not registry.services("eltern")._started


def test_single_household_keeps_its_own_smtp_receiver(env):
    settings = load_settings({**env, "DOCUSHELF_SMTP_ENABLED": "true", "DOCUSHELF_SMTP_RECIPIENT": "scan@x.de"})
    registry = Registry(settings)
    try:
        assert registry.services("default").smtp is not None
        assert registry.smtp is None
    finally:
        registry.close()


# -- OCR slots ----------------------------------------------------------------------------------------------


def test_ocr_parallel_setting(env):
    assert load_settings(env).ocr_parallel == 1
    assert load_settings({**env, "DOCUSHELF_OCR_PARALLEL": "3"}).ocr_parallel == 3
    with pytest.raises(ConfigError, match="OCR_PARALLEL"):
        load_settings({**env, "DOCUSHELF_OCR_PARALLEL": "0"})


def test_ocr_run_holds_a_process_wide_slot(settings, tmp_path, monkeypatch):
    src = tmp_path / "scan.pdf"
    src.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(ocr, "_ocrmypdf_command", lambda: ["ocrmypdf"])
    monkeypatch.setattr(ocr, "_tesseract_command", lambda: ["tesseract"])
    monkeypatch.setattr(ocr, "_inspect", lambda path: None)
    seen = []

    def run(*args, **kwargs):
        seen.append(ocr.OCR_SLOTS.acquire(blocking=False))
        return "result"

    monkeypatch.setattr(ocr, "_run_with_slot", run)
    try:
        ocr.configure_ocr_slots(1)
        assert ocr.run_ocr(src, tmp_path / "out.pdf", settings.with_overrides(ocr_enabled=True)) == "result"
        assert seen == [False]  # the only slot was taken by the run itself
        assert ocr.OCR_SLOTS.acquire(blocking=False)  # and released afterwards
        ocr.OCR_SLOTS.release()

        # a second run waits until the first one is done
        ocr.OCR_SLOTS.acquire()
        lines: list[str] = []
        done = threading.Event()
        thread = threading.Thread(
            target=lambda: (ocr.run_ocr(src, tmp_path / "out2.pdf", settings.with_overrides(ocr_enabled=True),
                                        log=lines.append), done.set()),
            daemon=True,
        )
        thread.start()
        assert not done.wait(0.3)
        assert any("freien Platz" in line for line in lines)
        ocr.OCR_SLOTS.release()
        assert done.wait(5)

        ocr.configure_ocr_slots(2)
        assert ocr.OCR_SLOTS.acquire(blocking=False) and ocr.OCR_SLOTS.acquire(blocking=False)
        ocr.OCR_SLOTS.release()
        ocr.OCR_SLOTS.release()
    finally:
        ocr.configure_ocr_slots(1)


# -- command line ---------------------------------------------------------------------------------------------


@pytest.fixture
def cli_env(env, monkeypatch):
    for key in list(os.environ):
        if key.startswith("DOCUSHELF_"):
            monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_cli_single_household_needs_no_option(cli_env, data_dir, capsys):
    code, out, err = run(capsys, "status", "--json")
    assert code == 0 and "household" not in json.loads(out)
    code, out, _ = run(capsys, "--haushalt", "default", "status")
    assert code == 0 and "Haushalt" not in out
    code, _, err = run(capsys, "--haushalt", "eltern", "status")
    assert code == cli.EXIT_ERROR and "„eltern“ gibt es nicht" in err and "default" in err
    code, out, err = run(capsys, "households")
    assert code == 0 and "default" in out and "Zuhause" in out and "keine config/households.yaml" in err


def test_cli_several_households_require_the_option(cli_env, data_dir, capsys):
    write_households(data_dir, TWO)
    for argv in (["status"], ["token", "list"], ["reconcile"], ["rebuild"], ["process", "20261123-125100-a1b2c3"]):
        code, _, err = run(capsys, *argv)
        assert code == cli.EXIT_ERROR, argv
        assert "mehrere Haushalte (familie-a, eltern)" in err and "--haushalt" in err, argv
    code, _, err = run(capsys, "--haushalt", "fremd", "status")
    assert code == cli.EXIT_ERROR and "Vorhandene Haushalte: familie-a, eltern" in err

    code, out, _ = run(capsys, "--haushalt", "eltern", "status", "--json")
    data = json.loads(out)
    assert code == 0 and data["household"] == {"id": "eltern", "name": "Eltern"}
    assert data["data_dir"] == str(data_dir / "haushalte" / "eltern")
    code, out, _ = run(capsys, "status", "--household", "familie-a")
    assert code == 0 and "Haushalt" in out and "Familie A (familie-a)" in out


def test_cli_households_command(cli_env, data_dir, capsys):
    write_households(data_dir, TWO)
    registry = Registry(load_settings(cli_env))
    for household in registry.households:
        registry.services(household.id)  # creates the data directories and indexes
    registry.close()
    for name in ("households", "haushalte"):
        code, out, _ = run(capsys, name)
        assert code == 0
        assert "familie-a" in out and "Familie A" in out and "Gruppen: familie-a" in out
        assert "eltern" in out and "Benutzer: opa@example.org" in out
    code, out, _ = run(capsys, "households", "--json")
    entries = json.loads(out)
    assert [e["id"] for e in entries] == ["familie-a", "eltern"]
    assert entries[1]["path"] == str(data_dir / "haushalte" / "eltern") and entries[1]["documents"] == 0


def test_cli_tokens_belong_to_the_selected_household(cli_env, data_dir, capsys):
    write_households(data_dir, TWO)
    code, out, _ = run(capsys, "--haushalt", "eltern", "token", "create", "iPhone")
    assert code == 0 and out.strip()
    assert (data_dir / "haushalte" / "eltern" / "config" / "tokens.yaml").is_file()
    assert not (data_dir / "config" / "tokens.yaml").exists()
    code, out, _ = run(capsys, "--haushalt", "familie-a", "token", "list")
    assert code == 0 and "Keine API-Tokens" in out
    code, out, _ = run(capsys, "token", "list", "--haushalt", "eltern")
    assert code == 0 and "iPhone" in out


def test_cli_maintenance_respects_the_global_server_lock(cli_env, data_dir, capsys):
    write_households(data_dir, TWO)
    settings = load_settings(cli_env)
    lock = cli.ServerLock(settings.data_dir / "index" / cli.SERVER_LOCK_NAME)
    assert lock.acquire({"pid": 4242}, attempts=1)
    try:
        code, _, err = run(capsys, "--haushalt", "eltern", "rebuild")
    finally:
        lock.release()
    assert code == cli.EXIT_ERROR and "PID 4242" in err


def test_cli_rebuild_acts_on_the_selected_household(cli_env, data_dir, capsys, monkeypatch):
    write_households(data_dir, TWO)
    opened = []
    real = cli._open_services

    def spy(settings, **kwargs):
        opened.append((settings.data_dir, kwargs.get("household").id))
        return real(settings, **kwargs)

    monkeypatch.setattr(cli, "_open_services", spy)
    code, out, _ = run(capsys, "--haushalt", "eltern", "rebuild")
    assert code == 0 and "neu aufgebaut" in out
    assert opened == [(data_dir / "haushalte" / "eltern", "eltern")]


def test_cli_serve_checks_households_first(cli_env, data_dir, capsys, monkeypatch):
    import docushelf.web.app as web_app

    write_households(data_dir, [{"id": "a", "grups": ["x"]}])
    monkeypatch.setattr(web_app, "create_app", lambda settings=None, **kw: pytest.fail("must not start"))
    code, _, err = run(capsys, "serve")
    assert code == cli.EXIT_ERROR and "unbekannter Eintrag „grups“" in err

    write_households(data_dir, TWO)
    created = []
    monkeypatch.setattr(web_app, "create_app", lambda settings=None, **kw: created.append(settings) or "app")
    monkeypatch.setattr(cli, "_uvicorn_server", lambda config: SimpleNamespace(run=lambda: None, started=True))
    code, _, err = run(capsys, "--haushalt", "eltern", "serve")
    assert code == 0 and "gilt nicht für serve" in err
    assert created[0].data_dir == data_dir  # serve works with the global settings
    assert (data_dir / "haushalte" / "eltern" / "archiv").is_dir()


def test_household_hash_uses_id_and_path(tmp_path):
    first = Household(id="a", name="A", path=tmp_path, imap={"password": "x"})
    assert hash(first) == hash(Household(id="a", name="anders", path=tmp_path))
    assert len({first, first}) == 1


def test_global_notify_url_only_reaches_the_root_household(tmp_path):
    from docushelf.config import load_settings
    from docushelf.households import load_households, settings_for

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "households.yaml").write_text(
        "households:\n"
        "  - {id: a, name: A, path: '.', groups: [a]}\n"
        "  - {id: b, name: B, groups: [b]}\n"
        "  - {id: c, name: C, groups: [c], notify_url: 'https://ntfy.example.org/c'}\n",
        encoding="utf-8",
    )
    settings = load_settings(
        {"DOCUSHELF_DATA_DIR": str(tmp_path), "DOCUSHELF_NOTIFY_URL": "https://ntfy.example.org/root"}
    )
    urls = {h.id: settings_for(settings, h).notify_url for h in load_households(settings)}
    assert urls == {"a": "https://ntfy.example.org/root", "b": None, "c": "https://ntfy.example.org/c"}
