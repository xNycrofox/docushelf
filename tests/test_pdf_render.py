"""Tests for docushelf.pdf.render: page previews and the thumbnail cache."""

from __future__ import annotations

import io
import os
import threading
import time
from pathlib import Path

import pikepdf
import pytest
from PIL import Image

from docushelf.errors import NotFound, ProcessingError
from docushelf.pdf import render
from docushelf.pdf.render import ThumbnailCache, render_page_png


def _png_size(data: bytes) -> tuple[int, int]:
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(io.BytesIO(data)) as image:
        return image.size


@pytest.fixture
def pdf(tmp_path: Path, samples) -> Path:
    return samples.text_pdf(tmp_path / "doc.pdf", ["Erste Seite", "Zweite Seite"])


def test_render_page_png_width_and_aspect(pdf: Path):
    width, height = _png_size(render_page_png(pdf, 1, width=300))
    assert width == 300
    assert height == pytest.approx(300 * 841.89 / 595.28, abs=2)
    assert _png_size(render_page_png(pdf, 2))[0] == 300  # default width


def test_render_shows_page_content(tmp_path: Path, samples):
    scan = samples.scan_pdf(tmp_path / "scan.pdf", [["DUNKLER TEXT " * 3] * 30])
    with Image.open(io.BytesIO(render_page_png(scan, 1, width=400))) as image:
        gray = image.convert("L")
        assert min(gray.getextrema()) < 100  # text is visible
        assert gray.getpixel((2, 2)) > 240  # white paper


def test_width_is_clamped(pdf: Path):
    assert _png_size(render_page_png(pdf, 1, width=10))[0] == 64
    assert _png_size(render_page_png(pdf, 1, width=50_000))[0] == 2000
    assert render.clamp_width("abc") == 300


def test_very_long_pages_are_limited(tmp_path: Path):
    receipt = tmp_path / "bon.pdf"
    with pikepdf.new() as pdf:
        pdf.add_blank_page(page_size=(140, 14000))
        pdf.save(receipt)
    width, height = _png_size(render_page_png(receipt, 1, width=2000))
    assert width * height <= render.MAX_PIXELS * 1.01
    assert height / width == pytest.approx(100, rel=0.05)


def test_rotated_page_renders_landscape(tmp_path: Path, pdf: Path):
    rotated = tmp_path / "rotated.pdf"
    with pikepdf.open(pdf) as doc:
        doc.pages[1].Rotate = 90
        doc.save(rotated)
    width, height = _png_size(render_page_png(rotated, 2, width=400))
    assert width == 400 and height < width


def test_missing_pages_and_files(pdf: Path, tmp_path: Path):
    for page in (0, 3, -1):
        with pytest.raises(NotFound):
            render_page_png(pdf, page)
    with pytest.raises(NotFound):
        render_page_png(tmp_path / "nope.pdf", 1)


def test_unreadable_files(tmp_path: Path, pdf: Path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 kaputt")
    with pytest.raises(ProcessingError):
        render_page_png(broken, 1)
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(pdf) as doc:
        doc.save(locked, encryption=pikepdf.Encryption(owner="o", user="u"))
    with pytest.raises(ProcessingError, match="passwortgeschützt"):
        render_page_png(locked, 1)


# ---------------------------------------------------------------------------
# ThumbnailCache


def test_cache_layout_and_hit(tmp_path: Path, pdf: Path, monkeypatch):
    cache = ThumbnailCache(tmp_path / "cache")
    calls = []
    original = render.render_page_png

    def counting(path, page, *, width=300):
        calls.append((path, page, width))
        return original(path, page, width=width)

    monkeypatch.setattr(render, "render_page_png", counting)
    key = "ab12cd34" * 8
    first = cache.get(pdf, 2, 240, key=key)
    assert first == tmp_path / "cache" / "ab" / f"{key}-p2-w240.png"
    assert _png_size(first.read_bytes())[0] == 240
    assert cache.get(pdf, 2, 240, key=key) == first
    assert len(calls) == 1
    # Width is clamped before it becomes part of the name.
    assert cache.get(pdf, 1, 99999, key=key).name == f"{key}-p1-w2000.png"
    assert [p.name for p in first.parent.iterdir() if p.name.startswith(".")] == []


def test_cache_renders_once_under_concurrency(tmp_path: Path, pdf: Path, monkeypatch):
    cache = ThumbnailCache(tmp_path / "cache")
    calls = []
    original = render.render_page_png

    def slow(path, page, *, width=300):
        calls.append(page)
        time.sleep(0.2)
        return original(path, page, width=width)

    monkeypatch.setattr(render, "render_page_png", slow)
    results: list[Path] = []
    errors: list[BaseException] = []

    def worker():
        try:
            results.append(cache.get(pdf, 1, 300, key="item-20250101-x"))
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert not errors
    assert len(results) == 8 and len(set(results)) == 1
    assert calls == [1]
    assert cache._locks == {}


def test_unsafe_keys_stay_inside_the_cache(tmp_path: Path, pdf: Path):
    cache = ThumbnailCache(tmp_path / "cache")
    for key in ("../../etc/passwd", "..", "a/b", "", "ümlaut key"):
        path = cache.get(pdf, 1, 64, key=key)
        assert path.resolve().is_relative_to((tmp_path / "cache").resolve())
        assert path.parent.parent == tmp_path / "cache"


def test_empty_cache_file_is_rendered_again(tmp_path: Path, pdf: Path):
    cache = ThumbnailCache(tmp_path / "cache")
    target = cache.path_for("deadbeef", 1, 100)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"")
    assert cache.get(pdf, 1, 100, key="deadbeef") == target
    assert _png_size(target.read_bytes())[0] == 100


def test_missing_page_is_not_cached(tmp_path: Path, pdf: Path):
    cache = ThumbnailCache(tmp_path / "cache")
    with pytest.raises(NotFound):
        cache.get(pdf, 9, 100, key="deadbeef")
    assert not cache.path_for("deadbeef", 9, 100).exists()


def test_old_hits_are_touched(tmp_path: Path, pdf: Path):
    cache = ThumbnailCache(tmp_path / "cache")
    path = cache.get(pdf, 1, 100, key="cafe")
    old = time.time() - 3 * 24 * 3600
    os.utime(path, (old, old))
    cache.get(pdf, 1, 100, key="cafe")
    assert path.stat().st_mtime > old + 3600


def test_prune(tmp_path: Path):
    cache = ThumbnailCache(tmp_path / "cache")
    now = time.time()
    files = []
    for number in range(6):
        path = cache.path_for(f"key{number}", 1, 300)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 1000)
        age = now - (10 - number) * 3600  # key0 is the oldest
        os.utime(path, (age, age))
        files.append(path)
    recent = cache.path_for("fresh", 1, 300)
    recent.parent.mkdir(parents=True, exist_ok=True)
    recent.write_bytes(b"x" * 1000)  # just written: never pruned
    stale_tmp = files[0].parent / ".docushelf-0123.part"
    stale_tmp.write_bytes(b"partial")
    os.utime(stale_tmp, (now - 7200, now - 7200))
    foreign = tmp_path / "cache" / "README.txt"
    foreign.write_text("not ours", encoding="utf-8")

    removed = cache.prune(max_bytes=3500)
    assert removed == 5  # four oldest thumbnails + the stale temp file
    assert [p.exists() for p in files] == [False, False, False, False, True, True]
    assert recent.exists() and foreign.exists() and not stale_tmp.exists()
    assert cache.prune(max_bytes=3500) == 0


def test_prune_without_cache_dir(tmp_path: Path):
    assert ThumbnailCache(tmp_path / "missing").prune() == 0
