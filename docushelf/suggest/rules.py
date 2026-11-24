"""Deterministic filing rules from ``config/rules.yaml``.

The file is meant to be edited by hand (or through the rules page)::

    rules:
      - name: Stadtwerke Strom
        match:            # all given conditions must hold; lists inside a condition are OR
          sender: [Stadtwerke Dresden, DREWAG]
          keywords_any: [Abschlag]
        action:
          folder: Wohnung/Strom
          title: "{sender} {type} {date:%m/%Y}"
          tags: ["steuerrelevant {year}"]
        auto: false

A ``sender`` condition looks at the detected sender. Only when no sender was
detected does it fall back to the letterhead of page 1 (first lines and return
address line, without bank details and fields such as "Versichert bei: …");
such a match is weaker (:attr:`RuleHit.page1_only`). Every match carries the
reasons why it matched. ``auto: true`` marks a rule for automatic filing:
when automatic filing is switched on (``DOCUSHELF_AUTO_FILE``), matching
documents are filed without confirmation (see :mod:`docushelf.suggest.auto`);
the suggestions of such a rule carry ``rule_auto``.

Structured changes (``add``, ``replace``, ``delete``) keep the file readable:
``add`` appends the new rule to the text, so comments stay; the others write
the file anew and first keep a copy with the user's comments in
``rules.yaml.bak``.
"""

from __future__ import annotations

import copy
import fnmatch
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import date
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from ..analysis import doctypes
from ..analysis.extract import FULL_MONTH_NAMES, FULL_MONTH_WORDS, MONTHS, iban_valid, letterhead_text
from ..errors import ConfigError, NotFound, PathError
from ..models import Analysis, FilingRequest
from ..util import fsops
from ..util.names import sanitize_folder, title_from_original_name
from ..util.textnorm import normalize
from ..util.timeutil import parse_date

log = logging.getLogger(__name__)

MATCH_KEYS = ("sender", "keywords_any", "keywords_all", "iban", "reference", "source", "mail_from", "doc_type")
PLACEHOLDERS = ("sender", "type", "date", "year", "month", "month_name", "subject", "original")
RULE_KEYS = ("name", "match", "action", "auto")
ACTION_KEYS = ("folder", "title", "tags")
# How much text a rule looks at (keywords, references).
MAX_TEXT_CHARS = 300_000
MAX_FILE_BYTES = 1_000_000

TEMPLATE = """\
# Ablageregeln für DocuShelf
#
# Jede Regel prüft neu eingegangene Dokumente. Treffen alle angegebenen
# Bedingungen zu, wird der Zielordner als Vorschlag angezeigt – mit der Regel
# als Begründung. Innerhalb einer Bedingung genügt ein Eintrag der Liste.
# Groß-/Kleinschreibung und Umlaute (ä/ae) spielen keine Rolle.
#
# Bedingungen (alle optional, mindestens eine angeben):
#   sender:        erkannter Absender enthält den Text (ist keiner erkannt:
#                  der Text steht im Briefkopf auf Seite 1)
#   keywords_any:  mindestens eines der Stichwörter steht im Text
#                  (Teilwörter zählen: „Abschlag“ trifft auch „Abschlagsrechnung“)
#   keywords_all:  alle Stichwörter stehen im Text
#   iban:          eine der IBANs steht im Dokument
#   reference:     Kunden-, Vertrags- oder andere Nummer steht im Text
#   source:        Eingangskanal: upload, smb, ftp, mail, smtp, watch-… (auch watch-*)
#   mail_from:     Absenderadresse der Mail, z. B. rechnung@example.com oder @example.com
#   doc_type:      erkannter Dokumenttyp, z. B. Rechnung, Kontoauszug, Gehaltsabrechnung
#
# Aktion:
#   folder:  Zielordner im Archiv, z. B. Wohnung/Strom
#   title:   Titelvorlage (optional), z. B. "{sender} {type} {date:%m/%Y}"
#            Platzhalter: {sender} {type} {date} {date:%Y-%m} {year} {month}
#                         {month_name} {subject} (Mail-Betreff) {original} (Dateiname)
#   tags:    Schlagwörter (optional), Platzhalter wie beim Titel
#
# auto: true legt passende Dokumente ohne Rückfrage ab, sobald die automatische
# Ablage eingeschaltet ist (DOCUSHELF_AUTO_FILE, siehe Einstellungen). Jede
# automatische Ablage steht unter Eingang → Automatisch abgelegt.
#
# Beispiel – zum Aktivieren die Rauten (#) am Zeilenanfang entfernen und
# „rules: []“ unten löschen:
#
# rules:
#   - name: Stadtwerke Strom
#     match:
#       sender: [Stadtwerke Dresden, DREWAG]
#       keywords_any: [Abschlag]
#     action:
#       folder: Wohnung/Strom
#       title: "{sender} {type} {date:%m/%Y}"
#       tags: ["steuerrelevant {year}"]
#     auto: false

rules: []
"""

