"""Background worker: runs queued jobs and periodic maintenance.

Jobs live in the SQLite job table (see :class:`docushelf.index.db.Index`), so
they survive a restart; their state is always reconstructible from the inbox
directories (:meth:`Worker.resume_pending`).

* ``settings.worker_threads`` threads claim the job kinds ``process``,
  ``llm_suggest``, ``refresh`` and ``auto_file`` and hand them to the pipeline.
* An ``llm_suggest`` job that carries ``{"auto_file": true}`` in its payload
  queues the automatic filing of its item when it ends – after a success as
  well as after giving up – so the decision waits for the model's answer but
  never for a model that does not answer.
* A transient error (``ProcessingError(transient=True)``, e.g. an OCR process
  that was killed) is retried after 30 s, 2 min and 10 min; when the third
  retry fails as well, a ``process`` job gives up and the item moves to
  ``eingang/fehler``. An OCR timeout is not transient: the item fails at once
  with a hint how to get past it. ``auto_file`` jobs are never retried: the
  pipeline records a failed automatic filing at the item, which then waits
  for a person.
* Idle threads sleep until :meth:`Worker.kick` is called or the next delayed
  job becomes due; there is no busy polling.
* A maintenance thread wakes up every minute: hourly it empties expired trash
  entries and old filed inbox items and removes work directories that killed
  OCR runs left behind, daily it prunes the thumbnail cache, and every ten
  minutes it re-queues items in ``eingang/arbeit`` whose job got lost
  (queueing is idempotent, so items with a job are not affected).
* A job that was claimed again and again without ever finishing (the process
  died while running it, e.g. on a document that crashes a PDF library) is not
  started once more; its item moves to ``eingang/fehler`` instead.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..errors import Conflict, NotFound, ProcessingError
from ..index.db import Job
from ..util.timeutil import now_iso
from .pipeline import AUTO_AFTER_LLM, AUTO_JOB, error_message

log = logging.getLogger(__name__)

JOB_KINDS = ("process", "llm_suggest", "refresh", AUTO_JOB)
# Job kinds whose failures are final, also transient ones.
NO_RETRY_KINDS = frozenset({AUTO_JOB})
# Delays before the 1st, 2nd and 3rd retry after a transient error.
RETRY_DELAYS = (30.0, 120.0, 600.0)
IDLE_TIMEOUT = 30.0
ERROR_BACKOFF = 5.0
MAINTENANCE_INTERVAL = 60.0
PURGE_INTERVAL = 3600.0
PRUNE_INTERVAL = 24 * 3600.0
RESUME_INTERVAL = 600.0


def _prune_thumbnails(cache_dir: Path) -> int:
    from ..pdf.render import ThumbnailCache

    return int(ThumbnailCache(cache_dir).prune() or 0)


def _remove_stale_ocr_workdirs(settings: Any) -> int:
    from ..pdf.ocr import remove_stale_workdirs

    return int(remove_stale_workdirs(settings) or 0)


def _describe_delay(seconds: float) -> str:
    if seconds >= 120 and seconds % 60 == 0:
        return f"{int(seconds // 60)} min"
    if seconds >= 10:
        return f"{seconds:.0f} s"
    return f"{seconds:.1f} s".replace(".", ",")


class Worker:
    def __init__(
        self,
        services: Any,
        *,
        threads: int | None = None,
        retry_delays: tuple[float, ...] = RETRY_DELAYS,
        idle_timeout: float = IDLE_TIMEOUT,
        error_backoff: float = ERROR_BACKOFF,
        maintenance_interval: float = MAINTENANCE_INTERVAL,
        purge_interval: float = PURGE_INTERVAL,
        prune_interval: float = PRUNE_INTERVAL,
        resume_interval: float | None = RESUME_INTERVAL,
        join_timeout: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.services = services
        self._threads_override = threads
        self.retry_delays = tuple(float(delay) for delay in retry_delays)
        self.idle_timeout = idle_timeout
        self.error_backoff = error_backoff
        self.maintenance_interval = maintenance_interval
        self.purge_interval = purge_interval
        self.prune_interval = prune_interval
        self.resume_interval = resume_interval
        self.join_timeout = join_timeout
        self._clock = clock

        # A job claimed more often than this was interrupted again and again
        # (crash or restart during processing) and is not started once more.
        self.max_claims = len(self.retry_delays) + 2

        self._lifecycle = threading.Lock()
        self._cond = threading.Condition()
        self._generation = 0  # bumped by kick(); guarded by _cond
        self._stop_event = threading.Event()
        self._running = False
        self._threads: list[threading.Thread] = []
        self._maintenance_thread: threading.Thread | None = None

        self._state_lock = threading.Lock()
        self._current: dict[threading.Thread, dict[str, Any]] = {}  # thread -> job it runs

        self._last_purge: float | None = None
        self._last_prune: float | None = None
        self._last_resume: float | None = None
        self.last_maintenance_at: str | None = None
        self.last_error: str | None = None

    # -- lifecycle ---------------------------------------------------------------------
    @property
    def thread_count(self) -> int:
        configured = self._threads_override or getattr(self.services.settings, "worker_threads", 1)
        return max(1, int(configured or 1))

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        with self._lifecycle:
            if self._running:
                return
            self._running = True
            # Every run gets its own stop event: a thread still finishing a job
            # from a previous run must not pick up work after a restart.
            stop = self._stop_event = threading.Event()
            self._last_resume = self._clock()
            self._threads = [
                threading.Thread(
                    target=self._work_loop, args=(stop,), name=f"docushelf-worker-{number}", daemon=True
                )
                for number in range(1, self.thread_count + 1)
            ]
            self._maintenance_thread = threading.Thread(
                target=self._maintenance_loop, args=(stop,), name="docushelf-maintenance", daemon=True
            )
            for thread in self._threads:
                thread.start()
            self._maintenance_thread.start()
        log.info("worker started with %d thread(s)", len(self._threads))

    def stop(self, timeout: float | None = None) -> None:
        """Stop the threads. Returns quickly: a job that is running is allowed to
        finish in the background, but no new job is started."""
        with self._lifecycle:
            if not self._running:
                return
            self._running = False
            self._stop_event.set()
            threads = list(self._threads)
            if self._maintenance_thread is not None:
                threads.append(self._maintenance_thread)
        self.kick()
        deadline = time.monotonic() + (self.join_timeout if timeout is None else timeout)
        with self._state_lock:
            busy = set(self._current)
        for thread in threads:
            if thread is threading.current_thread():
                continue
            if thread in busy:
                thread.join(0.05)
                continue
            thread.join(max(0.0, deadline - time.monotonic()))
        still_busy = [thread.name for thread in threads if thread.is_alive()]
        if still_busy:
            log.info("worker stopped; still finishing: %s", ", ".join(still_busy))
        else:
            log.info("worker stopped")

    def kick(self) -> None:
        """Wake idle threads (new job queued, or the next due time changed)."""
        with self._cond:
            self._generation += 1
            self._cond.notify_all()

    def resume_pending(self) -> int:
        """Queue a ``process`` job for every item in ``eingang/arbeit``.

        Returns the number of jobs that were not queued already.
        """
        queued = 0
        for item in self.services.inbox.iter_state("arbeit"):
            try:
                if self.services.index.enqueue("process", item.id):
                    queued += 1
            except Exception:
                log.exception("cannot queue inbox item %s", item.id)
        if queued:
            log.info("queued %d inbox item(s) for processing", queued)
            self.kick()
        return queued

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._state_lock:
            current = []
            for thread, info in self._current.items():
                entry = {k: v for k, v in info.items() if k != "_started"}
                entry["thread"] = thread.name
                entry["seconds"] = round(now - info["_started"], 1)
                current.append(entry)
        threads = list(self._threads)
        maintenance = self._maintenance_thread
        try:
            queue = self.services.index.job_counts()
        except Exception:
            queue = {}
        return {
            "running": self._running,
            "threads": self.thread_count,
            "threads_alive": sum(1 for thread in threads if thread.is_alive()),
            "maintenance_alive": bool(maintenance is not None and maintenance.is_alive()),
            "current_jobs": current,
            "queue": queue,
            "last_maintenance_at": self.last_maintenance_at,
            "last_error": self.last_error,
        }

    # -- job threads ---------------------------------------------------------------------------
    def _work_loop(self, stop: threading.Event) -> None:
        thread = threading.current_thread()
        worker_id = f"{os.getpid()}:{thread.name}"
        while not stop.is_set():
            with self._cond:
                generation = self._generation
            try:
                job = self.services.index.claim(JOB_KINDS, worker_id)
            except Exception as exc:
                self.last_error = f"Job-Warteschlange nicht lesbar: {exc}"
                log.exception("worker: cannot claim a job")
                self._sleep(stop, generation, self.error_backoff)
                continue
            if job is None:
                self._sleep(stop, generation, self._time_until_next_job())
                continue
            if stop.is_set():
                self._put_back(job)
                break
            self._run_job(job, thread)

    def _sleep(self, stop: threading.Event, generation: int, timeout: float) -> None:
        with self._cond:
            self._cond.wait_for(lambda: stop.is_set() or self._generation != generation, timeout=timeout)

    def _time_until_next_job(self) -> float:
        try:
            due = self.services.index.next_run_after(JOB_KINDS)
        except Exception:
            return self.idle_timeout
        if due is None:
            return self.idle_timeout
        return min(self.idle_timeout, max(0.05, due - time.time()))

    def _put_back(self, job: Job) -> None:
        try:
            self.services.index.release(job.id)
        except Exception:
            log.exception("worker: cannot return job %s to the queue", job.id)

    def _run_job(self, job: Job, thread: threading.Thread) -> None:
        with self._state_lock:
            self._current[thread] = {
                "kind": job.kind,
                "key": job.key,
                "attempt": job.attempts,
                "started_at": now_iso(),
                "_started": time.monotonic(),
            }
        try:
            if job.attempts > self.max_claims:
                log.warning("worker: job %s %s was interrupted %d times; giving up", job.kind, job.key, job.attempts)
                self._give_up(
                    job,
                    ProcessingError("Die Verarbeitung wurde wiederholt unterbrochen und wird nicht erneut gestartet"),
                )
                return
            try:
                self._dispatch(job)
            except ProcessingError as exc:
                if exc.transient and job.kind not in NO_RETRY_KINDS:
                    self._retry_later(job, exc)
                else:
                    self._give_up(job, exc)
                return
            except (NotFound, Conflict) as exc:
                # The item is gone or no longer waiting for this job: nothing follows either.
                log.info("worker: %s job for %s skipped: %s", job.kind, job.key, exc or "not found")
                self._complete(job)
                return
            except Exception as exc:
                self.last_error = error_message(exc)
                log.exception("worker: %s job for %s failed", job.kind, job.key)
                self._give_up(job, exc)
                return
            self._complete(job)
            self._follow_up(job)
        except Exception:
            # Bookkeeping failed (index unavailable); the job stays claimed
            # until the next start puts it back into the queue.
            log.exception("worker: cannot record the result of job %s", job.id)
        finally:
            with self._state_lock:
                self._current.pop(thread, None)

    def _dispatch(self, job: Job) -> None:
        pipeline = self.services.pipeline
        if job.kind == "process":
            pipeline.process(job.key)
        elif job.kind == "llm_suggest":
            pipeline.run_llm(job.key)
        elif job.kind == "refresh":
            # Queued (after a folder change, or for all items at once): not a person's edit of this item.
            pipeline.refresh_suggestions(job.key, manual=False)
        elif job.kind == AUTO_JOB:
            pipeline.auto_file(job.key)
        else:  # pragma: no cover - claim() only returns JOB_KINDS
            raise ValueError(f"unknown job kind {job.kind!r}")

    def _complete(self, job: Job) -> None:
        self.services.index.complete(job.id)

    def _follow_up(self, job: Job) -> None:
        """Queue the job that has to run after ``job`` ended, successfully or not."""
        if job.kind != "llm_suggest" or not (job.payload or {}).get(AUTO_AFTER_LLM):
            return
        try:
            if self.services.index.enqueue(AUTO_JOB, job.key):
                self.kick()
        except Exception:
            log.exception("worker: cannot queue the automatic filing of %s", job.key)

    def _retry_later(self, job: Job, exc: ProcessingError) -> None:
        attempt = max(1, job.attempts)
        if attempt > len(self.retry_delays):
            log.warning("worker: %s job for %s failed %d times: %s", job.kind, job.key, attempt, exc)
            self._give_up(job, exc)
            return
        delay = self.retry_delays[attempt - 1]
        self.services.index.fail(job.id, error_message(exc), retry_in=delay)
        log.warning(
            "worker: %s job for %s failed (%s); retry %d of %d in %.0f s",
            job.kind,
            job.key,
            exc,
            attempt,
            len(self.retry_delays),
            delay,
        )
        if job.kind == "process":
            self._item_log(
                job.key,
                f"Neuer Versuch in {_describe_delay(delay)} (Wiederholung {attempt} von {len(self.retry_delays)})",
            )
        self.kick()  # idle threads recompute when to wake up

    def _give_up(self, job: Job, exc: BaseException) -> None:
        index = self.services.index
        if job.kind != "process":
            index.fail(job.id, error_message(exc), retry_in=None)
            self._follow_up(job)
            return
        try:
            self.services.pipeline.fail(job.key, exc)
        except NotFound:
            pass
        except Exception:
            log.exception("worker: cannot move inbox item %s to the failed items", job.key)
            index.fail(job.id, error_message(exc), retry_in=None)
            return
        index.complete(job.id)

    def _item_log(self, item_id: str, message: str) -> None:
        inbox = self.services.inbox
        try:
            item = inbox.find(item_id)
            if item is not None and item.dir.is_dir():
                inbox.log(item, message)
        except Exception:
            log.debug("cannot write item log", exc_info=True)

    # -- maintenance -------------------------------------------------------------------------------
    def _maintenance_loop(self, stop: threading.Event) -> None:
        while not stop.wait(self.maintenance_interval):
            try:
                self.run_maintenance()
            except Exception:
                log.exception("maintenance failed")

    def run_maintenance(self, *, force: bool = False) -> dict[str, int]:
        """Run the maintenance tasks that are due (all of them with ``force``)."""
        now = self._clock()
        results: dict[str, int] = {}
        if force or self._last_purge is None or now - self._last_purge >= self.purge_interval:
            self._last_purge = now
            results["trash_purged"] = self._call_trash("purge_expired")
            results["done_items_purged"] = self._call_trash("purge_done_items")
            try:
                # Leftovers that were still too young when the services started.
                results["ocr_workdirs_removed"] = _remove_stale_ocr_workdirs(self.services.settings)
            except Exception:
                log.exception("maintenance: removing abandoned OCR work directories failed")
        if force or self._last_prune is None or now - self._last_prune >= self.prune_interval:
            self._last_prune = now
            try:
                results["thumbnails_pruned"] = _prune_thumbnails(self.services.layout.cache)
            except ImportError:
                log.debug("thumbnail cache not available")
            except Exception:
                log.exception("pruning the thumbnail cache failed")
        if self.resume_interval is not None and (
            force or self._last_resume is None or now - self._last_resume >= self.resume_interval
        ):
            # Self-healing: an item whose job was lost (index rebuilt, queue
            # error) would otherwise stay in "arbeit" until the next start.
            self._last_resume = now
            results["resumed"] = self.resume_pending()
        self.last_maintenance_at = now_iso()
        done = {key: value for key, value in results.items() if value}
        if done:
            log.info("maintenance: %s", ", ".join(f"{key}={value}" for key, value in done.items()))
        return results

    def _call_trash(self, method: str) -> int:
        trash = getattr(self.services, "trash", None)
        if trash is None:
            return 0
        try:
            return int(getattr(trash, method)() or 0)
        except Exception:
            log.exception("maintenance: trash.%s failed", method)
            return 0
