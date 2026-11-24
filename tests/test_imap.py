"""Tests for the IMAP fetcher (channels/imap.py) against an in-memory IMAP server."""

from __future__ import annotations

import imaplib
import re
import socket
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timezone

import pytest

from docushelf.channels.imap import (
    ImapError,
    ImapFetcher,
    decode_mailbox,
    default_connection,
    describe_error,
    encode_mailbox,
    parse_list_response,
    quote_mailbox,
)
from docushelf.channels.mailparse import mail_key, parse_message
from docushelf.channels.sendung import read_sendung
from docushelf.config import ImapConfig, load_settings
from tests.conftest import make_text_pdf

# -- a small IMAP server in memory ----------------------------------------------------------------


class FakeMail:
    def __init__(self, uid: int, raw: bytes, flags=()):
        self.uid = uid
        self.raw = raw
        self.flags = set(flags)


class FakeImapServer:
    """Enough IMAP semantics for the fetcher, with the quoting rules of a real server."""

    def __init__(
        self,
        *,
        capabilities=("IMAP4rev1", "MOVE", "UIDPLUS", "NAMESPACE"),
        delimiter: str = "/",
        prefix: str = "",
        username: str = "docs@example.org",
        password: str = "secret",
    ):
        self.capabilities = list(capabilities)
        self.delimiter = delimiter
        self.prefix = prefix
        self.username = username
        self.password = password
        self.folders: dict[str, list[FakeMail]] = {"INBOX": []}  # names as on the wire (modified UTF-7)
        self.subscribed: set[str] = set()
        self.expunged: list[FakeMail] = []
        self.commands: list[tuple] = []
        self.next_uid = 1
        self.connections = 0
        self.fail_next_connects = 0
        self.lock = threading.Lock()

    def deliver(self, raw: bytes, folder: str = "INBOX", flags=()) -> FakeMail:
        mail = FakeMail(self.next_uid, raw, flags)
        self.next_uid += 1
        self.folders[folder].append(mail)
        return mail

    def connect(self, config: ImapConfig) -> "FakeImapConnection":
        with self.lock:
            if self.fail_next_connects > 0:
                self.fail_next_connects -= 1
                raise ConnectionRefusedError(61, "Connection refused")
            self.connections += 1
        return FakeImapConnection(self)

    def raws(self, folder: str) -> list[bytes]:
        return [mail.raw for mail in self.folders.get(folder, [])]

    def all_raws(self) -> list[bytes]:
        return sorted(raw for box in self.folders.values() for raw in (m.raw for m in box))


