"""Regression tests for the Paperless importer: file choice and OCR, protected PDFs,
deleted documents, recomputed import suggestions, file names in mixed Unicode
normalization, the default mapping and the import report.

Synthetic exports follow tests/data/paperless_manifest_structure.json (see
``ExportBuilder`` in tests/test_paperless_import.py).
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import unicodedata
from datetime import date
from pathlib import Path

import pikepdf
import pytest

import docushelf.importers.paperless as paperless
from docushelf.importers.paperless import (
    DEFAULT_MAPPING,
    DELETED_REASON,
    MAPPING_FILENAME,
    PaperlessImporter,
    inspect_pdf,
    load_mapping,
    map_folder,
    recompute_import_suggestion,
)
from docushelf.models import DocumentRecord, Suggestion
from tests.conftest import make_scan_pdf, make_text_pdf
from tests.test_paperless_import import ExportBuilder, FakeOps, FakePipeline, by_pk, standard_export

TEXT_PAGE = "Rechnung Nr. 4711\nAlza GmbH, Musterstraße 1\nBetrag 199,99 EUR"


@pytest.fixture
def services(base_services):
    # OCR on, whatever the machine running the tests has installed.
    base_services.settings = dataclasses.replace(base_services.settings, ocr_enabled=True)
    return base_services


@pytest.fixture
def importer(services) -> PaperlessImporter:
    return PaperlessImporter(services)


@pytest.fixture
def export_dir(tmp_path) -> Path:
    return standard_export(tmp_path / "export").write()


# -- building blocks ----------------------------------------------------------------------------------


def small_export(root: Path) -> ExportBuilder:
    builder = ExportBuilder(root)
    builder.add("auth.user", 1, username="max")
    builder.add("documents.correspondent", 1, name="Alza")
    builder.add("documents.documenttype", 1, name="Rechnung")
    builder.add("documents.documenttype", 2, name="Steuerbescheid")
    return builder


def record_of(builder: ExportBuilder, pk: int) -> dict:
    return next(r for r in builder.records if r["model"] == "documents.document" and r["pk"] == pk)


def replace_file(builder: ExportBuilder, pk: int, kind: str, make) -> Path:
    """Replace the original or archive file of a document and fix the recorded checksum."""
    record = record_of(builder, pk)
    key = "__exported_file_name__" if kind == "original" else "__exported_archive_name__"
    if key not in record:
        record[key] = f"{'originals' if kind == 'original' else 'archive'}/{pk:07d}.pdf"
    path = builder.root / record[key]
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    make(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    record["fields"]["checksum" if kind == "original" else "archive_checksum"] = digest
    return path


def combine(path: Path, *parts: Path) -> Path:
    """One PDF with the pages of ``parts`` in order."""
    sources = [pikepdf.open(part) for part in parts]
    try:
        with pikepdf.new() as pdf:
            for source in sources:
                pdf.pages.extend(source.pages)
            pdf.save(path)
    finally:
        for source in sources:
            source.close()
    return path


def mixed_pdf(path: Path, text_pages: int, scan_pages: int) -> Path:
    """``text_pages`` born-digital pages followed by ``scan_pages`` image-only pages."""
    text = make_text_pdf(path.with_name(path.stem + "-text.pdf"), [f"{TEXT_PAGE}\nSeite {n}" for n in range(text_pages)])
    scan = make_scan_pdf(path.with_name(path.stem + "-scan.pdf"), [["Anlage", f"Seite {n}"] for n in range(scan_pages)])
    combine(path, text, scan)
    text.unlink()
    scan.unlink()
    return path


def encrypted_pdf(path: Path, pages: list[str] = (TEXT_PAGE,)) -> Path:
    plain = make_text_pdf(path.with_name(path.stem + "-plain.pdf"), list(pages))
    with pikepdf.open(plain) as pdf:
        pdf.save(path, encryption=pikepdf.Encryption(user="geheim", owner="geheim", R=6))
    plain.unlink()
    return path


def signed_pdf(path: Path, make=None, *, how: str = "sigflags") -> Path:
    """A PDF carrying a signature field (the signature bytes themselves do not matter here)."""
    plain = path.with_name(path.stem + "-plain.pdf")
    (make or (lambda p: make_text_pdf(p, [TEXT_PAGE])))(plain)
    with pikepdf.open(plain) as pdf:
        if how == "perms":
            pdf.Root.Perms = pikepdf.Dictionary(DocMDP=pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Sig)))
        else:
            field = pikepdf.Dictionary(
                Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Widget, FT=pikepdf.Name.Sig,
                T=pikepdf.String("Signatur"), Rect=pikepdf.Array([0, 0, 0, 0]),
            )
            if how != "empty-field":
                field.V = pdf.make_indirect(pikepdf.Dictionary(
                    Type=pikepdf.Name.Sig, Filter=pikepdf.Name("/Adobe.PPKLite"),
                    SubFilter=pikepdf.Name("/adbe.pkcs7.detached"), Contents=pikepdf.String(b"\x00" * 32),
                    ByteRange=pikepdf.Array([0, 0, 0, 0]),
                ))
            acroform = pikepdf.Dictionary(Fields=pikepdf.Array([pdf.make_indirect(field)]))
            if how == "sigflags":
                acroform.SigFlags = 3
            pdf.Root.AcroForm = acroform
        pdf.save(path)
    plain.unlink()
    return path


def item_by_pk(services, pk: int, state: str = "arbeit"):
    return next(i for i in services.inbox.list(state) if i.context["paperless"]["id"] == pk)


# -- import-1: which file, and OCR only where it helps -------------------------------------------------


def test_mixed_original_with_archive_version_takes_the_archive_version(importer, services, tmp_path):
    builder = small_export(tmp_path / "export")
    builder.document(10, "Rechnung mit Anlagen", correspondent=1, document_type=1)
    replace_file(builder, 10, "original", lambda p: mixed_pdf(p, 1, 2))
    replace_file(builder, 10, "archive", lambda p: make_text_pdf(p, [TEXT_PAGE, "Anlage 1 (OCR)", "Anlage 2 (OCR)"]))
    planned = by_pk(importer.plan(importer.load(builder.write())))[10]
    assert planned.file_kind == "archive"
    assert planned.has_text and not planned.ocr
    report = importer.run([planned])
    item = item_by_pk(services, 10)
    assert item.options["skip_ocr"] is True
    assert item.original_paths()[0].read_bytes() == (builder.root / "archive" / "0000010.pdf").read_bytes()
    assert report.entries[0].processing == "Archivfassung, ohne OCR"


def test_every_page_counts_not_only_the_first_five(importer, tmp_path):
    builder = small_export(tmp_path / "export")
    builder.document(11, "Vertrag mit Anlage", correspondent=1, document_type=1)
    replace_file(builder, 11, "original", lambda p: mixed_pdf(p, 5, 1))
    planned = by_pk(importer.plan(importer.load(builder.write())))[11]
    assert planned.file_kind == "archive"  # page 6 is a scan, the archive version has its OCR text


def test_mixed_original_without_archive_version_goes_through_ocr(importer, services, tmp_path):
    builder = small_export(tmp_path / "export")
    builder.document(12, "Brief mit Anlage", correspondent=1, document_type=1, archive=False)
    replace_file(builder, 12, "original", lambda p: mixed_pdf(p, 1, 1))
    planned = by_pk(importer.plan(importer.load(builder.write())))[12]
    assert planned.file_kind == "original"
    assert not planned.has_text and planned.ocr
    assert planned.processing == "Original, mit OCR (1 von 2 Seiten ohne Text)"
    importer.run([planned])
    assert item_by_pk(services, 12).options["skip_ocr"] is False  # OCR (--skip-text) reads the scanned page


def test_archive_version_with_a_page_without_text_is_not_marked_skip_ocr(importer, services, tmp_path):
    builder = small_export(tmp_path / "export")
    builder.document(13, "Scan", correspondent=1, document_type=1, original="scan")
    replace_file(builder, 13, "archive", lambda p: combine(
        p, make_text_pdf(tmp_path / "a.pdf", [TEXT_PAGE]), make_scan_pdf(tmp_path / "b.pdf", [["Foto"]])
    ))
    planned = by_pk(importer.plan(importer.load(builder.write())))[13]
    assert planned.file_kind == "archive"
    assert not planned.has_text and planned.ocr
    importer.run([planned])
    assert item_by_pk(services, 13).options["skip_ocr"] is False


def test_original_with_text_on_every_page_stays_the_original(importer, services, tmp_path):
    builder = small_export(tmp_path / "export")
    builder.document(14, "Rechnung", correspondent=1, document_type=1)
    original = replace_file(builder, 14, "original", lambda p: make_text_pdf(p, [TEXT_PAGE] * 7))
    planned = by_pk(importer.plan(importer.load(builder.write())))[14]
    assert planned.file_kind == "original" and planned.has_text and not planned.ocr
    importer.run([planned])
    item = item_by_pk(services, 14)
    assert item.options["skip_ocr"] is True
    assert item.original_paths()[0].read_bytes() == original.read_bytes()


# -- protected originals (encrypted, signed) ---------------------------------------------------------------


def test_inspect_pdf_never_raises_and_detects_protection(tmp_path):
    assert inspect_pdf(encrypted_pdf(tmp_path / "enc.pdf")).protection == "encrypted"
    for how in ("sigflags", "field", "perms"):
        assert inspect_pdf(signed_pdf(tmp_path / f"{how}.pdf", how=how)).protection == "signed", how
    unsigned = inspect_pdf(signed_pdf(tmp_path / "empty.pdf", how="empty-field"))
    assert unsigned.protection is None and unsigned.all_text
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4\nnot really a pdf")
    assert inspect_pdf(broken) == paperless.PdfFacts()
    facts = inspect_pdf(mixed_pdf(tmp_path / "mixed.pdf", 2, 1))
    assert (facts.pages, facts.text_pages, facts.all_text) == (3, 2, False)


def test_encrypted_original_prefers_the_archive_version(importer, tmp_path):
    builder = small_export(tmp_path / "export")
    builder.document(20, "Kontoauszug", correspondent=1, document_type=1)
    replace_file(builder, 20, "original", encrypted_pdf)
    planned = by_pk(importer.plan(importer.load(builder.write())))[20]
    assert not planned.skipped
    assert planned.file_kind == "archive" and planned.protection is None and not planned.ocr
    assert "Original ist verschlüsselt – übernommen wird die Archivfassung" in planned.warnings


def test_encrypted_original_without_archive_version_is_imported_unchanged(importer, services, tmp_path):
    builder = small_export(tmp_path / "export")
    builder.document(21, "Lohnabrechnung", correspondent=1, document_type=1, archive=False)
    original = replace_file(builder, 21, "original", encrypted_pdf)
    planned = by_pk(importer.plan(importer.load(builder.write())))[21]
    assert planned.file_kind == "original" and planned.protection == "encrypted"
    assert not planned.ocr and planned.processing == "Original, ohne OCR (verschlüsselt)"
    assert any("verschlüsselt" in warning for warning in planned.warnings)
    dry = importer.run([planned], dry_run=True)
    assert "Original, ohne OCR (verschlüsselt)" in dry.lines()[0]
    importer.run([planned])
    item = item_by_pk(services, 21)
    assert item.original_paths()[0].read_bytes() == original.read_bytes()
    assert item.origin_hash == hashlib.sha256(original.read_bytes()).hexdigest()


def test_signed_original_with_text_on_every_page_is_taken_byte_identical(importer, services, tmp_path):
    builder = small_export(tmp_path / "export")
    builder.document(22, "Rechnung Telekom", correspondent=1, document_type=1)
    original = replace_file(builder, 22, "original", signed_pdf)
    planned = by_pk(importer.plan(importer.load(builder.write())))[22]
    assert planned.file_kind == "original" and planned.protection == "signed" and planned.has_text
    assert any("Signatur gültig bleibt" in warning for warning in planned.warnings)
    importer.run([planned])
    item = item_by_pk(services, 22)
    assert item.original_paths()[0].read_bytes() == original.read_bytes()
    assert item.options["skip_ocr"] is True


def test_signed_original_with_scanned_pages_notes_the_lost_signature(importer, tmp_path):
    builder = small_export(tmp_path / "export")
    builder.document(23, "Bescheid", correspondent=1, document_type=1)
    replace_file(builder, 23, "original", lambda p: signed_pdf(p, lambda q: mixed_pdf(q, 1, 1)))
    planned = by_pk(importer.plan(importer.load(builder.write())))[23]
    assert planned.file_kind == "archive"
    assert any("Signatur nicht mehr gilt" in warning for warning in planned.warnings)


def test_signed_original_without_text_and_archive_is_passed_through_without_ocr(importer, tmp_path):
    builder = small_export(tmp_path / "export")
    builder.document(24, "Bescheid", correspondent=1, document_type=1, archive=False)
    replace_file(builder, 24, "original", lambda p: signed_pdf(p, lambda q: make_scan_pdf(q, [["Scan"]])))
    planned = by_pk(importer.plan(importer.load(builder.write())))[24]
    assert planned.file_kind == "original" and not planned.ocr
    assert planned.processing == "Original, ohne OCR (digital signiert)"


# -- import-5: documents deleted in DocuShelf ----------------------------------------------------------------


def test_items_deleted_from_the_inbox_are_not_imported_again(importer, services, export_dir):
    from docushelf.archive.trash import Trash

    importer.run(importer.plan(importer.load(export_dir)))
    Trash(services).put_inbox_item(item_by_pk(services, 1), user="max")

    again = by_pk(importer.plan(importer.load(export_dir)))
    assert again[1].skipped and again[1].reason == DELETED_REASON
    assert again[2].reason == "bereits im Eingang"

    forced = by_pk(importer.plan(importer.load(export_dir), include_deleted=True))
    assert not forced[1].skipped
    report = importer.run([forced[1]])
    assert report.imported == 1
    assert item_by_pk(services, 1).context["paperless"]["id"] == 1


def test_documents_deleted_from_the_archive_are_not_imported_again(importer, services, export_dir):
    from docushelf.archive.trash import Trash

    original = export_dir / "originals" / "0000001.pdf"
    target = services.layout.archive / "Finanzen" / "2023-05-01 Rechnung.pdf"
    target.parent.mkdir(parents=True)
    target.write_bytes(original.read_bytes())
    record = DocumentRecord(
        id="2f1b9c1e-0000-4000-8000-000000000001", path="Finanzen/2023-05-01 Rechnung.pdf", folder="Finanzen",
        filename="2023-05-01 Rechnung.pdf", ext=".pdf", title="Rechnung", doc_date=date(2023, 5, 1), sender=None,
        tags=[], origin_hash=hashlib.sha256(original.read_bytes()).hexdigest(), content_hash="c" * 64, xmp_id=None,
        has_xmp=True, size=1, mtime_ns=1, inode=None, page_count=1, text_status="ok", indexed_at="",
    )
    services.index.upsert(record)
    Trash(services).put_document(target, record, user="max")
    planned = by_pk(importer.plan(importer.load(export_dir)))
    assert planned[1].skipped and planned[1].reason == DELETED_REASON
    assert not by_pk(importer.plan(importer.load(export_dir), include_deleted=True))[1].skipped


def test_import_export_passes_include_deleted(importer, services, export_dir):
    from docushelf.archive.trash import Trash

    importer.import_export(export_dir)
    Trash(services).put_inbox_item(item_by_pk(services, 3), user="max")
    assert importer.import_export(export_dir).imported == 0
    assert importer.import_export(export_dir, include_deleted=True).imported == 1


# -- import-3 / C1: recomputing the import suggestion ----------------------------------------------------------


def _edit_mapping(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    stat = path.stat()
    path.write_text(text.replace(old, new), encoding="utf-8")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))


def test_recompute_applies_the_edited_mapping(importer, services, export_dir):
    importer.run(importer.plan(importer.load(export_dir)))
    item = item_by_pk(services, 1)
    stored = dict(item.context["import_suggestion"])
    config = services.layout.config
    assert recompute_import_suggestion(item.context, config) == stored

    _edit_mapping(
        config / MAPPING_FILENAME,
        'Rechnung: "Finanzen/Rechnungen/{correspondent}"',
        'Rechnung: "Ausgaben/{year}/{correspondent}"',
    )
    result = recompute_import_suggestion(item.context, config)
    assert result["folder"] == "Ausgaben/2023/Alza"
    for key in ("title", "doc_date", "tags", "sender"):
        assert result[key] == stored[key], key
    assert result["source"] == "import" and result["score"] == 0.95
    assert result["reasons"] == ["aus Paperless: Typ Rechnung, Korrespondent Alza", "Ordner nach Dokumenttyp „Rechnung“"]
    assert set(result) == set(stored) and Suggestion.from_dict(result).folder == "Ausgaben/2023/Alza"
    assert item.context["import_suggestion"] == stored  # the context itself is not changed


def test_recompute_applies_correspondent_renames_and_storage_paths(importer, services, export_dir):
    importer.run(importer.plan(importer.load(export_dir)))
    config = services.layout.config
    path = config / MAPPING_FILENAME
    _edit_mapping(path, "correspondents: {}", 'correspondents:\n  Alza: "Alza Shop"')
    result = recompute_import_suggestion(item_by_pk(services, 1).context, config)
    assert result["folder"] == "Finanzen/Rechnungen/Alza Shop" and result["sender"] == "Alza"
    _edit_mapping(path, "storage_paths: {}", 'storage_paths:\n  Erika: "Erika/{document_type}"')
    assert recompute_import_suggestion(item_by_pk(services, 2).context, config)["folder"] == "Erika/Kontoauszug"
    assert "Speicherpfad „Erika“" in recompute_import_suggestion(item_by_pk(services, 2).context, config)["reasons"][1]


def test_recompute_keeps_the_stored_folder_while_the_mapping_gives_the_same(importer, services, export_dir):
    (services.layout.archive / "finanzen" / "rechnungen").mkdir(parents=True)
    importer.run(importer.plan(importer.load(export_dir)))
    context = item_by_pk(services, 1).context
    assert context["import_suggestion"]["folder"] == "finanzen/rechnungen/Alza"  # spelled like the archive
    config = services.layout.config
    assert recompute_import_suggestion(context, config)["folder"] == "finanzen/rechnungen/Alza"
    # A folder renamed in the app rewrites the stored folder; recomputing must not undo that.
    context["import_suggestion"]["folder"] = "Finanzen/Belege/Alza"
    assert recompute_import_suggestion(context, config)["folder"] == "Finanzen/Belege/Alza"


def test_recompute_for_items_imported_before_mapped_folder_was_stored(importer, services, export_dir):
    importer.run(importer.plan(importer.load(export_dir)))
    context = item_by_pk(services, 1).context
    del context["paperless"]["mapped_folder"]
    config = services.layout.config
    assert recompute_import_suggestion(context, config)["folder"] == "Finanzen/Rechnungen/Alza"
    _edit_mapping(config / MAPPING_FILENAME, '"Finanzen/Rechnungen/{correspondent}"', '"Rechnungen/{correspondent}"')
    assert recompute_import_suggestion(context, config)["folder"] == "Rechnungen/Alza"


def test_recompute_falls_back_to_the_stored_suggestion(importer, services, export_dir):
    importer.run(importer.plan(importer.load(export_dir)))
    context = item_by_pk(services, 1).context
    stored = context["import_suggestion"]
    config = services.layout.config
    path = config / MAPPING_FILENAME
    path.write_text("default: [\n", encoding="utf-8")  # invalid YAML
    assert recompute_import_suggestion(context, config) == stored
    path.unlink()
    assert recompute_import_suggestion(context, config) == stored
    assert recompute_import_suggestion({"import_suggestion": stored}, config) is None
    assert recompute_import_suggestion({"paperless": context["paperless"]}, config) is None
    assert recompute_import_suggestion({}, config) is None


def test_recompute_never_raises(services, tmp_path):
    config = services.layout.config
    PaperlessImporter(services).ensure_mapping_file()
    odd = {"paperless": {"created": 12345, "document_type": ["x"], "correspondent": {"a": 1}},
           "import_suggestion": {"folder": None, "tags": None}}
    assert isinstance(recompute_import_suggestion(odd, config), dict)
    assert recompute_import_suggestion(None, config) is None  # type: ignore[arg-type]
    assert recompute_import_suggestion({"paperless": {}, "import_suggestion": {}}, tmp_path / "nirgends") == {}


def test_mapping_is_read_once_per_version_of_the_file(importer, services, export_dir, monkeypatch):
    importer.run(importer.plan(importer.load(export_dir)))
    context = item_by_pk(services, 1).context
    config = services.layout.config
    calls = []
    real = paperless.load_mapping
    monkeypatch.setattr(paperless, "load_mapping", lambda path: calls.append(path) or real(path))
    for _ in range(3):
        recompute_import_suggestion(context, config)
    assert len(calls) == 1
    _edit_mapping(config / MAPPING_FILENAME, '"Finanzen/Rechnungen/{correspondent}"', '"Belege/{correspondent}"')
    assert recompute_import_suggestion(context, config)["folder"] == "Belege/Alza"
    recompute_import_suggestion(context, config)
    assert len(calls) == 2


def test_plan_and_recompute_derive_folders_the_same_way(importer, services, export_dir):
    plan = importer.plan(importer.load(export_dir))
    mapping = load_mapping(services.layout.config / MAPPING_FILENAME)
    for planned in plan:
        doc = planned.document
        folder, rule = map_folder(
            mapping, correspondent=doc.correspondent, document_type=doc.document_type,
            storage_path=doc.storage_path, created=doc.created,
        )
        assert (folder, rule) == (planned.mapped_folder, planned.rule)
    importer.run(plan)
    for item in services.inbox.list("arbeit"):
        assert recompute_import_suggestion(item.context, services.layout.config) == item.context["import_suggestion"]


# -- (a) file names in mixed Unicode normalization ----------------------------------------------------------


MIXED = "originals/Rechnung Müller Bäckerei.pdf"  # NFD "ü" and NFC "ä" in one name
MANIFEST = unicodedata.normalize("NFC", MIXED)


def _mixed_name_export(root: Path) -> Path:
    builder = small_export(root)
    builder.document(30, "Rechnung Müller", correspondent=1, document_type=1, archive=False)
    record = record_of(builder, 30)
    (builder.root / record["__exported_file_name__"]).rename(builder.root / MIXED)
    record["__exported_file_name__"] = MANIFEST
    return builder.write()


def test_file_name_with_mixed_normalization_is_found(importer, tmp_path):
    export_dir = _mixed_name_export(tmp_path / "export")
    assert MANIFEST not in (MIXED, unicodedata.normalize("NFD", MANIFEST))
    planned = by_pk(importer.plan(importer.load(export_dir)))[30]
    assert not planned.skipped, planned.reason
    assert planned.file is not None and planned.file.is_file()


def test_mixed_normalization_on_a_byte_exact_file_system(importer, tmp_path, monkeypatch):
    """File systems like ext4 compare names byte by byte (macOS normalizes)."""
    export_dir = _mixed_name_export(tmp_path / "export")
    real_is_file = Path.is_file

    def exact_is_file(path: Path) -> bool:
        try:
            return path.name in os.listdir(path.parent) and real_is_file(path)
        except OSError:
            return False

    scanned = []
    real_scandir = os.scandir

    def counting_scandir(path):
        scanned.append(Path(path))
        return real_scandir(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "is_file", exact_is_file)
        assert not exact_is_file(export_dir / MANIFEST)  # the simulation holds
        export = importer.load(export_dir)
        document = next(d for d in export.documents if d.pk == 30)
        assert document.original is not None
        assert document.original.name == MIXED.split("/")[1]
        # Directory listings are read once per directory and shared between lookups.
        patch.setattr(paperless.os, "scandir", counting_scandir)
        listings: dict = {}
        for _ in range(3):
            assert paperless._export_file(export_dir, MANIFEST, [], listings) == document.original
        assert scanned.count(export_dir / "originals") == 1


# -- (b) default mapping ---------------------------------------------------------------------------------------


def test_default_mapping_files_tax_notices_without_year_and_explains_recompute(importer, services, export_dir):
    planned = by_pk(importer.plan(importer.load(export_dir)))
    assert planned[3].folder == "Steuern"
    text = (services.layout.config / MAPPING_FILENAME).read_text(encoding="utf-8")
    assert 'Steuerbescheid: "Steuern"\n' in text
    assert "„Vorschläge neu berechnen“" in text and "schon im Eingang warten" in text
    assert load_mapping(services.layout.config / MAPPING_FILENAME).document_types["Steuerbescheid"] == "Steuern"


# The default mapping file written by the previous version (unchanged by the user).
PREVIOUS_DEFAULT = DEFAULT_MAPPING.replace(
    "# Änderungen gelten beim nächsten Import – am besten vorher mit --dry-run prüfen.\n"
    "# Für Dokumente, die schon im Eingang warten, übernimmt „Vorschläge neu berechnen“\n"
    "# im Eingang die neuen Ordner (Titel, Datum, Tags und Absender bleiben, wie sie sind).\n",
    "# Änderungen gelten beim nächsten Lauf – am besten vorher mit --dry-run prüfen.\n",
).replace('Steuerbescheid: "Steuern"', 'Steuerbescheid: "Steuern/{year}"').replace(
    '#     Gemeinsam: "Haushalt/{document_type}"', '#     Steuer: "Steuern/{year}"'
).replace(
    "#   auto      das Original, wenn jede Seite Text hat (unverändert, digitale Signaturen\n"
    "#             bleiben gültig), sonst die Archivfassung (PDF/A mit OCR); fehlt sie,\n"
    "#             wird das Original per OCR erkannt\n",
    "#   auto      das Original, wenn es ein PDF mit Text ist, sonst die Archivfassung (PDF/A mit OCR)\n",
).replace(
    "#   original  immer das Original (Seiten ohne Text werden dann erneut per OCR erkannt)",
    "#   original  immer das Original (Scans ohne Text werden dann erneut per OCR erkannt)",
)


def test_untouched_old_default_mapping_is_recognized(importer, services):
    path = services.layout.config / MAPPING_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    assert PREVIOUS_DEFAULT != DEFAULT_MAPPING
    path.write_text(PREVIOUS_DEFAULT, encoding="utf-8")
    assert importer.mapping_is_old_default()
    path.write_text(PREVIOUS_DEFAULT.replace("Allgemein", "Sonstige"), encoding="utf-8")
    assert not importer.mapping_is_old_default()
    path.unlink()
    importer.ensure_mapping_file()
    assert not importer.mapping_is_old_default()


# -- (d) unsupported file types ---------------------------------------------------------------------------------


def _with_text_file(builder: ExportBuilder, pk: int, *, archive: bool) -> None:
    builder.document(pk, f"Notiz {pk}", correspondent=1, document_type=1, archive=archive)
    record = record_of(builder, pk)
    (builder.root / record["__exported_file_name__"]).unlink()
    name = f"originals/{pk:07d}.txt"
    (builder.root / name).write_text("Notiz: Zählerstand 12345", encoding="utf-8")
    record["__exported_file_name__"] = name
    record["fields"]["mime_type"] = "text/plain"
    record["fields"]["checksum"] = hashlib.sha256((builder.root / name).read_bytes()).hexdigest()


def test_unsupported_file_types_are_listed_for_manual_takeover(importer, tmp_path):
    builder = small_export(tmp_path / "export")
    builder.document(40, "Rechnung", correspondent=1, document_type=1)
    _with_text_file(builder, 41, archive=False)
    _with_text_file(builder, 42, archive=True)  # Paperless made a PDF of it: that one is imported
    planned = by_pk(importer.plan(importer.load(builder.write())))
    assert planned[41].skipped and planned[41].manual
    assert planned[41].reason == "Dateityp wird nicht unterstützt (text/plain) – bitte von Hand übernehmen"
    assert not planned[42].skipped and planned[42].file_kind == "archive"

    report = importer.run(list(planned.values()), dry_run=True)
    [manual] = report.manual_entries
    assert (manual.paperless_id, manual.source_file) == (41, "originals/0000041.txt")
    assert report.summary() == (
        "Probelauf: 2 Dokument(e) würden übernommen, 1 übersprungen (davon 1 von Hand zu übernehmen). "
        "Es wurde nichts verändert."
    )
    assert report.to_dict()["manual"] == 1


# -- (e) the report says whether OCR runs --------------------------------------------------------------------


def test_report_states_per_document_whether_ocr_runs(importer, export_dir):
    plan = importer.plan(importer.load(export_dir))
    dry = importer.run(plan, dry_run=True)
    lines = {entry.paperless_id: line for entry, line in zip(dry.entries, dry.lines())}
    assert lines[1] == "[geplant] #1 Rechnung 4711 → Finanzen/Rechnungen/Alza – Original, ohne OCR (neuer Ordner)"
    assert "– Archivfassung, ohne OCR" in lines[2]
    assert "– Original, mit OCR (kein Text)" in lines[3]
    assert "– Archivfassung, ohne OCR" in lines[5]  # image original, archive PDF taken
    assert "OCR" not in lines[6]  # skipped
    assert dry.with_ocr == 1 and dry.to_dict()["with_ocr"] == 1

    real = importer.run(plan)
    entries = {entry.paperless_id: entry for entry in real.entries}
    assert entries[3].status == "imported" and entries[3].ocr is True
    assert entries[3].processing == "Original, mit OCR (kein Text)"
    assert entries[1].ocr is False and entries[6].ocr is None
    assert "– Original, mit OCR (kein Text)" in next(line for line in real.lines() if "#3 " in line)
    assert real.with_ocr == 1


def test_report_says_when_ocr_is_switched_off(importer, services, export_dir):
    services.settings = dataclasses.replace(services.settings, ocr_enabled=False)
    planned = by_pk(importer.plan(importer.load(export_dir)))
    assert not planned[3].ocr and planned[3].processing == "Original, ohne OCR (Texterkennung ausgeschaltet)"
    assert planned[3].has_text is False  # OCR runs once it is switched on and the item is retried


def test_archive_mode_entries_carry_the_ocr_decision(importer, services, export_dir):
    services.pipeline = FakePipeline(services)
    services.ops = FakeOps(services)
    report = importer.run(importer.plan(importer.load(export_dir)), mode="archive")
    entries = {entry.paperless_id: entry for entry in report.entries}
    assert entries[1].status == "filed" and entries[1].processing == "Original, ohne OCR"
    assert entries[3].status == "filed" and entries[3].ocr is True
