"""Tests for archive fixes: signed and encrypted PDFs keep their bytes, Paperless
notes and custom fields reach the file, Finder-made folder names are reused,
extended attributes survive metadata edits, renamed folders take inbox
suggestions along, and filed items with open business are never purged."""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

import pytest

from archive_support import (
    add_archive_pdf,
    archive_files,
    attach_archive,
    make_encrypted_pdf,
    make_ready_item,
    make_signed_pdf,
)
from docushelf.archive import ops as ops_module
from docushelf.archive.ops import clean_text, context_extras
from docushelf.errors import Conflict, PathError, UnsupportedFile
from docushelf.models import FilingRequest, MetadataUpdate
from docushelf.pdf.xmp import read_meta
from docushelf.util import fsops
from docushelf.util.ids import is_provisional
from docushelf.util.timeutil import now


class SimulatedCrash(BaseException):
    """Stands in for the process dying: the application must not catch it."""


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    monkeypatch.setattr(ops_module, "RETRY_PAUSE", 0)


@pytest.fixture
def svc(base_services):
    return attach_archive(base_services)


def _request(**changes) -> FilingRequest:
    values = dict(folder="Telefon", title="Rechnung Telekom", doc_date=date(2026, 3, 5), tags=[], sender=None)
    values.update(changes)
    return FilingRequest(**values)


PAPERLESS = {
    "id": 17,
    "archive_serial_number": 120,
    "notes": [
        {"text": "Mit Karte bezahlt", "created": "2024-03-01T09:15:00Z", "user": "max"},
        {"text": "Widerspruch eingelegt\nam 3. Mai", "created": "2024-05-02T23:30:00+00:00", "user": None},
        {"text": "   ", "created": "2024-06-01T10:00:00Z", "user": "max"},
    ],
    "custom_fields": [
        {"name": "Betrag", "type": "monetary", "value": "EUR199.99"},
        {"name": "Status", "type": "select", "value": "bezahlt"},
        {"name": "Erledigt", "type": "boolean", "value": True},
        {"name": "Verknüpft", "type": "documentlink", "value": [3, 7]},
        {"name": "Leer", "type": "string", "value": None},
    ],
}


# ---------------------------------------------------------------------------
# Signed and encrypted PDFs (contract C3)


def test_signed_pdf_is_filed_byte_for_byte(svc, samples, tmp_path):
    signed = make_signed_pdf(samples, tmp_path / "Rechnung.pdf", ["Telekom Deutschland GmbH\nRechnung"])
    original = signed.read_bytes()
    item = make_ready_item(svc, samples, tmp_path, original=signed)

    record = svc.ops.file_item(item.id, _request(tags=["Telefon"], sender="Telekom"), user="max")

    assert record.path == "Telefon/2026-03-05 Rechnung Telekom.pdf"
    assert (svc.layout.archive / record.path).read_bytes() == original
    assert record.title == "Rechnung Telekom" and record.doc_date == date(2026, 3, 5)
    assert is_provisional(record.id)  # no XMP id in the file: identified by its content
    done = svc.inbox.load(item.id)
    assert done.state == "erledigt" and done.filing["state"] == "done" and done.filing["protected"] == "signed"
    log = svc.inbox.read_log(done)
    assert "digital signiert und wird unverändert abgelegt" in log
    assert "Schlagwörter und Absender lassen sich in dieser Datei nicht speichern" in log
    assert all(p.read_bytes() == original for p in done.original_paths())


def test_encrypted_pdf_is_filed_byte_for_byte(svc, samples, tmp_path):
    encrypted = make_encrypted_pdf(samples, tmp_path / "Vertrag.pdf", ["Mietvertrag"])
    original = encrypted.read_bytes()
    item = make_ready_item(svc, samples, tmp_path, original=encrypted)

    record = svc.ops.file_item(item.id, _request(folder="Wohnung", title="Mietvertrag", tags=[]), user=None)

    assert (svc.layout.archive / record.path).read_bytes() == original
    done = svc.inbox.load(item.id)
    assert done.filing["protected"] == "encrypted"
    log = svc.inbox.read_log(done)
    assert "verschlüsselt und wird unverändert abgelegt" in log and "Schlagwörter" not in log


