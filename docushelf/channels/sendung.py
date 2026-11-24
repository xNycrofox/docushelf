"""Batches ("Sendungen") in a drop zone.

Channels that receive several files at once (upload, mail, SMTP) write them
into a batch directory inside their drop zone and add the manifest
``_sendung.json`` last. The intake only touches a batch once the manifest
exists, so half-written batches are never picked up::

    eingang/quellen/mail/<batch-id>/
    ├── rechnung.pdf
    ├── foto1.jpg
    ├── foto2.jpg
    └── _sendung.json   {"documents": [{"files": ["rechnung.pdf"]},
                                       {"files": ["foto1.jpg", "foto2.jpg"]}],
                         "context": {...}, "options": {...}}

Each entry of ``documents`` becomes one inbox item; several files in one entry
are combined into one PDF.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Union

from ..util import fsops
from ..util.ids import new_item_id
from ..util.names import casefold_key, sanitize_component, unique_filename

MANIFEST = "_sendung.json"

FileSource = Union[bytes, Path]


@dataclass
class SendungDocument:
    files: list[str]
    title: str | None = None


@dataclass
class Sendung:
    directory: Path
    documents: list[SendungDocument]
    context: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)

    def file_paths(self, document: SendungDocument) -> list[Path]:
        return [self.directory / name for name in document.files]


def write_sendung(
    source_dir: Path,
    documents: list[list[tuple[str, FileSource]]],
    *,
    context: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    titles: list[str | None] | None = None,
    batch_id: str | None = None,
) -> Path:
    """Write a batch into ``source_dir`` and return its directory.

    ``documents`` is a list of documents, each a list of ``(filename, content)``
    where content is bytes or a path to copy. The manifest is written last.
    """
    if not documents or not any(documents):
        raise ValueError("a batch needs at least one file")
    batch_dir = source_dir / (batch_id or new_item_id())
    batch_dir.mkdir(parents=True, exist_ok=False)
    manifest_docs: list[dict[str, Any]] = []
    taken: set[str] = {MANIFEST.casefold()}
    try:
        for index, files in enumerate(documents):
            if not files:
                continue
            names: list[str] = []
            for filename, content in files:
                try:
                    clean = sanitize_component(filename or "datei")
                except Exception:
                    clean = "datei"
                name = unique_filename(batch_dir, clean, taken=taken)
                taken.add(casefold_key(name))
                target = batch_dir / name
                if isinstance(content, (bytes, bytearray)):
                    with open(target, "xb") as handle:
                        handle.write(content)
                        handle.flush()
                else:
                    fsops.copy_file_fsync(Path(content), target)
                names.append(name)
            entry: dict[str, Any] = {"files": names}
            if titles and index < len(titles) and titles[index]:
                entry["title"] = titles[index]
            manifest_docs.append(entry)
        fsops.atomic_write_json(
            batch_dir / MANIFEST,
            {"version": 1, "documents": manifest_docs, "context": context or {}, "options": options or {}},
        )
    except BaseException:
        shutil.rmtree(batch_dir, ignore_errors=True)
        raise
    return batch_dir


def read_sendung(batch_dir: Path) -> Sendung | None:
    """Return the batch if its manifest exists and is valid, else None."""
    path = batch_dir / MANIFEST
    if not path.is_file():
        return None
    try:
        data = fsops.read_json(path)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    raw_documents = data.get("documents") or []
    if not isinstance(raw_documents, list):
        return None
    documents = []
    for entry in raw_documents:
        if not isinstance(entry, dict):
            continue
        raw_files = entry.get("files") or []
        if not isinstance(raw_files, list):
            continue
        files = [str(name) for name in raw_files if str(name) and "/" not in str(name) and "\\" not in str(name)]
        if files:
            title = entry.get("title")
            documents.append(SendungDocument(files=files, title=str(title) if title else None))
    context = data.get("context")
    options = data.get("options")
    return Sendung(
        directory=batch_dir,
        documents=documents,
        context=dict(context) if isinstance(context, dict) else {},
        options=dict(options) if isinstance(options, dict) else {},
    )
