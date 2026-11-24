"""Tests for the suggestion engine (rules + similarity + heuristics + optional LLM)."""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
import respx

from docushelf.analysis.extract import analyze
from docushelf.config import LlmConfig
from docushelf.inbox.items import InboxItem
from docushelf.models import Analysis, DocumentRecord, Suggestion
from docushelf.suggest.engine import RankedSuggestion, SuggestionEngine, composed_title
from docushelf.suggest.llm import LlmClient
from docushelf.suggest.rules import RuleStore
from docushelf.util.names import parse_filename

TODAY = date(2025, 11, 20)

STADTWERKE = """Stadtwerke Leipzig GmbH
Augustusplatz 7 · 04109 Leipzig
Stadtwerke Leipzig GmbH · Postfach 10 06 26 · 04006 Leipzig
Herrn
Max Mustermann
Musterweg 5
04229 Leipzig
Abschlagsrechnung Strom
Rechnungsdatum: 03.02.2025
Kundennummer: 4711 0815
Vertragskonto: 2000 1234 5678
Leistungszeitraum: 01.02.2025 - 31.01.2026
Monatlicher Abschlag ab 01.03.2025: 85,00 €
Stromlieferung Arbeitspreis Grundpreis Ökostrom Abschlagsplan
Zahlbar bis 15.02.2025."""

KONTOAUSZUG = """Sparkasse Dresden
Kontoauszug Nr. {n} vom {day}
Herrn
Max Mustermann
Musterweg 5
01234 Dresden
Girokonto IBAN DE38 8505 0300 1234 5678 90
Buchungstag Wertstellung Verwendungszweck Betrag
Lastschrift REWE Markt Kartenzahlung girocard
Gutschrift Gehalt Muster Software GmbH
Neuer Kontostand vom {day} 2.345,67 EUR"""

POLICY = """Allianz Versicherungs-AG
Allianz Versicherungs-AG · 10850 Berlin
Herrn
Max Mustermann
Musterweg 5
01234 Dresden
Versicherungsschein
Privathaftpflichtversicherung
Versicherungsscheinnummer: AS-123/456789
Datum: 05.12.2024
Jahresbeitrag: 65,40 €"""

RULES = """\
rules:
  - name: Stadtwerke Strom
    match:
      sender: [Stadtwerke Leipzig]
      keywords_any: [Abschlag]
    action:
      folder: wohnung/strom
      title: "Abschlag {month_name} {year}"
      tags: ["steuerrelevant {year}", Strom]
"""


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


def make_item(*, source: str = "upload", files=("original/scan_0001.pdf",), context=None,
              received_at: str = "2025-11-20T10:00:00+01:00") -> InboxItem:
    return InboxItem(id="20251120-100000-abcdef", source=source, received_at=received_at,
                     original_files=list(files), origin_hash="0" * 64, context=dict(context or {}))


@pytest.fixture(autouse=True)
def no_retry_pause(monkeypatch):
    monkeypatch.setattr(LlmClient, "retry_delay", 0.0)


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


def assert_complete(suggestions: list[Suggestion]) -> None:
    assert suggestions
    folders = [s.folder.casefold() for s in suggestions]
    assert len(folders) == len(set(folders)) and len(suggestions) <= 5
    assert [s.score for s in suggestions] == sorted((s.score for s in suggestions), reverse=True)
    for s in suggestions:
        assert s.title and s.doc_date and 0 <= s.score <= 1 and s.reasons


# ---------------------------------------------------------------------------
# Sources and merging


def test_rule_suggestion_comes_first_with_rendered_title_and_tags(engine, svc):
    svc.index.add_folder_with_parents("Wohnung/Strom")
    svc.rules.save_raw_text(RULES)
    suggestions = suggest(engine, STADTWERKE)
    assert_complete(suggestions)
    top = suggestions[0]
    assert top.source == "rule" and top.score == 1.0 and top.rule_name == "Stadtwerke Strom"
    assert top.folder == "Wohnung/Strom"  # the archive's spelling, not the rule's
    assert top.new_folder is False
    assert top.title == "Abschlag Februar 2025"
    assert top.tags == ["steuerrelevant 2025", "Strom"]
    assert top.doc_date == "2025-02-03"
    # Senders named in rules are known senders: their spelling is used.
    assert top.sender == "Stadtwerke Leipzig"
    assert top.reasons[:3] == ["Regel „Stadtwerke Strom“", "Absender: Stadtwerke Leipzig", "Stichwort „Abschlag“"]


