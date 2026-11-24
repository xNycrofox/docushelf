"""Tests for the processing pipeline (stages, resumption, failures, retries, suggestions)."""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pikepdf
import pytest

from docushelf.config import LlmConfig
from docushelf.errors import Conflict, NotFound, ProcessingError, UnsupportedFile
from docushelf.inbox import pipeline as pipeline_module
from docushelf.inbox.pipeline import Pipeline, error_message
from docushelf.models import Analysis, DocumentRecord, FilingRequest, Suggestion
from docushelf.pdf.text import extract_pages
from docushelf.util import fsops

# The real collaborator hooks, captured before any test replaces them.
REAL = {
    name: getattr(pipeline_module, name)
    for name in ("_normalize_to_pdf", "_detect_blank_pages", "_remove_pages", "_run_ocr", "_analyze")
}

# -- fakes for the collaborators -------------------------------------------------------------


def fake_normalize(files: list[Path], out: Path) -> int:
    pdf = pikepdf.new()
    sources = [pikepdf.open(path) for path in files]
    try:
        for source in sources:
            pdf.pages.extend(source.pages)
        pdf.save(out)
        return len(pdf.pages)
    finally:
        for source in sources:
            source.close()


def fake_detect_blank_pages(pdf: Path, *, threshold: float, text_pages: list[str] | None = None) -> list[int]:
    """Pages without text count as blank."""
    return [number for number, text in enumerate(text_pages or [], 1) if not text.strip()]


def fake_remove_pages(src: Path, dst: Path, pages: list[int]) -> None:
    with pikepdf.open(src) as pdf:
        for number in sorted(pages, reverse=True):
            del pdf.pages[number - 1]
        pdf.save(dst)


class FakeOcr:
    def __init__(self) -> None:
        self.calls = 0
        self.error: BaseException | None = None

    def __call__(self, src: Path, dst: Path, settings, *, log=None):
        self.calls += 1
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        shutil.copyfile(src, dst)
        return SimpleNamespace(
            performed=False,
            output=dst,
            skipped_reason="Texterkennung ist ausgeschaltet",
            warnings=[],
            returncode=0,
            duration_s=0.0,
        )


def fake_analyze(pages, context=None, *, known_senders=None, today=None) -> Analysis:
    text = "\n".join(pages)
    return Analysis(
        doc_type="Rechnung" if "Rechnung" in text else None,
        sender=next((s for s in known_senders or [] if s in text), None),
        doc_date="2026-11-02" if "02.11.2026" in text else None,
        text_chars=len(text),
    )


class FakeSuggester:
    def __init__(self) -> None:
        self.calls = 0
        self.result = [
            Suggestion(folder="Wohnung/Strom", title="Stromrechnung", score=0.9, source="rule", reasons=["Regel"]),
            Suggestion(folder="Rechnungen", score=0.2, source="heuristic", new_folder=True),
        ]
        self.llm_result: Suggestion | None = Suggestion(folder="KI/Vorschlag", title="KI", score=0.5, source="llm")
        self.llm_calls = 0

    def known_senders(self) -> list[str]:
        return ["Stadtwerke"]

    def suggest(self, item, pages, analysis):
        self.calls += 1
        return list(self.result)

    def llm_suggest(self, item, pages, analysis):
        self.llm_calls += 1
        return self.llm_result


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, title: str, message: str, **kwargs) -> None:
        self.sent.append((title, message))


class FakeWorker:
    def __init__(self) -> None:
        self.kicks = 0

    def kick(self) -> None:
        self.kicks += 1


@pytest.fixture
def ocr(monkeypatch) -> FakeOcr:
    fake = FakeOcr()
    monkeypatch.setattr(pipeline_module, "_run_ocr", fake)
    return fake


@pytest.fixture
def svc(base_services, monkeypatch, ocr):
    monkeypatch.setattr(pipeline_module, "_normalize_to_pdf", fake_normalize)
    monkeypatch.setattr(pipeline_module, "_detect_blank_pages", fake_detect_blank_pages)
    monkeypatch.setattr(pipeline_module, "_remove_pages", fake_remove_pages)
    monkeypatch.setattr(pipeline_module, "_analyze", fake_analyze)
    base_services.suggester = FakeSuggester()
    base_services.notifier = FakeNotifier()
    base_services.worker = FakeWorker()
    return base_services


