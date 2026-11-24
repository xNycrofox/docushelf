"""Tests for the automatic filing decision (docushelf.suggest.auto)."""

from __future__ import annotations

import copy
import json
from datetime import date
from types import SimpleNamespace

import pytest

from docushelf.config import AutoFileConfig
from docushelf.inbox.items import InboxItem
from docushelf.suggest.auto import (
    AutoDecision,
    confidence_percent,
    decide,
    describe_sources,
    source_allowed,
)

ANALYSIS = {"doc_date": "2025-02-03", "sender": "Stadtwerke Leipzig GmbH", "doc_type": "Rechnung"}


def settings(**changes) -> SimpleNamespace:
    values = {"mode": "confident"}
    values.update(changes)
    return SimpleNamespace(auto_file=AutoFileConfig(**values))


def sug(folder: str | None, source: str = "similarity", score: float = 0.8, **extra) -> dict:
    """A suggestion dict as the engine stores it in item.json."""
    data = {
        "folder": folder,
        "title": f"Titel aus {source}",
        "doc_date": "2025-02-03",
        "tags": [],
        "sender": "Stadtwerke Leipzig GmbH",
        "score": score,
        "source": source,
        "reasons": [f"Grund {source}"],
        "new_folder": False,
        "rule_name": None,
        "rule_auto": False,
        "signals": {source: score} if folder else {},
        "title_source": None if source == "heuristic" else source,
        "date_source": "llm" if source == "llm" else "analysis",
    }
    data.update(extra)
    return data


def make_item(*suggestions: dict, source: str = "smb", state: str = "bereit", analysis: dict | None = None,
              context: dict | None = None, options: dict | None = None, duplicate_of: dict | None = None,
              received_at: str = "2025-11-20T10:00:00+01:00") -> InboxItem:
    item = InboxItem(
        id="20251120-100000-abcdef",
        source=source,
        received_at=received_at,
        original_files=["original/scan.pdf"],
        origin_hash="0" * 64,
        stage="ready",
        context=dict(context or {}),
        options=dict(options or {}),
        analysis=dict(ANALYSIS if analysis is None else analysis),
        suggestions=list(suggestions),
        duplicate_of=duplicate_of,
    )
    item.state = state
    return item


def exists(*folders: str):
    known = {folder.casefold() for folder in folders}
    return lambda folder: folder.casefold() in known


ALL = exists("Wohnung/Strom", "Wohnung/Gas", "Finanzen/Bank", "Versicherungen/Haftpflicht")


# ---------------------------------------------------------------------------
# Helpers


@pytest.mark.parametrize(
    "source,sources,allowed",
    [
        ("smb", AutoFileConfig().sources, True),
        ("upload", AutoFileConfig().sources, True),
        ("watch-scanner", AutoFileConfig().sources, True),
        ("paperless", AutoFileConfig().sources, False),
        ("unbekannt", AutoFileConfig().sources, False),
        ("paperless", ("*",), False),
        ("unbekannt", ("*",), False),
        ("paperless", ("paperless",), True),
        (None, ("unbekannt",), True),
        ("", ("*",), False),
        ("Upload", ("upload",), True),
        ("mail", ("watch-*",), False),
        ("watch-büro", ("watch*",), True),
        ("smtp", ("*",), True),
        ("smtp", (), False),
    ],
)
def test_source_allowed(source, sources, allowed):
    assert source_allowed(source, sources) is allowed


def test_describe_sources():
    assert describe_sources(["rule", "import", "similarity", "llm", "heuristic", "llm", "neu"]) == [
        "Regel", "Paperless", "Ähnlichkeit", "KI", "Heuristik", "neu"]
    assert describe_sources(None) == []


@pytest.mark.parametrize("value,percent", [(0.63, 63), (0.7499, 74), (0.75, 75), (1, 100), (0.57, 57), (None, 0),
                                           ("x", 0), (1.5, 100)])
def test_confidence_percent(value, percent):
    assert confidence_percent(value) == percent


# ---------------------------------------------------------------------------
# 1. Never automatically


