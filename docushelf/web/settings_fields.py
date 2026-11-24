"""What the settings pages let an admin change, and how.

A :class:`Section` is one card with one form and one save button; its
:class:`Field` objects know their key in ``settings.yaml`` (the environment
variable is ``DOCUSHELF_<KEY>``), how they are shown, how typed input is
checked and stored, and whether the running server picks a change up at once
or after a restart. Sections with ``scope="household"`` belong to one
household: with several households they are stored in that household's entry
of ``households.yaml`` instead of ``settings.yaml``.

Everything here is pure: no files, no requests. :mod:`.routes_settings` uses
it to render the pages and to save them.
"""

from __future__ import annotations

import ipaddress
import math
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import (
    DEFAULT_AUTO_FILE_SOURCES,
    DEFAULT_LLM_CONTEXT,
    PRIVATE_NETWORKS,
    parse_auto_file_mode,
    parse_llm_provider,
)
from ..errors import ConfigError

_TRUE_WORDS = {"1", "true", "yes", "ja", "on", "an"}
_URL_RE = re.compile(r"^https?://[^/\s?#]+[^\s]*$", re.IGNORECASE)
_LANGUAGES_RE = re.compile(r"^[a-z][a-z_]{1,31}(\+[a-z][a-z_]{1,31})*$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


# ---------------------------------------------------------------------------
# Model


@dataclass(frozen=True)
class Field:
    """One setting on a settings page.

    ``kind``: ``text``, ``url``, ``int``, ``decimal``, ``percent``, ``bool``,
    ``choice``, ``radio`` (a choice shown as a list of options), ``list``
    (comma or line separated), ``lines`` (one entry per line), ``sources``
    (checkboxes for inbox sources) or ``secret`` (write-only).
    ``default`` is the stored value the loader uses without an entry; a value
    equal to it is removed from the file. ``scale`` converts the stored value
    for display (seconds shown as hours: 3600). ``attr`` reads the value from
    a ``Settings`` object (restart detection, household values).
    """

    key: str
    label: str
    kind: str = "text"
    help: str = ""
    default: Any = None
    choices: tuple[tuple[str, str], ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""
    scale: float = 1
    store_int: bool = True
    placeholder: str = ""
    mono: bool = False
    max_length: int = 500
    attr: Callable[[Any], Any] | None = None
    check: Callable[[Any], None] | None = None
    # Shown only with one household ("single") or only with several ("multi").
    only: str | None = None

    def value_of(self, settings: Any) -> Any:
        """The value in a ``Settings`` object (for comparisons and household values)."""
        if settings is None:
            return None
        if self.attr is not None:
            try:
                value = self.attr(settings)
            except Exception:
                value = None
        else:
            value = getattr(settings, self.key, None)
        # Not set (e.g. the language model is off): the default applies.
        return self.default if value is None else value


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    icon: str
    intro: str
    fields: tuple[Field, ...]
    # "global": settings.yaml; "household": households.yaml entry with several households.
    scope: str = "global"
    # Applies without a restart (LIVE_KEYS of settings_file).
    live: bool = False
    # Text of a checkbox that must be ticked before a change is saved.
    confirm: str | None = None
    note: str = ""


@dataclass(frozen=True)
class Page:
    key: str
    label: str
    icon: str
    url: str
    admin: bool
    description: str
    sections: tuple[Section, ...] = ()


# ---------------------------------------------------------------------------
# Attribute readers


def _llm(name: str) -> Callable[[Any], Any]:
    return lambda s: getattr(s.llm, name, None) if s.llm is not None else None


def _smtp(name: str) -> Callable[[Any], Any]:
    return lambda s: getattr(s.smtp, name, None) if s.smtp is not None else None


def _auto(name: str) -> Callable[[Any], Any]:
    return lambda s: getattr(s.auto_file, name, None)


# ---------------------------------------------------------------------------
# Extra checks (things the loader accepts but the server would trip over later)


def _check_timezone(value: Any) -> None:
    try:
        ZoneInfo(str(value))
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"„{value}“ ist keine bekannte Zeitzone (z. B. Europe/Berlin).") from None


def _check_languages(value: Any) -> None:
    if not _LANGUAGES_RE.match(str(value)):
        raise ConfigError("Sprachen als Tesseract-Kürzel mit „+“ verbinden, z. B. deu+eng.")


def _check_networks(value: Any, *, allow_star: bool) -> None:
    for entry in value or ():
        if allow_star and entry == "*":
            continue
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            raise ConfigError(f"„{entry}“ ist keine IP-Adresse und kein Netz (z. B. 192.168.1.0/24).") from None


def _check_proxies(value: Any) -> None:
    entries = [part.strip() for part in str(value or "").split(",") if part.strip()]
    if not entries:
        raise ConfigError("Bitte mindestens eine Adresse oder ein Netz angeben (Standard: private Netze).")
    _check_networks(entries, allow_star=True)


def _check_extra_args(value: Any) -> None:
    for line in value or ():
        try:
            shlex.split(line)
        except ValueError:
            raise ConfigError(f"„{line}“ lässt sich nicht als Argumente lesen (Anführungszeichen prüfen).") from None


def _check_https(value: Any) -> None:
    if value and not str(value).lower().startswith("https://"):
        raise ConfigError("Der Link muss mit https:// beginnen.")


# ---------------------------------------------------------------------------
# The pages

SOURCE_CHOICES = (
    ("upload", "Hochgeladen (Browser, Handy, API)"),
    ("smb", "Scanner (SMB)"),
    ("ftp", "Scanner (FTP)"),
    ("mail", "Mail-Postfächer"),
    ("smtp", "Scan per Mail"),
    ("watch-*", "Alle Watch-Ordner"),
    ("paperless", "Paperless-Import"),
    ("unbekannt", "Unbekannte Herkunft"),
)

AUTO_MODES = (
    ("off", "Aus – alles wartet im Eingang auf dich"),
    ("rules", "Nur Regeln, die als „automatisch“ markiert sind"),
    ("confident", "Sicher – ablegen ab dem Schwellwert, der Rest wartet im Eingang"),
    ("always", "Immer – alles ablegen, Unsicheres zum Prüfen markieren"),
)

AUTO_SECTION = Section(
    key="automatisch",
    title="Automatische Ablage",
    icon="wand",
    intro=(
        "Legt DocuShelf Dokumente selbst ab? Jede automatische Ablage steht im Protokoll und lässt sich "
        "korrigieren – gelöscht wird nie etwas."
    ),
    scope="household",
    live=True,
    fields=(
        Field("auto_file", "Modus", "radio", default="off", choices=AUTO_MODES, attr=_auto("mode")),
        Field(
            "auto_file_threshold", "Sicher ab", "percent", default=0.75, minimum=0, maximum=100, unit="%",
            help="Ab dieser Sicherheit gilt eine Entscheidung als sicher (Standard 75 %).", attr=_auto("threshold"),
        ),
        Field(
            "auto_file_sources", "Quellen", "sources", default=DEFAULT_AUTO_FILE_SOURCES, choices=SOURCE_CHOICES,
            help="Nur Dokumente aus diesen Eingängen werden automatisch abgelegt.", attr=_auto("sources"),
        ),
        Field(
            "auto_file_new_folders", "Neue Ordner", "bool", default=True, attr=_auto("new_folders"),
            help="DocuShelf darf einen fehlenden Ordner anlegen, wenn er in die bestehende Struktur passt.",
        ),
        Field(
            "auto_file_wait_for_llm", "Auf die KI warten", "bool", default=True, attr=_auto("wait_for_llm"),
            help="Ist ein Sprachmodell eingeschaltet, wird erst nach seiner Antwort entschieden.",
        ),
        Field(
            "auto_file_notify", "Benachrichtigen", "bool", default=True, attr=_auto("notify"),
            help="Eine Benachrichtigung pro automatisch abgelegtem Dokument (Adresse unter Benachrichtigungen).",
        ),
    ),
)

NAMES_SECTION = Section(
    key="namen",
    title="Namen im Haushalt",
    icon="user",
    intro=(
        "Die Namen der Menschen, denen das Archiv gehört. Sie stehen auf fast jedem Brief – deshalb schlägt "
        "DocuShelf sie nie als Absender vor."
    ),
    scope="household",
    live=True,
    fields=(
        Field(
            "owner_names", "Namen", "lines", default=(), placeholder="Erika Mustermann\nMax Mustermann",
            help="Ein Name pro Zeile.", attr=lambda s: s.owner_names,
        ),
    ),
)

RETENTION_SECTION = Section(
    key="aufbewahrung",
    title="Papierkorb & Originale",
    icon="trash",
    intro="Wie lange Gelöschtes und die Originale abgelegter Eingänge aufbewahrt werden.",
    live=True,
    fields=(
        Field(
            "trash_retention_days", "Papierkorb leeren nach", "int", default=30, minimum=0, maximum=3650, unit="Tagen",
            help="0 = der Papierkorb wird nie automatisch geleert.",
        ),
        Field(
            "keep_originals_days", "Originale behalten", "int", default=30, minimum=0, maximum=3650, unit="Tage",
            help="So lange bleiben die unveränderten Originale abgelegter Dokumente im Eingangsordner „erledigt“.",
        ),
    ),
)

LLM_SECTION = Section(
    key="sprachmodell",
    title="Sprachmodell",
    icon="sparkle",
    intro=(
        "Ein Sprachmodell schlägt Titel, Datum und Ordner vor und hilft der automatischen Ablage. Der Text der "
        "Dokumente geht dafür an den Dienst unten – am besten einer im eigenen Netz."
    ),
    fields=(
        Field("llm_enabled", "Sprachmodell nutzen", "bool", default=False, attr=lambda s: s.llm is not None),
        Field(
            "llm_provider", "Anbieter", "choice", default="ollama", attr=_llm("provider"),
            choices=(
                ("ollama", "Ollama (lokal)"),
                ("openai", "OpenAI-kompatibler Server (llama.cpp, vLLM, LM Studio …)"),
                ("anthropic", "Anthropic (Cloud)"),
            ),
        ),
        Field(
            "llm_url", "Adresse", "url", placeholder="http://ollama:11434", mono=True, attr=_llm("url"),
            help="Leer lassen für Ollama unter http://ollama:11434. OpenAI-kompatible Server oft mit /v1. Bei Anthropic ohne Bedeutung: die Adresse der Anthropic-API ist fest.",
        ),
        Field("llm_model", "Modell", "text", placeholder="qwen2.5:7b", mono=True, attr=_llm("model"), max_length=200),
        Field(
            "llm_api_key", "API-Schlüssel", "secret", attr=_llm("api_key"),
            help="Nur nötig, wenn der Dienst einen Schlüssel verlangt. Wird nie angezeigt.",
        ),
    ),
)

LLM_LIMITS_SECTION = Section(
    key="grenzen",
    title="Antwortzeit & Umfang",
    icon="clock",
    intro="Wie lange DocuShelf auf eine Antwort wartet und wie viel Text das Modell bekommt.",
    fields=(
        Field("llm_timeout", "Zeitlimit", "int", default=180, minimum=1, maximum=3600, unit="Sekunden", attr=_llm("timeout")),
        Field(
            "llm_context", "Kontextgröße", "int", default=DEFAULT_LLM_CONTEXT, minimum=1024, maximum=1_000_000,
            unit="Tokens", attr=_llm("context_tokens"),
            help="Nur Ollama: Größe des Kontextfensters (num_ctx). Zu klein schneidet Ordnerliste oder Text ab.",
        ),
        Field(
            "llm_max_chars", "Text pro Dokument", "int", default=6000, minimum=500, maximum=200_000, unit="Zeichen",
            attr=_llm("max_chars"),
        ),
        Field(
            "similarity_enabled", "Ähnlichkeit", "bool", default=True,
            help="Vorschläge aus ähnlichen, schon abgelegten Dokumenten (lokal, ohne Sprachmodell).",
        ),
    ),
)

OCR_SECTION = Section(
    key="texterkennung",
    title="Texterkennung (OCR)",
    icon="scan",
    intro="Gescannte Seiten bekommen eine unsichtbare Textebene, damit die Suche sie findet.",
    fields=(
        Field("ocr_enabled", "Texterkennung", "bool", default=True, help="Ausgeschaltet bleiben Scans ohne Text."),
        Field(
            "ocr_languages", "Sprachen", "text", default="deu+eng", mono=True, max_length=200, check=_check_languages,
            help="Tesseract-Kürzel, mit „+“ verbunden – z. B. deu+eng.",
        ),
        Field(
            "ocr_output_type", "Ausgabeformat", "choice", default="pdfa-2",
            choices=(("pdfa-2", "PDF/A-2 (empfohlen)"), ("pdfa-1", "PDF/A-1"), ("pdfa-3", "PDF/A-3"), ("pdf", "normales PDF")),
        ),
        Field("ocr_jobs", "Kerne pro Lauf", "int", default=2, minimum=1, maximum=64, help="Wie viele Seiten gleichzeitig erkannt werden."),
        Field(
            "ocr_parallel", "Gleichzeitige Läufe", "int", default=1, minimum=1, maximum=32,
            help="Für die ganze Installation, alle Haushalte zusammen.",
        ),
        Field(
            "ocr_timeout", "Zeitlimit", "int", default=900, minimum=30, maximum=86_400, unit="Sekunden",
            help="Wächst bei langen Dokumenten pro Seite mit.",
        ),
        Field(
            "ocr_extra_args", "Zusätzliche Argumente", "lines", default=(), mono=True, check=_check_extra_args,
            placeholder="--clean", help="Für OCRmyPDF, ein Eintrag pro Zeile. Nur ändern, wenn du weißt, was es tut.",
        ),
    ),
)

BLANK_SECTION = Section(
    key="leerseiten",
    title="Leerseiten",
    icon="file",
    intro="Leere Rückseiten aus dem Duplex-Scan werden vor der Texterkennung entfernt.",
    fields=(
        Field("blank_page_removal", "Leerseiten entfernen", "bool", default=True),
        Field(
            "blank_page_threshold", "Schwelle", "decimal", default=0.004, minimum=0, maximum=0.2, store_int=False,
            mono=True, help="Anteil dunkler Bildpunkte, unter dem eine Seite als leer gilt (Standard 0,004).",
        ),
    ),
)

RUNTIME_SECTION = Section(
    key="betrieb",
    title="Leistung & Grenzen",
    icon="sliders",
    intro="Wie viel DocuShelf gleichzeitig verarbeitet und wie oft das Archiv abgeglichen wird.",
    fields=(
        Field("worker_threads", "Verarbeitungs-Threads", "int", default=1, minimum=1, maximum=16, help="Pro Haushalt."),
        Field("max_upload_mb", "Größte Datei beim Hochladen", "int", default=200, minimum=1, maximum=10_000, unit="MB"),
        Field(
            "reconcile_interval", "Vollabgleich alle", "decimal", default=6 * 3600, minimum=0.25, maximum=168,
            scale=3600, unit="Stunden", help="Zusätzlich beim Start und nach jeder Änderung im Archivordner.",
        ),
    ),
)

RECEIVE_SECTION = Section(
    key="empfang",
    title="Empfangsserver & Wartezeiten",
    icon="printer",
    intro=(
        "Der SMTP-Empfänger nimmt Scans direkt vom Drucker an (ein Port für alle Haushalte). Die Wartezeiten "
        "bestimmen, wann eine Datei in einem Ordner als vollständig gilt."
    ),
    fields=(
        Field("smtp_enabled", "SMTP-Empfänger", "bool", default=False, attr=lambda s: s.smtp is not None),
        Field(
            "smtp_recipient", "Empfangsadresse", "text", mono=True, placeholder="scan@docushelf.local", only="single",
            attr=_smtp("recipient"), help="Nötig mit nur einem Haushalt. Weitere Adressen trägst du oben bei „Scan per Mail“ ein.",
        ),
        Field(
            "smtp_allowed_networks", "Erlaubte Netze", "list", default=(), mono=True, attr=_smtp("allowed_networks"),
            placeholder="192.168.1.0/24, 100.64.0.0/10", check=lambda v: _check_networks(v, allow_star=False),
            help="Nur von diesen Adressen werden Mails angenommen. Leer: alles wird abgewiesen.",
        ),
        Field(
            "smtp_max_message_mb", "Größte Mail", "int", default=50, minimum=1, maximum=500, unit="MB",
            attr=lambda s: s.smtp.max_message_bytes // (1024 * 1024) if s.smtp else None,
        ),
        Field("smtp_user", "Benutzer", "text", mono=True, attr=_smtp("username"), help="Optional: Anmeldung am Empfänger."),
        Field("smtp_password", "Passwort", "secret", attr=_smtp("password")),
        Field(
            "stable_seconds", "Wartezeit Watch-Ordner", "decimal", default=5.0, minimum=0, maximum=3600, store_int=False,
            unit="Sekunden",
        ),
        Field(
            "remote_stable_seconds", "Wartezeit Scanner-Ordner", "decimal", default=10.0, minimum=0, maximum=3600,
            store_int=False, unit="Sekunden", attr=lambda s: next((x.stable_seconds for x in s.sources if x.name == "smb"), None),
            help="SMB und FTP: so lange muss eine Datei unverändert bleiben.",
        ),
    ),
)

NOTIFY_SECTION = Section(
    key="ziel",
    title="Benachrichtigungen",
    icon="bell",
    intro=(
        "DocuShelf meldet sich bei Fehlern in der Verarbeitung und – falls eingeschaltet – bei jeder automatischen "
        "Ablage. Gesendet wird ein einfacher POST, z. B. an ein ntfy-Thema."
    ),
    scope="household",
    live=True,
    fields=(
        Field(
            "notify_url", "Adresse", "url", mono=True, placeholder="https://ntfy.example.org/docushelf",
            help="Leer lassen, um Benachrichtigungen auszuschalten. Die Nachricht enthält Dokumenttitel.",
            attr=lambda s: s.notify_url,
        ),
    ),
)

SHORTCUT_SECTION = Section(
    key="kurzbefehl",
    title="iPhone-Kurzbefehl",
    icon="smartphone",
    intro=(
        "Liegt keine signierte Kurzbefehl-Datei in config/shortcuts/, führt der Installations-Knopf oben zu "
        "diesem iCloud-Link."
    ),
    fields=(
        Field(
            "ios_shortcut_url", "iCloud-Link", "url", mono=True, check=_check_https, max_length=2000,
            placeholder="https://www.icloud.com/shortcuts/…",
        ),
    ),
)

LOCKOUT = (
    "Ich habe geprüft, dass ich mich danach noch anmelden kann – notfalls über den lokalen Notfallzugang."
)

OIDC_SECTION = Section(
    key="oidc",
    title="Anmeldedienst (OIDC)",
    icon="shield",
    intro="Anmeldung über einen OIDC-Anbieter wie Authentik, Keycloak oder Kanidm.",
    confirm=LOCKOUT,
    fields=(
        Field("oidc_issuer", "Issuer-URL", "url", mono=True, placeholder="https://auth.example.org/application/o/docushelf/"),
        Field("oidc_client_id", "Client-ID", "text", mono=True, max_length=300),
        Field("oidc_client_secret", "Client-Secret", "secret"),
        Field("oidc_label", "Text auf dem Anmeldeknopf", "text", default="Mit SSO anmelden", max_length=80),
        Field("oidc_scopes", "Scopes", "text", default="openid email profile", mono=True, max_length=300),
    ),
)

ACCESS_SECTION = Section(
    key="zugang",
    title="Wer darf hinein",
    icon="users",
    intro="Konten und Gruppen aus dem Anmeldedienst. Benutzernamen oder bestätigte E-Mail-Adressen, Groß-/Kleinschreibung egal.",
    confirm=LOCKOUT,
    fields=(
        Field(
            "allowed_users", "Erlaubte Benutzer", "list", default=(), only="single",
            help="Mit mehreren Haushalten entscheiden die Haushalte (Einstellungen → Haushalte).",
        ),
        Field("allowed_groups", "Erlaubte Gruppen", "list", default=(), only="single"),
        Field(
            "allow_all_oidc_users", "Alle Konten zulassen", "bool", default=False, only="single",
            help="Nur, wenn der Anmeldedienst selbst schon begrenzt, wer DocuShelf nutzen darf.",
        ),
        Field("admin_users", "Administratoren", "list", default=(), help="Dürfen alle Einstellungen ändern."),
        Field("admin_groups", "Administratorgruppen", "list", default=()),
    ),
)

SESSION_SECTION = Section(
    key="sitzung",
    title="Adresse & Sitzungen",
    icon="lock",
    intro="Unter welcher Adresse DocuShelf erreichbar ist und wie lange eine Anmeldung gilt.",
    confirm=LOCKOUT,
    fields=(
        Field(
            "base_url", "Adresse im Browser", "url", default="http://localhost:8000", mono=True,
            help="Für Links und die Rückleitung vom Anmeldedienst. Mit https:// gehen Cookies nur über HTTPS.",
        ),
        Field(
            "session_max_age", "Anmeldung gilt", "decimal", default=14 * 24 * 3600, minimum=0.05, maximum=365,
            scale=86400, unit="Tage", help="Geänderte Gruppen im Anmeldedienst wirken erst bei der nächsten Anmeldung.",
        ),
        Field(
            "trusted_proxies", "Vertrauenswürdige Proxys", "text", default=PRIVATE_NETWORKS, mono=True, check=_check_proxies,
            max_length=2000, help="Adressen oder Netze, deren X-Forwarded-Header gelten (kommagetrennt). „*“ vertraut jedem.",
        ),
    ),
)

GENERAL_SECTION = Section(
    key="allgemein",
    title="Allgemein",
    icon="sliders",
    intro="Zeitzone für Datumsangaben und wie ausführlich DocuShelf ins Server-Protokoll schreibt.",
    fields=(
        Field("timezone", "Zeitzone", "text", default="Europe/Berlin", mono=True, check=_check_timezone, max_length=80),
        Field(
            "log_level", "Protokoll-Ausführlichkeit", "choice", default="info",
            choices=(("debug", "debug – sehr ausführlich"), ("info", "info"), ("warning", "warning"), ("error", "error")),
        ),
    ),
)


HOUSEHOLD_SECTION = Section(
    key="haushalt",
    title="Dein Haushalt",
    icon="building",
    intro="Der Name erscheint in der Oberfläche und in Benachrichtigungen.",
    fields=(Field("household_name", "Name", "text", default="Zuhause", only="single", max_length=80),),
)


PAGES: tuple[Page, ...] = (
    Page("uebersicht", "Übersicht", "grid", "/einstellungen", False, "Alles auf einen Blick"),
    Page(
        "haushalte", "Haushalte", "building", "/einstellungen/haushalte", True, "Wer zu welchem Haushalt gehört",
        (HOUSEHOLD_SECTION,),
    ),
    Page(
        "kanaele", "Eingangskanäle", "inbox", "/einstellungen/kanaele", True,
        "Postfächer, Scan per Mail, Scanner- und Watch-Ordner", (RECEIVE_SECTION,),
    ),
    Page(
        "ablage", "Ablage & Auto-Ablage", "wand", "/einstellungen/ablage", True,
        "Automatische Ablage, Papierkorb und Originale", (AUTO_SECTION, NAMES_SECTION, RETENTION_SECTION),
    ),
    Page("ki", "KI", "sparkle", "/einstellungen/ki", True, "Sprachmodell für Vorschläge", (LLM_SECTION, LLM_LIMITS_SECTION)),
    Page(
        "verarbeitung", "Verarbeitung", "scan", "/einstellungen/verarbeitung", True,
        "Texterkennung, Leerseiten und Leistung", (OCR_SECTION, BLANK_SECTION, RUNTIME_SECTION),
    ),
    Page(
        "benachrichtigungen", "Benachrichtigungen", "bell", "/einstellungen/benachrichtigungen", True,
        "Meldungen aufs Handy", (NOTIFY_SECTION,),
    ),
    Page("handy", "Handy & Tokens", "smartphone", "/einstellungen/handy", False, "Teilen vom Handy und API-Tokens", (SHORTCUT_SECTION,)),
    Page(
        "anmeldung", "Anmeldung & Sicherheit", "shield", "/einstellungen/anmeldung", True,
        "Anmeldedienst, Zugang und Sitzungen", (OIDC_SECTION, ACCESS_SECTION, SESSION_SECTION),
    ),
    Page("protokoll", "Protokoll", "history", "/einstellungen/protokoll", False, "Wer wann was getan hat"),
    Page("system", "System", "database", "/einstellungen/system", False, "Index, Pfade, Versionen und Neustart", (GENERAL_SECTION,)),
)

PAGES_BY_KEY = {page.key: page for page in PAGES}


def page(key: str) -> Page | None:
    return PAGES_BY_KEY.get(key)


def section(page_key: str, section_key: str) -> Section | None:
    found = page(page_key)
    if found is None:
        return None
    return next((s for s in found.sections if s.key == section_key), None)


def all_sections() -> Iterable[tuple[Page, Section]]:
    for item in PAGES:
        for sec in item.sections:
            yield item, sec


def visible_fields(sec: Section, *, multi: bool) -> tuple[Field, ...]:
    wanted = "multi" if multi else "single"
    return tuple(f for f in sec.fields if f.only in (None, wanted))


# ---------------------------------------------------------------------------
# Display


def _words(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [str(v).strip() for v in value]
    else:
        items = [part.strip() for chunk in str(value).split("\n") for part in chunk.split(",")]
    return [item for item in items if item]


def _number_text(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


def form_text(f: Field, value: Any) -> Any:
    """A stored value (any form: from YAML, the environment or Settings) as the form shows it.

    Booleans stay booleans, ``sources`` become a list, everything else a string.
    """
    if f.kind == "bool":
        if isinstance(value, bool):
            return value
        if value is None:
            return bool(f.default)
        return str(value).strip().casefold() in _TRUE_WORDS
    if f.kind == "sources":
        return [w.casefold() for w in _words(f.default if value is None else value)]
    if value is None:
        value = f.default
    if value is None:
        return ""
    if f.kind in ("list",):
        return ", ".join(_words(value))
    if f.kind == "lines":
        return "\n".join(_words(value))
    if f.kind == "percent":
        try:
            number = float(str(value).replace(",", ".").rstrip("%"))
        except ValueError:
            return str(value)
        return _number_text(round(number * 100, 1) if number <= 1 else number)
    if f.kind in ("int", "decimal"):
        try:
            number = float(str(value).replace(",", "."))
        except ValueError:
            return str(value)
        return _number_text(number / f.scale)
    if f.kind in ("choice", "radio"):
        text = str(value).strip()
        if f.key == "auto_file":
            try:
                return parse_auto_file_mode(value)
            except ConfigError:
                return text
        if f.key == "llm_provider":
            try:
                return parse_llm_provider(value)
            except ConfigError:
                return text
        return text.casefold() if f.key == "log_level" else text
    return str(value)


# ---------------------------------------------------------------------------
# Parsing what was typed


def _clean_text(f: Field, raw: str) -> str | None:
    text = raw.strip()
    if not text:
        return None
    if len(text) > f.max_length:
        raise ConfigError(f"{f.label}: höchstens {f.max_length} Zeichen.")
    if _CONTROL.search(text):
        raise ConfigError(f"{f.label}: Zeilenumbrüche und Steuerzeichen sind nicht erlaubt.")
    return text


def _parse_number(f: Field, raw: str) -> float | None:
    text = raw.strip().replace(" ", "").replace(",", ".").rstrip("%")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        raise ConfigError(f"{f.label}: bitte eine Zahl eingeben.") from None
    if math.isnan(number) or math.isinf(number):
        raise ConfigError(f"{f.label}: bitte eine Zahl eingeben.")
    if f.kind == "int" and f.scale == 1 and not number.is_integer():
        raise ConfigError(f"{f.label}: bitte eine ganze Zahl eingeben.")
    if f.minimum is not None and number < f.minimum:
        raise ConfigError(f"{f.label}: mindestens {_number_text(f.minimum)}{' ' + f.unit if f.unit else ''}.")
    if f.maximum is not None and number > f.maximum:
        raise ConfigError(f"{f.label}: höchstens {_number_text(f.maximum)}{' ' + f.unit if f.unit else ''}.")
    return number


def parse_field(f: Field, form: Any) -> Any:
    """The value to store for what was typed (None: no value, the default applies). ConfigError if invalid."""
    raw = _form_get(form, f.key)
    if f.kind == "bool":
        return raw.strip().casefold() in _TRUE_WORDS
    if f.kind == "sources":
        chosen = [v.strip().casefold() for v in _form_list(form, f.key) if v.strip()]
        known = {key for key, _ in f.choices}
        extra = [w.casefold() for w in _words(_form_get(form, f.key + "__extra"))]
        values: list[str] = []
        for value in [*chosen, *extra]:
            if value not in known and not re.match(r"^[a-z0-9][a-z0-9._-]*\*?$", value):
                raise ConfigError(f"{f.label}: „{value}“ ist kein gültiger Eingang (z. B. watch-nas oder watch-*).")
            if value not in values:
                values.append(value)
        return values
    if f.kind == "percent":
        number = _parse_number(f, raw)
        return None if number is None else round(number / 100, 4)
    if f.kind in ("int", "decimal"):
        number = _parse_number(f, raw)
        if number is None:
            return None
        stored = number * f.scale
        return int(round(stored)) if f.store_int else stored
    if f.kind in ("choice", "radio"):
        text = raw.strip()
        if not text:
            return None
        if text not in {key for key, _ in f.choices}:
            raise ConfigError(f"{f.label}: bitte eine der angebotenen Möglichkeiten wählen.")
        return text
    if f.kind == "list":
        items = _words(raw)
        for item in items:
            if len(item) > 200 or _CONTROL.search(item):
                raise ConfigError(f"{f.label}: „{item[:40]}“ ist zu lang oder enthält Steuerzeichen.")
        return list(dict.fromkeys(items)) or None
    if f.kind == "lines":
        items = [line.strip() for line in raw.replace("\r", "").split("\n") if line.strip()]
        for item in items:
            if len(item) > 200 or _CONTROL.search(item):
                raise ConfigError(f"{f.label}: „{item[:40]}“ ist zu lang oder enthält Steuerzeichen.")
        return list(dict.fromkeys(items)) or None
    if f.kind == "url":
        text = _clean_text(f, raw)
        if text is not None and not _URL_RE.match(text):
            raise ConfigError(f"{f.label}: die Adresse muss mit http:// oder https:// beginnen.")
        return text.rstrip("/") if text and f.key in ("base_url", "llm_url") else text
    return _clean_text(f, raw)


def stored_value(f: Field, value: Any) -> Any:
    """The value to write: None when it equals the default (the entry is then removed)."""
    if value is None:
        return None
    default = f.default
    if isinstance(default, tuple):
        default = list(default)
    if f.kind in ("int", "decimal", "percent") and default is not None:
        try:
            if abs(float(value) - float(default)) < 1e-9:
                return None
        except (TypeError, ValueError):
            pass
    if value == default:
        return None
    return value


def _form_get(form: Any, key: str) -> str:
    value = form.get(key) if hasattr(form, "get") else None
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _form_list(form: Any, key: str) -> list[str]:
    getlist = getattr(form, "getlist", None)
    if callable(getlist):
        return [str(v) for v in getlist(key)]
    value = form.get(key) if hasattr(form, "get") else None
    if value is None:
        return []
    return [str(v) for v in value] if isinstance(value, (list, tuple)) else [str(value)]


__all__ = [
    "Field",
    "PAGES",
    "Page",
    "Section",
    "all_sections",
    "form_text",
    "page",
    "parse_field",
    "section",
    "stored_value",
    "visible_fields",
]
