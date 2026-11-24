"""Reconciliation with real files: moves, renames, copies, deletions, foreign
files, in-place changes, empty folders, NFD names, ignored files, files that are
still being written, rebuilds, idempotency and transient errors."""

from __future__ import annotations

import itertools
import os
import shutil
import threading
import time
import unicodedata
from datetime import date
from pathlib import Path

import pytest

from docushelf.errors import DocuShelfError, NotFound, PathError
from docushelf.index import reconcile
from docushelf.index.db import Index
from docushelf.index.reconcile import Reconciler, derive_title_date
from docushelf.models import DocMeta
from docushelf.pdf.xmp import write_meta
from docushelf.util.ids import is_provisional, provisional_id

UUID_A = "1b4e28ba-2fa1-11d2-883f-0016d3cca427"
UUID_B = "6f1c2d9e-8a40-4c1b-9d3e-2a7b5c4d1e0f"
UUID_C = "0c7d3f52-41aa-4b8e-9f10-3e5d6c7b8a91"


@pytest.fixture
def svc(base_services):
    base_services.reconciler = Reconciler(base_services)
    return base_services


@pytest.fixture
def archive(svc) -> Path:
    return svc.layout.archive


@pytest.fixture
def scratch(tmp_path) -> Path:
    path = tmp_path / "scratch"
    path.mkdir()
    return path


@pytest.fixture
def put(archive, scratch, samples):
    """Create a PDF in the archive: ``put("Ordner/2025-01-01 Titel.pdf", "Text", meta=DocMeta(...))``."""
    counter = itertools.count()

    def _put(rel: str, pages: str | list[str], *, meta: DocMeta | None = None) -> Path:
        target = archive / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        pages = [pages] if isinstance(pages, str) else list(pages)
        if meta is None:
            return samples.text_pdf(target, pages)
        raw = samples.text_pdf(scratch / f"raw-{next(counter)}.pdf", pages)
        write_meta(raw, target, meta)
        return target

    return _put


@pytest.fixture
def replace_content(scratch, samples):
    """Overwrite a file in place (same inode) with a different PDF and a newer mtime."""
    counter = itertools.count()

    def _replace(path: Path, pages: list[str], *, meta: DocMeta | None = None) -> None:
        raw = samples.text_pdf(scratch / f"replacement-{next(counter)}.pdf", pages)
        if meta is not None:
            with_meta = scratch / f"replacement-meta-{next(counter)}.pdf"
            write_meta(raw, with_meta, meta)
            raw = with_meta
        before = os.stat(path)
        with open(path, "r+b") as handle:
            handle.truncate(0)
            handle.write(raw.read_bytes())
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))

    return _replace


def records(svc) -> dict[str, tuple]:
    """path -> (id, title, content hash, text status) of every indexed document."""
    return {
        r.path: (r.id, r.title, r.content_hash, r.text_status)
        for r in svc.index.list_in_folder("", recursive=True, limit=10_000)
    }


def changes(stats) -> tuple[int, int, int, int]:
    return stats.added, stats.updated, stats.moved, stats.removed


# -- title and date ----------------------------------------------------------------------


def test_derive_title_date_rules():
    with_id = DocMeta(doc_id=UUID_A, title="Titel aus XMP", doc_date=date(2020, 1, 2))
    # The schema in the file name wins, collision suffixes are dropped.
    assert derive_title_date("2025-03-01 Stromrechnung (2).pdf", with_id) == ("Stromrechnung", date(2025, 3, 1))
    # Without the schema: XMP title for files with a document id, date from XMP.
    assert derive_title_date("Scan 0001.pdf", with_id) == ("Titel aus XMP", date(2020, 1, 2))
    # Titles of other producers are not trusted.
    assert derive_title_date("Brief (3).pdf", DocMeta(title="Microsoft Word - Dokument1")) == ("Brief", None)
    # A date without a title in the name.
    assert derive_title_date("2025-03-01.pdf", with_id) == ("Titel aus XMP", date(2025, 3, 1))
    assert derive_title_date("2025-03-01.pdf", DocMeta()) == ("2025-03-01", date(2025, 3, 1))


# -- new files ------------------------------------------------------------------------------


def test_new_files_and_folders_are_indexed(svc, put, archive):
    put(
        "Wohnung/Nebenkosten/2025-03-01 Nebenkostenabrechnung.pdf",
        ["Abrechnung 2024", "Seite zwei"],
        meta=DocMeta(doc_id=UUID_A, title="NK", tags=["Wohnung", "steuerrelevant 2025"], sender="Hausverwaltung"),
    )
    put("Fremd.pdf", "Ein fremdes Dokument ohne Metadaten")
    (archive / "Leer" / "Unterordner").mkdir(parents=True)

    stats = svc.reconciler.scan()

    assert changes(stats) == (2, 0, 0, 0)
    assert stats.files_seen == 2 and stats.folders_seen == 4 and stats.errors == 0
    record = svc.index.get(UUID_A)
    assert record.path == "Wohnung/Nebenkosten/2025-03-01 Nebenkostenabrechnung.pdf"
    assert record.folder == "Wohnung/Nebenkosten" and record.filename == "2025-03-01 Nebenkostenabrechnung.pdf"
    assert record.title == "Nebenkostenabrechnung" and record.doc_date == date(2025, 3, 1)
    assert record.tags == ["Wohnung", "steuerrelevant 2025"] and record.sender == "Hausverwaltung"
    assert record.has_xmp and record.xmp_id == UUID_A
    assert record.text_status == "ok" and record.page_count == 2 and record.ext == ".pdf"
    assert svc.index.get_pages(UUID_A) == ["Abrechnung 2024", "Seite zwei"]

    foreign = svc.index.get_by_path("Fremd.pdf")
    assert foreign.id == provisional_id(foreign.content_hash) and is_provisional(foreign.id)
    assert foreign.title == "Fremd" and foreign.doc_date is None and not foreign.has_xmp
    assert foreign.origin_hash == foreign.content_hash  # duplicate detection for files dropped in later
    assert svc.index.list_folders() == ["Leer", "Leer/Unterordner", "Wohnung", "Wohnung/Nebenkosten"]
    assert svc.index.get_meta("last_scan") and svc.index.get_meta("last_full_scan")


