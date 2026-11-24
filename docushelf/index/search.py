"""Full-text search over the index.

Queries and indexed text go through the same normalization
(:func:`docushelf.util.textnorm.normalize`): lower case, umlauts as ae/oe/ue,
ß as ss, accents removed. Every term then matches as a substring – the FTS5
trigram indexes make that fast – so "Neben" finds "Nebenkostenabrechnung",
"mueller" finds "Müller" and compound words need no dictionary.

Numbers and compounds are also searched in the joined form of the text
(:func:`docushelf.util.textnorm.joined`), where digit groups and hyphenated
words are written together: "1234567890" finds "123 456 7890", an IBAN pasted
without spaces finds the grouped one, "Kfzversicherung" finds
"Kfz-Versicherung", and "Lohnsteuer-Bescheinigung" also finds
"Lohnsteuerbescheinigung". A closed compound without any hit also tries the
spelling with a space ("Kfz Versicherung"). A term that still matches nothing
is replaced by similarly spelled words from the archive's vocabulary. Both are
reported as corrections ("Meintest du …?").

Query syntax (see :func:`parse_query`): free text, ``"quoted phrases"`` and the
inline filters ``ordner:``, ``tag:``, ``von:``, ``jahr:``, ``ab:``, ``bis:``.
"""

from __future__ import annotations

import bisect
import html
import json
import logging
import math
import re
import sqlite3
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterator

from ..models import DocumentRecord
from ..util.textnorm import (
    STOPWORDS,
    bounded_levenshtein,
    join_map,
    joined,
    normalize,
    normalize_with_map,
    spellings,
)
from ..util.timeutil import today
from .db import row_to_record

log = logging.getLogger(__name__)

__all__ = ["SearchQuery", "Snippet", "SearchHit", "SearchResult", "Searcher", "parse_query"]

DEFAULT_LIMIT = 50
MAX_LIMIT = 500
MAX_SNIPPETS = 3
SNIPPET_CHARS = 160
SNIPPET_LEAD = 50  # characters of context before the first match of a snippet
MAX_ALTERNATIVES = 5  # vocabulary words used for one misspelled term
MIN_CORRECTABLE = 5  # shorter terms are never corrected
# Numbers, and the joined form of hyphenated input, are searched without their
# separators only from this length on: "2024" must not find the amount "20,24"
# and "e.V." must not find every "ev".
MIN_JOINED = 6
MIN_COMPOUND = 6  # closed compounds without any hit also try the spelling with a space
MIN_COMPOUND_PART = 3  # ... split into parts of at least this length ("kfz versicherung")
DISPLAY_SAMPLE_PAGES = 4  # pages read to find how a word from the page text is written
# Weight of a term found in a document field. Text hits score below all of
# them (at most ~4), so a hit in the title always beats a hit in the body.
FIELD_WEIGHTS = {"title": 10.0, "sender": 9.0, "folder": 5.0, "tags": 5.0, "filename": 4.0}
EXTRA_FIELD_BONUS = 0.5
MATCH_ORDER = ("title", "sender", "folder", "tags", "filename", "text")


# ---------------------------------------------------------------------------
# Query


@dataclass
class SearchQuery:
    text: str = ""
    folder: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    tags: list[str] = field(default_factory=list)
    sender: str | None = None
    limit: int = DEFAULT_LIMIT
    offset: int = 0

    def has_filters(self) -> bool:
        return bool(self.folder or self.date_from or self.date_to or self.tags or self.sender)

    def is_empty(self) -> bool:
        return not (self.text or "").strip() and not self.has_filters()

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "folder": self.folder,
            "date_from": self.date_from.isoformat() if self.date_from else None,
            "date_to": self.date_to.isoformat() if self.date_to else None,
            "tags": list(self.tags),
            "sender": self.sender,
            "limit": self.limit,
            "offset": self.offset,
        }


# key:value, key:"quoted value", "phrase" (closing quote optional) or a plain word
_TOKEN_RE = re.compile(
    r'(?P<key>[^\s:"]+):(?:"(?P<quoted>[^"]*)"?|(?P<value>[^\s"]*))'
    r'|"(?P<phrase>[^"]*)"?'
    r'|(?P<word>[^\s"]+)'
)
_FILTER_KEYS = {
    "ordner": "folder",
    "folder": "folder",
    "pfad": "folder",
    "tag": "tag",
    "tags": "tag",
    "schlagwort": "tag",
    "von": "sender",
    "absender": "sender",
    "sender": "sender",
    "jahr": "year",
    "year": "year",
    "ab": "from",
    "seit": "from",
    "bis": "to",
    "datum": "date",
    "date": "date",
}
_ISO_DAY_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_DE_DAY_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})")
_ISO_MONTH_RE = re.compile(r"(\d{4})-(\d{1,2})")
_DE_MONTH_RE = re.compile(r"(\d{1,2})[./](\d{4})")
_YEAR_RE = re.compile(r"(\d{4})(?:\s*(?:-|–|\.\.)\s*(\d{4}))?")


def _two_digit_year(value: int) -> int:
    century_cut = today().year % 100 + 1
    return 2000 + value if value <= century_cut else 1900 + value


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    first = date(year, month, 1)
    following = (first + timedelta(days=32)).replace(day=1)
    return first, following - timedelta(days=1)


