"""Tests for the rule store (rules.yaml), rule matching and title templates."""

from __future__ import annotations

import os
import threading
from datetime import date

import pytest
import yaml

from docushelf.errors import ConfigError, NotFound
from docushelf.models import Analysis, FilingRequest
from docushelf.suggest.rules import (
    TEMPLATE,
    Rule,
    RuleAction,
    RuleMatch,
    RuleStore,
    clean_subject,
    parse_rules,
    render_title,
    rule_from_filing,
)

STADTWERKE_PAGE = """Stadtwerke Dresden GmbH · Postfach 12 03 45 · 01005 Dresden
Herrn
Max Mustermann
Musterweg 5
01234 Dresden
Abschlagsrechnung Strom
Kundennummer: 123 456 789
Monatlicher Abschlag 85,00 €
Konto DE89 3704 0044 0532 0130 00"""

VALID = """\
# meine Regeln
rules:
  - name: Stadtwerke Strom
    match:
      sender: [Stadtwerke Dresden, DREWAG]
      keywords_any: [Abschlag]
      keywords_all: []
    action:
      folder: Wohnung/Strom
      title: "{sender} {type} {date:%m/%Y}"
      tags: ["steuerrelevant {year}"]
    auto: false
  - name: Gehalt
    match:
      doc_type: Gehaltsabrechnung
    action:
      folder: Arbeit/Gehalt
"""


def analysis(**values) -> Analysis:
    defaults = dict(sender="Stadtwerke Dresden GmbH", doc_type="Rechnung", ibans=["DE89370400440532013000"])
    defaults.update(values)
    return Analysis(**defaults)


@pytest.fixture
def store(layout) -> RuleStore:
    return RuleStore(layout.rules_file)


def temp_files(directory):
    return [p for p in directory.iterdir() if p.name.startswith(".docushelf-")]


# ---------------------------------------------------------------------------
# Reading and writing


def test_missing_file_gives_no_rules_and_a_template(store):
    assert store.load() == []
    assert store.error is None
    text = store.raw_text()
    assert text == TEMPLATE
    assert "# rules:" in text and "Stadtwerke Strom" in text  # commented example
    assert parse_rules(text) == []  # saving the template unchanged is valid
    assert not store.path.exists()  # reading never creates the file


def test_save_raw_text_validates_and_writes_atomically(store):
    rules = store.save_raw_text(VALID)
    assert [r.name for r in rules] == ["Stadtwerke Strom", "Gehalt"]
    assert store.path.read_text(encoding="utf-8") == VALID  # comments kept exactly
    assert temp_files(store.path.parent) == []
    loaded = store.load()
    assert loaded[0].match.sender == ["Stadtwerke Dresden", "DREWAG"]
    assert loaded[0].action == RuleAction(folder="Wohnung/Strom", title="{sender} {type} {date:%m/%Y}",
                                          tags=["steuerrelevant {year}"])
    assert loaded[1].match.doc_type == ["Gehaltsabrechnung"]


def test_invalid_yaml_is_rejected_with_line_and_file_untouched(store):
    store.save_raw_text(VALID)
    with pytest.raises(ConfigError) as excinfo:
        store.save_raw_text("rules:\n  - name: x\n    match: [unclosed\n")
    assert "Zeile" in str(excinfo.value)
    assert store.path.read_text(encoding="utf-8") == VALID