def test_quick_scan_does_not_count_as_full_scan(svc, put):
    put("A/2025-01-01 Eins.pdf", "eins")
    stats = svc.reconciler.scan(full=False)
    assert stats.added == 1 and not stats.full
    assert svc.index.get_meta("last_scan") and svc.index.get_meta("last_full_scan") is None


def test_non_pdf_and_damaged_files_are_indexed_by_name(svc, archive):
    (archive / "Fotos").mkdir()
    (archive / "Fotos" / "2024-08-01 Urlaub.JPG").write_bytes(b"\xff\xd8\xff\xe0 not really a jpeg")
    (archive / "Fotos" / "2024-08-02 Kaputt.pdf").write_bytes(b"%PDF-1.4 garbage without a trailer")

    stats = svc.reconciler.scan()

    assert stats.added == 2 and stats.errors == 0
    photo = svc.index.get_by_path("Fotos/2024-08-01 Urlaub.JPG")
    assert photo.ext == ".jpg" and photo.text_status == "unsupported" and photo.title == "Urlaub"
    broken = svc.index.get_by_path("Fotos/2024-08-02 Kaputt.pdf")
    assert broken.text_status == "error" and broken.page_count == 0 and broken.title == "Kaputt"
    # verify retries failed extractions but does not change anything else
    again = svc.reconciler.scan(verify=True)
    assert changes(again) == (0, 0, 0, 0)
    assert svc.index.get_by_path("Fotos/2024-08-02 Kaputt.pdf").text_status == "error"


# -- moves and renames ------------------------------------------------------------------------------


def test_rename_keeps_id_text_and_tags(svc, put, monkeypatch):
    path = put("Bank/2025-01-31 Kontoauszug.pdf", "Kontoauszug Januar", meta=DocMeta(doc_id=UUID_A, tags=["Bank"]))
    svc.reconciler.scan()
    os.rename(path, path.with_name("2025-02-01 Auszug Februar.pdf"))

    def no_extraction(*args, **kwargs):
        raise AssertionError("a move must not extract the text again")

    monkeypatch.setattr(reconcile, "extract_pages", no_extraction)
    stats = svc.reconciler.scan()

    assert changes(stats) == (0, 0, 1, 0)
    record = svc.index.get(UUID_A)
    assert record.path == "Bank/2025-02-01 Auszug Februar.pdf"
    assert record.title == "Auszug Februar" and record.doc_date == date(2025, 2, 1)
    assert record.tags == ["Bank"]
    assert svc.index.get_pages(UUID_A) == ["Kontoauszug Januar"]
    assert svc.index.count() == 1
    moves = [e for e in svc.audit.tail(10) if e["action"] == "external_move"]
    assert moves and moves[0]["from"] == "Bank/2025-01-31 Kontoauszug.pdf" and moves[0]["to"] == record.path


def test_move_to_another_folder(svc, put, archive):
    path = put("Eingang/2025-01-31 Rechnung.pdf", "Rechnung Strom", meta=DocMeta(doc_id=UUID_A))
    svc.reconciler.scan()
    target = archive / "Wohnung" / "Strom" / path.name
    target.parent.mkdir(parents=True)
    os.rename(path, target)

    stats = svc.reconciler.scan()

    assert changes(stats) == (0, 0, 1, 0)
    record = svc.index.get(UUID_A)
    assert record.folder == "Wohnung/Strom" and record.path == "Wohnung/Strom/2025-01-31 Rechnung.pdf"
    assert svc.index.get_pages(UUID_A) == ["Rechnung Strom"]
    assert set(svc.index.list_folders()) == {"Eingang", "Wohnung", "Wohnung/Strom"}


def test_move_and_rename_at_once(svc, put, archive):
    path = put("A/2025-01-31 Alt.pdf", "Inhalt", meta=DocMeta(doc_id=UUID_A))
    other = put("A/Fremd.pdf", "Fremder Inhalt")
    svc.reconciler.scan()
    foreign_id = svc.index.get_by_path("A/Fremd.pdf").id
    (archive / "B" / "C").mkdir(parents=True)
    os.rename(path, archive / "B" / "C" / "2024-12-01 Neu.pdf")
    os.rename(other, archive / "B" / "Umbenannt.pdf")

    stats = svc.reconciler.scan()

    assert changes(stats) == (0, 0, 2, 0)
    assert svc.index.get(UUID_A).path == "B/C/2024-12-01 Neu.pdf"
    assert svc.index.get(UUID_A).title == "Neu"
    # foreign files are recognized by their content hash
    assert svc.index.get(foreign_id).path == "B/Umbenannt.pdf"
    assert svc.index.get(foreign_id).title == "Umbenannt"


