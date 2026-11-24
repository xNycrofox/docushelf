"""Tests for the background worker: job loop, retries, stop behaviour, maintenance."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from docushelf.errors import Conflict, NotFound, ProcessingError
from docushelf.inbox import worker as worker_module
from docushelf.inbox.worker import Worker


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class FakePipeline:
    """Records calls; ``actions[key]`` is called with the job kind (may raise or block)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.actions: dict[str, object] = {}
        self.failed: list[tuple[str, BaseException]] = []
        self.manual_refreshes: list[bool] = []
        self._lock = threading.Lock()

    def _run(self, kind: str, key: str) -> None:
        with self._lock:
            self.calls.append((kind, key))
        action = self.actions.get(key)
        if action is not None:
            action(kind)

    def process(self, key: str) -> None:
        self._run("process", key)

    def run_llm(self, key: str) -> None:
        self._run("llm_suggest", key)

    def refresh_suggestions(self, key: str, *, manual: bool = True) -> None:
        self.manual_refreshes.append(manual)
        self._run("refresh", key)

    def auto_file(self, key: str) -> None:
        self._run("auto_file", key)

    def fail(self, key: str, exc: BaseException) -> None:
        self.failed.append((key, exc))


class FakeTrash:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def purge_expired(self) -> int:
        self.calls.append("purge_expired")
        return 2

    def purge_done_items(self) -> int:
        self.calls.append("purge_done_items")
        return 1


@pytest.fixture
def svc(base_services):
    base_services.pipeline = FakePipeline()
    base_services.trash = FakeTrash()
    return base_services


@pytest.fixture
def make_worker(svc):
    workers: list[Worker] = []

    def factory(**kwargs) -> Worker:
        kwargs.setdefault("idle_timeout", 5.0)
        kwargs.setdefault("maintenance_interval", 3600.0)
        kwargs.setdefault("resume_interval", None)
        worker = Worker(svc, **kwargs)
        workers.append(worker)
        return worker

    yield factory
    for worker in workers:
        worker.stop()


def jobs(svc, state: str | None = None) -> list:
    rows = svc.index.conn().execute("SELECT kind, key, state, attempts, run_after FROM jobs ORDER BY id").fetchall()
    return [dict(row) for row in rows if state is None or row["state"] == state]


# -- running jobs -----------------------------------------------------------------------------


def test_queued_jobs_are_dispatched_to_the_pipeline(svc, make_worker):
    svc.index.enqueue("process", "a")
    svc.index.enqueue("llm_suggest", "b")
    svc.index.enqueue("refresh", "c")
    worker = make_worker()
    worker.start()

    assert wait_until(lambda: len(svc.pipeline.calls) == 3)
    assert sorted(svc.pipeline.calls) == [("llm_suggest", "b"), ("process", "a"), ("refresh", "c")]
    assert wait_until(lambda: jobs(svc) == [])


def test_kick_wakes_an_idle_worker(svc, make_worker):
    worker = make_worker(idle_timeout=60.0)
    worker.start()
    time.sleep(0.1)  # the worker is idle now and would sleep for a minute

    svc.index.enqueue("process", "a")
    started = time.monotonic()
    worker.kick()

    assert wait_until(lambda: svc.pipeline.calls == [("process", "a")], timeout=2.0)
    assert time.monotonic() - started < 2.0


def test_delayed_job_runs_when_due_without_a_kick(svc, make_worker):
    svc.index.enqueue("process", "later", delay=0.3)
    worker = make_worker(idle_timeout=60.0)
    worker.start()

    time.sleep(0.1)
    assert svc.pipeline.calls == []
    assert wait_until(lambda: svc.pipeline.calls == [("process", "later")], timeout=3.0)


def test_several_threads_run_jobs_in_parallel(svc, make_worker):
    svc.settings = svc.settings.with_overrides(worker_threads=2)
    release = threading.Event()
    svc.pipeline.actions["a"] = lambda kind: release.wait(5)
    svc.pipeline.actions["b"] = lambda kind: release.wait(5)
    svc.index.enqueue("process", "a")
    svc.index.enqueue("process", "b")
    worker = make_worker()
    worker.start()

    assert wait_until(lambda: len(worker.status()["current_jobs"]) == 2)
    status = worker.status()
    assert status["threads"] == 2 and status["threads_alive"] == 2
    assert sorted(job["key"] for job in status["current_jobs"]) == ["a", "b"]
    release.set()
    assert wait_until(lambda: jobs(svc) == [])