@pytest.mark.parametrize(
    "body,fragment",
    [
        ("- match: {sender: X}\n  action: {folder: A}", "„name“ fehlt"),
        ("- name: R\n  action: {folder: A}", "„match“ fehlt"),
        ("- name: R\n  match: {sendr: X}\n  action: {folder: A}", "unbekannte Bedingung „sendr“"),
        ("- name: R\n  match: {sender: []}\n  action: {folder: A}", "keine Bedingung"),
        ("- name: R\n  match: {sender: X}", "„action“ mit „folder“ fehlt"),
        ("- name: R\n  match: {sender: X}\n  action: {folder: ../etc}", "ungültig"),
        ("- name: R\n  match: {sender: X}\n  action: {folder: A, title: '{absender}'}", "unbekannter Platzhalter"),
        ("- name: R\n  match: {sender: X}\n  action: {folder: A, title: 'a } b'}", "geschweifte Klammer"),
        ("- name: R\n  match: {doc_type: Rechnungen}\n  action: {folder: A}", "unbekannter Dokumenttyp"),
        ("- name: R\n  match: {mail_from: rechnung}\n  action: {folder: A}", "keine Mail-Adresse"),
        ("- name: R\n  match: {iban: 'DE 12 ?'}\n  action: {folder: A}", "keine IBAN"),
        ("- name: R\n  match: {sender: X}\n  action: {folder: A}\n  auto: vielleicht", "true oder false"),
        ("- name: R\n  match: {sender: X}\n  action: {folder: A}\n  prio: 3", "unbekannter Eintrag „prio“"),
        ("- name: R\n  match: {sender: {a: b}}\n  action: {folder: A}", "„match.sender“"),
    ],
)
def test_schema_errors_are_german_and_point_to_the_rule(store, body, fragment):
    text = "rules:\n" + "\n".join("  " + line for line in body.splitlines()) + "\n"
    with pytest.raises(ConfigError) as excinfo:
        store.save_raw_text(text)
    assert fragment in str(excinfo.value)
    assert "Regel 1" in str(excinfo.value)
    assert not store.path.exists()


def test_top_level_errors():
    with pytest.raises(ConfigError, match="rules:"):
        parse_rules("regeln: []\n")
    with pytest.raises(ConfigError, match="Regel 1"):
        parse_rules("- nur\n- eine Liste\n")
    with pytest.raises(ConfigError):
        parse_rules("rules: keine Liste\n")
    with pytest.raises(ConfigError):
        parse_rules("42\n")
    assert parse_rules("") == [] and parse_rules("rules:\n") == []


def test_oversized_text_is_refused(store):
    with pytest.raises(ConfigError, match="zu groß"):
        store.save_raw_text("# " + "x" * 1_100_000 + "\nrules: []\n")
    assert not store.path.exists()


def test_numbers_and_dates_stay_text():
    rules = parse_rules(
        "rules:\n  - name: 2025\n    match: {reference: [0815, 00123], keywords_any: [2025-01-01]}\n"
        "    action: {folder: Archiv/2025}\n"
    )
    assert rules[0].name == "2025"
    assert rules[0].match.reference == ["0815", "00123"]
    assert rules[0].match.keywords_any == ["2025-01-01"]


def test_save_writes_readable_yaml_that_round_trips(store):
    rules = parse_rules(VALID)
    store.save(rules)
    text = store.path.read_text(encoding="utf-8")
    assert text.startswith("# Ablageregeln")
    assert "sender: [Stadtwerke Dresden, DREWAG]" in text
    assert parse_rules(text) == rules
    assert yaml.safe_load(text)["rules"][0]["action"]["folder"] == "Wohnung/Strom"


def test_external_edit_is_picked_up_and_invalid_edit_keeps_last_good_rules(store):
    store.save_raw_text(VALID)
    assert len(store.load()) == 2
    # Edited in an editor: one rule removed.
    store.path.write_text(VALID.split("  - name: Gehalt")[0], encoding="utf-8")
    os.utime(store.path, ns=(1, 1))
    assert [r.name for r in store.load()] == ["Stadtwerke Strom"]
    # A typo: the last good rules stay active and the error is visible.
    store.path.write_text("rules:\n  - name: kaputt\n    match: {sendr: x}\n", encoding="utf-8")
    os.utime(store.path, ns=(2, 2))
    assert [r.name for r in store.load()] == ["Stadtwerke Strom"]
    assert store.error and "sendr" in store.error
    # Structured edits would overwrite the broken file: refused.
    with pytest.raises(ConfigError):
        store.add(Rule("Neu", RuleMatch(sender=["X"]), RuleAction(folder="A")))
    assert "kaputt" in store.path.read_text(encoding="utf-8")
    # Fixing the file clears the error.
    store.save_raw_text(VALID)
    assert store.error is None


def test_load_returns_copies(store):
    store.save_raw_text(VALID)
    rules = store.load()
    rules[0].match.sender.append("Fremd")
    rules.pop()
    assert store.load()[0].match.sender == ["Stadtwerke Dresden", "DREWAG"]
    assert len(store.load()) == 2


