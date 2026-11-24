"""Search: substring and umlaut tolerance, typo correction, filters, snippets,
ranking, the query language and term suggestions."""

from __future__ import annotations

import itertools
import re
from datetime import date

import pytest

from docushelf.index.reconcile import Reconciler
from docushelf.index.search import SearchQuery, Searcher, parse_query
from docushelf.models import DocMeta, DocumentRecord
from docushelf.pdf.xmp import write_meta
from docushelf.util.names import parse_filename

_counter = itertools.count()


@pytest.fixture
def searcher(base_services):
    searcher = Searcher(base_services)
    searcher.vocabulary_max_staleness = 0.0
    base_services.searcher = searcher
    return searcher


@pytest.fixture
def index(base_services):
    return base_services.index


def add(
    index,
    path: str,
    pages: list[str] | str = (),
    *,
    title: str | None = None,
    doc_date: date | None = None,
    sender: str | None = None,
    tags: list[str] | tuple[str, ...] = (),
) -> DocumentRecord:
    pages = [pages] if isinstance(pages, str) else list(pages)
    folder, _, filename = path.rpartition("/")
    parsed = parse_filename(filename)
    n = next(_counter)
    record = DocumentRecord(
        id=f"00000000-0000-4000-8000-{n:012d}",
        path=path,
        folder=folder,
        filename=filename,
        ext=parsed.ext,
        title=title or parsed.title,
        doc_date=doc_date if doc_date is not None else parsed.doc_date,
        sender=sender,
        tags=list(tags),
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
    for i in range(1, folder.count("/") + 2 if folder else 0):
        index.upsert_folder("/".join(folder.split("/")[:i]))
    return record


def ids(result) -> list[str]:
    return [hit.doc.id for hit in result.hits]


# -- tolerance -----------------------------------------------------------------------------------


def test_partial_words_find_compound_words(searcher, index):
    doc = add(index, "Wohnung/2025-03-01 Abrechnung.pdf", "Ihre Nebenkostenabrechnung für 2024")
    add(index, "Wohnung/2025-03-02 Sonstiges.pdf", "Mietvertrag")
    for query in ("Neben", "nebenkosten", "kostenab", "NEBENKOSTENABRECHNUNG"):
        assert ids(searcher.search(query)) == [doc.id], query


@pytest.mark.parametrize("query", ["mueller", "müller", "Müller", "MÜLLER", "Mueller"])
def test_umlaut_spellings_find_each_other(searcher, index, query):
    umlaut = add(index, "A/2025-01-01 Brief.pdf", "Sehr geehrter Herr Müller")
    transliterated = add(index, "A/2025-01-02 Brief.pdf", "Hallo Frau Mueller")
    assert set(ids(searcher.search(query))) == {umlaut.id, transliterated.id}


@pytest.mark.parametrize("query", ["strasse", "Straße", "STRASSE", "hauptstr"])
def test_sharp_s_and_ss(searcher, index, query):
    sharp = add(index, "A/2025-01-01 Brief.pdf", "Hauptstraße 5, 01067 Dresden")
    double = add(index, "A/2025-01-02 Brief.pdf", "Hauptstrasse 7")
    assert set(ids(searcher.search(query))) == {sharp.id, double.id}


def test_accents_and_case_are_ignored(searcher, index):
    doc = add(index, "A/2025-01-01 Rechnung.pdf", "Café Crème GmbH")
    assert ids(searcher.search("cafe creme")) == [doc.id]


# -- typo tolerance ---------------------------------------------------------------------------------


def test_misspelled_term_is_corrected(searcher, index):
    doc = add(index, "Wohnung/2025-03-01 Abrechnung.pdf", "Nebenkostenabrechnung 2024 für die Wohnung")
    result = searcher.search("Nebenkostenabrechnug")
    assert ids(result) == [doc.id]
    assert {k: [w.casefold() for w in v] for k, v in result.corrections.items()} == {
        "Nebenkostenabrechnug": ["nebenkostenabrechnung"]
    }


def test_correction_uses_spelling_from_titles(searcher, index):
    doc = add(index, "Versicherungen/2025-01-01 Haftpflichtversicherung.pdf", "Beitrag 2025")
    result = searcher.search("Haftplichtversicherung")
    assert ids(result) == [doc.id]
    assert result.corrections == {"Haftplichtversicherung": ["Haftpflichtversicherung"]}


def test_two_typos_in_a_long_word(searcher, index):
    doc = add(index, "A/2025-01-01 Brief.pdf", "Versicherungsschein Nummer 4711")
    result = searcher.search("Versicherunsgschein")  # transposition = 2 edits, 19 characters
    assert ids(result) == [doc.id]


def test_misspelled_beginning_of_a_long_word(searcher, index):
    doc = add(index, "Wohnung/2025-03-01 Abrechnung.pdf", "Nebenkostenabrechnung 2024")
    result = searcher.search("Nebenkostn")
    assert ids(result) == [doc.id]
    assert "Nebenkostn" in result.corrections


def test_short_terms_and_phrases_are_not_corrected(searcher, index):
    add(index, "A/2025-01-01 Brief.pdf", "Rechnung Strom")
    result = searcher.search("Rehc")
    assert result.hits == [] and result.corrections == {}
    result = searcher.search('"Rechnug Strom"')
    assert result.hits == [] and result.corrections == {}


def test_existing_term_is_not_corrected_even_if_filtered_out(searcher, index):
    add(index, "Finanzen/2025-01-01 Kontoauszug.pdf", "Sparkasse")
    add(index, "Wohnung/2025-01-01 Miete.pdf", "Miete")
    result = searcher.search("Kontoauszug ordner:Wohnung")
    assert result.hits == [] and result.corrections == {}


def test_vocabulary_is_reloaded_when_the_index_changes(base_services, index):
    searcher = Searcher(base_services)  # default settings
    add(index, "A/2025-01-01 Eins.pdf", "Kaufvertrag")
    vocabulary = searcher._vocabulary()
    assert searcher._vocabulary() is vocabulary  # cached while the index is unchanged
    assert searcher.search("Darlehnsvertrag").hits == []
    doc = add(index, "A/2025-01-02 Zwei.pdf", "Darlehensvertrag")
    assert searcher._vocabulary() is not vocabulary
    result = searcher.search("Darlehnsvertrag")
    assert ids(result) == [doc.id] and result.corrections
    assert searcher.suggest_terms("darl") == ["Darlehensvertrag"]


def test_expensive_vocabulary_may_lag_behind_briefly(base_services, index):
    searcher = Searcher(base_services)
    searcher.expensive_rebuild_seconds = 0.0  # treat every rebuild as expensive
    searcher.vocabulary_max_staleness = 60.0
    add(index, "A/2025-01-01 Eins.pdf", "Kaufvertrag")
    vocabulary = searcher._vocabulary()
    add(index, "A/2025-01-02 Zwei.pdf", "Darlehensvertrag")
    assert searcher._vocabulary() is vocabulary
    searcher.vocabulary_max_staleness = 0.0
    assert searcher._vocabulary() is not vocabulary


# -- semantics --------------------------------------------------------------------------------------------


def test_all_terms_must_match(searcher, index):
    both = add(index, "A/2025-01-01 Eins.pdf", "Stadtwerke Dresden Strom Abschlag")
    add(index, "A/2025-01-02 Zwei.pdf", "Stadtwerke Dresden Wasser")
    assert ids(searcher.search("Stadtwerke Strom")) == [both.id]


def test_terms_may_match_in_different_fields_and_pages(searcher, index):
    doc = add(
        index,
        "Finanzen/2025-01-31 Kontoauszug Januar.pdf",
        ["Seite eins", "Buchung an Stadtwerke"],
        sender="Sparkasse Dresden",
    )
    result = searcher.search("kontoauszug sparkasse stadtwerke")
    assert ids(result) == [doc.id]
    assert result.hits[0].matched_in == ["title", "sender", "filename", "text"]
    assert result.hits[0].pages == [2]


def test_phrases_match_as_substrings_of_the_normalized_text(searcher, index):
    spaced = add(index, "A/2025-01-01 Eins.pdf", "Neben kosten fallen an")
    joined = add(index, "A/2025-01-02 Zwei.pdf", "Nebenkosten fallen an")
    assert ids(searcher.search('"neben kosten"')) == [spaced.id]
    assert set(ids(searcher.search("neben kosten"))) == {spaced.id, joined.id}
    # Phrases are substrings too: "kosten fallen" also occurs in "nebenkosten fallen".
    assert set(ids(searcher.search('"Kosten fallen"'))) == {spaced.id, joined.id}
    assert ids(searcher.search('"neben kosten fallen"')) == [spaced.id]


def test_short_terms_use_substring_search(searcher, index):
    ag = add(index, "A/2025-01-01 Eins.pdf", "Muster AG, Berlin")
    add(index, "A/2025-01-02 Zwei.pdf", "Muster GmbH, Berlin")
    assert ids(searcher.search("muster ag")) == [ag.id]
    assert ids(searcher.search("A")) != []


def test_stopwords_are_dropped_when_other_terms_remain(searcher, index):
    doc = add(index, "A/2025-01-01 Eins.pdf", "Telekom Rechnung März")
    assert ids(searcher.search("Rechnung von der Telekom")) == [doc.id]


def test_fts_syntax_in_queries_is_harmless(searcher, index):
    add(index, "A/2025-01-01 Eins.pdf", "Text")
    for query in ['foo" OR "bar', "NEAR(a b)", "*", "a:b", '"', "(", "^x", "x AND", "text -foo", "'; DROP TABLE x"]:
        searcher.search(query)  # must not raise


def test_empty_query_returns_nothing(searcher, index):
    add(index, "A/2025-01-01 Eins.pdf", "Text")
    result = searcher.search("")
    assert result.hits == [] and result.total == 0
    assert searcher.search("   ").total == 0


# -- filters -------------------------------------------------------------------------------------------------


def test_folder_filter_covers_the_subtree(searcher, index):
    top = add(index, "Finanzen/2025-01-01 Rechnung.pdf", "Rechnung")
    sub = add(index, "Finanzen/Bank/2025-01-02 Rechnung.pdf", "Rechnung")
    add(index, "Finanzen2/2025-01-03 Rechnung.pdf", "Rechnung")
    add(index, "Wohnung/2025-01-04 Rechnung.pdf", "Rechnung")
    assert set(ids(searcher.search(SearchQuery(text="Rechnung", folder="Finanzen")))) == {top.id, sub.id}
    assert set(ids(searcher.search("Rechnung ordner:finanzen"))) == {top.id, sub.id}
    assert ids(searcher.search("Rechnung ordner:Bank")) == [sub.id]
    assert ids(searcher.search('Rechnung ordner:"Finanzen/Bank/"')) == [sub.id]
    assert searcher.search("Rechnung ordner:Gibtsnicht").hits == []


def test_date_range_filter(searcher, index):
    add(index, "A/2024-12-31 Alt.pdf", "Beleg")
    first = add(index, "A/2025-01-01 Neu.pdf", "Beleg")
    last = add(index, "A/2025-12-31 Ende.pdf", "Beleg")
    add(index, "A/Ohne Datum.pdf", "Beleg")
    expected = {first.id, last.id}
    assert set(ids(searcher.search(SearchQuery(text="Beleg", date_from=date(2025, 1, 1), date_to=date(2025, 12, 31))))) == expected
    assert set(ids(searcher.search("Beleg jahr:2025"))) == expected
    assert set(ids(searcher.search("Beleg ab:2025-01-01 bis:31.12.2025"))) == expected
    assert ids(searcher.search("Beleg ab:2025-06")) == [last.id]


def test_tag_filter_requires_all_tags(searcher, index):
    both = add(index, "A/2025-01-01 Fernseher.pdf", "Kaufbeleg", tags=["Garantie", "Elektronik"])
    add(index, "A/2025-01-02 Toaster.pdf", "Kaufbeleg", tags=["Garantie"])
    add(index, "A/2025-01-03 Radio.pdf", "Kaufbeleg", tags=["Elektronik"])
    query = SearchQuery(text="Kaufbeleg", tags=["garantie", "ELEKTRONIK"])
    assert ids(searcher.search(query)) == [both.id]
    assert ids(searcher.search("tag:Garantie tag:Elektronik")) == [both.id]
    assert searcher.search("Kaufbeleg tag:Unbekannt").hits == []


def test_sender_filter_is_a_substring_match(searcher, index):
    dresden = add(index, "A/2025-01-01 Eins.pdf", "Auszug", sender="Sparkasse Dresden")
    saxony = add(index, "A/2025-01-02 Zwei.pdf", "Auszug", sender="Ostsächsische Sparkasse")
    add(index, "A/2025-01-03 Drei.pdf", "Auszug", sender="Volksbank")
    add(index, "A/2025-01-04 Vier.pdf", "Auszug Sparkasse")  # only in the text
    assert set(ids(searcher.search("Auszug von:sparkasse"))) == {dresden.id, saxony.id}
    assert ids(searcher.search(SearchQuery(text="Auszug", sender="saechsische"))) == [saxony.id]
    assert searcher.search("von:Commerzbank").hits == []


def test_filters_without_text_list_documents_by_date(searcher, index):
    old = add(index, "Bank/2023-05-01 Alt.pdf", "a")
    new = add(index, "Bank/2025-05-01 Neu.pdf", "b")
    undated = add(index, "Bank/Ohne Datum.pdf", "c")
    middle = add(index, "Bank/Unter/2024-05-01 Mitte.pdf", "d")
    add(index, "Anderes/2025-06-01 Anderes.pdf", "e")
    result = searcher.search("ordner:Bank")
    assert ids(result) == [new.id, middle.id, old.id, undated.id]
    assert result.total == 4 and all(h.score == 0 and h.snippets == [] for h in result.hits)
    page = searcher.search(SearchQuery(folder="Bank", limit=2, offset=1))
    assert ids(page) == [middle.id, old.id] and page.total == 4


def test_inline_filters_merge_with_explicit_ones(searcher, index):
    doc = add(index, "Elektronik/2025-01-01 Fernseher.pdf", "Kaufbeleg", tags=["Garantie"], sender="Elektro Markt")
    result = searcher.search(SearchQuery(text="tag:Garantie Kaufbeleg", folder="Elektronik", sender="elektro"))
    assert ids(result) == [doc.id]
    assert result.query.tags == ["Garantie"] and result.query.text == "Kaufbeleg"
    assert result.query.folder == "Elektronik" and result.query.sender == "elektro"


# -- snippets ----------------------------------------------------------------------------------------------------


def test_snippets_carry_page_numbers(searcher, index):
    doc = add(index, "A/2025-01-01 Vertrag.pdf", ["Deckblatt", "Kündigung zum 31.12.", "Anlage", "Kündigungsfrist drei Monate"])
    hit = searcher.search("Kündigung").hits[0]
    assert hit.doc.id == doc.id and hit.pages == [2, 4]
    assert [s.page for s in hit.snippets] == [2, 4]
    assert hit.snippets[0].html == "<mark>Kündigung</mark> zum 31.12."
    assert hit.snippets[1].html == "<mark>Kündigung</mark>sfrist drei Monate"


@pytest.mark.parametrize(
    "query,expected",
    [
        ("mueller", "Herr <mark>Müller</mark> zahlt"),
        ("Müller", "Herr <mark>Müller</mark> zahlt"),
        ("ller", "Herr Mü<mark>ller</mark> zahlt"),
        ("eller", "Herr M<mark>üller</mark> zahlt"),
        ("mu", "Herr <mark>Mü</mark>ller zahlt"),
    ],
)
def test_marks_cover_the_original_characters(searcher, index, query, expected):
    add(index, "A/2025-01-01 Brief.pdf", "Herr Müller zahlt")
    assert searcher.search(query).hits[0].snippets[0].html == expected


def test_marks_with_sharp_s_and_line_breaks(searcher, index):
    add(index, "A/2025-01-01 Brief.pdf", "Wohnhaft in der Hauptstraße 5\nNeben-\nkostenabrechnung folgt")
    assert searcher.search("strasse").hits[0].snippets[0].html == "Wohnhaft in der Haupt<mark>straße</mark> 5 Neben- kostenabrechnung folgt"
    snippet = searcher.search("nebenkosten").hits[0].snippets[0].html
    assert "<mark>Neben- kosten</mark>abrechnung" in snippet


def test_snippets_escape_html(searcher, index):
    add(index, "A/2025-01-01 Brief.pdf", "<script>alert('x')</script> Rechnung & Co <b>fett</b>")
    html = searcher.search("Rechnung").hits[0].snippets[0].html
    assert "<script>" not in html and "<b>" not in html
    assert "&lt;script&gt;" in html and "&amp; Co" in html and "<mark>Rechnung</mark>" in html
    assert re.sub(r"</?mark>", "", html).count("<") == 0
    html = searcher.search("script").hits[0].snippets[0].html
    assert "&lt;<mark>script</mark>&gt;" in html


def test_long_pages_get_short_snippets_with_ellipsis(searcher, index):
    filler = " ".join(f"Wort{i}" for i in range(200))
    add(index, "A/2025-01-01 Lang.pdf", f"{filler} Haftpflichtversicherung {filler}")
    html = searcher.search("Haftpflicht").hits[0].snippets[0].html
    assert html.startswith("… ") and html.endswith(" …")
    assert "<mark>Haftpflicht</mark>versicherung" in html
    assert len(re.sub(r"</?mark>", "", html)) <= 200
    # no word is cut in half at the edges
    assert re.match(r"… Wort\d+ ", html) and re.search(r" Wort\d+ …$", html)


def test_at_most_three_snippets(searcher, index):
    add(index, "A/2025-01-01 Viele.pdf", [f"Seite {n}: Rechnung" for n in range(1, 6)])
    hit = searcher.search("Rechnung").hits[0]
    assert hit.pages == [1, 2, 3, 4, 5]
    assert len(hit.snippets) == 3 and [s.page for s in hit.snippets] == [1, 2, 3]


def test_several_snippets_from_one_long_page(searcher, index):
    filler = " ".join(f"Wort{i}" for i in range(80))
    add(index, "A/2025-01-01 Lang.pdf", f"Kaution am Anfang {filler} Kaution in der Mitte {filler} Kaution am Ende")
    hit = searcher.search("Kaution").hits[0]
    assert hit.pages == [1]
    assert len(hit.snippets) == 3 and all(s.page == 1 and "<mark>Kaution</mark>" in s.html for s in hit.snippets)


def test_snippets_from_page_prefixes_equal_snippets_from_whole_pages():
    """Only a prefix of long pages is mapped back to the original text; the result must not differ."""
    import random

    from docushelf.index import search
    from docushelf.util.textnorm import normalize

    rng = random.Random(42)
    words = ["Müller", "Straße", "Neben-\nkosten", "Konto", "12,50 €", "Überweisung", "GmbH", "Größe", "Café",
             "Rechnung", "Zahlung", "fällig", "(Kunde)", "Nr.:", "\n\n", "  ", "Ä-Ö-Ü", "Fuß", "x" * 30]
    for _ in range(100):
        source = " ".join(rng.choice(words) for _ in range(rng.randint(100, 700)))
        terms = search._terms(rng.choice(["mueller", "strasse", "nebenkosten", "rechnung zahlung", "fuss", '"12 50"']))
        for wanted in (1, 2, 3):
            fast = search._page_windows(source, normalize(source), terms, wanted=wanted)
            full = search._windows(source, search._match_spans(source, terms))[:wanted]
            assert [search._render(source, w) for w in fast] == [search._render(source, w) for w in full]


def test_hits_in_fields_only_have_no_snippets(searcher, index):
    doc = add(index, "Versicherungen/2025-01-01 Police.pdf", "Text ohne den Begriff", sender="Allianz")
    hit = searcher.search("Allianz").hits[0]
    assert hit.doc.id == doc.id and hit.snippets == [] and hit.pages == [] and hit.matched_in == ["sender"]


# -- ranking ---------------------------------------------------------------------------------------------------------


def test_title_hit_beats_body_hit(searcher, index):
    body = add(index, "A/2025-06-01 Brief.pdf", ["Haftpflichtversicherung " * 20, "Haftpflichtversicherung"])
    title = add(index, "A/2020-01-01 Haftpflichtversicherung.pdf", "Beitragsrechnung")
    result = searcher.search("Haftpflichtversicherung")
    assert ids(result) == [title.id, body.id]
    assert result.hits[0].score > result.hits[1].score
    assert "title" in result.hits[0].matched_in and result.hits[1].matched_in == ["text"]


def test_field_weights_order(searcher, index):
    in_text = add(index, "A/2025-01-05 Eins.pdf", "Allianz")
    in_folder = add(index, "Allianz/2025-01-04 Zwei.pdf", "x")
    in_sender = add(index, "B/2025-01-03 Drei.pdf", "x", sender="Allianz")
    in_title = add(index, "B/2025-01-02 Allianz Police.pdf", "x")
    assert ids(searcher.search("allianz")) == [in_title.id, in_sender.id, in_folder.id, in_text.id]


def test_newer_documents_win_ties(searcher, index):
    older = add(index, "A/2023-01-01 Kontoauszug.pdf", "x")
    newer = add(index, "A/2025-01-01 Kontoauszug.pdf", "x")
    undated = add(index, "A/Kontoauszug.pdf", "x")
    assert ids(searcher.search("Kontoauszug")) == [newer.id, older.id, undated.id]


def test_pagination_and_total(searcher, index):
    docs = [add(index, f"A/2025-01-{day:02d} Beleg.pdf", "Quittung") for day in range(1, 8)]
    full = searcher.search("Beleg")
    assert full.total == 7 and ids(full) == [d.id for d in reversed(docs)]
    page = searcher.search(SearchQuery(text="Beleg", limit=3, offset=3))
    assert page.total == 7 and ids(page) == ids(full)[3:6]
    assert searcher.search(SearchQuery(text="Beleg", limit=0)).query.limit == 50
    assert searcher.search(SearchQuery(text="Beleg", limit=10_000)).query.limit == 500


# -- query language --------------------------------------------------------------------------------------------------


def test_parse_query_with_filters_and_phrases():
    query = parse_query('Strom "Stadtwerke Dresden" ordner:"Wohnung/Strom" tag:Garantie tag:"steuer 2025" von:Sparkasse jahr:2025')
    assert query.text == 'Strom "Stadtwerke Dresden"'
    assert query.folder == "Wohnung/Strom" and query.tags == ["Garantie", "steuer 2025"] and query.sender == "Sparkasse"
    assert query.date_from == date(2025, 1, 1) and query.date_to == date(2025, 12, 31)


def test_parse_query_dates():
    query = parse_query("ab:2025-03-15 bis:31.12.2025")
    assert (query.date_from, query.date_to) == (date(2025, 3, 15), date(2025, 12, 31))
    query = parse_query("ab:2025-02 bis:2025-02")
    assert (query.date_from, query.date_to) == (date(2025, 2, 1), date(2025, 2, 28))
    query = parse_query("jahr:2023-2024")
    assert (query.date_from, query.date_to) == (date(2023, 1, 1), date(2024, 12, 31))
    query = parse_query("bis:2020 ab:2021")  # swapped bounds are put in order
    assert (query.date_from, query.date_to) == (date(2020, 12, 31), date(2021, 1, 1))
    assert parse_query("datum:03.2025").date_to == date(2025, 3, 31)


def test_parse_query_keeps_unknown_or_invalid_filters_as_text():
    query = parse_query("jahr:abc bis:2025-13-01 Termin 12:30 https://example.com tag:")
    assert query.text == "jahr:abc bis:2025-13-01 Termin 12:30 https://example.com"
    assert query.date_from is None and query.date_to is None and query.tags == []


def test_parse_query_unbalanced_quote():
    query = parse_query('Rechnung "Stadtwerke Dresden')
    assert query.text == 'Rechnung "Stadtwerke Dresden"'


def test_search_accepts_plain_strings(searcher, index):
    doc = add(index, "Bank/2025-01-01 Auszug.pdf", "Kontoauszug", tags=["Bank"])
    result = searcher.search("kontoauszug tag:bank")
    assert ids(result) == [doc.id] and result.query.tags == ["bank"]
    assert result.to_dict()["hits"][0]["doc"]["id"] == doc.id
    assert result.took_ms >= 0


# -- suggestions -------------------------------------------------------------------------------------------------------


def test_suggest_terms(searcher, index):
    add(index, "Wohnung/2025-01-01 Nebenkostenabrechnung.pdf", "Nebenkostenabrechnung Nebenkosten", sender="Hausverwaltung Müller")
    add(index, "Wohnung/2025-01-02 Nebenkosten.pdf", "Nebenkosten Vorauszahlung")
    assert searcher.suggest_terms("neb")[:2] == ["Nebenkosten", "Nebenkostenabrechnung"]
    assert searcher.suggest_terms("Rechnung von Mü") == ["Müller"]
    assert searcher.suggest_terms("vor") == ["Vorauszahlung"]  # spelled as in the page text
    assert searcher.suggest_terms("x") == [] and searcher.suggest_terms("") == []
    assert len(searcher.suggest_terms("ne", limit=1)) == 1


# -- with the real reconciler ---------------------------------------------------------------------------------------


def test_search_over_reconciled_pdfs(base_services, searcher, samples, tmp_path):
    archive = base_services.layout.archive
    raw = samples.text_pdf(tmp_path / "raw.pdf", ["Stadtwerke Dresden GmbH", "Jahresabrechnung Strom 2025\nZählerstand 4711"])
    (archive / "Wohnung" / "Strom").mkdir(parents=True)
    write_meta(
        raw,
        archive / "Wohnung" / "Strom" / "2026-01-15 Jahresabrechnung.pdf",
        DocMeta(doc_id="5f0f2d64-8a51-4a0e-9f0d-3c8a3b1c2d4e", sender="Stadtwerke Dresden", tags=["Strom"]),
    )
    samples.text_pdf(archive / "Wohnung" / "Mietvertrag.pdf", ["Mietvertrag für die Wohnung in der Königstraße"])
    Reconciler(base_services).scan()

    result = searcher.search("zaehlerstand")
    assert [h.doc.title for h in result.hits] == ["Jahresabrechnung"]
    assert result.hits[0].pages == [2] and result.hits[0].snippets[0].html.count("<mark>Zählerstand</mark>") == 1
    assert ids(searcher.search("koenigstrasse")) == [base_services.index.get_by_path("Wohnung/Mietvertrag.pdf").id]
    assert [h.doc.title for h in searcher.search("tag:strom von:stadtwerke").hits] == ["Jahresabrechnung"]
