"""Inbox items stored as directories with an ``item.json``.

An item is the unit of work between a drop zone and the archive. Its directory
moves through ``eingang/arbeit`` → ``bereit`` (or ``fehler``) → ``erledigt``.
All state lives in ``item.json`` next to the files, so the SQLite index can be
deleted without losing anything::

    <item>/
    ├── item.json        state, analysis, suggestions
    ├── original/        files exactly as received (never modified)
    ├── document.pdf     processed result (after OCR)
    ├── text.json        text per page of document.pdf
    ├── work/            intermediate files of the pipeline
    └── log.txt          processing log

An item exists once its ``item.json`` exists. ``InboxStore.create`` writes
originals under a temporary name and renames them when they are complete, so
a crash never leaves a truncated file under a real name. A directory without
``item.json`` is the remainder of an interrupted ``create``;
``InboxStore.recover_orphans`` turns it into an item if it holds originals
that were moved in (they exist nowhere else), and discards it if it was a copy
(its source still exists) or holds nothing complete.
"""

from __future__ import annotations

import errno
import logging
import os
import socket
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

from ..errors import NotFound
from ..layout import Layout
from ..util import fsops
from ..util.ids import is_item_id, new_item_id
from ..util.names import OWN_TEMP_PREFIX, casefold_key, sanitize_component, unique_filename
from ..util.timeutil import now_iso

log = logging.getLogger(__name__)

STATES = ("arbeit", "bereit", "fehler", "erledigt")
# Pipeline stages in order. Each stage is idempotent and can be re-run.
STAGES = ("received", "normalized", "deblanked", "ocr_done", "analyzed", "ready")

# Written into the directory of an item whose originals are being copied
# (create with move=False) and removed once item.json exists. A directory with
# this marker but without item.json is an interrupted copy: its sources still
# exist, so recover_orphans() discards it instead of adopting a partial item.
CREATING_MARKER = ".docushelf-creating"
# A marker written on another host (another container) is only trusted to
# belong to a dead process after this long; its process cannot be checked.
FOREIGN_MARKER_GRACE_SECONDS = 3600.0
# Errors of os.link() that mean "no hard links here", see fsops.
_LINK_UNSUPPORTED = {errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK, errno.EACCES, errno.ENOSYS}

# Ids of the items that create() is building in this process right now.
_creating: set[str] = set()
_creating_lock = threading.Lock()


@dataclass
class InboxItem:
    id: str
    source: str
    received_at: str
    original_files: list[str]  # relative to the item directory, e.g. "original/scan.pdf"
    origin_hash: str
    stage: str = "received"
    context: dict[str, Any] = field(default_factory=dict)  # mail headers, uploader, import data ...
    options: dict[str, Any] = field(default_factory=dict)  # skip_ocr, skip_blank_removal, combine ...
    attempts: int = 0
    error: dict[str, Any] | None = None  # {"stage", "message", "at"}
    page_count: int = 0
    pages_removed: list[int] = field(default_factory=list)  # 1-based page numbers of the normalized PDF
    analysis: dict[str, Any] | None = None
    suggestions: list[dict[str, Any]] = field(default_factory=list)
    duplicate_of: dict[str, Any] | None = None  # {"kind": "archive"|"inbox", "id", "path"|"title"}
    filing: dict[str, Any] | None = None  # journal of an ongoing filing, see archive.ops
    history: list[dict[str, Any]] = field(default_factory=list)
    # Not serialized:
    dir: Path = field(default=Path("."), repr=False, compare=False)
    state: str = field(default="arbeit", compare=False)

    _SKIP = ("dir", "state")

    # -- paths --------------------------------------------------------------------
    @property
    def item_json(self) -> Path:
        return self.dir / "item.json"

    @property
    def document_path(self) -> Path:
        return self.dir / "document.pdf"

    @property
    def text_path(self) -> Path:
        return self.dir / "text.json"

    @property
    def work_dir(self) -> Path:
        return self.dir / "work"

    @property
    def log_path(self) -> Path:
        return self.dir / "log.txt"

    def original_paths(self) -> list[Path]:
        return [self.dir / rel for rel in self.original_files]

    @property
    def display_name(self) -> str:
        """Name of the first original file, for lists."""
        if self.context.get("title"):
            return str(self.context["title"])
        if self.original_files:
            return self.original_files[0].split("/", 1)[-1]
        return self.id

    @property
    def top_suggestion(self) -> dict[str, Any] | None:
        return self.suggestions[0] if self.suggestions else None

    def add_history(self, event: str, **detail: Any) -> None:
        entry = {"at": now_iso(), "event": event}
        entry.update({k: v for k, v in detail.items() if v is not None})
        self.history.append(entry)
        del self.history[:-50]

    # -- (de)serialization ------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in self._SKIP:
            data.pop(key, None)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, directory: Path, state: str) -> "InboxItem":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k not in cls._SKIP}
        item = cls(**known)
        item.dir = directory
        item.state = state
        return item


