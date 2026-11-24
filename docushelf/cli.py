"""Command line interface: ``docushelf <command>``.

``serve`` runs the web application together with all background services
(worker, intake, archive watcher, mail channels). The other commands are
maintenance tools that work directly on the data directory. They never start
background threads.

While ``serve`` runs it holds an advisory lock (``index/server.lock``). Commands
that would get in the way of a running server – rebuilding the index, a
reconciliation pass, an import that files documents itself – check the lock
and refuse to run unless ``--force`` is given. This also works when the server
runs in another container that shares the data volume. ``process`` hands the
item over to a running server instead of processing it itself.

Households: with several households (``config/households.yaml``) the commands
that work on data act on one household, chosen with the global option
``--haushalt ID`` (``--household``); with a single household it is chosen
automatically. ``serve`` serves all households, ``households`` lists them.
The server lock is global (``index/server.lock`` in the data root).

Output meant for people is German. Modules with heavy dependencies are
imported inside the commands, so that quick commands such as ``healthcheck``
(called by the container health check every 30 seconds) start fast.
"""

from __future__ import annotations

import argparse
import inspect
import json
import logging
import os
import secrets
import sqlite3
import sys
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, NoReturn, Sequence

from . import __version__
from .errors import DocuShelfError, NotFound, ProcessingError

if TYPE_CHECKING:  # pragma: no cover
    from .config import Settings
    from .households import Household
    from .layout import Layout
    from .services import Services

log = logging.getLogger(__name__)

CLI_USER = "cli"  # name in the audit log for changes made from the command line

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

HEALTH_PATH = "/healthz"
HEALTH_TIMEOUT = 5.0
SERVER_LOCK_NAME = "server.lock"
# Time uvicorn gives running requests (uploads) after SIGTERM before it stops them.
GRACEFUL_SHUTDOWN_SECONDS = 20
MIN_PASSWORD_LENGTH = 8

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
# Libraries that log every request, file event or decoding detail below WARNING.
NOISY_LOGGERS = (
    "asyncio",
    "authlib",
    "fontTools",
    "httpcore",
    "httpx",
    "multipart",
    "pdfminer",
    "PIL",
    "pikepdf",
    "python_multipart",
    "watchdog",
)

INBOX_LABELS = {"arbeit": "in Verarbeitung", "bereit": "bereit", "fehler": "Fehler", "erledigt": "erledigt"}
JOB_STATE_LABELS = {"queued": "wartend", "running": "laufend", "failed": "fehlgeschlagen"}
JOB_KIND_LABELS = {
    "process": "Verarbeitung",
    "llm_suggest": "KI-Vorschlag",
    "refresh": "Vorschläge",
    "auto_file": "Automatische Ablage",
}
IMPORT_LABELS = {
    "imported": "im Eingang",
    "filed": "abgelegt",
    "skipped": "übersprungen",
    "failed": "fehlgeschlagen",
    "planned": "geplant",
}


class CliError(DocuShelfError):
    """A failure with a German message for the command line (exit code 1)."""


# ---------------------------------------------------------------------------
# Output helpers


def _out(text: str = "") -> None:
    """Results go to stdout."""
    print(text, file=sys.stdout, flush=True)


def _note(text: str) -> None:
    """Hints, warnings and progress go to stderr, so stdout stays usable in scripts."""
    print(text, file=sys.stderr, flush=True)


def _fmt_int(value: int) -> str:
    return f"{int(value):,}".replace(",", ".")


def _count(value: int, singular: str, plural: str) -> str:
    return f"{_fmt_int(value)} {singular if value == 1 else plural}"


def _fmt_seconds(milliseconds: float) -> str:
    seconds = max(0.0, milliseconds / 1000)
    text = f"{seconds:.1f}" if seconds < 100 else f"{seconds:.0f}"
    return text.replace(".", ",") + " s"


def _fmt_datetime(value: Any) -> str:
    from .util.timeutil import parse_iso_datetime, timezone

    parsed = parse_iso_datetime(value) if isinstance(value, str) else None
    if parsed is None:
        return "–"
    return parsed.astimezone(timezone()).strftime("%d.%m.%Y %H:%M")


def _os_reason(exc: OSError) -> str:
    return exc.strerror or str(exc)


def _field(entry: Any, key: str) -> Any:
    if isinstance(entry, dict):
        return entry.get(key)
    return getattr(entry, key, None)


