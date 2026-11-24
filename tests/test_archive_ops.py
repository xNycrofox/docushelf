"""Tests for archive operations: the filing transaction with crash recovery,
metadata edits, moves, folders and inbox deletions. Real files throughout."""

from __future__ import annotations

import errno
import os
import threading
from datetime import date
from pathlib import Path

import pikepdf
import pytest

from archive_support import (
    add_archive_file,
    add_archive_pdf,
    archive_files,
    attach_archive,
    install_fake_rules,
    make_ready_item,
)
from docushelf.archive import ops as ops_module
from docushelf.errors import Conflict, NotFound, PathError, ProcessingError, UnsupportedFile
from docushelf.inbox.items import InboxStore
from docushelf.index.db import Index
from docushelf.models import FilingRequest, MetadataUpdate
from docushelf.pdf.xmp import read_meta
from docushelf.util import fsops
from docushelf.util.ids import is_provisional


class SimulatedCrash(BaseException):
    """Stands in for the process dying: the application must not catch it."""


class Crasher:
    """Fires once, then lets everything run normally (as after a restart)."""

    def __init__(self):
        self.armed = True
        self.fired = False

    def fire(self):
        if self.armed:
            self.armed = False
            self.fired = True
            raise SimulatedCrash()


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    monkeypatch.setattr(ops_module, "RETRY_PAUSE", 0)


@pytest.fixture
def svc(base_services):
    return attach_archive(base_services)


def _request(**changes) -> FilingRequest:
    values = dict(folder="Wohnung/Strom", title="Stromrechnung Januar", doc_date=date(2026, 1, 5), tags=["Strom"],
                  sender="Stadtwerke Musterstadt")
    values.update(changes)
    return FilingRequest(**values)


def _origin_intact(svc, item_id: str) -> bool:
    item = svc.inbox.load(item_id)
    return all(p.is_file() for p in item.original_paths()) and fsops.sha256_files(item.original_paths()) == item.origin_hash


# ---------------------------------------------------------------------------
# Filing


def test_file_item_publishes_with_metadata_and_tidies_inbox(svc, samples, tmp_path):
    item = make_ready_item(svc, samples, tmp_path, source="mail",
                           context={"mail_subject": "Ihre Rechnung", "mail_from": "rechnung@example.com",
                                    "paperless": {"id": 42, "archive_serial_number": 7}})
    record = svc.ops.file_item(item.id, _request(), user="max")

    assert record.path == "Wohnung/Strom/2026-01-05 Stromrechnung Januar.pdf"
    assert archive_files(svc.layout) == [record.path]
    meta = read_meta(svc.layout.archive / record.path)
    assert meta.doc_id == record.id and not is_provisional(record.id)
    assert meta.title == "Stromrechnung Januar" and meta.doc_date == date(2026, 1, 5)
    assert meta.tags == ["Strom"] and meta.sender == "Stadtwerke Musterstadt"
    assert meta.origin_hash == item.origin_hash and meta.source == "mail"
    assert meta.received_at == item.received_at and meta.filed_at
    assert meta.extra == {"MailSubject": "Ihre Rechnung", "MailFrom": "rechnung@example.com",
                          "PaperlessId": "42", "ArchiveSerialNumber": "7"}

    done = svc.inbox.load(item.id)
    assert done.state == "erledigt" and done.filing["state"] == "done" and done.filing["path"] == record.path
    assert not done.document_path.exists() and not done.text_path.exists() and not done.work_dir.exists()
    assert done.item_json.exists() and done.log_path.exists() and _origin_intact(svc, item.id)
    assert "Abgelegt als" in svc.inbox.read_log(done)

    assert svc.index.get(record.id).path == record.path
    assert svc.index.folder_exists("Wohnung/Strom")
    entry = svc.audit.tail(10)[0]
    assert entry["action"] == "file" and entry["user"] == "max" and entry["path"] == record.path


def test_file_item_collision_suffix_never_overwrites(svc, samples, tmp_path):
    existing = svc.layout.archive / "Wohnung/Strom/2026-01-05 Stromrechnung Januar.pdf"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"%PDF-1.4 someone else's file")
    item = make_ready_item(svc, samples, tmp_path)
    record = svc.ops.file_item(item.id, _request(), user=None)
    assert record.filename == "2026-01-05 Stromrechnung Januar (2).pdf"
    assert record.title == "Stromrechnung Januar"
    assert existing.read_bytes() == b"%PDF-1.4 someone else's file"


def test_file_item_resolves_folder_spelling_and_creates_new_folders(svc, samples, tmp_path):
    (svc.layout.archive / "Wohnung" / "Strom").mkdir(parents=True)
    first = svc.ops.file_item(make_ready_item(svc, samples, tmp_path).id, _request(folder=" wohnung / STROM "), user=None)
    assert first.folder == "Wohnung/Strom"
    second = svc.ops.file_item(make_ready_item(svc, samples, tmp_path).id,
                               _request(folder="Versicherungen/Haftpflicht", doc_date=None, title="Police"), user=None)
    assert second.path == "Versicherungen/Haftpflicht/Police.pdf" and second.doc_date is None
    assert sorted(os.listdir(svc.layout.archive)) == ["Versicherungen", "Wohnung"]
    assert svc.index.folder_exists("Versicherungen/Haftpflicht")


def test_file_item_validation(svc, samples, tmp_path):
    item = make_ready_item(svc, samples, tmp_path)
    with pytest.raises(PathError):
        svc.ops.file_item(item.id, _request(title="   "), user=None)
    with pytest.raises(PathError):
        svc.ops.file_item(item.id, _request(title="???"), user=None)
    with pytest.raises(PathError):
        svc.ops.file_item(item.id, _request(folder="Wohnung/../../etc"), user=None)
    (svc.layout.archive / "Wohnung").write_bytes(b"a file, not a folder")
    with pytest.raises(Conflict):
        svc.ops.file_item(item.id, _request(), user=None)
    with pytest.raises(NotFound):
        svc.ops.file_item("20260101-000000-abcdef", _request(), user=None)
    # Nothing happened to the item: it can still be filed.
    loaded = svc.inbox.load(item.id)
    assert loaded.state == "bereit" and loaded.filing is None and not (loaded.work_dir / "filing.pdf").exists()
    record = svc.ops.file_item(item.id, _request(folder="Anderswo"), user=None)
    assert record.folder == "Anderswo"


