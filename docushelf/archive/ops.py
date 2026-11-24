"""Changes the application makes to the archive.

Everything DocuShelf writes into the archive goes through :class:`ArchiveOps`:
filing inbox items, editing metadata, moving and deleting documents and
managing folders. The ground rules:

* Every mutation holds ``services.archive_lock``, so a reconciliation pass
  never sees a half-done change.
* Nothing is ever overwritten. New names are created with hard links or
  exclusive creates; a taken name gets a collision suffix `` (2)``.
* Transient errors (file busy or locked) are retried a few times.
* Indexing problems never undo a change on disk. The index is a cache; if it
  cannot be updated right away, the next reconciliation pass catches up.
* Digitally signed and encrypted PDFs keep their bytes. They are filed as
  they are and only renamed or moved later; their title and date live in the
  file name alone.
* Folders that exist are used as they are spelled on disk; only folders that
  have to be created get sanitized names.

Filing an inbox item is a small transaction with a journal in ``item.json``
(``item.filing``)::

    publishing ──(file is in the archive)──▶ published ──(inbox tidied up)──▶ done

``publishing`` is saved before the file is published and carries the SHA-256
of the prepared file. If the process dies in between, :meth:`ArchiveOps.recover`
looks for that file in the target folder and either completes the filing or
resets the journal so the item can be filed again. The processed document is
only removed from the inbox after the archived copy is durable, and the
originals stay in ``eingang/erledigt``.

An automatic filing (see :meth:`docushelf.inbox.pipeline.Pipeline.auto_file`)
carries the record of its decision in the journal (``auto``). Completing the
filing writes it to the audit log (action ``auto_file``) and to
``item.context["auto"]``, exactly once, also when the filing is completed by
``recover()`` after a crash.

The module-level helpers are shared with :mod:`docushelf.archive.trash`.
"""

from __future__ import annotations

import dataclasses
import errno
import logging
import os
import re
import shutil
import stat
import time
import unicodedata
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, TypeVar

import pikepdf

from ..errors import Conflict, DocuShelfError, NotFound, PathError, ProcessingError, UnsupportedFile
from ..inbox.items import InboxItem
from ..layout import Layout
from ..models import Analysis, DocMeta, DocumentRecord, FilingRequest, MetadataUpdate
from ..pdf.text import page_count
from ..pdf.xmp import merge_meta, pdf_protection, read_meta, write_meta, xml_safe
from ..util import fsops
from ..util.ids import is_item_id, is_provisional, new_doc_id, provisional_id
from ..util.names import (
    OWN_TEMP_PREFIX,
    casefold_key,
    check_relative,
    format_filename,
    is_ignored,
    nfc,
    parse_filename,
    sanitize_component,
    split_folder,
    unique_filename,
    with_collision_suffix,
)
from ..util.timeutil import now_iso, parse_date, parse_iso_datetime, timezone

if TYPE_CHECKING:  # pragma: no cover
    from ..services import Services

log = logging.getLogger(__name__)

T = TypeVar("T")

# Transient errors (file busy or locked, e.g. opened by an SMB client) are
# retried this many times after the first attempt, with this pause in between.
RETRY_ATTEMPTS = 3
RETRY_PAUSE = 0.5
TRANSIENT_ERRNOS = frozenset({errno.EBUSY, errno.EACCES, errno.ETXTBSY})

# Name of the prepared file (document + metadata) in the item's work directory.
FILING_FILE = "filing.pdf"
# Journal states of a filing that has not completed.
UNFINISHED = ("publishing", "published")

# Temporary files in the inbox are left over from a crash once they are this
# old. In ``eingang/arbeit`` the pipeline may legitimately hold temporary files
# during a long OCR run, so the limit there is much larger.
STALE_TEMP_SECONDS = 60
STALE_WORK_TEMP_SECONDS = 3600

MODIFIED_MESSAGE = "Datei wurde zwischenzeitlich geändert. Bitte neu laden und erneut versuchen."

# Files and folders that operating systems create on their own. A folder that
# contains nothing else counts as empty when it is deleted.
_JUNK_FILES = frozenset({".ds_store", "thumbs.db", "ehthumbs.db", "desktop.ini", ".localized", "icon\r", ".directory"})
_JUNK_DIRS = frozenset({"@eadir", ".appledouble"})

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_EXTRA_MAX_CHARS = 500
# Notes and custom fields from Paperless-ngx are user-written content: they are
# kept (almost) completely instead of being cut like the other extras.
_LONG_EXTRAS = frozenset({"PaperlessNotes", "PaperlessFields"})
_LONG_EXTRA_MAX_CHARS = 20_000

# Signed or encrypted PDFs are filed byte for byte. What they cannot carry and
# exists nowhere else stays in the item.json of the filed inbox item (the trash
# does not purge such items).
UNSTORABLE_EXTRAS = ("PaperlessNotes", "PaperlessFields", "ArchiveSerialNumber")
_UNSTORED_LABELS = {"PaperlessNotes": "Notizen", "PaperlessFields": "eigene Felder", "ArchiveSerialNumber": "Archivnummer"}
_PROTECTED_FILING_NOTES = {
    "signed": "Das PDF ist digital signiert und wird unverändert abgelegt, damit die Signatur gültig bleibt.",
    "encrypted": "Das PDF ist verschlüsselt und wird unverändert abgelegt.",
}
_PROTECTED_EDIT_MESSAGES = {
    "signed": "Schlagwörter und Absender lassen sich in einem digital signierten PDF nicht speichern, "
    "ohne die Signatur ungültig zu machen. Titel, Datum und Ordner lassen sich trotzdem ändern.",
    "encrypted": "Schlagwörter und Absender lassen sich in einem verschlüsselten PDF nicht speichern. "
    "Titel, Datum und Ordner lassen sich trotzdem ändern.",
}


# ---------------------------------------------------------------------------
# Small helpers


