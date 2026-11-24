"""Text normalization for search.

Both the index and queries go through :func:`normalize`, so that "Müller",
"Mueller" and "MÜLLER" as well as "Straße" and "Strasse" become identical.
Words broken by a hyphen at a line end ("Neben-\\nkosten") are joined.

:func:`joined` is a second form of the same text in which digit groups
("123 456 7890", "DE89 3704 0044 …") and hyphenated words ("Kfz-Versicherung")
are written together, so that numbers and compounds can be found however
they were grouped.
"""

from __future__ import annotations

import bisect
import functools
import re
import unicodedata

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue"})
_COMBINING_RE = re.compile(r"[̀-ͯ]")
# A hyphen at a line end followed by a lowercase letter is a hyphenation break.
_HYPHEN_BREAK_RE = re.compile(r"(?<=\w)[-­‐‑][ \t]*\r?\n[ \t]*(?=[a-zäöüßàâçéèêëîïôûùÿñ])")
_SOFT_HYPHEN = "­"
_NON_WORD_RE = re.compile(r"[\W_]+")
_TOKEN_RE = re.compile(r"[^\W_]+")
# Separators that joined() drops: up to three spaces, dots, slashes or hyphens
# between two digits ("123 456", "31.12.2025", "0351 / 12 34 56"), and a single
# hyphen between two letters or digits ("Kfz-Versicherung", "456789-X"), also
# when the line breaks after it. Decimal commas, colons and plain spaces between
# words are kept, so amounts, times and ordinary word sequences stay apart.
# (Both patterns start with a consumed character rather than a lookbehind,
# which lets the regex engine skip most positions quickly.)
_DIGIT_GAP_RE = re.compile(r"(\d)[ \t\u00a0\u2007\u2009\u202f./\-\u2010\u2011]{1,3}(?=\d)")
_WORD_HYPHEN_RE = re.compile(r"[-\u00ad\u2010\u2011](?<=[^\W_].)(?:[ \t]*\r?\n[ \t]*)?(?=[^\W_])")


def _join_separators(source: str) -> str:
    return _WORD_HYPHEN_RE.sub("", _DIGIT_GAP_RE.sub(r"\1", source))


def _joinable_runs(source: str) -> list[tuple[int, int]]:
    """(start, end) in ``source`` of every separator run that :func:`joined` drops."""
    runs = [(m.end(1), m.end()) for m in _DIGIT_GAP_RE.finditer(source)]
    runs += [(m.start(), m.end()) for m in _WORD_HYPHEN_RE.finditer(source)]
    return runs


def normalize(text: str) -> str:
    """Lowercase, transliterate umlauts and ß, strip accents, keep only words and numbers."""
    if not text:
        return ""
    value = unicodedata.normalize("NFC", text)
    value = _HYPHEN_BREAK_RE.sub("", value)
    value = value.replace(_SOFT_HYPHEN, "")
    value = value.casefold().translate(_UMLAUTS)
    value = unicodedata.normalize("NFKD", value)
    value = _COMBINING_RE.sub("", value)
    value = _NON_WORD_RE.sub(" ", value)
    return value.strip()


