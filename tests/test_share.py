"""Sharing from phones: web app manifest, service worker, the share page and its server fallback.

The application runs on the real foundation (index, inbox, audit, tokens) with a
local login; uploads land in the real upload drop zone. Only the intake is a
fake that counts how often it was woken.
"""

from __future__ import annotations

import json
import math
import re
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from docushelf.channels.sendung import MANIFEST, read_sendung
from docushelf.web import routes_share
from docushelf.web.app import SESSION_COOKIE
from web_support import FakeProvider, client_for, login_local, make_app

STATIC = Path(routes_share.__file__).resolve().parent.parent / "static"
LOGO_BLUE = (0x50, 0x48, 0xE5)
# The smallest HEIC an iPhone could send: an ISO media "ftyp" box with the heic brand.
HEIC_BYTES = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64

EXPECTED_SHARE_TARGET = {
    "action": "/teilen",
    "method": "POST",
    "enctype": "multipart/form-data",
    "params": {
        "title": "title",
        "text": "text",
        "files": [
            {
                "name": "files",
                "accept": ["application/pdf", "image/*", ".pdf", ".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff", ".webp"],
            }
        ],
    },
}


class FakeIntake:
    def __init__(self) -> None:
        self.triggered = 0

    def trigger(self) -> None:
        self.triggered += 1


@pytest.fixture
def services(base_services):
    base_services.intake = FakeIntake()
    return base_services


@pytest.fixture
def client(services):
    return client_for(make_app(services))


@pytest.fixture
def browser(client):
    """A client with a logged-in browser session (local account)."""
    assert login_local(client, next_url="/eingang").status_code == 303
    return client


def batches(services) -> list:
    return [read_sendung(path.parent) for path in sorted(services.layout.source_dir("upload").glob(f"*/{MANIFEST}"))]


def share(client, files=None, *, site: str | None = "none", data=None, headers=None):
    """POST /teilen like a share sheet does: multipart, no CSRF token."""
    sent = dict(headers or {})
    if site is not None:
        sent["Sec-Fetch-Site"] = site
    return client.post("/teilen", files=files, data=data or {}, headers=sent, follow_redirects=False)


def phone_files(samples, tmp_path) -> list:
    """What a phone typically shares: a PDF, two photos both called image.jpeg and a HEIC photo."""
    pdf = samples.text_pdf(tmp_path / "Rechnung.pdf", ["Stadtwerke Musterstadt", "Rechnung"]).read_bytes()
    photo1 = samples.image(tmp_path / "a.jpg", ["Seite 1"]).read_bytes()
    photo2 = samples.image(tmp_path / "b.jpg", ["Seite 2"]).read_bytes()
    return [
        ("files", ("Rechnung.pdf", pdf, "application/pdf")),
        ("files", ("image.jpeg", photo1, "image/jpeg")),
        ("files", ("image.jpeg", photo2, "image/jpeg")),
        ("files", ("IMG_0001.HEIC", HEIC_BYTES, "image/heic")),
    ]


def flash_texts(html: str) -> list[str]:
    return [
        re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", match)).strip()
        for match in re.findall(r'<p class="flash-text">(.*?)</p>', html, re.S)
    ]


