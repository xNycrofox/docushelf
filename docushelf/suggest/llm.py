"""Suggestions from a language model: Ollama, an OpenAI-compatible server or the Anthropic API.

The document text is sent to the configured endpoint and nowhere else. The
default is a local Ollama instance, so nothing leaves the house;
``provider="openai"`` talks to any server with the OpenAI chat-completions API
(llama.cpp server, vLLM, LocalAI, LM Studio …) and ``provider="anthropic"`` to
Claude through the Anthropic API (the document text then leaves the house).

Requests ask for structured output with a JSON schema. A server that rejects
the schema (HTTP 400) is asked again in plain JSON mode, and the client
remembers that for later requests. The prompt lists the archive's folders with
their document counts, example file names from the folders that probably fit,
the most similar archived documents and the facts the analysis found.

Small CPU models answer slowly and sometimes produce nonsense, so every answer
is validated strictly: the folder must exist (or be a new folder that fits the
existing structure), the date must appear in the document, the title is
cleaned and shortened, and the model's own confidence counts only partly.
Any failure means "no suggestion"; :class:`LlmResult` carries the reason in
German.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

import httpx

from ..analysis.extract import MONTHS, parse_date_text
from ..config import LlmConfig
from ..errors import PathError
from ..models import Analysis, Suggestion
from ..util import timeutil
from ..util.names import casefold_key, sanitize_folder
from ..util.textnorm import normalize

log = logging.getLogger(__name__)

# Prompt limits: the folder list, the document and the examples must fit into the
# model's context together (see LlmConfig.context_tokens).
MAX_FOLDERS = 150
TAIL_CHARS = 1000
EXAMPLE_FOLDERS = 5
EXAMPLES_PER_FOLDER = 3
MAX_SIMILAR = 5
MAX_DATE_FACTS = 5
MAX_AMOUNT_FACTS = 5
MAX_REFERENCE_FACTS = 6

# Answer limits.
MAX_TITLE = 100
MAX_SENDER = 80
MAX_DOC_TYPE = 60
MAX_TAGS = 5
MAX_TAG = 40
MAX_REASON = 200

# The model's self-assessment is not trusted fully.
SCORE_FACTOR = 0.9
DEFAULT_CONFIDENCE = 0.5
# Archives with fewer folders than this may get new folders anywhere (a structure is still forming).
SMALL_ARCHIVE = 3

# Keys of the answer, in the order the model writes them: the facts first, the
# reasoning before the folder, the self-assessment after it.
ANSWER_KEYS = ("dokumenttyp", "absender", "datum", "titel", "begruendung", "ordner", "neuer_ordner",
               "sicherheit", "tags")

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "dokumenttyp": {"type": ["string", "null"]},
        "absender": {"type": ["string", "null"]},
        "datum": {"type": ["string", "null"]},
        "titel": {"type": "string"},
        "begruendung": {"type": "string"},
        "ordner": {"type": "string"},
        "neuer_ordner": {"type": "boolean"},
        "sicherheit": {"type": "number"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": list(ANSWER_KEYS),
    "additionalProperties": False,
}
SCHEMA_NAME = "ablage"
# Room for the model's (adaptive) thinking plus the small JSON answer.
ANTHROPIC_MAX_TOKENS = 8000

SYSTEM_PROMPT = """\
Du legst Dokumente eines privaten Haushalts in dessen Ordnerarchiv ab.
Du bekommst den Text eines Dokuments (oft per Texterkennung gelesen, mit Fehlern), die
vorhandenen Ordner mit der Zahl der Dokumente darin, Beispiel-Dateinamen aus wahrscheinlich
passenden Ordnern, ähnliche bereits abgelegte Dokumente und automatisch erkannte Angaben.
Die automatisch erkannten Angaben können falsch sein – maßgeblich ist der Dokumenttext.

Ordner:
- Nimm einen vorhandenen Ordner, wann immer einer passt, und schreibe ihn genau so wie in
  der Liste (ohne die Zahl in Klammern). Ähnliche Dokumente und Ordner mit Dokumenten
  desselben Absenders sind gute Hinweise.
- Nur wenn wirklich keiner passt: "neuer_ordner": true und ein neuer Ordner, der sich in die
  vorhandene Struktur einfügt – gleiche Tiefe und Benennung wie die Nachbarordner, z. B.
  "Versicherungen/<Versicherer>" neben "Versicherungen/Allianz". Lege keinen neuen Ordner auf
  der obersten Ebene an, außer das Archiv ist leer oder fast leer.

Titel:
- Aufbau: "<Dokumentart> <Absender kurz> <Zeitraum oder Betreff>", z. B.
  "Abschlagsrechnung Stadtwerke März 2025" oder "Kontoauszug Sparkasse Februar 2025".
- Kein Datum (es wird dem Dateinamen vorangestellt), keine Dateiendung, höchstens 60 Zeichen,
  deutsch. Ahme den Stil der Beispiel-Dateinamen nach.

