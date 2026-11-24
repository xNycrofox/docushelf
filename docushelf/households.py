"""Households: several families on one installation, each with its own archive.

Without ``<data>/config/households.yaml`` there is exactly one household
(id ``default``, name from ``DOCUSHELF_HOUSEHOLD_NAME``) living in the data root
itself; access is controlled by ``DOCUSHELF_ALLOWED_*`` as always and nothing
changes compared to an installation without households.

With ``households.yaml`` every household has its own data directory
(``haushalte/<id>`` by default, ``"."`` is the data root) with the complete
layout (archive, inbox, trash, config, audit log, index), its own rules,
tokens and channels, and its own :class:`~docushelf.services.Services`::

    households:
      - id: familie-a
        name: "Familie A"
        path: "."
        groups: [familie-a]
        users: []
        owner_names: ["Erika Mustermann"]
        notify_url: null
        auto_file: {mode: always, threshold: 0.75}
        imap: {host: imap.example.org, user: dokumente@example.org,
               password_file: /run/secrets/imap-familie-a}
        smtp_recipients: [scan-familie-a@docushelf.local]
        watch_folders: {nas: /watch/familie-a}

An optional top-level ``admins: [name, ...]`` lists the accounts (user name or
verified e-mail address, like ``users``) that may manage the ``users`` of every
household in the web interface; ``DOCUSHELF_ADMIN_USERS`` adds more, and the
local emergency account always may. Such a change is written back to the file
(:meth:`Registry.change_members`) and takes effect at once.

The file is validated strictly: unknown keys are errors, because a typo in
``groups`` must never silently change who has access. Secrets (IMAP
passwords) are never logged or shown.

:class:`Registry` owns the service containers of all households (created
lazily), starts and stops them together and runs the one global SMTP receiver,
which routes every message to a household by its recipient address.
"""

from __future__ import annotations

import logging
import os
import posixpath
import re
import stat
import threading
from dataclasses import dataclass, field, replace
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

import yaml

from .config import (
    HOUSEHOLDS_FILE,
    ImapConfig,
    Settings,
    SourceConfig,
    parse_auto_file_mode,
    parse_auto_file_sources,
    parse_threshold,
)
from .errors import Conflict, ConfigError, NotFound
from .layout import Layout
from .util.fsops import atomic_write_text

log = logging.getLogger(__name__)

DEFAULT_ID = "default"
DEFAULT_PARENT = "haushalte"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
# Longest user name or e-mail address accepted as a household member in the web interface.
MAX_MEMBER_LENGTH = 200
BACKUP_SUFFIX = ".bak"

_TOP_KEYS = ("households", "admins")

_HOUSEHOLD_KEYS = (
    "id",
    "name",
    "path",
    "groups",
    "users",
    "owner_names",
    "notify_url",
    "auto_file",
    "imap",
    "smtp_recipients",
    "watch_folders",
)
_AUTO_FILE_KEYS = ("mode", "threshold", "sources", "new_folders", "wait_for_llm", "notify")
_IMAP_SECRET_KEYS = ("password", "password_file", "password_env")
_IMAP_KEYS = (
    "host",
    "user",
    *_IMAP_SECRET_KEYS,
    "port",
    "ssl",
    "allow_plaintext",
    "folder",
    "processed_folder",
    "rejected_folder",
    "no_attachment_folder",
    "allowed_senders",
    "poll_seconds",
)
_TRUE_WORDS = {"1", "true", "yes", "ja", "on", "an"}
_FALSE_WORDS = {"0", "false", "no", "nein", "off", "aus"}


@dataclass(frozen=True)
class Household:
    """One household: who may use it, where its data lives, and its own settings.

    ``path`` is the absolute data directory. The overrides are None (or empty)
    where the global settings apply. ``imap`` holds the resolved password and
    is therefore left out of ``repr``.
    """

    id: str
    name: str
    path: Path
    groups: tuple[str, ...] = ()
    users: tuple[str, ...] = ()
    owner_names: tuple[str, ...] | None = None
    notify_url: str | None = None
    auto_file: dict[str, Any] | None = None
    imap: dict[str, Any] | None = field(default=None, repr=False)
    smtp_recipients: tuple[str, ...] = ()
    watch_folders: dict[str, Path] = field(default_factory=dict)

    def __hash__(self) -> int:
        return hash((self.id, str(self.path)))

    @property
    def access(self) -> str:
        """Who has access, for people (``Gruppen: a, b · Benutzer: c``)."""
        parts = []
        if self.groups:
            parts.append("Gruppen: " + ", ".join(self.groups))
        if self.users:
            parts.append("Benutzer: " + ", ".join(self.users))
        return " · ".join(parts) or "nur lokaler Administrator"