class FakeImapConnection:
    def __init__(self, server: FakeImapServer):
        self.server = server
        self.authenticated = False
        self.selected: str | None = None

    # helpers
    def _mailbox(self, arg) -> str:
        if isinstance(arg, bytes):
            arg = arg.decode()
        if arg.startswith('"'):
            assert arg.endswith('"') and len(arg) >= 2
            name = re.sub(r"\\(.)", r"\1", arg[1:-1])
        else:
            if " " in arg or not arg:
                raise imaplib.IMAP4.error("BAD Invalid mailbox name syntax")
            name = arg
        if not name.isascii():
            raise imaplib.IMAP4.error("BAD Mailbox names must be modified UTF-7")
        return "INBOX" if name.upper() == "INBOX" else name

    def _require_auth(self):
        if not self.authenticated:
            raise imaplib.IMAP4.error("command illegal in state NONAUTH")

    def _find(self, uid) -> FakeMail | None:
        for mail in self.server.folders[self.selected]:
            if mail.uid == int(uid):
                return mail
        return None

    # commands
    def capability(self):
        return "OK", [" ".join(self.server.capabilities).encode()]

    def login(self, user, password):
        if user != self.server.username or password != self.server.password:
            raise imaplib.IMAP4.error("b'[AUTHENTICATIONFAILED] Authentication failed.'")
        self.authenticated = True
        return "OK", [b"Logged in"]

    def namespace(self):
        self._require_auth()
        return "OK", [f'(("{self.server.prefix}" "{self.server.delimiter}")) NIL NIL'.encode()]

    def list(self, directory='""', pattern="*"):
        self._require_auth()
        delim = self.server.delimiter
        if pattern == '""':
            return "OK", [f'(\\Noselect) "{delim}" ""'.encode()]
        data: list = []
        for name in self.server.folders:
            if " " in name:  # some servers send such names as literals
                data.append((f'(\\HasNoChildren) "{delim}" {{{len(name)}}}'.encode(), name.encode()))
                data.append(b"")
            else:
                data.append(f'(\\HasNoChildren) "{delim}" "{name}"'.encode())
        return "OK", data

    def create(self, mailbox):
        self._require_auth()
        name = self._mailbox(mailbox)
        self.server.commands.append(("CREATE", name))
        if name in self.server.folders:
            return "NO", [b"[ALREADYEXISTS] Mailbox already exists"]
        if self.server.prefix and not name.startswith(self.server.prefix):
            return "NO", [b"[CANNOT] Invalid mailbox name"]
        self.server.folders[name] = []
        return "OK", [b"Create completed"]

    def subscribe(self, mailbox):
        self.server.subscribed.add(self._mailbox(mailbox))
        return "OK", [b"Subscribe completed"]

    def select(self, mailbox, readonly=False):
        self._require_auth()
        name = self._mailbox(mailbox)
        if name not in self.server.folders:
            return "NO", [b"Mailbox doesn't exist"]
        self.selected = name
        return "OK", [str(len(self.server.folders[name])).encode()]

    def uid(self, command, *args):
        self._require_auth()
        command = command.upper()
        self.server.commands.append((command,) + args)
        if self.selected is None:
            raise imaplib.IMAP4.error("No mailbox selected")
        box = self.server.folders[self.selected]
        caps = self.server.capabilities
        if command == "SEARCH":
            criterion = " ".join(a for a in args if a).upper()
            if criterion == "UNDELETED":
                uids = [m.uid for m in box if "\\Deleted" not in m.flags]
            elif criterion == "DELETED":
                uids = [m.uid for m in box if "\\Deleted" in m.flags]
            else:
                uids = [m.uid for m in box]
            return "OK", [" ".join(map(str, uids)).encode()]
        if command == "FETCH":
            assert "PEEK" in args[1].upper(), "fetching must not set the \\Seen flag"
            mail = self._find(args[0])
            if mail is None:
                return "OK", [None]
            return "OK", [(f"1 (UID {mail.uid} BODY[] {{{len(mail.raw)}}}".encode(), mail.raw), b")"]
        if command in ("MOVE", "COPY"):
            if command == "MOVE" and "MOVE" not in caps:
                raise imaplib.IMAP4.error("BAD Unknown command MOVE")
            target = self._mailbox(args[1])
            if target not in self.server.folders:
                return "NO", [b"[TRYCREATE] Mailbox doesn't exist"]
            mail = self._find(args[0])
            if mail is None:
                return "NO", [b"No such message"]
            copy = FakeMail(self.server.next_uid, mail.raw, mail.flags - {"\\Deleted"})
            self.server.next_uid += 1
            self.server.folders[target].append(copy)
            if command == "MOVE":
                box.remove(mail)
            return "OK", [f"{command} completed".encode()]
        if command == "STORE":
            mail = self._find(args[0])
            flags = set(args[2].strip("()").split())
            if args[1].upper().startswith("+FLAGS"):
                mail.flags |= flags
            return "OK", [b"Store completed"]
        if command == "EXPUNGE":
            if "UIDPLUS" not in caps:
                raise imaplib.IMAP4.error("BAD UID EXPUNGE needs UIDPLUS")
            mail = self._find(args[0])
            if mail is not None and "\\Deleted" in mail.flags:
                box.remove(mail)
                self.server.expunged.append(mail)
            return "OK", [b"Expunge completed"]
        raise imaplib.IMAP4.error(f"BAD unknown UID command {command}")

    def expunge(self):
        self.server.commands.append(("EXPUNGE",))
        box = self.server.folders[self.selected]
        removed = [m for m in box if "\\Deleted" in m.flags]
        box[:] = [m for m in box if "\\Deleted" not in m.flags]
        self.server.expunged.extend(removed)
        return "OK", [b"Expunge completed"]

    def logout(self):
        self.authenticated = False
        return "BYE", [b"Logging out"]


