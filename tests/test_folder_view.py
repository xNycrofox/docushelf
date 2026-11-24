"""The archive page as tiles or as an Explorer-like list ("Kacheln" / "Liste").

The choice lives in the cookie ``ds_ansicht`` and is read on the server, so the
page is right without JavaScript; ``?ansicht=…`` sets it without JavaScript.
"""

from __future__ import annotations

import re
from datetime import date
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from archive_support import add_archive_file, add_archive_pdf
from docushelf.models import DocMeta
from docushelf.web import routes_ui, views
from test_ui import STATIC, assert_clean_page, build_app, csrf_of, parse
from test_web_auto import svc  # noqa: F401 - the same service fixture

VIEW = views.VIEW_COOKIE


@pytest.fixture
def client(svc):  # noqa: F811
    with TestClient(build_app(svc), base_url="http://testserver") as test_client:
        yield test_client


@pytest.fixture
def archive(svc, samples):  # noqa: F811
    docs = SimpleNamespace(
        march=add_archive_pdf(svc, samples, "Finanzen/Bank/2025-03-12 Kontoauszug März.pdf",
                              meta=DocMeta(doc_id="11111111-1111-4111-8111-111111111111", title="Kontoauszug März", sender="Sparkasse")),
        tax=add_archive_pdf(svc, samples, "Finanzen/Steuer/2024-06-01 Steuerbescheid 2023.pdf"),
        nested=add_archive_pdf(svc, samples, "Finanzen/Steuer/2025/2025-09-30 Vorauszahlung.pdf"),
        root=add_archive_pdf(svc, samples, "Finanzen/2023-01-05 Jahresübersicht.pdf"),
        abo=add_archive_pdf(svc, samples, "Finanzen/2025-02-01 Abo.pdf",
                            meta=DocMeta(doc_id="22222222-2222-4222-8222-222222222222", title="Abo", sender="Verlag")),
        photo=add_archive_file(svc, "Finanzen/Zettel.jpg", b"\xff\xd8\xff\xe0 kein echtes jpeg"),
    )
    (svc.layout.archive / "Finanzen" / "Leer").mkdir()
    svc.index.add_folder_with_parents("Finanzen/Leer")
    return docs


def page(client, url: str) -> str:
    response = client.get(url)
    assert response.status_code == 200, (url, response.status_code, response.text[:500])
    assert_clean_page(response.text, csrf=csrf_of(response.text))
    return response.text


def panel(html: str, view: str) -> tuple[bool, str]:
    """(hidden, content) of a view panel."""
    match = re.search(rf'<div class="view-panel" data-view-panel="{view}"( hidden)?>(.*?)\n    </div>\n', html, re.S)
    assert match, view
    return bool(match.group(1)), match.group(2)


def list_rows(html: str) -> list[str]:
    _hidden, content = panel(html, "liste")
    body = content.split("<tbody>", 1)[1]
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", row)).strip() for row in re.findall(r"<tr class=.*?</tr>", body, re.S)]


def test_tiles_are_the_default_and_both_views_are_on_the_page(client, archive):
    html = page(client, "/archiv/Finanzen")
    hidden, tiles = panel(html, "kacheln")
    assert not hidden and 'class="folder-grid"' in tiles and 'class="doc-table"' in tiles
    hidden, listing = panel(html, "liste")
    assert hidden and 'class="list-table"' in listing
    toggle = re.search(r'<nav class="view-toggle".*?</nav>', html, re.S).group(0)
    assert 'href="/archiv/Finanzen?ansicht=liste"' in toggle and 'href="/archiv/Finanzen?ansicht=kacheln"' in toggle
    assert 'data-view="kacheln" aria-current="true"' in toggle and 'data-view="liste" aria-current="false"' in toggle
    assert html.index("view-toggle") < html.index("Neuer Ordner")