_HEADER = """\
# Ablageregeln für DocuShelf – Aufbau und Platzhalter siehe Beispiel auf der
# Regel-Seite. Alle Bedingungen einer Regel müssen zutreffen; innerhalb einer
# Bedingung genügt ein Eintrag der Liste.

"""


# ---------------------------------------------------------------------------
# Data types


@dataclass
class RuleMatch:
    """Conditions of a rule. Empty lists are ignored; all non-empty ones must hold."""

    sender: list[str] = field(default_factory=list)
    keywords_any: list[str] = field(default_factory=list)
    keywords_all: list[str] = field(default_factory=list)
    iban: list[str] = field(default_factory=list)
    reference: list[str] = field(default_factory=list)
    source: list[str] = field(default_factory=list)
    mail_from: list[str] = field(default_factory=list)
    doc_type: list[str] = field(default_factory=list)

    def conditions(self) -> dict[str, list[str]]:
        return {key: list(getattr(self, key)) for key in MATCH_KEYS if getattr(self, key)}

    def is_empty(self) -> bool:
        return not self.conditions()

    def to_dict(self) -> dict[str, list[str]]:
        return self.conditions()

    def key(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """Normalized conditions, used to recognise a rule that already exists."""
        return tuple(
            (name, tuple(sorted(normalize(value) for value in values))) for name, values in self.conditions().items()
        )


@dataclass
class RuleAction:
    folder: str
    title: str | None = None
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"folder": self.folder}
        if self.title:
            data["title"] = self.title
        if self.tags:
            data["tags"] = list(self.tags)
        return data


@dataclass
class Rule:
    name: str
    match: RuleMatch
    action: RuleAction
    auto: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "match": self.match.to_dict(), "action": self.action.to_dict(), "auto": self.auto}

    @classmethod
    def from_dict(cls, data: Any, *, position: int | None = None) -> "Rule":
        """Build a rule from parsed YAML/JSON and validate it (raises ConfigError in German)."""
        return _parse_rule(data, position or 1)


@dataclass
class RuleHit:
    """A rule whose conditions hold for a document, with the German reasons.

    ``page1_only``: no sender was detected and the rule's sender was only found
    in the letterhead of page 1 – a weaker match than the detected sender.
    """

    rule: Rule
    reasons: list[str]
    page1_only: bool = False


# ---------------------------------------------------------------------------
# YAML reading and writing


class _Loader(yaml.SafeLoader):
    """Safe loader that keeps numbers and dates as text ("0815" must stay "0815")."""


_Loader.yaml_implicit_resolvers = {
    first: [
        (tag, regexp)
        for tag, regexp in resolvers
        if tag not in ("tag:yaml.org,2002:int", "tag:yaml.org,2002:float", "tag:yaml.org,2002:timestamp")
    ]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


class _Dumper(yaml.SafeDumper):
    """Indents lists below their key and writes short string lists in flow style."""

    def increase_indent(self, flow: bool = False, indentless: bool = False):  # noqa: D401 - PyYAML API
        return super().increase_indent(flow, False)


def _represent_list(dumper: yaml.SafeDumper, data: list[Any]) -> yaml.Node:
    flow = bool(data) and all(isinstance(value, str) for value in data) and sum(len(v) for v in data) < 70
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=flow)


_Dumper.add_representer(list, _represent_list)


def _yaml_error_message(exc: yaml.YAMLError) -> str:
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None) or str(exc)
    if mark is not None:
        return f"rules.yaml ist kein gültiges YAML (Zeile {mark.line + 1}, Spalte {mark.column + 1}): {problem}"
    return f"rules.yaml ist kein gültiges YAML: {problem}"


def _where(position: int, name: str | None) -> str:
    return f"Regel {position} („{name}“)" if name else f"Regel {position}"


