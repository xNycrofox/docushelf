"""Tests for the command line interface (docushelf/cli.py).

Collaborators (service container, importer, web app) are replaced by small
fakes where a test is about the command itself; the integration tests at the
end run the commands against the real components on a temporary data
directory, including a real uvicorn server.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from docushelf import cli
from docushelf.layout import Layout
from docushelf.models import ScanStats

ITEM_ID = "20261123-125100-a1b2c3"


# -- fixtures and helpers ------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def restore_logging():
    """The commands configure logging globally; give every test a clean state back."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    names = cli.NOISY_LOGGERS + ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")
    levels = {name: logging.getLogger(name).level for name in names}
    access = logging.getLogger("uvicorn.access")
    access_state = (list(access.filters), list(access.handlers), access.propagate)
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
    root.setLevel(level)
    for name, value in levels.items():
        logging.getLogger(name).setLevel(value)
    access.filters[:], access.handlers[:], access.propagate = access_state
    logging.captureWarnings(False)


@pytest.fixture
def cli_env(env, monkeypatch):
    """The test environment as process environment (the CLI reads ``os.environ``)."""
    for key in list(os.environ):
        if key.startswith("DOCUSHELF_") or key.lower() in ("http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


@pytest.fixture
def data_layout(cli_env) -> Layout:
    from docushelf.config import load_settings

    return Layout.from_settings(load_settings(cli_env))


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def hold_server_lock(layout: Layout, **info) -> cli.ServerLock:
    lock = cli.ServerLock(Path(layout.index) / cli.SERVER_LOCK_NAME)
    assert lock.acquire({"pid": 4242, "port": 8000, **info}, attempts=1)
    return lock


class FakeIndex:
    def __init__(self):
        self.closed = False
        self.enqueued: list[tuple[str, str]] = []
        self.jobs = [SimpleNamespace(id=7, kind="process", key=ITEM_ID, state="queued")]
        self.completed: list[int] = []

    def close(self):
        self.closed = True

    def enqueue(self, kind, key, payload=None, *, delay=0):
        self.enqueued.append((kind, key))
        return True

    def pending_jobs(self, kind=None):
        return [job for job in self.jobs if kind is None or job.kind == kind]

    def complete(self, job_id):
        self.completed.append(job_id)


class FakeServices:
    """Just enough of ``Services`` for the maintenance commands."""

    def __init__(self, stats: ScanStats | None = None, item=None):
        self.index = FakeIndex()
        self.stats = stats or ScanStats(files_seen=3, folders_seen=2, added=3)
        self.rebuild_users: list[str | None] = []
        self.scans: list[dict] = []
        self.stopped = False
        self.item = item
        self.reconciler = SimpleNamespace(scan=self._scan)
        self.inbox = SimpleNamespace(counts=lambda: {"arbeit": 1, "bereit": 0}, find=self._find, save=lambda item: None)
        self.pipeline = FakePipeline(self)

    def start(self):
        raise AssertionError("maintenance commands must not start background services")

    def stop(self):
        self.stopped = True

    def rebuild(self, *, user=None):
        self.rebuild_users.append(user)
        return self.stats

    def _scan(self, *, full=True, verify=False):
        self.scans.append({"full": full, "verify": verify})
        return self.stats

    def _find(self, item_id, **kwargs):
        return self.item if self.item is not None and self.item.id == item_id else None


class FakePipeline:
    def __init__(self, services: FakeServices):
        self.services = services
        self.retried: list[dict] = []
        self.processed: list[str] = []

    def retry(self, item_id, *, skip_blank_removal=None, skip_ocr=None):
        self.retried.append({"id": item_id, "skip_ocr": skip_ocr, "skip_blank_removal": skip_blank_removal})
        self.services.item.state = "arbeit"
        return self.services.item

    def process(self, item_id):
        self.processed.append(item_id)
        item = self.services.item
        item.state = "bereit"
        item.page_count = 2
        item.suggestions = [
            {"folder": "Finanzen/Bank", "title": "Kontoauszug November", "score": 0.82, "reasons": ["Absender Sparkasse"]}
        ]
        return item


class FakeItem(SimpleNamespace):
    @property
    def top_suggestion(self):
        return self.suggestions[0] if self.suggestions else None


def fake_item(tmp_path: Path, state: str = "arbeit") -> FakeItem:
    directory = tmp_path / "item"
    directory.mkdir(exist_ok=True)
    return FakeItem(
        id=ITEM_ID,
        state=state,
        stage="received",
        options={},
        error=None,
        page_count=0,
        suggestions=[],
        duplicate_of=None,
        display_name="scan.pdf",
        log_path=directory / "log.txt",
    )


# -- argument parsing ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv, func, expected",
    [
        (["serve"], "cmd_serve", {"host": None, "port": None, "access_log": False}),
        (
            ["serve", "--host", "127.0.0.1", "--port", "9000", "--access-log"],
            "cmd_serve",
            {"host": "127.0.0.1", "port": 9000, "access_log": True},
        ),
        (["rebuild"], "cmd_rebuild", {"force": False}),
        (["rebuild", "--force"], "cmd_rebuild", {"force": True}),
        (["reconcile"], "cmd_reconcile", {"verify": False}),
        (["reconcile", "--verify"], "cmd_reconcile", {"verify": True}),
        (
            ["import-paperless", "/import", "--storage-path", "Privat", "--storage-path", "-", "--owner", "anna",
             "--mode", "archive", "--dry-run"],
            "cmd_import_paperless",
            {"export_dir": "/import", "storage_paths": ["Privat", "-"], "owners": ["anna"], "mode": "archive",
             "dry_run": True},
        ),
        (["import-paperless", "/import"], "cmd_import_paperless", {"storage_paths": [], "owners": [], "mode": "inbox",
                                                                   "dry_run": False, "quiet": False,
                                                                   "include_deleted": False}),
        (["import-paperless", "/import", "--include-deleted"], "cmd_import_paperless", {"include_deleted": True}),
        (["hash-password"], "cmd_hash_password", {"stdin": False, "env": False}),
        (["hash-password", "--stdin", "--env"], "cmd_hash_password", {"stdin": True, "env": True}),
        (["token", "create", "iPhone Kurzbefehl", "--user", "anna"], "cmd_token_create",
         {"name": "iPhone Kurzbefehl", "user": "anna"}),
        (["token", "create", "Skript"], "cmd_token_create", {"name": "Skript", "user": None}),
        (["token", "list"], "cmd_token_list", {}),
        (["token", "revoke", "3f2a9c1b"], "cmd_token_revoke", {"token_id": "3f2a9c1b"}),
        (["healthcheck"], "cmd_healthcheck", {"url": None, "timeout": 5.0}),
        (["healthcheck", "--url", "http://127.0.0.1:1/healthz", "--timeout", "2,5"], "cmd_healthcheck",
         {"url": "http://127.0.0.1:1/healthz", "timeout": 2.5}),
        (["status"], "cmd_status", {"json": False}),
        (["status", "--json"], "cmd_status", {"json": True}),
        (["process", ITEM_ID], "cmd_process", {"item_id": ITEM_ID, "skip_ocr": False, "skip_blank_removal": False}),
        (["process", ITEM_ID, "--skip-ocr", "--skip-blank-removal"], "cmd_process",
         {"skip_ocr": True, "skip_blank_removal": True}),
        (["--data-dir", "/srv/docushelf", "-vv", "status"], "cmd_status", {"data_dir": "/srv/docushelf", "verbose": 2}),
    ],
)
def test_parser_accepts_documented_commands(argv, func, expected):
    args = cli.build_parser().parse_args(argv)
    assert args.func is getattr(cli, func)
    for key, value in expected.items():
        assert getattr(args, key) == value, key


