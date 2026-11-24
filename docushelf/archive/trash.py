"""The trash: deleted documents and inbox items, kept for a while.

Each entry is a directory ``papierkorb/<entry-id>/`` with an ``entry.json``
describing what was deleted and a ``files/`` directory holding it::

    papierkorb/20261123-140501-a1b2c3/
    ├── entry.json   {"id", "kind": "document"|"inbox", "deleted_at", "deleted_by",
    │                 "original_path" | "item_id", "title", "doc_id", "meta": {...}}
    └── files/
        └── 2026-01-05 Stromrechnung.pdf      (a document)
            or <item-id>/…                    (a whole inbox item directory)

``entry.json`` is written before anything is moved, so an interrupted delete
leaves at most an entry without files (cleaned up later) and the original
untouched. Restoring never overwrites: a document whose name is taken again
comes back with a collision suffix.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import Conflict, NotFound, PathError
from ..inbox.items import InboxItem
from ..models import DocumentRecord
from ..util import fsops
from ..util.ids import is_item_id, new_item_id
from ..util.names import OWN_TEMP_PREFIX, check_relative
from ..util.timeutil import now, now_iso, parse_iso_datetime, timezone
from .ops import (
    archive_dir,
    forget_path,
    fs_error,
    index_archive_file,
    move_exact,
    parent_folder,
    publish_move,
    resolve_folder_case,
    retry_transient,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..services import Services

log = logging.getLogger(__name__)

ENTRY_FILE = "entry.json"
FILES_DIR = "files"


@dataclass
class TrashEntry:
    id: str
    kind: str  # "document" | "inbox"
    deleted_at: str  # ISO timestamp
    deleted_by: str | None
    title: str
    original_path: str | None  # documents: relative archive path at the time of deletion
    item_id: str | None  # inbox items
    doc_id: str | None
    files: list[Path]  # documents: the file; inbox items: the original files
    meta: dict[str, Any]

    @property
    def folder(self) -> str | None:
        return parent_folder(self.original_path) if self.original_path is not None else None

    @property
    def deleted(self) -> datetime | None:
        return parse_iso_datetime(self.deleted_at)


def _tombstone(path: Path) -> Path:
    return path.with_name(f"{OWN_TEMP_PREFIX}purge-{path.name}-{uuid.uuid4().hex[:8]}")


def _remove_path(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("cannot remove %s; it is retried at the next clean-up", path, exc_info=True)


def _mtime(path: Path) -> datetime:
    try:
        return datetime.fromtimestamp(os.stat(path).st_mtime, tz=timezone())
    except OSError:
        return now()


class Trash:
    def __init__(self, services: "Services"):
        self.services = services

    @property
    def root(self) -> Path:
        return self.services.layout.trash

    # -- putting things in -----------------------------------------------------------------
    def put_document(self, abs_path: Path, record: DocumentRecord, *, user: str | None = None) -> str:
        """Move an archived file into the trash and drop it from the index; returns the entry id."""
        services = self.services
        path = Path(abs_path)
        with services.archive_lock:
            if not path.is_file() or path.is_symlink():
                raise NotFound(f"„{record.path}“ existiert nicht mehr im Archiv.")
            entry_id, entry_dir = self._new_entry_dir()
            data = {
                "id": entry_id,
                "kind": "document",
                "deleted_at": now_iso(),
                "deleted_by": user,
                "original_path": record.path,
                "title": record.title,
                "doc_id": record.id,
                "meta": record.to_dict(),
            }
            try:
                (entry_dir / FILES_DIR).mkdir()
                fsops.atomic_write_json(entry_dir / ENTRY_FILE, data)
                move_exact(path, entry_dir / FILES_DIR / path.name)
            except BaseException as exc:
                if os.path.lexists(path):
                    # Nothing was moved: drop the half-made entry.
                    shutil.rmtree(entry_dir, ignore_errors=True)
                if isinstance(exc, OSError):
                    raise fs_error(exc, "Löschen fehlgeschlagen") from exc
                raise
            forget_path(services, record.path)
            services.audit.write(
                user, "delete", path=record.path, doc_id=record.id, title=record.title, trash=entry_id
            )
            return entry_id

    def put_inbox_item(self, item: InboxItem, *, user: str | None = None) -> str:
        """Move a whole inbox item directory into the trash; returns the entry id."""
        services = self.services
        with services.archive_lock:
            if not item.dir.is_dir():
                raise NotFound("Eingangsdokument nicht gefunden.")
            entry_id, entry_dir = self._new_entry_dir()
            top = item.top_suggestion or {}
            data = {
                "id": entry_id,
                "kind": "inbox",
                "deleted_at": now_iso(),
                "deleted_by": user,
                "item_id": item.id,
                "original_path": None,
                "title": self._inbox_title(item),
                "doc_id": None,
                "meta": {
                    "state": item.state,
                    "stage": item.stage,
                    "source": item.source,
                    "received_at": item.received_at,
                    "origin_hash": item.origin_hash,
                    "original_files": list(item.original_files),
                    "suggested_folder": top.get("folder"),
                },
            }
            source_dir = item.dir
            target = entry_dir / FILES_DIR / item.id
            try:
                (entry_dir / FILES_DIR).mkdir()
                fsops.atomic_write_json(entry_dir / ENTRY_FILE, data)
                retry_transient(fsops.move_dir, source_dir, target)
            except BaseException as exc:
                if source_dir.is_dir():
                    shutil.rmtree(entry_dir, ignore_errors=True)
                if isinstance(exc, OSError):
                    raise fs_error(exc, "Löschen fehlgeschlagen") from exc
                raise
            item.dir = target
            try:
                item.add_history("deleted", user=user)
                services.inbox.save(item)
                services.inbox.log(item, "In den Papierkorb verschoben")
            except Exception:
                log.warning("cannot note the deletion in inbox item %s", item.id, exc_info=True)
            services.audit.write(
                user, "delete_inbox", item=item.id, title=data["title"], state=data["meta"]["state"], trash=entry_id
            )
            return entry_id

    @staticmethod
    def _inbox_title(item: InboxItem) -> str:
        top = item.top_suggestion or {}
        return str(item.context.get("title") or top.get("title") or item.display_name)

    def _new_entry_dir(self) -> tuple[str, Path]:
        self.root.mkdir(parents=True, exist_ok=True)
        for _ in range(100):
            entry_id = new_item_id()
            entry_dir = self.root / entry_id
            try:
                entry_dir.mkdir()
            except FileExistsError:
                continue
            return entry_id, entry_dir
        raise Conflict("Kein freier Eintrag im Papierkorb.")

    # -- reading ----------------------------------------------------------------------------
    def list(self) -> list[TrashEntry]:
        """All complete entries, newest first."""
        try:
            names = os.listdir(self.root)
        except FileNotFoundError:
            return []
        entries = []
        for name in names:
            if not is_item_id(name):
                continue
            entry = self._load(self.root / name)
            if entry is not None:
                entries.append(entry)
        entries.sort(key=lambda e: (e.deleted.timestamp() if e.deleted else 0.0, e.id), reverse=True)
        return entries

    def get(self, entry_id: str) -> TrashEntry:
        if not is_item_id(entry_id or ""):
            raise NotFound("Eintrag im Papierkorb nicht gefunden.")
        entry = self._load(self.root / entry_id)
        if entry is None:
            raise NotFound("Eintrag im Papierkorb nicht gefunden.")
        return entry

    def expires_at(self, entry: TrashEntry) -> datetime | None:
        """When ``purge_expired`` will remove the entry (None: kept until purged by hand)."""
        days = self.services.settings.trash_retention_days
        deleted = entry.deleted
        if days <= 0 or deleted is None:
            return None
        return deleted + timedelta(days=days)

    @staticmethod
    def _read_entry(entry_dir: Path) -> dict[str, Any] | None:
        try:
            data = fsops.read_json(entry_dir / ENTRY_FILE)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            log.warning("cannot read %s", entry_dir / ENTRY_FILE, exc_info=True)
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _entry_files(entry_dir: Path, data: dict[str, Any]) -> list[Path]:
        """The payload of an entry; empty for an entry that is not (or no longer) complete."""
        files_dir = entry_dir / FILES_DIR
        if data.get("kind") == "inbox":
            item_id = str(data.get("item_id") or "")
            item_dir = files_dir / item_id
            if not is_item_id(item_id) or not item_dir.is_dir():
                return []
            original = item_dir / "original"
            try:
                originals = sorted(p for p in original.iterdir() if p.is_file())
            except OSError:
                originals = []
            return originals or [item_dir]
        try:
            return sorted(
                p
                for p in files_dir.iterdir()
                if not p.name.startswith(OWN_TEMP_PREFIX) and p.is_file() and not p.is_symlink()
            )
        except OSError:
            return []

    def _load(self, entry_dir: Path) -> TrashEntry | None:
        data = self._read_entry(entry_dir)
        if data is None or data.get("kind") not in ("document", "inbox"):
            return None
        files = self._entry_files(entry_dir, data)
        if not files:
            return None
        meta = data.get("meta")
        return TrashEntry(
            id=entry_dir.name,
            kind=data["kind"],
            deleted_at=str(data.get("deleted_at") or ""),
            deleted_by=data.get("deleted_by"),
            title=str(data.get("title") or (files[0].name if files else entry_dir.name)),
            original_path=data.get("original_path"),
            item_id=data.get("item_id"),
            doc_id=data.get("doc_id"),
            files=files,
            meta=meta if isinstance(meta, dict) else {},
        )

    # -- restoring ----------------------------------------------------------------------------
    def restore(self, entry_id: str, *, user: str | None = None) -> DocumentRecord | InboxItem:
        """Put an entry back: documents into their original folder, inbox items into the inbox."""
        with self.services.archive_lock:
            entry = self.get(entry_id)
            try:
                if entry.kind == "document":
                    return self._restore_document(entry, user)
                return self._restore_inbox(entry, user)
            except OSError as exc:
                raise fs_error(exc, "Wiederherstellen fehlgeschlagen") from exc

    def _restore_document(self, entry: TrashEntry, user: str | None) -> DocumentRecord:
        services = self.services
        layout = services.layout
        try:
            rel = check_relative(entry.original_path or "")
        except PathError:
            raise PathError("Der ursprüngliche Pfad des Dokuments ist ungültig.") from None
        folder, _, filename = rel.rpartition("/")
        source = entry.files[0]
        if not filename:
            filename = source.name
        folder = resolve_folder_case(layout.archive, folder)
        target_dir = archive_dir(layout, folder, create=True)
        final = publish_move(source, target_dir, filename)
        record = index_archive_file(services, final)
        self._discard(self.root / entry.id)
        services.audit.write(
            user,
            "restore",
            kind="document",
            path=record.path,
            original_path=entry.original_path if entry.original_path != record.path else None,
            doc_id=record.id,
            trash=entry.id,
        )
        return record

    def _restore_inbox(self, entry: TrashEntry, user: str | None) -> InboxItem:
        services = self.services
        inbox = services.inbox
        item_id = entry.item_id or ""
        source = self.root / entry.id / FILES_DIR / item_id
        if not is_item_id(item_id) or not source.is_dir():
            raise NotFound("Eintrag im Papierkorb nicht gefunden.")
        if inbox.find(item_id) is not None or any(
            (services.layout.state_dir(state) / item_id).exists() for state in ("arbeit", "bereit", "fehler", "erledigt")
        ):
            raise Conflict("Ein Eingangsdokument mit dieser Kennung existiert bereits.")
        state = self._restore_state(source, entry.meta.get("state"))
        target = services.layout.state_dir(state) / item_id
        retry_transient(fsops.move_dir, source, target)
        try:
            item = inbox.load(item_id, states=(state,))
        except NotFound:
            # The item directory had no readable item.json; let the inbox rebuild one.
            inbox.recover_orphans()
            item = inbox.load(item_id)
        item.add_history("restored", user=user)
        inbox.save(item)
        inbox.log(item, "Aus dem Papierkorb wiederhergestellt")
        if item.state == "arbeit":
            try:
                services.index.enqueue("process", item.id)
                worker = getattr(services, "worker", None)
                if worker is not None:
                    worker.kick()
            except Exception:
                log.warning("cannot queue restored inbox item %s", item.id, exc_info=True)
        self._discard(self.root / entry.id)
        services.audit.write(user, "restore", kind="inbox", item=item.id, state=item.state, trash=entry.id)
        return item

    @staticmethod
    def _restore_state(item_dir: Path, deleted_from: object) -> str:
        """Where a restored inbox item goes: ready items to ``bereit``, failed ones back to
        ``fehler``, anything else is processed again."""
        try:
            data = fsops.read_json(item_dir / "item.json")
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if deleted_from == "fehler" or data.get("error"):
            return "fehler"
        if data.get("stage") == "ready" and (item_dir / "document.pdf").is_file():
            return "bereit"
        return "arbeit"

    # -- purging --------------------------------------------------------------------------------
    def purge(self, entry_id: str, *, user: str | None = None) -> None:
        """Delete an entry for good."""
        with self.services.archive_lock:
            if not is_item_id(entry_id or ""):
                raise NotFound("Eintrag im Papierkorb nicht gefunden.")
            entry_dir = self.root / entry_id
            if not entry_dir.is_dir():
                raise NotFound("Eintrag im Papierkorb nicht gefunden.")
            data = self._read_entry(entry_dir) or {}
            self._discard(entry_dir)
        self.services.audit.write(
            user,
            "purge",
            trash=entry_id,
            kind=data.get("kind"),
            path=data.get("original_path"),
            item=data.get("item_id"),
            title=data.get("title"),
        )

    def purge_expired(self) -> int:
        """Delete entries older than ``trash_retention_days`` (0 or less keeps them forever).

        Also removes leftovers of interrupted operations. Returns the number of
        expired entries that were deleted.
        """
        days = self.services.settings.trash_retention_days
        cutoff = now() - timedelta(days=days) if days > 0 else None
        doomed: list[Path] = []
        purged: list[str] = []
        with self.services.archive_lock:
            try:
                names = sorted(os.listdir(self.root))
            except FileNotFoundError:
                return 0
            for name in names:
                path = self.root / name
                if name.startswith(OWN_TEMP_PREFIX):
                    doomed.append(path)  # leftover of an interrupted purge
                    continue
                if not is_item_id(name) or not path.is_dir() or path.is_symlink():
                    continue
                data = self._read_entry(path)
                if data is None:
                    if self._is_empty_shell(path):
                        doomed.append(self._bury(path))
                    else:
                        log.warning("trash entry %s has no readable entry.json; left alone", path)
                    continue
                if not self._entry_files(path, data):
                    # Interrupted delete or restore: the file is back in (or never left) its place.
                    if self._is_empty_shell(path):
                        doomed.append(self._bury(path))
                    else:
                        log.warning("trash entry %s is incomplete but not empty; left alone", path)
                    continue
                if cutoff is None:
                    continue
                deleted = parse_iso_datetime(str(data.get("deleted_at") or "")) or _mtime(path)
                if deleted < cutoff:
                    doomed.append(self._bury(path))
                    purged.append(name)
        for path in doomed:
            _remove_path(path)
        if purged:
            self.services.audit.write(None, "purge_expired", count=len(purged), entries=purged)
        return len(purged)

    def purge_done_items(self) -> int:
        """Delete filed inbox items (``eingang/erledigt``) older than ``keep_originals_days``.

        The age counts from the filing (journal or history in item.json, else the
        directory's modification time). 0 or less keeps them forever.

        Never deleted: items whose filing journal is not marked done (an
        unfinished filing is settled by ``recover()``; an unknown state is kept
        rather than guessed at) and items whose item.json is the only copy of
        data the archived file could not take (Paperless notes of a signed PDF,
        journal key ``unstored``).
        """
        days = self.services.settings.keep_originals_days
        if days <= 0:
            return 0
        cutoff = now() - timedelta(days=days)
        base = self.services.layout.done
        doomed: list[Path] = []
        purged: list[str] = []
        with self.services.archive_lock:
            try:
                names = sorted(os.listdir(base))
            except FileNotFoundError:
                return 0
            for name in names:
                path = base / name
                if name.startswith(f"{OWN_TEMP_PREFIX}purge-"):
                    doomed.append(path)
                    continue
                if not is_item_id(name) or not path.is_dir() or path.is_symlink():
                    continue
                try:
                    data = fsops.read_json(path / "item.json")
                except FileNotFoundError:
                    continue
                except (OSError, ValueError):
                    log.warning("cannot read %s", path / "item.json", exc_info=True)
                    continue
                if not isinstance(data, dict):
                    continue
                filing = data.get("filing")
                if filing and not (isinstance(filing, dict) and filing.get("state") == "done"):
                    continue  # filing not (known to be) complete: never purge the originals
                if isinstance(filing, dict) and filing.get("unstored"):
                    continue  # the archived file could not take everything; this is the only copy
                filed = self._filed_at(data) or _mtime(path)
                if filed < cutoff:
                    doomed.append(self._bury(path))
                    purged.append(name)
        for path in doomed:
            _remove_path(path)
        if purged:
            self.services.audit.write(None, "purge_done", count=len(purged), items=purged)
        return len(purged)

    @staticmethod
    def _filed_at(data: dict[str, Any]) -> datetime | None:
        filing = data.get("filing")
        if isinstance(filing, dict):
            filed = parse_iso_datetime(filing.get("finished_at") or filing.get("published_at"))
            if filed is not None:
                return filed
        for event in reversed(data.get("history") or []):
            if isinstance(event, dict) and event.get("event") == "filed":
                filed = parse_iso_datetime(event.get("at"))
                if filed is not None:
                    return filed
        return None

    @staticmethod
    def _is_empty_shell(entry_dir: Path) -> bool:
        """True for an entry that holds nothing but (maybe) ``entry.json`` and an empty ``files/``.

        Our own temporary files do not count; anything else means the entry is
        left alone, so unknown content is never deleted.
        """
        try:
            for name in os.listdir(entry_dir):
                if name not in (ENTRY_FILE, FILES_DIR) and not name.startswith(OWN_TEMP_PREFIX):
                    return False
            files_dir = entry_dir / FILES_DIR
            if not files_dir.exists():
                return True
            return all(name.startswith(OWN_TEMP_PREFIX) for name in os.listdir(files_dir))
        except OSError:
            return False

    def _bury(self, path: Path) -> Path:
        """Rename ``path`` to a hidden tombstone so it disappears at once; deletion follows."""
        tomb = _tombstone(path)
        try:
            os.rename(path, tomb)
        except FileNotFoundError:
            return tomb
        except OSError:
            log.warning("cannot rename %s for deletion", path, exc_info=True)
            return path
        fsops.fsync_dir(path.parent)
        return tomb

    def _discard(self, path: Path) -> None:
        _remove_path(self._bury(path))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Trash({self.root})"


__all__ = ["Trash", "TrashEntry"]
