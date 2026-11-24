"""Helpers for the archive tests: a small stand-in reconciler, ready inbox items and fake rules."""

from __future__ import annotations

import itertools
import os
import shutil
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from docushelf.archive.ops import ArchiveOps
from docushelf.archive.trash import Trash
from docushelf.inbox.items import InboxItem
from docushelf.models import DocMeta, DocumentRecord
from docushelf.pdf.text import extract_pages
from docushelf.pdf.xmp import read_meta, write_meta
from docushelf.util.fsops import sha256_file
from docushelf.util.ids import provisional_id
from docushelf.util.names import parse_filename
from docushelf.util.timeutil import now_iso


class FakeReconciler:
    """Indexes single files like the real reconciler does, without scanning."""

    def __init__(self, services):
        self.services = services
        self.indexed: list[str] = []

    def index_file(self, path: Path) -> DocumentRecord:
        layout = self.services.layout
        index = self.services.index
        rel = layout.rel_archive(path)
        st = os.stat(path)
        parsed = parse_filename(path.name)
        is_pdf = parsed.ext == ".pdf"
        meta = DocMeta()
        if is_pdf:
            try:
                meta = read_meta(path)
            except Exception:  # encrypted or damaged: indexed without metadata, like the real reconciler
                meta = DocMeta()
        content_hash = sha256_file(path)
        existing = index.get_by_path(rel)
        if existing is not None and (meta.doc_id is None or meta.doc_id == existing.id):
            doc_id = existing.id
        elif meta.doc_id:
            doc_id = meta.doc_id
            holder = index.get(doc_id)
            if holder is not None and holder.path != rel and (layout.archive / holder.path).exists():
                doc_id = provisional_id(content_hash)
        else:
            doc_id = provisional_id(content_hash)
        if parsed.doc_date is not None:
            title, doc_date = parsed.title, parsed.doc_date
        else:
            title = meta.title if (meta.doc_id and meta.title) else parsed.title
            doc_date = meta.doc_date
        try:
            pages = extract_pages(path) if is_pdf else []
        except Exception:
            pages = []
        folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
        record = DocumentRecord(
            id=doc_id,
            path=rel,
            folder=folder,
            filename=path.name,
            ext=parsed.ext,
            title=title,
            doc_date=doc_date,
            sender=meta.sender,
            tags=list(meta.tags),
            origin_hash=meta.origin_hash,
            content_hash=content_hash,
            xmp_id=meta.doc_id,
            has_xmp=bool(meta.doc_id),
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
            inode=st.st_ino,
            page_count=len(pages),
            text_status=("ok" if any(pages) else "empty") if is_pdf else "unsupported",
            indexed_at=now_iso(),
        )
        index.upsert(record, pages)
        index.add_folder_with_parents(folder)
        self.indexed.append(rel)
        return record

    def forget(self, rel_path: str) -> None:
        self.services.index.delete_path(rel_path)

    def ensure_folder(self, rel: str) -> None:
        self.services.index.add_folder_with_parents(rel)


class FakeRuleStore:
    def __init__(self):
        self.added: list = []

    def add(self, rule) -> None:
        self.added.append(rule)


def install_fake_rules(monkeypatch, *, fail: bool = False) -> None:
    """Provide ``docushelf.suggest.rules.rule_from_filing`` regardless of the real module."""
    module = types.ModuleType("docushelf.suggest.rules")

    def rule_from_filing(request, analysis, source):
        if fail:
            raise ValueError("kaputt")
        return SimpleNamespace(name=f"{analysis.sender or request.title} → {request.folder}", folder=request.folder)

    module.rule_from_filing = rule_from_filing
    monkeypatch.setitem(sys.modules, "docushelf.suggest.rules", module)


def attach_archive(services, *, reconciler=True):
    """Give a ``base_services`` stub what ArchiveOps and Trash need."""
    if reconciler:
        services.reconciler = FakeReconciler(services)
    services.rules = FakeRuleStore()
    services.watcher = None
    services.trash = Trash(services)
    services.ops = ArchiveOps(services)
    return services


_counter = itertools.count(1)