@pytest.mark.parametrize(
    "argv",
    [
        ["import-paperless", "/import", "--mode", "copy"],
        ["import-paperless"],
        ["serve", "--port", "70000"],
        ["serve", "--port", "http"],
        ["healthcheck", "--timeout", "0"],
        ["reconcile", "--ver"],  # no abbreviations
        ["token", "revoke"],
        ["unknown"],
    ],
)
def test_parser_rejects_invalid_arguments(argv, capsys):
    with pytest.raises(SystemExit) as raised:
        cli.build_parser().parse_args(argv)
    assert raised.value.code == cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert "Fehler:" in err and "Aufruf:" in err


def test_without_command_shows_help(capsys):
    code, out, err = run(capsys)
    assert code == cli.EXIT_USAGE
    assert "Befehle" in err and "import-paperless" in err


def test_token_without_action_shows_token_help(capsys):
    code, _, err = run(capsys, "token")
    assert code == cli.EXIT_USAGE
    assert "create" in err and "revoke" in err


def test_version(capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["--version"])
    assert raised.value.code == 0
    assert capsys.readouterr().out.strip() == f"DocuShelf {cli.__version__}"


# -- hash-password ------------------------------------------------------------------------------------


@pytest.fixture
def fake_hasher(monkeypatch):
    import docushelf.web.security as security

    seen: list[str] = []

    def hash_password(password: str) -> str:
        seen.append(password)
        return "scrypt$16$8$1$c2FsdA$aGFzaA"

    monkeypatch.setattr(security, "hash_password", hash_password)
    return seen


def test_hash_password_from_stdin_keeps_spaces(monkeypatch, capsys, fake_hasher):
    monkeypatch.setattr(sys, "stdin", io.StringIO("  mit Leerzeichen  \nzweite Zeile\n"))
    code, out, err = run(capsys, "hash-password", "--stdin")
    assert code == 0
    assert fake_hasher == ["  mit Leerzeichen  "]
    assert out == "scrypt$16$8$1$c2FsdA$aGFzaA\n"
    assert "Anführungszeichen" in err


def test_hash_password_env_line(monkeypatch, capsys, fake_hasher):
    monkeypatch.setattr(sys, "stdin", io.StringIO("korrekt-pferd-batterie\r\n"))
    code, out, _ = run(capsys, "hash-password", "--stdin", "--env")
    assert code == 0
    assert fake_hasher == ["korrekt-pferd-batterie"]
    assert out == "DOCUSHELF_ADMIN_PASSWORD_HASH='scrypt$16$8$1$c2FsdA$aGFzaA'\n"


@pytest.mark.parametrize("stdin, message", [("", "kein Passwort"), ("\n", "leer"), ("kurz\n", "zu kurz")])
def test_hash_password_rejects_missing_or_short_passwords(monkeypatch, capsys, fake_hasher, stdin, message):
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code, out, err = run(capsys, "hash-password", "--stdin")
    assert code == cli.EXIT_ERROR
    assert out == ""
    assert message in err
    assert fake_hasher == []


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_hash_password_asks_twice(monkeypatch, capsys, fake_hasher):
    import getpass

    answers = iter(["geheim-und-lang", "geheim-und-lang"])
    prompts: list[str] = []
    monkeypatch.setattr(sys, "stdin", _Terminal())
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": prompts.append(prompt) or next(answers))
    code, out, _ = run(capsys, "hash-password")
    assert code == 0
    assert len(prompts) == 2
    assert fake_hasher == ["geheim-und-lang"]
    assert out.startswith("scrypt$")


def test_hash_password_mismatch(monkeypatch, capsys, fake_hasher):
    import getpass

    answers = iter(["geheim-und-lang", "geheim-und-lang!"])
    monkeypatch.setattr(sys, "stdin", _Terminal())
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))
    code, out, err = run(capsys, "hash-password")
    assert code == cli.EXIT_ERROR
    assert "stimmen nicht überein" in err
    assert out == "" and fake_hasher == []


def test_hash_password_without_terminal_points_to_stdin_option(monkeypatch, capsys, fake_hasher):
    monkeypatch.setattr(sys, "stdin", io.StringIO("geheim-und-lang\n"))
    code, _, err = run(capsys, "hash-password")
    assert code == cli.EXIT_ERROR
    assert "--stdin" in err


def test_hash_password_real_hash_verifies(monkeypatch, capsys):
    from docushelf.web.security import verify_password

    monkeypatch.setattr(sys, "stdin", io.StringIO("ein langes Passwort\n"))
    code, out, _ = run(capsys, "hash-password", "--stdin")
    assert code == 0
    hashed = out.strip()
    assert hashed.startswith("scrypt$")
    assert verify_password("ein langes Passwort", hashed)
    assert not verify_password("ein anderes Passwort", hashed)


# -- token ----------------------------------------------------------------------------------------------


def test_token_create_list_revoke(cli_env, data_layout, capsys):
    from docushelf.web.tokens import TokenStore

    code, out, err = run(capsys, "token", "create", "iPhone Kurzbefehl", "--user", "anna")
    assert code == 0
    token = out.strip()
    assert out.count("\n") == 1 and token.startswith("dsk_")
    assert "nur jetzt angezeigt" in err

    store = TokenStore(data_layout.tokens_file)
    entry = store.verify(token)
    assert entry is not None and entry["user"] == "anna" and entry["name"] == "iPhone Kurzbefehl"
    assert token not in data_layout.tokens_file.read_text(encoding="utf-8")

    code, out, _ = run(capsys, "token", "list")
    assert code == 0
    header, line = out.strip().splitlines()
    assert header.split()[:3] == ["ID", "Name", "Benutzer"]
    assert entry["id"] in line and "iPhone Kurzbefehl" in line and "anna" in line
    assert token not in out

    code, out, _ = run(capsys, "token", "revoke", entry["id"])
    assert code == 0
    assert "widerrufen" in out
    assert store.verify(token) is None

    code, out, _ = run(capsys, "token", "list")
    assert code == 0 and "Keine API-Tokens" in out

    actions = [line["action"] for line in reversed(_audit_entries(data_layout))]
    assert actions == ["token_create", "token_revoke"]
    assert all(token not in json.dumps(line) for line in _audit_entries(data_layout))


def _audit_entries(layout: Layout) -> list[dict]:
    from docushelf.audit import AuditLog

    return AuditLog(layout.audit).tail(50)


def test_token_create_defaults_to_admin_user(cli_env, data_layout, capsys, monkeypatch):
    monkeypatch.setenv("DOCUSHELF_ADMIN_USER", "notfall")
    from docushelf.web.tokens import TokenStore

    code, out, _ = run(capsys, "token", "create", "Skript")
    assert code == 0
    assert TokenStore(data_layout.tokens_file).verify(out.strip())["user"] == "notfall"


def test_token_create_rejects_blank_name(cli_env, capsys):
    code, out, err = run(capsys, "token", "create", "   ")
    assert code == cli.EXIT_ERROR
    assert out == "" and "fehlt" in err


def test_token_revoke_unknown_id(cli_env, capsys):
    code, _, err = run(capsys, "token", "revoke", "deadbeef")
    assert code == cli.EXIT_ERROR
    assert "deadbeef" in err and "token list" in err


def test_token_list_with_broken_file(cli_env, data_layout, capsys):
    data_layout.config.mkdir(parents=True, exist_ok=True)
    data_layout.tokens_file.write_text("- id: [kaputt\n", encoding="utf-8")
    code, out, err = run(capsys, "token", "list")
    assert code == cli.EXIT_ERROR
    assert "Warnung" in err and "Keine API-Tokens" in out


