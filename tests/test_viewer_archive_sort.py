"""Default order of the archive: by name, numbers by value.

Folders are often numbered ("01 Finanzen", "02 Wohnung") to set their order;
the archive therefore starts sorted by name in both views. Another order chosen
by the user is remembered in a cookie, like the view.
"""

from __future__ import annotations

import re

import pytest
from starlette.testclient import TestClient

from archive_support import add_archive_pdf
from docushelf.web import routes_ui, views
from test_ui import build_app, get_page
from test_ui import svc  # noqa: F401 - the same service fixture


@pytest.fixture
def client(svc):  # noqa: F811
    with TestClient(build_app(svc), base_url="http://testserver") as test_client:
        yield test_client


@pytest.fixture
def numbered(svc, samples):  # noqa: F811
    for rel in (
        "Ablage/10 Steuer/2025-01-01 A.pdf",
        "Ablage/2 Auto/2024-01-01 B.pdf",
        "Ablage/01 Haus/2023-01-01 C.pdf",
        "Ablage/Ärzte/2025-06-01 D.pdf",
        "Ablage/2025-03-01 Rechnung 10.pdf",
        "Ablage/2025-01-01 Rechnung 2.pdf",
        "Ablage/2025-02-01 rechnung 1.pdf",
    ):
        add_archive_pdf(svc, samples, rel)


def tiles(html: str) -> tuple[list[str], list[str]]:
    panel = re.search(r'data-view-panel="kacheln"[^>]*>(.*?)data-view-panel="liste"', html, re.S).group(1)
    folders = re.findall(r'<span class="folder-card-name">([^<]*)</span>', panel)
    docs = re.findall(r'<td class="doc-title">\s*<a [^>]*>.*?<span>([^<]*)</span>', panel, re.S)
    return folders, docs


def list_names(html: str) -> list[str]:
    panel = html.split('data-view-panel="liste"', 1)[1]
    return re.findall(r'<span class="list-label">([^<]*)</span>', panel)


def test_natural_key_orders_numbers_by_value_and_ignores_case_and_accents():
    names = ["10 Steuer", "2 Auto", "01 Haus", "Ärzte", "apfel", "Zebra", "1 Bank", "02 Bad"]
    assert sorted(names, key=views.natural_key) == [
        "01 Haus", "1 Bank", "02 Bad", "2 Auto", "10 Steuer", "apfel", "Ärzte", "Zebra",
    ]
    assert sorted(["Rechnung 10", "Rechnung 9", "rechnung 1"], key=views.natural_key) == ["rechnung 1", "Rechnung 9", "Rechnung 10"]
    paths = ["10 B", "2 A/10 X", "2 A", "2 A/9 Y"]
    assert sorted(paths, key=views.natural_path_key) == ["2 A", "2 A/9 Y", "2 A/10 X", "10 B"]


def test_default_is_by_name_in_tiles_and_list(client, numbered):
    html = get_page(client, "/archiv/Ablage")
    folders, docs = tiles(html)
    assert folders == ["01 Haus", "2 Auto", "10 Steuer", "Ärzte"]
    assert docs == ["rechnung 1", "Rechnung 2", "Rechnung 10"]
    assert list_names(html) == folders + docs
    assert '<option value="title" selected>' in html
    listing = html.split('data-view-panel="liste"', 1)[1]
    assert re.search(r'class="col-name" aria-sort="ascending">\s*<a href="/archiv/Ablage\?sort=title_desc"', listing)
    # Without a choice in the URL nothing is remembered.
    assert views.SORT_COOKIE not in client.cookies


def test_sidebar_tree_in_natural_order(client, numbered):
    html = get_page(client, "/archiv")
    tree = html.split('class="archive-tree"', 1)[1].split("</aside>", 1)[0]
    names = re.findall(r'<span class="tree-name">([^<]*)</span>', tree)
    assert names[names.index("Ablage") + 1 :] == ["01 Haus", "2 Auto", "10 Steuer", "Ärzte"]


def test_reverse_name_order(client, numbered):
    folders, docs = tiles(get_page(client, "/archiv/Ablage?sort=title_desc"))
    assert folders == ["Ärzte", "10 Steuer", "2 Auto", "01 Haus"]
    assert docs == ["Rechnung 10", "Rechnung 2", "rechnung 1"]


def test_other_orders_still_work(client, numbered):
    _folders, docs = tiles(get_page(client, "/archiv/Ablage?sort=date_desc"))
    assert docs == ["Rechnung 10", "rechnung 1", "Rechnung 2"]
    names = list_names(get_page(client, "/archiv/Ablage?sort=date_asc"))
    assert names[:4] == ["01 Haus", "2 Auto", "10 Steuer", "Ärzte"]  # by their newest document
    assert names[4:] == ["Rechnung 2", "rechnung 1", "Rechnung 10"]


def test_the_choice_is_remembered(client, numbered):
    response = client.get("/archiv/Ablage?sort=date_desc")
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{views.SORT_COOKIE}=date_desc;") and "HttpOnly" in cookie
    assert "Path=/" in cookie and "Max-Age=31536000" in cookie and "SameSite=lax" in cookie
    # Every folder now opens in that order, until another one is chosen.
    html = get_page(client, "/archiv/Ablage")
    assert '<option value="date_desc" selected>' in html
    assert tiles(html)[1] == ["Rechnung 10", "rechnung 1", "Rechnung 2"]
    assert "set-cookie" not in client.get("/archiv/Ablage?sort=date_desc").headers  # unchanged: not sent again
    assert "set-cookie" not in client.get("/archiv/Ablage?sort=quatsch").headers  # unknown: ignored
    assert '<option value="date_desc" selected>' in get_page(client, "/archiv/Ablage?sort=quatsch")
    client.get("/archiv/Ablage?sort=title")
    assert tiles(get_page(client, "/archiv/Ablage"))[1] == ["rechnung 1", "Rechnung 2", "Rechnung 10"]
    # The view toggle keeps a non-default order in its links for clients without cookies.
    html = get_page(client, "/archiv/Ablage?sort=date_asc")
    assert 'href="/archiv/Ablage?ansicht=liste&amp;sort=date_asc"' in html


def test_secure_cookie_behind_https(svc, numbered):  # noqa: F811
    svc.settings = svc.settings.with_overrides(base_url="https://docs.example.org")
    with TestClient(build_app(svc), base_url="http://testserver") as test_client:
        response = test_client.get("/archiv?sort=date_asc")
    assert "Secure" in response.headers["set-cookie"]


def test_name_order_pages(client, svc, samples, monkeypatch):  # noqa: F811
    monkeypatch.setattr(routes_ui, "ARCHIVE_PAGE_SIZE", 2)
    for n in (1, 2, 3, 10, 11):
        add_archive_pdf(svc, samples, f"Briefe/2025-01-01 Brief {n}.pdf")
    seen = []
    for number in (1, 2, 3):
        html = get_page(client, f"/archiv/Briefe?seite={number}")
        seen += tiles(html)[1]
        assert ('rel="next"' in html) == (number < 3)
    assert seen == ["Brief 1", "Brief 2", "Brief 3", "Brief 10", "Brief 11"]
    seen = []
    for number in (1, 2, 3):
        seen += tiles(get_page(client, f"/archiv/Briefe?sort=title_desc&seite={number}"))[1]
    assert seen == ["Brief 11", "Brief 10", "Brief 3", "Brief 2", "Brief 1"]
