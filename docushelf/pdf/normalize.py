"""Turning incoming files into one PDF, and finding and removing blank pages.

``normalize_to_pdf`` is the first processing step of an inbox item: whatever
arrived (PDFs, scans, phone photos) becomes a single PDF. PDF pages are taken
over without re-encoding; images are embedded losslessly where the PDF format
allows it (JPEG data as is, PNG/TIFF compressed without loss) and only
re-encoded when they have to be (transparency, mirrored EXIF orientation,
HEIC/AVIF/WebP, more than 8 bits per sample).

``detect_blank_pages`` finds the empty back sides that duplex scanners
produce; ``remove_pages`` writes a copy without them.

``pdf_protection`` recognises PDFs that must never be rewritten: digitally
signed files (any rewrite invalidates the signature) and files encrypted with
a user password (they cannot be read without it). A single such PDF is taken
over byte for byte.
"""

from __future__ import annotations

import io
import logging
import math
import os
import tempfile
import threading
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from pathlib import Path

import img2pdf
import pikepdf
import pypdfium2 as pdfium
from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError

from ..errors import ProcessingError, UnsupportedFile
from ..util import fsops
from . import PDFIUM_LOCK

log = logging.getLogger(__name__)

SUPPORTED_IMAGE_EXT = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".jpe",
        ".jfif",
        ".png",
        ".tif",
        ".tiff",
        ".heic",
        ".heif",
        ".hif",
        ".avif",
        ".webp",
        ".gif",
        ".bmp",
    }
)
SUPPORTED_EXT = SUPPORTED_IMAGE_EXT | {".pdf"}

_SUPPORTED_LABEL = "PDF, JPEG, PNG, TIFF, HEIC, AVIF, WebP, GIF, BMP"

# ISO base media file brands of HEIF/HEIC and AVIF images.
_HEIF_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs", b"mif1", b"mif2", b"msf1"}
_AVIF_BRANDS = {b"avif", b"avis"}
_BMP_HEADER_SIZES = {12, 40, 52, 56, 64, 108, 124}


# ---------------------------------------------------------------------------
# File type detection


