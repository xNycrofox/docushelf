"""The inbound channels of a household as configured in the web interface.

Without ``<household>/config/channels.yaml`` the channels come from the
environment (and ``households.yaml``) as always. Once the file exists it
decides for that household alone: mailboxes, the scan-to-mail addresses,
scanner folders per person and watch folders::

    imap:
      - name: familie                 # [a-z0-9-], at most 32 characters
        enabled: true
        owner: erika                  # optional household member
        host: imap.example.org
        port: 993
        tls: ssl                      # ssl (implicit TLS) or starttls
        user: dokumente@example.org
        folder: INBOX
        processed_folder: DocuShelf/Verarbeitet
        rejected_folder: DocuShelf/Abgelehnt
        no_attachment_folder: DocuShelf/Ohne Anhang
        allowed_senders: [erika@example.org, "@example.org"]
        poll_seconds: 60
    smtp:
      enabled: true
      recipients:
        - {address: scan-familie@example.org}
        - {address: scan-erika@example.org, owner: erika}
    scanner:
      smb: {enabled: true, endpoints: [{folder: erika, owner: erika, enabled: true}]}
      ftp: {enabled: false, endpoints: []}
    watch:
      - {name: nas, path: /watch/nas, remove_after_import: true, enabled: true}

Passwords never go into the file: every mailbox has its password in
``config/secrets/imap-<name>`` (mode 600, directory 700). The file is written
by :class:`ChannelStore` atomically, the previous version kept as
``channels.yaml.bak``; what is written is parsed again with the same loader
that runs at start-up before it replaces the old file.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import threading
from dataclasses import dataclass, field, replace
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from ..errors import ConfigError
from ..util.fsops import atomic_write_text
from ..util.ids import is_item_id

log = logging.getLogger(__name__)

CHANNELS_FILE = "channels.yaml"
SECRETS_DIR = "secrets"
BACKUP_SUFFIX = ".bak"
SCANNER_KINDS = ("smb", "ftp")
TLS_MODES = ("ssl", "starttls")
DEFAULT_IMAP_FOLDERS = {
    "folder": "INBOX",
    "processed_folder": "DocuShelf/Verarbeitet",
    "rejected_folder": "DocuShelf/Abgelehnt",
    "no_attachment_folder": "DocuShelf/Ohne Anhang",
}
MIN_POLL_SECONDS = 10
MAX_POLL_SECONDS = 24 * 3600
MAX_OWNER_LENGTH = 200

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
# Watch folder names become source names (watch-<name>); names from the environment keep working.
WATCH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
FOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9._:\[\]-]{1,253}$")

_TOP_KEYS = ("imap", "smtp", "scanner", "watch")
_IMAP_KEYS = (
    "name",
    "enabled",
    "owner",
    "host",
    "port",
    "tls",
    "user",
    "folder",
    "processed_folder",
    "rejected_folder",
    "no_attachment_folder",
    "allowed_senders",
    "poll_seconds",
)
_SMTP_KEYS = ("enabled", "recipients")
_RECIPIENT_KEYS = ("address", "owner")
_SCANNER_KEYS = ("enabled", "endpoints")
_ENDPOINT_KEYS = ("folder", "owner", "enabled")
_WATCH_KEYS = ("name", "path", "remove_after_import", "enabled")
_TRUE_WORDS = {"1", "true", "yes", "ja", "on", "an"}
_FALSE_WORDS = {"0", "false", "no", "nein", "off", "aus"}

SCANNER_LABELS = {"smb": "Scanner (SMB)", "ftp": "Scanner (FTP)"}


# ---------------------------------------------------------------------------
# Model


@dataclass(frozen=True)
class Mailbox:
    """One IMAP mailbox. The password is kept apart (secrets file or environment)."""

    name: str
    host: str
    user: str
    port: int = 993
    tls: str = "ssl"
    folder: str = DEFAULT_IMAP_FOLDERS["folder"]
    processed_folder: str = DEFAULT_IMAP_FOLDERS["processed_folder"]
    rejected_folder: str = DEFAULT_IMAP_FOLDERS["rejected_folder"]
    no_attachment_folder: str = DEFAULT_IMAP_FOLDERS["no_attachment_folder"]
    allowed_senders: tuple[str, ...] = ()
    poll_seconds: int = 60
    owner: str | None = None
    enabled: bool = True
    # Only from the environment (DOCUSHELF_IMAP_ALLOW_PLAINTEXT); never set in the web interface.
    allow_plaintext: bool = False

    @property
    def secret_name(self) -> str:
        return secret_name(self.name)

    @property
    def endpoint_label(self) -> str:
        return f"Postfach „{self.name}“"


@dataclass(frozen=True)
class Recipient:
    """A scan-to-mail address of the household, optionally belonging to one member."""

    address: str
    owner: str | None = None


@dataclass(frozen=True)
class SmtpChannel:
    enabled: bool = False
    recipients: tuple[Recipient, ...] = ()

    def find(self, address: str | None) -> Recipient | None:
        normalized = normalize_address(address)
        if not normalized:
            return None
        return next((r for r in self.recipients if r.address == normalized), None)


@dataclass(frozen=True)
class Endpoint:
    """A scanner subfolder ``eingang/quellen/<kind>/<folder>`` of one member."""

    folder: str
    owner: str | None = None
    enabled: bool = True


@dataclass(frozen=True)
class ScannerChannel:
    kind: str
    enabled: bool = True
    endpoints: tuple[Endpoint, ...] = ()

    @property
    def label(self) -> str:
        return SCANNER_LABELS.get(self.kind, self.kind)

    def find(self, folder: str) -> Endpoint | None:
        key = str(folder).casefold()
        return next((e for e in self.endpoints if e.folder.casefold() == key), None)


@dataclass(frozen=True)
class WatchFolder:
    name: str
    path: Path
    remove_after_import: bool = True
    enabled: bool = True

    @property
    def source_name(self) -> str:
        return f"watch-{self.name}"


@dataclass(frozen=True)
class ChannelConfig:
    imap: tuple[Mailbox, ...] = ()
    smtp: SmtpChannel = field(default_factory=SmtpChannel)
    scanners: tuple[ScannerChannel, ...] = (ScannerChannel("smb"), ScannerChannel("ftp"))
    watch: tuple[WatchFolder, ...] = ()

    def mailbox(self, name: str) -> Mailbox | None:
        return next((m for m in self.imap if m.name == name), None)

    def scanner(self, kind: str) -> ScannerChannel:
        found = next((s for s in self.scanners if s.kind == kind), None)
        return found if found is not None else ScannerChannel(kind, enabled=False)

    def watch_folder(self, name: str) -> WatchFolder | None:
        return next((w for w in self.watch if w.name == name), None)

    def with_scanner(self, scanner: ScannerChannel) -> "ChannelConfig":
        scanners = [scanner if s.kind == scanner.kind else s for s in self.scanners]
        if not any(s.kind == scanner.kind for s in self.scanners):
            scanners.append(scanner)
        order = {kind: number for number, kind in enumerate(SCANNER_KINDS)}
        return replace(self, scanners=tuple(sorted(scanners, key=lambda s: order.get(s.kind, 99))))

    def owners(self) -> set[str]:
        """Every member named as an owner somewhere (casefolded)."""
        names = {m.owner for m in self.imap} | {r.owner for r in self.smtp.recipients}
        names |= {e.owner for s in self.scanners for e in s.endpoints}
        return {name.casefold() for name in names if name}

    def to_dict(self) -> dict[str, Any]:
        """The YAML structure of ``channels.yaml`` (no passwords)."""
        mailboxes = []
        for box in self.imap:
            entry: dict[str, Any] = {"name": box.name, "enabled": box.enabled}
            if box.owner:
                entry["owner"] = box.owner
            entry.update(
                host=box.host,
                port=box.port,
                tls=box.tls,
                user=box.user,
                folder=box.folder,
                processed_folder=box.processed_folder,
                rejected_folder=box.rejected_folder,
                no_attachment_folder=box.no_attachment_folder,
                allowed_senders=list(box.allowed_senders),
                poll_seconds=box.poll_seconds,
            )
            mailboxes.append(entry)
        recipients = []
        for recipient in self.smtp.recipients:
            item: dict[str, Any] = {"address": recipient.address}
            if recipient.owner:
                item["owner"] = recipient.owner
            recipients.append(item)
        scanners: dict[str, Any] = {}
        for scanner in self.scanners:
            endpoints = []
            for endpoint in scanner.endpoints:
                point: dict[str, Any] = {"folder": endpoint.folder}
                if endpoint.owner:
                    point["owner"] = endpoint.owner
                point["enabled"] = endpoint.enabled
                endpoints.append(point)
            scanners[scanner.kind] = {"enabled": scanner.enabled, "endpoints": endpoints}
        watch = [
            {
                "name": folder.name,
                "path": str(folder.path),
                "remove_after_import": folder.remove_after_import,
                "enabled": folder.enabled,
            }
            for folder in self.watch
        ]
        return {
            "imap": mailboxes,
            "smtp": {"enabled": self.smtp.enabled, "recipients": recipients},
            "scanner": scanners,
            "watch": watch,
        }


def secret_name(mailbox: str) -> str:
    return f"imap-{mailbox}"


# ---------------------------------------------------------------------------
# Parsing (the one loader used at start-up and before every write)


def normalize_address(address: Any) -> str | None:
    """``"Name <A@B.de>"`` → ``"a@b.de"``; None without an address."""
    if not address:
        return None
    _, addr = parseaddr(str(address))
    addr = (addr or str(address)).strip().strip("<>").strip().lower()
    return addr or None


def dump_channels(config: ChannelConfig) -> str:
    header = (
        "# Eingangskanäle dieses Haushalts – geschrieben von DocuShelf (Einstellungen → Eingangskanäle).\n"
        "# Passwörter stehen nicht hier, sondern in config/secrets/ (nur für DocuShelf lesbar).\n"
    )
    body = yaml.safe_dump(config.to_dict(), allow_unicode=True, sort_keys=False, default_flow_style=False, width=4096)
    return header + body


def parse_channels(text: str) -> ChannelConfig:
    """Validate the text of ``channels.yaml``; ConfigError (German) for anything invalid."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{CHANNELS_FILE} ist ungültig: {exc}") from None
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{CHANNELS_FILE} muss eine Zuordnung mit imap, smtp, scanner und watch sein.")
    _reject_unknown(raw, _TOP_KEYS, CHANNELS_FILE)
    config = ChannelConfig(
        imap=_parse_mailboxes(raw.get("imap")),
        smtp=_parse_smtp(raw.get("smtp")),
        scanners=_parse_scanners(raw.get("scanner")),
        watch=_parse_watch(raw.get("watch")),
    )
    check_config(config)
    return config


