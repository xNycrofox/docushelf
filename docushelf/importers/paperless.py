"""Import a Paperless-ngx export.

Reads the directory written by Paperless-ngx's ``document_exporter``:
``manifest.json`` (or, with ``--split-manifest``, one ``*-manifest.json`` per
document), ``metadata.json`` and the exported files (``originals/`` and
``archive/`` with ``--use-folder-prefix``, otherwise next to the manifest).

Every document becomes an inbox item with a filing suggestion: the folder is
derived from document type, correspondent, date and storage path through the
templates in ``config/paperless-import.yaml`` (created with defaults on first
use, never overwritten afterwards). Titles lose prefixes that only repeat the
date or the correspondent. Notes, custom fields, tags and the archive serial
number travel along in ``item.context["paperless"]``.
``recompute_import_suggestion`` derives the folder again from the current
mapping file, so editing the file and recomputing the suggestions in the
inbox applies the new folders to items that are still waiting.

The original is imported as it is when every page has a text layer (digital
signatures stay valid); otherwise the archive version with Paperless's OCR
is taken, and without one the original goes through OCR.

A reviewed plan (``load_import_plan``) can set folder, title, date, sender and
tags per Paperless document id; planned values win over the mapping and are
kept when suggestions are recomputed later.

The export is only read, never changed. Documents whose original is already
known (same SHA-256 in the index or in the inbox) are skipped, so an
interrupted import can simply be started again. So are documents that were
deleted in DocuShelf and still wait in the trash, unless ``include_deleted``
is given.

Typical use (``docushelf import-paperless``)::

    importer = PaperlessImporter(services)
    export = importer.load("/import/paperless-export")
    plan = importer.plan(export, storage_paths=["Privat"], owners=["max"])
    report = importer.run(plan, mode="inbox", dry_run=True)
    print(report.summary())
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import string
import threading
import unicodedata
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping

import yaml

from ..channels.mailparse import sniff
from ..errors import ConfigError, DocuShelfError, NotFound, PathError, ProcessingError
from ..models import FilingRequest, Suggestion
from ..util import fsops, timeutil
from ..util.names import casefold_key, sanitize_component, sanitize_folder, title_from_original_name
from ..util.textnorm import normalize

if TYPE_CHECKING:  # pragma: no cover
    from ..inbox.items import InboxItem
    from ..services import Services

log = logging.getLogger(__name__)

SOURCE = "paperless"
MAPPING_FILENAME = "paperless-import.yaml"
IMPORT_SCORE = 0.95
PLACEHOLDERS = ("correspondent", "document_type", "year", "storage_path")
FILE_PREFERENCES = ("auto", "archive", "original")
NO_VALUE = "-"  # filter value for "no storage path" / "no owner"
FALLBACK_FOLDER = "Paperless-Import"
DELETED_REASON = "im Papierkorb (gelöscht)"

DEFAULT_MAPPING = """\
# Zuordnung für den Import aus Paperless-ngx (docushelf import-paperless).
#
# Jeder Dokumenttyp aus Paperless bekommt eine Ordnervorlage im Archiv.
# Platzhalter:
#   {correspondent}  Korrespondent (fehlt er: missing_correspondent)
#   {document_type}  Dokumenttyp (fehlt er: missing_document_type)
#   {year}           Jahr des Dokumentdatums (fehlt es: missing_year)
#   {storage_path}   Name des Speicherpfads in Paperless (fehlt er: entfällt)
# Unterordner werden mit "/" getrennt. Vorhandene Ordner werden wiederverwendet,
# auch wenn sich Groß-/Kleinschreibung unterscheidet.
#
# Diese Datei wird beim ersten Import angelegt und danach nie überschrieben.
# Änderungen gelten beim nächsten Import – am besten vorher mit --dry-run prüfen.
# Für Dokumente, die schon im Eingang warten, übernimmt „Vorschläge neu berechnen“
# im Eingang die neuen Ordner (Titel, Datum, Tags und Absender bleiben, wie sie sind).

document_types:
  Rechnung: "Finanzen/Rechnungen/{correspondent}"
  Mahnung: "Finanzen/Rechnungen/{correspondent}"
  Kontoauszug: "Finanzen/Bank/{correspondent}"
  Angebot: "Finanzen/Angebote/{correspondent}"
  Gehaltsabrechnung: "Arbeit/Gehaltsabrechnungen"
  Steuerbescheid: "Steuern"
  Versicherungsschein: "Versicherungen/{correspondent}"
  Vertrag: "Verträge/{correspondent}"
  Kündigung: "Verträge/{correspondent}"
  Arztbrief: "Gesundheit/{correspondent}"
  "Urkunde / Ausweis": "Persönliches/Urkunden"
  Zeugnis: "Persönliches/Zeugnisse"
  Bescheinigung: "Persönliches/Bescheinigungen"
  Vollmacht: "Persönliches/Vollmachten"
  Antrag: "Behörden/{correspondent}"
  Information: "Sonstiges/{correspondent}"

# Vorlage für Dokumenttypen, die oben fehlen, und für Dokumente ohne Typ.
default: "Paperless-Import/{document_type}/{correspondent}"

# Speicherpfade mit eigener Vorlage; sie haben Vorrang vor dem Dokumenttyp.
# Beispiel:
#   storage_paths:
#     Gemeinsam: "Haushalt/{document_type}"
storage_paths: {}

# Korrespondenten für Ordner, Titel und Absender umbenennen (Paperless-Name: neuer Name).
# Beispiel:
#   correspondents:
#     "Alza.cz a.s.": "Alza"
correspondents: {}

# Ersatzwerte für fehlende Angaben
missing_correspondent: "Allgemein"
missing_document_type: "Ohne Typ"
missing_year: "Ohne Datum"

# Tags, die nicht übernommen werden (Posteingangs-Tags aus Paperless entfallen immer).
skip_tags: []

