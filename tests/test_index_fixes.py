"""Regression tests for the index: connections of finished threads, numbers and
compounds in the search, and display spellings of words from the page text."""

from __future__ import annotations

import itertools
import os
import random
import sqlite3
import threading
import unicodedata
from datetime import date
from types import SimpleNamespace

import pytest

from docushelf.index import search
from docushelf.index.db import Index
from docushelf.index.search import Searcher
from docushelf.models import DocumentRecord
from docushelf.util.names import parse_filename
from docushelf.util.textnorm import joined, joined_fragments, joined_with_map, normalize, spellings

_counter = itertools.count()


@pytest.fixture
def index(base_services):
    return base_services.index


@pytest.fixture
def searcher(base_services):
    searcher = Searcher(base_services)
    searcher.vocabulary_max_staleness = 0.0
    return searcher


def add(index, path: str, pages: list[str] | str, *, title: str | None = None, sender: str | None = None) -> DocumentRecord:
    pages = [pages] if isinstance(pages, str) else list(pages)
    folder, _, filename = path.rpartition("/")
    parsed = parse_filename(filename)
    n = next(_counter)
    record = DocumentRecord(
        id=f"00000000-0000-4000-9000-{n:012d}",
        path=path,
        folder=folder,
        filename=filename,
        ext=parsed.ext,
        title=title or parsed.title,
        doc_date=parsed.doc_date or date(2025, 1, 1),
        sender=sender,
        tags=[],
        origin_hash=None,
        content_hash=f"hash-{n}",
        xmp_id=None,
        has_xmp=False,
        size=1,
        mtime_ns=1,
        inode=n,
        page_count=len(pages),
        text_status="ok",
        indexed_at="2026-01-01T00:00:00+01:00",
    )
    index.upsert(record, pages)
    if folder:
        index.add_folder_with_parents(folder)
    return record


def ids(result) -> list[str]:
    return [hit.doc.id for hit in result.hits]


# -- connections of finished threads --------------------------------------------------------------


def _is_closed(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


def _open_fds() -> int | None:
    for directory in ("/proc/self/fd", "/dev/fd"):
        if os.path.isdir(directory):
            return len(os.listdir(directory))
    return None


def test_connections_of_finished_threads_are_closed(tmp_path):
    """Web servers run requests in worker threads that come and go; their connections must not pile up."""
    index = Index(tmp_path / "i.sqlite3")
    index.init_schema()
    opened: list[sqlite3.Connection] = []

    def request() -> None:
        opened.append(index.conn())
        index.count()

    def burst() -> None:
        thread = threading.Thread(target=request)
        thread.start()
        thread.join()

    burst()
    fds_before = _open_fds()
    for _ in range(60):
        burst()
    # Each new thread closed the connections of the threads that had ended before it.
    assert all(_is_closed(connection) for connection in opened[:-1])
    assert not _is_closed(opened[-1])  # its thread has ended too, but no thread has connected since
    assert len(index._connections) <= 2  # this thread and the last request thread
    if fds_before is not None:
        assert _open_fds() - fds_before <= 6
    index.close()
    assert _is_closed(opened[-1]) and index._connections == {}


def test_live_threads_keep_their_connections_while_others_come_and_go(tmp_path):
    index = Index(tmp_path / "i.sqlite3")
    index.init_schema()
    ready = threading.Barrier(5)
    release = threading.Event()
    errors: list[BaseException] = []

    def long_running() -> None:
        try:
            first = index.conn()
            first.execute("SELECT 1")
            ready.wait(10)
            assert release.wait(10)
            assert index.conn() is first
            assert index.count() == 0  # the connection still works
        except BaseException as exc:  # noqa: BLE001 - reported to the test thread
            errors.append(exc)

    workers = [threading.Thread(target=long_running) for _ in range(4)]
    for worker in workers:
        worker.start()
    ready.wait(10)
    for _ in range(20):
        short = threading.Thread(target=index.count)
        short.start()
        short.join()
    release.set()
    for worker in workers:
        worker.join(10)
    assert not errors
    assert index.count() == 0
    index.close()


def test_index_is_usable_after_close(tmp_path):
    index = Index(tmp_path / "i.sqlite3")
    index.init_schema()
    first = index.conn()
    index.close()
    assert _is_closed(first)
    assert index.count() == 0 and index.conn() is not first
    index.close()


# -- joined text -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Kundennummer 123 456 7890", "kundennummer 1234567890"),
        ("IBAN DE89 3704 0044 0532 0130 00", "iban de89370400440532013000"),
        ("Versicherungsnummer 123/456789-X", "versicherungsnummer 123456789x"),
        ("Tel. 0351 / 123 45 67", "tel 03511234567"),
        ("Kfz-Versicherung", "kfzversicherung"),
        ("Kfz-\nVersicherung", "kfzversicherung"),
        ("E-Mail-Adresse", "emailadresse"),
        # Kept apart: decimal commas, times, words, suspended hyphens.
        ("Betrag 20,24 EUR um 12:30", "betrag 20 24 eur um 12 30"),
        ("1.234,56 €", "1234 56"),
        ("Kranken- und Pflegeversicherung", "kranken und pflegeversicherung"),
        ("ab Rechnungsdatum", "ab rechnungsdatum"),
        ("Seite 1 von 2", "seite 1 von 2"),
    ],
)
def test_joined_form(text, expected):
    assert joined(text) == expected