@pytest.mark.parametrize(
    "item_args,config,reason",
    [
        ({}, {"mode": "off"}, "automatische Ablage ist ausgeschaltet"),
        ({"source": "paperless"}, {}, "Eingang „paperless“ ist für die automatische Ablage nicht freigegeben"),
        ({"source": "unbekannt"}, {"sources": ("*",)}, "Eingang „unbekannt“"),
        ({"source": "mail"}, {"sources": ("smb", "watch-*")}, "Eingang „mail“"),
        ({"state": "fehler"}, {}, "Dokument ist nicht bereit"),
        ({"state": "erledigt"}, {}, "Dokument ist nicht bereit"),
        ({"duplicate_of": {"kind": "archive", "id": "x", "path": "A/b.pdf"}}, {}, "mögliches Duplikat"),
        ({"options": {"no_auto": True}}, {}, "von Hand bearbeitet"),
    ],
)
def test_reasons_never_to_file_automatically(item_args, config, reason):
    item = make_item(sug("Wohnung/Strom", "rule", 1.0, rule_auto=True), **item_args)
    calls: list[str] = []
    decision = decide(item, settings=settings(**config), folder_exists=lambda f: calls.append(f) or True)
    assert decision.action == "hold" and decision.request is None and decision.review is False
    assert decision.reasons[0].startswith(reason)
    assert calls == []  # decided before looking at the archive


def test_no_suggestion_is_held():
    decision = decide(make_item(), settings=settings(mode="always"), folder_exists=ALL)
    assert decision.action == "hold" and decision.reasons == ["kein Ablagevorschlag"]
    only_title = make_item(sug(None, "llm", 0.0))
    decision = decide(only_title, settings=settings(mode="always"), folder_exists=ALL)
    assert decision.action == "hold" and decision.reasons == ["kein Ordnervorschlag"]


def test_checks_come_in_the_documented_order():
    item = make_item(duplicate_of={"kind": "inbox", "id": "y"}, options={"no_auto": True}, source="paperless")
    assert decide(item, settings=settings(mode="off"), folder_exists=ALL).reasons[0].startswith("automatische")
    assert decide(item, settings=settings(), folder_exists=ALL).reasons[0].startswith("Eingang")
    item.source = "smb"
    assert decide(item, settings=settings(), folder_exists=ALL).reasons[0] == "mögliches Duplikat"
    item.duplicate_of = None
    assert decide(item, settings=settings(), folder_exists=ALL).reasons[0] == "kein Ablagevorschlag"


def test_settings_without_auto_file_mean_off():
    item = make_item(sug("Wohnung/Strom", "rule", 1.0, rule_auto=True))
    decision = decide(item, settings=SimpleNamespace(), folder_exists=ALL)
    assert decision.action == "hold" and decision.reasons == ["automatische Ablage ist ausgeschaltet"]


# ---------------------------------------------------------------------------
# 2./3. Rules marked "auto"


def auto_rule(**extra) -> dict:
    values = dict(
        title="Abschlag Februar 2025",
        title_source="rule",
        tags=["Strom", "steuerrelevant 2025"],
        rule_name="Stadtwerke Strom",
        rule_auto=True,
        reasons=["Regel „Stadtwerke Strom“", "Absender: Stadtwerke Leipzig", "Stichwort „Abschlag“"],
        signals={"rule": 1.0, "similarity": 0.4},
    )
    values.update(extra)
    return sug("Wohnung/Strom", "rule", 1.0, **values)


@pytest.mark.parametrize("mode", ["rules", "confident", "always"])
def test_rule_marked_auto_files_in_every_mode(mode):
    item = make_item(sug("Finanzen/Bank", "similarity", 0.9), auto_rule(),
                     sug("Wohnung/Strom", "llm", 0.3, tags=["KI-Tag"]))
    decision = decide(item, settings=settings(mode=mode), folder_exists=ALL)
    assert decision.action == "file" and decision.confidence == 1.0 and decision.review is False
    request = decision.request
    assert request.folder == "Wohnung/Strom" and request.title == "Abschlag Februar 2025"
    assert request.doc_date == date(2025, 2, 3)
    assert request.tags == ["Strom", "steuerrelevant 2025"]
    assert request.sender == "Stadtwerke Leipzig GmbH" and request.create_rule is False
    assert decision.sources == ["rule", "similarity", "llm"]
    assert decision.reasons == ["automatische Regel „Stadtwerke Strom“", "Absender: Stadtwerke Leipzig",
                                "Stichwort „Abschlag“"]


