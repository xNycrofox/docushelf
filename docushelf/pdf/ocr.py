"""OCR with OCRmyPDF/Tesseract, run as a separate process.

The ``ocrmypdf`` command line tool runs in its own process group with a
timeout, so a hanging or runaway OCR job can always be stopped completely
(including the Tesseract processes it started) without affecting the
application. Its output is passed on line by line as progress messages.

The time limit grows with the length of the document: ``settings.ocr_timeout``
but at least :data:`SECONDS_PER_PAGE` per page. Exceeding it is a permanent
failure (:class:`OcrTimeout`); running the same job again with the same
limit would only keep the CPU busy for as long and fail again.

OCRmyPDF's temporary files go to a work directory under ``<data>/index/tmp``
(on the data volume, not in the system's temp directory). It is removed when
the run ends; directories left behind by a process that died meanwhile are
removed by :func:`remove_stale_workdirs`. While a run is going on, its
directory's modification time is refreshed every :data:`HEARTBEAT_SECONDS`,
so a directory that has not changed for long belongs to nobody.

OCR is CPU heavy: at most ``settings.ocr_parallel`` runs (``DOCUSHELF_OCR_PARALLEL``,
default 1) go on at the same time in the whole process, whatever household or
worker thread they come from (:data:`OCR_SLOTS`).
"""

from __future__ import annotations

import atexit
import codecs
import errno
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pikepdf

from ..config import Settings
from ..errors import ProcessingError
from ..layout import Layout
from ..util import fsops

log = logging.getLogger(__name__)
# run_ocr() has a ``log`` parameter (the progress callback) that shadows the
# module logger inside it; code reachable from there uses this name instead.
_logger = log

LogCallback = Callable[[str], None]

# Process-wide limit for concurrent ocrmypdf runs (all households share it).
# Replaced by configure_ocr_slots(); a run always releases the object it acquired.
OCR_SLOTS = threading.BoundedSemaphore(1)
_ocr_slots_size = 1
_ocr_slots_lock = threading.Lock()


def configure_ocr_slots(size: int) -> None:
    """Allow ``size`` concurrent OCR runs from now on (runs already going on are not affected)."""
    global OCR_SLOTS, _ocr_slots_size
    size = max(1, int(size or 1))
    with _ocr_slots_lock:
        if size == _ocr_slots_size:
            return
        OCR_SLOTS = threading.BoundedSemaphore(size)
        _ocr_slots_size = size

TESSERACT_TIMEOUT = 180
# The time limit of a run is settings.ocr_timeout, but at least this much per page.
SECONDS_PER_PAGE = 10
# Work directories: <data>/index/tmp/ocr-<pid>-<random>.
WORKDIR_PREFIX = "ocr-"
# A running job refreshes the modification time of its work directory this often.
HEARTBEAT_SECONDS = 60.0
# Leftover work directories are removed once unchanged for ocr_timeout plus this margin.
STALE_MARGIN_SECONDS = 600
# Work directories of earlier versions, directly in the system's temp directory.
_LEGACY_PREFIX = "docushelf-ocr-"
_TERMINATE_GRACE = 5.0
_READER_JOIN = 10.0
_MAX_LOG_LINES = 400
_TAIL_LINES = 200
_ERROR_TAIL_LINES = 8
_ERROR_TAIL_CHARS = 1200
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")

_EXIT_DESCRIPTIONS = {
    1: "ungültige Parameter",
    2: "Eingabedatei nicht verarbeitbar",
    3: "fehlendes Hilfsprogramm oder Sprachpaket",
    4: "ungültige Ausgabedatei",
    5: "Dateizugriffsfehler",
    7: "Fehler in einem Hilfsprogramm (Tesseract/Ghostscript)",
    9: "ungültige Konfiguration",
    15: "unerwarteter Fehler",
}

_MODE_FLAGS = {"-f", "--force-ocr", "-s", "--skip-text", "--redo-ocr", "-m", "--mode"}