def test_moving_a_whole_folder(svc, put, archive):
    for i in range(3):
        put(f"Alt/Unter/2025-01-0{i + 1} Dokument {i}.pdf", f"Text {i}")
    svc.reconciler.scan()
    ids = {r.filename: r.id for r in svc.index.list_in_folder("", recursive=True)}
    os.rename(archive / "Alt", archive / "Neu")

    stats = svc.reconciler.scan()

    assert changes(stats) == (0, 0, 3, 0)
    assert {r.filename: r.id for r in svc.index.list_in_folder("Neu/Unter")} == ids
    assert svc.index.list_folders() == ["Neu", "Neu/Unter"]


def test_case_only_rename_is_a_move(svc, put):
    path = put("A/2025-01-01 rechnung telekom.pdf", "Telekom", meta=DocMeta(doc_id=UUID_A))
    svc.reconciler.scan()
    os.rename(path, path.with_name("2025-01-01 Rechnung Telekom.pdf"))

    stats = svc.reconciler.scan()

    assert changes(stats) == (0, 0, 1, 0)
    assert svc.index.get(UUID_A).path == "A/2025-01-01 Rechnung Telekom.pdf"
    assert svc.index.count() == 1


def test_document_moved_onto_an_existing_file_replaces_it(svc, put):
    old = put("A/2025-01-01 Alt.pdf", "alter Inhalt", meta=DocMeta(doc_id=UUID_A))
    new = put("B/2025-01-02 Neu.pdf", "neuer Inhalt", meta=DocMeta(doc_id=UUID_B))
    svc.reconciler.scan()
    os.replace(new, old)

    stats = svc.reconciler.scan()

    assert stats.moved == 1 and stats.removed == 1
    assert svc.index.get(UUID_A) is None
    record = svc.index.get(UUID_B)
    assert record.path == "A/2025-01-01 Alt.pdf" and svc.index.get_pages(UUID_B) == ["neuer Inhalt"]
    assert svc.index.count() == 1


# -- deletions and copies --------------------------------------------------------------------------


def test_delete_removes_document(svc, put):
    put("A/2025-01-01 Bleibt.pdf", "bleibt")
    gone = put("A/2025-01-02 Weg.pdf", "weg", meta=DocMeta(doc_id=UUID_B))
    svc.reconciler.scan()
    gone.unlink()

    stats = svc.reconciler.scan()

    assert changes(stats) == (0, 0, 0, 1)
    assert svc.index.get(UUID_B) is None and svc.index.count() == 1
    deletions = [e for e in svc.audit.tail(10) if e["action"] == "external_delete"]
    assert deletions and deletions[0]["doc_id"] == UUID_B


def test_copy_with_the_same_xmp_id_gets_its_own_id(svc, put, archive):
    original = put("A/2025-01-01 Vertrag.pdf", "Vertrag", meta=DocMeta(doc_id=UUID_A))
    svc.reconciler.scan()
    copy = archive / "B" / "2025-01-01 Vertrag.pdf"
    copy.parent.mkdir()
    shutil.copy2(original, copy)

    stats = svc.reconciler.scan()

    assert changes(stats) == (1, 0, 0, 0)
    assert svc.index.get(UUID_A).path == "A/2025-01-01 Vertrag.pdf"
    duplicate = svc.index.get_by_path("B/2025-01-01 Vertrag.pdf")
    assert duplicate.id != UUID_A and is_provisional(duplicate.id) and duplicate.xmp_id == UUID_A
    assert svc.index.get_pages(duplicate.id) == ["Vertrag"]

    # Once the original is gone, the copy carries the id from its XMP again.
    original.unlink()
    stats = svc.reconciler.scan()
    assert stats.removed == 1
    assert svc.index.get(UUID_A).path == "B/2025-01-01 Vertrag.pdf"
    assert svc.index.count() == 1


def test_copy_and_delete_in_one_pass_is_a_move(svc, put, archive):
    original = put("A/2025-01-01 Vertrag.pdf", "Vertrag", meta=DocMeta(doc_id=UUID_A))
    svc.reconciler.scan()
    (archive / "B").mkdir()
    shutil.copy2(original, archive / "B" / original.name)
    original.unlink()

    stats = svc.reconciler.scan()

    assert changes(stats) == (0, 0, 1, 0)
    assert svc.index.get(UUID_A).path == "B/2025-01-01 Vertrag.pdf"


def test_identical_foreign_files_get_distinct_stable_ids(svc, put, archive):
    first = put("A/Scan.pdf", "gleicher Inhalt")
    (archive / "B").mkdir()
    shutil.copy2(first, archive / "B" / "Scan.pdf")
    svc.reconciler.scan()
    a, b = svc.index.get_by_path("A/Scan.pdf"), svc.index.get_by_path("B/Scan.pdf")
    base = provisional_id(a.content_hash)
    assert {a.id, b.id} == {base, base + "-2"}

    svc.index.reset()
    svc.reconciler.scan()
    assert svc.index.get_by_path("A/Scan.pdf").id == a.id
    assert svc.index.get_by_path("B/Scan.pdf").id == b.id


# -- foreign files ------------------------------------------------------------------------------------


