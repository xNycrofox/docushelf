"""Tests for the intake: stability detection, batches, subdirectories, watch folders."""

from __future__ import annotations

import os
import time

import pytest

from docushelf.channels.sendung import MANIFEST, write_sendung
from docushelf.config import SourceConfig
from docushelf.inbox.intake import Intake
from docushelf.util import fsops


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeWorker:
    def __init__(self) -> None:
        self.kicks = 0

    def kick(self) -> None:
        self.kicks += 1


def make_intake(services, sources=None, **kwargs) -> tuple[Intake, FakeClock]:
    if sources is not None:
        services.settings = services.settings.with_overrides(sources=tuple(sources))
    services.worker = FakeWorker()
    clock = FakeClock()
    return Intake(services, clock=clock, **kwargs), clock


def pending_keys(services, kind: str = "process") -> list[str]:
    return [job.key for job in services.index.pending_jobs(kind)]


# -- plain files ---------------------------------------------------------------------


def test_plain_file_is_taken_immediately_without_quiet_time(base_services, samples):
    intake, _ = make_intake(base_services)
    drop = base_services.layout.source_dir("upload")
    samples.text_pdf(drop / "Rechnung.pdf", ["Hallo"])

    created = intake.scan_once()

    assert len(created) == 1
    item = base_services.inbox.load(created[0])
    assert item.state == "arbeit" and item.source == "upload"
    assert [p.name for p in item.original_paths()] == ["Rechnung.pdf"]
    assert item.origin_hash == fsops.sha256_file(item.original_paths()[0])
    assert not (drop / "Rechnung.pdf").exists()
    assert pending_keys(base_services) == created
    assert base_services.worker.kicks >= 1
    assert "Eingang über „upload“: Rechnung.pdf" in base_services.inbox.read_log(item)
    assert intake.scan_once() == []


def test_file_needs_two_observations_when_quiet_time_is_set(base_services):
    intake, clock = make_intake(base_services, [SourceConfig("smb", stable_seconds=5)])
    drop = base_services.layout.source_dir("smb")
    (drop / "scan.pdf").write_bytes(b"%PDF-1.4 complete")

    assert intake.scan_once() == []  # first sight
    clock.advance(3)
    assert intake.scan_once() == []  # unchanged, but not long enough
    clock.advance(2.5)
    created = intake.scan_once()

    assert len(created) == 1
    assert not (drop / "scan.pdf").exists()


def test_growing_file_waits_until_it_is_stable(base_services):
    intake, clock = make_intake(base_services, [SourceConfig("smb", stable_seconds=5)])
    path = base_services.layout.source_dir("smb") / "scan.pdf"
    path.write_bytes(b"%PDF-1.4 part one")
    assert intake.scan_once() == []

    clock.advance(10)
    with open(path, "ab") as handle:
        handle.write(b" part two")
    assert intake.scan_once() == []  # changed since the last observation
    clock.advance(4)
    assert intake.scan_once() == []
    clock.advance(2)
    created = intake.scan_once()

    assert len(created) == 1
    original = base_services.inbox.load(created[0]).original_paths()[0]
    assert original.read_bytes() == b"%PDF-1.4 part one part two"


def test_zero_byte_files_wait_longer(base_services):
    intake, clock = make_intake(base_services, empty_file_seconds=30)
    drop = base_services.layout.source_dir("upload")
    (drop / "leer.pdf").write_bytes(b"")

    assert intake.scan_once() == []
    clock.advance(20)
    assert intake.scan_once() == []
    clock.advance(11)
    assert len(intake.scan_once()) == 1


def test_ignored_names_symlinks_and_special_files_are_left_alone(base_services, tmp_path):
    intake, clock = make_intake(base_services)
    drop = base_services.layout.source_dir("upload")
    names = [".DS_Store", "._scan.pdf", "~$brief.docx", "scan.pdf.part", ".hidden.pdf", "Thumbs.db", "x.tmp", MANIFEST]
    for name in names:
        (drop / name).write_bytes(b"x")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF outside")
    (drop / "link.pdf").symlink_to(outside)
    os.mkfifo(drop / "pipe.pdf")

    assert intake.scan_once() == []
    clock.advance(1000)
    assert intake.scan_once() == []

    for name in names + ["link.pdf", "pipe.pdf"]:
        assert os.path.lexists(drop / name), name
    assert outside.read_bytes() == b"%PDF outside"


