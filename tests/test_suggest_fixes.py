"""Regression tests for the analysis and suggestion layer.

Known senders that are only mentioned (bank details, "Versichert bei: …", the
owner's own name), sender rules that fire on any mention, combined date labels
and due dates, rule title templates for another month, comments in rules.yaml,
month names in adapted titles, file names chosen by phone apps and import
suggestions for folders that were renamed.
"""

from __future__ import annotations

from datetime import date

import pytest

import docushelf.importers.paperless as paperless
from docushelf.analysis.extract import analyze, letterhead_text
from docushelf.errors import ConfigError
from docushelf.inbox.items import InboxItem
from docushelf.models import Analysis, DocumentRecord, FilingRequest, Suggestion
from docushelf.suggest.engine import SuggestionEngine
from docushelf.suggest.rules import TEMPLATE, Rule, RuleAction, RuleMatch, RuleStore, parse_rules, rule_from_filing
from docushelf.suggest.similarity import adapt_title
from docushelf.util.names import parse_filename, title_from_original_name

TODAY = date(2025, 11, 20)
BANK = "Ostsächsische Sparkasse Dresden"


def run(text: str | list[str], *, known=None, context=None) -> Analysis:
    pages = text if isinstance(text, list) else [text]
    return analyze(pages, context or {}, known_senders=known, today=TODAY)


# ---------------------------------------------------------------------------
# Known senders that are only mentioned on page 1

CRAFTSMAN = """Elektro Krause
Meisterbetrieb für Elektrotechnik
Hauptstraße 12 · 01067 Dresden · Tel. 0351 445566
Elektro Krause · Hauptstraße 12 · 01067 Dresden
Herrn
Max Mustermann
Musterweg 5
01234 Dresden
Rechnung Nr. 2025-117
Rechnungsdatum = Leistungsdatum: 05.11.2025
Austausch Steckdosen Küche
Material und Arbeitszeit
Gesamtbetrag 238,00 €
Zahlbar bis 19.11.2025 ohne Abzug.
Vielen Dank für Ihren Auftrag!
Elektro Krause · Inhaber Peter Krause · Steuernummer 203/123/45678
Bankverbindung: Ostsächsische Sparkasse Dresden
IBAN DE89 3704 0044 0532 0130 00 · BIC OSDDDE81XXX"""

PAINTER = """Malerbetrieb Schulze
Lindenweg 3, 01099 Dresden
Herrn
Max Mustermann
Musterweg 5
01234 Dresden
Rechnung 0815
Datum: 05.11.2025
Gesamt 500,00 €
{bank}"""

LAB = """Labor Dr. Weber
Befundbericht
{field}
Patient: Max Mustermann
Datum: 12.03.2025
Befund: unauffällig"""

KITA = """Kita Sonnenschein
Lindenstraße 3 · 01069 Dresden
Max Mustermann
Musterweg 5
01234 Dresden
Dresden, 03.11.2025
Elternbrief
Liebe Eltern,
am Freitag bleibt die Kita wegen einer Fortbildung geschlossen.
Ihre Kita Sonnenschein"""


def test_known_bank_in_the_bank_details_is_not_the_sender():
    result = run(CRAFTSMAN, known=[BANK, "Sparkasse"])
    assert result.sender == "Elektro Krause"
    assert not any("Sparkasse" in name for name in result.sender_candidates)


@pytest.mark.parametrize(
    "bank",
    [
        f"{BANK} IBAN DE89 3704 0044 0532 0130 00",
        f"Bankverbindung:\n{BANK}\nIBAN DE89 3704 0044 0532 0130 00",
        f"{BANK}\nIBAN DE89 3704 0044 0532 0130 00 BIC OSDDDE81XXX",
    ],
    ids=["same-line", "below-heading", "above-iban"],
)
def test_bank_details_of_a_short_letter_never_name_the_sender(bank):
    for known in ([], [BANK]):
        result = run(PAINTER.format(bank=bank), known=known)
        assert result.sender == "Malerbetrieb Schulze"


