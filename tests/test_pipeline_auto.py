"""Tests for automatic filing in the pipeline: the job chain after processing,
Pipeline.auto_file (file / hold / failures / vetoes / notifications) and the
"no_auto" mark for items a person worked on.

The decision itself (``suggest.auto.decide``) is replaced by a stub here; it
has its own tests. The archive operations are the real ones.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pikepdf
import pytest

from archive_support import attach_archive, make_ready_item
from docushelf.archive import ops as ops_module
from docushelf.config import AutoFileConfig, LlmConfig
from docushelf.errors import Conflict, NotFound, ProcessingError
from docushelf.inbox import pipeline as pipeline_module
from docushelf.inbox.pipeline import AUTO_AFTER_LLM, AUTO_JOB, AUTO_USER, Pipeline
from docushelf.models import Analysis, FilingRequest, Suggestion


def with_auto(settings, **values):
    """``settings`` with ``auto_file = AutoFileConfig(**values)``."""
    return settings.with_overrides(auto_file=AutoFileConfig(**values))


NOTIFY_URL = "http://ntfy.invalid/docushelf"
# The real hook, captured before the fixtures replace it.
REAL_SOURCE_ALLOWED = pipeline_module._source_allowed


# -- fakes -------------------------------------------------------------------------------------------


@dataclass
class Decision:
    """Shaped like suggest.auto.AutoDecision."""

    action: str
    request: FilingRequest | None = None
    confidence: float = 0.0
    review: bool = False
    sources: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def file_decision(**changes) -> Decision:
    values = dict(
        action="file",
        request=FilingRequest(folder="Wohnung/Strom", title="Stromrechnung Januar", doc_date=date(2026, 1, 5)),
        confidence=0.82,
        sources=["similarity", "llm"],
        reasons=["ähnlich zu „2025-12-05 Stromrechnung Dezember“ (88 %)"],
    )
    values.update(changes)
    return Decision(**values)


class StubDecide:
    """Stands in for suggest.auto.decide and records how it was called."""

    def __init__(self, decision: Decision | None = None, error: BaseException | None = None):
        self.decision = decision or file_decision()
        self.error = error
        self.calls: list[dict] = []

    def __call__(self, item, *, settings, folder_exists):
        self.calls.append({
            "item": item.id,
            "settings": settings,
            "folder_exists": folder_exists,
            "suggestion_sources": [s.get("source") for s in item.suggestions],
        })
        if self.error is not None:
            raise self.error
        return self.decision


def stub_source_allowed(source: str, sources) -> bool:
    if source in ("paperless", "unbekannt"):
        return source in sources
    return any(source == entry or (entry.endswith("*") and source.startswith(entry[:-1])) for entry in sources)


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, title: str, message: str, **kwargs) -> None:
        self.sent.append({"title": title, "message": message, **kwargs})


class FakeWorker:
    def __init__(self) -> None:
        self.kicks = 0

    def kick(self) -> None:
        self.kicks += 1


class FakeSuggester:
    def __init__(self) -> None:
        self.llm_calls = 0

    def known_senders(self) -> list[str]:
        return []

    def suggest(self, item, pages, analysis):
        return [Suggestion(folder="Wohnung/Strom", title="Stromrechnung", score=0.9, source="similarity")]

    def llm_suggest(self, item, pages, analysis):
        self.llm_calls += 1
        return Suggestion(folder="Wohnung/Strom", title="Stromrechnung", score=0.5, source="llm")


def fake_normalize(files: list[Path], out: Path) -> int:
    with pikepdf.open(files[0]) as pdf:
        pdf.save(out)
        return len(pdf.pages)


def fake_ocr(src: Path, dst: Path, settings, *, log=None):
    shutil.copyfile(src, dst)
    return SimpleNamespace(performed=False, output=dst, skipped_reason="aus", warnings=[], duration_s=0.0)


# -- fixtures -----------------------------------------------------------------------------------------


@pytest.fixture
def decide(monkeypatch) -> StubDecide:
    stub = StubDecide()
    monkeypatch.setattr(pipeline_module, "_decide", stub)
    return stub


@pytest.fixture
def svc(base_services, monkeypatch):
    monkeypatch.setattr(ops_module, "RETRY_PAUSE", 0)
    monkeypatch.setattr(pipeline_module, "_source_allowed", stub_source_allowed)
    monkeypatch.setattr(pipeline_module, "_describe_sources", lambda sources: [s.upper() for s in sources])
    monkeypatch.setattr(pipeline_module, "_normalize_to_pdf", fake_normalize)
    monkeypatch.setattr(pipeline_module, "_run_ocr", fake_ocr)
    monkeypatch.setattr(pipeline_module, "_detect_blank_pages", lambda *args, **kwargs: [])
    monkeypatch.setattr(pipeline_module, "_analyze", lambda pages, context, *, known_senders=None: Analysis())
    attach_archive(base_services)
    base_services.notifier = FakeNotifier()
    base_services.worker = FakeWorker()
    base_services.suggester = FakeSuggester()
    base_services.settings = with_auto(base_services.settings.with_overrides(notify_url=NOTIFY_URL), mode="confident")
    return base_services


@pytest.fixture
def pipe(svc) -> Pipeline:
    svc.pipeline = Pipeline(svc)
    return svc.pipeline


def configure(svc, *, llm: bool = False, **auto) -> None:
    settings = svc.settings.with_overrides(llm=LlmConfig(url="http://llm.invalid", model="test") if llm else None)
    values = dict(mode="confident")
    values.update(auto)
    svc.settings = with_auto(settings, **values)


def new_item(svc, samples, tmp_path: Path, *, source: str = "smb", name: str = "scan.pdf"):
    original = samples.text_pdf(tmp_path / "drop" / name, ["Stadtwerke Musterstadt\nStromrechnung Januar"])
    return svc.inbox.create(source, [original])


def queued(svc) -> list[tuple[str, str, dict]]:
    return [(job.kind, job.key, job.payload) for job in svc.index.pending_jobs()]


def auto_entries(svc) -> list[dict]:
    return [entry for entry in svc.audit.tail(100) if entry["action"] == "auto_file"]


# -- the job chain after processing ------------------------------------------------------------------


def test_mode_off_queues_nothing_new(svc, pipe, samples, tmp_path):
    configure(svc, mode="off")
    item = new_item(svc, samples, tmp_path)
    pipe.process(item.id)
    assert queued(svc) == []

    configure(svc, mode="off", llm=True)
    other = new_item(svc, samples, tmp_path, name="b.pdf")
    pipe.process(other.id)
    assert queued(svc) == [("llm_suggest", other.id, {})]  # exactly as before


def test_settings_without_auto_file_count_as_off(svc, pipe, samples, tmp_path):
    svc.settings = SimpleNamespace(**{k: getattr(svc.settings, k) for k in ("llm", "blank_page_removal",
                                                                             "blank_page_threshold", "owner_names")})
    item = new_item(svc, samples, tmp_path)
    pipe.process(item.id)
    assert queued(svc) == []


def test_without_llm_the_automatic_filing_is_queued_directly(svc, pipe, samples, tmp_path):
    item = new_item(svc, samples, tmp_path)
    kicks = svc.worker.kicks
    pipe.process(item.id)
    assert queued(svc) == [(AUTO_JOB, item.id, {})]
    assert svc.worker.kicks > kicks


def test_with_llm_the_automatic_filing_waits_for_its_answer(svc, pipe, samples, tmp_path):
    configure(svc, llm=True, wait_for_llm=True)
    item = new_item(svc, samples, tmp_path)
    pipe.process(item.id)
    assert queued(svc) == [("llm_suggest", item.id, {AUTO_AFTER_LLM: True})]


def test_with_llm_but_not_waiting_the_decision_comes_first(svc, pipe, samples, tmp_path):
    configure(svc, llm=True, wait_for_llm=False)
    item = new_item(svc, samples, tmp_path)
    pipe.process(item.id)
    assert queued(svc) == [(AUTO_JOB, item.id, {}), ("llm_suggest", item.id, {})]


@pytest.mark.parametrize("source", ["paperless", "unbekannt", "scanner"])
def test_items_from_sources_that_are_not_eligible_behave_as_before(svc, pipe, samples, tmp_path, source):
    configure(svc, llm=True)
    item = new_item(svc, samples, tmp_path, source=source)
    loaded = pipe.process(item.id)
    assert queued(svc) == [("llm_suggest", item.id, {})]
    assert "auto" not in loaded.context and "no_auto" not in loaded.options


def test_the_decision_module_missing_means_nothing_is_eligible(svc, pipe, samples, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline_module, "_source_allowed", REAL_SOURCE_ALLOWED)
    monkeypatch.setitem(sys.modules, "docushelf.suggest.auto", None)  # the import fails
    item = new_item(svc, samples, tmp_path)
    pipe.process(item.id)
    assert queued(svc) == []


def test_failing_queue_does_not_fail_processing(svc, pipe, samples, tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("Datenbank gesperrt")

    monkeypatch.setattr(svc.index, "enqueue", broken)
    item = new_item(svc, samples, tmp_path)
    assert pipe.process(item.id).state == "bereit"


# -- filing automatically ------------------------------------------------------------------------------


def test_auto_file_files_records_and_notifies(svc, pipe, decide, samples, tmp_path):
    item = make_ready_item(svc, samples, tmp_path, source="smb")

    decision = pipe.auto_file(item.id)

    assert decision is decide.decision
    assert decide.calls[0]["item"] == item.id and decide.calls[0]["settings"] is svc.settings
    done = svc.inbox.load(item.id)
    assert done.state == "erledigt"
    path = "Wohnung/Strom/2026-01-05 Stromrechnung Januar.pdf"
    assert (svc.layout.archive / path).is_file()
    auto = done.context["auto"]
    assert auto["decision"] == "file" and auto["confidence"] == 0.82 and auto["review"] is False
    assert auto["sources"] == ["similarity", "llm"] and auto["path"] == path and auto["doc_id"]
    assert auto["reasons"] == decide.decision.reasons and auto["at"]
    assert done.filing["user"] == AUTO_USER and done.filing["auto"]["confidence"] == 0.82
    assert done.history[-1]["event"] == "filed" and done.history[-1]["user"] == AUTO_USER

    (entry,) = auto_entries(svc)
    assert entry["user"] == AUTO_USER
    assert {key: entry[key] for key in ("doc_id", "path", "title", "folder", "confidence", "review", "item")} == {
        "doc_id": auto["doc_id"],
        "path": path,
        "title": "Stromrechnung Januar",
        "folder": "Wohnung/Strom",
        "confidence": 0.82,
        "review": False,
        "item": item.id,
    }
    assert entry["sources"] == ["similarity", "llm"] and entry["reasons"] == decide.decision.reasons
    assert [e["action"] for e in svc.audit.tail(10)].count("file") == 1

    log = svc.inbox.read_log(done)
    assert "Automatische Ablage nach „Wohnung/Strom“ (82 % – SIMILARITY, LLM). Begründung: ähnlich zu" in log
    assert f"Automatisch abgelegt als „{path}“" in log

    assert svc.notifier.sent == [
        {
            "title": "Abgelegt: Stromrechnung Januar",
            "message": "Wohnung/Strom · 82 %",
            "tags": "file_folder",
            "click": f"http://testserver/dokument/{auto['doc_id']}",
        }
    ]


def test_auto_file_with_review_asks_to_check(svc, pipe, decide, samples, tmp_path):
    configure(svc, mode="always")
    decide.decision = file_decision(confidence=0.584, review=True)
    item = make_ready_item(svc, samples, tmp_path, source="mail")

    pipe.auto_file(item.id)

    assert svc.inbox.load(item.id).context["auto"]["review"] is True
    assert auto_entries(svc)[0]["review"] is True
    (sent,) = svc.notifier.sent
    assert sent["message"] == "Wohnung/Strom · 58 % – bitte prüfen" and sent["tags"] == "warning"
    assert ", unsicher – bitte prüfen" in svc.inbox.read_log(svc.inbox.load(item.id))


@pytest.mark.parametrize("notify, url", [(False, NOTIFY_URL), (True, None)])
def test_no_notification_unless_wanted_and_configured(svc, pipe, decide, samples, tmp_path, notify, url):
    configure(svc, notify=notify)
    svc.settings = svc.settings.with_overrides(notify_url=url)
    item = make_ready_item(svc, samples, tmp_path)
    pipe.auto_file(item.id)
    assert svc.inbox.load(item.id).state == "erledigt"
    assert svc.notifier.sent == []


def test_failing_notification_does_not_matter(svc, pipe, decide, samples, tmp_path):
    def broken(*args, **kwargs):
        raise RuntimeError("kein Netz")

    svc.notifier.send = broken
    item = make_ready_item(svc, samples, tmp_path)
    assert pipe.auto_file(item.id).action == "file"
    assert svc.inbox.load(item.id).state == "erledigt"


def test_filing_into_the_archive_root_names_the_archive(svc, pipe, decide, samples, tmp_path):
    decide.decision = file_decision(request=FilingRequest(folder="", title="Brief", doc_date=None))
    item = make_ready_item(svc, samples, tmp_path)
    pipe.auto_file(item.id)
    assert svc.notifier.sent[0]["message"] == "Archiv · 82 %"
    assert "Automatische Ablage nach „Archiv“" in svc.inbox.read_log(svc.inbox.load(item.id))


# -- holding back ----------------------------------------------------------------------------------------


def test_hold_leaves_the_item_waiting_with_the_reason(svc, pipe, decide, samples, tmp_path):
    decide.decision = Decision(action="hold", confidence=0.58, sources=["similarity"], reasons=["unsicher (58 %)"])
    item = make_ready_item(svc, samples, tmp_path)

    assert pipe.auto_file(item.id) is decide.decision

    held = svc.inbox.load(item.id)
    assert held.state == "bereit" and held.filing is None
    auto = held.context["auto"]
    assert set(auto) == {"decision", "confidence", "reasons", "at"}
    assert auto["decision"] == "hold" and auto["confidence"] == 0.58 and auto["reasons"] == ["unsicher (58 %)"]
    assert datetime.fromisoformat(auto["at"]) > datetime.now().astimezone() - timedelta(minutes=1)
    assert held.history[-1] == {"at": held.history[-1]["at"], "event": "auto_hold", "reason": "unsicher (58 %)"}
    assert "Nicht automatisch abgelegt: unsicher (58 %)" in svc.inbox.read_log(held)
    assert auto_entries(svc) == [] and svc.notifier.sent == []
    assert_archive_empty(svc)


def assert_archive_empty(svc) -> None:
    assert [p for p in svc.layout.archive.rglob("*") if p.is_file()] == []


def test_hold_without_reasons_still_says_something(svc, pipe, decide, samples, tmp_path):
    decide.decision = Decision(action="hold")
    item = make_ready_item(svc, samples, tmp_path)
    pipe.auto_file(item.id)
    assert svc.inbox.load(item.id).context["auto"]["reasons"] == ["kein Grund angegeben"]


def test_a_file_decision_without_request_is_a_hold(svc, pipe, decide, samples, tmp_path):
    decide.decision = Decision(action="file", request=None, confidence=0.9, reasons=["?"])
    item = make_ready_item(svc, samples, tmp_path)
    pipe.auto_file(item.id)
    assert svc.inbox.load(item.id).state == "bereit"
    assert svc.inbox.load(item.id).context["auto"]["decision"] == "hold"


@pytest.mark.parametrize(
    "change, reason",
    [
        (lambda item: item.options.update(no_auto=True), "von Hand bearbeitet"),
        (lambda item: setattr(item, "duplicate_of", {"kind": "archive", "id": "x", "path": "a.pdf"}),
         "mögliches Duplikat"),
    ],
)
def test_vetoes_win_over_a_file_decision(svc, pipe, decide, samples, tmp_path, change, reason):
    item = make_ready_item(svc, samples, tmp_path)
    change(item)
    svc.inbox.save(item)

    result = pipe.auto_file(item.id)

    assert result.action == "hold" and result.request is None and result.reasons == [reason]
    assert result.confidence == 0.82  # what the decision said
    held = svc.inbox.load(item.id)
    assert held.state == "bereit" and held.context["auto"]["reasons"] == [reason]
    assert_archive_empty(svc)


def test_failing_decision_is_a_hold(svc, pipe, decide, samples, tmp_path):
    decide.error = KeyError("score")
    item = make_ready_item(svc, samples, tmp_path)
    result = pipe.auto_file(item.id)
    assert result.action == "hold"
    assert svc.inbox.load(item.id).context["auto"]["reasons"] == [
        "Entscheidung nicht möglich: Unerwarteter Fehler (KeyError): 'score'"
    ]


def test_missing_decision_module_is_a_hold(svc, pipe, samples, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "docushelf.suggest.auto", None)
    item = make_ready_item(svc, samples, tmp_path)
    result = pipe.auto_file(item.id)
    assert result.action == "hold" and result.reasons == ["Automatische Ablage ist nicht verfügbar"]
    assert svc.inbox.load(item.id).state == "bereit"


def test_switched_off_meanwhile_leaves_the_item_untouched(svc, pipe, decide, samples, tmp_path):
    configure(svc, mode="off")
    item = make_ready_item(svc, samples, tmp_path)
    before = svc.inbox.load(item.id).to_dict()

    result = pipe.auto_file(item.id)

    assert result.action == "hold" and decide.calls == []
    assert svc.inbox.load(item.id).to_dict() == before


def test_items_not_waiting_in_ready_are_refused(svc, pipe, decide, samples, tmp_path):
    item = new_item(svc, samples, tmp_path)  # still in "arbeit"
    with pytest.raises(Conflict):
        pipe.auto_file(item.id)
    with pytest.raises(NotFound):
        pipe.auto_file("20990101-000000-abcdef")

    ready = make_ready_item(svc, samples, tmp_path)
    ready.filing = {"state": "publishing", "doc_id": "x"}
    svc.inbox.save(ready)
    with pytest.raises(Conflict):
        pipe.auto_file(ready.id)
    assert decide.calls == []


def test_second_run_after_filing_is_refused(svc, pipe, decide, samples, tmp_path):
    item = make_ready_item(svc, samples, tmp_path)
    pipe.auto_file(item.id)
    with pytest.raises(Conflict, match="bereits abgelegt"):
        pipe.auto_file(item.id)
    assert len(svc.notifier.sent) == 1 and len(auto_entries(svc)) == 1


# -- failures while filing ------------------------------------------------------------------------------------


def test_failed_filing_keeps_the_item_waiting_with_the_reason(svc, pipe, decide, samples, tmp_path):
    (svc.layout.archive / "Wohnung").write_bytes(b"a file where the folder should be")
    item = make_ready_item(svc, samples, tmp_path)

    result = pipe.auto_file(item.id)

    assert result.action == "hold" and result.request is None
    held = svc.inbox.load(item.id)
    assert held.state == "bereit" and held.filing is None and held.document_path.is_file()
    reasons = held.context["auto"]["reasons"]
    assert len(reasons) == 1 and reasons[0].startswith("Ablage fehlgeschlagen: „Wohnung“ ist eine Datei")
    assert held.context["auto"]["confidence"] == 0.82
    assert "Nicht automatisch abgelegt: Ablage fehlgeschlagen" in svc.inbox.read_log(held)
    assert auto_entries(svc) == [] and svc.notifier.sent == []


def test_transient_filing_error_is_not_raised_for_a_retry(svc, pipe, decide, samples, tmp_path, monkeypatch):
    def unreachable(*args, **kwargs):
        raise ProcessingError("Das Archiv ist nicht erreichbar.", stage="archive", transient=True)

    monkeypatch.setattr(svc.ops, "file_item", unreachable)
    item = make_ready_item(svc, samples, tmp_path)

    result = pipe.auto_file(item.id)

    assert result.action == "hold"
    assert svc.inbox.load(item.id).context["auto"]["reasons"] == [
        "Ablage fehlgeschlagen: Das Archiv ist nicht erreichbar."
    ]


def test_unexpected_filing_error_is_a_hold_too(svc, pipe, decide, samples, tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(svc.ops, "file_item", broken)
    item = make_ready_item(svc, samples, tmp_path)
    assert pipe.auto_file(item.id).reasons == ["Ablage fehlgeschlagen: Unerwarteter Fehler (RuntimeError): kaputt"]


def test_error_after_publishing_leaves_the_journal_to_recover(svc, pipe, decide, samples, tmp_path, monkeypatch):
    real_move = svc.inbox.move

    def move(item, state):
        if state == "erledigt":
            raise OSError(5, "Input/output error")
        return real_move(item, state)

    monkeypatch.setattr(svc.inbox, "move", move)
    item = make_ready_item(svc, samples, tmp_path)

    result = pipe.auto_file(item.id)

    assert result.action == "hold"
    waiting = svc.inbox.load(item.id)
    assert waiting.filing and waiting.context["auto"]["decision"] == "file"  # the filing happened
    monkeypatch.setattr(svc.inbox, "move", real_move)
    svc.ops.recover()
    done = svc.inbox.load(item.id)
    assert done.state == "erledigt" and done.context["auto"]["decision"] == "file"
    assert len(auto_entries(svc)) == 1


# -- folders the decision may ask about ---------------------------------------------------------------------------


def test_folder_exists_matches_like_filing_does(svc, pipe):
    (svc.layout.archive / "Versicherungen" / "Kfz  Versicherung").mkdir(parents=True)
    (svc.layout.archive / "Datei.pdf").write_bytes(b"%PDF")
    os.symlink(svc.layout.archive / "Versicherungen", svc.layout.archive / "Verknüpfung")

    exists = pipe._folder_exists
    assert exists("Versicherungen") and exists("versicherungen/kfz versicherung")
    assert exists(" Versicherungen / Kfz  Versicherung ")
    assert exists("")  # the archive itself
    assert not exists("Versicherungen/Hausrat")
    assert not exists("Datei.pdf")
    assert not exists("Verknüpfung")
    assert not exists("../data")
    assert not exists("Versicherungen/../..")


def test_decision_gets_a_working_folder_check(svc, pipe, decide, samples, tmp_path):
    (svc.layout.archive / "Wohnung" / "Strom").mkdir(parents=True)
    decide.decision = Decision(action="hold", reasons=["x"])
    item = make_ready_item(svc, samples, tmp_path)
    pipe.auto_file(item.id)
    folder_exists = decide.calls[0]["folder_exists"]
    assert folder_exists("wohnung/strom") is True and folder_exists("Wohnung/Gas") is False


# -- items a person worked on ---------------------------------------------------------------------------------------


def test_retry_marks_the_item_and_clears_the_old_decision(svc, pipe, decide, samples, tmp_path):
    decide.decision = Decision(action="hold", confidence=0.4, reasons=["unsicher (40 %)"])
    item = new_item(svc, samples, tmp_path)
    pipe.process(item.id)
    pipe.auto_file(item.id)
    assert "auto" in svc.inbox.load(item.id).context

    retried = pipe.retry(item.id)

    assert retried.options["no_auto"] is True and "auto" not in retried.context
    assert "Wird danach nicht automatisch abgelegt (von Hand neu verarbeitet)" in svc.inbox.read_log(retried)
    assert svc.inbox.load(item.id).options["no_auto"] is True

    # After processing, the automatic filing still runs, but holds the item back for the person.
    decide.decision = file_decision()
    pipe.process(item.id)
    result = pipe.auto_file(item.id)
    assert result.action == "hold" and result.reasons == ["von Hand bearbeitet"]
    assert svc.inbox.load(item.id).state == "bereit"


def test_retry_in_mode_off_changes_no_options(svc, pipe, samples, tmp_path):
    configure(svc, mode="off")
    item = new_item(svc, samples, tmp_path)
    pipe.process(item.id)
    retried = pipe.retry(item.id, skip_ocr=True)
    assert retried.options == {"skip_ocr": True}
    assert "nicht automatisch" not in svc.inbox.read_log(retried)


def test_manual_refresh_marks_the_item(svc, pipe, samples, tmp_path):
    item = new_item(svc, samples, tmp_path)
    pipe.process(item.id)

    refreshed = pipe.refresh_suggestions(item.id)

    assert refreshed.options["no_auto"] is True
    assert svc.inbox.load(item.id).options["no_auto"] is True
    assert svc.inbox.read_log(refreshed).count("Wird nicht mehr automatisch abgelegt (von Hand bearbeitet)") == 1
    pipe.refresh_suggestions(item.id)  # marked once, logged once
    assert svc.inbox.read_log(refreshed).count("Wird nicht mehr automatisch abgelegt") == 1


def test_queued_refresh_does_not_mark_the_item(svc, pipe, samples, tmp_path):
    item = new_item(svc, samples, tmp_path)
    pipe.process(item.id)
    refreshed = pipe.refresh_suggestions(item.id, manual=False)
    assert "no_auto" not in refreshed.options


def test_manual_refresh_in_mode_off_changes_no_options(svc, pipe, samples, tmp_path):
    configure(svc, mode="off")
    item = new_item(svc, samples, tmp_path)
    pipe.process(item.id)
    assert pipe.refresh_suggestions(item.id).options == {}


def test_manual_refresh_while_the_filing_is_pending_prevents_it(svc, pipe, decide, samples, tmp_path):
    item = new_item(svc, samples, tmp_path)
    pipe.process(item.id)
    assert queued(svc) == [(AUTO_JOB, item.id, {})]

    pipe.refresh_suggestions(item.id)  # the person looks at it before the job ran
    result = pipe.auto_file(item.id)

    assert result.action == "hold" and svc.inbox.load(item.id).state == "bereit"


# -- the whole chain with the real worker ----------------------------------------------------------------------


def wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.mark.parametrize("wait_for_llm", [True, False])
def test_worker_runs_processing_model_and_filing_in_order(svc, pipe, decide, samples, tmp_path, wait_for_llm):
    from docushelf.inbox.worker import Worker

    configure(svc, llm=True, wait_for_llm=wait_for_llm)
    svc.worker = worker = Worker(svc, idle_timeout=60.0, maintenance_interval=3600.0, resume_interval=None)
    worker.start()
    try:
        item = new_item(svc, samples, tmp_path)
        svc.index.enqueue("process", item.id)
        worker.kick()

        assert wait_until(lambda: svc.inbox.find(item.id, states=("erledigt",)) is not None)
        assert wait_until(lambda: svc.index.pending_jobs() == [])
    finally:
        worker.stop()

    (call,) = decide.calls
    if wait_for_llm:
        # The decision saw the model's answer.
        assert call["suggestion_sources"] == ["similarity", "llm"] and svc.suggester.llm_calls == 1
    else:
        # Decided (and filed) first; the model was not asked for an item that is filed already.
        assert call["suggestion_sources"] == ["similarity"] and svc.suggester.llm_calls == 0
    done = svc.inbox.load(item.id)
    assert done.context["auto"]["decision"] == "file" and len(auto_entries(svc)) == 1
    assert len(svc.notifier.sent) == 1