# Welche Datei übernommen wird:
#   auto      das Original, wenn jede Seite Text hat (unverändert, digitale Signaturen
#             bleiben gültig), sonst die Archivfassung (PDF/A mit OCR); fehlt sie,
#             wird das Original per OCR erkannt
#   archive   immer die Archivfassung, falls vorhanden
#   original  immer das Original (Seiten ohne Text werden dann erneut per OCR erkannt)
file: auto
"""

# SHA-256 of default mapping files written by earlier versions. Such a file was
# never edited, so the import can point out that the defaults have changed since.
# When DEFAULT_MAPPING changes, add the hash of the previous text here.
_OLD_DEFAULT_MAPPINGS = frozenset({"2864b1c0741ade305b855f1fdb3459a7256c3d66b3f41941c18e5831411525dd"})

# Notes for the report and the item log when the original is protected: (chosen file, protection) -> German text.
_PROTECTION_NOTES = {
    ("original", "signed"): (
        "Original ist digital signiert – wird unverändert übernommen, damit die Signatur gültig bleibt"
    ),
    ("original", "encrypted"): (
        "Original ist verschlüsselt – wird unverändert übernommen, der Inhalt ist nicht durchsuchbar"
    ),
    ("archive", "signed"): (
        "Original ist digital signiert, hat aber Seiten ohne Text – übernommen wird die Archivfassung, "
        "deren Signatur nicht mehr gilt"
    ),
    ("archive", "encrypted"): "Original ist verschlüsselt – übernommen wird die Archivfassung",
}


# ---------------------------------------------------------------------------
# Data


@dataclass
class PaperlessDocument:
    """One document of the export with all references resolved to names."""

    pk: int
    title: str
    created: date | None = None
    added: str | None = None
    correspondent: str | None = None
    document_type: str | None = None
    storage_path: str | None = None
    owner: str | None = None
    tags: list[str] = field(default_factory=list)
    inbox_tags: list[str] = field(default_factory=list)
    checksum: str | None = None
    archive_checksum: str | None = None
    original: Path | None = None
    archive: Path | None = None
    original_name: str | None = None  # as listed in the manifest (for messages)
    archive_name: str | None = None
    original_filename: str | None = None  # file name at upload time
    mime_type: str | None = None
    archive_serial_number: int | None = None
    page_count: int | None = None
    notes: list[dict[str, Any]] = field(default_factory=list)
    custom_fields: list[dict[str, Any]] = field(default_factory=list)
    deleted: bool = False
    version_of: int | None = None


@dataclass
class PaperlessExport:
    directory: Path
    version: str | None
    documents: list[PaperlessDocument]
    correspondents: dict[int, str] = field(default_factory=dict)
    document_types: dict[int, str] = field(default_factory=dict)
    tags: dict[int, str] = field(default_factory=dict)
    storage_paths: dict[int, str] = field(default_factory=dict)
    users: dict[int, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def storage_path_names(self) -> list[str]:
        return sorted(set(self.storage_paths.values()), key=str.casefold)

    def owner_names(self) -> list[str]:
        return sorted(set(self.users.values()), key=str.casefold)


@dataclass(frozen=True)
class PdfFacts:
    """Text layer and protection of a PDF in the export."""

    pages: int = 0  # pages whose text was read (0: the text could not be read)
    text_pages: int = 0  # pages with a text layer
    protection: str | None = None  # "encrypted" (needs a password) | "signed" | None

    @property
    def all_text(self) -> bool:
        """Every page has text, so OCR would have nothing left to do."""
        return self.pages > 0 and self.text_pages == self.pages


@dataclass(frozen=True)
class _FileChoice:
    path: Path
    kind: str  # "original" | "archive"
    facts: PdfFacts | None  # None for images


@dataclass
class PlannedDocument:
    """What the import will do with one document."""

    document: PaperlessDocument
    action: str  # "import" | "skip"
    folder: str
    title: str
    doc_date: date | None
    tags: list[str]
    sender: str | None
    new_folder: bool
    rule: str  # German description of the mapping that produced the folder
    reason: str | None = None  # German, why it is skipped
    file: Path | None = None
    file_kind: str | None = None  # "original" | "archive"
    origin_hash: str | None = None
    has_text: bool = False  # every page of the chosen file has text (OCR is skipped)
    warnings: list[str] = field(default_factory=list)
    suggestion: Suggestion | None = None
    mapped_folder: str | None = None  # folder from the mapping, before matching existing folders
    protection: str | None = None  # "encrypted" | "signed" (the file is passed through unchanged)
    ocr: bool = False  # OCR will run when the item is processed
    ocr_note: str | None = None  # German detail for the report, e.g. "kein Text"
    manual: bool = False  # cannot be imported, the user has to take it over by hand
    from_plan: bool = False  # folder/title/date come from a reviewed import plan

    @property
    def skipped(self) -> bool:
        return self.action != "import"

    @property
    def processing(self) -> str:
        """German summary for the report, e.g. "Original, mit OCR (kein Text)"."""
        if self.file is None:
            return ""
        kind = "Archivfassung" if self.file_kind == "archive" else "Original"
        text = f"{kind}, {'mit' if self.ocr else 'ohne'} OCR"
        return f"{text} ({self.ocr_note})" if self.ocr_note else text


@dataclass
class ImportEntry:
    paperless_id: int
    title: str
    status: str  # imported | filed | skipped | failed | planned
    message: str = ""
    folder: str | None = None
    item_id: str | None = None
    doc_id: str | None = None
    processing: str = ""  # German: which file is taken and whether OCR runs
    ocr: bool | None = None  # OCR runs for this document (None: not imported)
    manual: bool = False  # unsupported file type, to be taken over by hand
    source_file: str | None = None  # the file in the export (for documents taken over by hand)


_STATUS_LABELS = {
    "imported": "im Eingang",
    "filed": "abgelegt",
    "skipped": "übersprungen",
    "failed": "fehlgeschlagen",
    "planned": "geplant",
}


@dataclass
class ImportReport:
    mode: str
    dry_run: bool
    started_at: str
    entries: list[ImportEntry] = field(default_factory=list)
    finished_at: str | None = None

    def count(self, status: str) -> int:
        return sum(1 for entry in self.entries if entry.status == status)

    @property
    def imported(self) -> int:
        return self.count("imported") + self.count("filed")

    @property
    def skipped(self) -> int:
        return self.count("skipped")

    @property
    def failed(self) -> int:
        return self.count("failed")

    @property
    def planned(self) -> int:
        return self.count("planned")

    @property
    def item_ids(self) -> list[str]:
        return [entry.item_id for entry in self.entries if entry.item_id]

    @property
    def manual_entries(self) -> list[ImportEntry]:
        """Documents with a file type DocuShelf cannot take over; the user copies them by hand."""
        return [entry for entry in self.entries if entry.manual]

    @property
    def with_ocr(self) -> int:
        """Imported (or planned) documents that go through OCR."""
        return sum(1 for entry in self.entries if entry.ocr and entry.status in ("planned", "imported", "filed"))

    def summary(self) -> str:
        """One German sentence for the command line or the UI."""
        skipped = f"{self.skipped} übersprungen"
        manual = len(self.manual_entries)
        if manual:
            skipped += f" (davon {manual} von Hand zu übernehmen)"
        if self.dry_run:
            return (
                f"Probelauf: {self.planned} Dokument(e) würden übernommen, "
                f"{skipped}. Es wurde nichts verändert."
            )
        target = "abgelegt" if self.mode == "archive" else "in den Eingang übernommen"
        text = f"Paperless-Import: {self.imported} Dokument(e) {target}, {skipped}"
        if self.mode == "archive" and self.count("imported"):
            text += f" (davon {self.count('imported')} zur Prüfung im Eingang)"
        if self.failed:
            text += f", {self.failed} fehlgeschlagen"
        return text + "."

    def lines(self) -> list[str]:
        """One German line per document."""
        result = []
        for entry in self.entries:
            label = _STATUS_LABELS.get(entry.status, entry.status)
            target = f" → {entry.folder}" if entry.folder else ""
            processing = f" – {entry.processing}" if entry.processing else ""
            message = f" ({entry.message})" if entry.message else ""
            result.append(f"[{label}] #{entry.paperless_id} {entry.title}{target}{processing}{message}")
        return result

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.update(
            imported=self.imported,
            skipped=self.skipped,
            failed=self.failed,
            planned=self.planned,
            with_ocr=self.with_ocr,
            manual=len(self.manual_entries),
        )
        return data


# ---------------------------------------------------------------------------
# Mapping file


@dataclass
class ImportMapping:
    document_types: dict[str, str]
    default: str
    storage_paths: dict[str, str] = field(default_factory=dict)
    correspondents: dict[str, str] = field(default_factory=dict)
    missing_correspondent: str = "Allgemein"
    missing_document_type: str = "Ohne Typ"
    missing_year: str = "Ohne Datum"
    skip_tags: set[str] = field(default_factory=set)
    file: str = "auto"

    def __post_init__(self) -> None:
        self._types_exact = {casefold_key(_collapse(k)): v for k, v in self.document_types.items()}
        self._types_loose = {normalize(k): v for k, v in self.document_types.items() if normalize(k)}
        self._paths_exact = {casefold_key(_collapse(k)): v for k, v in self.storage_paths.items()}
        self._renames = {casefold_key(_collapse(k)): v for k, v in self.correspondents.items()}

    def template_for(self, document: PaperlessDocument) -> tuple[str, str]:
        """``(template, German explanation)`` for a document."""
        return self.template(storage_path=document.storage_path, document_type=document.document_type)

    def template(self, *, storage_path: str | None, document_type: str | None) -> tuple[str, str]:
        """``(template, German explanation)`` for a storage path and document type."""
        if storage_path:
            template = self._paths_exact.get(casefold_key(_collapse(storage_path)))
            if template is not None:
                return template, f"Speicherpfad „{storage_path}“"
        if document_type:
            key = casefold_key(_collapse(document_type))
            template = self._types_exact.get(key) or self._types_loose.get(normalize(document_type))
            if template is not None:
                return template, f"Dokumenttyp „{document_type}“"
            return self.default, f"Standardvorlage (Dokumenttyp „{document_type}“ ist nicht zugeordnet)"
        return self.default, "Standardvorlage (ohne Dokumenttyp)"

    def correspondent_name(self, name: str | None) -> str | None:
        if not name:
            return None
        return self._renames.get(casefold_key(_collapse(name))) or name


def load_mapping(path: Path) -> ImportMapping:
    """Read and validate ``paperless-import.yaml`` (German error messages)."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{MAPPING_FILENAME} ist kein gültiges YAML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{MAPPING_FILENAME} kann nicht gelesen werden: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{MAPPING_FILENAME} muss eine Zuordnung (Schlüssel: Wert) enthalten")

    def mapping_of(key: str) -> dict[str, str]:
        value = data.get(key) or {}
        if not isinstance(value, dict):
            raise ConfigError(f"{MAPPING_FILENAME}: „{key}“ muss eine Zuordnung sein")
        result = {}
        for name, template in value.items():
            if not isinstance(template, str) or not str(name).strip():
                raise ConfigError(f"{MAPPING_FILENAME}: Eintrag „{name}“ unter „{key}“ braucht eine Ordnervorlage als Text")
            result[str(name).strip()] = template
        return result

    def text_of(key: str, default: str) -> str:
        value = data.get(key, default)
        if value is None:
            return default
        if not isinstance(value, (str, int)):
            raise ConfigError(f"{MAPPING_FILENAME}: „{key}“ muss ein Text sein")
        return str(value)

    document_types = mapping_of("document_types")
    storage_paths = mapping_of("storage_paths")
    correspondents = {}
    for name, renamed in mapping_of("correspondents").items():
        if not renamed.strip():
            raise ConfigError(f"{MAPPING_FILENAME}: neuer Name für „{name}“ fehlt")
        correspondents[name] = renamed.strip()
    default = text_of("default", "Paperless-Import/{document_type}/{correspondent}")
    for where, template in (
        [(f"document_types → {k}", v) for k, v in document_types.items()]
        + [(f"storage_paths → {k}", v) for k, v in storage_paths.items()]
        + [("default", default)]
    ):
        _check_template(template, where)
    skip_tags_raw = data.get("skip_tags") or []
    if not isinstance(skip_tags_raw, list):
        raise ConfigError(f"{MAPPING_FILENAME}: „skip_tags“ muss eine Liste sein")
    preference = text_of("file", "auto").strip().lower()
    if preference not in FILE_PREFERENCES:
        raise ConfigError(f"{MAPPING_FILENAME}: „file“ muss auto, archive oder original sein")
    return ImportMapping(
        document_types=document_types,
        default=default,
        storage_paths=storage_paths,
        correspondents=correspondents,
        missing_correspondent=_safe_component(text_of("missing_correspondent", "Allgemein"), "Allgemein"),
        missing_document_type=_safe_component(text_of("missing_document_type", "Ohne Typ"), "Ohne Typ"),
        missing_year=_safe_component(text_of("missing_year", "Ohne Datum"), "Ohne Datum"),
        skip_tags={casefold_key(str(tag).strip()) for tag in skip_tags_raw if str(tag).strip()},
        file=preference,
    )


