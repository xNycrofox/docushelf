"""Page previews: PNG rendering with PDFium and a small on-disk thumbnail cache."""

from __future__ import annotations

import errno
import hashlib
import io
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pypdfium2 as pdfium
from PIL import Image

from ..errors import NotFound, ProcessingError, UnsupportedFile
from ..util import fsops
from ..util.names import OWN_TEMP_PREFIX
from . import PDFIUM_LOCK

log = logging.getLogger(__name__)

MIN_WIDTH = 64
MAX_WIDTH = 2000
# Upper bound for the pixels of one rendered page. Only very long pages (till
# receipts, plans) reach it; they are rendered narrower than requested.
MAX_PIXELS = 8_000_000

_SAFE_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_CACHE_FILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*-p\d+-w\d+\.png")
# Cache hits refresh the file's mtime at most this often, so pruning removes
# thumbnails that were not looked at for the longest time.
_TOUCH_INTERVAL = 24 * 3600
# Files written or touched this recently are never pruned (a request may be
# about to send them).
_PRUNE_GRACE = 60
_STALE_TEMP_AGE = 3600


def clamp_width(width: int) -> int:
    """The width actually used for a requested thumbnail width."""
    try:
        value = int(width)
    except (TypeError, ValueError):
        value = 300
    return max(MIN_WIDTH, min(MAX_WIDTH, value))


def _open_document(path: Path) -> pdfium.PdfDocument:
    """Open a PDF with PDFium (caller holds PDFIUM_LOCK)."""
    try:
        return pdfium.PdfDocument(Path(path))
    except FileNotFoundError:
        raise NotFound(f"Datei nicht gefunden: {Path(path).name}") from None
    except pdfium.PdfiumError as exc:
        if getattr(exc, "err_code", None) == pdfium.raw.FPDF_ERR_PASSWORD:
            # Not an error of the file: it simply cannot be shown without its password.
            raise UnsupportedFile("PDF ist passwortgeschützt – keine Vorschau möglich") from exc
        raise ProcessingError(f"PDF kann nicht geöffnet werden: {Path(path).name}") from exc
    except OSError as exc:
        raise ProcessingError(f"Datei kann nicht gelesen werden: {Path(path).name}") from exc


def render_page_image(path: Path, page: int, *, width: int = 300) -> Image.Image:
    """Render one page (1-based) to an RGB image ``width`` pixels wide.

    Page rotation is applied, so a landscape page comes out wider than tall.
    """
    width = clamp_width(width)
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise NotFound(f"Seite {page} existiert nicht")
    with PDFIUM_LOCK:
        document = _open_document(path)
        try:
            if page > len(document):
                raise NotFound(f"Seite {page} existiert nicht")
            pdf_page = document[page - 1]
            try:
                page_width, page_height = pdf_page.get_size()
                if page_width <= 0 or page_height <= 0:
                    raise ProcessingError(f"Seite {page} hat keine gültige Größe")
                scale = width / page_width
                if width * page_height * scale > MAX_PIXELS:
                    scale = (MAX_PIXELS / (page_width * page_height)) ** 0.5
                try:
                    bitmap = pdf_page.render(scale=scale, may_draw_forms=True)
                except (pdfium.PdfiumError, ValueError) as exc:
                    raise ProcessingError(f"Seite {page} kann nicht dargestellt werden") from exc
                try:
                    # to_pil() may share the bitmap's memory; copy before closing it.
                    image = bitmap.to_pil().convert("RGB")
                finally:
                    bitmap.close()
            finally:
                pdf_page.close()
        finally:
            document.close()
    return image


def render_page_png(path: Path, page: int, *, width: int = 300) -> bytes:
    """PNG preview of one page (1-based). ``width`` is clamped to 64..2000 pixels.

    Raises NotFound for a missing file or a page that does not exist,
    UnsupportedFile for PDFs that need a password and ProcessingError for
    files PDFium cannot open or render otherwise (both are DocuShelfErrors).
    """
    image = render_page_image(path, page, width=width)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=6)
    return buffer.getvalue()


