# DocuShelf – Architektur

Dieses Dokument hält fest, **wie** DocuShelf das Konzept (`docs/concept.md`) umsetzt und **warum**.
Die genauen Modulverträge für Entwickler stehen in `docs/internals.md`.

## Leitlinie

Messlatte jeder Entscheidung sind die Kernprinzipien 1–3 aus dem Konzept:
**Ordner sind die Wahrheit, der Index ist wegwerfbar, Änderungen von außen sind normal.**
Im Zweifel gewinnt das Dateisystem.

## Stack

| Bereich | Wahl | Warum |
|---|---|---|
| Sprache | Python 3.12 | OCRmyPDF, pikepdf und pypdfium2 sind die besten freien PDF-Werkzeuge und alle in Python zuhause. |
| Web | FastAPI + Jinja2, eigenes CSS/JS ohne Build-Schritt | Serverseitig gerenderte Seiten sind schnell, robust und brauchen keine Toolchain. Kein CDN, keine externen Fonts (Daten bleiben lokal). |
| PDF | pikepdf (Struktur, XMP), pypdfium2 (Text, Vorschaubilder), img2pdf/Pillow (Bilder) | verlustfrei, schnell, keine Ghostscript-Aufrufe außer in der OCR |
| OCR | OCRmyPDF/Tesseract `deu+eng`, Ausgabe PDF/A-2, `--skip-text` | Konzept §6. Läuft als Subprozess mit Timeout, damit ein hängender Lauf die App nicht blockiert. |
| Index | SQLite (WAL) mit FTS5 | eine Datei, kein Server, Volltextsuche eingebaut; laut Konzept nur Cache |
| Login | OIDC (Authlib) gegen einen beliebigen OIDC-Anbieter, lokaler Admin als Notfallzugang | Konzept §11 |
| Mail | `imaplib` (Abruf), `aiosmtpd` (Empfang) | Standardbibliothek bzw. schlanke Bibliothek |
| Sprachmodell | optional: Ollama, jeder OpenAI-kompatible Server oder die Anthropic-API | Standard ist ein lokales Modell, damit Dokumenttext das Haus nicht verlässt; ein Cloud-Anbieter ist eine bewusste Entscheidung. |
| Betrieb | ein Container für Web + Worker, Samba und FTP als eigene Container (Compose-Profile) | einfacher Betrieb; Druckerkanäle einzeln abschaltbar |

Ein Prozess, mehrere Hintergrund-Threads (Worker, Eingangsüberwachung, Archiv-Watcher,
IMAP, SMTP). Für eine Handvoll Haushalte ist das mehr als genug und vermeidet Koordination
zwischen Containern. OCR läuft in eigenen Prozessen und nutzt mehrere Kerne; wie viele
Läufe gleichzeitig starten dürfen, begrenzt ein prozessweiter Zähler (`DOCUSHELF_OCR_PARALLEL`,
Standard 1), damit mehrere Haushalte die Maschine nicht gemeinsam überlasten.

## Datenmodell ohne Datenbank

- **Ordnerpfad** = Ablageort. **Dateiname** `YYYY-MM-DD Titel.pdf` = Datum und Titel.
- **XMP im PDF**: Dokument-ID (UUID), Ursprungs-Hash, Dokumentdatum, Absender, Quelle,
  Eingangs-/Ablagezeitpunkt, `dc:title`, Tags in `dc:subject`/`pdf:Keywords`.
  Eigene Felder sind über ein PDF/A-Erweiterungsschema deklariert, damit PDF/A gültig bleibt.
- **Was gewinnt bei Widerspruch?** Der Dateiname. Wer im Finder umbenennt, sieht das sofort
  in der App; beim nächsten Bearbeiten in der App wird XMP angeglichen.
- **Fremde Dateien** (ohne XMP) bekommen eine vorläufige ID aus dem Inhalts-Hash (`h-…`).
  Erst wenn man sie in der App bearbeitet, schreibt DocuShelf XMP hinein und vergibt eine UUID.
- **Eingang**: Jedes Eingangsdokument ist ein Ordner unter `eingang/…/<id>/` mit `item.json`
  (Zustand, Analyse, Vorschläge), `original/` (unverändert), `document.pdf` (Ergebnis) und
  `log.txt`. Auch der Eingang lebt damit vollständig im Dateisystem.