def _period(value: str) -> tuple[date, date] | None:
    """First and last day of a day, month or year written as ISO or German date."""
    value = value.strip()
    try:
        if m := _ISO_DAY_RE.fullmatch(value):
            day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return day, day
        if m := _DE_DAY_RE.fullmatch(value):
            year = int(m.group(3))
            if len(m.group(3)) == 2:
                year = _two_digit_year(year)
            day = date(year, int(m.group(2)), int(m.group(1)))
            return day, day
        if m := _ISO_MONTH_RE.fullmatch(value):
            return _month_bounds(int(m.group(1)), int(m.group(2)))
        if m := _DE_MONTH_RE.fullmatch(value):
            return _month_bounds(int(m.group(2)), int(m.group(1)))
        if m := _YEAR_RE.fullmatch(value):
            first, last = int(m.group(1)), int(m.group(2) or m.group(1))
            if first > last:
                first, last = last, first
            return date(first, 1, 1), date(last, 12, 31)
    except ValueError:
        return None
    return None


def _apply_filter(query: SearchQuery, key: str, value: str) -> bool:
    if key == "folder":
        query.folder = value
    elif key == "tag":
        if all(t.casefold() != value.casefold() for t in query.tags):
            query.tags.append(value)
    elif key == "sender":
        query.sender = value
    elif key == "year":
        if not _YEAR_RE.fullmatch(value.strip()):
            return False
        span = _period(value)
        if span is None:
            return False
        query.date_from, query.date_to = span
    else:
        span = _period(value)
        if span is None:
            return False
        if key in ("from", "date"):
            query.date_from = span[0]
        if key in ("to", "date"):
            query.date_to = span[1]
    return True


def parse_query(raw: str) -> SearchQuery:
    """Split a search box input into free text and filters.

    ``Strom "Stadtwerke Dresden" ordner:"Wohnung/Strom" tag:Garantie von:Sparkasse
    jahr:2025 ab:2025-01-01 bis:31.12.2025`` – quotes keep phrases together;
    unknown or invalid filters stay part of the text.
    """
    query = SearchQuery()
    words: list[str] = []
    for match in _TOKEN_RE.finditer(raw or ""):
        if match.group("key") is not None:
            key = _FILTER_KEYS.get(match.group("key").casefold())
            quoted = match.group("quoted")
            value = (quoted if quoted is not None else match.group("value") or "").strip()
            if key is None:
                words.append(match.group(0))
            elif value and not _apply_filter(query, key, value):
                words.append(match.group(0))
        elif match.group("phrase") is not None:
            phrase = match.group("phrase").strip()
            if phrase:
                words.append(f'"{phrase}"')
        elif match.group("word"):
            words.append(match.group("word"))
    query.text = " ".join(words)
    if query.date_from and query.date_to and query.date_from > query.date_to:
        query.date_from, query.date_to = query.date_to, query.date_from
    return query


# ---------------------------------------------------------------------------
# Results


@dataclass
class Snippet:
    page: int
    html: str  # HTML-escaped text with <mark>…</mark> around the matches

    def to_dict(self) -> dict[str, Any]:
        return {"page": self.page, "html": self.html}


@dataclass
class SearchHit:
    doc: DocumentRecord
    score: float
    snippets: list[Snippet]
    matched_in: list[str]  # subset of MATCH_ORDER
    pages: list[int]  # pages whose text matches, ascending

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc": self.doc.to_dict(),
            "score": self.score,
            "snippets": [s.to_dict() for s in self.snippets],
            "matched_in": list(self.matched_in),
            "pages": list(self.pages),
        }


@dataclass
class SearchResult:
    hits: list[SearchHit]
    total: int
    query: SearchQuery
    corrections: dict[str, list[str]]  # query term -> words searched instead
    took_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "hits": [h.to_dict() for h in self.hits],
            "total": self.total,
            "query": self.query.to_dict(),
            "corrections": {k: list(v) for k, v in self.corrections.items()},
            "took_ms": self.took_ms,
        }


# ---------------------------------------------------------------------------
# Internals


@dataclass
class _Term:
    raw: str  # as typed, key of SearchResult.corrections
    norm: str
    phrase: bool  # quoted, or several words after normalization ("Kfz-Versicherung")
    alternatives: list[str]  # normalized strings, any of which must match
    quoted: bool = False
    # Space-free strings that may also match the joined text (digit groups and
    # hyphenated words written together). An alternative listed here is searched
    # only this way; the joined text contains every match of the normalized one.
    joined: list[str] = field(default_factory=list)

    def plain(self) -> list[str]:
        """Alternatives matched against the normalized text only."""
        return [alt for alt in self.alternatives if alt not in self.joined]


@dataclass
class _Filters:
    clauses: list[str]
    params: list[Any]


@dataclass
class _Ranked:
    score: float
    record: DocumentRecord
    matched: list[str]
    num: int


@dataclass
class _Vocabulary:
    generation: int
    built_at: float
    build_seconds: float
    words: list[str]  # sorted, normalized
    freq: dict[str, int]
    by_length: dict[int, list[str]]
    display: dict[str, str]  # normalized word -> spelling seen in titles, senders, folders, tags
    # normalized word -> spelling in the page text, filled when a word is first shown
    text_display: dict[str, str] = field(default_factory=dict)

    def with_prefix(self, prefix: str) -> list[str]:
        lo = bisect.bisect_left(self.words, prefix)
        hi = bisect.bisect_left(self.words, prefix + "\U0010ffff")
        return self.words[lo:hi]


_FREE_RE = re.compile(r'"([^"]*)"?|([^\s"]+)')
_WORD_RE = re.compile(r"[^\W_]+")
_SPACE_RE = re.compile(r"\s")
_WS_RUN_RE = re.compile(r"\s+")
# A plain space after a letter or digit: cutting there never splits a word or a
# hyphenated line break, so the normalized prefix is a prefix of the whole.
_SAFE_CUT_RE = re.compile(r"(?<=[^\W_]) ")
_PREFIX_MARGIN = 600
# Room for a match whose original text is longer than its normalized form.
_SPAN_SLACK = 200

