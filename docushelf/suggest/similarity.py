"""Folder suggestions from similar documents in the archive.

The archive itself is the training material: every filed document is a
labelled example ("this kind of text lives in this folder"). The model is a
TF-IDF over content words with cosine similarity, written in plain Python:

* every document becomes a sparse, L2-normalized vector (term id -> weight),
  limited to its most important terms,
* an inverted index (term -> documents with their weights) yields the dot
  products for a query without looking at unrelated documents,
* the model is rebuilt lazily when ``index.generation`` changes, at most every
  ``rebuild_interval`` seconds; term counts of unchanged documents are reused,
  so a rebuild after filing one document is cheap.
"""

from __future__ import annotations

import hashlib
import heapq
import logging
import math
import re
import threading
import time
from array import array
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from operator import itemgetter
from pathlib import PurePosixPath
from typing import Any

from ..analysis.extract import FULL_MONTH_NAMES, FULL_MONTH_WORDS, MONTHS, entity_key
from ..models import Suggestion
from ..util.textnorm import content_words, normalize
from ..util.timeutil import parse_date

log = logging.getLogger(__name__)

NEIGHBOURS = 7
MAX_TERMS_PER_DOC = 300
MAX_QUERY_TERMS = 150
MAX_CHARS_PER_DOC = 40_000
MIN_SIMILARITY = 0.08
# Folders scoring below this are noise (shared boilerplate, the user's own address).
MIN_FOLDER_SCORE = 0.2
# Documents at least this similar count as "similar" in the reasons.
SIMILAR_DOCUMENT = 0.3
TITLE_SIMILARITY = 0.5
# Similarity alone never outranks a rule or an imported decision.
MAX_SCORE = 0.94


@dataclass
class _Model:
    generation: int
    doc_ids: list[str]
    folders: list[str]
    titles: list[str]
    sender_keys: list[str]
    term_ids: dict[str, int]
    idf: array
    postings_docs: list[array]
    postings_weights: list[array]
    # sender key -> folder -> number of documents
    sender_folders: dict[str, Counter[str]]
    built_at: float

    @property
    def size(self) -> int:
        return len(self.doc_ids)


def _sender_key(sender: str | None) -> str:
    return entity_key(sender)


def _terms(text: str) -> Counter[str]:
    return Counter(content_words(text[:MAX_CHARS_PER_DOC]))