def test_file_item_refuses_items_that_are_not_ready(svc, samples, tmp_path):
    item = make_ready_item(svc, samples, tmp_path)
    svc.ops.file_item(item.id, _request(), user=None)
    with pytest.raises(Conflict, match="bereits abgelegt"):
        svc.ops.file_item(item.id, _request(), user=None)

    working = make_ready_item(svc, samples, tmp_path)
    svc.inbox.move(working, "arbeit")
    with pytest.raises(Conflict, match="verarbeitet"):
        svc.ops.file_item(working.id, _request(), user=None)

    unfinished = make_ready_item(svc, samples, tmp_path)
    unfinished.filing = {"state": "publishing", "sha256": "0" * 64, "folder": "X", "filename": "x.pdf"}
    svc.inbox.save(unfinished)
    with pytest.raises(Conflict, match="unterbrochen"):
        svc.ops.file_item(unfinished.id, _request(), user=None)


def test_concurrent_filing_of_the_same_item_files_once(svc, samples, tmp_path):
    item = make_ready_item(svc, samples, tmp_path)
    results: list[object] = []

    def run():
        try:
            results.append(svc.ops.file_item(item.id, _request(), user=None))
        except Conflict as exc:
            results.append(exc)

    threads = [threading.Thread(target=run) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sum(1 for r in results if isinstance(r, Conflict)) == 2
    assert archive_files(svc.layout) == ["Wohnung/Strom/2026-01-05 Stromrechnung Januar.pdf"]


def test_file_item_creates_rule_when_asked(svc, samples, tmp_path, monkeypatch):
    install_fake_rules(monkeypatch)
    item = make_ready_item(svc, samples, tmp_path)
    svc.ops.file_item(item.id, _request(create_rule=True), user="erika")
    assert len(svc.rules.added) == 1 and svc.rules.added[0].folder == "Wohnung/Strom"
    done = svc.inbox.load(item.id)
    assert done.filing["rule_name"] == "Stadtwerke Musterstadt → Wohnung/Strom"
    assert any(e["action"] == "create_rule" and e["user"] == "erika" for e in svc.audit.tail(10))


def test_rule_failure_does_not_block_filing(svc, samples, tmp_path, monkeypatch):
    install_fake_rules(monkeypatch, fail=True)
    item = make_ready_item(svc, samples, tmp_path)
    record = svc.ops.file_item(item.id, _request(create_rule=True), user=None)
    done = svc.inbox.load(item.id)
    assert done.state == "erledigt" and record.path in archive_files(svc.layout)
    assert "Regel konnte nicht angelegt werden" in svc.inbox.read_log(done)


# ---------------------------------------------------------------------------
# Crash after each step of the filing transaction, then recover()


def _crash_points(monkeypatch, crasher: Crasher):
    real_save = InboxStore.save
    real_move = InboxStore.move
    real_publish = fsops.publish_no_clobber
    real_upsert = Index.upsert
    real_write_meta = ops_module.write_meta
    real_remove_tree = fsops.remove_tree

    def journal_state(item):
        return item.filing.get("state") if isinstance(item.filing, dict) else None

    def save_crashing(state: str, *, after: bool, first_only_flag: str | None = None):
        def save(self, item):
            if crasher.armed and journal_state(item) == state and (
                first_only_flag is None or not item.filing.get(first_only_flag)
            ):
                if after:
                    real_save(self, item)
                crasher.fire()
            return real_save(self, item)

        monkeypatch.setattr(InboxStore, "save", save)

    def write_meta_crash(*args, **kwargs):
        crasher.fire()
        return real_write_meta(*args, **kwargs)

    def publish_before(*args, **kwargs):
        crasher.fire()
        return real_publish(*args, **kwargs)

    def publish_linked(src, dst_dir, filename, *, move):
        if crasher.armed:
            dst_dir.mkdir(parents=True, exist_ok=True)
            os.link(src, dst_dir / filename)  # the new name exists, the source too
            crasher.fire()
        return real_publish(src, dst_dir, filename, move=move)

    def publish_after(*args, **kwargs):
        result = real_publish(*args, **kwargs)
        crasher.fire()
        return result

    def upsert(self, *args, **kwargs):
        crasher.fire()
        return real_upsert(self, *args, **kwargs)

    def remove_tree(path):
        crasher.fire()
        return real_remove_tree(path)

    def move(self, item, state):
        if state == "erledigt":
            crasher.fire()
        return real_move(self, item, state)

    return {
        "write_meta": lambda: monkeypatch.setattr(ops_module, "write_meta", write_meta_crash),
        "journal_save_before": lambda: save_crashing("publishing", after=False),
        "journal_save_after": lambda: save_crashing("publishing", after=True),
        "publish_before": lambda: monkeypatch.setattr(fsops, "publish_no_clobber", publish_before),
        "publish_linked": lambda: monkeypatch.setattr(fsops, "publish_no_clobber", publish_linked),
        "publish_after": lambda: monkeypatch.setattr(fsops, "publish_no_clobber", publish_after),
        "published_save_before": lambda: save_crashing("published", after=False, first_only_flag="audited"),
        "published_save_after": lambda: save_crashing("published", after=True, first_only_flag="audited"),
        "index": lambda: monkeypatch.setattr(Index, "upsert", upsert),
        "tidy": lambda: monkeypatch.setattr(fsops, "remove_tree", remove_tree),
        "done_save_after": lambda: save_crashing("done", after=True),
        "move": lambda: monkeypatch.setattr(InboxStore, "move", move),
    }


CRASH_OUTCOMES = {
    # crash point: where the item is after recover()
    "write_meta": "bereit",
    "journal_save_before": "bereit",
    "journal_save_after": "bereit",
    "publish_before": "bereit",
    "publish_linked": "erledigt",
    "publish_after": "erledigt",
    "published_save_before": "erledigt",
    "published_save_after": "erledigt",
    "index": "erledigt",
    "tidy": "erledigt",
    "done_save_after": "erledigt",
    "move": "erledigt",
}


@pytest.mark.parametrize("point", sorted(CRASH_OUTCOMES))
def test_crash_during_filing_then_recover(svc, samples, tmp_path, monkeypatch, point):
    item = make_ready_item(svc, samples, tmp_path)
    crasher = Crasher()
    _crash_points(monkeypatch, crasher)[point]()

    with pytest.raises(SimulatedCrash):
        svc.ops.file_item(item.id, _request(), user="max")
    assert crasher.fired
    assert _origin_intact(svc, item.id)

    # The restart: everything works again, recover() repairs.
    svc.ops.recover()
    assert svc.ops.recover() == 0  # idempotent

    files = [f for f in archive_files(svc.layout)]
    assert not [f for f in files if Path(f).name.startswith(".docushelf-")]
    recovered = svc.inbox.load(item.id)
    assert recovered.state == CRASH_OUTCOMES[point]
    assert _origin_intact(svc, item.id)

    if recovered.state == "bereit":
        assert files == [] and recovered.filing is None and recovered.document_path.is_file()
        assert not (recovered.work_dir / "filing.pdf").exists()
        record = svc.ops.file_item(item.id, _request(), user="max")
        files = archive_files(svc.layout)
        recovered = svc.inbox.load(item.id)
    else:
        record = svc.index.get(recovered.filing["doc_id"])
        assert record is not None

    assert files == ["Wohnung/Strom/2026-01-05 Stromrechnung Januar.pdf"]
    assert record.path == files[0]
    assert read_meta(svc.layout.archive / files[0]).doc_id == recovered.filing["doc_id"] == record.id
    assert recovered.state == "erledigt" and recovered.filing["state"] == "done"
    assert not recovered.document_path.exists() and not recovered.work_dir.exists()
    assert [e["action"] for e in svc.audit.tail(50)].count("file") == 1


def test_publish_error_rolls_back_in_process(svc, samples, tmp_path, monkeypatch):
    item = make_ready_item(svc, samples, tmp_path)

    def broken(*args, **kwargs):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(fsops, "publish_no_clobber", broken)
    with pytest.raises(ProcessingError, match="Ablage fehlgeschlagen"):
        svc.ops.file_item(item.id, _request(), user=None)
    loaded = svc.inbox.load(item.id)
    assert loaded.state == "bereit" and loaded.filing is None and loaded.document_path.is_file()
    assert not loaded.work_dir.exists() and archive_files(svc.layout) == []
    assert "filing_reset" in [h["event"] for h in loaded.history]
    monkeypatch.undo()
    monkeypatch.setattr(ops_module, "RETRY_PAUSE", 0)
    assert svc.ops.file_item(item.id, _request(), user=None).path in archive_files(svc.layout)


def test_error_after_publishing_completes_in_process(svc, samples, tmp_path, monkeypatch):
    item = make_ready_item(svc, samples, tmp_path)
    real_publish = fsops.publish_no_clobber

    def publish_then_fail(*args, **kwargs):
        real_publish(*args, **kwargs)
        raise RuntimeError("lost the connection")

    monkeypatch.setattr(fsops, "publish_no_clobber", publish_then_fail)
    record = svc.ops.file_item(item.id, _request(), user=None)
    assert archive_files(svc.layout) == [record.path]
    assert svc.inbox.load(item.id).state == "erledigt"


def test_index_failure_does_not_undo_filing(svc, samples, tmp_path, monkeypatch):
    item = make_ready_item(svc, samples, tmp_path)

    def broken(self, *args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(Index, "upsert", broken)
    record = svc.ops.file_item(item.id, _request(), user=None)
    assert archive_files(svc.layout) == [record.path]
    assert read_meta(svc.layout.archive / record.path).doc_id == record.id
    assert svc.inbox.load(item.id).state == "erledigt"


def test_inbox_move_failure_is_reported_and_recovered(svc, samples, tmp_path, monkeypatch):
    item = make_ready_item(svc, samples, tmp_path)
    real_move = InboxStore.move

    def broken(self, it, state):
        if state == "erledigt":
            raise OSError(errno.EIO, "Input/output error")
        return real_move(self, it, state)

    monkeypatch.setattr(InboxStore, "move", broken)
    with pytest.raises(ProcessingError, match="abgelegt"):
        svc.ops.file_item(item.id, _request(), user=None)
    assert len(archive_files(svc.layout)) == 1
    stuck = svc.inbox.load(item.id)
    assert stuck.state == "bereit" and stuck.filing["state"] == "done"
    with pytest.raises(Conflict, match="bereits abgelegt"):
        svc.ops.file_item(item.id, _request(), user=None)
    monkeypatch.setattr(InboxStore, "move", real_move)
    assert svc.ops.recover() == 1
    assert svc.inbox.load(item.id).state == "erledigt" and len(archive_files(svc.layout)) == 1


def test_recover_finds_file_by_hash_and_resets_missing_ones(svc, samples, tmp_path, monkeypatch):
    found = make_ready_item(svc, samples, tmp_path)
    lost = make_ready_item(svc, samples, tmp_path)
    for item in (found, lost):
        crasher = Crasher()
        monkeypatch.setattr(fsops, "publish_no_clobber", _publish_then(crasher, fsops.publish_no_clobber))
        with pytest.raises(SimulatedCrash):
            svc.ops.file_item(item.id, _request(), user=None)
        monkeypatch.undo()
    # Both published as "…Januar.pdf" and "…Januar (2).pdf"; one of them vanishes before the restart.
    lost_journal = svc.inbox.load(lost.id).filing
    published = sorted(archive_files(svc.layout))
    assert len(published) == 2
    for rel in published:
        if read_meta(svc.layout.archive / rel).doc_id == lost_journal["doc_id"]:
            (svc.layout.archive / rel).unlink()
    assert svc.ops.recover() == 2
    assert svc.inbox.load(found.id).state == "erledigt"
    reset = svc.inbox.load(lost.id)
    assert reset.state == "bereit" and reset.filing is None and len(archive_files(svc.layout)) == 1


def _publish_then(crasher: Crasher, real):
    def publish(*args, **kwargs):
        result = real(*args, **kwargs)
        crasher.fire()
        return result

    return publish


def test_recover_removes_stale_temporary_files(svc, samples, tmp_path):
    item = make_ready_item(svc, samples, tmp_path)
    old = 1_000_000_000
    stale_ready = item.dir / ".docushelf-aaaa.part"
    stale_ready.write_bytes(b"x")
    os.utime(stale_ready, (old, old))
    work_item = make_ready_item(svc, samples, tmp_path)
    svc.inbox.move(work_item, "arbeit")
    stale_work = work_item.dir / ".docushelf-bbbb.part"
    fresh_work = work_item.dir / ".docushelf-cccc.part"
    stale_work.write_bytes(b"x")
    fresh_work.write_bytes(b"x")
    os.utime(stale_work, (old, old))
    leftover = svc.inbox.load(item.id).work_dir
    leftover.mkdir()
    (leftover / "filing.pdf").write_bytes(b"%PDF prepared but never journaled")
    svc.ops.recover()
    assert not stale_ready.exists() and not stale_work.exists() and fresh_work.exists()
    assert not leftover.exists()


# ---------------------------------------------------------------------------
# Editing documents


@pytest.fixture
def filed(svc, samples, tmp_path):
    item = make_ready_item(svc, samples, tmp_path)
    return svc.ops.file_item(item.id, _request(), user=None)


def test_update_title_and_date_renames_and_writes_xmp(svc, filed):
    updated = svc.ops.update_document(filed.id, MetadataUpdate(title="Strom Abschlag", doc_date=date(2026, 2, 1)),
                                      user="max")
    assert updated.id == filed.id
    assert updated.path == "Wohnung/Strom/2026-02-01 Strom Abschlag.pdf"
    assert archive_files(svc.layout) == [updated.path]
    meta = read_meta(svc.layout.archive / updated.path)
    assert meta.title == "Strom Abschlag" and meta.doc_date == date(2026, 2, 1) and meta.doc_id == filed.id
    assert meta.tags == ["Strom"] and meta.sender == "Stadtwerke Musterstadt"
    assert svc.index.get(filed.id).path == updated.path and svc.index.get(filed.id).title == "Strom Abschlag"
    assert svc.index.get_pages(filed.id)  # text kept
    entry = svc.audit.tail(1)[0]
    assert entry["action"] == "update" and entry["previous_path"] == filed.path


def test_update_tags_sender_and_clear(svc, filed):
    updated = svc.ops.update_document(filed.id, MetadataUpdate(tags=["Strom", "steuerrelevant 2026"], sender=""), user=None)
    assert updated.path == filed.path
    meta = read_meta(svc.layout.archive / filed.path)
    assert meta.tags == ["Strom", "steuerrelevant 2026"] and meta.sender is None
    assert updated.tags == ["Strom", "steuerrelevant 2026"] and updated.sender is None
    cleared = svc.ops.update_document(filed.id, MetadataUpdate(clear_date=True), user=None)
    assert cleared.filename == "Stromrechnung Januar.pdf" and cleared.doc_date is None


def test_update_without_changes_touches_nothing(svc, filed):
    path = svc.layout.archive / filed.path
    before = fsops.file_signature(path)
    same = svc.ops.update_document(filed.id, MetadataUpdate(title=filed.title, doc_date=filed.doc_date,
                                                            tags=list(filed.tags), folder=filed.folder), user=None)
    assert same.path == filed.path and fsops.file_signature(path) == before


def test_update_rename_collision_and_self_collision(svc, samples, filed):
    other = add_archive_pdf(svc, samples, "Wohnung/Strom/2026-02-01 Abschlag.pdf", text="anderes")
    moved = svc.ops.update_document(filed.id, MetadataUpdate(title="Abschlag", doc_date=date(2026, 2, 1)), user=None)
    assert moved.filename == "2026-02-01 Abschlag (2).pdf"
    assert (svc.layout.archive / other.path).read_bytes()  # untouched
    # Changing only the sender must not "free" the suffix by renaming onto itself.
    again = svc.ops.update_document(moved.id, MetadataUpdate(sender="Neu"), user=None)
    assert again.filename == "2026-02-01 Abschlag (2).pdf"
    # Same title again after a round trip: still (2), since the base name is taken.
    there = svc.ops.update_document(moved.id, MetadataUpdate(title="Zwischen"), user=None)
    back = svc.ops.update_document(there.id, MetadataUpdate(title="Abschlag"), user=None)
    assert back.filename == "2026-02-01 Abschlag (2).pdf"
    assert sorted(os.listdir(svc.layout.archive / "Wohnung/Strom")) == [
        "2026-02-01 Abschlag (2).pdf", "2026-02-01 Abschlag.pdf"]


def test_update_case_only_rename(svc, samples):
    record = add_archive_pdf(svc, samples, "Akten/2026-01-05 rechnung.pdf")
    updated = svc.ops.update_document(record.id, MetadataUpdate(title="Rechnung"), user=None)
    assert updated.filename == "2026-01-05 Rechnung.pdf"
    assert os.listdir(svc.layout.archive / "Akten") == ["2026-01-05 Rechnung.pdf"]
    assert svc.index.get(updated.id).path == "Akten/2026-01-05 Rechnung.pdf"


def test_update_detects_change_since_indexing(svc, filed):
    path = svc.layout.archive / filed.path
    with open(path, "ab") as handle:  # someone edits the file over SMB
        handle.write(b"\n% edited\n")
    changed = path.read_bytes()
    with pytest.raises(Conflict, match="zwischenzeitlich geändert"):
        svc.ops.update_document(filed.id, MetadataUpdate(title="Neu"), user=None)
    assert path.read_bytes() == changed
    assert archive_files(svc.layout) == [filed.path]
    # The index was refreshed, so the next attempt (after reloading) works.
    assert svc.ops.update_document(filed.id, MetadataUpdate(title="Neu"), user=None).title == "Neu"


def test_update_detects_change_during_write(svc, filed, monkeypatch):
    path = svc.layout.archive / filed.path
    real_write = ops_module.write_meta

    def write_and_interfere(src, dst, meta):
        real_write(src, dst, meta)
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))  # concurrent change

    monkeypatch.setattr(ops_module, "write_meta", write_and_interfere)
    before = path.read_bytes()
    with pytest.raises(Conflict, match="zwischenzeitlich geändert"):
        svc.ops.update_document(filed.id, MetadataUpdate(tags=["anders"]), user=None)
    assert path.read_bytes() == before
    assert archive_files(svc.layout) == [filed.path]  # no temporary file left behind