class FakeIntake:
    def __init__(self):
        self.triggered = 0

    def trigger(self):
        self.triggered += 1


# -- fixtures -------------------------------------------------------------------------------------------


@pytest.fixture
def make_services(base_services):
    def build(**overrides):
        values = dict(
            host="imap.example.org",
            username="docs@example.org",
            password="secret",
            allowed_senders=("max@example.org", "@stadtwerke.example"),
            poll_seconds=60,
        )
        values.update(overrides)
        base_services.settings = base_services.settings.with_overrides(imap=ImapConfig(**values))
        base_services.intake = FakeIntake()
        return base_services

    return build


@pytest.fixture
def services(make_services):
    return make_services()


@pytest.fixture
def mail(tmp_path):
    counter = {"n": 0}

    def build(sender="Max <max@example.org>", subject="Rechnung", *, attach=True, message_id=None) -> bytes:
        counter["n"] += 1
        n = counter["n"]
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = "docs@example.org"
        msg["Subject"] = subject
        msg["Date"] = format_datetime(datetime(2025, 3, n % 28 + 1, 9, 0, tzinfo=timezone.utc))
        msg["Message-ID"] = message_id or f"<mail-{n}@example.org>"
        msg.set_content("Hallo")
        if attach:
            pdf = make_text_pdf(tmp_path / f"m{n}.pdf", [f"Rechnung {n}"]).read_bytes()
            msg.add_attachment(pdf, maintype="application", subtype="pdf", filename=f"Rechnung {n}.pdf")
        return msg.as_bytes()

    return build


def batches(services) -> list:
    root = services.layout.source_dir("mail")
    return [read_sendung(path) for path in sorted(root.iterdir())] if root.exists() else []


# -- mailbox names -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "plain,encoded",
    [
        ("INBOX", "INBOX"),
        ("DocuShelf/Ohne Anhang", "DocuShelf/Ohne Anhang"),
        ("Dokumente/Bestätigt", "Dokumente/Best&AOQ-tigt"),
        ("Tom & Jerry", "Tom &- Jerry"),
        ("~peter/mail/台北/日本語", "~peter/mail/&U,BTFw-/&ZeVnLIqe-"),
        ("Grüße 📄", "Gr&APwA3w-e &2D3cxA-"),
    ],
)
def test_modified_utf7(plain, encoded):
    assert encode_mailbox(plain) == encoded
    assert decode_mailbox(encoded) == plain


def test_quote_mailbox_escapes():
    assert quote_mailbox('A "B"\\C') == '"A \\"B\\"\\\\C"'
    assert quote_mailbox("Ablage/Geprüft") == '"Ablage/Gepr&APw-ft"'


def test_parse_list_response_variants():
    data = [
        b'(\\HasNoChildren) "/" "INBOX"',
        b"(\\HasNoChildren) \".\" INBOX.Archiv",
        (b'(\\HasNoChildren) "/" {11}', b"Ohne Anhang"),
        b"",
        b'(\\Noselect) NIL "Flat"',
        b'(\\HasNoChildren) "/" "Best&AOQ-tigt"',
    ]
    entries = parse_list_response(data)
    assert [(d, n) for _, d, n in entries] == [
        ("/", "INBOX"),
        (".", "INBOX.Archiv"),
        ("/", "Ohne Anhang"),
        (None, "Flat"),
        ("/", "Bestätigt"),
    ]
    assert "\\noselect" in entries[3][0]


# -- fetching -----------------------------------------------------------------------------------------------