- **Regeln, Tokens, Einstellungen**: lesbare YAML-Dateien in `config/`.
- **Einstellungen aus der Oberfläche**: Was Admins im Browser ändern, landet in `config/settings.yaml`
  (Haushaltswerte in `households.yaml`, Kanäle in `channels.yaml`), Geheimnisse getrennt in
  `config/secrets/`. Umgebungsvariablen gewinnen weiterhin; eine Änderung wird vor dem Schreiben mit
  dem Start-Lader geprüft, damit eine Eingabe im Browser den nächsten Start nie verhindern kann. Was
  ohne Neustart sicher übernommen werden kann (automatische Ablage, Benachrichtigungen, Fristen,
  Kanäle), gilt sofort; alles andere erst nach „Jetzt neu starten“ – ein sauberes Beenden, das die
  Neustart-Regel des Containers auffängt. Ein Neuladen im laufenden Prozess hätte für Login, OCR
  oder Worker-Threads viele halbe Zustände bedeutet.
- **Haushalte**: optional `config/households.yaml` (siehe unten). Jeder Haushalt hat darunter
  den ganzen Baum (Archiv, Eingang, Papierkorb, Config, Protokoll, Index) in einem eigenen
  Verzeichnis, standardmäßig `haushalte/<id>/`.
- **Audit-Log**: JSON Lines pro Monat in `protokoll/`.
- **SQLite** (`index/`) enthält nur, was sich daraus jederzeit neu aufbauen lässt:
  Suchindex, Ordnerliste, Job-Warteschlange. `docushelf rebuild` löscht und baut neu.

## Abgleich mit dem Dateisystem

Ein Abgleich-Durchlauf geht den ganzen Archivbaum durch. Unveränderte Dateien
(Größe, mtime, Inode gleich) kosten nur einen `stat()`, ein Durchlauf über einige tausend
Dateien dauert deshalb Sekundenbruchteile. Er läuft
- beim Start und periodisch (Standard alle 6 h),
- 2 s nach der letzten Dateisystem-Änderung (inotify über `watchdog`, gebündelt, spätestens nach 20 s),
- nach jeder Aktion der App selbst (die den Index ohnehin sofort nachzieht).

Neue Pfade werden zuerst mit verschwundenen Dokumenten abgeglichen (XMP-ID, dann
Inhalts-Hash). So wird Verschieben/Umbenennen als solches erkannt und Text/ID bleiben
erhalten. Kopien (gleiche XMP-ID zweimal) bekommen eine eigene vorläufige ID. Leere Ordner
sind gültige Ablageziele. Dateien, die während des Abgleichs noch wachsen, werden erst im
nächsten Durchlauf übernommen.

Ein globaler Archiv-Lock serialisiert Abgleich und App-Aktionen, damit keiner den anderen
halbfertig sieht.

## Ablage ohne Datenverlust

Die Ablage eines Eingangsdokuments ist eine kleine Transaktion mit Journal in `item.json`:

1. XMP in eine Kopie schreiben (`work/filing.pdf`).
2. Journal „wird veröffentlicht“ + Hash speichern.
3. Per Hardlink ins Ziel veröffentlichen – ein Hardlink schlägt fehl, wenn der Name schon
   existiert, deshalb wird nie etwas überschrieben (bei Kollision ` (2)`). Liegt `archiv/` auf
   einem anderen Dateisystem, wird erst kopiert und mit `fsync` gesichert.
4. Indexieren, Audit-Log, Eingangsordner nach `erledigt/` (Originale bleiben dort
   standardmäßig 30 Tage).

Bricht der Prozess irgendwo ab, repariert `recover()` beim nächsten Start anhand des Journals:
entweder ist die Datei mit dem erwarteten Hash da (dann wird abgeschlossen) oder nicht
(dann bleibt das Dokument im Eingang). Das Original wird erst gelöscht, nachdem das Ziel
dauerhaft geschrieben ist.

Metadaten bearbeiten im Archiv: neue Datei daneben schreiben, prüfen, dass die alte seit dem
Lesen unverändert ist (sonst Abbruch mit Hinweis), dann atomar ersetzen und ggf. ohne
Überschreiben umbenennen. Löschen verschiebt in den Papierkorb (30 Tage).