# -- healthcheck -------------------------------------------------------------------------------------------


class _HealthHandler(BaseHTTPRequestHandler):
    responses: dict[str, tuple[int, dict, float]] = {}

    def do_GET(self):  # noqa: N802 - http.server API
        status, body, delay = self.responses.get(self.path, (404, {"detail": "nicht gefunden"}, 0.0))
        if delay:
            time.sleep(delay)
        payload = json.dumps(body).encode()
        self.send_response(status)
        if status in (301, 302):
            self.send_header("Location", "/healthz")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def health_server():
    _HealthHandler.responses = {
        "/healthz": (200, {"status": "ok", "checks": {"database": "ok", "data_dir": "ok", "archive": "ok"}}, 0.0),
        "/kaputt": (503, {"status": "error", "checks": {"database": "ok", "archive": "Fehler: Archivordner fehlt"}}, 0.0),
        "/umleitung": (302, {}, 0.0),
        "/langsam": (200, {"status": "ok"}, 2.0),
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(5)


def test_healthcheck_ok(health_server, capsys):
    port = health_server.server_address[1]
    code, out, _ = run(capsys, "healthcheck", "--url", f"http://127.0.0.1:{port}/healthz")
    assert code == 0
    assert out.strip() == "OK"


def test_healthcheck_reports_failing_checks(health_server, capsys):
    port = health_server.server_address[1]
    code, out, _ = run(capsys, "healthcheck", "--url", f"http://127.0.0.1:{port}/kaputt")
    assert code == cli.EXIT_ERROR
    assert "HTTP 503" in out and "Archivordner fehlt" in out


def test_healthcheck_does_not_follow_redirects(health_server, capsys):
    port = health_server.server_address[1]
    code, out, _ = run(capsys, "healthcheck", "--url", f"http://127.0.0.1:{port}/umleitung")
    assert code == cli.EXIT_ERROR
    assert "HTTP 302" in out


def test_healthcheck_times_out(health_server, capsys):
    port = health_server.server_address[1]
    started = time.monotonic()
    code, out, _ = run(capsys, "healthcheck", "--url", f"http://127.0.0.1:{port}/langsam", "--timeout", "0.3")
    assert code == cli.EXIT_ERROR
    assert "Nicht erreichbar" in out
    assert time.monotonic() - started < 1.9


def test_healthcheck_unreachable(capsys):
    port = free_port()
    code, out, _ = run(capsys, "healthcheck", "--url", f"http://127.0.0.1:{port}/healthz", "--timeout", "1")
    assert code == cli.EXIT_ERROR
    assert out.startswith("Nicht erreichbar")


def test_healthcheck_uses_configured_port_and_ignores_proxies(health_server, cli_env, monkeypatch, capsys):
    port = health_server.server_address[1]
    monkeypatch.setenv("DOCUSHELF_PORT", str(port))
    monkeypatch.setenv("DOCUSHELF_HOST", "0.0.0.0")
    # A proxy from the environment must not be used for the local check.
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{free_port()}")
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{free_port()}")
    code, out, _ = run(capsys, "healthcheck")
    assert code == 0, out


def test_healthcheck_survives_broken_configuration(health_server, cli_env, monkeypatch, capsys):
    port = health_server.server_address[1]
    monkeypatch.setenv("DOCUSHELF_PORT", str(port))
    monkeypatch.setenv("DOCUSHELF_IMAP_ENABLED", "true")  # IMAP without host: load_settings fails
    code, _, _ = run(capsys, "healthcheck")
    assert code == 0


@pytest.mark.parametrize(
    "host, expected",
    [("0.0.0.0", "127.0.0.1"), ("", "127.0.0.1"), ("::", "[::1]"), ("127.0.0.1", "127.0.0.1"),
     ("100.64.0.10", "100.64.0.10"), ("fd00::10", "[fd00::10]"), ("docushelf", "docushelf")],
)
def test_connect_host(host, expected):
    assert cli._connect_host(host) == expected


def test_healthcheck_does_not_create_files(cli_env, data_dir, monkeypatch, capsys):
    monkeypatch.delenv("DOCUSHELF_SECRET_KEY")
    monkeypatch.setenv("DOCUSHELF_PORT", str(free_port()))
    run(capsys, "healthcheck", "--timeout", "0.5")
    assert not (data_dir / "config" / "secret.key").exists()


# -- status ------------------------------------------------------------------------------------------------


def test_status_on_empty_data_dir(cli_env, data_dir, monkeypatch, capsys):
    monkeypatch.delenv("DOCUSHELF_SECRET_KEY")
    code, out, _ = run(capsys, "status")
    assert code == 0
    assert out.startswith(f"DocuShelf {cli.__version__}")
    assert "gestoppt" in out and "noch nicht angelegt" in out
    # status only reads: no secret, no index, no directories are created
    assert not (data_dir / "config").exists()
    assert not (data_dir / "index").exists()


def test_status_counts_inbox_and_jobs(cli_env, data_layout, tmp_path, capsys):
    from docushelf.inbox.items import InboxStore
    from docushelf.index.db import Index

    data_layout.ensure(("upload",))
    index = Index(data_layout.db_path)
    index.init_schema()
    upload = tmp_path / "scan.pdf"
    upload.write_bytes(b"%PDF-1.4\n%%EOF\n")
    item = InboxStore(data_layout).create("upload", [upload])
    index.enqueue("process", item.id)
    index.set_meta("last_scan", "2026-11-23T14:07:00+01:00")
    index.close()
    (data_layout.trash / "20261120-101010-abcdef").mkdir(parents=True)

    code, out, _ = run(capsys, "status")
    assert code == 0
    assert "1 in Verarbeitung" in out
    assert "1 wartend" in out and "Verarbeitung 1" in out
    assert "23.11.2026 14:07" in out
    assert "1 Eintrag" in out

    code, out, _ = run(capsys, "status", "--json")
    data = json.loads(out)
    assert data["inbox"]["arbeit"] == 1
    assert data["index"]["available"] and data["index"]["documents"] == 0
    assert data["index"]["jobs"]["queued"] == 1
    assert data["index"]["jobs"]["by_kind"] == {"process": {"queued": 1}}
    assert data["server"] == {"running": False}
    assert data["trash"] == 1


def test_status_reports_unreadable_index(cli_env, data_layout, capsys):
    data_layout.index.mkdir(parents=True)
    data_layout.db_path.write_bytes(b"this is not a database" * 100)
    code, out, _ = run(capsys, "status")
    assert code == 0
    assert "nicht lesbar" in out and "docushelf rebuild" in out


def test_status_shows_running_server(cli_env, data_layout, capsys):
    lock = hold_server_lock(data_layout, started_at="2026-11-23T08:00:00+01:00")
    try:
        code, out, _ = run(capsys, "status")
        assert code == 0
        assert "läuft (PID 4242, Port 8000, seit 23.11.2026 08:00)" in out
        code, out, _ = run(capsys, "status", "--json")
        assert json.loads(out)["server"]["running"] is True
    finally:
        lock.release()


# -- server lock ------------------------------------------------------------------------------------------


def test_server_lock_is_exclusive_and_released(tmp_path):
    layout = Layout(data=tmp_path, archive=tmp_path / "archiv")
    assert cli.probe_server(layout) is None
    first = cli.ServerLock(Path(layout.index) / cli.SERVER_LOCK_NAME)
    assert first.acquire({"pid": 1, "port": 8000}, attempts=1)
    try:
        assert cli.probe_server(layout) == {"pid": 1, "port": 8000}
        second = cli.ServerLock(Path(layout.index) / cli.SERVER_LOCK_NAME)
        assert not second.acquire({"pid": 2}, attempts=2, delay=0.01)
        assert cli.probe_server(layout) == {"pid": 1, "port": 8000}
    finally:
        first.release()
    assert cli.probe_server(layout) is None
    assert not first.held


def test_server_lock_ignores_a_concurrent_probe(tmp_path):
    """A status probe holds a shared lock for an instant; the server waits for it."""
    import fcntl

    path = tmp_path / "index" / cli.SERVER_LOCK_NAME
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    fd = os.open(path, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_SH)
    timer = threading.Timer(0.15, lambda: os.close(fd))
    timer.start()
    lock = cli.ServerLock(path)
    try:
        assert lock.acquire({"pid": 3}, attempts=10, delay=0.05)
    finally:
        timer.join()
        lock.release()


# -- rebuild and reconcile ---------------------------------------------------------------------------------


@pytest.fixture
def fake_services(monkeypatch):
    services = FakeServices()
    monkeypatch.setattr(cli, "_open_services", lambda settings, **kwargs: services)
    return services


def test_rebuild_reports_and_closes(cli_env, fake_services, capsys):
    code, out, err = run(capsys, "rebuild")
    assert code == 0
    assert fake_services.rebuild_users == [cli.CLI_USER]
    assert "3 Dokumente in 2 Ordnern" in out
    assert "1 Dokument zur Verarbeitung vorgemerkt" in out
    assert "neu aufgebaut" in err
    assert fake_services.index.closed and fake_services.stopped


def test_rebuild_with_read_errors_fails(cli_env, fake_services, capsys):
    fake_services.stats = ScanStats(files_seen=5, folders_seen=1, added=4, errors=1)
    code, out, _ = run(capsys, "rebuild")
    assert code == cli.EXIT_ERROR
    assert "1 Datei konnte nicht gelesen werden" in out


def test_reconcile_verify(cli_env, fake_services, capsys):
    fake_services.stats = ScanStats(files_seen=10, added=1, updated=2, moved=3, removed=4, duration_ms=1234)
    code, out, _ = run(capsys, "reconcile", "--verify")
    assert code == 0
    assert fake_services.scans == [{"full": True, "verify": True}]
    assert "in 1,2 s (alle Dateien neu gelesen)" in out
    assert "10 Dateien geprüft, 1 neu, 2 geändert, 3 verschoben, 4 entfernt" in out
    assert fake_services.index.closed


@pytest.mark.parametrize("command", [["rebuild"], ["reconcile"], ["reconcile", "--verify"]])
def test_maintenance_refuses_while_server_runs(cli_env, data_layout, fake_services, capsys, command):
    lock = hold_server_lock(data_layout)
    try:
        code, out, err = run(capsys, *command)
    finally:
        lock.release()
    assert code == cli.EXIT_ERROR
    assert "läuft gerade (PID 4242)" in err and "--force" in err and "/api/v1/admin/" in err
    assert fake_services.rebuild_users == [] and fake_services.scans == []


def test_rebuild_force_while_server_runs(cli_env, data_layout, fake_services, capsys):
    lock = hold_server_lock(data_layout)
    try:
        code, _, err = run(capsys, "rebuild", "--force")
    finally:
        lock.release()
    assert code == 0
    assert "Warnung" in err and "--force" in err
    assert fake_services.rebuild_users == [cli.CLI_USER]


def test_warning_when_running_as_root(cli_env, fake_services, data_dir, monkeypatch, capsys):
    if data_dir.stat().st_uid == 0:
        pytest.skip("the data directory belongs to root")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    code, _, err = run(capsys, "reconcile")
    assert code == 0
    assert "läuft als root" in err


# -- import-paperless ---------------------------------------------------------------------------------------


class FakeImporter:
    """Stands in for PaperlessImporter; ``outcomes`` decides the status of each planned document."""

    instances: list["FakeImporter"] = []
    outcomes: list[tuple[str, str]] = []

    def __init__(self, services):
        self.services = services
        self.mapping_path = Path("/data/config/paperless-import.yaml")
        self.plan_args: dict = {}
        self.run_args: dict = {}
        FakeImporter.instances.append(self)

    def load(self, export_dir):
        self.export_dir = export_dir
        documents = [
            SimpleNamespace(storage_path="Privat", owner="anna"),
            SimpleNamespace(storage_path="Privat", owner="ben"),
            SimpleNamespace(storage_path=None, owner="anna"),
        ]
        return SimpleNamespace(directory=export_dir, version="2.17.1", documents=documents, warnings=["Datei fehlt"])

    def plan(self, export, *, storage_paths=None, owners=None, include_deleted=False, overrides=None,
             only_planned=False):
        self.plan_args = {"storage_paths": storage_paths, "owners": owners}
        self.include_deleted = include_deleted
        self.overrides, self.only_planned = overrides, only_planned
        return [SimpleNamespace(pk=pk) for pk in range(1, len(self.outcomes) + 1)]

    def run(self, plan, *, mode="inbox", dry_run=False, user=None, progress=None):
        from docushelf.importers.paperless import ImportEntry, ImportReport

        self.run_args = {"mode": mode, "dry_run": dry_run, "user": user, "progress": progress}
        report = ImportReport(mode=mode, dry_run=dry_run, started_at="2026-11-23T10:00:00+01:00")
        for position, (planned, (status, message)) in enumerate(zip(plan, self.outcomes), start=1):
            if dry_run and status not in ("skipped",):
                status = "planned"
            entry = ImportEntry(paperless_id=planned.pk, title=f"Dokument {planned.pk}", status=status,
                                message=message, folder="Finanzen/Rechnungen")
            report.entries.append(entry)
            if progress:
                progress(position, len(plan), entry)
        return report


@pytest.fixture
def fake_importer(monkeypatch, fake_services):
    import docushelf.importers.paperless as paperless

    FakeImporter.instances = []
    FakeImporter.outcomes = [("imported", "wartet im Eingang auf Bestätigung"), ("skipped", "bereits im Archiv"),
                             ("imported", "wartet im Eingang auf Bestätigung")]
    monkeypatch.setattr(paperless, "PaperlessImporter", FakeImporter)
    return FakeImporter


def test_import_dry_run(cli_env, fake_importer, fake_services, capsys):
    code, out, err = run(capsys, "import-paperless", "/import", "--dry-run", "--storage-path", "Privat")
    assert code == 0
    importer = fake_importer.instances[0]
    assert importer.export_dir == Path("/import")
    assert importer.plan_args == {"storage_paths": ["Privat"], "owners": None}
    assert importer.run_args["dry_run"] is True and importer.run_args["mode"] == "inbox"
    assert importer.run_args["user"] == cli.CLI_USER
    assert "Paperless-Export /import: 3 Dokumente, Paperless-ngx 2.17.1" in out
    assert "Speicherpfade: Privat (2), - (ohne) (1)" in out
    assert "Probelauf: 3 Dokumente ausgewählt" in out
    assert "[1/3] geplant: #1 Dokument 1 → Finanzen/Rechnungen" in out
    assert "[2/3] übersprungen: #2 Dokument 2 → Finanzen/Rechnungen (bereits im Archiv)" in out
    assert "geplant:        2" in out
    assert "1× bereits im Archiv" in out
    assert "Es wurde nichts verändert" in out
    assert "Hinweis: Datei fehlt" in err
    assert fake_services.index.closed


def test_import_inbox_mode_reports_failures(cli_env, fake_importer, fake_services, capsys):
    fake_importer.outcomes = [("imported", "wartet"), ("failed", "Übernahme fehlgeschlagen: kaputt")]
    code, out, _ = run(capsys, "import-paperless", "/import", "--owner", "anna", "--owner", "ben")
    assert code == cli.EXIT_ERROR
    assert fake_importer.instances[0].plan_args == {"storage_paths": None, "owners": ["anna", "ben"]}
    assert "Fehlgeschlagen:\n  fehlgeschlagen: #2 Dokument 2" in out
    assert "1 fehlgeschlagen" in out
    assert "Der Server läuft nicht" in out


def test_import_include_deleted_is_passed_to_the_plan(cli_env, fake_importer, capsys):
    code, _, _ = run(capsys, "import-paperless", "/import")
    assert code == 0 and fake_importer.instances[-1].include_deleted is False
    code, _, _ = run(capsys, "import-paperless", "/import", "--include-deleted", "--dry-run")
    assert code == 0 and fake_importer.instances[-1].include_deleted is True


def test_import_quiet_prints_only_the_summary(cli_env, fake_importer, capsys):
    code, out, _ = run(capsys, "import-paperless", "/import", "--quiet")
    assert code == 0
    assert fake_importer.instances[0].run_args["progress"] is None
    assert "[1/3]" not in out
    assert "Ergebnis:" in out


def test_import_archive_mode_refuses_while_server_runs(cli_env, data_layout, fake_importer, capsys):
    lock = hold_server_lock(data_layout)
    try:
        code, _, err = run(capsys, "import-paperless", "/import", "--mode", "archive")
    finally:
        lock.release()
    assert code == cli.EXIT_ERROR
    assert "direkter Ablage" in err and "--force" in err
    assert fake_importer.instances == []


def test_import_inbox_mode_works_while_server_runs(cli_env, data_layout, fake_importer, capsys):
    lock = hold_server_lock(data_layout)
    try:
        code, out, _ = run(capsys, "import-paperless", "/import")
        code_archive_dry, _, _ = run(capsys, "import-paperless", "/import", "--mode", "archive", "--dry-run")
    finally:
        lock.release()
    assert code == 0 and code_archive_dry == 0
    assert "Der Server verarbeitet die Dokumente jetzt" in out


# -- process --------------------------------------------------------------------------------------------------


@pytest.fixture
def process_setup(monkeypatch, tmp_path):
    item = fake_item(tmp_path, "fehler")
    services = FakeServices(item=item)
    monkeypatch.setattr(cli, "_open_services", lambda settings, **kwargs: services)
    return services


def test_process_failed_item_runs_again(cli_env, process_setup, capsys):
    services = process_setup
    services.item.log_path.write_text("2026-11-23T10:00:00+01:00 alter Eintrag\n", encoding="utf-8")

    def process(item_id):
        with open(services.item.log_path, "a", encoding="utf-8") as handle:
            handle.write("2026-11-23T10:05:07+01:00 Verarbeitung gestartet\n")
        return FakePipeline.process(services.pipeline, item_id)

    services.pipeline.process = process
    code, out, err = run(capsys, "process", ITEM_ID, "--skip-ocr")
    assert code == 0
    assert services.pipeline.retried == [{"id": ITEM_ID, "skip_ocr": True, "skip_blank_removal": None}]
    assert "10:05:07 Verarbeitung gestartet" in out and "alter Eintrag" not in out
    assert "Ergebnis: bereit zur Ablage (2 Seiten)." in out
    assert "Vorschlag: Finanzen/Bank – „Kontoauszug November“, 82 %" in out
    assert "weil: Absender Sparkasse" in out
    assert "Verarbeite" in err
    # the job queued by retry() is obsolete once the item was processed here
    assert services.index.completed == [7]
    assert services.index.closed


def test_process_item_in_work_sets_options(cli_env, process_setup, capsys):
    services = process_setup
    services.item.state = "arbeit"
    services.pipeline.processed.clear()
    code, _, _ = run(capsys, "process", ITEM_ID, "--skip-blank-removal")
    assert code == 0
    assert services.pipeline.retried == []
    assert services.item.options == {"skip_blank_removal": True}


def test_process_hands_over_to_running_server(cli_env, data_layout, process_setup, capsys):
    services = process_setup
    services.item.state = "arbeit"
    lock = hold_server_lock(data_layout)
    try:
        code, out, _ = run(capsys, "process", ITEM_ID)
    finally:
        lock.release()
    assert code == 0
    assert services.index.enqueued == [("process", ITEM_ID)]
    assert services.pipeline.processed == []
    assert "Warteschlange" in out


def test_process_failed_item_hands_retry_to_running_server(cli_env, data_layout, process_setup, capsys):
    services = process_setup
    lock = hold_server_lock(data_layout)
    try:
        code, _, _ = run(capsys, "process", ITEM_ID)
    finally:
        lock.release()
    assert code == 0
    assert [entry["id"] for entry in services.pipeline.retried] == [ITEM_ID]
    assert services.pipeline.processed == []


@pytest.mark.parametrize("state, message", [("erledigt", "bereits abgelegt")])
def test_process_refuses_filed_items(cli_env, process_setup, capsys, state, message):
    process_setup.item.state = state
    code, _, err = run(capsys, "process", ITEM_ID)
    assert code == cli.EXIT_ERROR and message in err


def test_process_unknown_and_invalid_ids(cli_env, process_setup, capsys):
    code, _, err = run(capsys, "process", "20261123-125100-ffffff")
    assert code == cli.EXIT_ERROR and "gibt es nicht" in err
    code, _, err = run(capsys, "process", "../etc")
    assert code == cli.EXIT_ERROR and "keine Eingangs-ID" in err


def test_process_reports_pipeline_failure(cli_env, process_setup, capsys):
    services = process_setup

    def process(item_id):
        services.item.state = "fehler"
        services.item.error = {"stage": "ocr_done", "message": "PDF ist verschlüsselt"}
        return services.item

    services.pipeline.process = process
    code, out, _ = run(capsys, "process", ITEM_ID)
    assert code == cli.EXIT_ERROR
    assert "Ergebnis: Fehler (ocr_done): PDF ist verschlüsselt" in out


def test_process_transient_error_keeps_item_queued(cli_env, process_setup, capsys):
    from docushelf.errors import ProcessingError

    services = process_setup

    def process(item_id):
        raise ProcessingError("Zeitüberschreitung bei der Texterkennung", transient=True)

    services.pipeline.process = process
    code, _, err = run(capsys, "process", ITEM_ID)
    assert code == cli.EXIT_ERROR
    assert "Vorübergehender Fehler" in err
    assert ("process", ITEM_ID) in services.index.enqueued


# -- serve ---------------------------------------------------------------------------------------------------


class FakeServer:
    def __init__(self, config, *, start: bool = True):
        self.config = config
        self.start = start
        self.started = False
        self.lock_held_while_running = None

    def run(self):
        self.started = self.start


@pytest.fixture
def fake_uvicorn(monkeypatch):
    import docushelf.web.app as web_app

    created: dict = {}

    def create_app(settings=None, *, services=None):
        created["settings"] = settings
        return "the-app"

    servers: list[FakeServer] = []

    def make_server(config):
        server = FakeServer(config)
        servers.append(server)
        return server

    monkeypatch.setattr(web_app, "create_app", create_app)
    monkeypatch.setattr(cli, "_uvicorn_server", make_server)
    return SimpleNamespace(created=created, servers=servers)


def test_serve_runs_uvicorn_behind_proxy(cli_env, data_layout, fake_uvicorn, monkeypatch, capsys):
    monkeypatch.setenv("DOCUSHELF_TRUSTED_PROXIES", "172.16.0.0/12")
    monkeypatch.setenv("DOCUSHELF_LOG_LEVEL", "warning")
    probes = []
    original_run = FakeServer.run

    def run_and_probe(self):
        probes.append(cli.probe_server(data_layout))
        original_run(self)

    monkeypatch.setattr(FakeServer, "run", run_and_probe)
    code, _, _ = run(capsys, "serve", "--port", "8123")
    assert code == 0
    config = fake_uvicorn.servers[0].config
    assert config.app == "the-app"
    assert (config.host, config.port) == ("0.0.0.0", 8123)
    assert config.proxy_headers is True
    assert config.forwarded_allow_ips == "172.16.0.0/12"
    assert config.lifespan == "on" and config.log_config is None
    assert config.access_log is False and config.server_header is False
    assert config.timeout_graceful_shutdown == cli.GRACEFUL_SHUTDOWN_SECONDS
    assert fake_uvicorn.created["settings"].port == 8123
    assert fake_uvicorn.created["settings"].secret_key == "test-secret-key-0123456789abcdef"
    # the server lock is held while uvicorn runs and released afterwards
    assert probes[0]["port"] == 8123 and probes[0]["pid"] == os.getpid()
    assert cli.probe_server(data_layout) is None
    assert logging.getLogger().level == logging.WARNING
    assert logging.getLogger("httpx").level == logging.WARNING


def test_serve_access_log_skips_health_checks(cli_env, fake_uvicorn, capsys):
    code, _, _ = run(capsys, "serve", "--access-log")
    assert code == 0
    assert fake_uvicorn.servers[0].config.access_log is True
    access = logging.getLogger("uvicorn.access")
    health = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1:5000", "GET", "/healthz", "1.1", 200), None)
    page = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                             ("127.0.0.1:5000", "GET", "/eingang", "1.1", 200), None)
    assert not access.filter(health)
    assert access.filter(page)