def test_pipeline_verdict_alone_keeps_the_bytes(svc, samples, tmp_path):
    """If the pipeline marked the document as protected, it is never rewritten."""
    item = make_ready_item(svc, samples, tmp_path, context={"pdf_protection": "signed"})
    document = item.document_path.read_bytes()
    record = svc.ops.file_item(item.id, _request(), user=None)
    assert (svc.layout.archive / record.path).read_bytes() == document


def test_protected_filing_survives_a_crash_and_recover(svc, samples, tmp_path, monkeypatch):
    signed = make_signed_pdf(samples, tmp_path / "Rechnung.pdf", ["Rechnung"])
    item = make_ready_item(svc, samples, tmp_path, original=signed)
    real_publish = fsops.publish_no_clobber

    def publish_then_die(*args, **kwargs):
        real_publish(*args, **kwargs)
        raise SimulatedCrash()

    monkeypatch.setattr(fsops, "publish_no_clobber", publish_then_die)
    with pytest.raises(SimulatedCrash):
        svc.ops.file_item(item.id, _request(), user=None)
    monkeypatch.setattr(fsops, "publish_no_clobber", real_publish)

    assert svc.ops.recover() == 1
    done = svc.inbox.load(item.id)
    assert done.state == "erledigt" and archive_files(svc.layout) == ["Telefon/2026-03-05 Rechnung Telekom.pdf"]
    assert (svc.layout.archive / archive_files(svc.layout)[0]).read_bytes() == signed.read_bytes()


def test_signed_archive_file_title_edit_is_rename_only(svc, samples, tmp_path):
    path = svc.layout.archive / "Verträge" / "2025-01-01 Vertrag.pdf"
    path.parent.mkdir(parents=True)
    make_signed_pdf(samples, path, ["Vertrag"])
    original = path.read_bytes()
    record = svc.reconciler.index_file(path)

    renamed = svc.ops.update_document(record.id, MetadataUpdate(title="Mobilfunkvertrag", doc_date=date(2025, 2, 1)),
                                      user=None)

    assert renamed.path == "Verträge/2025-02-01 Mobilfunkvertrag.pdf" and renamed.id == record.id
    assert (svc.layout.archive / renamed.path).read_bytes() == original
    assert archive_files(svc.layout) == [renamed.path]

    with pytest.raises(UnsupportedFile, match="digital signierten PDF"):
        svc.ops.update_document(renamed.id, MetadataUpdate(tags=["Handy"]), user=None)
    with pytest.raises(UnsupportedFile, match="Titel, Datum und Ordner"):
        svc.ops.update_document(renamed.id, MetadataUpdate(sender="Telekom", title="Neu"), user=None)
    assert (svc.layout.archive / renamed.path).read_bytes() == original
    assert archive_files(svc.layout) == [renamed.path]

    moved = svc.ops.move_document(renamed.id, "Verträge/Handy", user=None)
    assert (svc.layout.archive / moved.path).read_bytes() == original


def test_encrypted_archive_file_title_edit_is_rename_only(svc, samples, tmp_path):
    path = svc.layout.archive / "Privat" / "2026-01-01 Geheim.pdf"
    path.parent.mkdir(parents=True)
    make_encrypted_pdf(samples, path, ["geheim"])
    original = path.read_bytes()
    record = svc.reconciler.index_file(path)

    renamed = svc.ops.update_document(record.id, MetadataUpdate(title="Vertraulich"), user=None)
    assert renamed.filename == "2026-01-01 Vertraulich.pdf"
    assert (svc.layout.archive / renamed.path).read_bytes() == original
    with pytest.raises(UnsupportedFile, match="verschlüsselten PDF"):
        svc.ops.update_document(renamed.id, MetadataUpdate(sender="Bank"), user=None)
    assert (svc.layout.archive / renamed.path).read_bytes() == original


@pytest.fixture
def full(services, monkeypatch):
    monkeypatch.setattr(ops_module, "RETRY_PAUSE", 0)
    return services