def test_rule_marked_auto_for_a_missing_folder():
    item = make_item(auto_rule())
    decision = decide(item, settings=settings(mode="rules"), folder_exists=exists())
    assert decision.action == "file" and decision.confidence == 1.0
    assert decision.reasons[-1] == "neuer Ordner „Wohnung/Strom“"
    decision = decide(item, settings=settings(mode="rules", new_folders=False), folder_exists=exists())
    assert decision.action == "hold" and decision.reasons[0] == "Ordner „Wohnung/Strom“ gibt es noch nicht"


def test_rule_marked_auto_without_date_uses_the_received_date():
    item = make_item(auto_rule(doc_date=None), analysis={})
    decision = decide(item, settings=settings(mode="rules"), folder_exists=ALL)
    assert decision.request.doc_date == date(2025, 11, 20) and decision.confidence == 1.0


def test_rule_marked_auto_without_usable_title_is_held():
    item = make_item(auto_rule(title="???"))
    decision = decide(item, settings=settings(mode="rules"), folder_exists=ALL)
    assert decision.action == "hold" and decision.reasons[0] == "kein brauchbarer Titel"


def test_mode_rules_holds_everything_else():
    item = make_item(sug("Wohnung/Strom", "rule", 1.0, rule_name="Strom"), sug("Wohnung/Strom", "llm", 0.9))
    decision = decide(item, settings=settings(mode="rules"), folder_exists=ALL)
    assert decision.action == "hold" and decision.reasons == ["keine automatische Regel passt"]
    assert decision.confidence == 0.0


# ---------------------------------------------------------------------------
# 4. Combining the sources per folder


def test_sources_for_the_same_folder_are_combined():
    item = make_item(
        sug("Wohnung/Strom", "similarity", 0.6, signals={"similarity": 0.6, "heuristic": 0.4}),
        sug("wohnung/strom", "llm", 0.5, new_folder=True),
    )
    decision = decide(item, settings=settings(), folder_exists=ALL)
    # 1 - (1 - 0.6) * (1 - 0.9 * 0.5) * (1 - 0.5 * 0.4)
    assert decision.confidence == pytest.approx(0.824)
    assert decision.action == "file" and decision.request.folder == "Wohnung/Strom"
    assert decision.sources == ["similarity", "llm", "heuristic"]
    assert decision.reasons == ["Grund similarity", "KI: Grund llm"]


def test_agreement_lifts_a_decision_over_the_threshold():
    alone = make_item(sug("Wohnung/Strom", "similarity", 0.6))
    decision = decide(alone, settings=settings(), folder_exists=ALL)
    assert decision.action == "hold" and decision.confidence == pytest.approx(0.6)
    assert decision.reasons[0] == "unsicher (60 %)"
    agreed = make_item(sug("Wohnung/Strom", "similarity", 0.6), sug("Wohnung/Strom", "llm", 0.6))
    decision = decide(agreed, settings=settings(), folder_exists=ALL)
    # 1 - (1 - 0.6) * (1 - 0.9 * 0.6)
    assert decision.action == "file" and decision.confidence == pytest.approx(0.816)


@pytest.mark.parametrize(
    "source,score,expected",
    [("rule", 0.9, 0.9), ("import", 0.95, 0.95), ("similarity", 0.8, 0.8), ("llm", 0.8, 0.72), ("heuristic", 0.6, 0.3)],
)
def test_source_weights(source, score, expected):
    decision = decide(make_item(sug("Wohnung/Strom", source, score)), settings=settings(mode="always"),
                      folder_exists=ALL)
    assert decision.confidence == pytest.approx(expected)


def test_old_suggestions_without_signals_still_count():
    legacy = {"folder": "Wohnung/Strom", "title": "Stromrechnung", "doc_date": "2025-02-03", "score": 0.85,
              "source": "similarity", "reasons": ["ähnlich"]}
    decision = decide(make_item(legacy), settings=settings(), folder_exists=ALL)
    assert decision.action == "file" and decision.confidence == pytest.approx(0.85)
    assert decision.request.title == "Stromrechnung"


def test_decide_does_not_change_the_item():
    item = make_item(sug("Wohnung/Strom", "similarity", 0.9), sug("Wohnung/Strom", "llm", 0.7))
    before = copy.deepcopy(item.suggestions)
    decide(item, settings=settings(), folder_exists=ALL)
    assert item.suggestions == before