def _table(rows: list[tuple[str, ...]]) -> list[str]:
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    return ["  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip() for row in rows]


def _aligned(rows: list[tuple[str, str]]) -> list[str]:
    width = max(len(label) for label, _ in rows) + 1
    return [f"{(label + ':').ljust(width)} {value}" for label, value in rows]


# ---------------------------------------------------------------------------
# Logging


class _StderrHandler(logging.StreamHandler):
    """Writes to whatever ``sys.stderr`` is when a record is emitted."""

    def __init__(self) -> None:
        super().__init__(sys.stderr)

    @property  # type: ignore[override]
    def stream(self) -> Any:
        return sys.stderr

    @stream.setter
    def stream(self, value: Any) -> None:
        pass


class _HealthCheckAccessFilter(logging.Filter):
    """Keeps the container health check (every 30 s) out of the access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and str(args[2]).startswith(HEALTH_PATH):
            return False
        return True


def _parse_level(name: Any, default: int = logging.INFO) -> int:
    level = logging.getLevelName(str(name or "").strip().upper())
    return level if isinstance(level, int) else default


def _verbosity_level(verbose: int) -> int:
    if verbose >= 2:
        return logging.DEBUG
    if verbose == 1:
        return logging.INFO
    return logging.WARNING


def configure_logging(level: int) -> None:
    """Concise log lines on stderr; noisy libraries only from WARNING on."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, _StderrHandler):
            root.removeHandler(handler)
    handler = _StderrHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    root.addHandler(handler)
    root.setLevel(level)
    library_level = logging.NOTSET if level <= logging.DEBUG else max(level, logging.WARNING)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(library_level)
    logging.captureWarnings(True)


# ---------------------------------------------------------------------------
# Settings, layout and services


def _load_settings(args: argparse.Namespace, *, for_server: bool = False) -> "Settings":
    """Settings from the environment; ``--data-dir`` replaces DOCUSHELF_DATA_DIR.

    Only the web server needs the session secret. Other commands get a
    throwaway value, so they never create ``config/secret.key`` themselves –
    a file created by a command run as root would lock the server out.
    """
    from .config import load_settings

    env = dict(os.environ)
    data_dir = getattr(args, "data_dir", None)
    if data_dir:
        env["DOCUSHELF_DATA_DIR"] = str(Path(data_dir).expanduser().resolve())
    overrides: dict[str, Any] = {}
    if not for_server:
        overrides["secret_key"] = secrets.token_urlsafe(32)
    try:
        return load_settings(env, **overrides)
    except OSError as exc:
        target = env.get("DOCUSHELF_DATA_DIR") or "/data"
        raise CliError(
            f"Das Datenverzeichnis {target} ist nicht nutzbar ({_os_reason(exc)}). "
            "DOCUSHELF_DATA_DIR setzen oder --data-dir angeben."
        ) from exc


def _layout(settings: "Settings") -> "Layout":
    from .layout import Layout

    return Layout.from_settings(settings)


def _select_household(args: argparse.Namespace, settings: "Settings") -> tuple["Household", "Settings"]:
    """The household a command acts on and its settings.

    ``--haushalt`` chooses it; without the option the only household is used.
    With several households the option is required.
    """
    from .households import load_households, settings_for

    households = load_households(settings)
    wanted = getattr(args, "household", None)
    ids = ", ".join(household.id for household in households)
    if wanted:
        household = next((h for h in households if h.id == wanted), None)
        if household is None:
            raise CliError(f"Den Haushalt „{wanted}“ gibt es nicht. Vorhandene Haushalte: {ids}.")
    elif len(households) == 1:
        household = households[0]
    else:
        raise CliError(
            f"Es gibt mehrere Haushalte ({ids}). Bitte mit --haushalt ID angeben, welcher gemeint ist, "
            f"z. B. „docushelf --haushalt {households[0].id} …“."
        )
    return household, settings_for(settings, household, households=households)


def _household_settings(args: argparse.Namespace) -> tuple["Settings", "Settings"]:
    """``(global settings, settings of the selected household)``; equal with a single household."""
    root = _load_settings(args)
    _, settings = _select_household(args, root)
    return root, settings


def _is_corrupt(exc: BaseException) -> bool:
    """True for a damaged database file (as opposed to a locked or busy one)."""
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    name = getattr(exc, "sqlite_errorname", None)
    if name:
        return name.startswith(("SQLITE_CORRUPT", "SQLITE_NOTADB"))
    if isinstance(exc, sqlite3.OperationalError):
        return False
    text = str(exc).lower()
    return "malformed" in text or "not a database" in text


def _set_aside_index(layout: "Layout") -> list[Path]:
    """Rename a damaged index database (and its WAL files) so a new one can be built."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    moved = []
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{layout.db_path}{suffix}")
        if not path.exists():
            continue
        target = path.with_name(f"{path.name}.defekt-{stamp}")
        counter = 2
        while target.exists():
            target = path.with_name(f"{path.name}.defekt-{stamp}-{counter}")
            counter += 1
        path.rename(target)
        moved.append(target)
    if moved:
        names = ", ".join(path.name for path in moved)
        _note(f"Der Index war beschädigt und wurde beiseitegelegt ({names}); die Dateien können gelöscht werden.")
        log.warning("damaged index database set aside: %s", names)
    return moved


def _open_services(
    settings: "Settings", *, replace_broken_index: bool = False, household: "Household | None" = None
) -> "Services":
    """The service container without starting any background thread."""
    from .services import Services

    try:
        return Services(settings, household=household)
    except sqlite3.DatabaseError as exc:
        if not _is_corrupt(exc):
            raise
        if not replace_broken_index:
            raise CliError(f"Der Index ist beschädigt ({exc}). „docushelf rebuild“ baut ihn neu auf.") from exc
        _set_aside_index(_layout(settings))
        return Services(settings, household=household)


def _close_services(services: Any) -> None:
    try:
        services.stop()
    except Exception:
        log.exception("error while stopping services")
    try:
        services.index.close()
    except Exception:
        log.debug("cannot close the index", exc_info=True)


def _warn_if_root(settings: "Settings") -> None:
    """Files created as root would not be writable for the server afterwards."""
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or geteuid() != 0:
        return
    try:
        owner = Path(settings.data_dir).stat().st_uid
    except OSError:
        return
    if owner != 0:
        _note(
            f"Warnung: Der Befehl läuft als root, das Datenverzeichnis gehört UID {owner}. "
            f"Neu angelegte Dateien gehören dann root – besser als UID {owner} ausführen "
            "(docker compose exec ohne „-u root“)."
        )


# ---------------------------------------------------------------------------
# Server lock


class ServerLock:
    """Exclusive advisory lock that ``serve`` holds for its whole lifetime.

    The lock file lives in the index directory and contains a little JSON
    about the server (pid, address, start time) for ``status``. The lock
    itself is released by the operating system when the process ends, so a
    crashed server never leaves a stale lock behind.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, info: dict[str, Any], *, attempts: int = 5, delay: float = 0.2) -> bool:
        """Take the lock; False if another process holds it."""
        try:
            import fcntl
        except ImportError:  # pragma: no cover - not available on Windows
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            for attempt in range(attempts):
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    # A status probe holds a shared lock for a moment; try again.
                    if attempt == attempts - 1:
                        os.close(fd)
                        return False
                    time.sleep(delay)
            os.ftruncate(fd, 0)
            os.write(fd, (json.dumps(info, ensure_ascii=False) + "\n").encode("utf-8"))
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            os.ftruncate(fd, 0)
        except OSError:
            pass
        os.close(fd)