# ---------------------------------------------------------------------------
# Loading and validation


def households_file(settings: Settings) -> Path:
    """``households.yaml`` in the global configuration directory."""
    return settings.root_dir / "config" / HOUSEHOLDS_FILE


def configured(settings: Settings) -> bool:
    """Whether the installation has a ``households.yaml`` (household mode)."""
    return households_file(settings).is_file()


def default_household(settings: Settings) -> Household:
    """The only household of an installation without ``households.yaml``."""
    return Household(
        id=DEFAULT_ID,
        name=settings.household_name or "Zuhause",
        path=Path(settings.data_dir),
        groups=tuple(settings.allowed_groups or ()),
        users=tuple(settings.allowed_users or ()),
    )


def load_households(settings: Settings, *, env: Mapping[str, str] | None = None) -> list[Household]:
    """All households, in the order of ``households.yaml``.

    Without the file: the single household :func:`default_household`. ``env``
    is where ``password_env`` is looked up (default: the process environment).
    Raises ConfigError (German) for anything invalid.
    """
    return _load(settings, env)[0]


def _load(settings: Settings, env: Mapping[str, str] | None) -> tuple[list[Household], tuple[str, ...]]:
    """The households and the configured admins (casefolded) of ``households.yaml``."""
    path = households_file(settings)
    if not path.is_file():
        return [default_household(settings)], ()
    raw = _parse_yaml(_read_text(path))
    households = parse_households(raw, settings, env=os.environ if env is None else env)
    return households, parse_admins(raw)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{HOUSEHOLDS_FILE} kann nicht gelesen werden: {exc.strerror or exc}") from exc


def _parse_yaml(text: str) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{HOUSEHOLDS_FILE} ist ungültig: {exc}") from exc


def parse_admins(raw: Any) -> tuple[str, ...]:
    """The ``admins`` of ``households.yaml`` (casefolded; empty without the entry)."""
    if not isinstance(raw, dict):
        return ()
    return tuple(name.casefold() for name in _strings(raw.get("admins"), f"{HOUSEHOLDS_FILE}: admins"))


def parse_households(raw: Any, settings: Settings, *, env: Mapping[str, str]) -> list[Household]:
    """Validate the content of ``households.yaml`` (already parsed YAML)."""
    if not isinstance(raw, dict):
        raise ConfigError(f"{HOUSEHOLDS_FILE} muss den Eintrag „households:“ mit einer Liste von Haushalten enthalten.")
    unknown = [str(key) for key in raw if key not in _TOP_KEYS]
    if unknown:
        raise ConfigError(
            f"{HOUSEHOLDS_FILE}: unbekannter Eintrag „{unknown[0]}“ – erlaubt sind nur „households:“ und „admins:“."
        )
    parse_admins(raw)
    entries = raw.get("households")
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"{HOUSEHOLDS_FILE}: „households:“ muss eine Liste mit mindestens einem Haushalt sein.")
    root = Path(settings.root_dir)
    households = [_parse_household(entry, number, root, env) for number, entry in enumerate(entries, start=1)]
    _check_households(households, root)
    return households


def _where(household_id: str | None, number: int) -> str:
    if household_id:
        return f"{HOUSEHOLDS_FILE}: Haushalt „{household_id}“"
    return f"{HOUSEHOLDS_FILE}: Haushalt Nr. {number}"