def test_foreign_file_without_xmp(svc, put, archive):
    put("Scans/Scan 0001.pdf", "Ein fremder Scan")
    put("Scans/2024-12-24 Geschenk (2).pdf", "Quittung")
    svc.reconciler.scan()

    scan = svc.index.get_by_path("Scans/Scan 0001.pdf")
    assert scan.id == provisional_id(scan.content_hash)
    assert scan.title == "Scan 0001" and scan.doc_date is None and scan.xmp_id is None and scan.tags == []
    gift = svc.index.get_by_path("Scans/2024-12-24 Geschenk (2).pdf")
    assert gift.title == "Geschenk" and gift.doc_date == date(2024, 12, 24)

    os.rename(archive / "Scans" / "Scan 0001.pdf", archive / "Scans" / "2023-05-05 Mietvertrag.pdf")
    stats = svc.reconciler.scan()
    assert changes(stats) == (0, 0, 1, 0)
    renamed = svc.index.get(scan.id)
    assert renamed.path == "Scans/2023-05-05 Mietvertrag.pdf" and renamed.title == "Mietvertrag"


# -- changes in place -------------------------------------------------------------------------------------


def test_in_place_modification_keeps_id_and_replaces_text(svc, put, replace_content):
    path = put("A/2025-01-01 Brief.pdf", "alter Text", meta=DocMeta(doc_id=UUID_A, tags=["alt"]))
    svc.reconciler.scan()
    inode = os.stat(path).st_ino
    replace_content(path, ["neuer Text", "zweite Seite"], meta=DocMeta(doc_id=UUID_A, tags=["neu"]))
    assert os.stat(path).st_ino == inode

    stats = svc.reconciler.scan()

    assert changes(stats) == (0, 1, 0, 0)
    record = svc.index.get(UUID_A)
    assert record.tags == ["neu"] and record.page_count == 2 and record.text_status == "ok"
    assert svc.index.get_pages(UUID_A) == ["neuer Text", "zweite Seite"]


def test_in_place_modification_of_foreign_file_keeps_its_id(svc, put, replace_content):
    path = put("A/Notiz.pdf", "Version eins")
    svc.reconciler.scan()
    doc_id = svc.index.get_by_path("A/Notiz.pdf").id
    replace_content(path, ["Version zwei"])

    stats = svc.reconciler.scan()

    assert changes(stats) == (0, 1, 0, 0)
    assert svc.index.get(doc_id).path == "A/Notiz.pdf"
    assert svc.index.get_pages(doc_id) == ["Version zwei"]


def test_changed_xmp_id_changes_the_document_id(svc, put, replace_content):
    path = put("A/2025-01-01 Brief.pdf", "eins", meta=DocMeta(doc_id=UUID_A))
    svc.reconciler.scan()
    replace_content(path, ["zwei"], meta=DocMeta(doc_id=UUID_C))

    stats = svc.reconciler.scan()

    assert stats.updated == 1
    assert svc.index.get(UUID_A) is None
    assert svc.index.get(UUID_C).path == "A/2025-01-01 Brief.pdf"
    assert svc.index.get_pages(UUID_C) == ["zwei"]


def test_touched_file_is_not_reextracted(svc, put, monkeypatch):
    path = put("A/2025-01-01 Brief.pdf", "Inhalt", meta=DocMeta(doc_id=UUID_A))
    svc.reconciler.scan()
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    monkeypatch.setattr(reconcile, "extract_pages", lambda *a, **k: pytest.fail("text extracted again"))

    stats = svc.reconciler.scan()

    assert changes(stats) == (0, 0, 0, 0) and stats.unchanged == 1
    assert svc.index.get(UUID_A).mtime_ns == st.st_mtime_ns + 5_000_000_000


def test_verify_finds_changes_that_keep_size_and_mtime(svc, archive):
    path = archive / "Notizen" / "liste.txt"
    path.parent.mkdir()
    path.write_bytes(b"Milch, Brot")
    svc.reconciler.scan()
    record = svc.index.get_by_path("Notizen/liste.txt")
    st = os.stat(path)
    with open(path, "r+b") as handle:
        handle.write(b"Tee, ")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))

    assert changes(svc.reconciler.scan()) == (0, 0, 0, 0)
    stats = svc.reconciler.scan(verify=True)

    assert stats.updated == 1
    updated = svc.index.get(record.id)
    assert updated.content_hash != record.content_hash and updated.text_status == "unsupported"


# -- folders, names, ignored files -----------------------------------------------------------------------------


def test_empty_folders_are_indexed_and_removed(svc, archive):
    (archive / "Versicherungen" / "Haftpflicht").mkdir(parents=True)
    (archive / "Versicherungen" / "Hausrat").mkdir()
    svc.reconciler.scan()
    assert svc.index.list_folders() == ["Versicherungen", "Versicherungen/Haftpflicht", "Versicherungen/Hausrat"]

    (archive / "Versicherungen" / "Haftpflicht").rmdir()
    os.rename(archive / "Versicherungen" / "Hausrat", archive / "Versicherungen" / "Hausratversicherung")
    svc.reconciler.scan()

    assert svc.index.list_folders() == ["Versicherungen", "Versicherungen/Hausratversicherung"]


def test_ensure_folder_survives_until_the_folder_is_gone(svc, archive):
    (archive / "Neu" / "Unter").mkdir(parents=True)
    svc.reconciler.ensure_folder("Neu/Unter")
    assert svc.index.list_folders() == ["Neu", "Neu/Unter"]
    with pytest.raises(PathError):
        svc.reconciler.ensure_folder("../draußen")
    svc.reconciler.scan()
    assert svc.index.list_folders() == ["Neu", "Neu/Unter"]