def test_joined_form_and_its_map_agree():
    """Indexing uses joined(), highlighting the map; both must see the same text."""
    rng = random.Random(11)
    pieces = ["Müller", "Straße", "Neben-\nkosten", "Kfz-\nVersicherung", "12,50 €", "1.234,56", "Überweisung", "(Kunde)",
              "Nr.:", "\n", "  ", "-", " - ", "/", " / ", "Kfz-Versicherung", "123 456 7890", "DE89 3704 0044",
              "31.12.2025", "0351 / 12 34", "456789-X", "Ä-Ö-Ü", "x\u00adY", "1\u00a0234", "e.V.", "a-1-b", "1- 2", "½", "ﬁ"]
    for _ in range(1500):
        text = "".join(rng.choice(pieces) + rng.choice(["", " ", "-", ".", "\n", "/", ", "]) for _ in range(rng.randint(1, 25)))
        value, offsets = joined_with_map(text)
        source = unicodedata.normalize("NFC", text)
        assert value == joined(text), text
        assert len(offsets) == len(value) and all(0 <= i < len(source) for i in offsets)
        # Every word of the joined text is a normalized word or one of the indexed fragments.
        words = set(normalize(text).split()) | set(joined_fragments(text).split())
        assert all(word in words for word in value.split()), text


def test_page_prefix_snippets_equal_whole_page_snippets_for_numbers_and_compounds():
    from docushelf.util.textnorm import normalize as norm

    rng = random.Random(5)
    words = ["Kundennummer", "123 456 7890", "DE89 3704 0044 0532 0130 00", "Kfz-Versicherung", "Kfz Versicherung",
             "Kfzversicherung", "Lohnsteuer-\nBescheinigung", "31.12.2025", "20,24 €", "Rechnung", "\n", "x" * 25,
             "Überweisung", "Neben-\nkosten", "1234567890", "-", "/"]
    queries = ["1234567890", "DE89370400440532013000", "Kfzversicherung", "Lohnsteuer-Bescheinigung", "versicherung 31122025",
               '"kfz versicherung"', "2024 rechnung"]
    for _ in range(80):
        source = " ".join(rng.choice(words) for _ in range(rng.randint(150, 600)))
        terms = search._terms(rng.choice(queries))
        for wanted in (1, 2, 3):
            fast = search._page_windows(source, norm(source), terms, wanted=wanted)
            full = search._windows(source, search._match_spans(source, terms))[:wanted]
            assert [search._render(source, w) for w in fast] == [search._render(source, w) for w in full]


# -- numbers and compounds in the search ---------------------------------------------------------------


@pytest.mark.parametrize(
    "query,text,marked",
    [
        ("1234567890", "Ihre Kundennummer 123 456 7890 bitte angeben", "123 456 7890"),
        ("DE89370400440532013000", "IBAN DE89 3704 0044 0532 0130 00 BIC COBADEFFXXX", "DE89 3704 0044 0532 0130 00"),
        ("123456789X", "Versicherungsnummer 123/456789-X", "123/456789-X"),
        ("123/456789-X", "Versicherungsnummer 123456789X", "123456789X"),
        ("05320130", "IBAN DE89 3704 0044 0532 0130 00", "0532 0130"),
        ("03511234567", "Telefon 0351 / 123 45 67", "0351 / 123 45 67"),
    ],
)
def test_numbers_are_found_however_they_are_grouped(searcher, index, query, text, marked):
    doc = add(index, "Bank/2025-01-01 Brief.pdf", text)
    add(index, "Bank/2025-01-02 Anderes.pdf", "Kundennummer 987 654 3210, IBAN DE02 1203 0000 0000 2020 51")
    result = searcher.search(query)
    assert ids(result) == [doc.id]
    assert f"<mark>{marked}</mark>" in result.hits[0].snippets[0].html
    assert result.hits[0].matched_in == ["text"] and result.corrections == {}