@pytest.fixture
def pipe(svc) -> Pipeline:
    return Pipeline(svc)


TEXT = "Stadtwerke Musterstadt\nRechnung vom 02.11.2026\nBetrag 12,00 EUR"


def new_item(services, samples, tmp_path: Path, pages=None, *, name: str = "scan.pdf", **kwargs):
    source = samples.text_pdf(tmp_path / "eingang" / name, pages or [TEXT])
    return services.inbox.create("upload", [source], **kwargs)


# -- the happy path -----------------------------------------------------------------------------


def test_process_runs_all_stages_and_moves_item_to_ready(svc, pipe, samples, tmp_path):
    item = new_item(svc, samples, tmp_path)

    result = pipe.process(item.id)

    assert result.state == "bereit" and result.stage == "ready"
    loaded = svc.inbox.load(item.id)
    assert loaded.state == "bereit" and loaded.stage == "ready" and loaded.error is None
    assert loaded.document_path.is_file()
    assert not loaded.work_dir.exists()
    assert svc.inbox.read_text(loaded) == extract_pages(loaded.document_path)
    assert "Stadtwerke" in svc.inbox.read_text(loaded)[0]
    assert loaded.page_count == 1 and loaded.pages_removed == []
    assert loaded.analysis["sender"] == "Stadtwerke" and loaded.analysis["doc_type"] == "Rechnung"
    assert [s["folder"] for s in loaded.suggestions] == ["Wohnung/Strom", "Rechnungen"]
    assert loaded.duplicate_of is None
    assert loaded.attempts == 1
    assert [h["event"] for h in loaded.history] == ["received", "processed"]
    # The original is untouched.
    assert fsops.sha256_file(loaded.original_paths()[0]) == loaded.origin_hash

    text = svc.inbox.read_log(loaded)
    for expected in (
        "Verarbeitung gestartet",
        "PDF erstellt: 1 Seite aus 1 Datei",
        "Keine Leerseiten gefunden",
        "OCR übersprungen: Texterkennung ist ausgeschaltet",
        "Text gelesen:",
        "Analyse: Rechnung, Absender „Stadtwerke“, Datum 02.11.2026",
        "2 Ablagevorschläge, bester: „Wohnung/Strom“",
        "Bereit zur Ablage",
    ):
        assert expected in text, expected
    assert svc.index.pending_jobs("llm_suggest") == []  # no LLM configured


def test_multiple_originals_become_one_document(svc, pipe, samples, tmp_path):
    first = samples.text_pdf(tmp_path / "a.pdf", ["Seite eins"])
    second = samples.text_pdf(tmp_path / "b.pdf", ["Seite zwei", "Seite drei"])
    item = svc.inbox.create("mail", [first, second])

    loaded = pipe.process(item.id)

    assert loaded.page_count == 3
    assert svc.inbox.read_text(loaded) == ["Seite eins", "Seite zwei", "Seite drei"]
    assert "PDF erstellt: 3 Seiten aus 2 Dateien" in svc.inbox.read_log(loaded)


def test_ocr_result_is_logged(svc, pipe, samples, tmp_path, monkeypatch):
    def ocr(src, dst, settings, *, log=None):
        shutil.copyfile(src, dst)
        if log:
            log("ocrmypdf meldet etwas")
        return SimpleNamespace(performed=True, output=dst, skipped_reason=None, warnings=["PDF/A nicht möglich"],
                               returncode=10, duration_s=12.4)

    monkeypatch.setattr(pipeline_module, "_run_ocr", ocr)
    item = new_item(svc, samples, tmp_path, [TEXT, "Seite 2", "Seite 3"])

    pipe.process(item.id)

    text = svc.inbox.read_log(svc.inbox.load(item.id))
    assert "OCR abgeschlossen (3 Seiten, 12 s)" in text
    assert "Hinweis der Texterkennung: PDF/A nicht möglich" in text
    assert "ocrmypdf meldet etwas" in text


# -- blank pages and OCR options -----------------------------------------------------------------


def test_blank_pages_are_removed(svc, pipe, samples, tmp_path):
    item = new_item(svc, samples, tmp_path, ["Seite eins", "", "Seite drei", ""])

    loaded = pipe.process(item.id)

    assert loaded.pages_removed == [2, 4]
    assert loaded.page_count == 2
    assert svc.inbox.read_text(loaded) == ["Seite eins", "Seite drei"]
    assert "2 Leerseiten entfernt (Seite 2, 4), 2 Seiten verbleiben" in svc.inbox.read_log(loaded)
    # The original keeps all pages.
    with pikepdf.open(loaded.original_paths()[0]) as original:
        assert len(original.pages) == 4