@pytest.mark.parametrize(
    "field", ["Versichert bei: AOK PLUS", "Krankenkasse   AOK PLUS", "Kostenträger AOK PLUS", "Krankenkasse: AOK PLUS"]
)
def test_known_insurer_in_a_field_is_not_the_sender(field):
    result = run(LAB.format(field=field), known=["AOK PLUS"])
    assert result.sender == "Labor Dr. Weber"
    assert not any("AOK" in name for name in result.sender_candidates)


def test_known_sender_in_its_own_letterhead_still_wins():
    letter = """AOK PLUS – Die Gesundheitskasse für Sachsen und Thüringen
AOK PLUS · 01058 Dresden
Herrn
Max Mustermann
Musterweg 5
01234 Dresden
Ihre Mitgliedsbescheinigung
Datum: 03.11.2025"""
    assert run(letter, known=["AOK PLUS"]).sender == "AOK PLUS"


def test_known_bank_mentioned_in_the_text_does_not_beat_the_letterhead():
    # A short letter: every line is in the header zone, but only the first lines are the letterhead.
    letter = """Elektro Krause
Elektro Krause · Hauptstraße 12 · 01067 Dresden
Herrn
Max Mustermann
Musterweg 5
01234 Dresden
Rechnung
Datum: 03.11.2025
Wir buchen den Betrag von Ihrem Konto bei der Ostsächsische Sparkasse Dresden ab."""
    result = run(letter, known=[BANK])
    assert result.sender == "Elektro Krause"
    assert BANK in result.sender_candidates  # still offered, but not chosen


def test_own_name_as_known_sender_is_never_the_sender():
    # Owner names configured (DOCUSHELF_OWNER_NAMES), also written the other way round.
    for known in (["Max Mustermann"], ["Mustermann, Max"]):
        result = run(KITA, known=known, context={"owner_names": ["Max Mustermann"]})
        assert result.sender == "Kita Sonnenschein"
        assert result.sender_candidates == ["Kita Sonnenschein"]
    # Not configured: an address block below the sender's address is the recipient's.
    assert run(KITA, known=["Max Mustermann"]).sender == "Kita Sonnenschein"


def test_private_sender_on_the_first_line_is_still_known():
    letter = """Hans Meier
Lindenallee 7
04109 Leipzig
Max Mustermann
Musterweg 5
01234 Dresden
Leipzig, 01.03.2025
Mieterhöhung zum 01.06.2025"""
    assert run(letter, known=["Hans Meier"]).sender == "Hans Meier"


def test_letterhead_text_leaves_out_recipient_bank_details_and_fields():
    page = """Stadtwerke Dresden GmbH · Postfach 12 03 45 · 01005 Dresden
Herrn
Max Mustermann
Musterweg 5
01234 Dresden
Abschlagsrechnung Strom"""
    text = letterhead_text([page, "Seite 2"])
    assert "stadtwerke dresden" in text
    assert "mustermann" not in text and "seite 2" not in text
    assert "aok" not in letterhead_text([LAB.format(field="Versichert bei: AOK PLUS")])
    assert "sparkasse" not in letterhead_text([f"{BANK} IBAN DE89 3704 0044 0532 0130 00\nRechnung"])
    assert letterhead_text([]) == ""


# ---------------------------------------------------------------------------
# Dates: combined labels and due dates

AMAZON = """Amazon EU S.à r.l., Niederlassung Deutschland
Marcel-Breuer-Straße 12, 80807 München
Rechnung
{label} 5 November 2025
Rechnungsnummer DE5000123456
Bestelldatum 28 Oktober 2025
Bestellnummer 302-1234567-7654321
Gesamtpreis 23,99 €"""


@pytest.mark.parametrize("label", ["Rechnungsdatum/Lieferdatum", "Rechnungsdatum / Lieferdatum"])
def test_invoice_and_delivery_date_label_keeps_the_invoice_date(label):
    result = run(AMAZON.format(label=label))
    assert result.doc_date == "2025-11-05"
    assert result.dates[0].label == label


@pytest.mark.parametrize(
    "label", ["Rechnungsdatum = Leistungsdatum:", "Rechnungs- und Leistungsdatum:", "Datum / Lieferdatum"]
)
def test_invoice_and_service_date_label_beats_the_due_date(label):
    text = CRAFTSMAN.replace("Rechnungsdatum = Leistungsdatum:", label)
    result = run(text)
    assert result.doc_date == "2025-11-05"
    due = next(d for d in result.dates if d.value == "2025-11-19")
    assert due.label == "Zahlbar bis" and due.score < 0.25