def test_signed_pdf_end_to_end_with_the_real_pipeline(full, samples, tmp_path):
    signed = make_signed_pdf(
        samples,
        tmp_path / "Rechnung März.pdf",
        ["Telekom Deutschland GmbH\nRechnung\nRechnungsdatum: 05.03.2026\nBetrag: 39,95 EUR"],
    )
    original = signed.read_bytes()
    item = full.inbox.create("upload", [signed], move=False)

    ready = full.pipeline.process(item.id)

    assert ready.state == "bereit", full.inbox.read_log(ready)
    assert ready.document_path.read_bytes() == original
    record = full.ops.file_item(item.id, _request(tags=["Telefon"]), user="max")
    assert (full.layout.archive / record.path).read_bytes() == original
    assert full.index.get(record.id).path == record.path

    renamed = full.ops.update_document(record.id, MetadataUpdate(title="Telefonrechnung"), user=None)
    assert (full.layout.archive / renamed.path).read_bytes() == original
    stats = full.reconciler.scan(full=True)
    assert (stats.added, stats.removed, stats.moved, stats.errors) == (0, 0, 0, 0)

    # The same invoice arriving again is still recognised, without any XMP in the file.
    again = full.inbox.create("mail", [signed], move=False)
    assert full.pipeline.process(again.id).duplicate_of["path"] == renamed.path


def test_encrypted_pdf_end_to_end_with_the_real_pipeline(full, samples, tmp_path):
    encrypted = make_encrypted_pdf(samples, tmp_path / "Kontoauszug.pdf", ["Sparkasse\nKontoauszug 3/2026"])
    original = encrypted.read_bytes()
    item = full.inbox.create("upload", [encrypted], move=False)

    ready = full.pipeline.process(item.id)

    assert ready.state == "bereit", full.inbox.read_log(ready)
    assert ready.document_path.read_bytes() == original
    record = full.ops.file_item(item.id, _request(folder="Bank", title="Kontoauszug"), user=None)
    assert (full.layout.archive / record.path).read_bytes() == original


# ---------------------------------------------------------------------------
# Paperless notes and custom fields (contract C2)


def test_context_extras_formats_notes_and_fields():
    extras = context_extras({"paperless": PAPERLESS})
    # Times are shown as local (Europe/Berlin) days: 23:30 UTC on 2 May is 3 May.
    assert extras["PaperlessNotes"] == "2024-03-01 max: Mit Karte bezahlt | 2024-05-03: Widerspruch eingelegt am 3. Mai"
    assert extras["PaperlessFields"] == "Betrag: EUR199.99; Status: bezahlt; Erledigt: ja; Verknüpft: 3, 7"
    assert extras["ArchiveSerialNumber"] == "120" and extras["PaperlessId"] == "17"
    assert "PaperlessNotes" not in context_extras({"paperless": {"notes": [], "custom_fields": []}})


def test_long_notes_are_not_cut_to_the_short_limit():
    text = "Lange Notiz " * 200
    extras = context_extras({"paperless": {"notes": [{"text": text, "created": None, "user": "max"}]},
                             "mail_subject": "Betreff " * 200})
    assert extras["PaperlessNotes"] == "max: " + text.strip()
    assert len(extras["MailSubject"]) <= 500
    huge = context_extras({"paperless": {"notes": [{"text": "x" * 50_000}]}})
    assert len(huge["PaperlessNotes"]) == 20_000


def test_filed_paperless_document_carries_notes_and_fields(svc, samples, tmp_path):
    item = make_ready_item(svc, samples, tmp_path, source="paperless", context={"paperless": PAPERLESS})
    record = svc.ops.file_item(item.id, _request(folder="Finanzen", title="Rechnung"), user=None)
    extra = read_meta(svc.layout.archive / record.path).extra
    assert extra["PaperlessNotes"].startswith("2024-03-01 max: Mit Karte bezahlt")
    assert extra["PaperlessFields"] == "Betrag: EUR199.99; Status: bezahlt; Erledigt: ja; Verknüpft: 3, 7"

    # Later edits keep them.
    svc.ops.update_document(record.id, MetadataUpdate(tags=["bezahlt"], title="Rechnung bezahlt"), user=None)
    updated = svc.index.get(record.id)
    assert read_meta(svc.layout.archive / updated.path).extra["PaperlessNotes"] == extra["PaperlessNotes"]