_DOC_FIELDS = (
    "num",
    "id",
    "path",
    "folder",
    "filename",
    "ext",
    "title",
    "doc_date",
    "sender",
    "tags",
    "origin_hash",
    "content_hash",
    "xmp_id",
    "has_xmp",
    "size",
    "mtime_ns",
    "inode",
    "page_count",
    "text_status",
    "indexed_at",
)
_COLUMNS = ", ".join(f"d.{name} AS {name}" for name in _DOC_FIELDS)


def _joined_forms(norm: str) -> list[str]:
    """What an unquoted term looks like in the joined text (see :class:`_Term`)."""
    value = norm.replace(" ", "")
    if " " in norm or any(char.isdigit() for char in value):
        # "lohnsteuer bescheinigung" -> "lohnsteuerbescheinigung", "123 456789 x" -> "123456789x"
        return [value] if len(value) >= MIN_JOINED else []
    # A word of letters gains only matches across hyphens there ("kfzversicherung").
    return [value] if len(value) >= 3 else []


def _terms(text: str) -> list[_Term]:
    terms: list[_Term] = []
    seen: set[str] = set()
    for match in _FREE_RE.finditer(text or ""):
        quoted = match.group(1) is not None
        raw = (match.group(1) if quoted else match.group(2)).strip()
        norm = normalize(raw)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        # "e.V." or "Müller-Lüdenscheidt" normalize to several words: match them as a phrase.
        terms.append(
            _Term(
                raw=raw,
                norm=norm,
                phrase=quoted or " " in norm,
                alternatives=[norm],
                quoted=quoted,
                joined=[] if quoted else _joined_forms(norm),
            )
        )
    meaningful = [t for t in terms if t.phrase or t.norm not in STOPWORDS]
    return meaningful or terms


def _correctable(term: _Term) -> bool:
    return not term.phrase and len(term.norm) >= MIN_CORRECTABLE and term.norm.isalpha()


def _compound(term: _Term) -> bool:
    """A closed word that might be written with a space in the documents ("Kfz Versicherung")."""
    return not term.phrase and len(term.norm) >= MIN_COMPOUND and term.norm.isalpha()


def _splits(word: str) -> list[str]:
    return [
        word[:cut] + " " + word[cut:] for cut in range(MIN_COMPOUND_PART, len(word) - MIN_COMPOUND_PART + 1)
    ]


def _fts_quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _fts_any(values: list[str]) -> str:
    return " OR ".join(_fts_quote(v) for v in values)


def _marks(count: int) -> str:
    return ",".join("?" for _ in range(count))


def _folder_key(path: str) -> str:
    parts = unicodedata.normalize("NFC", path).casefold().replace("\\", "/").split("/")
    return "/".join(p.strip() for p in parts if p.strip())


def _usable_word(word: str) -> bool:
    return len(word) <= 40 and not word.isdigit()


@contextmanager
def _read_snapshot(conn: sqlite3.Connection) -> Iterator[None]:
    """Run several queries against one consistent state of the index."""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _fts_lookups(term: _Term) -> list[tuple[str, str]]:
    """(FTS table, MATCH expression) pairs; the term occurs where any of them matches."""
    lookups: list[tuple[str, str]] = []
    # The joined text consists of the normalized words plus the words only it
    # contains, so joined forms are looked up in both.
    normal = [a for a in term.plain() if len(a) >= 3] + term.joined
    if normal:
        match = _fts_any(normal)
        lookups += [("fts_docs", match), ("fts_pages", match)]
    if term.joined:
        match = _fts_any(term.joined)
        lookups += [("fts_docs_joined", match), ("fts_joined", match)]
    return lookups


_LOOKUP_SQL = {
    "fts_docs": "SELECT rowid FROM fts_docs WHERE fts_docs MATCH ?",
    "fts_docs_joined": "SELECT rowid FROM fts_docs_joined WHERE fts_docs_joined MATCH ?",
    "fts_pages": "SELECT doc_num FROM pages WHERE rowid IN (SELECT rowid FROM fts_pages WHERE fts_pages MATCH ?)",
    "fts_joined": "SELECT doc_num FROM pages WHERE rowid IN (SELECT rowid FROM fts_joined WHERE fts_joined MATCH ?)",
}


def _term_subquery(term: _Term) -> tuple[str, list[Any]]:
    """Numbers of all documents in which one of the term's alternatives occurs."""
    parts: list[str] = []
    params: list[Any] = []
    for table, match in _fts_lookups(term):
        parts.append(_LOOKUP_SQL[table])
        params.append(match)
    for alt in term.plain():
        if len(alt) >= 3:
            continue
        # Trigram indexes cannot find strings shorter than three characters.
        parts.append("SELECT num FROM documents WHERE instr(search_norm, ?) > 0")
        parts.append("SELECT doc_num FROM pages WHERE instr(norm, ?) > 0")
        params += [alt, alt]
    return " UNION ".join(parts), params


def _position_factor(text: str, pos: int, length: int) -> float:
    starts_word = pos == 0 or text[pos - 1] == " "
    end = pos + length
    ends_word = end == len(text) or text[end] == " "
    if starts_word and ends_word:
        return 1.2
    if starts_word:
        return 1.1
    return 1.0


def _field_sources(record: DocumentRecord) -> tuple[tuple[str, str], ...]:
    # "|" keeps folder names and tags apart in the joined form, like db.search_joined.
    return (
        ("title", record.title),
        ("sender", record.sender or ""),
        ("folder", record.folder.replace("/", " | ")),
        ("tags", " | ".join(record.tags)),
        ("filename", record.filename),
    )