def test_rule_target_that_does_not_exist_yet_is_marked_new(engine, svc):
    svc.rules.save_raw_text(RULES)
    suggestions = suggest(engine, STADTWERKE)
    top = suggestions[0]
    assert top.source == "rule" and top.folder == "wohnung/strom" and top.new_folder is True
    # The rule decided; no additional folder is invented.
    assert [s.folder for s in suggestions] == ["wohnung/strom"]


def test_similarity_and_rule_for_the_same_folder_are_merged(engine, svc):
    add_doc(svc.index, "sw1", "Wohnung/Strom/2024-02-03 Abschlag Februar 2024.pdf", STADTWERKE.replace("2025", "2024"),
            sender="Stadtwerke Leipzig GmbH")
    svc.rules.save_raw_text(RULES)
    suggestions = suggest(engine, STADTWERKE)
    assert_complete(suggestions)
    top = suggestions[0]
    assert top.folder == "Wohnung/Strom" and top.score == 1.0 and top.source == "rule"
    assert any(reason.startswith("ähnlich zu „2024-02-03 Abschlag Februar 2024“") for reason in top.reasons)
    assert "gleicher Absender wie 1 Dokument in diesem Ordner" in top.reasons
    assert [s.folder for s in suggestions].count("Wohnung/Strom") == 1


def test_similarity_learns_from_filed_documents(engine, svc):
    for n, day in enumerate(["31.01.2025", "28.02.2025"], start=1):
        add_doc(svc.index, f"k{n}", f"Finanzen/Konten/2025-0{n}-{day[:2]} Kontoauszug {['Januar', 'Februar'][n - 1]}.pdf",
                KONTOAUSZUG.format(n=n, day=day), sender="Sparkasse Dresden")
    suggestions = suggest(engine, KONTOAUSZUG.format(n=3, day="31.03.2025"))
    assert_complete(suggestions)
    top = suggestions[0]
    assert top.folder == "Finanzen/Konten" and top.source == "similarity"
    assert top.title == "Kontoauszug März"  # neighbour title moved to the new month
    assert top.sender == "Sparkasse Dresden" and top.doc_date == "2025-03-31"
    assert "gleicher Absender wie 2 Dokumente in diesem Ordner" in top.reasons
    assert not any(s.new_folder for s in suggestions)


def test_sender_folders_without_similarity(engine, svc):
    svc.settings = svc.settings.with_overrides(similarity_enabled=False)
    for n in range(3):
        add_doc(svc.index, f"k{n}", f"Finanzen/Konten/2025-0{n + 1}-28 Auszug {n}.pdf", "kurz", sender="Sparkasse Dresden")
    add_doc(svc.index, "x", "Sonstiges/2025-01-01 Brief.pdf", "kurz", sender="Sparkasse Dresden")
    suggestions = suggest(engine, KONTOAUSZUG.format(n=3, day="31.03.2025"))
    top = suggestions[0]
    assert top.folder == "Finanzen/Konten" and top.source == "heuristic"
    assert top.reasons[0] == "gleicher Absender wie 3 Dokumente in diesem Ordner"
    assert any(s.folder == "Sonstiges" for s in suggestions)


def test_import_suggestion_from_paperless(engine, svc):
    svc.index.add_folder_with_parents("Rechnungen/Alza")
    imported = Suggestion(folder="Rechnungen/Alza", title="Monitor 27 Zoll", doc_date="2024-11-05", sender="Alza",
                          tags=["Garantie"])
    context = {
        "paperless": {"id": 42, "correspondent": "Alza", "document_type": "Rechnung"},
        "import_suggestion": imported.to_dict(),
        "title": "Monitor 27 Zoll",
    }
    item = make_item(source="paperless", files=("original/0000042.pdf",), context=context)
    suggestions = suggest(engine, "Alza.cz a.s.\nRechnung\nDatum 05.11.2024\nMonitor", item)
    assert_complete(suggestions)
    top = suggestions[0]
    assert top.source == "import" and top.score == 0.95 and top.folder == "Rechnungen/Alza"
    assert top.reasons[0] == "aus Paperless: Typ Rechnung, Korrespondent Alza"
    assert "Absender: Alza" in top.reasons  # the folder name agrees; reasons are combined
    assert top.tags == ["Garantie"]
    # The imported metadata pre-fills every suggestion.
    for suggestion in suggestions:
        assert suggestion.title == "Monitor 27 Zoll"
        assert suggestion.doc_date == "2024-11-05" and suggestion.sender == "Alza"


