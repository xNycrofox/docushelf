"""Tests for the trash: deleting and restoring documents and inbox items, purging."""

from __future__ import annotations

import errno
import os
from datetime import timedelta

import pytest

from archive_support import add_archive_pdf, archive_files, attach_archive, make_ready_item
from docushelf.archive import ops as ops_module
from docushelf.archive.trash import ENTRY_FILE, TrashEntry
from docushelf.errors import Conflict, NotFound
from docushelf.inbox.items import InboxItem
from docushelf.models import DocumentRecord, FilingRequest
from docushelf.pdf.xmp import read_meta
from docushelf.util import fsops
from docushelf.util.timeutil import now


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    monkeypatch.setattr(ops_module, "RETRY_PAUSE", 0)


@pytest.fixture
def svc(base_services):
    return attach_archive(base_services)


def _age_entry(svc, entry_id: str, days: int) -> None:
    path = svc.layout.trash / entry_id / ENTRY_FILE
    data = fsops.read_json(path)
    data["deleted_at"] = (now() - timedelta(days=days)).isoformat(timespec="seconds")
    fsops.atomic_write_json(path, data)


def test_put_document_list_get(svc, samples):
    record = add_archive_pdf(svc, samples, "Finanzen/Bank/2026-01-31 Auszug.pdf", text="Kontoauszug")
    path = svc.layout.archive / record.path
    content = path.read_bytes()
    entry_id = svc.trash.put_document(path, record, user="max")

    assert not path.exists() and svc.index.get(record.id) is None
    entry = svc.trash.get(entry_id)
    assert isinstance(entry, TrashEntry)
    assert (entry.id, entry.kind, entry.deleted_by) == (entry_id, "document", "max")
    assert entry.original_path == record.path and entry.folder == "Finanzen/Bank"
    assert entry.title == "Auszug" and entry.doc_id == record.id and entry.item_id is None
    assert entry.files[0].read_bytes() == content and entry.meta["content_hash"] == record.content_hash
    assert entry.deleted is not None and svc.trash.expires_at(entry) == entry.deleted + timedelta(days=30)
    raw = fsops.read_json(svc.layout.trash / entry_id / ENTRY_FILE)
    assert {"id", "kind", "deleted_at", "deleted_by", "original_path", "title", "doc_id", "meta"} <= set(raw)
    assert [e.id for e in svc.trash.list()] == [entry_id]
    assert svc.audit.tail(1)[0]["action"] == "delete"
    with pytest.raises(NotFound):
        svc.trash.get("../../etc")
    with pytest.raises(NotFound):
        svc.trash.get("20200101-000000-000000")


def test_list_is_newest_first(svc, samples):
    ids = []
    for n, days in enumerate((5, 1, 3)):
        record = add_archive_pdf(svc, samples, f"A/2026-01-0{n + 1} Brief.pdf")
        ids.append(svc.trash.put_document(svc.layout.archive / record.path, record, user=None))
        _age_entry(svc, ids[-1], days)
    assert [e.id for e in svc.trash.list()] == [ids[1], ids[2], ids[0]]


def test_restore_document_recreates_folder(svc, samples):
    record = add_archive_pdf(svc, samples, "Wohnung/Miete/2026-01-01 Mietvertrag.pdf", text="Mietvertrag")
    entry_id = svc.ops.delete_document(record.id, user=None)
    svc.ops.delete_folder("Wohnung/Miete", user=None)
    svc.ops.delete_folder("Wohnung", user=None)
    restored = svc.trash.restore(entry_id, user="erika")
    assert isinstance(restored, DocumentRecord)
    assert restored.path == record.path and restored.id == record.id
    assert archive_files(svc.layout) == [record.path]
    assert svc.index.get(record.id).path == record.path and svc.index.folder_exists("Wohnung/Miete")
    assert svc.trash.list() == [] and os.listdir(svc.layout.trash) == []
    with pytest.raises(NotFound):
        svc.trash.restore(entry_id, user=None)
    assert svc.audit.tail(1)[0]["action"] == "restore"


def test_restore_document_never_overwrites(svc, samples):
    record = add_archive_pdf(svc, samples, "Akten/2026-01-01 Brief.pdf", text="alt")
    entry_id = svc.ops.delete_document(record.id, user=None)
    newcomer = add_archive_pdf(svc, samples, "akten/2026-01-01 brief.pdf", text="neu")
    newcomer_bytes = (svc.layout.archive / newcomer.path).read_bytes()
    restored = svc.trash.restore(entry_id, user=None)
    assert (svc.layout.archive / newcomer.path).read_bytes() == newcomer_bytes
    assert len(archive_files(svc.layout)) == 2
    probe = svc.layout.archive / "Akten"
    if probe.is_dir() and (svc.layout.archive / "akten").is_dir() and os.path.samefile(probe, svc.layout.archive / "akten"):
        # Case-insensitive disk (macOS): same folder, so the restored file gets a suffix.
        assert restored.filename == "2026-01-01 Brief (2).pdf"
    else:
        # Case-sensitive disk (Linux): "Akten" and "akten" are different folders.
        assert restored.path != newcomer.path