# ---------------------------------------------------------------------------
# 5. Two folders that fit


def test_runner_up_close_to_the_best_costs_confidence():
    item = make_item(sug("Wohnung/Strom", "similarity", 0.9), sug("Wohnung/Gas", "similarity", 0.85))
    decision = decide(item, settings=settings(), folder_exists=ALL)
    assert decision.confidence == pytest.approx(0.72)
    assert decision.action == "hold" and decision.reasons[0] == "unsicher (72 %)"
    assert "zwei Ordner kommen in Frage" in decision.reasons


@pytest.mark.parametrize("runner_up,ambiguous", [(0.8, True), (0.79, False), (0.5, False)])
def test_ambiguity_margin(runner_up, ambiguous):
    item = make_item(sug("Wohnung/Strom", "similarity", 0.9), sug("Wohnung/Gas", "similarity", runner_up))
    decision = decide(item, settings=settings(mode="always"), folder_exists=ALL)
    assert decision.confidence == pytest.approx(0.72 if ambiguous else 0.9)
    assert ("zwei Ordner kommen in Frage" in decision.reasons) is ambiguous


# ---------------------------------------------------------------------------
# 6. New folders


def test_new_folder_is_held_when_new_folders_are_off():
    item = make_item(sug("Versicherungen/HUK", "llm", 0.95, new_folder=True))
    decision = decide(item, settings=settings(new_folders=False), folder_exists=ALL)
    assert decision.action == "hold" and decision.reasons[0] == "Ordner „Versicherungen/HUK“ gibt es noch nicht"
    assert decision.confidence == pytest.approx(0.855) and decision.sources == ["llm"]


def test_new_folder_costs_confidence_unless_a_rule_or_the_import_named_it():
    guessed = make_item(sug("Versicherungen/HUK", "similarity", 0.9, new_folder=True))
    decision = decide(guessed, settings=settings(), folder_exists=ALL)
    assert decision.action == "file" and decision.confidence == pytest.approx(0.81)
    assert "neuer Ordner „Versicherungen/HUK“" in decision.reasons
    for source, score in (("rule", 1.0), ("import", 0.95)):
        named = make_item(sug("Versicherungen/HUK", source, score, new_folder=True))
        decision = decide(named, settings=settings(), folder_exists=ALL)
        assert decision.action == "file" and decision.confidence == pytest.approx(score)


def test_existence_is_checked_now_not_when_suggested():
    # Suggested as new, created in the meantime: no penalty.
    item = make_item(sug("Wohnung/Gas", "similarity", 0.9, new_folder=True))
    assert decide(item, settings=settings(), folder_exists=ALL).confidence == pytest.approx(0.9)


def test_failing_folder_check_holds():
    def broken(folder):
        raise OSError("NAS weg")

    decision = decide(make_item(sug("Wohnung/Strom", "similarity", 0.9)), settings=settings(), folder_exists=broken)
    assert decision.action == "hold" and decision.reasons[0] == "Ordner konnte nicht geprüft werden"


# ---------------------------------------------------------------------------
# 7. Title, date, sender, tags


def test_title_priority_rule_llm_similarity_composed():
    rule = sug("Wohnung/Strom", "rule", 1.0, title="Regeltitel", title_source="rule")
    llm = sug("Wohnung/Strom", "llm", 0.8, title="KI-Titel")
    similar = sug("Wohnung/Strom", "similarity", 0.8, title="Ähnlicher Titel")
    composed = sug("Wohnung/Strom", "heuristic", 0.5, title="Rechnung Stadtwerke")
    run = lambda *s: decide(make_item(*s), settings=settings(mode="always"), folder_exists=ALL).request.title  # noqa: E731
    assert run(composed, similar, llm, rule) == "Regeltitel"
    assert run(composed, similar, llm) == "KI-Titel"
    assert run(composed, similar) == "Ähnlicher Titel"
    assert run(composed) == "Rechnung Stadtwerke"
    # A rule without title template has the composed title: the model's title wins.
    plain_rule = sug("Wohnung/Strom", "rule", 1.0, title="Rechnung Stadtwerke", title_source=None)
    assert run(plain_rule, llm) == "KI-Titel"


