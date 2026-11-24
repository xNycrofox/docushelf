"""Facts from the text of a document.

:func:`analyze` reads the text of all pages (text layer or OCR) plus the
context of the inbox item (mail headers, file name) and finds

* the document date – labelled dates ("Rechnungsdatum", "Dresden, den …")
  win, due dates, periods and birth dates lose,
* the sender – known senders on page 1, the letterhead, the return address
  line above the address window, the footer, mail headers and domains; the
  recipient's own address block is never used,
* amounts and the total, IBANs (mod-97 checked), reference numbers
  (customer, contract, invoice … numbers), e-mail addresses,
* the document type (keyword scoring, see :mod:`.doctypes`) and keywords.

Everything here is heuristic and tuned for German household paperwork. The
result is only ever a suggestion the user confirms.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parseaddr
from typing import Any, Callable, Iterable, Mapping, Sequence, TypeVar

from ..models import Analysis, DateCandidate
from ..util import timeutil
from ..util.textnorm import STOPWORDS, content_words, normalize, normalize_with_map
from . import doctypes

log = logging.getLogger(__name__)

T = TypeVar("T")

# Only the beginning of very long documents is analysed in detail.
MAX_PAGES = 30
MAX_KEYWORD_CHARS = 100_000
HEADER_LINES = 15
FOOTER_LINES = 8
# Candidates are listed but not chosen as the document date unless they score above
# MIN_DATE_SCORE, or as the sender unless they reach MIN_SENDER_SCORE.
MIN_DATE_SCORE = 0.25
MIN_SENDER_SCORE = 0.45
FUTURE_DAYS = 60

# ---------------------------------------------------------------------------
# Small text helpers

_FOLD_MAP = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue"})


def _fold(text: str) -> str:
    """Lower case with umlauts spelled out (ß becomes ss through casefold); punctuation stays."""
    return unicodedata.normalize("NFC", text).casefold().translate(_FOLD_MAP)


_OCR_LETTER_IN_NUMBER_RE = re.compile(r"(?<=\d)[OoIl](?=\d)|(?<=\d[.,])[OoIl](?=\d)|(?<=\d)[OoIl](?=[.,]\d)")


def _fix_ocr_digits(text: str) -> str:
    """Repair the most common OCR confusions inside numbers ("2O25", "l2.03.2025"); keeps length."""

    def repl(match: re.Match[str]) -> str:
        return "0" if match.group(0) in "Oo" else "1"

    return _OCR_LETTER_IN_NUMBER_RE.sub(repl, text)


def _safe(func: Callable[[], T], default: T, what: str) -> T:
    """Run one extractor; a bug in one of them must not lose the others."""
    try:
        return func()
    except Exception:  # pragma: no cover - defensive, logged
        log.exception("analysis step failed: %s", what)
        return default


# ---------------------------------------------------------------------------
# Document structure


@dataclass
class _Line:
    page: int  # 0-based
    index: int  # 0-based among the non-empty lines of the page
    text: str  # stripped original
    norm: str  # normalize(text)
    fixed: str = ""  # text with OCR digit repairs, for numbers
    zone: str = "body"  # header | body | footer (page 1 only)
    recipient: bool = False  # part of the recipient's address block
    return_line: bool = False  # the sender's one-line address above the address window

    @property
    def padded(self) -> str:
        return f" {self.norm} "


_SALUTATION_RE = re.compile(
    r"^(?:herrn?|frau|familie|fam\.|eheleute|eheleuten|z\.\s?hd\.?|zu händen|c/o)(?=$|[\s,.:])",
    re.IGNORECASE,
)
_RECIPIENT_LABEL_RE = re.compile(
    r"^(?:rechnungs(?:adresse|anschrift|empfänger)|liefer(?:adresse|anschrift)|empfänger(?:in)?|"
    r"kunde|kundin|kontoinhaber(?:in)?|versicherungsnehmer(?:in)?|versicherte person|versicherte[rn]?|"
    r"patient(?:in)?|mieter(?:in)?|name|antragsteller(?:in)?|arbeitnehmer(?:in)?|mitarbeiter(?:in)?|"
    r"steuerpflichtige[rn]?|ehegatte|ehefrau|ehemann|schüler(?:in)?|teilnehmer(?:in)?|auftraggeber(?:in)?|"
    r"besteller(?:in)?|mitglied|zahlungspflichtige[rn]?|schuldner(?:in)?|"
    r"anschrift|billing address|shipping address|bill to|ship to)(?![\wäöüß])\s*:?\s*",
    re.IGNORECASE,
)
_PLZ_LINE_RE = re.compile(r"^(?:D\s?-\s?)?\d{5}\s+\S")
_PLZ_ANYWHERE_RE = re.compile(r"(?<!\d)(?:D\s?-\s?)?\d{5}\s+[A-ZÄÖÜ][\wäöüß\-]+")
_STREET_RE = re.compile(
    r"^[A-ZÄÖÜ][^\d@]{1,45}?\s\d{1,4}(?:\s?[a-zA-Z])?(?:\s?[-/]\s?\d{1,4}[a-zA-Z]?)?$"
)
_STREET_WORD_RE = re.compile(
    r"(?:straße|strasse|str\.|weg|allee|platz|gasse|ring|damm|ufer|chaussee|steig|pfad|markt|hof|promenade|"
    r"berg|feld|grund|winkel|zeile|kamp|stieg|anger|graben|park)\s*\d",
    re.IGNORECASE,
)
_POSTFACH_RE = re.compile(r"\bpostfach\b|\bpf\.?\s*\d", re.IGNORECASE)


def _is_address_text(text: str) -> bool:
    stripped = text.strip()
    return bool(
        _PLZ_LINE_RE.match(stripped)
        or _STREET_RE.match(stripped)
        or _STREET_WORD_RE.search(stripped)
        or _POSTFACH_RE.search(stripped)
    )


@dataclass
class _Doc:
    pages: list[str]
    page_lines: list[list[_Line]]
    lines: list[_Line]

    @classmethod
    def build(cls, pages: Sequence[str]) -> "_Doc":
        page_lines: list[list[_Line]] = []
        for page_no, page in enumerate(pages[:MAX_PAGES]):
            result: list[_Line] = []
            for raw in (page or "").splitlines():
                text = raw.strip()
                if not text:
                    continue
                result.append(_Line(page=page_no, index=len(result), text=text, norm=normalize(text),
                                    fixed=_fix_ocr_digits(text)))
            page_lines.append(result)
        doc = cls(pages=list(pages), page_lines=page_lines, lines=[line for p in page_lines for line in p])
        doc._mark_zones()
        doc._mark_recipient_blocks()
        return doc

    @property
    def first(self) -> list[_Line]:
        return self.page_lines[0] if self.page_lines else []

    def _mark_zones(self) -> None:
        lines = self.first
        for line in lines[:HEADER_LINES]:
            line.zone = "header"
        if len(lines) > HEADER_LINES:
            for line in lines[max(HEADER_LINES, len(lines) - FOOTER_LINES):]:
                line.zone = "footer"

    def _mark_recipient_blocks(self) -> None:
        for lines in self.page_lines[:2]:
            limit = min(len(lines), 45)
            i = 0
            while i < limit:
                line = lines[i]
                starts = _SALUTATION_RE.match(line.text)
                labelled = _RECIPIENT_LABEL_RE.match(line.text) if not starts else None
                if labelled and not _looks_like_recipient_label(line.text, labelled):
                    labelled = None
                if not (starts or labelled) or len(line.text.split()) > 8:
                    i += 1
                    continue
                block = [i]
                end = i
                found_plz = bool(_PLZ_ANYWHERE_RE.search(line.text)) and starts is not None
                if not found_plz:
                    for j in range(i + 1, min(i + 6, len(lines))):
                        block.append(j)
                        end = j
                        if _PLZ_LINE_RE.match(lines[j].text) or _PLZ_ANYWHERE_RE.match(lines[j].text):
                            found_plz = True
                            break
                if not found_plz:
                    # No postal code: only the salutation and the name line are certain.
                    block = block[:2]
                    end = block[-1]
                for k in block:
                    lines[k].recipient = True
                if line.page == 0 and starts:
                    self._mark_return_line(lines, i)
                i = end + 1

    @staticmethod
    def _mark_return_line(lines: list[_Line], block_start: int) -> None:
        """The compact sender address printed above the window ("Firma · Straße 1 · 01067 Ort")."""
        for k in range(block_start - 1, max(-1, block_start - 3), -1):
            candidate = lines[k]
            if candidate.recipient:
                break
            text = candidate.text
            if len(_split_segments(text)) >= 2 and (_PLZ_ANYWHERE_RE.search(text) or _POSTFACH_RE.search(text)):
                candidate.return_line = True
                break


def _looks_like_recipient_label(text: str, match: re.Match[str]) -> bool:
    """"Kunde:" or "Rechnungsadresse" start an address block; "Kunde Nr. 123" does not."""
    rest = text[match.end():].strip()
    if rest and re.match(r"^(?:[\d\-/#]|nr\.?(?:\s|$)|no\.?(?:\s|$))", rest, re.IGNORECASE):
        return False
    return True


# ---------------------------------------------------------------------------
# Dates

MONTHS: dict[str, int] = {
    "januar": 1, "jänner": 1, "jaenner": 1, "jan": 1, "january": 1,
    "februar": 2, "feber": 2, "feb": 2, "febr": 2, "february": 2,
    "märz": 3, "maerz": 3, "mär": 3, "mrz": 3, "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "mai": 5, "may": 5,
    "juni": 6, "jun": 6, "june": 6,
    "juli": 7, "jul": 7, "july": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "oktober": 10, "okt": 10, "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "dezember": 12, "dez": 12, "december": 12, "dec": 12,
}
FULL_MONTH_NAMES = (
    "Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember",
)
_FULL_MONTHS = {
    "januar", "jänner", "februar", "märz", "maerz", "april", "mai", "juni", "juli", "august", "september",
    "oktober", "november", "dezember", "january", "february", "march", "may", "june", "july", "october",
    "december",
}
# Month names that are written out (casefolded); everything else in MONTHS is an abbreviation.
FULL_MONTH_WORDS = frozenset(_FULL_MONTHS)
_MONTH_ALT = "|".join(sorted((re.escape(m) for m in MONTHS), key=len, reverse=True))
_FULL_MONTH_ALT = "|".join(sorted((re.escape(m) for m in _FULL_MONTHS), key=len, reverse=True))

_NUM_DATE_RE = re.compile(r"(?<![\d.,/])(\d{1,2})\s?\.\s?(\d{1,2})\s?\.\s?(\d{4}|\d{2})(?![\d,]|\.\d)")
_ISO_DATE_RE = re.compile(r"(?<![\d.\-/])(\d{4})-(\d{2})-(\d{2})(?![\d\-])")
_TEXT_DATE_RE = re.compile(
    rf"(?<![\d.])(\d{{1,2}})\.?\s*(?:de\s+)?({_MONTH_ALT})\.?,?\s+(\d{{4}})(?!\d)", re.IGNORECASE
)
_TEXT_DATE_EN_RE = re.compile(
    rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})(?!\d)", re.IGNORECASE
)
_MONTH_YEAR_RE = re.compile(rf"\b({_FULL_MONTH_ALT})\s+(\d{{4}})(?!\d)", re.IGNORECASE)

_RANGE_GAP_RE = re.compile(r"^\s*(?:-|–|—|bis(?:\s+zum)?|to|\.\.\.?)\s*$", re.IGNORECASE)

# (pattern on folded text, score delta, max distance between label end and date)
_POSITIVE_LABELS: tuple[tuple[re.Pattern[str], float, int], ...] = tuple(
    (re.compile(pattern), delta, gap)
    for pattern, delta, gap in (
        (r"(?:rechnungs|beleg)- ?(?:und|u\.|bzw\.?|/) ?(?:liefer|leistungs)-?datum|"
         r"rechnungs-?datum|datum der rechnung|invoice date|date of invoice|beleg-?datum|ausstellungsdatum|"
         r"ausgestellt am|abrechnungsdatum|bescheid-?datum|datum des bescheids|gutschrifts-?datum|druckdatum|"
         r"erstellungsdatum|erstellt am|tag der ausstellung|auszugsdatum|briefdatum|dokumentdatum", 0.45, 30),
        (r"bescheid (?:fuer \d{4} )?vom|kontoauszug(?: nr\.?)?(?: ?\d+)?(?:/\d{4})? vom|auszug(?: nr\.?)?(?: ?\d+)? vom|"
         r"abrechnung vom|rechnung vom|gutschrift vom|mitteilung vom|police vom|versicherungsschein vom|"
         r"bescheinigung vom|zeugnis vom|stand vom|aufstellung vom|mahnung vom|vertrag vom", 0.4, 6),
        (r"(?<![\w-])datum|(?<![\w-])date(?!\w)|(?<![\w-])dat\.", 0.4, 25),
        (r"(?<![\w-])stand", 0.3, 8),
        (r"gesendet|sent", 0.25, 30),
        (r"neuer kontostand(?: vom| am)?|neuer saldo(?: vom| am)?|kontostand am|kontostand vom", 0.15, 6),
        (r"bestelldatum|auftragsdatum|order date|bestellt am", 0.1, 25),
        (r"(?<!\w)vom", 0.1, 3),
        (r"(?<!\w)fuer", 0.05, 3),
    )
)
_BIRTH_LABEL = re.compile(r"geb\.?-? ?datum|geburtsdatum|geburtstag|geboren|(?<!\w)geb\.|date of birth|birth")
_NEGATIVE_LABELS: tuple[tuple[re.Pattern[str], float, int], ...] = tuple(
    (re.compile(pattern), delta, gap)
    for pattern, delta, gap in (
        (r"faellig|zahlbar|zahlungsziel|zahlungsfrist|zahlungstermin|spaetestens|(?<!\w)frist|bis zum|due date|"
         r"(?<!\w)due(?!\w)|einzug|abbuchung|abgebucht|belastung|lastschrift am|wird am|werden am|"
         r"zahlen sie bis|ueberweisen sie bis", -0.5, 30),
        (r"zeitraum|leistungsdatum|leistungsmonat|lieferzeitraum|periode|abrechnungsjahr|beitragsjahr|"
         r"verbrauchszeitraum|service period|billing period", -0.5, 30),
        (r"gueltig|gueltigkeit|laufzeit|vertragsbeginn|versicherungsbeginn|versicherungsablauf|(?<!\w)ablauf|"
         r"ablaufdatum|(?<!\w)beginn|beginnt|(?<!\w)ende(?!\w)|endet|(?<!\w)eintritt|eintrittsdatum|austritt|"
         r"mitglied seit|(?<!\w)seit|hauptfaelligkeit|kuendbar|kuendigung zum|wirksam|valid until|valid from", -0.45, 30),
        (r"buchungstag|buchungsdatum|wertstellung|valuta|alter kontostand(?: vom| am)?|alter saldo(?: vom| am)?|"
         r"letzter kontoauszug(?: vom)?|vorheriger", -0.4, 30),
        (r"ihr(?:e|em|er|en)? (?:schreiben|nachricht|anfrage|e-mail|email|mail|bestellung|auftrag|anruf|antrag|brief|"
         r"zahlung|ueberweisung) vom|unser(?:e|em|en)? (?:schreiben|nachricht|brief|angebot|telefonat) vom|"
         r"(?<!\w)bezug|(?<!\w)betreff", -0.25, 30),
        (r"(?<!\w)(?:bis|zum|ab|ab dem|nach dem|vor dem|until|from)", -0.35, 2),
        (r"ablesung|ablesedatum|zaehlerstand|lieferdatum|versanddatum|lieferung am", -0.15, 30),
    )
)
# Deadlines ("zahlbar", "fällig", "zahlen Sie bis" …): the first group of negative labels.
_DUE_LABEL_RE = _NEGATIVE_LABELS[0][0]
# Delivery and service dates that an invoice often shares with its own date
# ("Rechnungsdatum / Lieferdatum", "Rechnungsdatum = Leistungsdatum").
_SHARED_DATE_LABEL_RE = re.compile(r"(?:liefer|leistungs)-?datum")
_LABEL_JOINER_RE = re.compile(r"[\s:(]*(?:[/=&,+–-]|und|u\.|bzw\.?|sowie)?[\s:(]*")
_PLACE_DATE_RE = re.compile(
    r"(?:^|[\s(])([A-ZÄÖÜ][a-zäöüß]+(?:[\s-](?:an der |am |im |bei )?[A-ZÄÖÜ][a-zäöüß]+){0,2})\s*,\s*(?:(?:den|im)\s*)?$"
)
_NOT_A_PLACE = {
    "datum", "stand", "date", "rechnungsdatum", "den", "vom", "am", "bis", "ab", "seite", "nr", "von", "gesendet",
    "montag", "dienstag", "mittwoch", "donnerstag", "freitag", "samstag", "sonnabend", "sonntag", "monday",
    "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "heute", "uhr",
}


@dataclass
class _DateHit:
    value: date
    text: str
    score: float
    label: str | None
    page: int
    line: int
    start: int
    end: int
    kind: str


def _two_digit_year(value: int, today: date) -> int:
    pivot = (today.year % 100) + 1
    return 2000 + value if value <= pivot else 1900 + value


def _make_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _scan_dates(text: str, today: date) -> list[tuple[date, int, int, str]]:
    """All date expressions in ``text``: (value, start, end, kind)."""
    found: list[tuple[date, int, int, str]] = []
    taken: list[tuple[int, int]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(start < t_end and end > t_start for t_start, t_end in taken)

    for match in _TEXT_DATE_RE.finditer(text):
        month = MONTHS.get(match.group(2).casefold())
        value = _make_date(int(match.group(3)), month or 0, int(match.group(1))) if month else None
        if value:
            found.append((value, match.start(), match.end(), "text"))
            taken.append((match.start(), match.end()))
    for match in _TEXT_DATE_EN_RE.finditer(text):
        if overlaps(match.start(), match.end()):
            continue
        month = MONTHS.get(match.group(1).casefold())
        value = _make_date(int(match.group(3)), month or 0, int(match.group(2))) if month else None
        if value:
            found.append((value, match.start(), match.end(), "text"))
            taken.append((match.start(), match.end()))
    for match in _NUM_DATE_RE.finditer(text):
        if overlaps(match.start(), match.end()):
            continue
        day, month, year_text = int(match.group(1)), int(match.group(2)), match.group(3)
        year = int(year_text) if len(year_text) == 4 else _two_digit_year(int(year_text), today)
        value = _make_date(year, month, day)
        if value:
            found.append((value, match.start(), match.end(), "numeric"))
            taken.append((match.start(), match.end()))
    for match in _ISO_DATE_RE.finditer(text):
        if overlaps(match.start(), match.end()):
            continue
        value = _make_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if value:
            found.append((value, match.start(), match.end(), "iso"))
            taken.append((match.start(), match.end()))
    for match in _MONTH_YEAR_RE.finditer(text):
        if overlaps(match.start(), match.end()):
            continue
        month = MONTHS.get(match.group(1).casefold())
        value = _make_date(int(match.group(2)), month or 0, 1) if month else None
        if value:
            found.append((value, match.start(), match.end(), "month"))
            taken.append((match.start(), match.end()))
    found.sort(key=lambda entry: entry[1])
    return found


def _labels_in(
    text: str, labels: Iterable[tuple[re.Pattern[str], float, int]], *, anywhere: bool = False
) -> list[tuple[float, str, int, int]]:
    """Label matches as (delta, text, start, end); unless ``anywhere``, only those close to the end."""
    found = []
    length = len(text)
    for pattern, delta, gap in labels:
        for match in pattern.finditer(text):
            if not anywhere and length - match.end() > gap:
                continue
            found.append((delta, match.group(0), match.start(), match.end()))
    return found


def _label_score(prefix: str) -> tuple[float, str | None]:
    """Score change and label for the text in front of a date on the same line.

    The label closest to the date wins; on a tie the longer one ("alter
    Kontostand vom" beats "vom"). Birth date labels always win. A delivery
    or service date label joined to a document date label ("Rechnungsdatum /
    Lieferdatum", "Rechnungsdatum = Leistungsdatum") keeps the document date
    meaning, and a short "bis" or "zum" after a deadline ("Zahlbar bis",
    "Bitte zahlen Sie bis zum") counts as the deadline.
    """
    original = prefix[-60:].rstrip()
    tail = _fold(original)
    birth = _BIRTH_LABEL.search(tail[-25:])
    if birth:
        return -1.0, _original_span(original, tail, len(tail) - len(tail[-25:]) + birth.start(),
                                    len(tail) - len(tail[-25:]) + birth.end())
    place = _PLACE_DATE_RE.search(original + " ")
    if place and place.group(1).casefold() not in _NOT_A_PLACE:
        return 0.4, f"{place.group(1)}, den"
    found = _labels_in(tail, _POSITIVE_LABELS) + _labels_in(tail, _NEGATIVE_LABELS)
    if not found:
        return 0.0, None
    closest = max(found, key=lambda entry: (entry[3], entry[3] - entry[2]))
    delta, text, start, end = closest
    if delta < 0:
        due = [entry for entry in found if entry[0] < 0 and _DUE_LABEL_RE.fullmatch(entry[1])]
        shared = None if due else _shared_document_label(tail, found, closest)
        if shared is not None:
            delta, start = shared[0], shared[2]
        elif due:
            delta = min(entry[0] for entry in found if entry[0] < 0)
            start = min(start, min(entry[2] for entry in due))
    elif delta >= 0.3 and text.endswith("vom") and len(tail[:start].split()) >= 3:
        # "… für die Rechnung vom 05.05.2025" refers to another document.
        delta = 0.1
    return delta, _original_span(original, tail, start, end)


def _shared_document_label(
    tail: str, found: list[tuple[float, str, int, int]], closest: tuple[float, str, int, int]
) -> tuple[float, str, int, int] | None:
    """The document date label a delivery or service date label is joined to, if any."""
    if not _SHARED_DATE_LABEL_RE.fullmatch(closest[1]):
        return None
    for entry in sorted(found, key=lambda e: e[3], reverse=True):
        if entry[0] >= 0.4 and entry[3] <= closest[2] and _LABEL_JOINER_RE.fullmatch(tail[entry[3]:closest[2]]):
            return entry
    return None


def _column_label(header: str) -> tuple[float, str | None]:
    """Label of the first column in a table header line ("Rechnungsdatum  Kundennummer …")."""
    folded = _fold(header)
    birth = _BIRTH_LABEL.search(folded)
    if birth:
        return -1.0, _original_span(header, folded, birth.start(), birth.end())
    found = _labels_in(folded, _POSITIVE_LABELS, anywhere=True) + _labels_in(folded, _NEGATIVE_LABELS, anywhere=True)
    if not found:
        return 0.0, None
    delta, _text, start, end = min(found, key=lambda entry: (entry[2], -(entry[3] - entry[2])))
    return delta, _original_span(header, folded, start, end)


def _original_span(original: str, folded: str, start: int, end: int) -> str:
    """The text of ``original`` that produced ``folded[start:end]``."""
    first = _original_offset(original, folded, start)
    last = _original_offset(original, folded, end)
    return original[first:last].strip(" :") or folded[start:end]


def _find_dates(doc: _Doc, today: date) -> list[DateCandidate]:
    hits: list[_DateHit] = []
    first_count = max(1, len(doc.first))
    for lines in doc.page_lines:
        previous_text = ""
        previous_has_date = False
        for line in lines:
            found = _scan_dates(line.fixed, today)
            line_hits: list[_DateHit] = []
            last_end = 0
            for value, start, end, kind in found:
                if value.year < 1900 or value > today + timedelta(days=FUTURE_DAYS):
                    last_end = end
                    continue
                base = {"numeric": 0.5, "text": 0.55, "iso": 0.45, "month": 0.25}[kind]
                prefix = line.fixed[last_end:start]
                delta, label = _label_score(prefix)
                if (
                    label is None
                    and last_end == 0
                    and previous_text
                    and not previous_has_date
                    and not re.search(r"[A-Za-zÄÖÜäöüß]", prefix)
                ):
                    # Table layout: the label is on the line above ("Rechnungsdatum  Kundennummer").
                    above_delta, above_label = _column_label(previous_text)
                    if above_label:
                        delta, label = above_delta * 0.6, above_label
                if kind == "month" and delta < 0:
                    # A month is a period by nature; only explicit due/birth labels count.
                    delta = delta if delta <= -0.9 else delta / 3
                score = base + delta
                score += 0.1 if line.page == 0 else (0.0 if line.page == 1 else -0.1)
                if line.page == 0 and line.index < first_count * 0.4:
                    score += 0.05
                if value > today:
                    score -= 0.15
                hit = _DateHit(value, line.fixed[start:end], score, label, line.page, line.index, start, end, kind)
                if line_hits and _RANGE_GAP_RE.match(line.fixed[line_hits[-1].end:start]):
                    hit.score -= 0.35
                    line_hits[-1].score -= 0.35
                line_hits.append(hit)
                last_end = end
            hits.extend(line_hits)
            previous_text = line.fixed
            previous_has_date = bool(found)
    if not hits:
        return []
    newest = max(hit.value for hit in hits if hit.score > 0) if any(hit.score > 0 for hit in hits) else None
    if newest:
        for hit in hits:
            if (newest - hit.value).days > 3650:
                hit.score -= 0.1
    merged: dict[date, tuple[_DateHit, int]] = {}
    for hit in hits:
        best, count = merged.get(hit.value, (hit, 0))
        if hit.score > best.score:
            best = hit
        merged[hit.value] = (best, count + 1)
    candidates = []
    for value, (best, count) in merged.items():
        score = best.score + 0.05 * min(3, count - 1)
        candidates.append(
            DateCandidate(value=value.isoformat(), text=best.text.strip(), score=round(score, 3), label=best.label)
        )
    candidates.sort(key=lambda c: (c.score, c.value), reverse=True)
    return candidates


def parse_date_text(text: str | None, *, today: date | None = None) -> date | None:
    """Parse a single date written by a person or a model ("12.03.2025", "12. März 2025", "2025-03-12").

    Returns None for anything else, for dates before 1900 and for dates more
    than 60 days in the future.
    """
    if not text:
        return None
    today = today or timeutil.today()
    value_text = _fix_ocr_digits(str(text).strip())
    found = _scan_dates(value_text, today)
    if not found:
        return None
    value = found[0][0]
    if value.year < 1900 or value > today + timedelta(days=FUTURE_DAYS):
        return None
    return value


# ---------------------------------------------------------------------------
# Sender

_LEGAL_FORM_PATTERNS = (
    r"GmbH\s*&\s*Co\.?\s*(?:KGaA|KG|OHG)",
    r"AG\s*&\s*Co\.?\s*(?:KGaA|KG|OHG)",
    r"SE\s*&\s*Co\.?\s*(?:KGaA|KG|OHG)",
    r"gGmbH", r"GmbH", r"GMBH", r"Gmbh", r"mbH", r"AG", r"SE", r"KGaA", r"KG", r"OHG", r"GbR",
    r"PartG(?:mbB)?", r"PartGmbB", r"UG\s*\(haftungsbeschränkt\)", r"UG", r"e\.\s?V\.", r"e\.\s?K\.", r"eG",
    r"VVaG", r"V\.?V\.?a\.?G\.?", r"a\.\s?G\.", r"Ltd\.?", r"Limited", r"Inc\.?", r"LLC", r"Corp\.?", r"plc",
    r"S\.A\.", r"S\.à\s?r\.\s?l\.", r"SARL", r"S\.C\.A\.", r"s\.r\.o\.", r"a\.\s?s\.", r"B\.V\.", r"N\.V\.",
    r"S\.p\.A\.", r"S\.r\.l\.", r"AB", r"A/S", r"Oy",
)
_LEGAL_FORM_RE = re.compile(
    r"(?:(?<=\s)|(?<=-)|^)(?:" + "|".join(_LEGAL_FORM_PATTERNS) + r")(?=$|[\s,;:.)·•|])"
)
_TRAILING_LEGAL_RE = re.compile(
    r"(?:[\s,]+|-)(?:" + "|".join(_LEGAL_FORM_PATTERNS) + r")\.?\s*$"
)

# Names of institutions that identify a sender on their own.
_INSTITUTION_NAMES = {
    "aok", "barmer", "dak", "bkk", "ikk", "tk", "kkh", "hkk", "hek", "knappschaft", "jobcenter", "familienkasse",
    "beitragsservice", "finanzamt", "stadtwerke", "sparkasse", "volksbank", "raiffeisenbank", "postbank",
    "commerzbank", "landeshauptstadt", "stadtverwaltung", "gemeinde", "gemeindeverwaltung", "landkreis",
    "landratsamt", "bürgeramt", "buergeramt", "praxis", "gemeinschaftspraxis", "zahnarztpraxis", "arztpraxis",
    "apotheke", "klinikum", "krankenhaus", "klinik", "universität", "universitaet", "hochschule", "kanzlei",
    "hausverwaltung", "wohnungsgenossenschaft", "wohnungsbaugenossenschaft", "immobilienverwaltung",
    "rechtsanwälte", "rechtsanwalt", "rechtsanwaltskanzlei", "notar", "notariat", "steuerberater",
    "steuerberatung", "steuerberatungsgesellschaft", "verein", "stiftung", "kirchgemeinde", "pfarramt",
    "polizei", "polizeidirektion", "bundesagentur", "rentenversicherung", "berufsgenossenschaft",
    "verbraucherzentrale", "kita", "kindertagesstätte", "kindergarten", "grundschule", "gymnasium",
    "oberschule", "gesamtschule", "berufsschule", "mvz", "tierarztpraxis", "physiotherapie",
}
# Words ending like this are institutions too (Commerzbank, Hauptzollamt, Techniker Krankenkasse …).
_INSTITUTION_SUFFIXES = (
    "sparkasse", "bank", "versicherung", "versicherungen", "krankenkasse", "pflegekasse", "kasse", "amt", "werke",
    "verwaltung", "genossenschaft", "klinik", "klinikum", "praxis", "schule", "apotheke", "verband", "stiftung",
    "kanzlei", "agentur", "ministerium", "behörde", "behoerde", "direktion", "zentrale", "gemeinde",
    "versorgung", "energie", "verkehrsbetriebe", "betriebe",
)
_NOT_INSTITUTION = {"gesamt", "insgesamt", "samt", "datenbank", "sitzbank", "kasse", "amt", "bank", "energie",
                    "betriebe", "versorgung", "zentrale"}
_TITLE_MARKER_RE = re.compile(r"(?:^|\s)(?:Dr\.\s?med\.(?:\s?dent\.)?|Dr\.|Prof\.|Dipl\.-\w+\.?)(?=\s)")

# Lines and segments that are field labels, not names ("Kundennummer: …", "Tel. …", "Sitz: …").
_FIELD_LABEL_RE = re.compile(
    r"^(?:"
    r"(?:kunden|rechnungs|vertrags|versicherungs|versicherten|policen|mitglieds|steuer|personal|z(?:ä|ae)hler|"
    r"bestell|auftrags|beleg|mandats|gl(?:ä|ae)ubiger|abrechnungs|leistungs|liefer|buchungs)[\s.-]*"
    r"(?:nummer|nr|id|konto|datum|betrag|adresse|anschrift|schein|nehmer|beginn|ablauf|summe|klasse|stand|"
    r"referenz|partner|zeitraum|tag|text)"
    r"|(?:datum|date|seite|page|tel|telefon|fon|phone|fax|telefax|mobil|handy|e-?mail|internet|web|www|https?|"
    r"betreff|betr|iban|bic|swift|blz|kontonummer|konto-?nr|bankverbindung|kontoverbindung|bankleitzahl|"
    r"amtsgericht|registergericht|handelsregister|hrb|hra|sitz|vorstand|vorstände|aufsichtsrat|vorsitz|"
    r"vorsitzende[r]?|inhaber(?:in)?|ust|ust-?id(?:nr)?|ust-?idnr|umsatzsteuer-?id\w*|steuernummer|kd|az|"
    r"kassenzeichen|aktenzeichen|patient(?:in)?|mieter(?:in)?|vermieter(?:in)?|objekt|lieferstelle|"
    r"verbrauchsstelle|abnahmestelle|zeitraum|ansprechpartner(?:in)?|sachbearbeiter(?:in)?|bearbeiter(?:in)?|"
    r"durchwahl|öffnungszeiten|oeffnungszeiten|sprechzeiten|registernummer|versicherungsnehmer(?:in)?|"
    r"geschäftsführ\w*|geschaeftsfuehr\w*|ihr zeichen|unser zeichen|ihre nachricht|unsere nachricht|"
    r"gesendet|sent|subject)(?![a-zäöüß])"
    r"|(?:von|an|to|from|cc|bcc|bank|konto|mail|name)\s*:"
    r")",
    re.IGNORECASE,
)
_BAD_FIRST_WORDS = {
    "ihre", "ihr", "ihrem", "ihren", "unsere", "unser", "unserem", "mit", "bei", "für", "fuer", "von", "vom", "an",
    "zu", "zur", "zum", "im", "in", "die", "der", "das", "den", "dem", "des", "sehr", "liebe", "lieber", "hallo",
    "guten", "bitte", "danke", "vielen", "wir", "sie", "es", "bis", "ab", "per", "und", "oder", "auf",
}
_BANK_LINE_RE = re.compile(
    r"\b(?:iban|bic|swift|blz|bankverbindung|kontoverbindung|konto-?nr|kontonummer|bankleitzahl|"
    r"gläubiger-?id|glaeubiger-?id)\b|^bank\s*:",
    re.IGNORECASE,
)
# A line that only announces bank details below it ("Bankverbindung:").
_BANK_HEADING_RE = re.compile(
    r"(?:bankverbindung(?:en)?|kontoverbindung(?:en)?|bank|kreditinstitut|geldinstitut)\s*:?", re.IGNORECASE
)
_ACCOUNT_LINE_RE = re.compile(r"(?:iban|bic|swift|blz|kto\b|konto(?:-?nr\.?|nummer)?\b)", re.IGNORECASE)
# Fields that name someone other than the author, also without a colon ("Krankenkasse AOK PLUS").
_THIRD_PARTY_LABEL_RE = re.compile(
    r"(?:versichert|versicherung|mitglied|mitgliedschaft)\s+bei|krankenkasse|krankenversicherung|pflegekasse|"
    r"kostentr(?:ä|ae)ger(?:in)?|arbeitgeber(?:in)?|kreditinstitut|geldinstitut",
    re.IGNORECASE,
)
# Labels that do introduce the author ("Absender:", "Verkauft von:", "Rechnungssteller:").
_SENDER_LABEL_RE = re.compile(
    r"(?:^|\s)(?:absender(?:in)?|von|from|durch|by|firma|verk(?:ä|ae)ufer(?:in)?|rechnungssteller(?:in)?|"
    r"aussteller(?:in)?|herausgeber(?:in)?|anbieter(?:in)?|lieferant(?:in)?|leistungserbringer(?:in)?|"
    r"seller|vendor)$",
    re.IGNORECASE,
)
# The first lines of page 1, where a letter names its author (logo text, name, address).
LETTERHEAD_LINES = 5
_SEGMENT_SPLIT_RE = re.compile(r"\s*[·•|∙・]\s*|\s{2,}|\t|\s+[–—]\s+|\s+-\s+|,\s+(?=\S)")

_GENERIC_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "gmx.de", "gmx.net", "gmx.at", "gmx.ch", "gmx.com", "web.de", "t-online.de",
    "outlook.com", "outlook.de", "hotmail.com", "hotmail.de", "live.com", "live.de", "msn.com", "yahoo.com",
    "yahoo.de", "ymail.com", "icloud.com", "me.com", "mac.com", "aol.com", "aol.de", "posteo.de", "posteo.net",
    "mailbox.org", "freenet.de", "arcor.de", "online.de", "proton.me", "protonmail.com", "pm.me", "mail.de",
    "email.de", "vodafonemail.de", "kabelmail.de", "magenta.de", "1und1.de", "ionos.de",
}
_LOCAL_TLDS = {"local", "lan", "home", "internal", "intern", "localdomain", "box", "test", "invalid", "localhost"}
_GENERIC_MAIL_NAMES = {
    "noreply", "no reply", "no-reply", "donotreply", "do not reply", "info", "service", "kundenservice",
    "kundendienst", "rechnung", "rechnungen", "rechnungsversand", "billing", "invoice", "invoices", "buchhaltung",
    "support", "mailer-daemon", "postmaster", "newsletter", "kontakt", "office", "team", "online-rechnung",
    "e-rechnung", "mail", "webmaster", "versand", "bestellung", "bestellungen", "order", "orders", "kundencenter",
    "scanner", "scan", "drucker", "printer",
}
_SERVICE_SUFFIX_RE = re.compile(
    r"\s+(?:Rechnungsservice|Rechnungsversand|Kundenservice|Kundendienst|Kundencenter|Service|Team|Billing|"
    r"Rechnung|Rechnungen|Buchhaltung|Online-Rechnung|Newsletter|Support|Info)$",
    re.IGNORECASE,
)
_DEVICE_RE = re.compile(r"scan|printer|drucker|mfp|canon|brother|epson|kyocera|ricoh|xerox|konica|lexmark|sharp", re.I)
_EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}(?![\w-])")
_URL_RE = re.compile(r"(?:https?://|(?<![\w@.])www\.)([A-Za-z0-9.-]+\.[A-Za-z]{2,24})(?![\w-])", re.IGNORECASE)
_MAIL_HEADER_RE = re.compile(r"^\s*(?:von|from|absender)\s*:\s*(.+)$", re.IGNORECASE)
_MAIL_RECIPIENT_HEADER_RE = re.compile(r"^\s*(?:an|to|cc|kopie|bcc)\s*:\s*(.+)$", re.IGNORECASE)


def short_sender(name: str | None) -> str | None:
    """Sender without legal form: "Muster Elektronik GmbH & Co. KG" -> "Muster Elektronik",
    "Allianz Versicherungs-AG" -> "Allianz"."""
    if not name:
        return None
    value = re.sub(r"\s+", " ", name).strip()
    for _ in range(3):
        match = _TRAILING_LEGAL_RE.search(value)
        if not match:
            break
        stripped = value[:match.start()].strip(" ,-")
        if match.group(0).startswith("-") and " " in stripped:
            # "Versicherungs-AG": the word glued to the legal form is part of it.
            stripped = stripped.rsplit(" ", 1)[0].strip(" ,-")
        if not stripped:
            break
        value = stripped
    return value or name.strip() or None


def entity_key(name: str | None) -> str:
    """Comparison key for sender names: normalized, without legal form."""
    return normalize(short_sender(name) or "")


def name_from_domain(domain: str | None) -> str | None:
    """"stadtwerke-dresden.de" -> "Stadtwerke Dresden"; None for mail providers."""
    if not domain:
        return None
    labels = [label for label in domain.lower().strip(".").split(".") if label]
    if len(labels) < 2 or labels[-1] in _LOCAL_TLDS:
        return None
    if ".".join(labels[-2:]) in _GENERIC_MAIL_DOMAINS or ".".join(labels[-3:]) in _GENERIC_MAIL_DOMAINS:
        return None
    if len(labels) >= 3 and labels[-2] in {"co", "com", "org", "net", "gv", "ac", "or"}:
        core = labels[-3]
    else:
        core = labels[-2]
    parts = [part for part in core.split("-") if part]
    if not parts:
        return None
    name = " ".join(part.capitalize() if part.isalpha() else part for part in parts)
    return name if len(name) >= 2 else None


_TLD_SUFFIX_RE = re.compile(r"\.(?:de|com|net|org|eu|at|ch|cz|sk|pl|fr|it|nl|es|co\.uk|info|biz|io|shop)$", re.I)


def _strip_tld(name: str) -> str:
    """"Alza.de" -> "Alza" (shop names are often written like their domain)."""
    return _TLD_SUFFIX_RE.sub("", name.strip())


def name_from_mail(value: str | None, display: str | None = None) -> str | None:
    """Sender name from a mail address ("Alza.de <rechnung@alza.de>" -> "Alza")."""
    parsed_name, address = parseaddr(value or "")
    shown = (display or parsed_name or "").strip().strip("\"'")
    if shown and "@" not in shown and normalize(shown) not in _GENERIC_MAIL_NAMES and not _DEVICE_RE.search(shown):
        cleaned = _strip_tld(shown)
        cleaned = _SERVICE_SUFFIX_RE.sub("", cleaned).strip(" -|:")
        if cleaned and normalize(cleaned) not in _GENERIC_MAIL_NAMES:
            if cleaned.islower() and len(cleaned.split()) == 1:
                cleaned = cleaned[:1].upper() + cleaned[1:]
            return cleaned[:80]
    if "@" in address:
        local, domain = address.rsplit("@", 1)
        if _DEVICE_RE.search(local) or _DEVICE_RE.search(domain.split(".")[0]):
            # Scan-to-mail from a printer: the mail sender says nothing about the document.
            return None
        return name_from_domain(domain)
    return None


_MAIL_HEADER_NEIGHBOUR_RE = re.compile(
    r"^\s*(?:gesendet|datum|date|sent|an|to|cc|betreff|subject|kopie)\s*:", re.IGNORECASE
)


def _is_mail_header_block(doc: _Doc, line: _Line) -> bool:
    """"Von: …" is a mail header if it carries an address or sits next to other header lines."""
    match = _MAIL_HEADER_RE.match(line.text)
    if not match:
        return False
    value = match.group(1)
    if _EMAIL_RE.search(value):
        return True
    if any(ch.isdigit() for ch in value) or len(value.split()) > 6:
        return False
    lines = doc.page_lines[line.page]
    neighbours = lines[max(0, line.index - 2): line.index] + lines[line.index + 1: line.index + 3]
    return any(_MAIL_HEADER_NEIGHBOUR_RE.match(other.text) for other in neighbours)


_CLOSING_RE = re.compile(
    r"^(?:mit\s+)?(?:freundlich(?:en|e)|besten|herzlich(?:en|e)|viele|liebe|beste)(?:\s+[a-zäöü]+)?"
    r"\s+gr(?:ü|ue)(?:ß|ss)(?:en|e)\b|"
    r"^(?:kind|best)\s+regards\b|^sincerely\b",
    re.IGNORECASE,
)
_CLOSING_NAME_RE = re.compile(r"^(?:ihre?|ihr|eure?|dein(?:e)?|i\.\s?a\.|i\.\s?v\.|ppa\.)\s+", re.IGNORECASE)
_GENERIC_NAME_WORDS = {
    "kundenservice", "kundendienst", "kundencenter", "service", "abteilung", "postanschrift", "hausanschrift",
    "besucheranschrift", "lieferanschrift", "rechnungsanschrift", "anschrift", "absender", "empfänger", "team",
    "buchhaltung", "personalabteilung", "verwaltung", "zentrale", "geschäftsstelle", "filiale",
}


def _has_street_segment(text: str) -> bool:
    return any(_STREET_RE.match(part) or _STREET_WORD_RE.search(part) for part in _split_segments(text))


def _plain_name(text: str) -> bool:
    """A short capitalised name without digits ("Hans Meier", "Muster Software")."""
    words = text.split()
    if not 1 <= len(words) <= 6 or len(text) > 60 or not text[:1].isupper():
        return False
    if any(ch.isdigit() for ch in text) or "@" in text or ":" in text:
        return False
    if _FIELD_LABEL_RE.match(text) or _is_address_text(text) or _SALUTATION_RE.match(text):
        return False
    if words[0].casefold() in _BAD_FIRST_WORDS or text.casefold() in _GENERIC_NAME_WORDS:
        return False
    if any(word.casefold() in _GENERIC_NAME_WORDS for word in words[:1]):
        return False
    norm = normalize(text)
    if any(regex.search(norm) for regex in _HEADING_RES):
        return False
    # Subtitles like "Fachärztin für Innere Medizin" contain function words, names do not.
    return not any(word in STOPWORDS for word in norm.split())


def _split_segments(text: str) -> list[str]:
    return [part.strip() for part in _SEGMENT_SPLIT_RE.split(text) if part and part.strip()]


_NAME_PREFIX_RE = re.compile(
    r"^(?:(?:absender|von|from|firma)\s*:\s*|(?:verkauft|versandt|versendet|geliefert|vermittelt|vertreten|"
    r"betrieben|herausgegeben|ausgestellt)\s+(?:von|durch)\s+|(?:verkauf|versand|lieferung)\s+durch\s+|"
    r"ein service (?:der|des|von)\s+|im auftrag (?:der|des|von)\s+|sold by\s+|shipped by\s+)",
    re.IGNORECASE,
)


def _clean_name(text: str) -> str:
    value = _NAME_PREFIX_RE.sub("", text.strip())
    value = re.sub(r"\s+", " ", value).strip(" ,;:-–—·•|*\"'")
    if value.endswith(".") and not re.search(r"(?:\b[A-Za-zà]\.|Co\.|Ltd\.|Inc\.|Corp\.|med\.|dent\.)$", value):
        value = value[:-1].rstrip()
    return _soften_caps(value)[:80]


def _soften_caps(value: str) -> str:
    """"STADTWERKE DRESDEN GmbH" -> "Stadtwerke Dresden GmbH"; short acronyms (AOK, REWE) stay."""
    words = value.split()
    if sum(1 for word in words if word.isupper() and len(word) >= 5) < 2:
        return value

    def soften(word: str) -> str:
        if not word.isupper() or len(word) < 5 or _LEGAL_FORM_RE.fullmatch(word):
            return word
        return "-".join(part.capitalize() for part in word.split("-"))

    return " ".join(soften(word) for word in words)


_HEADING_RES = tuple(doc_type.heading_re for doc_type in doctypes.DOC_TYPES)


def _segment_kind(segment: str) -> str | None:
    """How strongly a text segment looks like an organisation name: legal | institution | compound | None."""
    words = segment.split()
    if not words or len(words) > 8 or len(segment) > 70 or len(segment) < 2:
        return None
    lowered = segment.casefold()
    if "@" in segment or "www." in lowered or "http" in lowered:
        return None
    if sum(ch.isdigit() for ch in segment) > 2:
        return None
    if _FIELD_LABEL_RE.match(segment) or _is_address_text(segment):
        return None
    first = words[0].casefold().strip(".,:")
    has_legal = bool(_LEGAL_FORM_RE.search(segment))
    if first in _BAD_FIRST_WORDS and not has_legal:
        return None
    stop_hits = sum(1 for word in normalize(segment).split() if word in STOPWORDS)
    if stop_hits > 2 or (stop_hits > 1 and len(words) <= 4 and not has_legal):
        return None
    norm = normalize(segment)
    if any(regex.search(norm) for regex in _HEADING_RES) and not has_legal:
        # "Abschlagsrechnung Strom", "Versicherungsschein" … are headings, not names.
        return None
    if has_legal:
        # The legal form alone ("GmbH") is not a name.
        rest = _LEGAL_FORM_RE.sub("", segment).strip(" ,.-&")
        return "legal" if re.search(r"[A-Za-zÄÖÜäöüß]{2,}", rest) else None
    if not segment[:1].isupper() and not segment[:1].isdigit():
        return None
    if _TITLE_MARKER_RE.search(" " + segment + " ") and len(words) >= 2:
        return "institution"
    folded = [word.casefold().strip(".,:;()") for word in words]
    for word in folded:
        if word in _INSTITUTION_NAMES:
            return "institution" if len(words) >= 2 or word in _STANDALONE_INSTITUTIONS else None
    if len(words) >= 2:
        for word in folded:
            base = word.split("-")[-1]
            if base not in _NOT_INSTITUTION and base.endswith(_INSTITUTION_SUFFIXES) and len(base) > 4:
                return "compound"
    return None


_STANDALONE_INSTITUTIONS = {
    "aok", "barmer", "dak", "bkk", "ikk", "knappschaft", "jobcenter", "familienkasse", "beitragsservice",
    "stadtwerke", "postbank", "commerzbank", "hek", "hkk", "kkh",
}


@dataclass
class _Candidate:
    name: str
    score: float
    sources: set[str] = field(default_factory=set)
    known: bool = False


def _unordered(key: str) -> str:
    """Comparison key that ignores word order ("mustermann erika" = "erika mustermann")."""
    return " ".join(sorted(key.split()))


class _SenderPool:
    def __init__(self, excluded_keys: set[str]):
        self.items: dict[str, _Candidate] = {}
        self.excluded = {_unordered(key) for key in excluded_keys if key}

    def exclude(self, name: str) -> None:
        key = entity_key(name)
        if key:
            self.excluded.add(_unordered(key))

    def is_excluded(self, key: str) -> bool:
        return _unordered(key) in self.excluded

    def add(self, name: str | None, score: float, source: str, *, known: bool = False) -> None:
        if not name:
            return
        name = _clean_name(name)
        key = entity_key(name)
        if len(key) < 2 or self.is_excluded(key):
            return
        existing = self.items.get(key)
        if existing is None:
            self.items[key] = _Candidate(name=name, score=score, sources={source}, known=known)
            return
        if source not in existing.sources:
            # Independent sources agreeing make a candidate more credible.
            existing.score = min(1.2, max(existing.score, score) + 0.05)
            existing.sources.add(source)
        else:
            existing.score = max(existing.score, score)
        if known and not existing.known:
            existing.name, existing.known = name, True
        elif not existing.known and score > existing.score - 0.05 and len(name) > len(existing.name):
            existing.name = name

    def corroborate(self, name: str | None, score: float, source: str) -> None:
        """Domain names often are the first words of the company name (alza.de -> "Alza.cz a.s.")."""
        key = entity_key(name)
        if not key:
            return
        for cand_key, candidate in self.items.items():
            if cand_key == key or cand_key.startswith(key + " ") or key.replace(" ", "") == cand_key.replace(" ", ""):
                if source not in candidate.sources:
                    candidate.score += 0.05
                    candidate.sources.add(source)
                return
        self.add(name, score, source)

    def ranked(self) -> list[_Candidate]:
        """Candidates best first. Variants of one name ("Praxis Dr. Schmidt" and
        "Praxis Dr. med. Anna Schmidt") are merged; the longer spelling is kept."""
        keys = sorted(self.items, key=lambda k: len(k.split()))
        for short in keys:
            if short not in self.items or len(short.split()) < 2:
                continue
            for long in keys:
                if long == short or long not in self.items or not _ordered_subset(short.split(), long.split()):
                    continue
                small, big = self.items.pop(short), self.items[long]
                keep_long = big.known or (not small.known and big.score >= small.score - 0.1)
                if small.sources - big.sources:
                    big.score = min(1.2, max(big.score, small.score) + 0.05)
                else:
                    big.score = max(big.score, small.score)
                big.sources |= small.sources
                if not keep_long:
                    # "Verkauft von Amazon EU S.à r.l." in the body must not replace the letterhead name.
                    big.name, big.known = small.name, small.known
                break
        return sorted(self.items.values(), key=lambda c: (c.score, c.known, len(c.name)), reverse=True)


def _ordered_subset(small: list[str], big: list[str]) -> bool:
    """True if all words of ``small`` occur in ``big`` in the same order (and ``big`` is longer)."""
    if len(small) >= len(big):
        return False
    iterator = iter(big)
    return all(word in iterator for word in small)


def _looks_like_person(name: str) -> bool:
    """"Erika Mustermann" yes; "Vodafone", "Muster GmbH" or "Sparkasse Dresden" no."""
    words = name.split()
    if not 2 <= len(words) <= 4 or _segment_kind(name):
        return False
    return all(re.fullmatch(r"[A-ZÄÖÜ][a-zäöüß]+(?:-[A-ZÄÖÜ][a-zäöüß]+)?", word) for word in words)


def _owner_info(context: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    """Addresses and names of the archive's owners known from the mail context.

    ``forwarded_by_owner`` means "the mail sender is not the author" (a forward,
    an own address, a scanner). Its display name is only treated as an owner
    name when it looks like a person, so a vendor address on the allowlist
    never hides that vendor as the sender of its own letterhead.
    """
    addresses: set[str] = set()
    names: set[str] = set()
    flag = context.get("forwarded_by_owner")
    if isinstance(flag, str) and "@" in flag:
        addresses.add(parseaddr(flag)[1].lower())
    elif isinstance(flag, (list, tuple, set)):
        addresses.update(parseaddr(str(value))[1].lower() for value in flag if value)
    mail_from = parseaddr(str(context.get("mail_from") or ""))[1].lower()
    if flag is True or (mail_from and mail_from in addresses):
        if mail_from:
            addresses.add(mail_from)
        owner_name = context.get("mail_from_name") or parseaddr(str(context.get("mail_from") or ""))[0]
        if owner_name and _looks_like_person(str(owner_name)):
            names.add(str(owner_name))
    for value in context.get("owner_names") or []:
        names.add(str(value))
    return {a for a in addresses if a}, names


def _known_sender_keys(known_senders: Iterable[str] | None) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name in known_senders or ():
        if not name:
            continue
        key = normalize(str(name))
        if len(key) < 3 or key in seen:
            continue
        seen.add(key)
        result.append((str(name).strip(), key))
    return result


def _texts_before(line: _Line, key: str) -> list[str]:
    """The original text of ``line`` in front of every occurrence of ``key`` (a normalized name)."""
    text = unicodedata.normalize("NFC", line.text)
    norm, offsets = normalize_with_map(text)
    found = [
        text[:offsets[match.start()]]
        for match in re.finditer(r"(?<![^ ])" + re.escape(key) + r"(?![^ ])", norm)
    ]
    # Both normalizations agree; should they ever not, the name counts as unlabelled.
    return found or [""]


def _field_label_before(prefix: str) -> str | None:
    """The field label right in front of a name ("Versichert bei:", "Bankverbindung"), if any."""
    text = prefix.rstrip()
    if not text:
        return None
    segments = _split_segments(text)
    last = (segments[-1] if segments else text).strip()
    if text.endswith(":"):
        label = last.rstrip(" :").strip()
        return label if label and len(label.split()) <= 4 else None
    match = _FIELD_LABEL_RE.match(last)
    if (match and not last[match.end():].strip(" :.")) or _THIRD_PARTY_LABEL_RE.fullmatch(last):
        return last
    return None


def _names_third_party(prefix: str) -> bool:
    """True if a name behind ``prefix`` is a field value about someone else, not the author."""
    label = _field_label_before(prefix)
    return label is not None and not _SENDER_LABEL_RE.search(label) and _segment_kind(label) is None


def _third_party_segment(segments: list[str], position: int) -> bool:
    """A line segment that names someone else in a field: "Versichert bei: AOK PLUS",
    "Krankenkasse AOK PLUS", or "AOK PLUS" in the column right of "Krankenkasse"."""
    segment = segments[position]
    labelled = re.match(r"([^:]{1,40}):\s*\S", segment)
    if labelled:
        return _names_third_party(labelled.group(1) + ":")
    if _THIRD_PARTY_LABEL_RE.match(segment):
        return True
    return position > 0 and _names_third_party(segments[position - 1])


# Text that ends with a segment separator: what follows starts a segment of its own.
_SEGMENT_END_RE = re.compile(r"(?:[·•|∙・\t]|\s[–—-]|,)\s*$|\s{2,}$")


def _is_letterhead(line: _Line, prefix: str) -> bool:
    """A name at the top of page 1 that heads its line, where a letter names its author.

    ``prefix`` is the text in front of the name on its line.
    """
    if line.page != 0 or line.zone != "header" or line.recipient or line.index >= LETTERHEAD_LINES:
        return False
    segments = _split_segments(prefix.rstrip()) if prefix.strip() else []
    own_segment = "" if not segments or _SEGMENT_END_RE.search(prefix) else segments[-1]
    # Running text that merely mentions the name ("… an die Sparkasse Dresden") is not a letterhead.
    return len(own_segment.split()) <= 3


def _is_bank_details(lines: list[_Line], line: _Line, *, letterhead: bool) -> bool:
    """Bank details: an "IBAN …" or "Bankverbindung: …" line, a bank name right below
    a "Bankverbindung" heading or, outside the letterhead, right above an "IBAN …" line."""
    if _BANK_LINE_RE.search(line.text):
        return True
    before = lines[line.index - 1].text.strip() if line.index > 0 else ""
    if before and _BANK_HEADING_RE.fullmatch(before):
        return True
    after = lines[line.index + 1].text.strip() if line.index + 1 < len(lines) else ""
    # A bank's own letterhead is often followed by the customer's account; elsewhere a
    # name right above "IBAN …" is the bank of whoever wrote the document.
    return not letterhead and bool(_ACCOUNT_LINE_RE.match(after))


def _heads_second_address(lines: list[_Line], line: _Line) -> bool:
    """The name line of an address block that follows another address: the recipient's,
    even without "Herrn"/"Frau" ("Kita Sonnenschein · Lindenstr. 3 · 01069 Dresden" above
    "Erika Mustermann / Musterweg 5 / 01234 Dresden")."""
    following = lines[line.index + 1: line.index + 3]
    if not any(_is_address_text(other.text) for other in following):
        return False
    return any(_PLZ_ANYWHERE_RE.search(other.text) for other in lines[: line.index])


def _detect_sender(
    doc: _Doc, context: Mapping[str, Any], known_senders: Iterable[str] | None
) -> tuple[str | None, list[str]]:
    owner_addresses, owner_names = _owner_info(context)
    first = doc.first
    recipient_text = " " + " ".join(line.norm for line in first if line.recipient) + " "
    pool = _SenderPool({entity_key(name) for name in owner_names})

    # 1. Known senders (names the user confirmed before) on page 1. Only the letterhead
    #    and the return address line say who wrote the document; a known bank or health
    #    insurer in the bank details or in a field ("Versichert bei: AOK PLUS") does not,
    #    and the owners' own names are never the sender.
    for name, key in _known_sender_keys(known_senders):
        needle = f" {key} "
        if needle in recipient_text:
            # The addressee of this very document (e.g. the user) is not its sender.
            pool.exclude(name)
            continue
        if pool.is_excluded(entity_key(name)):
            continue
        person = _looks_like_person(name)
        best = 0.0
        hits = 0
        for line in first:
            if line.recipient or needle not in line.padded:
                continue
            prefixes = [prefix for prefix in _texts_before(line, key) if not _names_third_party(prefix)]
            if not prefixes:
                continue
            letterhead = any(_is_letterhead(line, prefix) for prefix in prefixes)
            if _is_bank_details(first, line, letterhead=letterhead):
                continue
            if letterhead and person and _heads_second_address(first, line):
                letterhead = False
            hits += 1
            best = max(best, 0.95 if letterhead or line.return_line else 0.5)
        if best:
            specificity = 0.03 * min(3, len(key.split()) - 1) + 0.02 * min(3, hits - 1)
            pool.add(name, best + specificity, "known", known=True)

    # 2. Mail headers.
    mail_from_value = str(context.get("mail_from") or "")
    mail_address = parseaddr(mail_from_value)[1].lower()
    forwarded_value = context.get("forwarded_from")
    if forwarded_value:
        address = parseaddr(str(forwarded_value))[1].lower()
        if address not in owner_addresses:
            pool.add(name_from_mail(str(forwarded_value), context.get("forwarded_from_name")), 0.75, "mail")
    if mail_from_value and mail_address and mail_address not in owner_addresses:
        pool.add(name_from_mail(mail_from_value, context.get("mail_from_name")), 0.7, "mail")
    recipient_domains: set[str] = {address.rsplit("@", 1)[-1] for address in owner_addresses if "@" in address}
    header_senders: list[str] = []
    for line in doc.lines:
        recipient_match = _MAIL_RECIPIENT_HEADER_RE.match(line.text)
        if recipient_match:
            for address in _EMAIL_RE.findall(recipient_match.group(1)):
                recipient_domains.add(address.rsplit("@", 1)[-1].lower())
            continue
        match = _MAIL_HEADER_RE.match(line.text)
        if match and _is_mail_header_block(doc, line):
            address = parseaddr(match.group(1))[1].lower()
            if address and (address in owner_addresses or address == mail_address and owner_addresses):
                continue
            header_senders.append(match.group(1))
    if header_senders:
        # In a forwarded chain the original sender's header comes last.
        pool.add(name_from_mail(header_senders[-1]), 0.72, "mail-header")

    # 3. Letterhead, return address line, footer and body of page 1.
    first_text_line = True
    for line in first:
        if line.recipient:
            first_text_line = False
            continue
        if _MAIL_HEADER_RE.match(line.text) or _MAIL_RECIPIENT_HEADER_RE.match(line.text):
            continue
        is_bank_line = _is_bank_details(first, line, letterhead=line.index < LETTERHEAD_LINES)
        segments = _split_segments(line.text)
        for position, segment in enumerate(segments):
            if _third_party_segment(segments, position):
                continue
            kind = _segment_kind(segment)
            if line.return_line and position == 0 and (kind or len(segment.split()) <= 6):
                if not _is_address_text(segment) and not _FIELD_LABEL_RE.match(segment):
                    pool.add(segment, 0.9 if kind else 0.8, "return-line")
                    continue
            if kind is None:
                continue
            if line.zone == "header":
                if is_bank_line and line.index >= LETTERHEAD_LINES:
                    # Bank details below the letterhead (short pages have no footer zone).
                    continue
                score = {"legal": 0.8, "institution": 0.75, "compound": 0.7}[kind]
                if line.index < LETTERHEAD_LINES:
                    score += 0.08
                if is_bank_line:
                    score -= 0.3
                pool.add(segment, score, "letterhead")
            elif line.zone == "footer":
                if is_bank_line:
                    continue
                score = {"legal": 0.6, "institution": 0.5, "compound": 0.45}[kind]
                pool.add(segment, score, "footer")
            elif kind == "legal" and not is_bank_line:
                pool.add(segment, 0.3, "body")
        if first_text_line and line.zone == "header":
            first_text_line = False
            # A plain name on the very first line (no legal form) is a weak hint.
            only = segments[0] if segments else ""
            words = only.split()
            if (
                1 <= len(words) <= 5
                and only[:1].isupper()
                and not any(ch.isdigit() for ch in only)
                and not _FIELD_LABEL_RE.match(only)
                and words[0].casefold() not in _BAD_FIRST_WORDS
                and not any(regex.search(normalize(only)) for regex in _HEADING_RES)
                and not (len(first) > 1 and _is_address_text(first[1].text) and not _segment_kind(only))
            ):
                pool.add(only, 0.35, "first-line")

    # An address block (name, street, postal code) that is not the recipient is the sender's.
    has_recipient = any(line.recipient for line in first)
    for position, line in enumerate(first[:HEADER_LINES + 5]):
        if line.recipient or not _PLZ_ANYWHERE_RE.search(line.text):
            continue
        name_index = position - 1 if _STREET_WORD_RE.search(line.text) or _has_street_segment(line.text) else position - 2
        if name_index < 0 or (name_index == position - 2 and not _is_address_text(first[position - 1].text)):
            continue
        name_line = first[name_index]
        if name_line.recipient or name_line.return_line:
            continue
        name = _split_segments(name_line.text)[0] if name_line.text else ""
        if _plain_name(name):
            pool.add(name, 0.6 if has_recipient else 0.4, "address-block")

    # The signature after the closing formula ("Mit freundlichen Grüßen / Ihre Stadtwerke Leipzig").
    for lines in doc.page_lines:
        for position, line in enumerate(lines):
            if not _CLOSING_RE.match(line.text):
                continue
            for follower in lines[position + 1: position + 3]:
                name = _CLOSING_NAME_RE.sub("", follower.text).strip()
                name = _strip_tld(re.sub(r"\s+Team$", "", name, flags=re.IGNORECASE)).strip(" ,")
                if _plain_name(name) or _segment_kind(name):
                    pool.add(name, 0.55 if _segment_kind(name) else 0.45, "closing")
                    break

    # 4. Domains of e-mail addresses and web sites on page 1.
    for line in first:
        if line.recipient or _MAIL_RECIPIENT_HEADER_RE.match(line.text):
            continue
        domains = [address.rsplit("@", 1)[-1].lower() for address in _EMAIL_RE.findall(line.text)]
        domains += [match.group(1).lower() for match in _URL_RE.finditer(line.text)]
        for domain in domains:
            if domain in recipient_domains or any(domain.endswith("." + d) for d in recipient_domains):
                continue
            pool.corroborate(name_from_domain(domain), 0.4, "domain")

    ranked = pool.ranked()
    if not ranked:
        return None, []
    known_by_key: dict[str, str] = {}
    for name, _key in _known_sender_keys(known_senders):
        # The first spelling wins: archive senders come before rule senders.
        known_by_key.setdefault(entity_key(name), name)
    names: list[str] = []
    for candidate in ranked[:6]:
        name = known_by_key.get(entity_key(candidate.name), candidate.name)
        if name not in names:
            names.append(name)
    sender = names[0] if ranked[0].score >= MIN_SENDER_SCORE else None
    return sender, names[:5]


# ---------------------------------------------------------------------------
# Amounts

_CURRENCY = r"(?:EUR|€|Euro)"
_AMOUNT_DE_RE = re.compile(
    rf"(?<![\w.,])(?:(?P<cur1>{_CURRENCY})\s?)?(?P<sign>[-−–]\s?)?"
    r"(?P<int>\d{1,3}(?:[.\u00a0\u202f']\d{3})+|\d+),(?P<dec>\d{2})(?![\d,])(?P<sign2>-(?![\d]))?"
    rf"(?:\s?(?P<cur2>{_CURRENCY})(?![A-Za-z]))?"
)
_AMOUNT_EN_RE = re.compile(
    rf"(?<![\w.,])(?:(?P<cur1>{_CURRENCY}|\$|USD)\s?(?P<int>\d{{1,3}}(?:,\d{{3}})+|\d+)\.(?P<dec>\d{{2}})(?![\d.])"
    rf"|(?P<int2>\d{{1,3}}(?:,\d{{3}})+|\d+)\.(?P<dec2>\d{{2}})\s?(?P<cur2>{_CURRENCY})(?![A-Za-z]))"
)
_AMOUNT_INT_RE = re.compile(
    rf"(?<![\w.,])(?:(?P<int>\d{{1,3}}(?:\.\d{{3}})+|\d+)\s?(?P<cur2>{_CURRENCY})(?![A-Za-z])"
    rf"|(?P<cur1>{_CURRENCY})\s?(?P<int2>\d{{1,3}}(?:\.\d{{3}})+|\d+)(?![\d,.]))"
)

# (pattern on folded line, priority). Higher priority wins, later occurrences win ties.
_TOTAL_LABELS: tuple[tuple[re.Pattern[str], int], ...] = tuple(
    (re.compile(pattern), priority)
    for pattern, priority in (
        (r"rechnungsbetrag|gesamtbetrag|endbetrag|zahlbetrag|zu zahlen(?:der betrag)?|zu zahlender (?:betrag|beitrag)|"
         r"auszahlungsbetrag|gesamtsumme|rechnungssumme|bruttobetrag|brutto gesamt|summe brutto|gesamt brutto|"
         r"(?<!\w)total(?!\w)|amount due|invoice total|nachzahlung|guthaben|erstattung|abschlagsbetrag|"
         r"neuer kontostand|neuer saldo|ueberweisungsbetrag|forderungsbetrag|offener betrag|gesamtforderung|"
         r"zu erstatten|ihr guthaben|ihre nachzahlung", 3),
        (r"(?<!\w)gesamt|(?<!\w)summe|(?<!\w)betrag|abschlag|jahresbeitrag|monatsbeitrag|(?<!\w)beitrag|"
         r"kontostand", 2),
    )
)
_NOT_TOTAL_RE = re.compile(
    r"zwischensumme|nettobetrag|(?<!\w)netto(?!\w)|mwst|mehrwertsteuer|umsatzsteuer|(?<!\w)ust(?!\w)|"
    r"rabatt|skonto|versandkosten|alter kontostand|alter saldo|vorauszahlung|bereits gezahlt|bereits bezahlt|"
    r"geleistete|abzueglich|darin enthalten|gesamt-?brutto|steuerbrutto|sv-?brutto"
)


def _decimal(int_part: str, dec: str) -> Decimal | None:
    digits = re.sub(r"[.\u00a0\u202f',]", "", int_part)
    try:
        return Decimal(f"{digits}.{dec}")
    except InvalidOperation:
        return None


def format_amount(value: Decimal) -> str:
    """German notation: 1.234,56 €."""
    quantized = value.quantize(Decimal("0.01"))
    sign = "-" if quantized < 0 else ""
    integer, _, fraction = f"{abs(quantized):.2f}".partition(".")
    grouped = f"{int(integer):,}".replace(",", ".")
    return f"{sign}{grouped},{fraction} €"


def _amounts_in(text: str) -> list[tuple[Decimal, int, int, bool]]:
    """Amounts in a line: (value, start, end, has_currency)."""
    found: list[tuple[Decimal, int, int, bool]] = []
    taken: list[tuple[int, int]] = []

    def free(start: int, end: int) -> bool:
        return not any(start < t_end and end > t_start for t_start, t_end in taken)

    for match in _AMOUNT_DE_RE.finditer(text):
        value = _decimal(match.group("int"), match.group("dec"))
        if value is None:
            continue
        if match.group("sign") or match.group("sign2"):
            value = -value
        found.append((value, match.start(), match.end(), bool(match.group("cur1") or match.group("cur2"))))
        taken.append((match.start(), match.end()))
    for match in _AMOUNT_EN_RE.finditer(text):
        if not free(match.start(), match.end()):
            continue
        int_part = match.group("int") or match.group("int2")
        dec = match.group("dec") or match.group("dec2")
        value = _decimal(int_part.replace(",", ""), dec)
        if value is not None:
            found.append((value, match.start(), match.end(), True))
            taken.append((match.start(), match.end()))
    for match in _AMOUNT_INT_RE.finditer(text):
        if not free(match.start(), match.end()):
            continue
        int_part = match.group("int") or match.group("int2")
        value = _decimal(int_part, "00")
        if value is not None and not (1900 <= value <= 2100 and "." not in int_part):
            found.append((value, match.start(), match.end(), True))
            taken.append((match.start(), match.end()))
    found.sort(key=lambda entry: entry[1])
    return found


def _find_amounts(doc: _Doc) -> tuple[list[str], str | None]:
    amounts: list[Decimal] = []
    best: tuple[int, int, Decimal] | None = None  # (priority, order, value)
    order = 0
    lines = doc.lines
    for position, line in enumerate(lines):
        folded = _fold(line.fixed)
        line_amounts = _amounts_in(line.fixed)
        for value, _start, _end, has_currency in line_amounts:
            if has_currency and value not in amounts:
                amounts.append(value)
        if _NOT_TOTAL_RE.search(folded):
            continue
        for pattern, priority in _TOTAL_LABELS:
            match = pattern.search(folded)
            if not match:
                continue
            # Positions in the folded text may shift by umlauts; search the original after the label word.
            label_end = _original_offset(line.fixed, folded, match.end())
            after = [entry for entry in _bare_amounts(line.fixed) if entry[1] >= label_end]
            chosen: Decimal | None = None
            if after:
                chosen = after[-1][0] if len(after) >= 3 else after[0][0]
            elif not line_amounts and position + 1 < len(lines) and len(line.text.split()) <= 4:
                following = _bare_amounts(lines[position + 1].fixed)
                if following and lines[position + 1].page == line.page:
                    chosen = following[0][0]
            if chosen is not None:
                order += 1
                candidate = (priority, order, chosen)
                if best is None or (candidate[0], candidate[1]) > (best[0], best[1]):
                    best = candidate
            break
    total = format_amount(best[2]) if best else None
    if total is None and len(amounts) == 1:
        total = format_amount(amounts[0])
    if total is not None and best is not None and best[2] not in amounts:
        amounts.append(best[2])
    return [format_amount(value) for value in amounts[:30]], total


def _bare_amounts(text: str) -> list[tuple[Decimal, int]]:
    """All amounts of a line with or without currency: (value, start)."""
    return [(value, start) for value, start, _end, _cur in _amounts_in(text)]


def _original_offset(original: str, folded: str, folded_offset: int) -> int:
    """Map an offset in ``_fold(original)`` back to ``original`` (umlauts and ß expand by one char)."""
    if len(original) == len(folded):
        return folded_offset
    produced = 0
    for index, char in enumerate(original):
        if produced >= folded_offset:
            return index
        produced += len(_fold(char))
    return len(original)


# ---------------------------------------------------------------------------
# IBAN

_IBAN_RE = re.compile(r"(?<![A-Z0-9])([A-Z]{2}\d{2}(?:[ \u00a0]?[A-Z0-9]{4}){2,7}(?:[ \u00a0]?[A-Z0-9]{1,4})?)(?![A-Za-z0-9])")
IBAN_LENGTHS = {
    "AD": 24, "AT": 20, "BE": 16, "BG": 22, "CH": 21, "CY": 28, "CZ": 24, "DE": 22, "DK": 18, "EE": 20, "ES": 24,
    "FI": 18, "FR": 27, "GB": 22, "GR": 27, "HR": 21, "HU": 28, "IE": 22, "IS": 26, "IT": 27, "LI": 21, "LT": 20,
    "LU": 20, "LV": 21, "MC": 27, "MT": 31, "NL": 18, "NO": 15, "PL": 28, "PT": 25, "RO": 24, "SE": 24, "SI": 19,
    "SK": 24, "SM": 27,
}


def iban_valid(value: str) -> bool:
    compact = re.sub(r"\s+", "", value or "").upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", compact):
        return False
    expected = IBAN_LENGTHS.get(compact[:2])
    if expected is not None and len(compact) != expected:
        return False
    rearranged = compact[4:] + compact[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(digits) % 97 == 1


def format_iban(value: str) -> str:
    compact = re.sub(r"\s+", "", value).upper()
    return " ".join(compact[i:i + 4] for i in range(0, len(compact), 4))


def _find_ibans(doc: _Doc) -> list[str]:
    result: list[str] = []
    for line in doc.lines:
        for match in _IBAN_RE.finditer(line.fixed.upper()):
            compact = re.sub(r"[\s\u00a0]+", "", match.group(1))
            expected = IBAN_LENGTHS.get(compact[:2])
            candidates = [compact[:expected]] if expected and len(compact) >= expected else [compact]
            for candidate in candidates:
                if iban_valid(candidate) and candidate not in result:
                    result.append(candidate)
    return result


# ---------------------------------------------------------------------------
# References

REFERENCE_LABELS: tuple[tuple[str, str], ...] = (
    ("Kundennummer", r"kunden[\s-]?(?:nummer|nr\.?|-?nr\.?|id)|kundennr\.?|kd\.?[\s-]?nr\.?|kd\.-nr\.?|"
                     r"kunde\s+nr\.?|customer\s*(?:no\.?|number|id)|debitor(?:en)?[\s-]?(?:nummer|nr\.?)"),
    ("Vertragsnummer", r"vertrags[\s-]?(?:nummer|nr\.?|konto(?:nummer)?|kontonr\.?)|vertragskonto(?:nummer)?|"
                       r"vertrag\s+nr\.?|contract\s*(?:no\.?|number)|vk[\s-]?nr\.?"),
    ("Rechnungsnummer", r"rechnungs[\s-]?(?:nummer|nr\.?)|rechnungsnr\.?|rechnung\s+nr\.?|re[\s.-]?nr\.?|"
                        r"beleg[\s-]?(?:nummer|nr\.?)|invoice\s*(?:no\.?|number|#)|rg[\s.-]?nr\.?"),
    ("Versicherungsnummer", r"versicherungs(?:schein)?[\s-]?(?:nummer|nr\.?)|versicherungsscheinnummer|"
                            r"versicherten[\s-]?(?:nummer|nr\.?)|policen?[\s-]?(?:nummer|nr\.?)|"
                            r"vers\.?[\s-]?(?:nr|schein-?nr)\.?|vsnr\.?|schein[\s-]?nr\.?"),
    ("Mitgliedsnummer", r"mitglieds?[\s-]?(?:nummer|nr\.?)|mitgliedsnr\.?"),
    ("Steuer-ID", r"steuer[\s-]?id(?:entifikationsnummer)?|steuerliche\s+identifikationsnummer|"
                  r"identifikationsnummer|idnr\.?|id-nr\.?"),
    ("Steuernummer", r"steuer[\s-]?(?:nummer|nr\.?)|st\.?[\s-]?nr\.?|stnr\.?"),
    ("Personalnummer", r"personal[\s-]?(?:nummer|nr\.?)|pers\.?[\s-]?nr\.?|mitarbeiter[\s-]?(?:nummer|nr\.?)"),
    ("Zählernummer", r"z(?:ä|ae)hler[\s-]?(?:nummer|nr\.?)|z(?:ä|ae)hlernr\.?|z(?:ä|ae)hlpunkt(?:bezeichnung)?|"
                     r"marktlokations?[\s-]?(?:id|nummer)|melo(?:-id)?|malo(?:-id)?"),
    ("Bestellnummer", r"bestell[\s-]?(?:nummer|nr\.?)|bestellnr\.?|bestellung\s+nr\.?|order\s*(?:no\.?|number|id|#)"),
    ("Auftragsnummer", r"auftrags[\s-]?(?:nummer|nr\.?)|auftragsnr\.?|auftrag\s+nr\.?"),
    ("Aktenzeichen", r"aktenzeichen|az\.|gesch(?:ä|ae)ftszeichen|unser\s+zeichen|vorgangsnummer"),
    ("Kassenzeichen", r"kassenzeichen"),
    ("Beitragsnummer", r"beitrags[\s-]?(?:nummer|nr\.?)"),
    ("Mandatsreferenz", r"mandatsreferenz|mandats[\s-]?(?:nummer|nr\.?)"),
)
_REFERENCE_RES = tuple(
    (label, re.compile(rf"(?<![\w-])(?:{pattern})(?![\w])", re.IGNORECASE)) for label, pattern in REFERENCE_LABELS
)
_VALUE_START_RE = re.compile(r"^[\s:.#=]*(?:(?:nr|no|nummer)\.?[\s:.#]*)?")
_VALUE_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9/.\-_]*")
_DIGIT_GROUP_RE = re.compile(r"\d[\d/.\-]*")
# Numbers that mix short letter groups and digit groups ("3 C 123/25", "12 010285 M 123").
_FILE_NUMBER_LABELS = {"Aktenzeichen", "Kassenzeichen", "Versicherungsnummer"}
_FILE_NUMBER_TOKEN_RE = re.compile(r"(?:[A-Z]{1,3}|[A-Za-z]{0,3}\d[\w/.\-]*)(?![\wäöüß])")


def _reference_value(rest: str, label: str) -> str | None:
    rest = _VALUE_START_RE.sub("", rest, count=1)
    match = _VALUE_TOKEN_RE.match(rest)
    if not match:
        return None
    value = match.group(0)
    position = match.end()
    if label in _FILE_NUMBER_LABELS:
        # File numbers mix short letter groups and numbers: "3 C 123/25", "12 O 345/24".
        while position < len(rest) and rest[position] == " ":
            token = _FILE_NUMBER_TOKEN_RE.match(rest, position + 1)
            if not token:
                break
            value += " " + token.group(0)
            position = token.end()
    # Digit groups separated by single spaces belong together ("4711 0815", "202 123 45678").
    while position < len(rest) and rest[position] == " ":
        group = _DIGIT_GROUP_RE.match(rest, position + 1)
        if not group or not value[-1:].isdigit():
            break
        value += " " + group.group(0)
        position = group.end()
    value = value.rstrip(".-/_,")
    digits = sum(ch.isdigit() for ch in value)
    if len(value) < 3 or len(value) > 40 or digits < (1 if label == "Aktenzeichen" else 2):
        return None
    if _NUM_DATE_RE.fullmatch(value) or _ISO_DATE_RE.fullmatch(value):
        return None
    if re.fullmatch(r"\d{1,2}[.,]\d{2}", value):
        return None
    return value


def _find_references(doc: _Doc) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}

    def add(label: str, value: str) -> None:
        values = result.setdefault(label, [])
        if value not in values and len(values) < 5:
            values.append(value)

    lines = doc.lines
    for position, line in enumerate(lines):
        text = line.fixed
        matches: list[tuple[int, int, str]] = []
        for label, regex in _REFERENCE_RES:
            for match in regex.finditer(text):
                if any(match.start() < end and match.end() > start for start, end, _ in matches):
                    continue
                matches.append((match.start(), match.end(), label))
        if not matches:
            continue
        matches.sort()
        pending: list[str] = []
        for index, (start, end, label) in enumerate(matches):
            next_start = matches[index + 1][0] if index + 1 < len(matches) else len(text)
            value = _reference_value(text[end:next_start], label)
            if value:
                add(label, value)
            else:
                pending.append(label)
        if pending and len(pending) == len(matches) and position + 1 < len(lines):
            # Table layout: labels in one line, values in the next one. Only used when
            # the number of values matches exactly, otherwise columns cannot be paired.
            tokens = [t.strip() for t in re.split(r"\s{2,}|\t|\s(?=\S*\d)", lines[position + 1].fixed) if t.strip()]
            values = [
                t for t in tokens
                if any(ch.isdigit() for ch in t) and not _NUM_DATE_RE.fullmatch(t) and not _ISO_DATE_RE.fullmatch(t)
            ]
            if len(values) == len(pending):
                for label, raw in zip(pending, values):
                    value = _reference_value(raw, label)
                    if value:
                        add(label, value)
    return result


# ---------------------------------------------------------------------------
# E-mail addresses, keywords, document type


def _find_emails(doc: _Doc) -> list[str]:
    result: list[str] = []
    for line in doc.lines:
        for address in _EMAIL_RE.findall(line.text):
            lowered = address.lower().strip(".")
            if lowered not in result:
                result.append(lowered)
    return result[:20]


_KEYWORD_NOISE = {
    "telefon", "telefax", "email", "mail", "internet", "seite", "datum", "strasse", "gmbh", "herrn", "frau", "herr",
    "sehr", "geehrter", "geehrte", "freundlichen", "gruessen", "gruss", "postfach", "bankverbindung", "iban", "bic",
    "amtsgericht", "geschaeftsfuehrer", "geschaeftsfuehrung", "sitz", "idnr", "www", "http", "https", "html", "tel",
    "fax", "nummer", "euro", "eur", "nr", "ihre", "ihren", "ihrem", "ihr", "unsere", "unserer", "bitte", "vielen",
    "dank", "uns", "sowie", "bzw", "inkl", "zzgl", "ggf", "usw", "etc", "de", "com", "hrb", "hra", "ust", "steuernr",
    "registergericht", "vorstand", "aufsichtsrat", "vorsitzender", "blz", "konto", "kontonummer", "betrag", "summe",
    "gesamt", "monat", "jahr", "januar", "februar", "maerz", "april", "mai", "juni", "juli", "august", "september",
    "oktober", "november", "dezember", "montag", "dienstag", "mittwoch", "donnerstag", "freitag", "samstag",
    "sonntag", "uhr", "gilt", "erhalten", "finden", "koennen", "moechten", "gerne", "fragen", "erreichen",
    "anlage", "anlagen", "hinweis", "beachten", "page", "date", "von", "fuer", "mehr", "unter", "wurde", "wurden",
}


_GREETING_RE = re.compile(r"^(?:sehr geehrte|liebe[rs]?\b|hallo\b|guten tag\b|dear\b)", re.IGNORECASE)


def _find_keywords(doc: _Doc, limit: int = 12) -> list[str]:
    """Frequent content words, without the address blocks and mail headers."""
    kept = [
        line.norm
        for line in doc.lines
        if not line.recipient
        and not line.return_line
        and not _is_address_text(line.text)
        and not _MAIL_HEADER_RE.match(line.text)
        and not _MAIL_RECIPIENT_HEADER_RE.match(line.text)
        and not _MAIL_HEADER_NEIGHBOUR_RE.match(line.text)
        and not _GREETING_RE.match(line.text)
    ]
    text = " ".join(kept)[:MAX_KEYWORD_CHARS]
    # Names and places of the addressee are not what a document is about.
    personal = {word for line in doc.lines if line.recipient for word in line.norm.split()}
    counts: Counter[str] = Counter()
    first_seen: dict[str, int] = {}
    for position, word in enumerate(content_words(text, min_length=4)):
        if word in _KEYWORD_NOISE or word in personal or any(ch.isdigit() for ch in word) or len(word) > 30:
            continue
        counts[word] += 1
        first_seen.setdefault(word, position)
    ranked = sorted(counts, key=lambda w: (-counts[w], first_seen[w]))
    return ranked[:limit]


def _heading_text(doc: _Doc, context: Mapping[str, Any]) -> str:
    """Short lines near the top of page 1 plus the mail subject: where a type is named."""
    parts: list[str] = []
    for line in doc.first[:25]:
        if line.recipient:
            continue
        if len(line.text.split()) <= 8:
            parts.append(line.norm)
        elif re.match(r"^(?:betreff|betr\.|subject)\s*:", line.text, re.IGNORECASE):
            parts.append(line.norm)
    for key in ("mail_subject", "title"):
        value = context.get(key)
        if value:
            parts.append(normalize(str(value)))
    return " | ".join(parts)


def _detect_doc_type(doc: _Doc, context: Mapping[str, Any]) -> str | None:
    text = normalize("\n".join(doc.pages[:MAX_PAGES]))
    return doctypes.classify(text, _heading_text(doc, context))


# ---------------------------------------------------------------------------
# Entry point


def analyze(
    pages: Sequence[str],
    context: Mapping[str, Any] | None = None,
    *,
    known_senders: Iterable[str] | None = None,
    today: date | None = None,
) -> Analysis:
    """Extract facts from the page texts of one document. Never raises for odd input."""
    pages = [str(page or "") for page in (pages or [])]
    context = dict(context or {})
    known = [str(name) for name in (known_senders or []) if name]
    today = today or timeutil.today()
    doc = _Doc.build(pages)
    analysis = Analysis(text_chars=sum(len(page.strip()) for page in pages))
    if not doc.lines and not context:
        return analysis

    dates = _safe(lambda: _find_dates(doc, today), [], "dates")
    analysis.dates = dates[:10]
    if dates and dates[0].score > MIN_DATE_SCORE:
        analysis.doc_date = dates[0].value

    sender, candidates = _safe(lambda: _detect_sender(doc, context, known), (None, []), "sender")
    analysis.sender = sender
    analysis.sender_candidates = candidates

    amounts, total = _safe(lambda: _find_amounts(doc), ([], None), "amounts")
    analysis.amounts = amounts
    analysis.total_amount = total
    analysis.ibans = _safe(lambda: _find_ibans(doc), [], "ibans")
    analysis.references = _safe(lambda: _find_references(doc), {}, "references")
    analysis.emails = _safe(lambda: _find_emails(doc), [], "emails")
    analysis.doc_type = _safe(lambda: _detect_doc_type(doc, context), None, "doc_type")
    analysis.keywords = _safe(lambda: _find_keywords(doc), [], "keywords")
    return analysis


def letterhead_text(pages: Sequence[str]) -> str:
    """Normalized text of the letterhead and the return address line of page 1.

    That is where a letter names its author: the first lines of the page and the
    one-line sender address above the address window. The recipient's address
    block, bank details and fields that name someone else ("Versichert bei: AOK
    PLUS") are left out. Lines are separated by newlines.
    """
    doc = _Doc.build([str(page or "") for page in list(pages or [])[:1]])
    parts: list[str] = []
    for line in doc.first:
        if line.recipient or not (line.return_line or _is_letterhead(line, "")):
            continue
        if _MAIL_HEADER_RE.match(line.text) or _MAIL_RECIPIENT_HEADER_RE.match(line.text):
            continue
        if _is_bank_details(doc.first, line, letterhead=True):
            continue
        segments = _split_segments(line.text)
        kept = [segment for position, segment in enumerate(segments) if not _third_party_segment(segments, position)]
        text = normalize(" ".join(kept))
        if text:
            parts.append(text)
    return "\n".join(parts)


def month_name(month: int) -> str:
    """German month name (1 = Januar)."""
    return FULL_MONTH_NAMES[(month - 1) % 12]


__all__ = [
    "analyze",
    "entity_key",
    "format_amount",
    "format_iban",
    "iban_valid",
    "letterhead_text",
    "month_name",
    "name_from_domain",
    "name_from_mail",
    "parse_date_text",
    "short_sender",
    "MONTHS",
    "FULL_MONTH_NAMES",
    "FULL_MONTH_WORDS",
    "REFERENCE_LABELS",
]
