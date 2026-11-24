"""Tests for the TF-IDF similarity model over the archive index."""

from __future__ import annotations

import random
import threading
import time
from datetime import date

import pytest

from docushelf.models import DocumentRecord
from docushelf.suggest.similarity import SimilarityModel, adapt_title
from docushelf.util.names import parse_filename
from docushelf.util.textnorm import normalize

STATEMENT = """Sparkasse Dresden
Kontoauszug {n}/2025 Seite 1 von 1
Kontoauszug Nr. {n} vom {day}
Girokonto IBAN DE38 8505 0300 1234 5678 90
Buchungstag Wertstellung Verwendungszweck Betrag
Alter Kontostand 1.234,56 EUR
Lastschrift {shop} Kartenzahlung girocard
Gutschrift Gehalt Muster Software GmbH
Dauerauftrag Miete Hausverwaltung
Neuer Kontostand 2.345,67 EUR
Sparkasse Dresden Güntzplatz 5 01307 Dresden"""

UTILITY = """Stadtwerke Leipzig GmbH
Abschlagsrechnung Strom
Rechnungsdatum {day}
Vertragskonto 2000 1234 5678 Zählernummer 1ESY1161234567
Leistungszeitraum Stromlieferung Abschlagsplan
Monatlicher Abschlag 85,00 € wird per Lastschrift eingezogen
Arbeitspreis Grundpreis Ökostrom Tarif"""

PAYSLIP = """Muster Software GmbH
Entgeltabrechnung für {month} 2025
Personalnummer 00123 Steuerklasse 1
Gesamtbrutto 4.500,00 Lohnsteuer Solidaritätszuschlag Kirchensteuer
Krankenversicherung Rentenversicherung Arbeitslosenversicherung Pflegeversicherung
Nettoverdienst Auszahlungsbetrag"""

POLICY = """Allianz Versicherungs-AG
Versicherungsschein Privathaftpflichtversicherung
Versicherungsnehmer Versicherungsbeginn Deckungssumme Selbstbeteiligung
Jahresbeitrag 65,40 €"""


def add_doc(index, doc_id: str, path: str, text: str, *, sender: str | None = None) -> None:
    folder = path.rsplit("/", 1)[0] if "/" in path else ""
    filename = path.rsplit("/", 1)[-1]
    parsed = parse_filename(filename)
    if folder:
        index.add_folder_with_parents(folder)
    record = DocumentRecord(
        id=doc_id, path=path, folder=folder, filename=filename, ext=".pdf", title=parsed.title,
        doc_date=parsed.doc_date, sender=sender, tags=[], origin_hash=None, content_hash="c-" + doc_id,
        xmp_id=None, has_xmp=False, size=1, mtime_ns=1, inode=None, page_count=1, text_status="ok", indexed_at="",
    )
    index.upsert(record, [text])


@pytest.fixture
def archive(base_services):
    index = base_services.index
    shops = ["REWE Markt", "Amazon Payments", "dm Drogerie"]
    for n, (month, day) in enumerate([("Januar", "31.01.2025"), ("Februar", "28.02.2025"), ("März", "31.03.2025")],
                                     start=1):
        add_doc(index, f"st{n}", f"Finanzen/Bank/Sparkasse/2025-{n:02d}-{int(day[:2]):02d} Kontoauszug {month}.pdf",
                STATEMENT.format(n=n, day=day, shop=shops[n - 1]), sender="Sparkasse Dresden")
    add_doc(index, "sw1", "Wohnung/Strom/2024-02-03 Abschlag Stadtwerke.pdf", UTILITY.format(day="03.02.2024"),
            sender="Stadtwerke Leipzig GmbH")
    add_doc(index, "sw2", "Wohnung/Strom/2025-02-03 Abschlag Stadtwerke.pdf", UTILITY.format(day="03.02.2025"),
            sender="Stadtwerke Leipzig GmbH")
    add_doc(index, "pay1", "Arbeit/Gehalt/2025-09-01 Gehaltsabrechnung September 2025.pdf",
            PAYSLIP.format(month="September"), sender="Muster Software GmbH")
    add_doc(index, "pay2", "Arbeit/Gehalt/2025-10-01 Gehaltsabrechnung Oktober 2025.pdf",
            PAYSLIP.format(month="Oktober"), sender="Muster Software GmbH")
    add_doc(index, "pol", "Versicherungen/Haftpflicht/2024-12-05 Versicherungsschein Haftpflicht.pdf", POLICY,
            sender="Allianz Versicherungs-AG")
    return base_services


