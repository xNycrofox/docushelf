"""Automatic filing decisions on suggestions computed by the real suggestion engine."""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
import respx

from docushelf.analysis.extract import analyze
from docushelf.config import AutoFileConfig, LlmConfig
from docushelf.inbox.items import InboxItem
from docushelf.models import DocumentRecord
from docushelf.suggest.auto import decide
from docushelf.suggest.engine import SuggestionEngine
from docushelf.suggest.llm import LlmClient
from docushelf.suggest.rules import RuleStore
from docushelf.util.names import parse_filename

TODAY = date(2025, 11, 20)
OLLAMA = "http://ollama.test:11434"

STATEMENT = """Sparkasse Dresden
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

UTILITY = """Stadtwerke Leipzig GmbH
Stadtwerke Leipzig GmbH · Postfach 10 06 26 · 04006 Leipzig
Herrn
Max Mustermann
Musterweg 5
04229 Leipzig
Abschlagsrechnung Strom
Rechnungsdatum: 03.02.2025
Kundennummer: 4711 0815
Monatlicher Abschlag ab 01.03.2025: 85,00 €"""

AUTO_RULE = """\
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
"""


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


def add_doc(index, doc_id: str, path: str, text: str, *, sender: str | None = None) -> None:
    folder, filename = path.rsplit("/", 1)
    parsed = parse_filename(filename)
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


def ready_item(engine: SuggestionEngine, text: str, *, source: str = "smb") -> tuple[InboxItem, list[str]]:
    item = InboxItem(id="20251120-100000-abcdef", source=source, received_at="2025-11-20T10:00:00+01:00",
                     original_files=["original/scan_0001.pdf"], origin_hash="0" * 64, stage="ready")
    item.state = "bereit"
    analysis = analyze([text], item.context, known_senders=engine.known_senders(), today=TODAY)
    item.analysis = analysis.to_dict()
    # Stored like the pipeline stores them: JSON in item.json.
    item.suggestions = json.loads(json.dumps([s.to_dict() for s in engine.suggest(item, [text], analysis)]))
    return item, [text]


def folder_exists(svc):
    return lambda folder: svc.index.folder_exists(folder)


def settings(svc, **changes):
    return svc.settings.with_overrides(auto_file=AutoFileConfig(**changes))


def test_documents_like_earlier_ones_are_filed_into_their_folder(engine, svc):
    for n, day in enumerate(["31.01.2025", "28.02.2025", "31.03.2025"], start=1):
        month = ["Januar", "Februar", "März"][n - 1]
        add_doc(svc.index, f"k{n}", f"Finanzen/Konten/2025-0{n}-{day[:2]} Kontoauszug {month}.pdf",
                STATEMENT.format(n=n, day=day), sender="Sparkasse Dresden")
    item, _pages = ready_item(engine, STATEMENT.format(n=4, day="30.04.2025"))
    decision = decide(item, settings=settings(svc, mode="confident"), folder_exists=folder_exists(svc))
    assert decision.action == "file", decision.reasons
    assert decision.request.folder == "Finanzen/Konten"
    assert decision.request.title == "Kontoauszug April"  # the neighbour's title, moved to the new month
    assert decision.request.doc_date == date(2025, 4, 30)
    assert decision.request.sender == "Sparkasse Dresden"
    assert decision.sources[0] == "similarity" and decision.confidence >= 0.75


def test_unknown_documents_stay_in_the_inbox(engine, svc):
    add_doc(svc.index, "k1", "Finanzen/Konten/2025-01-31 Kontoauszug Januar.pdf",
            STATEMENT.format(n=1, day="31.01.2025"), sender="Sparkasse Dresden")
    item, _pages = ready_item(engine, UTILITY)
    decision = decide(item, settings=settings(svc, mode="confident"), folder_exists=folder_exists(svc))
    assert decision.action == "hold" and decision.reasons[0].startswith("unsicher (")
    # Mode "always" files it all the same, marked for review.
    decision = decide(item, settings=settings(svc, mode="always"), folder_exists=folder_exists(svc))
    assert decision.action == "file" and decision.review is True


def test_rule_marked_auto_files_with_its_title_and_tags(engine, svc):
    svc.index.add_folder_with_parents("Wohnung/Strom")
    svc.rules.save_raw_text(AUTO_RULE)
    item, _pages = ready_item(engine, UTILITY)
    assert item.suggestions[0]["rule_auto"] is True
    decision = decide(item, settings=settings(svc, mode="rules"), folder_exists=folder_exists(svc))
    assert decision.action == "file" and decision.confidence == 1.0
    assert decision.request.folder == "Wohnung/Strom" and decision.request.title == "Abschlag Februar 2025"
    assert decision.request.tags == ["Strom"] and decision.request.doc_date == date(2025, 2, 3)
    assert decision.reasons[0] == "automatische Regel „Stadtwerke Strom“"
    # Without "auto" the same rule is not enough in mode "rules", but in mode "confident".
    svc.rules.save_raw_text(AUTO_RULE.replace("auto: true", "auto: false"))
    item, _pages = ready_item(engine, UTILITY)
    assert decide(item, settings=settings(svc, mode="rules"), folder_exists=folder_exists(svc)).action == "hold"
    decision = decide(item, settings=settings(svc, mode="confident"), folder_exists=folder_exists(svc))
    assert decision.action == "file" and decision.request.tags == ["Strom"]


@respx.mock
def test_language_model_and_similarity_agree(engine, svc):
    add_doc(svc.index, "k1", "Finanzen/Konten/2025-01-31 Kontoauszug Januar.pdf",
            STATEMENT.format(n=1, day="31.01.2025"), sender="Sparkasse Dresden")
    add_doc(svc.index, "x1", "Finanzen/Steuern/2025-01-10 Steuerbescheid 2023.pdf",
            "Finanzamt Dresden Bescheid über Einkommensteuer 2023 Girokonto IBAN", sender="Finanzamt Dresden")
    svc.settings = svc.settings.with_overrides(llm=LlmConfig(url=OLLAMA, model="qwen2.5:3b", timeout=5))
    text = STATEMENT.format(n=2, day="28.02.2025").replace("REWE Markt", "Bäckerei Schmidt")
    item, pages = ready_item(engine, text)
    content = {"dokumenttyp": "Kontoauszug", "absender": "Sparkasse Dresden", "datum": "2025-02-28",
               "titel": "Kontoauszug Sparkasse Februar 2025", "begruendung": "Kontoauszug des Girokontos.",
               "ordner": "Finanzen/Konten", "neuer_ordner": False, "sicherheit": 0.85, "tags": ["Bank"]}
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": json.dumps(content)}}))
    llm = engine.llm_suggest(item, pages, item.analysis)
    item.suggestions.append(json.loads(json.dumps(llm.to_dict())))

    decision = decide(item, settings=settings(svc, mode="confident"), folder_exists=folder_exists(svc))
    assert decision.action == "file" and decision.request.folder == "Finanzen/Konten"
    assert decision.sources[:2] == ["similarity", "llm"]
    assert decision.request.title == "Kontoauszug Sparkasse Februar 2025"  # the model's title beats the neighbour's
    assert decision.request.tags == []  # never the model's tags
    assert "KI: Kontoauszug des Girokontos." in decision.reasons