# Reasons for not running OCR (German, shown in the item log after "OCR übersprungen:").
REASON_DISABLED = "OCR ist deaktiviert"
REASON_MISSING_OCRMYPDF = "ocrmypdf ist nicht installiert"
REASON_MISSING_TESSERACT = "tesseract ist nicht installiert"
REASON_SIGNED = "PDF ist digital signiert (unverändert übernommen, damit die Signatur gültig bleibt)"
REASON_HAS_TEXT = "Dokument enthält bereits Text"
REASON_KEEP_AS_IS = "PDF enthält bereits Text und ist PDF/A oder hat Dateianhänge (unverändert übernommen)"
REASON_FONTS = "PDF enthält nicht eingebettete Schriften (unverändert übernommen)"


@dataclass
class OcrResult:
    """Outcome of ``run_ocr``. ``output`` always exists afterwards.

    ``performed`` is False when the input was copied unchanged; then
    ``skipped_reason`` says why (German, for the item log). ``warnings`` are
    German notes about a result that is usable but not as intended.
    """

    performed: bool
    output: Path
    skipped_reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    returncode: int = 0
    duration_s: float = 0.0


class OcrTimeout(ProcessingError):
    """OCR did not finish within its time limit.

    Not transient: a new attempt with the same limit would fail the same way
    after keeping the CPU busy just as long. The message tells the user what
    to do instead (raise the limit, or process the document without OCR).
    """

    def __init__(self, message: str, *, timeout: int, pages: int):
        super().__init__(message, transient=False)
        self.timeout = timeout
        self.pages = pages


def time_limit(settings: Settings, pages: int) -> int:
    """Seconds a run on a document with ``pages`` pages may take."""
    configured = max(1, int(settings.ocr_timeout or 1))
    return max(configured, max(0, int(pages or 0)) * SECONDS_PER_PAGE)


def _timeout_message(timeout: int, pages: int, tail: str) -> str:
    size = f" ({pages} Seiten)" if pages > 1 else ""
    text = (
        f"OCR hat das Zeitlimit von {timeout} s überschritten{size}. Du kannst das Zeitlimit mit "
        "DOCUSHELF_OCR_TIMEOUT (Sekunden) erhöhen oder das Dokument „ohne OCR“ erneut verarbeiten."
    )
    return f"{text} Letzte Ausgabe: {tail}" if tail else text


# ---------------------------------------------------------------------------
# Locating the tools


def _ocrmypdf_command() -> list[str] | None:
    """Command that starts ocrmypdf: from PATH, else next to the running interpreter."""
    found = shutil.which("ocrmypdf")
    if found:
        return [found]
    sibling = Path(sys.executable).parent / "ocrmypdf"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return [str(sibling)]
    return None


def _tesseract_command() -> list[str] | None:
    found = shutil.which("tesseract")
    return [found] if found else None


def ocr_available() -> bool:
    """True if ocrmypdf and tesseract can be started."""
    return _ocrmypdf_command() is not None and _tesseract_command() is not None


def ghostscript_available() -> bool:
    """ocrmypdf needs Ghostscript for PDF/A output (plain PDF output works without it)."""
    return any(shutil.which(name) for name in ("gs", "gswin64c", "gswin32c"))


def effective_output_type(settings: Settings) -> tuple[str, str | None]:
    """The output type to request and, if it had to change, a German note why.

    PDF/A needs Ghostscript. Without it ocrmypdf stops with exit code 3, so
    fall back to a plain PDF with a text layer instead of failing every scan.
    """
    wanted = (settings.ocr_output_type or "pdfa-2").strip()
    if wanted.lower().startswith("pdfa") and not ghostscript_available():
        return "pdf", ("Ghostscript fehlt – statt PDF/A wird ein normales PDF mit Textebene erzeugt")
    return wanted, None