def test_failed_take_over_is_retried_after_a_pause(base_services, monkeypatch):
    intake, clock = make_intake(base_services, failure_backoff_seconds=300)
    drop = base_services.layout.source_dir("upload")
    (drop / "a.pdf").write_bytes(b"%PDF a")
    calls = []

    def disk_full(*args, **kwargs):
        calls.append(1)
        raise OSError(28, "No space left on device")

    real_create = base_services.inbox.create
    monkeypatch.setattr(base_services.inbox, "create", disk_full)
    assert intake.scan_once() == []
    assert intake.scan_once() == []
    assert len(calls) == 1  # not hammered on every poll
    assert (drop / "a.pdf").exists()

    monkeypatch.setattr(base_services.inbox, "create", real_create)
    clock.advance(301)
    assert len(intake.scan_once()) == 1


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores permissions")
def test_file_that_cannot_be_removed_is_left_in_place(base_services):
    intake, clock = make_intake(base_services, subdir_stable_seconds=0)
    locked = base_services.layout.source_dir("smb") / "gesperrt"
    locked.mkdir()
    (locked / "scan.pdf").write_bytes(b"%PDF locked")
    os.chmod(locked, 0o555)
    try:
        assert intake.scan_once() == []
        assert (locked / "scan.pdf").exists()
        assert base_services.inbox.list("arbeit") == []
    finally:
        os.chmod(locked, 0o755)


# -- batches -------------------------------------------------------------------------------


def test_batch_with_manifest_creates_one_item_per_document(base_services, samples, tmp_path):
    intake, _ = make_intake(base_services)
    drop = base_services.layout.source_dir("mail")
    pdf = samples.text_pdf(tmp_path / "src" / "r.pdf", ["Rechnung"])
    first_image = samples.image(tmp_path / "src" / "a.jpg", ["Seite 1"])
    second_image = samples.image(tmp_path / "src" / "b.jpg", ["Seite 2"])
    batch = write_sendung(
        drop,
        [[("rechnung.pdf", pdf)], [("foto.jpg", first_image), ("foto.jpg", second_image)]],
        context={"mail_subject": "Ihre Rechnung", "mail_from": "rechnung@example.com"},
        options={"skip_blank_removal": True},
        titles=["Stromrechnung", None],
    )

    created = intake.scan_once()

    assert len(created) == 2
    first, second = (base_services.inbox.load(item_id) for item_id in created)
    assert first.context == {
        "mail_subject": "Ihre Rechnung",
        "mail_from": "rechnung@example.com",
        "title": "Stromrechnung",
    }
    assert second.context == {"mail_subject": "Ihre Rechnung", "mail_from": "rechnung@example.com"}
    assert first.options == second.options == {"skip_blank_removal": True}
    assert [p.name for p in first.original_paths()] == ["rechnung.pdf"]
    assert [p.name for p in second.original_paths()] == ["foto.jpg", "foto (2).jpg"]
    assert second.original_paths()[0].read_bytes() == first_image.read_bytes()
    assert not batch.exists()
    assert drop.is_dir()
    assert sorted(pending_keys(base_services)) == sorted(created)


def test_batch_directory_without_manifest_is_not_taken_apart(base_services):
    intake, clock = make_intake(base_services)
    batch = base_services.layout.source_dir("mail") / "20261123-125100-a1b2c3"
    batch.mkdir()
    (batch / "rechnung.pdf").write_bytes(b"%PDF-1.4 x")

    assert intake.scan_once() == []
    clock.advance(120)
    assert intake.scan_once() == []  # still waiting for the manifest

    fsops.atomic_write_json(
        batch / MANIFEST,
        {"version": 1, "documents": [{"files": ["rechnung.pdf"]}], "context": {"mail_subject": "S"}},
    )
    created = intake.scan_once()

    assert len(created) == 1
    assert base_services.inbox.load(created[0]).context == {"mail_subject": "S"}
    assert not batch.exists()


