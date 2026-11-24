"""Runtime configuration.

Everything is configured through environment variables (``DOCUSHELF_*``).
Secrets can be passed as files with the ``_FILE`` suffix
(``DOCUSHELF_OIDC_CLIENT_SECRET_FILE=/run/secrets/oidc``). Non-secret values
may also live in ``<data>/config/settings.yaml`` (lower-case keys without the
prefix); environment variables win.
"""

from __future__ import annotations

import math
import os
import re
import secrets
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from .errors import ConfigError

PREFIX = "DOCUSHELF_"
HOUSEHOLDS_FILE = "households.yaml"
DEFAULT_HOUSEHOLD_NAME = "Zuhause"

# Default for trusted reverse proxies: loopback and private networks (Docker,
# LAN). X-Forwarded-For from anywhere else is ignored, so clients cannot fake
# their address to dodge the login rate limit.
PRIVATE_NETWORKS = "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,fc00::/7"
BUILTIN_SOURCES = ("upload", "smb", "ftp", "mail", "smtp")

# Automatic filing: modes, their German names, and the sources filed by default.
AUTO_FILE_MODES = ("off", "rules", "confident", "always")
AUTO_FILE_MODE_ALIASES = {
    "aus": "off",
    "regeln": "rules",
    "sicher": "confident",
    "immer": "always",
    # YAML 1.1 reads a bare "off"/"no" in settings.yaml as false.
    "false": "off",
    "no": "off",
    "nein": "off",
    "0": "off",
}
DEFAULT_AUTO_FILE_SOURCES = ("upload", "smb", "ftp", "mail", "smtp", "watch-*")
DEFAULT_AUTO_FILE_THRESHOLD = 0.75

LLM_PROVIDERS = ("ollama", "openai", "anthropic")
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"
ANTHROPIC_URL = "https://api.anthropic.com"
_LLM_PROVIDER_ALIASES = {"openai-compatible": "openai", "openai_compatible": "openai", "openai-kompatibel": "openai"}
DEFAULT_LLM_URL = "http://ollama:11434"
DEFAULT_LLM_MODEL = "qwen2.5:7b"
DEFAULT_LLM_CONTEXT = 8192
MIN_LLM_CONTEXT = 1024

_TRUE_WORDS = {"1", "true", "yes", "ja", "on", "an"}
_FALSE_WORDS = {"0", "false", "no", "nein", "off", "aus"}
_URL_RE = re.compile(r"^https?://[^/\s?#]+", re.IGNORECASE)


def parse_auto_file_mode(value: Any, *, name: str = PREFIX + "AUTO_FILE") -> str:
    """The canonical mode (off, rules, confident, always) for a value typed by a person.

    Case does not matter; the German names aus, regeln, sicher, immer work as well.
    """
    if value is None or value is False:
        return "off"
    text = str(value).strip().casefold()
    if not text:
        return "off"
    if text in AUTO_FILE_MODES:
        return text
    if text in AUTO_FILE_MODE_ALIASES:
        return AUTO_FILE_MODE_ALIASES[text]
    raise ConfigError(
        f"{name}: „{value}“ ist ungültig – erlaubt sind off, rules, confident, always "
        "(oder aus, regeln, sicher, immer)."
    )