def test_import_suggestion_keeps_its_own_reasons_and_unsafe_folders_are_dropped(engine):
    context = {"import_suggestion": {"folder": "../Ausbruch", "title": "X", "reasons": ["eigene Begründung"]}}
    suggestions = suggest(engine, "Text", make_item(context=context))
    assert all(s.source != "import" for s in suggestions)
    context = {"import_suggestion": {"folder": "Belege", "reasons": ["eigene Begründung"]}}
    top = suggest(engine, "Text", make_item(context=context))[0]
    assert top.source == "import" and top.reasons == ["eigene Begründung"] and top.new_folder is True


# ---------------------------------------------------------------------------
# Heuristics and new folders


def test_folder_named_after_sender_and_document_type(engine, svc):
    for folder in ("Versicherungen/Allianz", "Versicherungen/HUK", "Versicherungen", "Steuern"):
        svc.index.add_folder_with_parents(folder)
    suggestions = suggest(engine, POLICY)
    assert_complete(suggestions)
    top = suggestions[0]
    assert top.folder == "Versicherungen/Allianz" and top.source == "heuristic"
    assert top.reasons == ["Absender: Allianz Versicherungs-AG", "Dokumenttyp: Versicherungsschein"]
    assert top.title == "Versicherungsschein Allianz"
    assert suggestions[1].folder == "Versicherungen"
    assert all(s.folder != "Versicherungen/HUK" for s in suggestions)
    assert not any(s.new_folder for s in suggestions)


def test_new_folder_only_when_nothing_fits(engine, svc):
    suggestions = suggest(engine, POLICY)
    assert len(suggestions) == 1
    proposal = suggestions[0]
    assert proposal.new_folder is True and proposal.folder == "Versicherungen/Allianz"
    assert proposal.score < 0.3
    assert "neuer Ordner" in proposal.reasons[0]
    assert proposal.title == "Versicherungsschein Allianz" and proposal.doc_date == "2024-12-05"

    svc.index.add_folder_with_parents("Versicherungen")
    suggestions = suggest(engine, POLICY)
    assert [s.folder for s in suggestions] == ["Versicherungen"]
    assert not suggestions[0].new_folder


def test_new_folder_template_that_already_exists_is_not_new(engine, svc):
    svc.index.add_folder_with_parents("Gesundheit/Befunde")
    text = "Praxis Dr. med. Anna Schmidt\nArztbrief\nDresden, 12.03.2025\nDiagnose: Hypertonie\nBefund: EKG unauffällig"
    top = suggest(engine, text)[0]
    assert top.folder == "Gesundheit/Befunde" and not top.new_folder


def test_unknown_everything_falls_back_cleanly(engine):
    suggestions = suggest(engine, "", make_item(source="smb"))
    assert len(suggestions) == 1
    top = suggestions[0]
    assert top.folder == "Sonstiges" and top.new_folder
    assert top.title == "Dokument vom 20.11.2025" and top.doc_date == "2025-11-20" and top.sender is None


# ---------------------------------------------------------------------------
# Titles, dates and senders on every suggestion


def test_title_from_meaningful_upload_name(engine):
    item = make_item(files=("original/Kaufvertrag Auto.pdf",))
    top = suggest(engine, "Kaufvertrag\nzwischen Hans Meier und Max Mustermann\nVertragspartner\nUnterschrift", item)[0]
    assert top.title == "Kaufvertrag Auto"


def test_scanner_names_and_mail_sources_use_composed_titles(engine):
    assert suggest(engine, POLICY, make_item(source="upload"))[0].title == "Versicherungsschein Allianz"
    item = make_item(source="mail", files=("original/Rechnung_2025123456.pdf",),
                     context={"mail_from": "Vodafone <rechnung@vodafone.de>", "mail_subject": "Ihre Rechnung"})
    top = suggest(engine, "Rechnung\nRechnungsnummer 2025123456\nRechnungsdatum 01.04.2025\nGesamtbetrag 39,99 €",
                  item)[0]
    assert top.title == "Rechnung Vodafone" and top.sender == "Vodafone"


