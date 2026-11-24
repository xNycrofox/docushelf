"""Installation-wide settings written from the web interface.

The settings pages store what an admin changes in ``<data>/config/settings.yaml``
(lower-case keys without the ``DOCUSHELF_`` prefix, read by
:func:`docushelf.config.load_settings`). Environment variables keep winning:
a key set through the environment is never written and the page shows it as
locked.

Secrets (the OIDC client secret, an API key of the language model, the SMTP
password) never go into the file itself. Each lives in
``config/secrets/<name>`` (file 600, directory 700) and the file references it
with ``<key>_file: secrets/<name>`` – the same ``_FILE`` mechanism the
environment offers. Secrets are never read back into a page.

Every save is validated with the loader the server uses at start-up before
anything is written; the previous file is kept as ``settings.yaml.bak`` and
the new one replaces it atomically.

:func:`apply_live` brings running service containers in line with settings
that take effect without a restart (automatic filing, notification URL, owner
names, retention periods).
"""

from __future__ import annotations

import logging
import os
import stat
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import yaml

from .config import PREFIX, SETTINGS_FILE, Settings, load_settings, parse_settings_text
from .errors import ConfigError
from .util.fsops import atomic_write_text

log = logging.getLogger(__name__)

SECRETS_DIR = "secrets"
BACKUP_SUFFIX = ".bak"
# Settings whose value is a secret, with the name of their file in config/secrets/.
SECRET_FILES = {
    "oidc_client_secret": "oidc-client-secret",
    "llm_api_key": "llm-api-key",
    "smtp_password": "smtp-password",
}
# Settings the running server picks up without a restart (see apply_live).
LIVE_KEYS = frozenset(
    {
        "auto_file",
        "auto_file_threshold",
        "auto_file_sources",
        "auto_file_new_folders",
        "auto_file_wait_for_llm",
        "auto_file_notify",
        "notify_url",
        "owner_names",
        "trash_retention_days",
        "keep_originals_days",
    }
)
HEADER = (
    "# DocuShelf – Einstellungen aus der Weboberfläche (Einstellungen → …).\n"
    "# Umgebungsvariablen (DOCUSHELF_…) haben Vorrang vor diesen Werten.\n"
    "# Geheimnisse stehen nicht hier, sondern in config/secrets/ (…_file).\n"
)
MAX_SECRET_LENGTH = 4096


def env_name(key: str) -> str:
    """``llm_url`` → ``DOCUSHELF_LLM_URL``."""
    return PREFIX + key.upper()


