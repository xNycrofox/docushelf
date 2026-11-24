"""Tests for docushelf.pdf.normalize: file type detection, conversion to PDF,
blank page detection and page removal. All inputs are real files."""

from __future__ import annotations

import hashlib
import io
import math
import os
import random
from pathlib import Path

import img2pdf
import pikepdf
import pytest
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, features

from conftest import make_scan_pdf, make_text_pdf
from docushelf.errors import ProcessingError, UnsupportedFile
from docushelf.pdf.normalize import (
    SUPPORTED_EXT,
    SUPPORTED_IMAGE_EXT,
    detect_blank_pages,
    detect_kind,
    ink_ratio,
    normalize_to_pdf,
    page_size_for,
    remove_pages,
)
from docushelf.pdf.render import render_page_image
from docushelf.pdf.text import extract_pages

DPI = 150
A4_PX = (1240, 1754)  # A4 at 150 dpi
A4_PT = (595.28, 841.89)


# ---------------------------------------------------------------------------
# helpers


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _boxes(path: Path) -> list[tuple[float, float, int]]:
    """(width, height, rotate) of every page, in points."""
    with pikepdf.open(path) as pdf:
        result = []
        for page in pdf.pages:
            box = [float(v) for v in page.MediaBox]
            result.append((box[2] - box[0], box[3] - box[1], int(page.get("/Rotate", 0))))
        return result


def _no_leftovers(directory: Path) -> None:
    left = [p.name for p in directory.iterdir() if p.name.startswith(".docushelf-")]
    assert left == []


def _scan_paper(*, gray: int = 236, noise: int = 6) -> Image.Image:
    """An empty A4 sheet as a scanner sees it: gray tone, sensor noise, uneven light."""
    base = Image.new("L", A4_PX, gray)
    grain = Image.effect_noise(A4_PX, noise * 4)
    page = ImageChops.add(base, grain, scale=1.0, offset=-128)
    shading = Image.linear_gradient("L").resize(A4_PX).point(lambda v: v * 10 // 255)
    return ImageChops.subtract(page, shading).convert("RGB")


def _add_dust(page: Image.Image, *, specks: int = 80, dust: int = 6, seed: int = 2) -> Image.Image:
    rnd = random.Random(seed)
    draw = ImageDraw.Draw(page)
    width, height = page.size
    for _ in range(specks):
        x, y = rnd.randrange(width), rnd.randrange(height)
        radius = rnd.choice([0.5, 1, 1, 1.5])
        value = rnd.randrange(10, 140)
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(value,) * 3)
    for _ in range(dust):
        x, y = rnd.randrange(width), rnd.randrange(height)
        radius = rnd.choice([2, 2.5, 3])
        value = rnd.randrange(10, 80)
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(value,) * 3)
    x, y = rnd.randrange(width // 4, width // 2), rnd.randrange(height // 4, height // 2)
    hair = [(x + i * 3, y + math.sin(i / 3) * 6) for i in range(20)]
    draw.line(hair, fill=(60, 60, 60), width=1)
    return page


def _add_punch_holes(page: Image.Image) -> Image.Image:
    draw = ImageDraw.Draw(page)
    px_mm = DPI / 25.4
    for y_mm in (108.5, 188.5):
        cx, cy, radius = 12 * px_mm, y_mm * px_mm, 2.75 * px_mm
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(40, 40, 40))
    return page


def _add_text_line(page: Image.Image, text: str, *, pt: float = 11) -> Image.Image:
    draw = ImageDraw.Draw(page)
    font = ImageFont.load_default(size=round(pt / 72 * DPI))
    draw.text((round(25 / 25.4 * DPI), round(150 / 25.4 * DPI)), text, fill=(25, 25, 25), font=font)
    return page


def _add_signature(page: Image.Image, *, width_mm: float = 0.3, color=(30, 50, 150)) -> Image.Image:
    draw = ImageDraw.Draw(page)
    px_mm = DPI / 25.4
    x0, y0 = 110 * px_mm, 200 * px_mm
    stroke = max(1, round(width_mm * px_mm))
    points = []
    for i in range(120):
        t = i / 119
        points.append((x0 + t * 45 * px_mm, y0 + math.sin(t * 14) * 5 * px_mm + math.sin(t * 5) * 3 * px_mm))
    draw.line(points, fill=color, width=stroke)
    draw.arc((x0 - 4 * px_mm, y0 - 8 * px_mm, x0 + 8 * px_mm, y0 + 6 * px_mm), 30, 330, fill=color, width=stroke)
    return page