# -- errors and retries ----------------------------------------------------------------------------


def test_transient_errors_are_retried_and_finally_given_up(svc, make_worker):
    def timeout(kind):
        raise ProcessingError("Zeitüberschreitung bei der Texterkennung", transient=True)

    svc.pipeline.actions["a"] = timeout
    svc.index.enqueue("process", "a")
    worker = make_worker(retry_delays=(0.05, 0.05, 0.05))
    worker.start()

    assert wait_until(lambda: len(svc.pipeline.failed) == 1)
    assert svc.pipeline.calls == [("process", "a")] * 4  # first run plus three retries
    key, exc = svc.pipeline.failed[0]
    assert key == "a" and isinstance(exc, ProcessingError) and exc.transient
    assert wait_until(lambda: jobs(svc) == [])


def test_first_retry_waits_thirty_seconds(svc, make_worker, samples, tmp_path):
    item = svc.inbox.create("upload", [samples.text_pdf(tmp_path / "a.pdf", ["x"])])

    def timeout(kind):
        raise ProcessingError("Zeitüberschreitung", transient=True)

    svc.pipeline.actions[item.id] = timeout
    svc.index.enqueue("process", item.id)
    worker = make_worker()
    worker.start()

    assert wait_until(lambda: [j["state"] for j in jobs(svc)] == ["queued"] and svc.pipeline.calls)
    job = jobs(svc)[0]
    assert job["attempts"] == 1
    assert 25 < job["run_after"] - time.time() <= 30
    assert worker.retry_delays == (30.0, 120.0, 600.0)
    assert "Neuer Versuch in 30 s (Wiederholung 1 von 3)" in svc.inbox.read_log(svc.inbox.load(item.id))
    assert svc.pipeline.failed == []


def test_transient_error_followed_by_success(svc, make_worker):
    outcomes = [ProcessingError("gesperrt", transient=True)]

    def flaky(kind):
        if outcomes:
            raise outcomes.pop()

    svc.pipeline.actions["a"] = flaky
    svc.index.enqueue("process", "a")
    worker = make_worker(retry_delays=(0.05, 0.05, 0.05))
    worker.start()

    assert wait_until(lambda: len(svc.pipeline.calls) == 2 and jobs(svc) == [])
    assert svc.pipeline.failed == []


def test_vanished_item_completes_the_job(svc, make_worker):
    def gone(kind):
        raise NotFound("a")

    svc.pipeline.actions["a"] = gone
    svc.index.enqueue("process", "a")
    worker = make_worker()
    worker.start()

    assert wait_until(lambda: svc.pipeline.calls and jobs(svc) == [])
    assert svc.pipeline.failed == []


def test_unexpected_error_in_a_process_job_fails_the_item(svc, make_worker):
    def broken(kind):
        raise RuntimeError("kaputt")

    svc.pipeline.actions["a"] = broken
    svc.index.enqueue("process", "a")
    worker = make_worker()
    worker.start()

    assert wait_until(lambda: len(svc.pipeline.failed) == 1 and jobs(svc) == [])
    assert worker.status()["last_error"] == "Unerwarteter Fehler (RuntimeError): kaputt"


def test_failing_llm_job_does_not_touch_the_item(svc, make_worker):
    def broken(kind):
        raise RuntimeError("Modell antwortet nicht")

    svc.pipeline.actions["a"] = broken
    svc.index.enqueue("llm_suggest", "a")
    worker = make_worker()
    worker.start()

    assert wait_until(lambda: [j["state"] for j in jobs(svc)] == ["failed"])
    assert svc.pipeline.failed == []


def test_queued_refresh_is_not_a_manual_one(svc, make_worker):
    svc.index.enqueue("refresh", "a")
    make_worker().start()
    assert wait_until(lambda: svc.pipeline.calls == [("refresh", "a")] and jobs(svc) == [])
    assert svc.pipeline.manual_refreshes == [False]


# -- automatic filing ---------------------------------------------------------------------------------


def test_auto_file_jobs_are_dispatched(svc, make_worker):
    svc.index.enqueue("auto_file", "a")
    make_worker().start()
    assert wait_until(lambda: svc.pipeline.calls == [("auto_file", "a")] and jobs(svc) == [])


def test_llm_job_asking_for_it_queues_the_automatic_filing_after_its_answer(svc, make_worker):
    svc.index.enqueue("llm_suggest", "a", {"auto_file": True})
    make_worker().start()
    assert wait_until(lambda: svc.pipeline.calls == [("llm_suggest", "a"), ("auto_file", "a")])
    assert wait_until(lambda: jobs(svc) == [])


