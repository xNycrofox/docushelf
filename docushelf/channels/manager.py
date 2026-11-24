"""Live management of a household's inbound channels.

One :class:`ChannelManager` exists per household (``services.channels``). It
knows the effective channel configuration – ``config/channels.yaml`` if the
household has one, otherwise the defaults from the environment and
``households.yaml`` – and keeps the running parts in line with it:

* the household's settings (``services.settings.sources`` and ``.imap``), which
  the intake reads on every pass,
* one IMAP fetcher per enabled mailbox,
* the scanner subfolders of the members (created with the drop zone's mode),
* SMTP routing: :meth:`smtp_route` answers which recipient address belongs to
  this household and to whom.

:meth:`update` changes the configuration from the web interface: it validates,
writes the file (see :class:`~docushelf.channels.store.ChannelStore`) and
applies the result at once – only this household's fetchers and intake are
restarted, nothing else.
"""

from __future__ import annotations

import logging
import os
import stat
import threading
from dataclasses import replace
from typing import Any, Callable, Mapping

from ..config import EndpointConfig, ImapConfig, Settings, SourceConfig
from ..errors import ConfigError
from . import origin
from .store import (
    SCANNER_KINDS,
    ChannelConfig,
    ChannelStore,
    Mailbox,
    Recipient,
    ScannerChannel,
    SmtpChannel,
    WatchFolder,
    check_config,
    normalize_address,
)

log = logging.getLogger(__name__)

DEFAULT_MAILBOX = "postfach"
REMOTE_STABLE_SECONDS = 10.0
SCANNER_ENDPOINT_LABEL = "Scanner"
SMTP_ENDPOINT_LABEL = "Scan per Mail"

FetcherFactory = Callable[[Any, ImapConfig], Any]


def _default_fetcher(services: Any, config: ImapConfig) -> Any:
    from .imap import ImapFetcher

    return ImapFetcher(services, config=config)


def mailbox_from_imap(config: ImapConfig, name: str = DEFAULT_MAILBOX) -> Mailbox:
    return Mailbox(
        name=config.name or name,
        host=config.host,
        user=config.username,
        port=config.port,
        tls="ssl" if config.ssl else "starttls",
        folder=config.folder,
        processed_folder=config.processed_folder,
        rejected_folder=config.rejected_folder,
        no_attachment_folder=config.no_attachment_folder,
        allowed_senders=tuple(config.allowed_senders),
        poll_seconds=int(config.poll_seconds),
        owner=config.owner,
        enabled=True,
        allow_plaintext=bool(config.allow_plaintext),
    )


def imap_config(box: Mailbox, password: str) -> ImapConfig:
    return ImapConfig(
        host=box.host,
        username=box.user,
        password=password,
        port=box.port,
        ssl=box.tls == "ssl",
        allow_plaintext=box.allow_plaintext,
        folder=box.folder,
        processed_folder=box.processed_folder,
        rejected_folder=box.rejected_folder,
        no_attachment_folder=box.no_attachment_folder,
        allowed_senders=tuple(box.allowed_senders),
        poll_seconds=box.poll_seconds,
        name=box.name,
        owner=box.owner,
    )


def defaults_from_settings(settings: Settings, household: Any = None) -> tuple[ChannelConfig, dict[str, str]]:
    """The channels an installation without ``channels.yaml`` runs with, and their passwords."""
    mailboxes: list[Mailbox] = []
    passwords: dict[str, str] = {}
    if settings.imap is not None:
        box = mailbox_from_imap(settings.imap)
        mailboxes.append(box)
        passwords[box.name] = settings.imap.password
    addresses: list[str] = []
    for address in getattr(household, "smtp_recipients", None) or ():
        normalized = normalize_address(address)
        if normalized and normalized not in addresses:
            addresses.append(normalized)
    if settings.smtp is not None and settings.smtp.recipient:
        normalized = normalize_address(settings.smtp.recipient)
        if normalized and normalized not in addresses:
            addresses.append(normalized)
    names = {source.name for source in settings.sources if source.path is None}
    scanners = tuple(ScannerChannel(kind, enabled=kind in names) for kind in SCANNER_KINDS)
    watch = []
    for source in settings.sources:
        if source.path is None:
            continue
        name = source.name[6:] if source.name.startswith("watch-") else source.name
        watch.append(WatchFolder(name=name, path=source.path, remove_after_import=source.remove_after_import))
    config = ChannelConfig(
        imap=tuple(mailboxes),
        smtp=SmtpChannel(enabled=bool(addresses), recipients=tuple(Recipient(a) for a in addresses)),
        scanners=scanners,
        watch=tuple(watch),
    )
    return config, passwords