def _add_show_through(page: Image.Image, *, strength: int = 30) -> Image.Image:
    """Mirrored, blurred text of the front side shining through thin paper."""
    layer = Image.new("L", page.size, 0)
    draw = ImageDraw.Draw(layer)
    font = ImageFont.load_default(size=23)
    y = 180
    for i in range(35):
        draw.text((150, y), f"Sehr geehrte Damen und Herren, anbei die Rechnung Nr. {i}", fill=255, font=font)
        y += 35
    layer = layer.transpose(Image.Transpose.FLIP_LEFT_RIGHT).filter(ImageFilter.GaussianBlur(1.2))
    darken = layer.point(lambda v: v * strength // 255)
    return Image.merge("RGB", [ImageChops.subtract(channel, darken) for channel in page.split()])


def _scan_pdf(path: Path, pages: list[Image.Image]) -> Path:
    """Image-only PDF like a scanner produces it (JPEG pages at 150 dpi)."""
    blobs = []
    for page in pages:
        buffer = io.BytesIO()
        page.save(buffer, format="JPEG", quality=85, dpi=(DPI, DPI))
        blobs.append(buffer.getvalue())
    path.write_bytes(img2pdf.convert(blobs))
    return path


def _marked_image(size: tuple[int, int], marker: str) -> Image.Image:
    """White image with a red band along one raw edge ("top", "left", ...)."""
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    width, height = size
    band = max(4, min(width, height) // 8)
    box = {
        "top": (0, 0, width, band),
        "bottom": (0, height - band, width, height),
        "left": (0, 0, band, height),
        "right": (width - band, 0, width, height),
    }[marker]
    draw.rectangle(box, fill=(255, 0, 0))
    return image


def _red_edge(pdf: Path) -> str:
    """Which edge of the rendered first page carries the red band."""
    image = render_page_image(pdf, 1, width=200)
    width, height = image.size

    def red(x: int, y: int) -> bool:
        r, g, b = image.getpixel((x, y))
        return r > 200 and g < 90 and b < 90

    # Sample a few pixels inside the image area along each edge.
    samples = {
        "top": [red(width // 2 + dx, y) for dx in (-10, 0, 10) for y in range(height // 3)],
        "bottom": [red(width // 2 + dx, y) for dx in (-10, 0, 10) for y in range(2 * height // 3, height)],
        "left": [red(x, height // 2 + dy) for dy in (-10, 0, 10) for x in range(width // 3)],
        "right": [red(x, height // 2 + dy) for dy in (-10, 0, 10) for x in range(2 * width // 3, width)],
    }
    found = [edge for edge, values in samples.items() if any(values)]
    assert len(found) == 1, found
    return found[0]


def _with_orientation(image: Image.Image, orientation: int, path: Path, **save) -> Path:
    exif = image.getexif()
    exif[0x0112] = orientation
    image.save(path, format="JPEG", exif=exif.tobytes(), quality=92, **save)
    return path


# ---------------------------------------------------------------------------
# detect_kind


def test_supported_extensions():
    assert ".pdf" in SUPPORTED_EXT and ".pdf" not in SUPPORTED_IMAGE_EXT
    for ext in (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".webp"):
        assert ext in SUPPORTED_IMAGE_EXT


def test_detect_kind_prefers_content_over_extension(tmp_path: Path, samples):
    pdf = samples.text_pdf(tmp_path / "doc.pdf", ["Hallo"])
    disguised_pdf = tmp_path / "scan.jpg"
    disguised_pdf.write_bytes(pdf.read_bytes())
    jpeg = tmp_path / "photo.pdf"
    Image.new("RGB", (20, 20), "white").save(jpeg, format="JPEG")
    junk_header = tmp_path / "junk.bin"
    junk_header.write_bytes(b"\r\n\x00garbage" + pdf.read_bytes())

    assert detect_kind(pdf) == "pdf"
    assert detect_kind(disguised_pdf) == "pdf"
    assert detect_kind(jpeg) == "image"
    assert detect_kind(junk_header) == "pdf"


@pytest.mark.parametrize("fmt,ext", [("PNG", ".png"), ("TIFF", ".tif"), ("GIF", ".gif"), ("BMP", ".bmp"), ("WEBP", ".webp")])
def test_detect_kind_images_without_extension(tmp_path: Path, fmt: str, ext: str):
    path = tmp_path / "upload"
    Image.new("RGB", (20, 20), "white").save(path, format=fmt)
    assert detect_kind(path) == "image"


def test_detect_kind_heif_brands(tmp_path: Path):
    heic = tmp_path / "IMG_0001"
    heic.write_bytes(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 32)
    avif = tmp_path / "bild"
    avif.write_bytes(b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1miaf" + b"\x00" * 32)
    video = tmp_path / "clip.mov"
    video.write_bytes(b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  " + b"\x00" * 32)
    assert detect_kind(heic) == "image"
    assert detect_kind(avif) == "image"
    assert detect_kind(video) is None


def test_detect_kind_falls_back_to_extension(tmp_path: Path):
    unknown = tmp_path / "notes.txt"
    unknown.write_text("just text", encoding="utf-8")
    odd_jpeg = tmp_path / "odd.jpg"
    odd_jpeg.write_bytes(b"not really an image")
    empty_pdf = tmp_path / "empty.pdf"
    empty_pdf.write_bytes(b"")
    docx = tmp_path / "brief.docx"
    docx.write_bytes(b"PK\x03\x04" + b"\x00" * 40)
    assert detect_kind(unknown) is None
    assert detect_kind(odd_jpeg) == "image"
    assert detect_kind(empty_pdf) == "pdf"
    assert detect_kind(docx) is None


# ---------------------------------------------------------------------------
# normalize_to_pdf: PDF input


def test_single_pdf_is_taken_over_unchanged(tmp_path: Path, samples):
    src = samples.text_pdf(tmp_path / "in" / "rechnung.pdf", ["Seite eins", "Seite zwei", "Seite drei"])
    out = tmp_path / "work" / "normalized.pdf"
    assert normalize_to_pdf([src], out) == 3
    assert _sha(out) == _sha(src)
    _no_leftovers(out.parent)


def test_single_pdf_keeps_attachments(tmp_path: Path, samples):
    src = samples.text_pdf(tmp_path / "invoice.pdf", ["Rechnung 4711"])
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        pdf.attachments["factur-x.xml"] = pikepdf.AttachedFileSpec(pdf, b"<Invoice/>", mime_type="text/xml")
        pdf.save()
    out = tmp_path / "normalized.pdf"
    normalize_to_pdf([src], out)
    with pikepdf.open(out) as pdf:
        assert list(pdf.attachments) == ["factur-x.xml"]


def test_owner_password_only_is_removed(tmp_path: Path, samples):
    plain = samples.text_pdf(tmp_path / "plain.pdf", ["Kontoauszug Januar", "Seite zwei"])
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(plain) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(owner="geheim", user="", allow=pikepdf.Permissions(extract=False)))
    out = tmp_path / "normalized.pdf"
    assert normalize_to_pdf([locked], out) == 2
    with pikepdf.open(out) as pdf:
        assert not pdf.is_encrypted
    assert extract_pages(out)[0] == "Kontoauszug Januar"


def test_user_password_pdf_is_taken_over_unchanged(tmp_path: Path, samples):
    plain = samples.text_pdf(tmp_path / "plain.pdf", ["Geheim"])
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(plain) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(owner="geheim", user="nutzer"))
    out = tmp_path / "out" / "normalized.pdf"
    # Protected files are never rewritten; without the password the pages cannot be counted.
    assert normalize_to_pdf([locked], out) == 0
    assert _sha(out) == _sha(locked)
    _no_leftovers(out.parent)


def test_user_password_in_a_batch_names_the_file(tmp_path: Path, samples):
    first = samples.text_pdf(tmp_path / "a.pdf", ["A"])
    plain = samples.text_pdf(tmp_path / "plain.pdf", ["B"])
    locked = tmp_path / "zweites.pdf"
    with pikepdf.open(plain) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(owner="x", user="y"))
    with pytest.raises(ProcessingError, match="PDF ist passwortgeschützt.*zweites.pdf"):
        normalize_to_pdf([first, locked], tmp_path / "out.pdf")


def test_broken_and_empty_inputs(tmp_path: Path):
    broken = tmp_path / "kaputt.pdf"
    broken.write_bytes(b"%PDF-1.4\n1 0 obj garbage\n")
    html = tmp_path / "download.pdf"
    html.write_bytes(b"<html><body>404 Not Found</body></html>")
    empty = tmp_path / "leer.pdf"
    empty.write_bytes(b"")
    zero_pages = tmp_path / "ohne-seiten.pdf"
    pikepdf.new().save(zero_pages)
    out = tmp_path / "out" / "normalized.pdf"

    with pytest.raises(ProcessingError, match="kaputt.pdf.*beschädigt"):
        normalize_to_pdf([broken], out)
    with pytest.raises(ProcessingError, match="download.pdf"):
        normalize_to_pdf([html], out)
    with pytest.raises(ProcessingError, match="leer"):
        normalize_to_pdf([empty], out)
    with pytest.raises(ProcessingError, match="keine Seiten"):
        normalize_to_pdf([zero_pages], out)
    assert not out.exists()
    _no_leftovers(out.parent)


def test_truncated_pdf_is_repaired(tmp_path: Path, samples):
    src = samples.text_pdf(tmp_path / "full.pdf", ["Erste Seite", "Zweite Seite"])
    data = src.read_bytes()
    truncated = tmp_path / "truncated.pdf"
    truncated.write_bytes(data[: len(data) * 2 // 3])
    out = tmp_path / "normalized.pdf"
    pages = normalize_to_pdf([truncated], out)
    assert pages >= 1
    with pikepdf.open(out) as pdf:
        assert len(pdf.pages) == pages
        assert not pdf.get_warnings()


def test_unsupported_type(tmp_path: Path):
    text = tmp_path / "notiz.txt"
    text.write_text("Hallo", encoding="utf-8")
    with pytest.raises(UnsupportedFile, match="notiz.txt"):
        normalize_to_pdf([text], tmp_path / "out.pdf")


def test_missing_input(tmp_path: Path):
    with pytest.raises(ProcessingError, match="nicht mehr vorhanden"):
        normalize_to_pdf([tmp_path / "weg.pdf"], tmp_path / "out.pdf")
    with pytest.raises(ValueError):
        normalize_to_pdf([], tmp_path / "out.pdf")


def test_merge_keeps_order_and_content(tmp_path: Path, samples):
    first = samples.text_pdf(tmp_path / "1.pdf", ["Brief Seite 1", "Brief Seite 2"])
    photo = samples.image(tmp_path / "2.jpg", ["Anlage"])
    last = samples.text_pdf(tmp_path / "3.pdf", ["Nachtrag"])
    out = tmp_path / "work" / "normalized.pdf"
    out.parent.mkdir()
    out.write_bytes(b"old content")  # replaced atomically

    assert normalize_to_pdf([first, photo, last], out) == 4
    texts = extract_pages(out)
    assert texts[0] == "Brief Seite 1"
    assert texts[1] == "Brief Seite 2"
    assert texts[2] == ""  # the photo has no text layer (yet)
    assert texts[3] == "Nachtrag"
    _no_leftovers(out.parent)
    assert [p.name for p in out.parent.iterdir()] == ["normalized.pdf"]


def test_merge_with_form_fields(tmp_path: Path, samples):
    form = samples.text_pdf(tmp_path / "form.pdf", ["Formular"])
    with pikepdf.open(form, allow_overwriting_input=True) as pdf:
        widget = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Annot,
                Subtype=pikepdf.Name.Widget,
                FT=pikepdf.Name.Tx,
                T=pikepdf.String("Name"),
                V=pikepdf.String("Erika"),
                Rect=[100, 100, 300, 120],
                P=pdf.pages[0].obj,
            )
        )
        pdf.pages[0].Annots = pdf.make_indirect(pikepdf.Array([widget]))
        pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array([widget]))
        pdf.save()
    other = samples.text_pdf(tmp_path / "other.pdf", ["Anschreiben"])
    out = tmp_path / "out.pdf"
    assert normalize_to_pdf([other, form], out) == 2
    with pikepdf.open(out) as pdf:
        assert len(pdf.pages) == 2
        if hasattr(pdf, "add_pages_from"):
            assert len(pdf.Root.AcroForm.Fields) == 1


# ---------------------------------------------------------------------------
# normalize_to_pdf: images


def test_jpeg_is_embedded_without_recompression(tmp_path: Path, samples):
    src = samples.image(tmp_path / "scan.jpg", ["Rechnung"], dpi=150)
    out = tmp_path / "out.pdf"
    assert normalize_to_pdf([src], out) == 1
    with pikepdf.open(out) as pdf:
        (image,) = pdf.pages[0].get_images().values()
        assert image.Filter == pikepdf.Name.DCTDecode
        assert image.read_raw_bytes() == src.read_bytes()


def test_scan_with_resolution_keeps_its_size(tmp_path: Path, samples):
    src = samples.image(tmp_path / "scan.png", ["Seite"], size=A4_PX, dpi=150)
    out = tmp_path / "out.pdf"
    normalize_to_pdf([src], out)
    (width, height, rotate), = _boxes(out)
    assert rotate == 0
    assert width == pytest.approx(A4_PT[0], abs=1) and height == pytest.approx(A4_PT[1], abs=1)

    receipt = samples.image(tmp_path / "bon.png", ["Bon"], size=(945, 2400), dpi=300)
    out2 = tmp_path / "bon.pdf"
    normalize_to_pdf([receipt], out2)
    (width, height, _), = _boxes(out2)
    assert width == pytest.approx(945 / 300 * 72, abs=1) and height == pytest.approx(2400 / 300 * 72, abs=1)


def test_phone_photos_are_fitted_to_a4(tmp_path: Path):
    landscape = tmp_path / "quer.jpg"
    Image.new("RGB", (4032, 3024), "white").save(landscape, format="JPEG", dpi=(72, 72))
    portrait = tmp_path / "hoch.png"
    Image.new("RGB", (1170, 2532), "white").save(portrait, format="PNG")  # no resolution at all
    out1, out2 = tmp_path / "quer.pdf", tmp_path / "hoch.pdf"
    normalize_to_pdf([landscape], out1)
    normalize_to_pdf([portrait], out2)
    assert _boxes(out1)[0][:2] == pytest.approx((A4_PT[1], A4_PT[0]), abs=1)
    assert _boxes(out2)[0][:2] == pytest.approx(A4_PT, abs=1)


def test_page_size_rules():
    # 150 dpi A4 scan: physical size
    assert page_size_for(1240, 1754, (150, 150))[:2] == pytest.approx((595.2, 842.0), abs=1)
    # absurd resolution: fitted into A4, image keeps its aspect ratio
    page_w, page_h, img_w, img_h = page_size_for(4000, 3000, (72, 72))
    assert (page_w, page_h) == pytest.approx((A4_PT[1], A4_PT[0]), abs=0.1)
    assert img_w / img_h == pytest.approx(4000 / 3000)
    assert img_w <= page_w + 0.01 and img_h <= page_h + 0.01
    # no resolution
    assert page_size_for(1000, 1000, None)[:2] == pytest.approx(A4_PT, abs=0.1)
    # nonsense values
    assert page_size_for(1240, 1754, (1, 1))[:2] == pytest.approx(A4_PT, abs=0.1)


def test_exif_rotation_is_applied(tmp_path: Path):
    # Raw pixels are landscape; orientation 6 means "rotate 90° clockwise to view".
    # The raw left edge therefore ends up at the top.
    src = _with_orientation(_marked_image((1600, 1200), "left"), 6, tmp_path / "phone.jpg", dpi=(72, 72))
    out = tmp_path / "out.pdf"
    normalize_to_pdf([src], out)
    width, height, rotate = _boxes(out)[0]
    displayed = (height, width) if rotate in (90, 270) else (width, height)
    assert displayed[0] < displayed[1]  # portrait as the photo was taken
    assert _red_edge(out) == "top"
    with pikepdf.open(out) as pdf:  # still the original JPEG data
        (image,) = pdf.pages[0].get_images().values()
        assert image.read_raw_bytes() == src.read_bytes()


@pytest.mark.parametrize(
    "orientation,raw_edge,expected",
    [
        (2, "left", "right"),  # mirrored horizontally
        (4, "top", "bottom"),  # mirrored vertically
        (5, "top", "left"),  # transposed
        (7, "top", "right"),  # transversed
        (3, "top", "bottom"),  # rotated 180° (lossless via /Rotate)
        (8, "left", "bottom"),  # rotated 90° counter-clockwise
    ],
)
def test_all_exif_orientations(tmp_path: Path, orientation: int, raw_edge: str, expected: str):
    src = _with_orientation(_marked_image((800, 600), raw_edge), orientation, tmp_path / f"o{orientation}.jpg")
    out = tmp_path / f"o{orientation}.pdf"
    normalize_to_pdf([src], out)
    assert _red_edge(out) == expected


@pytest.mark.parametrize("orientation", [0, 9])
def test_invalid_exif_orientation_is_ignored(tmp_path: Path, orientation: int):
    src = _with_orientation(_marked_image((800, 600), "top"), orientation, tmp_path / "odd.jpg")
    out = tmp_path / "odd.pdf"
    normalize_to_pdf([src], out)
    assert _red_edge(out) == "top"
    with pikepdf.open(out) as pdf:  # embedded without re-encoding
        (image,) = pdf.pages[0].get_images().values()
        assert image.read_raw_bytes() == src.read_bytes()


def test_png_with_alpha_gets_white_background(tmp_path: Path):
    src = tmp_path / "logo.png"
    image = Image.new("RGBA", (600, 800), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((200, 300, 400, 500), fill=(0, 0, 255, 255))
    image.save(src, dpi=(100, 100))
    out = tmp_path / "out.pdf"
    normalize_to_pdf([src], out)
    rendered = render_page_image(out, 1, width=300)
    assert rendered.getpixel((10, 10)) == (255, 255, 255)
    assert rendered.getpixel((150, 200))[2] > 200 and rendered.getpixel((150, 200))[0] < 60


@pytest.mark.parametrize("suffix", [".png", ".tif"])
def test_16_bit_grayscale(tmp_path: Path, suffix: str):
    src = tmp_path / f"gray16{suffix}"
    Image.new("I;16", (600, 800), 40000).save(src)
    out = tmp_path / "out.pdf"
    assert normalize_to_pdf([src], out) == 1
    value = render_page_image(out, 1, width=200).convert("L").getpixel((100, 140))
    assert value == pytest.approx(40000 / 65535 * 255, abs=6)


@pytest.mark.parametrize(
    "mode,fmt,color",
    [
        ("CMYK", "JPEG", (0, 255, 255, 0)),  # red in CMYK
        ("CMYK", "TIFF", (0, 255, 255, 0)),
        ("P", "PNG", None),
        ("1", "TIFF", None),
        ("L", "PNG", 90),
    ],
)
def test_color_modes(tmp_path: Path, mode: str, fmt: str, color):
    src = tmp_path / f"img.{fmt.lower()}"
    image = Image.new(mode, (600, 800), color if color is not None else 0)
    if mode == "P":
        image.putpalette([255, 0, 0] + [0, 0, 0] * 255)
    image.save(src, format=fmt, dpi=(100, 100))
    out = tmp_path / "out.pdf"
    assert normalize_to_pdf([src], out) == 1
    pixel = render_page_image(out, 1, width=100).getpixel((50, 66))
    if mode in ("CMYK", "P"):
        assert pixel[0] > 180 and pixel[1] < 80 and pixel[2] < 80
    elif mode == "1":
        assert pixel == (0, 0, 0)
    else:
        assert pixel[0] == pytest.approx(90, abs=4)


def test_multipage_tiff(tmp_path: Path):
    src = tmp_path / "fax.tif"
    frames = [Image.new("1", A4_PX, 1) for _ in range(3)]
    for number, frame in enumerate(frames, start=1):
        ImageDraw.Draw(frame).text((200, 200), f"Seite {number}", fill=0)
    frames[0].save(src, save_all=True, append_images=frames[1:], compression="group4", dpi=(150, 150))
    out = tmp_path / "out.pdf"
    assert normalize_to_pdf([src], out) == 3
    assert all(box[:2] == pytest.approx(A4_PT, abs=1) for box in _boxes(out))


def test_gif_with_transparency_and_bmp(tmp_path: Path):
    gif = tmp_path / "bild.gif"
    image = Image.new("P", (300, 400), 0)
    image.putpalette([0, 0, 0, 255, 0, 0] + [0, 0, 0] * 254)
    ImageDraw.Draw(image).rectangle((100, 100, 200, 200), fill=1)
    image.save(gif, transparency=0)
    bmp = tmp_path / "bild.bmp"
    Image.new("RGB", (800, 600), (0, 128, 0)).save(bmp)
    out = tmp_path / "out.pdf"
    assert normalize_to_pdf([gif, bmp], out) == 2
    first = render_page_image(out, 1, width=300)
    assert first.getpixel((5, 5)) == (255, 255, 255)  # transparent -> paper


@pytest.mark.parametrize("lossless", [False, True])
def test_webp(tmp_path: Path, lossless: bool):
    src = tmp_path / "bild.webp"
    Image.new("RGB", (800, 600), (200, 30, 30)).save(src, format="WEBP", lossless=lossless, quality=80)
    out = tmp_path / "out.pdf"
    assert normalize_to_pdf([src], out) == 1
    with pikepdf.open(out) as pdf:
        (image,) = pdf.pages[0].get_images().values()
        expected = pikepdf.Name.FlateDecode if lossless else pikepdf.Name.DCTDecode
        assert image.Filter == expected


def test_heic(tmp_path: Path):
    pillow_heif = pytest.importorskip("pillow_heif")
    if not pillow_heif.libheif_info().get("HEIF"):
        pytest.skip("no HEIF encoder available to create a sample")
    from docushelf.pdf import normalize

    # Like a phone: sensor-oriented pixels plus "rotate 90° clockwise" (EXIF 6),
    # which the HEIF encoder stores as an image transformation.
    pillow_heif.register_heif_opener()
    image = _marked_image((800, 600), "left")
    exif = image.getexif()
    exif[0x0112] = 6
    src = tmp_path / "IMG_0001.HEIC"
    image.save(src, format="HEIF", quality=90, exif=exif.tobytes())
    normalize._heif_state = None  # exercise the lazy registration
    out = tmp_path / "out.pdf"
    assert normalize_to_pdf([src], out) == 1
    assert normalize._heif_state is True
    assert _red_edge(out) == "top"


@pytest.mark.skipif(not features.check("avif"), reason="Pillow without AVIF support")
def test_avif(tmp_path: Path):
    src = tmp_path / "bild.avif"
    Image.new("RGB", (800, 600), (30, 30, 200)).save(src, format="AVIF")
    out = tmp_path / "out.pdf"
    assert normalize_to_pdf([src], out) == 1


def test_broken_image(tmp_path: Path):
    broken = tmp_path / "kaputt.jpg"
    broken.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
    with pytest.raises(ProcessingError, match="kaputt.jpg.*beschädigt"):
        normalize_to_pdf([broken], tmp_path / "out.pdf")
    _no_leftovers(tmp_path)


# ---------------------------------------------------------------------------
# blank pages


def test_blank_detection_on_scanned_pages(tmp_path: Path):
    pages = [
        _add_dust(_scan_paper(gray=232, noise=8)),  # 1 blank back side, dusty
        _add_text_line(_scan_paper(), "Seite 2 von 2", pt=10),  # 2 one short line
        _add_signature(_scan_paper()),  # 3 only a signature
        _add_dust(_scan_paper(gray=225, noise=12), specks=300, dust=20, seed=9),  # 4 very noisy blank
        Image.new("RGB", A4_PX, "white"),  # 5 perfectly white
        _add_punch_holes(_add_dust(_scan_paper(), seed=4)),  # 6 blank with punch holes
        _add_show_through(_scan_paper(gray=240)),  # 7 blank with show-through
        _add_text_line(_add_dust(_scan_paper(gray=232, noise=8)), "Mit freundlichen Grüßen"),  # 8
        _add_signature(_scan_paper(), width_mm=0.4, color=(130, 130, 130)),  # 9 light pencil signature
    ]
    pdf = _scan_pdf(tmp_path / "duplex.pdf", pages)
    assert detect_blank_pages(pdf, threshold=0.004) == [1, 4, 5, 6, 7]


def test_threshold_meaning(tmp_path: Path):
    pdf = _scan_pdf(tmp_path / "doc.pdf", [_add_text_line(_scan_paper(), "Seite 2 von 2", pt=10)])
    image = render_page_image(pdf, 1, width=round(8.27 * 40)).convert("L")
    ratio = ink_ratio(image, dpi=40)
    assert 0.004 < ratio < 0.01  # one short line is just above the default threshold
    assert detect_blank_pages(pdf, threshold=ratio * 1.2) == [1]
    assert detect_blank_pages(pdf, threshold=0) == []


def test_ink_ratio_extremes():
    assert ink_ratio(Image.new("L", (331, 468), 255)) == 0.0
    assert ink_ratio(Image.new("L", (331, 468), 0)) == 1.0  # dark sheet: never "blank"
    assert ink_ratio(Image.new("L", (4, 4), 255)) == 1.0  # too small to judge


def test_text_layer_protects_pages(tmp_path: Path, samples):
    pdf = samples.text_pdf(tmp_path / "digital.pdf", ["", "Nur eine Zeile mit Text", "kurz"])
    # Page 2 has a text layer with more than 15 characters: never blank, whatever the threshold.
    assert detect_blank_pages(pdf, threshold=0.9) == [1, 3]
    # Text supplied by the caller (e.g. after OCR) is used instead of the PDF's own layer.
    assert detect_blank_pages(pdf, threshold=0.9, text_pages=["Dieser Text kam aus der OCR", "", ""]) == [2, 3]


def test_conftest_scan_samples(tmp_path: Path):
    pdf = make_scan_pdf(tmp_path / "scan.pdf", [["Stadtwerke", "Rechnung 2025"], [], ["Anlage"], []])
    assert detect_blank_pages(pdf, threshold=0.004) == [2, 4]


def test_blank_detection_errors(tmp_path: Path):
    with pytest.raises(ProcessingError):
        detect_blank_pages(tmp_path / "missing.pdf", threshold=0.004)
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 nothing")
    with pytest.raises(ProcessingError):
        detect_blank_pages(broken, threshold=0.004)


# ---------------------------------------------------------------------------
# remove_pages


def test_remove_pages(tmp_path: Path):
    src = make_text_pdf(tmp_path / "in.pdf", ["eins", "zwei", "drei", "vier"])
    dst = tmp_path / "out" / "deblanked.pdf"
    remove_pages(src, dst, [2, 4])
    assert extract_pages(dst) == ["eins", "drei"]
    assert len(extract_pages(src)) == 4  # source untouched
    _no_leftovers(dst.parent)


def test_remove_pages_in_place_and_noop(tmp_path: Path):
    src = make_text_pdf(tmp_path / "in.pdf", ["eins", "zwei"])
    copy = tmp_path / "copy.pdf"
    remove_pages(src, copy, [])
    assert _sha(copy) == _sha(src)
    remove_pages(src, src, [1])
    assert extract_pages(src) == ["zwei"]


def test_remove_pages_refuses_to_remove_everything(tmp_path: Path):
    src = make_text_pdf(tmp_path / "in.pdf", ["eins", "zwei"])
    dst = tmp_path / "out.pdf"
    with pytest.raises(ValueError):
        remove_pages(src, dst, [1, 2])
    with pytest.raises(ValueError):
        remove_pages(src, dst, [3])
    with pytest.raises(ValueError):
        remove_pages(src, dst, [0])
    assert not dst.exists()


def test_scan_pipeline_roundtrip(tmp_path: Path, samples):
    """normalize -> detect -> remove, as the pipeline does it."""
    scan = samples.image(tmp_path / "seite1.png", ["Versicherungsschein", "Nr. 123"])
    back = tmp_path / "seite2.jpg"
    _add_dust(_scan_paper()).save(back, format="JPEG", quality=85, dpi=(150, 150))
    normalized = tmp_path / "work" / "normalized.pdf"
    assert normalize_to_pdf([scan, back], normalized) == 2
    blank = detect_blank_pages(normalized, threshold=0.004)
    assert blank == [2]
    deblanked = tmp_path / "work" / "deblanked.pdf"
    remove_pages(normalized, deblanked, blank)
    with pikepdf.open(deblanked) as pdf:
        assert len(pdf.pages) == 1
    assert sorted(os.listdir(normalized.parent)) == ["deblanked.pdf", "normalized.pdf"]