@pytest.mark.parametrize(
    "line,label",
    [
        ("Zahlbar bis 14.11.2025 ohne Abzug.", "Zahlbar bis"),
        ("Bitte zahlen Sie bis zum 14.11.2025 auf unser Konto.", "zahlen Sie bis zum"),
        ("Bitte überweisen Sie bis 14.11.2025.", "überweisen Sie bis"),
        ("Fällig bis 14.11.2025", "Fällig bis"),
    ],
)
def test_a_due_date_alone_is_never_the_document_date(line, label):
    result = run(f"Elektro Krause\nRechnung Nr. 2025-117\nGesamtbetrag 238,00 €\n{line}")
    assert result.doc_date is None
    assert result.dates[0].value == "2025-11-14" and result.dates[0].label == label


def test_a_date_scoring_exactly_the_threshold_is_not_chosen():
    # A month on page 2 scores 0.25: listed, but too weak for the document date.
    result = run(["Ohne Datum", "Gehaltsabrechnung März 2025"])
    assert result.dates[0].value == "2025-03-01" and result.dates[0].score == 0.25
    assert result.doc_date is None


def test_delivery_date_label_on_its_own_still_loses():
    result = run("Muster GmbH\nRechnungsdatum 05.11.2025\nLieferdatum 03.11.2025\nRechnung")
    assert result.doc_date == "2025-11-05"
    text = "Muster GmbH\nRechnungsdatum: siehe unten, Lieferdatum 03.11.2025\nDatum: 05.11.2025"
    assert run(text).doc_date == "2025-11-05"


# ---------------------------------------------------------------------------
# Sender rules match the detected sender, not any mention on page 1

BANK_RULE = """\
rules:
  - name: Bank
    match: {sender: [Ostsächsische Sparkasse Dresden]}
    action: {folder: Finanzen/Bank, title: "Kontoauszug {month_name} {year}"}
"""

ENERGY_INVOICE = """SachsenEnergie AG
SachsenEnergie AG · Friedrich-List-Platz 2 · 01069 Dresden
Herrn
Max Mustermann
Musterweg 5
01234 Dresden
Rechnung Strom
Rechnungsdatum 03.11.2025
Wir buchen den Betrag von Ihrem Konto bei der Ostsächsische Sparkasse Dresden ab.
Bankverbindung: Ostsächsische Sparkasse Dresden · IBAN DE89 3704 0044 0532 0130 00"""

STATEMENT = """Ostsächsische Sparkasse Dresden
Kontoauszug 10/2025
Herrn
Max Mustermann
Musterweg 5
01234 Dresden
Datum 01.11.2025
Lastschrift Telekom Deutschland GmbH -39,95
Lastschrift SachsenEnergie AG -85,00"""


@pytest.fixture
def store(layout) -> RuleStore:
    return RuleStore(layout.rules_file)


def test_bank_rule_does_not_capture_invoices_that_name_the_bank(store):
    store.save_raw_text(BANK_RULE)
    invoice = run(ENERGY_INVOICE, known=[BANK])
    assert invoice.sender == "SachsenEnergie AG"
    assert store.matches(invoice, [ENERGY_INVOICE], {}, "smb") == []
    # Without a detected sender only the letterhead counts, not the footer or the text.
    assert store.matches(Analysis(), [ENERGY_INVOICE], {}, "smb") == []


def test_merchant_rule_does_not_capture_bank_statements(store):
    store.save_raw_text("rules:\n  - name: Telekom\n    match: {sender: [Telekom]}\n    action: {folder: Telefon}\n")
    statement = run(STATEMENT, known=[BANK, "Telekom Deutschland GmbH"])
    assert statement.sender == BANK
    assert store.matches(statement, [STATEMENT], {}, "smb") == []
    # The bank's own rule still fires on its statements.
    store.save_raw_text(BANK_RULE)
    hits = store.matches(statement, [STATEMENT], {}, "smb")
    assert [hit.rule.name for hit in hits] == ["Bank"] and not hits[0].page1_only