def test_choosing_the_view_without_javascript_sets_the_cookie(client, svc, samples, archive):  # noqa: F811
    response = client.get("/archiv/Finanzen?ansicht=liste&sort=title", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/archiv/Finanzen?sort=title"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{VIEW}=liste;") and "Max-Age=31536000" in cookie and "Path=/" in cookie
    assert "SameSite=lax" in cookie and "HttpOnly" not in cookie and "Secure" not in cookie
    html = page(client, "/archiv/Finanzen?sort=title")
    assert panel(html, "liste")[0] is False and panel(html, "kacheln")[0] is True
    assert 'data-view="liste" aria-current="true"' in html
    # The cookie applies to every folder.
    assert panel(page(client, "/archiv"), "liste")[0] is False
    # Unknown values change nothing; folder names with spaces stay valid URLs.
    response = client.get("/archiv/Finanzen?ansicht=riesig", follow_redirects=False)
    assert response.headers["location"] == "/archiv/Finanzen" and "set-cookie" not in response.headers
    add_archive_pdf(svc, samples, "Mit Leerzeichen/2025-01-01 X.pdf")
    response = client.get("/archiv/Mit%20Leerzeichen?ansicht=kacheln", follow_redirects=False)
    assert response.headers["location"] == "/archiv/Mit%20Leerzeichen"
    assert client.get("/archiv/Gibt/Es/Nicht?ansicht=liste", follow_redirects=False).status_code == 404


def test_cookie_is_secure_behind_https(svc, archive):  # noqa: F811
    svc.settings = svc.settings.with_overrides(base_url="https://docs.example.org")
    with TestClient(build_app(svc), base_url="http://testserver") as test_client:
        response = test_client.get("/archiv?ansicht=liste", follow_redirects=False)
    assert "Secure" in response.headers["set-cookie"]


def test_list_shows_folders_first_then_documents(client, archive):
    client.cookies.set(VIEW, "liste")
    html = page(client, "/archiv/Finanzen")
    _hidden, listing = panel(html, "liste")
    headers = [re.sub(r"<[^>]+>", "", h).strip() for h in re.findall(r"<th scope=\"col\"[^>]*>(.*?)</th>", listing, re.S)]
    assert headers == ["Name", "Datum", "Absender", "Größe / Inhalt"]
    rows = list_rows(html)
    assert rows[:3] == [
        "Steuer 30.09.2025 2 Dokumente · 1 Ordner",  # newest document inside, subfolders included
        "Bank 12.03.2025 1 Dokument",
        "Leer – 0 Dokumente",
    ]
    assert rows[3].startswith("Abo 01.02.2025 Verlag ") and rows[3].endswith(("KB", "Bytes"))
    assert rows[4].startswith("Jahresübersicht 05.01.2023")
    assert rows[5].startswith("Zettel JPG –") and rows[5].endswith("Bytes")
    # Every row has one real link (keyboard) and is marked for the whole-row click.
    tags = parse(listing).tags
    main_links = [attrs["href"] for tag, attrs in tags if tag == "a" and "data-row-main" in attrs]
    assert main_links[:3] == ["/archiv/Finanzen/Steuer", "/archiv/Finanzen/Bank", "/archiv/Finanzen/Leer"]
    assert f"/dokument/{archive.abo.id}" in main_links and len(main_links) == 6
    assert sum(1 for tag, attrs in tags if tag == "tr" and "data-row-link" in attrs) == 6
    assert 'aria-sort="descending"' in listing  # date, newest first by default


@pytest.mark.parametrize(("sort", "expected"), [
    ("title", ["Bank", "Leer", "Steuer", "Abo", "Jahresübersicht", "Zettel"]),
    ("title_desc", ["Steuer", "Leer", "Bank", "Zettel", "Jahresübersicht", "Abo"]),
    ("date_desc", ["Steuer", "Bank", "Leer", "Abo", "Jahresübersicht", "Zettel"]),
    ("date_asc", ["Bank", "Steuer", "Leer", "Jahresübersicht", "Abo", "Zettel"]),
])
def test_list_sorting(client, archive, sort, expected):
    client.cookies.set(VIEW, "liste")
    html = page(client, f"/archiv/Finanzen?sort={sort}")
    assert [row.split(" ")[0] for row in list_rows(html)] == expected


def test_column_headers_sort_and_toggle(client, archive):
    client.cookies.set(VIEW, "liste")
    _hidden, listing = panel(page(client, "/archiv/Finanzen"), "liste")
    assert 'href="/archiv/Finanzen?sort=title"' in listing and 'href="/archiv/Finanzen?sort=date_asc"' in listing
    _hidden, listing = panel(page(client, "/archiv/Finanzen?sort=title"), "liste")
    assert re.search(r'<th scope="col" class="col-name" aria-sort="ascending">\s*<a href="/archiv/Finanzen\?sort=title_desc"', listing)
    assert 'sort-icon is-asc' in listing and 'href="/archiv/Finanzen?sort=date_desc"' in listing
    _hidden, listing = panel(page(client, "/archiv/Finanzen?sort=title_desc"), "liste")
    assert 'aria-sort="descending"' in listing and 'href="/archiv/Finanzen?sort=title"' in listing
    # The tile view offers the same orders in its menu.
    _hidden, tiles = panel(page(client, "/archiv/Finanzen?sort=title_desc"), "kacheln")
    assert '<option value="title_desc" selected>Titel Z–A</option>' in tiles