def test_mail_date_and_subject_as_fallbacks(engine):
    item = make_item(source="mail", files=("original/document.pdf",),
                     context={"mail_subject": "WG: Bauantrag Garage", "mail_date": "Tue, 04 Mar 2025 10:15:00 +0100"})
    top = suggest(engine, "", item)[0]
    assert top.title == "Bauantrag Garage"
    assert top.doc_date == "2025-03-04"


def test_context_title_wins_over_composed_title(engine):
    item = make_item(context={"title": "Wichtige Unterlagen"})
    assert suggest(engine, POLICY, item)[0].title == "Wichtige Unterlagen"


def test_composed_title():
    assert composed_title("Rechnung", "Muster Elektronik GmbH & Co. KG") == "Rechnung Muster Elektronik"
    assert composed_title(None, "Sparkasse Dresden") == "Sparkasse Dresden"
    assert composed_title("Kontoauszug", None) == "Kontoauszug"
    assert composed_title(None, None) is None


def test_at_most_five_suggestions_and_rules_keep_file_order(engine, svc):
    body = "".join(
        f"  - name: R{n}\n    match: {{keywords_any: [Kontoauszug]}}\n    action: {{folder: Ziel{n}}}\n"
        for n in range(7)
    )
    svc.rules.save_raw_text("rules:\n" + body)
    for n in range(4):
        add_doc(svc.index, f"d{n}", f"Ordner{n}/2025-01-01 Kontoauszug.pdf", KONTOAUSZUG.format(n=n, day="01.01.2025"),
                sender="Sparkasse Dresden")
    suggestions = suggest(engine, KONTOAUSZUG.format(n=13, day="31.03.2025"))
    assert_complete(suggestions)
    assert [s.folder for s in suggestions] == ["Ziel0", "Ziel1", "Ziel2", "Ziel3", "Ziel4"]


def test_analysis_as_dict_is_accepted(engine):
    analysis = Analysis(doc_type="Rechnung", sender="Muster GmbH", doc_date="2025-01-02")
    suggestions = engine.suggest(make_item(), ["Rechnung"], analysis.to_dict())
    assert suggestions[0].title == "Rechnung Muster" and suggestions[0].doc_date == "2025-01-02"


# ---------------------------------------------------------------------------
# Known senders, robustness


def test_known_senders_combine_archive_and_rules(engine, svc):
    add_doc(svc.index, "a", "Bank/2025-01-01 A.pdf", "x", sender="Sparkasse Dresden")
    add_doc(svc.index, "b", "Bank/2025-01-02 B.pdf", "x", sender="Sparkasse Dresden")
    add_doc(svc.index, "c", "Strom/2025-01-02 C.pdf", "x", sender="Stadtwerke Leipzig GmbH")
    svc.rules.save_raw_text(RULES + "  - name: Bank\n    match: {sender: [SPARKASSE DRESDEN, DREWAG]}\n"
                                    "    action: {folder: Bank}\n")
    assert engine.known_senders() == ["Sparkasse Dresden", "Stadtwerke Leipzig GmbH", "Stadtwerke Leipzig", "DREWAG"]


def test_failing_source_does_not_break_the_others(engine, svc, monkeypatch):
    svc.rules.save_raw_text(RULES)

    def broken(*args, **kwargs):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(engine.similarity, "suggest", broken)
    suggestions = suggest(engine, STADTWERKE)
    assert suggestions[0].source == "rule"
    # A broken rules file keeps the last good rules active.
    svc.rules.path.write_text("rules: [kaputt", encoding="utf-8")
    assert suggest(engine, STADTWERKE)[0].source == "rule"


def test_engine_without_rule_store(base_services):
    engine = SuggestionEngine(base_services)  # e.g. a minimal container in tools
    assert engine.known_senders() == []
    assert suggest(engine, POLICY)[0].new_folder


# ---------------------------------------------------------------------------
# Language model


def test_llm_suggest_without_configuration_returns_none(engine):
    assert engine.llm_suggest(make_item(), [POLICY], Analysis()) is None