def test_serve_startup_failure_exits_with_error(cli_env, data_layout, monkeypatch, fake_uvicorn, capsys):
    monkeypatch.setattr(cli, "_uvicorn_server", lambda config: FakeServer(config, start=False))
    code, _, err = run(capsys, "serve")
    assert code == cli.EXIT_ERROR
    assert "did not start" in err
    assert cli.probe_server(data_layout) is None


def test_serve_refuses_second_server(cli_env, data_layout, fake_uvicorn, capsys):
    lock = hold_server_lock(data_layout)
    try:
        code, _, err = run(capsys, "serve")
    finally:
        lock.release()
    assert code == cli.EXIT_ERROR
    assert "anderer DocuShelf-Server" in err and "PID 4242" in err
    assert fake_uvicorn.servers == []


def test_serve_refuses_missing_separate_archive(cli_env, tmp_path, monkeypatch, fake_uvicorn, capsys):
    archive = tmp_path / "nas" / "archiv"
    monkeypatch.setenv("DOCUSHELF_ARCHIVE_DIR", str(archive))
    code, _, err = run(capsys, "serve")
    assert code == cli.EXIT_ERROR
    assert "existiert nicht" in err and "eingehängt" in err
    assert not archive.exists()
    assert fake_uvicorn.servers == []


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can write everywhere")
def test_serve_explains_missing_write_permission(cli_env, data_layout, fake_uvicorn, capsys):
    data_layout.ensure(("upload",))
    os.chmod(data_layout.config, 0o500)
    try:
        code, _, err = run(capsys, "serve")
    finally:
        os.chmod(data_layout.config, 0o755)
    assert code == cli.EXIT_ERROR
    assert "kann hier nicht schreiben" in err and str(data_layout.config) in err and "chown" in err
    assert fake_uvicorn.servers == []