def test_suggests_the_folder_of_similar_documents(archive):
    model = SimilarityModel(archive)
    query = normalize(STATEMENT.format(n=4, day="30.04.2025", shop="Edeka Center"))
    suggestions = model.suggest(query, sender="Sparkasse Dresden", limit=3)
    top = suggestions[0]
    assert top.folder == "Finanzen/Bank/Sparkasse"
    assert top.source == "similarity"
    assert 0.5 < top.score <= 0.94  # never as confident as a rule
    assert top.reasons[0].startswith("ähnlich zu „2025-0") and top.reasons[0].endswith("%)")
    assert "3 ähnliche Dokumente in diesem Ordner" in top.reasons
    assert "gleicher Absender wie 3 Dokumente in diesem Ordner" in top.reasons
    assert all(s.folder != "Finanzen/Bank/Sparkasse" for s in suggestions[1:])
    assert all(s.score >= 0.2 for s in suggestions)


def test_other_document_kinds(archive):
    model = SimilarityModel(archive)
    assert model.suggest(normalize(UTILITY.format(day="03.02.2026")))[0].folder == "Wohnung/Strom"
    assert model.suggest(normalize(PAYSLIP.format(month="November")))[0].folder == "Arbeit/Gehalt"
    assert model.suggest(normalize(POLICY))[0].folder == "Versicherungen/Haftpflicht"
    assert model.suggest(normalize("Ganz anderer Text über Gartenmöbel und Rasenmäher")) == []


def test_title_of_nearest_document_is_adapted_to_the_new_date(archive):
    model = SimilarityModel(archive)
    payslip = normalize(PAYSLIP.format(month="November"))
    top = model.suggest(payslip, doc_date=date(2025, 11, 1))[0]
    assert top.title == "Gehaltsabrechnung November 2025"
    # Without the new date a title with a month cannot be reused.
    assert model.suggest(payslip)[0].title is None
    # Titles without dates are reused as they are.
    utility = model.suggest(normalize(UTILITY.format(day="03.02.2026")), doc_date="2026-02-03")[0]
    assert utility.title == "Abschlag Stadtwerke"


def test_sender_folders(archive):
    model = SimilarityModel(archive)
    assert model.sender_folders("Sparkasse Dresden") == [("Finanzen/Bank/Sparkasse", 3)]
    # Spelled with or without legal form, the sender is the same.
    assert model.sender_folders("STADTWERKE LEIPZIG") == [("Wohnung/Strom", 2)]
    assert model.sender_folders(None) == []


def test_empty_archive_and_root_documents(base_services):
    model = SimilarityModel(base_services)
    assert model.suggest(normalize(POLICY)) == []
    add_doc(base_services.index, "root1", "2024-12-05 Versicherungsschein.pdf", POLICY)
    model.invalidate()
    assert model.suggest(normalize(POLICY)) == []  # the archive root is no filing target
    assert model.neighbours(normalize(POLICY))[0][0] == "root1"


def test_rebuild_follows_index_generation_with_interval(archive):
    model = SimilarityModel(archive, rebuild_interval=3600)
    query = normalize("Zahnarztpraxis Dr. Lächler Heil- und Kostenplan Zahnersatz Implantat Krone")
    assert model.suggest(query) == []
    old_cache = dict(model._term_cache)
    add_doc(archive.index, "zahn", "Gesundheit/Zahnarzt/2025-01-10 Heil- und Kostenplan.pdf",
            "Zahnarztpraxis Dr. Lächler\nHeil- und Kostenplan Zahnersatz Implantat Krone Festzuschuss")
    # Rebuilt at most every rebuild_interval seconds: the stale model is still used.
    assert model.suggest(query) == []
    model.rebuild_interval = 0
    suggestions = model.suggest(query)
    assert suggestions and suggestions[0].folder == "Gesundheit/Zahnarzt"
    # Term counts of unchanged documents were reused, not recomputed.
    assert model._term_cache["st1"][1] is old_cache["st1"][1]
    assert model._model is not None and model._model.generation == archive.index.generation


