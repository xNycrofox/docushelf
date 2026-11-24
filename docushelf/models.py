"""Plain data types shared between modules."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from .util.timeutil import parse_date


@dataclass
class DocMeta:
    """Metadata stored inside a PDF (XMP). Missing values are None/empty."""

    doc_id: str | None = None
    title: str | None = None
    doc_date: date | None = None
    tags: list[str] = field(default_factory=list)
    sender: str | None = None
    origin_hash: str | None = None
    source: str | None = None
    received_at: str | None = None
    filed_at: str | None = None
    # Additional known text properties, e.g. {"ArchiveSerialNumber": "123", "PaperlessId": "42"}.
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def has_identity(self) -> bool:
        return bool(self.doc_id)


@dataclass
class DocumentRecord:
    """One archived file as seen by the index."""

    id: str
    path: str  # relative to the archive root, '/' separated, exactly as on disk
    folder: str  # relative folder ('' = root)
    filename: str
    ext: str  # lower-case with dot, e.g. ".pdf"
    title: str
    doc_date: date | None
    sender: str | None
    tags: list[str]
    origin_hash: str | None
    content_hash: str
    xmp_id: str | None
    has_xmp: bool
    size: int
    mtime_ns: int
    inode: int | None
    page_count: int
    text_status: str  # ok | empty | error | unsupported | pending
    indexed_at: str

    @property
    def is_pdf(self) -> bool:
        return self.ext == ".pdf"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["doc_date"] = self.doc_date.isoformat() if self.doc_date else None
        return data


@dataclass
class Suggestion:
    """A proposed filing target. Several sources contribute; the UI shows the reasons."""

    folder: str | None
    title: str | None = None
    doc_date: str | None = None  # ISO date
    tags: list[str] = field(default_factory=list)
    sender: str | None = None
    score: float = 0.0  # 0..1
    source: str = "heuristic"  # rule | similarity | llm | import | heuristic
    reasons: list[str] = field(default_factory=list)
    new_folder: bool = False
    rule_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Suggestion":
        known = {k: data.get(k) for k in cls.__dataclass_fields__ if k in data}
        known.setdefault("tags", [])
        known.setdefault("reasons", [])
        known["tags"] = list(known.get("tags") or [])
        known["reasons"] = list(known.get("reasons") or [])
        return cls(**known)


@dataclass
class DateCandidate:
    value: str  # ISO date
    text: str  # as found in the document
    score: float
    label: str | None = None  # e.g. "Rechnungsdatum"


@dataclass
class Analysis:
    """Facts extracted from the text of a document."""

    doc_type: str | None = None
    sender: str | None = None
    sender_candidates: list[str] = field(default_factory=list)
    doc_date: str | None = None  # ISO date of the best candidate
    dates: list[DateCandidate] = field(default_factory=list)
    amounts: list[str] = field(default_factory=list)  # "1.234,56 €"
    total_amount: str | None = None
    ibans: list[str] = field(default_factory=list)
    references: dict[str, list[str]] = field(default_factory=dict)  # label -> values
    emails: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    text_chars: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Analysis":
        if not data:
            return cls()
        values = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        values["dates"] = [DateCandidate(**d) if isinstance(d, dict) else d for d in values.get("dates", [])]
        return cls(**values)


@dataclass
class FilingRequest:
    """What the user confirmed for an inbox item."""

    folder: str
    title: str
    doc_date: date | None = None
    tags: list[str] = field(default_factory=list)
    sender: str | None = None
    # Optionally remember the decision as a rule ("documents from this sender always go here").
    create_rule: bool = False

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "FilingRequest":
        tags = data.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]
        return cls(
            folder=str(data.get("folder") or "").strip(),
            title=str(data.get("title") or "").strip(),
            doc_date=parse_date(data.get("doc_date") or data.get("date")),
            tags=[str(t).strip() for t in tags if str(t).strip()],
            sender=(str(data.get("sender")).strip() or None) if data.get("sender") else None,
            create_rule=bool(data.get("create_rule")),
        )


@dataclass
class MetadataUpdate:
    """Changes to an archived document. ``None`` means "leave unchanged"."""

    title: str | None = None
    doc_date: date | None = None
    clear_date: bool = False
    tags: list[str] | None = None
    sender: str | None = None
    folder: str | None = None


@dataclass
class ScanStats:
    """Result of a reconciliation run."""

    files_seen: int = 0
    folders_seen: int = 0
    added: int = 0
    updated: int = 0
    moved: int = 0
    removed: int = 0
    unchanged: int = 0
    errors: int = 0
    duration_ms: int = 0
    full: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
