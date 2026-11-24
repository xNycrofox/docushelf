"""Shared fixtures: settings on a temporary data directory and sample documents."""

from __future__ import annotations

import io
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image, ImageDraw, ImageFont


def _bind_authlib_to_httpx() -> None:
    """Make authlib's HTTP client use ``httpx`` so that respx can mock the identity provider.

    authlib prefers ``httpx2`` when it is installed (another dependency pulls it in), but
    respx only patches ``httpx``. The two share the same API; production code is unaffected.
    """
    real = sys.modules.get("httpx2")
    sys.modules["httpx2"] = httpx
    try:
        import authlib.integrations.httpx_client  # noqa: F401
        import authlib.integrations.starlette_client  # noqa: F401
    finally:
        if real is not None:
            sys.modules["httpx2"] = real
        else:
            del sys.modules["httpx2"]


_bind_authlib_to_httpx()

from docushelf.audit import AuditLog
from docushelf.config import Settings, load_settings
from docushelf.inbox.items import InboxStore
from docushelf.index.db import Index
from docushelf.layout import Layout
from docushelf.notify import Notifier


def _has_ocr() -> bool:
    # Same lookup as the application (also finds ocrmypdf next to a non-activated venv's python).
    from docushelf.pdf.ocr import ocr_available

    return ocr_available()


requires_ocr = pytest.mark.skipif(not _has_ocr(), reason="ocrmypdf/tesseract not installed")


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
def env(data_dir: Path) -> dict[str, str]:
    """Environment for load_settings; tests may extend it before requesting ``settings``."""
    return {
        "DOCUSHELF_DATA_DIR": str(data_dir),
        "DOCUSHELF_START_BACKGROUND": "false",
        "DOCUSHELF_OCR_ENABLED": "true" if _has_ocr() else "false",
        "DOCUSHELF_BASE_URL": "http://testserver",
        "DOCUSHELF_SECRET_KEY": "test-secret-key-0123456789abcdef",
        "DOCUSHELF_ADMIN_PASSWORD": "test-admin-pw",
        "DOCUSHELF_STABLE_SECONDS": "0",
        "DOCUSHELF_REMOTE_STABLE_SECONDS": "0",
        "TZ": "Europe/Berlin",
    }


@pytest.fixture
def settings(env: dict[str, str]) -> Settings:
    return load_settings(env)


@pytest.fixture
def layout(settings: Settings) -> Layout:
    layout = Layout.from_settings(settings)
    layout.ensure(tuple(s.name for s in settings.sources if s.path is None))
    return layout


@pytest.fixture
def base_services(settings: Settings, layout: Layout):
    """The foundation of ``Services`` without the components.

    Unit tests attach the collaborators they need (real or fake), e.g.
    ``base_services.reconciler = Reconciler(base_services)``.
    """
    index = Index(layout.db_path)
    index.init_schema()
    stub = SimpleNamespace(
        settings=settings,
        layout=layout,
        index=index,
        audit=AuditLog(layout.audit),
        inbox=InboxStore(layout),
        notifier=Notifier(None),
        archive_lock=threading.RLock(),
        needs_full_scan=False,
    )
    yield stub
    index.close()


@pytest.fixture
def services(settings: Settings):
    """The complete service container (background threads not started)."""
    from docushelf.services import Services

    instance = Services(settings)
    yield instance
    instance.stop()
    instance.index.close()


# ---------------------------------------------------------------------------
# Sample documents


def make_text_pdf(path: Path, pages: list[str]) -> Path:
    """A born-digital PDF with a text layer; each entry is one page (lines split on \\n)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=A4)
    for text in pages:
        y = 800
        for line in text.split("\n"):
            pdf.drawString(60, y, line)
            y -= 16
        pdf.showPage()
    pdf.save()
    return path


def render_page_image(lines: list[str], *, size: tuple[int, int] = (1240, 1754), font_size: int = 34) -> Image.Image:
    """A white A4 page at 150 dpi with dark text, like a scan."""
    image = Image.new("RGB", size, "white")
    if lines:
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default(size=font_size)
        y = 120
        for line in lines:
            draw.text((110, y), line, fill=(20, 20, 20), font=font)
            y += int(font_size * 1.6)
    return image


def make_image(path: Path, lines: list[str], *, size: tuple[int, int] = (1240, 1754), dpi: int = 150) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = render_page_image(lines, size=size)
    fmt = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".tif": "TIFF", ".tiff": "TIFF"}.get(path.suffix.lower(), "PNG")
    image.save(path, format=fmt, dpi=(dpi, dpi))
    return path


def make_scan_pdf(path: Path, pages: list[list[str]], *, dpi: int = 150) -> Path:
    """An image-only PDF (no text layer). An empty list of lines gives a blank page."""
    import img2pdf

    path.parent.mkdir(parents=True, exist_ok=True)
    blobs = []
    for lines in pages:
        buffer = io.BytesIO()
        render_page_image(lines).save(buffer, format="PNG", dpi=(dpi, dpi))
        blobs.append(buffer.getvalue())
    path.write_bytes(img2pdf.convert(blobs))
    return path


@pytest.fixture
def samples():
    """Access to the sample builders from tests: ``samples.text_pdf(path, [...])``."""
    return SimpleNamespace(
        text_pdf=make_text_pdf,
        image=make_image,
        scan_pdf=make_scan_pdf,
        page_image=render_page_image,
    )