class InboxStore:
    """Creates, loads, saves and moves inbox items."""

    def __init__(self, layout: Layout):
        self.layout = layout
        self._lock = threading.RLock()

    # -- creation ----------------------------------------------------------------------
    def create(
        self,
        source: str,
        files: list[Path],
        *,
        context: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        move: bool = True,
        origin_hash: str | None = None,
        received_at: str | None = None,
    ) -> InboxItem:
        """Take ``files`` over into a new item in ``eingang/arbeit`` and return it.

        With ``move=True`` the files are moved (the drop zone is emptied),
        otherwise copied. The origin hash is computed before anything moves.
        Each file gets its final name only once it is complete, and a moved
        source is removed only after that. A copy that is killed half-way
        leaves a marked directory that ``recover_orphans`` discards.
        """
        if not files:
            raise ValueError("an inbox item needs at least one file")
        digest = origin_hash or fsops.sha256_files(list(files))
        item_id = new_item_id()
        directory = self.layout.work / item_id
        while directory.exists():
            item_id = new_item_id()
            directory = self.layout.work / item_id
        with _creating_lock:
            _creating.add(item_id)
        try:
            directory.mkdir(parents=True)
            return self._fill(
                directory,
                source,
                [Path(path) for path in files],
                digest=digest,
                context=context,
                options=options,
                move=move,
                received_at=received_at,
            )
        finally:
            with _creating_lock:
                _creating.discard(item_id)

    def _fill(
        self,
        directory: Path,
        source: str,
        files: list[Path],
        *,
        digest: str,
        context: dict[str, Any] | None,
        options: dict[str, Any] | None,
        move: bool,
        received_at: str | None,
    ) -> InboxItem:
        """Take the files into the new, empty item ``directory`` and write item.json."""
        marker = directory / CREATING_MARKER
        original_dir = directory / "original"
        originals: list[str] = []
        taken: set[str] = set()
        moved_any = False
        try:
            if not move:
                _write_marker(marker)
            original_dir.mkdir()
            for path in files:
                try:
                    name = sanitize_component(path.name)
                except Exception:
                    name = "datei" + path.suffix.lower()
                name = unique_filename(original_dir, name, taken=taken)
                taken.add(casefold_key(name))
                _take_over(path, original_dir / name, move=move)
                moved_any = moved_any or move
                originals.append(f"original/{name}")
            item = InboxItem(
                id=directory.name,
                source=source,
                received_at=received_at or now_iso(),
                original_files=originals,
                origin_hash=digest,
                context=dict(context or {}),
                options=dict(options or {}),
            )
            item.dir = directory
            item.state = "arbeit"
            item.add_history("received", source=source)
            self.save(item)
        except BaseException:
            # Nothing of the caller's has been taken yet: drop the half-built item so
            # recover_orphans() does not turn it into a partial duplicate later. Once a
            # file has been moved in, it stays here and recover_orphans() keeps it.
            if not moved_any:
                try:
                    fsops.remove_tree(directory)
                except OSError:
                    log.warning("cannot remove half-created inbox item %s", directory)
            raise
        if not move:
            _remove_marker(marker)
        return item

    # -- persistence ------------------------------------------------------------------------
    def save(self, item: InboxItem) -> None:
        # Never resurrect an item directory that was moved or deleted meanwhile:
        # writing item.json into a fresh, empty directory would create a ghost item.
        if not item.dir.is_dir():
            raise NotFound(f"Eingangsdokument {item.id} existiert nicht mehr")
        fsops.atomic_write_json(item.item_json, item.to_dict())

    def _load_dir(self, directory: Path, state: str) -> InboxItem | None:
        path = directory / "item.json"
        try:
            data = fsops.read_json(path)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            log.exception("cannot read %s", path)
            return None
        return InboxItem.from_dict(data, directory=directory, state=state)

    def load(self, item_id: str, *, states: tuple[str, ...] = STATES) -> InboxItem:
        if not is_item_id(item_id):
            raise NotFound(item_id)
        for state in states:
            directory = self.layout.state_dir(state) / item_id
            if directory.is_dir():
                item = self._load_dir(directory, state)
                if item is not None:
                    return item
        raise NotFound(item_id)

    def find(self, item_id: str, *, states: tuple[str, ...] = STATES) -> InboxItem | None:
        try:
            return self.load(item_id, states=states)
        except NotFound:
            return None

    def iter_state(self, state: str) -> Iterator[InboxItem]:
        base = self.layout.state_dir(state)
        try:
            names = sorted(os.listdir(base))
        except FileNotFoundError:
            return
        for name in names:
            if not is_item_id(name):
                continue
            item = self._load_dir(base / name, state)
            if item is not None:
                yield item

    def list(self, state: str) -> list[InboxItem]:
        return list(self.iter_state(state))

    def move(self, item: InboxItem, state: str) -> InboxItem:
        """Move the item directory to another state directory."""
        if item.state == state:
            return item
        with self._lock:
            target = self.layout.state_dir(state) / item.id
            fsops.move_dir(item.dir, target)
            item.dir = target
            item.state = state
        return item

    def log(self, item: InboxItem, message: str) -> None:
        line = f"{now_iso()} {message.rstrip()}\n"
        if not item.dir.is_dir():
            log.debug("item %s is gone, not logging: %s", item.id, message)
            return
        try:
            with open(item.log_path, "a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            log.warning("cannot write item log for %s", item.id)

    def read_log(self, item: InboxItem, *, max_bytes: int = 64_000) -> str:
        try:
            with open(item.log_path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - max_bytes))
                return handle.read().decode("utf-8", errors="replace")
        except FileNotFoundError:
            return ""

    def read_text(self, item: InboxItem) -> list[str]:
        try:
            data = fsops.read_json(item.text_path)
        except FileNotFoundError:
            return []
        return [str(page) for page in data]

    def write_text(self, item: InboxItem, pages: list[str]) -> None:
        if not item.dir.is_dir():
            raise NotFound(f"Eingangsdokument {item.id} existiert nicht mehr")
        fsops.atomic_write_json(item.text_path, pages)

    # -- recovery -------------------------------------------------------------------------------
    def recover_orphans(self) -> list[InboxItem]:
        """Clean up item directories without item.json (a crash during ``create``).

        * An interrupted copy (marker of ``create(move=False)`` whose process is
          gone) is discarded: its sources still exist and are taken over again.
        * Temporary files of an interrupted take-over are deleted; their sources
          still exist as well. A directory left with no file at all is removed.
        * Originals that were moved in exist nowhere else: the directory becomes
          an item with a minimal item.json (source "unbekannt"), which is returned.

        Directories that a ``create`` in progress is working on are left alone.
        """
        recovered = []
        for state in ("arbeit", "bereit", "fehler"):
            base = self.layout.state_dir(state)
            if not base.exists():
                continue
            for name in sorted(os.listdir(base)):
                directory = base / name
                if not is_item_id(name) or not directory.is_dir():
                    continue
                with _creating_lock:
                    if name in _creating:
                        continue
                marker = directory / CREATING_MARKER
                if (directory / "item.json").exists():
                    if os.path.lexists(marker):
                        _remove_marker(marker)  # the process died right after writing item.json
                    continue
                if os.path.lexists(marker):
                    if _creator_alive(marker):
                        continue
                    log.warning("discarding inbox directory %s: copying its originals was interrupted", directory)
                    _discard(directory)
                    continue
                files = _complete_originals(directory / "original")
                if not files:
                    if _holds_no_files(directory):
                        log.warning("removing inbox directory %s: it holds no complete file", directory)
                        _discard(directory)
                    else:
                        log.warning("orphaned inbox directory without files: %s", directory)
                    continue
                item = InboxItem(
                    id=name,
                    source="unbekannt",
                    received_at=now_iso(),
                    original_files=[f"original/{p.name}" for p in files],
                    origin_hash=fsops.sha256_files(files),
                )
                item.dir = directory
                item.state = state
                if state != "arbeit":
                    item.dir = self.layout.work / name
                    fsops.move_dir(directory, item.dir)
                    item.state = "arbeit"
                item.add_history("recovered")
                self.save(item)
                recovered.append(item)
        return recovered

    def counts(self) -> dict[str, int]:
        result = {}
        for state in STATES:
            base = self.layout.state_dir(state)
            try:
                result[state] = sum(1 for name in os.listdir(base) if is_item_id(name))
            except FileNotFoundError:
                result[state] = 0
        return result