def test_rule_from_filing_uses_only_the_confirmed_sender():
    data = Analysis(sender="Ostsächsische Sparkasse Dresden", references={"Kundennummer": ["K-77"]},
                    ibans=["DE89370400440532013000"])
    # The user cleared the (wrong) detected sender: the rule uses the customer number.
    request = FilingRequest(folder="Handwerker", title="Rechnung Elektro", doc_date=date(2025, 11, 5), sender="")
    assert rule_from_filing(request, data, "upload").match.conditions() == {"reference": ["K-77"]}
    request.sender = None
    assert rule_from_filing(request, Analysis(sender="Sparkasse", ibans=["DE89370400440532013000"]),
                            "upload").match.conditions() == {"iban": ["DE89370400440532013000"]}
    with pytest.raises(ConfigError):
        rule_from_filing(request, Analysis(sender="Sparkasse"), "upload")
    request.sender = "Elektro Krause"
    assert rule_from_filing(request, data, "upload").match.conditions() == {"sender": ["Elektro Krause"]}


# ---------------------------------------------------------------------------
# Rule titles for another month than the document date


@pytest.mark.parametrize(
    "title,when",
    [
        ("Kontoauszug Oktober 2025", date(2025, 11, 1)),
        ("Kontoauszug 10/2025", date(2025, 11, 1)),
        ("Kontoauszug 10.2025", date(2025, 11, 1)),
        ("Kontoauszug 2025-10", date(2025, 11, 1)),
        ("Gehaltsabrechnung März 2025", date(2025, 4, 2)),
        ("Abschlag Nov. 2025", date(2025, 11, 3)),
        ("Steuerbescheid 2024 vom 14.05.2025", date(2025, 5, 14)),
        ("Kontoauszug 01.10.2025 - 31.10.2025", date(2025, 11, 1)),
    ],
)
def test_title_naming_another_period_gives_no_template(title, when):
    request = FilingRequest(folder="Finanzen/Bank", title=title, doc_date=when, sender="Sparkasse")
    assert rule_from_filing(request, Analysis(), "upload").action.title is None


@pytest.mark.parametrize(
    "title,when,template",
    [
        ("Kontoauszug November 2025", date(2025, 11, 30), "Kontoauszug {month_name} {year}"),
        ("Kontoauszug 11/2025", date(2025, 11, 30), "Kontoauszug {date:%m/%Y}"),
        ("Rechnung Jan Mustermann 2025", date(2025, 11, 3), "Rechnung Jan Mustermann {year}"),
        ("Bescheid vom 14.05.2025", date(2025, 5, 14), "Bescheid vom {date:%d.%m.%Y}"),
    ],
)
def test_title_of_the_document_month_still_becomes_a_template(title, when, template):
    request = FilingRequest(folder="Finanzen/Bank", title=title, doc_date=when, sender="Sparkasse")
    assert rule_from_filing(request, Analysis(), "upload").action.title == template


# ---------------------------------------------------------------------------
# rules.yaml: comments survive structured changes

COMMENTED = """\
# Strom: Vertrag 4711, Kündigungsfrist 3 Monate
rules:
  - name: Stadtwerke Strom   # seit 2019
    match:
      sender: [Stadtwerke Dresden]
    action:
      folder: Wohnung/Strom
"""


def new_rule(sender: str = "Sparkasse Dresden", folder: str = "Finanzen/Bank") -> Rule:
    return Rule(sender, RuleMatch(sender=[sender]), RuleAction(folder=folder, title="Kontoauszug {month_name}"))


def test_adding_a_rule_keeps_comments(store):
    store.save_raw_text(COMMENTED)
    assert store.add(new_rule()) == 1
    text = store.path.read_text(encoding="utf-8")
    assert text.startswith(COMMENTED)
    assert "# Strom: Vertrag 4711" in text and "# seit 2019" in text
    assert [rule.name for rule in store.load()] == ["Stadtwerke Strom", "Sparkasse Dresden"]
    assert parse_rules(text) == store.load()
    assert not store.backup_path.exists() and store.last_backup is None


