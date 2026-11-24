"""Fetch documents from a dedicated IMAP mailbox.

Every ``poll_seconds`` the fetcher logs in, looks at all messages in the
configured folder and

* moves mail from senders outside the allowlist to ``rejected_folder``,
* writes the attachments of accepted mail as a batch into
  ``eingang/quellen/mail`` (see :mod:`docushelf.channels.sendung`) and moves
  the mail to ``processed_folder``,
* moves mail without usable attachments to ``no_attachment_folder``.

Mail is never deleted: moving uses ``UID MOVE`` where available, otherwise
``COPY`` + ``\\Deleted`` + an expunge that is limited to the copied message
(``UID EXPUNGE``). A message is remembered by a key in the index
(``mail_seen``) before it is moved, so an interrupted run never imports a
mail twice.

Every household has its own fetcher (``services.settings.imap`` is the
household's mailbox, see ``households.settings_for``); a fetcher only ever
writes into the drop zone of its own household.

Folder names may contain spaces and non-ASCII characters; they are sent
quoted and in IMAP's modified UTF-7. ``/`` in configured names is translated
to the server's hierarchy delimiter and the personal namespace prefix (e.g.
``INBOX.``) is added where the server needs one.
"""

from __future__ import annotations

import base64
import hashlib
import imaplib
import logging
import re
import socket
import ssl
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from ..config import ImapConfig
from ..errors import DocuShelfError
from ..util.timeutil import now_iso
from .origin import mark as mark_origin
from .mailparse import extract_documents, looks_forwarded, mail_key, parse_message, sender_allowed, sender_of
from .sendung import write_sendung

if TYPE_CHECKING:  # pragma: no cover
    from ..services import Services

log = logging.getLogger(__name__)

ConnectionFactory = Callable[[ImapConfig], Any]

# How often a message that fails to process is retried before it is left alone
# (until the next restart).
MAX_MESSAGE_ATTEMPTS = 3


class ImapError(DocuShelfError):
    """The IMAP server refused an operation (message is German, shown in the UI)."""


# ---------------------------------------------------------------------------
# Mailbox names (RFC 3501 section 5.1.3: modified UTF-7)


def encode_mailbox(name: str) -> str:
    """Encode a folder name in IMAP's modified UTF-7."""
    result: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            raw = "".join(pending).encode("utf-16-be")
            result.append("&" + base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",") + "-")
            pending.clear()

    for char in name:
        if 0x20 <= ord(char) <= 0x7E:
            flush()
            result.append("&-" if char == "&" else char)
        else:
            pending.append(char)
    flush()
    return "".join(result)


def decode_mailbox(name: str) -> str:
    """Decode a folder name from IMAP's modified UTF-7 (invalid sequences stay as they are)."""
    result: list[str] = []
    index = 0
    while index < len(name):
        char = name[index]
        if char != "&":
            result.append(char)
            index += 1
            continue
        end = name.find("-", index + 1)
        if end < 0:
            result.append(name[index:])
            break
        chunk = name[index + 1 : end]
        if not chunk:
            result.append("&")
        else:
            try:
                padded = chunk.replace(",", "/") + "=" * (-len(chunk) % 4)
                result.append(base64.b64decode(padded, validate=True).decode("utf-16-be"))
            except (ValueError, UnicodeDecodeError):
                result.append(name[index : end + 1])
        index = end + 1
    return "".join(result)


def quote_mailbox(name: str) -> str:
    """Folder name as an IMAP quoted string (modified UTF-7 inside)."""
    encoded = encode_mailbox(name)
    return '"' + encoded.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(value: bytes) -> bytes:
    value = value.strip()
    if len(value) >= 2 and value[:1] == b'"' and value[-1:] == b'"':
        return re.sub(rb"\\(.)", rb"\1", value[1:-1])
    return value


