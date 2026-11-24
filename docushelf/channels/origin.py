"""Who a document came from: the owner and endpoint of an inbound channel.

Channels that belong to one person (a mailbox, a scan-to-mail address, a
scanner subfolder) write that person into the item context, so later steps
can route by it and the inbox can show "von Erika · Scanner":

* ``owner`` – the household member as configured (user name or e-mail),
* ``owner_label`` – a short display name derived from it,
* ``endpoint`` – a German label of the endpoint ("Scanner", "Scan per Mail",
  "Postfach „familie“").
"""

from __future__ import annotations

import re
from typing import Any, Mapping

OWNER = "owner"
OWNER_LABEL = "owner_label"
ENDPOINT = "endpoint"

_SEPARATORS = re.compile(r"[._\-+]+")


def owner_label(owner: str | None) -> str:
    """A short display name: ``erika.mustermann@example.org`` → ``Erika Mustermann``.

    Names that already contain capitals or spaces are kept as they are.
    """
    text = str(owner or "").strip()
    if not text:
        return ""
    local = text.split("@", 1)[0] if "@" in text else text
    if any(ch.isupper() for ch in local) or " " in local:
        return local
    parts = [part for part in _SEPARATORS.split(local) if part]
    if not parts:
        return local
    return " ".join(part[:1].upper() + part[1:] for part in parts)


def mark(context: dict[str, Any], *, owner: str | None = None, endpoint: str | None = None) -> dict[str, Any]:
    """Write owner and endpoint into ``context`` (existing values are kept) and return it."""
    if owner and not context.get(OWNER):
        context[OWNER] = owner
        context[OWNER_LABEL] = owner_label(owner)
    if endpoint and not context.get(ENDPOINT):
        context[ENDPOINT] = endpoint
    return context


def origin_label(context: Mapping[str, Any] | None) -> str | None:
    """``von Erika · Scanner`` (or only one of both parts); None when nothing is known."""
    if not isinstance(context, Mapping):
        return None
    owner = context.get(OWNER)
    name = str(context.get(OWNER_LABEL) or owner_label(owner if isinstance(owner, str) else None)).strip()
    endpoint = context.get(ENDPOINT)
    endpoint = str(endpoint).strip() if isinstance(endpoint, str) else ""
    parts = []
    if name:
        parts.append(f"von {name}")
    if endpoint:
        parts.append(endpoint)
    return " · ".join(parts) or None


__all__ = ["ENDPOINT", "OWNER", "OWNER_LABEL", "mark", "origin_label", "owner_label"]