def test_adding_a_rule_to_the_template_keeps_its_comments(store):
    store.save_raw_text(TEMPLATE)
    store.add(new_rule())
    text = store.path.read_text(encoding="utf-8")
    assert text.startswith(TEMPLATE.split("\nrules: []")[0])
    assert "\nrules: []" not in text and "\nrules:\n  - name: Sparkasse Dresden\n" in text
    assert [rule.name for rule in store.load()] == ["Sparkasse Dresden"]


def test_adding_a_rule_to_other_layouts(store):
    # A plain list at the top level, without "rules:".
    store.save_raw_text("# Liste\n- name: A\n  match: {sender: [A]}\n  action: {folder: A}\n")
    store.add(new_rule())
    assert store.path.read_text(encoding="utf-8").startswith("# Liste\n- name: A")
    assert [rule.name for rule in store.load()] == ["A", "Sparkasse Dresden"]
    # A flow-style list cannot be appended to: rewritten, the comments kept in the backup.
    flow = "# Kurzform\nrules: [{name: B, match: {sender: [B]}, action: {folder: B}}]\n"
    store.save_raw_text(flow)
    store.add(new_rule())
    assert [rule.name for rule in store.load()] == ["B", "Sparkasse Dresden"]
    assert store.last_backup == store.backup_path
    assert store.backup_path.read_text(encoding="utf-8") == flow


def test_replace_and_delete_keep_a_backup_of_comments(store):
    store.save_raw_text(COMMENTED)
    store.add(new_rule())
    commented = store.path.read_text(encoding="utf-8")
    store.delete(1)
    assert store.last_backup == store.backup_path
    assert store.backup_path.read_text(encoding="utf-8") == commented
    assert "Vertrag 4711" not in store.path.read_text(encoding="utf-8")
    # The file has no comments of its own now: the backup is not overwritten.
    store.replace(0, new_rule("Stadtwerke Dresden", "Wohnung/Energie"))
    assert store.last_backup is None
    assert store.backup_path.read_text(encoding="utf-8") == commented
    assert store.load()[0].action.folder == "Wohnung/Energie"


def test_structured_changes_without_comments_write_no_backup(store):
    store.add(new_rule())
    store.add(new_rule("Stadtwerke Dresden", "Wohnung/Strom"))
    store.add(new_rule("Stadtwerke Dresden", "Wohnung/Energie"))  # same conditions: replaced
    store.delete(0)
    assert [rule.action.folder for rule in store.load()] == ["Wohnung/Energie"]
    assert not store.backup_path.exists()


def test_comment_characters_inside_values_are_not_comments(store):
    store.save_raw_text('rules:\n  - name: "Rechnung #1"\n    match: {sender: [A]}\n    action: {folder: A}\n')
    store.delete(0)
    assert not store.backup_path.exists()


# ---------------------------------------------------------------------------
# Titles of similar documents and file names of phone apps


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Gehaltsabrechnung März 2025", "Gehaltsabrechnung April 2025"),
        ("Kontoauszug März 2025", "Kontoauszug April 2025"),
        ("Kontoauszug Mär. 2025", "Kontoauszug Apr. 2025"),
        ("Kontoauszug Maerz 2025", "Kontoauszug April 2025"),
    ],
)
def test_full_month_names_are_not_abbreviated(title, expected):
    assert adapt_title(title, date(2025, 3, 31), date(2025, 4, 30)) == expected


@pytest.mark.parametrize(
    "name",
    [
        "Gescanntes Dokument.pdf",
        "Gescanntes Dokument 2.pdf",
        "Scanned Document 2.pdf",
        "Adobe Scan 20. Nov. 2025.pdf",
        "Adobe Scan Nov 20, 2025 (1).pdf",
        "Microsoft Lens 20.11.2025 10.12.pdf",
        "Office Lens 20251120-101200.jpg",
        "Bildschirmfoto 2025-11-20 um 10.12.00.png",
        "Screenshot 2025-11-20 at 10.12.00.png",
        "Screenshot_20251120-101200_WhatsApp.jpg",
        "WhatsApp Bild 2025-11-20 um 10.12.00.jpeg",
        "WhatsApp Image 2025-11-20 at 10.12.00 (1).jpeg",
        "WhatsApp Image 2024-03-15 at 10.12.00_6b9c2a1f.jpg",
        "IMG-20251120-WA0003.jpg",
        "PXL_20251120_101200123.MP.jpg",
        "CamScanner 11-20-2025 10.12.pdf",
        "New Doc 2025-11-20 10.12.12.pdf",
        "Scan_20251120_101200.pdf",
        "Scan_Rechnung.pdf",
        "image (1).jpg",
        "2025-11-20 Gescanntes Dokument.pdf",
    ],
)
def test_file_names_of_phone_and_scanner_apps_are_no_titles(name):
    assert title_from_original_name(name) is None