class _Page(HTMLParser):
    """Collects what the Content-Security-Policy would block and all script sources."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.problems: list[str] = []
        self.scripts: list[str] = []
        self.ids: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if data.get("id"):
            self.ids.append(data["id"])
        if "style" in data or any(name.startswith("on") for name in data):
            self.problems.append(f"{tag} {data}")
        for name in ("href", "src", "action"):
            value = data.get(name) or ""
            if value.startswith(("http://", "https://", "//", "javascript:")):
                self.problems.append(f"{tag} {name}={value}")
        if tag == "script":
            self._in_script = True
            if data.get("src"):
                self.scripts.append(data["src"])
            elif data.get("type") != "application/json":
                self.problems.append("inline script")

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script and data.strip():
            self.problems.append("inline script content")


def inspect(html: str) -> _Page:
    page = _Page()
    page.feed(html)
    assert not page.problems, page.problems
    assert len(page.ids) == len(set(page.ids)), page.ids
    return page


# ---------------------------------------------------------------------------
# Manifest and icons


def test_manifest_is_public_and_describes_the_app(client):
    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/manifest+json"
    assert response.headers["cache-control"] == "no-cache"
    data = json.loads(response.text)
    assert data["name"] == data["short_name"] == "DocuShelf"
    assert data["start_url"] == "/eingang"
    assert data["scope"] == "/"
    assert data["display"] == "standalone"
    assert data["lang"] == "de"
    assert data["share_target"] == EXPECTED_SHARE_TARGET
    # The colours come from the palette of the stylesheet.
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert f"--bg: {data['background_color']};" in css
    assert f"--bg: {data['theme_color']};" in css
    assert client.head("/manifest.webmanifest").status_code == 200


def test_manifest_icons_exist_with_the_announced_sizes(client):
    icons = json.loads(client.get("/manifest.webmanifest").text)["icons"]
    announced = {(icon["sizes"], icon["purpose"]) for icon in icons}
    assert {("192x192", "any"), ("512x512", "any"), ("192x192", "maskable"), ("512x512", "maskable")} <= announced
    for icon in icons:
        assert icon["type"] == "image/png"
        response = client.get(icon["src"])  # static files need no login either
        assert response.status_code == 200, icon
        assert response.headers["content-type"] == "image/png"
        image = Image.open(BytesIO(response.content))
        width, height = (int(n) for n in icon["sizes"].split("x"))
        assert image.size == (width, height), icon
    touch = client.get("/static/icons/apple-touch-icon.png")
    assert touch.status_code == 200
    assert Image.open(BytesIO(touch.content)).size == (180, 180)


def _close(color, expected, tolerance: int = 6) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(color, expected))


@pytest.mark.parametrize("name", ["icon-192.png", "icon-512.png"])
def test_regular_icons_are_the_logo_with_rounded_corners(name):
    image = Image.open(STATIC / "icons" / name).convert("RGBA")
    size = image.size[0]
    assert image.getpixel((0, 0))[3] == 0  # transparent outside the rounded square
    assert _close(image.getpixel((size // 10, size // 2))[:3], LOGO_BLUE)


@pytest.mark.parametrize(
    "name, radius",
    [
        # Maskable icons: everything that matters lies in the safe zone, a circle with 40 % radius.
        ("icon-maskable-192.png", 0.4),
        ("icon-maskable-512.png", 0.4),
        # iOS cuts a rounded square with small corners; a little less padding is fine there.
        ("apple-touch-icon.png", 0.45),
    ],
)
def test_full_bleed_icons_keep_the_logo_away_from_the_edges(name, radius):
    image = Image.open(STATIC / "icons" / name)
    assert image.mode == "RGB"  # no transparency: launchers and iOS cut the shape themselves
    width, height = image.size
    pixels = image.load()
    assert _close(pixels[0, 0], LOGO_BLUE) and _close(pixels[width - 1, height - 1], LOGO_BLUE)
    farthest = 0.0
    for y in range(0, height, 2):
        for x in range(0, width, 2):
            if not _close(pixels[x, y], LOGO_BLUE, 12):
                farthest = max(farthest, math.hypot(x + 0.5 - width / 2, y + 0.5 - height / 2) / width)
    assert 0.3 < farthest <= radius


# ---------------------------------------------------------------------------
# Service worker and security headers


def test_service_worker_is_served_from_the_root(client):
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] == "text/javascript"
    assert response.headers["cache-control"] == "no-cache"
    assert response.content == (STATIC / "sw.js").read_bytes()
    assert "docushelf-teilen" in response.text and '"/teilen"' in response.text
    assert response.headers["x-content-type-options"] == "nosniff"
    assert client.head("/sw.js").status_code == 200


def test_cache_name_is_the_same_everywhere():
    assert routes_share.SHARE_CACHE == "docushelf-teilen"
    for name in ("sw.js", "share.js"):
        assert f'"{routes_share.SHARE_CACHE}"' in (STATIC / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("path", ["/login", "/manifest.webmanifest", "/sw.js", "/teilen", "/api/v1/status", "/healthz"])
def test_csp_allows_manifest_and_service_worker(client, path):
    policy = client.get(path, follow_redirects=False).headers["content-security-policy"]
    directives = [part.strip() for part in policy.split(";")]
    assert "manifest-src 'self'" in directives
    assert "worker-src 'self'" in directives
    assert "default-src 'self'" in directives
    assert directives[-1] == "form-action 'self'"
    assert "unsafe-inline" not in policy and "unsafe-eval" not in policy


def test_csp_keeps_the_identity_provider_on_form_action(services):
    provider = FakeProvider()
    client = client_for(make_app(services, **provider.settings()))
    policy = client.get("/manifest.webmanifest").headers["content-security-policy"]
    assert policy.endswith("form-action 'self' https://idp.example.test")
    assert "worker-src 'self';" in policy and "manifest-src 'self';" in policy


# ---------------------------------------------------------------------------
# The share page


def test_share_page_requires_login(client):
    response = client.get("/teilen", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fteilen"


def test_share_page_for_a_logged_in_user(browser):
    response = browser.get("/teilen")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    html = response.text
    page = inspect(html)
    assert [src.split("?")[0] for src in page.scripts].count("/static/share.js") == 1
    assert 'data-share ' in html and 'data-cache="docushelf-teilen"' in html
    assert 'data-api="/api/v1/upload"' in html
    assert re.search(r'data-max-mb="\d+"', html)
    assert "Bilder zu einem Dokument zusammenfügen" in html
    # Install help for Android and the Shortcut for iPhones.
    assert "App installieren" in html and "Zum Startbildschirm hinzufügen" in html
    assert "DocuShelf senden" in html and 'href="/einstellungen/handy#tokens"' in html
    assert "contrib/ios/README.md" in html
    assert 'value="http://testserver"' in html
    assert '<meta name="csrf-token" content="' in html


def test_share_page_explains_notices(browser):
    html = browser.get("/teilen?hinweis=leer").text
    assert "Es sind keine Dateien angekommen" in html
    html = browser.get("/teilen?hinweis=abgelehnt").text
    assert "aus Sicherheitsgründen nicht übernommen" in html
    html = browser.get("/teilen?hinweis=%3Cscript%3E").text
    assert "alert-warning" not in html and "<script>" not in html


def test_logged_out_visitors_see_the_message_on_the_login_page(client):
    response = client.get("/teilen?hinweis=abgelehnt", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login?next=%2Fteilen"
    login = client.get(response.headers["location"])
    assert any("aus Sicherheitsgründen nicht übernommen" in text for text in flash_texts(login.text))


def test_share_help_partial_renders_on_its_own(browser):
    """The settings page includes the partial with its own context (``settings``, no ``base_url``)."""
    from starlette.requests import Request

    from docushelf.web import templating

    app = browser.app
    scope = {"type": "http", "method": "GET", "path": "/einstellungen", "headers": [], "query_string": b"", "app": app}
    request = Request(scope)
    html = templating.environment.get_template("_share_help.html").render(
        {"request": request, "settings": app.state.settings, "csrf_token": "x"}
    )
    page = inspect(html)
    assert page.scripts and page.scripts[0].startswith("/static/share.js")
    assert 'value="http://testserver"' in html
    assert "data-share-install" in html and "data-share-installed" in html


# ---------------------------------------------------------------------------
# POST /teilen without service worker


@pytest.mark.parametrize("site", ["none", "same-origin"])
def test_fallback_stores_shared_files_like_an_upload(browser, services, samples, tmp_path, site):
    response = share(browser, phone_files(samples, tmp_path), site=site, data={"title": "Brief", "text": ""})
    assert response.status_code == 303
    assert response.headers["location"] == "/teilen"
    [batch] = batches(services)
    assert [document.files for document in batch.documents] == [
        ["Rechnung.pdf"],
        ["image.jpeg"],
        ["image (2).jpeg"],
        ["IMG_0001.HEIC"],
    ]
    assert batch.context == {
        "uploader": "admin",
        "original_names": ["Rechnung.pdf", "image.jpeg", "image.jpeg", "IMG_0001.HEIC"],
    }
    assert batch.options == {}
    assert (batch.directory / "IMG_0001.HEIC").read_bytes() == HEIC_BYTES
    assert services.intake.triggered == 1
    entry = services.audit.tail(1)[0]
    assert (entry["action"], entry["user"], entry["via"], entry["files"]) == ("upload", "admin", "teilen", 4)
    assert entry["batch"] == batch.directory.name
    page = browser.get("/teilen").text
    assert "4 Dateien übernommen – sie werden jetzt verarbeitet." in " ".join(flash_texts(page))
    assert 'href="/eingang"' in page


@pytest.mark.parametrize("site", ["cross-site", "same-site", None])
def test_fallback_refuses_requests_from_elsewhere(browser, services, samples, tmp_path, site):
    response = share(browser, phone_files(samples, tmp_path), site=site)
    assert response.status_code == 303
    assert response.headers["location"] == "/teilen?hinweis=abgelehnt"
    assert "set-cookie" not in response.headers  # the session is left alone
    assert batches(services) == []
    assert not list(services.layout.source_dir("upload").iterdir())
    assert services.intake.triggered == 0
    page = browser.get(response.headers["location"]).text
    assert "aus Sicherheitsgründen nicht übernommen" in page


def test_fallback_needs_a_login(client, services, samples, tmp_path):
    response = share(client, phone_files(samples, tmp_path), site="none")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fteilen"
    assert batches(services) == []
    login = client.get("/login?next=%2Fteilen")
    assert login.status_code == 200
    assert any("Bitte melde dich an" in text for text in flash_texts(login.text))


def test_cross_site_post_without_login_never_touches_the_session(client, services, samples, tmp_path):
    response = share(client, phone_files(samples, tmp_path), site="cross-site")
    assert response.status_code == 303
    assert response.headers["location"] == "/teilen?hinweis=abgelehnt"
    assert "set-cookie" not in response.headers
    assert client.cookies.get(SESSION_COOKIE) is None
    follow = client.get(response.headers["location"], follow_redirects=False)
    assert follow.status_code == 303 and follow.headers["location"] == "/login?next=%2Fteilen"
    login = client.get(follow.headers["location"])
    assert any("aus Sicherheitsgründen" in text for text in flash_texts(login.text))
    assert batches(services) == []


def test_fallback_ignores_api_tokens(client, services, samples, tmp_path):
    token, _entry = services.tokens.create("Handy", "anna")
    response = share(client, phone_files(samples, tmp_path), site="none", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fteilen"
    assert batches(services) == []


def test_fallback_without_files(browser, services):
    response = share(browser, None, data={"title": "Ein Link", "text": "https://example.org"})
    assert response.status_code == 303 and response.headers["location"] == "/teilen"
    assert batches(services) == []
    assert any("keine Dateien angekommen" in text for text in flash_texts(browser.get("/teilen").text))


def test_fallback_reports_unsupported_files(browser, services, samples, tmp_path):
    pdf = samples.text_pdf(tmp_path / "ok.pdf", ["ok"]).read_bytes()
    files = [
        ("files", ("ok.pdf", pdf, "application/pdf")),
        ("files", ("Notizen.docx", b"PK\x03\x04 kein PDF", "application/octet-stream")),
    ]
    response = share(browser, files)
    assert response.status_code == 303 and response.headers["location"] == "/teilen"
    assert batches(services) == []
    assert services.intake.triggered == 0
    texts = flash_texts(browser.get("/teilen").text)
    assert any("Notizen.docx" in text and "nicht unterstützt" in text for text in texts), texts


def test_fallback_respects_the_size_limit(services):
    client = client_for(make_app(services, max_upload_mb=1))
    assert login_local(client).status_code == 303
    big = b"%PDF-1.4\n" + b"0" * (1024 * 1024 + 100)
    response = share(client, [("files", ("gross.pdf", big, "application/pdf"))])
    assert response.status_code == 303
    assert batches(services) == []
    assert any("zu groß" in text for text in flash_texts(client.get("/teilen").text))


# ---------------------------------------------------------------------------
# The upload API takes what share.js and the iPhone Shortcut send


def test_upload_api_accepts_what_phones_send(client, services, samples, tmp_path):
    token, _entry = services.tokens.create("iPhone Kurzbefehl", "anna")
    headers = {"Authorization": f"Bearer {token}"}
    response = client.post("/api/v1/upload", files=phone_files(samples, tmp_path), headers=headers)
    assert response.status_code == 202, response.text
    assert response.json()["documents"] == 4
    assert response.json()["files"] == ["Rechnung.pdf", "image.jpeg", "image.jpeg", "IMG_0001.HEIC"]
    # share.js sends the photos as one request with "combine" when they belong together.
    photos = [part for part in phone_files(samples, tmp_path) if part[1][2].startswith("image/")]
    combined = client.post("/api/v1/upload", files=photos, data={"combine": "true"}, headers=headers)
    assert combined.status_code == 202 and combined.json()["documents"] == 1
    batch = read_sendung(services.layout.source_dir("upload") / combined.json()["batch_id"])
    assert [document.files for document in batch.documents] == [["image.jpeg", "image (2).jpeg", "IMG_0001.HEIC"]]


def test_upload_api_from_the_share_page_needs_the_csrf_header(browser, samples, tmp_path):
    pdf = samples.text_pdf(tmp_path / "scan.pdf", ["x"]).read_bytes()
    files = {"files": ("scan.pdf", pdf, "application/pdf")}
    assert browser.post("/api/v1/upload", files=files).status_code == 403
    html = browser.get("/teilen").text
    token = re.search(r'<meta name="csrf-token" content="([^"]+)"', html).group(1)
    response = browser.post("/api/v1/upload", files=files, headers={"X-CSRF-Token": token, "Accept": "application/json"})
    assert response.status_code == 202
