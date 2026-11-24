"""Archive watcher: initial pass, live updates through a real observer,
debouncing, event filtering, polling fallback, periodic passes and shutdown."""

from __future__ import annotations

import logging
import os
import threading
import time
from types import SimpleNamespace

import pytest
from watchdog.events import (
    DirModifiedEvent,
    EventsLostEvent,
    FileClosedEvent,
    FileClosedNoWriteEvent,
    FileCreatedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileOpenedEvent,
)

from docushelf.errors import DocuShelfError
from docushelf.index.reconcile import Reconciler
from docushelf.index.watcher import ArchiveWatcher
from docushelf.models import ScanStats


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class FakeReconciler:
    def __init__(self):
        self.calls: list[tuple[float, bool]] = []
        self.error: Exception | None = None
        self.block: threading.Event | None = None
        self.cancel_seen: threading.Event | None = None

    def scan(self, *, full: bool = True, verify: bool = False, cancel: threading.Event | None = None) -> ScanStats:
        self.calls.append((time.monotonic(), full))
        if self.block is not None:
            self.cancel_seen = cancel
            while not self.block.is_set() and not (cancel is not None and cancel.is_set()):
                time.sleep(0.01)
        if self.error is not None:
            raise self.error
        return ScanStats(full=full, files_seen=len(self.calls))


class FakeObserver:
    """Stands in for a watchdog observer; tests feed events through ``emit``."""

    def __init__(self):
        self.handlers = []
        self.alive = False
        self.stopped = False

    def schedule(self, handler, path, recursive=False):
        self.handlers.append((handler, path, recursive))

    def start(self):
        self.alive = True

    def stop(self):
        self.alive = False
        self.stopped = True

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return self.alive

    def emit(self, event):
        for handler, _, _ in self.handlers:
            handler.dispatch(event)


@pytest.fixture
def fake(base_services):
    """A watcher with a fake reconciler and a fake observer; fast timings."""
    reconciler = FakeReconciler()
    base_services.reconciler = reconciler
    observers: list[FakeObserver] = []

    def factory():
        observer = FakeObserver()
        observers.append(observer)
        return observer

    watcher = ArchiveWatcher(base_services, quiet=0.3, max_delay=1.0, poll_fallback=0.2, interval=0, observer_factory=factory)
    watcher.health_interval = 0.05
    ns = SimpleNamespace(watcher=watcher, reconciler=reconciler, observers=observers, root=str(base_services.layout.archive))
    yield ns
    watcher.stop()


def start_and_wait_for_initial_pass(ns) -> None:
    ns.watcher.start()
    assert wait_until(lambda: len(ns.reconciler.calls) == 1 and ns.watcher.last_result is not None)
    assert ns.reconciler.calls[0][1] is True  # the first pass is a full one


def path(ns, *parts: str) -> str:
    return os.path.join(ns.root, *parts)


# -- with a real observer and the real reconciler -------------------------------------------------------------


def test_live_updates_with_a_real_observer(base_services, samples, tmp_path):
    archive = base_services.layout.archive
    samples.text_pdf(archive / "Bank" / "2025-01-31 Kontoauszug.pdf", ["Kontoauszug Januar"])
    base_services.reconciler = Reconciler(base_services)
    watcher = ArchiveWatcher(base_services, quiet=0.2, max_delay=2.0, poll_fallback=30.0, interval=0)
    try:
        watcher.start()
        assert wait_until(lambda: watcher.last_result is not None, timeout=10)
        assert watcher.last_result.added == 1 and watcher.last_error is None
        assert watcher.observer_running and not watcher.polling

        # A new file (written elsewhere and moved in, like most clients do).
        raw = samples.text_pdf(tmp_path / "upload.pdf", ["Rechnung Stadtwerke"])
        (archive / "Wohnung").mkdir()
        os.replace(raw, archive / "Wohnung" / "2025-02-01 Stadtwerke.pdf")
        assert wait_until(lambda: base_services.index.get_by_path("Wohnung/2025-02-01 Stadtwerke.pdf") is not None, 10)

        # A rename keeps the id.
        doc_id = base_services.index.get_by_path("Bank/2025-01-31 Kontoauszug.pdf").id
        os.rename(archive / "Bank" / "2025-01-31 Kontoauszug.pdf", archive / "Bank" / "2025-01-31 Auszug.pdf")
        assert wait_until(lambda: base_services.index.get(doc_id).path == "Bank/2025-01-31 Auszug.pdf", 10)

        # A deletion and an empty folder.
        (archive / "Wohnung" / "2025-02-01 Stadtwerke.pdf").unlink()
        (archive / "Leer").mkdir()
        assert wait_until(lambda: base_services.index.count() == 1 and "Leer" in base_services.index.list_folders(), 10)
    finally:
        watcher.stop()
    assert not watcher.running and not watcher.observer_running