def test_imports_mail_and_moves_it_to_processed(services, mail):
    server = FakeImapServer()
    raw = mail()
    server.deliver(raw)
    fetcher = ImapFetcher(services, connection_factory=server.connect)

    assert fetcher.fetch_once() == 1

    [batch] = batches(services)
    assert [doc.files for doc in batch.documents] == [["Rechnung 1.pdf"]]
    assert (batch.directory / "Rechnung 1.pdf").read_bytes().startswith(b"%PDF-")
    assert batch.context["mail_from"] == "max@example.org"
    assert batch.context["mail_subject"] == "Rechnung"
    assert batch.context["forwarded_by_owner"] is True  # listed address = the household itself
    assert server.raws("INBOX") == []
    assert server.raws("DocuShelf/Verarbeitet") == [raw]
    assert {"DocuShelf", "DocuShelf/Verarbeitet", "DocuShelf/Abgelehnt", "DocuShelf/Ohne Anhang"} <= set(server.folders)
    assert "DocuShelf/Ohne Anhang" in server.subscribed
    assert services.index.mail_seen(mail_key(parse_message(raw), raw))
    assert services.intake.triggered == 1
    assert not server.expunged
    assert fetcher.last_error is None and fetcher.last_count == 1


def test_domain_allowlist_and_sender_context(services, mail):
    server = FakeImapServer()
    server.deliver(mail("Stadtwerke <rechnung@stadtwerke.example>", "Ihre Abschlagsrechnung"))
    fetcher = ImapFetcher(services, connection_factory=server.connect)
    assert fetcher.fetch_once() == 1
    [batch] = batches(services)
    assert batch.context["mail_from"] == "rechnung@stadtwerke.example"
    assert batch.context["forwarded_by_owner"] is False


def test_rejected_sender_is_moved_not_deleted(services, mail):
    server = FakeImapServer()
    raw = mail("Spam <offer@spam.example>")
    server.deliver(raw)
    fetcher = ImapFetcher(services, connection_factory=server.connect)
    assert fetcher.fetch_once() == 0
    assert batches(services) == []
    assert server.raws("DocuShelf/Abgelehnt") == [raw]
    assert server.raws("INBOX") == []


def test_empty_allowlist_rejects_everything(make_services, mail):
    services = make_services(allowed_senders=())
    server = FakeImapServer()
    server.deliver(mail())
    assert ImapFetcher(services, connection_factory=server.connect).fetch_once() == 0
    assert len(server.raws("DocuShelf/Abgelehnt")) == 1


def test_mail_without_documents_goes_to_its_folder(services, mail):
    server = FakeImapServer()
    raw = mail(attach=False)
    server.deliver(raw)
    assert ImapFetcher(services, connection_factory=server.connect).fetch_once() == 0
    assert server.raws("DocuShelf/Ohne Anhang") == [raw]
    assert batches(services) == []


def test_mail_seen_before_is_only_moved(services, mail):
    # Simulates a crash after the batch was written but before the mail was moved.
    server = FakeImapServer()
    raw = mail()
    server.deliver(raw)
    services.index.mark_mail_seen(mail_key(parse_message(raw), raw))
    assert ImapFetcher(services, connection_factory=server.connect).fetch_once() == 0
    assert batches(services) == []
    assert server.raws("DocuShelf/Verarbeitet") == [raw]


def test_copy_and_uid_expunge_without_move(services, mail):
    server = FakeImapServer(capabilities=("IMAP4rev1", "UIDPLUS"))
    ours = mail()
    foreign = mail("someone@else.example", "von Hand gelöscht")
    server.deliver(ours)
    server.deliver(foreign, flags={"\\Deleted"})  # deleted by another client, not yet expunged

    assert ImapFetcher(services, connection_factory=server.connect).fetch_once() == 1

    assert server.raws("DocuShelf/Verarbeitet") == [ours]
    assert server.raws("INBOX") == [foreign]  # untouched: never expunge foreign messages
    assert ("EXPUNGE",) not in server.commands
    assert [m.raw for m in server.expunged] == [ours]