def test_add_replace_delete(store):
    first = Rule("Sparkasse", RuleMatch(sender=["Sparkasse Dresden"]), RuleAction(folder="Finanzen/Bank"))
    assert store.add(first) == 0
    second = Rule("Gehalt", RuleMatch(doc_type=["Gehaltsabrechnung"]), RuleAction(folder="Arbeit/Gehalt"))
    assert store.add(second) == 1
    # Same conditions again (e.g. remembered twice): updated, not duplicated.
    again = Rule("Sparkasse neu", RuleMatch(sender=["sparkasse dresden"]), RuleAction(folder="Finanzen/Konto"))
    assert store.add(again) == 0
    rules = store.load()
    assert [r.name for r in rules] == ["Sparkasse neu", "Gehalt"]
    assert rules[0].action.folder == "Finanzen/Konto"
    store.replace(1, Rule("Lohn", RuleMatch(doc_type=["Gehaltsabrechnung"]), RuleAction(folder="Arbeit/Lohn")))
    assert store.load()[1].name == "Lohn"
    store.delete(0)
    assert [r.name for r in store.load()] == ["Lohn"]
    with pytest.raises(NotFound):
        store.delete(5)
    with pytest.raises(NotFound):
        store.replace(-1, first)
    with pytest.raises(ConfigError):
        store.add(Rule("Leer", RuleMatch(), RuleAction(folder="A")))


def test_concurrent_reads_while_writing(store):
    store.save_raw_text(VALID)
    errors: list[BaseException] = []
    stop = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                assert len(store.load()) in (2, 3)
                store.match(analysis(), [STADTWERKE_PAGE], {}, "upload")
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for thread in threads:
        thread.start()
    for i in range(20):
        store.add(Rule(f"R{i}", RuleMatch(sender=[f"Absender {i % 1}"]), RuleAction(folder="A")))
    stop.set()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors


# ---------------------------------------------------------------------------
# Matching


def write_rules(store, body: str) -> None:
    store.save_raw_text("rules:\n" + body)


def test_match_all_conditions_and_reasons(store):
    store.save_raw_text(VALID)
    matches = store.match(analysis(), [STADTWERKE_PAGE], {}, "smb")
    assert len(matches) == 1
    rule, reasons = matches[0]
    assert rule.name == "Stadtwerke Strom"
    assert reasons == ["Regel „Stadtwerke Strom“", "Absender: Stadtwerke Dresden GmbH", "Stichwort „Abschlag“"]


def test_all_conditions_must_hold(store):
    write_rules(store, "  - name: R\n    match: {sender: [Stadtwerke], keywords_all: [Abschlag, Gas]}\n"
                       "    action: {folder: A}\n")
    assert store.match(analysis(), [STADTWERKE_PAGE], {}, "upload") == []
    write_rules(store, "  - name: R\n    match: {sender: [Stadtwerke], keywords_all: [abschlag, STROM]}\n"
                       "    action: {folder: A}\n")
    assert store.match(analysis(), [STADTWERKE_PAGE], {}, "upload")[0][1][-1] == "Stichwörter „abschlag“, „STROM“"


def test_sender_matches_detected_sender_or_page_one(store):
    write_rules(store, "  - name: R\n    match: {sender: [DREWAG, Stadtwerke Dresden]}\n    action: {folder: A}\n")
    # Not detected as sender, but printed in the letterhead of page 1: a weaker match.
    matches = store.match(analysis(sender=None), [STADTWERKE_PAGE], {}, "upload")
    assert matches and matches[0][1][1] == "„Stadtwerke Dresden“ im Briefkopf"
    assert store.matches(analysis(sender=None), [STADTWERKE_PAGE], {}, "upload")[0].page1_only
    assert not store.matches(analysis(), [STADTWERKE_PAGE], {}, "upload")[0].page1_only
    # Only on page 2: no match.
    assert store.match(analysis(sender=None), ["Rechnung", STADTWERKE_PAGE], {}, "upload") == []
    # Word starts only: "AOK" must not match inside "Kakaokaufmann".
    write_rules(store, "  - name: R\n    match: {sender: [AOK]}\n    action: {folder: A}\n")
    assert store.match(analysis(sender="Kakaokaufmann GmbH"), ["Kakaokaufmann"], {}, "upload") == []


