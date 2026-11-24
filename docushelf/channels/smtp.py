"""Built-in SMTP receiver for scan-to-mail.

Printers send their scans directly to DocuShelf, without an external mail
provider. The receiver is deliberately strict:

* connections are only accepted from ``allowed_networks`` (empty: from nobody),
* mail is only accepted for known recipients – nothing is ever relayed or
  forwarded anywhere. With one household that is the configured
  ``recipient``; with several households a resolver maps each recipient
  address to the household it belongs to (``smtp_recipients`` in
  ``households.yaml``), and unknown recipients are refused,
* messages above ``max_message_bytes`` are refused (552),
* when a user name and password are configured, AUTH PLAIN/LOGIN is required
  (offered without TLS; the transport is expected to run inside a VPN).

Accepted mail is written as a batch into ``eingang/quellen/smtp`` of every
household it is addressed to (once per household). A mail without PDF or
image attachment is refused, so the sender learns about it.

The receiver is global: one listening socket per process, however many
households there are.
"""

from __future__ import annotations

import asyncio
import functools
import hmac
import ipaddress
import logging
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

from aiosmtpd.controller import Controller
from aiosmtpd.smtp import SMTP, AuthResult, LoginPassword

from ..config import SmtpConfig
from ..errors import ConfigError
from ..util.timeutil import now_iso
from .mailparse import extract_documents, mail_key, normalize_address, parse_message
from .sendung import write_sendung

if TYPE_CHECKING:  # pragma: no cover
    from ..services import Services

# Maps a (normalized) recipient address to the Services of the household it
# belongs to, or None if the address is unknown.
Resolver = Callable[[str], "Services | None"]

log = logging.getLogger(__name__)

Network = ipaddress.IPv4Network | ipaddress.IPv6Network

REPLY_DENIED = "554 5.7.1 Access denied"
REPLY_AUTH_REQUIRED = "530 5.7.0 Authentication required"
REPLY_TEMPORARY = "451 4.3.0 Local error, please try again later"
REPLY_NO_DOCUMENT = "550 5.6.0 No PDF or image attachment found"
# Kept ASCII on purpose: some scanners cannot display anything else.
REPLY_UNKNOWN_HOUSEHOLD = "550 5.1.1 <{address}>: Diese Adresse ist keinem Haushalt zugeordnet"
REPLY_UNKNOWN_RECIPIENT = "550 5.1.1 <{address}>: Recipient address rejected"


def parse_networks(entries: Iterable[str]) -> list[Network]:
    """Parse ``allowed_networks`` ("192.168.100.0/24", "192.0.2.6", "fd00::/8")."""
    networks: list[Network] = []
    for entry in entries:
        text = str(entry).strip()
        if not text:
            continue
        try:
            networks.append(ipaddress.ip_network(text, strict=False))
        except ValueError as exc:
            raise ConfigError(f"SMTP_ALLOWED_NETWORKS: ungültiger Eintrag „{text}“") from exc
    return networks


def peer_ip(peer: Any) -> str | None:
    """IP address of an asyncio peer name (``("1.2.3.4", 5678)`` or the IPv6 variant)."""
    if isinstance(peer, (tuple, list)) and peer:
        return str(peer[0])
    if isinstance(peer, str):
        return peer
    return None


class _SMTP(SMTP):
    """aiosmtpd's SMTP session, refusing connections from outside the allowed networks."""

    # Some scanners send overlong lines; accept them instead of failing the scan.
    line_length_limit = 64 * 1024

    def __init__(self, handler: Any, *, receiver: "SmtpReceiver", **kwargs: Any):
        self._receiver = receiver
        super().__init__(handler, **kwargs)

    async def _handle_client(self) -> None:
        peer = self.session.peer if self.session is not None else None
        if not self._receiver.peer_allowed(peer):
            local = self.transport.get_extra_info("sockname") if self.transport is not None else None
            self._receiver._note_refused_connection(peer, local)
            try:
                await self.push(REPLY_DENIED)
            except Exception:  # the client may already be gone
                pass
            if self.transport is not None:
                self.transport.close()
            return
        await super()._handle_client()