def test_update_assigns_uuid_to_provisional_id(svc, samples):
    record = add_archive_pdf(svc, samples, "Fremd/Scan 0001.pdf", text="fremd")
    assert is_provisional(record.id)
    updated = svc.ops.update_document(record.id, MetadataUpdate(tags=["Garantie"]), user=None)
    assert not is_provisional(updated.id)
    meta = read_meta(svc.layout.archive / updated.path)
    assert meta.doc_id == updated.id and meta.title == "Scan 0001" and meta.origin_hash == record.content_hash
    assert svc.index.get(record.id) is None and svc.index.get(updated.id).path == "Fremd/Scan 0001.pdf"
    assert updated.path == record.path  # tags only: no rename
    second = svc.ops.update_document(updated.id, MetadataUpdate(sender="Hersteller"), user=None)
    assert second.id == updated.id


def test_update_non_pdf_restrictions(svc):
    record = add_archive_file(svc, "Fotos/Garantiebeleg.jpg", b"\xff\xd8\xff\xe0 jpeg")
    with pytest.raises(ProcessingError, match="nur in PDF"):
        svc.ops.update_document(record.id, MetadataUpdate(tags=["Garantie"]), user=None)
    with pytest.raises(ProcessingError):
        svc.ops.update_document(record.id, MetadataUpdate(sender="Laden"), user=None)
    renamed = svc.ops.update_document(record.id, MetadataUpdate(title="Kassenbon", doc_date=date(2025, 5, 1)), user=None)
    assert renamed.path == "Fotos/2025-05-01 Kassenbon.jpg" and renamed.id == record.id
    assert (svc.layout.archive / renamed.path).read_bytes() == b"\xff\xd8\xff\xe0 jpeg"
    moved = svc.ops.update_document(record.id, MetadataUpdate(folder="Belege"), user=None)
    assert moved.path == "Belege/2025-05-01 Kassenbon.jpg"