def _score(
    record: DocumentRecord,
    terms: list[_Term],
    page_stat: tuple[float, int] | None,
    search_norm: str = "",
    search_joined: str = "",
) -> tuple[float, list[str]]:
    score = 0.0
    matched: set[str] = set()
    # search_norm (and search_joined, the words only the joined form has) hold
    # all fields at once; when no term occurs in them, no field can match.
    in_joined = not search_norm or any(j in search_joined for t in terms for j in t.joined)
    in_fields = (
        in_joined
        or any(alt in search_norm for t in terms for alt in t.plain())
        or any(j in search_norm for t in terms for j in t.joined)
    )
    fields: list[tuple[str, str, str]] = []
    if in_fields:
        for name, source in _field_sources(record):
            text = normalize(source)
            # Only needed for matches across separators; otherwise the normalized text is the same.
            fields.append((name, text, joined(source) if in_joined and text else text))
    for term in terms:
        best = 0.0
        count = 0
        for name, text, joined_text in fields:
            if not text:
                continue
            weight = 0.0
            for target, alternatives in ((text, term.plain()), (joined_text, term.joined)):
                for alt in alternatives:
                    pos = target.find(alt)
                    if pos >= 0:
                        weight = max(weight, FIELD_WEIGHTS[name] * _position_factor(target, pos, len(alt)))
            if weight:
                count += 1
                best = max(best, weight)
                matched.add(name)
        if count:
            score += best + EXTRA_FIELD_BONUS * (count - 1)
    if page_stat is not None:
        best_rank, pages = page_stat
        relevance = max(0.0, -float(best_rank or 0.0))
        text_score = 3.0 * relevance / (relevance + 8.0) + min(1.0, math.log1p(pages) / math.log(20))
        # Coarse steps, so that documents of similar relevance are ordered by date.
        score += round(text_score * 2) / 2
        matched.add("text")
    return score, [name for name in MATCH_ORDER if name in matched]


def _rank_key(item: _Ranked) -> tuple[Any, ...]:
    doc_date = item.record.doc_date
    return (
        -round(item.score, 6),
        doc_date is None,
        -(doc_date.toordinal() if doc_date else 0),
        item.record.title.casefold(),
        item.record.path,
    )


def _find_similar(norm: str, vocab: _Vocabulary) -> list[str]:
    """Vocabulary words within a small edit distance of ``norm``, best first."""
    limit = 2 if len(norm) >= 9 else 1
    length = len(norm)
    letters = set(norm)
    found: dict[str, int] = {}
    for size in range(max(1, length - limit), length + limit + 1):
        for word in vocab.by_length.get(size, ()):
            if word == norm:
                continue
            word_letters = set(word)
            # Each edit adds or removes at most one distinct letter.
            if len(word_letters - letters) > limit or len(letters - word_letters) > limit:
                continue
            distance = bounded_levenshtein(norm, word, limit)
            if distance <= limit:
                found[word] = distance
    if not found:
        # The term may be the beginning of a longer word with a typo, e.g.
        # "nebenkostn" for "nebenkostenabrechnung". Typos rarely hit the first letters.
        for word in vocab.with_prefix(norm[:2]):
            if len(word) < length - limit:
                continue
            best = limit + 1
            for cut in range(max(1, length - limit), min(len(word), length + limit) + 1):
                best = min(best, bounded_levenshtein(norm, word[:cut], limit))
                if best == 0:
                    break
            if best <= limit:
                found[word] = best
    ranked = sorted(found, key=lambda w: (found[w], -vocab.freq.get(w, 0), len(w), w))
    return ranked[:MAX_ALTERNATIVES]


def _find_all(text: str, value: str) -> Iterator[int]:
    """Start of every non-overlapping occurrence of ``value`` in ``text``."""
    start = 0
    while value and (pos := text.find(value, start)) >= 0:
        yield pos
        start = pos + len(value)


def _occurrences(
    norm: str, terms: list[_Term], joined_text: tuple[str, list[int] | None] | None = None
) -> list[tuple[int, int]]:
    """(start, end) of every occurrence of every alternative in a normalized text, sorted.

    ``joined_text`` is the joined form of the same text and, if known, the index
    into ``norm`` of each of its characters; without the index, positions of
    matches in the joined text are only approximate. Without ``joined_text``
    the joined forms are looked up in ``norm``.
    """
    found: list[tuple[int, int]] = []
    for term in terms:
        for alt in term.plain():
            found += ((pos, pos + len(alt)) for pos in _find_all(norm, alt))
        if not term.joined:
            continue
        text, index = joined_text if joined_text is not None else (norm, None)
        for value in term.joined:
            for pos in _find_all(text, value):
                end = pos + len(value)
                found.append((index[pos], index[end - 1] + 1) if index is not None else (pos, end))
    found.sort()
    return found


def _needs_joined(terms: list[_Term]) -> bool:
    return any(term.joined for term in terms)


def _spans(
    source: str, norm: str, offsets: list[int], terms: list[_Term]
) -> tuple[list[tuple[int, int]], int]:
    """Merged character ranges of ``source`` where terms match, and the number of occurrences.

    ``(norm, offsets)`` is ``normalize_with_map(source)``.
    """
    joined_text = join_map(source, norm, offsets) if _needs_joined(terms) else None
    occurrences = _occurrences(norm, terms, joined_text)
    merged: list[tuple[int, int]] = []
    for begin, end in ((offsets[a], offsets[b - 1] + 1) for a, b in occurrences):
        if merged and begin <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((begin, end))
    return merged, len(occurrences)