def test_keywords_match_parts_of_compound_words_and_umlauts(store):
    write_rules(store, "  - name: R\n    match: {keywords_any: [Strom, Müllabfuhr]}\n    action: {folder: A}\n")
    assert store.match(analysis(), ["Ihre Ökostromrechnung"], {}, "upload")
    assert store.match(analysis(), ["Gebühr für MUELLABFUHR"], {}, "upload")
    assert not store.match(analysis(), ["Gasrechnung"], {}, "upload")


def test_iban_reference_source_mail_and_type_conditions(store):
    write_rules(
        store,
        "  - name: IBAN\n    match: {iban: ['DE89 3704 0044 0532 0130 00']}\n    action: {folder: A}\n"
        "  - name: Nummer\n    match: {reference: ['123456789']}\n    action: {folder: B}\n"
        "  - name: Quelle\n    match: {source: ['watch-*']}\n    action: {folder: C}\n"
        "  - name: Mail\n    match: {mail_from: ['@stadtwerke-dresden.de']}\n    action: {folder: D}\n"
        "  - name: Typ\n    match: {doc_type: [rechnung]}\n    action: {folder: E}\n",
    )
    context = {"mail_from": "Stadtwerke <rechnung@mail.stadtwerke-dresden.de>"}
    matches = store.match(analysis(), [STADTWERKE_PAGE], context, "watch-nas")
    assert [rule.name for rule, _ in matches] == ["IBAN", "Nummer", "Quelle", "Mail", "Typ"]
    reasons = {rule.name: reasons[1] for rule, reasons in matches}
    assert reasons == {
        "IBAN": "IBAN DE89 3704 0044 0532 0130 00",
        "Nummer": "Nummer „123456789“",  # printed as "123 456 789"
        "Quelle": "Eingang über watch-nas",
        "Mail": "Mail von rechnung@mail.stadtwerke-dresden.de",
        "Typ": "Dokumenttyp: Rechnung",
    }
    other = store.match(analysis(doc_type="Kontoauszug", ibans=[]), ["Kundennummer 1234567890"], {}, "upload")
    assert other == []  # partial numbers do not count


def test_match_accepts_analysis_dict_and_survives_empty_input(store):
    store.save_raw_text(VALID)
    assert store.match(analysis().to_dict(), [STADTWERKE_PAGE], None, None)
    assert store.match(None, [], None, None) == []


# ---------------------------------------------------------------------------
# Titles


def test_render_title_placeholders():
    data = analysis(sender="Stadtwerke Dresden GmbH", doc_type="Rechnung")
    when = date(2025, 3, 9)
    assert render_title("{sender} {type} {date:%m/%Y}", analysis=data, context={}, doc_date=when) == \
        "Stadtwerke Dresden GmbH Rechnung 03/2025"
    assert render_title("{date} {year}-{month} {month_name} {date:%B %Y} {date:%d. %b}", analysis=data, context={},
                        doc_date="2025-03-09") == "09.03.2025 2025-03 März März 2025 09. Mär"
    context = {"mail_subject": "AW: WG: Ihre Rechnung 4711", "original_name": "Rechnung_Telekom_Mai.pdf"}
    assert render_title("{subject} / {original}", analysis=data, context=context, doc_date=None) == \
        "Ihre Rechnung 4711 / Rechnung Telekom Mai"
    assert render_title("{{wörtlich}} {type}", analysis=data, context={}, doc_date=None) == "{wörtlich} Rechnung"


def test_render_title_collapses_missing_values():
    empty = Analysis()
    assert render_title("{sender} {type} {date:%m/%Y}", analysis=Analysis(doc_type="Rechnung"), context={},
                        doc_date=date(2025, 3, 1)) == "Rechnung 03/2025"
    assert render_title("{sender} - {type} ({subject})", analysis=empty, context={}, doc_date=None) == ""
    assert render_title("Strom {sender} - {type} - {year}", analysis=empty, context={}, doc_date=date(2025, 1, 1)) == \
        "Strom - 2025"
    assert render_title("[{sender}] Abschlag", analysis=empty, context={}, doc_date=None) == "Abschlag"


def test_clean_subject():
    assert clean_subject("AW: WG: Fwd:  Ihre   Rechnung") == "Ihre Rechnung"
    assert clean_subject(None) == ""