def test_signed_paperless_document_keeps_its_notes_in_the_inbox(svc, samples, tmp_path):
    signed = make_signed_pdf(samples, tmp_path / "signiert.pdf", ["Rechnung"])
    item = make_ready_item(svc, samples, tmp_path, original=signed, source="paperless",
                           context={"paperless": PAPERLESS})
    svc.ops.file_item(item.id, _request(), user=None)
    done = svc.inbox.load(item.id)
    assert done.filing["unstored"] == ["PaperlessNotes", "PaperlessFields", "ArchiveSerialNumber"]
    assert ("Notizen, eigene Felder und Archivnummer aus Paperless-ngx lassen sich darin ebenfalls nicht speichern "
            "und bleiben deshalb im erledigten Eingang erhalten") in svc.inbox.read_log(done)
    done.filing["finished_at"] = (now() - timedelta(days=400)).isoformat(timespec="seconds")
    svc.inbox.save(done)
    assert svc.trash.purge_done_items() == 0
    assert svc.inbox.find(item.id) is not None


# ---------------------------------------------------------------------------
# Characters XML cannot carry (sec-5)


def test_clean_text_drops_xml_invalid_characters():
    assert clean_text("Rechnung ￿ März") == "Rechnung März"
    assert clean_text("Rechnung￾\ud800 März\x00\x07") == "Rechnung März"
    assert clean_text("a\tb\nc") == "a b c"


def test_mail_subject_with_noncharacters_can_be_filed(svc, samples, tmp_path):
    # U+FFFE/U+FFFF survive item.json (valid UTF-8), so they reach the filing.
    item = make_ready_item(svc, samples, tmp_path, source="mail",
                           context={"mail_subject": "Rechnung ￿ März ￾", "mail_from": "a@example.com￿"})
    record = svc.ops.file_item(item.id, _request(title="Rechnung ￿März", sender="Telekom￾\ud800"), user=None)
    assert record.filename == "2026-03-05 Rechnung März.pdf"
    meta = read_meta(svc.layout.archive / record.path)
    assert meta.extra["MailSubject"] == "Rechnung März" and meta.extra["MailFrom"] == "a@example.com"
    assert meta.title == "Rechnung März" and meta.sender == "Telekom"
    assert svc.inbox.load(item.id).state == "erledigt"


# ---------------------------------------------------------------------------
# Folders made in the Finder are used as they are (ux-1)


@pytest.mark.parametrize(
    ("existing", "typed"),
    [
        ("Vereine/TSV Dresden e.V.", "Vereine/TSV Dresden e.V."),
        ("Vereine/TSV Dresden e.V.", "vereine / tsv dresden e.v"),
        ("Steuerberater Meier & Co.", "Steuerberater Meier & Co"),
        ("Kfz  Versicherung", "Kfz  Versicherung"),
        ("Kfz  Versicherung", "kfz versicherung"),
        ("Ärzte/Dr. Müller ", "ärzte/dr. müller"),
    ],
)
def test_filing_reuses_finder_made_folders(svc, samples, tmp_path, existing, typed):
    (svc.layout.archive / existing).mkdir(parents=True)
    item = make_ready_item(svc, samples, tmp_path)
    record = svc.ops.file_item(item.id, _request(folder=typed), user=None)
    assert record.folder == existing
    assert archive_files(svc.layout) == [f"{existing}/2026-03-05 Rechnung Telekom.pdf"]
    top = existing.split("/")[0]
    assert os.listdir(svc.layout.archive) == [top]


def test_only_new_components_are_sanitized(svc, samples, tmp_path):
    (svc.layout.archive / "Kfz  Versicherung").mkdir()
    item = make_ready_item(svc, samples, tmp_path)
    record = svc.ops.file_item(item.id, _request(folder="Kfz  Versicherung/Schaden  2026./Gutachten?"), user=None)
    assert record.folder == "Kfz  Versicherung/Schaden 2026/Gutachten"


def test_exact_spelling_wins_over_a_similar_folder(svc, samples, tmp_path):
    (svc.layout.archive / "Kfz Versicherung").mkdir()
    (svc.layout.archive / "Kfz  Versicherung").mkdir()
    first = svc.ops.file_item(make_ready_item(svc, samples, tmp_path).id, _request(folder="Kfz Versicherung"), user=None)
    second = svc.ops.file_item(make_ready_item(svc, samples, tmp_path).id, _request(folder="Kfz  Versicherung"),
                               user=None)
    assert (first.folder, second.folder) == ("Kfz Versicherung", "Kfz  Versicherung")
    # A suggestion carries the on-disk name, trailing space included.
    (svc.layout.archive / "Rechnungen").mkdir()
    (svc.layout.archive / "Rechnungen ").mkdir()
    third = svc.ops.file_item(make_ready_item(svc, samples, tmp_path).id, _request(folder="Rechnungen "), user=None)
    fourth = svc.ops.file_item(make_ready_item(svc, samples, tmp_path).id, _request(folder=" rechnungen"), user=None)
    assert (third.folder, fourth.folder) == ("Rechnungen ", "Rechnungen")