def test_serve_sets_damaged_index_aside(cli_env, data_layout, monkeypatch, fake_uvicorn, capsys):
    import docushelf.web.app as web_app

    data_layout.index.mkdir(parents=True)
    data_layout.db_path.write_bytes(b"garbage")
    calls = []

    def create_app(settings=None, *, services=None):
        calls.append(settings)
        if len(calls) == 1:
            error = sqlite3.DatabaseError("file is not a database")
            error.sqlite_errorname = "SQLITE_NOTADB"
            raise error
        return "the-app"

    monkeypatch.setattr(web_app, "create_app", create_app)
    code, _, err = run(capsys, "serve")
    assert code == 0
    assert len(calls) == 2
    assert not data_layout.db_path.exists()
    assert list(data_layout.index.glob("docushelf.sqlite3.defekt-*"))
    assert "beiseitegelegt" in err


# -- main: error handling and logging ------------------------------------------------------------------------


def test_main_maps_errors_to_exit_codes(monkeypatch, capsys):
    from docushelf.errors import ConfigError, NotFound

    def fail_with(exc):
        def command(args):
            raise exc

        return command

    cases = [
        (ConfigError("IMAP ist aktiviert, aber IMAP_HOST fehlt"), cli.EXIT_ERROR, "Fehler: IMAP ist aktiviert"),
        (NotFound("abc"), cli.EXIT_ERROR, "Fehler: nicht gefunden: abc"),
        (PermissionError(13, "Permission denied", "/data/config"), cli.EXIT_ERROR,
         "Fehler: /data/config: Permission denied"),
        (KeyboardInterrupt(), cli.EXIT_INTERRUPTED, "Abgebrochen."),
        (RuntimeError("kaputt"), cli.EXIT_ERROR, "unerwarteter Programmfehler"),
    ]
    for exc, code, message in cases:
        monkeypatch.setattr(cli, "cmd_status", fail_with(exc))
        result, _, err = run(capsys, "status")
        assert result == code, exc
        assert message in err, exc
    assert "RuntimeError: kaputt" in err  # unexpected errors keep their traceback


