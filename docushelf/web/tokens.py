"""API tokens for scripts and shortcuts.

Tokens are kept in ``config/tokens.yaml`` as a readable list. Only the SHA-256
hash of a token is stored; the token itself is shown once, when it is created::

    - id: 3f2a9c1b
      name: iPhone Kurzbefehl
      user: anna
      hash: 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08
      prefix: dsk_Zt3kPq8w
      created_at: '2026-11-23T14:05:00+01:00'

Deleting an entry from the file revokes the token. Tokens are random 256-bit
values, so a plain SHA-256 is enough to protect them at rest; a slow password
hash would only make every API request slower.

Tokens created after a login in the browser also keep a snapshot of their
owner (login method, e-mail address and groups)::

      method: oidc
      email: anna@example.net
      groups: [familie]

Such a token is only accepted while that owner would still be allowed to log
in under the current configuration (see ``web/security.py``). Entries without
the snapshot (created on the command line, or before the snapshot existed)
are not tied to an account.

If the file cannot be parsed, no token is accepted until it is fixed (and
nothing is written over the broken file); :attr:`TokenStore.error` holds the
German message for the UI.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import unicodedata
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from ..errors import ConfigError, NotFound
from ..util import fsops
from ..util.ids import new_token
from ..util.timeutil import now_iso

log = logging.getLogger(__name__)

# Shown in lists so a token can be recognised without revealing it ("dsk_" + 8 characters).
PREFIX_LENGTH = 12
MAX_NAME_LENGTH = 100
MAX_TOKEN_LENGTH = 512

_HASH_RE = re.compile(r"[0-9a-f]{64}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SPACES_RE = re.compile(r"\s+")
_MISSING = ("missing",)
_FIELDS = ("id", "name", "user", "hash", "prefix", "created_at")
# The owner snapshot; absent (None / []) for tokens that are not tied to an account.
_OWNER_FIELDS = ("method", "email", "groups")
# Login methods whose tokens are checked against the current access rules.
OWNER_METHODS = ("oidc", "local")
MAX_GROUP_LENGTH = 200

HEADER = (
    "# API-Tokens für DocuShelf. Gespeichert wird nur der SHA-256-Hash eines Tokens.\n"
    "# Einen Eintrag zu löschen widerruft das Token.\n"
)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _clean_label(value: object, label: str) -> str:
    """Single-line text for names; raises ValueError with a German message."""
    text = unicodedata.normalize("NFC", str(value or ""))
    text = _SPACES_RE.sub(" ", _CONTROL_RE.sub(" ", text)).strip()
    if not text:
        raise ValueError(f"{label} fehlt.")
    if len(text) > MAX_NAME_LENGTH:
        raise ValueError(f"{label} darf höchstens {MAX_NAME_LENGTH} Zeichen lang sein.")
    return text


def _public(entry: dict[str, Any]) -> dict[str, Any]:
    """An entry without its hash (safe to show and to return from the API)."""
    public = {key: entry[key] for key in _FIELDS if key != "hash"}
    public["method"] = entry.get("method")
    public["email"] = entry.get("email")
    public["groups"] = list(entry.get("groups") or [])
    return public


def _clean_groups(values: Any) -> list[str]:
    groups: list[str] = []
    for value in values or ():
        if isinstance(value, str):
            text = _SPACES_RE.sub(" ", _CONTROL_RE.sub(" ", unicodedata.normalize("NFC", value))).strip()
            if text and len(text) <= MAX_GROUP_LENGTH and text not in groups:
                groups.append(text)
    return groups


def owner_snapshot(owner: Any) -> dict[str, Any]:
    """The fields that tie a token to its owner's access, taken from a logged-in user.

    ``owner`` is a ``web.security.User`` (or a mapping with the same keys).
    Returns ``{}`` for owners that cannot be checked again later, e.g. other
    API tokens.
    """
    if owner is None:
        return {}
    if isinstance(owner, Mapping):
        method, email, groups = owner.get("method"), owner.get("email"), owner.get("groups")
    else:
        method, email, groups = (getattr(owner, key, None) for key in _OWNER_FIELDS)
    if method not in OWNER_METHODS:
        return {}
    email = email.strip() if isinstance(email, str) else ""
    return {"method": method, "email": email or None, "groups": _clean_groups(groups)}


def _parse_owner(raw: dict[str, Any], position: int) -> dict[str, Any]:
    """The owner snapshot of a stored entry; fails closed on anything unexpected."""
    method = raw.get("method")
    if method in (None, ""):
        if raw.get("groups") or raw.get("email"):
            raise ConfigError(
                f"tokens.yaml: Eintrag {position} hat Gruppen oder eine E-Mail-Adresse, aber keine Anmeldeart (method)."
            )
        return {"method": None, "email": None, "groups": []}
    if method not in OWNER_METHODS:
        raise ConfigError(
            f"tokens.yaml: Eintrag {position} hat die unbekannte Anmeldeart „{method}“ "
            f"(erlaubt: {', '.join(OWNER_METHODS)})."
        )
    groups = raw.get("groups")
    if groups is None:
        groups = []
    if not isinstance(groups, list) or not all(isinstance(group, str) for group in groups):
        raise ConfigError(f"tokens.yaml: Eintrag {position}: „groups“ muss eine Liste von Gruppennamen sein.")
    email = raw.get("email")
    if email is not None and not isinstance(email, str):
        raise ConfigError(f"tokens.yaml: Eintrag {position}: „email“ muss ein Text sein.")
    return {"method": method, "email": (email or "").strip() or None, "groups": _clean_groups(groups)}


def parse_tokens(text: str) -> list[dict[str, Any]]:
    """Validate the content of tokens.yaml. Raises ConfigError with a German message."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"tokens.yaml ist ungültig: {exc}") from exc
    if data is None:
        return []
    if isinstance(data, dict) and "tokens" in data:
        data = data["tokens"] or []
    if not isinstance(data, list):
        raise ConfigError("tokens.yaml muss eine Liste von Tokens enthalten.")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, raw in enumerate(data, start=1):
        if not isinstance(raw, dict):
            raise ConfigError(f"tokens.yaml: Eintrag {position} ist keine Zuordnung.")
        token_id = str(raw.get("id") or "").strip()
        digest = str(raw.get("hash") or "").strip().lower()
        user = str(raw.get("user") or "").strip()
        if not token_id:
            raise ConfigError(f"tokens.yaml: Eintrag {position} hat keine id.")
        if token_id in seen:
            raise ConfigError(f"tokens.yaml: Die id „{token_id}“ kommt mehrfach vor.")
        if not _HASH_RE.fullmatch(digest):
            raise ConfigError(f"tokens.yaml: Eintrag {position} braucht einen SHA-256-Hash (64 Hex-Zeichen).")
        if not user:
            raise ConfigError(f"tokens.yaml: Eintrag {position} hat keinen Benutzer (user).")
        created = raw.get("created_at")
        if isinstance(created, (datetime, date)):
            created = created.isoformat()
        seen.add(token_id)
        entries.append(
            {
                "id": token_id,
                "name": str(raw.get("name") or "").strip(),
                "user": user,
                "hash": digest,
                "prefix": str(raw.get("prefix") or "").strip(),
                "created_at": str(created or ""),
                **_parse_owner(raw, position),
            }
        )
    return entries