def test_scan_errors_are_reported_and_cleared(base_services):
    base_services.reconciler = Reconciler(base_services)
    watcher = ArchiveWatcher(base_services, quiet=0.1, max_delay=0.5, poll_fallback=30.0, interval=0)
    archive = base_services.layout.archive
    try:
        watcher.start()
        assert wait_until(lambda: watcher.last_scan_at is not None, 10)
        assert watcher.last_error is None
        moved = archive.with_name("archiv-weg")
        os.rename(archive, moved)
        try:
            watcher.request_scan("Test")
            assert wait_until(lambda: watcher.last_error is not None, 10)
            assert "Archivordner" in watcher.last_error
        finally:
            os.rename(moved, archive)
        watcher.request_scan("wieder da")
        assert wait_until(lambda: watcher.last_error is None and not watcher.scanning, 10)
    finally:
        watcher.stop()


# -- debouncing ----------------------------------------------------------------------------------------------------------


def test_a_burst_of_events_leads_to_one_pass_after_quiet(fake):
    start_and_wait_for_initial_pass(fake)
    observer = fake.observers[0]
    first = time.monotonic()
    for name in ("a.pdf", "a.pdf", "b.pdf"):
        observer.emit(FileCreatedEvent(path(fake, name)))
        observer.emit(FileModifiedEvent(path(fake, name)))
    time.sleep(0.15)
    assert len(fake.reconciler.calls) == 1  # still waiting for quiet
    assert wait_until(lambda: len(fake.reconciler.calls) == 2, 2.0)
    started, full = fake.reconciler.calls[1]
    assert started - first >= 0.25 and full is False
    time.sleep(0.5)
    assert len(fake.reconciler.calls) == 2


def test_continuous_events_are_reconciled_after_max_delay(fake):
    start_and_wait_for_initial_pass(fake)
    observer = fake.observers[0]
    first = time.monotonic()
    while time.monotonic() - first < 2.3:
        observer.emit(FileModifiedEvent(path(fake, "wird-geschrieben.pdf")))
        time.sleep(0.05)
    during = [t for t, _ in fake.reconciler.calls[1:] if t - first < 2.3]
    assert len(during) >= 2  # quiet never came, max_delay forced passes
    assert during[0] - first >= 0.9
    assert wait_until(lambda: len(fake.reconciler.calls) >= len(during) + 2, 2.0)  # one more after the burst


def test_irrelevant_events_do_not_trigger_passes(fake):
    start_and_wait_for_initial_pass(fake)
    observer = fake.observers[0]
    for event in (
        FileOpenedEvent(path(fake, "2025-01-01 Rechnung.pdf")),
        FileClosedNoWriteEvent(path(fake, "2025-01-01 Rechnung.pdf")),
        FileCreatedEvent(path(fake, ".DS_Store")),
        FileModifiedEvent(path(fake, "A", "._2025-01-01 Rechnung.pdf")),
        FileCreatedEvent(path(fake, "A", ".docushelf-0123.part")),
        FileCreatedEvent(path(fake, "A", "~$Brief.docx")),
        FileCreatedEvent(path(fake, "@eaDir", "x.pdf")),
        FileMovedEvent(path(fake, "x.tmp"), path(fake, "y.tmp")),
    ):
        observer.emit(event)
    time.sleep(0.7)
    assert len(fake.reconciler.calls) == 1