def test_title_edit_keeps_document_in_finder_made_folder(svc, samples):
    record = add_archive_pdf(svc, samples, "Kfz  Versicherung/2026-01-10 Beitrag.pdf")
    # The edit form posts the folder field as shown, i.e. exactly record.folder.
    updated = svc.ops.update_document(record.id, MetadataUpdate(title="Beitragsrechnung", folder=record.folder),
                                      user=None)
    assert updated.path == "Kfz  Versicherung/2026-01-10 Beitragsrechnung.pdf"
    assert os.listdir(svc.layout.archive) == ["Kfz  Versicherung"]
    moved = svc.ops.move_document(updated.id, "kfz versicherung", user=None)
    assert moved.path == updated.path


def test_hidden_and_linked_folders_are_never_reused(svc, samples, tmp_path):
    hidden = svc.layout.archive / ".versteckt"
    hidden.mkdir()
    record = svc.ops.file_item(make_ready_item(svc, samples, tmp_path).id, _request(folder=".versteckt"), user=None)
    assert record.folder == "versteckt" and os.listdir(hidden) == []

    outside = tmp_path / "draussen"
    outside.mkdir()
    (svc.layout.archive / "Verknüpfung").symlink_to(outside, target_is_directory=True)
    inside = svc.layout.archive / "Ziel"
    inside.mkdir()
    (svc.layout.archive / "Abkürzung").symlink_to(inside, target_is_directory=True)
    item = make_ready_item(svc, samples, tmp_path)
    for typed in ("Verknüpfung", "verknüpfung", "Verknüpfung/Unterordner", "Abkürzung", "abkürzung/x"):
        with pytest.raises((PathError, Conflict)):
            svc.ops.file_item(item.id, _request(folder=typed), user=None)
    with pytest.raises(Conflict, match="symbolischer Link"):
        svc.ops.file_item(item.id, _request(folder="Abkürzung"), user=None)
    assert os.listdir(outside) == [] and os.listdir(inside) == []
    with pytest.raises(PathError):
        svc.ops.file_item(item.id, _request(folder="Vereine/../../draussen"), user=None)
    assert svc.inbox.load(item.id).state == "bereit"


def test_create_folder_with_finder_names(svc):
    (svc.layout.archive / "Verein e.V.").mkdir()
    with pytest.raises(Conflict, match="existiert bereits"):
        svc.ops.create_folder("verein e.v", user=None)
    assert svc.ops.create_folder("Verein e.V./Beiträge  2026", user=None) == "Verein e.V./Beiträge 2026"
    assert svc.ops.create_folder("Neuer Verein e.V.", user=None) == "Neuer Verein e.V"


# ---------------------------------------------------------------------------
# Extended attributes survive metadata edits (data-5)


class FakeXattrs:
    """In-memory extended attributes keyed by inode, standing in for os.*xattr."""

    def __init__(self) -> None:
        self.store: dict[tuple[int, int], dict[str, bytes]] = {}

    @staticmethod
    def _key(path) -> tuple[int, int]:
        st = os.stat(path, follow_symlinks=False)
        return st.st_dev, st.st_ino

    def listxattr(self, path, *, follow_symlinks=True):
        return list(self.store.get(self._key(path), {}))

    def getxattr(self, path, name, *, follow_symlinks=True):
        try:
            return self.store[self._key(path)][name]
        except KeyError:
            raise OSError(getattr(__import__("errno"), "ENODATA", 61), "No data available") from None

    def setxattr(self, path, name, value, flags=0, *, follow_symlinks=True):
        if name.startswith("security."):
            raise PermissionError(1, "Operation not permitted")
        self.store.setdefault(self._key(path), {})[name] = bytes(value)


@pytest.fixture
def xattrs(monkeypatch) -> FakeXattrs:
    fake = FakeXattrs()
    monkeypatch.setattr(os, "listxattr", fake.listxattr, raising=False)
    monkeypatch.setattr(os, "getxattr", fake.getxattr, raising=False)
    monkeypatch.setattr(os, "setxattr", fake.setxattr, raising=False)
    return fake