def _string_list(value: Any, where: str, key: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        raise ConfigError(f"{where}: „{key}“ muss ein Text oder eine Liste von Texten sein.")
    result: list[str] = []
    for item in values:
        if item is None or isinstance(item, (bool, dict, list)):
            raise ConfigError(f"{where}: „{key}“ enthält einen ungültigen Eintrag ({item!r}).")
        text = str(item).strip()
        if text:
            result.append(text)
    return result


_PLACEHOLDER_RE = re.compile(r"\{\{|\}\}|\{([^{}:]*)(?::([^{}]*))?\}")


def _check_template(template: str, where: str, key: str) -> None:
    for match in _PLACEHOLDER_RE.finditer(template):
        name = match.group(1)
        if name is None:
            continue
        if name not in PLACEHOLDERS:
            allowed = " ".join("{" + p + "}" for p in PLACEHOLDERS)
            raise ConfigError(f"{where}: unbekannter Platzhalter {{{name}}} in „{key}“. Erlaubt: {allowed}")
        if match.group(2) is not None and name != "date":
            raise ConfigError(f"{where}: nur {{date:…}} darf ein Format haben („{key}“).")
    stripped = _PLACEHOLDER_RE.sub("", template)
    if "{" in stripped or "}" in stripped:
        raise ConfigError(f"{where}: geschweifte Klammer ohne Gegenstück in „{key}“ (für ein Zeichen „{{{{“ schreiben).")


def _parse_rule(data: Any, position: int) -> Rule:
    if not isinstance(data, dict):
        raise ConfigError(f"Regel {position}: muss aus „name“, „match“ und „action“ bestehen.")
    raw_name = data.get("name")
    name = str(raw_name).strip() if raw_name is not None and not isinstance(raw_name, (dict, list)) else ""
    where = _where(position, name or None)
    unknown = [str(key) for key in data if key not in RULE_KEYS]
    if unknown:
        raise ConfigError(f"{where}: unbekannter Eintrag „{unknown[0]}“. Erlaubt: {', '.join(RULE_KEYS)}.")
    if not name:
        raise ConfigError(f"{where}: „name“ fehlt.")

    match_data = data.get("match")
    if match_data is None or match_data == {}:
        raise ConfigError(f"{where}: „match“ fehlt – mindestens eine Bedingung angeben.")
    if not isinstance(match_data, dict):
        raise ConfigError(f"{where}: „match“ muss Bedingungen enthalten (z. B. sender: …).")
    unknown = [str(key) for key in match_data if key not in MATCH_KEYS]
    if unknown:
        raise ConfigError(f"{where}: unbekannte Bedingung „{unknown[0]}“. Erlaubt: {', '.join(MATCH_KEYS)}.")
    values = {key: _string_list(match_data.get(key), where, f"match.{key}") for key in MATCH_KEYS}
    values["iban"] = [re.sub(r"\s+", "", value).upper() for value in values["iban"]]
    for value in values["iban"]:
        if not re.fullmatch(r"[A-Z0-9]{8,34}", value):
            raise ConfigError(f"{where}: „{value}“ ist keine IBAN.")
    values["mail_from"] = [value.lower() for value in values["mail_from"]]
    for value in values["mail_from"]:
        if "@" not in value:
            raise ConfigError(f"{where}: „{value}“ ist keine Mail-Adresse (oder @domain).")
    values["source"] = [value.lower() for value in values["source"]]
    canonical_types = []
    for value in values["doc_type"]:
        canonical = doctypes.canonical_name(value)
        if canonical is None:
            raise ConfigError(
                f"{where}: unbekannter Dokumenttyp „{value}“. Bekannt: {', '.join(doctypes.NAMES)}."
            )
        canonical_types.append(canonical)
    values["doc_type"] = canonical_types
    match = RuleMatch(**values)
    if match.is_empty():
        raise ConfigError(f"{where}: „match“ enthält keine Bedingung – mindestens eine angeben.")

    action_data = data.get("action")
    if not isinstance(action_data, dict):
        raise ConfigError(f"{where}: „action“ mit „folder“ fehlt.")
    unknown = [str(key) for key in action_data if key not in ACTION_KEYS]
    if unknown:
        raise ConfigError(f"{where}: unbekannter Eintrag „action.{unknown[0]}“. Erlaubt: {', '.join(ACTION_KEYS)}.")
    raw_folder = action_data.get("folder")
    if raw_folder is None or isinstance(raw_folder, (dict, list, bool)) or not str(raw_folder).strip():
        raise ConfigError(f"{where}: „action.folder“ fehlt.")
    try:
        folder = sanitize_folder(str(raw_folder))
    except PathError as exc:
        raise ConfigError(f"{where}: Ordner „{raw_folder}“ ist ungültig ({exc}).") from exc
    if not folder:
        raise ConfigError(f"{where}: „action.folder“ darf nicht das Archiv selbst sein.")
    raw_title = action_data.get("title")
    title: str | None = None
    if raw_title is not None:
        if isinstance(raw_title, (dict, list, bool)):
            raise ConfigError(f"{where}: „action.title“ muss ein Text sein.")
        title = str(raw_title).strip() or None
        if title:
            _check_template(title, where, "action.title")
    tags = _string_list(action_data.get("tags"), where, "action.tags")
    if isinstance(action_data.get("tags"), str):
        tags = [tag.strip() for tag in str(action_data["tags"]).split(",") if tag.strip()]
    for tag in tags:
        _check_template(tag, where, "action.tags")

    auto = data.get("auto", False)
    if auto is None:
        auto = False
    if not isinstance(auto, bool):
        raise ConfigError(f"{where}: „auto“ muss true oder false sein.")
    return Rule(name=name, match=match, action=RuleAction(folder=folder, title=title, tags=tags), auto=auto)


def parse_rules(text: str) -> list[Rule]:
    """Parse and validate the content of rules.yaml. Raises ConfigError with a German message."""
    if not text or not text.strip():
        return []
    try:
        data = yaml.load(text, Loader=_Loader)  # noqa: S506 - safe loader subclass
    except yaml.YAMLError as exc:
        raise ConfigError(_yaml_error_message(exc)) from exc
    if data is None:
        return []
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        unknown = [str(key) for key in data if key != "rules"]
        if unknown:
            raise ConfigError(f"rules.yaml: unbekannter Abschnitt „{unknown[0]}“ – erwartet wird „rules:“.")
        entries = data.get("rules")
        if entries is None:
            return []
        if not isinstance(entries, list):
            raise ConfigError("rules.yaml: „rules“ muss eine Liste von Regeln sein (Einträge mit „- name: …“).")
    else:
        raise ConfigError("rules.yaml muss mit „rules:“ beginnen und eine Liste von Regeln enthalten.")
    return [_parse_rule(entry, position) for position, entry in enumerate(entries, start=1)]


def _dump_body(rules: Sequence[Rule]) -> str:
    return yaml.dump(
        {"rules": [rule.to_dict() for rule in rules]},
        Dumper=_Dumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=100,
    )


def dump_rules(rules: Sequence[Rule]) -> str:
    return _HEADER + _dump_body(rules)


# Comment lines DocuShelf writes itself (header of a saved file, template of a new one,
# also as worded in earlier versions).
_GENERIC_COMMENTS = frozenset(
    line.strip() for line in (TEMPLATE + _HEADER).splitlines() if line.strip().startswith("#")
) | {
    "#   sender:        Absender enthält den Text (oder er steht auf Seite 1)",
    "# auto: false ist für die spätere automatische Ablage reserviert.",
}
_QUOTED_RE = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'(?:[^']|'')*'")
_INLINE_COMMENT_RE = re.compile(r"(?:^|\s)#")
_EMPTY_RULES_RE = re.compile(r"^rules:[ \t]*(?:\[[ \t]*\])?[ \t]*(#.*)?$", re.MULTILINE)
_LIST_ITEM_RE = re.compile(r"^( *)- ", re.MULTILINE)


