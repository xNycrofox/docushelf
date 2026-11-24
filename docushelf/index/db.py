"""SQLite index: search cache, folder list and job queue.

Nothing in here is authoritative. The whole database can be deleted and is
rebuilt from the archive and the inbox directories (see ``rebuild``).

Full-text search uses FTS5 tables with external content:

* ``fts_pages``  – trigram index over the normalized page text (substring search)
* ``fts_docs``   – trigram index over title, folder, sender, tags and file name
* ``fts_joined`` and ``fts_docs_joined`` – trigram indexes over the words that
  only exist in the joined form of the same texts (digit groups and hyphenated
  words written together, see :func:`docushelf.util.textnorm.joined`), so that
  "1234567890" finds "123 456 7890" and "Kfzversicherung" "Kfz-Versicherung"
* ``fts_words``  – word index over the page text, only used for its vocabulary
  (``fts_words_vocab``), which powers typo tolerance.

Each live thread uses its own connection (WAL mode); connections of threads
that have ended are closed when the next thread opens one.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..models import DocumentRecord
from ..util.textnorm import joined_fragments, normalize
from ..util.timeutil import now_iso, parse_date

SCHEMA_VERSION = "2"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS folders(
    path TEXT PRIMARY KEY,
    parent TEXT NOT NULL,
    name TEXT NOT NULL,
    depth INTEGER NOT NULL,
    scan_gen INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS documents(
    num INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL UNIQUE,
    folder TEXT NOT NULL,
    filename TEXT NOT NULL,
    ext TEXT NOT NULL,
    title TEXT NOT NULL,
    doc_date TEXT,
    sender TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    origin_hash TEXT,
    content_hash TEXT NOT NULL,
    xmp_id TEXT,
    has_xmp INTEGER NOT NULL DEFAULT 0,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    inode INTEGER,
    page_count INTEGER NOT NULL DEFAULT 0,
    text_status TEXT NOT NULL DEFAULT 'pending',
    indexed_at TEXT NOT NULL,
    scan_gen INTEGER NOT NULL DEFAULT 0,
    search_norm TEXT NOT NULL DEFAULT '',
    search_joined TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS documents_folder ON documents(folder);
CREATE INDEX IF NOT EXISTS documents_origin ON documents(origin_hash);
CREATE INDEX IF NOT EXISTS documents_content ON documents(content_hash);
CREATE INDEX IF NOT EXISTS documents_xmp ON documents(xmp_id);
CREATE INDEX IF NOT EXISTS documents_date ON documents(doc_date);

CREATE TABLE IF NOT EXISTS pages(
    rowid INTEGER PRIMARY KEY,
    doc_num INTEGER NOT NULL REFERENCES documents(num) ON DELETE CASCADE,
    page INTEGER NOT NULL,
    text TEXT NOT NULL,
    norm TEXT NOT NULL,
    joined TEXT NOT NULL DEFAULT '',
    UNIQUE(doc_num, page)
);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_pages USING fts5(
    norm, content='pages', content_rowid='rowid', tokenize='trigram'
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_joined USING fts5(
    joined, content='pages', content_rowid='rowid', tokenize='trigram'
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_words USING fts5(
    norm, content='pages', content_rowid='rowid', tokenize='unicode61'
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_words_vocab USING fts5vocab(fts_words, 'row');
CREATE VIRTUAL TABLE IF NOT EXISTS fts_docs USING fts5(
    search_norm, content='documents', content_rowid='num', tokenize='trigram'
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_docs_joined USING fts5(
    search_joined, content='documents', content_rowid='num', tokenize='trigram'
);

-- Rows without joined words are left out of fts_joined and fts_docs_joined
-- (most pages have none), which keeps them small and cheap to maintain.
CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
    INSERT INTO fts_pages(rowid, norm) VALUES (new.rowid, new.norm);
    INSERT INTO fts_words(rowid, norm) VALUES (new.rowid, new.norm);
    INSERT INTO fts_joined(rowid, joined) SELECT new.rowid, new.joined WHERE new.joined != '';
END;
CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
    INSERT INTO fts_pages(fts_pages, rowid, norm) VALUES ('delete', old.rowid, old.norm);
    INSERT INTO fts_words(fts_words, rowid, norm) VALUES ('delete', old.rowid, old.norm);
    INSERT INTO fts_joined(fts_joined, rowid, joined) SELECT 'delete', old.rowid, old.joined WHERE old.joined != '';
END;
CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE OF norm, joined ON pages BEGIN
    INSERT INTO fts_pages(fts_pages, rowid, norm) VALUES ('delete', old.rowid, old.norm);
    INSERT INTO fts_words(fts_words, rowid, norm) VALUES ('delete', old.rowid, old.norm);
    INSERT INTO fts_joined(fts_joined, rowid, joined) SELECT 'delete', old.rowid, old.joined WHERE old.joined != '';
    INSERT INTO fts_pages(rowid, norm) VALUES (new.rowid, new.norm);
    INSERT INTO fts_words(rowid, norm) VALUES (new.rowid, new.norm);
    INSERT INTO fts_joined(rowid, joined) SELECT new.rowid, new.joined WHERE new.joined != '';
END;

CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
    INSERT INTO fts_docs(rowid, search_norm) VALUES (new.num, new.search_norm);
    INSERT INTO fts_docs_joined(rowid, search_joined) SELECT new.num, new.search_joined WHERE new.search_joined != '';
END;
CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
    INSERT INTO fts_docs(fts_docs, rowid, search_norm) VALUES ('delete', old.num, old.search_norm);
    INSERT INTO fts_docs_joined(fts_docs_joined, rowid, search_joined)
        SELECT 'delete', old.num, old.search_joined WHERE old.search_joined != '';
END;
CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE OF search_norm, search_joined ON documents BEGIN
    INSERT INTO fts_docs(fts_docs, rowid, search_norm) VALUES ('delete', old.num, old.search_norm);
    INSERT INTO fts_docs_joined(fts_docs_joined, rowid, search_joined)
        SELECT 'delete', old.num, old.search_joined WHERE old.search_joined != '';
    INSERT INTO fts_docs(rowid, search_norm) VALUES (new.num, new.search_norm);
    INSERT INTO fts_docs_joined(rowid, search_joined) SELECT new.num, new.search_joined WHERE new.search_joined != '';
END;

CREATE TABLE IF NOT EXISTS jobs(
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    run_after REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    worker TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_active ON jobs(kind, key) WHERE state IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS jobs_pick ON jobs(state, run_after);

CREATE TABLE IF NOT EXISTS mail_seen(
    key TEXT PRIMARY KEY,
    seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hash_seen(
    source TEXT NOT NULL,
    hash TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY(source, hash)
);
"""