def _check_template(template: str, where: str) -> None:
    try:
        names = [name for _, name, _, _ in string.Formatter().parse(template) if name is not None]
    except ValueError as exc:
        raise ConfigError(f"{MAPPING_FILENAME}: Vorlage „{template}“ ({where}) ist ungültig: {exc}") from exc
    for name in names:
        if name not in PLACEHOLDERS:
            allowed = ", ".join("{" + p + "}" for p in PLACEHOLDERS)
            raise ConfigError(
                f"{MAPPING_FILENAME}: unbekannter Platzhalter {{{name}}} in „{template}“ ({where}); erlaubt: {allowed}"
            )


def render_folder(template: str, values: dict[str, str]) -> str:
    """Fill a folder template; components that end up empty are dropped."""
    parts = []
    for component in template.replace("\\", "/").split("/"):
        try:
            text = component.format_map(values).strip()
        except (KeyError, ValueError, IndexError):
            text = component.strip()
        if not text:
            continue
        try:
            parts.append(sanitize_component(text))
        except PathError:
            continue
    return "/".join(parts)


def map_folder(
    mapping: ImportMapping,
    *,
    correspondent: str | None,
    document_type: str | None,
    storage_path: str | None,
    created: date | None,
) -> tuple[str, str]:
    """Archive folder for a Paperless document and a German note on the template used.

    ``correspondent`` is the name in Paperless (renames from the mapping are
    applied here). The folder is sanitized but not yet matched against
    existing folders.
    """
    values = {
        "correspondent": _safe_component(mapping.correspondent_name(correspondent), mapping.missing_correspondent),
        "document_type": _safe_component(document_type, mapping.missing_document_type),
        "year": str(created.year) if created else mapping.missing_year,
        "storage_path": _safe_component(storage_path, ""),
    }
    template, rule = mapping.template(storage_path=storage_path, document_type=document_type)
    try:
        folder = sanitize_folder(render_folder(template, values)) or FALLBACK_FOLDER
    except PathError:
        folder = FALLBACK_FOLDER
    return folder, rule


def _suggestion_reasons(document_type: str | None, sender: str | None, rule: str) -> list[str]:
    type_text = f"Typ {document_type}" if document_type else "ohne Typ"
    sender_text = f"Korrespondent {sender}" if sender else "ohne Korrespondent"
    return [f"aus Paperless: {type_text}, {sender_text}", f"Ordner nach {rule}"]


_mapping_cache: dict[Path, tuple[tuple[int, int, int], ImportMapping | None]] = {}
_mapping_cache_lock = threading.Lock()


def cached_mapping(path: Path) -> ImportMapping | None:
    """The mapping in ``path``, read again only when the file changed.

    None if the file is missing or invalid (the problem is logged once per
    version of the file).
    """
    path = Path(path)
    try:
        stat = os.stat(path)
    except OSError:
        return None
    key = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
    with _mapping_cache_lock:
        cached = _mapping_cache.get(path)
    if cached is not None and cached[0] == key:
        return cached[1]
    mapping: ImportMapping | None
    try:
        mapping = load_mapping(path)
    except ConfigError as exc:
        log.warning("ignoring %s: %s", path, exc)
        mapping = None
    with _mapping_cache_lock:
        _mapping_cache[path] = (key, mapping)
    return mapping


def recompute_import_suggestion(context: Mapping[str, Any], config_dir: Path) -> dict[str, Any] | None:
    """The import suggestion of an inbox item with the folder from the current mapping.

    Title, date, tags and sender stay as stored at import time; only the folder
    is derived again from ``context["paperless"]`` and
    ``<config_dir>/paperless-import.yaml``. When the mapping still yields the
    folder it gave at import time, the stored folder is kept as it is (it may
    carry the spelling of an existing folder or a folder renamed in the app
    since). Returns None without Paperless data or stored suggestion, and the
    stored suggestion unchanged if the mapping file is missing or invalid.
    Never raises.
    """
    try:
        paperless = context.get("paperless")
        stored = context.get("import_suggestion")
        if not isinstance(paperless, Mapping) or not isinstance(stored, Mapping):
            return None
        result = _copy_suggestion(stored)
        if paperless.get("plan"):
            return result
        mapping = cached_mapping(Path(config_dir) / MAPPING_FILENAME)
        if mapping is None:
            return result
        document_type = _str_or_none(paperless.get("document_type"))
        folder, rule = map_folder(
            mapping,
            correspondent=_str_or_none(paperless.get("correspondent")),
            document_type=document_type,
            storage_path=_str_or_none(paperless.get("storage_path")),
            created=_parse_created(paperless.get("created")),
        )
        before = paperless.get("mapped_folder") or stored.get("folder") or ""
        if casefold_key(folder) == casefold_key(str(before)):
            return result
        result.update(
            folder=folder,
            source="import",
            score=IMPORT_SCORE,
            reasons=_suggestion_reasons(document_type, _str_or_none(stored.get("sender")), rule),
            new_folder=True,  # the suggestion engine checks against the existing folders
        )
        return result
    except Exception:
        log.warning("cannot recompute the import suggestion", exc_info=True)
        try:
            stored = context.get("import_suggestion")
            return _copy_suggestion(stored) if isinstance(stored, Mapping) else None
        except Exception:
            return None