class SimilarityModel:
    """Nearest-neighbour folder suggestions over the indexed archive (thread-safe)."""

    def __init__(self, services: Any, *, rebuild_interval: float = 30.0):
        self.services = services
        self.rebuild_interval = rebuild_interval
        self._lock = threading.Lock()
        self._model: _Model | None = None
        # doc id -> (fingerprint of its text, term counts, [(term, 1 + log tf)]); reused across rebuilds
        self._term_cache: dict[str, tuple[str, Counter[str], list[tuple[str, float]]]] = {}

    # -- building --------------------------------------------------------------
    def invalidate(self) -> None:
        """Force a rebuild on the next query."""
        with self._lock:
            self._model = None

    def _current(self) -> _Model | None:
        index = self.services.index
        generation = index.generation
        model = self._model
        if model is not None and (
            model.generation == generation or time.monotonic() - model.built_at < self.rebuild_interval
        ):
            return model
        if model is None:
            # Nothing to fall back to: wait for whoever is building.
            with self._lock:
                if self._model is None:
                    self._model = self._build(generation)
                return self._model
        # Stale: one thread rebuilds, the others keep using the previous model.
        if self._lock.acquire(blocking=False):
            try:
                if self._model is model:
                    self._model = self._build(generation)
            except Exception:
                log.exception("rebuilding the similarity model failed")
            finally:
                self._lock.release()
        return self._model

    def _build(self, generation: int) -> _Model:
        started = time.monotonic()
        doc_ids: list[str] = []
        folders: list[str] = []
        titles: list[str] = []
        sender_keys: list[str] = []
        counts: list[list[tuple[str, float]]] = []
        cache: dict[str, tuple[str, Counter[str], list[tuple[str, float]]]] = {}
        for doc_id, folder, title, sender, text in self.services.index.iter_corpus():
            text = text or ""
            extra = normalize(f"{title or ''} {sender or ''}")
            fingerprint = hashlib.blake2b(
                f"{len(text)}|{extra}|".encode() + text[:MAX_CHARS_PER_DOC].encode("utf-8", "replace"),
                digest_size=12,
            ).hexdigest()
            cached = self._term_cache.get(doc_id)
            if cached is not None and cached[0] == fingerprint:
                entry = cached
            else:
                terms = _terms(text)
                # Title and sender words count once more: they summarize the document.
                terms.update(content_words(extra))
                if len(terms) > MAX_TERMS_PER_DOC * 3:
                    terms = Counter(dict(terms.most_common(MAX_TERMS_PER_DOC * 3)))
                entry = (fingerprint, terms, [(term, 1.0 + math.log(tf)) for term, tf in terms.items()])
            cache[doc_id] = entry
            doc_ids.append(doc_id)
            folders.append(folder or "")
            titles.append(title or "")
            sender_keys.append(_sender_key(sender))
            counts.append(entry[2])
        self._term_cache = cache

        total = len(doc_ids)
        document_frequency: Counter[str] = Counter()
        for terms in counts:
            document_frequency.update(term for term, _ in terms)
        term_ids: dict[str, int] = {}
        idf = array("d")
        for term, df in document_frequency.items():
            term_ids[term] = len(idf)
            idf.append(math.log((total + 1) / (df + 1)) + 1.0)

        postings_docs: list[array] = [array("i") for _ in range(len(idf))]
        postings_weights: list[array] = [array("f") for _ in range(len(idf))]
        weight_of = itemgetter(1)
        for doc_index, terms in enumerate(counts):
            weighted = [(term_ids[term], tfw * idf[term_ids[term]]) for term, tfw in terms]
            if len(weighted) > MAX_TERMS_PER_DOC:
                weighted.sort(key=weight_of, reverse=True)
                del weighted[MAX_TERMS_PER_DOC:]
            norm = math.sqrt(sum(weight * weight for _, weight in weighted)) or 1.0
            for term_id, weight in weighted:
                postings_docs[term_id].append(doc_index)
                postings_weights[term_id].append(weight / norm)

        sender_folders: dict[str, Counter[str]] = defaultdict(Counter)
        for key, folder in zip(sender_keys, folders):
            if key:
                sender_folders[key][folder] += 1

        model = _Model(
            generation=generation,
            doc_ids=doc_ids,
            folders=folders,
            titles=titles,
            sender_keys=sender_keys,
            term_ids=term_ids,
            idf=idf,
            postings_docs=postings_docs,
            postings_weights=postings_weights,
            sender_folders=dict(sender_folders),
            built_at=time.monotonic(),
        )
        log.debug("similarity model: %d documents, %d terms in %.2fs", total, len(idf), time.monotonic() - started)
        return model

    # -- querying --------------------------------------------------------------
    def neighbours(self, normalized_text: str, *, limit: int = NEIGHBOURS) -> list[tuple[str, str, str, float]]:
        """The most similar archived documents: (doc_id, folder, title, similarity)."""
        model = self._current()
        if model is None or model.size == 0:
            return []
        return [
            (model.doc_ids[i], model.folders[i], model.titles[i], score)
            for i, score in self._scores(model, normalized_text, limit)
        ]

    def _scores(self, model: _Model, normalized_text: str, limit: int) -> list[tuple[int, float]]:
        terms = _terms(normalized_text or "")
        query = [
            (model.term_ids[term], (1.0 + math.log(tf)) * model.idf[model.term_ids[term]])
            for term, tf in terms.items()
            if term in model.term_ids
        ]
        if not query:
            return []
        # The norm includes words the archive has never seen: they make the document less similar.
        unknown = sum(
            ((1.0 + math.log(tf)) * (math.log(model.size + 1) + 1.0)) ** 2
            for term, tf in terms.items()
            if term not in model.term_ids
        )
        norm = math.sqrt(sum(weight * weight for _, weight in query) + unknown) or 1.0
        if len(query) > MAX_QUERY_TERMS:
            query = heapq.nlargest(MAX_QUERY_TERMS, query, key=lambda entry: entry[1])
        scores: dict[int, float] = defaultdict(float)
        for term_id, weight in query:
            q = weight / norm
            for doc_index, doc_weight in zip(model.postings_docs[term_id], model.postings_weights[term_id]):
                scores[doc_index] += q * doc_weight
        best = heapq.nlargest(limit, scores.items(), key=lambda entry: entry[1])
        return [(doc_index, min(1.0, score)) for doc_index, score in best if score >= MIN_SIMILARITY]

    def suggest(
        self,
        normalized_text: str,
        *,
        sender: str | None = None,
        limit: int = 3,
        doc_date: date | str | None = None,
    ) -> list[Suggestion]:
        """Folders of the most similar documents, best first (score 0..1).

        ``doc_date`` (the date of the new document) lets the title of a very
        similar document be reused with month and year adjusted.
        """
        model = self._current()
        if model is None or model.size == 0:
            return []
        neighbours = self._scores(model, normalized_text, NEIGHBOURS)
        key = _sender_key(sender)
        sender_counts = model.sender_folders.get(key, Counter()) if key else Counter()

        by_folder: dict[str, list[tuple[int, float]]] = defaultdict(list)
        for doc_index, score in neighbours:
            folder = model.folders[doc_index]
            if folder:  # the archive root is not a filing target
                by_folder[folder].append((doc_index, score))
        total = sum(score for entries in by_folder.values() for _, score in entries) or 1.0

        results: list[Suggestion] = []
        for folder, entries in by_folder.items():
            entries.sort(key=lambda entry: entry[1], reverse=True)
            best_index, best_score = entries[0]
            share = sum(score for _, score in entries) / total
            score = best_score * (0.75 + 0.25 * share)
            same_sender = sender_counts.get(folder, 0)
            if same_sender:
                score += 0.1 * min(1.0, same_sender / 3)
            if score < MIN_FOLDER_SCORE:
                continue
            reasons = [f"ähnlich zu „{self._label(model, best_index)}“ ({round(best_score * 100)} %)"]
            similar = sum(1 for _, value in entries if value >= SIMILAR_DOCUMENT)
            if similar > 1:
                reasons.append(f"{similar} ähnliche Dokumente in diesem Ordner")
            if same_sender:
                reasons.append(sender_reason(same_sender))
            title = None
            if best_score >= TITLE_SIMILARITY:
                title = self._adapted_title(model, best_index, doc_date)
            results.append(
                Suggestion(
                    folder=folder,
                    title=title,
                    score=round(min(MAX_SCORE, score), 3),
                    source="similarity",
                    reasons=reasons,
                )
            )
        results.sort(key=lambda suggestion: suggestion.score, reverse=True)
        return results[:limit]

    def sender_folders(self, sender: str | None) -> list[tuple[str, int]]:
        """Folders that already hold documents of ``sender``, most first."""
        key = _sender_key(sender)
        model = self._current()
        if not key or model is None:
            return []
        return model.sender_folders.get(key, Counter()).most_common()

    # -- helpers ---------------------------------------------------------------
    def _record(self, model: _Model, doc_index: int):
        try:
            return self.services.index.get(model.doc_ids[doc_index])
        except Exception:  # pragma: no cover - index closed or busy
            log.debug("cannot read neighbour record", exc_info=True)
            return None

    def _label(self, model: _Model, doc_index: int) -> str:
        record = self._record(model, doc_index)
        if record is not None:
            return PurePosixPath(record.filename).stem
        return model.titles[doc_index]

    def _adapted_title(self, model: _Model, doc_index: int, doc_date: date | str | None) -> str | None:
        record = self._record(model, doc_index)
        title = record.title if record is not None else model.titles[doc_index]
        if not title:
            return None
        new_date = parse_date(doc_date)
        old_date = record.doc_date if record is not None else None
        return adapt_title(title, old_date, new_date)


