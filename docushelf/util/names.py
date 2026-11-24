"""File and folder naming rules.

Archived files follow ``YYYY-MM-DD Title.pdf``. Names must stay valid on
Linux, macOS and Windows clients (the archive is often edited over SMB), so
characters that Windows rejects are replaced and comparisons for collisions
are case-insensitive.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..errors import PathError

# Prefix of temporary files the application itself creates next to targets.
OWN_TEMP_PREFIX = ".docushelf-"

_IGNORED_NAMES = {
    ".ds_store",
    "thumbs.db",
    "desktop.ini",
    "@eadir",
    "#recycle",
    "$recycle.bin",
    "system volume information",
    "lost+found",
    ".trashes",
    ".spotlight-v100",
    ".fseventsd",
    ".temporaryitems",
    ".apdisk",
    ".localized",
    "icon\r",
}
_TEMP_SUFFIXES = (
    ".tmp",
    ".temp",
    ".part",
    ".partial",
    ".crdownload",
    ".filepart",
    ".download",
    ".swp",
    ".lock",
    ".~lock",
)
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SPACES_RE = re.compile(r"\s+")
_DASH_RUN_RE = re.compile(r"(?:\s*-\s*){2,}")
_FILENAME_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})(?:[ _]+(?P<title>.*))?$")
_COLLISION_RE = re.compile(r"^(?P<base>.*\S) \((?P<n>[2-9]|[1-9]\d+)\)$")

MAX_COMPONENT_BYTES = 180


def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def casefold_key(name: str) -> str:
    """Key used to compare names the way case-insensitive clients do."""
    return nfc(name).casefold()


def is_ignored(name: str) -> bool:
    """True for hidden, system and temporary entries that are never documents."""
    if not name or name in (".", ".."):
        return True
    lowered = name.casefold()
    if name.startswith(".") or name.startswith("~$") or name.startswith("._"):
        return True
    if lowered in _IGNORED_NAMES:
        return True
    return lowered.endswith(_TEMP_SUFFIXES)


def _truncate_bytes(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    cut = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return cut.rstrip()


def sanitize_component(name: str, *, max_bytes: int = MAX_COMPONENT_BYTES) -> str:
    """Turn arbitrary text into a single safe path component.

    Raises PathError if nothing usable remains.
    """
    value = nfc(name or "")
    value = _CONTROL_RE.sub(" ", value)
    value = value.replace("/", "-").replace("\\", "-").replace("|", "-")
    value = value.replace(":", " -")
    for char in '*?"<>':
        value = value.replace(char, "")
    value = _SPACES_RE.sub(" ", value)
    value = _DASH_RUN_RE.sub(" - ", value)
    value = value.strip(" .")
    value = _truncate_bytes(value, max_bytes).strip(" .")
    if not value:
        raise PathError("Name ist leer")
    if value.casefold() in _WINDOWS_RESERVED or value.split(".")[0].casefold() in _WINDOWS_RESERVED:
        value = value + "_"
    if value.startswith("~$") or value.startswith("._"):
        value = value[2:].strip() or "Dokument"
    return value


def split_folder(rel: str) -> list[str]:
    return [part for part in (rel or "").replace("\\", "/").split("/") if part != ""]


def check_relative(rel: str) -> str:
    """Validate an existing relative archive path without changing it.

    Used for paths that come from the index or from the user and must refer to
    something that exists exactly like this. Rejects traversal, absolute paths
    and hidden components.
    """
    if rel is None:
        raise PathError("Pfad fehlt")
    rel = rel.replace("\\", "/").strip("/")
    if rel == "":
        return ""
    if rel.startswith("/") or os.path.isabs(rel):
        raise PathError("Absoluter Pfad ist nicht erlaubt")
    parts = rel.split("/")
    for part in parts:
        if part in ("", ".", "..") or part.startswith("."):
            raise PathError(f"Ungültiger Pfadbestandteil: {part!r}")
        if "\x00" in part:
            raise PathError("Ungültiges Zeichen im Pfad")
    return "/".join(parts)


def sanitize_folder(rel: str) -> str:
    """Normalize a folder path typed by a user ("Finanzen / Bank/") into "Finanzen/Bank".

    Every component is sanitized; "." and ".." are rejected. "" means the archive root.
    """
    parts = split_folder(rel)
    cleaned: list[str] = []
    for part in parts:
        stripped = part.strip()
        if stripped in (".", ".."):
            raise PathError("Ungültiger Ordnername")
        if stripped == "":
            continue
        cleaned.append(sanitize_component(stripped))
    return "/".join(cleaned)


def format_filename(title: str, doc_date: date | None, ext: str = ".pdf") -> str:
    """Build ``YYYY-MM-DD Title.ext`` (or ``Title.ext`` without date)."""
    if ext and not ext.startswith("."):
        ext = "." + ext
    ext = (ext or "").lower()
    reserve = len(ext.encode("utf-8")) + 12  # room for date prefix is added separately
    clean_title = sanitize_component(title, max_bytes=MAX_COMPONENT_BYTES - reserve)
    stem = f"{doc_date.isoformat()} {clean_title}" if doc_date else clean_title
    return stem + ext


@dataclass(frozen=True)
class ParsedName:
    doc_date: date | None
    title: str
    collision: int | None
    ext: str


def parse_filename(filename: str) -> ParsedName:
    """Read date and title from a file name following (or not following) the schema.

    ``2026-01-05 Stromrechnung (2).pdf`` → date 2026-01-05, title "Stromrechnung", collision 2.
    Names without a valid date prefix yield ``doc_date=None`` and the stem as title.
    """
    name = nfc(filename)
    stem, ext = os.path.splitext(name)
    if ext and not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", ext):
        stem, ext = name, ""
    doc_date: date | None = None
    title = stem.strip()
    match = _FILENAME_RE.match(title)
    if match:
        try:
            doc_date = date.fromisoformat(match.group("date"))
            title = (match.group("title") or "").strip(" -_")
        except ValueError:
            doc_date = None
    collision: int | None = None
    coll = _COLLISION_RE.match(title)
    if coll:
        title = coll.group("base")
        collision = int(coll.group("n"))
    if not title:
        title = doc_date.isoformat() if doc_date else stem
    return ParsedName(doc_date=doc_date, title=title, collision=collision, ext=ext.lower())


def with_collision_suffix(filename: str, n: int) -> str:
    stem, ext = os.path.splitext(filename)
    return f"{stem} ({n}){ext}"


def unique_filename(directory: Path, filename: str, *, taken: set[str] | None = None) -> str:
    """Return ``filename`` or the first free ``name (n).ext`` variant in ``directory``.

    Comparison is case-insensitive and Unicode-normalized because SMB clients
    treat "Rechnung.pdf" and "rechnung.pdf" as the same file.
    """
    existing: set[str] = set(taken or ())
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                existing.add(casefold_key(entry.name))
    except FileNotFoundError:
        pass
    if casefold_key(filename) not in existing:
        return filename
    n = 2
    while True:
        candidate = with_collision_suffix(filename, n)
        if casefold_key(candidate) not in existing:
            return candidate
        n += 1


# Names that scanners, cameras, phone apps and messengers give to files on their own.
_NOISE_PREFIX = (
    r"scan|scans|img|image|images|dsc|dscn|pxl|photo|foto|document|dokument|doc|file|datei|untitled|unbenannt|"
    r"scanned|scanfile|copy|kopie|attachment|anhang|screenshot|bildschirmfoto|"
    r"gescannte[s]?\s+dokument(?:e)?|scanned\s+document(?:s)?|adobe\s+scan|(?:microsoft|office)\s+lens|"
    r"camscanner|whatsapp\s+(?:image|bild|foto|photo)|new\s+doc(?:ument)?|neues\s+dokument"
)
_NOISE_MONTH = (
    r"(?:jan(?:uar|uary)?|j(?:ä|ae)nner|feb(?:r|ruar|ruary)?|m(?:ä|ae)rz|m(?:ä|a)r|march|apr(?:il)?|mai|may|"
    r"jun[ei]?|jul[iy]?|aug(?:ust)?|sep(?:t|tember)?|o[ck]t(?:ober)?|nov(?:ember)?|de[cz](?:ember)?)\.?"
)
# What such apps put after their name: dates, times, counters, hashes ("20. Nov. 2025",
# "2025-11-20 um 10.12.00", "(1)", "WA0003", "6b9c2a1f"). Every token can be read in
# one way only, so a long name that does not match fails fast.
_NOISE_TOKEN = (
    rf"(?:\d+(?:[.:/]\d+)*\.?|\(\d+\)|~\d+|{_NOISE_MONTH}|um|at|vom|uhr|[ap]\.?m\.?|wa\d+|"
    r"(?=[0-9a-f]*[a-f])(?=[a-f]*\d)[0-9a-f]{6,})"
)
_NOISE_RES = (
    # An app or device word, optionally followed by dates, times and counters only.
    re.compile(rf"(?:{_NOISE_PREFIX})(?:[\s\-]*{_NOISE_TOKEN}(?:[\s,\-]+{_NOISE_TOKEN})*)?[\s.,\-]*", re.IGNORECASE),
    # No app word: numbers only ("20261123123000", "0001 (2)").
    re.compile(r"[\d\s\-.]*(?:\(\d+\))?\s*"),
    # Screenshots on phones end with the name of the app that was shown.
    re.compile(rf"(?:screenshot|bildschirmfoto)(?:[\s\-]+{_NOISE_TOKEN})+\s+[^\W\d_]+", re.IGNORECASE),
)
# Camera time stamps; anything after them is a camera suffix ("PXL_20251120_101200123.MP").
_CAMERA_RE = re.compile(r"^(?:pxl|img|vid|mvimg|dsc)[\s\-]\d{8}[\s\-]\d{6}", re.IGNORECASE)
# Android stores WhatsApp images as "IMG-20251120-WA0003"; many scanner apps use "Scan_…".
_RAW_NOISE_RE = re.compile(r"^(?:img-\d{8}-wa\d+|scan_)", re.IGNORECASE)


def _is_noise_name(stem: str) -> bool:
    spaced = re.sub(r"\s+", " ", stem.replace("_", " ")).strip()
    if not spaced or _RAW_NOISE_RE.match(stem.strip()) or _CAMERA_RE.match(spaced):
        return True
    return any(regex.fullmatch(spaced) for regex in _NOISE_RES)


def title_from_original_name(filename: str) -> str | None:
    """Derive a usable title from an uploaded file name, or None for scanner noise.

    Names that scanners, cameras and phone apps choose themselves ("scan_0001.pdf",
    "IMG_2041.JPG", "20261123123000.pdf", "Gescanntes Dokument.pdf",
    "Adobe Scan 20. Nov. 2025.pdf", "WhatsApp Bild 2025-11-20 um 10.12.00.jpeg")
    carry no meaning and should not become titles.
    """
    stem = os.path.splitext(nfc(filename))[0].strip()
    if _is_noise_name(stem):
        return None
    parsed = parse_filename(nfc(filename))
    title = parsed.title.replace("_", " ").strip()
    if not title or _is_noise_name(title):
        return None
    return title