def test_restore_of_filed_document_keeps_its_uuid(svc, samples, tmp_path):
    item = make_ready_item(svc, samples, tmp_path)
    filed = svc.ops.file_item(item.id, FilingRequest(folder="Strom", title="Rechnung"), user=None)
    entry_id = svc.ops.delete_document(filed.id, user=None)
    restored = svc.trash.restore(entry_id, user=None)
    assert restored.id == filed.id == read_meta(svc.layout.archive / restored.path).doc_id


def test_purge(svc, samples):
    record = add_archive_pdf(svc, samples, "A/2026-01-01 Brief.pdf")
    entry_id = svc.trash.put_document(svc.layout.archive / record.path, record, user=None)
    svc.trash.purge(entry_id, user="max")
    assert svc.trash.list() == [] and os.listdir(svc.layout.trash) == []
    assert svc.audit.tail(1)[0]["action"] == "purge"
    with pytest.raises(NotFound):
        svc.trash.purge(entry_id, user=None)


def test_purge_expired_respects_retention(svc, samples):
    old = add_archive_pdf(svc, samples, "A/2026-01-01 Alt.pdf")
    new = add_archive_pdf(svc, samples, "A/2026-01-02 Neu.pdf")
    old_id = svc.trash.put_document(svc.layout.archive / old.path, old, user=None)
    new_id = svc.trash.put_document(svc.layout.archive / new.path, new, user=None)
    _age_entry(svc, old_id, 31)
    _age_entry(svc, new_id, 29)
    assert svc.trash.purge_expired() == 1
    assert [e.id for e in svc.trash.list()] == [new_id]
    assert svc.audit.tail(1)[0]["action"] == "purge_expired"

    svc.settings = svc.settings.with_overrides(trash_retention_days=0)  # keep forever
    _age_entry(svc, new_id, 400)
    assert svc.trash.purge_expired() == 0 and [e.id for e in svc.trash.list()] == [new_id]


def test_purge_expired_cleans_leftovers(svc, samples):
    # An entry whose file never arrived (crash between entry.json and the move) …
    record = add_archive_pdf(svc, samples, "A/2026-01-01 Brief.pdf")
    entry_id = svc.trash.put_document(svc.layout.archive / record.path, record, user=None)
    moved = svc.trash.get(entry_id).files[0]
    os.replace(moved, svc.layout.archive / record.path)  # … looks like this
    # An empty shell (crash right after mkdir) and an interrupted purge.
    (svc.layout.trash / "20260101-000000-aaaaaa" / "files").mkdir(parents=True)
    (svc.layout.trash / ".docushelf-purge-20260101-000000-bbbbbb-1234").mkdir()
    # Unknown content is never deleted.
    stranger = svc.layout.trash / "20260101-000000-cccccc" / "files"
    stranger.mkdir(parents=True)
    (stranger / "wichtig.pdf").write_bytes(b"%PDF")

    assert svc.trash.list() == []
    assert svc.trash.purge_expired() == 0
    assert sorted(os.listdir(svc.layout.trash)) == ["20260101-000000-cccccc"]
    assert archive_files(svc.layout) == [record.path]


def test_failed_move_leaves_document_in_place(svc, samples, monkeypatch):
    record = add_archive_pdf(svc, samples, "A/2026-01-01 Brief.pdf")
    path = svc.layout.archive / record.path
    real_unlink = os.unlink

    def unlink(target, *args, **kwargs):
        if os.fspath(target) == os.fspath(path):
            raise OSError(errno.EIO, "Input/output error")
        return real_unlink(target, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)
    with pytest.raises(Exception):
        svc.trash.put_document(path, record, user=None)
    monkeypatch.undo()
    assert path.is_file() and svc.index.get(record.id) is not None
    assert os.listdir(svc.layout.trash) == []