def test_abandoned_batch_directory_is_taken_after_the_grace_period(base_services):
    intake, clock = make_intake(base_services, batch_grace_seconds=600)
    batch = base_services.layout.source_dir("mail") / "20261123-125100-a1b2c3"
    batch.mkdir()
    (batch / "rechnung.pdf").write_bytes(b"%PDF-1.4 x")

    assert intake.scan_once() == []
    clock.advance(601)
    created = intake.scan_once()

    assert len(created) == 1
    assert not batch.exists()


def test_interrupted_batch_intake_resumes_with_the_remaining_documents(base_services):
    intake, _ = make_intake(base_services)
    drop = base_services.layout.source_dir("upload")
    batch = write_sendung(drop, [[("a.pdf", b"%PDF a")], [("b.pdf", b"%PDF b")]])
    (batch / "a.pdf").unlink()  # taken over just before a crash

    created = intake.scan_once()

    assert len(created) == 1
    assert base_services.inbox.load(created[0]).original_paths()[0].read_bytes() == b"%PDF b"
    assert not batch.exists()


def test_files_not_listed_in_the_manifest_are_kept(base_services):
    intake, clock = make_intake(base_services, subdir_stable_seconds=0, batch_grace_seconds=50)
    drop = base_services.layout.source_dir("upload")
    batch = write_sendung(drop, [[("a.pdf", b"%PDF a")]])
    (batch / "extra.pdf").write_bytes(b"%PDF extra")

    assert len(intake.scan_once()) == 1
    assert (batch / "extra.pdf").read_bytes() == b"%PDF extra"
    assert not (batch / MANIFEST).exists()

    # Without its manifest the directory is an ordinary one; named like a batch,
    # it waits for the grace period before its files are taken one by one.
    assert intake.scan_once() == []
    clock.advance(51)
    created = intake.scan_once()
    assert len(created) == 1
    assert base_services.inbox.load(created[0]).original_paths()[0].read_bytes() == b"%PDF extra"
    assert not batch.exists()


def test_unreadable_manifest_falls_back_to_single_files(base_services):
    intake, clock = make_intake(base_services, broken_manifest_seconds=60)
    batch = base_services.layout.source_dir("mail") / "kaputt"
    batch.mkdir()
    (batch / "a.pdf").write_bytes(b"%PDF a")
    (batch / "b.pdf").write_bytes(b"%PDF b")
    (batch / MANIFEST).write_text("{ not json", encoding="utf-8")

    assert intake.scan_once() == []
    clock.advance(61)
    created = intake.scan_once()

    assert len(created) == 2
    assert not batch.exists()


# -- subdirectories ------------------------------------------------------------------------------


def test_plain_subdirectories_are_walked_and_kept(base_services):
    intake, clock = make_intake(base_services)
    drop = base_services.layout.source_dir("smb")
    (drop / "Steuer" / "2025").mkdir(parents=True)
    (drop / "Steuer" / "2025" / "beleg.pdf").write_bytes(b"%PDF 1")
    (drop / "Steuer" / "notiz.pdf").write_bytes(b"%PDF 2")
    (drop / "Steuer" / ".DS_Store").write_bytes(b"junk")
    (drop / "leer").mkdir()

    assert intake.scan_once() == []  # files in subdirectories need a short quiet time
    clock.advance(6)
    created = intake.scan_once()

    assert len(created) == 2
    paths = {base_services.inbox.load(item_id).context.get("source_path") for item_id in created}
    assert paths == {"Steuer/2025/beleg.pdf", "Steuer/notiz.pdf"}
    # Emptied, but kept: a scanner may be set up to write into exactly this folder.
    assert (drop / "Steuer" / "2025").is_dir()
    assert not (drop / "Steuer" / "2025" / "beleg.pdf").exists()
    assert not (drop / "Steuer" / "notiz.pdf").exists()
    assert (drop / "leer").is_dir()
    assert drop.is_dir()