## Verarbeitung im Eingang

```
quellen/<kanal>/  ──(stabil?)──▶  arbeit/<id>/  ──▶  bereit/<id>/  ──(Bestätigung)──▶  archiv/…
                                     │                    ▲
                                     └──── Fehler ──▶ fehler/<id>/ ──(erneut)──┘
```

Stufen (jede idempotent, Zustand in `item.json`, übersteht Neustarts):
`received → normalized` (Bilder → PDF, Sendungen zusammenführen) → `deblanked`
(Leerseiten, Schwellwert einstellbar) → `ocr_done` (OCRmyPDF) → `analyzed`
(Text, Datum, Absender, Beträge, IBAN, Nummern, Dokumenttyp, Duplikatprüfung) → `ready`
(Vorschläge).

Signierte und passwortgeschützte PDFs werden nie verändert: kein XMP, keine Leerseitenentfernung,
keine OCR, die Datei wandert byte-identisch ins Archiv (eine Signatur wäre sonst ungültig).
Ein OCR-Zeitlimit ist nicht „vorübergehend“: Ein zweiter Versuch scheitert genauso und
würde nur Rechenzeit verbrennen, das Dokument landet mit Hinweis im Fehlerbereich.

Kanäle schreiben nur in ihre Drop-Zone. Mehrteilige Eingänge (Mail mit Anhängen, Upload
mehrerer Bilder) landen als „Sendung“ mit Manifest `_sendung.json`, das zuletzt geschrieben
wird – halbe Sendungen werden so nie verarbeitet.

## Vorschläge

Gestaffelt, jeder Vorschlag mit Begründung:
1. **Regeln** aus `config/rules.yaml` (Absender, Stichwörter, IBAN, Kunden-/Vertragsnummer,
   Quelle, Mail-Absender, Dokumenttyp → Ordner, Titelvorlage, Tags). Beim Ablegen kann mit
   einem Haken eine Regel entstehen.
2. **Ähnlichkeit zum Bestand**: TF-IDF über den Text aller abgelegten Dokumente, nächste
   Nachbarn → Ordner. Lernt automatisch aus jeder Ablage, ohne Pflege.
3. **Sprachmodell** (optional, `ollama`, `openai` für jeden OpenAI-kompatiblen Server oder
   `anthropic`): Text, vorhandene Ordner mit Dokumentzahl, Beispiel-Dateinamen, erkannte Fakten
   und ähnliche Dokumente → Titel/Datum/Absender/Ordner mit Selbsteinschätzung. Die Antwort wird
   gegen das Archiv geprüft (nur vorhandene Ordner, neue nur in der bestehenden Struktur; ein
   Datum muss im Text stehen), die Selbsteinschätzung zählt nur zu 90 %. Asynchron, mit Timeout,
   standardmäßig aus. Der Dokumenttext geht an den eingestellten Endpunkt – für Dokumente, die
   das Haus nicht verlassen sollen, also ein lokales Modell.
4. **Fallback**: passender bestehender Ordner nach Absender/Dokumenttyp, sonst ein deutlich
   als „neu“ markierter Ordnervorschlag.

## Suche

SQLite FTS5 mit Trigramm-Tokenizer über normalisierten Text: Kleinschreibung, Umlaute als
ae/oe/ue, ß als ss, Akzente entfernt, Silbentrennung am Zeilenende zusammengefügt.
Dadurch findet „Neben“ „Nebenkostenabrechnung“, „Mueller“ findet „Müller“, und
zusammengesetzte Wörter funktionieren ohne Wörterbuch. Tippfehler: Findet ein Begriff
nichts, werden ähnlich geschriebene Wörter aus dem Vokabular des Archivs genommen
(Levenshtein 1–2) und als „Meinten Sie …“ angezeigt. Treffer zeigen Seite und Ausschnitt.

## Automatische Ablage

Konzeptprinzip 4 (Ablage unter Kontrolle des Nutzers, im Zweifel landet alles im Eingang) sieht
eine Ausnahme zum Einschalten vor. Die automatische Ablage ist genau das: standardmäßig aus, ausdrücklich einzuschalten,
vollständig protokolliert, jede Entscheidung sichtbar und korrigierbar, nie wird etwas gelöscht.

