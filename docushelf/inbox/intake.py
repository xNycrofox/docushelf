"""Intake: moves complete files from the drop zones into the inbox.

Channels only ever write into their drop zone (``eingang/quellen/<name>`` or an
external watch folder). The intake polls these directories, waits until a file
is completely written and turns it into an inbox item in ``eingang/arbeit``,
which is then queued for processing.

Rules:

* **Plain files** are taken once their ``(size, mtime_ns)`` stayed unchanged for
  the source's ``stable_seconds``. With ``stable_seconds > 0`` at least one
  earlier observation is required, so a file that is still being written is
  never taken on first sight.
* **Batches** ("Sendungen", see :mod:`docushelf.channels.sendung`) are
  directories with a ``_sendung.json`` manifest. The manifest is written last,
  so its presence means the batch is complete. Every document of the manifest
  becomes one item; the batch directory is removed afterwards.
* **Directories without a manifest** are walked recursively and their files
  are taken one by one. Subdirectories emptied this way are removed; the drop
  zone itself never is. Files in subdirectories need a short minimum quiet
  time, and directories named like a batch wait for their manifest, so a batch
  that is still being written is never taken apart. Their path relative to the
  drop zone is kept in ``item.context["source_path"]``.
* **External watch folders** are copied from, never moved from. With
  ``remove_after_import`` (the default) a file is deleted once its item
  exists; a file that cannot be deleted is remembered by hash and left alone.
  With ``remove_after_import=False`` the folder is never modified: files are
  copied and their hash is remembered, so every file is imported once.

Every household has its own intake: it scans only the sources of its own
settings (``eingang/quellen/*`` below the household's data directory plus
the household's watch folders) and creates items only in its own inbox. A
watch folder that overlaps the data directories of another household
(``protected_paths``) is ignored, so one household can never pull files out
of another one's archive or inbox.

Apart from files that were taken over and processed batch manifests, the
intake deletes nothing but emptied directories and the system clutter in them
(``.DS_Store``, ``Thumbs.db``, AppleDouble files).
"""

from __future__ import annotations

import logging
import os
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from ..channels.sendung import MANIFEST, read_sendung
from ..config import SourceConfig
from ..errors import PathError
from ..util import fsops
from ..util.ids import is_item_id
from ..util.names import is_ignored
from ..util.timeutil import now_iso
from .items import STATES, InboxItem

log = logging.getLogger(__name__)

POLL_INTERVAL = 2.0
# Minimum quiet time for files inside subdirectories of a drop zone. Channels
# write batches as subdirectories and add the manifest last; this keeps the
# intake from taking such a directory apart just before its manifest arrives.
SUBDIR_STABLE_SECONDS = 5.0
# Directories named like a batch (``20261123-125100-a1b2c3``) wait this long for
# their manifest before their files are taken one by one (e.g. after a crash of
# the writing channel).
BATCH_GRACE_SECONDS = 600.0
# Zero-byte files are usually still being created.
EMPTY_FILE_SECONDS = 30.0
# An unreadable manifest must stay unchanged this long before the batch is
# taken file by file.
BROKEN_MANIFEST_SECONDS = 60.0
# After a failed take-over the same unchanged file is left alone for a while.
FAILURE_BACKOFF_SECONDS = 300.0
MAX_DEPTH = 16

# Clutter that operating systems drop into folders; deleted with an emptied directory.
_JUNK_NAMES = frozenset({".ds_store", "thumbs.db", "desktop.ini", ".localized", "icon\r"})

# Results of a take-over attempt.
_CREATED = "created"
_KNOWN = "known"  # imported before from an external folder; left where it is
_GONE = "gone"  # vanished in the meantime
_FAILED = "failed"


def _is_junk(name: str) -> bool:
    return name.casefold() in _JUNK_NAMES or name.startswith("._")