class _Controller(Controller):
    """Controller serving on a socket that was bound beforehand (bind errors surface at once)."""

    def __init__(self, handler: Any, *, receiver: "SmtpReceiver", sock: socket.socket, **kwargs: Any):
        self._receiver = receiver
        self._sock = sock
        host, port = sock.getsockname()[:2]
        connect_host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
        super().__init__(handler, hostname=connect_host, port=port, **kwargs)

    def factory(self) -> SMTP:
        return _SMTP(self.handler, receiver=self._receiver, **self.SMTP_kwargs)

    def _create_server(self):  # type: ignore[override]
        return self.loop.create_server(self._factory_invoker, sock=self._sock, ssl=self.ssl_context)


class _Handler:
    """aiosmtpd hooks: envelope checks and storing of accepted mail."""

    def __init__(self, receiver: "SmtpReceiver"):
        self.receiver = receiver

    def _refusal(self, session: Any) -> str | None:
        if not self.receiver.peer_allowed(session.peer):
            return REPLY_DENIED
        if self.receiver.auth_required and not session.authenticated:
            return REPLY_AUTH_REQUIRED
        return None

    async def handle_MAIL(self, server: SMTP, session: Any, envelope: Any, address: str, mail_options: list[str]) -> str:
        refusal = self._refusal(session)
        if refusal:
            return refusal
        envelope.mail_from = address
        envelope.mail_options.extend(mail_options)
        return "250 OK"

    async def handle_RCPT(self, server: SMTP, session: Any, envelope: Any, address: str, rcpt_options: list[str]) -> str:
        refusal = self._refusal(session)
        if refusal:
            return refusal
        if not self.receiver.recipient_allowed(address):
            if self.receiver.multi_household:
                log.warning("SMTP: refusing recipient %r from %s (no household has this address)", address, peer_ip(session.peer))
                return REPLY_UNKNOWN_HOUSEHOLD.format(address=address)
            log.warning("SMTP: refusing recipient %r from %s (only %r is accepted)", address, peer_ip(session.peer), self.receiver.recipient)
            return REPLY_UNKNOWN_RECIPIENT.format(address=address)
        envelope.rcpt_tos.append(address)
        envelope.rcpt_options.extend(rcpt_options)
        return "250 OK"

    async def handle_DATA(self, server: SMTP, session: Any, envelope: Any) -> str:
        refusal = self._refusal(session)
        if refusal:
            return refusal
        raw = envelope.original_content
        if raw is None:
            content = envelope.content
            raw = content.encode("utf-8", "surrogateescape") if isinstance(content, str) else content
        if not raw:
            return REPLY_NO_DOCUMENT
        loop = asyncio.get_running_loop()
        store = functools.partial(
            self.receiver.deliver,
            bytes(raw),
            list(envelope.rcpt_tos),
            envelope_from=envelope.mail_from,
            peer=peer_ip(session.peer),
        )
        try:
            return await loop.run_in_executor(self.receiver._executor, store)
        except Exception:
            log.exception("SMTP: cannot store a received mail")
            self.receiver.last_error = "Empfangene Mail konnte nicht gespeichert werden"
            return REPLY_TEMPORARY