def test_without_uidplus_foreign_deleted_messages_block_expunge(services, mail):
    server = FakeImapServer(capabilities=("IMAP4rev1",))
    ours = mail()
    foreign = mail("someone@else.example")
    server.deliver(ours)
    server.deliver(foreign, flags={"\\Deleted"})
    fetcher = ImapFetcher(services, connection_factory=server.connect)

    assert fetcher.fetch_once() == 1
    assert ("EXPUNGE",) not in server.commands
    assert sorted(server.raws("INBOX")) == sorted([ours, foreign])  # ours stays, flagged
    assert server.raws("DocuShelf/Verarbeitet") == [ours]
    # The flagged copy is not imported again.
    assert fetcher.fetch_once() == 0
    assert len(batches(services)) == 1


def test_without_uidplus_expunges_when_only_own_messages_are_flagged(services, mail):
    server = FakeImapServer(capabilities=("IMAP4rev1",))
    ours = mail()
    server.deliver(ours)
    assert ImapFetcher(services, connection_factory=server.connect).fetch_once() == 1
    assert ("EXPUNGE",) in server.commands
    assert server.raws("INBOX") == []
    assert server.raws("DocuShelf/Verarbeitet") == [ours]


def test_folder_names_with_spaces_and_umlauts(make_services, mail):
    services = make_services(
        processed_folder="Dokumente/Übernommen",
        rejected_folder="Dokumente/Nicht erlaubt",
        no_attachment_folder="Dokumente/Ohne Anhänge",
    )
    server = FakeImapServer()
    allowed, rejected, empty = mail(), mail("x@spam.example"), mail(attach=False)
    for raw in (allowed, rejected, empty):
        server.deliver(raw)

    assert ImapFetcher(services, connection_factory=server.connect).fetch_once() == 1

    assert server.raws("Dokumente/&ANw-bernommen") == [allowed]
    assert server.raws("Dokumente/Nicht erlaubt") == [rejected]
    assert server.raws("Dokumente/Ohne Anh&AOQ-nge") == [empty]
    # Existing folders (also those listed as literals) are not created again.
    creates = [c for c in server.commands if c[0] == "CREATE"]
    ImapFetcher(services, connection_factory=server.connect).fetch_once()
    assert [c for c in server.commands if c[0] == "CREATE"] == creates


def test_namespace_prefix_and_dot_delimiter(services, mail):
    server = FakeImapServer(delimiter=".", prefix="INBOX.")
    raw = mail()
    server.deliver(raw)
    assert ImapFetcher(services, connection_factory=server.connect).fetch_once() == 1
    assert server.raws("INBOX.DocuShelf.Verarbeitet") == [raw]
    assert "INBOX.DocuShelf.Ohne Anhang" in server.folders


def test_source_folder_other_than_inbox(make_services, mail):
    services = make_services(folder="Scans")
    server = FakeImapServer()
    server.folders["Scans"] = []
    server.deliver(mail(), folder="Scans")
    server.deliver(mail())  # INBOX is not looked at
    assert ImapFetcher(services, connection_factory=server.connect).fetch_once() == 1
    assert len(server.raws("INBOX")) == 1


def test_missing_source_folder_is_reported_in_german(make_services):
    services = make_services(folder="Gibt es nicht")
    fetcher = ImapFetcher(services, connection_factory=FakeImapServer().connect)
    with pytest.raises(Exception) as excinfo:
        fetcher.fetch_once()
    assert "kann nicht geöffnet werden" in str(excinfo.value)


def test_target_folder_must_differ_from_the_source(make_services, mail):
    services = make_services(processed_folder="inbox")
    server = FakeImapServer()
    raw = mail()
    server.deliver(raw)
    with pytest.raises(Exception, match="darf nicht der abgerufene Ordner sein"):
        ImapFetcher(services, connection_factory=server.connect).fetch_once()
    assert server.raws("INBOX") == [raw]


def test_message_that_breaks_processing_is_left_alone(services, mail, monkeypatch):
    server = FakeImapServer()
    raw = mail()
    server.deliver(raw)
    fetcher = ImapFetcher(services, connection_factory=server.connect)

    def broken(*args, **kwargs):
        raise RuntimeError("parser bug")

    monkeypatch.setattr("docushelf.channels.imap.extract_documents", broken)
    for _ in range(4):
        assert fetcher.fetch_once() == 0
    assert server.raws("INBOX") == [raw]
    fetches = [c for c in server.commands if c[0] == "FETCH"]
    assert len(fetches) == 4  # fetched, but no longer processed after three attempts
    monkeypatch.undo()


