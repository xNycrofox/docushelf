"""Directory layout of the data volume.

::

    <data>/
    ├── archiv/            the truth: a plain folder tree (may be a separate mount)
    ├── eingang/
    │   ├── quellen/<name>/  drop zones per channel
    │   ├── arbeit/<item>/   items being processed
    │   ├── bereit/<item>/   processed, waiting for confirmation
    │   ├── fehler/<item>/   processing failed (with log)
    │   └── erledigt/<item>/ originals of filed items, kept for a while
    ├── papierkorb/<entry>/  deleted documents and inbox items
    ├── config/            rules, settings, tokens (readable files); in the data
    │                      root also households.yaml (several households)
    ├── protokoll/         audit log (JSON lines)
    └── index/             SQLite cache; may be deleted at any time

With several households every household has this layout in its own directory
(``haushalte/<id>`` by default, see :mod:`docushelf.households`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .errors import PathError
from .util.names import check_relative


@dataclass(frozen=True)
class Layout:
    data: Path
    archive: Path

    @classmethod
    def from_settings(cls, settings: Settings) -> "Layout":
        return cls(data=Path(settings.data_dir), archive=Path(settings.archive_dir))

    # -- top level -----------------------------------------------------------
    @property
    def inbox(self) -> Path:
        return self.data / "eingang"

    @property
    def sources(self) -> Path:
        return self.inbox / "quellen"

    @property
    def work(self) -> Path:
        return self.inbox / "arbeit"

    @property
    def ready(self) -> Path:
        return self.inbox / "bereit"

    @property
    def failed(self) -> Path:
        return self.inbox / "fehler"

    @property
    def done(self) -> Path:
        return self.inbox / "erledigt"

    @property
    def trash(self) -> Path:
        return self.data / "papierkorb"

    @property
    def config(self) -> Path:
        return self.data / "config"

    @property
    def audit(self) -> Path:
        return self.data / "protokoll"

    @property
    def index(self) -> Path:
        return self.data / "index"

    @property
    def cache(self) -> Path:
        return self.index / "cache"

    @property
    def db_path(self) -> Path:
        return self.index / "docushelf.sqlite3"

    @property
    def rules_file(self) -> Path:
        return self.config / "rules.yaml"

    @property
    def tokens_file(self) -> Path:
        return self.config / "tokens.yaml"

    @property
    def households_file(self) -> Path:
        """``households.yaml``; only the one in the data root's config directory counts."""
        return self.config / "households.yaml"

    def data_dirs(self) -> tuple[Path, ...]:
        """The directories DocuShelf owns in this data directory (no other household may live inside)."""
        return (self.archive, self.inbox, self.trash, self.config, self.audit, self.index)

    def state_dir(self, state: str) -> Path:
        mapping = {
            "arbeit": self.work,
            "bereit": self.ready,
            "fehler": self.failed,
            "erledigt": self.done,
        }
        try:
            return mapping[state]
        except KeyError:
            raise ValueError(f"unknown inbox state {state!r}") from None

    def source_dir(self, name: str) -> Path:
        if not name or "/" in name or name.startswith("."):
            raise PathError(f"invalid source name {name!r}")
        return self.sources / name

    def ensure(self, source_names: tuple[str, ...] = ()) -> None:
        for path in (
            self.archive,
            self.sources,
            self.work,
            self.ready,
            self.failed,
            self.done,
            self.trash,
            self.config,
            self.audit,
            self.index,
            self.cache,
        ):
            path.mkdir(parents=True, exist_ok=True)
        for name in source_names:
            self.source_dir(name).mkdir(parents=True, exist_ok=True)

    # -- archive paths ---------------------------------------------------------
    def archive_path(self, rel: str) -> Path:
        """Absolute path for a relative archive path; rejects anything escaping the archive."""
        rel = check_relative(rel)
        path = self.archive / rel if rel else self.archive
        root = os.path.realpath(self.archive)
        resolved = os.path.realpath(path)
        if resolved != root and not resolved.startswith(root + os.sep):
            raise PathError("Pfad liegt außerhalb des Archivs")
        return path

    def rel_archive(self, path: Path) -> str:
        """Relative POSIX path of ``path`` inside the archive ('' for the root)."""
        rel = os.path.relpath(path, self.archive)
        if rel == ".":
            return ""
        if rel.startswith(".."):
            raise PathError("Pfad liegt außerhalb des Archivs")
        return rel.replace(os.sep, "/")