def _parse_household(entry: Any, number: int, root: Path, env: Mapping[str, str]) -> Household:
    if not isinstance(entry, dict):
        raise ConfigError(f"{_where(None, number)} muss eine Zuordnung (id, name, groups …) sein.")
    raw_id = entry.get("id")
    household_id = raw_id if isinstance(raw_id, str) else None
    where = _where(household_id, number)
    _reject_unknown(entry, _HOUSEHOLD_KEYS, where)
    if household_id is None:
        raise ConfigError(f"{where}: „id“ fehlt (z. B. id: familie-a).")
    if not ID_RE.match(household_id):
        raise ConfigError(
            f"{where}: die id ist ungültig – erlaubt sind Kleinbuchstaben, Ziffern und Bindestriche, "
            "am Anfang ein Buchstabe oder eine Ziffer, höchstens 32 Zeichen."
        )

    name = entry.get("name")
    if name is None:
        name = household_id
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"{where}: „name“ muss ein Text sein.")

    return Household(
        id=household_id,
        name=name.strip(),
        path=_parse_path(entry.get("path"), household_id, root, where),
        groups=_strings(entry.get("groups"), f"{where}: groups"),
        users=tuple(user.casefold() for user in _strings(entry.get("users"), f"{where}: users")),
        owner_names=(
            None if entry.get("owner_names") is None else _strings(entry.get("owner_names"), f"{where}: owner_names")
        ),
        notify_url=_optional_text(entry.get("notify_url"), f"{where}: notify_url"),
        auto_file=_parse_auto_file(entry.get("auto_file"), where),
        imap=_parse_imap(entry.get("imap"), where, env),
        smtp_recipients=_parse_recipients(entry.get("smtp_recipients"), where),
        watch_folders=_parse_watch_folders(entry.get("watch_folders"), where),
    )


def _reject_unknown(mapping: Mapping[Any, Any], allowed: Sequence[str], where: str) -> None:
    for key in mapping:
        if key not in allowed:
            raise ConfigError(f"{where}: unbekannter Eintrag „{key}“ – erlaubt sind {', '.join(allowed)}.")