def check_config(config: ChannelConfig) -> None:
    """Checks across entries (unique names, folders and addresses)."""
    names: set[str] = set()
    for box in config.imap:
        if box.name in names:
            raise ConfigError(f"Den Postfachnamen „{box.name}“ gibt es zweimal.")
        names.add(box.name)
        _check_mailbox(box, f"Postfach „{box.name}“")
    addresses: set[str] = set()
    for recipient in config.smtp.recipients:
        if recipient.address in addresses:
            raise ConfigError(f"Die Adresse {recipient.address} ist doppelt eingetragen.")
        addresses.add(recipient.address)
    for scanner in config.scanners:
        if scanner.kind not in SCANNER_KINDS:
            raise ConfigError(f"Unbekannter Scanner-Kanal „{scanner.kind}“ – erlaubt sind smb und ftp.")
        folders: set[str] = set()
        for endpoint in scanner.endpoints:
            key = endpoint.folder.casefold()
            if key in folders:
                raise ConfigError(f"Den Ordner „{endpoint.folder}“ gibt es bei {scanner.label} zweimal.")
            folders.add(key)
    watch_names: set[str] = set()
    for folder in config.watch:
        if folder.name in watch_names:
            raise ConfigError(f"Den Watch-Ordner „{folder.name}“ gibt es zweimal.")
        watch_names.add(folder.name)