@pytest.mark.parametrize(
    "name,title",
    [
        ("Kaufvertrag Auto.pdf", "Kaufvertrag Auto"),
        ("Rechnung_Telekom_Mai.pdf", "Rechnung Telekom Mai"),
        ("Screenshot Kontostand.png", "Screenshot Kontostand"),
        ("Kopie Mietvertrag.pdf", "Kopie Mietvertrag"),
        ("Juli 2023.pdf", "Juli 2023"),
        ("2025-11-03 Rechnung Strom.pdf", "Rechnung Strom"),
    ],
)
def test_meaningful_file_names_stay_titles(name, title):
    assert title_from_original_name(name) == title


# ---------------------------------------------------------------------------
# The suggestion engine

ENGINE_INVOICE = """Stadtwerke Dresden GmbH
Stadtwerke Dresden GmbH · Rosenstraße 32 · 01067 Dresden
Herrn
Max Mustermann
Musterweg 5
01234 Dresden
Rechnung Fernwärme
Rechnungsdatum 03.11.2025
Fernwärme Grundpreis Arbeitspreis Verbrauch Heizperiode
Gesamtbetrag 120,00 €"""


def add_doc(index, doc_id: str, path: str, text: str, *, sender: str | None = None) -> None:
    folder = path.rsplit("/", 1)[0] if "/" in path else ""
    filename = path.rsplit("/", 1)[-1]
    parsed = parse_filename(filename)
    if folder:
        index.add_folder_with_parents(folder)
    index.upsert(
        DocumentRecord(
            id=doc_id, path=path, folder=folder, filename=filename, ext=".pdf", title=parsed.title,
            doc_date=parsed.doc_date, sender=sender, tags=[], origin_hash=None, content_hash="c-" + doc_id,
            xmp_id=None, has_xmp=False, size=1, mtime_ns=1, inode=None, page_count=1, text_status="ok",
            indexed_at="",
        ),
        [text],
    )


def make_item(*, source: str = "upload", files=("original/scan_0001.pdf",), context=None) -> InboxItem:
    return InboxItem(id="20251120-100000-abcdef", source=source, received_at="2025-11-20T10:00:00+01:00",
                     original_files=list(files), origin_hash="0" * 64, context=dict(context or {}))


@pytest.fixture
def svc(base_services, layout):
    base_services.rules = RuleStore(layout.rules_file)
    return base_services


@pytest.fixture
def engine(svc) -> SuggestionEngine:
    return SuggestionEngine(svc)


def suggest(engine: SuggestionEngine, text: str, item: InboxItem | None = None) -> list[Suggestion]:
    item = item or make_item()
    analysis = analyze([text], item.context, known_senders=engine.known_senders(), today=TODAY)
    return engine.suggest(item, [text], analysis)


def test_weak_rule_match_ranks_below_import_and_full_rules(engine, svc):
    svc.rules.save_raw_text(
        "rules:\n"
        "  - name: Logo\n    match: {sender: [DREWAG]}\n    action: {folder: Wohnung/Logo}\n"
        "  - name: Typ\n    match: {doc_type: [Rechnung]}\n    action: {folder: Rechnungen}\n"
    )
    text = "DREWAG\nRechnung\nRechnungsdatum 03.11.2025\nGesamtbetrag 12,00 €"
    analysis = analyze([text], {}, today=TODAY)
    assert analysis.sender is None
    suggestions = engine.suggest(make_item(), [text], analysis)
    assert [(s.folder, s.score) for s in suggestions[:2]] == [("Rechnungen", 1.0), ("Wohnung/Logo", 0.9)]
    assert "„DREWAG“ im Briefkopf" in suggestions[1].reasons
    imported = Suggestion(folder="Belege", title="Rechnung", doc_date="2025-11-03")
    item = make_item(context={"import_suggestion": imported.to_dict()})
    svc.rules.save_raw_text("rules:\n  - name: Logo\n    match: {sender: [DREWAG]}\n    action: {folder: Wohnung/Logo}\n")
    suggestions = engine.suggest(item, [text], analysis)
    assert [(s.source, s.score) for s in suggestions[:2]] == [("import", 0.95), ("rule", 0.9)]