- **Stufen** (`DOCUSHELF_AUTO_FILE`): `off`; `rules` (nur Regeln mit `auto: true`); `confident`
  (zusätzlich Entscheidungen ab Schwellwert, Rest bleibt im Eingang); `always` (alles, unter dem
  Schwellwert mit Markierung „unsicher“ zur Kontrolle). Nur bestimmte Eingänge dürfen automatisch
  ablegen; `paperless` und `unbekannt` nur, wenn sie ausdrücklich genannt sind.
- **Entscheidung** (`suggest/auto.py`) ist eine reine Funktion ohne Seiteneffekte, damit sie
  testbar bleibt. Die Signale (Regel, Import, Ähnlichkeit, Sprachmodell, Heuristik) werden je Ordner
  mit `1 − Π(1 − Gewicht · Score)` verrechnet: Übereinstimmung unabhängiger Quellen erhöht die
  Sicherheit, ein knapper zweiter Ordner oder ein neuer Ordner senken sie. Mögliche Duplikate,
  von Hand bearbeitete Dokumente und Dokumente ohne brauchbaren Titel werden nie automatisch abgelegt.
- **Ablauf**: Nach `ready` stellt die Pipeline einen Job `auto_file` ein; ist ein Sprachmodell an
  und `wait_for_llm` gesetzt, läuft zuerst `llm_suggest` und stößt danach `auto_file` an – auch bei
  Fehlschlag. Die Ablage selbst ist dieselbe Transaktion wie die manuelle (Journal, `recover()`),
  nur mit Benutzer „automatisch“. Ein Fehler wird nicht in einer Schleife wiederholt, das Dokument
  bleibt mit Begründung im Eingang.
- **Nachvollziehbarkeit**: Audit-Eintrag `auto_file` (Sicherheit, Signale, Gründe), Eintrag im
  Eingangsordner (`item.context["auto"]`), Seite „Automatisch abgelegt“ mit „Passt“
  (`auto_review`). Das Audit-Log liegt im Datenvolumen und überlebt einen Index-Neuaufbau, der
  Index bleibt wegwerfbar.
- **Warteschlange ist Cache**: Geht sie beim Index-Neuaufbau verloren, bleiben wartende
  Dokumente im Eingang, statt nachträglich automatisch abgelegt zu werden – auch solche, die schon
  vor dem Einschalten dort lagen.

## Oberfläche: Ordneransicht und Handy

- **Ordneransicht**: Kacheln oder Liste, die Wahl steckt im Cookie `ds_ansicht` und wird serverseitig
  gelesen – die Seite stimmt ohne JavaScript und ohne Flackern.
- **Teilen vom Handy**: Android und Desktop-Chrome/Edge installieren DocuShelf als Web-App mit
  `share_target`; ein Service-Worker nimmt die geteilten Dateien entgegen und übergibt sie der
  Seite `/teilen`, die sie mit der angemeldeten Sitzung hochlädt. Ein Formular-POST ohne Worker kann
  kein CSRF-Token mitschicken; stattdessen zählt der Browser-Header `Sec-Fetch-Site` (`none` oder
  `same-origin`), den fremde Seiten nicht setzen können. iPhone/iPad: ein Kurzbefehl, den
  `contrib/ios/build_shortcut.py` erzeugt und der mit einem API-Token an `/api/v1/upload` sendet.
  Eine signierte Datei liegt bewusst nicht im Repository, denn die Signatur hängt an der Apple-ID
  des Signierenden. Das Token gehört zu einem Haushalt, die Dateien landen also automatisch im
  richtigen Eingang.

## Haushalte

Mehrere Familien teilen sich eine Installation (ein Server, eine Pflege), ohne dass jemand die
Dokumente der anderen sieht.

- **Konfiguration**: Ohne `config/households.yaml` gibt es genau einen Haushalt `default` im
  Datenverzeichnis selbst – alles verhält sich wie vor der Einführung, nichts wird verschoben.
  Mit der Datei hat jeder Haushalt ein eigenes Verzeichnis mit vollständigem Baum, eigenen Regeln,
  Tokens, Kanälen und einem eigenen `Services`-Container (Index, Worker, Watcher, Eingang).
  Getrennte Container statt Mandanten-Spalten im Index: Es gibt keine Abfrage, die versehentlich
  über Haushalte hinweg suchen könnte, und jeder Haushalt bleibt ein für sich lesbarer,
  sicherbarer Datenbaum.
