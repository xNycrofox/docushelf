"""Wiring of all components. One ``Services`` instance exists per household.

With a single household that is one per process; with several households
(``config/households.yaml``) :class:`docushelf.households.Registry` creates one
per household, each with its own settings (``households.settings_for``).

Components receive the container in their constructor and look up their
collaborators through it (``services.index``, ``services.ops`` ...). Background
components implement ``start()`` and ``stop()``.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from .audit import AuditLog
from .config import Settings
from .inbox.items import InboxStore
from .index.db import Index
from .layout import Layout
from .notify import Notifier
from .util import timeutil

if TYPE_CHECKING:  # pragma: no cover
    from .households import Household
    from .archive.ops import ArchiveOps
    from .archive.trash import Trash
    from .channels.imap import ImapFetcher
    from .channels.smtp import SmtpReceiver
    from .inbox.intake import Intake
    from .inbox.pipeline import Pipeline
    from .inbox.worker import Worker
    from .index.reconcile import Reconciler
    from .index.search import Searcher
    from .index.watcher import ArchiveWatcher
    from .models import ScanStats
    from .suggest.engine import SuggestionEngine
    from .suggest.rules import RuleStore
    from .web.tokens import TokenStore

log = logging.getLogger(__name__)


class Services:
    def __init__(self, settings: Settings, *, household: "Household | None" = None):
        self.settings = settings
        if household is None:
            from .households import default_household

            household = default_household(settings)
        # The household these services belong to (logging, user interface).
        self.household: Household = household
        timeutil.set_timezone(settings.timezone)
        from .pdf.ocr import configure_ocr_slots

        configure_ocr_slots(settings.ocr_parallel)
        self.layout = Layout.from_settings(settings)
        self.layout.ensure(tuple(s.name for s in settings.sources if s.path is None))
        self.index = Index(self.layout.db_path)
        self.needs_full_scan = self.index.init_schema()
        self.audit = AuditLog(self.layout.audit)
        self.inbox = InboxStore(self.layout)
        self.notifier = Notifier(settings.notify_url)
        # Serializes every change the application makes to the archive tree and
        # every reconciliation pass, so they never see each other half-done.
        self.archive_lock = threading.RLock()

        from .archive.ops import ArchiveOps
        from .archive.trash import Trash
        from .inbox.intake import Intake
        from .inbox.pipeline import Pipeline
        from .inbox.worker import Worker
        from .index.reconcile import Reconciler
        from .index.search import Searcher
        from .index.watcher import ArchiveWatcher
        from .suggest.engine import SuggestionEngine
        from .suggest.rules import RuleStore
        from .web.tokens import TokenStore

        self.rules: RuleStore = RuleStore(self.layout.rules_file)
        self.tokens: TokenStore = TokenStore(self.layout.tokens_file)
        self.reconciler: Reconciler = Reconciler(self)
        self.searcher: Searcher = Searcher(self)
        self.trash: Trash = Trash(self)
        self.ops: ArchiveOps = ArchiveOps(self)
        self.suggester: SuggestionEngine = SuggestionEngine(self)
        self.pipeline: Pipeline = Pipeline(self)
        self.worker: Worker = Worker(self)
        self.intake: Intake = Intake(self)
        self.watcher: ArchiveWatcher = ArchiveWatcher(self)

        self.imap: ImapFetcher | None = None
        self.smtp: SmtpReceiver | None = None
        if settings.imap:
            from .channels.imap import ImapFetcher

            self.imap = ImapFetcher(self)
        if settings.smtp:
            from .channels.smtp import SmtpReceiver

            self.smtp = SmtpReceiver(self)
        self._started = False

    # -- lifecycle ------------------------------------------------------------------
    def recover(self) -> None:
        """Bring the data directory into a consistent state after a crash or restart."""
        self.index.requeue_running()
        recovered = self.inbox.recover_orphans()
        if recovered:
            log.warning("recovered %d inbox directories without item.json", len(recovered))
        self.ops.recover()
        self._remove_stale_ocr_workdirs()

    def _remove_stale_ocr_workdirs(self) -> None:
        """OCR runs that were killed (e.g. by a container stop) leave their work directory behind."""
        from .pdf.ocr import remove_stale_workdirs

        try:
            removed = remove_stale_workdirs(self.settings)
        except Exception:
            log.warning("cannot remove abandoned OCR work directories", exc_info=True)
            return
        if removed:
            log.info("removed %d abandoned OCR work director%s", removed, "y" if removed == 1 else "ies")

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.recover()
        self.worker.start()
        self.worker.resume_pending()
        self.intake.start()
        # The watcher runs the initial full scan in its own thread.
        self.watcher.start()
        if self.imap:
            self.imap.start()
        if self.smtp:
            self.smtp.start()
        log.info("DocuShelf services started%s", self._label())

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        for component in (self.smtp, self.imap, self.watcher, self.intake, self.worker):
            if component is None:
                continue
            try:
                component.stop()
            except Exception:
                log.exception("error while stopping %s", type(component).__name__)
        self.index.close()
        log.info("DocuShelf services stopped%s", self._label())

    def _label(self) -> str:
        household_id = getattr(self.household, "id", None)
        return f" (household {household_id})" if household_id and household_id != "default" else ""

    # -- maintenance ------------------------------------------------------------------
    def rebuild(self, *, user: str | None = None) -> "ScanStats":
        """Throw the index away and rebuild it from the file system."""
        with self.archive_lock:
            self.index.reset()
            stats = self.reconciler.scan(full=True)
        self.worker.resume_pending()
        self.audit.write(user, "rebuild", documents=stats.added)
        return stats