def test_update_encrypted_pdf_allows_rename_only(svc, samples, tmp_path):
    plain = samples.text_pdf(tmp_path / "plain.pdf", ["geheim"])
    target = svc.layout.archive / "Privat/2026-01-01 Geheim.pdf"
    target.parent.mkdir(parents=True)
    with pikepdf.open(plain) as pdf:
        pdf.save(target, encryption=pikepdf.Encryption(owner="owner", user="", R=4))
    record = svc.reconciler.index_file(target)
    with pytest.raises(UnsupportedFile):
        svc.ops.update_document(record.id, MetadataUpdate(tags=["x"]), user=None)
    renamed = svc.ops.update_document(record.id, MetadataUpdate(title="Vertraulich"), user=None)
    assert renamed.filename == "2026-01-01 Vertraulich.pdf"
    assert archive_files(svc.layout) == ["Privat/2026-01-01 Vertraulich.pdf"]


def test_update_missing_file_is_not_found_and_forgotten(svc, filed):
    (svc.layout.archive / filed.path).unlink()
    with pytest.raises(NotFound):
        svc.ops.update_document(filed.id, MetadataUpdate(title="Neu"), user=None)
    assert svc.index.get(filed.id) is None


def test_move_document_with_collision_and_noop(svc, samples, filed):
    add_archive_pdf(svc, samples, "Archiv 2025/2026-01-05 Stromrechnung Januar.pdf", text="anderes")
    moved = svc.ops.move_document(filed.id, "archiv 2025", user="max")
    assert moved.path == "Archiv 2025/2026-01-05 Stromrechnung Januar (2).pdf" and moved.id == filed.id
    assert not (svc.layout.archive / filed.path).exists()
    assert svc.index.get(filed.id).folder == "Archiv 2025"
    assert svc.ops.move_document(filed.id, "Archiv 2025", user=None).path == moved.path
    assert svc.audit.tail(1)[0]["action"] == "move"


