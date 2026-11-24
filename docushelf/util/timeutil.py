"""Time and date helpers using one configurable time zone."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

_tz = ZoneInfo("Europe/Berlin")


def set_timezone(name: str) -> None:
    global _tz
    _tz = ZoneInfo(name)


def timezone() -> ZoneInfo:
    return _tz


def now() -> datetime:
    return datetime.now(_tz)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def today() -> date:
    return now().date()


def parse_date(value: object) -> date | None:
    """Accept a date, a datetime or an ISO string ("2026-11-23", "2026-11-23T10:00:00+01:00")."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def format_date(value: date | None) -> str | None:
    return value.isoformat() if value else None


def format_date_de(value: date | str | None) -> str:
    """German display format: 23.11.2026."""
    parsed = parse_date(value)
    return parsed.strftime("%d.%m.%Y") if parsed else ""


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_tz)
    return parsed