def test_llm_job_without_the_request_queues_nothing(svc, make_worker):
    svc.index.enqueue("llm_suggest", "a")
    make_worker().start()
    assert wait_until(lambda: svc.pipeline.calls == [("llm_suggest", "a")] and jobs(svc) == [])
    time.sleep(0.1)
    assert svc.pipeline.calls == [("llm_suggest", "a")]


def test_failing_llm_job_still_queues_the_automatic_filing(svc, make_worker):
    def broken(kind):
        if kind == "llm_suggest":
            raise RuntimeError("Modell antwortet nicht")

    svc.pipeline.actions["a"] = broken
    svc.index.enqueue("llm_suggest", "a", {"auto_file": True})
    make_worker().start()

    assert wait_until(lambda: ("auto_file", "a") in svc.pipeline.calls)
    assert wait_until(lambda: [(j["kind"], j["state"]) for j in jobs(svc)] == [("llm_suggest", "failed")])
    assert svc.pipeline.failed == []


def test_llm_job_with_transient_errors_queues_the_filing_only_at_the_end(svc, make_worker):
    outcomes = [ProcessingError("Modell lädt noch", transient=True)] * 2

    def flaky(kind):
        if kind == "llm_suggest" and outcomes:
            raise outcomes.pop()

    svc.pipeline.actions["a"] = flaky
    svc.index.enqueue("llm_suggest", "a", {"auto_file": True})
    make_worker(retry_delays=(0.05, 0.05, 0.05)).start()

    assert wait_until(lambda: ("auto_file", "a") in svc.pipeline.calls)
    assert svc.pipeline.calls == [("llm_suggest", "a")] * 3 + [("auto_file", "a")]
    assert wait_until(lambda: jobs(svc) == [])


def test_llm_job_giving_up_after_retries_queues_the_filing(svc, make_worker):
    def always(kind):
        if kind == "llm_suggest":
            raise ProcessingError("Modell lädt noch", transient=True)

    svc.pipeline.actions["a"] = always
    svc.index.enqueue("llm_suggest", "a", {"auto_file": True})
    make_worker(retry_delays=(0.05, 0.05)).start()

    assert wait_until(lambda: ("auto_file", "a") in svc.pipeline.calls)
    assert svc.pipeline.calls == [("llm_suggest", "a")] * 3 + [("auto_file", "a")]


@pytest.mark.parametrize("error", [NotFound("a"), Conflict("Das Dokument wurde bereits abgelegt")])
def test_llm_job_for_an_item_that_left_the_inbox_queues_nothing(svc, make_worker, error):
    def gone(kind):
        raise error

    svc.pipeline.actions["a"] = gone
    svc.index.enqueue("llm_suggest", "a", {"auto_file": True})
    make_worker().start()

    assert wait_until(lambda: svc.pipeline.calls and jobs(svc) == [])
    time.sleep(0.1)
    assert svc.pipeline.calls == [("llm_suggest", "a")]


def test_auto_file_job_is_never_retried(svc, make_worker):
    def unreachable(kind):
        raise ProcessingError("Das Archiv ist nicht erreichbar.", transient=True)

    svc.pipeline.actions["a"] = unreachable
    svc.index.enqueue("auto_file", "a")
    make_worker(retry_delays=(0.05, 0.05, 0.05)).start()

    assert wait_until(lambda: [j["state"] for j in jobs(svc)] == ["failed"])
    time.sleep(0.2)
    assert svc.pipeline.calls == [("auto_file", "a")]
    assert svc.pipeline.failed == []  # the item is never moved to the failed ones


def test_job_interrupted_again_and_again_is_given_up(svc, make_worker):
    svc.index.enqueue("process", "a")
    svc.index.conn().execute("UPDATE jobs SET attempts = 5")  # five crashes during processing
    worker = make_worker()
    worker.start()

    assert wait_until(lambda: len(svc.pipeline.failed) == 1)
    assert svc.pipeline.calls == []
    assert "wiederholt unterbrochen" in str(svc.pipeline.failed[0][1])
    assert wait_until(lambda: jobs(svc) == [])


# -- lifecycle ----------------------------------------------------------------------------------------