@pytest.mark.parametrize(
    "make_event",
    [
        lambda ns: FileMovedEvent(path(ns, "A", ".upload.tmp"), path(ns, "A", "2025-01-01 Rechnung.pdf")),
        lambda ns: FileClosedEvent(path(ns, "A", "scan.pdf")),
        lambda ns: DirModifiedEvent(path(ns, "A")),
        lambda ns: EventsLostEvent(ns.root),
    ],
    ids=["moved-into-view", "closed-after-write", "directory", "events-lost"],
)
def test_relevant_events_trigger_a_pass(fake, make_event):
    start_and_wait_for_initial_pass(fake)
    fake.observers[0].emit(make_event(fake))
    assert wait_until(lambda: len(fake.reconciler.calls) == 2, 2.0)


def test_request_scan_runs_soon_and_requests_are_combined(fake):
    start_and_wait_for_initial_pass(fake)
    fake.reconciler.block = threading.Event()
    fake.watcher.request_scan("erste")
    assert wait_until(lambda: len(fake.reconciler.calls) == 2, 1.0)
    for i in range(5):
        fake.watcher.request_scan(f"weitere {i}")
    fake.reconciler.block.set()
    assert wait_until(lambda: len(fake.reconciler.calls) == 3, 1.0)
    time.sleep(0.3)
    assert len(fake.reconciler.calls) == 3
    assert "weitere 0" in fake.watcher.last_reason and "weitere 4" in fake.watcher.last_reason


# -- periodic passes, polling and errors ------------------------------------------------------------------------------


def test_periodic_full_passes(fake):
    fake.watcher.interval = 0.25
    start_and_wait_for_initial_pass(fake)
    assert wait_until(lambda: len(fake.reconciler.calls) >= 3, 2.0)
    assert all(full for _, full in fake.reconciler.calls)


def test_polling_when_the_observer_cannot_start(base_services, caplog):
    reconciler = FakeReconciler()
    base_services.reconciler = reconciler

    def broken():
        raise OSError(28, "inotify watch limit reached")

    watcher = ArchiveWatcher(base_services, quiet=0.1, max_delay=0.5, poll_fallback=0.2, interval=0, observer_factory=broken)
    caplog.set_level(logging.WARNING, logger="docushelf.index.watcher")
    try:
        watcher.start()
        assert wait_until(lambda: len(reconciler.calls) >= 4, 3.0)
        assert watcher.polling and not watcher.observer_running
        assert all(not full for _, full in reconciler.calls[1:])
        assert any("inotify watch limit" in r.getMessage() for r in caplog.records)
    finally:
        watcher.stop()


def test_a_dead_observer_is_replaced_by_polling_and_restarted_later(fake):
    fake.watcher.observer_retry_interval = 0.5
    start_and_wait_for_initial_pass(fake)
    fake.observers[0].alive = False
    assert wait_until(lambda: fake.watcher.polling, 2.0)
    assert wait_until(lambda: len(fake.reconciler.calls) >= 2, 2.0)  # missed events: look right away
    assert wait_until(lambda: not fake.watcher.polling and len(fake.observers) == 2, 3.0)
    assert fake.watcher.observer_running
    calls = len(fake.reconciler.calls)
    fake.observers[1].emit(FileCreatedEvent(path(fake, "neu.pdf")))
    assert wait_until(lambda: len(fake.reconciler.calls) > calls, 2.0)


def test_failed_pass_sets_last_error_and_watcher_keeps_running(fake):
    start_and_wait_for_initial_pass(fake)
    fake.reconciler.error = DocuShelfError("Archivordner ist nicht erreichbar")
    fake.watcher.request_scan()
    assert wait_until(lambda: fake.watcher.last_error == "Archivordner ist nicht erreichbar", 2.0)
    fake.reconciler.error = RuntimeError("kaputt")
    fake.watcher.request_scan()
    assert wait_until(lambda: fake.watcher.last_error == "kaputt", 2.0)
    fake.reconciler.error = None
    fake.watcher.request_scan()
    assert wait_until(lambda: fake.watcher.last_error is None, 2.0)
    assert fake.watcher.running and fake.watcher.status()["last_result"] is not None