def test_transient_errors_are_retried(svc, filed, monkeypatch):
    calls = {"n": 0}
    real_place = fsops.place_exact

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError(errno.EBUSY, "Device or resource busy")
        return real_place(*args, **kwargs)

    monkeypatch.setattr(fsops, "place_exact", flaky)
    moved = svc.ops.move_document(filed.id, "Neu", user=None)
    assert moved.folder == "Neu" and calls["n"] == 3


def test_locked_file_becomes_conflict(svc, filed, monkeypatch):
    def locked(*args, **kwargs):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(fsops, "place_exact", locked)
    with pytest.raises(Conflict, match="gesperrt"):
        svc.ops.move_document(filed.id, "Neu", user=None)
    assert archive_files(svc.layout) == [filed.path]


def test_delete_document_goes_to_trash(svc, filed):
    entry_id = svc.ops.delete_document(filed.id, user="max")
    assert archive_files(svc.layout) == []
    assert svc.index.get(filed.id) is None
    entry = svc.trash.get(entry_id)
    assert entry.kind == "document" and entry.original_path == filed.path and entry.doc_id == filed.id
    assert entry.deleted_by == "max" and [p.name for p in entry.files] == [filed.filename]
    with pytest.raises(NotFound):
        svc.ops.delete_document(filed.id, user=None)