def test_short_numbers_do_not_bridge_separators(searcher, index):
    add(index, "A/2025-01-01 Beleg.pdf", "Betrag 20,24 EUR, Kassennummer 47 11")
    year = add(index, "A/2025-01-02 Bescheid.pdf", "Steuerbescheid 2024")
    assert ids(searcher.search("2024")) == [year.id]
    assert searcher.search("4711").hits == []


def test_numbers_in_titles_and_file_names(searcher, index):
    doc = add(index, "Vertraege/2025-01-01 Vertrag 123 456 7890.pdf", "Vertragsbedingungen")
    result = searcher.search("1234567890")
    assert ids(result) == [doc.id]
    assert result.hits[0].matched_in == ["title", "filename"]


def test_closed_compound_finds_hyphenated_spelling(searcher, index):
    hyphen = add(index, "A/2025-01-01 Brief.pdf", "Ihre Kfz-Versicherung für 2025")
    closed = add(index, "A/2025-01-02 Brief.pdf", "Ihre Kfzversicherung für 2026")
    folder = add(index, "Versicherungen/Kfz-Versicherung/2025-01-03 Beitrag.pdf", "Beitragsrechnung")
    for query in ("Kfzversicherung", "kfzversicherung", "Kfz-Versicherung"):
        result = searcher.search(query)
        assert set(ids(result)) == {hyphen.id, closed.id, folder.id}, query
        assert result.corrections == {}
    hit = next(h for h in searcher.search("Kfzversicherung").hits if h.doc.id == hyphen.id)
    assert "<mark>Kfz-Versicherung</mark>" in hit.snippets[0].html
    assert next(h for h in searcher.search("Kfzversicherung").hits if h.doc.id == folder.id).matched_in == ["folder"]


def test_hyphenated_input_finds_closed_and_spaced_spellings(searcher, index):
    closed = add(index, "A/2025-01-01 Lohn.pdf", "Lohnsteuerbescheinigung für 2024")
    hyphen = add(index, "A/2025-01-02 Lohn.pdf", "Lohnsteuer-Bescheinigung für 2023")
    spaced = add(index, "A/2025-01-03 Lohn.pdf", "Lohnsteuer Bescheinigung für 2022")
    add(index, "A/2025-01-04 Lohn.pdf", "Lohnsteuer und eine Bescheinigung")
    result = searcher.search("Lohnsteuer-Bescheinigung")
    assert set(ids(result)) == {closed.id, hyphen.id, spaced.id}
    snippets = {hit.doc.id: hit.snippets[0].html for hit in result.hits}
    assert snippets[closed.id] == "<mark>Lohnsteuerbescheinigung</mark> für 2024"
    assert snippets[hyphen.id] == "<mark>Lohnsteuer-Bescheinigung</mark> für 2023"


def test_closed_compound_without_hits_tries_the_spaced_spelling(searcher, index):
    doc = add(index, "A/2025-01-01 Brief.pdf", "Die Haftpflicht Versicherung zahlt den Schaden")
    result = searcher.search("Haftpflichtversicherung")
    assert ids(result) == [doc.id]
    assert result.corrections == {"Haftpflichtversicherung": ["Haftpflicht Versicherung"]}
    assert "<mark>Haftpflicht Versicherung</mark>" in result.hits[0].snippets[0].html


def test_words_are_not_joined_across_plain_spaces(searcher, index):
    add(index, "A/2025-01-01 Brief.pdf", "Zahlbar innerhalb von 14 Tagen ab Rechnungsdatum")
    result = searcher.search("Abrechnung")
    assert result.hits == [] and result.corrections == {}
    # Quoted phrases stay exact.
    add(index, "A/2025-01-02 Brief.pdf", "Lohnsteuerbescheinigung")
    assert searcher.search('"Lohnsteuer Bescheinigung"').hits == []


def test_short_hyphenated_input_stays_a_phrase(searcher, index):
    club = add(index, "A/2025-01-01 Verein.pdf", "Sportverein Musterstadt e.V.")
    add(index, "A/2025-01-02 Brief.pdf", "Bitte dringend zurückrufen, ev. morgen")
    assert ids(searcher.search("e.V.")) == [club.id]
    assert searcher.search("Dr.-Ing.").hits == []


