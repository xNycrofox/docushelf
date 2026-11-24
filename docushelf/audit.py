"""Append-only audit log (JSON lines, one file per month).

The log lives in ``protokoll/`` on the data volume, next to the archive and
the inbox, and is never part of the (disposable) index. Besides the general
history it records the automatic decisions (``auto_file``) and their
confirmation by a person (``auto_review``), so they survive an index rebuild.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta
from datetime import timezone as _dt_timezone
from pathlib import Path
from typing import Any, Iterable

from .util.timeutil import now, now_iso, parse_iso_datetime, timezone

log = logging.getLogger(__name__)

_PREFIX = "audit-"
_SUFFIX = ".jsonl"
# Entries are stamped before they are written and the file is chosen at write
# time, possibly in another time zone than today's: when looking for entries
# since a moment, the month file before that moment's month is read as well.
_MONTH_SLACK = timedelta(days=1)


class AuditLog:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self._lock = threading.Lock()

    def _file(self) -> Path:
        return self.directory / f"{_PREFIX}{now():%Y-%m}{_SUFFIX}"

    def write(self, user: str | None, action: str, **details: Any) -> None:
        entry = {"at": now_iso(), "user": user or "system", "action": action}
        entry.update({k: v for k, v in details.items() if v is not None})
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        try:
            with self._lock:
                self.directory.mkdir(parents=True, exist_ok=True)
                with open(self._file(), "a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
        except OSError:
            log.exception("cannot write audit log")

    def _files(self) -> list[Path]:
        """The monthly files, newest first."""
        return sorted(self.directory.glob(f"{_PREFIX}*{_SUFFIX}"), reverse=True)

    @staticmethod
    def _read_lines(path: Path) -> list[str]:
        try:
            return path.read_text(encoding="utf-8").splitlines()
        except OSError:
            log.warning("cannot read audit log %s", path, exc_info=True)
            return []

    def tail(self, limit: int = 200) -> list[dict[str, Any]]:
        """The newest entries first."""
        entries: list[dict[str, Any]] = []
        for path in self._files():
            for line in reversed(self._read_lines(path)):
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
                if len(entries) >= limit:
                    return entries
        return entries

    def entries(
        self,
        actions: Iterable[str] | None = None,
        since: datetime | None = None,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Entries with one of ``actions`` written at or after ``since``, newest first.

        ``actions`` empty or None means every action, ``since`` None the whole
        log. A naive ``since`` is taken as local time of the configured time
        zone. Only the monthly files that can hold such entries are read;
        damaged lines are skipped.
        """
        wanted = {str(action) for action in actions} if actions else None
        cutoff: datetime | None = None
        first_file: str | None = None
        if since is not None:
            cutoff = since if since.tzinfo is not None else since.replace(tzinfo=timezone())
            first_month = (cutoff.astimezone(timezone()) - _MONTH_SLACK).strftime("%Y-%m")
            first_file = f"{_PREFIX}{first_month}{_SUFFIX}"

        found: list[tuple[datetime | None, dict[str, Any]]] = []
        for path in self._files():
            if first_file is not None and path.name < first_file:
                break  # older months only from here on
            for line in reversed(self._read_lines(path)):
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if wanted is not None and entry.get("action") not in wanted:
                    continue
                at = parse_iso_datetime(entry.get("at")) if isinstance(entry.get("at"), str) else None
                if cutoff is not None and (at is None or at < cutoff):
                    continue
                found.append((at, entry))

        # Lines are appended in time order, but two threads may stamp and write
        # in opposite order; sorting keeps "newest first" exact. The sort is
        # stable, so entries with the same time keep the order of the file.
        oldest = datetime.min.replace(tzinfo=_dt_timezone.utc)
        found.sort(key=lambda pair: pair[0] or oldest, reverse=True)
        result = [entry for _at, entry in found]
        return result[:limit] if limit is not None else result