def parse_threshold(value: Any, *, name: str = PREFIX + "AUTO_FILE_THRESHOLD") -> float:
    """A confidence threshold between 0 and 1; "0,8" and "80 %" are understood too."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return DEFAULT_AUTO_FILE_THRESHOLD
    if isinstance(value, bool):
        raise ConfigError(f"{name} muss eine Zahl zwischen 0 und 1 sein (z. B. 0.75).")
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().replace(",", ".")
        percent = text.endswith("%")
        text = text.rstrip("%").strip()
        try:
            number = float(text)
        except ValueError as exc:
            raise ConfigError(f"{name} muss eine Zahl zwischen 0 und 1 sein (z. B. 0.75).") from exc
        if percent:
            number /= 100
    if math.isnan(number) or not 0.0 <= number <= 1.0:
        raise ConfigError(f"{name} muss zwischen 0 und 1 liegen (z. B. 0.75 für 75 %), nicht {value}.")
    return number


def parse_auto_file_sources(values: Iterable[Any], *, name: str = PREFIX + "AUTO_FILE_SOURCES") -> tuple[str, ...]:
    """Source names (lower case); a trailing ``*`` is a prefix wildcard ("watch-*")."""
    result: list[str] = []
    for value in values:
        text = str(value or "").strip().casefold()
        if not text:
            continue
        if "*" in text[:-1] or any(ch in text for ch in ",;\n\t") or any(ord(ch) < 32 for ch in text):
            raise ConfigError(f"{name}: „{value}“ ist ungültig – „*“ ist nur am Ende erlaubt (z. B. watch-*).")
        if text not in result:
            result.append(text)
    return tuple(result)


def parse_llm_provider(value: Any, *, name: str = PREFIX + "LLM_PROVIDER") -> str:
    """``ollama``, ``openai`` (any OpenAI-compatible server) or ``anthropic``."""
    text = str(value or "").strip().casefold() or "ollama"
    text = _LLM_PROVIDER_ALIASES.get(text, text)
    if text not in LLM_PROVIDERS:
        raise ConfigError(f"{name}: „{value}“ ist ungültig – erlaubt sind ollama, openai und anthropic.")
    return text


@dataclass(frozen=True)
class AutoFileConfig:
    """Automatic filing of inbox items (off unless switched on).

    ``mode``: ``off`` – nothing is filed automatically; ``rules`` – only matches of
    rules marked ``auto: true``; ``confident`` – also decisions at or above
    ``threshold``; ``always`` – everything, decisions below the threshold are
    marked for review. ``sources`` lists the inbox sources that may be filed
    automatically (a trailing ``*`` is a prefix wildcard; ``paperless`` and
    ``unbekannt`` only when listed explicitly).
    """

    mode: str = "off"
    threshold: float = DEFAULT_AUTO_FILE_THRESHOLD
    sources: tuple[str, ...] = DEFAULT_AUTO_FILE_SOURCES
    new_folders: bool = True
    wait_for_llm: bool = True
    notify: bool = True

    def __post_init__(self) -> None:
        # Values may come from code as well as from the environment: normalize and validate both.
        object.__setattr__(self, "mode", parse_auto_file_mode(self.mode))
        object.__setattr__(self, "threshold", parse_threshold(self.threshold))
        sources = (self.sources,) if isinstance(self.sources, str) else tuple(self.sources or ())
        object.__setattr__(self, "sources", parse_auto_file_sources(sources))
        for key in ("new_folders", "wait_for_llm", "notify"):
            object.__setattr__(self, key, bool(getattr(self, key)))

    @property
    def enabled(self) -> bool:
        return self.mode != "off"


@dataclass(frozen=True)
class SourceConfig:
    """A drop zone the intake watches. ``path=None`` means ``eingang/quellen/<name>``."""

    name: str
    path: Path | None = None
    stable_seconds: float = 5.0
    # Files in external watch folders are removed after they were taken over.
    # Set to False to leave them (they are then remembered by hash).
    remove_after_import: bool = True


@dataclass(frozen=True)
class ImapConfig:
    host: str
    username: str
    password: str = field(repr=False)
    port: int = 993
    # True: implicit TLS. False: plain port, upgraded with STARTTLS.
    ssl: bool = True
    # Only with ssl=False: log in without encryption if the server offers no
    # STARTTLS. Off by default, because the password would cross the network in clear.
    allow_plaintext: bool = False
    folder: str = "INBOX"
    processed_folder: str = "DocuShelf/Verarbeitet"
    rejected_folder: str = "DocuShelf/Abgelehnt"
    no_attachment_folder: str = "DocuShelf/Ohne Anhang"
    allowed_senders: tuple[str, ...] = ()
    poll_seconds: int = 60


@dataclass(frozen=True)
class SmtpConfig:
    recipient: str
    host: str = "0.0.0.0"
    port: int = 2525
    allowed_networks: tuple[str, ...] = ()
    max_message_bytes: int = 50 * 1024 * 1024
    username: str | None = None
    password: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class LlmConfig:
    """A language model for suggestions. The document text is sent to ``url``.

    ``provider``: ``ollama`` (``POST {url}/api/chat``) or ``openai`` – any server
    with the OpenAI chat-completions API (llama.cpp, vLLM, LocalAI, LM Studio …)
    or ``anthropic`` (the Anthropic Messages API; ``url`` is informational).
    ``context_tokens`` is Ollama's ``num_ctx``: its small default context would
    silently cut off the folder list or the document. ``api_key`` is sent as a
    bearer token and never shown or logged.
    """

    url: str
    model: str
    timeout: int = 180
    max_chars: int = 6000
    provider: str = "ollama"
    api_key: str | None = field(default=None, repr=False)
    context_tokens: int = DEFAULT_LLM_CONTEXT

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", parse_llm_provider(self.provider))
        object.__setattr__(self, "api_key", (str(self.api_key).strip() or None) if self.api_key else None)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    archive_dir: Path
    base_url: str = "http://localhost:8000"
    secret_key: str = ""
    timezone: str = "Europe/Berlin"
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"

    # Authentication
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    oidc_label: str = "Mit SSO anmelden"
    oidc_scopes: str = "openid email profile"
    allowed_users: tuple[str, ...] = ()
    allowed_groups: tuple[str, ...] = ()
    allow_all_oidc_users: bool = False
    admin_user: str = "admin"
    admin_password_hash: str | None = None
    admin_password: str | None = None
    session_max_age: int = 14 * 24 * 3600
    secure_cookies: bool | None = None
    trusted_proxies: str = PRIVATE_NETWORKS

    # Processing
    ocr_enabled: bool = True
    ocr_languages: str = "deu+eng"
    ocr_jobs: int = 2
    ocr_timeout: int = 900
    ocr_output_type: str = "pdfa-2"
    ocr_extra_args: tuple[str, ...] = ()
    blank_page_removal: bool = True
    blank_page_threshold: float = 0.004
    worker_threads: int = 1
    # Number of OCR runs allowed at the same time in the whole process (all households).
    ocr_parallel: int = 1
    max_upload_mb: int = 200

    # Sources
    sources: tuple[SourceConfig, ...] = ()
    # How long a file in a local watch folder must stay unchanged before it is taken over.
    stable_seconds: float = 5.0
    imap: ImapConfig | None = None
    smtp: SmtpConfig | None = None

    # Suggestions
    llm: LlmConfig | None = None
    similarity_enabled: bool = True
    # Automatic filing (always present; mode "off" unless switched on)
    auto_file: AutoFileConfig = field(default_factory=AutoFileConfig)
    # Names of the people the archive belongs to; never suggested as a document's sender.
    owner_names: tuple[str, ...] = ()

    # Maintenance
    reconcile_interval: int = 6 * 3600
    trash_retention_days: int = 30
    keep_originals_days: int = 30
    notify_url: str | None = None

    # Households: display name of the only household without households.yaml, and
    # the global data root when these settings belong to a household elsewhere
    # (None: ``data_dir`` is the data root).
    household_name: str = DEFAULT_HOUSEHOLD_NAME
    data_root: Path | None = None

    # Feature switches for background services (tests turn them off)
    start_background: bool = True
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def cookies_secure(self) -> bool:
        if self.secure_cookies is not None:
            return self.secure_cookies
        return self.base_url.startswith("https://")

    @property
    def root_dir(self) -> Path:
        """The global data root (holds ``config/settings.yaml``, ``households.yaml`` and the server lock)."""
        return Path(self.data_root) if self.data_root is not None else Path(self.data_dir)

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id)

    @property
    def local_login_enabled(self) -> bool:
        return bool(self.admin_password_hash or self.admin_password)

    def with_overrides(self, **changes: Any) -> "Settings":
        return replace(self, **changes)


# ---------------------------------------------------------------------------
# Parsing helpers


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "ja", "on"}


def _split_list(value: str) -> tuple[str, ...]:
    parts = [part.strip() for chunk in value.split("\n") for part in chunk.split(",")]
    return tuple(part for part in parts if part)


class _Source:
    """Looks a key up in env (with optional _FILE) and then in settings.yaml."""

    def __init__(self, env: Mapping[str, str], file_values: Mapping[str, Any]):
        self.env = env
        self.file_values = file_values

    def raw(self, key: str) -> str | None:
        env_key = PREFIX + key.upper()
        if env_key in self.env and self.env[env_key] != "":
            return self.env[env_key]
        file_key = env_key + "_FILE"
        if file_key in self.env and self.env[file_key]:
            try:
                return Path(self.env[file_key]).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ConfigError(f"{file_key}: {exc}") from exc
        value = self.file_values.get(key.lower())
        if value is None:
            return None
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (list, tuple)):
            return ",".join(str(v) for v in value)
        return str(value)

    def str(self, key: str, default: str | None = None) -> str | None:
        value = self.raw(key)
        return default if value is None else value

    def bool(self, key: str, default: bool) -> bool:
        value = self.raw(key)
        return default if value is None else _truthy(value)

    def strict_bool(self, key: str, default: bool) -> bool:
        """Like :meth:`bool`, but anything other than a yes/no word is an error."""
        value = self.raw(key)
        if value is None or not value.strip():
            return default
        word = value.strip().casefold()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
        raise ConfigError(f"{PREFIX}{key.upper()} muss true oder false sein, nicht „{value}“.")

    def int(self, key: str, default: int) -> int:
        value = self.raw(key)
        if value is None:
            return default
        try:
            return int(value)
        except ValueError as exc:
            raise ConfigError(f"{PREFIX}{key.upper()} muss eine Zahl sein") from exc

    def float(self, key: str, default: float) -> float:
        value = self.raw(key)
        if value is None:
            return default
        try:
            return float(value)
        except ValueError as exc:
            raise ConfigError(f"{PREFIX}{key.upper()} muss eine Zahl sein") from exc

    def list(self, key: str) -> tuple[str, ...]:
        value = self.raw(key)
        return _split_list(value) if value else ()


def _load_settings_file(data_dir: Path) -> dict[str, Any]:
    path = data_dir / "config" / "settings.yaml"
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"settings.yaml ist ungültig: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError("settings.yaml muss eine Zuordnung (key: value) enthalten")
    return {str(k).lower(): v for k, v in loaded.items()}


def _load_or_create_secret(data_dir: Path) -> str:
    path = data_dir / "config" / "secret.key"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(48)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
    return value


def _parse_watch_folders(value: tuple[str, ...], stable: float) -> list[SourceConfig]:
    """``name=/path`` entries for additional watch folders."""
    result = []
    for entry in value:
        if "=" not in entry:
            raise ConfigError(f"Watch-Ordner '{entry}' muss die Form name=/pfad haben")
        name, path = (part.strip() for part in entry.split("=", 1))
        if not name or not path:
            raise ConfigError(f"Watch-Ordner '{entry}' ist unvollständig")
        result.append(SourceConfig(name=f"watch-{name}", path=Path(path), stable_seconds=stable))
    return result


def _load_llm(src: _Source) -> LlmConfig | None:
    """The language model settings, or None while ``DOCUSHELF_LLM_ENABLED`` is off."""
    if not src.bool("llm_enabled", False):
        return None
    provider = parse_llm_provider(src.str("llm_provider"))
    url = (src.str("llm_url") or "").strip().rstrip("/")
    if not url and provider == "anthropic":
        url = ANTHROPIC_URL
    if not url:
        if provider != "ollama":
            raise ConfigError(
                f"{PREFIX}LLM_URL fehlt – Adresse des OpenAI-kompatiblen Servers, z. B. http://llama:8080/v1"
            )
        url = DEFAULT_LLM_URL
    if not _URL_RE.match(url):
        raise ConfigError(f"{PREFIX}LLM_URL muss mit http:// oder https:// beginnen, nicht „{url}“.")
    timeout = src.int("llm_timeout", 180)
    if timeout < 1:
        raise ConfigError(f"{PREFIX}LLM_TIMEOUT muss mindestens 1 Sekunde sein.")
    max_chars = src.int("llm_max_chars", 6000)
    if max_chars < 500:
        raise ConfigError(f"{PREFIX}LLM_MAX_CHARS muss mindestens 500 sein.")
    context_tokens = src.int("llm_context", DEFAULT_LLM_CONTEXT)
    if context_tokens < MIN_LLM_CONTEXT:
        raise ConfigError(f"{PREFIX}LLM_CONTEXT muss mindestens {MIN_LLM_CONTEXT} sein (Standard {DEFAULT_LLM_CONTEXT}).")
    return LlmConfig(
        url=url,
        model=(src.str("llm_model") or "").strip()
        or (DEFAULT_ANTHROPIC_MODEL if provider == "anthropic" else DEFAULT_LLM_MODEL),
        timeout=timeout,
        max_chars=max_chars,
        provider=provider,
        api_key=(src.str("llm_api_key") or "").strip() or None,
        context_tokens=context_tokens,
    )


def load_settings(env: Mapping[str, str] | None = None, **overrides: Any) -> Settings:
    """Build Settings from the environment (and settings.yaml)."""
    env = dict(os.environ if env is None else env)
    data_dir = Path(env.get(PREFIX + "DATA_DIR") or "/data")
    src = _Source(env, _load_settings_file(data_dir))

    archive_dir = Path(src.str("archive_dir") or str(data_dir / "archiv"))
    stable = src.float("stable_seconds", 5.0)
    remote_stable = src.float("remote_stable_seconds", 10.0)

    sources: list[SourceConfig] = [SourceConfig(name="upload", stable_seconds=0.0)]
    if src.bool("smb_enabled", False) or src.bool("source_smb", True):
        sources.append(SourceConfig(name="smb", stable_seconds=remote_stable))
    if src.bool("ftp_enabled", False) or src.bool("source_ftp", True):
        sources.append(SourceConfig(name="ftp", stable_seconds=remote_stable))
    sources.append(SourceConfig(name="mail", stable_seconds=0.0))
    sources.append(SourceConfig(name="smtp", stable_seconds=0.0))
    sources.extend(_parse_watch_folders(src.list("watch_folders"), stable))

    imap = None
    if src.bool("imap_enabled", False):
        host, user, password = src.str("imap_host"), src.str("imap_user"), src.str("imap_password")
        if not (host and user and password):
            raise ConfigError("IMAP ist aktiviert, aber IMAP_HOST/IMAP_USER/IMAP_PASSWORD fehlen")
        imap = ImapConfig(
            host=host,
            username=user,
            password=password,
            port=src.int("imap_port", 993),
            ssl=src.bool("imap_ssl", True),
            allow_plaintext=src.bool("imap_allow_plaintext", False),
            folder=src.str("imap_folder", "INBOX") or "INBOX",
            processed_folder=src.str("imap_processed_folder", "DocuShelf/Verarbeitet") or "DocuShelf/Verarbeitet",
            rejected_folder=src.str("imap_rejected_folder", "DocuShelf/Abgelehnt") or "DocuShelf/Abgelehnt",
            no_attachment_folder=src.str("imap_no_attachment_folder", "DocuShelf/Ohne Anhang") or "DocuShelf/Ohne Anhang",
            allowed_senders=tuple(s.lower() for s in src.list("imap_allowed_senders")),
            poll_seconds=src.int("imap_poll_seconds", 60),
        )

    smtp = None
    if src.bool("smtp_enabled", False):
        recipient = src.str("smtp_recipient")
        # With households.yaml every household lists its own addresses (smtp_recipients).
        if not recipient and not (data_dir / "config" / HOUSEHOLDS_FILE).is_file():
            raise ConfigError("SMTP-Empfang ist aktiviert, aber SMTP_RECIPIENT fehlt")
        smtp = SmtpConfig(
            recipient=(recipient or "").lower(),
            host=src.str("smtp_host", "0.0.0.0") or "0.0.0.0",
            port=src.int("smtp_port", 2525),
            allowed_networks=src.list("smtp_allowed_networks"),
            max_message_bytes=src.int("smtp_max_message_mb", 50) * 1024 * 1024,
            username=src.str("smtp_user"),
            password=src.str("smtp_password"),
        )

    llm = _load_llm(src)
    auto_file = AutoFileConfig(
        mode=parse_auto_file_mode(src.str("auto_file")),
        threshold=parse_threshold(src.str("auto_file_threshold")),
        sources=(
            parse_auto_file_sources(src.list("auto_file_sources")) if src.raw("auto_file_sources")
            else DEFAULT_AUTO_FILE_SOURCES
        ),
        new_folders=src.strict_bool("auto_file_new_folders", True),
        wait_for_llm=src.strict_bool("auto_file_wait_for_llm", True),
        notify=src.strict_bool("auto_file_notify", True),
    )

    ocr_parallel = src.int("ocr_parallel", 1)
    if ocr_parallel < 1:
        raise ConfigError(f"{PREFIX}OCR_PARALLEL muss mindestens 1 sein.")

    secret_key = src.str("secret_key") or ""
    settings = Settings(
        data_dir=data_dir,
        archive_dir=archive_dir,
        base_url=(src.str("base_url", "http://localhost:8000") or "http://localhost:8000").rstrip("/"),
        secret_key=secret_key,
        timezone=src.str("timezone") or env.get("TZ") or "Europe/Berlin",
        host=src.str("host", "0.0.0.0") or "0.0.0.0",
        port=src.int("port", 8000),
        log_level=(src.str("log_level", "info") or "info").lower(),
        oidc_issuer=(src.str("oidc_issuer") or None),
        oidc_client_id=src.str("oidc_client_id"),
        oidc_client_secret=src.str("oidc_client_secret"),
        oidc_label=src.str("oidc_label", "Mit SSO anmelden") or "Mit SSO anmelden",
        oidc_scopes=src.str("oidc_scopes", "openid email profile") or "openid email profile",
        allowed_users=tuple(u.lower() for u in src.list("allowed_users")),
        allowed_groups=src.list("allowed_groups"),
        allow_all_oidc_users=src.bool("allow_all_oidc_users", False),
        admin_user=src.str("admin_user", "admin") or "admin",
        admin_password_hash=src.str("admin_password_hash"),
        admin_password=src.str("admin_password"),
        session_max_age=src.int("session_max_age", 14 * 24 * 3600),
        secure_cookies=(None if src.raw("secure_cookies") is None else src.bool("secure_cookies", True)),
        trusted_proxies=src.str("trusted_proxies", PRIVATE_NETWORKS) or PRIVATE_NETWORKS,
        ocr_enabled=src.bool("ocr_enabled", True),
        ocr_languages=src.str("ocr_languages", "deu+eng") or "deu+eng",
        ocr_jobs=src.int("ocr_jobs", 2),
        ocr_timeout=src.int("ocr_timeout", 900),
        ocr_output_type=src.str("ocr_output_type", "pdfa-2") or "pdfa-2",
        ocr_extra_args=src.list("ocr_extra_args"),
        blank_page_removal=src.bool("blank_page_removal", True),
        blank_page_threshold=src.float("blank_page_threshold", 0.004),
        worker_threads=max(1, src.int("worker_threads", 1)),
        ocr_parallel=ocr_parallel,
        max_upload_mb=src.int("max_upload_mb", 200),
        sources=tuple(sources),
        stable_seconds=stable,
        imap=imap,
        smtp=smtp,
        llm=llm,
        similarity_enabled=src.bool("similarity_enabled", True),
        auto_file=auto_file,
        owner_names=src.list("owner_names"),
        reconcile_interval=src.int("reconcile_interval", 6 * 3600),
        trash_retention_days=src.int("trash_retention_days", 30),
        keep_originals_days=src.int("keep_originals_days", 30),
        notify_url=src.str("notify_url"),
        household_name=(src.str("household_name") or "").strip() or DEFAULT_HOUSEHOLD_NAME,
        start_background=src.bool("start_background", True),
    )
    if overrides:
        settings = settings.with_overrides(**overrides)
    if not settings.secret_key:
        settings = settings.with_overrides(secret_key=_load_or_create_secret(settings.data_dir))
    return settings