def test_subdirectory_with_a_file_in_progress_is_kept(base_services):
    intake, _ = make_intake(base_services, subdir_stable_seconds=0)
    job = base_services.layout.source_dir("ftp") / "auftrag"
    job.mkdir()
    (job / "seite1.pdf").write_bytes(b"%PDF 1")
    (job / "seite2.pdf.part").write_bytes(b"%PDF 2 (incomplete")

    assert len(intake.scan_once()) == 1
    assert (job / "seite2.pdf.part").exists()
    assert job.is_dir()


# -- external watch folders -----------------------------------------------------------------------


def test_watch_folder_without_removal_is_imported_once(base_services, tmp_path):
    nas = tmp_path / "nas"
    (nas / "sub").mkdir(parents=True)
    (nas / "a.pdf").write_bytes(b"%PDF a")
    (nas / "sub" / "b.pdf").write_bytes(b"%PDF b")
    source = SourceConfig("watch-nas", path=nas, stable_seconds=0, remove_after_import=False)
    intake, _ = make_intake(base_services, [source], subdir_stable_seconds=0)

    created = intake.scan_once()

    assert len(created) == 2
    assert (nas / "a.pdf").read_bytes() == b"%PDF a"
    assert (nas / "sub" / "b.pdf").read_bytes() == b"%PDF b"
    digest = fsops.sha256_file(nas / "a.pdf")
    assert base_services.index.hash_seen("watch-nas", digest)
    assert intake.scan_once() == []

    # A restarted intake remembers the files through the index ...
    assert Intake(base_services, subdir_stable_seconds=0).scan_once() == []
    # ... and, when the index was thrown away, through the inbox items.
    base_services.index.reset()
    assert Intake(base_services, subdir_stable_seconds=0).scan_once() == []
    assert base_services.index.hash_seen("watch-nas", digest)

    # The same content under another name is not imported again ...
    (nas / "a-kopie.pdf").write_bytes(b"%PDF a")
    assert intake.scan_once() == []
    # ... a changed file is.
    (nas / "a.pdf").write_bytes(b"%PDF a, version 2")
    created = intake.scan_once()
    assert len(created) == 1
    assert base_services.inbox.load(created[0]).original_paths()[0].read_bytes() == b"%PDF a, version 2"
    assert sorted(p.name for p in nas.iterdir()) == ["a-kopie.pdf", "a.pdf", "sub"]


def test_batch_in_an_unmodified_watch_folder_is_imported_once(base_services, tmp_path):
    nas = tmp_path / "nas"
    nas.mkdir()
    batch = write_sendung(nas, [[("a.pdf", b"%PDF a")], [("b.pdf", b"%PDF b")]], context={"k": "v"})
    source = SourceConfig("watch-nas", path=nas, stable_seconds=0, remove_after_import=False)
    intake, _ = make_intake(base_services, [source])

    created = intake.scan_once()

    assert len(created) == 2
    assert {base_services.inbox.load(i).context["k"] for i in created} == {"v"}
    assert sorted(p.name for p in batch.iterdir()) == [MANIFEST, "a.pdf", "b.pdf"]
    assert intake.scan_once() == []
    assert Intake(base_services).scan_once() == []  # remembered by hash after a restart