@functools.lru_cache(maxsize=8192)
def _char_variants(char: str) -> str:
    # Called once per character of every mapped text; texts use few distinct characters.
    folded = char.casefold().translate(_UMLAUTS)
    decomposed = unicodedata.normalize("NFKD", folded)
    return _COMBINING_RE.sub("", decomposed)


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Like :func:`normalize`, plus the index into ``NFC(text)`` for every output character.

    Used to highlight matches in the original text. The input is NFC-normalized
    first; callers should pass NFC text (all stored texts are NFC).
    """
    source = unicodedata.normalize("NFC", text or "")
    joined_spans = [(m.start(), m.end()) for m in _HYPHEN_BREAK_RE.finditer(source)]
    skip: set[int] = set()
    for start, end in joined_spans:
        skip.update(range(start, end))
    out: list[str] = []
    offsets: list[int] = []
    last_space = True
    for index, char in enumerate(source):
        if index in skip or char == _SOFT_HYPHEN:
            continue
        for produced in _char_variants(char):
            if _NON_WORD_RE.fullmatch(produced):
                if not last_space:
                    out.append(" ")
                    offsets.append(index)
                    last_space = True
                continue
            out.append(produced)
            offsets.append(index)
            last_space = False
    while out and out[-1] == " ":
        out.pop()
        offsets.pop()
    return "".join(out), offsets


def joined(text: str) -> str:
    """Like :func:`normalize`, but with digit groups and hyphenated words written together.

    "IBAN DE89 3704 0044", "Kd.-Nr. 123/456789-X" and "Kfz-Versicherung" become
    "iban de8937040044", "kd nr 123456789x" and "kfzversicherung". The result
    is :func:`normalize` of the same text with some spaces removed.
    """
    if not text:
        return ""
    return normalize(_join_separators(unicodedata.normalize("NFC", text)))


def join_map(source: str, norm: str, offsets: list[int]) -> tuple[str, list[int]]:
    """The :func:`joined` form of ``source`` from ``(norm, offsets) = normalize_with_map(source)``.

    Returns the joined text and, for each of its characters, the index of the
    character of ``norm`` it comes from. ``source`` must be NFC.
    """
    drops: set[int] = set()
    for start, end in _joinable_runs(source):
        # A dropped separator run became at most one space in norm (none for a
        # hyphenation break), mapped to one of the run's characters.
        i = bisect.bisect_left(offsets, start)
        if i < len(norm) and offsets[i] < end and norm[i] == " ":
            drops.add(i)
    if not drops:
        return norm, list(range(len(norm)))
    drop = sorted(drops)
    pieces: list[str] = []
    keep: list[int] = []
    start = 0
    for i in drop:
        pieces.append(norm[start:i])
        keep.extend(range(start, i))
        start = i + 1
    pieces.append(norm[start:])
    keep.extend(range(start, len(norm)))
    return "".join(pieces), keep


def joined_with_map(text: str) -> tuple[str, list[int]]:
    """Like :func:`joined`, plus the index into ``NFC(text)`` for every output character."""
    source = unicodedata.normalize("NFC", text or "")
    norm, offsets = normalize_with_map(source)
    value, keep = join_map(source, norm, offsets)
    return value, [offsets[i] for i in keep]


def joined_fragments(text: str, norm: str | None = None) -> str:
    """The words of :func:`joined` that :func:`normalize` does not contain, separated by spaces.

    Every substring of the joined text that contains no space occurs either in
    the normalized text or in one of these fragments, so indexing them next to
    the normalized text is enough to search the joined form.
    """
    source = unicodedata.normalize("NFC", text or "")
    if not (_DIGIT_GAP_RE.search(source) or _WORD_HYPHEN_RE.search(source)):
        return ""
    value = normalize(_join_separators(source))
    words = set((normalize(source) if norm is None else norm).split())
    return " ".join(word for word in dict.fromkeys(value.split()) if word not in words)


def spellings(word: str, text: str) -> list[str]:
    """How the normalized single word ``word`` is written in ``text``, in order of appearance.

    ``spellings("ueberweisung", "Ihre Überweisung vom …")`` is ``["Überweisung"]``.
    Words broken by a hyphen at a line end are found too.
    """
    if not word or " " in word:
        return []
    source = unicodedata.normalize("NFC", text or "")
    source = _HYPHEN_BREAK_RE.sub("", source).replace(_SOFT_HYPHEN, "")
    found: list[str] = []
    first = word[0]
    for match in _TOKEN_RE.finditer(source):
        token = match.group()
        # A word never gets shorter when it is normalized.
        if len(token) > len(word) or not _char_variants(token[0]).startswith(first):
            continue
        if normalize(token) == word:
            found.append(token)
    return found


def tokenize(normalized: str) -> list[str]:
    return normalized.split()


STOPWORDS = frozenset(
    """
    aber alle allem allen aller alles als also am an ander andere anderem anderen anderer anderes auch auf aus bei bin bis
    bist da damit dann das dass dasselbe dazu dein deine deinem deinen deiner dem den denn der derer des dessen dich die dies
    diese dieselbe dieselben diesem diesen dieser dieses dir doch dort du durch ein eine einem einen einer eines einig einige
    einigem einigen einiger einiges einmal er es etwas euch euer eure eurem euren eurer eures fuer gegen gewesen hab habe haben
    hat hatte hatten hier hin hinter ich ihm ihn ihnen ihr ihre ihrem ihren ihrer ihres im in indem ins ist jede jedem jeden
    jeder jedes jene jenem jenen jener jenes jetzt kann kein keine keinem keinen keiner keines koennen koennte machen man manche
    manchem manchen mancher manches mein meine meinem meinen meiner meines mit muss musste nach nicht nichts noch nun nur ob oder
    ohne sehr sein seine seinem seinen seiner seines selbst sich sie sind so solche solchem solchen solcher solches soll sollte
    sondern sonst ueber um und uns unser unsere unserem unseren unserer unseres unter viel vom von vor waehrend war waren warst
    was weg weil weiter welche welchem welchen welcher welches wenn werde werden wie wieder will wir wird wirst wo wollen wollte
    wuerde wuerden zu zum zur zwar zwischen sehr geehrte geehrter damen herren freundlichen gruessen mit bitte seite
    a an and are as at be by for from has have in is it its of on or that the this to was were will with your you our we
    """.split()
)


def content_words(normalized: str, *, min_length: int = 3) -> list[str]:
    """Words useful for similarity: no stopwords, no pure numbers, minimum length."""
    return [
        word
        for word in normalized.split()
        if len(word) >= min_length and word not in STOPWORDS and not word.isdigit()
    ]


def bounded_levenshtein(a: str, b: str, limit: int) -> int:
    """Edit distance between ``a`` and ``b``, or ``limit + 1`` once it exceeds ``limit``."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    if len(a) > len(b):
        a, b = b, a
    previous = list(range(len(a) + 1))
    for i, char_b in enumerate(b, start=1):
        current = [i] + [0] * len(a)
        row_min = current[0]
        for j, char_a in enumerate(a, start=1):
            cost = 0 if char_a == char_b else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            row_min = min(row_min, current[j])
        if row_min > limit:
            return limit + 1
        previous = current
    return previous[-1] if previous[-1] <= limit else limit + 1