# ---------------------------------------------------------------------------
# Helpers for create() and recover_orphans()


def _take_over(src: Path, target: Path, *, move: bool) -> None:
    """Make ``target`` (a new name in a fresh item directory) a complete copy of ``src``.

    A hard link appears complete at once; a copy is written under a temporary
    name and renamed when it is complete, so a crash never leaves a truncated
    file under the final name. With ``move`` the source is removed only after
    the target is durable.
    """
    if move:
        try:
            os.link(src, target)
        except FileExistsError:
            raise
        except OSError as exc:
            if exc.errno not in _LINK_UNSUPPORTED:
                raise
            _copy_complete(src, target)
        else:
            fsops.fsync_dir(target.parent)
        os.unlink(src)
        fsops.fsync_dir(src.parent)
    else:
        _copy_complete(src, target)


def _copy_complete(src: Path, target: Path) -> None:
    tmp = fsops.temp_name_for(target)
    fsops.copy_file_fsync(src, tmp)  # removes tmp itself if the copy fails
    try:
        if os.path.lexists(target):
            raise FileExistsError(errno.EEXIST, "target exists", str(target))
        os.rename(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    fsops.fsync_dir(target.parent)


def _hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return ""


def _write_marker(marker: Path) -> None:
    fsops.atomic_write_json(marker, {"pid": os.getpid(), "host": _hostname(), "started_at": now_iso()})


def _remove_marker(marker: Path) -> None:
    try:
        os.unlink(marker)
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("cannot remove %s", marker, exc_info=True)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, but belongs to another user
    except OSError:
        return False
    return True


def _creator_alive(marker: Path) -> bool:
    """Whether the process that wrote ``marker`` may still be copying into its directory."""
    try:
        info = fsops.read_json(marker)
        written = os.stat(marker).st_mtime
    except FileNotFoundError:
        return True  # removed just now: the item was completed meanwhile
    except (OSError, ValueError):
        return False
    if not isinstance(info, dict):
        return False
    host = info.get("host")
    if host and host != _hostname():
        # Another container or machine: its processes cannot be checked from here.
        return time.time() - written < FOREIGN_MARKER_GRACE_SECONDS
    pid = info.get("pid")
    if not isinstance(pid, int) or pid == os.getpid():
        # Creations of this process are registered in _creating; this one is not,
        # so it is left over from an earlier process that had the same id.
        return False
    return _pid_alive(pid)


def _complete_originals(original_dir: Path) -> list[Path]:
    """The original files in ``original_dir``; temporary files of an interrupted take-over are deleted."""
    if not original_dir.is_dir():
        return []
    files = []
    for path in sorted(original_dir.iterdir()):
        if path.name.startswith(OWN_TEMP_PREFIX):
            # Never the only copy: a source is removed only after its file got its final name.
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                log.warning("cannot remove temporary file %s", path, exc_info=True)
            continue
        if path.is_file():
            files.append(path)
    return files


def _holds_no_files(directory: Path) -> bool:
    """True if ``directory`` contains nothing but directories and our own temporary files."""
    for _root, _dirs, names in os.walk(directory):
        if any(not name.startswith(OWN_TEMP_PREFIX) for name in names):
            return False
    return True


def _discard(directory: Path) -> None:
    try:
        fsops.remove_tree(directory)
    except OSError:
        log.warning("cannot remove inbox directory %s", directory, exc_info=True)
