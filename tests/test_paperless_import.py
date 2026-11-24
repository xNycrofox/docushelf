"""Tests for the Paperless-ngx importer (importers/paperless.py) with a synthetic export.

The export mirrors the structure of a real ``document_exporter`` run
(``--use-folder-prefix --no-thumbnail``, see tests/data/paperless_manifest_structure.json):
``manifest.json`` with model records, ``metadata.json``, ``originals/`` and ``archive/``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from docushelf.errors import Conflict, ConfigError, NotFound
from docushelf.importers.paperless import (
    MAPPING_FILENAME,
    ImportReport,
    PaperlessImporter,
    clean_title,
    load_mapping,
    render_folder,
)
from docushelf.models import DocumentRecord
from docushelf.util.names import format_filename
from tests.conftest import make_image, make_scan_pdf, make_text_pdf

STRUCTURE = Path(__file__).parent / "data" / "paperless_manifest_structure.json"


# -- building an export ------------------------------------------------------------------------------


class ExportBuilder:
    """Writes a Paperless export directory with tiny real files."""

    def __init__(self, root: Path, *, folder_prefix: bool = True, md5: bool = False):
        self.root = root
        self.folder_prefix = folder_prefix
        self.md5 = md5
        self.records: list[dict] = []
        root.mkdir(parents=True, exist_ok=True)

    def add(self, model: str, pk: int, **fields) -> int:
        self.records.append({"model": model, "pk": pk, "fields": fields})
        return pk

    def _path(self, kind: str, pk: int, suffix: str) -> tuple[str, Path]:
        if self.folder_prefix:
            name = f"{'originals' if kind == 'original' else 'archive'}/{pk:07d}{suffix}"
        else:
            name = f"{pk:07d}{'-archive' if kind == 'archive' else ''}{suffix}"
        return name, self.root / name

    def document(
        self,
        pk: int,
        title: str,
        *,
        created="2023-05-01",
        original: str = "text",  # text | scan | jpeg | none
        archive: bool = True,
        correspondent=None,
        document_type=None,
        storage_path=None,
        owner=None,
        tags=(),
        checksum: str | None = None,
        **extra,
    ) -> dict:
        record: dict = {"model": "documents.document", "pk": pk}
        original_name = None
        if original != "none":
            suffix = ".jpg" if original == "jpeg" else ".pdf"
            original_name, path = self._path("original", pk, suffix)
            if original == "text":
                make_text_pdf(path, [f"{title}\nDokument {pk}"])
            elif original == "scan":
                make_scan_pdf(path, [[f"Scan {title}", f"Dokument {pk}"]])
            else:
                make_image(path, [f"Foto {title}"])
            record["__exported_file_name__"] = original_name
            digest = (hashlib.md5 if self.md5 else hashlib.sha256)(path.read_bytes()).hexdigest()
        else:
            record["__exported_file_name__"] = f"originals/{pk:07d}.pdf"  # listed, but missing
            digest = "0" * 64
        archive_checksum = None
        if archive:
            archive_name, path = self._path("archive", pk, ".pdf")
            make_text_pdf(path, [f"{title}\nDokument {pk}\n(OCR)"])
            record["__exported_archive_name__"] = archive_name
            archive_checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        fields = {
            "title": title,
            "content": f"{title} Dokument {pk}",
            "created": created,
            "added": "2023-05-02T10:11:12.123456+02:00",
            "modified": "2023-05-02T10:11:12.123456+02:00",
            "checksum": checksum or digest,
            "archive_checksum": archive_checksum,
            "archive_serial_number": None,
            "correspondent": correspondent,
            "document_type": document_type,
            "storage_path": storage_path,
            "owner": owner,
            "tags": list(tags),
            "filename": original_name,
            "archive_filename": record.get("__exported_archive_name__"),
            "original_filename": f"upload-{pk}.pdf",
            "mime_type": "image/jpeg" if original == "jpeg" else "application/pdf",
            "page_count": 1,
            "deleted_at": None,
            "restored_at": None,
            "root_document": None,
            "version_index": None,
            "version_label": None,
            "transaction_id": None,
        }
        fields.update(extra)
        record["fields"] = fields
        self.records.append(record)
        return record

    def write(self, *, split: bool = False, version: str = "2.17.1") -> Path:
        (self.root / "metadata.json").write_text(json.dumps({"version": version}), encoding="utf-8")
        if not split:
            (self.root / "manifest.json").write_text(json.dumps(self.records, ensure_ascii=False), encoding="utf-8")
            return self.root
        per_doc = {"documents.document", "documents.note", "documents.customfieldinstance"}
        main = [r for r in self.records if r["model"] not in per_doc]
        (self.root / "manifest.json").write_text(json.dumps(main, ensure_ascii=False), encoding="utf-8")
        for record in (r for r in self.records if r["model"] == "documents.document"):
            pk = record["pk"]
            content = [record] + [
                r
                for r in self.records
                if r["model"] in ("documents.note", "documents.customfieldinstance") and r["fields"].get("document") == pk
            ]
            target = self.root / "json" / f"{pk:07d}-manifest.json"
            target.parent.mkdir(exist_ok=True)
            target.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
        return self.root


def standard_export(root: Path, **options) -> ExportBuilder:
    builder = ExportBuilder(root, **options)
    builder.add("auth.user", 1, username="max", email="max@example.org", first_name="Max")
    builder.add("auth.user", 2, username="erika", email="erika@example.org", first_name="Erika")
    builder.add("documents.correspondent", 1, name="Alza", owner=1, match="", matching_algorithm=6, is_insensitive=True)
    builder.add("documents.correspondent", 2, name="Stadtwerke Dresden", owner=1)
    builder.add("documents.correspondent", 3, name="Finanzamt Dresden", owner=None)
    for pk, name in enumerate(["Rechnung", "Kontoauszug", "Steuerbescheid", "Brief", "Urkunde / Ausweis"], start=1):
        builder.add("documents.documenttype", pk, name=name, owner=None)
    builder.add("documents.tag", 1, name="Posteingang", is_inbox_tag=True, color="#a6cee3")
    builder.add("documents.tag", 2, name="steuerrelevant", is_inbox_tag=False)
    builder.add("documents.tag", 3, name="Garantie", is_inbox_tag=False)
    builder.add("documents.storagepath", 1, name="Max", path="max/{{ title }}", owner=1)
    builder.add("documents.storagepath", 2, name="Erika", path="erika/{{ title }}", owner=2)
    builder.add("documents.storagepath", 3, name="Gemeinsam", path="gemeinsam/{{ title }}", owner=1)
    builder.add("documents.customfield", 1, name="Betrag", data_type="monetary", extra_data=None, created="2024-01-01T00:00:00Z")
    builder.add("documents.customfield", 2, name="Vertragsnummer", data_type="string", extra_data=None)
    builder.add(
        "documents.customfield",
        3,
        name="Status",
        data_type="select",
        extra_data={"select_options": [{"id": "a1", "label": "bezahlt"}, {"id": "b2", "label": "offen"}]},
    )

    builder.document(
        1, "2023-05-01 - Alza - Rechnung 4711", correspondent=1, document_type=1, storage_path=1, owner=1,
        tags=[1, 2, 3], archive_serial_number=17,
    )
    builder.document(2, "Kontoauszug Mai 2023", original="scan", document_type=2, storage_path=2, owner=2)
    builder.document(
        3, "Steuerbescheid 2022", original="scan", archive=False, correspondent=3, document_type=3, storage_path=3,
        owner=1, created="2023-08-13T22:30:00Z",
    )
    builder.document(4, "scan_0001", correspondent=2, document_type=4)
    builder.document(5, "Personalausweis", original="jpeg", document_type=5, storage_path=1, owner=1)
    builder.document(6, "Gelöscht", correspondent=1, document_type=1, storage_path=1, owner=1, deleted_at="2024-01-01T00:00:00Z")
    builder.document(7, "Vertrag Strom", correspondent=2, storage_path=3, owner=1, checksum="f" * 64)
    builder.document(8, "Ohne Datei", original="none", archive=False, document_type=1, storage_path=1, owner=1)

    builder.add("documents.note", 1, document=1, note="Mit Karte bezahlt", created="2023-05-03T08:00:00Z", user=1, deleted_at=None)
    builder.add("documents.note", 2, document=1, note="alte Notiz", created="2023-05-02T08:00:00Z", user=1, deleted_at="2023-06-01T00:00:00Z")
    builder.add("documents.customfieldinstance", 1, document=1, field=1, value_monetary="EUR199.99", value_text=None, deleted_at=None)
    builder.add("documents.customfieldinstance", 2, document=1, field=3, value_select="a1", deleted_at=None)
    builder.add("documents.customfieldinstance", 3, document=7, field=2, value_text="V-2023-55", deleted_at=None)
    return builder


@pytest.fixture
def export_dir(tmp_path) -> Path:
    return standard_export(tmp_path / "export").write()


@pytest.fixture
def importer(base_services) -> PaperlessImporter:
    return PaperlessImporter(base_services)


def by_pk(plan):
    return {planned.document.pk: planned for planned in plan}


# -- reading -----------------------------------------------------------------------------------------------


def test_fixture_follows_the_real_export_structure(export_dir):
    structure = json.loads(STRUCTURE.read_text())
    manifest = json.loads((export_dir / "manifest.json").read_text())
    real_fields = set(structure["models"]["documents.document"]["fields"])
    ours = next(r for r in manifest if r["model"] == "documents.document")
    assert set(ours["fields"]) <= real_fields
    assert set(ours) <= set(structure["models"]["documents.document"]["top_level_keys"])
    assert set(structure["export_layout"]) <= {p.name for p in export_dir.iterdir()} | {"originals", "archive"}


def test_load_resolves_references(importer, export_dir):
    export = importer.load(export_dir)
    assert export.version == "2.17.1"
    assert len(export.documents) == 8
    doc = export.documents[0]
    assert doc.pk == 1 and doc.correspondent == "Alza" and doc.document_type == "Rechnung"
    assert doc.storage_path == "Max" and doc.owner == "max"
    assert doc.tags == ["steuerrelevant", "Garantie"] and doc.inbox_tags == ["Posteingang"]
    assert doc.created == date(2023, 5, 1)
    assert doc.added == "2023-05-02T10:11:12+02:00"
    assert doc.archive_serial_number == 17
    assert doc.original == (export_dir / "originals" / "0000001.pdf").resolve()
    assert doc.archive == (export_dir / "archive" / "0000001.pdf").resolve()
    assert [n["text"] for n in doc.notes] == ["Mit Karte bezahlt"]
    assert doc.notes[0]["user"] == "max"
    assert {f["name"]: f["value"] for f in doc.custom_fields} == {"Betrag": "EUR199.99", "Status": "bezahlt"}
    # created as a UTC timestamp (older exports) is converted to the local date
    assert export.documents[2].created == date(2023, 8, 14)
    assert export.documents[5].deleted
    assert export.storage_path_names() == ["Erika", "Gemeinsam", "Max"]
    assert export.owner_names() == ["erika", "max"]


def test_load_errors(importer, tmp_path):
    with pytest.raises(NotFound):
        importer.load(tmp_path / "fehlt")
    (tmp_path / "leer").mkdir()
    with pytest.raises(NotFound):
        importer.load(tmp_path / "leer")
    zipped = tmp_path / "export.zip"
    zipped.write_bytes(b"PK\x03\x04")
    with pytest.raises(ConfigError, match="ZIP"):
        importer.load(zipped)
    broken = tmp_path / "kaputt"
    broken.mkdir()
    (broken / "manifest.json").write_text("{nope")
    with pytest.raises(ConfigError):
        importer.load(broken)


# -- mapping and titles --------------------------------------------------------------------------------------


def test_default_mapping_file_is_created_with_german_comments(importer, export_dir, base_services):
    path = base_services.layout.config / MAPPING_FILENAME
    assert not path.exists()
    plan = importer.plan(importer.load(export_dir))
    text = path.read_text(encoding="utf-8")
    assert "# Zuordnung für den Import aus Paperless-ngx" in text
    assert 'Kündigung: "Verträge/{correspondent}"' in text
    mapping = load_mapping(path)
    assert mapping.default == "Paperless-Import/{document_type}/{correspondent}"

    planned = by_pk(plan)
    assert planned[1].folder == "Finanzen/Rechnungen/Alza"
    assert planned[1].title == "Rechnung 4711"
    assert planned[2].folder == "Finanzen/Bank/Allgemein"  # no correspondent
    assert planned[3].folder == "Steuern"  # the notice year is often not the tax year
    assert planned[4].folder == "Paperless-Import/Brief/Stadtwerke Dresden"  # type without mapping
    assert planned[4].title == "Brief Stadtwerke Dresden"  # scanner name replaced
    assert planned[5].folder == "Persönliches/Urkunden"
    assert planned[7].folder == "Paperless-Import/Ohne Typ/Stadtwerke Dresden"
    assert "Standardvorlage" in planned[4].rule


def test_existing_mapping_file_is_used_and_never_overwritten(importer, export_dir, base_services):
    path = base_services.layout.config / MAPPING_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    custom = (
        "document_types:\n"
        "  rechnung: 'Ausgaben/{year}/{correspondent}'\n"
        "  Urkunde/Ausweis: 'Dokumente/Ausweise'\n"
        "default: 'Import/{document_type}'\n"
        "storage_paths:\n"
        "  Gemeinsam: 'Haushalt/{storage_path}/{year}'\n"
        "correspondents:\n"
        "  Stadtwerke Dresden: Stadtwerke\n"
        "missing_correspondent: Unbekannt\n"
        "skip_tags: [Garantie]\n"
        "file: archive\n"
    )
    path.write_text(custom, encoding="utf-8")
    planned = by_pk(importer.plan(importer.load(export_dir)))
    assert path.read_text(encoding="utf-8") == custom
    assert planned[1].folder == "Ausgaben/2023/Alza"  # case-insensitive type match
    assert planned[1].tags == ["steuerrelevant"]
    assert planned[1].file_kind == "archive"
    assert planned[3].folder == "Haushalt/Gemeinsam/2023"  # storage path wins over the type
    assert planned[4].folder == "Import/Brief" and planned[4].sender == "Stadtwerke"
    assert planned[4].title == "Brief Stadtwerke"
    assert planned[5].folder == "Dokumente/Ausweise"  # "Urkunde / Ausweis" matched loosely


@pytest.mark.parametrize(
    "content,message",
    [
        ("default: 'Import/{foo}'\n", "unbekannter Platzhalter"),
        ("document_types: [a, b]\n", "Zuordnung"),
        ("default: [\n", "kein gültiges YAML"),
        ("file: beides\n", "auto, archive oder original"),
    ],
)
def test_invalid_mapping_files(importer, export_dir, base_services, content, message):
    path = base_services.layout.config / MAPPING_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        importer.plan(importer.load(export_dir))


def test_render_folder_sanitizes_values():
    values = {"correspondent": "AOK Plus - Sachsen", "document_type": "Rechnung", "year": "2023", "storage_path": ""}
    assert render_folder("Gesundheit/{correspondent}/{storage_path}", values) == "Gesundheit/AOK Plus - Sachsen"
    assert render_folder("../{year}", values) == "2023"


@pytest.mark.parametrize(
    "title,correspondent,doc_type,expected",
    [
        ("2023-05-01 - Alza - Rechnung 4711", "Alza", "Rechnung", "Rechnung 4711"),
        ("Alza - 2023-05-01 - Rechnung 4711", "Alza", "Rechnung", "Rechnung 4711"),
        ("20230501_Rechnung_Strom", "Stadtwerke", "Rechnung", "Rechnung Strom"),
        ("01.05.2023 Kontoauszug Mai", "Sparkasse", "Kontoauszug", "Kontoauszug Mai"),
        ("12345678 Vertrag", None, "Vertrag", "12345678 Vertrag"),
        ("Alza", "Alza", "Rechnung", "Rechnung Alza"),
        ("Alzaheimer Info", "Alza", None, "Alzaheimer Info"),
        ("Alza.cz a.s. – Faktura 2025001", "Alza.cz a.s.", "Rechnung", "Faktura 2025001"),
        ("2023-05-01", None, None, "Dokument"),
        ("Kontoauszug 2023-05", "Sparkasse", "Kontoauszug", "Kontoauszug 2023-05"),
    ],
)
def test_clean_title(title, correspondent, doc_type, expected):
    assert clean_title(title, correspondent=correspondent, document_type=doc_type) == expected


# -- planning ----------------------------------------------------------------------------------------------------


def test_filter_by_storage_path_and_owner(importer, export_dir):
    export = importer.load(export_dir)
    assert {p.document.pk for p in importer.plan(export, storage_paths=["max"])} == {1, 5, 6, 8}
    assert {p.document.pk for p in importer.plan(export, owners=["ERIKA"])} == {2}
    assert {p.document.pk for p in importer.plan(export, storage_paths=["Gemeinsam"], owners=["max"])} == {3, 7}
    assert {p.document.pk for p in importer.plan(export, owners=["-"])} == {4}
    assert {p.document.pk for p in importer.plan(export, storage_paths=["-", "Erika"])} == {2, 4}
    assert {p.document.pk for p in importer.plan(export, storage_paths=["2"])} == {2}  # by id
    assert len(importer.plan(export, storage_paths=[], owners=None)) == 8
    with pytest.raises(ConfigError, match="vorhanden: Erika, Gemeinsam, Max"):
        importer.plan(export, storage_paths=["Privat"])
    with pytest.raises(ConfigError, match="Besitzer"):
        importer.plan(export, owners=["bob"])


def test_file_choice_and_ocr_option(importer, export_dir):
    planned = by_pk(importer.plan(importer.load(export_dir)))
    # born-digital original with text: the original itself
    assert planned[1].file_kind == "original" and planned[1].has_text
    assert planned[1].file == export_dir.resolve() / "originals" / "0000001.pdf"
    # scanned original with an OCR'd archive version: the archive version
    assert planned[2].file_kind == "archive" and planned[2].has_text
    # scan without archive version: the original, OCR needed
    assert planned[3].file_kind == "original" and not planned[3].has_text
    # image original: the archive PDF
    assert planned[5].file_kind == "archive"


def test_file_preference_original(importer, export_dir, base_services):
    path = base_services.layout.config / MAPPING_FILENAME
    importer.ensure_mapping_file()
    path.write_text(path.read_text(encoding="utf-8").replace("file: auto", "file: original"), encoding="utf-8")
    planned = by_pk(importer.plan(importer.load(export_dir)))
    assert planned[2].file_kind == "original" and not planned[2].has_text
    assert planned[5].file_kind == "original" and planned[5].file.suffix == ".jpg"


def test_skipped_documents(importer, export_dir):
    planned = by_pk(importer.plan(importer.load(export_dir)))
    assert planned[6].skipped and "gelöscht" in planned[6].reason
    assert planned[8].skipped and "Datei fehlt im Export" in planned[8].reason
    assert not planned[1].skipped


def test_checksum_is_verified(importer, export_dir):
    planned = by_pk(importer.plan(importer.load(export_dir)))
    original = export_dir / "originals" / "0000001.pdf"
    assert planned[1].origin_hash == hashlib.sha256(original.read_bytes()).hexdigest()
    assert planned[1].warnings == []
    assert any("Prüfsumme" in w for w in planned[7].warnings)
    assert planned[7].origin_hash == hashlib.sha256((export_dir / "originals" / "0000007.pdf").read_bytes()).hexdigest()


def test_existing_folder_is_reused_ignoring_case(importer, export_dir, base_services):
    (base_services.layout.archive / "Finanzen" / "rechnungen" / "Alza").mkdir(parents=True)
    planned = by_pk(importer.plan(importer.load(export_dir)))
    assert planned[1].folder == "Finanzen/rechnungen/Alza"
    assert planned[1].new_folder is False
    assert planned[1].suggestion.new_folder is False
    assert planned[2].new_folder is True  # Finanzen/Bank/Allgemein does not exist


def test_folder_known_to_the_index_is_not_new(importer, export_dir, base_services):
    base_services.index.add_folder_with_parents("Steuern")
    planned = by_pk(importer.plan(importer.load(export_dir)))
    assert planned[3].new_folder is False


# -- running ------------------------------------------------------------------------------------------------------


def test_dry_run_changes_nothing(importer, export_dir, base_services):
    before = sorted(p.relative_to(export_dir) for p in export_dir.rglob("*"))
    report = importer.run(importer.plan(importer.load(export_dir)), dry_run=True)
    assert report.dry_run and report.planned == 6 and report.skipped == 2 and report.imported == 0
    assert base_services.inbox.list("arbeit") == []
    assert base_services.index.pending_jobs() == []
    assert sorted(p.relative_to(export_dir) for p in export_dir.rglob("*")) == before
    assert report.summary().startswith("Probelauf: 6 Dokument(e) würden übernommen, 2 übersprungen")
    line = next(l for l in report.lines() if "#1 " in l)
    assert line.startswith("[geplant] #1 Rechnung 4711 → Finanzen/Rechnungen/Alza")
    assert base_services.audit.tail() == []


def test_inbox_import_creates_items_with_suggestion_and_context(importer, export_dir, base_services):
    report = importer.run(importer.plan(importer.load(export_dir)), user="max")
    assert report.imported == 6 and report.skipped == 2 and report.failed == 0
    assert report.summary() == "Paperless-Import: 6 Dokument(e) in den Eingang übernommen, 2 übersprungen."

    items = {item.context["paperless"]["id"]: item for item in base_services.inbox.list("arbeit")}
    assert set(items) == {1, 2, 3, 4, 5, 7}
    item = items[1]
    original = export_dir / "originals" / "0000001.pdf"
    assert item.source == "paperless"
    assert item.origin_hash == hashlib.sha256(original.read_bytes()).hexdigest()
    assert item.original_paths()[0].read_bytes() == original.read_bytes()
    assert original.exists()  # copied, never moved
    assert item.options == {"skip_ocr": True, "skip_blank_removal": True}
    assert item.received_at == "2023-05-02T10:11:12+02:00"
    assert item.context["title"] == "Rechnung 4711"
    assert item.display_name == "Rechnung 4711"

    suggestion = item.context["import_suggestion"]
    assert suggestion["source"] == "import" and suggestion["score"] == 0.95
    assert suggestion["folder"] == "Finanzen/Rechnungen/Alza"
    assert suggestion["title"] == "Rechnung 4711"
    assert suggestion["doc_date"] == "2023-05-01"
    assert suggestion["sender"] == "Alza"
    assert suggestion["tags"] == ["steuerrelevant", "Garantie"]
    assert suggestion["new_folder"] is True
    assert suggestion["reasons"][0] == "aus Paperless: Typ Rechnung, Korrespondent Alza"

    paperless = item.context["paperless"]
    assert paperless["notes"][0]["text"] == "Mit Karte bezahlt"
    assert {f["name"]: f["value"] for f in paperless["custom_fields"]} == {"Betrag": "EUR199.99", "Status": "bezahlt"}
    assert paperless["archive_serial_number"] == 17
    assert paperless["correspondent"] == "Alza" and paperless["document_type"] == "Rechnung"
    assert paperless["storage_path"] == "Max" and paperless["owner"] == "max"
    assert paperless["file"] == "original"
    assert paperless["title"] == "2023-05-01 - Alza - Rechnung 4711"

    assert items[2].options["skip_ocr"] is True  # archive version has text
    assert items[3].options["skip_ocr"] is False  # scan without text must be OCR'd
    assert items[2].context["import_suggestion"]["reasons"][0] == "aus Paperless: Typ Kontoauszug, ohne Korrespondent"
    assert "Prüfsumme" in base_services.inbox.read_log(items[7])
    assert "Aus Paperless-ngx übernommen (Dokument #1, Original)" in base_services.inbox.read_log(item)

    jobs = {job.key for job in base_services.index.pending_jobs("process")}
    assert jobs == {i.id for i in items.values()}
    audit = base_services.audit.tail()
    assert audit[0]["action"] == "import_paperless" and audit[0]["imported"] == 6 and audit[0]["user"] == "max"


def test_import_runs_again_without_duplicates(importer, export_dir, base_services):
    importer.run(importer.plan(importer.load(export_dir)))
    second_plan = importer.plan(importer.load(export_dir))
    assert all(p.skipped for p in second_plan)
    assert by_pk(second_plan)[1].reason == "bereits im Eingang"
    report = importer.run(second_plan)
    assert report.imported == 0 and report.skipped == 8
    assert len(base_services.inbox.list("arbeit")) == 6


def test_documents_known_in_the_archive_are_skipped(importer, export_dir, base_services):
    original = export_dir / "originals" / "0000001.pdf"
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    base_services.index.upsert(
        DocumentRecord(
            id="2f1b9c1e-0000-4000-8000-000000000001", path="Finanzen/2023-05-01 Rechnung.pdf", folder="Finanzen",
            filename="2023-05-01 Rechnung.pdf", ext=".pdf", title="Rechnung", doc_date=date(2023, 5, 1), sender=None,
            tags=[], origin_hash=digest, content_hash="c" * 64, xmp_id=None, has_xmp=True, size=1, mtime_ns=1,
            inode=None, page_count=1, text_status="ok", indexed_at="",
        )
    )
    archive_copy = export_dir / "archive" / "0000002.pdf"
    base_services.index.upsert(
        DocumentRecord(
            id="h-" + "a" * 32, path="Bank/kopie.pdf", folder="Bank", filename="kopie.pdf", ext=".pdf", title="kopie",
            doc_date=None, sender=None, tags=[], origin_hash=None,
            content_hash=hashlib.sha256(archive_copy.read_bytes()).hexdigest(), xmp_id=None, has_xmp=False, size=1,
            mtime_ns=1, inode=None, page_count=1, text_status="ok", indexed_at="",
        )
    )
    planned = by_pk(importer.plan(importer.load(export_dir)))
    assert planned[1].skipped and planned[1].reason == "bereits im Archiv"
    assert planned[2].skipped and planned[2].reason == "Datei liegt bereits im Archiv"


def test_progress_callback(importer, export_dir):
    seen = []
    importer.run(importer.plan(importer.load(export_dir)), dry_run=True, progress=lambda i, n, e: seen.append((i, n, e.status)))
    assert [s[:2] for s in seen] == [(i, 8) for i in range(1, 9)]


def test_unknown_mode_is_rejected(importer, export_dir):
    with pytest.raises(ConfigError):
        importer.run([], mode="irgendwo")


# -- older export formats -----------------------------------------------------------------------------------------


def test_older_export_with_split_manifests_md5_and_no_folder_prefix(importer, tmp_path, base_services):
    builder = standard_export(tmp_path / "alt", folder_prefix=False, md5=True)
    export_dir = builder.write(split=True, version="1.17.4")
    export = importer.load(export_dir)
    assert len(export.documents) == 8
    doc = export.documents[0]
    assert doc.original == (export_dir / "0000001.pdf").resolve()
    assert doc.archive == (export_dir / "0000001-archive.pdf").resolve()
    assert [n["text"] for n in doc.notes] == ["Mit Karte bezahlt"]
    assert {f["name"] for f in doc.custom_fields} == {"Betrag", "Status"}
    planned = by_pk(importer.plan(export))
    assert planned[1].warnings == []  # MD5 checksum verified
    assert planned[1].origin_hash == hashlib.sha256((export_dir / "0000001.pdf").read_bytes()).hexdigest()
    report = importer.run(list(planned.values()))
    assert report.imported == 6


def test_file_names_in_other_unicode_normalization_are_found(importer, tmp_path):
    import unicodedata

    builder = standard_export(tmp_path / "export")
    record = next(r for r in builder.records if r["model"] == "documents.document" and r["pk"] == 1)
    old = builder.root / record["__exported_file_name__"]
    nfd_name = unicodedata.normalize("NFD", "originals/Rechnung Müller.pdf")
    old.rename(builder.root / nfd_name)
    record["__exported_file_name__"] = "originals/Rechnung Müller.pdf"  # NFC in the manifest
    export = importer.load(builder.write())
    assert export.documents[0].original is not None and export.documents[0].original.is_file()


def test_paths_outside_the_export_are_ignored(importer, tmp_path):
    builder = standard_export(tmp_path / "export")
    secret = tmp_path / "secret.pdf"
    make_text_pdf(secret, ["geheim"])
    record = next(r for r in builder.records if r["model"] == "documents.document" and r["pk"] == 8)
    record["__exported_file_name__"] = "../secret.pdf"
    export = importer.load(builder.write())
    assert any("außerhalb" in w for w in export.warnings)
    doc = next(d for d in export.documents if d.pk == 8)
    assert doc.original is None


# -- archive mode --------------------------------------------------------------------------------------------------


class FakePipeline:
    """Turns the original into document.pdf and marks the item ready (like the real pipeline)."""

    def __init__(self, services, *, fail: set[int] = frozenset()):
        self.services = services
        self.fail = fail

    def process(self, item_id):
        item = self.services.inbox.load(item_id)
        if item.context["paperless"]["id"] in self.fail:
            item.error = {"stage": "normalized", "message": "PDF ist beschädigt", "at": "now"}
            self.services.inbox.save(item)
            return self.services.inbox.move(item, "fehler")
        shutil.copyfile(item.original_paths()[0], item.document_path)
        item.stage = "ready"
        item.suggestions = [item.context["import_suggestion"]]
        self.services.inbox.save(item)
        return self.services.inbox.move(item, "bereit")


class FakeOps:
    """Records filing requests and moves filed items to erledigt (like ArchiveOps.file_item)."""

    def __init__(self, services, *, conflict_for: set[str] = frozenset()):
        self.services = services
        self.filed = []
        self.conflict_for = conflict_for

    def file_item(self, item_id, request, *, user=None):
        item = self.services.inbox.load(item_id, states=("bereit",))
        if request.title in self.conflict_for:
            raise Conflict("Datei wurde zwischenzeitlich geändert")
        self.filed.append((item_id, request, user))
        self.services.inbox.move(item, "erledigt")
        name = format_filename(request.title, request.doc_date)
        return SimpleNamespace(id=f"doc-{len(self.filed)}", folder=request.folder, path=f"{request.folder}/{name}")


def test_archive_mode_files_directly(importer, export_dir, base_services):
    base_services.pipeline = FakePipeline(base_services, fail={4})
    base_services.ops = FakeOps(base_services, conflict_for={"Steuerbescheid 2022"})
    plan = importer.plan(importer.load(export_dir))
    report = importer.run(plan, mode="archive", user="max")

    statuses = {e.paperless_id: e.status for e in report.entries}
    assert statuses == {1: "filed", 2: "filed", 3: "failed", 4: "failed", 5: "filed", 6: "skipped", 7: "filed", 8: "skipped"}
    item_id, request, user = base_services.ops.filed[0]
    assert (request.folder, request.title, request.doc_date) == ("Finanzen/Rechnungen/Alza", "Rechnung 4711", date(2023, 5, 1))
    assert request.tags == ["steuerrelevant", "Garantie"] and request.sender == "Alza" and user == "max"
    entry = report.entries[0]
    assert entry.doc_id == "doc-1" and entry.message == "Finanzen/Rechnungen/Alza/2023-05-01 Rechnung 4711.pdf"
    failed = {e.paperless_id: e.message for e in report.entries if e.status == "failed"}
    assert "Ablage fehlgeschlagen" in failed[3] and "PDF ist beschädigt" in failed[4]
    # items that could not be filed stay in the inbox
    assert {i.context["paperless"]["id"] for i in base_services.inbox.list("bereit")} == {3}
    assert {i.context["paperless"]["id"] for i in base_services.inbox.list("erledigt")} == {1, 2, 5, 7}
    assert {i.context["paperless"]["id"] for i in base_services.inbox.list("fehler")} == {4}
    assert base_services.index.pending_jobs("process") == []  # processed synchronously, not queued
    assert report.summary() == "Paperless-Import: 4 Dokument(e) abgelegt, 2 übersprungen, 2 fehlgeschlagen."


def test_archive_mode_needs_pipeline_and_ops(importer, export_dir):
    with pytest.raises(ConfigError, match="Archivmodus"):
        importer.run(importer.plan(importer.load(export_dir)), mode="archive")


def test_report_serializes(importer, export_dir):
    report = importer.run(importer.plan(importer.load(export_dir)), dry_run=True)
    data = report.to_dict()
    assert data["planned"] == 6 and data["entries"][0]["paperless_id"] == 1
    assert isinstance(ImportReport(mode="inbox", dry_run=False, started_at="x").summary(), str)


# -- with the real collaborators --------------------------------------------------------------------------------


def attach_real_components(services):
    """Wire the real pipeline, reconciler and archive operations into ``base_services``."""
    pipeline = pytest.importorskip("docushelf.inbox.pipeline")
    reconcile = pytest.importorskip("docushelf.index.reconcile")
    ops = pytest.importorskip("docushelf.archive.ops")
    trash = pytest.importorskip("docushelf.archive.trash")
    services.suggester = None
    services.rules = None
    services.watcher = None
    services.worker = None
    services.reconciler = reconcile.Reconciler(services)
    services.trash = trash.Trash(services)
    services.ops = ops.ArchiveOps(services)
    services.pipeline = pipeline.Pipeline(services)
    return services


def test_archive_mode_end_to_end(importer, export_dir, base_services):
    from docushelf.pdf.xmp import read_meta

    attach_real_components(base_services)
    plan = importer.plan(importer.load(export_dir), storage_paths=["Max", "Gemeinsam"])
    report = importer.run(plan, mode="archive", user="max")

    statuses = {e.paperless_id: e.status for e in report.entries}
    assert statuses == {1: "filed", 3: "filed", 5: "filed", 6: "skipped", 7: "filed", 8: "skipped"}, report.lines()
    archive = base_services.layout.archive
    path = archive / "Finanzen" / "Rechnungen" / "Alza" / "2023-05-01 Rechnung 4711.pdf"
    assert path.is_file()
    meta = read_meta(path)
    original = export_dir / "originals" / "0000001.pdf"
    assert meta.title == "Rechnung 4711" and meta.doc_date == date(2023, 5, 1)
    assert meta.sender == "Alza" and meta.tags == ["steuerrelevant", "Garantie"]
    assert meta.source == "paperless"
    assert meta.origin_hash == hashlib.sha256(original.read_bytes()).hexdigest()
    assert meta.extra.get("PaperlessId") == "1"
    assert meta.extra.get("ArchiveSerialNumber") == "17"
    assert (archive / "Steuern" / "2023-08-14 Steuerbescheid 2022.pdf").is_file()
    assert (archive / "Persönliches" / "Urkunden").is_dir()
    record = base_services.index.get_by_path("Finanzen/Rechnungen/Alza/2023-05-01 Rechnung 4711.pdf")
    assert record is not None and record.origin_hash == meta.origin_hash
    # A second run finds everything in the archive.
    again = importer.plan(importer.load(export_dir), storage_paths=["Max", "Gemeinsam"])
    assert {p.document.pk: p.reason for p in again if p.document.pk in (1, 3, 5, 7)} == {
        1: "bereits im Archiv",
        3: "bereits im Archiv",
        5: "bereits im Archiv",
        7: "bereits im Archiv",
    }


def test_inbox_items_are_processed_by_the_real_pipeline(importer, export_dir, base_services):
    attach_real_components(base_services)
    report = importer.run(importer.plan(importer.load(export_dir), owners=["erika"]))
    [item_id] = report.item_ids
    item = base_services.pipeline.process(item_id)
    assert item.state == "bereit" and item.stage == "ready", base_services.inbox.read_log(item)
    assert item.document_path.is_file()
    log_text = base_services.inbox.read_log(item)
    assert "Aus Paperless-ngx übernommen" in log_text


# -- reviewed import plans -------------------------------------------------------------------------------


def write_plan(tmp_path, documents) -> Path:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"version": 1, "documents": documents}, ensure_ascii=False), encoding="utf-8")
    return path


def test_plan_overrides_folder_title_date_sender_and_tags(importer, export_dir, tmp_path):
    from docushelf.importers.paperless import load_import_plan

    overrides = load_import_plan(
        write_plan(
            tmp_path,
            [
                {
                    "pk": 1,
                    "folder": "Wohnen/Haushaltsgeräte",
                    "title": "Rechnung Waschmaschine",
                    "doc_date": "2023-05-02",
                    "sender": "Alza",
                    "tags": ["Garantie"],
                }
            ],
        )
    )
    plan = by_pk(importer.plan(importer.load(export_dir), overrides=overrides))
    first = plan[1]
    assert (first.folder, first.title, first.doc_date, first.sender, first.tags) == (
        "Wohnen/Haushaltsgeräte",
        "Rechnung Waschmaschine",
        date(2023, 5, 2),
        "Alza",
        ["Garantie"],
    )
    assert first.from_plan and first.rule == "Zuordnungsplan"
    assert first.suggestion.folder == "Wohnen/Haushaltsgeräte"
    # Documents missing from the plan keep the mapping …
    assert not plan[3].from_plan and plan[3].folder != "Wohnen/Haushaltsgeräte"
    # … or are skipped with only_planned.
    only = by_pk(importer.plan(importer.load(export_dir), overrides=overrides, only_planned=True))
    assert only[3].skipped and only[3].reason == "nicht im Zuordnungsplan"
    assert not only[1].skipped


def test_plan_values_survive_recomputing_suggestions(importer, export_dir, tmp_path, base_services):
    from docushelf.importers.paperless import load_import_plan, recompute_import_suggestion

    overrides = load_import_plan(write_plan(tmp_path, [{"pk": 1, "folder": "Wohnen/Geräte", "title": "Rechnung"}]))
    plan = importer.plan(importer.load(export_dir), overrides=overrides)
    report = importer.run([p for p in plan if p.document.pk == 1], mode="inbox")
    item = base_services.inbox.load(report.item_ids[0])
    assert item.context["paperless"]["plan"] is True
    again = recompute_import_suggestion(item.context, base_services.layout.config)
    assert again["folder"] == "Wohnen/Geräte"


@pytest.mark.parametrize(
    ("documents", "message"),
    [
        ({"pk": "1", "folder": "A", "title": "B"}, "Dokumentnummer"),
        ({"pk": 1, "title": "B"}, "„folder“ fehlt"),
        ({"pk": 1, "folder": "A"}, "„title“ fehlt"),
        ({"pk": 1, "folder": "A", "title": "B", "doc_date": "01.02.2023"}, "JJJJ-MM-TT"),
        ({"pk": 1, "folder": "../A", "title": "B"}, "ungültiger Ordner"),
        ({"pk": 1, "folder": "A", "title": "B", "tags": "x"}, "Liste"),
    ],
)
def test_invalid_plans_are_rejected(tmp_path, documents, message):
    from docushelf.errors import ConfigError
    from docushelf.importers.paperless import load_import_plan

    with pytest.raises(ConfigError, match=message):
        load_import_plan(write_plan(tmp_path, [documents]))


def test_duplicate_plan_entries_are_rejected(tmp_path):
    from docushelf.errors import ConfigError
    from docushelf.importers.paperless import load_import_plan

    entry = {"pk": 1, "folder": "A", "title": "B"}
    with pytest.raises(ConfigError, match="doppelt"):
        load_import_plan(write_plan(tmp_path, [entry, entry]))