_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.*)$', re.IGNORECASE)
_NAMESPACE_RE = re.compile(r'^\s*\(\(\s*"(?P<prefix>(?:[^"\\]|\\.)*)"\s+(?:"(?P<delim>(?:[^"\\]|\\.)*)"|NIL)', re.IGNORECASE)


def parse_list_response(data: list[Any] | None) -> list[tuple[set[str], str | None, str]]:
    """``(flags, delimiter, decoded name)`` for every entry of a LIST response."""
    entries: list[tuple[set[str], str | None, str]] = []
    for item in data or []:
        if item is None:
            continue
        literal: bytes | None = None
        head = item
        if isinstance(item, tuple):
            head = item[0]
            literal = item[1] if len(item) > 1 and isinstance(item[1], (bytes, bytearray)) else None
        if isinstance(head, str):
            head = head.encode("utf-8", "surrogateescape")
        if not isinstance(head, (bytes, bytearray)):
            continue
        match = _LIST_RE.match(bytes(head).strip())
        if not match:
            continue
        flags = {flag.decode("ascii", "replace").lower() for flag in match.group("flags").split()}
        delimiter_raw = match.group("delim")
        delimiter = None if delimiter_raw.upper() == b"NIL" else _unquote(delimiter_raw).decode("utf-8", "replace")
        if literal is not None:
            name_bytes = bytes(literal)
        else:
            raw_name = match.group("name").strip()
            if raw_name.startswith(b"{"):
                continue
            name_bytes = _unquote(raw_name)
        entries.append((flags, delimiter, decode_mailbox(name_bytes.decode("utf-8", "replace"))))
    return entries


def _response_text(data: list[Any] | None) -> str:
    parts = []
    for item in data or []:
        if isinstance(item, tuple):
            item = item[0]
        if isinstance(item, (bytes, bytearray)):
            parts.append(bytes(item).decode("utf-8", "replace"))
        elif item is not None:
            parts.append(str(item))
    return " ".join(parts).strip()


NO_STARTTLS_MESSAGE = (
    "Der IMAP-Server bietet kein STARTTLS an. Benutzername und Passwort würden unverschlüsselt übertragen, "
    "deshalb wurde die Verbindung abgebrochen. Nutze implizites TLS (DOCUSHELF_IMAP_SSL=true, meist Port 993) "
    "oder erlaube unverschlüsselte Verbindungen ausdrücklich mit DOCUSHELF_IMAP_ALLOW_PLAINTEXT=true "
    "(nur in einem vertrauenswürdigen Netz)."
)


def default_connection(config: ImapConfig, *, timeout: float = 60.0) -> imaplib.IMAP4:
    """Open an encrypted connection: implicit TLS, or STARTTLS on a plain port.

    Certificates are verified. A plain-port server that does not offer STARTTLS
    is refused with an :class:`ImapError` unless ``config.allow_plaintext`` is
    set: the login would cross the network in clear, and an attacker on the way
    can remove STARTTLS from the server's capabilities. A failing STARTTLS never
    falls back to plain text.
    """
    context = ssl.create_default_context()
    if config.ssl:
        return imaplib.IMAP4_SSL(config.host, config.port, ssl_context=context, timeout=timeout)
    connection = imaplib.IMAP4(config.host, config.port, timeout=timeout)
    try:
        if "STARTTLS" in getattr(connection, "capabilities", ()):
            connection.starttls(ssl_context=context)
        elif config.allow_plaintext:
            log.warning(
                "IMAP server %s offers no STARTTLS; logging in without encryption (IMAP_ALLOW_PLAINTEXT is set)",
                config.host,
            )
        else:
            raise ImapError(NO_STARTTLS_MESSAGE)
    except BaseException:
        # Nothing else is sent over a connection that is not what it should be.
        try:
            connection.shutdown()
        except Exception:
            pass
        raise
    return connection


# ---------------------------------------------------------------------------
# One logged-in connection