def test_blank_page_removal_can_be_skipped(svc, pipe, samples, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline_module, "_detect_blank_pages", lambda *a, **k: calls.append(1) or [2])
    item = new_item(svc, samples, tmp_path, ["eins", ""], options={"skip_blank_removal": True})
    assert pipe.process(item.id).page_count == 2
    assert "für dieses Dokument ausgeschaltet" in svc.inbox.read_log(svc.inbox.load(item.id))

    svc.settings = svc.settings.with_overrides(blank_page_removal=False)
    other = new_item(svc, samples, tmp_path, ["eins", ""], name="b.pdf")
    assert pipe.process(other.id).page_count == 2
    assert calls == []


def test_all_blank_pages_are_kept(svc, pipe, samples, tmp_path):
    item = new_item(svc, samples, tmp_path, ["", ""])
    loaded = pipe.process(item.id)
    assert loaded.page_count == 2 and loaded.pages_removed == []
    assert "Alle Seiten wirken leer" in svc.inbox.read_log(loaded)


def test_failing_blank_page_detection_keeps_all_pages(svc, pipe, samples, tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("render failed")

    monkeypatch.setattr(pipeline_module, "_detect_blank_pages", broken)
    item = new_item(svc, samples, tmp_path, ["eins", ""])
    loaded = pipe.process(item.id)
    assert loaded.state == "bereit" and loaded.page_count == 2
    assert "Leerseitenerkennung fehlgeschlagen" in svc.inbox.read_log(loaded)


def test_skip_ocr_copies_the_document(svc, pipe, ocr, samples, tmp_path):
    item = new_item(svc, samples, tmp_path, options={"skip_ocr": True})
    loaded = pipe.process(item.id)
    assert ocr.calls == 0
    assert "Stadtwerke" in svc.inbox.read_text(loaded)[0]
    assert "Texterkennung übersprungen (für dieses Dokument ausgeschaltet)" in svc.inbox.read_log(loaded)


# -- resumption ----------------------------------------------------------------------------------


def test_transient_error_keeps_progress_and_processing_resumes(svc, pipe, ocr, samples, tmp_path, monkeypatch):
    normalize_calls = []

    def counting_normalize(files, out):
        normalize_calls.append(1)
        return fake_normalize(files, out)

    monkeypatch.setattr(pipeline_module, "_normalize_to_pdf", counting_normalize)
    ocr.error = ProcessingError("Zeitüberschreitung bei der Texterkennung", transient=True)
    item = new_item(svc, samples, tmp_path)

    with pytest.raises(ProcessingError):
        pipe.process(item.id)

    stalled = svc.inbox.load(item.id)
    assert stalled.state == "arbeit" and stalled.stage == "deblanked"
    assert (stalled.work_dir / "deblanked.pdf").is_file()
    assert "Vorübergehender Fehler bei Texterkennung (OCR): Zeitüberschreitung" in svc.inbox.read_log(stalled)
    assert svc.notifier.sent == []

    loaded = pipe.process(item.id)
    assert loaded.state == "bereit"
    assert len(normalize_calls) == 1  # finished stages are not repeated
    assert loaded.attempts == 2
    assert "nach Stufe „Leerseitenerkennung“ fortgesetzt" in svc.inbox.read_log(loaded)


class Crash(BaseException):
    """Simulates the process being killed."""


def test_crash_after_the_last_stage_is_completed_on_the_next_run(svc, pipe, samples, tmp_path, monkeypatch):
    item = new_item(svc, samples, tmp_path)
    real_move = svc.inbox.move
    crashes = []

    def crashing_move(target, state):
        if not crashes:
            crashes.append(state)
            raise Crash()
        return real_move(target, state)

    monkeypatch.setattr(svc.inbox, "move", crashing_move)
    with pytest.raises(Crash):
        pipe.process(item.id)
    assert crashes == ["bereit"]
    stalled = svc.inbox.load(item.id)
    assert stalled.state == "arbeit" and stalled.stage == "ready"

    loaded = pipe.process(item.id)
    assert loaded.state == "bereit"
    assert svc.suggester.calls == 1


def test_missing_intermediate_files_are_recreated(svc, pipe, ocr, samples, tmp_path):
    ocr.error = ProcessingError("Zeitüberschreitung", transient=True)
    item = new_item(svc, samples, tmp_path)
    with pytest.raises(ProcessingError):
        pipe.process(item.id)
    stalled = svc.inbox.load(item.id)
    shutil.rmtree(stalled.work_dir)

    loaded = pipe.process(item.id)

    assert loaded.state == "bereit"
    assert "Zwischenergebnis work/deblanked.pdf fehlt" in svc.inbox.read_log(loaded)


def test_item_removed_during_processing_is_not_recreated(svc, pipe, samples, tmp_path, monkeypatch):
    item = new_item(svc, samples, tmp_path)
    elsewhere = tmp_path / "papierkorb" / item.id

    def normalize_then_delete(files, out):
        pages = fake_normalize(files, out)
        elsewhere.parent.mkdir(parents=True)
        shutil.move(str(item.dir), str(elsewhere))
        return pages

    monkeypatch.setattr(pipeline_module, "_normalize_to_pdf", normalize_then_delete)
    with pytest.raises(NotFound):
        pipe.process(item.id)
    assert not (svc.layout.work / item.id).exists()
    assert not (svc.layout.failed / item.id).exists()
    assert (elsewhere / "item.json").is_file()


def test_the_same_item_is_never_processed_twice_at_once(svc, pipe, samples, tmp_path, monkeypatch):
    item = new_item(svc, samples, tmp_path)
    refused = []

    def normalize_while_someone_else_tries(files, out):
        with pytest.raises(Conflict):
            pipe.process(item.id)
        refused.append(item.id)
        return fake_normalize(files, out)

    monkeypatch.setattr(pipeline_module, "_normalize_to_pdf", normalize_while_someone_else_tries)
    assert pipe.process(item.id).state == "bereit"
    assert refused == [item.id]
    assert pipe.retry(item.id).state == "arbeit"
    assert pipe.process(item.id).state == "bereit"  # released again


def test_process_requires_an_item_in_work(svc, pipe, samples, tmp_path):
    item = new_item(svc, samples, tmp_path)
    pipe.process(item.id)
    with pytest.raises(NotFound):
        pipe.process(item.id)  # already in bereit
    with pytest.raises(NotFound):
        pipe.process("20990101-000000-abcdef")


# -- failures ------------------------------------------------------------------------------------


def test_permanent_error_moves_item_to_failed_and_notifies(svc, pipe, samples, tmp_path, monkeypatch):
    def unsupported(files, out):
        raise UnsupportedFile("Dateityp wird nicht unterstützt: .docx")

    monkeypatch.setattr(pipeline_module, "_normalize_to_pdf", unsupported)
    item = new_item(svc, samples, tmp_path)

    result = pipe.process(item.id)

    assert result.state == "fehler"
    loaded = svc.inbox.load(item.id)
    assert loaded.state == "fehler"
    assert loaded.error["stage"] == "normalized"
    assert loaded.error["message"] == "Dateityp wird nicht unterstützt: .docx"
    assert loaded.error["at"]
    assert "Fehler bei Umwandlung in PDF: Dateityp wird nicht unterstützt" in svc.inbox.read_log(loaded)
    assert loaded.original_paths()[0].is_file()
    assert len(svc.notifier.sent) == 1
    title, message = svc.notifier.sent[0]
    assert "fehlgeschlagen" in title and "Dateityp wird nicht unterstützt" in message


def test_unexpected_exception_fails_the_item(svc, pipe, ocr, samples, tmp_path):
    ocr.error = RuntimeError("kaputt")
    item = new_item(svc, samples, tmp_path)

    result = pipe.process(item.id)

    assert result.state == "fehler"
    assert result.error["stage"] == "ocr_done"
    assert result.error["message"] == "Unerwarteter Fehler (RuntimeError): kaputt"


def test_broken_analysis_and_suggestions_do_not_block_filing(svc, pipe, samples, tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("regex explodiert")

    monkeypatch.setattr(pipeline_module, "_analyze", broken)
    monkeypatch.setattr(svc.suggester, "suggest", broken)
    item = new_item(svc, samples, tmp_path)

    loaded = pipe.process(item.id)

    assert loaded.state == "bereit"
    assert loaded.analysis == Analysis().to_dict()
    assert loaded.suggestions == []
    text = svc.inbox.read_log(loaded)
    assert "Analyse fehlgeschlagen" in text and "Vorschläge konnten nicht berechnet werden" in text


def test_fail_records_the_error_of_a_given_up_job(svc, pipe, samples, tmp_path):
    item = new_item(svc, samples, tmp_path)
    exc = ProcessingError("Zeitüberschreitung bei der Texterkennung", transient=True)

    result = pipe.fail(item.id, exc)

    assert result.state == "fehler"
    assert result.error["stage"] == "normalized"
    assert result.error["transient"] is True
    assert svc.inbox.load(item.id).error["message"] == "Zeitüberschreitung bei der Texterkennung"


def test_error_message_is_german():
    assert error_message(PermissionError(13, "denied", "/x/a.pdf")) == "Keine Berechtigung: /x/a.pdf"
    assert error_message(OSError(28, "No space left on device")) == "Dateisystemfehler: No space left on device"
    assert error_message(ProcessingError("PDF ist verschlüsselt")) == "PDF ist verschlüsselt"
    assert error_message(KeyError("x")).startswith("Unerwarteter Fehler (KeyError)")


# -- duplicates ------------------------------------------------------------------------------------


def test_duplicate_of_an_archived_document_is_marked(svc, pipe, samples, tmp_path):
    item = new_item(svc, samples, tmp_path)
    svc.index.upsert(
        DocumentRecord(
            id="1b4e28ba-2fa1-11d2-883f-0016d3cca427", path="Wohnung/Strom/2026-11-02 Stromrechnung.pdf",
            folder="Wohnung/Strom", filename="2026-11-02 Stromrechnung.pdf", ext=".pdf", title="Stromrechnung",
            doc_date=date(2026, 11, 2), sender="Stadtwerke", tags=[], origin_hash=item.origin_hash,
            content_hash="c" * 64, xmp_id=None, has_xmp=True, size=1, mtime_ns=1, inode=None, page_count=1,
            text_status="ok", indexed_at="",
        )
    )

    loaded = pipe.process(item.id)

    assert loaded.duplicate_of == {
        "kind": "archive",
        "id": "1b4e28ba-2fa1-11d2-883f-0016d3cca427",
        "path": "Wohnung/Strom/2026-11-02 Stromrechnung.pdf",
        "title": "Stromrechnung",
    }
    assert "Mögliches Duplikat: bereits im Archiv" in svc.inbox.read_log(loaded)


def test_only_the_later_of_two_identical_inbox_items_is_a_duplicate(svc, pipe, samples, tmp_path):
    source = samples.text_pdf(tmp_path / "x.pdf", [TEXT])
    first = svc.inbox.create("upload", [source], move=False, received_at="2026-11-23T10:00:00+01:00")
    second = svc.inbox.create("mail", [source], move=False, received_at="2026-11-23T10:05:00+01:00")
    assert first.origin_hash == second.origin_hash

    later = pipe.process(second.id)
    earlier = pipe.process(first.id)

    assert later.duplicate_of == {"kind": "inbox", "id": first.id, "title": "x.pdf", "state": "arbeit"}
    assert earlier.duplicate_of is None
    assert pipe.refresh_suggestions(second.id).duplicate_of["state"] == "bereit"


# -- language model ----------------------------------------------------------------------------------


def test_llm_suggestion_is_queued_and_appended(svc, pipe, samples, tmp_path):
    svc.settings = svc.settings.with_overrides(llm=LlmConfig(url="http://llm.invalid", model="test"))
    item = new_item(svc, samples, tmp_path)

    pipe.process(item.id)
    assert [job.key for job in svc.index.pending_jobs("llm_suggest")] == [item.id]

    loaded = pipe.run_llm(item.id)
    assert [s["source"] for s in loaded.suggestions] == ["rule", "heuristic", "llm"]
    assert loaded.suggestions[-1]["folder"] == "KI/Vorschlag"
    assert "KI-Vorschlag: „KI/Vorschlag“" in svc.inbox.read_log(loaded)

    # A second run replaces the earlier answer instead of piling up.
    svc.suggester.llm_result = Suggestion(folder="KI/Anders", source="llm")
    loaded = pipe.run_llm(item.id)
    assert [s["folder"] for s in loaded.suggestions if s["source"] == "llm"] == ["KI/Anders"]

    svc.suggester.llm_result = None
    loaded = pipe.run_llm(item.id)
    assert [s["source"] for s in loaded.suggestions] == ["rule", "heuristic"]
    assert "Kein KI-Vorschlag erhalten" in svc.inbox.read_log(loaded)


def test_llm_is_not_asked_when_disabled(svc, pipe, samples, tmp_path):
    item = new_item(svc, samples, tmp_path)
    pipe.process(item.id)
    assert len(pipe.run_llm(item.id).suggestions) == 2
    assert svc.suggester.llm_calls == 0


def test_llm_answer_for_an_item_filed_meanwhile_is_dropped(svc, pipe, samples, tmp_path):
    svc.settings = svc.settings.with_overrides(llm=LlmConfig(url="http://llm.invalid", model="test"))
    item = new_item(svc, samples, tmp_path)
    pipe.process(item.id)

    def slow_llm(target, pages, analysis):
        svc.inbox.move(svc.inbox.load(item.id), "erledigt")  # filed while the model was thinking
        return Suggestion(folder="Zu spät", source="llm")

    svc.suggester.llm_suggest = slow_llm
    with pytest.raises(Conflict, match="bereits abgelegt"):
        pipe.run_llm(item.id)
    assert not (svc.layout.ready / item.id).exists()
    done = svc.inbox.load(item.id)
    assert done.state == "erledigt"
    assert all(s["source"] != "llm" for s in done.suggestions)


# -- retry and refresh ---------------------------------------------------------------------------------


def test_retry_of_a_failed_item_starts_over_with_new_options(svc, pipe, ocr, samples, tmp_path):
    ocr.error = ProcessingError("PDF ist verschlüsselt")
    item = new_item(svc, samples, tmp_path)
    assert pipe.process(item.id).state == "fehler"
    calls_before = ocr.calls

    retried = pipe.retry(item.id, skip_ocr=True, skip_blank_removal=True)

    assert retried.state == "arbeit" and retried.stage == "received" and retried.error is None
    assert retried.options == {"skip_ocr": True, "skip_blank_removal": True}
    assert not retried.work_dir.exists() and not retried.document_path.exists()
    assert [job.key for job in svc.index.pending_jobs("process")] == [item.id]
    assert svc.worker.kicks >= 1
    assert "Erneute Verarbeitung angestoßen (ohne Leerseitenerkennung, ohne OCR)" in svc.inbox.read_log(retried)

    loaded = pipe.process(item.id)
    assert loaded.state == "bereit"
    assert ocr.calls == calls_before


def test_retry_of_a_ready_item_removes_derived_files(svc, pipe, samples, tmp_path):
    item = new_item(svc, samples, tmp_path)
    pipe.process(item.id)

    retried = pipe.retry(item.id)

    assert retried.state == "arbeit"
    assert not retried.document_path.exists() and not retried.text_path.exists()
    assert retried.suggestions == [] and retried.analysis is None
    assert retried.original_paths()[0].is_file()
    assert pipe.process(item.id).state == "bereit"


def test_retry_is_refused_while_processing_or_filing(svc, pipe, samples, tmp_path):
    item = new_item(svc, samples, tmp_path)
    with pytest.raises(Conflict):
        pipe.retry(item.id)  # still in arbeit
    pipe.process(item.id)
    loaded = svc.inbox.load(item.id)
    loaded.filing = {"state": "publishing", "doc_id": "x"}
    svc.inbox.save(loaded)
    with pytest.raises(Conflict):
        pipe.retry(item.id)
    with pytest.raises(NotFound):
        pipe.retry("20990101-000000-abcdef")


def test_refresh_recomputes_suggestions_and_keeps_the_llm_answer(svc, pipe, samples, tmp_path):
    svc.settings = svc.settings.with_overrides(llm=LlmConfig(url="http://llm.invalid", model="test"))
    item = new_item(svc, samples, tmp_path)
    pipe.process(item.id)
    pipe.run_llm(item.id)
    svc.suggester.result = [Suggestion(folder="Neu/Regel", score=1.0, source="rule")]

    loaded = pipe.refresh_suggestions(item.id)

    assert [s["folder"] for s in loaded.suggestions] == ["Neu/Regel", "KI/Vorschlag"]
    assert loaded.analysis["sender"] == "Stadtwerke"
    assert svc.inbox.load(item.id).suggestions == loaded.suggestions
    assert "Vorschläge neu berechnet" in svc.inbox.read_log(loaded)


def test_refresh_requires_a_ready_item(svc, pipe, samples, tmp_path):
    item = new_item(svc, samples, tmp_path)
    with pytest.raises(Conflict):
        pipe.refresh_suggestions(item.id)
    with pytest.raises(NotFound):
        pipe.refresh_suggestions("20990101-000000-abcdef")


# -- with the real collaborators ---------------------------------------------------------------------


def test_real_analysis_module(svc, pipe, samples, tmp_path, monkeypatch):
    pytest.importorskip("docushelf.analysis.extract")
    monkeypatch.setattr(pipeline_module, "_analyze", REAL["_analyze"])
    item = new_item(
        svc,
        samples,
        tmp_path,
        ["Stadtwerke Musterstadt GmbH\nMusterweg 1, 01234 Musterstadt\n\nRechnung\nRechnungsdatum: 02.11.2026\n"
         "Rechnungsbetrag: 48,20 EUR"],
    )

    loaded = pipe.process(item.id)

    assert loaded.state == "bereit"
    assert loaded.analysis["doc_date"] == "2026-11-02"
    assert loaded.analysis["sender"]
    assert "Analyse:" in svc.inbox.read_log(loaded)


def test_processed_item_is_accepted_by_the_filing_transaction(svc, pipe, samples, tmp_path):
    ops_module = pytest.importorskip("docushelf.archive.ops")
    trash_module = pytest.importorskip("docushelf.archive.trash")
    from docushelf.pdf.xmp import read_meta

    svc.trash = trash_module.Trash(svc)
    svc.ops = ops_module.ArchiveOps(svc)
    source = samples.text_pdf(tmp_path / "rechnung.pdf", [TEXT])
    item = svc.inbox.create("upload", [source], move=False)
    pipe.process(item.id)

    record = svc.ops.file_item(
        item.id, FilingRequest(folder="Wohnung/Strom", title="Stromrechnung", doc_date=date(2026, 11, 2)), user="test"
    )

    filed = svc.layout.archive / record.path
    assert record.path == "Wohnung/Strom/2026-11-02 Stromrechnung.pdf"
    assert read_meta(filed).origin_hash == item.origin_hash
    assert svc.inbox.load(item.id).state == "erledigt"
    with pytest.raises(Conflict, match="bereits abgelegt"):
        pipe.refresh_suggestions(item.id)
    with pytest.raises(Conflict, match="bereits abgelegt"):
        pipe.retry(item.id)

    # The same original arriving again is recognised as already archived.
    again = svc.inbox.create("mail", [source], move=False)
    assert pipe.process(again.id).duplicate_of["path"] == record.path

    # Deleting a processed item goes through the trash and leaves no ghost behind.
    svc.ops.delete_inbox_item(again.id, user="test")
    assert svc.inbox.find(again.id) is None
    with pytest.raises(NotFound):
        pipe.retry(again.id)
    assert not (svc.layout.ready / again.id).exists()


def test_end_to_end_with_the_real_pdf_modules(svc, pipe, samples, tmp_path, monkeypatch):
    pytest.importorskip("docushelf.pdf.normalize")
    pytest.importorskip("docushelf.pdf.ocr")
    for name, function in REAL.items():
        monkeypatch.setattr(pipeline_module, name, function)
    svc.settings = svc.settings.with_overrides(ocr_enabled=False)
    letter = samples.text_pdf(tmp_path / "brief.pdf", [TEXT, ""])
    photo = samples.image(tmp_path / "beilage.jpg", ["Beilage zur Rechnung", "Seite 3"])
    item = svc.inbox.create("upload", [letter, photo])

    loaded = pipe.process(item.id)

    assert loaded.state == "bereit", svc.inbox.read_log(loaded)
    assert loaded.pages_removed == [2]
    assert loaded.page_count == 2
    assert "Stadtwerke" in svc.inbox.read_text(loaded)[0]
    assert "OCR übersprungen" in svc.inbox.read_log(loaded)


def test_end_to_end_with_real_rules_and_suggestion_engine(svc, samples, tmp_path, monkeypatch):
    engine_module = pytest.importorskip("docushelf.suggest.engine")
    rules_module = pytest.importorskip("docushelf.suggest.rules")
    pytest.importorskip("docushelf.pdf.normalize")
    pytest.importorskip("docushelf.pdf.ocr")
    for name, function in REAL.items():
        monkeypatch.setattr(pipeline_module, name, function)
    svc.settings = svc.settings.with_overrides(ocr_enabled=False)
    svc.layout.rules_file.write_text(
        "rules:\n"
        "  - name: Strom\n"
        "    match:\n"
        "      sender: [Stadtwerke Musterstadt]\n"
        "    action:\n"
        "      folder: Wohnung/Strom\n"
        "      title: \"Stromrechnung {date:%m/%Y}\"\n",
        encoding="utf-8",
    )
    svc.rules = rules_module.RuleStore(svc.layout.rules_file)
    svc.suggester = engine_module.SuggestionEngine(svc)
    pipe = Pipeline(svc)
    letter = samples.text_pdf(
        tmp_path / "brief.pdf",
        ["Stadtwerke Musterstadt GmbH\nMusterweg 1, 01234 Musterstadt\n\nRechnung\n"
         "Rechnungsdatum: 02.11.2026\nRechnungsbetrag: 48,20 EUR"],
    )
    item = svc.inbox.create("upload", [letter])

    loaded = pipe.process(item.id)

    assert loaded.state == "bereit", svc.inbox.read_log(loaded)
    top = loaded.suggestions[0]
    assert top["folder"] == "Wohnung/Strom" and top["source"] == "rule"
    assert top["title"] and top["doc_date"] == "2026-11-02"
    assert all(isinstance(s, dict) for s in loaded.suggestions)
    assert "bester: „Wohnung/Strom“" in svc.inbox.read_log(loaded)
    assert pipe.refresh_suggestions(item.id).suggestions[0]["folder"] == "Wohnung/Strom"


# -- queued refreshes ------------------------------------------------------------------------------


def _queued_refresh_keys(services) -> set[str]:
    return {job.key for job in services.index.pending_jobs("refresh")}


def test_queue_refresh_queues_every_ready_item_once(svc, pipe, samples, tmp_path):
    first = new_item(svc, samples, tmp_path, name="a.pdf")
    second = new_item(svc, samples, tmp_path, name="b.pdf")
    pipe.process(first.id)
    pipe.process(second.id)
    kicks = svc.worker.kicks

    assert pipe.queue_refresh() == 2
    assert _queued_refresh_keys(svc) == {first.id, second.id}
    assert svc.worker.kicks > kicks
    assert pipe.queue_refresh() == 0  # already queued


def test_queue_refresh_only_stale_picks_suggestions_whose_folder_disappeared(svc, pipe, samples, tmp_path):
    item = new_item(svc, samples, tmp_path)
    pipe.process(item.id)  # first suggestion: existing folder "Wohnung/Strom"
    svc.index.add_folder_with_parents("Wohnung/Strom")
    assert pipe.queue_refresh(only_stale=True) == 0

    svc.index.remove_folder_tree("Wohnung/Strom")  # e.g. renamed in the Finder
    assert pipe.queue_refresh(only_stale=True) == 1
    assert _queued_refresh_keys(svc) == {item.id}


def test_queue_refresh_only_stale_decides_by_the_folders_existing_now(svc, pipe, samples, tmp_path):
    # The new_folder flag stored with a suggestion does not count: a proposed new folder
    # (e.g. from the Paperless import) may have to follow a renamed parent folder.
    svc.suggester.result = [Suggestion(folder="Ganz/Neu", score=0.4, source="heuristic", new_folder=True)]
    item = new_item(svc, samples, tmp_path)
    pipe.process(item.id)
    assert pipe.queue_refresh(only_stale=True) == 1
    assert _queued_refresh_keys(svc) == {item.id}
    for job in svc.index.pending_jobs("refresh"):
        svc.index.complete(job.id)

    # Created meanwhile (spelled differently): the suggestion is not stale, whatever its flag says.
    svc.index.add_folder_with_parents("ganz/neu")
    assert pipe.queue_refresh(only_stale=True) == 0


def test_queue_refresh_skips_items_whose_filing_is_unfinished(svc, pipe, samples, tmp_path):
    item = new_item(svc, samples, tmp_path)
    pipe.process(item.id)
    loaded = svc.inbox.load(item.id)
    loaded.filing = {"state": "publishing", "doc_id": "x"}
    svc.inbox.save(loaded)
    assert pipe.queue_refresh() == 0
    assert pipe.queue_refresh(only_stale=True) == 0