# Everything that is read from the archive. A schema upgrade drops and rebuilds
# only these, so queued jobs and the memories of imported mail and files survive it.
_DROP_ARCHIVE = """
DROP TRIGGER IF EXISTS pages_ai;
DROP TRIGGER IF EXISTS pages_ad;
DROP TRIGGER IF EXISTS pages_au;
DROP TRIGGER IF EXISTS documents_ai;
DROP TRIGGER IF EXISTS documents_ad;
DROP TRIGGER IF EXISTS documents_au;
DROP TABLE IF EXISTS fts_words_vocab;
DROP TABLE IF EXISTS fts_words;
DROP TABLE IF EXISTS fts_pages;
DROP TABLE IF EXISTS fts_joined;
DROP TABLE IF EXISTS fts_docs;
DROP TABLE IF EXISTS fts_docs_joined;
DROP TABLE IF EXISTS pages;
DROP TABLE IF EXISTS documents;
DROP TABLE IF EXISTS folders;
"""

_DROP = _DROP_ARCHIVE + """
DROP TABLE IF EXISTS jobs;
DROP TABLE IF EXISTS mail_seen;
DROP TABLE IF EXISTS hash_seen;
DROP TABLE IF EXISTS meta;
"""

_DOC_COLUMNS = (
    "id, path, folder, filename, ext, title, doc_date, sender, tags, origin_hash, content_hash, xmp_id, "
    "has_xmp, size, mtime_ns, inode, page_count, text_status, indexed_at"
)