class SmtpReceiver:
    """Runs the SMTP server in its own thread (aiosmtpd controller)."""

    min_image_bytes: int = 20_000
    retry_seconds: float = 30.0

    def __init__(
        self,
        services: "Services | None" = None,
        *,
        config: SmtpConfig | None = None,
        resolver: Resolver | None = None,
        multi_household: bool = False,
    ):
        """Create the receiver.

        Single household (the classic form): ``SmtpReceiver(services)`` accepts
        only ``settings.smtp.recipient`` and stores into ``services``.

        Several households: ``SmtpReceiver(config=..., resolver=...)`` routes
        every recipient through ``resolver`` (see :func:`registry_resolver`);
        ``multi_household`` selects the German refusal for unknown addresses.
        """
        self.services = services
        if config is None and services is not None:
            config = services.settings.smtp
        if config is None:
            raise ValueError("SMTP is not configured")
        if services is None and resolver is None:
            raise ValueError("SMTP receiver needs the services or a recipient resolver")
        self.config: SmtpConfig = config
        self.recipient = config.recipient.strip().strip("<>").lower()
        self.multi_household = bool(multi_household)
        self._resolver: Resolver = resolver if resolver is not None else self._single_resolver
        self.networks = parse_networks(config.allowed_networks)
        self.auth_required = bool(config.username and config.password)
        self._username = (config.username or "").encode("utf-8")
        self._password = (config.password or "").encode("utf-8")
        self._lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._stop = threading.Event()
        self._controller: _Controller | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._retry_thread: threading.Thread | None = None
        self.port: int | None = None
        self.last_error: str | None = None
        self.last_received: str | None = None
        self.accepted = 0

    # -- lifecycle ----------------------------------------------------------------------
    def start(self) -> None:
        """Start listening. If the address cannot be bound yet, retry in the background."""
        with self._lock:
            if self._controller is not None or (self._retry_thread is not None and self._retry_thread.is_alive()):
                return
            self._stop.clear()
            if not self.networks:
                log.warning("SMTP receiver: SMTP_ALLOWED_NETWORKS is empty, every connection is refused")
            try:
                self._start_locked()
            except OSError as exc:
                self.last_error = f"SMTP-Empfang kann {self.config.host}:{self.config.port} nicht öffnen: {exc}"
                log.error("SMTP receiver cannot listen on %s:%s: %s (retrying)", self.config.host, self.config.port, exc)
                self._retry_thread = threading.Thread(target=self._retry, name="smtp-retry", daemon=True)
                self._retry_thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            retry = self._retry_thread
            self._retry_thread = None
            controller = self._controller
            self._controller = None
            executor = self._executor
            self._executor = None
        if retry is not None:
            retry.join(timeout=5)
        if controller is not None:
            try:
                controller.stop()
            except Exception:
                log.exception("error while stopping the SMTP receiver")
        if executor is not None:
            # Let mails that are being written finish (they were not acknowledged yet).
            executor.shutdown(wait=True)
        if controller is not None:
            log.info("SMTP receiver stopped")
        self.port = None

    @property
    def running(self) -> bool:
        return self._controller is not None

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "host": self.config.host,
            "port": self.port or self.config.port,
            "recipient": self.recipient,
            "multi_household": self.multi_household,
            "accepted": self.accepted,
            "last_received": self.last_received,
            "last_error": self.last_error,
        }

    def _retry(self) -> None:
        delay = self.retry_seconds
        while not self._stop.wait(delay):
            with self._lock:
                if self._stop.is_set() or self._controller is not None:
                    return
                try:
                    self._start_locked()
                    self.last_error = None
                    return
                except OSError as exc:
                    log.warning("SMTP receiver still cannot listen on %s:%s: %s", self.config.host, self.config.port, exc)
            delay = min(delay * 2, 600.0)

    def _start_locked(self) -> None:
        sock = _bind(self.config.host, self.config.port)
        kwargs: dict[str, Any] = {
            "server_hostname": "docushelf",
            "ident": "DocuShelf ESMTP",
            "data_size_limit": int(self.config.max_message_bytes),
            "enable_SMTPUTF8": True,
            "timeout": 300,
        }
        if self.auth_required:
            kwargs.update(authenticator=self._authenticate, auth_require_tls=False)
        executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="smtp-store")
        self._executor = executor
        controller = _Controller(_Handler(self), receiver=self, sock=sock, **kwargs)
        mail_log = logging.getLogger("mail.log")
        if mail_log.level == logging.NOTSET:
            # aiosmtpd logs every protocol line at INFO.
            mail_log.setLevel(logging.WARNING)
        try:
            controller.start()
        except BaseException:
            self._executor = None
            executor.shutdown(wait=False)
            try:
                sock.close()
            except OSError:
                pass
            raise
        self._controller = controller
        self.port = controller.port
        log.info(
            "SMTP receiver listening on %s:%s for %s%s",
            self.config.host,
            self.port,
            "the households' addresses" if self.multi_household else self.recipient,
            " (AUTH required)" if self.auth_required else "",
        )

    # -- policy -------------------------------------------------------------------------------
    def peer_allowed(self, peer: Any) -> bool:
        ip_text = peer_ip(peer)
        if not ip_text or not self.networks:
            return False
        try:
            address = ipaddress.ip_address(ip_text.split("%", 1)[0])
        except ValueError:
            return False
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        return any(address.version == net.version and address in net for net in self.networks)

    def recipient_allowed(self, address: str | None) -> bool:
        return self.resolve(address) is not None

    def resolve(self, address: str | None) -> "Services | None":
        """The Services of the household that ``address`` belongs to (None: refuse it)."""
        normalized = normalize_address(address)
        if not normalized:
            return None
        try:
            return self._resolver(normalized)
        except Exception:
            log.exception("SMTP: cannot resolve the recipient %r", normalized)
            return None

    def _single_resolver(self, address: str) -> "Services | None":
        return self.services if address == self.recipient else None

    def _note_refused_connection(self, peer: Any, local: Any) -> None:
        remote, own = peer_ip(peer), peer_ip(local)
        if remote and remote == own:
            # The controller connects to itself once at start-up.
            log.debug("SMTP: refused local connection from %s", remote)
        else:
            log.warning("SMTP: refused connection from %s (not in SMTP_ALLOWED_NETWORKS)", remote or "?")

    def _authenticate(self, server: Any, session: Any, envelope: Any, mechanism: str, auth_data: Any) -> AuthResult:
        if mechanism not in ("PLAIN", "LOGIN") or not isinstance(auth_data, LoginPassword):
            return AuthResult(success=False, handled=False)
        user_ok = hmac.compare_digest(bytes(auth_data.login), self._username)
        password_ok = hmac.compare_digest(bytes(auth_data.password), self._password)
        if user_ok and password_ok:
            return AuthResult(success=True)
        log.warning("SMTP: failed authentication from %s", peer_ip(session.peer))
        return AuthResult(success=False, handled=False)

    # -- storing -------------------------------------------------------------------------------
    def deliver(
        self,
        raw: bytes,
        recipients: Iterable[str],
        *,
        envelope_from: str | None = None,
        peer: str | None = None,
    ) -> str:
        """Store an accepted mail once for every household among ``recipients``.

        Returns one SMTP reply for the whole transaction: the first refusal or
        error wins, so the client retries (households that already stored the
        mail recognize the copy by its key).
        """
        recipients = list(recipients)
        targets: list[Any] = []
        for address in recipients:
            services = self.resolve(address)
            if services is not None and not any(services is known for known in targets):
                targets.append(services)
        if not targets:
            # Accepted at RCPT time, but its household is gone now.
            log.warning("SMTP: no household for the recipients %s any more", recipients)
            return REPLY_TEMPORARY
        replies = [self.store(raw, envelope_from=envelope_from, peer=peer, services=services) for services in targets]
        for reply in replies:
            if not reply.startswith("2"):
                return reply
        return replies[0]

    def store(
        self,
        raw: bytes,
        *,
        envelope_from: str | None = None,
        peer: str | None = None,
        services: "Services | None" = None,
    ) -> str:
        """Write an accepted mail as a batch into one household and return the SMTP reply."""
        services = services if services is not None else self.services
        if services is None:
            raise ValueError("no household to store the mail in")
        household = _household_label(services)
        message = parse_message(raw)
        key = mail_key(message, raw)
        index = services.index
        if index.mail_seen(key):
            log.info("SMTP: mail from %s was received before%s, ignoring the copy", peer, household)
            return "250 2.0.0 OK (already received)"
        documents, context = extract_documents(message, min_image_bytes=self.min_image_bytes)
        if not documents:
            log.warning("SMTP: mail from %s (%s) has no PDF or image attachment, refused", peer, context.get("mail_from"))
            return REPLY_NO_DOCUMENT
        if not context.get("mail_from") and envelope_from:
            context["mail_from"] = normalize_address(envelope_from)
        # Scan-to-mail: the device sending the mail is not the author of the document.
        context["forwarded_by_owner"] = True
        batch = write_sendung(services.layout.source_dir("smtp"), documents, context=context)
        index.mark_mail_seen(key)
        with self._stats_lock:
            self.accepted += 1
            self.last_received = now_iso()
            self.last_error = None
        log.info("SMTP: received %d document(s) from %s%s (batch %s)", len(documents), peer, household, batch.name)
        intake = getattr(services, "intake", None)
        if intake is not None:
            try:
                intake.trigger()
            except Exception:
                log.debug("cannot wake the intake", exc_info=True)
        return f"250 2.0.0 OK: queued as {batch.name}"