def test_stop_returns_quickly_while_a_job_runs(svc, make_worker):
    release = threading.Event()
    running = threading.Event()

    def slow(kind):
        running.set()
        release.wait(10)

    svc.pipeline.actions["a"] = slow
    svc.index.enqueue("process", "a")
    worker = make_worker()
    worker.start()
    assert running.wait(5)

    started = time.monotonic()
    worker.stop()
    assert time.monotonic() - started < 1.0
    assert worker.status()["running"] is False

    svc.index.enqueue("process", "b")
    release.set()
    # The running job finishes and is recorded; nothing new is started.
    assert wait_until(lambda: [j["key"] for j in jobs(svc)] == ["b"])
    time.sleep(0.2)
    assert svc.pipeline.calls == [("process", "a")]
    assert [job["state"] for job in jobs(svc)] == ["queued"]


def test_worker_can_be_restarted(svc, make_worker):
    worker = make_worker()
    worker.start()
    worker.start()  # idempotent
    worker.stop()
    worker.stop()  # idempotent
    svc.index.enqueue("process", "a")
    worker.start()
    worker.kick()
    assert wait_until(lambda: svc.pipeline.calls == [("process", "a")])
    assert worker.status()["threads_alive"] == 1


def test_restart_while_an_old_job_is_still_finishing(svc, make_worker):
    release = threading.Event()
    running = threading.Event()

    def slow(kind):
        running.set()
        release.wait(10)

    svc.pipeline.actions["a"] = slow
    svc.index.enqueue("process", "a")
    worker = make_worker()
    worker.start()
    assert running.wait(5)
    worker.stop()
    worker.start()

    svc.index.enqueue("process", "b")
    worker.kick()
    assert wait_until(lambda: ("process", "b") in svc.pipeline.calls)
    assert [job["key"] for job in worker.status()["current_jobs"]] == ["a"]
    release.set()
    assert wait_until(lambda: jobs(svc) == [] and worker.status()["current_jobs"] == [])
    assert worker.status()["threads_alive"] == 1
    assert svc.pipeline.calls == [("process", "a"), ("process", "b")]


def test_resume_pending_queues_items_in_work(svc, make_worker, samples, tmp_path):
    def new(name: str, state: str):
        item = svc.inbox.create("upload", [samples.text_pdf(tmp_path / f"{name}.pdf", [name])])
        return svc.inbox.move(item, state).id

    in_work = sorted([new("a", "arbeit"), new("b", "arbeit")])
    new("c", "bereit")
    new("d", "fehler")
    worker = make_worker()

    assert worker.resume_pending() == 2
    assert sorted(j["key"] for j in jobs(svc)) == in_work
    assert worker.resume_pending() == 0


# -- maintenance ---------------------------------------------------------------------------------------


def test_maintenance_thread_purges_trash_and_prunes_thumbnails(svc, make_worker, monkeypatch):
    pruned: list[Path] = []
    monkeypatch.setattr(worker_module, "_prune_thumbnails", lambda cache: pruned.append(cache) or 3)
    worker = make_worker(maintenance_interval=0.05)
    worker.start()

    assert wait_until(lambda: len(svc.trash.calls) == 2 and pruned)
    time.sleep(0.3)  # several more ticks: nothing is due again within the hour/day
    assert svc.trash.calls == ["purge_expired", "purge_done_items"]
    assert pruned == [svc.layout.cache]
    assert worker.status()["last_maintenance_at"]


def test_maintenance_schedule(svc, make_worker, monkeypatch):
    class Clock:
        now = 0.0

        def __call__(self):
            return self.now

    clock = Clock()
    pruned: list[Path] = []
    monkeypatch.setattr(worker_module, "_prune_thumbnails", lambda cache: pruned.append(cache) or 0)
    worker = make_worker(clock=clock, resume_interval=7200)

    first = worker.run_maintenance()
    assert first["trash_purged"] == 2 and first["done_items_purged"] == 1 and len(pruned) == 1
    clock.now = 3599
    assert worker.run_maintenance() == {}
    clock.now = 3601
    assert set(worker.run_maintenance()) == {"trash_purged", "done_items_purged", "ocr_workdirs_removed"}
    clock.now = 7201
    assert set(worker.run_maintenance()) == {"trash_purged", "done_items_purged", "ocr_workdirs_removed", "resumed"}
    assert len(pruned) == 1
    clock.now = 24 * 3600 + 1
    assert "thumbnails_pruned" in worker.run_maintenance()
    assert len(pruned) == 2
    assert set(worker.run_maintenance(force=True)) == {
        "trash_purged",
        "done_items_purged",
        "ocr_workdirs_removed",
        "thumbnails_pruned",
        "resumed",
    }