# -- lifecycle --------------------------------------------------------------------------------------------------------


def test_start_and_stop_are_idempotent_and_restartable(fake):
    start_and_wait_for_initial_pass(fake)
    fake.watcher.start()
    time.sleep(0.1)
    assert len(fake.reconciler.calls) == 1 and len(fake.observers) == 1
    fake.watcher.stop()
    fake.watcher.stop()
    assert not fake.watcher.running and fake.observers[0].stopped
    fake.watcher.start()
    assert wait_until(lambda: len(fake.reconciler.calls) == 2, 2.0)
    assert fake.reconciler.calls[1][1] is True and len(fake.observers) == 2
    fake.watcher.stop()
    assert not fake.watcher.running


def test_stop_cancels_a_running_pass(fake):
    fake.reconciler.block = threading.Event()  # never set: the pass only ends when cancelled
    fake.watcher.start()
    assert wait_until(lambda: fake.watcher.scanning, 2.0)
    started = time.monotonic()
    fake.watcher.stop()
    assert time.monotonic() - started < 2.0
    assert fake.reconciler.cancel_seen is not None and fake.reconciler.cancel_seen.is_set()
    assert not fake.watcher.running and not fake.watcher.scanning


def test_constructor_does_no_io(base_services):
    calls = []
    watcher = ArchiveWatcher(base_services, observer_factory=lambda: calls.append("observer"))
    assert calls == [] and not watcher.running
    assert watcher.quiet == 2.0 and watcher.max_delay == 20.0 and watcher.poll_fallback == 60.0
    assert watcher.interval == base_services.settings.reconcile_interval
    watcher.stop()  # stopping a watcher that never ran is fine


def test_watcher_started_by_the_service_container(services, samples, tmp_path):
    archive = services.layout.archive
    samples.text_pdf(archive / "Bank" / "2025-01-31 Kontoauszug.pdf", ["Kontoauszug"])
    services.watcher.quiet = 0.2
    services.start()
    try:
        assert wait_until(lambda: services.watcher.last_result is not None, 15)
        assert services.watcher.last_result.added == 1 and services.watcher.last_error is None
        raw = samples.text_pdf(tmp_path / "neu.pdf", ["Rechnung Stadtwerke"])
        os.replace(raw, archive / "Bank" / "2025-02-01 Stadtwerke.pdf")
        assert wait_until(lambda: services.index.get_by_path("Bank/2025-02-01 Stadtwerke.pdf") is not None, 15)
        assert [h.doc.title for h in services.searcher.search("stadtwerke").hits] == ["Stadtwerke"]
    finally:
        services.stop()
    assert not services.watcher.running


# -- inbox suggestions after archive changes ---------------------------------------------------------------


class RecordingPipeline:
    def __init__(self):
        self.calls: list[bool] = []

    def queue_refresh(self, *, only_stale=False):
        self.calls.append(only_stale)
        return 1


def test_moves_in_the_archive_refresh_stale_inbox_suggestions(fake, base_services):
    base_services.pipeline = RecordingPipeline()
    start_and_wait_for_initial_pass(fake)
    assert base_services.pipeline.calls == []  # nothing moved

    fake.reconciler.scan = lambda **kwargs: ScanStats(moved=3)
    fake.watcher.request_scan("Ordner umbenannt")
    assert wait_until(lambda: base_services.pipeline.calls == [True], 2.0)


def test_a_failing_refresh_does_not_break_the_watcher(fake, base_services):
    class Broken:
        def queue_refresh(self, *, only_stale=False):
            raise RuntimeError("boom")

    base_services.pipeline = Broken()
    fake.reconciler.scan = lambda **kwargs: ScanStats(removed=1)
    fake.watcher.start()
    assert wait_until(lambda: fake.watcher.last_result is not None, 2.0)
    assert fake.watcher.last_error is None and fake.watcher.running