def test_llm_title_counts_even_for_another_folder():
    similar = sug("Wohnung/Strom", "similarity", 0.95, title="Rechnung Stadtwerke", title_source=None)
    llm = sug("Wohnung/Gas", "llm", 0.3, title="Abschlagsrechnung Stadtwerke März 2025")
    decision = decide(make_item(similar, llm), settings=settings(), folder_exists=ALL)
    assert decision.request.folder == "Wohnung/Strom"
    assert decision.request.title == "Abschlagsrechnung Stadtwerke März 2025"
    titled = sug(None, "llm", 0.0, title="Nur ein Titel")
    decision = decide(make_item(similar, titled), settings=settings(), folder_exists=ALL)
    assert decision.request.title == "Nur ein Titel"


def test_unusable_title_holds():
    item = make_item(sug("Wohnung/Strom", "similarity", 0.95, title="  ?? "))
    decision = decide(item, settings=settings(mode="always"), folder_exists=ALL)
    assert decision.action == "hold" and decision.reasons[0] == "kein brauchbarer Titel"


def test_long_titles_are_shortened():
    item = make_item(sug("Wohnung/Strom", "similarity", 0.95, title="Sehr langer Titel " * 20))
    title = decide(item, settings=settings(), folder_exists=ALL).request.title
    assert len(title) <= 150 and not title.endswith(" ")


def test_date_agreeing_with_the_analysis():
    item = make_item(sug("Wohnung/Strom", "similarity", 0.9, doc_date="2025-02-03"))
    decision = decide(item, settings=settings(), folder_exists=ALL)
    assert decision.request.doc_date == date(2025, 2, 3) and decision.confidence == pytest.approx(0.9)


def test_checked_llm_date_wins_over_the_analysis():
    item = make_item(sug("Wohnung/Strom", "similarity", 0.9, doc_date="2025-02-03"),
                     sug("Wohnung/Strom", "llm", 0.8, doc_date="2025-02-01", date_source="llm"))
    assert decide(item, settings=settings(), folder_exists=ALL).request.doc_date == date(2025, 2, 1)
    # A rule's date (the analysis date) comes first.
    item.suggestions.insert(0, sug("Wohnung/Strom", "rule", 1.0, doc_date="2025-02-03"))
    assert decide(item, settings=settings(), folder_exists=ALL).request.doc_date == date(2025, 2, 3)
    # An unchecked date of the model (filled in from elsewhere) does not count.
    item = make_item(sug("Wohnung/Strom", "llm", 0.9, doc_date="2025-01-15", date_source="received"))
    assert decide(item, settings=settings(), folder_exists=ALL).request.doc_date == date(2025, 2, 3)


def test_import_date_is_kept():
    item = make_item(sug("Rechnungen/Alza", "import", 0.95, doc_date="2024-11-05", date_source="import"),
                     source="paperless")
    decision = decide(item, settings=settings(sources=("paperless",)), folder_exists=exists("Rechnungen/Alza"))
    assert decision.action == "file" and decision.request.doc_date == date(2024, 11, 5)


def test_analysis_date_when_the_suggestion_date_is_not_trusted():
    item = make_item(sug("Wohnung/Strom", "similarity", 0.9, doc_date="2025-11-20", date_source="received"),
                     analysis={"doc_date": "2025-02-03"})
    decision = decide(item, settings=settings(), folder_exists=ALL)
    assert decision.request.doc_date == date(2025, 2, 3) and decision.confidence == pytest.approx(0.9)


def test_received_date_costs_confidence():
    item = make_item(sug("Wohnung/Strom", "similarity", 0.9, doc_date="2025-11-20", date_source="received"),
                     analysis={})
    decision = decide(item, settings=settings(), folder_exists=ALL)
    assert decision.request.doc_date == date(2025, 11, 20)
    assert decision.confidence == pytest.approx(0.765) and decision.action == "file"
    assert "kein Datum erkannt – Eingangsdatum verwendet" in decision.reasons


def test_received_date_of_a_mail_is_the_mail_date():
    item = make_item(sug("Wohnung/Strom", "similarity", 0.95, doc_date=None), analysis={}, source="mail",
                     context={"mail_date": "Tue, 04 Mar 2025 10:15:00 +0100"})
    assert decide(item, settings=settings(), folder_exists=ALL).request.doc_date == date(2025, 3, 4)