def test_nfd_file_names(svc, put, archive):
    folder = unicodedata.normalize("NFD", "Gebühren")
    name = unicodedata.normalize("NFD", "2025-05-05 Müller Straßenreinigung.pdf")
    put(f"{folder}/{name}", "Gebührenbescheid für Familie Müller")
    on_disk = os.listdir(archive / folder)[0]

    stats = svc.reconciler.scan()

    assert stats.added == 1
    record = svc.index.list_in_folder("", recursive=True)[0]
    assert record.path == f"{folder}/{on_disk}"  # stored exactly as on disk
    assert record.title == unicodedata.normalize("NFC", "Müller Straßenreinigung")
    assert svc.index.list_folders() == [folder]
    # The next pass must not mistake the name for a different one.
    again = svc.reconciler.scan()
    assert changes(again) == (0, 0, 0, 0) and again.unchanged == 1

    from docushelf.index.search import Searcher

    hits = Searcher(svc).search("müller straßenreinigung").hits
    assert [h.doc.id for h in hits] == [record.id]


def test_ignored_hidden_and_temporary_files(svc, put, archive):
    put("A/2025-01-01 Echt.pdf", "echt")
    for name in (".DS_Store", "._2025-01-01 Echt.pdf", "~$Brief.docx", "Thumbs.db", "Download.pdf.part", "x.tmp"):
        (archive / "A" / name).write_bytes(b"junk")
    for folder in ("@eaDir", ".Trashes", "#recycle", ".versteckt"):
        put(f"{folder}/2025-01-01 Echt.pdf", "versteckt")
    stale = archive / "A" / ".docushelf-0123abcd.part"
    stale.write_bytes(b"left over")
    two_hours_ago = time.time() - 7200
    os.utime(stale, (two_hours_ago, two_hours_ago))
    fresh = archive / "A" / ".docushelf-4567abcd.part"
    fresh.write_bytes(b"being copied")

    stats = svc.reconciler.scan()

    assert stats.files_seen == 1 and svc.index.count() == 1
    assert svc.index.list_folders() == ["A"]
    assert not stale.exists() and fresh.exists()


def test_symlinks_are_not_followed(svc, put, archive, tmp_path, samples):
    outside = tmp_path / "draussen"
    samples.text_pdf(outside / "geheim.pdf", ["geheim"])
    put("A/2025-01-01 Echt.pdf", "echt")
    (archive / "Link.pdf").symlink_to(outside / "geheim.pdf")
    (archive / "LinkOrdner").symlink_to(outside, target_is_directory=True)

    svc.reconciler.scan()

    assert list(records(svc)) == ["A/2025-01-01 Echt.pdf"]
    assert svc.index.list_folders() == ["A"]


# -- files in flux and transient errors ------------------------------------------------------------------


def test_file_growing_during_scan_is_skipped(svc, put, monkeypatch):
    path = put("A/2025-01-01 Gross.pdf", "Inhalt")
    real_hash = reconcile.sha256_file

    def growing(target, *args, **kwargs):
        if Path(target) == path:
            with open(target, "ab") as handle:
                handle.write(b"\n% still being written\n")
        return real_hash(target, *args, **kwargs)

    monkeypatch.setattr(reconcile, "sha256_file", growing)
    stats = svc.reconciler.scan()
    assert stats.added == 0 and svc.index.count() == 0

    monkeypatch.setattr(reconcile, "sha256_file", real_hash)
    stats = svc.reconciler.scan()
    assert stats.added == 1 and svc.index.get_by_path("A/2025-01-01 Gross.pdf").text_status == "ok"


def test_known_file_changing_during_scan_keeps_its_record(svc, put, replace_content, monkeypatch):
    path = put("A/2025-01-01 Brief.pdf", "alter Text", meta=DocMeta(doc_id=UUID_A))
    svc.reconciler.scan()
    replace_content(path, ["neuer Text"], meta=DocMeta(doc_id=UUID_A))
    real_hash = reconcile.sha256_file

    def still_writing(target, *args, **kwargs):
        with open(target, "ab") as handle:
            handle.write(b"%")
        return real_hash(target, *args, **kwargs)

    monkeypatch.setattr(reconcile, "sha256_file", still_writing)
    stats = svc.reconciler.scan()
    assert changes(stats) == (0, 0, 0, 0)
    assert svc.index.get_pages(UUID_A) == ["alter Text"]

    monkeypatch.setattr(reconcile, "sha256_file", real_hash)
    assert svc.reconciler.scan().updated == 1
    assert svc.index.get_pages(UUID_A) == ["neuer Text"]


def test_file_vanishing_mid_scan_is_kept_until_the_next_pass(svc, put, replace_content, monkeypatch):
    path = put("A/2025-01-01 Weg.pdf", "weg", meta=DocMeta(doc_id=UUID_A))
    put("A/2025-01-02 Bleibt.pdf", "bleibt")
    svc.reconciler.scan()
    replace_content(path, ["geändert"], meta=DocMeta(doc_id=UUID_A))
    real_hash = reconcile.sha256_file

    def vanish(target, *args, **kwargs):
        if Path(target) == path:
            path.unlink()
        return real_hash(target, *args, **kwargs)

    monkeypatch.setattr(reconcile, "sha256_file", vanish)
    stats = svc.reconciler.scan()
    assert stats.errors == 1 and stats.removed == 0
    assert svc.index.get(UUID_A) is not None

    monkeypatch.setattr(reconcile, "sha256_file", real_hash)
    stats = svc.reconciler.scan()
    assert stats.removed == 1 and svc.index.get(UUID_A) is None


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file permissions")
def test_unreadable_folder_keeps_its_documents(svc, put, archive):
    put("Privat/2025-01-01 Geheim.pdf", "geheim", meta=DocMeta(doc_id=UUID_A))
    (archive / "Privat" / "Unter").mkdir()
    put("Offen/2025-01-01 Offen.pdf", "offen")
    svc.reconciler.scan()
    os.chmod(archive / "Privat", 0)
    try:
        stats = svc.reconciler.scan()
        assert stats.errors >= 1 and stats.removed == 0
        assert svc.index.get(UUID_A) is not None
        assert "Privat/Unter" in svc.index.list_folders()
    finally:
        os.chmod(archive / "Privat", 0o755)
    stats = svc.reconciler.scan()
    assert stats.errors == 0 and changes(stats) == (0, 0, 0, 0)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file permissions")