_cache_lock = threading.Lock()
_version_cache: dict[tuple, str | None] = {}
_languages_cache: dict[tuple, list[str]] = {}


def _cache_key(command: list[str]) -> tuple:
    try:
        st = os.stat(command[-1])
        return (tuple(command), st.st_mtime_ns, st.st_size)
    except OSError:
        return (tuple(command),)


def _run_quick(command: list[str], *, timeout: float = 60) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=_child_env(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("cannot run %s: %s", command[0], exc)
        return None


def ocr_version() -> str | None:
    """Version reported by ``ocrmypdf --version``, or None if it is not available."""
    command = _ocrmypdf_command()
    if command is None:
        return None
    key = _cache_key(command)
    with _cache_lock:
        if key in _version_cache:
            return _version_cache[key]
    result = _run_quick([*command, "--version"])
    version = None
    if result is not None and result.returncode == 0:
        text = _decode(result.stdout).strip() or _decode(result.stderr).strip()
        for line in text.splitlines():
            line = line.strip()
            if line:
                version = line
                break
    if version is not None:  # failures are retried on the next call
        with _cache_lock:
            _version_cache[key] = version
    return version


def tesseract_languages() -> list[str]:
    """Language packs installed for Tesseract (``tesseract --list-langs``)."""
    command = _tesseract_command()
    if command is None:
        return []
    key = _cache_key(command)
    with _cache_lock:
        if key in _languages_cache:
            return list(_languages_cache[key])
    result = _run_quick([*command, "--list-langs"])
    if result is None or result.returncode != 0:
        return []
    languages: list[str] = []
    text = _decode(result.stdout) + "\n" + _decode(result.stderr)
    for line in text.splitlines():
        line = line.strip()
        if not line or " " in line or line.endswith(":"):
            continue  # header ("List of available languages in ...:") and warnings
        if line not in languages:
            languages.append(line)
    with _cache_lock:
        _languages_cache[key] = languages
    return list(languages)


# ---------------------------------------------------------------------------
# Process management

_active_lock = threading.Lock()
_active: set[subprocess.Popen] = set()


def _signal_group(process: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        # The group is gone and the id was reused, or only zombies are left.
        with suppress(ProcessLookupError, PermissionError):
            if process.poll() is None:
                process.send_signal(sig)


def _stop_group(process: subprocess.Popen) -> None:
    """Terminate the whole process group, forcefully if it does not exit in time."""
    _signal_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=_TERMINATE_GRACE)
    except subprocess.TimeoutExpired:
        pass
    # Children may outlive the leader; make sure nothing of the group survives.
    _signal_group(process, signal.SIGKILL)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=_TERMINATE_GRACE)


@atexit.register
def _terminate_active() -> None:
    """Stop OCR runs that are still going when the interpreter exits."""
    with _active_lock:
        processes = list(_active)
    for process in processes:
        if process.poll() is None:
            _stop_group(process)


# ---------------------------------------------------------------------------
# Work directories

# Work directories of the runs going on in this process (real paths); guarded by _active_lock.
_active_workdirs: set[str] = set()


def workdir_root(settings: Settings) -> Path:
    """Where OCR work directories are created: ``<data>/index/tmp``."""
    return Layout.from_settings(settings).index / "tmp"


def _workdir_key(path: Path | str) -> str:
    path = Path(path)
    return os.path.join(os.path.realpath(path.parent), path.name)


def _create_workdir(settings: Settings) -> Path:
    root = workdir_root(settings)
    try:
        root.mkdir(parents=True, exist_ok=True)
        path = Path(tempfile.mkdtemp(prefix=f"{WORKDIR_PREFIX}{os.getpid()}-", dir=root))
    except OSError as exc:
        raise ProcessingError(
            f"Arbeitsverzeichnis für die Texterkennung kann nicht angelegt werden: {exc.strerror or exc}"
        ) from exc
    with _active_lock:
        _active_workdirs.add(_workdir_key(path))
    return path