def _sniff(head: bytes) -> str | None:
    """Kind of file from its first bytes: "pdf", "image" or None (unknown)."""
    if head.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a")):
        return "image"
    if head.startswith((b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")):
        return "image"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image"
    if head[:2] == b"BM" and len(head) >= 18 and int.from_bytes(head[14:18], "little") in _BMP_HEADER_SIZES:
        return "image"
    if _bmff_brands(head) & (_HEIF_BRANDS | _AVIF_BRANDS):
        return "image"
    # PDF readers accept a header anywhere in the first kilobyte.
    if b"%PDF-" in head[:1024]:
        return "pdf"
    return None


def _bmff_brands(head: bytes) -> set[bytes]:
    """Major and compatible brands of an ISO base media ``ftyp`` box."""
    if len(head) < 16 or head[4:8] != b"ftyp":
        return set()
    size = int.from_bytes(head[0:4], "big")
    end = min(len(head), size if size >= 16 else 16)
    brands = {head[8:12]}
    for offset in range(16, end - 3, 4):
        brands.add(head[offset : offset + 4])
    return brands


def _is_heif(head: bytes, path: Path) -> bool:
    if _bmff_brands(head) & _HEIF_BRANDS:
        return True
    return not _sniff(head) and path.suffix.lower() in {".heic", ".heif", ".hif"}


def _read_head(path: Path, size: int = 1024) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(size)


def detect_kind(path: Path) -> str | None:
    """"pdf", "image" or None. The file content decides; the extension is the fallback."""
    path = Path(path)
    kind = _sniff(_read_head(path))
    if kind:
        return kind
    ext = path.suffix.lower()
    if ext == ".pdf":
        return "pdf"
    if ext in SUPPORTED_IMAGE_EXT:
        return "image"
    return None


# ---------------------------------------------------------------------------
# Writing PDFs safely


def _save_atomic(pdf: pikepdf.Pdf, out: Path, **save_options) -> None:
    """Save ``pdf`` to ``out`` through a temporary file in the same directory."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with suppress(Exception):
        if "/Size" not in pdf.trailer:
            # A trailer rebuilt from a damaged file may lack /Size; qpdf only
            # writes the correct value when the key is present.
            pdf.trailer.Size = 0
    tmp = fsops.temp_name_for(out)
    try:
        pdf.save(tmp, **save_options)
        fsops.fsync_file(tmp)
        os.replace(tmp, out)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    fsops.fsync_dir(out.parent)


def _copy_atomic(src: Path, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = fsops.temp_name_for(out)
    fsops.copy_file_fsync(src, tmp)
    try:
        os.replace(tmp, out)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    fsops.fsync_dir(out.parent)


def _version_key(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(version).split("."))
    except ValueError:
        return (1, 3)


# ---------------------------------------------------------------------------
# Protected PDFs

PROTECTION_SIGNED = "signed"
PROTECTION_ENCRYPTED = "encrypted"
# Upper bound for the form fields looked at when searching for a signature.
_MAX_FIELDS = 10_000


def is_signed(pdf: pikepdf.Pdf) -> bool:
    """True if the open PDF carries a digital signature.

    Signed means: the AcroForm ``/SigFlags`` has bit 1 (signatures exist), a
    form field of type ``/Sig`` has a value, or the catalog has ``/Perms``
    (certification or usage-rights signatures).
    """
    try:
        root = pdf.Root
        if "/Perms" in root:
            return True
        acroform = root.get("/AcroForm")
        if not isinstance(acroform, pikepdf.Dictionary):
            return False
        with suppress(Exception):
            if int(acroform.get("/SigFlags", 0)) & 1:
                return True
        fields = acroform.get("/Fields")
        return isinstance(fields, pikepdf.Array) and _has_signature_value(fields)
    except Exception:
        log.debug("cannot look for signatures", exc_info=True)
        return False


def _has_signature_value(fields: pikepdf.Array) -> bool:
    """Walk the field tree (type and value are inheritable) looking for a filled signature field."""
    stack: list[tuple[object, object, object]] = [(field, None, None) for field in fields]
    seen: set[tuple[int, int]] = set()
    visited = 0
    while stack and visited < _MAX_FIELDS:
        field, parent_type, parent_value = stack.pop()
        if not isinstance(field, pikepdf.Dictionary):
            continue
        if field.is_indirect:
            if field.objgen in seen:
                continue
            seen.add(field.objgen)
        visited += 1
        field_type = field.get("/FT", parent_type)
        value = field.get("/V", parent_value)
        if field_type == pikepdf.Name.Sig and value is not None:
            return True
        kids = field.get("/Kids")
        if isinstance(kids, pikepdf.Array):
            stack.extend((kid, field_type, value) for kid in kids)
    return False


def pdf_protection(path: Path) -> str | None:
    """``"signed"``, ``"encrypted"`` (needs a user password) or None.

    Protected files are never rewritten. A file encrypted with an owner
    password only is not protected: it opens without a password and is
    rewritten without encryption. Files that cannot be read for other reasons
    (damaged, not a PDF) count as not protected; this function never raises.
    """
    try:
        pdf = pikepdf.open(path)
    except pikepdf.PasswordError:
        return PROTECTION_ENCRYPTED
    except Exception:
        return None
    with pdf:
        return PROTECTION_SIGNED if is_signed(pdf) else None


# ---------------------------------------------------------------------------
# PDF input


def _password_message(name: str, several: bool) -> str:
    if several:
        return (
            f"PDF ist passwortgeschützt („{name}“) und kann nicht mit anderen Dateien zusammengeführt werden "
            "– lade die Dateien bitte einzeln hoch"
        )
    return "PDF ist passwortgeschützt"


class _NeedsPassword(ProcessingError):
    """The PDF is encrypted with a user password."""


def _open_pdf(path: Path, *, several: bool) -> pikepdf.Pdf:
    """Open an input PDF; PDFs with only an owner password open without one."""
    name = path.name
    try:
        pdf = pikepdf.open(path)
    except pikepdf.PasswordError:
        raise _NeedsPassword(_password_message(name, several)) from None
    except pikepdf.PdfError as exc:
        raise ProcessingError(f"„{name}“ ist beschädigt oder kein gültiges PDF") from exc
    except OSError as exc:
        raise ProcessingError(f"„{name}“ kann nicht gelesen werden: {exc.strerror or exc}") from exc
    try:
        count = len(pdf.pages)
    except Exception as exc:  # broken page tree
        pdf.close()
        raise ProcessingError(f"„{name}“ ist beschädigt (Seitenstruktur unlesbar)") from exc
    if count == 0:
        pdf.close()
        raise ProcessingError(f"„{name}“ enthält keine Seiten")
    return pdf


def _normalize_single_pdf(path: Path, out: Path) -> int:
    """One PDF in, one PDF out: keep the file as it is whenever possible.

    A byte-for-byte copy keeps everything a page copy would lose (attachments
    such as e-invoice XML, PDF/A identification, document structure, digital
    signatures). Signed PDFs and PDFs that need a user password are always
    copied unchanged; the page count of the latter cannot be read and is
    returned as 0. Other files that are encrypted with an owner password only,
    or that needed repairs to open, are rewritten without encryption.
    """
    try:
        pdf = _open_pdf(path, several=False)
    except _NeedsPassword:
        _copy_atomic(path, out)
        return 0
    with pdf:
        count = len(pdf.pages)
        warnings = pdf.get_warnings()  # reading them clears qpdf's list
        if (pdf.is_encrypted or warnings) and not is_signed(pdf):
            if warnings:
                log.info("repairing %s: %s", path.name, "; ".join(warnings[:5]))
            try:
                _save_atomic(pdf, out)
                if not _opens_cleanly(out, count):
                    # Fall back to fresh document structure around the same pages.
                    log.info("rewritten %s is still not clean, copying its pages instead", path.name)
                    with pikepdf.new() as fresh:
                        fresh.pages.extend(pdf.pages)
                        _save_atomic(fresh, out, min_version=pdf.pdf_version)
            except (pikepdf.PdfError, pikepdf.ForeignObjectError) as exc:
                raise ProcessingError(f"„{path.name}“ ist beschädigt und kann nicht übernommen werden") from exc
            return count
    _copy_atomic(path, out)
    return count


def _opens_cleanly(path: Path, pages: int) -> bool:
    try:
        with pikepdf.open(path) as pdf:
            return len(pdf.pages) == pages and not pdf.get_warnings()
    except Exception:
        return False


def _append_pages(target: pikepdf.Pdf, source: pikepdf.Pdf, name: str) -> None:
    """Append all pages of ``source``; keep form fields and bookmarks where pikepdf can."""
    before = len(target.pages)
    add_pages_from = getattr(target, "add_pages_from", None)
    if add_pages_from is not None:
        try:
            add_pages_from(source)
            return
        except Exception:
            log.warning("cannot carry forms/outlines of %s, copying the plain pages", name, exc_info=True)
            while len(target.pages) > before:
                del target.pages[len(target.pages) - 1]
    try:
        target.pages.extend(source.pages)
    except Exception as exc:
        raise ProcessingError(f"Die Seiten von „{name}“ können nicht übernommen werden") from exc


# ---------------------------------------------------------------------------
# Image input

_A4_PT = (595.2756, 841.8898)
# An image keeps its physical size only if its resolution is believable and
# the resulting page is a plausible paper size (between a receipt and A3).
_MIN_DPI = 50.0
_MAX_DPI = 4800.0
_MIN_SHORT_SIDE_IN = 1.0
_MIN_LONG_SIDE_IN = 2.0
_MAX_LONG_SIDE_IN = 17.5

_heif_lock = threading.Lock()
_heif_state: bool | None = None


def _ensure_heif_support() -> bool:
    """Register the HEIF/HEIC opener of pillow-heif once, on first use."""
    global _heif_state
    if _heif_state is None:
        with _heif_lock:
            if _heif_state is None:
                try:
                    import pillow_heif

                    pillow_heif.register_heif_opener()
                    _heif_state = True
                except Exception:
                    log.warning("pillow-heif is not available; HEIC/HEIF images cannot be converted", exc_info=True)
                    _heif_state = False
    return bool(_heif_state)


def page_size_for(width_px: int, height_px: int, dpi: tuple[float, float] | None) -> tuple[float, float, float, float]:
    """Page and image size in points: ``(page_w, page_h, image_w, image_h)``.

    Images with a plausible resolution keep their physical size (a 150 dpi A4
    scan stays A4). Images without one - phone photos usually claim 72 dpi
    for several thousand pixels - are fitted into an A4 page in portrait or
    landscape orientation, whichever matches the image.
    """
    if dpi is not None:
        dpi_x, dpi_y = dpi
        if _MIN_DPI <= dpi_x <= _MAX_DPI and _MIN_DPI <= dpi_y <= _MAX_DPI:
            width_in, height_in = width_px / dpi_x, height_px / dpi_y
            long_side, short_side = max(width_in, height_in), min(width_in, height_in)
            if short_side >= _MIN_SHORT_SIDE_IN and _MIN_LONG_SIDE_IN <= long_side <= _MAX_LONG_SIDE_IN:
                return width_in * 72, height_in * 72, width_in * 72, height_in * 72
    page_w, page_h = _A4_PT if height_px >= width_px else (_A4_PT[1], _A4_PT[0])
    scale = min(page_w / width_px, page_h / height_px)
    return page_w, page_h, width_px * scale, height_px * scale


class _Layout:
    """img2pdf layout function using our own DPI information, one entry per image."""

    def __init__(self, dpis: list[tuple[float, float] | None]):
        self._dpis = list(dpis)
        self._index = 0

    def __call__(self, width_px, height_px, _img2pdf_dpi):
        dpi = self._dpis[self._index] if self._index < len(self._dpis) else None
        self._index += 1
        return page_size_for(width_px, height_px, dpi)


@dataclass
class _Frame:
    size: tuple[int, int]
    mode: str
    dpi: tuple[float, float] | None
    transparent: bool


def _image_dpi(image: Image.Image) -> tuple[float, float] | None:
    raw = image.info.get("dpi")
    if not raw:
        return None
    try:
        dpi_x, dpi_y = float(raw[0]), float(raw[1])
    except (TypeError, ValueError, IndexError, ZeroDivisionError):
        return None
    if not (math.isfinite(dpi_x) and math.isfinite(dpi_y)) or dpi_x <= 1.5 or dpi_y <= 1.5:
        return None
    return dpi_x, dpi_y


def _has_transparency(image: Image.Image) -> bool:
    return image.mode in ("RGBA", "LA", "PA", "RGBa", "La") or "transparency" in image.info


def _orientation(image: Image.Image) -> int | None:
    try:
        value = image.getexif().get(0x0112)
    except Exception:
        return None
    return value if isinstance(value, int) else None


def _webp_is_lossless(path: Path) -> bool:
    """True if the WebP image data is VP8L (lossless) rather than VP8 (lossy)."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(1 << 16)
    except OSError:
        return False
    offset = 12
    while offset + 8 <= len(data):
        fourcc = data[offset : offset + 4]
        size = int.from_bytes(data[offset + 4 : offset + 8], "little")
        if fourcc == b"VP8L":
            return True
        if fourcc == b"VP8 ":
            return False
        offset += 8 + size + (size & 1)
    return False


# Formats img2pdf can embed directly (JPEG as is, the others losslessly re-compressed).
_DIRECT_FORMATS = {"JPEG", "MPO", "PNG", "TIFF", "GIF", "BMP"}
_DIRECT_MODES = {"1", "L", "P", "RGB", "CMYK"}
# Mirrored EXIF orientations cannot be expressed with the page's /Rotate entry;
# the other ones (and invalid values, which are ignored) can.
_MIRRORED = {2, 4, 5, 7}
_LOSSY_FORMATS = {"JPEG", "MPO", "HEIF", "AVIF"}


def _direct_possible(fmt: str, frames: list[_Frame], orientation: int | None) -> bool:
    if fmt not in _DIRECT_FORMATS or orientation in _MIRRORED:
        return False
    for frame in frames:
        if frame.transparent:
            return False
        if frame.mode in _DIRECT_MODES:
            continue
        if fmt == "PNG" and frame.mode.startswith("I;16"):
            continue  # 16-bit grayscale PNG data is embedded as is
        return False
    return True


def _to_8bit_gray(image: Image.Image) -> Image.Image:
    low, high = image.getextrema()
    if image.mode == "F":
        factor = 255.0 if high <= 1.0 else (255.0 / high if high > 255 else 1.0)
        return image.point(lambda value: value * factor).convert("L")
    if image.mode != "I":
        image = image.convert("I")
    if high > 255:
        factor = 255.0 / (65535 if high <= 65535 else high)
        image = image.point(lambda value: value * factor)
    return image.convert("L")


def _flatten_alpha(image: Image.Image) -> Image.Image:
    """Composite onto white paper; transparent areas must not turn black."""
    gray = image.mode in ("LA", "La", "L", "1")
    rgba = image.convert("RGBA")
    background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    background.alpha_composite(rgba)
    return background.convert("L" if gray else "RGB")


def _prepare_frame(image: Image.Image) -> Image.Image:
    """A copy of the frame in a mode that JPEG/PNG and PDF can carry: 1, L, P or RGB."""
    mode = image.mode
    if mode.startswith("I;16") or mode in ("I", "F"):
        return _to_8bit_gray(image)
    if _has_transparency(image):
        return _flatten_alpha(image)
    if mode in ("1", "L", "P", "RGB"):
        return image.copy()
    try:
        return image.convert("RGB")
    except (ValueError, OSError) as exc:
        raise UnsupportedFile(f"Farbraum {mode} wird nicht unterstützt") from exc


def _matching_icc(icc: bytes | None, mode: str) -> bytes | None:
    """The ICC profile if it describes ``mode`` (RGB or gray), else None."""
    if not icc or mode not in ("RGB", "L"):
        return None
    try:
        from PIL import ImageCms

        space = ImageCms.ImageCmsProfile(io.BytesIO(icc)).profile.xcolor_space.strip().upper()
    except Exception:
        return None
    return icc if space == ("RGB" if mode == "RGB" else "GRAY") else None


def _encode(frame: Image.Image, *, lossy: bool, quality: int, icc: bytes | None) -> bytes:
    buffer = io.BytesIO()
    options = {}
    if lossy:
        if frame.mode == "P":
            frame = frame.convert("RGB")
        elif frame.mode == "1":
            frame = frame.convert("L")
        profile = _matching_icc(icc, frame.mode)
        if profile:
            options["icc_profile"] = profile
        frame.save(buffer, format="JPEG", quality=quality, optimize=True, **options)
    else:
        profile = _matching_icc(icc, frame.mode)
        if profile:
            options["icc_profile"] = profile
        frame.save(buffer, format="PNG", compress_level=6, **options)
    return buffer.getvalue()


def _upright(image: Image.Image) -> Image.Image:
    """A copy of the image turned as its EXIF orientation says (unreadable EXIF is ignored)."""
    try:
        return ImageOps.exif_transpose(image) or image.copy()
    except Exception:
        log.info("ignoring unreadable EXIF data", exc_info=True)
        return image.copy()


def _open_image(path: Path) -> Image.Image:
    head = _read_head(path, 64)
    if _is_heif(head, path) and not _ensure_heif_support():
        raise UnsupportedFile(f"HEIC/HEIF-Bilder werden nicht unterstützt („{path.name}“): pillow-heif fehlt")
    try:
        return Image.open(path)
    except UnidentifiedImageError as exc:
        if _sniff(head) == "image":  # a format we know, but the data is unusable
            raise ProcessingError(f"Bild „{path.name}“ ist beschädigt") from exc
        raise UnsupportedFile(f"Bildformat von „{path.name}“ wird nicht unterstützt") from exc
    except Image.DecompressionBombError as exc:
        raise ProcessingError(f"Bild „{path.name}“ ist zu groß") from exc
    except OSError as exc:
        raise ProcessingError(f"Bild „{path.name}“ kann nicht gelesen werden") from exc


def _image_to_pdf(path: Path, target: Path) -> None:
    """Write all pages of one image file as a PDF to ``target``."""
    image = _open_image(path)
    with image:
        fmt = (image.format or "").upper()
        try:
            frame_count = getattr(image, "n_frames", 1) if fmt == "TIFF" else 1
            frames: list[_Frame] = []
            for index in range(frame_count):
                if index:
                    image.seek(index)
                frames.append(_Frame(image.size, image.mode, _image_dpi(image), _has_transparency(image)))
            if frame_count > 1:
                image.seek(0)
        except (OSError, EOFError, ValueError) as exc:
            raise ProcessingError(f"Bild „{path.name}“ ist beschädigt") from exc
        orientation = _orientation(image)

        if _direct_possible(fmt, frames, orientation):
            try:
                with open(target, "wb") as handle:
                    img2pdf.convert(
                        path,
                        layout_fun=_Layout([frame.dpi for frame in frames]),
                        rotation=img2pdf.Rotation.ifvalid,
                        first_frame_only=frame_count == 1,
                        outputstream=handle,
                    )
                return
            except Exception as exc:
                log.info("embedding %s directly failed (%s), converting it with Pillow", path.name, exc)
                with suppress(FileNotFoundError):
                    os.unlink(target)

        lossy = fmt in _LOSSY_FORMATS or (fmt == "WEBP" and not _webp_is_lossless(path))
        quality = 95 if fmt in ("JPEG", "MPO") else 92
        icc = image.info.get("icc_profile")
        encoded: list[io.BytesIO] = []
        dpis: list[tuple[float, float] | None] = []
        try:
            for index, info in enumerate(frames):
                if frame_count > 1:
                    image.seek(index)
                image.load()
                upright = _upright(image)
                dpi = info.dpi
                if dpi and (orientation if index == 0 else _orientation(image)) in (5, 6, 7, 8):
                    dpi = (dpi[1], dpi[0])
                frame = _prepare_frame(upright)
                encoded.append(io.BytesIO(_encode(frame, lossy=lossy, quality=quality, icc=icc)))
                dpis.append(dpi)
        except Image.DecompressionBombError as exc:
            raise ProcessingError(f"Bild „{path.name}“ ist zu groß") from exc
        except (OSError, EOFError, SyntaxError, ValueError) as exc:
            raise ProcessingError(f"Bild „{path.name}“ ist beschädigt") from exc
    try:
        with open(target, "wb") as handle:
            img2pdf.convert(encoded, layout_fun=_Layout(dpis), rotation=img2pdf.Rotation.none, outputstream=handle)
    except Exception as exc:
        raise ProcessingError(f"Bild „{path.name}“ kann nicht in ein PDF umgewandelt werden") from exc


# ---------------------------------------------------------------------------
# normalize_to_pdf


def _check_input(path: Path) -> str:
    name = path.name
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        raise ProcessingError(f"„{name}“ ist nicht mehr vorhanden") from None
    except OSError as exc:
        raise ProcessingError(f"„{name}“ kann nicht gelesen werden: {exc.strerror or exc}") from exc
    if not path.is_file():
        raise ProcessingError(f"„{name}“ ist keine Datei")
    if size == 0:
        raise ProcessingError(f"„{name}“ ist leer (0 Byte)")
    try:
        kind = detect_kind(path)
    except OSError as exc:
        raise ProcessingError(f"„{name}“ kann nicht gelesen werden: {exc.strerror or exc}") from exc
    if kind is None:
        raise UnsupportedFile(f"Dateityp von „{name}“ wird nicht unterstützt (möglich sind {_SUPPORTED_LABEL})")
    return kind


def normalize_to_pdf(files: list[Path], out: Path) -> int:
    """Combine ``files`` (PDFs and images, in this order) into the PDF ``out``.

    Returns the number of pages. A single PDF is taken over unchanged (only
    owner-password encryption is removed and damaged files are repaired);
    a single signed or user-password PDF (see ``pdf_protection``) is always
    copied byte for byte, and for a user-password PDF the result is 0 because
    its pages cannot be counted. Several files are merged page by page.
    ``out`` is replaced atomically.

    Raises UnsupportedFile for file types that cannot become a PDF and
    ProcessingError for damaged or empty input and for user-password PDFs
    that would have to be merged with other files.
    """
    paths = [Path(p) for p in files]
    if not paths:
        raise ValueError("normalize_to_pdf needs at least one file")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    kinds = [_check_input(path) for path in paths]
    if len(paths) == 1 and kinds[0] == "pdf":
        return _normalize_single_pdf(paths[0], out)

    several = len(paths) > 1
    with tempfile.TemporaryDirectory(prefix=".docushelf-", suffix=".tmp", dir=out.parent) as tmp, ExitStack() as stack:
        result = stack.enter_context(pikepdf.new())
        version = "1.3"
        for number, (path, kind) in enumerate(zip(paths, kinds), start=1):
            if kind == "pdf":
                source = stack.enter_context(_open_pdf(path, several=several))
            else:
                converted = Path(tmp) / f"image-{number}.pdf"
                _image_to_pdf(path, converted)
                try:
                    source = stack.enter_context(pikepdf.open(converted))
                except pikepdf.PdfError as exc:
                    raise ProcessingError(f"Bild „{path.name}“ kann nicht in ein PDF umgewandelt werden") from exc
            if _version_key(source.pdf_version) > _version_key(version):
                version = source.pdf_version
            _append_pages(result, source, path.name)
        count = len(result.pages)
        if count == 0:
            raise ProcessingError("Das Dokument enthält keine Seiten")
        try:
            _save_atomic(result, out, min_version=version)
        except (pikepdf.PdfError, pikepdf.ForeignObjectError) as exc:
            raise ProcessingError("Die Eingangsdateien enthalten beschädigte PDF-Daten") from exc
    return count


# ---------------------------------------------------------------------------
# Blank pages
#
# A page is blank when, after ignoring its outer margins, almost nothing on it
# looks like a deliberate mark. Scanned back sides are never perfectly white:
# they carry a gray paper tone, sensor noise, dust, hairs, punch holes and
# faint show-through of the front side. The measurement therefore works on a
# coarse rendering and on "marks" instead of single dark pixels:
#
# 1. render at about 40 dpi in grayscale (this alone averages away sensor
#    noise and tiny dust) and cut away BLANK_MARGIN on every side;
# 2. blur slightly, take the median as paper tone and treat everything at
#    least INK_DELTA gray levels darker as ink (show-through stays lighter);
# 3. drop isolated ink pixels, then join ink within JOIN_MM so that letters
#    form words and pen strokes form a signature;
# 4. discard joined marks smaller than MIN_MARK_MM2 - dust and specks;
# 5. let every remaining mark claim the area within REACH_MM around it.
#
# The resulting ratio is the share of the inner page area claimed by marks.
# The default threshold 0.004 (0.4 %) is about the area of one short line of
# text (roughly a dozen characters at 10 pt) or a small signature on an A4
# page; noisy, dusty or show-through back sides measure well below it.

BLANK_RENDER_DPI = 40.0
BLANK_MARGIN = 0.07
BLANK_TEXT_CHARS = 15
BLUR_RADIUS_PX = 0.5
INK_DELTA = 30
JOIN_MM = 1.3
MIN_MARK_MM2 = 20.0
REACH_MM = 1.3
_MAX_RENDER_SIDE_PX = 1600
_DARK_PAPER = 100
_BUSY_SHARE = 0.25


def _px(mm: float, dpi: float) -> int:
    return max(1, round(mm / 25.4 * dpi))


def _median(histogram: list[int], total: int) -> int:
    seen = 0
    for value, count in enumerate(histogram):
        seen += count
        if seen * 2 >= total:
            return value
    return 255


def _significant_marks(mask: Image.Image, min_pixels: int) -> Image.Image | None:
    """Keep only connected ink regions (value 0) with at least ``min_pixels`` pixels."""
    width = mask.width
    data = mask.tobytes()
    size = len(data)
    seen = bytearray(size)
    keep: bytearray | None = None
    position = data.find(b"\x00")
    while position != -1:
        if not seen[position]:
            seen[position] = 1
            stack = [position]
            region = []
            while stack:
                index = stack.pop()
                region.append(index)
                column = index % width
                if column:
                    neighbour = index - 1
                    if not seen[neighbour] and not data[neighbour]:
                        seen[neighbour] = 1
                        stack.append(neighbour)
                if column + 1 < width:
                    neighbour = index + 1
                    if not seen[neighbour] and not data[neighbour]:
                        seen[neighbour] = 1
                        stack.append(neighbour)
                neighbour = index - width
                if neighbour >= 0 and not seen[neighbour] and not data[neighbour]:
                    seen[neighbour] = 1
                    stack.append(neighbour)
                neighbour = index + width
                if neighbour < size and not seen[neighbour] and not data[neighbour]:
                    seen[neighbour] = 1
                    stack.append(neighbour)
            if len(region) >= min_pixels:
                if keep is None:
                    keep = bytearray(b"\xff" * size)
                for index in region:
                    keep[index] = 0
        position = data.find(b"\x00", position + 1)
    if keep is None:
        return None
    return Image.frombytes("L", mask.size, bytes(keep))


def ink_ratio(image: Image.Image, *, dpi: float = BLANK_RENDER_DPI) -> float:
    """Share (0..1) of the inner page area claimed by significant marks.

    ``image`` is a rendering of the whole page at ``dpi``. See the comment
    above for the steps; ``detect_blank_pages`` compares this value with its
    threshold. Pages with a lot of ink get an upper estimate (they are far
    from blank anyway).
    """
    gray = image if image.mode == "L" else image.convert("L")
    width, height = gray.size
    margin_x, margin_y = round(width * BLANK_MARGIN), round(height * BLANK_MARGIN)
    inner = gray.crop((margin_x, margin_y, width - margin_x, height - margin_y))
    if inner.width < 8 or inner.height < 8:
        return 1.0
    inner = inner.filter(ImageFilter.GaussianBlur(BLUR_RADIUS_PX))
    total = inner.width * inner.height
    paper = _median(inner.histogram(), total)
    if paper < _DARK_PAPER:
        return 1.0  # mostly dark: a photo or a dark sheet, not empty paper
    cut = paper - INK_DELTA
    mask = inner.point([0 if value < cut else 255 for value in range(256)])
    mask = mask.filter(ImageFilter.RankFilter(3, 2))  # drop isolated pixels
    join = _px(JOIN_MM, dpi)
    reach = _px(REACH_MM, dpi)
    upper = mask.filter(ImageFilter.MinFilter(2 * (join + reach) + 1)).histogram()[0] / total
    if upper == 0.0:
        return 0.0
    joined = mask.filter(ImageFilter.MinFilter(2 * join + 1))
    if joined.histogram()[0] > total * _BUSY_SHARE:
        return upper
    min_pixels = max(1, math.ceil(MIN_MARK_MM2 / (25.4 / dpi) ** 2))
    marks = _significant_marks(joined, min_pixels)
    if marks is None:
        return 0.0
    return marks.filter(ImageFilter.MinFilter(2 * reach + 1)).histogram()[0] / total


def _text_chars(text: str | None) -> int:
    return sum(1 for char in (text or "") if not char.isspace())


def _open_pdfium(path: Path) -> pdfium.PdfDocument:
    try:
        return pdfium.PdfDocument(Path(path))
    except FileNotFoundError:
        raise ProcessingError(f"„{Path(path).name}“ ist nicht mehr vorhanden") from None
    except pdfium.PdfiumError as exc:
        if getattr(exc, "err_code", None) == pdfium.raw.FPDF_ERR_PASSWORD:
            raise ProcessingError("PDF ist passwortgeschützt") from exc
        raise ProcessingError(f"„{Path(path).name}“ kann nicht gelesen werden") from exc


def detect_blank_pages(pdf: Path, *, threshold: float, text_pages: list[str] | None = None) -> list[int]:
    """1-based numbers of the blank pages of ``pdf``.

    ``threshold`` is compared with ``ink_ratio`` of each page (share of the
    inner page area claimed by marks; 0.004 by default, see above). Pages
    with at least 15 non-space characters of text - from ``text_pages`` or,
    if not given, from the PDF's text layer - are never blank. Pages that
    cannot be rendered count as not blank. The result may contain every page;
    callers keep at least one (``remove_pages`` refuses to remove all).
    """
    blank: list[int] = []
    with PDFIUM_LOCK:
        document = _open_pdfium(Path(pdf))
    try:
        with PDFIUM_LOCK:
            count = len(document)
        for index in range(count):
            number = index + 1
            image: Image.Image | None = None
            dpi = BLANK_RENDER_DPI
            with PDFIUM_LOCK:
                try:
                    page = document[index]
                except pdfium.PdfiumError:
                    log.warning("cannot load page %d of %s for blank detection", number, pdf)
                    continue
                try:
                    if text_pages is not None:
                        chars = _text_chars(text_pages[index]) if index < len(text_pages) else 0
                    else:
                        chars = _page_text_chars(page)
                    if chars >= BLANK_TEXT_CHARS:
                        continue
                    page_w, page_h = page.get_size()
                    scale = BLANK_RENDER_DPI / 72
                    longest = max(page_w, page_h) * scale
                    if longest > _MAX_RENDER_SIDE_PX:
                        scale *= _MAX_RENDER_SIDE_PX / longest
                    dpi = scale * 72
                    bitmap = page.render(scale=scale, grayscale=True)
                    try:
                        image = bitmap.to_pil().convert("L")
                    finally:
                        bitmap.close()
                except (pdfium.PdfiumError, ValueError) as exc:
                    log.warning("cannot render page %d of %s for blank detection: %s", number, pdf, exc)
                    image = None
                finally:
                    page.close()
            if image is not None and ink_ratio(image, dpi=dpi) < threshold:
                blank.append(number)
    finally:
        with PDFIUM_LOCK:
            document.close()
    return blank


def _page_text_chars(page: pdfium.PdfPage) -> int:
    """Non-space characters in the text layer of a page (caller holds PDFIUM_LOCK)."""
    try:
        textpage = page.get_textpage()
    except pdfium.PdfiumError:
        return 0
    try:
        return _text_chars(textpage.get_text_range())
    except pdfium.PdfiumError:
        return 0
    finally:
        textpage.close()


def remove_pages(src: Path, dst: Path, pages: list[int]) -> None:
    """Write ``src`` without the given 1-based pages to ``dst`` (may be ``src``).

    Raises ValueError for page numbers that do not exist and when every page
    would be removed.
    """
    src, dst = Path(src), Path(dst)
    drop = sorted(set(int(p) for p in pages))
    try:
        pdf = pikepdf.open(src)
    except pikepdf.PasswordError:
        raise ProcessingError("PDF ist passwortgeschützt") from None
    except pikepdf.PdfError as exc:
        raise ProcessingError(f"„{src.name}“ ist beschädigt oder kein gültiges PDF") from exc
    with pdf:
        count = len(pdf.pages)
        invalid = [p for p in drop if p < 1 or p > count]
        if invalid:
            raise ValueError(f"pages {invalid} do not exist (document has {count})")
        if len(drop) >= count:
            raise ValueError("refusing to remove every page of a document")
        if not drop:
            if src.resolve() != dst.resolve():
                _copy_atomic(src, dst)
            return
        for number in reversed(drop):
            del pdf.pages[number - 1]
        try:
            _save_atomic(pdf, dst)
        except (pikepdf.PdfError, pikepdf.ForeignObjectError) as exc:
            raise ProcessingError(f"„{src.name}“ kann nicht ohne Leerseiten gespeichert werden") from exc