class ThumbnailCache:
    """PNG previews on disk, keyed by a caller-chosen string.

    The key must change whenever the page content may change (a content hash,
    or an inbox item id plus the file's mtime). Files live at
    ``<cache>/<key[:2]>/<key>-p<page>-w<width>.png``; keys with characters
    outside ``[A-Za-z0-9_.-]`` are replaced by their SHA-256.
    """

    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)
        self._guard = threading.Lock()
        self._locks: dict[str, list] = {}  # target path -> [Lock, users]

    # -- paths -----------------------------------------------------------------
    @staticmethod
    def safe_key(key: str) -> str:
        key = str(key)
        if _SAFE_KEY_RE.fullmatch(key):
            return key
        return hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()

    def path_for(self, key: str, page: int, width: int) -> Path:
        """Where the thumbnail for these parameters is (or would be) stored."""
        safe = self.safe_key(key)
        return self.cache_dir / safe[:2] / f"{safe}-p{int(page)}-w{clamp_width(width)}.png"

    # -- access ------------------------------------------------------------------
    def get(self, path: Path, page: int, width: int, *, key: str) -> Path:
        """Return the cached PNG for ``page`` of ``path``, rendering it on a miss."""
        target = self.path_for(key, page, width)
        if self._usable(target):
            return target
        with self._exclusive(target):
            # Another thread may have rendered it while we waited.
            if self._usable(target):
                return target
            data = render_page_png(path, page, width=clamp_width(width))
            fsops.atomic_write_bytes(target, data)
            return target

    def _usable(self, target: Path) -> bool:
        try:
            st = os.stat(target)
        except OSError:
            return False
        if st.st_size <= 0:
            # Left behind by a crash before the data reached the disk.
            return False
        if time.time() - st.st_mtime > _TOUCH_INTERVAL:
            try:
                os.utime(target)
            except OSError:
                pass
        return True

    @contextmanager
    def _exclusive(self, target: Path) -> Iterator[None]:
        name = str(target)
        with self._guard:
            entry = self._locks.get(name)
            if entry is None:
                entry = self._locks[name] = [threading.Lock(), 0]
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._guard:
                entry[1] -= 1
                if entry[1] == 0:
                    self._locks.pop(name, None)

    # -- maintenance ---------------------------------------------------------------
    def prune(self, max_bytes: int = 500 * 1024 * 1024) -> int:
        """Delete the least recently used thumbnails until the cache fits ``max_bytes``.

        Also removes temporary files of interrupted writes. Files that do not
        look like cache entries are left alone. Returns the number of deleted files.
        """
        if not self.cache_dir.is_dir():
            return 0
        now = time.time()
        removed = 0
        entries: list[tuple[float, int, str]] = []
        total = 0
        for dirpath, _dirnames, filenames in os.walk(self.cache_dir):
            for name in filenames:
                full = os.path.join(dirpath, name)
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                if name.startswith(OWN_TEMP_PREFIX) and name.endswith(".part"):
                    if now - st.st_mtime > _STALE_TEMP_AGE and self._unlink(full) == "deleted":
                        removed += 1
                    continue
                if not _CACHE_FILE_RE.fullmatch(name):
                    continue
                entries.append((st.st_mtime, st.st_size, full))
                total += st.st_size
        if total <= max_bytes:
            return removed
        entries.sort()
        for mtime, size, full in entries:
            if total <= max_bytes:
                break
            if now - mtime < _PRUNE_GRACE:
                continue
            outcome = self._unlink(full)
            if outcome == "deleted":
                removed += 1
            if outcome != "failed":
                total -= size
        if total > max_bytes:
            log.info("thumbnail cache still holds %d bytes after pruning (recently used files kept)", total)
        return removed

    @staticmethod
    def _unlink(path: str) -> str:
        """Delete a cache file; returns "deleted", "missing" or "failed"."""
        try:
            os.unlink(path)
            return "deleted"
        except FileNotFoundError:
            return "missing"
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EPERM, errno.EBUSY):
                log.warning("cannot delete cached thumbnail %s: %s", path, exc)
            return "failed"