@respx.mock
def test_llm_suggest_with_ollama(engine, svc):
    svc.settings = svc.settings.with_overrides(
        llm=LlmConfig(url="http://ollama.test:11434", model="qwen2.5:3b", timeout=5)
    )
    svc.index.add_folder_with_parents("Versicherungen/Haftpflicht")
    content = {"titel": "Haftpflicht Police", "datum": None, "absender": None, "ordner": "versicherungen/haftpflicht",
               "neuer_ordner": False, "tags": [], "begruendung": "Police einer Haftpflichtversicherung."}
    route = respx.post("http://ollama.test:11434/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": json.dumps(content)}})
    )
    item = make_item(files=("original/police.pdf",), context={"mail_subject": "Ihre Police"})
    analysis = analyze([POLICY], {}, today=TODAY)
    suggestion = engine.llm_suggest(item, [POLICY], analysis)
    assert suggestion is not None and suggestion.source == "llm"
    assert suggestion.folder == "Versicherungen/Haftpflicht" and not suggestion.new_folder
    assert suggestion.title == "Haftpflicht Police"
    # Missing values come from the analysis, like on every other suggestion.
    assert suggestion.doc_date == "2024-12-05" and suggestion.sender == "Allianz Versicherungs-AG"
    user = json.loads(route.calls.last.request.content)["messages"][1]["content"]
    assert "Dateiname: police.pdf" in user and "Betreff der E-Mail: Ihre Police" in user


@respx.mock
def test_llm_failure_gives_none(engine, svc):
    svc.settings = svc.settings.with_overrides(llm=LlmConfig(url="http://ollama.test:11434", model="m", timeout=1))
    respx.post("http://ollama.test:11434/api/chat").mock(side_effect=httpx.ConnectError("refused"))
    assert engine.llm_suggest(make_item(), [POLICY], Analysis()) is None


# ---------------------------------------------------------------------------
# With the real service container (only once all components exist)


def test_with_real_services(settings, tmp_path, samples):
    try:
        from docushelf.services import Services

        instance = Services(settings)
    except ImportError as exc:  # other components are developed in parallel
        pytest.skip(f"service container not complete yet: {exc}")
    try:
        from docushelf.pdf.text import extract_pages

        assert isinstance(instance.suggester, SuggestionEngine)
        assert isinstance(instance.rules, RuleStore)
        instance.rules.save_raw_text(RULES)
        pdf = samples.text_pdf(tmp_path / "abschlag.pdf", [STADTWERKE])
        pages = extract_pages(pdf)
        analysis = analyze(pages, {}, known_senders=instance.suggester.known_senders(), today=TODAY)
        suggestions = instance.suggester.suggest(make_item(), pages, analysis)
        assert suggestions[0].source == "rule" and suggestions[0].title == "Abschlag Februar 2025"
    finally:
        instance.stop()
        instance.index.close()


# ---------------------------------------------------------------------------
# Details for automatic filing

AUTO_RULES = """\
rules:
  - name: Stadtwerke Strom
    match:
      sender: [Stadtwerke Leipzig]
      keywords_any: [Abschlag]
    action:
      folder: Wohnung/Strom
      title: "Abschlag {month_name} {year}"
      tags: [Strom]
    auto: true
  - name: Alles von den Stadtwerken
    match:
      sender: [Stadtwerke Leipzig]
    action:
      folder: Wohnung/Allgemein
"""

OLLAMA = "http://ollama.test:11434"


def llm_settings(svc, **changes) -> None:
    values = dict(url=OLLAMA, model="qwen2.5:3b", timeout=5)
    values.update(changes)
    svc.settings = svc.settings.with_overrides(llm=LlmConfig(**values))


def test_rule_suggestions_carry_their_auto_flag(engine, svc):
    svc.index.add_folder_with_parents("Wohnung/Strom")
    svc.rules.save_raw_text(AUTO_RULES)
    suggestions = suggest(engine, STADTWERKE)
    assert all(isinstance(s, RankedSuggestion) for s in suggestions)
    by_folder = {s.folder: s.to_dict() for s in suggestions}
    auto = by_folder["Wohnung/Strom"]
    assert auto["rule_auto"] is True and auto["rule_name"] == "Stadtwerke Strom"
    assert auto["signals"]["rule"] == 1.0
    assert auto["title_source"] == "rule" and auto["title"] == "Abschlag Februar 2025"
    assert auto["date_source"] == "analysis" and auto["doc_date"] == "2025-02-03"
    assert auto["tags"] == ["Strom"]
    plain = by_folder["Wohnung/Allgemein"]
    assert plain["rule_auto"] is False and plain["rule_name"] == "Alles von den Stadtwerken"
    assert plain["title_source"] is None  # no title template: the composed title
    # item.json gets the extra keys; reading a suggestion back ignores them.
    json.dumps(list(by_folder.values()))
    assert Suggestion.from_dict(auto).rule_name == "Stadtwerke Strom"