def sender_reason(count: int) -> str:
    if count == 1:
        return "gleicher Absender wie 1 Dokument in diesem Ordner"
    return f"gleicher Absender wie {count} Dokumente in diesem Ordner"


# ---------------------------------------------------------------------------
# Titles of similar documents

_MONTH_WORD_RE = re.compile(
    r"\b(" + "|".join(sorted((re.escape(m) for m in MONTHS), key=len, reverse=True)) + r")\b(\.?)(\s+(\d{4}))?",
    re.IGNORECASE,
)
_NUMERIC_MONTH_RE = re.compile(r"(?<![\d.])(0?[1-9]|1[0-2])([/.\-])((?:19|20)\d{2})(?!\d)")
_ISO_MONTH_RE = re.compile(r"(?<![\d.])((?:19|20)\d{2})-(0[1-9]|1[0-2])(?![\d])(?!-\d)")
_YEAR_RE = re.compile(r"(?<![\d.\-/])((?:19|20)\d{2})(?![\d.\-/])")
_FULL_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.((?:19|20)\d{2})(?!\d)")
_ISO_DATE_RE = re.compile(r"(?<![\d.])((?:19|20)\d{2})-(\d{2})-(\d{2})(?!\d)")


def _is_month_mention(match: re.Match[str]) -> bool:
    """Full month names always; abbreviations ("Jan", "Nov") only with a dot or a year."""
    return match.group(1).casefold() in FULL_MONTH_WORDS or bool(match.group(2) or match.group(3))