# ---------------------------------------------------------------------------
# Folders


def test_create_folder(svc):
    assert svc.ops.create_folder(" Steuern / 2026 ", user="max") == "Steuern/2026"
    assert (svc.layout.archive / "Steuern/2026").is_dir()
    assert svc.index.folder_exists("Steuern") and svc.index.folder_exists("Steuern/2026")
    with pytest.raises(Conflict):
        svc.ops.create_folder("steuern/2026", user=None)
    assert svc.ops.create_folder("steuern/2027", user=None) == "Steuern/2027"
    (svc.layout.archive / "Datei").write_bytes(b"x")
    with pytest.raises(Conflict):
        svc.ops.create_folder("Datei/Unterordner", user=None)
    with pytest.raises(PathError):
        svc.ops.create_folder("../draussen", user=None)
    with pytest.raises(PathError):
        svc.ops.create_folder("  ", user=None)


def test_rename_folder_updates_documents_below(svc, samples):
    top = add_archive_pdf(svc, samples, "Bank/2026-01-31 Auszug.pdf")
    deep = add_archive_pdf(svc, samples, "Bank/Giro/2026-02-28 Auszug.pdf")
    other = add_archive_pdf(svc, samples, "Bankfremd/2026-01-01 Brief.pdf")
    svc.index.add_folder_with_parents("Bank/Giro")
    assert svc.ops.rename_folder("Bank", "Sparkasse", user="max") == "Sparkasse"
    assert (svc.layout.archive / "Sparkasse/Giro/2026-02-28 Auszug.pdf").is_file()
    assert not (svc.layout.archive / "Bank").exists()
    assert svc.index.get(top.id).path == "Sparkasse/2026-01-31 Auszug.pdf"
    assert svc.index.get(deep.id).path == "Sparkasse/Giro/2026-02-28 Auszug.pdf"
    assert svc.index.get(deep.id).folder == "Sparkasse/Giro"
    assert svc.index.get(other.id).path == "Bankfremd/2026-01-01 Brief.pdf"
    assert svc.index.folder_exists("Sparkasse/Giro") and not svc.index.folder_exists("Bank/Giro")
    assert svc.index.get_pages(deep.id)


def test_rename_folder_refuses_existing_target_and_allows_case_change(svc):
    (svc.layout.archive / "Akten").mkdir()
    (svc.layout.archive / "Belege").mkdir()
    (svc.layout.archive / "Akten" / "a.txt").write_bytes(b"a")
    with pytest.raises(Conflict):
        svc.ops.rename_folder("Akten", "belege", user=None)
    assert (svc.layout.archive / "Akten" / "a.txt").read_bytes() == b"a"
    assert svc.ops.rename_folder("Akten", "AKTEN", user=None) == "AKTEN"
    assert sorted(os.listdir(svc.layout.archive)) == ["AKTEN", "Belege"]
    with pytest.raises(PathError):
        svc.ops.rename_folder("AKTEN", "a/b", user=None)
    with pytest.raises(PathError):
        svc.ops.rename_folder("", "x", user=None)
    with pytest.raises(NotFound):
        svc.ops.rename_folder("Gibtsnicht", "x", user=None)


def test_delete_folder_only_when_empty(svc, samples):
    (svc.layout.archive / "Leer").mkdir()
    svc.index.add_folder_with_parents("Leer")
    svc.ops.delete_folder("Leer", user=None)
    assert not (svc.layout.archive / "Leer").exists() and not svc.index.folder_exists("Leer")

    junk = svc.layout.archive / "Mac"
    junk.mkdir()
    (junk / ".DS_Store").write_bytes(b"x")
    (junk / "._Bild.pdf").write_bytes(b"x")
    svc.ops.delete_folder("Mac", user=None)
    assert not junk.exists()

    add_archive_pdf(svc, samples, "Voll/2026-01-01 Brief.pdf")
    with pytest.raises(Conflict, match="nicht leer"):
        svc.ops.delete_folder("Voll", user=None)
    (svc.layout.archive / "Eltern" / "Kind").mkdir(parents=True)
    with pytest.raises(Conflict):
        svc.ops.delete_folder("Eltern", user=None)
    with pytest.raises(PathError):
        svc.ops.delete_folder("", user=None)
    with pytest.raises(NotFound):
        svc.ops.delete_folder("Gibtsnicht", user=None)


# ---------------------------------------------------------------------------
# Inbox items


def test_delete_inbox_item_and_restore(svc, samples, tmp_path):
    item = make_ready_item(svc, samples, tmp_path)
    entry_id = svc.ops.delete_inbox_item(item.id, user="erika")
    assert svc.inbox.find(item.id) is None
    entry = svc.trash.get(entry_id)
    assert entry.kind == "inbox" and entry.item_id == item.id and entry.files[0].name.startswith("scan-")
    restored = svc.trash.restore(entry_id, user="erika")
    assert restored.state == "bereit" and _origin_intact(svc, item.id)
    assert svc.ops.file_item(item.id, _request(), user=None).path in archive_files(svc.layout)