def test_watch_folder_with_removal_is_emptied_but_kept(base_services, tmp_path):
    scans = tmp_path / "scans"
    (scans / "2026" / "11").mkdir(parents=True)
    (scans / "2026" / "11" / "scan.pdf").write_bytes(b"%PDF s")
    (scans / "brief.pdf").write_bytes(b"%PDF b")
    source = SourceConfig("watch-scans", path=scans, stable_seconds=0, remove_after_import=True)
    intake, _ = make_intake(base_services, [source], subdir_stable_seconds=0)

    created = intake.scan_once()

    assert len(created) == 2
    assert {base_services.inbox.load(i).source for i in created} == {"watch-scans"}
    # The files are gone, the folder structure stays for the scanner.
    assert scans.is_dir() and [p for p in scans.rglob("*") if p.is_file()] == []
    assert (scans / "2026" / "11").is_dir()


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores permissions")
def test_watch_folder_file_that_cannot_be_removed_is_imported_once(base_services, tmp_path):
    scans = tmp_path / "scans"
    scans.mkdir()
    (scans / "scan.pdf").write_bytes(b"%PDF s")
    source = SourceConfig("watch-scans", path=scans, stable_seconds=0, remove_after_import=True)
    intake, _ = make_intake(base_services, [source])
    os.chmod(scans, 0o555)  # readable, but nothing can be deleted
    try:
        assert len(intake.scan_once()) == 1
        assert (scans / "scan.pdf").exists()
        assert intake.scan_once() == []
        assert Intake(base_services).scan_once() == []  # also after a restart
    finally:
        os.chmod(scans, 0o755)
    assert len(base_services.inbox.list("arbeit")) == 1
    assert [p.name for p in base_services.layout.work.iterdir()] == [base_services.inbox.list("arbeit")[0].id]


def test_emptied_watch_folder_takes_the_same_content_again(base_services, tmp_path):
    scans = tmp_path / "scans"
    scans.mkdir()
    source = SourceConfig("watch-scans", path=scans, stable_seconds=0, remove_after_import=True)
    intake, _ = make_intake(base_services, [source])
    (scans / "scan.pdf").write_bytes(b"%PDF s")
    first = intake.scan_once()
    (scans / "scan.pdf").write_bytes(b"%PDF s")  # dropped once more
    second = intake.scan_once()

    assert len(first) == len(second) == 1
    assert list(scans.iterdir()) == []
    origins = {base_services.inbox.load(i).origin_hash for i in first + second}
    assert len(origins) == 1  # the pipeline marks the second one as a duplicate


def test_watch_folder_overlapping_the_data_directory_is_ignored(base_services, tmp_path):
    layout = base_services.layout
    (layout.archive / "Bank").mkdir(parents=True)
    (layout.archive / "Bank" / "auszug.pdf").write_bytes(b"%PDF archived")
    sources = [
        SourceConfig("watch-archiv", path=layout.archive, stable_seconds=0),
        SourceConfig("watch-bank", path=layout.archive / "Bank", stable_seconds=0),
        SourceConfig("watch-data", path=layout.data, stable_seconds=0),
        SourceConfig("watch-bereit", path=layout.ready, stable_seconds=0),
    ]
    intake, _ = make_intake(base_services, sources, subdir_stable_seconds=0)

    assert intake.scan_once() == []
    assert (layout.archive / "Bank" / "auszug.pdf").read_bytes() == b"%PDF archived"


def test_missing_watch_folder_is_skipped(base_services, tmp_path):
    source = SourceConfig("watch-weg", path=tmp_path / "nicht-da", stable_seconds=0)
    intake, _ = make_intake(base_services, [source, SourceConfig("upload", stable_seconds=0)])
    (base_services.layout.source_dir("upload") / "a.pdf").write_bytes(b"%PDF a")

    assert len(intake.scan_once()) == 1
    assert intake.last_error is None


# -- background thread ------------------------------------------------------------------------------


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_thread_scans_on_trigger_and_stops_quickly(base_services):
    base_services.worker = FakeWorker()
    intake = Intake(base_services, poll_interval=60)
    intake.start()
    try:
        assert wait_until(lambda: intake.last_scan_at is not None)
        (base_services.layout.source_dir("upload") / "a.pdf").write_bytes(b"%PDF a")
        intake.trigger()
        assert wait_until(lambda: len(pending_keys(base_services)) == 1)
        intake.start()  # idempotent
    finally:
        started = time.monotonic()
        intake.stop()
        assert time.monotonic() - started < 2
    assert not intake.running
    intake.stop()  # idempotent