def probe_server(layout: "Layout") -> dict[str, Any] | None:
    """Information about a server that holds the lock of this data directory, else None."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - not available on Windows
        return None
    path = Path(layout.index) / SERVER_LOCK_NAME
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                info = json.loads(os.pread(fd, 4096, 0).decode("utf-8") or "{}")
            except (OSError, ValueError):
                info = {}
            return info if isinstance(info, dict) else {}
        except OSError:
            return None
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def _server_label(info: dict[str, Any]) -> str:
    pid = info.get("pid")
    return f" (PID {pid})" if pid else ""


def _guard_server(layout: "Layout", *, force: bool, action: str, hint: str) -> None:
    """Refuse an action that would interfere with a running server."""
    info = probe_server(layout)
    if info is None:
        return
    if force:
        _note(f"Warnung: Der Server läuft{_server_label(info)} – {action} wird trotzdem ausgeführt (--force).")
        return
    raise CliError(
        f"Der DocuShelf-Server läuft gerade{_server_label(info)}; {action} würde ihm in die Quere kommen.\n"
        f"{hint}\nMit --force trotzdem ausführen."
    )


# ---------------------------------------------------------------------------
# serve


def _layout_dirs(settings: "Settings", layout: "Layout") -> list[Path]:
    dirs = [
        layout.data,
        layout.archive,
        layout.inbox,
        layout.sources,
        layout.work,
        layout.ready,
        layout.failed,
        layout.done,
        layout.trash,
        layout.config,
        layout.audit,
        layout.index,
        layout.cache,
    ]
    for source in settings.sources:
        if source.path is None:
            dirs.append(layout.sources / source.name)
    return [Path(path) for path in dirs]


def check_data_dirs(settings: "Settings", layout: "Layout") -> None:
    """Fail early, with a German explanation, if the server could not write its data.

    The data directory is created if needed. A separately configured archive
    directory is not: if it is missing, the network share or disk is probably
    not mounted, and an empty archive must not be mistaken for the real one.
    """
    data_dir = Path(settings.data_dir)
    archive_dir = Path(settings.archive_dir)
    for path, label, create in (
        (data_dir, "Datenverzeichnis", True),
        (archive_dir, "Archivverzeichnis", archive_dir == data_dir / "archiv"),
    ):
        if path.is_dir():
            continue
        if path.exists():
            raise CliError(f"{label} {path} ist kein Verzeichnis.")
        if not create:
            raise CliError(
                f"{label} {path} existiert nicht. Ist das Laufwerk bzw. die Freigabe eingehängt? "
                "(DOCUSHELF_ARCHIVE_DIR)"
            )
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CliError(f"{label} {path} kann nicht angelegt werden ({_os_reason(exc)}).") from exc
    problems = []
    for path in _layout_dirs(settings, layout):
        if path.is_dir() and not os.access(path, os.R_OK | os.W_OK | os.X_OK):
            try:
                owner = f"gehört UID {path.stat().st_uid}"
            except OSError:
                owner = "nicht lesbar"
            problems.append(f"  {path} ({owner})")
    for source in settings.sources:
        if source.path is not None and not Path(source.path).is_dir():
            log.warning("watch folder %s (%s) does not exist (yet)", source.name, source.path)
    if problems:
        uid = os.geteuid() if hasattr(os, "geteuid") else "?"
        gid = os.getegid() if hasattr(os, "getegid") else "?"
        listing = "\n".join(problems[:12])
        raise CliError(
            f"DocuShelf läuft als UID {uid} (GID {gid}) und kann hier nicht schreiben:\n{listing}\n"
            f"Abhilfe auf dem Host: sudo chown {uid}:{gid} <Verzeichnis> "
            "(oder chown -R für den ganzen Datenordner)."
        )


def _serve_households(args: argparse.Namespace, settings: "Settings") -> "dict[str, Settings] | None":
    """Validate the households before the server starts; None without ``households.yaml``.

    Returns the settings of every household by id, after checking their data
    directories (see :func:`check_data_dirs`).
    """
    from .households import configured, load_households, settings_for

    if getattr(args, "household", None):
        _note("Hinweis: --haushalt gilt nicht für serve – der Server bedient alle Haushalte.")
    if not configured(settings):
        return None
    households = load_households(settings)
    root = Path(settings.data_dir)
    if not root.is_dir():
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CliError(f"Datenverzeichnis {root} kann nicht angelegt werden ({_os_reason(exc)}).") from exc
    result: dict[str, Settings] = {}
    for household in households:
        household_settings = settings_for(settings, household, households=households)
        check_data_dirs(household_settings, _layout(household_settings))
        result[household.id] = household_settings
    log.info("%d households: %s", len(households), ", ".join(h.id for h in households))
    return result


def _index_damaged(layout: "Layout") -> bool:
    """Whether the index database of ``layout`` exists and is damaged."""
    path = Path(layout.db_path)
    if not path.exists():
        return False
    try:
        connection = sqlite3.connect(str(path))
    except sqlite3.Error as exc:
        return _is_corrupt(exc)
    try:
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return False
    except sqlite3.Error as exc:
        return _is_corrupt(exc)
    finally:
        connection.close()


def _uvicorn_server(config: Any) -> Any:
    import uvicorn

    return uvicorn.Server(config)


def cmd_serve(args: argparse.Namespace) -> int:
    settings = _load_settings(args, for_server=True)
    overrides: dict[str, Any] = {}
    if args.host:
        overrides["host"] = args.host
    if args.port:
        overrides["port"] = args.port
    if overrides:
        settings = settings.with_overrides(**overrides)

    level = _parse_level(settings.log_level)
    if args.verbose:
        level = min(level, _verbosity_level(args.verbose))
    configure_logging(level)
    if args.access_log:
        access = logging.getLogger("uvicorn.access")
        if not any(isinstance(f, _HealthCheckAccessFilter) for f in access.filters):
            access.addFilter(_HealthCheckAccessFilter())

    layout = _layout(settings)
    households = _serve_households(args, settings)
    if households is None:
        check_data_dirs(settings, layout)
    from .util.timeutil import now_iso

    lock = ServerLock(Path(layout.index) / SERVER_LOCK_NAME)
    info = {
        "pid": os.getpid(),
        "host": settings.host,
        "port": settings.port,
        "started_at": now_iso(),
        "version": __version__,
    }
    if not lock.acquire(info):
        other = probe_server(layout) or {}
        raise CliError(
            f"Ein anderer DocuShelf-Server verwendet dieses Datenverzeichnis bereits{_server_label(other)}. "
            "Zwei Server würden sich gegenseitig stören."
        )
    try:
        import uvicorn

        from .web.app import create_app

        try:
            app = create_app(settings)
        except sqlite3.DatabaseError as exc:
            if not _is_corrupt(exc):
                raise
            log.warning("the index database is damaged (%s); it is set aside and rebuilt from the files", exc)
            if households is None:
                _set_aside_index(layout)
            else:
                for household_settings in households.values():
                    household_layout = _layout(household_settings)
                    if _index_damaged(household_layout):
                        _set_aside_index(household_layout)
            app = create_app(settings)

        config = uvicorn.Config(
            app,
            host=settings.host,
            port=settings.port,
            proxy_headers=True,
            forwarded_allow_ips=settings.trusted_proxies,
            access_log=bool(args.access_log),
            log_config=None,
            use_colors=False,
            lifespan="on",
            server_header=False,
            ws="none",
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
        )
        server = _uvicorn_server(config)
        log.info(
            "DocuShelf %s: data %s, archive %s, listening on %s:%d",
            __version__,
            settings.data_dir,
            settings.archive_dir,
            settings.host,
            settings.port,
        )
        try:
            server.run()
        except KeyboardInterrupt:
            pass  # Ctrl+C: uvicorn has already shut down gracefully
        except SystemExit as exc:
            # uvicorn exits this way when it cannot start, e.g. if the port is taken.
            if exc.code not in (None, 0):
                log.error("the server did not start")
                return EXIT_ERROR
        if not getattr(server, "started", False):
            log.error("the server did not start")
            return EXIT_ERROR
        log.info("DocuShelf stopped")
        return EXIT_OK
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# rebuild and reconcile


def cmd_rebuild(args: argparse.Namespace) -> int:
    root = _load_settings(args)
    household, settings = _select_household(args, root)
    layout = _layout(settings)
    _guard_server(
        _layout(root),
        force=args.force,
        action="der Neuaufbau des Index",
        hint="Den Neuaufbau in der Weboberfläche (Einstellungen → Index neu aufbauen) oder per API "
        "(POST /api/v1/admin/rebuild) starten, oder den Server vorher stoppen.",
    )
    _warn_if_root(settings)
    _note("Index wird aus den Dateien neu aufgebaut …")
    started = time.monotonic()
    services = _open_services(settings, replace_broken_index=True, household=household)
    try:
        try:
            stats = services.rebuild(user=CLI_USER)
        except sqlite3.DatabaseError as exc:
            if not _is_corrupt(exc):
                raise
            _close_services(services)
            _set_aside_index(layout)
            services = _open_services(settings, household=household)
            stats = services.rebuild(user=CLI_USER)
        waiting = services.inbox.counts().get("arbeit", 0)
    finally:
        _close_services(services)
    elapsed = (time.monotonic() - started) * 1000
    _out(
        f"Index neu aufgebaut in {_fmt_seconds(elapsed)}: "
        f"{_count(stats.added, 'Dokument', 'Dokumente')} in {_count(stats.folders_seen, 'Ordner', 'Ordnern')}."
    )
    if waiting:
        _out(f"Eingang: {_count(waiting, 'Dokument', 'Dokumente')} zur Verarbeitung vorgemerkt.")
    if stats.errors:
        _out(
            f"Achtung: {_count(stats.errors, 'Datei konnte', 'Dateien konnten')} nicht gelesen werden "
            "(Details mit -v). Ein erneuter Abgleich holt sie nach."
        )
        return EXIT_ERROR
    return EXIT_OK


def cmd_reconcile(args: argparse.Namespace) -> int:
    root = _load_settings(args)
    household, settings = _select_household(args, root)
    _guard_server(
        _layout(root),
        force=args.force,
        action="ein Abgleich",
        hint="Der laufende Server gleicht selbst ab. Einen sofortigen Abgleich in der Weboberfläche "
        "(Einstellungen) oder per API (POST /api/v1/admin/reconcile) starten, oder den Server vorher stoppen.",
    )
    _warn_if_root(settings)
    services = _open_services(settings, household=household)
    try:
        stats = services.reconciler.scan(full=True, verify=bool(args.verify))
    finally:
        _close_services(services)
    changes = ", ".join(
        f"{_fmt_int(value)} {label}"
        for value, label in (
            (stats.added, "neu"),
            (stats.updated, "geändert"),
            (stats.moved, "verschoben"),
            (stats.removed, "entfernt"),
        )
    )
    mode = " (alle Dateien neu gelesen)" if args.verify else ""
    _out(
        f"Abgleich abgeschlossen in {_fmt_seconds(stats.duration_ms)}{mode}: "
        f"{_count(stats.files_seen, 'Datei', 'Dateien')} geprüft, {changes}."
    )
    if stats.errors:
        _out(
            f"Achtung: {_count(stats.errors, 'Datei konnte', 'Dateien konnten')} nicht gelesen werden "
            "(gesperrt oder keine Rechte, Details mit -v). Sie bleiben im Index und werden beim nächsten Abgleich erneut geprüft."
        )
        return EXIT_ERROR
    return EXIT_OK


# ---------------------------------------------------------------------------
# import-paperless


def _supports_keyword(function: Callable[..., Any], name: str) -> bool:
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False
    return name in parameters or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())


def _import_line(entry: Any) -> str:
    status = _field(entry, "status") or ""
    label = IMPORT_LABELS.get(status, status)
    folder = _field(entry, "folder")
    message = _field(entry, "message")
    processing = _field(entry, "processing")  # which file is taken and whether OCR runs
    target = f" → {folder}" if folder else ""
    how = f" – {processing}" if processing else ""
    detail = f" ({message})" if message else ""
    return f"{label}: #{_field(entry, 'paperless_id')} {_field(entry, 'title')}{target}{how}{detail}"


class _ImportProgress:
    """Prints one line per document while the import runs."""

    def __init__(self, total: int):
        self.width = len(str(max(total, 1)))

    def __call__(self, position: int, total: int, entry: Any) -> None:
        _out(f"[{position:>{self.width}}/{total}] {_import_line(entry)}")


def _print_export_overview(export: Any) -> None:
    documents = list(getattr(export, "documents", []) or [])
    version = getattr(export, "version", None)
    suffix = f", Paperless-ngx {version}" if version else ""
    _out(f"Paperless-Export {getattr(export, 'directory', '')}: {_count(len(documents), 'Dokument', 'Dokumente')}{suffix}")

    def overview(attribute: str) -> str:
        counts = Counter((getattr(document, attribute, None) or "-") for document in documents)
        return ", ".join(
            f"{'- (ohne)' if name == '-' else name} ({_fmt_int(number)})" for name, number in counts.most_common()
        )

    if documents:
        _out(f"  Speicherpfade: {overview('storage_path')}")
        _out(f"  Besitzer:      {overview('owner')}")
    warnings = list(getattr(export, "warnings", []) or [])
    for warning in warnings[:20]:
        _note(f"Hinweis: {warning}")
    if len(warnings) > 20:
        _note(f"… und {_count(len(warnings) - 20, 'weiterer Hinweis', 'weitere Hinweise')}")


def _print_import_report(report: Any, *, quiet: bool, server_running: bool) -> None:
    from .importers.paperless import DELETED_REASON

    entries = list(getattr(report, "entries", []) or [])
    statuses = Counter(_field(entry, "status") for entry in entries)
    skipped_reasons = Counter(
        (_field(entry, "message") or "ohne Angabe") for entry in entries if _field(entry, "status") == "skipped"
    )
    rows: list[tuple[str, str]] = [("ausgewählt", _fmt_int(len(entries)))]
    if report.dry_run:
        rows.append(("geplant", _fmt_int(statuses["planned"])))
    else:
        rows.append(("im Eingang", _fmt_int(statuses["imported"])))
        if report.mode == "archive" or statuses["filed"]:
            rows.append(("abgelegt", _fmt_int(statuses["filed"])))
    with_ocr = sum(
        1 for entry in entries if _field(entry, "ocr") and _field(entry, "status") in ("planned", "imported", "filed")
    )
    rows.append(("davon mit OCR", _fmt_int(with_ocr)))
    skipped = _fmt_int(statuses["skipped"])
    if skipped_reasons:
        reasons = ", ".join(f"{number}× {reason}" for reason, number in skipped_reasons.most_common(5))
        more = len(skipped_reasons) - 5
        skipped += f"  ({reasons}{f', {more} weitere Gründe' if more > 0 else ''})"
    rows.append(("übersprungen", skipped))
    rows.append(("fehlgeschlagen", _fmt_int(statuses["failed"])))
    _out("")
    _out("Ergebnis:")
    for line in _aligned(rows):
        _out(f"  {line}")
    failed = [entry for entry in entries if _field(entry, "status") == "failed"]
    if failed and not quiet:
        _out("")
        _out("Fehlgeschlagen:")
        for entry in failed:
            _out(f"  {_import_line(entry)}")
    _out("")
    _out(report.summary())
    if report.dry_run:
        _out("Ordnerzuordnung bei Bedarf anpassen und den Import ohne --dry-run starten.")
    elif statuses["imported"]:
        if server_running:
            _out("Der Server verarbeitet die Dokumente jetzt; sie erscheinen nach und nach im Eingang.")
        else:
            _out("Der Server läuft nicht – die Verarbeitung beginnt beim nächsten Start („docushelf serve“).")
    deleted = skipped_reasons.get(DELETED_REASON, 0)
    if deleted:
        _out(
            f"{_count(deleted, 'Dokument wurde', 'Dokumente wurden')} in DocuShelf gelöscht und "
            f"{'liegt' if deleted == 1 else 'liegen'} im Papierkorb – mit --include-deleted "
            f"{'wird es' if deleted == 1 else 'werden sie'} trotzdem übernommen."
        )
    manual = [entry for entry in entries if _field(entry, "manual")]
    if manual and not quiet:
        _out("")
        _out("Von Hand übernehmen (Dateityp wird nicht unterstützt):")
        for entry in manual:
            source = _field(entry, "source_file")
            where = f" – {source}" if source else ""
            _out(f"  #{_field(entry, 'paperless_id')} {_field(entry, 'title')}{where}")


def cmd_import_paperless(args: argparse.Namespace) -> int:
    root = _load_settings(args)
    household, settings = _select_household(args, root)
    layout = _layout(root)  # the server lock is global
    if args.mode == "archive" and not args.dry_run:
        _guard_server(
            layout,
            force=args.force,
            action="ein Import mit direkter Ablage",
            hint="Ohne --mode archive importieren (die Dokumente landen im Eingang), oder den Server stoppen "
            "und den Import dann ausführen, z. B. mit „docker compose run --rm docushelf docushelf import-paperless …“.",
        )
    server = probe_server(layout)
    _warn_if_root(settings)
    services = _open_services(settings, household=household)
    try:
        from .importers.paperless import PaperlessImporter

        importer = PaperlessImporter(services)
        export = importer.load(Path(args.export_dir).expanduser())
        _print_export_overview(export)
        overrides = None
        if args.plan:
            from .importers.paperless import load_import_plan

            overrides = load_import_plan(Path(args.plan).expanduser())
            _out(f"Zuordnungsplan: {args.plan} ({_count(len(overrides), 'Eintrag', 'Einträge')})")
        plan = importer.plan(
            export,
            storage_paths=args.storage_paths or None,
            owners=args.owners or None,
            include_deleted=args.include_deleted,
            overrides=overrides,
            only_planned=args.only_planned,
        )
        _out(f"Ordnerzuordnung: {importer.mapping_path}")
        if getattr(importer, "mapping_is_old_default", None) and importer.mapping_is_old_default():
            _note(
                "Hinweis: Die Ordnerzuordnung enthält noch die unveränderten Vorgaben einer älteren Version "
                "(z. B. Steuerbescheid → „Steuern/{year}“, neu: „Steuern“). "
                "Die Datei löschen, dann wird sie mit den aktuellen Vorgaben neu angelegt."
            )
        if not plan:
            _out("Keine Dokumente ausgewählt – die Filter --storage-path und --owner prüfen.")
            return EXIT_OK
        if args.dry_run:
            heading = "Probelauf"
        elif args.mode == "archive":
            heading = "Import mit direkter Ablage"
        else:
            heading = "Import in den Eingang"
        _out(f"{heading}: {_count(len(plan), 'Dokument', 'Dokumente')} ausgewählt")
        options: dict[str, Any] = {"mode": args.mode, "dry_run": args.dry_run, "user": CLI_USER}
        if not args.quiet and _supports_keyword(importer.run, "progress"):
            options["progress"] = _ImportProgress(len(plan))
        report = importer.run(plan, **options)
    finally:
        _close_services(services)
    _print_import_report(report, quiet=args.quiet, server_running=server is not None)
    return EXIT_ERROR if report.failed else EXIT_OK


# ---------------------------------------------------------------------------
# hash-password


def _read_password(args: argparse.Namespace) -> str:
    if args.stdin:
        line = sys.stdin.readline()
        if not line:
            raise CliError("Auf der Standardeingabe kam kein Passwort an.")
        return line.rstrip("\r\n")
    import getpass

    if not sys.stdin.isatty():
        raise CliError("Kein Terminal für die Passworteingabe – mit --stdin das Passwort über die Standardeingabe übergeben.")
    first = getpass.getpass("Neues Passwort: ")
    second = getpass.getpass("Passwort wiederholen: ")
    if first != second:
        raise CliError("Die beiden Eingaben stimmen nicht überein.")
    return first


def cmd_hash_password(args: argparse.Namespace) -> int:
    password = _read_password(args)
    if not password:
        raise CliError("Das Passwort darf nicht leer sein.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise CliError(f"Das Passwort ist zu kurz (mindestens {MIN_PASSWORD_LENGTH} Zeichen).")
    from .web.security import hash_password

    hashed = hash_password(password)
    if args.env:
        _out(f"DOCUSHELF_ADMIN_PASSWORD_HASH='{hashed}'")
    else:
        _out(hashed)
    _note(
        "Hinweis: Der Hash enthält $-Zeichen. In der .env in einfache Anführungszeichen setzen "
        "(DOCUSHELF_ADMIN_PASSWORD_HASH='…'), direkt in einer Compose-Datei jedes $ als $$ schreiben."
    )
    return EXIT_OK


# ---------------------------------------------------------------------------
# token


def _token_store(settings: "Settings") -> Any:
    from .web.tokens import TokenStore

    layout = _layout(settings)
    Path(layout.config).mkdir(parents=True, exist_ok=True)
    return TokenStore(layout.tokens_file)


def _audit(settings: "Settings", action: str, **details: Any) -> None:
    from .audit import AuditLog

    AuditLog(_layout(settings).audit).write(CLI_USER, action, **details)


def cmd_token_create(args: argparse.Namespace) -> int:
    _, settings = _household_settings(args)
    _warn_if_root(settings)
    user = (args.user or settings.admin_user or "").strip()
    store = _token_store(settings)
    try:
        token, entry = store.create(args.name, user)
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    token_id = _field(entry, "id")
    _audit(settings, "token_create", token_id=token_id, name=_field(entry, "name"), token_user=_field(entry, "user"))
    _out(token)
    _note(
        f"Token „{_field(entry, 'name')}“ für {_field(entry, 'user')} erstellt (ID {token_id}). "
        "Es wird nur jetzt angezeigt – bitte sicher aufbewahren."
    )
    _note("Verwendung: HTTP-Header „Authorization: Bearer <Token>“, z. B. für POST /api/v1/upload.")
    return EXIT_OK


def cmd_token_list(args: argparse.Namespace) -> int:
    _, settings = _household_settings(args)
    store = _token_store(settings)
    entries = list(store.list())
    error = getattr(store, "error", None)
    if error:
        _note(f"Warnung: {error}")
    if not entries:
        _out("Keine API-Tokens vorhanden.")
        return EXIT_ERROR if error else EXIT_OK
    rows: list[tuple[str, ...]] = [("ID", "Name", "Benutzer", "Anfang", "Erstellt")]
    for entry in entries:
        prefix = _field(entry, "prefix")
        rows.append(
            (
                str(_field(entry, "id") or ""),
                str(_field(entry, "name") or ""),
                str(_field(entry, "user") or ""),
                f"{prefix}…" if prefix else "",
                _fmt_datetime(_field(entry, "created_at")),
            )
        )
    for line in _table(rows):
        _out(line)
    return EXIT_OK


def cmd_token_revoke(args: argparse.Namespace) -> int:
    _, settings = _household_settings(args)
    _warn_if_root(settings)
    store = _token_store(settings)
    try:
        entry = store.revoke(args.token_id)
    except NotFound as exc:
        raise CliError(
            f"Kein API-Token mit der ID „{args.token_id}“. Die IDs zeigt „docushelf token list“."
        ) from exc
    name = _field(entry, "name") if entry else None
    _audit(settings, "token_revoke", token_id=args.token_id, name=name)
    _out(f"Token „{name or args.token_id}“ ({args.token_id}) widerrufen.")
    return EXIT_OK


# ---------------------------------------------------------------------------
# healthcheck


def _connect_host(host: str) -> str:
    """Address to reach a server that listens on ``host``."""
    host = (host or "").strip()
    if host in ("", "0.0.0.0", "*"):
        return "127.0.0.1"
    if host in ("::", "[::]"):
        return "[::1]"
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def health_url(args: argparse.Namespace) -> str:
    try:
        settings = _load_settings(args)
        host, port = settings.host, settings.port
    except Exception:
        # A broken configuration must not hide whether the server answers.
        host = os.environ.get("DOCUSHELF_HOST", "0.0.0.0")
        try:
            port = int(os.environ.get("DOCUSHELF_PORT") or 8000)
        except ValueError:
            port = 8000
    return f"http://{_connect_host(host)}:{port}{HEALTH_PATH}"


def _short_body(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        return " ".join(text.split())[:200]
    if isinstance(data, dict):
        checks = data.get("checks")
        failing = {k: v for k, v in checks.items() if v != "ok"} if isinstance(checks, dict) else {}
        if failing:
            return "; ".join(f"{key}: {value}" for key, value in failing.items())[:300]
        return str(data.get("status", ""))
    return text[:200]


def check_health(url: str, *, timeout: float = HEALTH_TIMEOUT) -> tuple[bool, str]:
    """GET ``url``; healthy means HTTP 200. Returns ``(healthy, German description)``."""
    import urllib.error
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: Any, **kwargs: Any) -> None:
            return None

    # No proxies from the environment: the server is local.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(
        url, headers={"User-Agent": f"docushelf-healthcheck/{__version__}", "Accept": "application/json"}
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            body = response.read(4096)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(4096)
        except Exception:
            body = b""
        detail = _short_body(body)
        return False, f"Nicht bereit: HTTP {exc.code}" + (f" – {detail}" if detail else "")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", None) or exc
        return False, f"Nicht erreichbar: {url} ({reason})"
    if status != 200:
        return False, f"Nicht bereit: HTTP {status}"
    detail = _short_body(body)
    return True, "OK" + (f" ({detail})" if detail and detail != "ok" else "")


def cmd_healthcheck(args: argparse.Namespace) -> int:
    url = args.url or health_url(args)
    healthy, description = check_health(url, timeout=args.timeout)
    _out(description)
    return EXIT_OK if healthy else EXIT_ERROR


# ---------------------------------------------------------------------------
# status


def _summarize_jobs(raw: dict[str, int]) -> dict[str, Any]:
    summary: dict[str, Any] = {"queued": 0, "running": 0, "failed": 0, "by_kind": {}}
    for key, number in (raw or {}).items():
        kind, _, state = str(key).partition(":")
        if state in ("queued", "running", "failed"):
            summary[state] += int(number)
        summary["by_kind"].setdefault(kind, {})[state] = int(number)
    return summary


def _index_status(layout: "Layout") -> dict[str, Any]:
    path = Path(layout.db_path)
    if not path.exists():
        return {"available": False, "error": None}
    from .index.db import Index

    index = Index(path)
    try:
        stats = dict(index.stats())
    except sqlite3.Error as exc:
        return {"available": False, "error": str(exc)}
    finally:
        index.close()
    jobs = stats.pop("jobs", {}) or {}
    return {"available": True, "error": None, **stats, "jobs": _summarize_jobs(jobs)}


def _trash_count(layout: "Layout") -> int:
    try:
        with os.scandir(layout.trash) as entries:
            return sum(1 for entry in entries if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."))
    except OSError:
        return 0


def _ocr_available() -> bool:
    import shutil

    return shutil.which("ocrmypdf") is not None and shutil.which("tesseract") is not None


def collect_status(
    settings: "Settings", layout: "Layout", *, server_layout: "Layout | None" = None, household: Any = None
) -> dict[str, Any]:
    """Everything ``status`` shows, as plain data (also the ``--json`` output).

    ``server_layout`` is where the (global) server lock lives, if not in ``layout``;
    ``household`` is only given with several households.
    """
    from .inbox.items import InboxStore

    server = probe_server(server_layout or layout)
    data = {
        "version": __version__,
        "data_dir": str(settings.data_dir),
        "archive_dir": str(settings.archive_dir),
        "server": {"running": server is not None, **(server or {})},
        "index": _index_status(layout),
        "inbox": InboxStore(layout).counts(),
        "trash": _trash_count(layout),
        "ocr": {"enabled": bool(settings.ocr_enabled), "available": _ocr_available()},
        "login": {"oidc": bool(settings.oidc_enabled), "local": bool(settings.local_login_enabled)},
        "channels": {
            "sources": [source.name for source in settings.sources],
            "imap": settings.imap.host if settings.imap else None,
            "smtp_port": settings.smtp.port if settings.smtp else None,
            "llm_model": settings.llm.model if settings.llm else None,
        },
    }
    if household is not None:
        data["household"] = {"id": household.id, "name": household.name}
    return data


def format_status(data: dict[str, Any]) -> list[str]:
    rows: list[tuple[str, str]] = []
    household = data.get("household")
    if household:
        rows.append(("Haushalt", f"{household['name']} ({household['id']})"))
    rows.append(("Daten", data["data_dir"]))
    if Path(data["archive_dir"]) != Path(data["data_dir"]) / "archiv":
        rows.append(("Archiv", data["archive_dir"]))

    server = data["server"]
    if server.get("running"):
        details = [f"PID {server['pid']}"] if server.get("pid") else []
        if server.get("port"):
            details.append(f"Port {server['port']}")
        if server.get("started_at"):
            details.append(f"seit {_fmt_datetime(server['started_at'])}")
        rows.append(("Server", "läuft" + (f" ({', '.join(details)})" if details else "")))
    else:
        rows.append(("Server", "gestoppt"))

    index = data["index"]
    if index.get("available"):
        text = (
            f"{_count(index.get('documents', 0), 'Dokument', 'Dokumente')} · "
            f"{_count(index.get('pages', 0), 'Seite', 'Seiten')} · "
            f"{_count(index.get('folders', 0), 'Ordner', 'Ordner')}"
        )
        if index.get("without_text"):
            text += f" ({_fmt_int(index['without_text'])} ohne Text)"
        rows.append(("Index", text))
        rows.append(
            (
                "Abgleich",
                f"zuletzt {_fmt_datetime(index.get('last_scan'))} · vollständig {_fmt_datetime(index.get('last_full_scan'))}",
            )
        )
        jobs = index.get("jobs") or {}
        job_text = " · ".join(f"{_fmt_int(jobs.get(state, 0))} {label}" for state, label in JOB_STATE_LABELS.items())
        kinds = [
            f"{JOB_KIND_LABELS.get(kind, kind)} {_fmt_int(sum(v for s, v in states.items() if s in ('queued', 'running')))}"
            for kind, states in sorted((jobs.get("by_kind") or {}).items())
            if any(s in ("queued", "running") for s in states)
        ]
        if kinds:
            job_text += f" ({', '.join(kinds)})"
        rows.append(("Aufträge", job_text))
    elif index.get("error"):
        rows.append(("Index", f"nicht lesbar ({index['error']}) – „docushelf rebuild“ baut ihn neu auf"))
    else:
        rows.append(("Index", "noch nicht angelegt (entsteht beim ersten Start des Servers)"))

    inbox = data["inbox"]
    rows.append(("Eingang", " · ".join(f"{_fmt_int(inbox.get(state, 0))} {label}" for state, label in INBOX_LABELS.items())))
    rows.append(("Papierkorb", _count(data["trash"], "Eintrag", "Einträge")))

    ocr = data["ocr"]
    if not ocr["enabled"]:
        rows.append(("OCR", "ausgeschaltet"))
    else:
        rows.append(("OCR", "bereit" if ocr["available"] else "eingeschaltet, aber ocrmypdf/tesseract fehlen"))

    login = data["login"]
    methods = [name for name, active in (("SSO (OIDC)", login["oidc"]), ("lokaler Notfallzugang", login["local"])) if active]
    rows.append(("Anmeldung", ", ".join(methods) if methods else "nicht eingerichtet"))

    channels = data["channels"]
    names = {"upload": "Upload", "smb": "SMB", "ftp": "FTP", "mail": "Mail", "smtp": "SMTP"}
    sources = [names.get(name, name) for name in channels["sources"] if name not in ("mail", "smtp")]
    if channels.get("imap"):
        sources.append(f"IMAP ({channels['imap']})")
    if channels.get("smtp_port"):
        sources.append(f"SMTP-Empfang (Port {channels['smtp_port']})")
    rows.append(("Eingänge", ", ".join(sources)))
    if channels.get("llm_model"):
        rows.append(("KI-Vorschläge", channels["llm_model"]))
    return [f"DocuShelf {data['version']}", *(f"  {line}" for line in _aligned(rows))]


def cmd_status(args: argparse.Namespace) -> int:
    from .households import configured

    root = _load_settings(args)
    household, settings = _select_household(args, root)
    data = collect_status(
        settings,
        _layout(settings),
        server_layout=_layout(root),
        household=household if configured(root) else None,
    )
    if args.json:
        _out(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        for line in format_status(data):
            _out(line)
    return EXIT_OK


# ---------------------------------------------------------------------------
# process


def _flag(value: bool) -> bool | None:
    """Command line switches only ever turn an option on; None keeps the item's setting."""
    return True if value else None