@dataclass
class Job:
    id: int
    kind: str
    key: str
    payload: dict[str, Any]
    state: str
    attempts: int
    run_after: float
    last_error: str | None


def folder_parent(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def folder_name(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _search_source(record: DocumentRecord) -> str:
    # "|" is never dropped by the joined form, so fields, folder names and tags
    # are never joined with each other (the normalized text is the same as with spaces).
    parts = [record.title, record.folder.replace("/", " | "), record.sender or "", " | ".join(record.tags), record.filename]
    return " | ".join(parts)


def search_text(record: DocumentRecord) -> str:
    """Normalized text for the per-document search fields."""
    return normalize(_search_source(record))


def search_joined(record: DocumentRecord, norm: str | None = None) -> str:
    """Words of the joined form of the search fields that ``search_text`` does not contain."""
    return joined_fragments(_search_source(record), norm)


def row_to_record(row: sqlite3.Row) -> DocumentRecord:
    return DocumentRecord(
        id=row["id"],
        path=row["path"],
        folder=row["folder"],
        filename=row["filename"],
        ext=row["ext"],
        title=row["title"],
        doc_date=parse_date(row["doc_date"]),
        sender=row["sender"],
        tags=json.loads(row["tags"] or "[]"),
        origin_hash=row["origin_hash"],
        content_hash=row["content_hash"],
        xmp_id=row["xmp_id"],
        has_xmp=bool(row["has_xmp"]),
        size=row["size"],
        mtime_ns=row["mtime_ns"],
        inode=row["inode"],
        page_count=row["page_count"],
        text_status=row["text_status"],
        indexed_at=row["indexed_at"],
    )


class Index:
    """Thread-safe access to the SQLite index (one connection per live thread)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._local = threading.local()
        # The connection of every thread that opened one. Web servers run requests
        # in worker threads that come and go, so the connections of threads that
        # have ended are closed whenever a thread opens a new one.
        self._connections: dict[threading.Thread, sqlite3.Connection] = {}
        self._lock = threading.Lock()
        # Bumped on every change to documents; consumers (similarity model) use it for caching.
        self._generation = 0

    # -- connections ---------------------------------------------------------
    def conn(self) -> sqlite3.Connection:
        connection = getattr(self._local, "conn", None)
        if connection is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=30000")
            except BaseException:
                connection.close()
                raise
            self._local.conn = connection
            self._register(connection)
        return connection

    def _register(self, connection: sqlite3.Connection) -> None:
        current = threading.current_thread()
        with self._lock:
            # A thread that has ended cannot use its connection any more (and
            # check_same_thread=False allows closing it from here).
            finished = [thread for thread in self._connections if thread is not current and not thread.is_alive()]
            obsolete = [self._connections.pop(thread) for thread in finished]
            previous = self._connections.get(current)
            if previous is not None and previous is not connection:
                obsolete.append(previous)
            self._connections[current] = connection
        for stale in obsolete:
            with suppress(sqlite3.Error):
                stale.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction (``BEGIN IMMEDIATE``). Nested use becomes a savepoint."""
        connection = self.conn()
        if connection.in_transaction:
            name = f"sp_{threading.get_ident()}_{time.monotonic_ns()}"
            connection.execute(f"SAVEPOINT {name}")
            try:
                yield connection
            except BaseException:
                connection.execute(f"ROLLBACK TO {name}")
                connection.execute(f"RELEASE {name}")
                raise
            connection.execute(f"RELEASE {name}")
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")

    def close(self) -> None:
        """Close the connections of all threads; a later ``conn()`` opens a new one."""
        with self._lock:
            connections = list(self._connections.values())
            self._connections.clear()
            self._local = threading.local()
        for connection in connections:
            with suppress(sqlite3.Error):
                connection.close()

    @property
    def generation(self) -> int:
        return self._generation

    def _touch(self) -> None:
        self._generation += 1

    # -- schema --------------------------------------------------------------
    def init_schema(self) -> bool:
        """Create tables. Returns True if the index was (re)created and needs a full scan."""
        connection = self.conn()
        version = None
        try:
            row = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            version = row["value"] if row else None
        except sqlite3.OperationalError:
            version = None
        if version is not None and version != SCHEMA_VERSION:
            self._upgrade()
            return True
        connection.executescript(_SCHEMA)
        if version is None:
            self.set_meta("schema_version", SCHEMA_VERSION)
            return True
        return False

    def reset(self) -> None:
        """Drop everything and create an empty schema."""
        connection = self.conn()
        connection.executescript(_DROP)
        connection.executescript(_SCHEMA)
        self.set_meta("schema_version", SCHEMA_VERSION)
        self._touch()

    def _upgrade(self) -> None:
        """Recreate the tables read from the archive in the current layout (they are refilled by a full scan).

        The job queue and the remembered mails and file hashes are kept: they
        cannot be read from the archive, and losing them would import files
        from watch folders that are not emptied a second time.
        """
        connection = self.conn()
        connection.executescript(_DROP_ARCHIVE)
        connection.executescript(_SCHEMA)
        connection.execute("DELETE FROM meta WHERE key IN ('last_scan', 'last_full_scan')")
        self.set_meta("schema_version", SCHEMA_VERSION)
        self._touch()

    # -- meta ------------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self.conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        self.conn().execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def next_scan_gen(self) -> int:
        with self.tx() as c:
            row = c.execute("SELECT value FROM meta WHERE key='scan_gen'").fetchone()
            value = int(row["value"]) + 1 if row else 1
            c.execute(
                "INSERT INTO meta(key, value) VALUES('scan_gen', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(value),),
            )
        return value

    # -- folders -----------------------------------------------------------------
    def upsert_folder(self, path: str, scan_gen: int = 0) -> None:
        if not path:
            return
        c = self.conn()
        c.execute(
            "INSERT INTO folders(path, parent, name, depth, scan_gen) VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET scan_gen=MAX(folders.scan_gen, excluded.scan_gen)",
            (path, folder_parent(path), folder_name(path), path.count("/") + 1, scan_gen),
        )

    def add_folder_with_parents(self, path: str, scan_gen: int = 0) -> None:
        parts = path.split("/") if path else []
        with self.tx():
            for i in range(1, len(parts) + 1):
                self.upsert_folder("/".join(parts[:i]), scan_gen)

    def delete_unseen_folders(self, scan_gen: int) -> list[str]:
        with self.tx() as c:
            rows = c.execute("SELECT path FROM folders WHERE scan_gen < ?", (scan_gen,)).fetchall()
            c.execute("DELETE FROM folders WHERE scan_gen < ?", (scan_gen,))
        return [r["path"] for r in rows]

    def remove_folder_tree(self, path: str) -> None:
        with self.tx() as c:
            c.execute(
                "DELETE FROM folders WHERE path=? OR substr(path, 1, ?)=?", (path, *_prefix_params(path))
            )

    def rename_folder_tree(self, old: str, new: str) -> None:
        """Rename a folder and its subfolders in the folders table (documents are moved separately)."""
        with self.tx() as c:
            rows = c.execute(
                "SELECT path, scan_gen FROM folders WHERE path=? OR substr(path, 1, ?)=?", (old, *_prefix_params(old))
            ).fetchall()
            c.execute("DELETE FROM folders WHERE path=? OR substr(path, 1, ?)=?", (old, *_prefix_params(old)))
            for row in rows:
                renamed = new + row["path"][len(old) :]
                self.upsert_folder(renamed, row["scan_gen"])

    def list_folders(self) -> list[str]:
        rows = self.conn().execute("SELECT path FROM folders").fetchall()
        return sorted((r["path"] for r in rows), key=lambda p: p.casefold())

    def folder_exists(self, path: str) -> bool:
        if path == "":
            return True
        return self.conn().execute("SELECT 1 FROM folders WHERE path=?", (path,)).fetchone() is not None

    def subfolders(self, path: str) -> list[str]:
        rows = self.conn().execute("SELECT path FROM folders WHERE parent=?", (path,)).fetchall()
        return sorted((r["path"] for r in rows), key=lambda p: p.casefold())

    def folder_counts(self) -> dict[str, int]:
        """Number of documents directly in each folder."""
        rows = self.conn().execute("SELECT folder, COUNT(*) AS n FROM documents GROUP BY folder").fetchall()
        return {r["folder"]: r["n"] for r in rows}

    # -- documents ---------------------------------------------------------------
    def get(self, doc_id: str) -> DocumentRecord | None:
        row = self.conn().execute(f"SELECT {_DOC_COLUMNS} FROM documents WHERE id=?", (doc_id,)).fetchone()
        return row_to_record(row) if row else None

    def get_by_path(self, path: str) -> DocumentRecord | None:
        row = self.conn().execute(f"SELECT {_DOC_COLUMNS} FROM documents WHERE path=?", (path,)).fetchone()
        return row_to_record(row) if row else None

    def _where(self, column: str, value: str) -> list[DocumentRecord]:
        rows = self.conn().execute(f"SELECT {_DOC_COLUMNS} FROM documents WHERE {column}=?", (value,)).fetchall()
        return [row_to_record(r) for r in rows]

    def by_xmp_id(self, xmp_id: str) -> list[DocumentRecord]:
        return self._where("xmp_id", xmp_id)

    def by_content_hash(self, content_hash: str) -> list[DocumentRecord]:
        return self._where("content_hash", content_hash)

    def by_origin_hash(self, origin_hash: str) -> list[DocumentRecord]:
        return self._where("origin_hash", origin_hash)

    def id_exists(self, doc_id: str) -> bool:
        return self.conn().execute("SELECT 1 FROM documents WHERE id=?", (doc_id,)).fetchone() is not None

    def path_index(self) -> dict[str, tuple[str, int, int, int | None]]:
        """path -> (id, size, mtime_ns, inode) for every indexed document."""
        rows = self.conn().execute("SELECT path, id, size, mtime_ns, inode FROM documents").fetchall()
        return {r["path"]: (r["id"], r["size"], r["mtime_ns"], r["inode"]) for r in rows}

    def upsert(self, record: DocumentRecord, pages: list[str] | None = None, *, scan_gen: int | None = None) -> None:
        """Insert or replace a document (matched by id). ``pages`` replaces the stored text if given."""
        with self.tx() as c:
            existing = c.execute("SELECT num, scan_gen FROM documents WHERE id=?", (record.id,)).fetchone()
            # A different document may still occupy the path (e.g. replaced in place); drop it.
            clash = c.execute("SELECT num, id FROM documents WHERE path=?", (record.path,)).fetchone()
            if clash and (existing is None or clash["num"] != existing["num"]):
                c.execute("DELETE FROM documents WHERE num=?", (clash["num"],))
            gen = scan_gen if scan_gen is not None else (existing["scan_gen"] if existing else 0)
            fields_norm = search_text(record)
            values = (
                record.path,
                record.folder,
                record.filename,
                record.ext,
                record.title,
                record.doc_date.isoformat() if record.doc_date else None,
                record.sender,
                json.dumps(record.tags, ensure_ascii=False),
                record.origin_hash,
                record.content_hash,
                record.xmp_id,
                int(record.has_xmp),
                record.size,
                record.mtime_ns,
                record.inode,
                record.page_count,
                record.text_status,
                record.indexed_at or now_iso(),
                gen,
                fields_norm,
                search_joined(record, fields_norm),
            )
            if existing:
                num = existing["num"]
                c.execute(
                    "UPDATE documents SET path=?, folder=?, filename=?, ext=?, title=?, doc_date=?, sender=?, tags=?, "
                    "origin_hash=?, content_hash=?, xmp_id=?, has_xmp=?, size=?, mtime_ns=?, inode=?, page_count=?, "
                    "text_status=?, indexed_at=?, scan_gen=?, search_norm=?, search_joined=? WHERE num=?",
                    values + (num,),
                )
            else:
                cursor = c.execute(
                    "INSERT INTO documents(id, path, folder, filename, ext, title, doc_date, sender, tags, origin_hash, "
                    "content_hash, xmp_id, has_xmp, size, mtime_ns, inode, page_count, text_status, indexed_at, "
                    "scan_gen, search_norm, search_joined) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (record.id,) + values,
                )
                num = cursor.lastrowid
            if pages is not None:
                c.execute("DELETE FROM pages WHERE doc_num=?", (num,))
                rows = []
                for i, text in enumerate(pages):
                    norm = normalize(text)
                    rows.append((num, i + 1, text, norm, joined_fragments(text, norm)))
                c.executemany("INSERT INTO pages(doc_num, page, text, norm, joined) VALUES(?, ?, ?, ?, ?)", rows)
        self._touch()

    def mark_seen(self, doc_id: str, scan_gen: int) -> None:
        self.conn().execute("UPDATE documents SET scan_gen=? WHERE id=?", (scan_gen, doc_id))

    def mark_paths_seen(self, paths: Iterable[str], scan_gen: int) -> None:
        with self.tx() as c:
            c.executemany("UPDATE documents SET scan_gen=? WHERE path=?", [(scan_gen, p) for p in paths])

    def unseen(self, scan_gen: int) -> list[DocumentRecord]:
        rows = self.conn().execute(f"SELECT {_DOC_COLUMNS} FROM documents WHERE scan_gen < ?", (scan_gen,)).fetchall()
        return [row_to_record(r) for r in rows]

    def change_id(self, old_id: str, new_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE documents SET id=? WHERE id=?", (new_id, old_id))
        self._touch()

    def delete(self, doc_id: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        self._touch()

    def delete_path(self, path: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM documents WHERE path=?", (path,))
        self._touch()

    def paths_with_text_status(self, statuses: Iterable[str]) -> list[tuple[str, str]]:
        """(path, content_hash) of all documents whose text_status is one of ``statuses``."""
        statuses = tuple(statuses)
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        rows = self.conn().execute(
            f"SELECT path, content_hash FROM documents WHERE text_status IN ({placeholders}) ORDER BY path", statuses
        ).fetchall()
        return [(r["path"], r["content_hash"]) for r in rows]

    def list_in_folder(
        self,
        folder: str,
        *,
        recursive: bool = False,
        order: str = "date_desc",
        limit: int = 500,
        offset: int = 0,
    ) -> list[DocumentRecord]:
        orders = {
            "date_desc": "doc_date IS NULL, doc_date DESC, title COLLATE NOCASE",
            "date_asc": "doc_date IS NULL, doc_date ASC, title COLLATE NOCASE",
            "title": "title COLLATE NOCASE",
            "indexed_desc": "indexed_at DESC",
        }
        order_sql = orders.get(order, orders["date_desc"])
        if recursive and folder:
            sql = (
                f"SELECT {_DOC_COLUMNS} FROM documents WHERE folder=? OR substr(folder, 1, ?)=? "
                f"ORDER BY {order_sql} LIMIT ? OFFSET ?"
            )
            params: tuple[Any, ...] = (folder, *_prefix_params(folder), limit, offset)
        elif recursive:
            sql = f"SELECT {_DOC_COLUMNS} FROM documents ORDER BY {order_sql} LIMIT ? OFFSET ?"
            params = (limit, offset)
        else:
            sql = f"SELECT {_DOC_COLUMNS} FROM documents WHERE folder=? ORDER BY {order_sql} LIMIT ? OFFSET ?"
            params = (folder, limit, offset)
        return [row_to_record(r) for r in self.conn().execute(sql, params).fetchall()]

    def recent(self, limit: int = 20) -> list[DocumentRecord]:
        rows = self.conn().execute(
            f"SELECT {_DOC_COLUMNS} FROM documents ORDER BY indexed_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [row_to_record(r) for r in rows]

    def count(self) -> int:
        return self.conn().execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    def get_pages(self, doc_id: str) -> list[str]:
        rows = self.conn().execute(
            "SELECT p.text FROM pages p JOIN documents d ON d.num = p.doc_num WHERE d.id=? ORDER BY p.page",
            (doc_id,),
        ).fetchall()
        return [r["text"] for r in rows]

    def iter_corpus(self) -> Iterator[tuple[str, str, str, str | None, str]]:
        """(doc_id, folder, title, sender, normalized full text) for every document."""
        rows = self.conn().execute(
            "SELECT d.id, d.folder, d.title, d.sender, group_concat(p.norm, ' ') AS norm "
            "FROM documents d LEFT JOIN pages p ON p.doc_num = d.num GROUP BY d.num"
        )
        for row in rows:
            yield row["id"], row["folder"], row["title"], row["sender"], row["norm"] or ""

    def senders(self) -> list[tuple[str, int]]:
        rows = self.conn().execute(
            "SELECT sender, COUNT(*) AS n FROM documents WHERE sender IS NOT NULL AND sender != '' "
            "GROUP BY sender ORDER BY n DESC, sender COLLATE NOCASE"
        ).fetchall()
        return [(r["sender"], r["n"]) for r in rows]

    def tags(self) -> list[tuple[str, int]]:
        rows = self.conn().execute(
            "SELECT value AS tag, COUNT(*) AS n FROM documents, json_each(documents.tags) "
            "GROUP BY value ORDER BY n DESC, value COLLATE NOCASE"
        ).fetchall()
        return [(r["tag"], r["n"]) for r in rows]

    def sender_folders(self, sender: str) -> list[tuple[str, int]]:
        rows = self.conn().execute(
            "SELECT folder, COUNT(*) AS n FROM documents WHERE sender = ? COLLATE NOCASE "
            "GROUP BY folder ORDER BY n DESC",
            (sender,),
        ).fetchall()
        return [(r["folder"], r["n"]) for r in rows]

    # -- vocabulary ----------------------------------------------------------------
    def vocabulary(self, min_length: int = 4) -> list[tuple[str, int]]:
        rows = self.conn().execute(
            "SELECT term, doc FROM fts_words_vocab WHERE length(term) >= ?", (min_length,)
        ).fetchall()
        return [(r["term"], r["doc"]) for r in rows]

    # -- jobs -------------------------------------------------------------------------
    def enqueue(self, kind: str, key: str, payload: dict[str, Any] | None = None, *, delay: float = 0) -> bool:
        """Queue a job unless the same (kind, key) is already queued or running."""
        now = now_iso()
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO jobs(kind, key, payload, state, run_after, created_at, updated_at) "
                    "VALUES(?, ?, ?, 'queued', ?, ?, ?)",
                    (kind, key, json.dumps(payload or {}, ensure_ascii=False), time.time() + delay, now, now),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def claim(self, kinds: Iterable[str], worker: str) -> Job | None:
        kinds = tuple(kinds)
        placeholders = ",".join("?" for _ in kinds)
        with self.tx() as c:
            row = c.execute(
                f"SELECT id FROM jobs WHERE state='queued' AND kind IN ({placeholders}) AND run_after <= ? "
                "ORDER BY run_after, id LIMIT 1",
                kinds + (time.time(),),
            ).fetchone()
            if not row:
                return None
            c.execute(
                "UPDATE jobs SET state='running', worker=?, attempts=attempts+1, updated_at=? WHERE id=?",
                (worker, now_iso(), row["id"]),
            )
            job = c.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
        return Job(
            id=job["id"],
            kind=job["kind"],
            key=job["key"],
            payload=json.loads(job["payload"] or "{}"),
            state=job["state"],
            attempts=job["attempts"],
            run_after=job["run_after"],
            last_error=job["last_error"],
        )

    def complete(self, job_id: int) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    def fail(self, job_id: int, error: str, *, retry_in: float | None) -> None:
        """Record a failure; requeue after ``retry_in`` seconds or mark failed for good."""
        with self.tx() as c:
            if retry_in is None:
                c.execute(
                    "UPDATE jobs SET state='failed', last_error=?, updated_at=? WHERE id=?",
                    (error[:4000], now_iso(), job_id),
                )
            else:
                c.execute(
                    "UPDATE jobs SET state='queued', last_error=?, run_after=?, updated_at=? WHERE id=?",
                    (error[:4000], time.time() + retry_in, now_iso(), job_id),
                )

    def release(self, job_id: int) -> None:
        """Put a claimed job back in the queue without counting the attempt."""
        with self.tx() as c:
            c.execute(
                "UPDATE jobs SET state='queued', worker=NULL, attempts=MAX(attempts-1, 0), updated_at=? "
                "WHERE id=? AND state='running'",
                (now_iso(), job_id),
            )

    def next_run_after(self, kinds: Iterable[str]) -> float | None:
        """Earliest run_after of the queued jobs of these kinds (None if there are none)."""
        kinds = tuple(kinds)
        if not kinds:
            return None
        placeholders = ",".join("?" for _ in kinds)
        row = self.conn().execute(
            f"SELECT MIN(run_after) AS due FROM jobs WHERE state='queued' AND kind IN ({placeholders})", kinds
        ).fetchone()
        return row["due"] if row and row["due"] is not None else None

    def requeue_running(self) -> int:
        """After a restart nothing is running anymore."""
        with self.tx() as c:
            cursor = c.execute("UPDATE jobs SET state='queued', worker=NULL WHERE state='running'")
            return cursor.rowcount

    def job_counts(self) -> dict[str, int]:
        rows = self.conn().execute("SELECT kind || ':' || state AS k, COUNT(*) AS n FROM jobs GROUP BY kind, state")
        return {r["k"]: r["n"] for r in rows}

    def pending_jobs(self, kind: str | None = None) -> list[Job]:
        sql = "SELECT * FROM jobs WHERE state IN ('queued','running')"
        params: tuple[Any, ...] = ()
        if kind:
            sql += " AND kind=?"
            params = (kind,)
        rows = self.conn().execute(sql + " ORDER BY id", params).fetchall()
        return [
            Job(
                id=r["id"],
                kind=r["kind"],
                key=r["key"],
                payload=json.loads(r["payload"] or "{}"),
                state=r["state"],
                attempts=r["attempts"],
                run_after=r["run_after"],
                last_error=r["last_error"],
            )
            for r in rows
        ]

    def clear_jobs(self, kind: str | None = None) -> None:
        with self.tx() as c:
            if kind:
                c.execute("DELETE FROM jobs WHERE kind=?", (kind,))
            else:
                c.execute("DELETE FROM jobs")

    # -- small key/value memories ---------------------------------------------------------
    def mail_seen(self, key: str) -> bool:
        return self.conn().execute("SELECT 1 FROM mail_seen WHERE key=?", (key,)).fetchone() is not None

    def mark_mail_seen(self, key: str) -> None:
        self.conn().execute("INSERT OR IGNORE INTO mail_seen(key, seen_at) VALUES(?, ?)", (key, now_iso()))

    def hash_seen(self, source: str, digest: str) -> bool:
        return (
            self.conn().execute("SELECT 1 FROM hash_seen WHERE source=? AND hash=?", (source, digest)).fetchone()
            is not None
        )

    def mark_hash_seen(self, source: str, digest: str) -> None:
        self.conn().execute(
            "INSERT OR IGNORE INTO hash_seen(source, hash, seen_at) VALUES(?, ?, ?)", (source, digest, now_iso())
        )

    # -- statistics -------------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        c = self.conn()
        return {
            "documents": c.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
            "pages": c.execute("SELECT COUNT(*) FROM pages").fetchone()[0],
            "folders": c.execute("SELECT COUNT(*) FROM folders").fetchone()[0],
            "without_text": c.execute(
                "SELECT COUNT(*) FROM documents WHERE text_status != 'ok'"
            ).fetchone()[0],
            "jobs": self.job_counts(),
            "last_scan": self.get_meta("last_scan"),
            "last_full_scan": self.get_meta("last_full_scan"),
        }


def _prefix_params(path: str) -> tuple[int, str]:
    """Parameters for ``substr(col, 1, ?)=?``: an exact, case-sensitive "below this folder" test.

    LIKE would ignore ASCII case, so on a case-sensitive disk 'Bank' would also
    match 'bank/…'. substr counts characters, like Python's len().
    """
    prefix = path + "/"
    return len(prefix), prefix