def test_unreadable_file_is_not_dropped(svc, put, replace_content):
    path = put("A/2025-01-01 Gesperrt.pdf", "vorher", meta=DocMeta(doc_id=UUID_A))
    svc.reconciler.scan()
    replace_content(path, ["nachher"], meta=DocMeta(doc_id=UUID_A))
    os.chmod(path, 0)
    try:
        stats = svc.reconciler.scan()
        assert stats.errors == 1 and changes(stats) == (0, 0, 0, 0)
        assert svc.index.get_pages(UUID_A) == ["vorher"]
    finally:
        os.chmod(path, 0o644)
    assert svc.reconciler.scan().updated == 1
    assert svc.index.get_pages(UUID_A) == ["nachher"]


def test_empty_archive_root_is_treated_as_a_missing_mount(svc, put, archive):
    put("A/2025-01-01 Eins.pdf", "eins", meta=DocMeta(doc_id=UUID_A))
    svc.reconciler.scan()
    shutil.rmtree(archive / "A")

    stats = svc.reconciler.scan()

    assert stats.errors == 1 and stats.removed == 0
    assert svc.index.get(UUID_A) is not None and svc.index.list_folders() == ["A"]


def test_missing_archive_root_raises(svc, archive):
    shutil.rmtree(archive)
    with pytest.raises(DocuShelfError):
        svc.reconciler.scan()


def test_interrupted_text_extraction_is_resumed(svc, put, monkeypatch):
    put("A/2025-01-01 Eins.pdf", "eins", meta=DocMeta(doc_id=UUID_A))
    monkeypatch.setattr(Reconciler, "_extract_jobs", lambda self, jobs, stats, cancel: None)
    svc.reconciler.scan()
    assert svc.index.get(UUID_A).text_status == "pending"

    monkeypatch.undo()
    stats = svc.reconciler.scan()

    assert changes(stats) == (0, 0, 0, 0)
    assert svc.index.get(UUID_A).text_status == "ok" and svc.index.get_pages(UUID_A) == ["eins"]


def test_cancelled_scan_changes_nothing(svc, put):
    put("A/2025-01-01 Eins.pdf", "eins")
    cancel = threading.Event()
    cancel.set()
    svc.reconciler.scan(cancel=cancel)
    assert svc.index.count() == 0 and svc.index.get_meta("last_scan") is None