def test_configure_logging_writes_concise_lines_to_current_stderr(capsys):
    cli.configure_logging(logging.INFO)
    cli.configure_logging(logging.INFO)  # idempotent: one handler only
    handlers = [h for h in logging.getLogger().handlers if isinstance(h, cli._StderrHandler)]
    assert len(handlers) == 1
    logging.getLogger("docushelf.test").info("hallo %s", "Welt")
    logging.getLogger("watchdog.observers").info("noise")
    err = capsys.readouterr().err
    assert "INFO    docushelf.test: hallo Welt" in err
    assert "noise" not in err


@pytest.mark.parametrize(
    "exc, corrupt",
    [
        (ValueError("no database error"), False),
        (sqlite3.OperationalError("database is locked"), False),
        (sqlite3.DatabaseError("database disk image is malformed"), True),
        (sqlite3.DatabaseError("file is not a database"), True),
    ],
)
def test_is_corrupt(exc, corrupt):
    assert cli._is_corrupt(exc) is corrupt


def test_number_formatting():
    assert cli._fmt_int(1234567) == "1.234.567"
    assert cli._fmt_seconds(3249) == "3,2 s"
    assert cli._fmt_seconds(123456) == "123 s"
    assert cli._count(1, "Datei", "Dateien") == "1 Datei"
    assert cli._count(1200, "Datei", "Dateien") == "1.200 Dateien"


# -- integration with the real components ----------------------------------------------------------------------


def test_rebuild_and_reconcile_real_archive(cli_env, data_layout, samples, capsys):
    samples.text_pdf(data_layout.archive / "Finanzen" / "Bank" / "2025-11-03 Kontoauszug November.pdf",
                     ["Sparkasse Musterstadt\nKontoauszug November 2025"])
    samples.text_pdf(data_layout.archive / "Wohnung" / "2025-01-15 Nebenkosten.pdf", ["Nebenkostenabrechnung 2024"])
    (data_layout.archive / "Leer").mkdir(parents=True)

    code, out, _ = run(capsys, "rebuild")
    assert code == 0, out
    assert "2 Dokumente in 4 Ordnern" in out

    code, out, _ = run(capsys, "status", "--json")
    data = json.loads(out)
    assert data["index"]["documents"] == 2
    assert data["index"]["last_full_scan"]

    moved = data_layout.archive / "Leer" / "2025-01-15 Nebenkosten.pdf"
    (data_layout.archive / "Wohnung" / "2025-01-15 Nebenkosten.pdf").rename(moved)
    code, out, _ = run(capsys, "reconcile")
    assert code == 0, out
    assert "1 verschoben" in out

    audit = [entry["action"] for entry in _audit_entries(data_layout)]
    assert "rebuild" in audit