def _log_size(item: Any) -> int:
    try:
        return item.log_path.stat().st_size
    except OSError:
        return 0


def _print_log_since(item: Any, offset: int) -> None:
    from .util.timeutil import parse_iso_datetime

    try:
        with open(item.log_path, "rb") as handle:
            handle.seek(offset)
            text = handle.read(256_000).decode("utf-8", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        if not line.strip():
            continue
        stamp, _, message = line.partition(" ")
        moment = parse_iso_datetime(stamp)
        _out(f"  {moment:%H:%M:%S} {message}" if moment and message else f"  {line}")


def _drop_queued_jobs(index: Any, kind: str, key: str) -> None:
    """Remove queued jobs that became pointless because the item was processed here."""
    try:
        for job in index.pending_jobs(kind):
            if job.key == key and job.state == "queued":
                index.complete(job.id)
    except Exception:
        log.debug("cannot clean up queued %s jobs for %s", kind, key, exc_info=True)


def _print_process_result(item: Any) -> int:
    if item.state == "bereit":
        pages = f" ({_count(item.page_count, 'Seite', 'Seiten')})" if item.page_count else ""
        _out(f"Ergebnis: bereit zur Ablage{pages}.")
        top = item.top_suggestion
        if top:
            score = top.get("score")
            share = f", {round(float(score) * 100)} %" if isinstance(score, (int, float)) else ""
            new = " (neuer Ordner)" if top.get("new_folder") else ""
            title = f" – „{top['title']}“" if top.get("title") else ""
            _out(f"Vorschlag: {top.get('folder') or '–'}{new}{title}{share}")
            reasons = [str(reason) for reason in top.get("reasons") or []]
            if reasons:
                _out(f"  weil: {'; '.join(reasons[:3])}")
        duplicate = item.duplicate_of
        if duplicate:
            where = duplicate.get("path") or duplicate.get("title") or duplicate.get("id") or ""
            kind = "im Archiv" if duplicate.get("kind") == "archive" else "im Eingang"
            _out(f"Achtung: mögliches Duplikat {kind}: {where}")
        return EXIT_OK
    if item.state == "fehler":
        error = item.error or {}
        stage = f" ({error['stage']})" if error.get("stage") else ""
        _out(f"Ergebnis: Fehler{stage}: {error.get('message') or 'unbekannter Fehler'}")
        return EXIT_ERROR
    _out(f"Ergebnis: {INBOX_LABELS.get(item.state, item.state)} (Stufe {item.stage}).")
    return EXIT_ERROR


def _hand_over(services: Any, item: Any, args: argparse.Namespace) -> int:
    """Let the running server process the item; never process it here at the same time."""
    if item.state in ("fehler", "bereit"):
        services.pipeline.retry(
            item.id, skip_ocr=_flag(args.skip_ocr), skip_blank_removal=_flag(args.skip_blank_removal)
        )
        _out(f"Der Server läuft: {item.id} wird erneut verarbeitet und ist in die Warteschlange gestellt.")
    else:
        if args.skip_ocr or args.skip_blank_removal:
            _note("Hinweis: --skip-ocr/--skip-blank-removal gelten nur für Dokumente im Bereich „bereit“ oder „Fehler“.")
        if services.index.enqueue("process", item.id):
            _out(f"Der Server läuft: {item.id} ist in die Warteschlange gestellt.")
        else:
            _out(f"Der Server läuft: {item.id} wartet bereits auf die Verarbeitung oder wird gerade verarbeitet.")
    _out("Das Ergebnis erscheint im Eingang der Weboberfläche.")
    return EXIT_OK


def cmd_process(args: argparse.Namespace) -> int:
    from .util.ids import is_item_id

    if not is_item_id(args.item_id):
        raise CliError(f"„{args.item_id}“ ist keine Eingangs-ID (Form JJJJMMTT-hhmmss-xxxxxx).")
    root = _load_settings(args)
    household, settings = _select_household(args, root)
    _warn_if_root(settings)
    server = probe_server(_layout(root))  # the server lock is global
    services = _open_services(settings, household=household)
    try:
        item = services.inbox.find(args.item_id)
        if item is None:
            raise CliError(f"Das Eingangsdokument {args.item_id} gibt es nicht.")
        if item.state == "erledigt":
            raise CliError(f"{args.item_id} wurde bereits abgelegt.")
        if server is not None:
            return _hand_over(services, item, args)

        pipeline = services.pipeline
        if item.state in ("fehler", "bereit"):
            item = pipeline.retry(item.id, skip_ocr=_flag(args.skip_ocr), skip_blank_removal=_flag(args.skip_blank_removal))
        elif args.skip_ocr or args.skip_blank_removal:
            if args.skip_ocr:
                item.options["skip_ocr"] = True
            if args.skip_blank_removal:
                item.options["skip_blank_removal"] = True
            services.inbox.save(item)
        offset = _log_size(item)
        _note(f"Verarbeite {item.id} ({item.display_name}) …")
        try:
            result = pipeline.process(item.id)
        except ProcessingError as exc:
            # Only transient errors get here; the item stays in "arbeit".
            current = services.inbox.find(item.id) or item
            _print_log_since(current, offset)
            services.index.enqueue("process", item.id)
            raise CliError(
                f"Vorübergehender Fehler: {exc}. Das Dokument bleibt in Verarbeitung; "
                "der Server versucht es beim nächsten Start erneut."
            ) from exc
        _drop_queued_jobs(services.index, "process", item.id)
        _print_log_since(result, offset)
        return _print_process_result(result)
    finally:
        _close_services(services)


# ---------------------------------------------------------------------------
# households


def _access_rules(settings: "Settings", household: Any, *, single: bool) -> str:
    """Who may use a household, for people."""
    if single:
        if not (settings.allowed_users or settings.allowed_groups):
            return "alle SSO-Benutzer" if settings.allow_all_oidc_users else "nur lokaler Administrator"
    return household.access


def collect_households(settings: "Settings") -> list[dict[str, Any]]:
    """The households with path, access rules and document count (``households`` command)."""
    from .households import configured, load_households, settings_for

    households = load_households(settings)
    single = not configured(settings)
    result = []
    for household in households:
        household_settings = settings_for(settings, household, households=households)
        layout = _layout(household_settings)
        index = _index_status(layout)
        result.append(
            {
                "id": household.id,
                "name": household.name,
                "path": str(household.path),
                "archive_dir": str(household_settings.archive_dir),
                "groups": list(settings.allowed_groups if single else household.groups),
                "users": list(settings.allowed_users if single else household.users),
                "access": _access_rules(settings, household, single=single),
                "documents": index.get("documents") if index.get("available") else None,
            }
        )
    return result


def cmd_households(args: argparse.Namespace) -> int:
    from .households import configured

    settings = _load_settings(args)
    entries = collect_households(settings)
    if args.json:
        _out(json.dumps(entries, ensure_ascii=False, indent=2))
        return EXIT_OK
    rows: list[tuple[str, ...]] = [("ID", "Name", "Pfad", "Zugriff", "Dokumente")]
    for entry in entries:
        documents = entry["documents"]
        rows.append(
            (
                entry["id"],
                entry["name"],
                entry["path"],
                entry["access"],
                _fmt_int(documents) if documents is not None else "–",
            )
        )
    for line in _table(rows):
        _out(line)
    if not configured(settings):
        _note("Ein einzelner Haushalt (keine config/households.yaml); Zugriff über DOCUSHELF_ALLOWED_USERS/_GROUPS.")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Argument parsing


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    def add_usage(self, usage: Any, actions: Any, groups: Any, prefix: str | None = None) -> None:
        super().add_usage(usage, actions, groups, "Aufruf: " if prefix is None else prefix)


class _Parser(argparse.ArgumentParser):
    """ArgumentParser with German headings and error prefix."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("formatter_class", _HelpFormatter)
        kwargs.setdefault("add_help", False)
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)
        self._positionals.title = "Argumente"
        self._optionals.title = "Optionen"
        self.add_argument("-h", "--help", action="help", default=argparse.SUPPRESS, help="diese Hilfe anzeigen")

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: Fehler: {message}\n")


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"ungültiger Port: {value}") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"ungültiger Port: {value}")
    return port


def _positive_float(value: str) -> float:
    try:
        number = float(value.replace(",", "."))
    except ValueError:
        raise argparse.ArgumentTypeError(f"keine Zahl: {value}") from None
    if number <= 0:
        raise argparse.ArgumentTypeError(f"muss größer als 0 sein: {value}")
    return number


def _show_help(parser: argparse.ArgumentParser) -> Callable[[argparse.Namespace], int]:
    def show(args: argparse.Namespace) -> int:
        parser.print_help(sys.stderr)
        return EXIT_USAGE

    return show


def _household_option(parser: argparse.ArgumentParser) -> None:
    """``--haushalt`` also after the command name (the global option stays when it is not given)."""
    parser.add_argument(
        "--haushalt",
        "--household",
        dest="household",
        metavar="ID",
        default=argparse.SUPPRESS,
        help="Haushalt, auf den sich der Befehl bezieht",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="docushelf",
        description="DocuShelf – ordnerbasiertes Dokumentenarchiv mit OCR, Eingang und Volltextsuche.",
        epilog="Konfiguration über Umgebungsvariablen DOCUSHELF_* (siehe deploy/.env.example).",
    )
    parser.add_argument("--version", action="version", version=f"DocuShelf {__version__}", help="Version anzeigen")
    parser.add_argument("--data-dir", metavar="PFAD", help="Datenverzeichnis (statt DOCUSHELF_DATA_DIR)")
    parser.add_argument(
        "--haushalt",
        "--household",
        dest="household",
        metavar="ID",
        help="Haushalt, auf den sich der Befehl bezieht (nötig bei mehreren Haushalten)",
    )
    parser.add_argument(
        "-v", "--verbose", action="count", default=0, help="mehr Protokollausgaben (-vv für Debug-Ausgaben)"
    )
    commands = parser.add_subparsers(dest="command", metavar="BEFEHL", title="Befehle")

    serve = commands.add_parser(
        "serve",
        help="Weboberfläche und Hintergrunddienste starten",
        description="Startet Weboberfläche, API und alle Hintergrunddienste (Verarbeitung, Eingangskanäle, Abgleich).",
    )
    serve.add_argument("--host", help="Adresse, auf der der Server lauscht (statt DOCUSHELF_HOST)")
    serve.add_argument("--port", type=_port, help="Port (statt DOCUSHELF_PORT)")
    serve.add_argument("--access-log", action="store_true", help="jede HTTP-Anfrage protokollieren")
    serve.set_defaults(func=cmd_serve)

    rebuild = commands.add_parser(
        "rebuild",
        help="Index verwerfen und aus den Dateien neu aufbauen",
        description="Löscht den Suchindex und liest Archiv und Eingang vollständig neu ein. "
        "Dokumente mit DocuShelf-Metadaten behalten ihre ID.",
    )
    rebuild.add_argument("--force", action="store_true", help="auch ausführen, wenn der Server läuft")
    _household_option(rebuild)
    rebuild.set_defaults(func=cmd_rebuild)

    reconcile = commands.add_parser(
        "reconcile",
        help="Index einmal mit dem Archiv abgleichen",
        description="Gleicht den Index mit dem Archivordner ab (neue, geänderte, verschobene, gelöschte Dateien).",
    )
    reconcile.add_argument("--verify", action="store_true", help="alle Dateien neu lesen, auch unveränderte")
    reconcile.add_argument("--force", action="store_true", help="auch ausführen, wenn der Server läuft")
    _household_option(reconcile)
    reconcile.set_defaults(func=cmd_reconcile)

    paperless = commands.add_parser(
        "import-paperless",
        help="Export aus Paperless-ngx übernehmen",
        description="Übernimmt einen Export von Paperless-ngx (document_exporter). Standardmäßig landen die "
        "Dokumente mit Ablagevorschlag im Eingang; die Ordner ergeben sich aus config/paperless-import.yaml.",
    )
    paperless.add_argument("export_dir", metavar="EXPORT_DIR", help="Verzeichnis mit manifest.json")
    paperless.add_argument(
        "--storage-path",
        dest="storage_paths",
        action="append",
        default=[],
        metavar="NAME",
        help="nur Dokumente mit diesem Speicherpfad (mehrfach möglich, „-“ = ohne Speicherpfad)",
    )
    paperless.add_argument(
        "--owner",
        dest="owners",
        action="append",
        default=[],
        metavar="NAME",
        help="nur Dokumente dieses Besitzers (mehrfach möglich, „-“ = ohne Besitzer)",
    )
    paperless.add_argument(
        "--mode",
        choices=("inbox", "archive"),
        default="inbox",
        help="inbox: in den Eingang zur Bestätigung (Standard); archive: direkt ablegen",
    )
    paperless.add_argument(
        "--include-deleted",
        action="store_true",
        help="auch Dokumente übernehmen, die in DocuShelf gelöscht wurden und im Papierkorb liegen",
    )
    paperless.add_argument(
        "--plan",
        metavar="DATEI",
        help="Zuordnungsplan (JSON) mit Ordner, Titel, Datum, Absender und Tags je Paperless-Dokument",
    )
    paperless.add_argument(
        "--only-planned",
        action="store_true",
        help="nur Dokumente übernehmen, die im Zuordnungsplan stehen",
    )
    paperless.add_argument("--dry-run", action="store_true", help="nur anzeigen, was passieren würde")
    paperless.add_argument("-q", "--quiet", action="store_true", help="nur die Zusammenfassung ausgeben")
    paperless.add_argument("--force", action="store_true", help="--mode archive auch bei laufendem Server")
    _household_option(paperless)
    paperless.set_defaults(func=cmd_import_paperless)

    password = commands.add_parser(
        "hash-password",
        help="Passwort-Hash für DOCUSHELF_ADMIN_PASSWORD_HASH erzeugen",
        description="Fragt ein Passwort zweimal ab und gibt den scrypt-Hash für den lokalen Notfallzugang aus.",
    )
    password.add_argument("--stdin", action="store_true", help="Passwort von der Standardeingabe lesen (eine Zeile)")
    password.add_argument("--env", action="store_true", help="als fertige Zeile für die .env ausgeben")
    password.set_defaults(func=cmd_hash_password)

    token = commands.add_parser(
        "token", help="API-Tokens verwalten", description="API-Tokens für Skripte und Kurzbefehle verwalten."
    )
    token.set_defaults(func=_show_help(token))
    actions = token.add_subparsers(dest="token_command", metavar="AKTION", title="Aktionen")
    create = actions.add_parser("create", help="neues Token erzeugen (wird nur einmal angezeigt)")
    create.add_argument("name", metavar="NAME", help="Bezeichnung, z. B. „iPhone Kurzbefehl“")
    create.add_argument(
        "--user", metavar="BENUTZER", help="Benutzer, in dessen Namen das Token handelt (Standard: DOCUSHELF_ADMIN_USER)"
    )
    _household_option(create)
    create.set_defaults(func=cmd_token_create)
    listing = actions.add_parser("list", help="Tokens auflisten")
    _household_option(listing)
    listing.set_defaults(func=cmd_token_list)
    revoke = actions.add_parser("revoke", help="Token widerrufen")
    revoke.add_argument("token_id", metavar="ID", help="ID aus „docushelf token list“")
    _household_option(revoke)
    revoke.set_defaults(func=cmd_token_revoke)

    health = commands.add_parser(
        "healthcheck",
        help="prüfen, ob der Server antwortet (für Docker)",
        description=f"Ruft {HEALTH_PATH} des laufenden Servers auf. Exit-Code 0 = gesund, 1 = nicht bereit.",
    )
    health.add_argument("--url", help=f"zu prüfende URL (Standard: http://127.0.0.1:<Port>{HEALTH_PATH})")
    health.add_argument(
        "--timeout", type=_positive_float, default=HEALTH_TIMEOUT, help="Zeitlimit in Sekunden (Standard: 5)"
    )
    health.set_defaults(func=cmd_healthcheck)

    status = commands.add_parser("status", help="Überblick über Index, Eingang und Aufträge")
    status.add_argument("--json", action="store_true", help="maschinenlesbar ausgeben")
    _household_option(status)
    status.set_defaults(func=cmd_status)

    households = commands.add_parser(
        "households",
        aliases=["haushalte"],
        help="Haushalte mit Pfad, Zugriff und Anzahl der Dokumente auflisten",
        description="Listet die Haushalte aus config/households.yaml (ohne die Datei: den einen Haushalt).",
    )
    households.add_argument("--json", action="store_true", help="maschinenlesbar ausgeben")
    households.set_defaults(func=cmd_households)

    process = commands.add_parser(
        "process",
        help="ein Eingangsdokument sofort verarbeiten",
        description="Verarbeitet ein Eingangsdokument jetzt (Dokumente aus „Fehler“ und „bereit“ von vorn). "
        "Läuft der Server, übernimmt er die Verarbeitung.",
    )
    process.add_argument("item_id", metavar="ITEM_ID", help="ID des Eingangsdokuments, z. B. 20261123-125100-a1b2c3")
    process.add_argument("--skip-ocr", action="store_true", help="ohne Texterkennung")
    process.add_argument("--skip-blank-removal", action="store_true", help="ohne Leerseitenerkennung")
    _household_option(process)
    process.set_defaults(func=cmd_process)
    return parser


# ---------------------------------------------------------------------------
# Entry point


def _silence_stdout() -> None:
    """After a broken pipe (``docushelf status | head``) further output must not raise again."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError):
        pass


def _message(exc: BaseException) -> str:
    text = str(exc).strip()
    if isinstance(exc, NotFound) and (not text or " " not in text):
        return f"nicht gefunden: {text}" if text else "nicht gefunden"
    return text or type(exc).__name__


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    if func is not cmd_serve:
        configure_logging(_verbosity_level(args.verbose))
    try:
        return int(func(args) or EXIT_OK)
    except KeyboardInterrupt:
        _note("\nAbgebrochen.")
        return EXIT_INTERRUPTED
    except BrokenPipeError:
        _silence_stdout()
        return EXIT_ERROR
    except DocuShelfError as exc:
        _note(f"Fehler: {_message(exc)}")
        return EXIT_ERROR
    except OSError as exc:
        where = f"{exc.filename}: " if getattr(exc, "filename", None) else ""
        _note(f"Fehler: {where}{_os_reason(exc)}")
        if args.verbose:
            traceback.print_exc()
        return EXIT_ERROR
    except Exception:
        traceback.print_exc()
        _note("Fehler: unerwarteter Programmfehler (Details siehe oben).")
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