def test_local_write_errors_keep_the_mail(services, mail, monkeypatch):
    server = FakeImapServer()
    raw = mail()
    server.deliver(raw)
    fetcher = ImapFetcher(services, connection_factory=server.connect)

    def disk_full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("docushelf.channels.imap.write_sendung", disk_full)
    with pytest.raises(OSError):
        fetcher.fetch_once()
    assert server.raws("INBOX") == [raw]
    assert not services.index.mail_seen(mail_key(parse_message(raw), raw))
    monkeypatch.undo()
    assert fetcher.fetch_once() == 1
    assert server.raws("DocuShelf/Verarbeitet") == [raw]


def test_mail_is_never_lost(services, mail):
    server = FakeImapServer(capabilities=("IMAP4rev1", "UIDPLUS"))
    raws = [mail(), mail("x@spam.example"), mail(attach=False), mail("Stadtwerke <a@stadtwerke.example>")]
    for raw in raws:
        server.deliver(raw)
    ImapFetcher(services, connection_factory=server.connect).fetch_once()
    remaining = server.all_raws()
    assert sorted(raws) == remaining


def test_login_failure_is_described_in_german(make_services):
    services = make_services(password="falsch")
    fetcher = ImapFetcher(services, connection_factory=FakeImapServer().connect)
    with pytest.raises(Exception) as excinfo:
        fetcher.fetch_once()
    assert "Anmeldung am IMAP-Server fehlgeschlagen" in str(excinfo.value)


def test_utf8_password_uses_authenticate(make_services):
    services = make_services(password="pässwort")
    calls = []

    class Conn(FakeImapConnection):
        def authenticate(self, mechanism, callback):
            calls.append((mechanism, callback(b"")))
            self.authenticated = True
            return "OK", [b"Authenticated"]

    server = FakeImapServer(capabilities=("IMAP4rev1", "MOVE", "AUTH=PLAIN"), password="pässwort")
    fetcher = ImapFetcher(services, connection_factory=lambda config: Conn(server))
    assert fetcher.fetch_once() == 0
    assert calls == [("PLAIN", "\0docs@example.org\0pässwort".encode())]


# -- connection security (real imaplib against a plain-text socket server) ----------------------------------


class PlainSocketServer:
    """A tiny IMAP server without TLS on a local port; records every line it receives."""

    def __init__(self, capabilities: str = "IMAP4rev1 AUTH=PLAIN", starttls_reply: bytes = b"NO TLS not available"):
        self.capabilities = capabilities.encode()
        self.starttls_reply = starttls_reply
        self.received: list[bytes] = []
        self.disconnected = threading.Event()
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(10)
                reader = conn.makefile("rb")
                conn.sendall(b"* OK IMAP4rev1 ready\r\n")
                try:
                    for line in reader:
                        self.received.append(line)
                        tag, command = (line.split() + [b"", b""])[:2]
                        command = command.upper()
                        if command == b"CAPABILITY":
                            conn.sendall(b"* CAPABILITY " + self.capabilities + b"\r\n" + tag + b" OK done\r\n")
                        elif command == b"STARTTLS":
                            conn.sendall(tag + b" " + self.starttls_reply + b"\r\n")
                        elif command == b"LOGOUT":
                            conn.sendall(b"* BYE\r\n" + tag + b" OK bye\r\n")
                            break
                        else:
                            conn.sendall(tag + b" OK done\r\n")
                except OSError:
                    pass
                self.disconnected.set()

    def commands(self) -> list[bytes]:
        return [(line.split() + [b"", b""])[1].upper() for line in self.received]

    def close(self) -> None:
        self.listener.close()


@pytest.fixture
def plain_server():
    servers: list[PlainSocketServer] = []

    def build(**kwargs) -> PlainSocketServer:
        server = PlainSocketServer(**kwargs)
        servers.append(server)
        return server

    yield build
    for server in servers:
        server.close()