def test_bank_rule_does_not_win_against_similar_documents(engine, svc):
    svc.rules.save_raw_text(BANK_RULE)
    for n in range(3):
        add_doc(svc.index, f"e{n}", f"Wohnung/Strom/2025-0{n + 1}-03 Rechnung Strom.pdf", ENERGY_INVOICE,
                sender="SachsenEnergie AG")
    top = suggest(engine, ENERGY_INVOICE)[0]
    assert top.folder == "Wohnung/Strom" and top.source == "similarity"


def test_upload_named_by_a_phone_app_gets_a_composed_title(engine):
    item = make_item(files=("original/Gescanntes Dokument.pdf",))
    assert suggest(engine, ENGINE_INVOICE, item)[0].title == "Rechnung Stadtwerke Dresden"


def import_item(folder: str, *, sender: str = "Stadtwerke Dresden GmbH", paperless_data=None) -> InboxItem:
    imported = Suggestion(folder=folder, title="Rechnung Fernwärme", doc_date="2025-11-03", sender=sender)
    data = {"id": 7, "correspondent": sender, "document_type": "Rechnung", "created": "2025-11-03"}
    data.update(paperless_data or {})
    return make_item(source="paperless", files=("original/0000007.pdf",),
                     context={"paperless": data, "import_suggestion": imported.to_dict()})


def test_import_folder_follows_the_current_mapping(engine, svc, monkeypatch):
    calls = []

    def recompute(context, config_dir):
        calls.append(config_dir)
        return dict(context["import_suggestion"], folder="Rechnungen/Stadtwerke Dresden GmbH")

    monkeypatch.setattr(paperless, "recompute_import_suggestion", recompute, raising=False)
    top = suggest(engine, ENGINE_INVOICE, import_item("Finanzen/Rechnungen/Stadtwerke Dresden GmbH"))[0]
    assert top.source == "import" and top.folder == "Rechnungen/Stadtwerke Dresden GmbH"
    assert top.title == "Rechnung Fernwärme" and calls == [svc.layout.config]


@pytest.mark.parametrize("broken", ["missing", "raises", "returns-none"])
def test_stored_import_folder_without_a_usable_mapping(engine, monkeypatch, broken):
    if broken == "missing":
        monkeypatch.delattr(paperless, "recompute_import_suggestion", raising=False)
    else:
        def recompute(context, config_dir):
            if broken == "raises":
                raise RuntimeError("kaputt")
            return None

        monkeypatch.setattr(paperless, "recompute_import_suggestion", recompute, raising=False)
    top = suggest(engine, ENGINE_INVOICE, import_item("Finanzen/Rechnungen/Stadtwerke Dresden GmbH"))[0]
    assert top.source == "import" and top.folder == "Finanzen/Rechnungen/Stadtwerke Dresden GmbH"


def test_changed_mapping_file_reaches_waiting_items(engine, svc):
    if not hasattr(paperless, "recompute_import_suggestion"):
        pytest.skip("the importer cannot render import suggestions again")
    (svc.layout.config / paperless.MAPPING_FILENAME).write_text(
        'document_types:\n  Rechnung: "Rechnungen/{correspondent}"\ndefault: "Sonstiges"\n', encoding="utf-8"
    )
    item = import_item("Finanzen/Rechnungen/Stadtwerke Dresden GmbH",
                       paperless_data={"mapped_folder": "Finanzen/Rechnungen/Stadtwerke Dresden GmbH"})
    top = suggest(engine, ENGINE_INVOICE, item)[0]
    assert top.source == "import" and top.folder == "Rechnungen/Stadtwerke Dresden GmbH"


