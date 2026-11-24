"""Identifier helpers.

Inbox items and trash entries use readable, time-sortable ids
(``20261123-125100-a1b2c3``). Archived documents use UUIDs stored in XMP;
files without such an id get a provisional id derived from their content hash.
"""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import datetime

ITEM_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")
DOC_ID_RE = re.compile(r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|h-[0-9a-f]{32}(?:-\d+)?)$")
PROVISIONAL_PREFIX = "h-"


def new_item_id(now: datetime | None = None) -> str:
    """Return a new id for an inbox item or trash entry."""
    now = now or datetime.now()
    return f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def is_item_id(value: str) -> bool:
    return bool(ITEM_ID_RE.match(value or ""))


def new_doc_id() -> str:
    """Return a new permanent document id (stored in the file's XMP)."""
    return str(uuid.uuid4())


def provisional_id(content_hash: str) -> str:
    """Id for files that carry no document id of their own."""
    return PROVISIONAL_PREFIX + content_hash[:32]


def is_provisional(doc_id: str) -> bool:
    return doc_id.startswith(PROVISIONAL_PREFIX)


def is_doc_id(value: str) -> bool:
    return bool(DOC_ID_RE.match(value or ""))


def new_token() -> str:
    """Return a new random API token (shown to the user once)."""
    return "dsk_" + secrets.token_urlsafe(32)