@dataclass
class _Session:
    conn: Any
    capabilities: set[str]
    delimiter: str | None
    prefix: str
    flagged: list[str] = field(default_factory=list)

    def resolve(self, name: str) -> str:
        """Server-side name for a configured folder ("DocuShelf/Verarbeitet")."""
        cleaned = name.strip().strip("/")
        if cleaned.upper() == "INBOX":
            return "INBOX"
        if self.delimiter and self.delimiter != "/":
            cleaned = cleaned.replace("/", self.delimiter)
        if self.prefix and not cleaned.upper().startswith(self.prefix.upper()):
            cleaned = self.prefix + cleaned
        return cleaned


# ---------------------------------------------------------------------------
# The fetcher


class ImapFetcher:
    """Polls the IMAP mailbox in a background thread."""

    # Tunables (instances may override them, tests do).
    min_poll_seconds: float = 10.0
    backoff_base: float = 5.0
    backoff_max: float = 900.0
    notify_after_failures: int = 5
    min_image_bytes: int = 20_000

    def __init__(
        self,
        services: "Services",
        *,
        connection_factory: ConnectionFactory | None = None,
        config: ImapConfig | None = None,
    ):
        self.services = services
        if config is None:
            config = services.settings.imap
        if config is None:
            raise ValueError("IMAP is not configured")
        self.config: ImapConfig = config
        household = getattr(services, "household", None)
        self.household_id: str | None = getattr(household, "id", None)
        self.household_name: str | None = getattr(household, "name", None)
        self._factory: ConnectionFactory = connection_factory or default_connection
        self._stop = threading.Event()
        self._wakeup = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._fetch_lock = threading.Lock()
        self._conn: Any = None
        self._message_failures: dict[str, int] = {}
        self._failures_in_row = 0
        self.last_run: str | None = None
        self.last_success: str | None = None
        self.last_error: str | None = None
        self.last_count = 0
        self.imported_total = 0

    # -- lifecycle -------------------------------------------------------------------
    def start(self) -> None:
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if not self.config.allowed_senders:
                log.warning(
                    "IMAP sender allowlist%s is empty: every mail is moved to %r (set IMAP_ALLOWED_SENDERS)",
                    self._label,
                    self.config.rejected_folder,
                )
            self._stop.clear()
            self._wakeup.clear()
            name = f"imap-fetcher-{self.household_id}" if self._label else "imap-fetcher"
            if self.config.name:
                name = f"{name}-{self.config.name}"
            self._thread = threading.Thread(target=self._run, name=name, daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 15.0) -> None:
        with self._state_lock:
            thread = self._thread
            self._thread = None
            self._stop.set()
            self._wakeup.set()
        if thread is None:
            return
        self._abort_connection()
        thread.join(timeout)
        if thread.is_alive():
            log.warning("IMAP fetcher did not stop within %.0f s", timeout)

    def trigger(self) -> None:
        """Fetch now instead of waiting for the next poll."""
        self._wakeup.set()

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def _label(self) -> str:
        """`` of household <id>`` for log lines; empty with a single household."""
        if not self.household_id or self.household_id == "default":
            return ""
        return f" of household {self.household_id}"

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "household": self.household_id,
            "name": self.config.name,
            "host": self.config.host,
            "folder": self.config.folder,
            "last_run": self.last_run,
            "last_success": self.last_success,
            "last_error": self.last_error,
            "last_count": self.last_count,
            "imported_total": self.imported_total,
        }

    def _run(self) -> None:
        log.info(
            "IMAP fetcher%s started (%s@%s, folder %s)", self._label, self.config.username, self.config.host, self.config.folder
        )
        while not self._stop.is_set():
            delay = self._poll()
            if self._stop.is_set():
                break
            self._wakeup.wait(delay)
            self._wakeup.clear()
        log.info("IMAP fetcher%s stopped", self._label)

    def _poll(self) -> float:
        """One polling round; returns the delay until the next one."""
        try:
            self._fetch_round(interruptible=True)
        except Exception as exc:  # the thread must survive every error
            if self._stop.is_set():
                return 0.0
            self._failures_in_row += 1
            self.last_error = describe_error(exc)
            delay = min(self.backoff_max, self.backoff_base * (2 ** (self._failures_in_row - 1)))
            if isinstance(exc, (OSError, imaplib.IMAP4.error, ImapError)):
                log.warning("IMAP fetch%s failed: %s (retry in %.0f s)", self._label, exc, delay)
            else:
                log.exception("IMAP fetch%s failed unexpectedly (retry in %.0f s)", self._label, delay)
            if self._failures_in_row == self.notify_after_failures:
                where = f" ({self.household_name or self.household_id})" if self._label else ""
                self.services.notifier.send(
                    f"DocuShelf: Mail-Abruf gestört{where}",
                    f"Der IMAP-Abruf ist {self._failures_in_row}-mal hintereinander fehlgeschlagen: {self.last_error}",
                )
            return delay
        if self._failures_in_row:
            log.info("IMAP fetch%s works again after %d failures", self._label, self._failures_in_row)
        self._failures_in_row = 0
        return max(self.min_poll_seconds, float(self.config.poll_seconds))

    # -- one round -----------------------------------------------------------------------
    def fetch_once(self) -> int:
        """Process all mail in the configured folder once. Returns the number of imported mails."""
        return self._fetch_round(interruptible=False)

    def _fetch_round(self, *, interruptible: bool) -> int:
        with self._fetch_lock:
            self.last_run = now_iso()
            session = self._open()
            try:
                self._ensure_folders(session)
                self._select(session, self.config.folder)
                imported = 0
                for uid in self._search(session, "UNDELETED"):
                    if interruptible and self._stop.is_set():
                        break
                    if self._process(session, uid):
                        imported += 1
                self._finish(session)
            finally:
                self._close(session.conn)
            self.last_count = imported
            self.imported_total += imported
            self.last_success = now_iso()
            self.last_error = None
            return imported

    def _open(self) -> _Session:
        conn = self._factory(self.config)
        self._conn = conn
        try:
            capabilities = self._capabilities(conn)
            self._login(conn, capabilities)
            capabilities = self._capabilities(conn) or capabilities
            delimiter, prefix = self._namespace(conn, capabilities)
        except BaseException:
            self._close(conn)
            raise
        return _Session(conn=conn, capabilities=capabilities, delimiter=delimiter, prefix=prefix)

    def _close(self, conn: Any) -> None:
        if self._conn is conn:
            self._conn = None
        try:
            conn.logout()
        except Exception:
            pass

    def _abort_connection(self) -> None:
        """Unblock a thread waiting on the server (used by ``stop``)."""
        conn = self._conn
        sock = getattr(conn, "sock", None)
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    # -- protocol helpers --------------------------------------------------------------------
    @staticmethod
    def _capabilities(conn: Any) -> set[str]:
        try:
            typ, data = conn.capability()
        except Exception:
            return {str(c).upper() for c in getattr(conn, "capabilities", ())}
        words: set[str] = set()
        if typ == "OK":
            for item in data or []:
                if isinstance(item, (bytes, bytearray)):
                    item = bytes(item).decode("ascii", "replace")
                words.update(word.upper() for word in str(item).split())
        return words

    def _login(self, conn: Any, capabilities: set[str]) -> None:
        user, password = self.config.username, self.config.password
        try:
            if (user + password).isascii() or "AUTH=PLAIN" not in capabilities:
                conn.login(user, password)
            else:
                payload = f"\0{user}\0{password}".encode("utf-8")
                conn.authenticate("PLAIN", lambda _challenge: payload)
        except imaplib.IMAP4.abort:
            raise
        except imaplib.IMAP4.error as exc:
            raise ImapError(f"Anmeldung am IMAP-Server fehlgeschlagen ({_short(exc)})") from exc

    @staticmethod
    def _namespace(conn: Any, capabilities: set[str]) -> tuple[str | None, str]:
        prefix = ""
        delimiter: str | None = None
        if "NAMESPACE" in capabilities and hasattr(conn, "namespace"):
            try:
                typ, data = conn.namespace()
                text = _response_text(data) if typ == "OK" else ""
                match = _NAMESPACE_RE.match(text)
                if match:
                    prefix = decode_mailbox(re.sub(r"\\(.)", r"\1", match.group("prefix")))
                    if match.group("delim") is not None:
                        delimiter = re.sub(r"\\(.)", r"\1", match.group("delim"))
            except (imaplib.IMAP4.error, ValueError):
                log.debug("NAMESPACE failed", exc_info=True)
        if delimiter is None:
            try:
                typ, data = conn.list('""', '""')
                if typ == "OK":
                    for _flags, found, _name in parse_list_response(data):
                        if found:
                            delimiter = found
                            break
            except imaplib.IMAP4.error:
                log.debug("LIST for the hierarchy delimiter failed", exc_info=True)
        return (delimiter or "/"), prefix

    def _ensure_folders(self, session: _Session) -> None:
        typ, data = session.conn.list('""', "*")
        if typ != "OK":
            raise ImapError(f"IMAP-Ordnerliste nicht lesbar: {_response_text(data)}")
        existing = {name for _flags, _delim, name in parse_list_response(data)}
        source = session.resolve(self.config.folder)
        for folder in (self.config.processed_folder, self.config.rejected_folder, self.config.no_attachment_folder):
            resolved = session.resolve(folder)
            if resolved == source:
                raise ImapError(f"IMAP-Zielordner „{folder}“ darf nicht der abgerufene Ordner sein")
            self._create_with_parents(session, resolved, existing)

    def _create_with_parents(self, session: _Session, resolved: str, existing: set[str]) -> None:
        delimiter = session.delimiter or "/"
        parts = resolved.split(delimiter)
        for depth in range(1, len(parts) + 1):
            name = delimiter.join(parts[:depth])
            if not name or name in existing or name.upper() == "INBOX":
                continue
            if session.prefix and session.prefix.startswith(name + delimiter):
                continue  # the namespace itself ("INBOX." → "INBOX")
            typ, data = session.conn.create(quote_mailbox(name))
            if typ != "OK":
                text = _response_text(data)
                if "ALREADYEXISTS" not in text.upper():
                    raise ImapError(f"IMAP-Ordner „{name}“ kann nicht angelegt werden: {text}")
            else:
                log.info("created IMAP folder %r", name)
                try:
                    session.conn.subscribe(quote_mailbox(name))
                except Exception:  # subscribing is a convenience for mail clients
                    log.debug("cannot subscribe to %r", name, exc_info=True)
            existing.add(name)

    def _select(self, session: _Session, folder: str) -> None:
        resolved = session.resolve(folder)
        typ, data = session.conn.select(quote_mailbox(resolved))
        if typ != "OK":
            raise ImapError(f"IMAP-Ordner „{folder}“ kann nicht geöffnet werden: {_response_text(data)}")

    @staticmethod
    def _search(session: _Session, criterion: str) -> list[int]:
        typ, data = session.conn.uid("SEARCH", criterion)
        if typ != "OK":
            raise ImapError(f"IMAP-Suche fehlgeschlagen: {_response_text(data)}")
        uids: set[int] = set()
        for chunk in data or []:
            if isinstance(chunk, (bytes, bytearray)):
                chunk = bytes(chunk).decode("ascii", "replace")
            if not chunk:
                continue
            uids.update(int(word) for word in str(chunk).split() if word.isdigit())
        return sorted(uids)

    @staticmethod
    def _fetch(session: _Session, uid: int) -> bytes | None:
        typ, data = session.conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if typ != "OK":
            raise ImapError(f"Mail {uid} kann nicht gelesen werden: {_response_text(data)}")
        for item in data or []:
            if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], (bytes, bytearray)):
                return bytes(item[1])
        return None  # vanished in the meantime

    def _move(self, session: _Session, uid: int, folder: str) -> bool:
        """Move a message without ever deleting it. Returns False if the server refused."""
        target = quote_mailbox(session.resolve(folder))
        conn = session.conn
        if "MOVE" in session.capabilities:
            typ, data = conn.uid("MOVE", str(uid), target)
            if typ == "OK":
                return True
            log.warning("IMAP MOVE of mail %s to %r failed: %s", uid, folder, _response_text(data))
            return False
        typ, data = conn.uid("COPY", str(uid), target)
        if typ != "OK":
            log.warning("IMAP COPY of mail %s to %r failed: %s", uid, folder, _response_text(data))
            return False
        typ, data = conn.uid("STORE", str(uid), "+FLAGS.SILENT", r"(\Deleted)")
        if typ != "OK":
            log.warning("mail %s was copied to %r but cannot be flagged: %s", uid, folder, _response_text(data))
            return False
        if "UIDPLUS" in session.capabilities:
            typ, data = conn.uid("EXPUNGE", str(uid))
            if typ != "OK":
                log.warning("UID EXPUNGE of mail %s failed: %s", uid, _response_text(data))
        else:
            session.flagged.append(str(uid))
        return True

    def _finish(self, session: _Session) -> None:
        """Without UIDPLUS: expunge only if no other message is flagged as deleted."""
        if not session.flagged:
            return
        flagged_by_us = {int(uid) for uid in session.flagged}
        try:
            deleted = set(self._search(session, "DELETED"))
        except ImapError:
            return
        if deleted <= flagged_by_us:
            typ, data = session.conn.expunge()
            if typ != "OK":
                log.warning("IMAP EXPUNGE failed: %s", _response_text(data))
        else:
            log.info(
                "not expunging: %d other message(s) are flagged as deleted; moved mails stay flagged in %r",
                len(deleted - flagged_by_us),
                self.config.folder,
            )

    # -- one message ----------------------------------------------------------------------------
    def _process(self, session: _Session, uid: int) -> bool:
        """Handle one message; True if it was imported."""
        raw = self._fetch(session, uid)
        if raw is None:
            return False
        failure_key = _sha256(raw)
        if self._message_failures.get(failure_key, 0) >= MAX_MESSAGE_ATTEMPTS:
            return False
        index = self.services.index
        try:
            message = parse_message(raw)
            key = mail_key(message, raw)
            if index.mail_seen(key):
                log.info("mail %s was imported before, moving it to %r", uid, self.config.processed_folder)
                self._move(session, uid, self.config.processed_folder)
                return False
            _name, address = sender_of(message)
            if not sender_allowed(address, self.config.allowed_senders):
                log.warning("mail %s from %s is not on the allowlist, moving it to %r", uid, address or "?", self.config.rejected_folder)
                self._move(session, uid, self.config.rejected_folder)
                return False
            documents, context = extract_documents(message, min_image_bytes=self.min_image_bytes)
            context["forwarded_by_owner"] = self._forwarded_by_owner(address, message, context)
            if self.config.owner:
                # A mailbox of one member: the documents are theirs.
                mark_origin(context, owner=self.config.owner, endpoint=f"Postfach „{self.config.name or self.config.username}“")
        except (imaplib.IMAP4.error, OSError):
            raise
        except Exception:
            attempts = self._message_failures.get(failure_key, 0) + 1
            self._message_failures[failure_key] = attempts
            log.exception("cannot process mail %s (attempt %d of %d)", uid, attempts, MAX_MESSAGE_ATTEMPTS)
            return False
        if not documents:
            log.info("mail %s from %s has no usable attachment, moving it to %r", uid, address, self.config.no_attachment_folder)
            self._move(session, uid, self.config.no_attachment_folder)
            return False
        # Local write errors (disk full ...) abort the round; the mail stays and is retried.
        batch = write_sendung(self.services.layout.source_dir("mail"), documents, context=context)
        index.mark_mail_seen(key)
        log.info(
            "imported mail %s from %s%s (%d document(s), batch %s)",
            uid,
            address,
            self._label.replace(" of ", " into ", 1),
            len(documents),
            batch.name,
        )
        self._move(session, uid, self.config.processed_folder)
        self._wake_intake()
        return True

    def _forwarded_by_owner(self, address: str | None, message: Any, context: dict[str, Any]) -> bool:
        """True if the mail sender is not the author of the documents.

        That is the case for forwarded mail and for mail from addresses that are
        listed individually in the allowlist (the household's own addresses or
        a scanner sending through a mail provider).
        """
        if looks_forwarded(message, context):
            return True
        listed = {str(entry).strip().strip("<>").lower() for entry in self.config.allowed_senders}
        return bool(address) and address in listed

    def _wake_intake(self) -> None:
        intake = getattr(self.services, "intake", None)
        if intake is None:
            return
        try:
            intake.trigger()
        except Exception:
            log.debug("cannot wake the intake", exc_info=True)