# -- index tables ---------------------------------------------------------------------------------------


def test_joined_tables_follow_upserts_and_deletes(index):
    record = add(index, "A/2025-01-01 Brief.pdf", ["Kundennummer 123 456 7890", "ohne Nummern"], title="Kfz-Versicherung")
    conn = index.conn()
    hit = lambda table, q: conn.execute(f"SELECT rowid FROM {table} WHERE {table} MATCH ?", (q,)).fetchall()  # noqa: E731
    assert hit("fts_joined", '"1234567890"') and hit("fts_docs_joined", '"kfzversicherung"')
    index.upsert(record, ["anderer Text 98 76"])
    assert not hit("fts_joined", '"1234567890"') and hit("fts_joined", '"9876"')
    index.delete(record.id)
    assert not hit("fts_joined", '"9876"') and not hit("fts_docs_joined", '"kfzversicherung"')
    for table in ("fts_pages", "fts_joined", "fts_docs", "fts_docs_joined", "fts_words"):
        conn.execute(f"INSERT INTO {table}({table}) VALUES('integrity-check')")


def test_schema_upgrade_rebuilds_the_archive_tables_and_keeps_jobs_and_memories(tmp_path):
    path = tmp_path / "i.sqlite3"
    index = Index(path)
    index.init_schema()
    add(index, "A/2025-01-01 Alt.pdf", "alter Text")
    index.enqueue("process", "item-1")
    index.mark_mail_seen("mail-key")
    index.mark_hash_seen("watch-nas", "abc")
    index.set_meta("schema_version", "1")  # as written by the previous layout
    index.set_meta("last_full_scan", "2026-01-01T00:00:00+01:00")
    index.close()

    index = Index(path)
    assert index.init_schema() is True  # a full scan has to refill the index
    assert index.count() == 0 and index.list_folders() == []
    assert index.get_meta("schema_version") == "2" and index.get_meta("last_full_scan") is None
    assert [job.key for job in index.pending_jobs()] == ["item-1"]
    assert index.mail_seen("mail-key") and index.hash_seen("watch-nas", "abc")
    doc = add(index, "A/2025-01-02 Neu.pdf", "Kundennummer 123 456 7890")
    assert ids(Searcher(SimpleNamespace(index=index)).search("1234567890")) == [doc.id]
    assert index.init_schema() is False
    index.close()


# -- display spellings of words from the page text -------------------------------------------------------


def test_spellings_in_text():
    text = "ÜBERWEISUNG\nIhre Überweisung vom 1.2. und die Über-\nweisung danach; Hauptstraße 5"
    assert spellings("ueberweisung", text) == ["ÜBERWEISUNG", "Überweisung", "Überweisung"]
    assert spellings("strasse", text) == []  # whole words only
    assert spellings("hauptstrasse", text) == ["Hauptstraße"]


def test_autocomplete_shows_words_from_the_page_text_as_written(searcher, index):
    add(index, "A/2025-01-01 Brief.pdf", "ÜBERWEISUNG\nIhre Überweisung für das Grundstück, Gebühren fällig")
    add(index, "A/2025-01-02 Brief.pdf", "Die Überweisung ist eingegangen. Gebühren")
    assert searcher.suggest_terms("Überw") == ["Überweisung"]
    assert searcher.suggest_terms("ueberw") == ["Überweisung"]
    assert searcher.suggest_terms("Grundst") == ["Grundstück"]
    assert searcher.suggest_terms("Gebü") == ["Gebühren"]
    assert searcher.suggest_terms("fae") == ["fällig"]


def test_corrections_show_words_from_the_page_text_as_written(searcher, index):
    doc = add(index, "A/2025-01-01 Brief.pdf", "Ihre Überweisung ist eingegangen")
    result = searcher.search("Überweisnug")
    assert ids(result) == [doc.id]
    assert result.corrections == {"Überweisnug": ["Überweisung"]}


def test_spelling_from_titles_comes_first(searcher, index):
    add(index, "A/2025-01-01 Brief.pdf", "HAUSVERWALTUNG MÜLLER, hausverwaltung", sender="Hausverwaltung Müller")
    assert searcher.suggest_terms("hausverw") == ["Hausverwaltung"]
    add(index, "A/2025-01-02 Brief.pdf", "Ihre Überweisung, ÜBERWEISUNGSTRÄGER")
    assert searcher.suggest_terms("überweisungst") == ["ÜBERWEISUNGSTRÄGER"]  # all a page offers