def _shift(year: int, month: int, delta: int) -> tuple[int, int]:
    total = year * 12 + (month - 1) + delta
    return total // 12, total % 12 + 1


def adapt_title(title: str, old_date: date | None, new_date: date | None) -> str | None:
    """Reuse a neighbour's title for a new document.

    Months and years in the title are moved by the distance between the two
    document dates ("Kontoauszug November" -> "Kontoauszug Dezember",
    "Steuerbescheid 2023" -> "Steuerbescheid 2024"). Without a new date, a
    title that mentions a date cannot be reused.
    """
    title = (title or "").strip()
    if not title:
        return None
    mentions_date = bool(
        any(_is_month_mention(match) for match in _MONTH_WORD_RE.finditer(title))
        or _NUMERIC_MONTH_RE.search(title) or _ISO_MONTH_RE.search(title) or _ISO_DATE_RE.search(title)
        or _YEAR_RE.search(title) or _FULL_DATE_RE.search(title)
    )
    if not mentions_date:
        return title
    if new_date is None:
        return None
    month_delta = (
        (new_date.year * 12 + new_date.month) - (old_date.year * 12 + old_date.month) if old_date else None
    )
    year_delta = new_date.year - old_date.year if old_date else None

    def full_date(match: re.Match[str]) -> str:
        return new_date.strftime("%d.%m.%Y")

    def month_word(match: re.Match[str]) -> str:
        word, dot, year_part, year_text = match.group(1), match.group(2), match.group(3), match.group(4)
        month = MONTHS.get(word.casefold())
        if month is None or not _is_month_mention(match):
            return match.group(0)
        if month_delta is None:
            new_year, new_month = new_date.year, new_date.month
        else:
            base_year = int(year_text) if year_text else new_date.year
            new_year, new_month = _shift(base_year, month, month_delta)
        name = FULL_MONTH_NAMES[new_month - 1]
        if word.casefold() not in FULL_MONTH_WORDS and len(word) <= 4:
            # The neighbour abbreviated its month ("Jan.", "Sept"); "März" or "Mai" are full names.
            name = name[:3] if name != "März" else "Mär"
        if word.isupper():
            name = name.upper()
        result = name + dot
        if year_part:
            result += f" {new_year}"
        return result

    def numeric_month(match: re.Match[str]) -> str:
        month, sep, year = int(match.group(1)), match.group(2), int(match.group(3))
        if month_delta is None:
            new_year, new_month = new_date.year, new_date.month
        else:
            new_year, new_month = _shift(year, month, month_delta)
        width = len(match.group(1))
        return f"{new_month:0{width}d}{sep}{new_year}"

    def iso_month(match: re.Match[str]) -> str:
        year, month = int(match.group(1)), int(match.group(2))
        if month_delta is None:
            new_year, new_month = new_date.year, new_date.month
        else:
            new_year, new_month = _shift(year, month, month_delta)
        return f"{new_year}-{new_month:02d}"

    def plain_year(match: re.Match[str]) -> str:
        year = int(match.group(1))
        return str(year + year_delta) if year_delta is not None else str(new_date.year)

    # Placeholders keep replaced parts away from the later, more general patterns.
    parts: list[str] = []

    def protect(func):
        def wrapper(match: re.Match[str]) -> str:
            parts.append(func(match))
            return f"\x00{len(parts) - 1}\x00"

        return wrapper

    value = _FULL_DATE_RE.sub(protect(full_date), title)
    value = _ISO_DATE_RE.sub(protect(lambda match: new_date.isoformat()), value)
    value = _MONTH_WORD_RE.sub(protect(month_word), value)
    value = _NUMERIC_MONTH_RE.sub(protect(numeric_month), value)
    value = _ISO_MONTH_RE.sub(protect(iso_month), value)
    value = _YEAR_RE.sub(protect(plain_year), value)
    return re.sub(r"\x00(\d+)\x00", lambda m: parts[int(m.group(1))], value)


__all__ = ["SimilarityModel", "adapt_title"]