def test_changed_text_of_a_document_is_recomputed(archive):
    model = SimilarityModel(archive, rebuild_interval=0)
    model.suggest(normalize(POLICY))
    before = model._term_cache["pol"][1]
    add_doc(archive.index, "pol", "Versicherungen/Haftpflicht/2024-12-05 Versicherungsschein Haftpflicht.pdf",
            POLICY + "\nNachtrag Hausrat Glasversicherung")
    model.suggest(normalize(POLICY))
    assert model._term_cache["pol"][1] is not before
    assert model._term_cache["pol"][1]["glasversicherung"] == 1


def test_concurrent_queries_while_the_archive_changes(archive):
    model = SimilarityModel(archive, rebuild_interval=0)
    errors: list[BaseException] = []
    done = threading.Event()
    query = normalize(STATEMENT.format(n=5, day="31.05.2025", shop="REWE"))

    def worker():
        try:
            while not done.is_set():
                result = model.suggest(query, sender="Sparkasse Dresden")
                assert result and result[0].folder == "Finanzen/Bank/Sparkasse"
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for i in range(15):
        add_doc(archive.index, f"extra{i}", f"Sonstiges/2025-01-{i + 1:02d} Notiz {i}.pdf", f"Notiz Nummer {i} Garten")
        time.sleep(0.01)
    done.set()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors


def test_thousands_of_documents_build_and_query_quickly(base_services):
    rng = random.Random(7)
    common = [f"wort{i}" for i in range(3000)]
    folders = [f"Bereich{f // 10}/Thema{f}" for f in range(50)]
    topics = {folder: [f"thema{n}begriff{k}" for k in range(25)] for n, folder in enumerate(folders)}
    index = base_services.index
    conn = index.conn()
    conn.execute("BEGIN")
    for folder in folders:
        index.upsert_folder(folder.split("/")[0])
        index.upsert_folder(folder)
    conn.execute("COMMIT")
    count = 3000
    for i in range(count):
        folder = folders[i % len(folders)]
        words = rng.choices(topics[folder], k=60) + rng.choices(common, k=240)
        add_doc(index, f"d{i}", f"{folder}/2025-01-01 Dokument {i}.pdf", " ".join(words))
    model = SimilarityModel(base_services)
    started = time.monotonic()
    model.suggest("")
    build = time.monotonic() - started
    target = folders[17]
    query = " ".join(rng.choices(topics[target], k=40) + rng.choices(common, k=150))
    started = time.monotonic()
    suggestions = model.suggest(normalize(query))
    elapsed = time.monotonic() - started
    assert suggestions[0].folder == target
    assert build < 20, f"building took {build:.1f}s"
    assert elapsed < 1.0, f"query took {elapsed:.2f}s"


@pytest.mark.parametrize(
    "title,old,new,expected",
    [
        ("Kontoauszug November", date(2025, 11, 3), date(2025, 12, 2), "Kontoauszug Dezember"),
        ("Gehaltsabrechnung Dezember 2025", date(2026, 1, 2), date(2026, 2, 1), "Gehaltsabrechnung Januar 2026"),
        ("Gehaltsabrechnung März 2025", date(2025, 3, 31), date(2025, 4, 30), "Gehaltsabrechnung April 2025"),
        ("Steuerbescheid 2023", date(2024, 5, 10), date(2025, 6, 1), "Steuerbescheid 2024"),
        ("Kontoauszug 11/2025", date(2025, 11, 30), date(2025, 12, 31), "Kontoauszug 12/2025"),
        ("Abschlag Jan. 2025", date(2025, 1, 1), date(2025, 2, 1), "Abschlag Feb. 2025"),
        ("Rechnung Jan Müller", date(2025, 1, 1), date(2025, 2, 1), "Rechnung Jan Müller"),
        ("Bericht 2025-11-03", date(2025, 11, 3), date(2025, 12, 2), "Bericht 2025-12-02"),
        ("Kontoauszug November", None, date(2025, 12, 2), "Kontoauszug Dezember"),
        ("Kontoauszug November", date(2025, 11, 3), None, None),
        ("Stromrechnung", None, None, "Stromrechnung"),
    ],
)
def test_adapt_title(title, old, new, expected):
    assert adapt_title(title, old, new) == expected