Antworte nur mit einem JSON-Objekt mit diesen Schlüsseln:
- "dokumenttyp": Art des Dokuments, z. B. Rechnung, Kontoauszug, Vertrag, Bescheid, oder null
- "absender": Firma, Behörde oder Person, die das Dokument geschickt hat (nicht der
  Empfänger), oder null
- "datum": Ausstellungsdatum als JJJJ-MM-TT (nicht Fälligkeit, Zeitraum oder Buchungstag),
  nur wenn es im Text steht, sonst null
- "titel": der Titel nach der Regel oben
- "begruendung": ein kurzer Satz, warum der Ordner passt
- "ordner": der gewählte Ordner
- "neuer_ordner": true nur für einen neuen Ordner, sonst false
- "sicherheit": Zahl von 0 bis 1 – wie sicher du dir beim Ordner bist
- "tags": höchstens 3 kurze Schlagwörter, darf leer sein

Erfinde nichts, was nicht im Dokument steht."""

# A made-up letter for the connection test (no real person or company).
SAMPLE_LETTER = """\
Stadtwerke Musterstadt GmbH · Beispielallee 1 · 12345 Musterstadt
Frau
Erika Mustermann
Musterweg 7
12345 Musterstadt
Musterstadt, 14.03.2025
Abschlagsrechnung Strom
Kundennummer: 100 200 300
Sehr geehrte Frau Mustermann,
für Ihren Stromliefervertrag berechnen wir ab April 2025 einen monatlichen Abschlag von 68,00 €.
Der Betrag wird am 01.04.2025 von Ihrem Konto abgebucht.
Mit freundlichen Grüßen
Ihre Stadtwerke Musterstadt"""
SAMPLE_FOLDERS = {
    "Finanzen": 0,
    "Finanzen/Bank": 12,
    "Finanzen/Steuern": 4,
    "Versicherungen": 0,
    "Versicherungen/Haftpflicht": 3,
    "Wohnung": 0,
    "Wohnung/Miete": 6,
    "Wohnung/Strom": 9,
}
SAMPLE_EXAMPLES = {
    "Wohnung/Strom": ["2025-01-10 Jahresabrechnung Strom Stadtwerke 2024.pdf"],
    "Wohnung/Miete": ["2024-12-01 Nebenkostenabrechnung Hausverwaltung 2023.pdf"],
}

_NULL_WORDS = {"null", "none", "unbekannt", "n/a", "-", "keine", "kein", "nicht bekannt", "unknown"}
_QUOTES_RE = re.compile(r"[\"„“”«»‚‘`´]")
_EXTENSION_RE = re.compile(r"\.(?:pdf|jpe?g|png|tiff?|heic|webp|docx?|odt|txt)$", re.IGNORECASE)
_DATE_PREFIX_RE = re.compile(r"^(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}\.\s?\d{1,2}\.\s?\d{2,4})(?:[\s_\-–:,]+|$)")
_DATE_SUFFIX_RE = re.compile(r"\s+vom\s+(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}\.\s?\d{1,2}\.\s?\d{2,4})$", re.IGNORECASE)
_COUNT_SUFFIX_RE = re.compile(r"\s*\(\s*\d+(?:\s+[A-Za-zäöü]+)?\s*\)$")
_RETRYABLE = (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError)


# ---------------------------------------------------------------------------
# Request and result


@dataclass
class SimilarDocument:
    """An archived document similar to the new one."""

    name: str  # file name (or title) as shown in the archive
    folder: str
    score: float  # 0..1


@dataclass
class LlmRequest:
    """Everything the prompt is built from.

    ``folders`` are all existing folders (spelled as on disk); the answer is
    validated against them. ``candidates`` are folders that probably fit, best
    first (from rules, similar documents, the sender's folders); they are always
    listed and their ``examples`` (file names) show the naming style.
    """

    text: str
    folders: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    candidates: list[str] = field(default_factory=list)
    examples: dict[str, list[str]] = field(default_factory=dict)
    similar: list[SimilarDocument] = field(default_factory=list)
    analysis: Analysis | None = None
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class LlmResult:
    """Outcome of one classification; ``error`` (German) is set when there is no suggestion."""

    suggestion: Suggestion | None = None
    confidence: float | None = None  # the model's own assessment, 0..1
    answer: dict[str, Any] | None = None  # the JSON object the model returned
    error: str | None = None
    duration_s: float = 0.0


class _LlmError(Exception):
    """A request or answer that gives no suggestion; the message is German.

    ``reachable`` is False when no answer came from the server at all.
    """

    def __init__(self, message: str, *, reachable: bool = True):
        super().__init__(message)
        self.reachable = reachable


# ---------------------------------------------------------------------------
# Cleaning and validation


def _clean_text(value: Any, limit: int) -> str | None:
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = re.sub(r"\s+", " ", str(value)).strip().strip("\"'„“”`")
    if not text or text.casefold() in _NULL_WORDS:
        return None
    if len(text) > limit:
        cut = text[:limit].rsplit(" ", 1)[0]
        text = (cut if len(cut) >= limit // 2 else text[:limit]).rstrip(" ,;:-")
    return text or None


def clean_title(value: Any) -> str | None:
    """A title without date prefix, file extension or quotes, at most 100 characters."""
    title = _clean_text(value, MAX_TITLE * 3)
    if not title:
        return None
    title = _QUOTES_RE.sub("", title).strip(" '’")
    previous = None
    while previous != title:
        previous = title
        title = _EXTENSION_RE.sub("", title).strip()
        # The date goes into the file name separately.
        title = _DATE_PREFIX_RE.sub("", title).strip(" -–_:,")
        title = _DATE_SUFFIX_RE.sub("", title).strip()
    title = title.replace("/", "-").replace("\\", "-")
    return _clean_text(title, MAX_TITLE)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().casefold() in {"true", "ja", "yes", "1", "wahr"}


def parse_confidence(value: Any) -> float:
    """The model's ``sicherheit`` clamped to 0..1 (missing or unreadable: 0.5).

    Percentages are understood: "85 %", and numbers from 10 to 100, mean 0.85.
    """
    if value is None or isinstance(value, (bool, dict, list)):
        return DEFAULT_CONFIDENCE
    if isinstance(value, (int, float)):
        number = float(value)
        percent = False
    else:
        text = str(value).strip().replace(",", ".")
        percent = text.endswith("%")
        match = re.match(r"^-?\d+(?:\.\d+)?", text.rstrip("%").strip())
        if not match:
            return DEFAULT_CONFIDENCE
        number = float(match.group(0))
    if math.isnan(number) or math.isinf(number):
        return DEFAULT_CONFIDENCE
    if percent or 10.0 <= number <= 100.0:
        number /= 100.0
    return max(0.0, min(1.0, number))


def parse_json_object(content: str) -> dict[str, Any] | None:
    """The first JSON object in a model answer (tolerates code fences and chatter)."""
    if not content:
        return None
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        data = json.loads(text)
    except ValueError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


_parse_json_object = parse_json_object


def date_in_text(value: date, text: str) -> bool:
    """True if ``value`` is written in ``text`` (12.03.2025, 12.3.25, 2025-03-12, 12. März 2025, March 12, 2025)."""
    if not text:
        return False
    day = rf"0?{value.day}" if value.day < 10 else str(value.day)
    month = rf"0?{value.month}" if value.month < 10 else str(value.month)
    year = rf"(?:{value.year}|{value.year % 100:02d})"
    names = "|".join(
        sorted((re.escape(name) for name, number in MONTHS.items() if number == value.month), key=len, reverse=True)
    )
    patterns = [
        rf"(?<![\d.,/]){day}\s?\.\s?{month}\s?\.\s?{year}(?![\d,]|\.\d)",
        rf"(?<![\d.\-/]){value.year}-{value.month:02d}-{value.day:02d}(?![\d\-])",
    ]
    if names:
        patterns.append(rf"(?<![\d.]){day}\.?\s*(?:{names})\.?,?\s+{value.year}(?!\d)")
        patterns.append(rf"(?<![\w.])(?:{names})\.?\s+{day}(?:st|nd|rd|th)?,?\s+{value.year}(?!\d)")
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _depth(folder: str) -> int:
    return len([part for part in folder.split("/") if part]) if folder else 0


def _existing_folder(candidate: str, folders: Sequence[str]) -> str | None:
    """The existing folder ``candidate`` names: same path ignoring case (or umlaut spelling);
    a bare folder name also matches a unique folder of that name anywhere."""
    by_key = {casefold_key(folder): folder for folder in folders}
    if casefold_key(candidate) in by_key:
        return by_key[casefold_key(candidate)]

    def loose(folder: str) -> str:
        return normalize(folder.replace("/", " / "))

    by_loose: dict[str, str] = {}
    for folder in folders:
        by_loose.setdefault(loose(folder), folder)
    found = by_loose.get(loose(candidate))
    if found:
        return found
    if "/" not in candidate:
        leaf = casefold_key(candidate)
        matches = [folder for folder in folders if casefold_key(folder.rsplit("/", 1)[-1]) == leaf]
        if len(matches) == 1:
            return matches[0]
    return None


def acceptable_new_folder(path: str, folders: Sequence[str]) -> bool:
    """Whether a new folder proposed by the model fits the existing structure.

    In an archive with at least :data:`SMALL_ARCHIVE` folders the parent must
    exist (so exactly one level is added below an existing folder) and new
    top-level folders are refused. In any case the folder may go at most one
    level deeper than the deepest existing folder around it (below its nearest
    existing ancestor; the whole archive if there is none).
    """
    parts = [part for part in path.split("/") if part]
    if not parts:
        return False
    keys = {casefold_key(folder) for folder in folders}
    if len(folders) >= SMALL_ARCHIVE:
        if len(parts) < 2 or casefold_key("/".join(parts[:-1])) not in keys:
            return False
    ancestor = ""
    for index in range(len(parts) - 1, 0, -1):
        prefix = "/".join(parts[:index])
        if casefold_key(prefix) in keys:
            ancestor = prefix
            break
    if ancestor:
        key = casefold_key(ancestor)
        around = [folder for folder in folders if casefold_key(folder) == key or casefold_key(folder).startswith(key + "/")]
    else:
        around = list(folders)
    deepest = max((_depth(folder) for folder in around), default=0)
    return len(parts) <= max(deepest, 1) + 1


def _with_parents(folders: Sequence[str]) -> list[str]:
    """``folders`` plus the parents they imply ("Wohnung" for "Wohnung/Strom")."""
    result = list(dict.fromkeys(folder for folder in folders if folder))
    known = {casefold_key(folder) for folder in result}
    for folder in list(result):
        parts = folder.split("/")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            if casefold_key(parent) not in known:
                known.add(casefold_key(parent))
                result.append(parent)
    return result


def resolve_folder(raw: Any, folders: Sequence[str], *, wants_new: bool) -> tuple[str | None, bool]:
    """The folder of an answer: (existing folder as spelled on disk, False), (new folder, True) or (None, False)."""
    text = _clean_text(raw, 300)
    if not text:
        return None, False
    folders = _with_parents(folders)
    candidate = ""
    for variant in (text, _COUNT_SUFFIX_RE.sub("", text)):  # "Wohnung/Strom (12)" as copied from the list
        candidate = "/".join(part.strip() for part in variant.replace("\\", "/").split("/") if part.strip())
        existing = _existing_folder(candidate, folders) if candidate else None
        if existing is not None:
            return existing, False
    if not candidate:
        return None, False
    try:
        cleaned = sanitize_folder(candidate)
    except PathError:
        return None, False
    if not cleaned:
        return None, False
    existing = _existing_folder(cleaned, folders)
    if existing is not None:
        return existing, False
    if wants_new and acceptable_new_folder(cleaned, folders):
        return cleaned, True
    return None, False


def select_folders(
    folders: Sequence[str],
    counts: Mapping[str, int] | None = None,
    candidates: Sequence[str] = (),
    *,
    limit: int = MAX_FOLDERS,
) -> tuple[list[str], int]:
    """The folders listed in the prompt (sorted) and how many were left out.

    All folders if there are at most ``limit``; otherwise the candidates (with
    their parents) first, then the top-level and second-level folders, then
    the folders holding the most documents.
    """
    counts = counts or {}
    unique = list(dict.fromkeys(folder for folder in folders if folder))
    if len(unique) <= limit:
        return sorted(unique, key=str.casefold), 0
    by_key = {casefold_key(folder): folder for folder in unique}
    chosen: dict[str, str] = {}

    def add(folder: str) -> None:
        key = casefold_key(folder)
        if len(chosen) < limit and key in by_key and key not in chosen:
            chosen[key] = by_key[key]

    for candidate in candidates:
        parts = [part for part in str(candidate or "").split("/") if part]
        for index in range(1, len(parts) + 1):
            add("/".join(parts[:index]))
    for folder in sorted(unique, key=lambda f: (_depth(f), -counts.get(f, 0), f.casefold())):
        if _depth(folder) <= 2:
            add(folder)
    for folder in sorted(unique, key=lambda f: (-counts.get(f, 0), f.casefold())):
        add(folder)
    shown = sorted(chosen.values(), key=str.casefold)
    return shown, len(unique) - len(shown)


def document_text(text: str, max_chars: int) -> str:
    """The first ``max_chars`` characters plus the last 1000 if the text is longer."""
    text = (text or "").strip()
    limit = max(500, int(max_chars))
    if len(text) <= limit:
        return text
    tail = text[max(limit, len(text) - TAIL_CHARS):].strip()
    return text[:limit].rstrip() + "\n[…]\n" + tail


def _date_de(value: Any) -> str:
    return timeutil.format_date_de(value) or str(value or "")


def _candidate(candidate: Any) -> tuple[Any, Any]:
    """(value, label) of a detected date (a DateCandidate or its dict form)."""
    if isinstance(candidate, Mapping):
        return candidate.get("value"), candidate.get("label")
    return getattr(candidate, "value", None), getattr(candidate, "label", None)


def _facts(analysis: Analysis | None) -> list[str]:
    """Detected facts as German lines for the prompt."""
    if analysis is None:
        return []
    lines: list[str] = []
    if analysis.doc_type:
        lines.append(f"Dokumenttyp: {analysis.doc_type}")
    dates = []
    for candidate in analysis.dates[:MAX_DATE_FACTS]:
        value, label = _candidate(candidate)
        if value:
            dates.append(f"{_date_de(value)}" + (f" ({label})" if label else ""))
    if analysis.doc_date:
        lines.append(f"Datum des Dokuments: {_date_de(analysis.doc_date)}")
    if dates:
        lines.append("Datumsangaben im Text: " + ", ".join(dates))
    if analysis.sender:
        lines.append(f"Absender: {analysis.sender}")
    others = [name for name in analysis.sender_candidates if name and name != analysis.sender][:3]
    if others:
        lines.append("Weitere mögliche Absender: " + ", ".join(others))
    if analysis.amounts:
        amounts = ", ".join(analysis.amounts[:MAX_AMOUNT_FACTS])
        total = f" (Gesamtbetrag {analysis.total_amount})" if analysis.total_amount else ""
        lines.append(f"Beträge: {amounts}{total}")
    references = [
        f"{label} {value}" for label, values in analysis.references.items() for value in values[:2]
    ][:MAX_REFERENCE_FACTS]
    if references:
        lines.append("Nummern: " + "; ".join(references))
    if analysis.ibans:
        lines.append("IBAN: " + ", ".join(analysis.ibans[:2]))
    return lines


_CONTEXT_LABELS = {
    "mail_subject": "Betreff der E-Mail",
    "mail_from": "Absender der E-Mail",
    "forwarded_from": "Ursprünglicher Absender (weitergeleitet)",
    "original_name": "Dateiname",
    "title": "Bezeichnung beim Eingang",
}


def build_prompt(request: LlmRequest, *, max_chars: int) -> str:
    """The user message: folders, examples, similar documents, detected facts and the text."""
    folders = [folder for folder in request.folders if folder]
    shown, omitted = select_folders(folders, request.counts, request.candidates)
    parts: list[str] = []
    if shown:
        lines = [f"- {folder} ({int(request.counts.get(folder, 0) or 0)})" for folder in shown]
        if omitted:
            lines.append(f"… und {omitted} weitere Ordner (nicht aufgeführt)")
        parts.append("Vorhandene Ordner (Zahl der Dokumente darin):\n" + "\n".join(lines))
    else:
        parts.append("Vorhandene Ordner: noch keine – das Archiv ist leer.")

    example_lines: list[str] = []
    for folder in list(dict.fromkeys(request.candidates))[:EXAMPLE_FOLDERS]:
        names = [str(name)[:120] for name in request.examples.get(folder, [])[:EXAMPLES_PER_FOLDER] if name]
        if names:
            example_lines.append(f"{folder}:")
            example_lines.extend(f"  - {name}" for name in names)
    if example_lines:
        parts.append("Beispiel-Dateinamen aus wahrscheinlich passenden Ordnern:\n" + "\n".join(example_lines))

    similar = [doc for doc in request.similar if doc.folder][:MAX_SIMILAR]
    if similar:
        lines = [f"- „{doc.name}“ in {doc.folder} ({round(doc.score * 100)} % ähnlich)" for doc in similar]
        parts.append("Ähnliche, bereits abgelegte Dokumente:\n" + "\n".join(lines))

    facts = _facts(request.analysis)
    if facts:
        parts.append("Automatisch erkannt (kann falsch sein):\n" + "\n".join(f"- {line}" for line in facts))

    hints = [
        f"- {label}: {str(request.context[key])[:200]}"
        for key, label in _CONTEXT_LABELS.items()
        if request.context.get(key)
    ]
    if hints:
        parts.append("Angaben zum Eingang:\n" + "\n".join(hints))

    parts.append(f"Dokumenttext:\n\"\"\"\n{document_text(request.text, max_chars)}\n\"\"\"")
    parts.append("Antworte nur mit dem JSON-Objekt.")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# The client


class LlmClient:
    """Asks a chat model for folder, title, date and sender of a document."""

    #: Pause before the one retry after a connection error (seconds).
    retry_delay = 2.0

    def __init__(self, config: LlmConfig, *, transport: httpx.BaseTransport | None = None):
        self.config = config
        self._transport = transport
        # Request formats the server rejected (HTTP 400) are skipped next time.
        self._skip_formats = 0
        #: Outcome of the last request: {"ok" (a usable suggestion), "reachable" (the server
        #: answered at all), "error" (German), "at", "duration_s"}; None before the first one.
        self.last_status: dict[str, Any] | None = None

    # -- addresses -------------------------------------------------------------
    @property
    def provider(self) -> str:
        return getattr(self.config, "provider", "ollama") or "ollama"

    @property
    def endpoint(self) -> str:
        url = self.config.url.rstrip("/")
        if self.provider == "anthropic":
            return "https://api.anthropic.com/v1/messages"
        if self.provider == "openai":
            if url.endswith("/chat/completions"):
                return url
            if url.endswith("/v1"):
                return url + "/chat/completions"
            return url + "/v1/chat/completions"
        return url + "/api/chat"

    @property
    def host(self) -> str:
        """Host and port of the endpoint, for messages (no path, no credentials)."""
        if self.provider == "anthropic":
            return "api.anthropic.com"
        parts = urlsplit(self.config.url)
        host = parts.hostname or self.config.url
        if ":" in host:
            host = f"[{host}]"
        return f"{host}:{parts.port}" if parts.port else host

    # -- prompt ------------------------------------------------------------------
    def build_messages(self, request: LlmRequest) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_prompt(request, max_chars=self.config.max_chars)},
        ]

    def _payloads(self, messages: list[dict[str, str]]) -> list[dict[str, Any]]:
        """Request bodies, the preferred format first; later ones are fallbacks for HTTP 400."""
        model = self.config.model
        if self.provider == "openai":
            base = {"model": model, "messages": messages, "temperature": 0, "stream": False}
            return [
                {**base, "response_format": {"type": "json_schema", "json_schema": {
                    "name": SCHEMA_NAME, "schema": ANSWER_SCHEMA, "strict": True}}},
                {**base, "response_format": {"type": "json_object"}},
                base,
            ]
        options = {"temperature": 0, "num_ctx": int(getattr(self.config, "context_tokens", 8192) or 8192)}
        return [
            # "think": false keeps reasoning models from spending minutes before the answer.
            {"model": model, "messages": messages, "stream": False, "format": ANSWER_SCHEMA, "think": False,
             "options": options},
            {"model": model, "messages": messages, "stream": False, "format": "json", "options": options},
        ]

    # -- public API --------------------------------------------------------------
    def suggest(
        self, text: str, folders: Sequence[str], context: Mapping[str, Any] | None = None, **details: Any
    ) -> Suggestion | None:
        """A validated suggestion, or None if the model is unreachable, slow or talks nonsense.

        ``details`` are further fields of :class:`LlmRequest` (counts, candidates …).
        """
        request = LlmRequest(text=text, folders=list(folders), context=dict(context or {}), **details)
        return self.classify(request).suggestion

    def classify(self, request: LlmRequest) -> LlmResult:
        """Ask the model about one document. Never raises; see :class:`LlmResult`."""
        if not (request.text or "").strip():
            return LlmResult(error="Das Dokument enthält keinen Text – das Sprachmodell wurde nicht gefragt.")
        started = time.monotonic()
        result = LlmResult()
        reachable = True
        try:
            content = self._chat(self.build_messages(request))
            answer = parse_json_object(content)
            if answer is None:
                raise _LlmError("Die Antwort des Sprachmodells ist kein JSON-Objekt.")
            result.answer = answer
            suggestion, confidence = self._to_suggestion(answer, request)
            result.confidence = confidence
            if suggestion is None:
                raise _LlmError("Die Antwort des Sprachmodells enthält keinen brauchbaren Vorschlag.")
            result.suggestion = suggestion
        except _LlmError as exc:
            result.error = str(exc)
            reachable = exc.reachable
        except Exception:  # never let a model answer break the pipeline
            log.exception("cannot interpret the answer of the language model")
            result.error = "Die Antwort des Sprachmodells konnte nicht ausgewertet werden."
        result.duration_s = round(time.monotonic() - started, 2)
        if result.error:
            log.warning("no suggestion from the language model at %s: %s", self.host, result.error)
        self.last_status = {
            "ok": result.suggestion is not None,
            "reachable": reachable,
            "error": result.error,
            "at": timeutil.now_iso(),
            "duration_s": result.duration_s,
        }
        return result

    def probe(self) -> dict[str, Any]:
        """Classify a built-in, made-up letter to test the connection. Never raises.

        Returns ``{"ok", "provider", "model", "duration_s", "answer", "error"}``;
        ``answer`` is the JSON object the model returned (its known keys).
        """
        started = time.monotonic()
        outcome: dict[str, Any] = {
            "ok": False,
            "provider": self.provider,
            "model": getattr(self.config, "model", ""),
            "duration_s": 0.0,
            "answer": None,
            "error": None,
        }
        try:
            request = LlmRequest(
                text=SAMPLE_LETTER,
                folders=list(SAMPLE_FOLDERS),
                counts=dict(SAMPLE_FOLDERS),
                candidates=["Wohnung/Strom", "Wohnung/Miete"],
                examples={folder: list(names) for folder, names in SAMPLE_EXAMPLES.items()},
            )
            result = self.classify(request)
            if result.answer is not None:
                outcome["ok"] = True
                lowered = {str(key).casefold(): value for key, value in result.answer.items()}
                outcome["answer"] = {key: lowered[key] for key in ANSWER_KEYS if key in lowered} or dict(result.answer)
            else:
                outcome["error"] = result.error or "Keine Antwort vom Sprachmodell."
        except Exception as exc:  # pragma: no cover - classify() does not raise
            log.exception("connection test of the language model failed")
            outcome["error"] = f"Der Test ist fehlgeschlagen: {exc}"
        outcome["duration_s"] = round(time.monotonic() - started, 2)
        return outcome

    # -- HTTP --------------------------------------------------------------------
    def _chat(self, messages: list[dict[str, str]]) -> str:
        """The text of the model's answer; tries simpler request formats after HTTP 400."""
        if self.provider == "anthropic":
            return self._chat_anthropic(messages)
        payloads = self._payloads(messages)
        first = min(self._skip_formats, len(payloads) - 1)
        for index in range(first, len(payloads)):
            response = self._post(payloads[index])
            if response.status_code == 400 and index + 1 < len(payloads):
                log.info(
                    "language model server rejected the request format (%s), trying a simpler one",
                    _error_detail(response) or "HTTP 400",
                )
                continue
            if response.is_error:
                raise _LlmError(self._http_error(response))
            if index > self._skip_formats:
                self._skip_formats = index
            try:
                data = response.json()
            except ValueError as exc:
                raise _LlmError("Die Antwort des Servers ist kein JSON.") from exc
            return self._content(data)
        raise _LlmError("Der Server lehnt die Anfrage ab.")  # pragma: no cover - loop always returns or raises

    def _anthropic_client(self) -> Any:
        import anthropic

        # api_key=None lets the SDK fall back to ANTHROPIC_API_KEY from the environment.
        return anthropic.Anthropic(
            api_key=self.config.api_key or None,
            timeout=float(self.config.timeout),
            max_retries=1,
        )

    def _chat_anthropic(self, messages: list[dict[str, str]]) -> str:
        """Ask a Claude model through the Anthropic API, with the answer constrained to ANSWER_SCHEMA."""
        import anthropic

        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        conversation = [m for m in messages if m["role"] != "system"]
        try:
            response = self._anthropic_client().messages.create(
                model=self.config.model,
                max_tokens=ANTHROPIC_MAX_TOKENS,
                system=system,
                messages=conversation,
                # Filing one document is a routine classification: low effort keeps it fast and cheap.
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": ANSWER_SCHEMA}},
            )
        except anthropic.AuthenticationError as exc:
            raise _LlmError("Die Anthropic-API lehnt den API-Schlüssel ab (401) – Schlüssel prüfen.") from exc
        except anthropic.PermissionDeniedError as exc:
            raise _LlmError("Die Anthropic-API verweigert den Zugriff (403) – Rechte des Schlüssels prüfen.") from exc
        except anthropic.NotFoundError as exc:
            raise _LlmError(f"Modell „{self.config.model}“ ist bei der Anthropic-API nicht bekannt.") from exc
        except anthropic.RateLimitError as exc:
            raise _LlmError("Die Anthropic-API meldet zu viele Anfragen oder ein aufgebrauchtes Guthaben (429).") from exc
        except anthropic.APITimeoutError as exc:
            raise _LlmError(
                f"Das Sprachmodell hat nicht innerhalb von {self.config.timeout} s geantwortet."
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise _LlmError("Keine Verbindung zur Anthropic-API (api.anthropic.com).", reachable=False) from exc
        except anthropic.APIStatusError as exc:
            raise _LlmError(f"Die Anthropic-API meldet Fehler {exc.status_code}.") from exc
        if response.stop_reason == "refusal":
            raise _LlmError("Das Sprachmodell hat die Anfrage abgelehnt.")
        if response.stop_reason == "max_tokens":
            raise _LlmError("Die Antwort des Sprachmodells wurde abgeschnitten.")
        text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
        if not text.strip():
            raise _LlmError("Die Antwort des Sprachmodells ist leer.")
        return text

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        headers = {"Accept": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        seconds = float(self.config.timeout)
        timeout = httpx.Timeout(seconds, connect=min(10.0, seconds))
        for attempt in (1, 2):
            try:
                with httpx.Client(timeout=timeout, transport=self._transport) as client:
                    return client.post(self.endpoint, json=payload, headers=headers)
            except _RETRYABLE as exc:
                if attempt == 2:
                    raise _LlmError(self._transport_error(exc), reachable=False) from exc
                log.info("language model at %s not reachable (%s), retrying once", self.host, type(exc).__name__)
                if self.retry_delay:
                    time.sleep(self.retry_delay)
            except httpx.TimeoutException as exc:
                raise _LlmError(
                    f"Das Sprachmodell hat nicht innerhalb von {self.config.timeout} s geantwortet."
                ) from exc
            except httpx.HTTPError as exc:
                raise _LlmError(
                    f"Anfrage an {self.host} fehlgeschlagen: {type(exc).__name__}", reachable=False
                ) from exc
        raise _LlmError(f"Keine Verbindung zu {self.host}.", reachable=False)  # pragma: no cover

    def _transport_error(self, exc: Exception) -> str:
        if isinstance(exc, httpx.ConnectTimeout):
            return f"Der Server {self.host} antwortet nicht (Verbindungsaufbau dauert zu lange)."
        if isinstance(exc, httpx.ConnectError):
            return f"Keine Verbindung zum Sprachmodell unter {self.host}."
        return f"Die Verbindung zu {self.host} wurde unterbrochen."

    def _http_error(self, response: httpx.Response) -> str:
        status = response.status_code
        detail = _error_detail(response)
        suffix = f": {detail}" if detail else ""
        if status in (401, 403):
            return f"Der Server {self.host} verweigert den Zugriff ({status}) – API-Schlüssel prüfen{suffix}."
        if status == 404:
            return f"Nicht gefunden ({status}) – Adresse und Modell „{self.config.model}“ prüfen{suffix}."
        return f"Der Server {self.host} meldet Fehler {status}{suffix}."

    def _content(self, data: Any) -> str:
        if not isinstance(data, dict):
            raise _LlmError("Die Antwort des Servers hat ein unerwartetes Format.")
        content: Any = None
        if self.provider == "openai":
            choices = data.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message = choices[0].get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                if content is None:
                    content = choices[0].get("text")
        else:
            message = data.get("message")
            if isinstance(message, dict):
                content = message.get("content")
            if content is None:
                content = data.get("response")
        if isinstance(content, list):  # content parts
            content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        if not isinstance(content, str) or not content.strip():
            raise _LlmError("Die Antwort des Sprachmodells ist leer.")
        return content

    # -- validation ----------------------------------------------------------------
    def _to_suggestion(self, data: Mapping[str, Any], request: LlmRequest) -> tuple[Suggestion | None, float]:
        lowered = {str(key).casefold(): value for key, value in data.items()}

        def pick(*keys: str) -> Any:
            for key in keys:
                if key in lowered:
                    return lowered[key]
            return None

        confidence = parse_confidence(pick("sicherheit", "confidence"))
        title = clean_title(pick("titel", "title"))
        sender = _clean_text(pick("absender", "sender"), MAX_SENDER)
        reason = _clean_text(pick("begruendung", "begründung", "reason"), MAX_REASON)
        doc_date = self._valid_date(pick("datum", "date"), request)
        folders = [folder for folder in request.folders if folder]
        folder, new_folder = resolve_folder(
            pick("ordner", "folder"), folders, wants_new=_truthy(pick("neuer_ordner", "new_folder"))
        )
        tags: list[str] = []
        tags_raw = pick("tags", "schlagwoerter")
        if isinstance(tags_raw, str):
            tags_raw = re.split(r"[,;]", tags_raw)
        if isinstance(tags_raw, list):
            for item in tags_raw:
                tag = _clean_text(item, MAX_TAG)
                if tag and tag.casefold() not in {t.casefold() for t in tags}:
                    tags.append(tag)
                if len(tags) >= MAX_TAGS:
                    break
        if folder is None and not title and doc_date is None:
            return None, confidence
        suggestion = Suggestion(
            folder=folder,
            title=title,
            doc_date=doc_date.isoformat() if doc_date else None,
            tags=tags,
            sender=sender,
            score=round(confidence * SCORE_FACTOR, 3) if folder else 0.0,
            source="llm",
            reasons=[reason] if reason else [f"Vorschlag des Sprachmodells {self.config.model}"],
            new_folder=new_folder,
        )
        return suggestion, confidence

    @staticmethod
    def _valid_date(raw: Any, request: LlmRequest) -> date | None:
        """The answer's date if it parses and occurs in the text (or among the detected dates)."""
        if raw is None or isinstance(raw, (bool, int, float, dict, list)):
            return None
        value = parse_date_text(str(raw), today=timeutil.today())
        if value is None:
            return None
        detected: set[date | None] = set()
        if request.analysis is not None:
            detected = {timeutil.parse_date(_candidate(candidate)[0]) for candidate in request.analysis.dates}
            detected.add(timeutil.parse_date(request.analysis.doc_date))
        if value in detected or date_in_text(value, request.text):
            return value
        return None


def _error_detail(response: httpx.Response) -> str:
    """The error text a server put into its response (Ollama and OpenAI style), shortened."""
    try:
        data = response.json()
    except ValueError:
        data = None
    detail: Any = None
    if isinstance(data, dict):
        detail = data.get("error") or data.get("message") or data.get("detail")
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("type")
    if detail is None:
        detail = response.text
    text = re.sub(r"\s+", " ", str(detail or "")).strip()
    return text[:200]


__all__ = [
    "ANSWER_SCHEMA",
    "LlmClient",
    "LlmRequest",
    "LlmResult",
    "SAMPLE_LETTER",
    "SYSTEM_PROMPT",
    "SimilarDocument",
    "acceptable_new_folder",
    "build_prompt",
    "clean_title",
    "date_in_text",
    "document_text",
    "parse_confidence",
    "parse_json_object",
    "resolve_folder",
    "select_folders",
]