def test_rebuild_replaces_a_damaged_index(cli_env, data_layout, samples, capsys):
    samples.text_pdf(data_layout.archive / "2025-03-01 Brief.pdf", ["Ein Brief"])
    data_layout.index.mkdir(parents=True)
    data_layout.db_path.write_bytes(b"\x00garbage, not sqlite" * 512)

    code, out, _ = run(capsys, "reconcile")
    assert code == cli.EXIT_ERROR  # reconcile does not touch a damaged index ...

    code, out, err = run(capsys, "rebuild")  # ... rebuild sets it aside and starts over
    assert code == 0, err
    assert "1 Dokument in" in out
    assert "beiseitegelegt" in err
    assert list(data_layout.index.glob("docushelf.sqlite3.defekt-*"))


def test_process_real_item(cli_env, data_layout, samples, tmp_path, capsys):
    from docushelf.inbox.items import InboxStore

    data_layout.ensure(("upload",))
    pdf = samples.text_pdf(tmp_path / "upload" / "rechnung.pdf",
                           ["Stadtwerke Musterstadt GmbH\nRechnung Nr. 4711\nRechnungsdatum: 12.03.2025\nGesamt 84,20 EUR"])
    item = InboxStore(data_layout).create("upload", [pdf], options={"skip_ocr": True})

    code, out, err = run(capsys, "process", item.id)
    assert code == 0, out + err
    assert "Ergebnis: bereit zur Ablage (1 Seite)." in out
    assert "Vorschlag:" in out
    assert InboxStore(data_layout).find(item.id).state == "bereit"

    code, out, _ = run(capsys, "status", "--json")
    assert json.loads(out)["inbox"]["bereit"] == 1


def _write_paperless_export(root: Path, samples) -> Path:
    root.mkdir(parents=True)
    original = samples.text_pdf(root / "originals" / "0000001.pdf", ["Stadtwerke Musterstadt\nRechnung 4711"])
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    records = [
        {"model": "auth.user", "pk": 1, "fields": {"username": "anna"}},
        {"model": "documents.correspondent", "pk": 3, "fields": {"name": "Stadtwerke Musterstadt", "owner": 1}},
        {"model": "documents.documenttype", "pk": 4, "fields": {"name": "Rechnung", "owner": 1}},
        {"model": "documents.storagepath", "pk": 5, "fields": {"name": "Privat", "path": "{title}", "owner": 1}},
        {
            "model": "documents.document",
            "pk": 1,
            "__exported_file_name__": "originals/0000001.pdf",
            "fields": {
                "title": "2025-03-12 Stadtwerke Musterstadt Rechnung 4711",
                "content": "Rechnung 4711",
                "created": "2025-03-12",
                "added": "2025-03-13T10:00:00+01:00",
                "modified": "2025-03-13T10:00:00+01:00",
                "checksum": digest,
                "archive_checksum": None,
                "correspondent": 3,
                "document_type": 4,
                "storage_path": 5,
                "owner": 1,
                "tags": [],
                "filename": "originals/0000001.pdf",
                "original_filename": "scan.pdf",
                "mime_type": "application/pdf",
                "page_count": 1,
            },
        },
    ]
    (root / "manifest.json").write_text(json.dumps(records), encoding="utf-8")
    (root / "metadata.json").write_text(json.dumps({"version": "2.17.1"}), encoding="utf-8")
    return root


def test_import_paperless_real_export(cli_env, data_layout, samples, tmp_path, capsys):
    export = _write_paperless_export(tmp_path / "export", samples)

    code, out, _ = run(capsys, "import-paperless", str(export), "--dry-run")
    assert code == 0, out
    assert "Probelauf: 1 Dokument ausgewählt" in out
    assert "[1/1] geplant: #1" in out
    assert list(data_layout.work.glob("*")) == []  # a dry run changes nothing

    code, out, _ = run(capsys, "import-paperless", str(export), "--storage-path", "Privat")
    assert code == 0, out
    assert "[1/1] im Eingang: #1" in out
    items = [path for path in data_layout.work.iterdir() if path.is_dir()]
    assert len(items) == 1

    code, out, _ = run(capsys, "import-paperless", str(export))  # running it again skips the document
    assert code == 0
    assert "übersprungen" in out
    assert len([path for path in data_layout.work.iterdir() if path.is_dir()]) == 1

    code, _, err = run(capsys, "import-paperless", str(export), "--storage-path", "Gibt es nicht")
    assert code == cli.EXIT_ERROR and "Fehler:" in err


def _write_mixed_paperless_export(root: Path, samples) -> Path:
    """Three documents: a text PDF, a scan without OCR text and a plain text file.

    Records use the fields of a real export (tests/data/paperless_manifest_structure.json).
    """
    root.mkdir(parents=True)
    samples.text_pdf(root / "originals" / "0000001.pdf", ["Stadtwerke Musterstadt\nRechnung 4711 über 12,00 EUR"])
    samples.scan_pdf(root / "originals" / "0000002.pdf", [["Steuerbescheid 2024"]])
    (root / "originals" / "0000003.txt").write_text("Zählerstand 4711", encoding="utf-8")
    records: list[dict] = [
        {"model": "auth.user", "pk": 1, "fields": {"username": "anna"}},
        {"model": "documents.correspondent", "pk": 3, "fields": {"name": "Stadtwerke Musterstadt", "owner": 1}},
        {"model": "documents.documenttype", "pk": 4, "fields": {"name": "Rechnung", "owner": None}},
        {"model": "documents.documenttype", "pk": 5, "fields": {"name": "Steuerbescheid", "owner": None}},
        {"model": "documents.storagepath", "pk": 5, "fields": {"name": "Privat", "path": "{title}", "owner": 1}},
    ]
    for pk, title, name, mime, doc_type in (
        (1, "Rechnung 4711", "originals/0000001.pdf", "application/pdf", 4),
        (2, "Steuerbescheid 2024", "originals/0000002.pdf", "application/pdf", 5),
        (3, "Zählerstand", "originals/0000003.txt", "text/plain", 4),
    ):
        records.append({
            "model": "documents.document",
            "pk": pk,
            "__exported_file_name__": name,
            "fields": {
                "title": title, "content": title, "created": "2025-03-12", "added": "2025-03-13T10:00:00+01:00",
                "modified": "2025-03-13T10:00:00+01:00",
                "checksum": hashlib.sha256((root / name).read_bytes()).hexdigest(), "archive_checksum": None,
                "archive_filename": None, "archive_serial_number": None, "correspondent": 3,
                "document_type": doc_type, "storage_path": 5, "owner": 1, "tags": [], "filename": name,
                "original_filename": name.split("/")[1], "mime_type": mime, "page_count": 1, "deleted_at": None,
                "restored_at": None, "root_document": None,
            },
        })
    structure = json.loads((Path(__file__).parent / "data" / "paperless_manifest_structure.json").read_text())
    real_fields = set(structure["models"]["documents.document"]["fields"])
    assert all(set(r["fields"]) <= real_fields for r in records if r["model"] == "documents.document")
    (root / "manifest.json").write_text(json.dumps(records), encoding="utf-8")
    (root / "metadata.json").write_text(json.dumps({"version": "2.17.1"}), encoding="utf-8")
    return root