@dataclass
class ImapProbe:
    """Result of a connection test (``message`` is German, for the web interface)."""

    ok: bool
    message: str
    messages: int | None = None


def probe_connection(config: ImapConfig, *, connection_factory: ConnectionFactory | None = None) -> ImapProbe:
    """Log in, open the configured folder read-only and log out again.

    Nothing in the mailbox is changed: no folders are created and no mail is
    moved. Never raises; errors are described in German.
    """
    from types import SimpleNamespace

    fetcher = ImapFetcher(
        SimpleNamespace(household=None, settings=None), config=config, connection_factory=connection_factory
    )
    try:
        session = fetcher._open()
    except Exception as exc:
        return ImapProbe(False, describe_error(exc))
    try:
        resolved = session.resolve(config.folder)
        typ, data = session.conn.select(quote_mailbox(resolved), readonly=True)
        if typ != "OK":
            return ImapProbe(
                False,
                f"Anmeldung klappt, aber der Ordner „{config.folder}“ lässt sich nicht öffnen: {_response_text(data)}",
            )
        count: int | None = None
        text = _response_text(data)
        if text.split() and text.split()[0].isdigit():
            count = int(text.split()[0])
        found = f" mit {count} Nachricht{'' if count == 1 else 'en'}" if count is not None else ""
        return ImapProbe(
            True, f"Verbindung klappt: angemeldet als {config.username}, Ordner „{config.folder}“{found}.", count
        )
    except Exception as exc:
        return ImapProbe(False, describe_error(exc))
    finally:
        fetcher._close(session.conn)


def describe_error(exc: BaseException) -> str:
    """German description of a fetch error for the UI."""
    if isinstance(exc, ImapError):
        return str(exc)
    if isinstance(exc, ssl.SSLError):
        return f"TLS-Fehler bei der Verbindung zum IMAP-Server: {_short(exc)}"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "Zeitüberschreitung bei der Verbindung zum IMAP-Server"
    if isinstance(exc, imaplib.IMAP4.abort):
        return f"Verbindung zum IMAP-Server abgebrochen: {_short(exc)}"
    if isinstance(exc, imaplib.IMAP4.error):
        return f"IMAP-Fehler: {_short(exc)}"
    if isinstance(exc, OSError):
        return f"IMAP-Server nicht erreichbar: {_short(exc)}"
    return f"Unerwarteter Fehler beim Mail-Abruf: {_short(exc)}"


def _short(exc: BaseException) -> str:
    text = str(exc)
    if text.startswith(("b'", 'b"')) and text.endswith(("'", '"')):
        text = text[2:-1]
    return text.strip() or type(exc).__name__


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
