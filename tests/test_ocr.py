"""Tests for docushelf.pdf.ocr.

Most tests replace the ocrmypdf executable with a small script that behaves
like it (exit codes, output, hanging), so the process handling is tested
without Tesseract. Tests marked ``requires_ocr`` run the real tools.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pikepdf
import pytest

from conftest import requires_ocr
from docushelf.errors import ProcessingError
from docushelf.pdf import ocr
from docushelf.pdf.ocr import OcrResult, build_command, ocr_available, ocr_version, run_ocr, tesseract_languages
from docushelf.pdf.text import extract_pages

FAKE_OCRMYPDF = r'''
import json, os, shutil, signal, subprocess, sys, time

args = sys.argv[1:]
if args == ["--version"]:
    print("99.1.0")
    sys.exit(0)
record = os.environ.get("FAKE_OCR_RECORD")
if record:
    with open(record, "w", encoding="utf-8") as handle:
        json.dump({"argv": args, "cwd": os.getcwd(), "tmpdir": os.environ.get("TMPDIR"),
                   "no_color": os.environ.get("NO_COLOR")}, handle)
mode = os.environ.get("FAKE_OCR_MODE", "ok")
src, dst = args[-2], args[-1]
err = sys.stderr
print("Starting processing with 2 workers concurrently", file=err, flush=True)
print("    1 page is facing ⇧, confidence 3.57 - no change", file=err, flush=True)
print("\x1b[33mcolored line\x1b[0m", file=err, flush=True)


def produce():
    shutil.copyfile(src, dst)
    with open(dst, "ab") as handle:
        handle.write(b"\n%fake-ocr\n")


if mode == "fail_if_run":
    print("this must not run", file=err)
    sys.exit(15)
if mode == "ok":
    produce()
    sys.exit(0)
if mode == "pdfa_failed":
    produce()
    print("PDF/A conversion failed", file=err)
    sys.exit(10)
if mode == "already":
    sys.exit(6)
if mode == "encrypted":
    print("Input PDF is encrypted.", file=err)
    sys.exit(8)
if mode == "missing_gs":
    print("The program 'gs' could not be executed or was not found on your", file=err)
    print("system PATH.", file=err)
    print("Could not find program 'gs' on the PATH", file=err)
    sys.exit(3)
if mode == "signature":
    print("DigitalSignatureError: Input PDF has a digital signature. OCR would alter the document,", file=err)
    print("invalidating the signature.", file=err)
    sys.exit(2)
if mode == "no_output":
    sys.exit(0)
if mode == "suicide":
    os.kill(os.getpid(), signal.SIGKILL)
if mode == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(os.environ["FAKE_OCR_CHILD"], "w") as handle:
        handle.write(str(child.pid))
    print("still working", file=err, flush=True)
    time.sleep(120)
if mode == "chatty":
    for number in range(1000):
        print(f"line {number}", file=err)
    produce()
    sys.exit(0)
sys.exit(99)
'''

FAKE_TESSERACT = r'''
import sys
if sys.argv[1:] == ["--list-langs"]:
    print('List of available languages in "/usr/share/tessdata/" (3):')
    print("deu")
    print("eng")
    print("osd")
    sys.exit(0)
sys.exit(1)
'''


@pytest.fixture
def fake(tmp_path: Path, monkeypatch):
    script = tmp_path / "fake_ocrmypdf.py"
    script.write_text(FAKE_OCRMYPDF, encoding="utf-8")
    tesseract = tmp_path / "fake_tesseract.py"
    tesseract.write_text(FAKE_TESSERACT, encoding="utf-8")
    record = tmp_path / "record.json"
    monkeypatch.setattr(ocr, "_ocrmypdf_command", lambda: [sys.executable, str(script)])
    monkeypatch.setattr(ocr, "_tesseract_command", lambda: [sys.executable, str(tesseract)])
    monkeypatch.setattr(ocr, "ghostscript_available", lambda: True)
    monkeypatch.setenv("FAKE_OCR_RECORD", str(record))
    monkeypatch.setenv("FAKE_OCR_MODE", "ok")

    def info() -> dict:
        return json.loads(record.read_text(encoding="utf-8"))

    return SimpleNamespace(
        script=script,
        record=record,
        mode=lambda value: monkeypatch.setenv("FAKE_OCR_MODE", value),
        info=info,
    )


@pytest.fixture
def ocr_settings(settings):
    return settings.with_overrides(ocr_enabled=True, ocr_timeout=60)


@pytest.fixture
def scan(tmp_path: Path, samples) -> Path:
    return samples.scan_pdf(tmp_path / "in" / "deblanked.pdf", [["Rechnung Nr. 4711", "Betrag 12,00 EUR"]])


def _dst(tmp_path: Path) -> Path:
    path = tmp_path / "item" / "document.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _leftovers(directory: Path) -> list[str]:
    return [p.name for p in directory.iterdir() if p.name.startswith(".docushelf-")]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        pass
    try:
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return bool(state.strip()) and not state.strip().startswith("Z")


def _wait_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return False


# ---------------------------------------------------------------------------
# without OCR


def test_disabled_copies_input(tmp_path: Path, settings, scan: Path, fake):
    fake.mode("fail_if_run")
    dst = _dst(tmp_path)
    result = run_ocr(scan, dst, settings.with_overrides(ocr_enabled=False))
    assert isinstance(result, OcrResult)
    assert result.performed is False
    assert result.output == dst
    assert result.skipped_reason == "OCR ist deaktiviert"
    assert dst.read_bytes() == scan.read_bytes()
    assert not fake.record.exists()


def test_missing_tools_copy_input(tmp_path: Path, ocr_settings, scan: Path, monkeypatch):
    dst = _dst(tmp_path)
    monkeypatch.setattr(ocr, "_ocrmypdf_command", lambda: None)
    result = run_ocr(scan, dst, ocr_settings)
    assert not result.performed and "ocrmypdf" in result.skipped_reason
    assert dst.read_bytes() == scan.read_bytes()

    monkeypatch.setattr(ocr, "_ocrmypdf_command", lambda: ["/bin/false"])
    monkeypatch.setattr(ocr, "_tesseract_command", lambda: None)
    result = run_ocr(scan, dst, ocr_settings)
    assert not result.performed and "tesseract" in result.skipped_reason
    assert ocr_available() is False


def test_missing_input(tmp_path: Path, ocr_settings, fake):
    with pytest.raises(ProcessingError, match="fehlt"):
        run_ocr(tmp_path / "nothing.pdf", _dst(tmp_path), ocr_settings)


# ---------------------------------------------------------------------------
# command line


def test_command_line(settings, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ocr, "ghostscript_available", lambda: True)
    command = build_command(["ocrmypdf"], tmp_path / "in.pdf", tmp_path / "out.pdf", settings)
    assert command == [
        "ocrmypdf",
        "--skip-text",
        "--rotate-pages",
        "--deskew",
        "--output-type",
        "pdfa-2",
        "-l",
        "deu+eng",
        "--jobs",
        "2",
        "--optimize",
        "1",
        "--tesseract-timeout",
        "180",
        str(tmp_path / "in.pdf"),
        str(tmp_path / "out.pdf"),
    ]


def test_command_line_options(settings, tmp_path: Path):
    custom = settings.with_overrides(
        ocr_languages="deu, eng ,fra",
        ocr_jobs=0,
        ocr_output_type="pdf",
        ocr_extra_args=("--clean", "--tesseract-pagesegmode 6", "--title 'Mein Titel'"),
    )
    command = build_command(["ocrmypdf"], Path("in.pdf"), Path("out.pdf"), custom)
    assert command[command.index("-l") + 1] == "deu+eng+fra"
    assert command[command.index("--jobs") + 1] == "1"
    assert command[command.index("--output-type") + 1] == "pdf"
    assert ["--clean", "--tesseract-pagesegmode", "6", "--title", "Mein Titel"] == command[-7:-2]
    assert Path(command[-2]).is_absolute() and Path(command[-1]).is_absolute()

    redo = build_command(["ocrmypdf"], Path("a.pdf"), Path("b.pdf"), settings.with_overrides(ocr_extra_args=("--redo-ocr",)))
    assert "--skip-text" not in redo and "--deskew" not in redo and "--redo-ocr" in redo
    force = build_command(["ocrmypdf"], Path("a.pdf"), Path("b.pdf"), settings.with_overrides(ocr_extra_args=("--mode force",)))
    assert "--skip-text" not in force and "--deskew" in force


# ---------------------------------------------------------------------------
# running the (fake) tool


def test_success(tmp_path: Path, ocr_settings, scan: Path, fake):
    dst = _dst(tmp_path)
    dst.write_bytes(b"left over from an earlier attempt")
    lines: list[str] = []
    result = run_ocr(scan, dst, ocr_settings, log=lines.append)

    assert result.performed is True
    assert result.returncode == 0 and result.skipped_reason is None and result.warnings == []
    assert result.duration_s > 0
    assert dst.read_bytes() == scan.read_bytes() + b"\n%fake-ocr\n"
    assert _leftovers(dst.parent) == []

    argv = fake.info()["argv"]
    assert argv[:2] == ["--skip-text", "--rotate-pages"]
    assert argv[-2] == str(scan.absolute())
    assert Path(argv[-1]).parent == dst.parent and Path(argv[-1]).name.startswith(".docushelf-")

    # Runs in its own temporary directory, which is removed afterwards.
    info = fake.info()
    assert info["cwd"] == info["tmpdir"] or os.path.realpath(info["cwd"]) == os.path.realpath(info["tmpdir"])
    assert not Path(info["tmpdir"]).exists()
    assert info["no_color"] == "1"

    assert lines[0].startswith("OCR startet")
    assert "OCR: Starting processing with 2 workers concurrently" in lines
    assert "OCR: 1 page is facing ⇧, confidence 3.57 - no change" in lines
    assert "OCR: colored line" in lines  # escape sequences removed


def test_pdfa_conversion_failure_is_accepted(tmp_path: Path, ocr_settings, scan: Path, fake):
    fake.mode("pdfa_failed")
    dst = _dst(tmp_path)
    result = run_ocr(scan, dst, ocr_settings)
    assert result.performed and result.returncode == 10
    assert result.warnings and "PDF/A" in result.warnings[0]
    assert dst.read_bytes().endswith(b"%fake-ocr\n")


def test_already_done_copies_input(tmp_path: Path, ocr_settings, scan: Path, fake):
    fake.mode("already")
    dst = _dst(tmp_path)
    result = run_ocr(scan, dst, ocr_settings)
    assert not result.performed and result.returncode == 6
    assert result.skipped_reason
    assert dst.read_bytes() == scan.read_bytes()


def test_encrypted(tmp_path: Path, ocr_settings, scan: Path, fake):
    fake.mode("encrypted")
    with pytest.raises(ProcessingError) as info:
        run_ocr(scan, _dst(tmp_path), ocr_settings)
    assert str(info.value) == "PDF ist verschlüsselt"
    assert not info.value.transient


def test_user_password_pdf_is_rejected_before_running(tmp_path: Path, ocr_settings, scan: Path, fake):
    fake.mode("fail_if_run")
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(scan) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(owner="o", user="u"))
    with pytest.raises(ProcessingError, match="PDF ist verschlüsselt"):
        run_ocr(locked, _dst(tmp_path), ocr_settings)
    assert not fake.record.exists()


def test_failure_carries_output_tail(tmp_path: Path, ocr_settings, scan: Path, fake):
    fake.mode("missing_gs")
    dst = _dst(tmp_path)
    with pytest.raises(ProcessingError) as info:
        run_ocr(scan, dst, ocr_settings)
    message = str(info.value)
    assert "Exit-Code 3" in message
    assert message.rstrip().endswith("Could not find program 'gs' on the PATH")
    assert not info.value.transient
    assert not dst.exists()
    assert _leftovers(dst.parent) == []


def test_missing_output_file(tmp_path: Path, ocr_settings, scan: Path, fake):
    fake.mode("no_output")
    with pytest.raises(ProcessingError, match="keine Ausgabedatei"):
        run_ocr(scan, _dst(tmp_path), ocr_settings)


def test_killed_process_is_transient(tmp_path: Path, ocr_settings, scan: Path, fake):
    fake.mode("suicide")
    with pytest.raises(ProcessingError) as info:
        run_ocr(scan, _dst(tmp_path), ocr_settings)
    assert info.value.transient
    assert "SIGKILL" in str(info.value)


def test_timeout_kills_the_whole_process_group(tmp_path: Path, ocr_settings, scan: Path, fake, monkeypatch):
    fake.mode("hang")
    monkeypatch.setattr(ocr, "SECONDS_PER_PAGE", 1)  # the configured 2 s win over 1 page × 1 s
    child_file = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_OCR_CHILD", str(child_file))
    dst = _dst(tmp_path)
    started = time.monotonic()
    with pytest.raises(ocr.OcrTimeout) as info:
        run_ocr(scan, dst, ocr_settings.with_overrides(ocr_timeout=2))
    elapsed = time.monotonic() - started
    # Not retried with the same limit: the message says how to get past it.
    assert not info.value.transient
    assert "Zeitlimit von 2 s" in str(info.value)
    assert "DOCUSHELF_OCR_TIMEOUT" in str(info.value) and "ohne OCR" in str(info.value)
    assert "still working" in str(info.value)
    assert elapsed < 15
    child = int(child_file.read_text())
    assert _wait_gone(child), "a process started by the OCR tool survived the timeout"
    assert not dst.exists() and _leftovers(dst.parent) == []
    assert ocr._active == set()


def test_running_ocr_is_stopped_at_exit(tmp_path: Path, ocr_settings, scan: Path, fake, monkeypatch):
    fake.mode("hang")
    child_file = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_OCR_CHILD", str(child_file))
    outcome: dict = {}

    def target():
        try:
            run_ocr(scan, _dst(tmp_path), ocr_settings)
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=target)
    thread.start()
    deadline = time.monotonic() + 15
    while not child_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert child_file.exists()
    ocr._terminate_active()
    thread.join(20)
    assert not thread.is_alive()
    error = outcome["error"]
    assert isinstance(error, ProcessingError) and error.transient
    assert _wait_gone(int(child_file.read_text()))


def test_progress_lines_are_capped(tmp_path: Path, ocr_settings, scan: Path, fake):
    fake.mode("chatty")
    lines: list[str] = []
    result = run_ocr(scan, _dst(tmp_path), ocr_settings, log=lines.append)
    assert result.performed
    assert len(lines) <= ocr._MAX_LOG_LINES + 2
    assert lines[-1] == "OCR: weitere Ausgaben werden nicht protokolliert"


def test_failing_log_callback_does_not_break_ocr(tmp_path: Path, ocr_settings, scan: Path, fake):
    def broken(_line: str) -> None:
        raise RuntimeError("log target gone")

    result = run_ocr(scan, _dst(tmp_path), ocr_settings, log=broken)
    assert result.performed


# ---------------------------------------------------------------------------
# files OCRmyPDF must not rewrite


def _sign(pdf_path: Path, out: Path) -> Path:
    with pikepdf.open(pdf_path) as pdf:
        field = pdf.make_indirect(
            pikepdf.Dictionary(
                FT=pikepdf.Name.Sig,
                T=pikepdf.String("Signatur"),
                V=pikepdf.Dictionary(Type=pikepdf.Name.Sig, Filter=pikepdf.Name("/Adobe.PPKLite")),
            )
        )
        pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array([field]), SigFlags=3)
        pdf.save(out)
    return out


def test_signed_pdf_is_left_alone(tmp_path: Path, ocr_settings, scan: Path, fake):
    fake.mode("fail_if_run")
    signed = _sign(scan, tmp_path / "signed.pdf")
    dst = _dst(tmp_path)
    result = run_ocr(signed, dst, ocr_settings)
    assert not result.performed and result.skipped_reason == ocr.REASON_SIGNED
    assert dst.read_bytes() == signed.read_bytes()
    assert not fake.record.exists()


def test_signature_reported_by_the_tool(tmp_path: Path, ocr_settings, scan: Path, fake):
    fake.mode("signature")
    dst = _dst(tmp_path)
    result = run_ocr(scan, dst, ocr_settings)
    assert not result.performed and result.returncode == 2
    assert result.skipped_reason == ocr.REASON_SIGNED
    assert dst.read_bytes() == scan.read_bytes()


def test_pdfa_with_text_is_kept(tmp_path: Path, ocr_settings, samples, fake):
    fake.mode("fail_if_run")
    src = samples.text_pdf(tmp_path / "pdfa.pdf", ["Rechnung", "Seite zwei"])
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
            meta["pdfaid:part"] = "3"
            meta["pdfaid:conformance"] = "B"
        pdf.save()
    dst = _dst(tmp_path)
    result = run_ocr(src, dst, ocr_settings)
    assert not result.performed and result.skipped_reason == ocr.REASON_KEEP_AS_IS
    assert dst.read_bytes() == src.read_bytes()


def test_attachments_with_text_are_kept(tmp_path: Path, ocr_settings, samples, fake):
    fake.mode("fail_if_run")
    src = samples.text_pdf(tmp_path / "zugferd.pdf", ["Rechnung 4711"])
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        pdf.attachments["factur-x.xml"] = pikepdf.AttachedFileSpec(pdf, b"<Invoice/>", mime_type="text/xml")
        pdf.save()
    result = run_ocr(src, _dst(tmp_path), ocr_settings)
    assert not result.performed and result.skipped_reason == ocr.REASON_KEEP_AS_IS


def test_attachments_on_scans_still_get_ocr(tmp_path: Path, ocr_settings, scan: Path, fake):
    with pikepdf.open(scan, allow_overwriting_input=True) as pdf:
        pdf.attachments["notiz.txt"] = pikepdf.AttachedFileSpec(pdf, b"hallo")
        pdf.save()
    result = run_ocr(scan, _dst(tmp_path), ocr_settings)
    assert result.performed


# ---------------------------------------------------------------------------
# status information


def test_version_and_languages(fake):
    ocr._version_cache.clear()
    ocr._languages_cache.clear()
    assert ocr_available() is True
    assert ocr_version() == "99.1.0"
    assert tesseract_languages() == ["deu", "eng", "osd"]
    fake.script.write_text("import sys; sys.exit(1)\n", encoding="utf-8")
    os.utime(fake.script, (time.time() + 5, time.time() + 5))  # invalidates the cache
    assert ocr_version() is None
    fake.script.write_text("print('100.0')\n", encoding="utf-8")
    os.utime(fake.script, (time.time() + 10, time.time() + 10))
    assert ocr_version() == "100.0"  # a failed lookup is not remembered


def test_version_without_tools(monkeypatch):
    monkeypatch.setattr(ocr, "_ocrmypdf_command", lambda: None)
    monkeypatch.setattr(ocr, "_tesseract_command", lambda: None)
    assert ocr_version() is None
    assert tesseract_languages() == []
    assert ocr_available() is False


# ---------------------------------------------------------------------------
# the real tools


@requires_ocr
def test_real_ocr_adds_text(tmp_path: Path, settings, samples):
    src = samples.scan_pdf(tmp_path / "scan.pdf", [["Stadtwerke Musterstadt GmbH", "Rechnung Nr. 4711", "Betrag 123,45 EUR"]])
    dst = _dst(tmp_path)
    lines: list[str] = []
    # Plain PDF output does not need Ghostscript.
    custom = settings.with_overrides(ocr_enabled=True, ocr_output_type="pdf", ocr_jobs=1)
    result = run_ocr(src, dst, custom, log=lines.append)
    assert result.performed and result.returncode == 0
    text = " ".join(extract_pages(dst))
    assert "Rechnung" in text and "4711" in text
    assert any(line.startswith("OCR: ") for line in lines)
    assert _leftovers(dst.parent) == []


@requires_ocr
def test_real_ocr_keeps_existing_text(tmp_path: Path, settings, samples):
    src = samples.text_pdf(tmp_path / "digital.pdf", ["Kontoauszug Nr. 12", "Seite zwei"])
    dst = _dst(tmp_path)
    custom = settings.with_overrides(ocr_enabled=True, ocr_output_type="pdf", ocr_jobs=1)
    result = run_ocr(src, dst, custom)
    assert result.returncode in (0, 6)
    assert extract_pages(dst) == ["Kontoauszug Nr. 12", "Seite zwei"]


@requires_ocr
def test_real_tool_information():
    assert ocr_available()
    version = ocr_version()
    assert version and version[0].isdigit()
    assert "eng" in tesseract_languages()


# PDF/A needs Ghostscript


def test_pdfa_falls_back_to_plain_pdf_without_ghostscript(settings, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ocr, "ghostscript_available", lambda: False)
    output_type, note = ocr.effective_output_type(settings)
    assert output_type == "pdf"
    assert note and "Ghostscript" in note
    command = build_command(["ocrmypdf"], tmp_path / "in.pdf", tmp_path / "out.pdf", settings)
    assert command[command.index("--output-type") + 1] == "pdf"


def test_plain_pdf_needs_no_ghostscript(settings, monkeypatch):
    monkeypatch.setattr(ocr, "ghostscript_available", lambda: False)
    assert ocr.effective_output_type(settings.with_overrides(ocr_output_type="pdf")) == ("pdf", None)


def test_fallback_is_reported_as_warning(tmp_path: Path, ocr_settings, scan: Path, fake, monkeypatch):
    monkeypatch.setattr(ocr, "ghostscript_available", lambda: False)
    lines: list[str] = []
    result = run_ocr(scan, tmp_path / "out.pdf", ocr_settings, log=lines.append)
    assert result.performed
    assert any("Ghostscript" in warning for warning in result.warnings)
    assert any("Ghostscript" in line for line in lines)
    assert fake.info()["argv"][fake.info()["argv"].index("--output-type") + 1] == "pdf"