def dump_tokens(entries: list[dict[str, Any]]) -> str:
    ordered = []
    for entry in entries:
        data = {key: entry.get(key, "") for key in _FIELDS}
        if entry.get("method"):
            # Entries without an owner snapshot keep their original shape.
            data["method"] = entry["method"]
            if entry.get("email"):
                data["email"] = entry["email"]
            data["groups"] = list(entry.get("groups") or [])
        ordered.append(data)
    body = yaml.safe_dump(ordered, allow_unicode=True, sort_keys=False, default_flow_style=False) if ordered else "[]\n"
    return HEADER + body


class TokenStore:
    """Reads and writes ``tokens.yaml``; the parsed list is cached by file signature."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._entries: list[dict[str, Any]] = []
        self._signature: tuple[Any, ...] | None = None
        self.error: str | None = None

    # -- reading ---------------------------------------------------------------
    def _stat(self) -> tuple[Any, ...]:
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return _MISSING
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def _refresh(self) -> list[dict[str, Any]]:
        """The current entries (internal list; callers must not modify it)."""
        with self._lock:
            signature = self._stat()
            if signature == self._signature:
                return self._entries
            if signature == _MISSING:
                self._entries, self.error, self._signature = [], None, signature
                return self._entries
            try:
                text = self.path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                # Not cached: the next call tries again.
                self._entries, self._signature = [], None
                self.error = f"tokens.yaml kann nicht gelesen werden: {exc}"
                log.warning("cannot read %s: %s", self.path, exc)
                return self._entries
            try:
                entries = parse_tokens(text)
            except ConfigError as exc:
                if self.error != str(exc):
                    log.warning("invalid token file %s; no API token is accepted until it is fixed: %s", self.path, exc)
                self._entries, self.error, self._signature = [], str(exc), signature
                return self._entries
            self._entries, self.error, self._signature = entries, None, signature
            return self._entries

    def list(self) -> list[dict[str, Any]]:
        """All tokens without their hashes, oldest first."""
        return [_public(entry) for entry in self._refresh()]

    def get(self, token_id: str) -> dict[str, Any]:
        for entry in self._refresh():
            if entry["id"] == token_id:
                return _public(entry)
        raise NotFound("API-Token nicht gefunden.")

    def verify(self, token: str) -> dict[str, Any] | None:
        """The entry (without hash) the token belongs to, or None.

        Every stored hash is compared in constant time, so the response time
        does not reveal how much of a guessed token was right or which entry
        matched.
        """
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH:
            return None
        entries = self._refresh()
        digest = hash_token(token).encode("ascii")
        match: dict[str, Any] | None = None
        for entry in entries:
            if hmac.compare_digest(digest, entry["hash"].encode("ascii")) and match is None:
                match = entry
        return _public(match) if match is not None else None

    # -- writing ---------------------------------------------------------------
    def _editable(self) -> list[dict[str, Any]]:
        entries = self._refresh()
        if self.error:
            raise ConfigError(f"Die Token-Datei ist fehlerhaft und muss zuerst korrigiert werden: {self.error}")
        return copy.deepcopy(entries)

    def _write(self, entries: list[dict[str, Any]]) -> None:
        fsops.atomic_write_text(self.path, dump_tokens(entries), mode=0o600)
        self._entries, self.error, self._signature = entries, None, self._stat()

    def create(self, name: str, user: str, *, owner: Any = None) -> tuple[str, dict[str, Any]]:
        """Create a token for ``user``. Returns the token (shown once) and its entry.

        With ``owner`` (the logged-in user creating the token) the entry keeps
        a snapshot of the owner's login method, e-mail address and groups, so
        the token stops working once that owner is no longer allowed in.
        Without it (command line) the token is not tied to an account.
        """
        name = _clean_label(name, "Der Name des Tokens")
        user = _clean_label(user, "Der Benutzer des Tokens")
        snapshot = owner_snapshot(owner)
        with self._lock:
            entries = self._editable()
            taken = {entry["id"] for entry in entries}
            token_id = secrets.token_hex(4)
            while token_id in taken:
                token_id = secrets.token_hex(4)
            token = new_token()
            entry = {
                "id": token_id,
                "name": name,
                "user": user,
                "hash": hash_token(token),
                "prefix": token[:PREFIX_LENGTH],
                "created_at": now_iso(),
                "method": snapshot.get("method"),
                "email": snapshot.get("email"),
                "groups": list(snapshot.get("groups") or []),
            }
            entries.append(entry)
            self._write(entries)
        log.info("API token %s (%s) created for %s", token_id, name, user)
        return token, _public(entry)

    def revoke(self, token_id: str) -> dict[str, Any]:
        """Delete a token; returns its entry. Raises NotFound for unknown ids."""
        with self._lock:
            entries = self._editable()
            for position, entry in enumerate(entries):
                if entry["id"] == token_id:
                    del entries[position]
                    self._write(entries)
                    log.info("API token %s (%s) revoked", token_id, entry["name"])
                    return _public(entry)
        raise NotFound("API-Token nicht gefunden.")