def test_delete_inbox_item_refusals(svc, samples, tmp_path):
    done = make_ready_item(svc, samples, tmp_path)
    svc.ops.file_item(done.id, _request(), user=None)
    with pytest.raises(Conflict):
        svc.ops.delete_inbox_item(done.id, user=None)

    unfinished = make_ready_item(svc, samples, tmp_path)
    unfinished.filing = {"state": "published", "sha256": "0" * 64, "folder": "X", "filename": "x.pdf"}
    svc.inbox.save(unfinished)
    with pytest.raises(Conflict):
        svc.ops.delete_inbox_item(unfinished.id, user=None)

    busy = make_ready_item(svc, samples, tmp_path)
    svc.inbox.move(busy, "arbeit")
    svc.index.enqueue("process", busy.id)
    svc.index.claim(["process"], "worker-1")
    with pytest.raises(Conflict, match="verarbeitet"):
        svc.ops.delete_inbox_item(busy.id, user=None)
    with pytest.raises(NotFound):
        svc.ops.delete_inbox_item("20200101-000000-000000", user=None)


def test_operations_hold_the_archive_lock(svc, filed):
    """While another thread holds the lock (a reconciliation pass), operations wait."""
    started = threading.Event()
    release = threading.Event()

    def hold():
        with svc.archive_lock:
            started.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    started.wait(5)
    result: list[object] = []
    worker = threading.Thread(target=lambda: result.append(svc.ops.move_document(filed.id, "Neu", user=None)))
    worker.start()
    worker.join(0.3)
    assert worker.is_alive() and (svc.layout.archive / filed.path).exists()
    release.set()
    worker.join(5)
    holder.join(5)
    assert result and result[0].folder == "Neu"


# ---------------------------------------------------------------------------
# Against the real reconciler: after every operation, a full reconciliation
# pass must find the index exactly in line with the disk.


@pytest.fixture
def real(base_services):
    reconcile = pytest.importorskip("docushelf.index.reconcile")
    attach_archive(base_services, reconciler=False)
    base_services.reconciler = reconcile.Reconciler(base_services)
    return base_services


def _assert_index_matches_disk(svc):
    stats = svc.reconciler.scan(full=True)
    assert (stats.added, stats.removed, stats.moved, stats.updated, stats.errors) == (0, 0, 0, 0, 0), stats
    indexed = sorted(svc.index.path_index())
    assert indexed == [f for f in archive_files(svc.layout)]


def test_real_reconciler_filing_and_edits(real, samples, tmp_path):
    item = make_ready_item(real, samples, tmp_path)
    record = real.ops.file_item(item.id, _request(), user=None)
    assert record.id == read_meta(real.layout.archive / record.path).doc_id
    _assert_index_matches_disk(real)

    renamed = real.ops.update_document(record.id, MetadataUpdate(title="Abschlag", doc_date=date(2026, 3, 1),
                                                                 tags=["Strom", "2026"]), user=None)
    assert renamed.id == record.id and renamed.path == "Wohnung/Strom/2026-03-01 Abschlag.pdf"
    assert renamed.tags == ["Strom", "2026"]
    _assert_index_matches_disk(real)

    moved = real.ops.move_document(record.id, "Archiv/2026", user=None)
    assert moved.id == record.id and moved.folder == "Archiv/2026"
    _assert_index_matches_disk(real)

    assert real.ops.rename_folder("Archiv", "Altablage", user=None) == "Altablage"
    assert real.index.get(record.id).path == "Altablage/2026/2026-03-01 Abschlag.pdf"
    _assert_index_matches_disk(real)

    entry_id = real.ops.delete_document(record.id, user=None)
    assert real.index.get(record.id) is None
    _assert_index_matches_disk(real)
    restored = real.trash.restore(entry_id, user=None)
    assert restored.id == record.id
    _assert_index_matches_disk(real)


def test_real_reconciler_provisional_and_case_rename(real, samples):
    path = real.layout.archive / "Fremd" / "2026-01-05 rechnung.pdf"
    samples.text_pdf(path, ["fremde Rechnung"])
    real.reconciler.scan(full=True)
    foreign = real.index.get_by_path("Fremd/2026-01-05 rechnung.pdf")
    assert is_provisional(foreign.id)

    updated = real.ops.update_document(foreign.id, MetadataUpdate(title="Rechnung", sender="Händler"), user=None)
    assert not is_provisional(updated.id) and updated.filename == "2026-01-05 Rechnung.pdf"
    assert real.index.count() == 1 and real.index.get(foreign.id) is None
    _assert_index_matches_disk(real)

    photo = real.layout.archive / "Fremd" / "beleg.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xe0 jpeg")
    real.reconciler.scan(full=True)
    image = real.index.get_by_path("Fremd/beleg.jpg")
    renamed = real.ops.update_document(image.id, MetadataUpdate(title="Beleg", doc_date=date(2025, 1, 2)), user=None)
    assert renamed.id == image.id and renamed.path == "Fremd/2025-01-02 Beleg.jpg"
    _assert_index_matches_disk(real)


@pytest.mark.parametrize("point", ["publish_after", "index", "move"])
def test_real_reconciler_crash_recovery(real, samples, tmp_path, monkeypatch, point):
    item = make_ready_item(real, samples, tmp_path)
    crasher = Crasher()
    _crash_points(monkeypatch, crasher)[point]()
    with pytest.raises(SimulatedCrash):
        real.ops.file_item(item.id, _request(), user=None)
    assert real.ops.recover() == 1
    done = real.inbox.load(item.id)
    assert done.state == "erledigt" and real.index.get(done.filing["doc_id"]) is not None
    _assert_index_matches_disk(real)


# ---------------------------------------------------------------------------
# Edge cases


def test_metadata_write_error_leaves_item_fileable(svc, samples, tmp_path, monkeypatch):
    item = make_ready_item(svc, samples, tmp_path)

    def broken(src, dst, meta):
        Path(dst).write_bytes(b"half")
        raise pikepdf.PdfError("damaged object stream")

    monkeypatch.setattr(ops_module, "write_meta", broken)
    with pytest.raises(ProcessingError, match="Metadaten"):
        svc.ops.file_item(item.id, _request(), user=None)
    loaded = svc.inbox.load(item.id)
    assert loaded.filing is None and not loaded.work_dir.exists() and loaded.document_path.is_file()
    assert archive_files(svc.layout) == []