class SettingsFile:
    """``config/settings.yaml`` of an installation and the secrets it references.

    ``env`` is the environment the server was started with (default: the
    process environment); it decides which keys are locked.
    """

    def __init__(self, root_dir: Path, env: Mapping[str, str] | None = None):
        self.root_dir = Path(root_dir)
        self.config_dir = self.root_dir / "config"
        self.path = self.config_dir / SETTINGS_FILE
        self.backup_path = self.path.with_name(self.path.name + BACKUP_SUFFIX)
        self.secrets_dir = self.config_dir / SECRETS_DIR
        self.env = dict(os.environ if env is None else env)
        self._lock = threading.RLock()

    # -- reading ---------------------------------------------------------------------
    def exists(self) -> bool:
        return self.path.is_file()

    def read(self) -> dict[str, Any]:
        """The file as a mapping (lower-case keys; empty without the file). ConfigError if invalid."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise ConfigError(f"{SETTINGS_FILE} kann nicht gelesen werden: {exc.strerror or exc}") from None
        return parse_settings_text(text)

    def env_source(self, key: str) -> str | None:
        """The environment variable that sets ``key`` (``DOCUSHELF_KEY`` or ``…_FILE``), or None."""
        name = env_name(key)
        if self.env.get(name, "") != "":
            return name
        if self.env.get(name + "_FILE"):
            return name + "_FILE"
        return None

    def env_value(self, key: str) -> str | None:
        """The value of a key set directly in the environment (never for ``_FILE`` references)."""
        value = self.env.get(env_name(key), "")
        return value if value != "" else None

    def has_secret(self, key: str, values: Mapping[str, Any] | None = None) -> bool:
        """Whether a secret is configured (environment, file reference or plain value)."""
        if self.env_source(key):
            return True
        values = self.read() if values is None else values
        if values.get(key) not in (None, ""):
            return True
        reference = values.get(key + "_file")
        if not reference:
            return False
        try:
            return bool(self._resolve(str(reference)).read_text(encoding="utf-8").strip())
        except OSError:
            return False

    def _resolve(self, reference: str) -> Path:
        path = Path(reference).expanduser()
        return path if path.is_absolute() else self.config_dir / path

    def validation_env(self) -> dict[str, str]:
        """The environment for checking a change: the server's, pointed at this data root."""
        env = dict(self.env)
        env[PREFIX + "DATA_DIR"] = str(self.root_dir)
        # Checking must never create config/secret.key as a side effect.
        if not env.get(PREFIX + "SECRET_KEY") and not env.get(PREFIX + "SECRET_KEY_FILE"):
            env[PREFIX + "SECRET_KEY"] = "settings-check"
        return env

    def load(self, values: Mapping[str, Any] | None = None) -> Settings:
        """Settings as the server would load them now (environment + this file, or ``values``)."""
        return load_settings(self.validation_env(), file_values=self.read() if values is None else values)

    # -- writing ---------------------------------------------------------------------
    def save(
        self,
        changes: Mapping[str, Any],
        *,
        secrets: Mapping[str, str | None] | None = None,
        check: Callable[[Settings], None] | None = None,
    ) -> tuple[Settings, list[str]]:
        """Write changed values (and secrets); returns the new settings and the keys that changed.

        ``changes`` maps keys to new values; None removes a key (its default
        applies again). ``secrets`` maps secret keys to a new value, or None to
        remove the stored secret. Keys set through the environment are refused.
        ``check`` gets the resulting settings before anything is written and may
        refuse them (ConfigError). Nothing is written when the result does not
        load or is refused (ConfigError, German).
        """
        secrets = dict(secrets or {})
        with self._lock:
            current = self.read()
            new = dict(current)
            changed: list[str] = []
            for key, value in changes.items():
                key = str(key).lower()
                if key in SECRET_FILES:
                    raise ValueError(f"{key} is a secret; pass it in secrets=")
                if self.env_source(key):
                    raise ConfigError(f"{env_name(key)} ist über eine Umgebungsvariable festgelegt und lässt sich hier nicht ändern.")
                if value is None:
                    if key in new:
                        del new[key]
                        changed.append(key)
                elif new.get(key) != value:
                    new[key] = value
                    changed.append(key)

            candidate = dict(new)
            to_write: dict[str, str] = {}
            to_remove: list[str] = []
            for key, value in secrets.items():
                if key not in SECRET_FILES:
                    raise ValueError(f"{key} is not a secret setting")
                if self.env_source(key):
                    raise ConfigError(f"{env_name(key)} ist über eine Umgebungsvariable festgelegt und lässt sich hier nicht ändern.")
                if value is None:
                    if key in new or key + "_file" in new:
                        new.pop(key, None)
                        new.pop(key + "_file", None)
                        candidate.pop(key, None)
                        candidate.pop(key + "_file", None)
                        to_remove.append(key)
                        changed.append(key)
                    continue
                text = str(value).strip()
                if not text:
                    continue
                if len(text) > MAX_SECRET_LENGTH or any(ord(ch) < 32 for ch in text):
                    raise ConfigError("Das Geheimnis enthält ungültige Zeichen oder ist zu lang.")
                new.pop(key, None)
                new[key + "_file"] = f"{SECRETS_DIR}/{SECRET_FILES[key]}"
                candidate.pop(key + "_file", None)
                candidate[key] = text  # checked in memory; the file is written only once all is valid
                to_write[key] = text
                changed.append(key)

            settings = load_settings(self.validation_env(), file_values=candidate)
            if not changed:
                return settings, []
            if check is not None:
                check(settings)
            try:
                self.config_dir.mkdir(parents=True, exist_ok=True)
                for key, text in to_write.items():
                    self._write_secret(SECRET_FILES[key], text)
                previous = None
                mode = 0o640
                if self.path.is_file():
                    previous = self.path.read_text(encoding="utf-8")
                    mode = stat.S_IMODE(self.path.stat().st_mode) or 0o640
                    atomic_write_text(self.backup_path, previous, mode=mode)
                atomic_write_text(self.path, dump_settings(new), mode=mode)
            except OSError as exc:
                raise ConfigError(f"{SETTINGS_FILE} konnte nicht gespeichert werden: {exc.strerror or exc}") from None
            for key in to_remove:
                self._remove_secret(SECRET_FILES[key])
        log.info("settings.yaml changed: %s", ", ".join(sorted(set(changed))))
        return settings, sorted(set(changed))

    def _write_secret(self, name: str, value: str) -> None:
        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.secrets_dir, 0o700)
        except OSError:
            log.debug("cannot set the permissions of %s", self.secrets_dir, exc_info=True)
        atomic_write_text(self.secrets_dir / name, value + "\n", mode=0o600)

    def _remove_secret(self, name: str) -> None:
        try:
            (self.secrets_dir / name).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("cannot remove the secret file %s", name, exc_info=True)


def dump_settings(values: Mapping[str, Any]) -> str:
    """``settings.yaml`` text for ``values`` (sorted keys, a short header)."""
    body = yaml.safe_dump(
        {key: values[key] for key in sorted(values)},
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=4096,
    )
    return HEADER + ("\n" + body if values else "")


# ---------------------------------------------------------------------------
# Applying changes to running service containers


def apply_live(containers: Iterable[Any], changes: Mapping[str, Any]) -> None:
    """Replace ``Settings`` fields of running service containers (and their channel managers).

    ``changes`` holds Settings attributes (``auto_file``, ``notify_url``,
    ``owner_names``, ``trash_retention_days``, ``keep_originals_days``). Every
    component reads these from ``services.settings`` when it needs them; the
    notifier keeps its own copy of the URL.
    """
    if not changes:
        return
    for services in containers:
        if services is None:
            continue
        try:
            current = services.settings
            if hasattr(current, "with_overrides"):
                services.settings = current.with_overrides(**changes)
            channels = getattr(services, "channels", None)
            base = getattr(channels, "base_settings", None)
            if base is not None and hasattr(base, "with_overrides"):
                channels.base_settings = base.with_overrides(**changes)
            notifier = getattr(services, "notifier", None)
            if "notify_url" in changes and notifier is not None and hasattr(notifier, "url"):
                notifier.url = changes["notify_url"]
        except Exception:
            log.exception("cannot apply changed settings to a running household")


__all__ = [
    "LIVE_KEYS",
    "SECRET_FILES",
    "SettingsFile",
    "apply_live",
    "dump_settings",
    "env_name",
]
