"""The embedded document viewer and the wide layouts.

The pages are server-rendered page images inside the viewer markup (works
without JavaScript); viewer.js upgrades them to PDF.js pages when the vendored
build is present. No iframe and no browser PDF chrome any more.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest
from fastapi import FastAPI
from starlette.staticfiles import StaticFiles
from starlette.testclient import TestClient

from archive_support import add_archive_file, add_archive_pdf, make_encrypted_pdf, make_ready_item
from docushelf.web import app as web_app
from docushelf.web import templating
from test_ui import STATIC, build_app, get_page, parse
from test_ui import svc  # noqa: F401 - the same service fixture


@pytest.fixture
def client(svc):  # noqa: F811
    with TestClient(build_app(svc), base_url="http://testserver") as test_client:
        yield test_client


def viewer_attrs(html: str) -> dict[str, str | None]:
    viewers = [attrs for tag, attrs in parse(html).tags if tag == "div" and "data-viewer" in attrs]
    assert len(viewers) == 1, viewers
    return viewers[0]


def scripts(html: str) -> list[str]:
    return [attrs["src"] or "" for tag, attrs in parse(html).tags if tag == "script" and attrs.get("src")]


# ---------------------------------------------------------------------------
# Markup


def test_document_page_has_the_viewer_and_no_iframe(client, svc, samples):  # noqa: F811
    record = add_archive_pdf(svc, samples, "Finanzen/2025-03-12 Kontoauszug.pdf", text="Saldo")
    html = get_page(client, f"/dokument/{record.id}")
    assert "<iframe" not in html and "#view=FitH" not in html
    attrs = viewer_attrs(html)
    assert attrs["data-src"] == f"/api/v1/documents/{record.id}/file"
    assert attrs["data-pages"] == f"/api/v1/documents/{record.id}/pages"
    assert attrs["data-version"] == record.content_hash[:12]
    pages = record.page_count or 1
    assert attrs["data-page-count"] == str(pages)
    assert html.count('class="viewer-page"') == pages
    assert f"/api/v1/documents/{record.id}/pages/1.png?w=1400&amp;v={record.content_hash[:12]}" in html
    assert any(src.startswith("/static/viewer.js") for src in scripts(html))
    # Toolbar: zoom (shown by the script), page counter, download, new tab.
    assert 'data-zoom="in"' in html and 'data-zoom="out"' in html and 'data-zoom="fit"' in html
    assert "data-viewer-count" in html
    assert f'href="/api/v1/documents/{record.id}/file?download=1" download="2025-03-12 Kontoauszug.pdf"' in html
    assert f'href="/api/v1/documents/{record.id}/file" target="_blank" rel="noopener"' in html
    assert "In neuem Tab öffnen" in html and "Herunterladen" in html
    assert 'class="layout-wide' in html or "layout-wide" in re.search(r"<body[^>]*>", html).group(0)


def test_images_keep_the_image_preview(client, svc):  # noqa: F811
    photo = add_archive_file(svc, "Wohnung/Quittung.jpg", b"\xff\xd8\xff\xe0 kein echtes jpeg")
    html = get_page(client, f"/dokument/{photo.id}")
    assert "data-viewer" not in html and "viewer.js" not in html
    assert f'<div class="preview-image"><img src="/api/v1/documents/{photo.id}/file"' in html
    assert f'href="/api/v1/documents/{photo.id}/file?download=1"' in html


def test_inbox_item_has_the_viewer(client, svc, samples, tmp_path):  # noqa: F811
    item = make_ready_item(svc, samples, tmp_path)
    html = get_page(client, f"/eingang/{item.id}")
    assert "<iframe" not in html
    attrs = viewer_attrs(html)
    assert attrs["data-src"] == f"/api/v1/inbox/{item.id}/document"
    assert attrs["data-pages"] == f"/api/v1/inbox/{item.id}/pages"
    assert f"/api/v1/inbox/{item.id}/pages/1.png?w=1400" in html
    assert f'href="/api/v1/inbox/{item.id}/document?download=1"' in html
    assert f"/api/v1/inbox/{item.id}/original/1?download=1" in html  # the originals stay below


def test_password_protected_item_keeps_the_placeholder(client, svc, samples, tmp_path):  # noqa: F811
    locked = make_encrypted_pdf(samples, tmp_path / "quelle" / "Vertrag.pdf", ["Vertraulich"])
    item = make_ready_item(svc, samples, tmp_path, original=locked, context={"pdf_protection": "encrypted"})
    html = get_page(client, f"/eingang/{item.id}")
    assert "data-preview-placeholder" in html and "data-viewer" not in html and "<iframe" not in html
    # Pages that could be counted are shown as page images; PDF.js gives up on the password.
    item.page_count = 2
    svc.inbox.save(item)
    html = get_page(client, f"/eingang/{item.id}")
    assert viewer_attrs(html)["data-page-count"] == "2" and "<iframe" not in html


def test_pdfjs_is_only_offered_when_it_is_vendored(client, svc, samples, tmp_path, monkeypatch):  # noqa: F811
    record = add_archive_pdf(svc, samples, "A/2025-01-01 Brief.pdf")
    vendored = (STATIC / "vendor" / "pdfjs" / "pdf.min.mjs").is_file()
    attrs = viewer_attrs(get_page(client, f"/dokument/{record.id}"))
    assert ("data-pdfjs" in attrs) == vendored
    # With the build in place the viewer gets the library, the worker and the decoder folder.
    static = tmp_path / "static"
    (static / "vendor" / "pdfjs").mkdir(parents=True)
    (static / "vendor" / "pdfjs" / "pdf.min.mjs").write_text("export {};\n")
    (static / "vendor" / "pdfjs" / "pdf.worker.min.mjs").write_text("export {};\n")
    (static / "viewer.js").write_text("\n")
    (static / "icons.svg").write_text("<svg></svg>")
    monkeypatch.setattr(templating, "_static_versions", templating._StaticVersions(static))
    attrs = viewer_attrs(get_page(client, f"/dokument/{record.id}"))
    assert re.fullmatch(r"/static/vendor/pdfjs/pdf\.min\.mjs\?v=[0-9a-f]{10}", attrs["data-pdfjs"] or "")
    assert re.fullmatch(r"/static/vendor/pdfjs/pdf\.worker\.min\.mjs\?v=[0-9a-f]{10}", attrs["data-pdfjs-worker"] or "")
    assert attrs["data-pdfjs-wasm"] == "/static/vendor/pdfjs/wasm/"


def test_wide_pages_and_narrow_text_pages(client, svc, samples):  # noqa: F811
    def body_class(url: str) -> str:
        return re.search(r'<body class="([^"]*)"', get_page(client, url)).group(1)

    for url in ("/archiv", "/eingang", "/suche"):
        assert "layout-wide" in body_class(url), url
    for url in ("/einstellungen", "/regeln"):
        assert "layout-wide" not in body_class(url), url


# ---------------------------------------------------------------------------
# Content-Security-Policy and static files


def test_csp_allows_the_viewer_and_nothing_broader(svc):  # noqa: F811
    policy = web_app.security_headers(svc.settings)["Content-Security-Policy"]
    directives = {part.split()[0]: part.split()[1:] for part in policy.split("; ")}
    # PDF.js runs its parser in a module worker loaded from the app itself.
    assert directives["worker-src"] == ["'self'"]
    assert directives["default-src"] == ["'self'"]
    # No eval, no inline code or styles, no WebAssembly (PDF.js falls back to JavaScript decoders).
    assert "unsafe-eval" not in policy and "unsafe-inline" not in policy and "wasm-unsafe-eval" not in policy
    assert "script-src" not in directives and "style-src" not in directives
    assert "http" not in policy.split("form-action")[0]


def static_client(directory) -> TestClient:
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=directory), name="static")
    return TestClient(app)


@pytest.mark.parametrize(("name", "kind"), [
    ("vendor/pdfjs/pdf.min.mjs", "javascript"),
    ("vendor/pdfjs/pdf.worker.min.mjs", "javascript"),
    ("vendor/pdfjs/wasm/openjpeg.wasm", "application/wasm"),
    ("viewer.js", "javascript"),
])
def test_static_types_for_module_scripts_and_wasm(tmp_path, name, kind):
    """Module scripts and workers are refused by browsers unless served as JavaScript."""
    target = tmp_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x00asm" if name.endswith(".wasm") else b"export {};\n")
    with static_client(tmp_path) as client:
        response = client.get(f"/static/{name}")
    assert response.status_code == 200 and kind in response.headers["content-type"]


def test_vendored_pdfjs_is_complete_and_served(svc):  # noqa: F811
    vendor = STATIC / "vendor" / "pdfjs"
    if not (vendor / "pdf.min.mjs").is_file():
        pytest.skip("PDF.js is not vendored in this checkout")
    for name in ("pdf.min.mjs", "pdf.worker.min.mjs", "LICENSE"):
        assert (vendor / name).is_file(), name
    with static_client(STATIC) as client:
        for name in ("pdf.min.mjs", "pdf.worker.min.mjs"):
            response = client.get(f"/static/vendor/pdfjs/{name}")
            assert response.status_code == 200 and "javascript" in response.headers["content-type"]
    notices = (STATIC.parent.parent / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "pdfjs-dist" in notices and "Apache" in notices


def test_viewer_script_is_self_contained():
    js = (STATIC / "viewer.js").read_text(encoding="utf-8")
    assert "http://" not in js and "https://" not in js
    assert "eval(" not in js and "new Function" not in js and "innerHTML" not in js
    assert 'setAttribute("style"' not in js and ".cssText" not in js
    assert "isEvalSupported: false" in js  # PDF.js must not try eval under the policy
    # Shortcuts + - 0, the file endpoints from the markup, password files stay as images.
    for needle in ('"+": "in"', '"-": "out"', '"0": "fit"', "dataset.src", "onPassword", "TextLayer"):
        assert needle in js, needle


def test_css_has_the_viewer_and_no_browser_frame():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert ".preview-frame" not in css and "iframe" not in css
    for selector in (".viewer-page", ".viewer-scroll", ".textLayer", ".layout-wide .content", "@media (min-width: 1900px)"):
        assert selector in css, selector
    # The form column of the item pages stays readable; the viewer takes the rest.
    assert re.search(r"\.item-layout \{[^}]*minmax\(0, 1fr\) clamp\(360px, 28vw, 470px\)", css)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("name", ["viewer.js", "app.js"])
def test_javascript_syntax(name):
    result = subprocess.run(["node", "--check", str(STATIC / name)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