def _has_own_comments(text: str) -> bool:
    """True if ``text`` contains comments a user wrote (not only the generic ones)."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if stripped not in _GENERIC_COMMENTS:
                return True
        elif "#" in line and _INLINE_COMMENT_RE.search(_QUOTED_RE.sub("", line)):
            return True
    return False


def _append_rule_text(text: str, rule: Rule, expected: Sequence[Rule]) -> str | None:
    """``text`` with ``rule`` added at the end of its rule list and everything else kept.

    The result must read back as exactly ``expected``; None for layouts where
    plain appending does not work (e.g. a flow-style list).
    """
    items = _dump_body([rule]).splitlines()[1:]  # below "rules:", indented by two spaces
    base = text if not text or text.endswith("\n") else text + "\n"
    candidates: list[str] = []
    empty = _EMPTY_RULES_RE.search(base)
    if empty and not _LIST_ITEM_RE.search(base, empty.end()):
        # "rules: []" (the template) or a bare "rules:": the list starts right there.
        comment = f"  {empty.group(1)}" if empty.group(1) else ""
        candidates.append(base[: empty.start()] + "rules:" + comment + "\n" + "\n".join(items) + base[empty.end():])
    first_item = _LIST_ITEM_RE.search(base)
    indents = [len(first_item.group(1))] if first_item else []
    for indent in dict.fromkeys([*indents, 2, 0]):
        block = "\n".join(" " * indent + line[2:] for line in items)
        candidates.append(base + block + "\n")
    for candidate in candidates:
        if len(candidate.encode("utf-8")) > MAX_FILE_BYTES:
            return None
        try:
            if parse_rules(candidate) == list(expected):
                return candidate
        except ConfigError:
            continue
    return None


# ---------------------------------------------------------------------------
# Matching


class _Facts:
    """What rules look at, computed lazily once per document."""

    def __init__(self, analysis: Analysis, pages: Sequence[str], context: Mapping[str, Any], source: str | None):
        self.analysis = analysis
        self.pages = [str(page or "") for page in pages]
        self.context = context
        self.source = (source or "").lower()
        self._cache: dict[str, Any] = {}

    def _get(self, key: str, compute):
        if key not in self._cache:
            self._cache[key] = compute()
        return self._cache[key]

    @property
    def text_norm(self) -> str:
        return self._get("text", lambda: normalize("\n".join(self.pages)[:MAX_TEXT_CHARS]))

    @property
    def letterhead_norm(self) -> str:
        return self._get("letterhead", lambda: letterhead_text(self.pages[:1]))

    @property
    def compact_upper(self) -> str:
        return self._get("compact", lambda: re.sub(r"\s+", "", "\n".join(self.pages)[:MAX_TEXT_CHARS]).upper())

    @property
    def mail_addresses(self) -> list[str]:
        def compute() -> list[str]:
            result = []
            for key in ("mail_from", "forwarded_from"):
                address = parseaddr(str(self.context.get(key) or ""))[1].lower()
                if address:
                    result.append(address)
            return result

        return self._get("mail", compute)


def _word_start_in(haystack: str, needle: str) -> bool:
    """``needle`` occurs in ``haystack`` starting at a word boundary (both normalized)."""
    if not needle:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(needle), haystack) is not None


def _reference_pattern(value: str) -> re.Pattern[str] | None:
    chars = normalize(value).replace(" ", "")
    if not chars:
        return None
    # Separators inside a number vary ("123 456 789", "123-456-789", "123456789").
    body = " ?".join(re.escape(ch) for ch in chars)
    return re.compile(r"(?<![a-z0-9])" + body + r"(?![a-z0-9])")


def _mail_matches(address: str, pattern: str) -> bool:
    if pattern.startswith("@"):
        domain = address.rsplit("@", 1)[-1]
        wanted = pattern[1:]
        return domain == wanted or domain.endswith("." + wanted)
    return address == pattern


def _pretty_iban(value: str) -> str:
    return " ".join(value[i:i + 4] for i in range(0, len(value), 4))


def _evaluate(match: RuleMatch, facts: _Facts) -> tuple[list[str], bool] | None:
    """German reasons if every condition holds, else None; plus whether the sender
    condition only held through the letterhead (no sender was detected)."""
    reasons: list[str] = []
    analysis = facts.analysis
    page1_only = False

    if match.sender:
        sender_norm = normalize(analysis.sender or "")
        reason = None
        for value in match.sender:
            needle = normalize(value)
            if sender_norm:
                # A detected sender decides: a bank, insurer or shop that is merely
                # mentioned on page 1 must not pull the document into its folder.
                if _word_start_in(sender_norm, needle):
                    reason = f"Absender: {analysis.sender}"
                    break
            elif _word_start_in(facts.letterhead_norm, needle):
                reason = f"„{value}“ im Briefkopf"
                page1_only = True
                break
        if reason is None:
            return None
        reasons.append(reason)

    if match.keywords_any:
        hit = next(
            (value for value in match.keywords_any if normalize(value) and normalize(value) in facts.text_norm),
            None,
        )
        if hit is None:
            return None
        reasons.append(f"Stichwort „{hit}“")

    if match.keywords_all:
        if not all(normalize(value) and normalize(value) in facts.text_norm for value in match.keywords_all):
            return None
        reasons.append("Stichwörter " + ", ".join(f"„{value}“" for value in match.keywords_all))

    if match.iban:
        found = {value.upper() for value in analysis.ibans}
        hit = next((value for value in match.iban if value in found or value in facts.compact_upper), None)
        if hit is None:
            return None
        reasons.append(f"IBAN {_pretty_iban(hit)}")

    if match.reference:
        hit = None
        for value in match.reference:
            pattern = _reference_pattern(value)
            if pattern is not None and pattern.search(facts.text_norm):
                hit = value
                break
        if hit is None:
            return None
        reasons.append(f"Nummer „{hit}“")

    if match.source:
        if not facts.source or not any(fnmatch.fnmatchcase(facts.source, value) for value in match.source):
            return None
        reasons.append(f"Eingang über {facts.source}")

    if match.mail_from:
        hit = next(
            (address for address in facts.mail_addresses for value in match.mail_from if _mail_matches(address, value)),
            None,
        )
        if hit is None:
            return None
        reasons.append(f"Mail von {hit}")

    if match.doc_type:
        current = normalize(analysis.doc_type or "")
        if not current or current not in {normalize(value) for value in match.doc_type}:
            return None
        reasons.append(f"Dokumenttyp: {analysis.doc_type}")

    return reasons, page1_only


# ---------------------------------------------------------------------------
# The store


_MISSING = ("missing",)


class RuleStore:
    """Reads and writes ``rules.yaml``; caches the parsed rules by file signature.

    If the file becomes invalid (e.g. a typo while editing it by hand), the last
    valid rules stay active and :attr:`error` holds the German error message.
    Structured edits (``add``/``replace``/``delete``) are refused while the file
    is invalid, so a broken hand edit is never silently overwritten.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._rules: list[Rule] = []
        self._signature: tuple[Any, ...] | None = None
        self.error: str | None = None
        # Set by the last structured save that had to drop the user's comments.
        self.last_backup: Path | None = None

    @property
    def backup_path(self) -> Path:
        """Where the text with the user's comments is kept before a structured save."""
        return self.path.with_name(self.path.name + ".bak")

    # -- reading -------------------------------------------------------------
    def _stat(self) -> tuple[Any, ...]:
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return _MISSING
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def _refresh(self) -> list[Rule]:
        """Current rules (the internal list; callers must not modify it)."""
        with self._lock:
            signature = self._stat()
            if signature == self._signature:
                return self._rules
            if signature == _MISSING:
                self._rules, self.error = [], None
                self._signature = signature
                return self._rules
            try:
                text = self.path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                message = f"rules.yaml kann nicht gelesen werden: {exc}"
                if self.error != message:
                    log.warning("cannot read %s: %s", self.path, exc)
                # Not remembered as read: the next call tries again.
                self.error = message
                return self._rules
            try:
                rules = parse_rules(text)
            except ConfigError as exc:
                if self.error != str(exc):
                    log.warning("invalid rules file %s, keeping previous rules: %s", self.path, exc)
                self.error = str(exc)
                self._signature = signature
                return self._rules
            self._rules, self.error, self._signature = rules, None, signature
            return self._rules

    def load(self) -> list[Rule]:
        """The active rules (copies; changing them has no effect on the store)."""
        return copy.deepcopy(self._refresh())

    def raw_text(self) -> str:
        """The file as written by the user, or a commented template if there is none yet."""
        try:
            return self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return TEMPLATE

    # -- writing -------------------------------------------------------------
    def save_raw_text(self, text: str) -> list[Rule]:
        """Validate and store text from the editor. Raises ConfigError without touching the file."""
        text = (text or "").replace("\r\n", "\n")
        if len(text.encode("utf-8")) > MAX_FILE_BYTES:
            raise ConfigError("Die Regeldatei ist zu groß (höchstens 1 MB).")
        rules = parse_rules(text)
        if text and not text.endswith("\n"):
            text += "\n"
        with self._lock:
            fsops.atomic_write_text(self.path, text)
            self._rules, self.error, self._signature = rules, None, self._stat()
        return copy.deepcopy(rules)

    def save(self, rules: Sequence[Rule]) -> None:
        """Replace all rules (validated). The file is written anew, so comments are lost;
        if it had comments of the user's own, its previous text is kept in ``rules.yaml.bak``
        (:attr:`last_backup`)."""
        validated = [_parse_rule(rule.to_dict(), position) for position, rule in enumerate(rules, start=1)]
        text = dump_rules(validated)
        with self._lock:
            self._keep_comments()
            fsops.atomic_write_text(self.path, text)
            self._rules, self.error, self._signature = validated, None, self._stat()

    def _keep_comments(self) -> None:
        """Copy the current file to ``rules.yaml.bak`` if it has comments a user wrote."""
        self.last_backup = None
        try:
            current = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return
        if _has_own_comments(current):
            fsops.atomic_write_text(self.backup_path, current)
            self.last_backup = self.backup_path
            log.info("rules.yaml is rewritten; its comments were kept in %s", self.backup_path)

    def _editable(self) -> list[Rule]:
        rules = self._refresh()
        if self.error:
            raise ConfigError(f"Die Regeldatei ist fehlerhaft und muss zuerst korrigiert werden: {self.error}")
        return copy.deepcopy(rules)

    def add(self, rule: Rule) -> int:
        """Append a rule and return its index. A rule with exactly the same conditions
        is updated instead, so remembering the same decision twice creates no duplicate.

        A new rule is appended to the text of the file, so comments and the layout
        of the existing rules stay as they are.
        """
        with self._lock:
            rules = self._editable()
            key = rule.match.key()
            for index, existing in enumerate(rules):
                if existing.match.key() == key:
                    rules[index] = rule
                    self.save(rules)
                    return index
            added = _parse_rule(rule.to_dict(), len(rules) + 1)
            expected = [*rules, added]
            if not self._append_text(added, expected):
                self.save(expected)
            return len(rules)

    def _append_text(self, rule: Rule, expected: list[Rule]) -> bool:
        """Append ``rule`` to the file's text; False if that cannot be done safely."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        appended = _append_rule_text(text, rule, expected)
        if appended is None:
            return False
        self.last_backup = None
        fsops.atomic_write_text(self.path, appended)
        self._rules, self.error, self._signature = expected, None, self._stat()
        return True

    def replace(self, index: int, rule: Rule) -> None:
        with self._lock:
            rules = self._editable()
            if not 0 <= index < len(rules):
                raise NotFound("Regel nicht gefunden")
            rules[index] = rule
            self.save(rules)

    def delete(self, index: int) -> None:
        with self._lock:
            rules = self._editable()
            if not 0 <= index < len(rules):
                raise NotFound("Regel nicht gefunden")
            del rules[index]
            self.save(rules)

    # -- matching ------------------------------------------------------------
    def matches(
        self,
        analysis: Analysis | Mapping[str, Any] | None,
        pages: Sequence[str],
        context: Mapping[str, Any] | None,
        source: str | None,
    ) -> list[RuleHit]:
        """Rules whose conditions all hold, in file order, with German reasons and
        whether the sender was only found in the letterhead.

        The first reason names the rule ("Regel „Stadtwerke Strom“").
        """
        rules = self._refresh()
        if not rules:
            return []
        if not isinstance(analysis, Analysis):
            analysis = Analysis.from_dict(dict(analysis or {}))
        facts = _Facts(analysis, pages or [], context or {}, source)
        result: list[RuleHit] = []
        for rule in rules:
            try:
                evaluated = _evaluate(rule.match, facts)
            except Exception:  # pragma: no cover - defensive
                log.exception("rule %r failed", rule.name)
                continue
            if evaluated is not None:
                reasons, page1_only = evaluated
                result.append(RuleHit(copy.deepcopy(rule), [f"Regel „{rule.name}“", *reasons], page1_only))
        return result

    def match(
        self,
        analysis: Analysis | Mapping[str, Any] | None,
        pages: Sequence[str],
        context: Mapping[str, Any] | None,
        source: str | None,
    ) -> list[tuple[Rule, list[str]]]:
        """Rules whose conditions all hold, in file order, each with German reasons."""
        return [(hit.rule, hit.reasons) for hit in self.matches(analysis, pages, context, source)]


# ---------------------------------------------------------------------------
# Titles


_SUBJECT_PREFIX_RE = re.compile(r"^\s*(?:(?:re|aw|wg|fw|fwd|wtr|antw)\s*(?:\[\d+\])?\s*:\s*)+", re.IGNORECASE)
_SEPARATORS = "-–—|/,;:_·"
_DANGLING_RE = re.compile(rf"^[\s{re.escape(_SEPARATORS)}.]+|[\s{re.escape(_SEPARATORS)}]+$")
_DOUBLE_SEPARATOR_RE = re.compile(rf"\s*([{re.escape(_SEPARATORS)}])(?:\s*[{re.escape(_SEPARATORS)}])+\s*")
_EMPTY_BRACKETS_RE = re.compile(r"\(\s*\)|\[\s*\]|„\s*“|\"\s*\"")


def clean_subject(subject: str | None) -> str:
    """Mail subject without "AW:"/"WG:"/"Fwd:" prefixes."""
    return re.sub(r"\s+", " ", _SUBJECT_PREFIX_RE.sub("", subject or "")).strip()


def _format_date(value: date, fmt: str | None) -> str:
    """strftime-like formatting with German month names, independent of the process locale."""
    month = FULL_MONTH_NAMES[value.month - 1]
    codes = {
        "d": f"{value.day:02d}",
        "e": str(value.day),
        "m": f"{value.month:02d}",
        "Y": f"{value.year:04d}",
        "y": f"{value.year % 100:02d}",
        "B": month,
        "b": month[:3],
        "%": "%",
    }
    return re.sub(r"%(.)", lambda m: codes.get(m.group(1), m.group(0)), fmt or "%d.%m.%Y")


def _collapse(text: str) -> str:
    value = re.sub(r"\s+", " ", text)
    value = _EMPTY_BRACKETS_RE.sub("", value)
    value = _DOUBLE_SEPARATOR_RE.sub(lambda m: f" {m.group(1)} " if m.group(1) not in "/,;" else f"{m.group(1)} ", value)
    value = re.sub(r"\s+", " ", value)
    previous = None
    while previous != value:
        previous = value
        value = _DANGLING_RE.sub("", value).strip()
    return value


def render_title(
    template: str,
    *,
    analysis: Analysis | Mapping[str, Any] | None,
    context: Mapping[str, Any] | None,
    doc_date: date | str | None,
) -> str:
    """Fill a title template. Missing values disappear together with dangling separators.

    ``{sender} {type} {date:%m/%Y}`` with an unknown sender gives "Rechnung 03/2025".
    """
    if not template:
        return ""
    if not isinstance(analysis, Analysis):
        analysis = Analysis.from_dict(dict(analysis or {}))
    context = context or {}
    when = parse_date(doc_date)
    original = ""
    for key in ("original_name", "filename"):
        if context.get(key):
            original = title_from_original_name(str(context[key])) or ""
            if original:
                break
    if not original and context.get("title"):
        original = str(context["title"]).strip()

    def value_for(name: str, fmt: str | None) -> str:
        if name == "sender":
            return analysis.sender or ""
        if name == "type":
            return analysis.doc_type or ""
        if name == "subject":
            return clean_subject(str(context.get("mail_subject") or ""))
        if name == "original":
            return original
        if when is None:
            return ""
        if name == "date":
            return _format_date(when, fmt)
        if name == "year":
            return f"{when.year:04d}"
        if name == "month":
            return f"{when.month:02d}"
        if name == "month_name":
            return FULL_MONTH_NAMES[when.month - 1]
        return ""

    def substitute(match: re.Match[str]) -> str:
        token = match.group(0)
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        return value_for(match.group(1) or "", match.group(2)).replace("\n", " ")

    return _collapse(_PLACEHOLDER_RE.sub(substitute, template))


# ---------------------------------------------------------------------------
# Rules from filing decisions

_REFERENCE_PREFERENCE = (
    "Vertragsnummer", "Versicherungsnummer", "Kundennummer", "Mitgliedsnummer", "Mandatsreferenz",
    "Personalnummer", "Steuernummer", "Zählernummer",
)


def _generalize(text: str, when: date) -> tuple[str, bool]:
    """Replace the parts of ``text`` that come from the document date by placeholders."""
    value = text.replace("{", "{{").replace("}", "}}")
    changed = False
    month = FULL_MONTH_NAMES[when.month - 1]
    replacements = (
        (rf"(?<!\d){when:%d}\.{when:%m}\.{when:%Y}(?!\d)", "{date:%d.%m.%Y}"),
        (rf"(?<!\d){when:%Y}-{when:%m}-{when:%d}(?!\d)", "{date:%Y-%m-%d}"),
        (rf"(?<!\d){when:%m}/{when:%Y}(?!\d)", "{date:%m/%Y}"),
        (rf"(?<!\d){when:%m}-{when:%Y}(?!\d)", "{date:%m-%Y}"),
        (rf"(?<!\d){when:%m}\.{when:%Y}(?!\d)", "{date:%m.%Y}"),
        (rf"(?<!\d){when:%Y}-{when:%m}(?!\d)", "{date:%Y-%m}"),
        (rf"\b{re.escape(month)}\b", "{month_name}"),
        (rf"(?<!\d){when:%Y}(?!\d)", "{year}"),
    )
    for pattern, placeholder in replacements:
        value, count = re.subn(pattern, placeholder, value, flags=re.IGNORECASE)
        changed = changed or count > 0
    return value, changed


_PERIOD_MONTH_RE = re.compile(
    r"(?<![^\W\d_])(" + "|".join(sorted((re.escape(m) for m in MONTHS), key=len, reverse=True)) + r")(?![^\W\d_])"
    r"(\.?)(\s*(?:\x00|\d{4}))?",
    re.IGNORECASE,
)
# "10/2025", "10.{year}", "{year}-10", "01.10.2025", "2024" – after placeholders became \x00.
_PERIOD_NUMBER_RES = (
    re.compile(r"(?<![\d.])(?:0?[1-9]|1[0-2])\s?[/.\-]\s?(?:\x00|(?:19|20)\d{2}(?!\d))"),
    re.compile(r"(?:\x00|(?<!\d)(?:19|20)\d{2})-(?:0?[1-9]|1[0-2])(?!\d)"),
    re.compile(r"(?<!\d)\d{1,2}\.\d{1,2}\.\s?(?:\x00|\d{4}(?!\d)|\d{2}(?!\d))"),
    re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)"),
)


def _names_other_period(template: str) -> bool:
    """True if a generalized title still names a month or a year literally.

    "Kontoauszug Oktober {year}" (the statement for October, dated 1 November),
    "Kontoauszug 10/{year}" or "Steuerbescheid 2024 vom {date:%d.%m.%Y}" belong to
    one period: as a template they would give every later document the same month
    or year.
    """
    text = _PLACEHOLDER_RE.sub(lambda match: "\x00" if match.group(1) is not None else " ", template)
    for match in _PERIOD_MONTH_RE.finditer(text):
        if match.group(1).casefold() in FULL_MONTH_WORDS or match.group(2) or match.group(3):
            return True
    return any(regex.search(text) for regex in _PERIOD_NUMBER_RES)


def rule_from_filing(request: FilingRequest, analysis: Analysis | Mapping[str, Any] | None, source: str) -> Rule:
    """A rule that sends future documents like this one to the same folder.

    Matches on the sender the user confirmed (``request.sender``; a sender the
    user cleared is not taken from the analysis); without one on a contract or
    customer number, then on an IBAN, and for dedicated watch folders on the
    source. Dates in the confirmed title and tags become placeholders
    ("Abschlag März 2025" -> "Abschlag {month_name} {year}"); a title that names
    another month or year than the document date ("Kontoauszug Oktober 2025"
    dated 1 November) gives no title template. Raises ConfigError if nothing
    identifies such documents.
    """
    if not isinstance(analysis, Analysis):
        analysis = Analysis.from_dict(dict(analysis or {}))
    try:
        folder = sanitize_folder(request.folder)
    except PathError as exc:
        raise ConfigError(f"Ordner „{request.folder}“ ist ungültig ({exc}).") from exc
    if not folder:
        raise ConfigError("Für eine Regel wird ein Zielordner benötigt.")

    sender = (request.sender or "").strip()
    match = RuleMatch()
    name = sender
    if sender:
        match.sender = [sender]
    else:
        reference = None
        for label in _REFERENCE_PREFERENCE:
            values = analysis.references.get(label) or []
            if values:
                reference = (label, values[0])
                break
        if reference:
            match.reference = [reference[1]]
            name = f"{reference[0]} {reference[1]}"
        elif analysis.ibans and iban_valid(analysis.ibans[0]):
            match.iban = [analysis.ibans[0]]
            name = f"IBAN {_pretty_iban(analysis.ibans[0])}"
        elif (source or "").startswith("watch-"):
            match.source = [source]
            name = f"Eingang {source}"
        else:
            raise ConfigError("Für eine Regel fehlt ein Merkmal – bitte einen Absender angeben.")

    title: str | None = None
    tags = list(request.tags)
    when = request.doc_date
    if when is not None:
        generalized, changed = _generalize(request.title, when)
        title = generalized if changed and not _names_other_period(generalized) else None
        tags = [_generalize(tag, when)[0] for tag in request.tags]
    else:
        tags = [tag.replace("{", "{{").replace("}", "}}") for tag in request.tags]
    rule = Rule(name=name[:80], match=match, action=RuleAction(folder=folder, title=title, tags=tags), auto=False)
    # Validate exactly like a rule read from the file.
    return _parse_rule(rule.to_dict(), 1)


__all__ = [
    "Rule",
    "RuleAction",
    "RuleHit",
    "RuleMatch",
    "RuleStore",
    "TEMPLATE",
    "clean_subject",
    "dump_rules",
    "parse_rules",
    "render_title",
    "rule_from_filing",
]