def test_import_paperless_report_shows_ocr_and_documents_for_manual_takeover(cli_env, samples, tmp_path, capsys):
    export = _write_mixed_paperless_export(tmp_path / "export", samples)
    code, out, _ = run(capsys, "import-paperless", str(export), "--dry-run")
    assert code == 0, out
    assert "#1 Rechnung 4711 → Finanzen/Rechnungen/Stadtwerke Musterstadt – Original, ohne OCR (neuer Ordner)" in out
    assert "#2 Steuerbescheid 2024 → Steuern – Original, mit OCR (kein Text)" in out
    assert "#3 Zählerstand → Finanzen/Rechnungen/Stadtwerke Musterstadt (Dateityp wird nicht unterstützt " \
           "(text/plain) – bitte von Hand übernehmen)" in out
    assert "davon mit OCR:  1" in out
    assert "1 übersprungen (davon 1 von Hand zu übernehmen)" in out
    tail = out.rstrip().splitlines()[-2:]
    assert tail == ["Von Hand übernehmen (Dateityp wird nicht unterstützt):",
                    "  #3 Zählerstand – originals/0000003.txt"]

    code, out, _ = run(capsys, "import-paperless", str(export))
    assert code == 0, out
    assert "im Eingang: #2 Steuerbescheid 2024 → Steuern – Original, mit OCR (kein Text)" in out
    assert out.rstrip().endswith("  #3 Zählerstand – originals/0000003.txt")


def test_import_paperless_skips_deleted_items_unless_asked(cli_env, samples, tmp_path, capsys):
    from docushelf.config import load_settings
    from docushelf.services import Services

    export = _write_paperless_export(tmp_path / "export", samples)
    code, out, _ = run(capsys, "import-paperless", str(export))
    assert code == 0 and "[1/1] im Eingang: #1" in out
    services = Services(load_settings(cli_env))
    try:
        [item] = services.inbox.list("arbeit")
        services.trash.put_inbox_item(item, user="anna")
    finally:
        services.index.close()

    code, out, _ = run(capsys, "import-paperless", str(export))
    assert code == 0
    assert "[1/1] übersprungen: #1" in out and "(im Papierkorb (gelöscht))" in out
    assert "1 Dokument wurde in DocuShelf gelöscht und liegt im Papierkorb – mit --include-deleted" in out

    code, out, _ = run(capsys, "import-paperless", str(export), "--include-deleted")
    assert code == 0 and "[1/1] im Eingang: #1" in out


def test_import_paperless_points_out_an_untouched_old_default_mapping(cli_env, data_layout, samples, tmp_path, capsys):
    from docushelf.importers.paperless import MAPPING_FILENAME
    from tests.test_import_fixes import PREVIOUS_DEFAULT

    export = _write_paperless_export(tmp_path / "export", samples)
    path = Path(data_layout.config) / MAPPING_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PREVIOUS_DEFAULT, encoding="utf-8")
    code, _, err = run(capsys, "import-paperless", str(export), "--dry-run")
    assert code == 0
    assert "unveränderten Vorgaben einer älteren Version" in err
    assert path.read_text(encoding="utf-8") == PREVIOUS_DEFAULT  # never overwritten

    path.write_text(PREVIOUS_DEFAULT + "\n# angepasst\n", encoding="utf-8")
    code, _, err = run(capsys, "import-paperless", str(export), "--dry-run")
    assert code == 0 and "älteren Version" not in err


def test_serve_real_app_in_thread(cli_env, data_layout, monkeypatch, capsys):
    """Real uvicorn and the real application: health check, lock, status and shutdown."""
    port = free_port()
    monkeypatch.setenv("DOCUSHELF_PORT", str(port))
    monkeypatch.setenv("DOCUSHELF_HOST", "127.0.0.1")
    servers = []
    real_server = cli._uvicorn_server

    def capture(config):
        server = real_server(config)
        servers.append(server)
        return server

    monkeypatch.setattr(cli, "_uvicorn_server", capture)
    result: dict = {}
    thread = threading.Thread(target=lambda: result.setdefault("code", cli.main(["serve"])), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while not (servers and servers[0].started):
            assert thread.is_alive(), "serve ended early"
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.05)
        assert cli.main(["healthcheck"]) == 0
        assert cli.main(["rebuild"]) == cli.EXIT_ERROR  # refused: the server runs
        assert cli.main(["status", "--json"]) == 0
        out = capsys.readouterr().out
        status = json.loads(out[out.index("{"):])
        assert status["server"]["running"] and status["server"]["port"] == port
    finally:
        if servers:
            servers[0].should_exit = True
        thread.join(30)
    assert not thread.is_alive()
    assert result["code"] == 0
    assert cli.probe_server(data_layout) is None


def test_serve_subprocess_stops_on_sigterm(cli_env, data_layout, tmp_path):
    """The container stops the server with SIGTERM: graceful shutdown, lock released."""
    port = free_port()
    environment = {key: value for key, value in os.environ.items() if not key.startswith("DOCUSHELF_")}
    environment.update(cli_env)
    environment.update({"DOCUSHELF_PORT": str(port), "DOCUSHELF_HOST": "127.0.0.1", "DOCUSHELF_LOG_LEVEL": "info"})
    log_path = tmp_path / "serve.log"
    with open(log_path, "wb") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "docushelf", "serve"],
            cwd=Path(cli.__file__).resolve().parents[1],
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 60
            while not cli.check_health(f"http://127.0.0.1:{port}/healthz", timeout=1)[0]:
                assert process.poll() is None, log_path.read_text(errors="replace")
                assert time.monotonic() < deadline, "server did not become healthy"
                time.sleep(0.2)
            assert cli.probe_server(data_layout) is not None
            process.send_signal(signal.SIGTERM)
            returncode = process.wait(timeout=30)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(10)
    log_text = log_path.read_text(errors="replace")
    assert returncode in (0, -signal.SIGTERM), log_text
    assert "Application startup complete" in log_text
    assert "Application shutdown complete" in log_text
    assert cli.probe_server(data_layout) is None


def test_serve_subprocess_port_in_use(cli_env, data_layout, tmp_path):
    """A port that is taken ends the server with exit code 1 and a readable log line."""
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    environment = {key: value for key, value in os.environ.items() if not key.startswith("DOCUSHELF_")}
    environment.update(cli_env)
    environment.update({"DOCUSHELF_PORT": str(port), "DOCUSHELF_HOST": "127.0.0.1"})
    try:
        result = subprocess.run(
            [sys.executable, "-m", "docushelf", "serve"],
            cwd=Path(cli.__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        blocker.close()
    assert result.returncode == 1, result.stderr
    assert "address already in use" in result.stderr.lower()
    assert cli.probe_server(data_layout) is None


def test_import_plan_file_is_loaded_and_passed(cli_env, fake_importer, fake_services, capsys, tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text('{"documents": [{"pk": 2, "folder": "Wohnen/Miete", "title": "Mietvertrag"}]}', encoding="utf-8")
    code, out, err = run(capsys, "import-paperless", "/import", "--dry-run", "--plan", str(plan), "--only-planned")
    assert code == 0, err
    importer = fake_importer.instances[0]
    assert set(importer.overrides) == {2} and importer.overrides[2].folder == "Wohnen/Miete"
    assert importer.only_planned is True
    assert "Zuordnungsplan" in out


def test_import_with_a_broken_plan_stops_before_anything_happens(cli_env, fake_importer, fake_services, capsys, tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text('{"documents": [{"pk": 2, "title": "ohne Ordner"}]}', encoding="utf-8")
    code, out, err = run(capsys, "import-paperless", "/import", "--plan", str(plan))
    assert code != 0
    assert "folder" in (out + err)
    assert not getattr(fake_importer.instances[0], "run_args", None)