def test_missing_import_folder_yields_to_a_folder_with_very_similar_documents(engine, svc):
    # The folder the first invoice was filed into has been renamed since.
    add_doc(svc.index, "f1", "Finanzen/Rechnungen/Stadtwerke/2025-10-03 Rechnung Fernwärme.pdf", ENGINE_INVOICE,
            sender="Stadtwerke Dresden GmbH")
    suggestions = suggest(engine, ENGINE_INVOICE, import_item("Finanzen/Rechnungen/Stadtwerke Dresden GmbH"))
    top, second = suggestions[0], suggestions[1]
    assert top.folder == "Finanzen/Rechnungen/Stadtwerke" and top.source == "similarity" and top.score >= 0.9
    assert second.source == "import" and second.new_folder and second.score < top.score
    assert second.reasons[-1] == "Ordner gibt es noch nicht – „Finanzen/Rechnungen/Stadtwerke“ passt besser"
    # Title, date and sender of the import still pre-fill the form.
    assert top.title == "Rechnung Fernwärme" and top.sender == "Stadtwerke Dresden GmbH"


def test_missing_import_folder_yields_to_the_senders_folder_next_to_it(engine, svc):
    svc.settings = svc.settings.with_overrides(similarity_enabled=False)
    add_doc(svc.index, "f1", "Finanzen/Rechnungen/Stadtwerke/2025-10-03 Rechnung.pdf", "kurz",
            sender="Stadtwerke Dresden GmbH")
    suggestions = suggest(engine, ENGINE_INVOICE, import_item("Finanzen/Rechnungen/Stadtwerke Dresden GmbH"))
    assert [s.source for s in suggestions[:2]] == ["heuristic", "import"]
    assert suggestions[0].folder == "Finanzen/Rechnungen/Stadtwerke"


@pytest.mark.parametrize(
    "existing,missing",
    [
        # The same sender's contract lives elsewhere; invoices get their own new folder.
        ("Verträge/Stadtwerke Dresden GmbH", "Finanzen/Rechnungen/Stadtwerke Dresden GmbH"),
        # Folders per document type side by side, named after the type, not the sender.
        ("Persönliches/Zeugnisse", "Persönliches/Bescheinigungen"),
    ],
)
def test_import_folder_stays_first_when_the_existing_folder_is_another_kind(engine, svc, existing, missing):
    svc.settings = svc.settings.with_overrides(similarity_enabled=False)
    add_doc(svc.index, "v1", f"{existing}/2024-01-01 Dokument.pdf", "kurz", sender="Stadtwerke Dresden GmbH")
    top = suggest(engine, ENGINE_INVOICE, import_item(missing))[0]
    assert top.source == "import" and top.score == 0.95 and top.new_folder and top.folder == missing


def test_import_folder_from_a_changed_mapping_stays_first(engine, svc, monkeypatch):
    # The user restructured the mapping; documents filed before stay where they are.
    add_doc(svc.index, "f1", "Finanzen/Rechnungen/Stadtwerke Dresden GmbH/2025-10-03 Rechnung Fernwärme.pdf",
            ENGINE_INVOICE, sender="Stadtwerke Dresden GmbH")

    def recompute(context, config_dir):
        return dict(context["import_suggestion"], folder="Rechnungen/Stadtwerke Dresden GmbH")

    monkeypatch.setattr(paperless, "recompute_import_suggestion", recompute, raising=False)
    suggestions = suggest(engine, ENGINE_INVOICE, import_item("Finanzen/Rechnungen/Stadtwerke Dresden GmbH"))
    assert suggestions[0].source == "import" and suggestions[0].score == 0.95
    assert suggestions[0].folder == "Rechnungen/Stadtwerke Dresden GmbH" and suggestions[0].new_folder
    assert suggestions[1].folder == "Finanzen/Rechnungen/Stadtwerke Dresden GmbH"


def test_import_folder_for_a_new_year_stays_first(engine, svc):
    add_doc(svc.index, "s1", "Steuern/2024/2024-11-03 Rechnung Fernwärme.pdf", ENGINE_INVOICE,
            sender="Stadtwerke Dresden GmbH")
    top = suggest(engine, ENGINE_INVOICE, import_item("Steuern/2025"))[0]
    assert top.source == "import" and top.folder == "Steuern/2025" and top.score == 0.95