def test_replace_if_unchanged_keeps_extended_attributes(xattrs, tmp_path):
    original = tmp_path / "Rechnung.pdf"
    original.write_bytes(b"alt")
    os.chmod(original, 0o640)
    os.utime(original, ns=(1_000_000_000, 1_000_000_000))
    xattrs.setxattr(original, "user.DosStream.AFP_AfpInfo:$DATA", b"finder-tag")
    xattrs.store[xattrs._key(original)]["security.selinux"] = b"label"
    replacement = tmp_path / ".docushelf-x.part"
    replacement.write_bytes(b"neu")

    fsops.replace_if_unchanged(original, replacement, fsops.file_signature(original))

    assert original.read_bytes() == b"neu"
    assert xattrs.getxattr(original, "user.DosStream.AFP_AfpInfo:$DATA") == b"finder-tag"
    assert os.stat(original).st_mode & 0o777 == 0o640
    assert os.stat(original).st_mtime_ns != 1_000_000_000  # times are not copied


def test_metadata_edit_keeps_finder_tags(svc, samples, tmp_path, xattrs):
    record = svc.ops.file_item(make_ready_item(svc, samples, tmp_path).id, _request(), user=None)
    path = svc.layout.archive / record.path
    inode = os.stat(path).st_ino
    xattrs.setxattr(path, "user.DOSATTRIB", b"\x00\x00dos")
    updated = svc.ops.update_document(record.id, MetadataUpdate(tags=["Telefon"]), user=None)
    path = svc.layout.archive / updated.path
    assert os.stat(path).st_ino != inode  # the file was rewritten ...
    assert xattrs.getxattr(path, "user.DOSATTRIB") == b"\x00\x00dos"  # ... and kept its attributes


def test_copies_across_file_systems_keep_extended_attributes(xattrs, tmp_path):
    src = tmp_path / "a.pdf"
    src.write_bytes(b"inhalt")
    xattrs.setxattr(src, "user.comment", b"Kommentar")
    dst = tmp_path / "b.pdf"
    fsops.copy_file_fsync(src, dst)
    assert xattrs.getxattr(dst, "user.comment") == b"Kommentar"


def test_extended_attribute_errors_never_block_the_edit(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "listxattr", broken, raising=False)
    monkeypatch.setattr(os, "getxattr", broken, raising=False)
    monkeypatch.setattr(os, "setxattr", broken, raising=False)
    original = tmp_path / "a.pdf"
    original.write_bytes(b"alt")
    replacement = tmp_path / "b.part"
    replacement.write_bytes(b"neu")
    fsops.replace_if_unchanged(original, replacement, fsops.file_signature(original))
    assert original.read_bytes() == b"neu"


def _real_user_xattrs(tmp_path: Path) -> bool:
    if not hasattr(os, "setxattr"):
        return False
    probe = tmp_path / "probe"
    probe.write_bytes(b"x")
    try:
        os.setxattr(probe, "user.docushelf-probe", b"1")
    except OSError:
        return False
    return True


def test_real_extended_attributes_survive_a_tag_edit(svc, samples, tmp_path):
    if not _real_user_xattrs(tmp_path):
        pytest.skip("no user extended attributes on this platform or file system")
    record = svc.ops.file_item(make_ready_item(svc, samples, tmp_path).id, _request(), user=None)
    path = svc.layout.archive / record.path
    os.setxattr(path, "user.DosStream.com.apple.metadata:_kMDItemUserTags:$DATA", b"Rot")
    updated = svc.ops.update_document(record.id, MetadataUpdate(tags=["Telefon"]), user=None)
    path = svc.layout.archive / updated.path
    assert os.getxattr(path, "user.DosStream.com.apple.metadata:_kMDItemUserTags:$DATA") == b"Rot"


# ---------------------------------------------------------------------------
# Renamed folders take inbox suggestions along (contract C4)


class FakePipeline:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def queue_refresh(self, **kwargs) -> int:
        self.calls.append(kwargs)
        return 0


def _ready_with(svc, samples, tmp_path, folders: list[str], **kwargs):
    item = make_ready_item(svc, samples, tmp_path, **kwargs)
    item.suggestions = [{"folder": folder, "title": "T", "score": 0.5, "source": "rule"} for folder in folders]
    svc.inbox.save(item)
    return item