def _plain_config(server: PlainSocketServer, **overrides) -> ImapConfig:
    values = dict(host="127.0.0.1", port=server.port, ssl=False, username="docs@example.org", password="geheim-123")
    values.update(overrides)
    return ImapConfig(**values)


def test_plain_port_without_starttls_is_refused_before_the_login(plain_server):
    server = plain_server()
    with pytest.raises(ImapError) as excinfo:
        default_connection(_plain_config(server), timeout=5)
    message = str(excinfo.value)
    assert "STARTTLS" in message and "DOCUSHELF_IMAP_ALLOW_PLAINTEXT" in message
    assert describe_error(excinfo.value) == message  # shown as is in the UI
    assert server.disconnected.wait(5)  # the connection is closed right away
    assert server.commands() == [b"CAPABILITY"]


def test_fetcher_never_sends_the_password_without_starttls(make_services, plain_server):
    server = plain_server()
    services = make_services(host="127.0.0.1", port=server.port, ssl=False, password="geheim-123")
    fetcher = ImapFetcher(services)  # the real connection factory
    with pytest.raises(ImapError):
        fetcher.fetch_once()
    assert server.disconnected.wait(5)
    assert not any(b"geheim-123" in line for line in server.received)
    assert b"LOGIN" not in server.commands() and b"AUTHENTICATE" not in server.commands()


def test_failed_starttls_never_falls_back_to_plain_text(plain_server):
    server = plain_server(capabilities="IMAP4rev1 STARTTLS AUTH=PLAIN")
    with pytest.raises(imaplib.IMAP4.error):
        default_connection(_plain_config(server, allow_plaintext=True), timeout=5)
    assert server.disconnected.wait(5)
    assert server.commands() == [b"CAPABILITY", b"STARTTLS"]


def test_starttls_is_used_with_certificate_checks(plain_server, monkeypatch):
    server = plain_server(capabilities="IMAP4rev1 STARTTLS")
    contexts = []
    monkeypatch.setattr(imaplib.IMAP4, "starttls", lambda self, ssl_context=None: contexts.append(ssl_context))
    connection = default_connection(_plain_config(server), timeout=5)
    connection.shutdown()
    assert len(contexts) == 1
    assert contexts[0].verify_mode == ssl.CERT_REQUIRED and contexts[0].check_hostname


def test_plain_text_login_only_when_explicitly_allowed(plain_server):
    server = plain_server()
    connection = default_connection(_plain_config(server, allow_plaintext=True), timeout=5)
    try:
        assert connection.login("docs@example.org", "geheim-123")[0] == "OK"
    finally:
        connection.shutdown()
    assert server.commands()[:2] == [b"CAPABILITY", b"LOGIN"]


def test_allow_plaintext_setting(tmp_path):
    base = {
        "DOCUSHELF_DATA_DIR": str(tmp_path / "data"),
        "DOCUSHELF_SECRET_KEY": "x",
        "DOCUSHELF_IMAP_ENABLED": "true",
        "DOCUSHELF_IMAP_HOST": "imap.example.org",
        "DOCUSHELF_IMAP_USER": "docs@example.org",
        "DOCUSHELF_IMAP_PASSWORD": "secret",
        "DOCUSHELF_IMAP_SSL": "false",
    }
    assert load_settings(base).imap.allow_plaintext is False
    assert load_settings({**base, "DOCUSHELF_IMAP_ALLOW_PLAINTEXT": "true"}).imap.allow_plaintext is True
    assert ImapConfig(host="h", username="u", password="p").allow_plaintext is False


# -- background thread -----------------------------------------------------------------------------------------


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def test_poll_loop_reconnects_with_backoff(services, mail):
    server = FakeImapServer()
    server.deliver(mail())
    server.fail_next_connects = 2
    fetcher = ImapFetcher(services, connection_factory=server.connect)
    fetcher.backoff_base = 0.01
    delays = []
    original = fetcher._poll

    def recording_poll():
        delay = original()
        delays.append(delay)
        return delay

    fetcher._poll = recording_poll
    fetcher.start()
    fetcher.start()  # idempotent
    try:
        assert wait_for(lambda: len(batches(services)) == 1)
    finally:
        fetcher.stop()
    assert not fetcher.running
    assert delays[:2] == [0.01, 0.02]  # exponential backoff
    assert fetcher.last_error is None
    assert server.connections == 1
    fetcher.stop()  # idempotent