def _copy_suggestion(data: Mapping[str, Any]) -> dict[str, Any]:
    return {key: list(value) if isinstance(value, list) else value for key, value in data.items()}


# ---------------------------------------------------------------------------
# Titles

_SEPARATORS = r"[\s_\-–—.,:;|]"
_DATE_PREFIX_RE = re.compile(
    r"^\s*(?P<date>\d{4}[-_.]\d{1,2}[-_.]\d{1,2}|\d{8}|\d{1,2}\.\d{1,2}\.\d{4})"
    r"(?:[T_]\d{2}[-_.:]?\d{2}(?:[-_.:]?\d{2})?|\s\d{1,2}:\d{2}(?::\d{2})?)?"
    rf"(?=$|{_SEPARATORS})(?:{_SEPARATORS})*"
)


def _valid_date_prefix(text: str) -> bool:
    digits = re.findall(r"\d+", text)
    try:
        if len(digits) == 1 and len(digits[0]) == 8:
            year, month, day = int(digits[0][:4]), int(digits[0][4:6]), int(digits[0][6:])
        elif len(digits[0]) == 4:
            year, month, day = (int(d) for d in digits[:3])
        else:
            day, month, year = (int(d) for d in digits[:3])
        date(year, month, day)
    except (ValueError, IndexError):
        return False
    return 1900 <= year <= 2100


def clean_title(
    title: str | None,
    *,
    correspondent: str | None = None,
    document_type: str | None = None,
    aliases: Iterable[str] = (),
) -> str:
    """Remove leading date and correspondent prefixes that only repeat metadata.

    ``"2023-05-01 - Alza - Rechnung 4711"`` → ``"Rechnung 4711"``. Titles that
    are empty afterwards or look like scanner file names fall back to
    "<Dokumenttyp> <Korrespondent>".
    """
    text = unicodedata.normalize("NFC", (title or "").strip())
    names = [n for n in [correspondent, *aliases] if n and n.strip()]
    patterns = [
        re.compile(rf"^\s*{_flexible(name)}(?=$|{_SEPARATORS})(?:{_SEPARATORS})*", re.IGNORECASE) for name in names
    ]
    for _ in range(4):
        changed = False
        match = _DATE_PREFIX_RE.match(text)
        if match and _valid_date_prefix(match.group("date")):
            text, changed = text[match.end() :], True
        for pattern in patterns:
            match = pattern.match(text)
            if match and match.end() > 0:
                text, changed = text[match.end() :], True
        if not changed:
            break
    text = text.strip(" -_–—.,:;|")
    if "_" in text and " " not in text:
        text = text.replace("_", " ")  # titles taken over from file names
    text = re.sub(r"\s+", " ", text).strip(" -_–—.,:;|")
    if text and title_from_original_name(text + ".pdf") is not None:
        return text
    fallback = " ".join(part.strip() for part in (document_type, correspondent) if part and part.strip())
    return fallback or text or "Dokument"


def _flexible(name: str) -> str:
    """Regex for a name with flexible whitespace."""
    return r"\s+".join(re.escape(part) for part in name.split())


# ---------------------------------------------------------------------------
# The importer


@dataclass(frozen=True)
class PlanOverride:
    """Reviewed filing values for one Paperless document (from an import plan file)."""

    pk: int
    folder: str
    title: str
    doc_date: date | None = None
    sender: str | None = None
    tags: tuple[str, ...] | None = None  # None keeps the Paperless tags


