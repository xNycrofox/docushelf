"""Regression tests for the inbox and PDF processing fixes.

* Taking files over into the inbox never leaves a truncated original under a
  real name, and an interrupted copy is discarded instead of being adopted.
* OCR work directories live on the data volume and leftovers are cleaned up.
* The OCR time limit grows with the page count; a timeout fails the item at
  once with a hint instead of being retried with the same limit.
* Protected PDFs (digitally signed, or encrypted with a user password) pass
  through the pipeline byte for byte and still reach "bereit".
"""

from __future__ import annotations

import errno
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pikepdf
import pytest

from conftest import make_scan_pdf, make_text_pdf
from docushelf.errors import DocuShelfError, ProcessingError, UnsupportedFile
from docushelf.inbox import items as items_module
from docushelf.inbox import pipeline as pipeline_module
from docushelf.inbox.items import CREATING_MARKER, InboxStore
from docushelf.inbox.pipeline import Pipeline
from docushelf.inbox.worker import Worker
from docushelf.pdf import ocr, render
from docushelf.pdf.normalize import normalize_to_pdf, pdf_protection
from docushelf.pdf.ocr import run_ocr
from docushelf.util import fsops
from docushelf.util.names import OWN_TEMP_PREFIX

REPO = Path(__file__).resolve().parent.parent

LETTER = "Telekom Deutschland GmbH\nRechnung 4711\nRechnungsdatum 02.11.2026\nBetrag 39,95 EUR"