def test_inbox_item_roundtrip_to_ready(svc, samples, tmp_path):
    item = make_ready_item(svc, samples, tmp_path, context={"title": "Brief vom Amt"})
    entry_id = svc.trash.put_inbox_item(item, user="erika")
    assert svc.inbox.find(item.id) is None
    entry = svc.trash.get(entry_id)
    assert entry.kind == "inbox" and entry.item_id == item.id and entry.title == "Brief vom Amt"
    assert entry.original_path is None and entry.meta["state"] == "bereit"
    assert [p.name for p in entry.files] == [p.name for p in item.original_paths()]
    restored = svc.trash.restore(entry_id, user="erika")
    assert isinstance(restored, InboxItem) and restored.state == "bereit"
    assert [h["event"] for h in restored.history][-2:] == ["deleted", "restored"]
    assert "Aus dem Papierkorb wiederhergestellt" in svc.inbox.read_log(restored)
    assert os.listdir(svc.layout.trash) == []


def test_inbox_item_restore_to_failed_and_to_processing(svc, samples, tmp_path):
    failed = make_ready_item(svc, samples, tmp_path)
    failed.error = {"stage": "ocr_done", "message": "kaputt", "at": "2026-01-01T00:00:00+01:00"}
    svc.inbox.save(failed)
    svc.inbox.move(failed, "fehler")
    restored = svc.trash.restore(svc.trash.put_inbox_item(failed, user=None), user=None)
    assert restored.state == "fehler"

    stuck = make_ready_item(svc, samples, tmp_path)
    stuck.stage = "deblanked"
    stuck.document_path.unlink()
    svc.inbox.save(stuck)
    svc.inbox.move(stuck, "arbeit")
    restored = svc.trash.restore(svc.trash.put_inbox_item(stuck, user=None), user=None)
    assert restored.state == "arbeit"
    assert [job.key for job in svc.index.pending_jobs("process")] == [stuck.id]


def test_restore_inbox_item_never_replaces_existing(svc, samples, tmp_path):
    item = make_ready_item(svc, samples, tmp_path)
    entry_id = svc.trash.put_inbox_item(item, user=None)
    (svc.layout.ready / item.id).mkdir()
    with pytest.raises(Conflict):
        svc.trash.restore(entry_id, user=None)
    assert svc.trash.get(entry_id).item_id == item.id


def test_purge_done_items(svc, samples, tmp_path):
    def filed_item(days_ago: int):
        item = make_ready_item(svc, samples, tmp_path)
        svc.ops.file_item(item.id, FilingRequest(folder="A", title=f"Brief {item.id}"), user=None)
        done = svc.inbox.load(item.id)
        done.filing["finished_at"] = (now() - timedelta(days=days_ago)).isoformat(timespec="seconds")
        svc.inbox.save(done)
        return done

    old = filed_item(45)
    recent = filed_item(3)
    unfinished = filed_item(90)
    unfinished.filing["state"] = "published"
    svc.inbox.save(unfinished)

    assert svc.trash.purge_done_items() == 1
    assert svc.inbox.find(old.id) is None
    assert svc.inbox.find(recent.id) is not None and svc.inbox.find(unfinished.id) is not None
    assert len(archive_files(svc.layout)) == 3  # the archive is never touched
    assert svc.audit.tail(1)[0]["action"] == "purge_done"

    svc.settings = svc.settings.with_overrides(keep_originals_days=0)
    recent.filing["finished_at"] = (now() - timedelta(days=400)).isoformat(timespec="seconds")
    svc.inbox.save(recent)
    assert svc.trash.purge_done_items() == 0


def test_purge_done_items_falls_back_to_history_and_mtime(svc, samples, tmp_path):
    item = make_ready_item(svc, samples, tmp_path)
    svc.ops.file_item(item.id, FilingRequest(folder="A", title="Brief"), user=None)
    done = svc.inbox.load(item.id)
    done.filing = None
    done.history[-1]["at"] = (now() - timedelta(days=60)).isoformat(timespec="seconds")
    svc.inbox.save(done)

    legacy = make_ready_item(svc, samples, tmp_path)
    svc.inbox.move(legacy, "erledigt")
    legacy.history = []
    svc.inbox.save(legacy)
    old = (now() - timedelta(days=60)).timestamp()
    os.utime(legacy.dir, (old, old))

    assert svc.trash.purge_done_items() == 2
    assert svc.inbox.list("erledigt") == []


def test_incomplete_entry_with_unknown_content_is_kept(svc, samples):
    record = add_archive_pdf(svc, samples, "A/2026-01-01 Brief.pdf")
    entry_id = svc.trash.put_document(svc.layout.archive / record.path, record, user=None)
    files_dir = svc.layout.trash / entry_id / "files"
    os.replace(svc.trash.get(entry_id).files[0], svc.layout.archive / record.path)
    (files_dir / "unterordner").mkdir()  # not something the trash would create
    assert svc.trash.purge_expired() == 0
    assert (files_dir / "unterordner").is_dir()