def _regular_file_signature(path: Path) -> tuple[int, int] | None:
    """(size, mtime_ns) of a regular file that is not a symlink, else None."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return st.st_size, st.st_mtime_ns


def _can_remove(path: Path) -> bool:
    """Whether this process may delete ``path`` (directory permissions and sticky bit)."""
    parent = path.parent
    if not os.access(parent, os.W_OK | os.X_OK):
        return False
    try:
        parent_st = os.stat(parent)
        file_st = os.lstat(path)
    except OSError:
        return False
    if parent_st.st_mode & stat.S_ISVTX and hasattr(os, "geteuid"):
        uid = os.geteuid()
        if uid != 0 and uid not in (file_st.st_uid, parent_st.st_uid):
            return False
    return True


def _overlaps(a: str, b: str) -> bool:
    """True if one of two absolute paths contains the other (or both are equal)."""
    try:
        common = os.path.commonpath([a, b])
    except ValueError:
        return False
    return common in (a, b)


@dataclass
class _Observation:
    signature: tuple[int, int]
    since: float


@dataclass
class _Pass:
    """State of one scan over one source."""

    source: SourceConfig
    root: Path
    remove: bool
    live: set[str]
    created: list[str]
    stop: threading.Event | None = None
    inbox_hashes: set[str] | None = None
    # Real paths of other households' watch folders; never entered.
    foreign: frozenset[str] = frozenset()

    @property
    def stopping(self) -> bool:
        return self.stop is not None and self.stop.is_set()


class Intake:
    """Polls all configured sources and creates inbox items from complete files."""

    def __init__(
        self,
        services: Any,
        *,
        poll_interval: float = POLL_INTERVAL,
        subdir_stable_seconds: float = SUBDIR_STABLE_SECONDS,
        batch_grace_seconds: float = BATCH_GRACE_SECONDS,
        empty_file_seconds: float = EMPTY_FILE_SECONDS,
        broken_manifest_seconds: float = BROKEN_MANIFEST_SECONDS,
        failure_backoff_seconds: float = FAILURE_BACKOFF_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        protected_paths: Iterable[Path] = (),
    ):
        self.services = services
        # Directories outside this household's layout that a watch folder must
        # not overlap (the data directories of the other households).
        self.protected_paths: tuple[Path, ...] = tuple(Path(p) for p in protected_paths)
        # Watch folders of the other households. A watch folder of this
        # household that contains one of them skips it, one that equals one of
        # them is ignored: files there belong to the other household.
        self.foreign_watch_folders: tuple[Path, ...] = ()
        household = getattr(services, "household", None)
        household_id = getattr(household, "id", None)
        self.household_id: str | None = household_id if household_id and household_id != "default" else None
        self.poll_interval = poll_interval
        self.subdir_stable_seconds = subdir_stable_seconds
        self.batch_grace_seconds = batch_grace_seconds
        self.empty_file_seconds = empty_file_seconds
        self.broken_manifest_seconds = broken_manifest_seconds
        self.failure_backoff_seconds = failure_backoff_seconds
        self._clock = clock

        # Memory between polls, keyed by absolute path. Entries for paths that
        # were not seen in a pass are dropped at its end.
        self._observations: dict[str, _Observation] = {}
        self._failures: dict[str, tuple[tuple[int, int], float]] = {}
        # Files (and batch manifests) in external folders that were imported
        # already and stay where they are, because the folder is never modified
        # or the file could not be deleted: path -> (signature, sha256 or "batch").
        self._handled: dict[str, tuple[tuple[int, int], str]] = {}
        self._warned: dict[str, str] = {}

        self._scan_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self.last_scan_at: str | None = None
        self.last_error: str | None = None

    # -- lifecycle -----------------------------------------------------------------
    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive() and not self._stop.is_set():
                return
            self._stop = threading.Event()
            name = f"docushelf-intake-{self.household_id}" if self.household_id else "docushelf-intake"
            self._thread = threading.Thread(target=self._run, args=(self._stop,), name=name, daemon=True)
            self._thread.start()
        if self.household_id:
            log.info("intake of household %s started (%d sources)", self.household_id, len(self.services.settings.sources))
        else:
            log.info("intake started (%d sources)", len(self.services.settings.sources))

    def stop(self, timeout: float = 5.0) -> None:
        with self._lifecycle_lock:
            thread = self._thread
            self._thread = None
            self._stop.set()
            self._wake.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                log.warning("intake thread did not stop within %.1f s", timeout)

    def trigger(self) -> None:
        """Scan as soon as possible instead of waiting for the next poll."""
        self._wake.set()

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "last_scan_at": self.last_scan_at,
            "last_error": self.last_error,
            "pending_files": len(self._observations),
        }

    def _run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            # Cleared before scanning: a trigger() during the scan causes another pass.
            self._wake.clear()
            try:
                self._scan(stop)
            except Exception:  # pragma: no cover - _scan already guards each source
                log.exception("intake pass failed")
            self._wake.wait(self.poll_interval)

    # -- scanning --------------------------------------------------------------------
    def scan_once(self) -> list[str]:
        """Run one pass over all sources and return the ids of the created items."""
        return self._scan(None)

    def _scan(self, stop: threading.Event | None) -> list[str]:
        created: list[str] = []
        with self._scan_lock:
            live: set[str] = set()
            errors: list[str] = []
            for source in self.services.settings.sources:
                if stop is not None and stop.is_set():
                    break
                try:
                    self._scan_source(source, live, created, stop)
                except Exception as exc:
                    errors.append(f"{source.name}: {exc}")
                    log.exception("intake: scanning source %s failed", source.name)
            self._forget_unseen(live)
            self.last_scan_at = now_iso()
            self.last_error = "; ".join(errors) or None
        return created

    def _scan_source(self, source: SourceConfig, live: set[str], created: list[str], stop: threading.Event | None) -> None:
        root = self._source_root(source)
        if root is None:
            return
        remove = source.path is None or source.remove_after_import
        foreign = frozenset(os.path.realpath(path) for path in self.foreign_watch_folders)
        state = _Pass(source=source, root=root, remove=remove, live=live, created=created, stop=stop, foreign=foreign)
        self._scan_dir(state, root, 0)

    def _source_root(self, source: SourceConfig) -> Path | None:
        layout = self.services.layout
        key = f"source:{source.name}"
        if source.path is None:
            try:
                root = layout.source_dir(source.name)
                root.mkdir(parents=True, exist_ok=True)
            except (PathError, OSError) as exc:
                self._warn(key, "intake: drop zone of source %s is not usable: %s", source.name, exc)
                return None
            self._warned.pop(key, None)
            return root
        root = Path(source.path)
        real = os.path.realpath(root)
        for protected in (*layout_protected_paths(layout), *self.protected_paths):
            if _overlaps(real, os.path.realpath(protected)):
                self._warn(
                    key,
                    "intake: watch folder %s of source %s overlaps the data directory (%s); it is ignored",
                    root,
                    source.name,
                    protected,
                )
                return None
        for foreign in self.foreign_watch_folders:
            if real == os.path.realpath(foreign):
                self._warn(
                    key,
                    "intake: watch folder %s of source %s is also a watch folder of another household; it is ignored",
                    root,
                    source.name,
                )
                return None
        if not root.is_dir():
            self._warn(key, "intake: watch folder %s of source %s is not available", root, source.name)
            return None
        self._warned.pop(key, None)
        return root

    def _scan_dir(self, state: _Pass, directory: Path, depth: int) -> bool:
        """Take files from ``directory`` and below. Returns True if anything was taken."""
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except FileNotFoundError:
            return False
        except OSError as exc:
            self._warn(f"dir:{directory}", "intake: cannot read %s: %s", directory, exc)
            return False
        if depth > 0 and any(entry.name == MANIFEST for entry in entries):
            return self._take_batch(state, directory)
        took = False
        for entry in entries:
            if state.stopping:
                break
            name = entry.name
            # A manifest directly in the drop zone belongs to no batch; it is never a document.
            if name == MANIFEST or is_ignored(name):
                continue
            path = Path(entry.path)
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if state.foreign and os.path.realpath(path) in state.foreign:
                        continue  # another household's watch folder
                    if depth < MAX_DEPTH and self._scan_dir(state, path, depth + 1):
                        took = True
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue  # sockets, FIFOs, devices
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if self._take_file(state, path, (st.st_size, st.st_mtime_ns), depth) == _CREATED:
                took = True
        # Batch directories (named like inbox items, created by the channels) go once
        # emptied. Other subdirectories stay: scanners are often set up to write into a
        # fixed subfolder (e.g. scan/max) and fail if it disappears.
        if took and depth > 0 and state.remove and is_item_id(directory.name):
            self._remove_dir_if_empty(directory)
        return took

    # -- plain files -------------------------------------------------------------------
    def _take_file(self, state: _Pass, path: Path, signature: tuple[int, int], depth: int) -> str | None:
        key = str(path)
        state.live.add(key)
        handled = self._handled.get(key)
        if handled is not None and handled[0] == signature:
            return None
        if self._in_backoff(key, signature):
            return None
        if not self._is_stable(key, signature, self._quiet_time(state.source, path, depth, signature[0])):
            return None
        if state.remove and state.source.path is None and not _can_remove(path):
            self._warn(key, "intake: no permission to remove %s from its folder; it is left in place", path)
            return None
        context: dict[str, Any] = {}
        rel = path.relative_to(state.root).as_posix()
        if "/" in rel:
            context["source_path"] = rel
        return self._import(state, [path], key=key, signature=signature, context=context, options={}, single=True)

    def _quiet_time(self, source: SourceConfig, path: Path, depth: int, size: int) -> float:
        required = float(source.stable_seconds or 0.0)
        if depth > 0:
            required = max(required, self.subdir_stable_seconds)
            if is_item_id(path.parent.name):
                required = max(required, self.batch_grace_seconds)
        if size == 0:
            required = max(required, self.empty_file_seconds)
        return required

    def _is_stable(self, key: str, signature: tuple[int, int], required: float) -> bool:
        now = self._clock()
        observation = self._observations.get(key)
        if observation is None or observation.signature != signature:
            self._observations[key] = _Observation(signature, now)
            return required <= 0
        return now - observation.since >= required

    def _in_backoff(self, key: str, signature: tuple[int, int]) -> bool:
        entry = self._failures.get(key)
        if entry is None:
            return False
        failed_signature, until = entry
        if failed_signature != signature or self._clock() >= until:
            del self._failures[key]
            return False
        return True

    # -- batches -------------------------------------------------------------------------
    def _take_batch(self, state: _Pass, directory: Path) -> bool:
        manifest = directory / MANIFEST
        key = str(manifest)
        state.live.add(key)
        signature = _regular_file_signature(manifest)
        if signature is None:
            self._warn(key, "intake: %s is not a regular file; batch ignored", manifest)
            return False
        if not state.remove:
            handled = self._handled.get(key)
            if handled is not None and handled[0] == signature:
                return False
        if self._in_backoff(key, signature):
            return False
        try:
            sendung = read_sendung(directory)
        except Exception:  # a manifest with an unexpected structure
            sendung = None
        if sendung is None:
            return self._take_broken_batch(state, directory, key, signature)

        took = False
        for number, document in enumerate(sendung.documents, 1):
            if state.stopping:
                return took
            files = [path for path in sendung.file_paths(document) if _regular_file_signature(path) is not None]
            if not files:
                continue  # taken over in an earlier, interrupted pass
            if len(files) < len(document.files):
                log.warning(
                    "intake: batch %s, document %d: %d of %d files are missing",
                    directory,
                    number,
                    len(document.files) - len(files),
                    len(document.files),
                )
            context = dict(sendung.context)
            if document.title:
                context["title"] = document.title
            result = self._import(
                state, files, key=key, signature=signature, context=context, options=dict(sendung.options), single=False
            )
            if result == _FAILED:
                return took  # the manifest stays; the rest is tried again later
            took = took or result == _CREATED

        if not state.remove:
            self._handled[key] = (signature, "batch")
            return took
        try:
            os.unlink(manifest)
        except FileNotFoundError:
            pass
        except OSError as exc:
            self._warn(key, "intake: cannot remove batch manifest %s: %s", manifest, exc)
            return took
        self._observations.pop(key, None)
        # Files the manifest does not mention stay and are taken as plain files later.
        self._remove_dir_if_empty(directory)
        return True

    def _take_broken_batch(self, state: _Pass, directory: Path, key: str, signature: tuple[int, int]) -> bool:
        if not self._is_stable(key, signature, self.broken_manifest_seconds):
            return False
        manifest = directory / MANIFEST
        try:
            head = manifest.read_bytes()[:300]
        except OSError:
            head = b""
        self._warn(key, "intake: batch manifest %s is unreadable (%r); taking its files one by one", manifest, head)
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError:
            return False
        took = False
        for entry in entries:
            if state.stopping:
                return took
            if entry.name == MANIFEST or is_ignored(entry.name):
                continue
            path = Path(entry.path)
            file_signature = _regular_file_signature(path)
            if file_signature is None:
                continue
            file_key = str(path)
            state.live.add(file_key)
            handled = self._handled.get(file_key)
            if handled is not None and handled[0] == file_signature:
                continue
            result = self._import(
                state, [path], key=file_key, signature=file_signature, context={}, options={}, single=True
            )
            if result == _FAILED:
                return took
            took = took or result == _CREATED
        if not state.remove:
            return took
        try:
            os.unlink(manifest)
        except FileNotFoundError:
            pass
        except OSError as exc:
            self._warn(key, "intake: cannot remove batch manifest %s: %s", manifest, exc)
            return took
        self._observations.pop(key, None)
        self._remove_dir_if_empty(directory)
        return True

    # -- creating items ---------------------------------------------------------------------
    def _import(
        self,
        state: _Pass,
        files: list[Path],
        *,
        key: str,
        signature: tuple[int, int],
        context: dict[str, Any],
        options: dict[str, Any],
        single: bool,
    ) -> str:
        """Create one inbox item from ``files``.

        Drop zones inside the data directory are moved from. External watch
        folders (often network mounts with their own idea of permissions) are
        copied from; with ``remove_after_import`` the sources are deleted only
        once the item exists. A source that cannot be deleted is remembered by
        hash, so it is imported once and then left alone.
        """
        external = state.source.path is not None
        origin_hash: str | None = None
        if external:
            try:
                origin_hash = fsops.sha256_files(files)
            except FileNotFoundError:
                return _GONE
            except OSError as exc:
                self._failures[key] = (signature, self._clock() + self.failure_backoff_seconds)
                self._warn(key, "intake: cannot read %s: %s", files[0], exc)
                return _FAILED
            if single and _regular_file_signature(files[0]) != signature:
                # Changed while it was hashed: start observing again.
                self._observations.pop(key, None)
                return _GONE
            # A folder that is emptied may receive the same content again later;
            # only files that were imported but could not be deleted are skipped.
            if self._hash_seen(state, origin_hash) if state.remove else self._already_imported(state, origin_hash):
                if single:
                    self._handled[key] = (signature, origin_hash)
                return _KNOWN
        try:
            item = self.services.inbox.create(
                state.source.name,
                files,
                context=context,
                options=options,
                move=state.remove and not external,
                origin_hash=origin_hash,
            )
        except FileNotFoundError:
            log.info("intake: %s vanished before it could be taken over", files[0])
            self._observations.pop(key, None)
            return _GONE
        except Exception as exc:
            self._failures[key] = (signature, self._clock() + self.failure_backoff_seconds)
            self._warn(key, "intake: cannot take over %s: %s", files[0], exc)
            log.debug("take-over failure", exc_info=True)
            return _FAILED
        self._failures.pop(key, None)
        self._warned.pop(key, None)
        if single:
            self._observations.pop(key, None)
        if external and origin_hash is not None:
            kept = self._remove_sources(files) if state.remove else list(files)
            if kept:
                try:
                    self.services.index.mark_hash_seen(state.source.name, origin_hash)
                except Exception:
                    log.exception("intake: cannot remember the hash of %s", files[0])
                for path in kept:
                    kept_signature = _regular_file_signature(path)
                    if kept_signature is not None:
                        self._handled[str(path)] = (kept_signature, origin_hash)
        self._created(state, item)
        return _CREATED

    def _remove_sources(self, files: list[Path]) -> list[Path]:
        """Delete imported files from an external folder; returns those that could not be deleted."""
        kept: list[Path] = []
        for path in files:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                kept.append(path)
                self._warn(
                    str(path), "intake: %s was imported but cannot be removed (%s); it is left in place", path, exc
                )
        return kept

    def _hash_seen(self, state: _Pass, digest: str) -> bool:
        try:
            return bool(self.services.index.hash_seen(state.source.name, digest))
        except Exception:
            log.warning("intake: hash lookup failed", exc_info=True)
            return False

    def _already_imported(self, state: _Pass, digest: str) -> bool:
        """Was this content imported before? Checked against the remembered hashes and,
        because the index may have been rebuilt, also against the archive and the inbox."""
        index = self.services.index
        name = state.source.name
        known = False
        try:
            if index.hash_seen(name, digest):
                return True
            known = bool(index.by_origin_hash(digest))
        except Exception:
            log.warning("intake: hash lookup failed", exc_info=True)
        if not known:
            if state.inbox_hashes is None:
                state.inbox_hashes = self._inbox_origin_hashes()
            known = digest in state.inbox_hashes
        if known:
            try:
                index.mark_hash_seen(name, digest)
            except Exception:
                log.debug("cannot remember hash", exc_info=True)
        return known

    def _inbox_origin_hashes(self) -> set[str]:
        hashes: set[str] = set()
        for inbox_state in STATES:
            try:
                for item in self.services.inbox.iter_state(inbox_state):
                    if item.origin_hash:
                        hashes.add(item.origin_hash)
            except Exception:
                log.warning("intake: cannot list inbox items in %s", inbox_state, exc_info=True)
        return hashes

    def _created(self, state: _Pass, item: InboxItem) -> None:
        state.created.append(item.id)
        names = ", ".join(rel.split("/", 1)[-1] for rel in item.original_files)
        if self.household_id:
            log.info(
                "intake: %s from %s became inbox item %s of household %s", names, state.source.name, item.id, self.household_id
            )
        else:
            log.info("intake: %s from %s became inbox item %s", names, state.source.name, item.id)
        try:
            self.services.inbox.log(item, f"Eingang über „{state.source.name}“: {names}")
        except Exception:
            log.debug("cannot write item log", exc_info=True)
        try:
            self.services.index.enqueue("process", item.id)
        except Exception:
            log.exception("intake: cannot queue item %s; it is picked up again on the next start", item.id)
        worker = getattr(self.services, "worker", None)
        if worker is not None:
            try:
                worker.kick()
            except Exception:
                log.exception("intake: cannot wake the worker")

    # -- housekeeping -------------------------------------------------------------------------
    def _remove_dir_if_empty(self, directory: Path) -> bool:
        """Remove a directory that holds nothing but system clutter. Never touches anything else."""
        try:
            with os.scandir(directory) as iterator:
                entries = list(iterator)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        for entry in entries:
            try:
                if not _is_junk(entry.name) or entry.is_dir(follow_symlinks=False):
                    return False
            except OSError:
                return False
        for entry in entries:
            try:
                os.unlink(entry.path)
            except FileNotFoundError:
                pass
            except OSError:
                return False
        try:
            os.rmdir(directory)
        except FileNotFoundError:
            return True
        except OSError:
            return False  # something new arrived in the meantime
        log.debug("intake: removed emptied directory %s", directory)
        return True

    def _forget_unseen(self, live: set[str]) -> None:
        for memory in (self._observations, self._failures, self._handled):
            for key in [key for key in memory if key not in live]:
                del memory[key]
        if len(self._warned) > 10_000:
            self._warned.clear()

    def _warn(self, key: str, message: str, *args: Any) -> None:
        """Log a problem once; repeated identical reports from later polls go to debug."""
        text = message % args if args else message
        if self._warned.get(key) == text:
            log.debug(text)
            return
        self._warned[key] = text
        log.warning(text)


def layout_protected_paths(layout: Any) -> tuple[Path, ...]:
    """The directories of a household that no watch folder may overlap."""
    return (layout.archive, layout.inbox, layout.trash, layout.config, layout.audit, layout.index)


def protect_households(registry: Any) -> None:
    """Keep every household's watch folders out of the other households' data.

    Sets ``protected_paths`` of each household's intake to the data
    directories of all other households. Without this a misconfigured watch
    folder of one household could move files out of another one's archive.
    ``foreign_watch_folders`` are the watch folders of all other households,
    so a watch folder never takes files from another household's watch folder
    nested inside it (e.g. ``/watch`` next to ``/watch/eltern``).
    """
    households = list(registry.households)
    if len(households) < 2:
        return
    layouts = {household.id: registry.services(household.id).layout for household in households}
    watched = {
        household.id: tuple(getattr(registry.services(household.id).settings, "sources", None) or ())
        for household in households
    }
    for household in households:
        intake = getattr(registry.services(household.id), "intake", None)
        if intake is None:
            continue
        intake.protected_paths = tuple(
            path for other, layout in layouts.items() if other != household.id for path in layout_protected_paths(layout)
        )
        intake.foreign_watch_folders = tuple(
            Path(source.path)
            for other, sources in watched.items()
            if other != household.id
            for source in sources
            if source.path is not None
        )