def test_rename_folder_retargets_inbox_suggestions(svc, samples, tmp_path):
    svc.pipeline = FakePipeline()
    (svc.layout.archive / "Finanzen" / "Rechnungen" / "Alza").mkdir(parents=True)
    svc.index.add_folder_with_parents("Finanzen/Rechnungen/Alza")
    first = _ready_with(svc, samples, tmp_path, ["Finanzen/Rechnungen/Alza", "Andere/Rechnungen"])
    second = _ready_with(svc, samples, tmp_path, ["finanzen/rechnungen"], source="paperless",
                         context={"import_suggestion": {"folder": "Finanzen/Rechnungen/Telekom", "title": "Rechnung",
                                                        "source": "import", "score": 0.95}})
    untouched = _ready_with(svc, samples, tmp_path, ["Finanzen/Rechnungen2/X", "Finanzen"])
    filing = _ready_with(svc, samples, tmp_path, ["Finanzen/Rechnungen/Alza"])
    filing.filing = {"state": "publishing", "sha256": "0" * 64, "folder": "Finanzen/Rechnungen/Alza", "filename": "x.pdf"}
    svc.inbox.save(filing)

    assert svc.ops.rename_folder("Finanzen/Rechnungen", "Belege", user=None) == "Finanzen/Belege"

    assert [s["folder"] for s in svc.inbox.load(first.id).suggestions] == ["Finanzen/Belege/Alza", "Andere/Rechnungen"]
    moved = svc.inbox.load(second.id)
    assert [s["folder"] for s in moved.suggestions] == ["Finanzen/Belege"]
    assert moved.context["import_suggestion"]["folder"] == "Finanzen/Belege/Telekom"
    assert [s["folder"] for s in svc.inbox.load(untouched.id).suggestions] == ["Finanzen/Rechnungen2/X", "Finanzen"]
    assert [s["folder"] for s in svc.inbox.load(filing.id).suggestions] == ["Finanzen/Rechnungen/Alza"]
    assert "umbenannt" in svc.inbox.read_log(svc.inbox.load(first.id))
    assert svc.pipeline.calls == [{"only_stale": True}]

    # Filing the top suggestion now goes into the renamed folder; the old one is not re-created.
    record = svc.ops.file_item(first.id, _request(folder=svc.inbox.load(first.id).suggestions[0]["folder"]), user=None)
    assert record.folder == "Finanzen/Belege/Alza"
    assert sorted(os.listdir(svc.layout.archive / "Finanzen")) == ["Belege"]


def test_rename_to_the_same_name_changes_nothing(svc, samples, tmp_path):
    svc.pipeline = FakePipeline()
    (svc.layout.archive / "Bank").mkdir()
    item = _ready_with(svc, samples, tmp_path, ["Bank"])
    assert svc.ops.rename_folder("Bank", "Bank", user=None) == "Bank"
    assert svc.pipeline.calls == [] and svc.inbox.load(item.id).suggestions[0]["folder"] == "Bank"


def test_rename_folder_with_the_real_pipeline(full, samples, tmp_path):
    (full.layout.archive / "Bank" / "Giro").mkdir(parents=True)
    full.reconciler.scan(full=True)
    item = _ready_with(full, samples, tmp_path, ["Bank/Giro"])

    assert full.ops.rename_folder("Bank", "Sparkasse", user=None) == "Sparkasse"

    assert full.index.folder_exists("Sparkasse/Giro")
    assert full.inbox.load(item.id).suggestions[0]["folder"] == "Sparkasse/Giro"
    # The suggestion is current again, so it needs no recomputation.
    assert item.id not in {job.key for job in full.index.pending_jobs("refresh")}


# ---------------------------------------------------------------------------
# Purging filed inbox items


@pytest.mark.parametrize("state", ["publishing", "published", "unbekannt", None])
def test_purge_never_removes_items_with_an_open_filing(svc, samples, tmp_path, state):
    item = make_ready_item(svc, samples, tmp_path)
    svc.ops.file_item(item.id, _request(), user=None)
    done = svc.inbox.load(item.id)
    done.filing["finished_at"] = (now() - timedelta(days=90)).isoformat(timespec="seconds")
    done.filing["state"] = state
    svc.inbox.save(done)
    assert svc.trash.purge_done_items() == 0
    assert svc.inbox.find(item.id) is not None

    done.filing["state"] = "done"
    svc.inbox.save(done)
    assert svc.trash.purge_done_items() == 1