def _release_workdir(path: Path) -> None:
    key = _workdir_key(path)
    shutil.rmtree(path, ignore_errors=True)
    with _active_lock:
        _active_workdirs.discard(key)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, but belongs to another user
    except OSError:
        return False
    return True


def _owner_pid(name: str) -> int | None:
    head, separator, _rest = name[len(WORKDIR_PREFIX) :].partition("-")
    return int(head) if separator and head.isdigit() else None


def _workdir_in_use(key: str, name: str, prefix: str) -> bool:
    with _active_lock:
        if key in _active_workdirs:
            return True
    if prefix != WORKDIR_PREFIX:
        return False
    pid = _owner_pid(name)
    # A directory named after this process but not in _active_workdirs is left
    # over from an earlier process that had the same id (e.g. before a restart).
    return pid is not None and pid != os.getpid() and _pid_alive(pid)


def remove_stale_workdirs(settings: Settings, *, now: float | None = None) -> int:
    """Remove OCR work directories that processes which died during OCR left behind.

    A directory is removed only if it has not changed for ``ocr_timeout`` plus
    :data:`STALE_MARGIN_SECONDS` (a running job refreshes it every
    :data:`HEARTBEAT_SECONDS`), and never while a job of this process uses it
    or the process that created it is still alive. Leftovers of earlier
    versions in the system's temp directory follow the same age rule.
    Returns the number of directories removed.
    """
    now = time.time() if now is None else now
    max_age = max(0, int(settings.ocr_timeout or 0)) + STALE_MARGIN_SECONDS
    removed = 0
    for root, prefix in ((workdir_root(settings), WORKDIR_PREFIX), (Path(tempfile.gettempdir()), _LEGACY_PREFIX)):
        try:
            with os.scandir(root) as iterator:
                entries = [entry for entry in iterator if entry.name.startswith(prefix)]
        except OSError:
            continue
        real_root = os.path.realpath(root)
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if now - st.st_mtime < max_age:
                continue
            if prefix == _LEGACY_PREFIX and hasattr(os, "getuid") and st.st_uid != os.getuid():
                continue
            if _workdir_in_use(os.path.join(real_root, entry.name), entry.name, prefix):
                continue
            shutil.rmtree(entry.path, ignore_errors=True)
            if os.path.lexists(entry.path):
                _logger.warning("cannot remove abandoned OCR work directory %s", entry.path)
                continue
            removed += 1
            _logger.info("removed abandoned OCR work directory %s", entry.path)
    return removed