def _strings(value: Any, where: str) -> tuple[str, ...]:
    """A list of non-empty texts; a single text counts as a list with one entry."""
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ConfigError(f"{where} muss eine Liste sein, z. B. [eintrag-1, eintrag-2].")
    result: list[str] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            raise ConfigError(f"{where}: jeder Eintrag muss ein Text sein.")
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _optional_text(value: Any, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{where} muss ein Text sein.")
    return value.strip() or None


def _parse_path(value: Any, household_id: str, root: Path, where: str) -> Path:
    if value is None:
        value = f"{DEFAULT_PARENT}/{household_id}"
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: „path“ muss ein Pfad relativ zum Datenverzeichnis sein, z. B. haushalte/{household_id}.")
    text = value.strip().replace("\\", "/")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise ConfigError(f"{where}: „path“ muss relativ zum Datenverzeichnis sein, nicht „{value}“.")
    normalized = posixpath.normpath(text)
    if normalized == ".." or normalized.startswith("../"):
        raise ConfigError(f"{where}: „path“ darf nicht aus dem Datenverzeichnis hinausführen („{value}“).")
    if normalized == ".":
        return Path(root)
    return Path(root) / normalized


def _bool(value: Any, where: str) -> bool:
    if isinstance(value, bool):
        return value
    word = str(value).strip().casefold()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise ConfigError(f"{where} muss true oder false sein, nicht „{value}“.")


def _int(value: Any, where: str, *, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{where} muss eine Zahl sein.")
    try:
        number = int(str(value).strip())
    except ValueError:
        raise ConfigError(f"{where} muss eine Zahl sein.") from None
    if number < minimum or (maximum is not None and number > maximum):
        upper = f" und höchstens {maximum}" if maximum is not None else ""
        raise ConfigError(f"{where} muss mindestens {minimum}{upper} sein.")
    return number


def _parse_auto_file(value: Any, where: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: „auto_file“ muss eine Zuordnung sein, z. B. {{mode: always, threshold: 0.75}}.")
    _reject_unknown(value, _AUTO_FILE_KEYS, f"{where}: auto_file")
    result: dict[str, Any] = {}
    for key, item in value.items():
        name = f"{where}: auto_file.{key}"
        if key == "mode":
            result[key] = parse_auto_file_mode(item, name=name)
        elif key == "threshold":
            result[key] = parse_threshold(item, name=name)
        elif key == "sources":
            result[key] = parse_auto_file_sources(_strings(item, name), name=name)
        else:
            result[key] = _bool(item, name)
    return result or None


def _parse_imap(value: Any, where: str, env: Mapping[str, str]) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: „imap“ muss eine Zuordnung sein (host, user, password_file …).")
    _reject_unknown(value, _IMAP_KEYS, f"{where}: imap")
    result: dict[str, Any] = {}
    for key in ("host", "user"):
        text = value.get(key)
        if not isinstance(text, str) or not text.strip():
            raise ConfigError(f"{where}: imap.{key} fehlt.")
        result[key] = text.strip()
    secrets = [key for key in _IMAP_SECRET_KEYS if value.get(key) not in (None, "")]
    if len(secrets) != 1:
        raise ConfigError(
            f"{where}: imap braucht genau eine der Angaben password, password_file oder password_env"
            + (f" (angegeben: {', '.join(secrets)})." if secrets else ".")
        )
    result["password"] = _imap_password(secrets[0], value[secrets[0]], where, env)
    if "port" in value:
        result["port"] = _int(value["port"], f"{where}: imap.port", minimum=1, maximum=65535)
    for key in ("ssl", "allow_plaintext"):
        if key in value:
            result[key] = _bool(value[key], f"{where}: imap.{key}")
    for key in ("folder", "processed_folder", "rejected_folder", "no_attachment_folder"):
        if key in value:
            text = value[key]
            if not isinstance(text, str) or not text.strip():
                raise ConfigError(f"{where}: imap.{key} muss ein Ordnername sein.")
            result[key] = text.strip()
    if "allowed_senders" in value:
        senders = _strings(value["allowed_senders"], f"{where}: imap.allowed_senders")
        result["allowed_senders"] = tuple(sender.lower() for sender in senders)
    if "poll_seconds" in value:
        result["poll_seconds"] = _int(value["poll_seconds"], f"{where}: imap.poll_seconds", minimum=1)
    return result


def _imap_password(kind: str, value: Any, where: str, env: Mapping[str, str]) -> str:
    """The IMAP password; error messages name the source, never the secret."""
    if kind == "password":
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise ConfigError(f"{where}: imap.password muss ein Text sein.")
        return str(value)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: imap.{kind} muss ein Text sein.")
    if kind == "password_file":
        try:
            password = Path(value.strip()).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(
                f"{where}: imap.password_file {value.strip()} kann nicht gelesen werden ({exc.strerror or exc})."
            ) from None
        if not password:
            raise ConfigError(f"{where}: imap.password_file {value.strip()} ist leer.")
        return password
    password = env.get(value.strip()) or ""
    if not password:
        raise ConfigError(f"{where}: die Umgebungsvariable {value.strip()} (imap.password_env) ist nicht gesetzt oder leer.")
    return password


def normalize_address(address: str | None) -> str | None:
    """``"Name <A@B.de>"`` → ``"a@b.de"`` (as the SMTP receiver does); None without address."""
    if not address:
        return None
    _, addr = parseaddr(str(address))
    addr = (addr or str(address)).strip().strip("<>").strip().lower()
    return addr or None


def _parse_recipients(value: Any, where: str) -> tuple[str, ...]:
    result: list[str] = []
    for text in _strings(value, f"{where}: smtp_recipients"):
        address = normalize_address(text)
        if not address or "@" not in address or any(ch.isspace() for ch in address):
            raise ConfigError(f"{where}: smtp_recipients: „{text}“ ist keine E-Mail-Adresse.")
        if address not in result:
            result.append(address)
    return tuple(result)


def _parse_watch_folders(value: Any, where: str) -> dict[str, Path]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: „watch_folders“ muss eine Zuordnung name: /pfad sein, z. B. {{nas: /watch/nas}}.")
    result: dict[str, Path] = {}
    for name, path in value.items():
        label = str(name).strip()
        if not label or "/" in label or "\\" in label or label.startswith(".") or any(ch.isspace() for ch in label):
            raise ConfigError(f"{where}: watch_folders: „{name}“ ist kein gültiger Name (z. B. nas).")
        if not isinstance(path, str) or not path.strip():
            raise ConfigError(f"{where}: watch_folders.{label} muss ein Pfad sein.")
        folder = Path(path.strip())
        if not folder.is_absolute():
            raise ConfigError(f"{where}: watch_folders.{label} muss ein absoluter Pfad sein, nicht „{path}“.")
        result[label] = folder
    return result


def _resolved(path: Path) -> Path:
    try:
        return Path(path).resolve()
    except OSError:
        return Path(os.path.abspath(path))


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _check_households(households: list[Household], root: Path) -> None:
    """Checks across households: unique ids, paths and addresses, no nesting into data directories."""
    seen: dict[str, Household] = {}
    for household in households:
        if household.id in seen:
            raise ConfigError(f"{HOUSEHOLDS_FILE}: die id „{household.id}“ ist doppelt vergeben.")
        seen[household.id] = household

    root_resolved = _resolved(root)
    in_root = [h.id for h in households if _resolved(h.path) == root_resolved]
    if len(in_root) > 1:
        raise ConfigError(
            f"{HOUSEHOLDS_FILE}: nur ein Haushalt darf path: \".\" (das Datenverzeichnis selbst) verwenden, "
            f"nicht {', '.join(in_root)}."
        )

    paths: dict[Path, str] = {}
    for household in households:
        resolved = _resolved(household.path)
        if resolved in paths:
            raise ConfigError(
                f"{HOUSEHOLDS_FILE}: die Haushalte „{paths[resolved]}“ und „{household.id}“ verwenden "
                "dasselbe Verzeichnis."
            )
        paths[resolved] = household.id

    # The data root's own directories (and the global config/index with
    # households.yaml and the server lock) are off limits for every other
    # household, as are the data directories of all households.
    owners: list[tuple[str | None, Path]] = [(None, d) for d in Layout(data=root, archive=root / "archiv").data_dirs()]
    for household in households:
        layout = Layout(data=household.path, archive=household.path / "archiv")
        owners.extend((household.id, d) for d in layout.data_dirs())
    for household in households:
        resolved = _resolved(household.path)
        for owner, directory in owners:
            if owner == household.id or (owner is None and resolved == root_resolved):
                continue
            if _inside(resolved, _resolved(directory)):
                whose = f"des Haushalts „{owner}“" if owner else "des Datenverzeichnisses"
                raise ConfigError(
                    f"{_where(household.id, 0)}: das Verzeichnis {household.path} liegt im Ordner "
                    f"„{directory.name}“ {whose} – bitte einen eigenen Pfad wählen (z. B. haushalte/{household.id})."
                )

    addresses: dict[str, str] = {}
    for household in households:
        for address in household.smtp_recipients:
            if address in addresses and addresses[address] != household.id:
                raise ConfigError(
                    f"{HOUSEHOLDS_FILE}: die SMTP-Adresse {address} ist bei „{addresses[address]}“ und "
                    f"„{household.id}“ eingetragen."
                )
            addresses[address] = household.id


# ---------------------------------------------------------------------------
# Settings of a household


def settings_for(
    settings: Settings, household: Household, *, households: Sequence[Household] | None = None
) -> Settings:
    """The settings ``household``'s services run with.

    Without ``households.yaml`` these are ``settings`` themselves. Otherwise:
    ``data_dir`` is the household's directory and ``archive_dir`` its
    ``archiv`` (``DOCUSHELF_ARCHIVE_DIR`` only counts with a single
    household); owner names, automatic filing, IMAP and the notification URL
    come from the household where it sets them. The global IMAP account and
    watch folders belong to the household living in the data root (path
    ``"."``). The SMTP receiver is global (see :class:`Registry`), so the
    household's settings have none.

    ``households`` (all households) saves reading ``households.yaml`` again.
    """
    if not configured(settings):
        return settings
    if households is None:
        households = load_households(settings)
    data_dir = Path(household.path)
    root = settings.root_dir
    in_root = _resolved(data_dir) == _resolved(root)
    archive_dir = data_dir / "archiv"
    explicit_archive = Path(settings.archive_dir) != root / "archiv"
    if explicit_archive and len(households) == 1:
        archive_dir = Path(settings.archive_dir)

    sources: list[SourceConfig] = [s for s in settings.sources if s.path is None]
    if in_root:
        sources.extend(s for s in settings.sources if s.path is not None)
    for name, path in household.watch_folders.items():
        source_name = f"watch-{name}"
        sources = [s for s in sources if s.name != source_name]
        sources.append(SourceConfig(name=source_name, path=Path(path), stable_seconds=settings.stable_seconds))

    imap = settings.imap if in_root else None
    if household.imap:
        imap = _imap_config(household.imap)

    changes: dict[str, Any] = {
        "data_dir": data_dir,
        "archive_dir": archive_dir,
        "sources": tuple(sources),
        "imap": imap,
        "smtp": None,
        "household_name": household.name,
        "data_root": None if data_dir == root else root,
    }
    if household.owner_names is not None:
        changes["owner_names"] = tuple(household.owner_names)
    if household.notify_url is not None:
        changes["notify_url"] = household.notify_url
    elif not in_root:
        # Notifications contain document titles: like the global mailbox, the global
        # notification target only belongs to the household in the data root.
        changes["notify_url"] = None
    if household.auto_file:
        changes["auto_file"] = replace(settings.auto_file, **household.auto_file)
    return settings.with_overrides(**changes)


def _imap_config(values: Mapping[str, Any]) -> ImapConfig:
    options = {key: value for key, value in values.items() if key not in ("host", "user", "password")}
    return ImapConfig(host=values["host"], username=values["user"], password=values["password"], **options)


def warn_ignored_archive_dir(settings: Settings, households: Sequence[Household]) -> None:
    """With several households a separately configured archive directory is not used."""
    if len(households) > 1 and Path(settings.archive_dir) != settings.root_dir / "archiv":
        log.warning(
            "DOCUSHELF_ARCHIVE_DIR (%s) is ignored with several households; every household uses "
            "<household>/archiv",
            settings.archive_dir,
        )


# ---------------------------------------------------------------------------
# Registry


def _user_matches(household: Household, user: Any) -> bool:
    """Membership by user name, e-mail address or group (case-insensitive)."""
    users = {u.casefold() for u in household.users}
    groups = {g.casefold() for g in household.groups}
    username = str(getattr(user, "username", "") or "").strip().casefold()
    email = str(getattr(user, "email", "") or "").strip().casefold()
    if username and username in users:
        return True
    if email and email in users:
        return True
    return any(str(group).strip().casefold() in groups for group in getattr(user, "groups", None) or ())


def clean_member(value: Any) -> str:
    """A user name or e-mail address entered as a household member (trimmed), or ConfigError."""
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise ConfigError("Bitte einen Benutzernamen oder eine E-Mail-Adresse angeben.")
    if len(text) > MAX_MEMBER_LENGTH:
        raise ConfigError(f"Der Benutzername ist zu lang (höchstens {MAX_MEMBER_LENGTH} Zeichen).")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ConfigError("Der Benutzername darf keine Zeilenumbrüche oder Steuerzeichen enthalten.")
    return text


def _member_list(value: Any) -> list[str]:
    """The ``users`` entry of a household as a list of texts (already validated by the loader)."""
    if value is None:
        return []
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        value = [value]
    return [str(item).strip() for item in value if str(item).strip()]


def _single_household_allowed(settings: Settings, user: Any) -> bool:
    """The classic rules: ``DOCUSHELF_ALLOWED_USERS/_GROUPS`` or ``ALLOW_ALL_OIDC_USERS``."""
    users = {str(u).strip().casefold() for u in settings.allowed_users or () if str(u).strip()}
    wanted = {str(g).strip().casefold() for g in settings.allowed_groups or () if str(g).strip()}
    if not users and not wanted:
        return bool(settings.allow_all_oidc_users)
    username = str(getattr(user, "username", "") or "").strip().casefold()
    email = str(getattr(user, "email", "") or "").strip().casefold()
    if (username and username in users) or (email and email in users):
        return True
    return any(str(group).strip().casefold() in wanted for group in getattr(user, "groups", None) or ())


def _default_services_factory(settings: Settings, *, household: Household) -> Any:
    from .services import Services

    return Services(settings, household=household)


class Registry:
    """The households of the installation and their service containers.

    ``default`` is the only household without ``households.yaml`` and None in
    household mode (the web layer then resolves the household per request).
    Service containers are created on first use and kept.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        services_factory: Callable[..., Any] | None = None,
        env: Mapping[str, str] | None = None,
    ):
        self.settings = settings
        self.configured = configured(settings)
        self._env = env
        households, admins = _load(settings, env)
        self.households: list[Household] = households
        # Admins named in households.yaml (casefolded); see is_admin().
        self.admins: tuple[str, ...] = admins
        self.default: Household | None = None if self.configured else self.households[0]
        self._index(self.households)
        self._factory = services_factory or _default_services_factory
        self._services: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._started: list[str] = []
        # The global SMTP receiver (household mode only; a single household keeps its own).
        self.smtp: Any = None
        from .pdf.ocr import configure_ocr_slots

        configure_ocr_slots(settings.ocr_parallel)
        if self.configured:
            warn_ignored_archive_dir(settings, self.households)

    @property
    def multi(self) -> bool:
        """Household mode (``households.yaml`` exists)."""
        return self.default is None

    def __iter__(self) -> Iterator[Household]:
        return iter(self.households)

    def _index(self, households: list[Household]) -> None:
        self._by_id = {household.id: household for household in households}
        self._recipients = {address: household for household in households for address in household.smtp_recipients}

    def get(self, household_id: str) -> Household:
        try:
            return self._by_id[household_id]
        except (KeyError, TypeError):
            raise NotFound(f"Den Haushalt „{household_id}“ gibt es nicht.") from None

    def settings_for(self, household_id: str) -> Settings:
        return settings_for(self.settings, self.get(household_id), households=self.households)

    def services(self, household_id: str) -> Any:
        """The service container of a household (created on first use)."""
        household = self.get(household_id)
        with self._lock:
            services = self._services.get(household.id)
            if services is None:
                services = self._factory(self.settings_for(household.id), household=household)
                if getattr(services, "household", None) is None:
                    try:
                        services.household = household
                    except AttributeError:
                        pass
                self._services[household.id] = services
            return services

    def created(self) -> list[Any]:
        """The service containers created so far (in household order)."""
        with self._lock:
            return [self._services[h.id] for h in self.households if h.id in self._services]

    # -- access ---------------------------------------------------------------------
    def for_user(self, user: Any) -> list[Household]:
        """The households ``user`` may use.

        The local emergency account may use every household. Without
        ``households.yaml`` the classic ``DOCUSHELF_ALLOWED_*`` rules decide
        for SSO accounts; otherwise the groups and users of each household.
        """
        if user is None:
            return []
        method = getattr(user, "method", None)
        if method == "local":
            return list(self.households)
        if self.default is not None:
            if method == "token":
                return [self.default]
            return [self.default] if _single_household_allowed(self.settings, user) else []
        if method == "token":
            # A token belongs to the household whose tokens.yaml holds it; the web layer knows which.
            return []
        return [household for household in self.households if _user_matches(household, user)]

    def is_admin(self, user: Any) -> bool:
        """Whether ``user`` may manage household members (household mode only).

        Admins are the local emergency account, the ``admins`` of
        ``households.yaml`` and ``DOCUSHELF_ADMIN_USERS`` (user name or e-mail
        address, case-insensitive). API tokens never are.
        """
        if user is None or not self.multi:
            return False
        method = getattr(user, "method", None)
        if method == "local":
            return True
        if method != "oidc":
            return False
        names = set(self.admins) | {str(n).strip().casefold() for n in self.settings.admin_users or () if str(n).strip()}
        username = str(getattr(user, "username", "") or "").strip().casefold()
        email = str(getattr(user, "email", "") or "").strip().casefold()
        return bool((username and username in names) or (email and email in names))

    def by_smtp_recipient(self, address: str) -> Household | None:
        """The household mail to ``address`` belongs to (None: unknown address).

        Without ``households.yaml`` everything belongs to the only household.
        """
        if self.default is not None:
            return self.default
        normalized = normalize_address(address)
        if not normalized:
            return None
        return self._recipients.get(normalized)

    # -- membership ---------------------------------------------------------------------
    def change_members(self, household_id: str, *, add: str | None = None, remove: str | None = None) -> Household:
        """Add a user to or remove a user from the ``users`` of a household.

        The change is made in ``households.yaml`` itself: the current file is
        read, the entry changed, and the whole result validated like at start-up
        before the file is replaced (atomically, the previous content kept as
        ``households.yaml.bak``). Other entries and the order of the households
        stay as they are; comments are lost. Afterwards the groups, users and
        admins of the running households are taken from the new file, so access
        changes with the next request; service containers keep running.

        Raises ConfigError (German) for invalid input or an invalid file,
        Conflict for a user already listed and NotFound for an unknown
        household or a user not listed. Nothing is written in these cases.
        """
        if not self.multi:
            raise ConfigError("Haushaltsmitglieder lassen sich nur mit households.yaml verwalten.")
        if (add is None) == (remove is None):
            raise ValueError("exactly one of add and remove is required")
        name = clean_member(add if add is not None else remove)
        env = os.environ if self._env is None else self._env
        path = households_file(self.settings)
        with self._lock:
            household = self.get(household_id)
            text = _read_text(path)
            raw = _parse_yaml(text)
            try:
                parse_households(raw, self.settings, env=env)
            except ConfigError as exc:
                raise ConfigError(f"Die Datei ist derzeit ungültig, es wurde nichts geändert. {exc}") from exc
            entry = next(
                (e for e in raw["households"] if isinstance(e, dict) and e.get("id") == household.id), None
            )
            if entry is None:
                raise NotFound(
                    f"Den Haushalt „{household.id}“ gibt es in {HOUSEHOLDS_FILE} nicht mehr. "
                    "Haushalte hinzufügen oder entfernen wirkt erst nach einem Neustart."
                )
            users = _member_list(entry.get("users"))
            key = name.casefold()
            listed = [u for u in users if u.strip().casefold() == key]
            if add is not None:
                if listed:
                    raise Conflict(f"„{name}“ ist schon Mitglied im Haushalt „{household.name}“.")
                users.append(name)
            else:
                if not listed:
                    raise NotFound(f"„{name}“ steht nicht (mehr) bei den Benutzern des Haushalts „{household.name}“.")
                users = [u for u in users if u.strip().casefold() != key]
            entry["users"] = users

            new_text = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False, default_flow_style=False, width=4096)
            new_raw = _parse_yaml(new_text)
            households = parse_households(new_raw, self.settings, env=env)
            admins = parse_admins(new_raw)
            if [h.id for h in households] != [h.id for h in self.households]:
                raise ConfigError(
                    f"In {HOUSEHOLDS_FILE} wurden inzwischen Haushalte hinzugefügt, entfernt oder umsortiert. "
                    "Bitte DocuShelf zuerst neu starten; es wurde nichts geändert."
                )
            backup = path.with_name(path.name + BACKUP_SUFFIX)
            try:
                # Keep the file's permissions: it may hold an IMAP password.
                file_mode = stat.S_IMODE(path.stat().st_mode)
                atomic_write_text(backup, text, mode=file_mode)
                atomic_write_text(path, new_text, mode=file_mode)
            except OSError as exc:
                raise ConfigError(
                    f"{HOUSEHOLDS_FILE} konnte nicht gespeichert werden: {exc.strerror or exc}"
                ) from exc
            self._apply_members(households, admins)
            changed = self.get(household.id)
        log.info(
            "household %s: user %s %s", household.id, "added" if add is not None else "removed", name
        )
        return changed

    def _apply_members(self, households: Sequence[Household], admins: tuple[str, ...]) -> None:
        """Take groups and users from freshly loaded households; everything else stays as started."""
        fresh = {household.id: household for household in households}
        updated = [
            replace(old, groups=fresh[old.id].groups, users=fresh[old.id].users) if old.id in fresh else old
            for old in self.households
        ]
        self._index(updated)
        self.households = updated
        self.admins = admins
        for household in updated:
            services = self._services.get(household.id)
            if services is not None and getattr(services, "household", None) is not None:
                try:
                    services.household = household
                except AttributeError:
                    pass

    # -- lifecycle --------------------------------------------------------------------
    def recover(self) -> None:
        for household in self.households:
            self.services(household.id).recover()

    def start(self) -> None:
        """Start the background services of every household (and the global SMTP receiver)."""
        with self._lock:
            containers = [(h, self.services(h.id)) for h in self.households]
            if self.multi:
                from .inbox.intake import protect_households

                protect_households(self)
            try:
                for household, services in containers:
                    if household.id in self._started:
                        continue
                    services.start()
                    self._started.append(household.id)
                if self.multi and self.settings.smtp is not None and self.smtp is None:
                    from .channels.smtp import receiver_for_registry

                    self.smtp = receiver_for_registry(self, self.settings.smtp)
                    self.smtp.start()
            except BaseException:
                self.stop()
                raise
        if self.multi:
            log.info("%d households started: %s", len(self.households), ", ".join(self._started))

    def stop(self) -> None:
        """Stop everything :meth:`start` started (newest first); errors are logged."""
        with self._lock:
            smtp, self.smtp = self.smtp, None
            started, self._started = list(self._started), []
            if smtp is not None:
                try:
                    smtp.stop()
                except Exception:
                    log.exception("error while stopping the SMTP receiver")
            for household_id in reversed(started):
                services = self._services.get(household_id)
                if services is None:
                    continue
                try:
                    services.stop()
                except Exception:
                    log.exception("error while stopping household %s", household_id)

    def close(self) -> None:
        """Close the index databases of containers that were never started."""
        with self._lock:
            for household_id, services in self._services.items():
                if household_id in self._started:
                    continue
                try:
                    services.index.close()
                except Exception:
                    log.debug("cannot close the index of household %s", household_id, exc_info=True)