def wait_until(predicate, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    return predicate()


def dead_pid() -> int:
    """The id of a process that has ended (and was reaped)."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(10)
    return process.pid


def set_age(path: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


# ---------------------------------------------------------------------------
# Sample PDFs


def make_signed_pdf(path: Path, *, variant: str = "sigflags", owner_password: str | None = None) -> Path:
    """A PDF with the structure of a detached PKCS#7 signature.

    ``variant``: "sigflags" (AcroForm /SigFlags 3 and a signed field),
    "field" (a filled /Sig field only, its type inherited from the parent
    field), "perms" (catalog /Perms only), "unsigned" (an empty signature field).
    """
    source = make_text_pdf(path.with_name(path.stem + "-plain.pdf"), [LETTER, "Seite zwei"])
    with pikepdf.open(source) as pdf:
        value = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Sig,
                Filter=pikepdf.Name("/Adobe.PPKLite"),
                SubFilter=pikepdf.Name("/adbe.pkcs7.detached"),
                ByteRange=pikepdf.Array([0, 1000, 3000, 500]),
                Contents=pikepdf.String(b"\x00" * 256),
                M=pikepdf.String("D:20261102120000+01'00'"),
            )
        )
        if variant == "perms":
            pdf.Root.Perms = pikepdf.Dictionary(DocMDP=value)
        else:
            widget = pdf.make_indirect(
                pikepdf.Dictionary(
                    Type=pikepdf.Name.Annot,
                    Subtype=pikepdf.Name.Widget,
                    Rect=pikepdf.Array([0, 0, 0, 0]),
                    F=132,
                    P=pdf.pages[0].obj,
                )
            )
            if variant == "field":
                parent = pdf.make_indirect(
                    pikepdf.Dictionary(FT=pikepdf.Name.Sig, T=pikepdf.String("Signatur"), Kids=pikepdf.Array([widget]))
                )
                widget.Parent = parent
                widget.V = value
                fields = pikepdf.Array([parent])
            else:
                widget.FT = pikepdf.Name.Sig
                widget.T = pikepdf.String("Signatur")
                if variant != "unsigned":
                    widget.V = value
                fields = pikepdf.Array([widget])
            pdf.pages[0].Annots = pdf.make_indirect(pikepdf.Array([widget]))
            acroform = pikepdf.Dictionary(Fields=fields)
            if variant == "sigflags":
                acroform.SigFlags = 3
            pdf.Root.AcroForm = pdf.make_indirect(acroform)
        encryption = None
        if owner_password:
            encryption = pikepdf.Encryption(owner=owner_password, user="", allow=pikepdf.Permissions(modify_other=False))
        pdf.save(path, encryption=encryption)
    return path


def make_user_password_pdf(path: Path, pages: list[str] | None = None) -> Path:
    plain = make_text_pdf(path.with_name(path.stem + "-plain.pdf"), pages or [LETTER])
    with pikepdf.open(plain) as pdf:
        pdf.save(path, encryption=pikepdf.Encryption(owner="eigentuemer", user="geheim"))
    return path


# ---------------------------------------------------------------------------
# import-2: taking files over into the inbox


def test_copy_gets_its_final_name_only_when_complete(base_services, tmp_path, monkeypatch):
    store = base_services.inbox
    source = tmp_path / "export" / "0000123.pdf"
    source.parent.mkdir()
    source.write_bytes(b"%PDF-1.7\n" + os.urandom(20_000))
    seen = []
    real_copy = fsops.copy_file_fsync

    def observing_copy(src, dst):
        dst = Path(dst)
        item_dir = dst.parent.parent
        seen.append((dst.name, sorted(p.name for p in dst.parent.iterdir()), (item_dir / CREATING_MARKER).is_file()))
        return real_copy(src, dst)

    monkeypatch.setattr(fsops, "copy_file_fsync", observing_copy)
    item = store.create("paperless", [source], move=False)

    ((name, present, marked),) = seen
    assert name.startswith(OWN_TEMP_PREFIX) and name.endswith(".part")
    assert present == []  # the real name appears only once the data is complete
    assert marked  # an interrupted copy can be told from moved-in originals
    assert not (item.dir / CREATING_MARKER).exists()
    assert sorted(p.name for p in (item.dir / "original").iterdir()) == ["0000123.pdf"]
    assert item.original_paths()[0].read_bytes() == source.read_bytes()
    assert source.exists()


def test_move_across_file_systems_gets_its_final_name_only_when_complete(base_services, monkeypatch):
    store = base_services.inbox
    drop = base_services.layout.source_dir("smb")
    drop.mkdir(parents=True, exist_ok=True)
    source = drop / "Scan 7.pdf"
    data = b"%PDF-1.4\n" + os.urandom(20_000)
    source.write_bytes(data)
    seen = []
    real_copy = fsops.copy_file_fsync

    def no_hard_links(*args, **kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    def observing_copy(src, dst):
        dst = Path(dst)
        seen.append((dst.name, sorted(p.name for p in dst.parent.iterdir()), Path(src).exists()))
        return real_copy(src, dst)

    monkeypatch.setattr(os, "link", no_hard_links)
    monkeypatch.setattr(fsops, "copy_file_fsync", observing_copy)
    item = store.create("smb", [source])

    ((name, present, source_there),) = seen
    assert name.startswith(OWN_TEMP_PREFIX) and present == [] and source_there
    assert not source.exists()  # removed only after the copy was complete
    assert item.original_paths()[0].read_bytes() == data
    assert not (item.dir / CREATING_MARKER).exists()  # moved originals need no marker


_COPY_SCRIPT = """
import sys
from pathlib import Path
from docushelf.inbox.items import InboxStore
from docushelf.layout import Layout

layout = Layout(data=Path(sys.argv[1]), archive=Path(sys.argv[2]))
InboxStore(layout).create("paperless", [Path(sys.argv[3])], move=False, origin_hash="0" * 64)
"""


def _open_fifo_writer(fifo: Path, process: subprocess.Popen) -> int:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno != errno.ENXIO:
                raise
        else:
            os.set_blocking(fd, True)
            return fd
        if process.poll() is not None:
            raise AssertionError(f"the copying process ended early (exit code {process.returncode})")
        time.sleep(0.02)
    raise AssertionError("the copying process never opened its source")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _copied_bytes(work: Path) -> int:
    total = 0
    for root, _dirs, names in os.walk(work):
        if os.path.basename(root) == "original":
            for name in names:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    return total


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_import_killed_while_copying_is_discarded_instead_of_adopted(layout, tmp_path):
    # The source is a named pipe, so the copy waits for data: the process is
    # killed for real (SIGKILL) while a part of the file has been copied.
    fifo = tmp_path / "export" / "0000042.pdf"
    fifo.parent.mkdir()
    os.mkfifo(fifo)
    env = dict(os.environ, PYTHONPATH=str(REPO) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    process = subprocess.Popen(
        [sys.executable, "-c", _COPY_SCRIPT, str(layout.data), str(layout.archive), str(fifo)], env=env
    )
    writer = None
    chunk = 1 << 20  # the copy reads (and writes) the source in pieces of this size
    try:
        writer = _open_fifo_writer(fifo, process)
        _write_all(writer, b"%PDF-1.7\n" + b"x" * chunk)  # one piece and a bit; the rest never comes
        assert wait_until(lambda: _copied_bytes(layout.work) >= chunk, 30), "the copy did not start"
    finally:
        process.kill()
        process.wait(30)
        if writer is not None:
            os.close(writer)

    store = InboxStore(layout)
    assert store.recover_orphans() == []
    assert store.list("arbeit") == []
    assert list(layout.work.iterdir()) == []  # nothing truncated is left to be filed later


def _marker(directory: Path, *, pid: int, host: str | None = None, age: float = 0.0) -> None:
    path = directory / CREATING_MARKER
    path.write_text(json.dumps({"pid": pid, "host": host or socket.gethostname(), "started_at": "x"}), encoding="utf-8")
    if age:
        set_age(path, age)


def _orphan(layout, name: str, files: dict[str, bytes]) -> Path:
    directory = layout.work / name
    (directory / "original").mkdir(parents=True)
    for rel, data in files.items():
        (directory / rel).write_bytes(data)
    return directory


def test_recover_orphans_tells_interrupted_copies_from_moved_originals(layout, samples, tmp_path):
    store = InboxStore(layout)
    temp = f"{OWN_TEMP_PREFIX}0123abcd.part"
    killed_copy = _orphan(layout, "20261101-100000-aaaaaa", {"original/a.pdf": b"%PDF complete copy"})
    _marker(killed_copy, pid=dead_pid())
    only_temp = _orphan(layout, "20261101-100000-bbbbbb", {f"original/{temp}": b"%PDF trunc"})
    moved = _orphan(layout, "20261101-100000-cccccc", {"original/scan.pdf": b"%PDF moved", f"original/{temp}": b"%P"})
    copying = _orphan(layout, "20261101-100000-dddddd", {f"original/{temp}": b"%PDF still"})
    _marker(copying, pid=os.getppid())  # a live process on this machine
    foreign_young = _orphan(layout, "20261101-100000-eeeeee", {f"original/{temp}": b"%PDF"})
    _marker(foreign_young, pid=1, host="other-container")
    foreign_old = _orphan(layout, "20261101-100000-ffffff", {"original/b.pdf": b"%PDF"})
    _marker(foreign_old, pid=1, host="other-container", age=2 * items_module.FOREIGN_MARKER_GRACE_SECONDS)
    other_content = _orphan(layout, "20261101-100000-gggggg", {"log.txt": b"Protokoll"})
    complete = store.create("upload", [samples.text_pdf(tmp_path / "ok.pdf", ["ok"])], move=False)
    _marker(complete.dir, pid=dead_pid())  # killed right after item.json was written

    recovered = store.recover_orphans()

    assert [item.id for item in recovered] == [moved.name]
    adopted = store.load(moved.name)
    assert adopted.original_files == ["original/scan.pdf"] and adopted.source == "unbekannt"
    assert sorted(p.name for p in (moved / "original").iterdir()) == ["scan.pdf"]
    assert not killed_copy.exists() and not only_temp.exists() and not foreign_old.exists()
    assert (copying / CREATING_MARKER).exists() and (foreign_young / CREATING_MARKER).exists()
    assert (other_content / "log.txt").exists()
    assert not (complete.dir / CREATING_MARKER).exists()
    assert store.load(complete.id).original_paths()[0].is_file()
    assert store.recover_orphans() == []  # idempotent


@pytest.mark.parametrize("move", [False, True])
def test_recover_orphans_leaves_a_creation_in_progress_alone(base_services, tmp_path, monkeypatch, move):
    # Restoring an inbox item from the trash runs recover_orphans() while the
    # intake or an import may be in the middle of create().
    store = base_services.inbox
    drop = base_services.layout.source_dir("smb")
    drop.mkdir(parents=True, exist_ok=True)
    source = drop / "Rechnung.pdf"
    source.write_bytes(b"%PDF-1.4\n" + os.urandom(5000))
    data = source.read_bytes()
    real_copy = fsops.copy_file_fsync
    results = []

    def copy_with_recovery_in_between(src, dst):
        real_copy(src, dst)
        results.append(store.recover_orphans())

    def no_hard_links(*args, **kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(fsops, "copy_file_fsync", copy_with_recovery_in_between)
    monkeypatch.setattr(os, "link", no_hard_links)
    item = store.create("smb", [source], move=move)

    assert results == [[]]
    assert store.load(item.id).original_paths()[0].read_bytes() == data
    assert source.exists() is not move


# ---------------------------------------------------------------------------
# ocr-1 and ocr-2: OCR work directories and time limits

FAKE_OCRMYPDF = r"""
import json, os, shutil, sys, time

args = sys.argv[1:]
if args == ["--version"]:
    print("99.1.0")
    sys.exit(0)
record = os.environ.get("FAKE_OCR_RECORD")
if record:
    with open(record, "w", encoding="utf-8") as handle:
        json.dump({"argv": args, "cwd": os.getcwd(), "tmpdir": os.environ.get("TMPDIR")}, handle)
mode = os.environ.get("FAKE_OCR_MODE", "ok")
if mode == "fail_if_run":
    sys.exit(15)
if mode == "hang":
    print("still working", file=sys.stderr, flush=True)
    time.sleep(120)
shutil.copyfile(args[-2], args[-1])
"""


@pytest.fixture
def fake_ocr(tmp_path: Path, monkeypatch):
    script = tmp_path / "fake_ocrmypdf.py"
    script.write_text(FAKE_OCRMYPDF, encoding="utf-8")
    record = tmp_path / "ocr-record.json"
    monkeypatch.setattr(ocr, "_ocrmypdf_command", lambda: [sys.executable, str(script)])
    monkeypatch.setattr(ocr, "_tesseract_command", lambda: ["tesseract"])
    monkeypatch.setattr(ocr, "ghostscript_available", lambda: True)
    monkeypatch.setenv("FAKE_OCR_RECORD", str(record))
    monkeypatch.setenv("FAKE_OCR_MODE", "ok")
    return SimpleNamespace(
        record=record,
        mode=lambda value: monkeypatch.setenv("FAKE_OCR_MODE", value),
        info=lambda: json.loads(record.read_text(encoding="utf-8")),
    )


@pytest.fixture
def ocr_settings(settings):
    return settings.with_overrides(ocr_enabled=True, ocr_timeout=60)


def test_ocr_works_in_a_directory_on_the_data_volume(ocr_settings, layout, tmp_path, fake_ocr):
    scan = make_scan_pdf(tmp_path / "scan.pdf", [["Rechnung 4711"]])

    result = run_ocr(scan, tmp_path / "out.pdf", ocr_settings)

    assert result.performed
    tmpdir = Path(fake_ocr.info()["tmpdir"])
    assert os.path.realpath(tmpdir.parent) == os.path.realpath(layout.index / "tmp")
    assert tmpdir.name.startswith(f"ocr-{os.getpid()}-")
    assert not tmpdir.exists()
    assert ocr._active_workdirs == set()


def test_abandoned_ocr_work_directories_are_removed(settings, layout, tmp_path, monkeypatch):
    root = layout.index / "tmp"
    root.mkdir(parents=True, exist_ok=True)
    stale_age = settings.ocr_timeout + ocr.STALE_MARGIN_SECONDS + 60

    def workdir(name: str, age: float) -> Path:
        path = root / name
        (path / "ocrmypdf.io.x").mkdir(parents=True)
        (path / "ocrmypdf.io.x" / "000001.png").write_bytes(b"x" * 100)
        set_age(path, age)
        return path

    dead = workdir(f"ocr-{dead_pid()}-a1b2c3", stale_age)
    young = workdir(f"ocr-{dead_pid()}-d4e5f6", 60)
    alive = workdir(f"ocr-{os.getppid()}-g7h8i9", stale_age)
    mine_running = workdir(f"ocr-{os.getpid()}-j1k2l3", stale_age)
    mine_left_over = workdir(f"ocr-{os.getpid()}-m4n5o6", stale_age)
    unrelated = workdir("something-else", stale_age)
    monkeypatch.setattr(ocr, "_active_workdirs", {ocr._workdir_key(mine_running)})
    # Leftovers of earlier versions in the system's temp directory.
    system_tmp = tmp_path / "systmp"
    system_tmp.mkdir()
    monkeypatch.setattr(ocr.tempfile, "gettempdir", lambda: str(system_tmp))
    legacy_old = system_tmp / "docushelf-ocr-abcd1234"
    legacy_old.mkdir()
    set_age(legacy_old, stale_age)
    legacy_young = system_tmp / "docushelf-ocr-efgh5678"
    legacy_young.mkdir()

    assert ocr.remove_stale_workdirs(settings) == 3

    assert not dead.exists() and not mine_left_over.exists() and not legacy_old.exists()
    assert young.exists() and alive.exists() and mine_running.exists() and unrelated.exists()
    assert legacy_young.exists()
    assert ocr.remove_stale_workdirs(settings) == 0


def test_running_ocr_refreshes_its_work_directory(ocr_settings, layout, tmp_path, fake_ocr, monkeypatch):
    fake_ocr.mode("hang")
    monkeypatch.setattr(ocr, "HEARTBEAT_SECONDS", 0.1)
    monkeypatch.setattr(ocr, "SECONDS_PER_PAGE", 0)
    scan = make_scan_pdf(tmp_path / "scan.pdf", [["Rechnung 4711"]])
    outcome: dict = {}

    def target():
        try:
            run_ocr(scan, tmp_path / "out.pdf", ocr_settings.with_overrides(ocr_timeout=3))
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=target)
    thread.start()
    try:
        workdir = wait_until(lambda: next((layout.index / "tmp").glob("ocr-*"), None))
        assert workdir is not None
        set_age(workdir, 7200)
        assert wait_until(lambda: time.time() - workdir.stat().st_mtime < 60, 5), "no sign of life"
        set_age(workdir, 7200)
        assert ocr.remove_stale_workdirs(ocr_settings) == 0  # in use by this process
        assert workdir.exists()
    finally:
        thread.join(30)
    assert isinstance(outcome.get("error"), ocr.OcrTimeout)
    assert not workdir.exists()


def test_services_recover_removes_abandoned_ocr_work_directories(services):
    root = services.layout.index / "tmp"
    leftover = root / f"ocr-{dead_pid()}-zz9999"
    (leftover / "ocrmypdf.io.y").mkdir(parents=True)
    (leftover / "ocrmypdf.io.y" / "page.png").write_bytes(b"x" * 1000)
    set_age(leftover, services.settings.ocr_timeout + ocr.STALE_MARGIN_SECONDS + 60)

    services.recover()

    assert not leftover.exists()


def test_maintenance_removes_abandoned_ocr_work_directories(base_services):
    leftover = base_services.layout.index / "tmp" / f"ocr-{dead_pid()}-yy8888"
    leftover.mkdir(parents=True)
    set_age(leftover, base_services.settings.ocr_timeout + ocr.STALE_MARGIN_SECONDS + 60)

    results = Worker(base_services, resume_interval=None).run_maintenance(force=True)

    assert results["ocr_workdirs_removed"] == 1
    assert not leftover.exists()


def test_ocr_time_limit_grows_with_the_page_count(ocr_settings, tmp_path, fake_ocr):
    assert ocr.time_limit(ocr_settings, 1) == 60
    assert ocr.time_limit(ocr_settings, 30) == 30 * ocr.SECONDS_PER_PAGE
    scan = make_scan_pdf(tmp_path / "scan.pdf", [[f"Seite {n}"] for n in range(1, 13)])
    lines: list[str] = []

    assert run_ocr(scan, tmp_path / "out.pdf", ocr_settings, log=lines.append).performed

    assert f"Zeitlimit {12 * ocr.SECONDS_PER_PAGE} s" in lines[0]


def test_ocr_timeout_fails_the_item_at_once_with_a_hint(base_services, settings, tmp_path, fake_ocr, monkeypatch):
    fake_ocr.mode("hang")
    monkeypatch.setattr(ocr, "SECONDS_PER_PAGE", 0)
    base_services.settings = settings.with_overrides(ocr_enabled=True, ocr_timeout=1)
    pipe = Pipeline(base_services)
    scan = make_scan_pdf(tmp_path / "scan.pdf", [["Rechnung 4711", "Betrag 12,00 EUR"]])
    item = base_services.inbox.create("upload", [scan], move=False)

    result = pipe.process(item.id)  # no transient error that would be retried

    assert result.state == "fehler"
    message = result.error["message"]
    assert "Zeitlimit von 1 s" in message
    assert "DOCUSHELF_OCR_TIMEOUT" in message and "ohne OCR" in message
    assert "transient" not in result.error
    assert base_services.index.pending_jobs("process") == []


def test_retry_without_ocr_after_a_timeout_reaches_ready(base_services, settings, tmp_path, fake_ocr, monkeypatch):
    fake_ocr.mode("hang")
    monkeypatch.setattr(ocr, "SECONDS_PER_PAGE", 0)
    base_services.settings = settings.with_overrides(ocr_enabled=True, ocr_timeout=1)
    base_services.worker = SimpleNamespace(kick=lambda: None)
    pipe = Pipeline(base_services)
    item = base_services.inbox.create("upload", [make_scan_pdf(tmp_path / "scan.pdf", [["Rechnung"]])], move=False)
    assert pipe.process(item.id).state == "fehler"

    pipe.retry(item.id, skip_ocr=True)

    assert pipe.process(item.id).state == "bereit"


# ---------------------------------------------------------------------------
# Protected PDFs


@pytest.mark.parametrize(
    ("variant", "expected"),
    [("sigflags", "signed"), ("field", "signed"), ("perms", "signed"), ("unsigned", None)],
)
def test_pdf_protection_recognises_signatures(tmp_path, variant, expected):
    assert pdf_protection(make_signed_pdf(tmp_path / f"{variant}.pdf", variant=variant)) == expected


def test_pdf_protection_of_encrypted_and_ordinary_files(tmp_path):
    assert pdf_protection(make_user_password_pdf(tmp_path / "geheim.pdf")) == "encrypted"
    plain = make_text_pdf(tmp_path / "plain.pdf", ["Brief"])
    assert pdf_protection(plain) is None
    owner_only = tmp_path / "owner.pdf"
    with pikepdf.open(plain) as pdf:
        pdf.save(owner_only, encryption=pikepdf.Encryption(owner="x", user=""))
    assert pdf_protection(owner_only) is None  # readable, may be rewritten without encryption
    broken = tmp_path / "kaputt.pdf"
    broken.write_bytes(b"%PDF-1.4 kaputt")
    assert pdf_protection(broken) is None
    assert pdf_protection(tmp_path / "missing.pdf") is None


@pytest.mark.parametrize("variant", ["sigflags", "field", "perms"])
def test_signed_pdf_with_owner_password_is_not_rewritten(tmp_path, variant):
    signed = make_signed_pdf(tmp_path / "signed.pdf", variant=variant, owner_password="eigentuemer")
    out = tmp_path / "work" / "normalized.pdf"

    assert normalize_to_pdf([signed], out) == 2

    assert out.read_bytes() == signed.read_bytes()


def test_encrypted_pdf_cannot_be_rendered_but_is_reported_cleanly(tmp_path):
    locked = make_user_password_pdf(tmp_path / "geheim.pdf")
    with pytest.raises(UnsupportedFile, match="passwortgeschützt"):
        render.render_page_png(locked, 1)
    cache = render.ThumbnailCache(tmp_path / "cache")
    with pytest.raises(DocuShelfError):
        cache.get(locked, 1, 200, key="geheim")
    assert not cache.path_for("geheim", 1, 200).exists()


@pytest.mark.parametrize("variant", ["field", "perms"])
def test_ocr_leaves_every_kind_of_signed_pdf_alone(ocr_settings, tmp_path, fake_ocr, variant):
    fake_ocr.mode("fail_if_run")
    signed = make_signed_pdf(tmp_path / "signed.pdf", variant=variant)
    dst = tmp_path / "out.pdf"

    result = run_ocr(signed, dst, ocr_settings)

    assert not result.performed and result.skipped_reason == ocr.REASON_SIGNED
    assert dst.read_bytes() == signed.read_bytes()
    assert not fake_ocr.record.exists()


@pytest.fixture
def stage_calls(monkeypatch):
    """Records which of the real PDF-rewriting stages the pipeline calls."""
    calls: list[str] = []
    for name in ("_detect_blank_pages", "_remove_pages", "_run_ocr"):
        real = getattr(pipeline_module, name)

        def spy(*args, _real=real, _name=name, **kwargs):
            calls.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(pipeline_module, name, spy)
    return calls


@pytest.mark.parametrize("owner_password", [None, "eigentuemer"])
def test_signed_pdf_passes_through_the_pipeline_byte_for_byte(services, tmp_path, stage_calls, owner_password):
    signed = make_signed_pdf(tmp_path / "Rechnung.pdf", owner_password=owner_password)
    original = signed.read_bytes()
    item = services.inbox.create("upload", [signed], move=False)

    result = services.pipeline.process(item.id)

    assert result.state == "bereit", services.inbox.read_log(result)
    assert result.context["pdf_protection"] == "signed"
    assert result.document_path.read_bytes() == original
    assert result.original_paths()[0].read_bytes() == original
    assert result.page_count == 2 and result.pages_removed == []
    assert "Telekom" in services.inbox.read_text(result)[0]
    assert result.suggestions
    assert stage_calls == []
    log_text = services.inbox.read_log(result)
    assert "Digital signiertes PDF unverändert übernommen" in log_text
    assert "Leerseitenerkennung übersprungen (PDF ist digital signiert)" in log_text
    assert "Texterkennung übersprungen (PDF ist digital signiert)" in log_text

    refreshed = services.pipeline.refresh_suggestions(item.id)
    assert refreshed.document_path.read_bytes() == original


def test_user_password_pdf_passes_through_the_pipeline_byte_for_byte(services, tmp_path, stage_calls):
    locked = make_user_password_pdf(tmp_path / "Kontoauszug.pdf")
    original = locked.read_bytes()
    item = services.inbox.create("mail", [locked], move=False, context={"mail_subject": "Kontoauszug"})

    result = services.pipeline.process(item.id)

    assert result.state == "bereit", services.inbox.read_log(result)
    assert result.context["pdf_protection"] == "encrypted"
    assert result.document_path.read_bytes() == original
    assert result.page_count == 0  # unknown without the password
    assert services.inbox.read_text(result) == []
    assert result.suggestions  # fallback suggestions still give a title and a folder
    assert result.error is None
    assert stage_calls == []
    log_text = services.inbox.read_log(result)
    assert "Passwortgeschütztes PDF unverändert übernommen" in log_text
    assert "Text kann nicht gelesen werden (PDF ist passwortgeschützt)" in log_text
    with pytest.raises(UnsupportedFile):
        render.render_page_png(result.document_path, 1)

    # Recomputing suggestions works without text as well.
    services.pipeline.refresh_suggestions(item.id)
    assert services.inbox.load(item.id).state == "bereit"
    # And after a retry the same happens again.
    services.pipeline.retry(item.id)
    again = services.pipeline.process(item.id)
    assert again.state == "bereit" and again.document_path.read_bytes() == original


def test_ordinary_pdf_after_a_protected_one_gets_no_protection_flag(services, tmp_path):
    item = services.inbox.create("upload", [make_text_pdf(tmp_path / "brief.pdf", [LETTER])], move=False)
    item.context["pdf_protection"] = "signed"  # e.g. left from an earlier, different original
    services.inbox.save(item)

    result = services.pipeline.process(item.id)

    assert result.state == "bereit"
    assert "pdf_protection" not in result.context


def test_encrypted_pdf_in_a_combined_upload_fails_with_a_clear_message(services, tmp_path):
    first = make_text_pdf(tmp_path / "anschreiben.pdf", ["Anschreiben"])
    locked = make_user_password_pdf(tmp_path / "anlage.pdf")
    item = services.inbox.create("upload", [first, locked], move=False)

    result = services.pipeline.process(item.id)

    assert result.state == "fehler"
    assert "anlage.pdf" in result.error["message"] and "einzeln" in result.error["message"]


def test_protected_pdf_text_failure_is_no_error_but_other_failures_are(base_services, tmp_path, monkeypatch):
    # The pipeline's own guard, independent of the PDF libraries.
    base_services.settings = base_services.settings.with_overrides(ocr_enabled=False)
    pipe = Pipeline(base_services)

    def unreadable(path, **kwargs):
        raise ProcessingError("kaputt")

    signed = make_signed_pdf(tmp_path / "signed.pdf")
    plain = make_text_pdf(tmp_path / "plain.pdf", [LETTER])
    protected_item = base_services.inbox.create("upload", [signed], move=False)
    plain_item = base_services.inbox.create("upload", [plain], move=False)
    monkeypatch.setattr(pipeline_module, "extract_pages", unreadable)

    assert pipe.process(protected_item.id).state == "bereit"
    assert pipe.process(plain_item.id).state == "fehler"