def _match_spans(source: str, terms: list[_Term]) -> list[tuple[int, int]]:
    """Character ranges of ``source`` (NFC) where a term matches, merged and sorted."""
    norm, offsets = normalize_with_map(source)
    return _spans(source, norm, offsets, terms)[0]


def _page_windows(
    source: str, norm: str, terms: list[_Term], *, wanted: int
) -> list[tuple[int, int, list[tuple[int, int]]]]:
    """The first ``wanted`` snippet windows of one page (``norm`` is its stored normalized text).

    Mapping normalized positions back to the original text is the expensive
    part of a search, so for long pages only a prefix is mapped: up to a word
    boundary a good stretch after the estimated position of the last match the
    windows need. The prefix is used only if the result is provably the same
    as for the whole page; otherwise the whole page is mapped.
    """
    size = len(source)
    if size > 2 * _PREFIX_MARGIN and norm:
        joined_page: tuple[str, None] | None = None
        occurrences = _occurrences(norm, terms)
        if not occurrences and _needs_joined(terms):
            # Matches across separators only ("123 456 7890"): estimate from the joined text.
            joined_page = (joined(source), None)
            occurrences = _occurrences(norm, terms, joined_page)
        # Matches of the first ``wanted`` clusters (a cluster spans one snippet length).
        last_end, clusters, cluster_start = 0, 0, -SNIPPET_CHARS
        for begin, end in occurrences:
            if begin >= cluster_start + SNIPPET_CHARS:
                if clusters == wanted:
                    break
                clusters, cluster_start = clusters + 1, begin
            last_end = max(last_end, end)
        if occurrences:
            estimate = last_end * size // len(norm)
            cut = _SAFE_CUT_RE.search(source, min(size, estimate + _PREFIX_MARGIN))
            if cut is not None:
                limit = cut.start()
                prefix = source[:limit]
                prefix_norm, offsets = normalize_with_map(prefix)
                spans, found = _spans(prefix, prefix_norm, offsets, terms)
                windows = _windows(source, spans, size=limit)
                taken = windows[:wanted]
                # None of the windows reaches the cut, and there are enough of them
                # (or the prefix holds every match of the page).
                if taken and taken[-1][0] + SNIPPET_CHARS + _SPAN_SLACK <= limit:
                    if len(windows) >= wanted:
                        return taken
                    if _needs_joined(terms) and joined_page is None:
                        joined_page = (joined(source), None)
                    if found == len(_occurrences(norm, terms, joined_page)):
                        return taken
    return _windows(source, _match_spans(source, terms))[:wanted]


def _windows(
    source: str, spans: list[tuple[int, int]], *, size: int | None = None
) -> list[tuple[int, int, list[tuple[int, int]]]]:
    """Cut ``source[:size]`` into snippet windows of about SNIPPET_CHARS around the spans."""
    windows: list[tuple[int, int, list[tuple[int, int]]]] = []
    size = len(source) if size is None else size
    i = 0
    while i < len(spans):
        first_start, first_end = spans[i]
        start = max(0, first_start - SNIPPET_LEAD)
        if start > 0 and not source[start - 1].isspace():
            space = _SPACE_RE.search(source, start, first_start)
            if space is not None:
                start = space.end()
        limit = min(size, max(start + SNIPPET_CHARS, first_end))
        end = limit
        inside: list[tuple[int, int]] = []
        while i < len(spans) and spans[i][0] < limit:
            inside.append(spans[i])
            end = max(end, spans[i][1])  # never cut a highlighted match
            i += 1
        if end < size and not source[end].isspace():
            floor = inside[-1][1]
            for k in range(end - 1, floor - 1, -1):
                if source[k].isspace():
                    end = k
                    break
        windows.append((start, end, inside))
    return windows


def _render(source: str, window: tuple[int, int, list[tuple[int, int]]]) -> str:
    start, end, spans = window
    pieces: list[tuple[str, bool]] = []
    pos = start
    for begin, finish in spans:
        begin = max(begin, pos)
        if begin > pos:
            pieces.append((source[pos:begin], False))
        if finish > begin:
            pieces.append((source[begin:finish], True))
        pos = max(pos, finish)
    if pos < end:
        pieces.append((source[pos:end], False))
    out: list[str] = []
    for k, (text, marked) in enumerate(pieces):
        text = _WS_RUN_RE.sub(" ", text)
        if not marked and k == 0:
            text = text.lstrip()
        if not marked and k == len(pieces) - 1:
            text = text.rstrip()
        escaped = html.escape(text)
        out.append(f"<mark>{escaped}</mark>" if marked else escaped)
    body = "".join(out)
    if start > 0:
        body = "… " + body
    if end < len(source):
        body += " …"
    return body


# ---------------------------------------------------------------------------