def test_maintenance_requeues_items_whose_job_was_lost(svc, make_worker, samples, tmp_path):
    item = svc.inbox.create("upload", [samples.text_pdf(tmp_path / "a.pdf", ["x"])])
    worker = make_worker(maintenance_interval=0.05, resume_interval=0.1)
    worker.start()

    assert wait_until(lambda: ("process", item.id) in svc.pipeline.calls)


# -- with the real collaborators ---------------------------------------------------------------------


def test_maintenance_with_the_real_trash_and_thumbnail_cache(svc, make_worker, samples, tmp_path):
    trash_module = pytest.importorskip("docushelf.archive.trash")
    pytest.importorskip("docushelf.pdf.render")
    svc.trash = trash_module.Trash(svc)
    old = svc.inbox.create("upload", [samples.text_pdf(tmp_path / "alt.pdf", ["alt"])])
    old.filing = {"state": "done", "finished_at": "2020-01-01T00:00:00+01:00"}
    svc.inbox.save(old)
    svc.inbox.move(old, "erledigt")
    recent = svc.inbox.create("upload", [samples.text_pdf(tmp_path / "neu.pdf", ["neu"])])
    svc.inbox.move(recent, "erledigt")

    results = make_worker().run_maintenance(force=True)

    assert results["done_items_purged"] == 1
    assert results["trash_purged"] == 0
    assert results["thumbnails_pruned"] == 0
    assert svc.inbox.find(old.id) is None
    assert svc.inbox.find(recent.id) is not None


def _copying_ocr(src, dst, settings, *, log=None):
    import shutil
    from types import SimpleNamespace

    shutil.copyfile(src, dst)
    return SimpleNamespace(performed=False, output=dst, skipped_reason="ausgeschaltet", warnings=[], duration_s=0.0)


def _copying_normalize(files, out):
    import pikepdf

    with pikepdf.open(files[0]) as pdf:
        pdf.save(out)
        return len(pdf.pages)


def test_drop_zone_to_ready_through_intake_worker_and_pipeline(base_services, samples, monkeypatch):
    from docushelf.inbox import pipeline as pipeline_module
    from docushelf.inbox.intake import Intake
    from docushelf.inbox.pipeline import Pipeline

    monkeypatch.setattr(pipeline_module, "_normalize_to_pdf", _copying_normalize)
    monkeypatch.setattr(pipeline_module, "_run_ocr", _copying_ocr)
    monkeypatch.setattr(pipeline_module, "_detect_blank_pages", lambda *args, **kwargs: [])
    base_services.pipeline = Pipeline(base_services)
    base_services.worker = worker = Worker(
        base_services, idle_timeout=60.0, maintenance_interval=3600.0, resume_interval=None
    )
    intake = Intake(base_services)
    worker.start()
    try:
        drop = base_services.layout.source_dir("upload")
        samples.text_pdf(drop / "Rechnung.pdf", ["Stadtwerke Musterstadt\nRechnung vom 02.11.2026"])
        (created,) = intake.scan_once()

        assert wait_until(lambda: base_services.inbox.find(created, states=("bereit",)) is not None)
        item = base_services.inbox.load(created)
        assert item.stage == "ready" and item.document_path.is_file()
        assert wait_until(lambda: jobs(base_services) == [])
    finally:
        worker.stop()


@pytest.fixture
def full_services(settings):
    try:
        from docushelf.services import Services

        instance = Services(settings)
    except ImportError as exc:
        pytest.skip(f"service container not complete yet: {exc}")
    yield instance
    instance.stop()
    instance.index.close()


def test_full_service_container_processes_an_upload(full_services, samples):
    pytest.importorskip("docushelf.pdf.normalize")
    pytest.importorskip("docushelf.pdf.ocr")
    services = full_services
    services.worker.start()
    try:
        drop = services.layout.source_dir("upload")
        samples.text_pdf(drop / "Rechnung.pdf", ["Stadtwerke Musterstadt\nRechnung vom 02.11.2026"])
        (created,) = services.intake.scan_once()
        assert wait_until(lambda: services.inbox.find(created, states=("bereit", "fehler")) is not None, 30)
        item = services.inbox.load(created)
        assert item.state == "bereit", services.inbox.read_log(item)
        assert item.suggestions
    finally:
        services.worker.stop()