def test_sender_from_import_analysis_or_model():
    llm = sug("Wohnung/Strom", "llm", 0.9, sender="Stadtwerke (KI)")
    similar = sug("Wohnung/Strom", "similarity", 0.9, sender="egal")
    assert decide(make_item(similar, llm), settings=settings(), folder_exists=ALL).request.sender == (
        "Stadtwerke Leipzig GmbH")
    no_sender = make_item(similar, llm, analysis={"doc_date": "2025-02-03"})
    assert decide(no_sender, settings=settings(), folder_exists=ALL).request.sender == "Stadtwerke (KI)"
    imported = make_item(sug("Wohnung/Strom", "import", 0.95, sender="Stadtwerke Leipzig"), llm, source="paperless")
    decision = decide(imported, settings=settings(sources=("paperless",)), folder_exists=ALL)
    assert decision.request.sender == "Stadtwerke Leipzig"
    nobody = make_item(similar, analysis={"doc_date": "2025-02-03"})
    assert decide(nobody, settings=settings(), folder_exists=ALL).request.sender is None


def test_only_rule_tags_are_used():
    rule = sug("Wohnung/Strom", "rule", 1.0, tags=["Strom", "steuerrelevant 2025"],
               signals={"rule": 1.0, "similarity": 0.7})
    llm = sug("Wohnung/Strom", "llm", 0.9, tags=["KI-Tag"])
    similar = sug("Wohnung/Strom", "similarity", 0.9, tags=["Irgendwas"])
    assert decide(make_item(rule, llm), settings=settings(), folder_exists=ALL).request.tags == [
        "Strom", "steuerrelevant 2025"]
    assert decide(make_item(similar, llm), settings=settings(), folder_exists=ALL).request.tags == []


# ---------------------------------------------------------------------------
# 8. Threshold and modes


def test_confident_mode_files_at_the_threshold_and_holds_below():
    at = make_item(sug("Wohnung/Strom", "similarity", 0.75))
    decision = decide(at, settings=settings(), folder_exists=ALL)
    assert decision.action == "file" and decision.review is False
    below = make_item(sug("Wohnung/Strom", "similarity", 0.749))
    decision = decide(below, settings=settings(), folder_exists=ALL)
    assert decision.action == "hold" and decision.reasons[0] == "unsicher (74 %)"
    assert decision.request is None and decision.confidence == 0.749
    # The confidence is rounded to three places before it is compared.
    assert decide(make_item(sug("Wohnung/Strom", "similarity", 0.7499)), settings=settings(),
                  folder_exists=ALL).confidence == 0.75


def test_custom_threshold():
    item = make_item(sug("Wohnung/Strom", "similarity", 0.6))
    assert decide(item, settings=settings(threshold=0.5), folder_exists=ALL).action == "file"
    assert decide(item, settings=settings(threshold=0.9), folder_exists=ALL).action == "hold"


def test_always_mode_files_and_marks_uncertain_decisions_for_review():
    unsure = make_item(sug("Wohnung/Strom", "similarity", 0.63))
    decision = decide(unsure, settings=settings(mode="always"), folder_exists=ALL)
    assert decision.action == "file" and decision.review is True
    assert decision.request.folder == "Wohnung/Strom"
    assert decision.reasons[0] == "unsicher (63 %)"
    sure = make_item(sug("Wohnung/Strom", "similarity", 0.93))
    decision = decide(sure, settings=settings(mode="always"), folder_exists=ALL)
    assert decision.action == "file" and decision.review is False
    assert not decision.reasons[0].startswith("unsicher")


def test_decision_as_dict():
    decision = decide(make_item(sug("Wohnung/Strom", "similarity", 0.9)), settings=settings(), folder_exists=ALL)
    data = decision.to_dict()
    assert data == {
        "action": "file",
        "confidence": 0.9,
        "review": False,
        "sources": ["similarity"],
        "reasons": ["Grund similarity"],
        "request": {"folder": "Wohnung/Strom", "title": "Titel aus similarity", "doc_date": "2025-02-03",
                    "tags": [], "sender": "Stadtwerke Leipzig GmbH"},
    }
    json.dumps(data)
    held = AutoDecision("hold", None, 0.4, reasons=["unsicher (40 %)"])
    assert held.to_dict()["request"] is None and held.sources == []
