"""Reconciliation of the index with the archive folder tree.

The archive is the truth. A reconciliation pass walks the whole tree and
brings the index in line with it:

* Files whose size, mtime and inode match the index cost one ``stat`` call.
* New paths are first matched against documents whose file vanished in the
  same pass – by the document id stored in XMP, then by content hash – so a
  move or rename keeps id, text and tags.
* Files that cannot be read right now (locked, no permission, vanishing while
  the pass runs) never remove a document; the next pass looks again.
* Empty folders are valid filing targets and are indexed as well.

To keep the application responsive during large passes, the tree is walked and
new files are hashed without holding ``archive_lock``. The changes are then
applied in one index transaction under the lock, after re-checking everything
that may have changed in the meantime (the application itself changes the
archive only while holding the lock). Text is extracted afterwards, one
document at a time, each step under the lock. A caller that already holds the
lock (``Services.rebuild``) keeps it for the whole pass.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from ..errors import Conflict, DocuShelfError, NotFound, PathError
from ..models import DocMeta, DocumentRecord, ScanStats
from ..pdf.text import extract_pages, has_text
from ..pdf.xmp import read_meta
from ..util.fsops import sha256_file
from ..util.ids import is_doc_id, is_provisional, provisional_id
from ..util.names import OWN_TEMP_PREFIX, check_relative, is_ignored, parse_filename
from ..util.timeutil import now_iso

log = logging.getLogger(__name__)

__all__ = ["Reconciler", "derive_title_date"]

PDF_EXT = ".pdf"
# Temporary files of our own (``.docushelf-*.part``) older than this are leftovers of a crash.
STALE_TEMP_SECONDS = 3600
# index_file retries a file that is still being written this often before giving up.
_INDEX_FILE_ATTEMPTS = 5
_INDEX_FILE_RETRY_DELAY = 0.2


def derive_title_date(filename: str, meta: DocMeta) -> tuple[str, date | None]:
    """Title and document date shown and indexed for an archived file.

    A name following ``YYYY-MM-DD Titel.ext`` wins, because users rename files in
    their file manager. Otherwise the XMP title is used for files that carry a
    document id (titles written by other producers are often meaningless), else
    the file name; the date then comes from XMP. Collision suffixes such as
    `` (2)`` are never part of the title.
    """
    parsed = parse_filename(filename)
    xmp_title = (meta.title or "").strip() if meta.has_identity else ""
    if parsed.doc_date is not None:
        title = parsed.title
        if xmp_title and title == parsed.doc_date.isoformat():
            # "2025-03-01.pdf" has no title of its own.
            title = xmp_title
        return title, parsed.doc_date
    return (xmp_title or parsed.title or filename), meta.doc_date


# ---------------------------------------------------------------------------
# Internal data


class _Unstable(Exception):
    """The file changed while it was being read."""


class _Cancelled(Exception):
    """The pass was asked to stop."""


@dataclass
class _Identity:
    """What a file says about itself, together with the stat it was read under."""

    content_hash: str
    meta: DocMeta
    xmp_id: str | None
    size: int
    mtime_ns: int
    inode: int


@dataclass
class _Walk:
    # rel path -> lstat result, or None when the entry could not be stat'ed
    files: dict[str, os.stat_result | None] = field(default_factory=dict)
    failures: dict[str, OSError] = field(default_factory=dict)
    dirs: set[str] = field(default_factory=set)
    # directories that could not be listed; nothing below them is touched
    unreadable: set[str] = field(default_factory=set)
    root_entries: int = 0


@dataclass
class _TextJob:
    path: str
    content_hash: str
    statuses: tuple[str, ...]


class _ApplyState:
    """Book-keeping of one apply step."""

    def __init__(self, stats: ScanStats):
        self.stats = stats
        # Documents whose file vanished in this pass, by id. Entries are taken
        # out when a new path turns out to be the same document.
        self.missing: dict[str, DocumentRecord] = {}
        self.by_xmp: dict[str, list[DocumentRecord]] = {}
        self.by_hash: dict[str, list[DocumentRecord]] = {}
        # path -> content hash of documents whose text has to be (re)extracted
        self.text: dict[str, str] = {}
        # ids no longer used by any document after this pass
        self.freed: set[str] = set()
        self.events: list[tuple[str, dict[str, Any]]] = []

    def add_missing(self, record: DocumentRecord) -> None:
        self.missing[record.id] = record
        if record.xmp_id:
            self.by_xmp.setdefault(record.xmp_id, []).append(record)
        self.by_hash.setdefault(record.content_hash, []).append(record)

    def missing_by_xmp(self, xmp_id: str) -> DocumentRecord | None:
        record = self.missing.get(xmp_id)
        if record is not None:
            return record
        for candidate in self.by_xmp.get(xmp_id, ()):
            if candidate.id in self.missing:
                return candidate
        return None

    def missing_by_hash(self, content_hash: str, rel: str) -> DocumentRecord | None:
        candidates = [r for r in self.by_hash.get(content_hash, ()) if r.id in self.missing]
        if not candidates:
            return None
        folder, _, name = rel.rpartition("/")
        candidates.sort(key=lambda r: (r.filename != name, r.folder != folder, r.path))
        return candidates[0]


class _Listings:
    """Exact directory listings, cached for one apply step.

    Existence checks compare names exactly as stored on disk. ``os.path.exists``
    is not good enough: on case- or normalization-insensitive file systems it
    reports "Rechnung.pdf" as present after a rename to "rechnung.pdf".
    """

    def __init__(self, root: Path):
        self.root = root
        self._cache: dict[str, dict[str, str] | None] = {}

    def _entries(self, rel_dir: str) -> dict[str, str] | None:
        if rel_dir in self._cache:
            return self._cache[rel_dir]
        path = self.root / rel_dir if rel_dir else self.root
        result: dict[str, str] | None
        try:
            result = {}
            with os.scandir(path) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_symlink():
                            kind = "other"
                        elif entry.is_dir(follow_symlinks=False):
                            kind = "dir"
                        elif entry.is_file(follow_symlinks=False):
                            kind = "file"
                        else:
                            kind = "other"
                    except OSError:
                        kind = "unknown"
                    result[entry.name] = kind
        except (FileNotFoundError, NotADirectoryError):
            result = {}
        except OSError:
            result = None
        self._cache[rel_dir] = result
        return result

    def presence(self, rel: str, kind: str) -> bool | None:
        """True/False if ``rel`` exists as ``kind`` ("file"/"dir"); None if that cannot be told."""
        parent, _, name = rel.rpartition("/")
        entries = self._entries(parent)
        if entries is None:
            return None
        found = entries.get(name)
        if found is None:
            return False
        if found == "unknown":
            return None
        return found == kind


def _clean_doc_id(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip().lower()
    return candidate if is_doc_id(candidate) else None


def _ext_of(filename: str) -> str:
    return parse_filename(filename).ext


def _entry_matches(st: os.stat_result, entry: tuple[str, int, int, int | None]) -> bool:
    _, size, mtime_ns, inode = entry
    return st.st_size == size and st.st_mtime_ns == mtime_ns and inode is not None and st.st_ino == inode


def _ident_matches(ident: _Identity, entry: tuple[str, int, int, int | None]) -> bool:
    _, size, mtime_ns, inode = entry
    return ident.size == size and ident.mtime_ns == mtime_ns and inode is not None and ident.inode == inode


def _same_signature(st: os.stat_result, ident: _Identity) -> bool:
    return st.st_size == ident.size and st.st_mtime_ns == ident.mtime_ns and st.st_ino == ident.inode


def _record_matches(st: os.stat_result, record: DocumentRecord) -> bool:
    if st.st_size != record.size or st.st_mtime_ns != record.mtime_ns:
        return False
    return record.inode is None or st.st_ino == record.inode


def _has_ancestor(rel: str, prefixes: set[str]) -> bool:
    """True if a proper ancestor directory of ``rel`` is in ``prefixes``."""
    if not prefixes:
        return False
    parts = rel.split("/")
    for i in range(1, len(parts)):
        if "/".join(parts[:i]) in prefixes:
            return True
    return False


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise _Cancelled()


def _storable(name: str) -> bool:
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


# ---------------------------------------------------------------------------


class Reconciler:
    """Keeps the index in line with the archive tree (see module docstring)."""

    def __init__(self, services: Any):
        self.services = services
        self._warned: set[str] = set()
        self._warned_lock = threading.Lock()

    def _warn_once(self, key: str, message: str, *args: Any) -> None:
        """Log a problem with a path as a warning once, later passes only at debug level."""
        with self._warned_lock:
            first = key not in self._warned
            if first and len(self._warned) < 10_000:
                self._warned.add(key)
        log.log(logging.WARNING if first else logging.DEBUG, message, *args)

    # -- public API ----------------------------------------------------------------
    def scan(self, *, full: bool = True, verify: bool = False, cancel: threading.Event | None = None) -> ScanStats:
        """Run one reconciliation pass over the whole archive.

        ``verify`` re-hashes files even if their stat is unchanged and retries
        documents whose text extraction failed. A quick pass (``full=False``) does
        the same work but does not count as the periodic full pass
        (``last_full_scan``). ``cancel`` stops the pass early: the index changes
        of a pass are applied in one transaction or not at all; text extraction
        stops between documents and the next pass resumes it.

        Raises DocuShelfError when the archive root itself cannot be read.
        """
        started = time.monotonic()
        stats = ScanStats(full=full)
        index = self.services.index
        try:
            snapshot = index.path_index()
            walk = self._walk(stats, cancel)
            idents, skipped = self._read_changed(walk, snapshot, stats, verify=verify, cancel=cancel)
            with self.services.archive_lock:
                _check_cancel(cancel)
                jobs = self._apply(walk, idents, skipped, stats, verify=verify)
        except _Cancelled:
            stats.duration_ms = int((time.monotonic() - started) * 1000)
            log.info("reconciliation cancelled before changes were applied")
            return stats
        self._extract_jobs(jobs, stats, cancel)
        stats.duration_ms = int((time.monotonic() - started) * 1000)
        finished = now_iso()
        index.set_meta("last_scan", finished)
        if full:
            index.set_meta("last_full_scan", finished)
        index.set_meta("last_scan_stats", json.dumps(stats.to_dict()))
        changes = stats.added + stats.updated + stats.moved + stats.removed
        log.log(
            logging.INFO if changes or stats.errors else logging.DEBUG,
            "reconciliation (%s): %d files, %d folders, +%d ~%d >%d -%d, %d errors, %d ms",
            "full" if full else "quick",
            stats.files_seen,
            stats.folders_seen,
            stats.added,
            stats.updated,
            stats.moved,
            stats.removed,
            stats.errors,
            stats.duration_ms,
        )
        return stats

    def index_file(self, path: Path) -> DocumentRecord:
        """(Re)index one archive file right now and return its record.

        Used by the application after it placed or changed a file. The record
        at that path keeps its id when the identity matches; a file whose old
        record still points to a vanished path (moved by the caller) takes that
        record over. Raises NotFound, PathError (outside the archive, ignored
        names), Conflict (file keeps changing) or OSError (transient).
        """
        index = self.services.index
        with self.services.archive_lock:
            rel = self._archive_rel(Path(path))
            abs_path = self._abs(rel)
            ident: _Identity | None = None
            for attempt in range(_INDEX_FILE_ATTEMPTS):
                try:
                    st = os.lstat(abs_path)
                except FileNotFoundError:
                    raise NotFound(f"Datei nicht gefunden: {rel}") from None
                if not stat.S_ISREG(st.st_mode):
                    raise PathError(f"Keine reguläre Datei: {rel}")
                try:
                    ident = self._read_identity(abs_path, st)
                    break
                except _Unstable:
                    time.sleep(_INDEX_FILE_RETRY_DELAY * (attempt + 1))
            if ident is None:
                raise Conflict(f"Die Datei wird gerade noch verändert: {rel}")
            existing = index.get_by_path(rel)
            doc_id = self._index_file_id(rel, ident, existing)
            pages, status = self._extract_text(abs_path, _ext_of(abs_path.name))
            record = self._build_record(rel, ident, doc_id, page_count=len(pages), text_status=status)
            freed: set[str] = set()
            with index.tx():
                if record.folder:
                    index.add_folder_with_parents(record.folder)
                if existing is not None and existing.id != doc_id:
                    if not index.id_exists(doc_id):
                        index.change_id(existing.id, doc_id)
                    # Otherwise the row of the moved document takes this path over
                    # and upsert drops the old row.
                    freed.add(existing.id)
                index.upsert(record, pages)
                self._promote_copies(freed)
            result = index.get(doc_id)
            if result is None:  # pragma: no cover - upsert always stores the record
                raise NotFound(doc_id)
            return result

    def forget(self, rel_path: str) -> None:
        """Remove a document (or a folder with everything below it) from the index only."""
        rel = (rel_path or "").replace("\\", "/").strip("/")
        if not rel:
            return
        index = self.services.index
        with self.services.archive_lock:
            freed: set[str] = set()
            with index.tx():
                record = index.get_by_path(rel)
                if record is not None:
                    index.delete(record.id)
                    freed.add(record.id)
                elif index.folder_exists(rel):
                    prefix = rel + "/"
                    for doc in index.list_in_folder(rel, recursive=True, limit=10_000_000):
                        if doc.folder == rel or doc.folder.startswith(prefix):
                            index.delete(doc.id)
                            freed.add(doc.id)
                    index.remove_folder_tree(rel)
                self._promote_copies(freed)

    def ensure_folder(self, rel: str) -> None:
        """Record a folder (and its parents) in the index, e.g. right after mkdir."""
        rel = check_relative(rel)
        if not rel:
            return
        with self.services.archive_lock:
            self.services.index.add_folder_with_parents(rel)

    # -- walking ---------------------------------------------------------------------
    def _abs(self, rel: str) -> Path:
        return self.services.layout.archive / rel

    def _walk(self, stats: ScanStats, cancel: threading.Event | None) -> _Walk:
        root = self.services.layout.archive
        try:
            root_stat = os.stat(root)
        except OSError as exc:
            raise DocuShelfError(f"Archivordner ist nicht erreichbar: {root} ({exc.strerror or exc})") from exc
        if not stat.S_ISDIR(root_stat.st_mode):
            raise DocuShelfError(f"Archivpfad ist kein Ordner: {root}")
        walk = _Walk()
        now = time.time()
        stack: list[tuple[str, str]] = [("", os.fspath(root))]
        while stack:
            _check_cancel(cancel)
            rel_dir, abs_dir = stack.pop()
            try:
                with os.scandir(abs_dir) as iterator:
                    entries = sorted(iterator, key=lambda e: e.name)
            except OSError as exc:
                if not rel_dir:
                    raise DocuShelfError(
                        f"Archivordner kann nicht gelesen werden: {root} ({exc.strerror or exc})"
                    ) from exc
                walk.unreadable.add(rel_dir)
                stats.errors += 1
                log.warning("cannot list archive folder %s: %s", abs_dir, exc)
                continue
            for entry in entries:
                name = entry.name
                if name.startswith(OWN_TEMP_PREFIX):
                    self._remove_stale_temp(entry, now)
                    continue
                if is_ignored(name):
                    continue
                if not rel_dir:
                    walk.root_entries += 1
                if not _storable(name):
                    # Bytes that are not UTF-8 (old SMB clients) cannot be stored in the index.
                    stats.errors += 1
                    self._warn_once(entry.path, "skipping %r: the name is not valid UTF-8", entry.path)
                    continue
                rel = f"{rel_dir}/{name}" if rel_dir else name
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    # Vanished or unreadable between listing and stat: whatever the
                    # index knows about this path stays as it is.
                    walk.files[rel] = None
                    walk.failures[rel] = exc
                    continue
                if stat.S_ISDIR(st.st_mode):
                    walk.dirs.add(rel)
                    stack.append((rel, entry.path))
                elif stat.S_ISREG(st.st_mode):
                    walk.files[rel] = st
                # Symlinks are never followed; sockets, FIFOs and devices are no documents.
        stats.files_seen = len(walk.files)
        stats.folders_seen = len(walk.dirs)
        return walk

    def _remove_stale_temp(self, entry: os.DirEntry, now: float) -> None:
        if not entry.name.endswith(".part"):
            return
        try:
            st = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode) or now - st.st_mtime < STALE_TEMP_SECONDS:
                return
            os.unlink(entry.path)
            log.info("removed stale temporary file %s", entry.path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("cannot remove stale temporary file %s: %s", entry.path, exc)

    # -- reading files -------------------------------------------------------------------
    def _read_identity(self, path: Path, st: os.stat_result) -> _Identity:
        """Hash and metadata of ``path``; raises _Unstable if it changed meanwhile, OSError if unreadable."""
        content_hash = sha256_file(path)
        meta = DocMeta()
        if _ext_of(path.name) == PDF_EXT:
            try:
                meta = read_meta(path)
            except OSError:
                raise
            except Exception as exc:  # damaged or encrypted PDF: index it without metadata
                log.info("cannot read metadata of %s: %s", path, exc)
        after = os.lstat(path)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_size != st.st_size
            or after.st_mtime_ns != st.st_mtime_ns
            or after.st_ino != st.st_ino
        ):
            raise _Unstable(str(path))
        return _Identity(
            content_hash=content_hash,
            meta=meta,
            xmp_id=_clean_doc_id(meta.doc_id),
            size=after.st_size,
            mtime_ns=after.st_mtime_ns,
            inode=after.st_ino,
        )

    def _read_changed(
        self,
        walk: _Walk,
        snapshot: dict[str, tuple[str, int, int, int | None]],
        stats: ScanStats,
        *,
        verify: bool,
        cancel: threading.Event | None,
    ) -> tuple[dict[str, _Identity], set[str]]:
        """Read the identity of every new or changed file (without holding the archive lock)."""
        idents: dict[str, _Identity] = {}
        skipped: set[str] = set()
        for rel in sorted(walk.files):
            st = walk.files[rel]
            if st is None:
                skipped.add(rel)
                failure = walk.failures.get(rel)
                if rel in snapshot or not isinstance(failure, FileNotFoundError):
                    stats.errors += 1
                continue
            entry = snapshot.get(rel)
            if entry is not None and not verify and _entry_matches(st, entry):
                continue
            _check_cancel(cancel)
            try:
                idents[rel] = self._read_identity(self._abs(rel), st)
            except _Unstable:
                # Still being written; the next pass takes it.
                skipped.add(rel)
                log.debug("skipping %s: it changed while being read", rel)
            except FileNotFoundError:
                skipped.add(rel)
                if rel in snapshot:
                    stats.errors += 1
            except OSError as exc:
                skipped.add(rel)
                stats.errors += 1
                log.warning("cannot read %s: %s", rel, exc)
        return idents, skipped

    def _extract_text(self, path: Path, ext: str) -> tuple[list[str], str]:
        """Page texts and text status. OSError means "try again later"."""
        if ext != PDF_EXT:
            return [], "unsupported"
        # PDFium reports access problems as its own error type; open the file
        # first so they surface as OSError and are not mistaken for broken content.
        with open(path, "rb"):
            pass
        try:
            pages = extract_pages(path)
        except OSError:
            raise
        except Exception as exc:
            os.lstat(path)  # vanished meanwhile -> FileNotFoundError (transient)
            log.warning("cannot extract text from %s: %s", path, exc)
            return [], "error"
        # Any text at all means the file has a text layer; "empty" marks files that need OCR.
        return pages, ("ok" if has_text(pages, min_chars=1) else "empty")

    def _build_record(
        self, rel: str, ident: _Identity, doc_id: str, *, page_count: int, text_status: str
    ) -> DocumentRecord:
        folder, _, filename = rel.rpartition("/")
        title, doc_date = derive_title_date(filename, ident.meta)
        meta = ident.meta
        return DocumentRecord(
            id=doc_id,
            path=rel,
            folder=folder,
            filename=filename,
            ext=_ext_of(filename),
            title=title,
            doc_date=doc_date,
            sender=(meta.sender or "").strip() or None,
            tags=list(meta.tags),
            # Files that arrived without our metadata are their own origin.
            origin_hash=meta.origin_hash or ident.content_hash,
            content_hash=ident.content_hash,
            xmp_id=ident.xmp_id,
            has_xmp=ident.xmp_id is not None,
            size=ident.size,
            mtime_ns=ident.mtime_ns,
            inode=ident.inode,
            page_count=page_count,
            text_status=text_status,
            indexed_at=now_iso(),
        )

    @staticmethod
    def _initial_status(rel: str) -> str:
        return "pending" if _ext_of(rel.rpartition("/")[2]) == PDF_EXT else "unsupported"

    # -- applying a pass ------------------------------------------------------------------
    def _apply(
        self,
        walk: _Walk,
        idents: dict[str, _Identity],
        skipped: set[str],
        stats: ScanStats,
        *,
        verify: bool,
    ) -> list[_TextJob]:
        index = self.services.index
        known = index.path_index()
        if walk.root_entries == 0 and known:
            # An empty archive root while documents are indexed is far more likely
            # a missing mount than a deliberate deletion of everything. ``rebuild``
            # starts from an empty index and is not affected.
            log.warning(
                "archive root %s is empty although %d documents are indexed; leaving the index unchanged",
                self.services.layout.archive,
                len(known),
            )
            stats.errors += 1
            return []
        listings = _Listings(self.services.layout.archive)
        state = _ApplyState(stats)
        with index.tx():
            self._apply_folders(walk, listings)
            changed, new = self._classify(walk, known, idents, skipped, stats, verify=verify)
            self._collect_missing(known, walk, listings, state)
            for rel, ident in changed:
                self._apply_changed(rel, ident, state)
            for rel, ident in new:
                self._apply_new(rel, ident, state)
            for record in sorted(state.missing.values(), key=lambda r: r.path):
                index.delete(record.id)
                state.freed.add(record.id)
                stats.removed += 1
                state.events.append(("external_delete", {"doc_id": record.id, "path": record.path, "title": record.title}))
                log.debug("document %s vanished from %s", record.id, record.path)
            self._promote_copies(state.freed)
            jobs = self._text_jobs(state, verify=verify)
        self._audit(state.events)
        return jobs

    def _apply_folders(self, walk: _Walk, listings: _Listings) -> None:
        index = self.services.index
        gen = index.next_scan_gen()
        existing = set(index.list_folders())
        for rel in sorted(walk.dirs):
            if rel not in existing and listings.presence(rel, "dir") is False:
                continue  # removed again since the walk
            index.upsert_folder(rel, gen)
        for rel in existing - walk.dirs:
            # Keep folders below unreadable directories and folders created after
            # the walk (e.g. by the application, which uses ensure_folder).
            if _has_ancestor(rel, walk.unreadable) or listings.presence(rel, "dir") is not False:
                index.upsert_folder(rel, gen)
        index.delete_unseen_folders(gen)

    def _classify(
        self,
        walk: _Walk,
        known: dict[str, tuple[str, int, int, int | None]],
        idents: dict[str, _Identity],
        skipped: set[str],
        stats: ScanStats,
        *,
        verify: bool,
    ) -> tuple[list[tuple[str, _Identity]], list[tuple[str, _Identity]]]:
        changed: list[tuple[str, _Identity]] = []
        new: list[tuple[str, _Identity]] = []
        for rel in sorted(walk.files):
            if rel in skipped:
                continue
            st = walk.files[rel]
            assert st is not None  # entries without stat are always skipped
            entry = known.get(rel)
            ident = idents.get(rel)
            path = self._abs(rel)
            try:
                current = os.lstat(path) if ident is not None or entry is None or not _entry_matches(st, entry) else None
            except FileNotFoundError:
                # Gone since the walk. A document at this path stays until the next pass confirms it.
                if entry is not None:
                    stats.errors += 1
                continue
            except OSError as exc:
                stats.errors += 1
                log.warning("cannot stat %s: %s", rel, exc)
                continue
            if current is None:
                stats.unchanged += 1
                continue
            if ident is None:
                # The index changed since the snapshot was taken (the application
                # indexed or forgot this path meanwhile): read the file now.
                try:
                    ident = self._read_identity(path, current)
                except _Unstable:
                    continue
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    stats.errors += 1
                    log.warning("cannot read %s: %s", rel, exc)
                    continue
            elif not _same_signature(current, ident):
                continue  # changed again after it was read; the next pass takes it
            if entry is not None:
                if not verify and _ident_matches(ident, entry):
                    stats.unchanged += 1
                    continue
                changed.append((rel, ident))
            else:
                new.append((rel, ident))
        return changed, new

    def _collect_missing(
        self,
        known: dict[str, tuple[str, int, int, int | None]],
        walk: _Walk,
        listings: _Listings,
        state: _ApplyState,
    ) -> None:
        index = self.services.index
        for rel, entry in known.items():
            if rel in walk.files or _has_ancestor(rel, walk.unreadable):
                continue
            presence = listings.presence(rel, "file")
            if presence is None:
                state.stats.errors += 1
                continue
            if presence:
                continue  # placed after the walk; the next pass looks at it
            record = index.get(entry[0])
            if record is not None and record.path == rel:
                state.add_missing(record)

    def _apply_changed(self, rel: str, ident: _Identity, state: _ApplyState) -> None:
        index = self.services.index
        record = index.get_by_path(rel)
        if record is None:
            self._apply_new(rel, ident, state)
            return
        if ident.content_hash == record.content_hash:
            # Same bytes (touched, or replaced by an identical copy): refresh the stat only.
            refreshed = self._build_record(
                rel, ident, record.id, page_count=record.page_count, text_status=record.text_status
            )
            index.upsert(refreshed)
            state.stats.unchanged += 1
            if refreshed.text_status == "pending":
                state.text[rel] = ident.content_hash
            return
        new_id = record.id
        xmp = ident.xmp_id
        if xmp is not None and xmp not in (record.id, record.xmp_id):
            source = state.missing_by_xmp(xmp)
            if source is not None:
                # Another document was moved onto this path and replaced the file that was here.
                self._move(source, rel, ident, state)
                state.freed.add(record.id)
                state.stats.removed += 1
                state.events.append(
                    ("external_delete", {"doc_id": record.id, "path": record.path, "title": record.title})
                )
                return
            if not index.id_exists(xmp):
                new_id = xmp
            elif not is_provisional(record.id):
                # The file is now a copy of a document that lives elsewhere.
                new_id = self._free_provisional(ident.content_hash)
        if new_id != record.id:
            index.change_id(record.id, new_id)
            state.freed.add(record.id)
        status = self._initial_status(rel)
        # The old text stays searchable until the new one is extracted.
        updated = self._build_record(rel, ident, new_id, page_count=record.page_count, text_status=status)
        index.upsert(updated)
        state.stats.updated += 1
        if status == "pending":
            state.text[rel] = ident.content_hash
        log.debug("document %s at %s changed", new_id, rel)

    def _apply_new(self, rel: str, ident: _Identity, state: _ApplyState) -> None:
        source = state.missing_by_xmp(ident.xmp_id) if ident.xmp_id else None
        if source is None:
            source = state.missing_by_hash(ident.content_hash, rel)
        if source is not None:
            self._move(source, rel, ident, state)
            return
        doc_id = self._choose_id(ident, rel)
        status = self._initial_status(rel)
        self.services.index.upsert(self._build_record(rel, ident, doc_id, page_count=0, text_status=status), [])
        state.stats.added += 1
        if status == "pending":
            state.text[rel] = ident.content_hash
        log.debug("new document %s at %s", doc_id, rel)

    def _move(self, source: DocumentRecord, rel: str, ident: _Identity, state: _ApplyState) -> None:
        del state.missing[source.id]
        if source.content_hash == ident.content_hash:
            status, page_count = source.text_status, source.page_count
        else:
            status, page_count = self._initial_status(rel), source.page_count
        record = self._build_record(rel, ident, source.id, page_count=page_count, text_status=status)
        self.services.index.upsert(record)
        state.stats.moved += 1
        state.events.append(("external_move", {"doc_id": source.id, "from": source.path, "to": rel}))
        if status == "pending":
            state.text[rel] = ident.content_hash
        log.debug("document %s moved from %s to %s", source.id, source.path, rel)

    def _choose_id(self, ident: _Identity, rel: str) -> str:
        if ident.xmp_id:
            owner = self.services.index.get(ident.xmp_id)
            if owner is None or owner.path == rel:
                return ident.xmp_id
            # A copy (e.g. made in a file manager) of a document that still exists.
        return self._free_provisional(ident.content_hash)

    def _free_provisional(self, content_hash: str, *, keep: str | None = None) -> str:
        base = provisional_id(content_hash)
        candidate, n = base, 2
        while candidate != keep and self.services.index.id_exists(candidate):
            candidate = f"{base}-{n}"
            n += 1
        return candidate

    def _promote_copies(self, freed: set[str]) -> None:
        """Give a copy the document id from its XMP once the original no longer uses it."""
        index = self.services.index
        for doc_id in sorted(freed):
            if index.id_exists(doc_id):
                continue
            copies = sorted((r for r in index.by_xmp_id(doc_id) if r.id != doc_id), key=lambda r: r.path)
            if copies:
                index.change_id(copies[0].id, doc_id)
                log.info("%s carries document id %s again", copies[0].path, doc_id)

    def _text_jobs(self, state: _ApplyState, *, verify: bool) -> list[_TextJob]:
        statuses = ("pending", "error") if verify else ("pending",)
        wanted = dict(state.text)
        for path, content_hash in self.services.index.paths_with_text_status(statuses):
            wanted.setdefault(path, content_hash)
        return [_TextJob(path, content_hash, statuses) for path, content_hash in sorted(wanted.items())]

    def _extract_jobs(self, jobs: list[_TextJob], stats: ScanStats, cancel: threading.Event | None) -> None:
        """Extract the text of new and changed documents, one at a time under the lock."""
        for job in jobs:
            if cancel is not None and cancel.is_set():
                return
            with self.services.archive_lock:
                try:
                    self._extract_job(job, stats)
                except Exception:
                    # One broken document must not hold up the others; it stays
                    # pending and the next pass tries again.
                    stats.errors += 1
                    log.exception("cannot store the text of %s", job.path)

    def _extract_job(self, job: _TextJob, stats: ScanStats) -> None:
        index = self.services.index
        record = index.get_by_path(job.path)
        if record is None or record.content_hash != job.content_hash or record.text_status not in job.statuses:
            return
        path = self._abs(record.path)
        try:
            before = os.lstat(path)
            if not _record_matches(before, record):
                return  # changed again; the next pass takes care of it
            pages, status = self._extract_text(path, record.ext)
            after = os.lstat(path)
        except FileNotFoundError:
            return  # moved or deleted meanwhile
        except OSError as exc:
            stats.errors += 1
            log.warning("cannot read %s: %s", record.path, exc)
            return
        if not _record_matches(after, record):
            return
        record.page_count = len(pages)
        record.text_status = status
        record.indexed_at = now_iso()
        index.upsert(record, pages)

    def _audit(self, events: list[tuple[str, dict[str, Any]]]) -> None:
        audit = getattr(self.services, "audit", None)
        if audit is None:
            return
        for action, details in events:
            audit.write(None, action, **details)

    # -- index_file helpers --------------------------------------------------------------------
    def _archive_rel(self, path: Path) -> str:
        layout = self.services.layout
        if not path.is_absolute():
            path = layout.archive / path
        try:
            rel = layout.rel_archive(path)
        except PathError:
            # The same location may be given through a different spelling of the
            # archive root (symlinked mount points); compare resolved directories.
            parent = os.path.realpath(path.parent)
            rel = os.path.relpath(os.path.join(parent, path.name), os.path.realpath(layout.archive))
            if rel == "." or rel.startswith(".."):
                raise
            rel = rel.replace(os.sep, "/")
        rel = check_relative(rel)
        if not rel:
            raise PathError("Der Archivordner selbst ist kein Dokument")
        if not _storable(rel):
            raise PathError("Der Dateiname enthält ungültige Zeichen")
        for part in rel.split("/"):
            if part.startswith(OWN_TEMP_PREFIX) or is_ignored(part):
                raise PathError(f"Diese Datei wird nicht indexiert: {rel}")
        return self._exact_rel(rel)

    def _exact_rel(self, rel: str) -> str:
        """``rel`` spelled exactly as stored on disk (Unicode normalization, case).

        Paths must match what a later walk reports, otherwise the next pass
        would see a vanished and a new document.
        """
        resolved: list[str] = []
        for part in rel.split("/"):
            directory = self._abs("/".join(resolved)) if resolved else self.services.layout.archive
            resolved.append(self._exact_name(directory, part))
        return "/".join(resolved)

    @staticmethod
    def _exact_name(directory: Path, name: str) -> str:
        try:
            with os.scandir(directory) as iterator:
                names = [entry.name for entry in iterator]
        except OSError:
            return name
        if name in names:
            return name
        key = unicodedata.normalize("NFC", name)
        same = [n for n in names if unicodedata.normalize("NFC", n) == key]
        if len(same) == 1:
            return same[0]
        folded = [n for n in names if unicodedata.normalize("NFC", n).casefold() == key.casefold()]
        if len(folded) == 1:
            return folded[0]
        return name

    def _file_exists_exactly(self, rel: str) -> bool:
        presence = _Listings(self.services.layout.archive).presence(rel, "file")
        # When it cannot be told, assume the file is still there: never take an
        # id away from a document that may still exist.
        return presence is not False

    def _index_file_id(self, rel: str, ident: _Identity, existing: DocumentRecord | None) -> str:
        index = self.services.index
        xmp = ident.xmp_id
        if existing is not None and (xmp is None or xmp in (existing.id, existing.xmp_id)):
            return existing.id
        if xmp is not None:
            owner = index.get(xmp)
            if owner is None or owner.path == rel or not self._file_exists_exactly(owner.path):
                return xmp
            # A copy of a document that still exists elsewhere.
            if existing is not None and is_provisional(existing.id):
                return existing.id
            return self._free_provisional(ident.content_hash)
        # No id in the file. A file the caller moved keeps the id of its old record.
        preferred = provisional_id(ident.content_hash)
        for record in sorted(index.by_content_hash(ident.content_hash), key=lambda r: (r.id != preferred, r.path)):
            if record.path != rel and not self._file_exists_exactly(record.path):
                return record.id
        return self._free_provisional(ident.content_hash)