- **Streng validiert**: Unbekannte Schlüssel sind Fehler, denn ein Tippfehler bei `groups` darf
  nie stillschweigend ändern, wer Zugriff hat. Eindeutige IDs, Pfade und SMTP-Adressen; kein Haushalt
  darf in den Datenordnern eines anderen liegen, und ein Watch-Ordner, der in fremde Daten
  hineinreicht, wird vom Eingang ignoriert; Geheimnisse (IMAP-Passwort) kommen aus Datei oder Umgebung und werden nie protokolliert.
- **Zugriff**: Mitgliedschaft über OIDC-Gruppen oder Benutzer je Haushalt; der lokale Admin darf
  alle. Beim SSO-Login muss ein Konto zu mindestens einem Haushalt gehören. Wer zu keinem gehört,
  bekommt 403 mit deutscher Erklärung.
- **Zuordnung je Anfrage**: API-Token gehören dem Haushalt, in dessen `tokens.yaml` sie stehen;
  eine Browser-Sitzung merkt sich den gewählten Haushalt (`POST /haushalt`, Umschalter nur ab zwei
  Haushalten). Jede Route holt ihre Daten ausschließlich über `get_services(request)`; Dokument-IDs
  eines anderen Haushalts liefern 404. Vorschaubild-Cache und Wartungsjobs sind je Haushalt getrennt.
- **Kanäle**: IMAP je Haushalt (eigenes Postfach). Der SMTP-Empfänger ist global, denn es gibt nur
  einen Port: Er ordnet jede Nachricht nach Empfängeradresse einem Haushalt zu, unbekannte Adressen
  werden abgewiesen, nie weitergeleitet. SMB/FTP bekommen je Haushalt eine Freigabe.
- **Gemeinsame Ressourcen**: OCR ist der Engpass und wird deshalb prozessweit begrenzt, nicht je
  Haushalt. Die CLI braucht bei mehreren Haushalten `--haushalt ID`; `docushelf households` listet sie.

## Sicherheit

- Weboberfläche nur im privaten Netz (VPN oder Reverse-Proxy mit IP-Allowlist), Login per OIDC,
  Zugriff nur für freigegebene Nutzer/Gruppen bzw. Haushalte (Standard: niemand, bis konfiguriert).
  Sitzungen werden bei jeder Anfrage gegen die aktuelle Konfiguration geprüft; Sitzungen des lokalen
  Admins tragen einen Fingerabdruck des Passwort-Hashes, eine Passwortänderung beendet sie.
- Sitzungs-Cookie `HttpOnly`, `SameSite=Lax`, `Secure`; CSRF-Token für alle schreibenden
  Formulare; strenge Content-Security-Policy (neben `'self'` nur `manifest-src` und `worker-src` für die
  Handy-App), kein Inline-JavaScript.
- API-Tokens (gehasht in `config/tokens.yaml`, je Haushalt) für Skripte und iOS-Kurzbefehle. Ein im
  Browser erzeugtes Token merkt sich seinen Besitzer und wird abgelehnt, sobald dieser nicht mehr
  zugelassen wäre. Tokens verwalten und den Papierkorb endgültig leeren geht nur mit Browser-Sitzung.
- SMTP-Empfänger: nur bekannte Empfängeradressen (je Haushalt konfiguriert), nur Allowlist-IPs, nie Relay.
- IMAP ohne TLS nur mit ausdrücklicher Erlaubnis: Ohne Implicit-TLS ist STARTTLS Pflicht, ein
  Passwort geht nie im Klartext über das Netz.
- XMP wird ohne Entity-Auflösung geparst (kein XXE), Pfade werden strikt geprüft
  (kein Ausbrechen aus dem Archiv).

## Betrieb

Siehe `DEPLOY.md`. Kurz: ein Compose-Stack, Daten unter einem Volume, Index darf
weg, Backups für `archiv/`, `eingang/`, `config/`, `protokoll/` – bei mehreren Haushalten je Haushalt
unter `haushalte/<id>/` plus die globale `config/` (u. a. `households.yaml`, `secret.key`).