def _reject_unknown(mapping: Mapping[Any, Any], allowed: Iterable[str], where: str) -> None:
    allowed = tuple(allowed)
    for key in mapping:
        if key not in allowed:
            raise ConfigError(f"{where}: unbekannter Eintrag „{key}“ – erlaubt sind {', '.join(allowed)}.")


def _list(value: Any, where: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{where} muss eine Liste sein.")
    return value


def _mapping(value: Any, where: str) -> dict[Any, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where} muss eine Zuordnung sein.")
    return value


def parse_bool(value: Any, where: str, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    word = str(value).strip().casefold()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise ConfigError(f"{where} muss an oder aus sein, nicht „{value}“.")


def parse_int(value: Any, where: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{where} muss eine Zahl sein.")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ConfigError(f"{where} muss eine Zahl sein.") from None
    if not minimum <= number <= maximum:
        raise ConfigError(f"{where} muss zwischen {minimum} und {maximum} liegen.")
    return number


def clean_text(value: Any, where: str, *, required: bool = True, max_length: int = 200) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ConfigError(f"{where} fehlt.")
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ConfigError(f"{where} muss ein Text sein.")
    text = str(value).strip()
    if len(text) > max_length:
        raise ConfigError(f"{where} ist zu lang (höchstens {max_length} Zeichen).")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ConfigError(f"{where} darf keine Zeilenumbrüche oder Steuerzeichen enthalten.")
    return text


def clean_owner(value: Any, where: str = "Person") -> str | None:
    """An optional household member (user name or e-mail address)."""
    return clean_text(value, where, required=False, max_length=MAX_OWNER_LENGTH)


def clean_name(value: Any, where: str) -> str:
    text = clean_text(value, where) or ""
    if not NAME_RE.match(text):
        raise ConfigError(
            f"{where} „{text}“ ist ungültig – erlaubt sind Kleinbuchstaben, Ziffern und Bindestriche "
            "(am Anfang ein Buchstabe oder eine Ziffer, höchstens 32 Zeichen)."
        )
    return text


def clean_folder(value: Any, where: str) -> str:
    text = clean_text(value, where) or ""
    if not FOLDER_RE.match(text) or is_item_id(text):
        raise ConfigError(
            f"{where} „{text}“ ist ungültig – erlaubt sind Buchstaben, Ziffern, Punkt, Unter- und Bindestrich "
            "(ohne Umlaute und Leerzeichen, höchstens 64 Zeichen)."
        )
    return text


def clean_address(value: Any, where: str = "Adresse") -> str:
    text = clean_text(value, where) or ""
    address = normalize_address(text)
    if not address or address.count("@") != 1 or any(ch.isspace() for ch in address):
        raise ConfigError(f"„{text}“ ist keine E-Mail-Adresse.")
    local, domain = address.split("@")
    if not local or not domain or "." not in domain:
        raise ConfigError(f"„{text}“ ist keine E-Mail-Adresse.")
    return address


def clean_senders(value: Any, where: str) -> tuple[str, ...]:
    """Allowed senders: addresses, ``@domain`` or ``*``."""
    if value is None:
        return ()
    if isinstance(value, str):
        value = re.split(r"[,\s;]+", value)
    if not isinstance(value, list):
        raise ConfigError(f"{where} muss eine Liste sein.")
    result: list[str] = []
    for item in value:
        text = str(item or "").strip().lower()
        if not text:
            continue
        if text != "*":
            if text.startswith("@"):
                valid = "." in text[1:] and "@" not in text[1:]
            else:
                valid = text.count("@") == 1 and all(text.split("@"))
            if not valid or any(ch.isspace() for ch in text) or len(text) > 254:
                raise ConfigError(f"{where}: „{item}“ ist weder eine E-Mail-Adresse noch eine Domain wie @example.org.")
        if text not in result:
            result.append(text)
    return tuple(result)


def clean_imap_folder(value: Any, where: str, default: str) -> str:
    text = clean_text(value, where, required=False, max_length=200)
    return text or default


def _parse_mailboxes(value: Any) -> tuple[Mailbox, ...]:
    result = []
    for number, entry in enumerate(_list(value, f"{CHANNELS_FILE}: imap"), start=1):
        where = f"{CHANNELS_FILE}: imap Nr. {number}"
        entry = _mapping(entry, where)
        _reject_unknown(entry, _IMAP_KEYS, where)
        name = clean_name(entry.get("name"), f"{where}: name")
        result.append(build_mailbox(entry, name=name, where=f"Postfach „{name}“"))
    return tuple(result)


def build_mailbox(values: Mapping[str, Any], *, name: str, where: str) -> Mailbox:
    """A mailbox from loose values (YAML entry or web form), validated."""
    tls = str(values.get("tls") or "ssl").strip().lower()
    if tls not in TLS_MODES:
        raise ConfigError(f"{where}: Verschlüsselung muss ssl oder starttls sein.")
    port_raw = values.get("port")
    port = 993 if port_raw in (None, "") and tls == "ssl" else (143 if port_raw in (None, "") else port_raw)
    box = Mailbox(
        name=name,
        host=clean_text(values.get("host"), f"{where}: Server") or "",
        user=clean_text(values.get("user"), f"{where}: Benutzer", max_length=254) or "",
        port=parse_int(port, f"{where}: Port", minimum=1, maximum=65535),
        tls=tls,
        folder=clean_imap_folder(values.get("folder"), f"{where}: Ordner", DEFAULT_IMAP_FOLDERS["folder"]),
        processed_folder=clean_imap_folder(
            values.get("processed_folder"), f"{where}: Ordner für Verarbeitetes", DEFAULT_IMAP_FOLDERS["processed_folder"]
        ),
        rejected_folder=clean_imap_folder(
            values.get("rejected_folder"), f"{where}: Ordner für Abgelehntes", DEFAULT_IMAP_FOLDERS["rejected_folder"]
        ),
        no_attachment_folder=clean_imap_folder(
            values.get("no_attachment_folder"),
            f"{where}: Ordner für Mails ohne Anhang",
            DEFAULT_IMAP_FOLDERS["no_attachment_folder"],
        ),
        allowed_senders=clean_senders(values.get("allowed_senders"), f"{where}: erlaubte Absender"),
        poll_seconds=parse_int(
            values.get("poll_seconds") if values.get("poll_seconds") not in (None, "") else 60,
            f"{where}: Abrufintervall (Sekunden)",
            minimum=MIN_POLL_SECONDS,
            maximum=MAX_POLL_SECONDS,
        ),
        owner=clean_owner(values.get("owner"), f"{where}: Person"),
        enabled=parse_bool(values.get("enabled"), f"{where}: enabled"),
    )
    _check_mailbox(box, where)
    return box


def _check_mailbox(box: Mailbox, where: str) -> None:
    if not _HOST_RE.match(box.host):
        raise ConfigError(f"{where}: „{box.host}“ ist kein gültiger Servername.")
    source = box.folder.strip("/").casefold()
    for label, folder in (
        ("Verarbeitetes", box.processed_folder),
        ("Abgelehntes", box.rejected_folder),
        ("Mails ohne Anhang", box.no_attachment_folder),
    ):
        if folder.strip("/").casefold() == source:
            raise ConfigError(f"{where}: der Ordner für {label} darf nicht der abgerufene Ordner „{box.folder}“ sein.")


def _parse_smtp(value: Any) -> SmtpChannel:
    where = f"{CHANNELS_FILE}: smtp"
    entry = _mapping(value, where)
    _reject_unknown(entry, _SMTP_KEYS, where)
    recipients = []
    for number, item in enumerate(_list(entry.get("recipients"), f"{where}.recipients"), start=1):
        if isinstance(item, str):
            item = {"address": item}
        item = _mapping(item, f"{where}.recipients Nr. {number}")
        _reject_unknown(item, _RECIPIENT_KEYS, f"{where}.recipients Nr. {number}")
        recipients.append(
            Recipient(
                address=clean_address(item.get("address")),
                owner=clean_owner(item.get("owner"), f"{where}.recipients Nr. {number}: owner"),
            )
        )
    return SmtpChannel(enabled=parse_bool(entry.get("enabled"), f"{where}.enabled", default=bool(recipients)), recipients=tuple(recipients))


def _parse_scanners(value: Any) -> tuple[ScannerChannel, ...]:
    where = f"{CHANNELS_FILE}: scanner"
    entry = _mapping(value, where)
    _reject_unknown(entry, SCANNER_KINDS, where)
    result = []
    for kind in SCANNER_KINDS:
        if kind not in entry:
            result.append(ScannerChannel(kind, enabled=False))
            continue
        item = _mapping(entry.get(kind), f"{where}.{kind}")
        _reject_unknown(item, _SCANNER_KEYS, f"{where}.{kind}")
        endpoints = []
        for number, point in enumerate(_list(item.get("endpoints"), f"{where}.{kind}.endpoints"), start=1):
            point_where = f"{where}.{kind}.endpoints Nr. {number}"
            point = _mapping(point, point_where)
            _reject_unknown(point, _ENDPOINT_KEYS, point_where)
            endpoints.append(
                Endpoint(
                    folder=clean_folder(point.get("folder"), f"{point_where}: Ordner"),
                    owner=clean_owner(point.get("owner"), f"{point_where}: owner"),
                    enabled=parse_bool(point.get("enabled"), f"{point_where}: enabled"),
                )
            )
        result.append(ScannerChannel(kind, enabled=parse_bool(item.get("enabled"), f"{where}.{kind}.enabled"), endpoints=tuple(endpoints)))
    return tuple(result)


def _parse_watch(value: Any) -> tuple[WatchFolder, ...]:
    result = []
    for number, entry in enumerate(_list(value, f"{CHANNELS_FILE}: watch"), start=1):
        where = f"{CHANNELS_FILE}: watch Nr. {number}"
        entry = _mapping(entry, where)
        _reject_unknown(entry, _WATCH_KEYS, where)
        result.append(build_watch(entry, where=where))
    return tuple(result)


def build_watch(values: Mapping[str, Any], *, where: str = "Watch-Ordner") -> WatchFolder:
    name = clean_text(values.get("name"), f"{where}: Name") or ""
    if not WATCH_NAME_RE.match(name):
        raise ConfigError(
            f"{where}: der Name „{name}“ ist ungültig – erlaubt sind Buchstaben, Ziffern, Punkt, Unter- und "
            "Bindestrich (z. B. nas)."
        )
    path_text = clean_text(values.get("path"), f"{where}: Pfad", max_length=1024) or ""
    path = Path(path_text)
    if not path.is_absolute():
        raise ConfigError(f"{where}: der Pfad muss absolut sein (z. B. /watch/nas), nicht „{path_text}“.")
    if ".." in path.parts:
        raise ConfigError(f"{where}: der Pfad darf kein „..“ enthalten.")
    return WatchFolder(
        name=name,
        path=Path(os.path.normpath(path_text)),
        remove_after_import=parse_bool(values.get("remove_after_import"), f"{where}: remove_after_import"),
        enabled=parse_bool(values.get("enabled"), f"{where}: enabled"),
    )


# ---------------------------------------------------------------------------
# The file and the secrets


class ChannelStore:
    """``config/channels.yaml`` and ``config/secrets/`` of one household."""

    def __init__(self, config_dir: Path):
        self.config_dir = Path(config_dir)
        self.path = self.config_dir / CHANNELS_FILE
        self.backup_path = self.path.with_name(self.path.name + BACKUP_SUFFIX)
        self.secrets_dir = self.config_dir / SECRETS_DIR
        self._lock = threading.RLock()

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> ChannelConfig:
        """Read and validate the file (ConfigError, German); enabled mailboxes need a password."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"{self.path} kann nicht gelesen werden: {exc.strerror or exc}") from None
        config = parse_channels(text)
        self.check_secrets(config)
        return config

    def check_secrets(self, config: ChannelConfig, *, pending: Iterable[str] = ()) -> None:
        pending = set(pending)
        for box in config.imap:
            if box.enabled and box.name not in pending and self.password(box.name) is None:
                raise ConfigError(
                    f"Für das Postfach „{box.name}“ ist kein Passwort gespeichert "
                    f"({self.secret_path(box.name)} fehlt oder ist leer)."
                )

    def secret_path(self, mailbox: str) -> Path:
        return self.secrets_dir / secret_name(mailbox)

    def password(self, mailbox: str) -> str | None:
        path = self.secret_path(mailbox)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ConfigError(f"Das Passwort des Postfachs „{mailbox}“ ist nicht lesbar ({exc.strerror or exc}).") from None
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o077:
                os.chmod(path, 0o600)
        except OSError:
            log.debug("cannot tighten the permissions of %s", path, exc_info=True)
        return text.strip("\r\n") or None

    def has_password(self, mailbox: str) -> bool:
        try:
            return self.password(mailbox) is not None
        except ConfigError:
            return False

    def save(
        self,
        config: ChannelConfig,
        *,
        passwords: Mapping[str, str] | None = None,
        remove_secrets: Iterable[str] = (),
    ) -> None:
        """Write ``channels.yaml`` (and new passwords); nothing is written if the result is invalid.

        ``passwords`` maps mailbox names to new passwords; ``remove_secrets``
        lists mailboxes whose stored password is deleted afterwards.
        """
        passwords = dict(passwords or {})
        text = dump_channels(config)
        with self._lock:
            parsed = parse_channels(text)
            self.check_secrets(parsed, pending=[name for name, value in passwords.items() if value])
            try:
                self.config_dir.mkdir(parents=True, exist_ok=True)
                for name, value in passwords.items():
                    if value:
                        self._write_secret(name, value)
                if self.path.is_file():
                    previous = self.path.read_text(encoding="utf-8")
                    atomic_write_text(self.backup_path, previous, mode=0o640)
                atomic_write_text(self.path, text, mode=0o640)
            except OSError as exc:
                raise ConfigError(f"{CHANNELS_FILE} konnte nicht gespeichert werden: {exc.strerror or exc}") from None
            # A password is only deleted once no mailbox of the new file uses it.
            keep = {box.name for box in parsed.imap}
            for name in remove_secrets:
                if name in keep:
                    continue
                try:
                    self.secret_path(name).unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    log.warning("cannot remove the password file of mailbox %s", name, exc_info=True)

    def _write_secret(self, mailbox: str, password: str) -> None:
        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.secrets_dir, 0o700)
        except OSError:
            log.debug("cannot set the permissions of %s", self.secrets_dir, exc_info=True)
        atomic_write_text(self.secret_path(mailbox), password, mode=0o600)


__all__ = [
    "CHANNELS_FILE",
    "ChannelConfig",
    "ChannelStore",
    "Endpoint",
    "Mailbox",
    "Recipient",
    "SCANNER_KINDS",
    "ScannerChannel",
    "SmtpChannel",
    "WatchFolder",
    "build_mailbox",
    "build_watch",
    "dump_channels",
    "parse_channels",
]