def test_auto_flag_survives_merging_with_another_rule(engine, svc):
    rules = AUTO_RULES.replace("folder: Wohnung/Allgemein", "folder: Wohnung/Strom").replace(
        "  - name: Stadtwerke Strom", "  - name: Erste Regel\n    match: {keywords_any: [Abschlag]}\n"
        "    action: {folder: Wohnung/Strom}\n  - name: Stadtwerke Strom", 1)
    svc.rules.save_raw_text(rules)
    top = suggest(engine, STADTWERKE)[0]
    assert top.folder == "Wohnung/Strom" and top.rule_auto is True
    assert top.rule_name == "Stadtwerke Strom"  # the rule marked for automatic filing names it


def test_merged_suggestion_keeps_the_score_of_every_source(engine, svc):
    add_doc(svc.index, "sw1", "Wohnung/Strom/2024-02-03 Abschlag Februar 2024.pdf", STADTWERKE.replace("2025", "2024"),
            sender="Stadtwerke Leipzig GmbH")
    svc.rules.save_raw_text(RULES)
    top = suggest(engine, STADTWERKE)[0]
    assert top.source == "rule" and top.score == 1.0
    assert top.signals["rule"] == 1.0 and 0.3 < top.signals["similarity"] <= 0.94
    assert top.rule_auto is False


def test_title_and_date_sources(engine, svc):
    for n, day in enumerate(["31.01.2025", "28.02.2025"], start=1):
        add_doc(svc.index, f"k{n}", f"Finanzen/Konten/2025-0{n}-{day[:2]} Kontoauszug {['Januar', 'Februar'][n - 1]}.pdf",
                KONTOAUSZUG.format(n=n, day=day), sender="Sparkasse Dresden")
    top = suggest(engine, KONTOAUSZUG.format(n=3, day="31.03.2025"))[0]
    assert top.title == "Kontoauszug März" and top.title_source == "similarity"
    assert top.date_source == "analysis" and top.signals["similarity"] > 0.5

    fallback = suggest(engine, "", make_item(source="smb"))[0]
    assert fallback.title_source is None and fallback.date_source == "received"

    item = make_item(source="mail", files=("original/document.pdf",),
                     context={"mail_subject": "WG: Bauantrag Garage", "mail_date": "Tue, 04 Mar 2025 10:15:00 +0100"})
    assert suggest(engine, "", item)[0].date_source == "mail"


def test_import_suggestion_marks_its_title_and_date(engine, svc):
    svc.index.add_folder_with_parents("Rechnungen/Alza")
    imported = Suggestion(folder="Rechnungen/Alza", title="Monitor 27 Zoll", doc_date="2024-11-05", sender="Alza")
    item = make_item(source="paperless", context={"import_suggestion": imported.to_dict()})
    top = suggest(engine, "Alza.cz a.s.\nRechnung\nDatum 05.11.2024\nMonitor", item)[0]
    assert top.source == "import" and top.signals["import"] == 0.95
    assert top.title_source == "import" and top.date_source == "import"


def test_llm_request_collects_what_the_prompt_needs(engine, svc):
    days = ["31.01.2025", "28.02.2025", "31.03.2025", "30.04.2025"]
    names = ["Januar", "Februar", "März", "April"]
    for n, (day, name) in enumerate(zip(days, names), start=1):
        add_doc(svc.index, f"k{n}", f"Finanzen/Konten/2025-0{n}-{day[:2]} Kontoauszug {name}.pdf",
                KONTOAUSZUG.format(n=n, day=day), sender="Sparkasse Dresden")
    svc.index.add_folder_with_parents("Versicherungen/Allianz")
    text = KONTOAUSZUG.format(n=5, day="31.05.2025")
    item = make_item(files=("original/auszug.pdf",), context={"mail_subject": "Ihr Kontoauszug", "other": "x"})
    analysis = analyze([text], item.context, known_senders=engine.known_senders(), today=TODAY)

    request = engine.llm_request(item, [text], analysis)
    assert request.text == text and request.analysis is analysis
    assert request.folders == ["Finanzen", "Finanzen/Konten", "Versicherungen", "Versicherungen/Allianz"]
    assert request.counts == {"Finanzen/Konten": 4}
    assert request.candidates[0] == "Finanzen/Konten"
    assert request.examples["Finanzen/Konten"] == [
        "2025-04-30 Kontoauszug April.pdf", "2025-03-31 Kontoauszug März.pdf", "2025-02-28 Kontoauszug Februar.pdf"]
    assert request.similar and request.similar[0].folder == "Finanzen/Konten"
    assert request.similar[0].name.startswith("2025-0") and 0 < request.similar[0].score <= 1
    assert request.context == {"mail_subject": "Ihr Kontoauszug", "original_name": "auszug.pdf"}