def test_pages_in_reverse_title_order(client, svc, samples, monkeypatch):  # noqa: F811
    monkeypatch.setattr(routes_ui, "ARCHIVE_PAGE_SIZE", 2)
    for n, name in enumerate(("Anton", "Berta", "Cäsar", "Dora", "Emil"), start=1):
        add_archive_pdf(svc, samples, f"Briefe/2025-01-0{n} {name}.pdf")
    (svc.layout.archive / "Briefe" / "Unterordner").mkdir()
    svc.index.add_folder_with_parents("Briefe/Unterordner")
    client.cookies.set(VIEW, "liste")
    titles = []
    for number in (1, 2, 3):
        html = page(client, f"/archiv/Briefe?sort=title_desc&seite={number}")
        rows = [row.split(" ")[0] for row in list_rows(html)]
        if number == 1:
            assert rows[0] == "Unterordner"
            rows = rows[1:]
        else:
            assert "Unterordner" not in rows  # folders only on the first page
        titles += rows
        assert ("rel=\"next\"" in html) == (number < 3)
    assert titles == ["Emil", "Dora", "Cäsar", "Berta", "Anton"]
    tiles = panel(page(client, "/archiv/Briefe?sort=title"), "kacheln")[1]
    assert tiles.index("Anton") < tiles.index("Berta") and "Cäsar" not in tiles


def test_empty_folder_in_the_list(client, archive):
    client.cookies.set(VIEW, "liste")
    html = page(client, "/archiv/Finanzen/Leer")
    assert "list-table" not in panel(html, "liste")[1] and html.count("Dieser Ordner ist leer") == 1


def test_phones_and_scripts():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    phone = re.search(r"@media \(max-width: 760px\) \{\n  /\* Phones: name and date only\. \*/(.*?)\n\}", css, re.S)
    assert phone and ".list-table .col-sender, .list-table .col-size { display: none; }" in phone.group(1)
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'const VIEW_COOKIE = "ds_ansicht"' in js and "SameSite=Lax" in js and "data-view-panel" in js
    assert "initViewToggle();" in js and "initRowLinks();" in js
    assert views.archive_view("LISTE") == "liste" and views.archive_view("quatsch") == "kacheln" and views.archive_view(None) == "kacheln"


def test_folder_rows_helpers():
    rows = [{"name": "b", "newest": date(2024, 1, 1)}, {"name": "A", "newest": None}, {"name": "c", "newest": date(2025, 1, 1)}]
    assert [r["name"] for r in views.sort_folder_rows(rows, "title")] == ["A", "b", "c"]
    assert [r["name"] for r in views.sort_folder_rows(rows, "date_desc")] == ["c", "b", "A"]
    assert [r["name"] for r in views.sort_folder_rows(rows, "date_asc")] == ["b", "c", "A"]
    sorting = views.list_sorting("date_asc")
    assert sorting["date"] == {"next": "date_desc", "state": "ascending"} and sorting["name"]["state"] is None


def test_list_view_with_the_real_service_container(services, samples, monkeypatch):
    from test_ui import OcrStub

    monkeypatch.setattr(routes_ui, "_ocr_info", lambda settings: OcrStub(languages=[], missing_languages=[]))
    samples.text_pdf(services.layout.archive / "Finanzen/Bank/2025-01-31 Kontoauszug Januar.pdf", ["Sparkasse"])
    samples.text_pdf(services.layout.archive / "Finanzen/2025-05-02 Brief.pdf", ["Brief"])
    services.reconciler.scan(full=True)
    with TestClient(build_app(services)) as test_client:
        response = test_client.get("/archiv/Finanzen?ansicht=liste", follow_redirects=False)
        assert response.status_code == 303
        html = page(test_client, response.headers["location"])
        assert panel(html, "liste")[0] is False
        rows = list_rows(html)
        assert rows[0] == "Bank 31.01.2025 1 Dokument" and rows[1].startswith("Brief 02.05.2025")