def test_errors_are_kept_for_the_ui_and_notified(services, mail):
    server = FakeImapServer()
    server.fail_next_connects = 100
    sent = []
    services.notifier.send = lambda title, message, **kw: sent.append((title, message))
    fetcher = ImapFetcher(services, connection_factory=server.connect)
    fetcher.backoff_base = 0.001
    fetcher.backoff_max = 0.005
    fetcher.notify_after_failures = 3
    fetcher.start()
    try:
        assert wait_for(lambda: len(sent) == 1)
    finally:
        fetcher.stop()
    assert "nicht erreichbar" in fetcher.last_error
    assert "Mail-Abruf" in sent[0][0]


def test_trigger_wakes_the_poll_loop(services, mail):
    server = FakeImapServer()
    fetcher = ImapFetcher(services, connection_factory=server.connect)
    fetcher.min_poll_seconds = 3600
    fetcher.start()
    try:
        assert wait_for(lambda: server.connections == 1)
        server.deliver(mail())
        fetcher.trigger()
        assert wait_for(lambda: len(batches(services)) == 1)
    finally:
        fetcher.stop(timeout=5)
    assert not fetcher.running


def test_stop_interrupts_a_hanging_server(services):
    ours, theirs = socket.socketpair()
    entered = threading.Event()

    class Hanging(FakeImapConnection):
        sock = ours

        def login(self, user, password):
            entered.set()
            if not self.sock.recv(1):  # a server that never answers
                raise imaplib.IMAP4.abort("socket error: EOF")
            return super().login(user, password)

    server = FakeImapServer()
    fetcher = ImapFetcher(services, connection_factory=lambda config: Hanging(server))
    fetcher.start()
    try:
        assert entered.wait(5)
        started = time.monotonic()
        fetcher.stop(timeout=5)
        assert time.monotonic() - started < 2
        assert not fetcher.running
    finally:
        ours.close()
        theirs.close()


def test_manual_fetch_after_stop_still_works(services, mail):
    server = FakeImapServer()
    fetcher = ImapFetcher(services, connection_factory=server.connect)
    fetcher.start()
    fetcher.stop()
    server.deliver(mail())
    assert fetcher.fetch_once() == 1


# -- with the real intake ---------------------------------------------------------------------------------------


def test_batches_are_taken_over_by_the_real_intake(services, mail, tmp_path):
    intake_module = pytest.importorskip("docushelf.inbox.intake")
    server = FakeImapServer()
    msg = EmailMessage()
    msg["From"] = "Max <max@example.org>"
    msg["Subject"] = "WG: Ihre Rechnung"
    msg["Date"] = "Mon, 03 Mar 2025 10:15:00 +0100"
    msg["Message-ID"] = "<fwd-1@example.org>"
    msg.set_content("---------- Weitergeleitete Nachricht ---------\nVon: Alza.cz <faktury@alza.cz>\n")
    pdf = make_text_pdf(tmp_path / "f.pdf", ["Alza.cz a.s.", "Rechnung 2025001"]).read_bytes()
    msg.add_attachment(pdf, maintype="application", subtype="pdf", filename="Faktura 2025001.pdf")
    server.deliver(msg.as_bytes())
    assert ImapFetcher(services, connection_factory=server.connect).fetch_once() == 1

    services.worker = None
    created = intake_module.Intake(services).scan_once()
    assert len(created) == 1
    item = services.inbox.load(created[0])
    assert item.source == "mail"
    assert item.original_files == ["original/Faktura 2025001.pdf"]
    assert item.context["mail_from"] == "max@example.org"
    assert item.context["forwarded_from"] == "faktury@alza.cz"
    assert item.context["forwarded_by_owner"] is True
    assert item.context["mail_subject"] == "WG: Ihre Rechnung"
    assert list(services.layout.source_dir("mail").iterdir()) == []
