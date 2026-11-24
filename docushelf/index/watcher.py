"""Live watching of the archive tree.

A watchdog observer reports changes below the archive. They are bundled: a
reconciliation pass runs once the tree has been quiet for ``quiet`` seconds
(SMB clients write files in several steps), but no later than ``max_delay``
seconds after the first change of a burst. A full pass also runs at start and
every ``settings.reconcile_interval`` seconds, because watchers miss events
(restarts, network mounts, queue overflows). Where no observer can be started
(inotify limits, unsupported file systems) the watcher polls instead.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from typing import Any, Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from ..errors import DocuShelfError
from ..models import ScanStats
from ..util.names import OWN_TEMP_PREFIX, is_ignored
from ..util.timeutil import now_iso

log = logging.getLogger(__name__)

__all__ = ["ArchiveWatcher"]

# Reading files (our own text extraction, thumbnails, SMB clients browsing)
# must not trigger passes; neither do permission or timestamp changes.
_IGNORED_EVENT_TYPES = frozenset({"opened", "closed_no_write", "attrib"})
_EVENTS_LOST = "events_lost"


class _EventHandler(FileSystemEventHandler):
    def __init__(self, watcher: "ArchiveWatcher"):
        super().__init__()
        self._watcher = watcher

    def on_any_event(self, event: FileSystemEvent) -> None:
        self._watcher._on_event(event)


class ArchiveWatcher:
    """Runs reconciliation passes in a background thread when the archive changes."""

    # How often the run loop wakes up at the latest to check the observer (seconds).
    health_interval = 30.0
    # How long to wait before trying to start an observer again while polling (seconds).
    observer_retry_interval = 600.0
    # How long stop() waits for the background thread (seconds).
    stop_timeout = 15.0

    def __init__(
        self,
        services: Any,
        *,
        quiet: float = 2.0,
        max_delay: float = 20.0,
        poll_fallback: float = 60.0,
        interval: float | None = None,
        observer_factory: Callable[[], Any] | None = None,
    ):
        self.services = services
        self.quiet = float(quiet)
        self.max_delay = max(float(max_delay), self.quiet)
        self.poll_fallback = float(poll_fallback)
        self.interval = float(services.settings.reconcile_interval if interval is None else interval)
        self._observer_factory = observer_factory or Observer

        self.last_result: ScanStats | None = None
        self.last_error: str | None = None
        self.scanning: bool = False
        self.polling: bool = False
        self.last_scan_at: str | None = None
        self.last_reason: str | None = None

        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._observer_lock = threading.Lock()
        self._observer: Any = None
        self._roots: tuple[str, ...] = ()
        # Guarded by _cond:
        self._first_event: float | None = None
        self._last_event: float | None = None
        self._requests: list[str] = []
        self._next_full = math.inf
        self._next_poll = math.inf
        self._next_observer_retry = math.inf

    # -- public API -------------------------------------------------------------------
    def start(self) -> None:
        """Start watching: an initial full pass in a background thread, then live updates."""
        with self._cond:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._first_event = self._last_event = None
            self._requests = []
            self._next_full = time.monotonic()  # the first pass is a full one, right away
            self._next_poll = math.inf
            self.polling = False
            thread = threading.Thread(target=self._run, name="archive-watcher", daemon=True)
            self._thread = thread
        thread.start()

    def stop(self) -> None:
        """Stop the observer and the background thread (a running pass is cancelled)."""
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(self.stop_timeout)
            if thread.is_alive():
                log.warning("archive watcher did not stop within %.0f s", self.stop_timeout)
        self._stop_observer()
        with self._cond:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None

    def request_scan(self, reason: str = "") -> None:
        """Ask for a reconciliation pass as soon as possible (requests are combined)."""
        with self._cond:
            self._requests.append(reason or "Anforderung")
            self._cond.notify_all()

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def observer_running(self) -> bool:
        observer = self._observer
        return observer is not None and observer.is_alive()

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "scanning": self.scanning,
            "polling": self.polling,
            "last_scan_at": self.last_scan_at,
            "last_reason": self.last_reason,
            "last_error": self.last_error,
            "last_result": self.last_result.to_dict() if self.last_result else None,
        }

    # -- events -------------------------------------------------------------------------
    def _on_event(self, event: FileSystemEvent) -> None:
        try:
            event_type = event.event_type
            if event_type in _IGNORED_EVENT_TYPES:
                return
            if event_type == _EVENTS_LOST:
                log.warning("file system events were lost; reconciling the archive")
                self._note_change()
                return
            paths = [event.src_path]
            dest = getattr(event, "dest_path", "")
            if dest:
                paths.append(dest)
            if any(self._is_relevant(path) for path in paths):
                self._note_change()
        except Exception:  # never let a strange event kill the observer thread
            log.debug("ignoring unexpected file system event %r", event, exc_info=True)

    def _is_relevant(self, path: bytes | str) -> bool:
        text = os.fsdecode(path)
        rel: str | None = None
        for root in self._roots:
            if text == root:
                return True
            if text.startswith(root + os.sep):
                rel = text[len(root) + 1 :]
                break
        if rel is None:
            return True  # unexpected location; better one pass too many
        for part in rel.split(os.sep):
            if part.startswith(OWN_TEMP_PREFIX) or is_ignored(part):
                return False
        return True

    def _note_change(self) -> None:
        now = time.monotonic()
        with self._cond:
            self._last_event = now
            if self._first_event is None:
                self._first_event = now
                self._cond.notify_all()

    # -- background thread ---------------------------------------------------------------
    def _run(self) -> None:
        log.info("archive watcher started")
        try:
            if not self._start_observer():
                # The initial full pass is due right away anyway.
                self._enter_polling(immediate=False)
            while not self._stop.is_set():
                try:
                    self._maintain_observer()
                    job = self._wait_for_job()
                    if job is not None:
                        full, reason = job
                        self._run_pass(full=full, reason=reason)
                except Exception as exc:  # pragma: no cover - defensive; passes report their own errors
                    log.exception("error in the archive watcher")
                    self.last_error = str(exc) or type(exc).__name__
                    self._stop.wait(5.0)
        finally:
            self._stop_observer()
            log.info("archive watcher stopped")

    def _wait_for_job(self) -> tuple[bool, str] | None:
        """Block until a pass is due; None when it is time to check the observer or to stop."""
        with self._cond:
            if self._stop.is_set():
                return None
            now = time.monotonic()
            due = math.inf
            if self._requests:
                due = now
            if self._first_event is not None and self._last_event is not None:
                due = min(due, self._last_event + self.quiet, self._first_event + self.max_delay)
            due = min(due, self._next_full)
            if self.polling:
                due = min(due, self._next_poll)
            if due > now:
                self._cond.wait(min(due - now, self.health_interval))
                return None
            full = self._next_full <= now
            reasons: list[str] = []
            if full:
                reasons.append("Vollabgleich" if self.last_scan_at else "Start")
            reasons += self._requests
            if self._first_event is not None:
                reasons.append("Änderungen im Archiv")
            elif self.polling and self._next_poll <= now:
                reasons.append("regelmäßige Prüfung")
            # A pass covers everything that happened before it starts.
            self._requests = []
            self._first_event = self._last_event = None
            if full:
                self._next_full = now + self.interval if self.interval > 0 else math.inf
            if self.polling:
                self._next_poll = now + self.poll_fallback
            return full, ", ".join(dict.fromkeys(reasons)) or "Abgleich"

    def _run_pass(self, *, full: bool, reason: str) -> None:
        self.scanning = True
        self.last_reason = reason
        try:
            stats = self.services.reconciler.scan(full=full, cancel=self._stop)
        except DocuShelfError as exc:
            self.last_error = str(exc)
            log.error("reconciliation failed (%s): %s", reason, exc)
        except Exception as exc:
            self.last_error = str(exc) or type(exc).__name__
            log.exception("reconciliation failed (%s)", reason)
        else:
            self.last_result = stats
            self.last_error = None
            if stats.moved or stats.removed:
                self._refresh_stale_suggestions()
        finally:
            self.scanning = False
            self.last_scan_at = now_iso()

    def _refresh_stale_suggestions(self) -> None:
        """Folders may have been renamed or moved: recompute inbox suggestions that point nowhere now."""
        pipeline = getattr(self.services, "pipeline", None)
        if pipeline is None or not hasattr(pipeline, "queue_refresh"):
            return
        try:
            queued = pipeline.queue_refresh(only_stale=True)
        except Exception:
            log.warning("cannot queue suggestion refreshes after archive changes", exc_info=True)
            return
        if queued:
            log.info("archive changed: recomputing suggestions of %d inbox item(s)", queued)

    # -- observer -------------------------------------------------------------------------
    def _start_observer(self) -> bool:
        root = os.fspath(self.services.layout.archive)
        self._roots = tuple(dict.fromkeys((os.path.abspath(root), os.path.realpath(root))))
        observer = None
        try:
            observer = self._observer_factory()
            observer.schedule(_EventHandler(self), root, recursive=True)
            observer.start()
        except Exception as exc:
            if observer is not None:
                try:
                    observer.stop()
                except Exception:
                    pass
            log.warning(
                "cannot watch %s for changes (%s); checking every %.0f s instead", root, exc, self.poll_fallback
            )
            return False
        with self._observer_lock:
            self._observer = observer
        if self._stop.is_set():
            self._stop_observer()
        return True

    def _stop_observer(self) -> None:
        with self._observer_lock:
            observer, self._observer = self._observer, None
        if observer is None:
            return
        try:
            observer.stop()
            observer.join(5)
        except Exception:
            log.debug("error while stopping the archive observer", exc_info=True)

    def _enter_polling(self, *, immediate: bool) -> None:
        now = time.monotonic()
        with self._cond:
            self.polling = True
            self._next_poll = now if immediate else now + self.poll_fallback
            self._next_observer_retry = now + self.observer_retry_interval
            self._cond.notify_all()

    def _maintain_observer(self) -> None:
        if self._stop.is_set():
            return
        if not self.polling:
            observer = self._observer
            if observer is not None and not observer.is_alive():
                log.warning("archive observer stopped; checking every %.0f s instead", self.poll_fallback)
                self._stop_observer()
                # Events may have been missed: look at the archive right away.
                self._enter_polling(immediate=True)
            return
        if time.monotonic() < self._next_observer_retry:
            return
        if not self._start_observer():
            with self._cond:
                self._next_observer_retry = time.monotonic() + self.observer_retry_interval
            return
        log.info("watching %s for changes again", self.services.layout.archive)
        with self._cond:
            self.polling = False
            self._next_poll = math.inf
            self._next_observer_retry = math.inf
        # Changes between the last poll and the start of the observer.
        self.request_scan("Überwachung wieder aktiv")