def test_candidate_folders_prefer_the_stored_suggestions(engine, svc):
    for folder in ("A/B", "C", "D"):
        svc.index.add_folder_with_parents(folder)
    item = make_item()
    item.suggestions = [
        {"folder": "a/b", "source": "rule"},
        {"folder": "Neu/Ordner", "source": "heuristic", "new_folder": True},
        {"folder": "D", "source": "llm"},
        {"folder": "C", "source": "similarity"},
    ]
    assert engine.candidate_folders(item, ["Text"], Analysis(), neighbours=[]) == ["A/B", "C"]


@respx.mock
def test_llm_suggestion_carries_confidence_and_sources(engine, svc):
    llm_settings(svc)
    svc.index.add_folder_with_parents("Versicherungen/Haftpflicht")
    content = {"dokumenttyp": "Versicherungsschein", "absender": "Allianz", "datum": "2024-12-05",
               "titel": "Versicherungsschein Allianz Haftpflicht", "begruendung": "Police der Haftpflicht.",
               "ordner": "versicherungen/haftpflicht", "neuer_ordner": False, "sicherheit": 0.9, "tags": ["Police"]}
    route = respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": json.dumps(content)}})
    )
    suggestion = engine.llm_suggest(make_item(), [POLICY], analyze([POLICY], {}, today=TODAY))
    data = suggestion.to_dict()
    assert data["folder"] == "Versicherungen/Haftpflicht" and data["source"] == "llm"
    assert data["confidence"] == 0.9 and data["score"] == pytest.approx(0.81)
    assert data["signals"] == {"llm": pytest.approx(0.81)} and data["rule_auto"] is False
    assert data["title_source"] == "llm" and data["date_source"] == "llm"
    assert data["reasons"] == ["Police der Haftpflicht."]
    json.dumps(data)
    sent = json.loads(route.calls.last.request.content)
    assert sent["options"]["num_ctx"] == 8192
    assert "- Versicherungen/Haftpflicht (0)" in sent["messages"][1]["content"]


@respx.mock
def test_llm_failure_reason_goes_to_the_item_log(engine, svc):
    llm_settings(svc)
    assert engine.llm_status()["reachable"] is None  # nothing asked yet
    respx.post(f"{OLLAMA}/api/chat").mock(side_effect=httpx.ConnectError("refused"))
    lines: list[str] = []
    assert engine.llm_suggest(make_item(), [POLICY], Analysis(), log=lines.append) is None
    assert lines == ["KI: Keine Verbindung zum Sprachmodell unter ollama.test:11434."]
    status = engine.llm_status()
    assert status["reachable"] is False and status["ok"] is False and status["error"] == lines[0][4:]
    assert status["provider"] == "ollama" and status["model"] == "qwen2.5:3b"
    assert status["endpoint"] == f"{OLLAMA}/api/chat" and status["host"] == "ollama.test:11434"


@respx.mock
def test_llm_probe_through_the_engine(engine, svc):
    assert engine.llm_probe() is None and engine.llm_status() is None
    llm_settings(svc, provider="openai", url="http://llama.test:8080/v1", api_key="k")
    respx.post("http://llama.test:8080/v1/chat/completions").mock(return_value=httpx.Response(200, json={
        "choices": [{"message": {"content": json.dumps({"ordner": "Wohnung/Strom", "titel": "Abschlag"})}}]}))
    outcome = engine.llm_probe()
    assert outcome["ok"] is True and outcome["provider"] == "openai"
    assert engine.llm_status()["reachable"] is True