def _child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("FORCE_COLOR", None)
    env.update({"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "400", "PYTHONUNBUFFERED": "1"})
    if extra:
        env.update(extra)
    return env


def _decode(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", errors="replace")


class _OutputReader(threading.Thread):
    """Reads the combined output of the OCR process: keeps a tail, forwards lines."""

    def __init__(self, stream, callback: LogCallback | None):
        super().__init__(name="ocr-output", daemon=True)
        self._stream = stream
        self._callback = callback
        self.tail: deque[str] = deque(maxlen=_TAIL_LINES)
        self.forwarded = 0
        self.lines = 0

    def run(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        try:
            while True:
                chunk = self._stream.read1(65536) if hasattr(self._stream, "read1") else self._stream.read(65536)
                if not chunk:
                    break
                pending += decoder.decode(chunk)
                *complete, pending = re.split(r"\r\n|\r|\n", pending)
                for line in complete:
                    self._handle(line)
            pending += decoder.decode(b"", final=True)
            if pending:
                self._handle(pending)
        except (OSError, ValueError):
            pass  # stream closed while the process is being stopped
        finally:
            with suppress(OSError):
                self._stream.close()

    def _handle(self, raw: str) -> None:
        line = _ANSI_RE.sub("", raw).strip()
        if not line:
            return
        self.lines += 1
        if self.tail and self.tail[-1] == line:
            return  # repeated progress output
        self.tail.append(line)
        if self._callback is None:
            return
        if self.forwarded < _MAX_LOG_LINES:
            self._emit(f"OCR: {line}")
        elif self.forwarded == _MAX_LOG_LINES:
            self._emit("OCR: weitere Ausgaben werden nicht protokolliert")
        self.forwarded += 1

    def _emit(self, message: str) -> None:
        try:
            self._callback(message)  # type: ignore[misc]
        except Exception:
            log.warning("OCR log callback failed", exc_info=True)

    def error_tail(self) -> str:
        lines = [line for line in self.tail if line]
        text = "\n".join(lines[-_ERROR_TAIL_LINES:])
        if len(text) > _ERROR_TAIL_CHARS:
            text = "…" + text[-_ERROR_TAIL_CHARS:]
        return text

    def output_text(self) -> str:
        return "\n".join(self.tail)


# ---------------------------------------------------------------------------
# Inspecting the input


@dataclass
class _PdfFacts:
    signed: bool = False
    pdfa: bool = False
    attachments: bool = False
    pages: int = 0


def _inspect(src: Path) -> _PdfFacts | None:
    """Properties that decide whether (and how long) OCRmyPDF may work on the file."""
    from .normalize import is_signed

    try:
        with pikepdf.open(src) as pdf:
            facts = _PdfFacts()
            facts.signed = is_signed(pdf)
            with suppress(Exception):
                facts.pages = len(pdf.pages)
            with suppress(Exception):
                facts.attachments = len(pdf.attachments) > 0 or "/AF" in pdf.Root
            with suppress(Exception):
                facts.pdfa = bool(pdf.open_metadata().pdfa_status)
            return facts
    except pikepdf.PasswordError:
        raise ProcessingError("PDF ist verschlüsselt") from None
    except Exception:
        log.debug("cannot inspect %s before OCR; leaving it to ocrmypdf", src, exc_info=True)
        return None


def _all_pages_have_text(src: Path) -> bool:
    from .text import extract_pages

    try:
        pages = extract_pages(src)
    except Exception:
        return False
    return bool(pages) and all(page.strip() for page in pages)


# ---------------------------------------------------------------------------
# Building the command


def _languages(value: str) -> str:
    parts = [part for part in re.split(r"[+,;\s]+", value or "") if part]
    return "+".join(parts) or "deu+eng"


def _extra_args(values: tuple[str, ...] | list[str]) -> list[str]:
    args: list[str] = []
    for value in values or ():
        try:
            args.extend(shlex.split(value))
        except ValueError:
            args.append(value)
    return args


def build_command(base: list[str], src: Path, dst: Path, settings: Settings) -> list[str]:
    """The full ocrmypdf argument list (no shell involved)."""
    extra = _extra_args(settings.ocr_extra_args)
    mode_given = any(arg in _MODE_FLAGS or arg.startswith("--mode=") for arg in extra)
    redo = "--redo-ocr" in extra or any(
        arg in ("--mode=redo", "-mredo") or (arg in ("-m", "--mode") and nxt == "redo")
        for arg, nxt in zip(extra, extra[1:] + [""])
    )
    command = list(base)
    if not mode_given:
        command.append("--skip-text")
    command.append("--rotate-pages")
    if not redo:
        command.append("--deskew")  # not combinable with --redo-ocr
    command += [
        "--output-type",
        effective_output_type(settings)[0],
        "-l",
        _languages(settings.ocr_languages),
        "--jobs",
        str(max(1, int(settings.ocr_jobs or 1))),
        "--optimize",
        "1",
        "--tesseract-timeout",
        str(TESSERACT_TIMEOUT),
    ]
    command += extra
    # Absolute paths never look like options.
    command += [os.fspath(Path(src).absolute()), os.fspath(Path(dst).absolute())]
    return command


# ---------------------------------------------------------------------------
# Running


def _copy(src: Path, dst: Path) -> None:
    """Make ``dst`` a copy of ``src`` (atomic replace; nothing to do if they are the same file)."""
    with suppress(OSError):
        if os.path.samefile(src, dst):
            return
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = fsops.temp_name_for(dst)
    fsops.copy_file_fsync(src, tmp)
    try:
        os.replace(tmp, dst)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    fsops.fsync_dir(dst.parent)


def _skip(src: Path, dst: Path, reason: str, started: float, *, returncode: int = 0, warnings=None) -> OcrResult:
    _copy(src, dst)
    return OcrResult(
        performed=False,
        output=dst,
        skipped_reason=reason,
        warnings=list(warnings or []),
        returncode=returncode,
        duration_s=round(time.monotonic() - started, 3),
    )


def run_ocr(src: Path, dst: Path, settings: Settings, *, log: LogCallback | None = None) -> OcrResult:
    """OCR ``src`` into ``dst`` with ocrmypdf; pages that already have text are kept.

    Without OCR (disabled, tools missing, digitally signed PDFs, files that
    are already PDF/A or carry attachments and have text on every page)
    ``dst`` becomes a copy of ``src`` and ``performed`` is False. Raises
    ProcessingError on failure: a killed OCR process is ``transient`` so the
    job can be retried; exceeding the time limit (see :func:`time_limit`)
    raises :class:`OcrTimeout`, which is not.
    """
    started = time.monotonic()
    src, dst = Path(src), Path(dst)
    if not src.is_file():
        raise ProcessingError(f"Eingabedatei für die OCR fehlt: {src.name}")
    if not settings.ocr_enabled:
        return _skip(src, dst, REASON_DISABLED, started)
    base = _ocrmypdf_command()
    if base is None:
        return _skip(src, dst, REASON_MISSING_OCRMYPDF, started)
    if _tesseract_command() is None:
        return _skip(src, dst, REASON_MISSING_TESSERACT, started)

    facts = _inspect(src)
    pages = facts.pages if facts is not None else 0
    if facts is not None:
        if facts.signed:
            return _skip(src, dst, REASON_SIGNED, started)
        if (facts.pdfa or facts.attachments) and _all_pages_have_text(src):
            return _skip(src, dst, REASON_KEEP_AS_IS, started)

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = fsops.temp_name_for(dst)
    command = build_command(base, src, tmp_out, settings)
    timeout = time_limit(settings, pages)
    output_type, output_note = effective_output_type(settings)
    if output_note:
        _emit(log, output_note)
    slots = OCR_SLOTS
    if not slots.acquire(blocking=False):
        _emit(log, "Wartet auf einen freien Platz für die Texterkennung …")
        slots.acquire()
        started = time.monotonic()  # the duration counts from the start of the run itself
    try:
        _emit(log, f"OCR startet (Sprachen {_languages(settings.ocr_languages)}, "
                   f"Ausgabe {output_type}, Zeitlimit {timeout} s)")
        return _run_with_slot(src, dst, tmp_out, command, settings, timeout, pages, output_note, started, log)
    finally:
        slots.release()


def _run_with_slot(
    src: Path,
    dst: Path,
    tmp_out: Path,
    command: list[str],
    settings: Settings,
    timeout: int,
    pages: int,
    output_note: str | None,
    started: float,
    log: LogCallback | None,
) -> OcrResult:
    """The ocrmypdf run itself; the caller holds one of the :data:`OCR_SLOTS`."""
    workdir = _create_workdir(settings)
    try:
        try:
            returncode, reader = _execute(command, workdir, timeout, log)
        except _TimedOut as exc:
            raise OcrTimeout(_timeout_message(timeout, pages, exc.tail), timeout=timeout, pages=pages) from None
        duration = round(time.monotonic() - started, 3)
        output = reader.output_text()

        if returncode in (0, 10):
            if not _usable(tmp_out):
                raise ProcessingError(f"OCR hat keine Ausgabedatei erzeugt (Exit-Code {returncode})")
            warnings = [output_note] if output_note else []
            if returncode == 10:
                warnings.append("PDF/A-Umwandlung fehlgeschlagen – das Ergebnis ist ein normales PDF mit Textebene")
            fsops.fsync_file(tmp_out)
            os.replace(tmp_out, dst)
            fsops.fsync_dir(dst.parent)
            return OcrResult(True, dst, None, warnings, returncode, duration)
        if returncode == 6:
            return _skip(src, dst, REASON_HAS_TEXT, started, returncode=returncode)
        if returncode == 8:
            raise ProcessingError("PDF ist verschlüsselt")
        if returncode == 2 and "digital signature" in output.lower():
            return _skip(src, dst, REASON_SIGNED, started, returncode=returncode)
        if returncode == 2 and "non-embedded" in output.lower():
            return _skip(src, dst, REASON_FONTS, started, returncode=returncode)

        tail = reader.error_tail()
        _logger.warning("ocrmypdf failed with exit code %s for %s:\n%s", returncode, src, output[-4000:])
        if returncode < 0 or returncode == 130:
            name = _signal_name(-returncode) if returncode < 0 else "SIGINT"
            message = f"OCR wurde abgebrochen ({name})"
            raise ProcessingError(f"{message}: {tail}" if tail else message, transient=True)
        description = _EXIT_DESCRIPTIONS.get(returncode, "Fehler")
        message = f"OCR fehlgeschlagen ({description}, Exit-Code {returncode})"
        raise ProcessingError(f"{message}: {tail}" if tail else message)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(tmp_out)
        _release_workdir(workdir)


def _emit(callback: LogCallback | None, message: str) -> None:
    if callback is None:
        return
    try:
        callback(message)
    except Exception:
        _logger.warning("OCR log callback failed", exc_info=True)


def _usable(path: Path) -> bool:
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"Signal {number}"


class _TimedOut(Exception):
    """The OCR process exceeded its time limit and was stopped."""

    def __init__(self, tail: str):
        super().__init__(tail)
        self.tail = tail


def _wait(process: subprocess.Popen, timeout: float, workdir: Path) -> int:
    """Wait for ``process`` at most ``timeout`` seconds, showing signs of life in ``workdir`` meanwhile."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        try:
            return process.wait(timeout=min(remaining, HEARTBEAT_SECONDS))
        except subprocess.TimeoutExpired:
            with suppress(OSError):
                os.utime(workdir)


def _execute(command: list[str], workdir: Path, timeout: int, callback: LogCallback | None):
    """Run ocrmypdf in its own process group; returns (returncode, reader).

    Raises _TimedOut (with the last output lines) after ``timeout`` seconds.
    """
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=workdir,
            env=_child_env({"TMPDIR": str(workdir), "TMP": str(workdir), "TEMP": str(workdir)}),
            start_new_session=True,
        )
    except OSError as exc:
        transient = exc.errno in (errno.EAGAIN, errno.ENOMEM, errno.EMFILE, errno.ENFILE)
        raise ProcessingError(f"OCR kann nicht gestartet werden: {exc.strerror or exc}", transient=transient) from exc
    with _active_lock:
        _active.add(process)
    reader = _OutputReader(process.stdout, callback)
    reader.start()
    try:
        try:
            returncode = _wait(process, timeout, workdir)
        except subprocess.TimeoutExpired:
            _logger.warning("ocrmypdf exceeded %s s; stopping its process group", timeout)
            _stop_group(process)
            reader.join(_READER_JOIN)
            raise _TimedOut(reader.error_tail()) from None
        except BaseException:
            _stop_group(process)
            raise
        reader.join(_READER_JOIN)
        if reader.is_alive():
            # Something the tool started still holds the output pipe open.
            _signal_group(process, signal.SIGKILL)
            reader.join(_TERMINATE_GRACE)
        return returncode, reader
    finally:
        with _active_lock:
            _active.discard(process)