def _household_label(services: Any) -> str:
    """`` for household <id>`` for log lines, empty with a single household."""
    household = getattr(services, "household", None)
    household_id = getattr(household, "id", None)
    if not household_id or household_id == "default":
        return ""
    return f" for household {household_id}"


def registry_resolver(registry: Any, recipient: str | None = None) -> Resolver:
    """Recipient resolver for a household registry.

    An address listed in a household's ``smtp_recipients`` goes to that
    household. The global ``DOCUSHELF_SMTP_RECIPIENT`` (``recipient``) keeps
    working: with a single household it goes there, with several to the
    household whose path is the data root itself (the installation from
    before households existed), if there is one. Everything else is unknown.
    """
    global_recipient = normalize_address(recipient) if recipient else None

    def resolve(address: str) -> "Services | None":
        normalized = normalize_address(address)
        if not normalized:
            return None
        household = registry.by_smtp_recipient(normalized)
        if household is None and global_recipient and normalized == global_recipient:
            household = _root_household(registry)
        if household is None:
            return None
        return registry.services(household.id)

    return resolve


def _root_household(registry: Any) -> Any:
    """The only household, or the one living in the data root itself (None if there is none)."""
    default = getattr(registry, "default", None)
    if default is not None:
        return default
    if len(registry.households) == 1:
        return registry.households[0]
    settings = getattr(registry, "settings", None)
    data_dir = getattr(settings, "data_dir", None)
    if data_dir is None:
        return None
    root = _resolved(Path(data_dir))
    for household in registry.households:
        if _resolved(Path(household.path)) == root:
            return household
    return None


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def receiver_for_registry(registry: Any, config: SmtpConfig) -> SmtpReceiver:
    """The global SMTP receiver of a household registry."""
    return SmtpReceiver(
        config=config,
        resolver=registry_resolver(registry, config.recipient),
        multi_household=bool(getattr(registry, "multi", len(registry.households) > 1)),
    )


def _bind(host: str, port: int) -> socket.socket:
    """Create the listening socket (IPv4, IPv6 or dual stack for "::")."""
    host = (host or "0.0.0.0").strip("[]")
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    if not infos:
        raise OSError(f"cannot resolve {host}")
    family, _type, _proto, _canon, address = infos[0]
    dualstack = family == socket.AF_INET6 and host == "::" and socket.has_dualstack_ipv6()
    return socket.create_server(address, family=family, backlog=16, dualstack_ipv6=dualstack)