def load_import_plan(path: Path) -> dict[int, PlanOverride]:
    """Read an import plan: ``{"documents": [{"pk", "folder", "title", "doc_date", "sender", "tags"}]}``.

    ``folder`` and ``title`` are required; ``doc_date`` (YYYY-MM-DD), ``sender`` and
    ``tags`` are optional (missing: the Paperless values are used). Raises
    ConfigError with a German message for anything malformed, so a broken plan
    never files documents into unintended places.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"Zuordnungsplan {path} nicht gefunden") from None
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Zuordnungsplan {path} ist nicht lesbar: {exc}") from None
    entries = data.get("documents") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ConfigError("Zuordnungsplan: es fehlt die Liste „documents“")
    result: dict[int, PlanOverride] = {}
    for number, entry in enumerate(entries, start=1):
        where = f"Zuordnungsplan, Eintrag {number}"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where}: muss ein Objekt sein")
        pk = entry.get("pk")
        if not isinstance(pk, int) or isinstance(pk, bool):
            raise ConfigError(f"{where}: „pk“ muss die Paperless-Dokumentnummer sein")
        if pk in result:
            raise ConfigError(f"{where}: Dokument #{pk} steht doppelt im Plan")
        folder, title = entry.get("folder"), entry.get("title")
        if not isinstance(folder, str) or not folder.strip():
            raise ConfigError(f"{where} (#{pk}): „folder“ fehlt")
        try:
            folder = sanitize_folder(folder.strip())
        except PathError as exc:
            raise ConfigError(f"{where} (#{pk}): ungültiger Ordner „{folder}“: {exc}") from None
        if not folder:
            raise ConfigError(f"{where} (#{pk}): „folder“ ist leer")
        if not isinstance(title, str) or not title.strip():
            raise ConfigError(f"{where} (#{pk}): „title“ fehlt")
        raw_date = entry.get("doc_date")
        doc_date = None
        if raw_date not in (None, ""):
            try:
                doc_date = date.fromisoformat(str(raw_date))
            except ValueError:
                raise ConfigError(f"{where} (#{pk}): „doc_date“ muss JJJJ-MM-TT sein") from None
        sender = entry.get("sender")
        if sender is not None and not isinstance(sender, str):
            raise ConfigError(f"{where} (#{pk}): „sender“ muss Text sein")
        tags = entry.get("tags")
        if tags is not None and (not isinstance(tags, list) or not all(isinstance(t, str) for t in tags)):
            raise ConfigError(f"{where} (#{pk}): „tags“ muss eine Liste von Texten sein")
        result[pk] = PlanOverride(
            pk=pk,
            folder=folder,
            title=" ".join(title.split()),
            doc_date=doc_date,
            sender=(" ".join(sender.split()) or None) if isinstance(sender, str) else None,
            tags=tuple(t.strip() for t in tags if t.strip()) if tags is not None else None,
        )
    return result


class PaperlessImporter:
    def __init__(self, services: "Services", *, mapping_path: Path | None = None):
        self.services = services
        self.mapping_path = Path(mapping_path) if mapping_path else services.layout.config / MAPPING_FILENAME
        self._run_lock = threading.Lock()

    # -- mapping ------------------------------------------------------------------------------
    def mapping(self) -> ImportMapping:
        """The folder mapping; the file is created with defaults if it does not exist."""
        self.ensure_mapping_file()
        return load_mapping(self.mapping_path)

    def mapping_is_old_default(self) -> bool:
        """True if the mapping file is an untouched copy of the defaults of an earlier version."""
        try:
            digest = hashlib.sha256(self.mapping_path.read_bytes()).hexdigest()
        except OSError:
            return False
        return digest in _OLD_DEFAULT_MAPPINGS

    def ensure_mapping_file(self) -> Path:
        path = self.mapping_path
        if path.exists():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = fsops.temp_name_for(path)
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(DEFAULT_MAPPING)
                handle.flush()
                os.fsync(handle.fileno())
            fsops.place_exact(tmp, path, move=True)
            log.info("created %s with default mapping", path)
        except FileExistsError:
            pass  # created concurrently; never overwrite
        finally:
            if tmp.exists():
                tmp.unlink()
        return path

    # -- reading the export ------------------------------------------------------------------------
    def load(self, export_dir: str | Path) -> PaperlessExport:
        """Read the manifest(s) of an export directory."""
        directory = Path(export_dir).expanduser()
        if directory.is_file() and directory.suffix.lower() == ".zip":
            raise ConfigError("Der Export ist ein ZIP-Archiv – bitte zuerst entpacken und den Ordner angeben")
        if not directory.is_dir():
            raise NotFound(f"Exportverzeichnis nicht gefunden: {directory}")
        manifests = _manifest_files(directory)
        if not manifests:
            raise NotFound(f"In {directory} liegt keine manifest.json – ist das ein Paperless-Export?")
        records: list[dict[str, Any]] = []
        for path in manifests:
            records.extend(_read_manifest(path))
        export = _build_export(directory, records, _read_version(directory))
        log.info(
            "loaded Paperless export %s: %d documents, version %s", directory, len(export.documents), export.version or "?"
        )
        return export

    # -- planning --------------------------------------------------------------------------------------
    def plan(
        self,
        export: PaperlessExport,
        *,
        storage_paths: Iterable[str] | None = None,
        owners: Iterable[str] | None = None,
        include_deleted: bool = False,
        overrides: Mapping[int, PlanOverride] | None = None,
        only_planned: bool = False,
    ) -> list[PlannedDocument]:
        """Decide folder, title and file for every selected document.

        ``storage_paths`` and ``owners`` restrict the import to documents with one
        of the given storage path names / owner user names (case-insensitive;
        ``"-"`` selects documents without storage path / owner). Documents that
        were deleted in DocuShelf and still wait in the trash are skipped unless
        ``include_deleted`` is set. ``overrides`` (from ``load_import_plan``) set
        folder, title, date, sender and tags per document; with ``only_planned``
        documents missing from them are skipped.
        """
        mapping = self.mapping()
        overrides = overrides or {}
        path_filter = _resolve_filter(storage_paths, export.storage_paths, "Speicherpfad", export.storage_path_names())
        owner_filter = _resolve_filter(owners, export.users, "Besitzer", export.owner_names())
        inbox_hashes = self._inbox_hashes()
        deleted_hashes = set() if include_deleted else self._deleted_hashes()
        folder_cache: dict[Path, dict[str, str]] = {}
        planned = []
        for document in sorted(export.documents, key=lambda d: d.pk):
            if path_filter is not None and _filter_key(document.storage_path) not in path_filter:
                continue
            if owner_filter is not None and _filter_key(document.owner) not in owner_filter:
                continue
            override = overrides.get(document.pk)
            entry = self._plan_document(document, mapping, inbox_hashes, deleted_hashes, folder_cache, override)
            if override is None and only_planned and not entry.skipped:
                entry = _skip(entry, "nicht im Zuordnungsplan")
            planned.append(entry)
        return planned

    def _plan_document(
        self,
        document: PaperlessDocument,
        mapping: ImportMapping,
        inbox_hashes: set[str],
        deleted_hashes: set[str],
        folder_cache: dict[Path, dict[str, str]],
        override: PlanOverride | None = None,
    ) -> PlannedDocument:
        correspondent = mapping.correspondent_name(document.correspondent)
        mapped, rule = map_folder(
            mapping,
            correspondent=document.correspondent,
            document_type=document.document_type,
            storage_path=document.storage_path,
            created=document.created,
        )
        folder, exists = self._existing_folder(mapped, folder_cache)
        if not exists:
            exists = self.services.index.folder_exists(folder)
        aliases = [document.correspondent] if correspondent != document.correspondent else []
        title = clean_title(
            document.title, correspondent=correspondent, document_type=document.document_type, aliases=aliases
        )
        tags = [tag for tag in document.tags if casefold_key(tag) not in mapping.skip_tags]
        doc_date = document.created
        sender = correspondent
        if override is not None:
            mapped, rule = override.folder, "Zuordnungsplan"
            folder, exists = self._existing_folder(override.folder, folder_cache)
            if not exists:
                exists = self.services.index.folder_exists(folder)
            title = override.title
            doc_date = override.doc_date or document.created
            if override.sender is not None:
                sender = override.sender
            if override.tags is not None:
                tags = list(override.tags)
        planned = PlannedDocument(
            document=document,
            action="import",
            folder=folder,
            title=title,
            doc_date=doc_date,
            tags=tags,
            sender=sender,
            new_folder=not exists,
            rule=rule,
            mapped_folder=mapped,
            from_plan=override is not None,
        )
        if document.deleted:
            return _skip(planned, "in Paperless gelöscht (Papierkorb)")
        if document.version_of is not None:
            return _skip(planned, f"ältere Fassung von Dokument #{document.version_of}")
        choice = self._choose_file(document, mapping.file, planned.warnings)
        if choice is None:
            if document.original is None and document.archive is None:
                missing = document.original_name or document.archive_name or "?"
                return _skip(planned, f"Datei fehlt im Export: {missing}")
            planned.manual = True
            mime = document.mime_type or "unbekannt"
            return _skip(planned, f"Dateityp wird nicht unterstützt ({mime}) – bitte von Hand übernehmen")
        planned.file, planned.file_kind = choice.path, choice.kind
        planned.has_text = choice.facts is not None and choice.facts.all_text
        planned.protection = choice.facts.protection if choice.facts is not None else None
        planned.ocr, planned.ocr_note = self._ocr_plan(choice)
        try:
            planned.origin_hash = self._origin_hash(document, planned.file, planned.warnings)
        except OSError as exc:
            return _skip(planned, f"Datei nicht lesbar: {exc}")
        known = self._known(planned, inbox_hashes, deleted_hashes)
        if known:
            return _skip(planned, known)
        planned.suggestion = self._suggestion(planned)
        return planned

    def _choose_file(self, document: PaperlessDocument, preference: str, warnings: list[str]) -> _FileChoice | None:
        """The file to import: in ``auto`` mode the original only if every page has text.

        Otherwise the archive version (Paperless ran OCR on every page) is
        preferred; without one the original is taken and goes through OCR.
        Encrypted originals cannot be read, so they count as without text.
        """
        original_kind = _file_kind(document.original)
        archive_ok = _file_kind(document.archive) == "pdf"
        if document.original is None and document.original_name:
            warnings.append(f"Original fehlt im Export ({document.original_name})")
        if document.archive is None and document.archive_name:
            warnings.append(f"Archivfassung fehlt im Export ({document.archive_name})")
        original_facts: PdfFacts | None = None
        if original_kind == "pdf" and preference in ("auto", "original"):
            original_facts = inspect_pdf(document.original)
        archive_first = preference == "archive" or (
            preference == "auto" and not (original_facts is not None and original_facts.all_text)
        )
        available = {"original": original_kind is not None, "archive": archive_ok}
        order = ("archive", "original") if archive_first else ("original", "archive")
        kind = next((candidate for candidate in order if available[candidate]), None)
        if kind is None:
            return None
        path = document.original if kind == "original" else document.archive
        if path is None:
            return None
        if kind == "archive":
            facts = inspect_pdf(path)
        elif original_kind == "pdf":
            facts = original_facts if original_facts is not None else inspect_pdf(path)
        else:
            facts = None  # an image
        if original_facts is not None and original_facts.protection:
            warnings.append(_PROTECTION_NOTES[(kind, original_facts.protection)])
        return _FileChoice(path=path, kind=kind, facts=facts)

    def _ocr_plan(self, choice: _FileChoice) -> tuple[bool, str | None]:
        """Whether processing will run OCR on the chosen file, and a German detail for the report."""
        facts = choice.facts
        if facts is not None and facts.all_text:
            return False, None
        if facts is not None and facts.protection == "encrypted":
            return False, "verschlüsselt"
        if facts is not None and facts.protection == "signed":
            return False, "digital signiert"
        if not getattr(self.services.settings, "ocr_enabled", True):
            return False, "Texterkennung ausgeschaltet"
        if facts is None:
            return True, "Bild"
        if facts.text_pages:
            return True, f"{facts.pages - facts.text_pages} von {facts.pages} Seiten ohne Text"
        return True, "kein Text"

    def _origin_hash(self, document: PaperlessDocument, chosen: Path, warnings: list[str]) -> str:
        """SHA-256 of the original, checked against the checksum Paperless recorded."""
        expected = (document.checksum or "").strip().lower()
        if document.original is not None:
            actual = fsops.sha256_file(document.original)
            if len(expected) == 64 and expected != actual:
                warnings.append("Prüfsumme des Originals weicht von Paperless ab")
            elif len(expected) == 32 and _md5_file(document.original) != expected:
                warnings.append("Prüfsumme (MD5) des Originals weicht von Paperless ab")
            return actual
        if re.fullmatch(r"[0-9a-f]{64}", expected):
            return expected
        return fsops.sha256_file(chosen)

    def _known(self, planned: PlannedDocument, inbox_hashes: set[str], deleted_hashes: set[str]) -> str | None:
        index = self.services.index
        origin = planned.origin_hash or ""
        if origin and index.by_origin_hash(origin):
            return "bereits im Archiv"
        if origin and origin in inbox_hashes:
            return "bereits im Eingang"
        contents = {origin}
        if planned.file is not None and planned.file != planned.document.original:
            contents.add(fsops.sha256_file(planned.file))
        for digest in contents:
            if digest and index.by_content_hash(digest):
                return "Datei liegt bereits im Archiv"
        if origin and origin.lower() in deleted_hashes:
            return DELETED_REASON
        return None

    def _suggestion(self, planned: PlannedDocument) -> Suggestion:
        document = planned.document
        reasons = _suggestion_reasons(document.document_type, planned.sender, planned.rule)
        return Suggestion(
            folder=planned.folder,
            title=planned.title,
            doc_date=planned.doc_date.isoformat() if planned.doc_date else None,
            tags=list(planned.tags),
            sender=planned.sender,
            score=IMPORT_SCORE,
            source="import",
            reasons=reasons,
            new_folder=planned.new_folder,
        )

    def _existing_folder(self, folder: str, cache: dict[Path, dict[str, str]]) -> tuple[str, bool]:
        """Reuse existing folders whose names differ only in case; ``(folder, exists)``."""
        current = self.services.layout.archive
        resolved: list[str] = []
        exists = True
        for part in folder.split("/") if folder else []:
            match = None
            if exists:
                names = cache.get(current)
                if names is None:
                    names = {}
                    try:
                        with os.scandir(current) as entries:
                            for entry in entries:
                                if entry.is_dir(follow_symlinks=False):
                                    names.setdefault(casefold_key(entry.name), entry.name)
                    except OSError:
                        pass
                    cache[current] = names
                match = names.get(casefold_key(part))
            if match is None:
                exists = False
                match = part
            resolved.append(match)
            current = current / match
        return "/".join(resolved), exists

    def _inbox_hashes(self) -> set[str]:
        hashes: set[str] = set()
        inbox = self.services.inbox
        for state in ("arbeit", "bereit", "fehler", "erledigt"):
            for item in inbox.iter_state(state):
                if item.origin_hash:
                    hashes.add(item.origin_hash)
        return hashes

    def _deleted_hashes(self) -> set[str]:
        """Origin hashes of inbox items and documents that were deleted and wait in the trash."""
        trash = getattr(self.services, "trash", None)
        if trash is None:
            from ..archive.trash import Trash

            trash = Trash(self.services)
        try:
            entries = trash.list()
        except Exception:
            log.warning("cannot read the trash; deleted documents may be imported again", exc_info=True)
            return set()
        hashes: set[str] = set()
        for entry in entries:
            meta = entry.meta if isinstance(entry.meta, dict) else {}
            digest = meta.get("origin_hash")
            if entry.kind in ("inbox", "document") and isinstance(digest, str) and digest.strip():
                hashes.add(digest.strip().lower())
        return hashes

    # -- running -------------------------------------------------------------------------------------------
    def run(
        self,
        plan: list[PlannedDocument],
        *,
        mode: str = "inbox",
        dry_run: bool = False,
        user: str | None = None,
        progress: Callable[[int, int, ImportEntry], None] | None = None,
    ) -> ImportReport:
        """Carry out a plan.

        ``mode="inbox"`` (default) creates inbox items that wait for
        confirmation. ``mode="archive"`` processes each item right away and
        files it with the planned values (items that cannot be filed stay in
        the inbox). ``dry_run`` only reports what would happen.
        """
        if mode not in ("inbox", "archive"):
            raise ConfigError(f"Unbekannter Importmodus „{mode}“ (erlaubt: inbox, archive)")
        if mode == "archive" and not dry_run:
            missing = [name for name in ("pipeline", "ops") if getattr(self.services, name, None) is None]
            if missing:
                raise ConfigError("Der Archivmodus braucht die vollständige Anwendung (Pipeline und Ablage)")
        report = ImportReport(mode=mode, dry_run=dry_run, started_at=timeutil.now_iso())
        with self._run_lock:
            inbox_hashes = set() if dry_run else self._inbox_hashes()
            total = len(plan)
            for position, planned in enumerate(plan, start=1):
                entry = self._run_one(planned, mode=mode, dry_run=dry_run, user=user, inbox_hashes=inbox_hashes)
                report.entries.append(entry)
                if progress is not None:
                    progress(position, total, entry)
        report.finished_at = timeutil.now_iso()
        if not dry_run:
            self.services.audit.write(
                user,
                "import_paperless",
                mode=mode,
                imported=report.imported,
                skipped=report.skipped,
                failed=report.failed,
            )
            if mode == "inbox" and report.imported:
                self._kick_worker()
        log.info("Paperless import finished: %s", report.summary())
        return report

    def import_export(
        self,
        export_dir: str | Path,
        *,
        storage_paths: Iterable[str] | None = None,
        owners: Iterable[str] | None = None,
        include_deleted: bool = False,
        mode: str = "inbox",
        dry_run: bool = False,
        user: str | None = None,
        progress: Callable[[int, int, ImportEntry], None] | None = None,
    ) -> ImportReport:
        """``load`` + ``plan`` + ``run`` in one call."""
        export = self.load(export_dir)
        plan = self.plan(export, storage_paths=storage_paths, owners=owners, include_deleted=include_deleted)
        return self.run(plan, mode=mode, dry_run=dry_run, user=user, progress=progress)

    def _run_one(
        self, planned: PlannedDocument, *, mode: str, dry_run: bool, user: str | None, inbox_hashes: set[str]
    ) -> ImportEntry:
        document = planned.document
        entry = ImportEntry(paperless_id=document.pk, title=planned.title, status="skipped", folder=planned.folder)
        if planned.skipped:
            entry.message = planned.reason or ""
            if planned.manual:
                entry.manual = True
                entry.source_file = document.original_name or document.archive_name
            return entry
        if dry_run:
            details = ["neuer Ordner"] if planned.new_folder else []
            details.extend(planned.warnings)
            entry.status, entry.message = "planned", ", ".join(details)
            entry.processing, entry.ocr = planned.processing, planned.ocr
            return entry
        origin = planned.origin_hash or ""
        if origin and (origin in inbox_hashes or self.services.index.by_origin_hash(origin)):
            entry.message = "bereits importiert"
            return entry
        try:
            item = self._create_item(planned, mode=mode)
        except Exception as exc:
            log.exception("cannot import Paperless document %s", document.pk)
            entry.status, entry.message = "failed", f"Übernahme fehlgeschlagen: {_describe(exc)}"
            return entry
        entry.processing, entry.ocr = planned.processing, planned.ocr
        inbox_hashes.add(origin)
        entry.item_id = item.id
        if mode == "inbox":
            self.services.index.enqueue("process", item.id)
            entry.status, entry.message = "imported", "wartet im Eingang auf Bestätigung"
            return entry
        return self._file_directly(item, planned, entry, user=user)

    def _create_item(self, planned: PlannedDocument, *, mode: str = "inbox") -> "InboxItem":
        document = planned.document
        assert planned.file is not None and planned.suggestion is not None
        context: dict[str, Any] = {
            "title": planned.title,
            "paperless": self._paperless_context(planned),
            "import_suggestion": planned.suggestion.to_dict(),
        }
        options: dict[str, Any] = {"skip_ocr": planned.has_text, "skip_blank_removal": True}
        if mode == "archive":
            # The importer files these itself; automatic filing must not race it.
            options["no_auto"] = True
        received_at = _iso_datetime(document.added)
        item = self.services.inbox.create(
            SOURCE,
            [planned.file],
            context=context,
            options=options,
            move=False,
            origin_hash=planned.origin_hash,
            received_at=received_at,
        )
        kind = "Archivfassung" if planned.file_kind == "archive" else "Original"
        self.services.inbox.log(item, f"Aus Paperless-ngx übernommen (Dokument #{document.pk}, {kind})")
        for warning in planned.warnings:
            self.services.inbox.log(item, f"Hinweis: {warning}")
        return item

    def _paperless_context(self, planned: PlannedDocument) -> dict[str, Any]:
        document = planned.document
        return {
            "id": document.pk,
            "title": document.title,
            "created": document.created.isoformat() if document.created else None,
            "added": document.added,
            "correspondent": document.correspondent,
            "document_type": document.document_type,
            "storage_path": document.storage_path,
            "owner": document.owner,
            "tags": list(document.tags),
            "archive_serial_number": document.archive_serial_number,
            "original_filename": document.original_filename,
            "mime_type": document.mime_type,
            "page_count": document.page_count,
            "checksum": document.checksum,
            "file": planned.file_kind,
            "notes": [dict(note) for note in document.notes],
            "custom_fields": [dict(value) for value in document.custom_fields],
            "warnings": list(planned.warnings),
            # The folder the mapping gave at import time; recompute_import_suggestion
            # compares with it to see whether the mapping changed for this document.
            "mapped_folder": planned.mapped_folder,
            # Planned values were reviewed by a person: recomputing keeps them.
            "plan": planned.from_plan,
        }

    def _file_directly(self, item: "InboxItem", planned: PlannedDocument, entry: ImportEntry, *, user: str | None) -> ImportEntry:
        pipeline = self.services.pipeline
        ops = self.services.ops
        try:
            processed = pipeline.process(item.id)
        except ProcessingError as exc:
            if exc.transient:
                self.services.index.enqueue("process", item.id)
                entry.status = "imported"
                entry.message = f"Verarbeitung wird später wiederholt: {exc}"
                return entry
            entry.status, entry.message = "failed", f"Verarbeitung fehlgeschlagen: {exc}"
            return entry
        except Exception as exc:
            log.exception("processing of imported item %s failed", item.id)
            entry.status, entry.message = "failed", f"Verarbeitung fehlgeschlagen: {_describe(exc)}"
            return entry
        if processed.error or processed.state == "fehler":
            message = (processed.error or {}).get("message") or "unbekannter Fehler"
            entry.status, entry.message = "failed", f"Verarbeitung fehlgeschlagen: {message} (siehe Eingang → Fehler)"
            return entry
        if processed.stage != "ready":
            entry.status, entry.message = "imported", "Verarbeitung noch nicht abgeschlossen, liegt im Eingang"
            return entry
        if processed.duplicate_of:
            entry.status, entry.message = "imported", "mögliches Duplikat – zur Prüfung im Eingang belassen"
            return entry
        request = FilingRequest(
            folder=planned.folder,
            title=planned.title,
            doc_date=planned.doc_date,
            tags=list(planned.tags),
            sender=planned.sender,
        )
        try:
            record = ops.file_item(item.id, request, user=user)
        except Exception as exc:
            log.warning("filing imported item %s failed: %s", item.id, exc)
            entry.status, entry.message = "failed", f"Ablage fehlgeschlagen, liegt im Eingang: {_describe(exc)}"
            return entry
        entry.status = "filed"
        entry.doc_id = record.id
        entry.folder = record.folder
        entry.message = record.path
        return entry

    def _kick_worker(self) -> None:
        worker = getattr(self.services, "worker", None)
        if worker is None:
            return
        try:
            worker.kick()
        except Exception:
            log.debug("cannot wake the worker", exc_info=True)


# ---------------------------------------------------------------------------
# Manifest parsing


def _manifest_files(directory: Path) -> list[Path]:
    files = []
    main = directory / "manifest.json"
    if main.is_file():
        files.append(main)
    for path in sorted(directory.rglob("*-manifest.json")):
        if path.is_file() and path != main:
            files.append(path)
    return files


def _read_manifest(path: Path) -> list[dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path.name} ist kein gültiges JSON: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path.name} kann nicht gelesen werden: {exc}") from exc
    if not isinstance(data, list):
        raise ConfigError(f"{path.name} ist kein Paperless-Manifest (Liste von Datensätzen erwartet)")
    return [record for record in data if isinstance(record, dict) and "model" in record]


def _read_version(directory: Path) -> str | None:
    path = directory / "metadata.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("version"):
        return str(data["version"])
    return None


def _pk(record: dict[str, Any]) -> int | None:
    try:
        return int(record.get("pk"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _fields(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("fields")
    return value if isinstance(value, dict) else {}


def _build_export(directory: Path, records: list[dict[str, Any]], version: str | None) -> PaperlessExport:
    by_model: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for record in records:
        pk = _pk(record)
        if pk is None:
            continue
        by_model[str(record.get("model", "")).lower()][pk] = record  # later records win (split manifests)

    def names(model: str, key: str = "name") -> dict[int, str]:
        return {pk: str(_fields(r).get(key) or "").strip() for pk, r in by_model[model].items()}

    correspondents = names("documents.correspondent")
    document_types = names("documents.documenttype")
    storage_paths = names("documents.storagepath")
    users = names("auth.user", "username")
    tags = names("documents.tag")
    inbox_tag_ids = {pk for pk, r in by_model["documents.tag"].items() if _fields(r).get("is_inbox_tag")}
    custom_fields = {pk: _fields(r) for pk, r in by_model["documents.customfield"].items()}
    warnings: list[str] = []

    notes: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for record in by_model["documents.note"].values():
        values = _fields(record)
        if values.get("deleted_at"):
            continue
        doc = _int_or_none(values.get("document"))
        text = str(values.get("note") or "").strip()
        if doc is None or not text:
            continue
        notes[doc].append(
            {"text": text, "created": values.get("created"), "user": users.get(_int_or_none(values.get("user")) or -1)}
        )

    field_values: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for record in by_model["documents.customfieldinstance"].values():
        values = _fields(record)
        if values.get("deleted_at"):
            continue
        doc = _int_or_none(values.get("document"))
        definition = custom_fields.get(_int_or_none(values.get("field")) or -1, {})
        value = _custom_value(values, definition)
        if doc is None or value in (None, "", []):
            continue
        field_values[doc].append(
            {"name": str(definition.get("name") or f"Feld {values.get('field')}"), "type": definition.get("data_type"), "value": value}
        )

    documents = []
    listings: dict[Path, dict[str, str]] = {}  # directory -> {NFC name: name on disk}
    for pk, record in by_model["documents.document"].items():
        values = _fields(record)
        original_name = record.get("__exported_file_name__")
        archive_name = record.get("__exported_archive_name__")
        tag_ids = [tid for tid in (_int_or_none(t) for t in values.get("tags") or []) if tid is not None]
        document = PaperlessDocument(
            pk=pk,
            title=str(values.get("title") or "").strip(),
            created=_parse_created(values.get("created") or values.get("created_date")),
            added=_iso_datetime(values.get("added")),
            correspondent=correspondents.get(_int_or_none(values.get("correspondent")) or -1) or None,
            document_type=document_types.get(_int_or_none(values.get("document_type")) or -1) or None,
            storage_path=storage_paths.get(_int_or_none(values.get("storage_path")) or -1) or None,
            owner=users.get(_int_or_none(values.get("owner")) or -1) or None,
            tags=[tags[t] for t in tag_ids if t in tags and t not in inbox_tag_ids and tags[t]],
            inbox_tags=[tags[t] for t in tag_ids if t in inbox_tag_ids and tags.get(t)],
            checksum=_str_or_none(values.get("checksum")),
            archive_checksum=_str_or_none(values.get("archive_checksum")),
            original=_export_file(directory, original_name, warnings, listings),
            archive=_export_file(directory, archive_name, warnings, listings),
            original_name=_str_or_none(original_name),
            archive_name=_str_or_none(archive_name),
            original_filename=_str_or_none(values.get("original_filename")),
            mime_type=_str_or_none(values.get("mime_type")),
            archive_serial_number=_int_or_none(values.get("archive_serial_number")),
            page_count=_int_or_none(values.get("page_count")),
            notes=sorted(notes.get(pk, []), key=lambda n: str(n.get("created") or "")),
            custom_fields=field_values.get(pk, []),
            deleted=bool(values.get("deleted_at")),
            version_of=_int_or_none(values.get("root_document")),
        )
        documents.append(document)
    documents.sort(key=lambda d: d.pk)
    return PaperlessExport(
        directory=directory,
        version=version,
        documents=documents,
        correspondents=correspondents,
        document_types=document_types,
        tags=tags,
        storage_paths=storage_paths,
        users=users,
        warnings=warnings,
    )


_VALUE_FIELDS = {
    "string": "value_text",
    "longtext": "value_long_text",
    "url": "value_url",
    "date": "value_date",
    "boolean": "value_bool",
    "integer": "value_int",
    "float": "value_float",
    "monetary": "value_monetary",
    "documentlink": "value_document_ids",
    "select": "value_select",
}


def _custom_value(values: dict[str, Any], definition: dict[str, Any]) -> Any:
    data_type = str(definition.get("data_type") or "")
    key = _VALUE_FIELDS.get(data_type)
    value = values.get(key) if key else None
    if value in (None, "", []):
        value = next(
            (v for k, v in values.items() if k.startswith("value_") and v not in (None, "", [])),
            None,
        )
    if data_type == "select" and value is not None:
        extra = definition.get("extra_data") or {}
        options = extra.get("select_options") if isinstance(extra, dict) else None
        for index, option in enumerate(options or []):
            if isinstance(option, dict) and str(option.get("id")) == str(value):
                return option.get("label")
            if isinstance(option, str) and str(index) == str(value):
                return option
    return value


def _export_file(
    directory: Path, name: Any, warnings: list[str], listings: dict[Path, dict[str, str]] | None = None
) -> Path | None:
    """Resolve a file name from the manifest inside the export directory.

    The name is tried as written, then in NFC and NFD. Exports copied between
    systems may store names in another Unicode normalization than the manifest,
    sometimes mixed within one name, so as a last resort every path component
    is compared in NFC with the entries of its directory (listings are cached
    in ``listings`` per directory).
    """
    if not name or not isinstance(name, str):
        return None
    root = directory.resolve()

    def inside(path: Path) -> bool:
        if path == root or root in path.parents:
            return True
        warnings.append(f"Pfad außerhalb des Exports ignoriert: {name}")
        return False

    for variant in dict.fromkeys((name, unicodedata.normalize("NFC", name), unicodedata.normalize("NFD", name))):
        candidate = (directory / variant).resolve()
        if not inside(candidate):
            return None
        if candidate.is_file():
            return candidate
    found = _match_normalized(directory, name, {} if listings is None else listings)
    if found is None:
        return None
    candidate = found.resolve()
    if not inside(candidate):
        return None
    return candidate if candidate.is_file() else None


def _match_normalized(directory: Path, name: str, listings: dict[Path, dict[str, str]]) -> Path | None:
    """Walk ``name`` component by component, matching directory entries by their NFC form."""
    current = directory
    for part in name.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        names = listings.get(current)
        if names is None:
            names = {}
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        names.setdefault(unicodedata.normalize("NFC", entry.name), entry.name)
            except OSError:
                pass
            listings[current] = names
        match = names.get(unicodedata.normalize("NFC", part))
        if match is None:
            return None
        current = current / match
    return None if current == directory else current


def _parse_created(value: Any) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return timeutil.parse_date(text)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timeutil.timezone())
    return parsed.date()


def _iso_datetime(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timeutil.timezone())
    return parsed.astimezone(timeutil.timezone()).isoformat(timespec="seconds")


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# ---------------------------------------------------------------------------
# Small helpers


def _collapse(value: str) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def _safe_component(value: str | None, fallback: str) -> str:
    if value and str(value).strip():
        try:
            return sanitize_component(str(value))
        except PathError:
            pass
    return fallback


def _filter_key(value: str | None) -> str:
    return casefold_key(value) if value else NO_VALUE


def _resolve_filter(
    wanted: Iterable[str] | None, known: dict[int, str], label: str, available: list[str]
) -> set[str] | None:
    """Turn filter values (names, ids or "-") into a set of keys; unknown names are an error."""
    if wanted is None:
        return None
    values = [str(value).strip() for value in wanted if str(value).strip()]
    if not values:
        return None
    by_name = {casefold_key(name): name for name in known.values() if name}
    result: set[str] = set()
    for value in values:
        if value == NO_VALUE:
            result.add(NO_VALUE)
            continue
        key = casefold_key(value)
        if key in by_name:
            result.add(key)
            continue
        if value.isdigit() and int(value) in known and known[int(value)]:
            result.add(casefold_key(known[int(value)]))
            continue
        choices = ", ".join(available) or "keine"
        raise ConfigError(f"{label} „{value}“ gibt es im Export nicht (vorhanden: {choices})")
    return result


def _skip(planned: PlannedDocument, reason: str) -> PlannedDocument:
    planned.action = "skip"
    planned.reason = reason
    return planned


def _file_kind(path: Path | None) -> str | None:
    """"pdf", "image" or None (missing/unsupported), judged by content."""
    if path is None:
        return None
    try:
        with open(path, "rb") as handle:
            head = handle.read(2048)
    except OSError:
        return None
    kind = sniff(head)
    if kind == "pdf":
        return "pdf"
    if kind in ("jpeg", "png", "tiff", "webp", "heic"):
        return "image"
    return None


def inspect_pdf(path: Path) -> PdfFacts:
    """Text layer of every page and protection of a PDF; never raises.

    A PDF that needs a password is "encrypted" and nothing can be read from
    it; a digitally signed or certified one is "signed". Damaged files count
    as having no text.
    """
    import pikepdf

    protection = None
    try:
        with pikepdf.open(path) as pdf:
            if _has_signature(pdf):
                protection = "signed"
    except pikepdf.PasswordError:
        return PdfFacts(protection="encrypted")
    except Exception:
        log.debug("cannot inspect %s", path, exc_info=True)
    from ..pdf.text import extract_pages

    try:
        pages = extract_pages(path)
    except Exception:
        log.debug("cannot read the text of %s", path, exc_info=True)
        return PdfFacts(protection=protection)
    return PdfFacts(pages=len(pages), text_pages=sum(1 for page in pages if page.strip()), protection=protection)


def _has_signature(pdf: Any) -> bool:
    """A digital signature or certification that any change to the file would break."""
    import pikepdf

    root = pdf.Root
    if "/Perms" in root:
        return True
    acroform = root.get("/AcroForm")
    if not isinstance(acroform, pikepdf.Dictionary):
        return False
    try:
        if int(acroform.get("/SigFlags", 0)) & 1:
            return True
    except (TypeError, ValueError):
        pass
    fields = acroform.get("/Fields")
    pending = [(field, None) for field in fields] if isinstance(fields, pikepdf.Array) else []
    visited = 0
    while pending and visited < 10_000:  # bounded: field trees in damaged files may loop
        field, inherited = pending.pop()
        visited += 1
        if not isinstance(field, pikepdf.Dictionary):
            continue
        field_type = field.get("/FT", inherited)
        if field_type == pikepdf.Name.Sig and field.get("/V") is not None:
            return True
        kids = field.get("/Kids")
        if isinstance(kids, pikepdf.Array):
            pending.extend((kid, field_type) for kid in kids)
    return False


def _md5_file(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _describe(exc: BaseException) -> str:
    if isinstance(exc, DocuShelfError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"