# ---------------------------------------------------------------------------
# Rules from filing decisions


def test_rule_from_filing_generalizes_dates():
    request = FilingRequest(
        folder="Wohnung / Strom",
        title="Abschlag März 2025",
        doc_date=date(2025, 3, 3),
        tags=["steuerrelevant 2025", "Strom"],
        sender="Stadtwerke Dresden",
        create_rule=True,
    )
    rule = rule_from_filing(request, analysis(), "mail")
    assert rule.name == "Stadtwerke Dresden"
    assert rule.match.conditions() == {"sender": ["Stadtwerke Dresden"]}
    assert rule.action.folder == "Wohnung/Strom"
    assert rule.action.title == "Abschlag {month_name} {year}"
    assert rule.action.tags == ["steuerrelevant {year}", "Strom"]
    assert rule.auto is False
    rendered = render_title(rule.action.title, analysis=analysis(), context={}, doc_date=date(2025, 4, 2))
    assert rendered == "Abschlag April 2025"


def test_rule_from_filing_keeps_no_title_without_date_parts():
    request = FilingRequest(folder="Bank", title="Kontoauszug", doc_date=date(2025, 3, 31), sender="Sparkasse")
    assert rule_from_filing(request, analysis(), "upload").action.title is None


def test_rule_from_filing_without_sender_uses_numbers_then_iban_then_watch_folder():
    request = FilingRequest(folder="Versicherungen/Haftpflicht", title="Police", doc_date=None)
    data = Analysis(references={"Kundennummer": ["K-1"], "Versicherungsnummer": ["AS-123/456789"]})
    rule = rule_from_filing(request, data, "upload")
    assert rule.match.conditions() == {"reference": ["AS-123/456789"]}
    rule = rule_from_filing(request, Analysis(ibans=["DE89370400440532013000"]), "upload")
    assert rule.match.conditions() == {"iban": ["DE89370400440532013000"]}
    rule = rule_from_filing(request, Analysis(), "watch-steuer")
    assert rule.match.conditions() == {"source": ["watch-steuer"]}
    with pytest.raises(ConfigError):
        rule_from_filing(request, Analysis(), "upload")


def test_rule_from_filing_result_can_be_stored(store):
    request = FilingRequest(folder="Finanzen/Bank", title="Kontoauszug 03/2025", doc_date=date(2025, 3, 31),
                            sender="Sparkasse Dresden")
    rule = rule_from_filing(request, analysis(sender="Sparkasse Dresden"), "smb")
    store.add(rule)
    assert store.load()[0].action.title == "Kontoauszug {date:%m/%Y}"
    matches = store.match(analysis(sender="Sparkasse Dresden"), ["Sparkasse Dresden\nKontoauszug"], {}, "smb")
    assert matches and matches[0][0].action.folder == "Finanzen/Bank"


# ---------------------------------------------------------------------------
# Rules for automatic filing


def test_template_explains_the_auto_flag():
    assert "# auto: true legt passende Dokumente ohne Rückfrage ab" in TEMPLATE
    assert "DOCUSHELF_AUTO_FILE" in TEMPLATE
    assert "reserviert" not in TEMPLATE


def test_auto_flag_is_read_and_reaches_the_matches(store):
    store.save_raw_text(VALID.replace("auto: false", "auto: true"))
    rules = store.load()
    assert [rule.auto for rule in rules] == [True, False]
    hits = store.matches(analysis(), [STADTWERKE_PAGE], {}, "smb")
    assert [(hit.rule.name, hit.rule.auto) for hit in hits] == [("Stadtwerke Strom", True)]


def test_comment_of_the_earlier_template_is_not_taken_for_an_own_comment(store):
    earlier = (
        "# auto: false ist für die spätere automatische Ablage reserviert.\n"
        "rules:\n  - name: A\n    match: {sender: [A]}\n    action: {folder: A}\n"
        "  - name: B\n    match: {sender: [B]}\n    action: {folder: B}\n"
    )
    store.save_raw_text(earlier)
    store.delete(0)
    assert store.last_backup is None and not store.backup_path.exists()
    store.save_raw_text("# meine Notiz\n" + earlier)
    store.delete(0)
    assert store.last_backup == store.backup_path