def test_missing_archive_root_is_refused(svc, samples, tmp_path):
    item = make_ready_item(svc, samples, tmp_path)
    svc.layout.archive.rmdir()
    with pytest.raises(ProcessingError, match="nicht erreichbar"):
        svc.ops.file_item(item.id, _request(), user=None)
    assert not svc.layout.archive.exists()
    assert svc.inbox.load(item.id).filing is None


def test_folder_operations_accept_other_spelling(svc):
    (svc.layout.archive / "Bank").mkdir()
    svc.index.add_folder_with_parents("Bank")
    if not (svc.layout.archive / "bank").exists():
        pytest.skip("case-sensitive file system")
    assert svc.ops.rename_folder("bank", "Sparkasse", user=None) == "Sparkasse"
    assert svc.index.folder_exists("Sparkasse") and not svc.index.folder_exists("Bank")
    svc.ops.delete_folder("SPARKASSE", user=None)
    assert os.listdir(svc.layout.archive) == []


def test_file_item_tolerates_missing_rules_module(svc, samples, tmp_path, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "docushelf.suggest.rules", None)  # import fails
    item = make_ready_item(svc, samples, tmp_path)
    record = svc.ops.file_item(item.id, _request(create_rule=True), user=None)
    done = svc.inbox.load(item.id)
    assert done.state == "erledigt" and record.path in archive_files(svc.layout)
    assert done.filing["rule_error"]


# ---------------------------------------------------------------------------
# Automatic filings keep their decision (journal, audit log, item.json)

AUTO = {
    "decision": "file",
    "confidence": 0.82,
    "review": False,
    "sources": ["similarity", "llm"],
    "reasons": ["ähnlich zu „2025-12-05 Stromrechnung Dezember“ (88 %)"],
    "at": "2026-11-24T10:00:00+01:00",
}


def _auto_entries(svc) -> list[dict]:
    return [entry for entry in svc.audit.tail(100) if entry["action"] == "auto_file"]


def test_automatic_filing_keeps_its_decision(svc, samples, tmp_path):
    item = make_ready_item(svc, samples, tmp_path, source="smb")

    record = svc.ops.file_item(item.id, _request(), user="automatisch", auto=dict(AUTO))

    done = svc.inbox.load(item.id)
    assert done.state == "erledigt"
    assert done.filing["auto"] == AUTO and done.filing["auto_audited"] is True
    assert done.context["auto"] == {**AUTO, "doc_id": record.id, "path": record.path}
    (entry,) = _auto_entries(svc)
    assert entry == {
        "at": entry["at"],
        "user": "automatisch",
        "action": "auto_file",
        "doc_id": record.id,
        "path": record.path,
        "title": "Stromrechnung Januar",
        "folder": "Wohnung/Strom",
        "confidence": 0.82,
        "review": False,
        "sources": ["similarity", "llm"],
        "reasons": AUTO["reasons"],
        "item": item.id,
    }
    file_entry = next(e for e in svc.audit.tail(10) if e["action"] == "file")
    assert file_entry["user"] == "automatisch" and file_entry["doc_id"] == record.id
    assert f"Automatisch abgelegt als „{record.path}“" in svc.inbox.read_log(done)


def test_filing_by_a_person_records_no_automatic_decision(svc, samples, tmp_path):
    item = make_ready_item(svc, samples, tmp_path)
    svc.ops.file_item(item.id, _request(), user="max")
    done = svc.inbox.load(item.id)
    assert "auto" not in done.filing and "auto" not in done.context
    assert _auto_entries(svc) == []
    assert "Automatisch" not in svc.inbox.read_log(done)


def test_automatic_decision_is_stored_as_plain_values():
    record = ops_module.auto_filing_record(
        {"confidence": "1.7", "review": 1, "sources": "rule", "reasons": ("Regel „Strom“", None, "  "),
         "at": None, "extra": {"nested": True}, "rule": "Strom"}
    )
    assert record["decision"] == "file" and record["confidence"] == 1.0 and record["review"] is True
    assert record["sources"] == ["rule"] and record["reasons"] == ["Regel „Strom“"]
    assert record["at"] and record["rule"] == "Strom" and "extra" not in record
    assert ops_module.auto_filing_record({"confidence": "viel"})["confidence"] == 0.0
    assert ops_module.auto_filing_record(None) is None and ops_module.auto_filing_record({}) is None


@pytest.mark.parametrize("point", sorted(CRASH_OUTCOMES))
def test_crash_during_automatic_filing_then_recover(svc, samples, tmp_path, monkeypatch, point):
    item = make_ready_item(svc, samples, tmp_path)
    crasher = Crasher()
    _crash_points(monkeypatch, crasher)[point]()

    with pytest.raises(SimulatedCrash):
        svc.ops.file_item(item.id, _request(), user="automatisch", auto=dict(AUTO))
    svc.ops.recover()
    assert svc.ops.recover() == 0

    recovered = svc.inbox.load(item.id)
    assert recovered.state == CRASH_OUTCOMES[point]
    actions = [e["action"] for e in svc.audit.tail(50)]
    if recovered.state == "bereit":
        assert actions.count("file") == 0 and actions.count("auto_file") == 0
        # Either nothing was recorded yet, or the item says why it waits for a person now.
        assert recovered.context.get("auto") in (
            None,
            {"decision": "hold", "confidence": 0.82, "reasons": ["Automatische Ablage wurde unterbrochen"],
             "at": (recovered.context.get("auto") or {}).get("at")},
        )
    else:
        assert actions.count("file") == 1 and actions.count("auto_file") == 1
        (entry,) = _auto_entries(svc)
        assert entry["doc_id"] == recovered.filing["doc_id"] and entry["item"] == item.id
        assert recovered.context["auto"]["decision"] == "file"
        assert recovered.context["auto"]["path"] == "Wohnung/Strom/2026-01-05 Stromrechnung Januar.pdf"