def retry_transient(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Call ``fn``; retry when the file system reports a busy or locked file.

    Only use this for operations that either complete or change nothing.
    """
    attempt = 0
    while True:
        try:
            return fn(*args, **kwargs)
        except OSError as exc:
            if exc.errno not in TRANSIENT_ERRNOS or attempt >= RETRY_ATTEMPTS:
                raise
            attempt += 1
            log.info(
                "%s failed (%s), retry %d of %d",
                getattr(fn, "__name__", "file operation"),
                exc,
                attempt,
                RETRY_ATTEMPTS,
            )
            time.sleep(RETRY_PAUSE)


def fs_error(exc: OSError, action: str) -> DocuShelfError:
    """An application error with a German message for a failed file operation."""
    code = exc.errno
    if code in TRANSIENT_ERRNOS or code == errno.EPERM:
        return Conflict(f"{action}: Die Datei ist gesperrt oder schreibgeschützt. Bitte später erneut versuchen.")
    if code == errno.ENOSPC or code == getattr(errno, "EDQUOT", None):
        return ProcessingError(f"{action}: Kein freier Speicherplatz.", stage="archive")
    if code == errno.EROFS:
        return ProcessingError(f"{action}: Das Dateisystem ist schreibgeschützt.", stage="archive")
    if code == errno.ENAMETOOLONG:
        return PathError(f"{action}: Der Pfad ist zu lang.")
    if isinstance(exc, FileExistsError):
        return Conflict(f"{action}: Das Ziel existiert bereits.")
    if isinstance(exc, FileNotFoundError):
        return NotFound(f"{action}: Datei oder Ordner nicht gefunden.")
    return ProcessingError(f"{action}: {exc.strerror or exc}", stage="archive")


def clean_text(value: object) -> str:
    """Single-line text for metadata: NFC, no control characters, single spaces.

    Characters XML cannot carry (lone surrogates, U+FFFE, U+FFFF) are dropped,
    so the text can always be written into XMP.
    """
    text = xml_safe("" if value is None else str(value))
    text = unicodedata.normalize("NFC", text)
    text = _CONTROL_RE.sub(" ", text)
    return " ".join(text.split())


def clean_tags(tags: list[str] | None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for tag in tags or []:
        value = clean_text(tag)
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            result.append(value)
    return result


def parent_folder(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def join_rel(folder: str, name: str) -> str:
    return f"{folder}/{name}" if folder else name


def _describe(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


def _unlink_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("cannot remove %s", path, exc_info=True)


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.abspath(a) == os.path.abspath(b)


def _is_own_temp(name: str) -> bool:
    return name.startswith(OWN_TEMP_PREFIX) and name.endswith(".part")


def remove_stale_temp_files(directory: Path, max_age: float, *, now: float | None = None) -> int:
    """Delete our own ``.docushelf-*.part`` files in ``directory`` that are older than ``max_age`` seconds."""
    now = time.time() if now is None else now
    removed = 0
    try:
        entries = list(os.scandir(directory))
    except (FileNotFoundError, NotADirectoryError):
        return 0
    for entry in entries:
        if not _is_own_temp(entry.name):
            continue
        try:
            if not entry.is_file(follow_symlinks=False) or now - entry.stat(follow_symlinks=False).st_mtime < max_age:
                continue
            os.unlink(entry.path)
            removed += 1
        except FileNotFoundError:
            continue
        except OSError:
            log.warning("cannot remove stale temporary file %s", entry.path, exc_info=True)
    return removed


def context_extras(context: dict[str, Any] | None) -> dict[str, str]:
    """XMP extra properties taken over from an inbox item's context (mail, Paperless import).

    Paperless notes become ``PaperlessNotes`` ("YYYY-MM-DD Name: Text", joined
    with " | ") and custom field values ``PaperlessFields`` ("Feldname: Wert",
    joined with "; "), so they live on in the archived file.
    """
    context = context or {}
    paperless = context.get("paperless") if isinstance(context.get("paperless"), dict) else {}
    extras: dict[str, str] = {}

    def put(key: str, *values: object) -> None:
        limit = _LONG_EXTRA_MAX_CHARS if key in _LONG_EXTRAS else _EXTRA_MAX_CHARS
        for value in values:
            if value is None or isinstance(value, (dict, list, tuple, set, bool)):
                continue
            text = clean_text(value)[:limit].rstrip()
            if text:
                extras[key] = text
                return

    put("MailSubject", context.get("mail_subject"))
    put("MailFrom", context.get("mail_from"))
    put(
        "ArchiveSerialNumber",
        context.get("archive_serial_number"),
        context.get("asn"),
        paperless.get("archive_serial_number"),
        paperless.get("asn"),
    )
    put("PaperlessId", context.get("paperless_id"), paperless.get("id"), paperless.get("document_id"), paperless.get("pk"))
    put("PaperlessNotes", " | ".join(_paperless_notes(paperless.get("notes"))))
    put("PaperlessFields", "; ".join(_paperless_fields(paperless.get("custom_fields"))))
    return extras


def _paperless_notes(notes: object) -> list[str]:
    """Notes of a Paperless document as "YYYY-MM-DD Name: Text" (date and name when known)."""
    entries: list[str] = []
    for note in notes if isinstance(notes, list) else []:
        if not isinstance(note, dict):
            text = clean_text(note) if isinstance(note, str) else ""
            if text:
                entries.append(text)
            continue
        text = clean_text(note.get("text") or note.get("note"))
        if not text:
            continue
        prefix = " ".join(part for part in (_note_day(note.get("created")), clean_text(note.get("user"))) if part)
        entries.append(f"{prefix}: {text}" if prefix else text)
    return entries


def _note_day(value: object) -> str:
    """Local calendar day of a Paperless timestamp ("2024-03-01T09:15:00Z" → "2024-03-01")."""
    if not isinstance(value, str) or not value.strip():
        return ""
    moment = parse_iso_datetime(value.strip())
    if moment is not None:
        return moment.astimezone(timezone()).date().isoformat()
    day = parse_date(value)
    return day.isoformat() if day else ""


def _paperless_fields(fields: object) -> list[str]:
    """Custom field values of a Paperless document as "Feldname: Wert"."""
    entries: list[str] = []
    for field in fields if isinstance(fields, list) else []:
        if not isinstance(field, dict):
            continue
        value = _field_value(field.get("value"))
        if not value:
            continue
        name = clean_text(field.get("name"))
        entries.append(f"{name}: {value}" if name else value)
    return entries


def _field_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "ja" if value else "nein"
    if isinstance(value, (list, tuple)):
        return ", ".join(part for part in (_field_value(v) for v in value) if part)
    if isinstance(value, dict):
        return ""
    return clean_text(value)


def _text_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [text for text in (clean_text(entry) for entry in value) if text]


def auto_filing_record(auto: dict[str, Any] | None) -> dict[str, Any] | None:
    """The record of an automatic decision as kept in the filing journal (plain JSON values).

    Known keys are normalized: ``decision`` is "file", ``confidence`` a
    number in 0..1, ``review`` a bool, ``sources`` and ``reasons`` lists of
    text, ``at`` the time of the decision.
    """
    if not auto:
        return None
    record: dict[str, Any] = {}
    for key, value in auto.items():
        if value is None or isinstance(value, (str, int, float, bool)):
            record[str(key)] = value
        elif isinstance(value, (list, tuple)):
            record[str(key)] = _text_list(value)
    try:
        confidence = float(auto.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    record["decision"] = "file"
    record["confidence"] = round(min(1.0, max(0.0, confidence)), 3)
    record["review"] = bool(auto.get("review"))
    record["sources"] = _text_list(auto.get("sources"))
    record["reasons"] = _text_list(auto.get("reasons"))
    if not isinstance(record.get("at"), str) or not record.get("at"):
        record["at"] = now_iso()
    return record


def protected_filing_note(kind: str, *, tags: list[str], sender: str | None, unstored: list[str]) -> str:
    """German note for the item log when a signed or encrypted PDF is filed unchanged."""
    parts = [
        _PROTECTED_FILING_NOTES.get(kind, "Das PDF ist geschützt und wird unverändert abgelegt."),
        "Titel und Datum stehen nur im Dateinamen.",
    ]
    if tags or sender:
        parts.append("Schlagwörter und Absender lassen sich in dieser Datei nicht speichern.")
    labels = [_UNSTORED_LABELS[key] for key in unstored if key in _UNSTORED_LABELS]
    if labels:
        listed = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " und " + labels[-1]
        parts.append(
            f"{listed} aus Paperless-ngx lassen sich darin ebenfalls nicht speichern "
            "und bleiben deshalb im erledigten Eingang erhalten."
        )
    return " ".join(parts)


def _write_via_temp(dst: Path, write: Callable[[Path], None]) -> None:
    """Let ``write`` fill a temporary file next to ``dst``, then rename it to ``dst``.

    ``dst`` is always complete when it exists (written, fsynced, renamed).
    """
    tmp = fsops.temp_name_for(dst)
    try:
        write(tmp)
        fsops.fsync_file(tmp)
        os.replace(tmp, dst)
    except BaseException:
        _unlink_quietly(tmp)
        raise
    fsops.fsync_dir(dst.parent)


def write_pdf_with_meta(src: Path, dst: Path, meta: DocMeta) -> None:
    """Write ``src`` with ``meta`` to ``dst`` via a temporary file."""
    _write_via_temp(dst, lambda tmp: write_meta(src, tmp, meta))


def copy_unchanged(src: Path, dst: Path) -> None:
    """Copy ``src`` byte for byte to ``dst`` via a temporary file."""
    _write_via_temp(dst, lambda tmp: fsops.copy_file_fsync(src, tmp))


# ---------------------------------------------------------------------------
# Moving files without overwriting anything


def move_exact(src: Path, dst: Path) -> None:
    """Move a file to ``dst``, which must not exist. Never replaces anything.

    The new name is created first (hard link, or a durable copy on another file
    system), then the old name is removed. If the old name cannot be removed,
    the new one is dropped again, so exactly one copy remains.
    Raises FileExistsError if ``dst`` exists.
    """
    retry_transient(fsops.place_exact, src, dst, move=False)
    try:
        retry_transient(os.unlink, src)
    except FileNotFoundError:
        # Removed by someone else in the meantime: the new copy is the only one left.
        log.warning("%s vanished while it was being moved to %s", src, dst)
    except Exception:
        if os.path.lexists(src):
            _unlink_quietly(dst)
            fsops.fsync_dir(dst.parent)
        raise
    fsops.fsync_dir(src.parent)


def publish_move(src: Path, dst_dir: Path, filename: str) -> Path:
    """Move ``src`` into ``dst_dir`` as ``filename`` or the first free ``filename (n)``."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    taken: set[str] = set()
    for _ in range(10_000):
        name = unique_filename(dst_dir, filename, taken=taken)
        target = dst_dir / name
        try:
            move_exact(src, target)
        except FileExistsError:
            taken.add(casefold_key(name))
            continue
        return target
    raise Conflict(f"Kein freier Dateiname für „{filename}“")


def _free_name(directory: Path, filename: str, *, own_name: str | None, taken: set[str]) -> str:
    """First free variant of ``filename`` in ``directory``, ignoring the file ``own_name`` itself."""
    counts: dict[str, int] = {}
    with os.scandir(directory) as entries:
        for entry in entries:
            key = casefold_key(entry.name)
            counts[key] = counts.get(key, 0) + 1
    if own_name is not None:
        own_key = casefold_key(own_name)
        counts[own_key] = counts.get(own_key, 1) - 1
    occupied = {key for key, count in counts.items() if count > 0} | taken
    candidate = filename
    n = 2
    while casefold_key(candidate) in occupied:
        candidate = with_collision_suffix(filename, n)
        n += 1
    return candidate


def _respell(src: Path, target: Path) -> bool:
    """Rename ``src`` to ``target`` when the names differ only in case or Unicode form.

    Returns False if ``target`` is a different, existing file.
    """
    try:
        retry_transient(os.link, src, target)
    except FileExistsError:
        try:
            same = os.path.samefile(src, target)
        except OSError:
            same = False
        if not same:
            return False
        # Case-insensitive file system: both spellings are the same entry.
        retry_transient(os.rename, src, target)
        fsops.fsync_dir(target.parent)
        return True
    except OSError as exc:
        if exc.errno in TRANSIENT_ERRNOS:
            raise
        # No hard links here; fall back to a checked rename.
        if os.path.lexists(target) and not os.path.samefile(src, target):
            return False
        retry_transient(os.rename, src, target)
        fsops.fsync_dir(target.parent)
        return True
    # Case-sensitive file system: both names exist now; drop the old one.
    try:
        retry_transient(os.unlink, src)
    except Exception:
        _unlink_quietly(target)
        raise
    fsops.fsync_dir(target.parent)
    return True


def relocate_file(src: Path, target_dir: Path, filename: str) -> Path:
    """Rename and/or move an archive file without overwriting anything; returns the new path.

    Within the same folder the file does not collide with itself: renaming
    ``X (2).pdf`` to ``X.pdf`` keeps ``X (2).pdf`` if ``X.pdf`` is taken, and a
    change of case only ("rechnung" → "Rechnung") is a plain rename.
    """
    same_dir = _same_dir(src.parent, target_dir)
    if same_dir and src.name == filename:
        return src
    own_key = casefold_key(src.name) if same_dir else None
    taken: set[str] = set()
    for _ in range(10_000):
        name = _free_name(target_dir, filename, own_name=src.name if same_dir else None, taken=taken)
        if same_dir and name == src.name:
            return src
        target = target_dir / name
        if same_dir and casefold_key(name) == own_key:
            if _respell(src, target):
                return target
            taken.add(casefold_key(name))
            continue
        try:
            move_exact(src, target)
        except FileExistsError:
            taken.add(casefold_key(name))
            continue
        return target
    raise Conflict(f"Kein freier Dateiname für „{filename}“")


# ---------------------------------------------------------------------------
# Archive folders


def _existing_name(directory: Path, name: str) -> str | None:
    """How ``name`` is spelled in ``directory`` (exact match first, then ignoring case)."""
    key = casefold_key(name)
    candidates: list[str] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name == name:
                    return entry.name
                if casefold_key(entry.name) == key:
                    candidates.append(entry.name)
    except (FileNotFoundError, NotADirectoryError):
        return None
    if not candidates:
        return None
    candidates.sort()
    for candidate in candidates:
        if (directory / candidate).is_dir():
            return candidate
    return candidates[0]


def resolve_folder_case(root: Path, folder: str) -> str:
    """Spell ``folder`` like the folders that already exist.

    "finanzen/bank" becomes "Finanzen/Bank" when those folders exist. SMB
    clients treat names case-insensitively, so a second folder that differs
    only in case would confuse them (and the index stores paths exactly as on
    disk).
    """
    if not folder:
        return ""
    current = root
    parts: list[str] = []
    resolving = True
    for part in folder.split("/"):
        name = part
        if resolving:
            match = _existing_name(current, part)
            if match is None:
                resolving = False
            else:
                name = match
        parts.append(name)
        current = current / name
    return "/".join(parts)


def _loose_key(name: str) -> str:
    """Comparison key ignoring case, Unicode form, repeated whitespace and trailing dots/spaces."""
    return " ".join(nfc(name).split()).rstrip(" .").casefold()


def _reusable_name(name: str) -> bool:
    """Whether an existing entry may be reused as a folder name exactly as it is spelled.

    Hidden, system and temporary entries never are, nor names the path checks
    or the index could not handle.
    """
    if name.startswith(".") or "\\" in name or is_ignored(name) or _CONTROL_RE.search(name):
        return False
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _existing_component(directory: Path, wanted: str) -> str | None:
    """The entry of ``directory`` that ``wanted`` refers to, spelled as on disk.

    Matches are tried from strict to loose: exact, same Unicode form, same up
    to case, and finally ignoring repeated whitespace and trailing dots or
    spaces ("verein e.v" finds "Verein e.V.", "Kfz Versicherung" finds
    "Kfz  Versicherung"). Real directories win; a file or symbolic link of
    the same name (up to case) is returned too, so the caller refuses it
    instead of creating a confusing near-twin next to it.
    """
    try:
        with os.scandir(directory) as entries:
            found = [(entry.name, _entry_is_dir(entry)) for entry in entries if _reusable_name(entry.name)]
    except OSError:
        return None
    keys: tuple[Callable[[str], str], ...] = (
        lambda name: name,
        nfc,
        lambda name: casefold_key(name.strip()),
        _loose_key,
    )
    for key in keys:
        target = key(wanted)
        matches = sorted(name for name, is_dir in found if is_dir and key(name) == target)
        if matches:
            return matches[0]
    for key in keys[:3]:
        target = key(wanted)
        matches = sorted(name for name, is_dir in found if not is_dir and key(name) == target)
        if matches:
            return matches[0]
    return None


def _entry_is_dir(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def _is_real_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def resolve_folder(root: Path, raw: str) -> str:
    """Turn a folder path typed by a user (or taken from a suggestion) into a relative archive path.

    Components that exist below ``root`` are used verbatim, as spelled on
    disk, even if the sanitizer would write them differently ("Verein e.V.",
    "Meier & Co.", double spaces), so folders made in the Finder stay usable
    targets. Only components that have to be created are sanitized. "." and
    ".." are refused; hidden entries and symbolic links are never followed.
    """
    parts: list[str] = []
    current: Path | None = root
    for part in split_folder(raw):
        stripped = part.strip()
        if stripped in (".", ".."):
            raise PathError("Ungültiger Ordnername")
        if not stripped:
            continue
        name = _existing_component(current, part) if current is not None else None
        if name is None:
            name = sanitize_component(stripped)
            if current is not None:
                name = _existing_component(current, name) or name
        parts.append(name)
        if current is not None:
            candidate = current / name
            current = candidate if _is_real_dir(candidate) else None
    return "/".join(parts)


def _moved_folder(folder: object, old_keys: list[str], new: str) -> str | None:
    """``folder`` with the prefix ``old`` (given as casefolded components) replaced by ``new``.

    None if ``folder`` does not lie in ``old`` or would not change.
    """
    if not isinstance(folder, str) or not folder.strip("/"):
        return None
    parts = folder.strip("/").split("/")
    if len(parts) < len(old_keys) or [casefold_key(part) for part in parts[: len(old_keys)]] != old_keys:
        return None
    moved = "/".join([new, *parts[len(old_keys) :]])
    return moved if moved != folder else None


def archive_dir(layout: Layout, folder: str, *, create: bool = False, must_exist: bool = False) -> Path:
    """Absolute path of the archive folder ``folder`` ('' is the archive root).

    Every existing component must be a real directory, not a file or a
    symbolic link. With ``create`` missing components are created; with
    ``must_exist`` a missing folder raises NotFound.
    """
    if not layout.archive.is_dir():
        raise ProcessingError("Das Archiv ist nicht erreichbar.", stage="archive", transient=True)
    target = layout.archive_path(folder)
    current = layout.archive
    parts = folder.split("/") if folder else []
    for position, part in enumerate(parts):
        current = current / part
        shown = "/".join(parts[: position + 1])
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            if must_exist:
                raise NotFound(f"Der Ordner „{folder}“ existiert nicht.") from None
            if not create:
                break
            try:
                retry_transient(os.mkdir, current)
            except FileExistsError:
                st = os.lstat(current)
            else:
                fsops.fsync_dir(current.parent)
                continue
        if stat.S_ISLNK(st.st_mode):
            raise Conflict(f"„{shown}“ ist ein symbolischer Link und kein Ordner.")
        if not stat.S_ISDIR(st.st_mode):
            raise Conflict(f"„{shown}“ ist eine Datei und kein Ordner.")
    return target


# ---------------------------------------------------------------------------
# Index updates (best effort: the file system is the truth)


def request_scan(services: Any, reason: str) -> None:
    """Ask the archive watcher for a reconciliation pass, if there is one."""
    watcher = getattr(services, "watcher", None)
    if watcher is None:
        return
    try:
        watcher.request_scan(reason)
    except Exception:
        log.debug("cannot request a reconciliation pass", exc_info=True)


def ensure_folder_indexed(services: Any, folder: str) -> None:
    if not folder:
        return
    reconciler = getattr(services, "reconciler", None)
    try:
        if reconciler is not None and hasattr(reconciler, "ensure_folder"):
            reconciler.ensure_folder(folder)
        else:
            services.index.add_folder_with_parents(folder)
    except Exception:
        log.warning("cannot add folder %s to the index", folder, exc_info=True)


def forget_path(services: Any, rel: str) -> None:
    """Remove the document at ``rel`` from the index."""
    reconciler = getattr(services, "reconciler", None)
    try:
        if reconciler is not None and hasattr(reconciler, "forget"):
            reconciler.forget(rel)
        else:
            services.index.delete_path(rel)
    except Exception:
        log.warning("cannot remove %s from the index", rel, exc_info=True)
        request_scan(services, f"forget {rel}")


def record_from_file(
    layout: Layout,
    path: Path,
    *,
    doc_id: str | None = None,
    previous: DocumentRecord | None = None,
) -> DocumentRecord:
    """A DocumentRecord built straight from the file, for when the reconciler cannot index it."""
    rel = layout.rel_archive(path)
    st = os.stat(path)
    parsed = parse_filename(path.name)
    is_pdf = parsed.ext == ".pdf"
    meta = DocMeta()
    if is_pdf:
        try:
            meta = read_meta(path)
        except Exception:
            log.debug("cannot read metadata of %s", path, exc_info=True)
    if parsed.doc_date is not None:
        title, doc_date = parsed.title, parsed.doc_date
    else:
        title = meta.title if (meta.doc_id and meta.title) else parsed.title
        doc_date = meta.doc_date
    content_hash = fsops.sha256_file(path)
    if previous is not None:
        pages, text_status = previous.page_count, previous.text_status
    elif is_pdf:
        try:
            pages = page_count(path)
        except Exception:
            pages = 0
        text_status = "pending"
    else:
        pages, text_status = 0, "unsupported"
    return DocumentRecord(
        id=doc_id or meta.doc_id or provisional_id(content_hash),
        path=rel,
        folder=parent_folder(rel),
        filename=path.name,
        ext=parsed.ext,
        title=title,
        doc_date=doc_date,
        sender=meta.sender,
        tags=list(meta.tags),
        origin_hash=meta.origin_hash,
        content_hash=content_hash,
        xmp_id=meta.doc_id,
        has_xmp=bool(meta.doc_id),
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
        inode=st.st_ino,
        page_count=pages,
        text_status=text_status,
        indexed_at=now_iso(),
    )


def _unclaimed_id(services: Any, record: DocumentRecord) -> str:
    """``record.id``, or a provisional id if another live document already uses it."""
    try:
        other = services.index.get(record.id)
        if other is None or other.path == record.path:
            return record.id
        try:
            other_alive = services.layout.archive_path(other.path).exists()
        except PathError:
            other_alive = False
        if not other_alive:
            return record.id
        base = provisional_id(record.content_hash)
        candidate, n = base, 2
        while True:
            holder = services.index.get(candidate)
            if holder is None or holder.path == record.path:
                return candidate
            candidate = f"{base}-{n}"
            n += 1
    except Exception:
        return record.id


def index_archive_file(services: Any, path: Path, *, previous: DocumentRecord | None = None) -> DocumentRecord:
    """(Re)index one archive file now and return its record.

    ``previous`` is the record the file had before the application renamed,
    moved or rewrote it; the index entry (id and text) moves with the file.
    Indexing problems never undo a file operation: they are logged, a
    reconciliation pass is requested and a record built from the file is
    returned instead.
    """
    layout = services.layout
    index = services.index
    rel = layout.rel_archive(path)
    if previous is not None and previous.path != rel:
        interim = dataclasses.replace(
            previous,
            path=rel,
            folder=parent_folder(rel),
            filename=path.name,
            mtime_ns=0,  # forces a full re-read by the next pass if indexing below fails
            indexed_at=now_iso(),
        )
        try:
            index.upsert(interim)
        except Exception:
            log.warning("cannot move the index entry of %s to %s", previous.path, rel, exc_info=True)
    ensure_folder_indexed(services, parent_folder(rel))
    reconciler = getattr(services, "reconciler", None)
    if reconciler is not None:
        try:
            return reconciler.index_file(path)
        except Exception:
            log.exception("cannot index %s now; the next reconciliation pass will pick it up", rel)
    else:
        log.warning("no reconciler available to index %s", rel)
    record = record_from_file(layout, path, doc_id=previous.id if previous else None, previous=previous)
    if previous is None:
        record = dataclasses.replace(record, id=_unclaimed_id(services, record))
    try:
        # mtime 0 marks the entry as outdated, so the next pass reads the file completely.
        index.upsert(dataclasses.replace(record, mtime_ns=0))
    except Exception:
        log.warning("cannot store a provisional index entry for %s", rel, exc_info=True)
    request_scan(services, f"index {rel}")
    return record


# ---------------------------------------------------------------------------


class ArchiveOps:
    """All changes the application makes to the archive tree."""

    def __init__(self, services: "Services"):
        self.services = services

    # -- filing ----------------------------------------------------------------------
    def file_item(
        self,
        item_id: str,
        request: FilingRequest,
        *,
        user: str | None = None,
        auto: dict[str, Any] | None = None,
    ) -> DocumentRecord:
        """File a ready inbox item into the archive (see the module docstring).

        ``auto`` is given for automatic filings: the decision (confidence,
        review flag, supporting sources, reasons) that is kept in the journal,
        the audit log and the item.json of the filed item.
        """
        with self.services.archive_lock:
            try:
                return self._file_item(item_id, request, user, auto_filing_record(auto))
            except OSError as exc:
                raise fs_error(exc, "Ablage fehlgeschlagen") from exc

    def _file_item(
        self, item_id: str, request: FilingRequest, user: str | None, auto: dict[str, Any] | None = None
    ) -> DocumentRecord:
        inbox = self.services.inbox
        layout = self.services.layout

        # 1. Validate.
        item = self._fileable_item(item_id)
        title = self._clean_title(request.title)
        folder = self._clean_folder(request.folder)
        filename = self._filename(title, request.doc_date, ".pdf")
        archive_dir(layout, folder)  # no files or symlinks on the way
        document = item.document_path
        if not document.is_file():
            raise ProcessingError(
                "Das verarbeitete Dokument fehlt. Bitte das Dokument erneut verarbeiten lassen.", stage="filing"
            )
        tags = clean_tags(request.tags)
        sender = clean_text(request.sender) or None

        # 2. The file to publish: the processed document with metadata. A signed
        # or encrypted PDF is published byte for byte instead (a rewrite would
        # break the signature); its title and date live in the file name only.
        work = item.work_dir
        work.mkdir(parents=True, exist_ok=True)
        prepared = work / FILING_FILE
        _unlink_quietly(prepared)
        remove_stale_temp_files(work, 0)
        doc_id = new_doc_id()
        meta = self._filing_meta(item, title=title, doc_date=request.doc_date, tags=tags, sender=sender, doc_id=doc_id)
        protection = self._protection(item, document)
        try:
            if protection is None:
                try:
                    write_pdf_with_meta(document, prepared, meta)
                except UnsupportedFile:
                    protection = pdf_protection(document) or "protected"
            if protection is not None:
                copy_unchanged(document, prepared)
            digest = fsops.sha256_file(prepared)
            size = prepared.stat().st_size
        except Exception as exc:
            self._discard_prepared(item)
            if isinstance(exc, (pikepdf.PdfError, ValueError)):
                raise ProcessingError(
                    f"Das Dokument kann nicht mit Metadaten versehen werden: {exc}", stage="filing"
                ) from exc
            raise

        # 3. Journal.
        journal: dict[str, Any] = {
            "state": "publishing",
            "doc_id": doc_id,
            "folder": folder,
            "filename": filename,
            "sha256": digest,
            "size": size,
            "started_at": now_iso(),
            "user": user,
            "title": title,
            "doc_date": request.doc_date.isoformat() if request.doc_date else None,
            "tags": tags,
            "sender": sender,
            "create_rule": bool(request.create_rule),
        }
        if auto is not None:
            journal["auto"] = auto
        unstored: list[str] = []
        if protection is not None:
            journal["protected"] = protection
            unstored = [key for key in UNSTORABLE_EXTRAS if meta.extra.get(key)]
            if unstored:
                journal["unstored"] = unstored
        item.filing = journal
        try:
            inbox.save(item)
            inbox.log(item, f"Ablage nach „{join_rel(folder, filename)}“ begonnen")
            if protection is not None:
                inbox.log(item, protected_filing_note(protection, tags=tags, sender=sender, unstored=unstored))
            # 4. Publish; never replaces an existing file.
            target_dir = archive_dir(layout, folder, create=True)
            final = fsops.publish_no_clobber(prepared, target_dir, filename, move=True)
            journal.update(state="published", path=layout.rel_archive(final), published_at=now_iso())
            inbox.save(item)
        except Exception as exc:
            record = self._settle_interrupted_filing(item_id, exc)
            if record is not None:
                return record
            raise

        # 5. Index, audit, rule, tidy up the inbox.
        return self._finish_filing(item, final, recovered=False)

    def _fileable_item(self, item_id: str) -> InboxItem:
        item = self.services.inbox.find(item_id)
        if item is None:
            raise NotFound("Eingangsdokument nicht gefunden.")
        journal = item.filing if isinstance(item.filing, dict) else None
        status = journal.get("state") if journal else None
        if item.state == "erledigt" or status == "done":
            raise Conflict("Dieses Dokument wurde bereits abgelegt.")
        if status in UNFINISHED:
            raise Conflict(
                "Eine frühere Ablage dieses Dokuments wurde unterbrochen und ist noch nicht repariert. "
                "Sie wird bei der nächsten Wiederherstellung (spätestens beim Neustart) abgeschlossen oder zurückgesetzt."
            )
        if item.state == "arbeit":
            raise Conflict("Das Dokument wird noch verarbeitet.")
        if item.state == "fehler":
            raise Conflict("Die Verarbeitung dieses Dokuments ist fehlgeschlagen. Bitte zuerst erneut verarbeiten.")
        if item.stage != "ready":
            raise Conflict("Das Dokument ist noch nicht bereit zur Ablage.")
        return item

    def _filing_meta(
        self,
        item: InboxItem,
        *,
        title: str,
        doc_date: date | None,
        tags: list[str],
        sender: str | None,
        doc_id: str,
    ) -> DocMeta:
        existing = DocMeta()
        try:
            existing = read_meta(item.document_path)
        except Exception:
            log.debug("cannot read existing metadata of %s", item.document_path, exc_info=True)
        extra = {key: value for key, value in existing.extra.items() if value}
        extra.update(context_extras(item.context))
        return DocMeta(
            doc_id=doc_id,
            title=title,
            doc_date=doc_date,
            tags=tags,
            sender=sender,
            origin_hash=item.origin_hash,
            source=item.source,
            received_at=item.received_at,
            filed_at=now_iso(),
            extra=extra,
        )

    @staticmethod
    def _protection(item: InboxItem, document: Path) -> str | None:
        """Why the processed document must be filed byte for byte ("signed", "encrypted"), or None.

        The pipeline's verdict (``context["pdf_protection"]``) counts as well as
        what the file itself shows, so neither side alone can cause a rewrite.
        """
        flagged = item.context.get("pdf_protection") if isinstance(item.context, dict) else None
        if flagged in ("signed", "encrypted"):
            return str(flagged)
        return pdf_protection(document)

    def _settle_interrupted_filing(self, item_id: str, exc: BaseException) -> DocumentRecord | None:
        """After an error while publishing: complete the filing if the file made it, else roll back.

        Returns the record if the filing could be completed after all.
        """
        log.warning("filing of inbox item %s was interrupted: %s", item_id, _describe(exc))
        inbox = self.services.inbox
        try:
            item = inbox.load(item_id)
        except Exception:
            log.exception("cannot reload inbox item %s after a failed filing", item_id)
            return None
        journal = item.filing if isinstance(item.filing, dict) else None
        try:
            if journal is None or journal.get("state") not in UNFINISHED:
                # The journal never reached the disk, so nothing was published.
                self._discard_prepared(item)
                inbox.log(item, f"Ablage fehlgeschlagen: {_describe(exc)}")
                return None
            return self._recover_item(item, reason=_describe(exc))
        except Exception:
            log.exception("cannot settle the interrupted filing of %s; recover() will retry", item_id)
            return None

    def _finish_filing(self, item: InboxItem, path: Path, *, recovered: bool) -> DocumentRecord:
        """Step 5 of the filing. Idempotent; flags in the journal avoid repeated side effects."""
        layout = self.services.layout
        journal = item.filing
        if not isinstance(journal, dict):
            raise ProcessingError("Ablage ohne Journal kann nicht abgeschlossen werden.", stage="filing")
        rel = layout.rel_archive(path)
        try:
            return self._complete_filing(item, journal, path, rel, recovered=recovered)
        except Exception as exc:
            raise ProcessingError(
                f"Das Dokument wurde als „{rel}“ abgelegt, der Eingang konnte aber nicht aufgeräumt werden "
                f"({_describe(exc)}). Das wird bei der nächsten Wiederherstellung nachgeholt.",
                stage="filing",
            ) from exc

    def _complete_filing(
        self, item: InboxItem, journal: dict[str, Any], path: Path, rel: str, *, recovered: bool
    ) -> DocumentRecord:
        inbox = self.services.inbox
        if journal.get("state") != "published" or journal.get("path") != rel:
            journal.update(state="published", path=rel)
            journal.setdefault("published_at", now_iso())
        user = journal.get("user")

        record = index_archive_file(self.services, path)
        if not journal.get("audited"):
            self.services.audit.write(
                user,
                "file",
                item=item.id,
                doc_id=record.id,
                path=rel,
                title=journal.get("title"),
                source=item.source,
                recovered=True if recovered else None,
            )
            journal["audited"] = True
        auto = journal.get("auto") if isinstance(journal.get("auto"), dict) else None
        if auto is not None:
            self._record_auto_filing(item, journal, auto, record, rel, user)
        if journal.get("create_rule") and not journal.get("rule_done"):
            journal["rule_done"] = True
            self._create_rule(item, journal, user)
        inbox.save(item)

        # The archived copy is durable: the processed files are no longer needed.
        self._tidy_inbox_item(item)
        journal.update(state="done", finished_at=now_iso())
        if record.id != journal.get("doc_id"):
            journal["record_id"] = record.id
        item.add_history("filed", path=rel, doc_id=record.id, user=user)
        inbox.save(item)
        suffix = " (nach einer Unterbrechung abgeschlossen)" if recovered else ""
        verb = "Automatisch abgelegt" if auto is not None else "Abgelegt"
        inbox.log(item, f"{verb} als „{rel}“{suffix}")
        retry_transient(inbox.move, item, "erledigt")
        return record

    def _record_auto_filing(
        self,
        item: InboxItem,
        journal: dict[str, Any],
        auto: dict[str, Any],
        record: DocumentRecord,
        rel: str,
        user: str | None,
    ) -> None:
        """Audit entry ``auto_file`` (once per filing) and ``item.context["auto"]`` of an automatic filing."""
        if not journal.get("auto_audited"):
            self.services.audit.write(
                user,
                "auto_file",
                doc_id=record.id,
                path=rel,
                title=journal.get("title"),
                folder=parent_folder(rel),
                confidence=auto.get("confidence"),
                review=bool(auto.get("review")),
                sources=list(auto.get("sources") or []),
                reasons=list(auto.get("reasons") or []),
                item=item.id,
            )
            journal["auto_audited"] = True
        if isinstance(item.context, dict):
            item.context["auto"] = {**auto, "decision": "file", "doc_id": record.id, "path": rel}

    def _create_rule(self, item: InboxItem, journal: dict[str, Any], user: str | None) -> None:
        inbox = self.services.inbox
        rules = getattr(self.services, "rules", None)
        try:
            from ..suggest.rules import rule_from_filing
        except ImportError:
            rule_from_filing = None  # type: ignore[assignment]
        if rules is None or rule_from_filing is None:
            log.warning("rules are not available; no rule created for inbox item %s", item.id)
            journal["rule_error"] = "Regeln sind nicht verfügbar"
            inbox.log(item, "Regel konnte nicht angelegt werden: Regeln sind nicht verfügbar")
            return
        request = FilingRequest(
            folder=journal.get("folder") or "",
            title=journal.get("title") or "",
            doc_date=parse_date(journal.get("doc_date")),
            tags=list(journal.get("tags") or []),
            sender=journal.get("sender"),
            create_rule=True,
        )
        try:
            rule = rule_from_filing(request, Analysis.from_dict(item.analysis), item.source)
            rules.add(rule)
        except Exception as exc:
            log.warning("cannot create a rule from inbox item %s", item.id, exc_info=True)
            journal["rule_error"] = _describe(exc)
            inbox.log(item, f"Regel konnte nicht angelegt werden: {_describe(exc)}")
            return
        name = getattr(rule, "name", None)
        journal["rule_name"] = name
        inbox.log(item, f"Regel „{name}“ angelegt")
        self.services.audit.write(user, "create_rule", name=name, folder=request.folder, item=item.id)

    def _tidy_inbox_item(self, item: InboxItem) -> None:
        """Remove the processed files of a filed item; originals, item.json and log.txt stay."""
        for path in (item.document_path, item.text_path):
            try:
                retry_transient(os.unlink, path)
            except FileNotFoundError:
                pass
        if item.work_dir.exists():
            retry_transient(fsops.remove_tree, item.work_dir)

    def _discard_prepared(self, item: InboxItem) -> None:
        """Remove the prepared file of a filing that did not happen."""
        work = item.work_dir
        _unlink_quietly(work / FILING_FILE)
        remove_stale_temp_files(work, 0)
        try:
            if work.is_dir() and fsops.dir_is_empty(work):
                work.rmdir()
        except OSError:
            pass

    # -- recovery ---------------------------------------------------------------------
    def recover(self) -> int:
        """Complete or roll back filings that were interrupted; returns the number of repaired items.

        Also deletes stale temporary files in the inbox item directories.
        """
        repaired = 0
        inbox = self.services.inbox
        with self.services.archive_lock:
            for state in ("bereit", "arbeit", "fehler", "erledigt"):
                for item in list(inbox.iter_state(state)):
                    journal = item.filing if isinstance(item.filing, dict) else None
                    status = journal.get("state") if journal else None
                    try:
                        if status in UNFINISHED:
                            self._recover_item(item, reason="Wiederherstellung")
                            repaired += 1
                        elif status == "done" and item.state != "erledigt":
                            self._tidy_inbox_item(item)
                            retry_transient(inbox.move, item, "erledigt")
                            repaired += 1
                        elif state == "bereit" and (item.work_dir / FILING_FILE).exists():
                            # Prepared, but the journal never reached the disk.
                            self._discard_prepared(item)
                    except Exception:
                        log.exception("cannot recover the filing of inbox item %s", item.id)
            self._cleanup_inbox_temp_files()
        if repaired:
            log.warning("repaired %d interrupted filing(s)", repaired)
        return repaired

    def _recover_item(self, item: InboxItem, *, reason: str) -> DocumentRecord | None:
        """Complete an unfinished filing if its file is in the archive, otherwise reset it."""
        journal = item.filing if isinstance(item.filing, dict) else {}
        found = self._find_published(journal)
        if found is not None:
            log.info("completing interrupted filing of %s (%s)", item.id, found)
            return self._finish_filing(item, found, recovered=True)
        self._reset_filing(item, reason)
        return None

    def _find_published(self, journal: dict[str, Any]) -> Path | None:
        """The archived file of a journal: the recorded path or any file in the target folder
        with the journal's SHA-256 (falling back to the document id in its XMP)."""
        digest = journal.get("sha256")
        folder = journal.get("folder")
        if not digest or folder is None:
            return None
        layout = self.services.layout
        size = journal.get("size")
        doc_id = journal.get("doc_id")
        candidates: list[Path] = []
        if journal.get("path"):
            try:
                candidates.append(layout.archive_path(journal["path"]))
            except PathError:
                pass
        likely: list[Path] = []
        try:
            target_dir = layout.archive_path(folder)
            names = sorted(os.listdir(target_dir))
        except (PathError, OSError):
            names = []
            target_dir = None
        if target_dir is not None:
            stem_key = casefold_key(os.path.splitext(journal.get("filename") or "")[0])
            for name in names:
                if name.startswith(OWN_TEMP_PREFIX):
                    continue
                path = target_dir / name
                if stem_key and casefold_key(name).startswith(stem_key):
                    likely.append(path)
                candidates.append(path)
        if doc_id:
            try:
                record = self.services.index.get(doc_id)
                if record is not None:
                    candidates.append(layout.archive_path(record.path))
            except Exception:
                pass

        seen: set[str] = set()
        for path in candidates:
            if str(path) in seen:
                continue
            seen.add(str(path))
            try:
                st = os.lstat(path)
                if not stat.S_ISREG(st.st_mode) or (size is not None and st.st_size != size):
                    continue
                if fsops.sha256_file(path) == digest:
                    return path
            except OSError:
                continue
        # Changed since it was published (e.g. edited over SMB before the restart): match by id.
        if doc_id:
            for path in likely:
                try:
                    if path.suffix.lower() == ".pdf" and read_meta(path).doc_id == doc_id:
                        return path
                except Exception:
                    continue
        return None

    def _reset_filing(self, item: InboxItem, reason: str) -> None:
        """Forget an unfinished filing whose file never reached the archive."""
        inbox = self.services.inbox
        journal = item.filing if isinstance(item.filing, dict) else {}
        self._discard_prepared(item)
        self._remove_archive_temp_files(journal.get("folder"))
        item.filing = None
        item.add_history("filing_reset", reason=reason)
        auto = journal.get("auto")
        if isinstance(auto, dict) and isinstance(item.context, dict):
            # The item waits for a person now; say why it was not filed automatically.
            item.context["auto"] = {
                "decision": "hold",
                "confidence": auto.get("confidence") or 0.0,
                "reasons": ["Automatische Ablage wurde unterbrochen"],
                "at": now_iso(),
            }
        if item.state == "arbeit":
            inbox.save(item)
            inbox.log(item, "Unterbrochene Ablage zurückgesetzt.")
            return
        if item.stage == "ready" and item.document_path.is_file():
            inbox.save(item)
            inbox.log(item, "Die unterbrochene Ablage wurde zurückgesetzt. Das Dokument kann erneut abgelegt werden.")
            if item.state == "erledigt":
                retry_transient(inbox.move, item, "bereit")
            return
        message = "Die Ablage wurde unterbrochen und das verarbeitete Dokument fehlt. Bitte erneut verarbeiten."
        item.error = {"stage": "filing", "message": message, "at": now_iso()}
        inbox.save(item)
        inbox.log(item, message)
        if item.state != "fehler":
            retry_transient(inbox.move, item, "fehler")

    def _remove_archive_temp_files(self, folder: str | None) -> None:
        """Remove leftovers of an interrupted copy into ``folder``. Caller holds the archive lock."""
        if folder is None:
            return
        try:
            directory = self.services.layout.archive_path(folder)
        except PathError:
            return
        remove_stale_temp_files(directory, 0)

    def _cleanup_inbox_temp_files(self) -> int:
        layout = self.services.layout
        now = time.time()
        long_age = max(STALE_WORK_TEMP_SECONDS, int(getattr(self.services.settings, "ocr_timeout", 0)) + 600)
        removed = 0
        for state in ("arbeit", "bereit", "fehler", "erledigt"):
            base = layout.state_dir(state)
            max_age = long_age if state == "arbeit" else STALE_TEMP_SECONDS
            try:
                names = os.listdir(base)
            except FileNotFoundError:
                continue
            for name in names:
                if not is_item_id(name):
                    continue
                item_dir = base / name
                for directory in (item_dir, item_dir / "work"):
                    removed += remove_stale_temp_files(directory, max_age, now=now)
        if removed:
            log.info("removed %d stale temporary file(s) from the inbox", removed)
        return removed

    # -- documents ----------------------------------------------------------------------
    def update_document(self, doc_id: str, update: MetadataUpdate, *, user: str | None = None) -> DocumentRecord:
        """Change title, date, tags, sender and/or folder of an archived document.

        Signed and encrypted PDFs keep their bytes: title, date and folder
        changes only rename or move them, tag and sender changes raise
        UnsupportedFile with a German message.
        """
        with self.services.archive_lock:
            try:
                return self._update(doc_id, update, user=user, action="update")
            except OSError as exc:
                raise fs_error(exc, "Änderung fehlgeschlagen") from exc

    def move_document(self, doc_id: str, folder: str, *, user: str | None = None) -> DocumentRecord:
        with self.services.archive_lock:
            try:
                return self._update(doc_id, MetadataUpdate(folder=folder), user=user, action="move")
            except OSError as exc:
                raise fs_error(exc, "Verschieben fehlgeschlagen") from exc

    def delete_document(self, doc_id: str, *, user: str | None = None) -> str:
        """Move a document into the trash; returns the trash entry id."""
        with self.services.archive_lock:
            try:
                record = self._record(doc_id)
                path = self._document_path(record)
                return self.services.trash.put_document(path, record, user=user)
            except OSError as exc:
                raise fs_error(exc, "Löschen fehlgeschlagen") from exc

    def _record(self, doc_id: str) -> DocumentRecord:
        record = self.services.index.get(doc_id) if doc_id else None
        if record is None:
            raise NotFound("Dokument nicht gefunden.")
        return record

    def _document_path(self, record: DocumentRecord) -> Path:
        """The file of an indexed document; NotFound (and the index is corrected) if it is gone."""
        try:
            path = self.services.layout.archive_path(record.path)
        except PathError:
            raise NotFound("Dokument nicht gefunden.") from None
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            forget_path(self.services, record.path)
            request_scan(self.services, f"missing {record.path}")
            raise NotFound(f"„{record.path}“ existiert nicht mehr im Archiv.") from None
        if not stat.S_ISREG(st.st_mode):
            raise NotFound(f"„{record.path}“ ist keine normale Datei.")
        return path

    def _update(self, doc_id: str, update: MetadataUpdate, *, user: str | None, action: str) -> DocumentRecord:
        layout = self.services.layout
        record = self._record(doc_id)
        path = self._document_path(record)

        title = record.title if update.title is None else self._clean_title(update.title)
        if update.clear_date:
            doc_date = None
        elif update.doc_date is not None:
            doc_date = update.doc_date
        else:
            doc_date = record.doc_date
        tags = None if update.tags is None else clean_tags(update.tags)
        sender = None if update.sender is None else clean_text(update.sender)  # "" removes the sender
        folder = record.folder if update.folder is None else self._clean_folder(update.folder)

        changes: dict[str, Any] = {}
        if title != record.title:
            changes["title"] = title
        if doc_date != record.doc_date:
            changes["doc_date"] = doc_date.isoformat() if doc_date else None
        if tags is not None and tags != record.tags:
            changes["tags"] = tags
        if sender is not None and (sender or None) != (record.sender or None):
            changes["sender"] = sender or None
        if folder != record.folder:
            changes["folder"] = folder
        if not changes:
            return record

        needs_metadata = "tags" in changes or "sender" in changes
        if needs_metadata and not record.is_pdf:
            raise ProcessingError(
                "Schlagwörter und Absender lassen sich nur in PDF-Dateien speichern. "
                "Bei anderen Dateien können nur Titel, Datum und Ordner geändert werden.",
                stage="metadata",
            )

        old_id = record.id
        if record.is_pdf and (needs_metadata or "title" in changes or "doc_date" in changes):
            new_id = self._write_document_meta(
                record, path, title=title, doc_date=doc_date, tags=tags, sender=sender, required=needs_metadata
            )
            if new_id != record.id:
                try:
                    self.services.index.change_id(record.id, new_id)
                except Exception:
                    log.warning("cannot change index id %s to %s", record.id, new_id, exc_info=True)
                record = dataclasses.replace(record, id=new_id)

        new_path = path
        if "title" in changes or "doc_date" in changes or "folder" in changes:
            if "title" in changes or "doc_date" in changes:
                filename = self._filename(title, doc_date, record.ext)
            else:
                filename = path.name
            target_dir = archive_dir(layout, folder, create=True)
            new_path = relocate_file(path, target_dir, filename)

        result = index_archive_file(self.services, new_path, previous=record)
        self.services.audit.write(
            user,
            action,
            doc_id=result.id,
            previous_id=old_id if old_id != result.id else None,
            path=result.path,
            previous_path=record.path if record.path != result.path else None,
            changes=changes,
        )
        return result

    def _write_document_meta(
        self,
        record: DocumentRecord,
        path: Path,
        *,
        title: str,
        doc_date: date | None,
        tags: list[str] | None,
        sender: str | None,
        required: bool,
    ) -> str:
        """Write XMP into an archived PDF in place (never over a concurrent change).

        Returns the document id now stored in the file: a provisional id is
        replaced by a new UUID. If the PDF cannot carry metadata (signed,
        encrypted, damaged) and only title/date change, the file is left as it
        is and the rename alone carries the change; with ``required`` the
        error is raised. Signed and encrypted PDFs are never rewritten.
        """
        expected = fsops.file_signature(path)
        if expected != (record.size, record.mtime_ns):
            # The file changed after it was indexed (e.g. edited over SMB): the user saw stale data.
            index_archive_file(self.services, path)
            raise Conflict(MODIFIED_MESSAGE)
        protection = pdf_protection(path)
        if protection is not None:
            if required:
                raise UnsupportedFile(_PROTECTED_EDIT_MESSAGES[protection], stage="metadata")
            log.info("%s is %s; renaming it without rewriting the file", path, protection)
            return record.id
        try:
            current = read_meta(path)
        except Exception as exc:
            if required:
                raise ProcessingError(f"Die PDF-Datei kann nicht gelesen werden: {_describe(exc)}", stage="metadata") from exc
            log.warning("cannot read %s, renaming without metadata", path, exc_info=True)
            return record.id
        new_id = new_doc_id() if is_provisional(record.id) else record.id
        meta = merge_meta(
            current,
            doc_id=new_id,
            title=title,
            doc_date=doc_date,
            tags=list(tags) if tags is not None else list(current.tags),
            sender=(sender or None) if sender is not None else current.sender,
            # A file without origin hash gets the hash it had before the first edit,
            # so the same file arriving again is still recognised as a duplicate.
            origin_hash=current.origin_hash or record.content_hash,
        )
        tmp = fsops.temp_name_for(path)
        try:
            write_meta(path, tmp, meta)
            fsops.fsync_file(tmp)
            retry_transient(fsops.replace_if_unchanged, path, tmp, expected)
        except Conflict:
            raise Conflict(MODIFIED_MESSAGE) from None
        except UnsupportedFile:
            if required:
                raise
            log.warning("cannot write metadata into %s, renaming only", path)
            return record.id
        except (pikepdf.PdfError, ValueError) as exc:
            if required:
                raise ProcessingError(
                    f"Die Metadaten können nicht in die PDF-Datei geschrieben werden: {_describe(exc)}",
                    stage="metadata",
                ) from exc
            log.warning("cannot write metadata into %s, renaming only", path, exc_info=True)
            return record.id
        finally:
            if os.path.lexists(tmp):
                _unlink_quietly(tmp)
        return new_id

    # -- folders ---------------------------------------------------------------------------
    def create_folder(self, rel: str, *, user: str | None = None) -> str:
        """Create a folder (and missing parents); returns its relative path."""
        with self.services.archive_lock:
            layout = self.services.layout
            folder = self._clean_folder(rel)
            if not folder:
                raise PathError("Bitte einen Ordnernamen angeben.")
            path = layout.archive_path(folder)
            if os.path.lexists(path):
                if path.is_dir() and not path.is_symlink():
                    raise Conflict(f"Der Ordner „{folder}“ existiert bereits.")
                raise Conflict(f"„{folder}“ existiert bereits als Datei.")
            try:
                archive_dir(layout, folder, create=True)
            except OSError as exc:
                raise fs_error(exc, "Ordner anlegen fehlgeschlagen") from exc
            ensure_folder_indexed(self.services, folder)
            self.services.audit.write(user, "create_folder", folder=folder)
            return folder

    def rename_folder(self, rel: str, new_name: str, *, user: str | None = None) -> str:
        """Rename a folder in place (``os.rename``); returns the new relative path.

        Suggestions of inbox items waiting in ``bereit`` follow the folder (see
        :meth:`_retarget_suggestions`); remaining stale ones are recomputed.
        """
        with self.services.archive_lock:
            layout = self.services.layout
            old = resolve_folder_case(layout.archive, check_relative(rel))
            if not old:
                raise PathError("Der Archivordner selbst kann nicht umbenannt werden.")
            if "/" in (new_name or "") or "\\" in (new_name or ""):
                raise PathError("Der neue Name darf keinen Schrägstrich enthalten.")
            try:
                name = sanitize_component(new_name or "")
            except PathError:
                raise PathError("Bitte einen Ordnernamen angeben.") from None
            src = layout.archive_path(old)
            try:
                st = os.lstat(src)
            except FileNotFoundError:
                raise NotFound(f"Der Ordner „{old}“ existiert nicht.") from None
            if not stat.S_ISDIR(st.st_mode):
                raise NotFound(f"„{old}“ ist kein Ordner.")
            if name == src.name:
                return old
            parent = parent_folder(old)
            new_rel = join_rel(parent, name)
            try:
                for entry in os.listdir(src.parent):
                    if entry != src.name and casefold_key(entry) == casefold_key(name):
                        raise Conflict(f"„{join_rel(parent, entry)}“ existiert bereits.")
                dst = src.parent / name
                if os.path.lexists(dst) and not os.path.samefile(src, dst):
                    raise Conflict(f"„{new_rel}“ existiert bereits.")
                retry_transient(os.rename, src, dst)
                fsops.fsync_dir(src.parent)
            except OSError as exc:
                raise fs_error(exc, "Umbenennen fehlgeschlagen") from exc
            self._reindex_folder(old, new_rel)
            self.services.audit.write(user, "rename_folder", folder=old, new_folder=new_rel)
            self._retarget_suggestions(old, new_rel)
        self._refresh_stale_suggestions()
        return new_rel

    def _retarget_suggestions(self, old: str, new: str) -> int:
        """Let suggestions of items in ``bereit`` follow a renamed folder; returns the number of changed items.

        Folders of ``item.suggestions`` and of the stored import suggestion that
        lie in ``old`` (whole path components, ignoring case) are moved to
        ``new``. Items whose filing has not finished are left alone. Never
        fails the rename: problems are logged. Caller holds the archive lock.
        """
        inbox = self.services.inbox
        old_keys = [casefold_key(part) for part in old.split("/")]
        changed_items = 0
        try:
            items = list(inbox.iter_state("bereit"))
        except Exception:
            log.warning("cannot list inbox items to update their suggestions", exc_info=True)
            return 0
        for item in items:
            journal = item.filing if isinstance(item.filing, dict) else None
            if journal and journal.get("state") in UNFINISHED:
                continue
            changed = False
            targets: list[Any] = list(item.suggestions or [])
            if isinstance(item.context, dict):
                targets.append(item.context.get("import_suggestion"))
            for suggestion in targets:
                if not isinstance(suggestion, dict):
                    continue
                moved = _moved_folder(suggestion.get("folder"), old_keys, new)
                if moved is not None:
                    suggestion["folder"] = moved
                    changed = True
            if not changed:
                continue
            try:
                inbox.save(item)
                inbox.log(item, f"Ordner „{old}“ wurde in „{new}“ umbenannt – die Vorschläge zeigen jetzt dorthin")
                changed_items += 1
            except Exception:
                log.warning("cannot update the suggestions of inbox item %s", item.id, exc_info=True)
        return changed_items

    def _refresh_stale_suggestions(self) -> None:
        """Queue a recomputation of inbox suggestions whose folder does not exist (anymore)."""
        queue_refresh = getattr(getattr(self.services, "pipeline", None), "queue_refresh", None)
        if queue_refresh is None:
            return
        try:
            queue_refresh(only_stale=True)
        except Exception:
            log.warning("cannot queue the recomputation of stale suggestions", exc_info=True)

    def _reindex_folder(self, old: str, new: str) -> None:
        """Point folders and documents below ``old`` to ``new`` (files are unchanged by a rename)."""
        index = self.services.index
        try:
            index.rename_folder_tree(old, new)
            prefix = old + "/"
            records = [
                record
                for record in index.list_in_folder(old, recursive=True, limit=1_000_000_000)
                if record.folder == old or record.folder.startswith(prefix)
            ]
            with index.tx():
                for record in records:
                    index.upsert(
                        dataclasses.replace(
                            record,
                            path=new + record.path[len(old) :],
                            folder=new + record.folder[len(old) :],
                        )
                    )
        except Exception:
            log.exception("cannot update the index after renaming %s to %s", old, new)
            request_scan(self.services, f"rename {old}")

    def delete_folder(self, rel: str, *, user: str | None = None) -> None:
        """Delete an empty folder. Files created by operating systems (.DS_Store …) do not count."""
        with self.services.archive_lock:
            layout = self.services.layout
            folder = resolve_folder_case(layout.archive, check_relative(rel))
            if not folder:
                raise PathError("Der Archivordner selbst kann nicht gelöscht werden.")
            path = layout.archive_path(folder)
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                raise NotFound(f"Der Ordner „{folder}“ existiert nicht.") from None
            if not stat.S_ISDIR(st.st_mode):
                raise NotFound(f"„{folder}“ ist kein Ordner.")
            try:
                junk: list[os.DirEntry[str]] = []
                with os.scandir(path) as entries:
                    for entry in entries:
                        if not self._is_junk(entry):
                            raise Conflict(f"Der Ordner „{folder}“ ist nicht leer.")
                        junk.append(entry)
                for entry in junk:
                    if entry.is_dir(follow_symlinks=False):
                        shutil.rmtree(entry.path)
                    else:
                        os.unlink(entry.path)
                retry_transient(os.rmdir, path)
                fsops.fsync_dir(path.parent)
            except OSError as exc:
                if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                    raise Conflict(f"Der Ordner „{folder}“ ist nicht leer.") from exc
                raise fs_error(exc, "Ordner löschen fehlgeschlagen") from exc
            self._forget_folder(folder)
            self.services.audit.write(user, "delete_folder", folder=folder)

    @staticmethod
    def _is_junk(entry: os.DirEntry[str]) -> bool:
        name = entry.name.casefold()
        try:
            if entry.is_dir(follow_symlinks=False):
                return name in _JUNK_DIRS
            if not entry.is_file(follow_symlinks=False):
                return False
        except OSError:
            return False
        return name in _JUNK_FILES or entry.name.startswith("._") or _is_own_temp(entry.name)

    def _forget_folder(self, folder: str) -> None:
        index = self.services.index
        try:
            prefix = folder + "/"
            for record in index.list_in_folder(folder, recursive=True, limit=1_000_000_000):
                if record.folder == folder or record.folder.startswith(prefix):
                    index.delete(record.id)
            index.remove_folder_tree(folder)
        except Exception:
            log.warning("cannot remove folder %s from the index", folder, exc_info=True)
            request_scan(self.services, f"delete {folder}")

    # -- inbox -------------------------------------------------------------------------------
    def delete_inbox_item(self, item_id: str, *, user: str | None = None) -> str:
        """Move an inbox item (with its originals) into the trash; returns the trash entry id."""
        with self.services.archive_lock:
            item = self.services.inbox.find(item_id)
            if item is None:
                raise NotFound("Eingangsdokument nicht gefunden.")
            journal = item.filing if isinstance(item.filing, dict) else None
            if journal and journal.get("state") in UNFINISHED:
                raise Conflict("Für dieses Dokument wurde eine Ablage unterbrochen. Bitte zuerst die Wiederherstellung abwarten.")
            if item.state == "erledigt":
                raise Conflict("Das Dokument wurde bereits abgelegt und liegt im Archiv.")
            if item.state == "arbeit" and self._is_processing(item.id):
                raise Conflict("Das Dokument wird gerade verarbeitet. Bitte kurz warten und erneut versuchen.")
            try:
                return self.services.trash.put_inbox_item(item, user=user)
            except OSError as exc:
                raise fs_error(exc, "Löschen fehlgeschlagen") from exc

    def _is_processing(self, item_id: str) -> bool:
        try:
            return any(job.key == item_id and job.state == "running" for job in self.services.index.pending_jobs("process"))
        except Exception:
            return False

    # -- validation --------------------------------------------------------------------------
    @staticmethod
    def _clean_title(raw: str | None) -> str:
        title = clean_text(raw)
        if not title:
            raise PathError("Bitte einen Titel angeben.")
        return title

    def _clean_folder(self, raw: str | None) -> str:
        """A user-typed folder as a safe relative path, spelled like existing folders.

        Existing folders are used exactly as they are named on disk, also when
        the sanitizer would spell them differently (see :func:`resolve_folder`).
        """
        layout = self.services.layout
        folder = check_relative(resolve_folder(layout.archive, raw or ""))
        layout.archive_path(folder)  # rejects anything that escapes the archive
        return folder

    @staticmethod
    def _filename(title: str, doc_date: date | None, ext: str) -> str:
        try:
            return format_filename(title, doc_date, ext)
        except PathError:
            raise PathError("Aus dem Titel lässt sich kein gültiger Dateiname bilden.") from None