def make_ready_item(
    services,
    samples,
    tmp_path: Path,
    *,
    text: str = "Stadtwerke Musterstadt\nStromrechnung Januar",
    source: str = "upload",
    context: dict | None = None,
    original: Path | None = None,
) -> InboxItem:
    """An inbox item in ``bereit`` as the pipeline leaves it (document.pdf, text.json, suggestions).

    ``original`` is taken over as given (a copy); document.pdf is then a byte
    copy of it, as for signed or encrypted PDFs.
    """
    n = next(_counter)
    if original is None:
        original = samples.text_pdf(tmp_path / "drop" / str(n) / f"scan-{n}.pdf", text.split("\n\n"))
        item = services.inbox.create(source, [original], context=context or {})
    else:
        item = services.inbox.create(source, [original], context=context or {}, move=False)
    shutil.copyfile(item.original_paths()[0], item.document_path)
    services.inbox.write_text(item, [text])
    item.stage = "ready"
    item.analysis = {"sender": "Stadtwerke Musterstadt", "doc_type": "Rechnung"}
    item.suggestions = [
        {"folder": "Wohnung/Strom", "title": "Stromrechnung", "score": 0.9, "source": "rule", "reasons": []}
    ]
    services.inbox.save(item)
    services.inbox.move(item, "bereit")
    return item


def add_archive_pdf(services, samples, rel: str, *, text: str = "Inhalt", meta: DocMeta | None = None) -> DocumentRecord:
    """Put a PDF into the archive (optionally with XMP) and index it."""
    path = services.layout.archive / rel
    samples.text_pdf(path, [text])
    if meta is not None:
        tmp = path.with_name(".tmp-" + path.name)
        write_meta(path, tmp, meta)
        os.replace(tmp, path)
    return services.reconciler.index_file(path)


def add_archive_file(services, rel: str, data: bytes = b"not a pdf") -> DocumentRecord:
    path = services.layout.archive / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return services.reconciler.index_file(path)


def make_signed_pdf(samples, path: Path, pages: list[str], *, sig_flags: int | None = 3, perms: bool = False) -> Path:
    """A PDF with a signature field that has a value, like a digitally signed invoice.

    The signature itself is a placeholder; the structure is what signature
    detection looks at. ``sig_flags=None`` leaves out ``/SigFlags``; ``perms``
    adds a certification entry (``/Perms``) to the catalog.
    """
    import pikepdf

    plain = samples.text_pdf(path.with_name(path.stem + "-plain.pdf"), pages)
    with pikepdf.open(plain) as pdf:
        signature = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Sig,
                Filter=pikepdf.Name("/Adobe.PPKLite"),
                SubFilter=pikepdf.Name("/adbe.pkcs7.detached"),
                ByteRange=pikepdf.Array([0, 0, 0, 0]),
                Contents=pikepdf.String(b"\x00" * 64),
            )
        )
        field = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Annot,
                Subtype=pikepdf.Name.Widget,
                FT=pikepdf.Name.Sig,
                T=pikepdf.String("Signatur1"),
                V=signature,
                F=132,
                Rect=pikepdf.Array([0, 0, 0, 0]),
                P=pdf.pages[0].obj,
            )
        )
        pdf.pages[0].obj["/Annots"] = pdf.make_indirect(pikepdf.Array([field]))
        form = pikepdf.Dictionary(Fields=pikepdf.Array([field]))
        if sig_flags is not None:
            form["/SigFlags"] = sig_flags
        pdf.Root["/AcroForm"] = pdf.make_indirect(form)
        if perms:
            pdf.Root["/Perms"] = pikepdf.Dictionary(DocMDP=signature)
        pdf.save(path)
    plain.unlink()
    return path


def make_encrypted_pdf(samples, path: Path, pages: list[str], *, user_password: str = "geheim") -> Path:
    """A PDF that needs ``user_password`` to open (``""``: owner password only)."""
    import pikepdf

    plain = samples.text_pdf(path.with_name(path.stem + "-plain.pdf"), pages)
    with pikepdf.open(plain) as pdf:
        pdf.save(path, encryption=pikepdf.Encryption(owner="besitzer", user=user_password, R=6))
    plain.unlink()
    return path


def archive_files(layout) -> list[str]:
    """Every file below the archive root (hidden ones included), relative and sorted."""
    root = layout.archive
    result = []
    for directory, _dirs, files in os.walk(root):
        for name in files:
            result.append(os.path.relpath(os.path.join(directory, name), root).replace(os.sep, "/"))
    return sorted(result)