def test_application_changes_between_walk_and_apply(svc, put, archive, monkeypatch):
    """The application moves and files documents while a pass is reading files."""
    letter = put("A/2025-01-01 Brief.pdf", "Brief", meta=DocMeta(doc_id=UUID_A))
    svc.reconciler.scan()
    fresh = put("A/2025-01-02 Neu.pdf", "Neu", meta=DocMeta(doc_id=UUID_B))
    original = Reconciler._read_changed

    def racing(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        target = archive / "B"
        target.mkdir(exist_ok=True)
        with svc.archive_lock:
            os.rename(letter, target / letter.name)
            self.index_file(target / letter.name)
            os.rename(fresh, target / fresh.name)
            self.index_file(target / fresh.name)
        return result

    monkeypatch.setattr(Reconciler, "_read_changed", racing)
    stats = svc.reconciler.scan()
    monkeypatch.undo()

    assert stats.removed == 0 and svc.index.count() == 2
    assert svc.index.get(UUID_A).path == "B/2025-01-01 Brief.pdf"
    assert svc.index.get(UUID_B).path == "B/2025-01-02 Neu.pdf"
    assert set(svc.index.list_folders()) == {"A", "B"}
    assert changes(svc.reconciler.scan()) == (0, 0, 0, 0)


# -- rebuild and idempotency -------------------------------------------------------------------------------


def test_scan_after_the_index_was_deleted_gives_the_same_ids(svc, put, layout):
    put("A/2025-01-01 Eins.pdf", "eins", meta=DocMeta(doc_id=UUID_A, tags=["t"]))
    put("B/2025-01-02 Zwei.pdf", "zwei", meta=DocMeta(doc_id=UUID_B))
    put("C/Fremd.pdf", "fremd")
    svc.reconciler.scan()
    before = records(svc)

    svc.index.close()
    for path in layout.index.glob("docushelf.sqlite3*"):
        path.unlink()
    original_index = svc.index
    svc.index = Index(layout.db_path)
    try:
        svc.index.init_schema()
        stats = svc.reconciler.scan()
        assert stats.added == 3
        assert records(svc) == before
        assert svc.index.get(UUID_A).tags == ["t"]
        assert svc.index.get_pages(UUID_A) == ["eins"]
    finally:
        svc.index.close()
        svc.index = original_index


def test_rebuild_with_reset_index_is_idempotent(svc, put):
    put("A/2025-01-01 Eins.pdf", "eins", meta=DocMeta(doc_id=UUID_A))
    put("A/Fremd.pdf", "fremd")
    svc.reconciler.scan()
    before = records(svc)
    for _ in range(2):
        with svc.archive_lock:
            svc.index.reset()
            stats = svc.reconciler.scan(full=True)
        assert stats.added == 2
        assert records(svc) == before


def test_repeated_scans_are_idempotent(svc, put, archive):
    original = put("A/2025-01-01 Vertrag.pdf", "Vertrag", meta=DocMeta(doc_id=UUID_A))
    (archive / "B").mkdir()
    shutil.copy2(original, archive / "B" / original.name)
    put("C/Fremd.pdf", "fremd")
    (archive / "C" / "Bild.png").write_bytes(b"\x89PNG not really")
    (archive / "Leer").mkdir()
    first = svc.reconciler.scan()
    assert first.added == 4
    state = (records(svc), svc.index.list_folders())

    for _ in range(2):
        again = svc.reconciler.scan()
        assert changes(again) == (0, 0, 0, 0)
        assert again.unchanged == first.files_seen and again.errors == 0
        assert (records(svc), svc.index.list_folders()) == state
    verified = svc.reconciler.scan(verify=True)
    assert changes(verified) == (0, 0, 0, 0) and verified.unchanged == first.files_seen
    assert (records(svc), svc.index.list_folders()) == state


# -- index_file and forget ------------------------------------------------------------------------------------


def test_index_file_indexes_and_keeps_identity(svc, put):
    path = put("A/2025-01-01 Neu.pdf", "neu", meta=DocMeta(doc_id=UUID_A, tags=["x"]))
    record = svc.reconciler.index_file(path)
    assert record.id == UUID_A and record.text_status == "ok" and record.tags == ["x"]
    assert svc.index.get_pages(UUID_A) == ["neu"] and svc.index.list_folders() == ["A"]
    assert svc.reconciler.index_file(path).id == UUID_A
    stats = svc.reconciler.scan()
    assert changes(stats) == (0, 0, 0, 0) and stats.unchanged == 1


def test_index_file_after_the_first_metadata_write(svc, put, archive):
    path = put("A/Fremd.pdf", "fremd")
    svc.reconciler.scan()
    provisional = svc.index.get_by_path("A/Fremd.pdf").id
    temp = archive / "A" / ".docushelf-write.part"
    write_meta(path, temp, DocMeta(doc_id=UUID_B, title="Fremd"))
    os.replace(temp, path)

    record = svc.reconciler.index_file(path)

    assert record.id == UUID_B and record.has_xmp
    assert svc.index.get(provisional) is None and svc.index.count() == 1


def test_index_file_after_the_application_moved_a_file(svc, put, archive):
    letter = put("A/2025-01-01 Brief.pdf", "brief", meta=DocMeta(doc_id=UUID_A))
    foreign = put("A/Fremd.pdf", "fremd")
    svc.reconciler.scan()
    foreign_id = svc.index.get_by_path("A/Fremd.pdf").id
    (archive / "B").mkdir()
    os.rename(letter, archive / "B" / letter.name)
    os.rename(foreign, archive / "B" / "Fremd umbenannt.pdf")

    moved = svc.reconciler.index_file(archive / "B" / letter.name)
    moved_foreign = svc.reconciler.index_file(archive / "B" / "Fremd umbenannt.pdf")

    assert moved.id == UUID_A and moved.path == "B/2025-01-01 Brief.pdf"
    assert moved_foreign.id == foreign_id and moved_foreign.title == "Fremd umbenannt"
    assert svc.index.count() == 2
    assert changes(svc.reconciler.scan()) == (0, 0, 0, 0)


def test_index_file_of_a_copy_keeps_the_original_id(svc, put, archive):
    original = put("A/2025-01-01 Brief.pdf", "brief", meta=DocMeta(doc_id=UUID_A))
    svc.reconciler.scan()
    copy = archive / "A" / "2025-01-01 Brief (2).pdf"
    shutil.copy2(original, copy)

    record = svc.reconciler.index_file(copy)

    assert record.id != UUID_A and is_provisional(record.id)
    assert svc.index.get(UUID_A).path == "A/2025-01-01 Brief.pdf"


def test_index_file_uses_the_spelling_on_disk(svc, put, archive):
    name = unicodedata.normalize("NFD", "2025-01-01 Gebühr.pdf")
    put(f"A/{name}", "Gebühr")
    nfc_path = archive / "A" / unicodedata.normalize("NFC", name)
    if not nfc_path.exists():
        pytest.skip("file system distinguishes normalization forms")
    record = svc.reconciler.index_file(nfc_path)
    assert record.path == "A/" + os.listdir(archive / "A")[0]
    assert changes(svc.reconciler.scan()) == (0, 0, 0, 0)


def test_index_file_rejects_invalid_paths(svc, archive, tmp_path, samples):
    outside = samples.text_pdf(tmp_path / "draussen.pdf", ["x"])
    with pytest.raises(PathError):
        svc.reconciler.index_file(outside)
    hidden = samples.text_pdf(archive / "A" / ".versteckt.pdf", ["x"])
    with pytest.raises(PathError):
        svc.reconciler.index_file(hidden)
    with pytest.raises(NotFound):
        svc.reconciler.index_file(archive / "A" / "fehlt.pdf")
    with pytest.raises(PathError):
        svc.reconciler.index_file(archive / "A")


def test_forget_document_and_folder(svc, put, archive):
    put("A/2025-01-01 Eins.pdf", "eins", meta=DocMeta(doc_id=UUID_A))
    put("A/Sub/2025-01-02 Zwei.pdf", "zwei", meta=DocMeta(doc_id=UUID_B))
    put("B/2025-01-03 Drei.pdf", "drei", meta=DocMeta(doc_id=UUID_C))
    svc.reconciler.scan()

    svc.reconciler.forget("B/2025-01-03 Drei.pdf")
    assert svc.index.get(UUID_C) is None
    assert (archive / "B" / "2025-01-03 Drei.pdf").exists()  # the index only

    svc.reconciler.forget("A")
    assert svc.index.get(UUID_A) is None and svc.index.get(UUID_B) is None
    assert svc.index.list_folders() == ["B"]
    svc.reconciler.forget("gibt/es/nicht.pdf")

    # Everything that still exists comes back with the same ids.
    svc.reconciler.scan()
    assert {svc.index.get(i).path for i in (UUID_A, UUID_B, UUID_C)} == {
        "A/2025-01-01 Eins.pdf",
        "A/Sub/2025-01-02 Zwei.pdf",
        "B/2025-01-03 Drei.pdf",
    }


def test_forgetting_the_original_promotes_a_copy(svc, put, archive):
    original = put("A/2025-01-01 Brief.pdf", "brief", meta=DocMeta(doc_id=UUID_A))
    (archive / "B").mkdir()
    shutil.copy2(original, archive / "B" / original.name)
    svc.reconciler.scan()
    original.unlink()
    svc.reconciler.forget("A/2025-01-01 Brief.pdf")
    assert svc.index.get(UUID_A).path == "B/2025-01-01 Brief.pdf"


# -- through the service container -----------------------------------------------------------------------


def test_rebuild_through_the_service_container(services, samples, tmp_path):
    archive = services.layout.archive
    raw = samples.text_pdf(tmp_path / "raw.pdf", ["Beitragsrechnung Haftpflicht 2026"])
    (archive / "Versicherungen").mkdir()
    write_meta(raw, archive / "Versicherungen" / "2026-01-02 Beitragsrechnung.pdf", DocMeta(doc_id=UUID_A))
    samples.text_pdf(archive / "Versicherungen" / "Police.pdf", ["Versicherungsschein"])
    (archive / "Leer").mkdir()
    first = services.rebuild(user="test")
    assert first.added == 2
    before = records(services)

    second = services.rebuild(user="test")

    assert second.added == 2 and records(services) == before
    assert services.index.list_folders() == ["Leer", "Versicherungen"]
    hits = services.searcher.search("haftpflicht").hits
    assert [h.doc.id for h in hits] == [UUID_A]
    assert any(e["action"] == "rebuild" for e in services.audit.tail(5))


def test_names_that_are_not_utf8_are_skipped(svc, put, archive):
    put("A/2025-01-01 Gut.pdf", "gut")
    raw = os.fsencode(archive / "A") + b"/Rechnung \xff.pdf"
    try:
        with open(raw, "wb") as handle:
            handle.write(b"%PDF-1.4 not utf-8 named")
    except OSError:
        pytest.skip("the file system only accepts UTF-8 names")
    stats = svc.reconciler.scan()
    assert stats.added == 1 and stats.errors == 1
    assert list(records(svc)) == ["A/2025-01-01 Gut.pdf"]


def test_walk_skips_entries_that_cannot_be_stored(svc, put, archive, monkeypatch):
    """Simulates a directory entry whose name was decoded with surrogate escapes."""
    put("A/2025-01-01 Gut.pdf", "gut")
    real_scandir = os.scandir
    sample = next(e for e in real_scandir(archive / "A"))

    class BadEntry:
        name = "Rechnung \udcff.pdf"
        path = os.path.join(os.fspath(archive / "A"), name)

        def stat(self, follow_symlinks=True):
            return sample.stat(follow_symlinks=False)

    class Listing:
        def __init__(self, path):
            self._inner = real_scandir(path)
            self._extra = [BadEntry()] if os.fspath(path) == os.fspath(archive / "A") else []

        def __enter__(self):
            return iter([*self._inner, *self._extra])

        def __exit__(self, *exc):
            self._inner.close()

    monkeypatch.setattr(reconcile.os, "scandir", Listing)
    stats = svc.reconciler.scan()
    monkeypatch.undo()

    assert stats.added == 1 and stats.errors == 1
    assert list(records(svc)) == ["A/2025-01-01 Gut.pdf"]


def test_one_failing_document_does_not_block_the_others(svc, put, monkeypatch):
    put("A/2025-01-01 Eins.pdf", "eins", meta=DocMeta(doc_id=UUID_A))
    put("A/2025-01-02 Zwei.pdf", "zwei", meta=DocMeta(doc_id=UUID_B))
    real = Reconciler._extract_text

    def failing(self, path, ext):
        if path.name.startswith("2025-01-01"):
            raise RuntimeError("kaputt")
        return real(self, path, ext)

    monkeypatch.setattr(Reconciler, "_extract_text", failing)
    stats = svc.reconciler.scan()
    assert stats.added == 2 and stats.errors == 1
    assert svc.index.get(UUID_A).text_status == "pending"
    assert svc.index.get_pages(UUID_B) == ["zwei"]

    monkeypatch.undo()
    stats = svc.reconciler.scan()
    assert stats.errors == 0 and svc.index.get_pages(UUID_A) == ["eins"]