class Searcher:
    """Full-text search over the index. Safe to use from several threads."""

    # The cached vocabulary is rebuilt whenever the index changed. Only if a
    # rebuild is expensive (large archives) may it lag behind by up to this many
    # seconds, because during a large import the index changes constantly.
    vocabulary_max_staleness = 1.0
    expensive_rebuild_seconds = 0.1

    def __init__(self, services: Any):
        self.services = services
        self._vocab_lock = threading.Lock()
        self._vocab: _Vocabulary | None = None
        self._similar: dict[str, list[str]] = {}

    # -- public API ----------------------------------------------------------------
    def search(self, query: SearchQuery | str) -> SearchResult:
        """Run a search. Inline filters in ``query.text`` are merged with the explicit ones."""
        started = time.monotonic()
        effective = self._effective(query)
        terms = _terms(effective.text)
        conn = self.services.index.conn()
        with _read_snapshot(conn):
            hits, total, corrections = self._execute(conn, effective, terms)
        took_ms = int(round((time.monotonic() - started) * 1000))
        log.debug("search %r: %d hits in %d ms", effective.text, total, took_ms)
        return SearchResult(hits=hits, total=total, query=effective, corrections=corrections, took_ms=took_ms)

    def suggest_terms(self, prefix: str, limit: int = 8) -> list[str]:
        """Words from the archive that start like the last word of ``prefix``, frequent first,
        spelled as they are written there ("Überweisung", not "ueberweisung")."""
        norm = normalize(prefix or "")
        if not norm:
            return []
        last = norm.split()[-1]
        if len(last) < 2:
            return []
        try:
            limit = max(1, min(int(limit), 50))
        except (TypeError, ValueError):
            limit = 8
        vocab = self._vocabulary()
        words = vocab.with_prefix(last)
        words.sort(key=lambda w: (-vocab.freq.get(w, 0), len(w), w))
        result: list[str] = []
        conn = self.services.index.conn()
        for word in words:
            shown = self._display_word(conn, vocab, word)
            if shown not in result:
                result.append(shown)
            if len(result) >= limit:
                break
        return result

    # -- query preparation -------------------------------------------------------------
    @staticmethod
    def _effective(query: SearchQuery | str) -> SearchQuery:
        base = SearchQuery(text=query) if isinstance(query, str) else query
        inline = parse_query(base.text or "")
        tags: list[str] = []
        for tag in [*(base.tags or []), *inline.tags]:
            tag = str(tag).strip()
            key = normalize(tag)
            if key and all(normalize(t) != key for t in tags):
                tags.append(tag)
        folder = base.folder if base.folder is not None and str(base.folder).strip(" /") else inline.folder
        folder = (folder or "").replace("\\", "/").strip().strip("/") or None
        sender = (base.sender or inline.sender or "").strip() or None
        if sender and not normalize(sender):
            sender = None
        date_from = base.date_from or inline.date_from
        date_to = base.date_to or inline.date_to
        if date_from and date_to and date_from > date_to:
            date_from, date_to = date_to, date_from
        try:
            limit = int(base.limit)
        except (TypeError, ValueError):
            limit = DEFAULT_LIMIT
        limit = DEFAULT_LIMIT if limit <= 0 else min(limit, MAX_LIMIT)
        try:
            offset = max(0, int(base.offset or 0))
        except (TypeError, ValueError):
            offset = 0
        return SearchQuery(
            text=inline.text,
            folder=folder,
            date_from=date_from,
            date_to=date_to,
            tags=tags,
            sender=sender,
            limit=limit,
            offset=offset,
        )

    def _filters(self, query: SearchQuery) -> _Filters | None:
        """SQL conditions for the filters; None if a filter can match nothing."""
        index = self.services.index
        clauses: list[str] = []
        params: list[Any] = []
        if query.folder:
            parts = []
            for folder in self._resolve_folders(query.folder):
                parts.append("(d.folder = ? OR substr(d.folder, 1, ?) = ?)")
                params += [folder, len(folder) + 1, folder + "/"]
            clauses.append("(" + " OR ".join(parts) + ")")
        if query.date_from:
            clauses.append("d.doc_date >= ?")
            params.append(query.date_from.isoformat())
        if query.date_to:
            clauses.append("d.doc_date <= ?")
            params.append(query.date_to.isoformat())
        if query.sender:
            needle = normalize(query.sender)
            senders = [name for name, _ in index.senders() if needle in normalize(name)]
            if not senders:
                return None
            clauses.append(f"d.sender IN ({_marks(len(senders))})")
            params += senders
        if query.tags:
            known_tags = [name for name, _ in index.tags()]
            for tag in query.tags:
                key = normalize(tag)
                variants = [name for name in known_tags if normalize(name) == key]
                if not variants:
                    return None
                clauses.append(f"EXISTS (SELECT 1 FROM json_each(d.tags) AS t WHERE t.value IN ({_marks(len(variants))}))")
                params += variants
        return _Filters(clauses, params)

    def _resolve_folders(self, wanted: str) -> list[str]:
        """Folder paths meant by a filter value: exact, then case-insensitive, then by trailing components."""
        known = self.services.index.list_folders()
        if wanted in known:
            return [wanted]
        key = _folder_key(wanted)
        same = [f for f in known if _folder_key(f) == key]
        if same:
            return same
        suffix = "/" + key
        tail = [f for f in known if _folder_key(f).endswith(suffix)]
        return tail or [wanted]

    # -- execution ---------------------------------------------------------------------
    def _execute(
        self, conn: sqlite3.Connection, query: SearchQuery, terms: list[_Term]
    ) -> tuple[list[SearchHit], int, dict[str, list[str]]]:
        filters = self._filters(query)
        if filters is None:
            return [], 0, {}
        if not terms:
            if not query.has_filters():
                return [], 0, {}
            return self._list_by_date(conn, query, filters)
        corrections: dict[str, list[str]] = {}
        for term in terms:
            if not (_correctable(term) or _compound(term)) or self._has_hit(conn, term):
                continue
            spaced = self._spaced_compounds(conn, term.norm) if _compound(term) else []
            if spaced:
                # "Kfzversicherung" written as "Kfz Versicherung".
                term.alternatives, term.joined = spaced, []
                corrections[term.raw] = self._display_forms(spaced)
                continue
            similar = self._similar_words(term.norm) if _correctable(term) else []
            if similar:
                term.alternatives = similar
                term.joined = [form for word in similar for form in _joined_forms(word)]
                corrections[term.raw] = self._display_forms(similar)
        rows = self._candidates(conn, terms, filters)
        if not rows:
            return [], 0, corrections
        page_stats = self._page_stats(conn, terms)
        ranked: list[_Ranked] = []
        for row in rows:
            record = row_to_record(row)
            score, matched = _score(
                record, terms, page_stats.get(row["num"]), row["search_norm"] or "", row["search_joined"] or ""
            )
            ranked.append(_Ranked(score=score, record=record, matched=matched, num=row["num"]))
        ranked.sort(key=_rank_key)
        window = ranked[query.offset : query.offset + query.limit]
        hits = [self._hit(conn, item, terms) for item in window]
        return hits, len(ranked), corrections

    def _list_by_date(
        self, conn: sqlite3.Connection, query: SearchQuery, filters: _Filters
    ) -> tuple[list[SearchHit], int, dict[str, list[str]]]:
        where = " AND ".join(filters.clauses) or "1"
        total = conn.execute(f"SELECT COUNT(*) FROM documents d WHERE {where}", filters.params).fetchone()[0]
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM documents d WHERE {where} "
            "ORDER BY d.doc_date IS NULL, d.doc_date DESC, d.title COLLATE NOCASE, d.path LIMIT ? OFFSET ?",
            [*filters.params, query.limit, query.offset],
        ).fetchall()
        hits = [SearchHit(doc=row_to_record(r), score=0.0, snippets=[], matched_in=[], pages=[]) for r in rows]
        return hits, total, {}

    @staticmethod
    def _has_hit(conn: sqlite3.Connection, term: _Term) -> bool:
        for table, match in _fts_lookups(term):
            if conn.execute(f"SELECT 1 FROM {table} WHERE {table} MATCH ? LIMIT 1", (match,)).fetchone():
                return True
        return False

    def _spaced_compounds(self, conn: sqlite3.Connection, word: str) -> list[str]:
        """Spellings of ``word`` with one space ("kfz versicherung") that occur in the archive."""
        splits = _splits(word)
        if not splits:
            return []

        def occurs(match: str) -> bool:
            return any(
                conn.execute(f"SELECT 1 FROM {table} WHERE {table} MATCH ? LIMIT 1", (match,)).fetchone()
                for table in ("fts_docs", "fts_pages")
            )

        if not occurs(_fts_any(splits)):
            return []
        return [split for split in splits if occurs(_fts_quote(split))][:MAX_ALTERNATIVES]

    @staticmethod
    def _candidates(conn: sqlite3.Connection, terms: list[_Term], filters: _Filters) -> list[sqlite3.Row]:
        clauses = list(filters.clauses)
        params = list(filters.params)
        for term in terms:
            subquery, sub_params = _term_subquery(term)
            clauses.append(f"d.num IN ({subquery})")
            params += sub_params
        sql = (
            f"SELECT {_COLUMNS}, d.search_norm AS search_norm, d.search_joined AS search_joined "
            f"FROM documents d WHERE {' AND '.join(clauses)}"
        )
        return conn.execute(sql, params).fetchall()

    @staticmethod
    def _page_stats(conn: sqlite3.Connection, terms: list[_Term]) -> dict[int, tuple[float, int]]:
        """doc num -> (best bm25 of its pages, number of matching pages) for the trigram terms."""
        plain = {a for t in terms for a in t.plain() if len(a) >= 3}
        forms = {j for t in terms for j in t.joined}
        if not plain and not forms:
            return {}
        # bm25() cannot be evaluated inside a join or aggregate; materialize it first.
        selects = ["SELECT rowid AS r, bm25(fts_pages) AS s FROM fts_pages WHERE fts_pages MATCH ?"]
        params = [_fts_any(sorted(plain | forms))]
        if forms:
            selects.append("SELECT rowid AS r, bm25(fts_joined) AS s FROM fts_joined WHERE fts_joined MATCH ?")
            params.append(_fts_any(sorted(forms)))
        scored = ", ".join(f"m{i} AS MATERIALIZED ({sql})" for i, sql in enumerate(selects))
        union = " UNION ALL ".join(f"SELECT r, s FROM m{i}" for i in range(len(selects)))
        rows = conn.execute(
            f"WITH {scored}, m AS (SELECT r, MIN(s) AS s FROM ({union}) GROUP BY r) "
            "SELECT p.doc_num AS num, MIN(m.s) AS best, COUNT(*) AS n FROM m JOIN pages p ON p.rowid = m.r "
            "GROUP BY p.doc_num",
            params,
        ).fetchall()
        return {row["num"]: (row["best"], row["n"]) for row in rows}

    def _hit(self, conn: sqlite3.Connection, item: _Ranked, terms: list[_Term]) -> SearchHit:
        page_hits: list[tuple[int, int, int]] = []  # (page, terms found, occurrences)
        norms: dict[int, str] = {}
        rows = conn.execute("SELECT page, norm, joined FROM pages WHERE doc_num = ? ORDER BY page", (item.num,))
        for row in rows:
            norm = row["norm"] or ""
            extra = row["joined"] or ""  # words only the joined form of the page has
            found = occurrences = 0
            for term in terms:
                count = sum(norm.count(alt) for alt in term.plain())
                count += sum(norm.count(value) or extra.count(value) for value in term.joined)
                if count:
                    found += 1
                    occurrences += count
            if found:
                page_hits.append((row["page"], found, occurrences))
                norms[row["page"]] = norm
        matched = set(item.matched)
        if page_hits:
            matched.add("text")
        return SearchHit(
            doc=item.record,
            score=round(item.score, 3),
            snippets=self._snippets(conn, item.num, page_hits, norms, terms),
            matched_in=[name for name in MATCH_ORDER if name in matched],
            pages=[page for page, _, _ in page_hits],
        )

    @staticmethod
    def _snippets(
        conn: sqlite3.Connection,
        num: int,
        page_hits: list[tuple[int, int, int]],
        norms: dict[int, str],
        terms: list[_Term],
    ) -> list[Snippet]:
        if not page_hits:
            return []
        best_pages = sorted(page_hits, key=lambda h: (-h[1], -h[2], h[0]))[:MAX_SNIPPETS]
        # With fewer matching pages than snippets, further windows of the same pages are shown.
        wanted = MAX_SNIPPETS - len(best_pages) + 1
        candidates: list[tuple[int, str, list[tuple[int, int, list[tuple[int, int]]]]]] = []
        for page, _, _ in best_pages:
            row = conn.execute("SELECT text FROM pages WHERE doc_num = ? AND page = ?", (num, page)).fetchone()
            if row is None:
                continue
            source = unicodedata.normalize("NFC", row["text"] or "")
            windows = _page_windows(source, norms.get(page, ""), terms, wanted=wanted)
            if windows:
                candidates.append((page, source, windows))
        # One snippet for each of the best pages first, further matches on them after that.
        selected = [(page, source, windows[0]) for page, source, windows in candidates]
        for page, source, windows in candidates:
            for window in windows[1:]:
                if len(selected) >= MAX_SNIPPETS:
                    break
                selected.append((page, source, window))
        selected = selected[:MAX_SNIPPETS]
        selected.sort(key=lambda s: (s[0], s[2][0]))
        return [Snippet(page=page, html=_render(source, window)) for page, source, window in selected]

    # -- vocabulary ------------------------------------------------------------------------
    def _vocabulary(self) -> _Vocabulary:
        generation = self.services.index.generation
        with self._vocab_lock:
            vocab = self._vocab
            if vocab is not None:
                if vocab.generation == generation:
                    return vocab
                recent = time.monotonic() - vocab.built_at < self.vocabulary_max_staleness
                if recent and vocab.build_seconds >= self.expensive_rebuild_seconds:
                    return vocab
            vocab = self._build_vocabulary(generation)
            self._vocab = vocab
            self._similar.clear()
            return vocab

    def _build_vocabulary(self, generation: int) -> _Vocabulary:
        started = time.monotonic()
        index = self.services.index
        freq: dict[str, int] = {}
        for term, docs in index.vocabulary(min_length=4):
            if _usable_word(term):
                freq[term] = docs
        display: dict[str, str] = {}
        for row in index.conn().execute("SELECT title, sender, folder, tags FROM documents"):
            texts = [row["title"] or "", row["sender"] or "", (row["folder"] or "").replace("/", " ")]
            try:
                texts += [str(t) for t in json.loads(row["tags"] or "[]")]
            except ValueError:
                pass
            for text in texts:
                for word in _WORD_RE.findall(text):
                    norm = normalize(word)
                    if len(norm) < 3 or " " in norm or not _usable_word(norm):
                        continue
                    freq[norm] = freq.get(norm, 0) + 1
                    current = display.get(norm)
                    if current is None or (current.isupper() and not word.isupper()):
                        display[norm] = word
        words = sorted(freq)
        by_length: dict[int, list[str]] = {}
        for word in words:
            by_length.setdefault(len(word), []).append(word)
        finished = time.monotonic()
        return _Vocabulary(
            generation=generation,
            built_at=finished,
            build_seconds=finished - started,
            words=words,
            freq=freq,
            by_length=by_length,
            display=display,
        )

    def _similar_words(self, norm: str) -> list[str]:
        vocab = self._vocabulary()
        with self._vocab_lock:
            cached = self._similar.get(norm) if self._vocab is vocab else None
        if cached is not None:
            return list(cached)
        result = _find_similar(norm, vocab)
        with self._vocab_lock:
            if self._vocab is vocab:
                if len(self._similar) >= 512:
                    self._similar.clear()
                self._similar[norm] = result
        return list(result)

    def _display_forms(self, words: list[str]) -> list[str]:
        """How normalized words (or phrases) are written in the archive, for corrections."""
        vocab = self._vocabulary()
        conn = self.services.index.conn()
        return [" ".join(self._display_word(conn, vocab, part) for part in word.split()) for word in words]

    @staticmethod
    def _display_word(conn: sqlite3.Connection, vocab: _Vocabulary, word: str) -> str:
        """A normalized word as written in the archive ("ueberweisung" -> "Überweisung").

        Titles, senders, folders and tags come first; other words are looked up
        in a few pages that contain them, and the most frequent spelling wins
        (not all in capitals if possible). Unknown words are returned as they are.
        """
        shown = vocab.display.get(word) or vocab.text_display.get(word)
        if shown:
            return shown
        counts: dict[str, int] = {}
        try:
            rows = conn.execute(
                "SELECT text FROM pages WHERE rowid IN "
                "(SELECT rowid FROM fts_words WHERE fts_words MATCH ? LIMIT ?)",
                (_fts_quote(word), DISPLAY_SAMPLE_PAGES),
            ).fetchall()
        except sqlite3.Error:
            log.debug("cannot look up the spelling of %r", word, exc_info=True)
            rows = []
        for row in rows:
            for spelling in spellings(word, row["text"] or ""):
                counts[spelling] = counts.get(spelling, 0) + 1
        shown = min(counts, key=lambda s: (s.isupper(), -counts[s], s)) if counts else word
        if len(vocab.text_display) >= 20_000:
            vocab.text_display.clear()
        vocab.text_display[word] = shown
        return shown