class ChannelManager:
    """The inbound channels of one household (see the module documentation)."""

    def __init__(
        self,
        services: Any,
        settings: Settings,
        *,
        fetcher_factory: FetcherFactory | None = None,
    ):
        self.services = services
        # The settings the household was started with (environment, households.yaml).
        self.base_settings = settings
        self.household = getattr(services, "household", None)
        self.store = ChannelStore(services.layout.config)
        self.fetcher_factory: FetcherFactory = fetcher_factory or _default_fetcher
        # Called (without arguments) after every applied change, e.g. by the registry.
        self.listeners: list[Callable[[], None]] = []
        self.fetchers: dict[str, Any] = {}
        self.last_change: str | None = None
        self._lock = threading.RLock()
        self._running = False
        self._default_passwords: dict[str, str] = {}
        self.from_file = self.store.exists()
        if self.from_file:
            self.config = self.store.load()
        else:
            self.config, self._default_passwords = defaults_from_settings(settings, self.household)

    # -- effective configuration ---------------------------------------------------------
    def effective_settings(self) -> Settings:
        """The household's settings with the channel configuration applied.

        Without ``channels.yaml`` these are the base settings unchanged.
        """
        base = self.base_settings
        if not self.from_file:
            return base
        stable = {source.name: source.stable_seconds for source in base.sources}
        sources: list[SourceConfig] = [
            source for source in base.sources if source.path is None and source.name not in SCANNER_KINDS
        ]
        for scanner in self.config.scanners:
            if not scanner.enabled:
                continue
            endpoints = tuple(
                EndpointConfig(
                    folder=point.folder,
                    owner=point.owner,
                    enabled=point.enabled,
                    label=SCANNER_ENDPOINT_LABEL if point.owner else f"Scanner-Ordner „{point.folder}“",
                )
                for point in scanner.endpoints
            )
            sources.append(
                SourceConfig(
                    name=scanner.kind,
                    stable_seconds=stable.get(scanner.kind, REMOTE_STABLE_SECONDS),
                    endpoints=endpoints,
                )
            )
        for folder in self.config.watch:
            if folder.enabled:
                sources.append(
                    SourceConfig(
                        name=folder.source_name,
                        path=folder.path,
                        stable_seconds=base.stable_seconds,
                        remove_after_import=folder.remove_after_import,
                    )
                )
        configs = self.imap_configs()
        return base.with_overrides(sources=tuple(sources), imap=configs[0][1] if configs else None)

    def password(self, name: str) -> str | None:
        if not self.from_file:
            return self._default_passwords.get(name)
        return self.store.password(name)

    def has_password(self, name: str) -> bool:
        if not self.from_file:
            return bool(self._default_passwords.get(name))
        return self.store.has_password(name)

    def imap_configs(self) -> list[tuple[Mailbox, ImapConfig]]:
        """Enabled mailboxes with their connection settings (mailboxes without password are skipped)."""
        if not self.from_file:
            imap = self.base_settings.imap
            return [(self.config.imap[0], imap)] if imap is not None and self.config.imap else []
        result = []
        for box in self.config.imap:
            if not box.enabled:
                continue
            try:
                password = self.store.password(box.name)
            except ConfigError as exc:
                log.warning("mailbox %s: %s", box.name, exc)
                continue
            if not password:
                log.warning("mailbox %s has no password; it is not fetched", box.name)
                continue
            result.append((box, imap_config(box, password)))
        return result

    # -- SMTP --------------------------------------------------------------------------------
    def smtp_route(self, address: str | None) -> Recipient | None:
        """The recipient entry of this household for ``address`` (None: not ours or switched off)."""
        smtp = self.config.smtp
        if not smtp.enabled:
            return None
        return smtp.find(address)

    def adopt_global_recipient(self, address: str | None) -> None:
        """Default configuration only: the global ``DOCUSHELF_SMTP_RECIPIENT`` belongs to this household."""
        normalized = normalize_address(address)
        with self._lock:
            if self.from_file or not normalized or self.config.smtp.find(normalized) is not None:
                return
            recipients = (*self.config.smtp.recipients, Recipient(normalized))
            self.config = replace(self.config, smtp=SmtpChannel(enabled=True, recipients=recipients))

    def mark_smtp(self, context: dict[str, Any], address: str | None) -> None:
        recipient = self.smtp_route(address)
        if recipient is not None and recipient.owner:
            origin.mark(context, owner=recipient.owner, endpoint=SMTP_ENDPOINT_LABEL)

    # -- lifecycle -------------------------------------------------------------------------------
    def build(self) -> None:
        """Create the fetchers (not started) and the scanner folders; called once by Services."""
        with self._lock:
            self._ensure_endpoint_dirs()
            self._sync_fetchers()

    def start(self) -> None:
        with self._lock:
            self._running = True
            for fetcher in self.fetchers.values():
                fetcher.start()

    def stop(self) -> None:
        with self._lock:
            self._running = False
            fetchers = list(self.fetchers.values())
        for fetcher in fetchers:
            try:
                fetcher.stop()
            except Exception:
                log.exception("error while stopping an IMAP fetcher")

    @property
    def running(self) -> bool:
        return self._running

    # -- changes from the web interface ----------------------------------------------------
    def update(
        self,
        change: Callable[[ChannelConfig], ChannelConfig],
        *,
        passwords: Mapping[str, str | None] | None = None,
        renamed: Mapping[str, str] | None = None,
    ) -> ChannelConfig:
        """Change the configuration, write ``channels.yaml`` and apply it live.

        ``change`` gets the current configuration and returns the new one (it
        may raise ConfigError). ``passwords`` holds newly entered passwords by
        mailbox name; ``renamed`` maps new mailbox names to old ones, whose
        stored password is taken over. Passwords from the environment are
        copied into the secrets directory on the first save, so mailboxes keep
        working. Raises ConfigError (German) without changing anything.
        """
        renamed = dict(renamed or {})
        with self._lock:
            current = self.config
            new = change(current)
            check_config(new)
            pending = {name: value for name, value in (passwords or {}).items() if value}
            for box in new.imap:
                if box.name in pending or (self.from_file and self.store.has_password(box.name)):
                    continue
                previous = self.password(renamed.get(box.name, box.name))
                if previous:
                    pending[box.name] = previous
            removed = [box.name for box in current.imap if new.mailbox(box.name) is None]
            self.store.save(new, passwords=pending, remove_secrets=removed)
            self.config = new
            self.from_file = True
            self._default_passwords = {}
            self.apply()
        return new

    def reload(self) -> None:
        """Read ``channels.yaml`` again (e.g. after it was edited by hand) and apply it."""
        with self._lock:
            if self.store.exists():
                self.config = self.store.load()
                self.from_file = True
            else:
                self.from_file = False
                self.config, self._default_passwords = defaults_from_settings(self.base_settings, self.household)
            self.apply()

    def apply(self) -> None:
        """Bring settings, fetchers, scanner folders and intake in line with the configuration."""
        with self._lock:
            self.services.settings = self.effective_settings()
            self._ensure_endpoint_dirs()
            self._sync_fetchers()
            self._restart_intake()
        for listener in list(self.listeners):
            try:
                listener()
            except Exception:
                log.exception("error while applying the channel change")

    def _sync_fetchers(self) -> None:
        wanted = {box.name: config for box, config in self.imap_configs()}
        for name in list(self.fetchers):
            fetcher = self.fetchers[name]
            if wanted.get(name) != getattr(fetcher, "config", None):
                del self.fetchers[name]
                try:
                    fetcher.stop()
                except Exception:
                    log.exception("error while stopping the IMAP fetcher of mailbox %s", name)
        ordered: dict[str, Any] = {}
        for name, config in wanted.items():
            fetcher = self.fetchers.get(name)
            if fetcher is None:
                fetcher = self.fetcher_factory(self.services, config)
                if self._running:
                    fetcher.start()
            ordered[name] = fetcher
        self.fetchers = ordered
        # The first mailbox stays reachable the classic way (status on the settings page).
        try:
            self.services.imap = next(iter(ordered.values()), None)
        except AttributeError:
            pass

    def _restart_intake(self) -> None:
        intake = getattr(self.services, "intake", None)
        if intake is None or not getattr(intake, "running", False):
            if intake is not None and hasattr(intake, "trigger"):
                intake.trigger()
            return
        try:
            intake.stop()
            intake.start()
        except Exception:
            log.exception("cannot restart the intake")

    def _ensure_endpoint_dirs(self) -> None:
        """Create the members' scanner folders with the mode of the drop zone (e.g. 1733 for SMB)."""
        if not self.from_file:
            return
        layout = self.services.layout
        for scanner in self.config.scanners:
            if not scanner.enabled or not scanner.endpoints:
                continue
            try:
                root = layout.source_dir(scanner.kind)
                root.mkdir(parents=True, exist_ok=True)
                mode = stat.S_IMODE(os.stat(root).st_mode)
            except Exception:
                log.warning("drop zone %s is not usable", scanner.kind, exc_info=True)
                continue
            for point in scanner.endpoints:
                folder = root / point.folder
                if folder.is_dir():
                    continue
                try:
                    folder.mkdir()
                    os.chmod(folder, mode)
                except FileExistsError:
                    pass
                except OSError as exc:
                    log.warning("cannot create the scanner folder %s: %s", folder, exc)

    # -- status for the web interface -----------------------------------------------------------
    def fetcher_status(self, name: str) -> dict[str, Any] | None:
        fetcher = self.fetchers.get(name)
        if fetcher is None or not hasattr(fetcher, "status"):
            return None
        try:
            return dict(fetcher.status())
        except Exception:
            log.debug("cannot read the status of mailbox %s", name, exc_info=True)
            return None


__all__ = ["ChannelManager", "DEFAULT_MAILBOX", "defaults_from_settings", "imap_config", "mailbox_from_imap"]
