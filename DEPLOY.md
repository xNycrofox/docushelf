# DocuShelf betreiben

Diese Anleitung beschreibt den Dauerbetrieb mit Docker Compose: Reverse Proxy, Anmeldung per
SSO (Authentik), Datenablage und Backup, die Scan-Kanäle samt Netzwerkweg vom Drucker zum Server,
Sprachmodell und automatische Ablage, mehrere Haushalte auf einer Installation, die Migration von
Paperless-ngx, Updates und Fehlersuche.

Alle Namen und Adressen sind Beispiele – bitte durch die eigenen ersetzen:

| Beispiel | Bedeutung |
|---|---|
| `docs.example.net` | Adresse der DocuShelf-Weboberfläche |
| `auth.example.net` | Authentik (OIDC-Provider) |
| `100.64.0.10` | VPN-Adresse des Servers (z. B. Tailscale/Headscale, WireGuard) |
| `192.168.1.0/24` | Heimnetz mit dem Drucker |
| `192.168.1.20` | feste IP des Druckers |
| `192.168.1.2` | Subnetz-Router im Heimnetz (z. B. ein Raspberry Pi mit Tailscale) |

## Inhalt

1. [Überblick](#1-überblick)
2. [Installation](#2-installation)
3. [Reverse Proxy](#3-reverse-proxy)
4. [Anmeldung mit Authentik (OIDC)](#4-anmeldung-mit-authentik-oidc)
5. [Daten und Backup](#5-daten-und-backup)
6. [Eingangskanäle](#6-eingangskanäle)
7. [Sprachmodell (KI) und automatische Ablage](#7-sprachmodell-ki-und-automatische-ablage)
8. [Netzwerk: vom Drucker zum Server über das VPN](#8-netzwerk-vom-drucker-zum-server-über-das-vpn)
9. [Migration von Paperless-ngx](#9-migration-von-paperless-ngx)
10. [Updates und Neuaufbau des Index](#10-updates-und-neuaufbau-des-index)
11. [Fehlersuche](#11-fehlersuche)
12. [Sicherheit im Überblick](#12-sicherheit-im-überblick)
13. [Mehrere Haushalte](#13-mehrere-haushalte)
14. [Einstellungen in der Weboberfläche](#14-einstellungen-in-der-weboberfläche)

---

## 1. Überblick

Der Stack in `deploy/compose.yaml`:

| Dienst | Aufgabe | Ports (Host) | Profil |
|---|---|---|---|
| `init` | bereitet das Datenverzeichnis vor (Besitzer, Eingangsordner) und beendet sich | – | immer |
| `docushelf` | Weboberfläche, API, Verarbeitung (OCR), Abgleich, IMAP-Abruf, SMTP-Empfänger | `127.0.0.1:8000` (Web), `<DOCUSHELF_SMTP_BIND>:2525` (SMTP) | immer |
| `samba` | Freigaben `scan` (Drucker, nur Schreiben) und `archiv` (Haushalt) | `<DOCUSHELF_PRIVATE_IP>:445` | `smb` |
| `ftp` | Scan-to-FTP, nur Upload | `<DOCUSHELF_PRIVATE_IP>:21`, `…:30000-30009` | `ftp` |

- DocuShelf läuft als **UID 1000**; alle Dateien im Datenverzeichnis gehören diesem Benutzer.
- Samba und FTP sind einzeln zuschaltbar: `docker compose --profile smb --profile ftp up -d`.
- Die Scan-Kanäle werden ausschließlich auf der **VPN-Adresse** veröffentlicht, nie öffentlich:
  SMB und FTP auf `DOCUSHELF_PRIVATE_IP`, der SMTP-Empfänger im App-Container auf
  `DOCUSHELF_SMTP_BIND`. Ohne Angabe lauschen sie nur auf `127.0.0.1` – dann startet der Stack auf
  jedem Host, und die App hängt nicht vom VPN ab, solange Scan-to-Mail nicht genutzt wird.
- Alle Container haben Healthchecks; `docker compose ps` zeigt den Zustand.
- Ein Sprachmodell (Ollama) kann als weiterer Dienst dazukommen – optional, siehe Abschnitt 7.
- Eine Datenbank-Sicherung ist nicht nötig: Der SQLite-Index ist nur ein Cache.

## 2. Installation

Voraussetzungen: Docker Engine mit Compose v2.24 oder neuer, ein Reverse Proxy mit TLS
(Abschnitt 3), optional Authentik (Abschnitt 4).

```sh
git clone <repository-url> /opt/docushelf
cd /opt/docushelf/deploy
cp .env.example .env
```

In der `.env` mindestens setzen:

- `DOCUSHELF_DATA_PATH` – Datenverzeichnis auf dem Host, am besten absolut, z. B. `/srv/docushelf/data`.
  Es muss nicht existieren: der Dienst `init` legt es an und übergibt es an UID 1000.
- `DOCUSHELF_BASE_URL` – die Adresse im Browser, z. B. `https://docs.example.net`.
- Anmeldung: SSO (Abschnitt 4) und/oder den Notfallzugang (siehe unten).

Empfohlen:

- `DOCUSHELF_OWNER_NAMES` – die Namen der Haushaltsmitglieder, kommagetrennt, z. B.
  `DOCUSHELF_OWNER_NAMES=Alex Beispiel, Sam Beispiel`. Diese Namen werden nie als Absender
  vorgeschlagen. Ohne die Angabe hält DocuShelf den eigenen Namen auf Briefen ohne „Herrn/Frau“
  leicht für den Absender (gerade nach dem Paperless-Import, wenn er als Korrespondent bekannt ist).

Nur für die Scan-Kanäle (Abschnitt 6 und 8), erst wenn sie genutzt werden:

- `DOCUSHELF_PRIVATE_IP` – VPN-Adresse des Servers für SMB und FTP.
- `DOCUSHELF_SMTP_BIND` – dieselbe Adresse für den SMTP-Empfänger (Scan-to-Mail).

Beide stehen in der `.env.example` bewusst auskommentiert: Eine Adresse, die der Host (noch)
nicht hat, verhindert den Start des Containers („cannot assign requested address“). Sind sie
gesetzt, muss das VPN beim Booten vor Docker oben sein. Dafür Docker nach dem VPN starten
(`systemctl edit docker.service` → im Abschnitt `[Unit]` `After=tailscaled.service` und
`Wants=tailscaled.service`) oder das Binden an noch nicht vorhandene Adressen erlauben
(`sysctl net.ipv4.ip_nonlocal_bind=1`, dauerhaft in `/etc/sysctl.d/`).

Image bauen, Notfallpasswort erzeugen, starten:

```sh
docker compose build
docker compose run --rm docushelf docushelf hash-password --env
# Ausgabe: DOCUSHELF_ADMIN_PASSWORD_HASH='scrypt$…'  → so (mit den einfachen Anführungszeichen) in die .env
docker compose up -d
docker compose ps
docker compose exec docushelf docushelf status
```

> Der Passwort-Hash enthält `$`. In der `.env` deshalb in **einfache** Anführungszeichen setzen;
> direkt in einer Compose-Datei jedes `$` als `$$` schreiben. Alternativ den Hash in eine Datei
> legen und `DOCUSHELF_ADMIN_PASSWORD_HASH_FILE` verwenden.

Das Image wird hier auf dem Server gebaut. Wer es aus einer Registry beziehen will, setzt
`DOCUSHELF_IMAGE=registry.example.net/docushelf:latest` und verwendet `docker compose pull`.

**Ein Archiv an anderer Stelle** (z. B. auf einem NAS): in `compose.yaml` zusätzlich einbinden
(`- /mnt/nas/archiv:/archiv`) und `DOCUSHELF_ARCHIVE_DIR=/archiv` setzen. Fehlt das Verzeichnis
beim Start – etwa weil die Freigabe nicht eingehängt ist –, startet DocuShelf bewusst nicht, statt
ein leeres Archiv für das echte zu halten. Für die Samba-Freigabe `archiv` denselben Pfad einbinden.

## 3. Reverse Proxy

DocuShelf spricht selbst nur HTTP auf Port 8000. TLS und die Beschränkung auf das VPN übernimmt
der Reverse Proxy. Die Anmeldung macht DocuShelf selbst (OIDC) – ein vorgeschaltetes Forward-Auth
ist nicht nötig.

### Traefik (im selben Docker-Host)

In `compose.yaml` beim Dienst `docushelf` die Labels und Netze einkommentieren und anpassen:

```yaml
    labels:
      - traefik.enable=true
      - traefik.docker.network=proxy
      - traefik.http.routers.docushelf.rule=Host(`docs.example.net`)
      - traefik.http.routers.docushelf.entrypoints=websecure
      - traefik.http.routers.docushelf.tls=true
      - traefik.http.routers.docushelf.tls.certresolver=letsencrypt
      - traefik.http.routers.docushelf.middlewares=vpn-only@file
      - traefik.http.services.docushelf.loadbalancer.server.port=8000
    networks:
      - default
      - proxy

networks:
  proxy:
    external: true
```

`proxy` ist das Docker-Netz, in dem Traefik hängt; `websecure` der HTTPS-Entrypoint;
`letsencrypt` der Zertifikats-Resolver. Die Middleware `vpn-only` lässt nur Adressen aus dem VPN
zu, z. B. im dynamischen Datei-Provider von Traefik (v3):

```yaml
http:
  middlewares:
    vpn-only:
      ipAllowList:
        sourceRange:
          - 100.64.0.0/10          # Tailscale/Headscale IPv4
          - fd7a:115c:a1e0::/48    # Tailscale/Headscale IPv6
```

Alternativ hört nur ein eigener Entrypoint auf der VPN-Adresse – dann entfällt die Middleware.

### Reverse Proxy auf dem Host (Caddy, nginx …)

Ohne Docker-Netz zeigt der Proxy auf `http://127.0.0.1:8000` (Bindung über `DOCUSHELF_HTTP_BIND`
und `DOCUSHELF_HTTP_PORT`). Bei nginx die Upload-Größe anheben, sonst scheitern große Scans:
`client_max_body_size 210m;` (etwas über `DOCUSHELF_MAX_UPLOAD_MB`).

### Einstellungen in DocuShelf

- `DOCUSHELF_BASE_URL=https://docs.example.net` – mit `https://` werden Sitzungs-Cookies nur über
  HTTPS gesendet.
- `DOCUSHELF_TRUSTED_PROXIES` – von wem `X-Forwarded-*`-Header akzeptiert werden. Standard sind
  Loopback und private Netze (`127.0.0.1`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`,
  `100.64.0.0/10`, `fc00::/7`); das deckt Docker-Netze ab. Enger geht es mit dem Netz des Proxys,
  z. B. `172.18.0.0/16`, oder `127.0.0.1` für einen Proxy auf dem Host.

## 4. Anmeldung mit Authentik (OIDC)

### In Authentik

1. **Provider anlegen:** *Anwendungen → Provider → Erstellen → OAuth2/OpenID-Provider*
   - Name: `DocuShelf`
   - Autorisierungs-Flow: z. B. `default-provider-authorization-implicit-consent`
   - Client-Typ: **Vertraulich** (confidential); Client-ID und Client-Secret notieren
   - Redirect-URIs: `https://docs.example.net/auth/callback` (Modus „strict“)
   - Signaturschlüssel: ein Zertifikat auswählen (z. B. das selbstsignierte von Authentik),
     damit die Tokens mit RS256 signiert werden
   - Scopes (erweiterte Einstellungen): `openid`, `email`, `profile` – die Standard-Zuordnung für
     `profile` liefert bei Authentik auch den Claim `groups` (Liste der Gruppennamen)
2. **Anwendung anlegen:** *Anwendungen → Anwendungen → Erstellen*
   - Name: `DocuShelf`, Slug: `docushelf`, Provider: den eben angelegten
   - Start-URL: `https://docs.example.net/`
   - Unter *Richtlinien/Gruppen-Bindungen* die Gruppe binden, die DocuShelf nutzen darf
     (z. B. `docushelf-users`) – dann lässt Authentik nur Mitglieder dieser Gruppe überhaupt
     zur Anmeldung bei DocuShelf durch. Zusammen mit `DOCUSHELF_ALLOWED_GROUPS` (unten) ist das
     die eigentliche Zugangskontrolle: Wer in der Gruppe ist, bestimmt nur der Authentik-Admin.
3. Der Issuer steht auf der Provider-Seite („OpenID Configuration Issuer“):
   `https://auth.example.net/application/o/docushelf/`.

### In der `.env`

```sh
DOCUSHELF_BASE_URL=https://docs.example.net
DOCUSHELF_OIDC_ISSUER=https://auth.example.net/application/o/docushelf/
DOCUSHELF_OIDC_CLIENT_ID=<Client-ID>
DOCUSHELF_OIDC_CLIENT_SECRET=<Client-Secret>
# wer hinein darf – am besten über die Gruppe, an die auch die Anwendung gebunden ist:
DOCUSHELF_ALLOWED_GROUPS=docushelf-users
# einzelne Konten nur zusätzlich (Benutzernamen bei Authentik, siehe unten):
# DOCUSHELF_ALLOWED_USERS=alex,sam
```

- DocuShelf lädt die Konfiguration von `<Issuer>/.well-known/openid-configuration`, nutzt PKCE
  (S256), `state` und `nonce`. Der Container muss den Issuer erreichen (DNS und Netz).
- Benutzername ist `preferred_username`, sonst die (bestätigte) E-Mail-Adresse.
- **Gruppe vor Einzelkonten:** Einträge in `DOCUSHELF_ALLOWED_USERS` werden mit dem Benutzernamen bei
  Authentik (`preferred_username`) und mit der bestätigten E-Mail-Adresse verglichen. Ein Eintrag
  wie `alex@example.net` passt also auch auf ein *anderes* Konto, das diesen Text als Benutzernamen
  trägt. Das ist nur sicher, solange niemand seinen Benutzernamen selbst wählen oder ändern kann –
  bei Authentik der Standard; wer Selbstregistrierung oder Umbenennen erlaubt, verwendet nur
  `DOCUSHELF_ALLOWED_GROUPS` und die Gruppen-Bindung der Anwendung.
- Sind weder `DOCUSHELF_ALLOWED_USERS` noch `DOCUSHELF_ALLOWED_GROUPS` gesetzt, kommt niemand
  hinein (außer mit `DOCUSHELF_ALLOW_ALL_OIDC_USERS=true`).
- Beim Abmelden leitet DocuShelf zum End-Session-Endpunkt von Authentik weiter.
- Die Uhren von Server und Authentik müssen stimmen (NTP), sonst gelten Tokens als ungültig.

### Sitzungen und Abmelden

Eine Anmeldung gilt bis zu `DOCUSHELF_SESSION_MAX_AGE` (Standard 14 Tage). Die Sitzung steckt
in einem signierten Cookie; auf dem Server wird nichts gespeichert. Daraus folgt:

- **Gruppen aus Authentik wirken bei der nächsten Anmeldung.** Wird jemand in Authentik aus der
  Gruppe genommen, bleibt die laufende Sitzung bis zum Ablauf gültig. Sofort wirkt dagegen eine
  Änderung von `DOCUSHELF_ALLOWED_USERS`/`_GROUPS` (nach `docker compose up -d`): Sitzungen von
  Konten, die danach nicht mehr passen, enden beim nächsten Aufruf. Wem 14 Tage zu lang sind,
  setzt z. B. `DOCUSHELF_SESSION_MAX_AGE=172800` (2 Tage).
- **Abmelden** beendet nur die Sitzung im eigenen Browser.
- **Notfallzugang:** Ein neuer `DOCUSHELF_ADMIN_PASSWORD_HASH` (danach `docker compose up -d`)
  beendet alle Sitzungen des Notfallzugangs.
- **Alle abmelden** (z. B. wenn ein angemeldetes Gerät verloren ging): einen neuen Schlüssel
  verwenden und neu starten. Ist `DOCUSHELF_SECRET_KEY` in der `.env` gesetzt, dort einen neuen
  Wert eintragen (z. B. `openssl rand -base64 48`); sonst `config/secret.key` im Datenverzeichnis
  löschen – er wird beim Start neu erzeugt. Danach `docker compose up -d` bzw.
  `docker compose restart docushelf`. API-Tokens sind davon nicht betroffen.

### Notfallzugang

Der lokale Benutzer `DOCUSHELF_ADMIN_USER` (Standard `admin`) mit
`DOCUSHELF_ADMIN_PASSWORD_HASH` funktioniert auch, wenn Authentik nicht erreichbar ist. Ohne Hash
ist der Notfallzugang aus. Anmeldeversuche werden gebremst. Wer das Passwort ändert (neuen Hash
eintragen, `docker compose up -d`), beendet damit alle Sitzungen des Notfallzugangs.

### API-Tokens

Für Skripte und Kurzbefehle (z. B. „Teilen → An DocuShelf senden“ auf dem iPhone). Am besten
erstellt jede Person ihr Token selbst in der Oberfläche unter *Einstellungen → Handy & Tokens*. Ein
so erstelltes Token ist an das Konto gebunden: Es gilt nur, solange dieses Konto nach
`DOCUSHELF_ALLOWED_USERS`/`_GROUPS` bzw. als Notfallzugang noch hinein dürfte.

Auf der Kommandozeile geht es auch – solche Tokens sind aber an kein Konto gebunden und gelten,
bis sie widerrufen werden (in der Oberfläche als „ohne Kontobindung“ markiert):

```sh
docker compose exec docushelf docushelf token create "Scanner-Skript" --user alex
docker compose exec docushelf docushelf token list
docker compose exec docushelf docushelf token revoke <ID>
```

Das Token wird nur einmal angezeigt. Hochladen:

```sh
curl -H "Authorization: Bearer dsk_…" -F "files=@rechnung.pdf" https://docs.example.net/api/v1/upload
```

Ein Token hat dieselben Rechte wie sein Konto, mit zwei Ausnahmen: Tokens verwalten und Einträge
im Papierkorb endgültig löschen geht nur nach einer Anmeldung im Browser. Ein verlorenes Token
kann also nichts unwiederbringlich löschen – Gelöschtes landet im Papierkorb (standardmäßig
30 Tage wiederherstellbar). Lesen, hochladen und ändern kann es aber alles, und Tokens laufen
nicht von selbst ab: verlorene Tokens deshalb sofort widerrufen.

**Gerät verloren oder jemand verlässt den Haushalt:**

1. Die Tokens der Person widerrufen (*Einstellungen → Handy & Tokens* oder `docushelf token revoke <ID>`).
   Das ist auch für gebundene Tokens nötig, wenn die Person nur in Authentik aus der Gruppe
   entfernt wird: Ein Token kennt die Gruppen vom Zeitpunkt seiner Erstellung.
2. Die Person aus der Gruppe in Authentik bzw. aus `DOCUSHELF_ALLOWED_USERS` nehmen
   (`.env` ändern, `docker compose up -d`).
3. Das Gerät aus dem VPN entfernen (z. B. in Tailscale/Headscale die Node löschen).
4. War auf dem Gerät eine Sitzung im Browser offen: alle abmelden (siehe „Sitzungen und Abmelden“).

## 5. Daten und Backup

```
<DOCUSHELF_DATA_PATH>/
├── archiv/        die Wahrheit – dein Ordnerbaum                       → sichern
├── eingang/       Drop-Zonen, Eingang, Fehler, Originale (erledigt/)   → sichern
├── config/        rules.yaml, tokens.yaml, paperless-import.yaml,
│                  settings.yaml, secret.key                            → sichern
├── protokoll/     Audit-Log, auch jede automatische Ablage             → sichern
├── papierkorb/    gelöschte Dokumente (Standard 30 Tage)               → empfohlen
└── index/         SQLite-Suchindex, Vorschaubilder                     → nicht nötig
```

- **Was gesichert werden muss:** `archiv/`, `eingang/`, `config/`, `protokoll/` – und
  `papierkorb/`, wenn versehentlich gelöschte Dokumente auch nach einer Wiederherstellung noch
  zurückholbar sein sollen. Dazu die `.env` und `samba.env` (enthalten Zugangsdaten – verschlüsselt
  sichern).
- **`index/` muss nicht gesichert werden.** Fehlt er, baut DocuShelf ihn beim Start aus den Dateien
  neu auf.
- Sicherungen im laufenden Betrieb sind in Ordnung: DocuShelf schreibt Dateien atomar
  (temporäre Datei + Umbenennen) und überschreibt nie etwas. Temporäre Dateien heißen
  `.docushelf-*.part` und können ausgeschlossen werden.
- Mit mehreren Haushalten (Abschnitt 13) liegt jeder weitere Haushalt unter `haushalte/<id>/`
  mit demselben Aufbau; die Sicherung des ganzen Datenverzeichnisses deckt alle ab.
- Alle Metadaten stecken in den PDFs (XMP) und Dateinamen. Auch ohne DocuShelf bleibt das Archiv
  ein lesbarer Ordnerbaum. Ausnahme: Digital signierte und mit Passwort geschützte PDFs verändert
  DocuShelf nie (die Signatur bliebe sonst nicht gültig) – bei ihnen stehen Titel und Datum nur im
  Dateinamen, Tags und Absender werden nicht gespeichert.

Beispiel für borgmatic:

```yaml
source_directories:
  - /srv/docushelf/data
  - /opt/docushelf/deploy/.env
  - /opt/docushelf/deploy/samba.env
exclude_patterns:
  - /srv/docushelf/data/index
  - "*.docushelf-*.part"
```

**Wiederherstellen:** Verzeichnisse zurückspielen, Besitzer prüfen (`chown -R 1000:1000 …`),
`docker compose up -d`. Der Index entsteht beim Start neu; ausdrücklich geht das auch mit
`docushelf rebuild` (siehe Abschnitt 10).

## 6. Eingangskanäle

Jeder Kanal schreibt nur in seinen eigenen Ordner unter `eingang/quellen/`. (Mit mehreren
Haushalten hat jeder Haushalt eigene Ordner – siehe Abschnitt 13.) Keiner hat Zugriff auf
das Archiv (außer der Samba-Freigabe `archiv`, die genau dafür da ist). Was ankommt, erscheint
sofort im Eingang der Oberfläche, mit Quelle.

### Web-Upload und API

Drag & Drop in der Oberfläche oder per API mit Token (Abschnitt 4). Mehrere Bilder können zu einem
PDF zusammengefasst werden (`combine=true`).

### Überwachte Ordner

`DOCUSHELF_WATCH_FOLDERS=scanner=/watch/scanner` und den Ordner in `compose.yaml` einbinden. Dateien
werden nach der Übernahme entfernt.

### SMB (Scan-to-Folder) – Profil `smb`

Der Samba-Container (Image `ghcr.io/servercontainers/samba`) stellt zwei Freigaben bereit:

| Freigabe | Zweck | Zugriff |
|---|---|---|
| `scan` | Briefkasten für den Drucker → `eingang/quellen/smb` | Konto `scanner`, nur Schreiben |
| `archiv` | Archiv im Finder/Explorer bearbeiten → `archiv/` | Konten aus `SAMBA_ARCHIVE_USERS` |

Einrichten:

```sh
cp samba.env.example samba.env   # Passwörter setzen
# in der .env:
#   DOCUSHELF_PRIVATE_IP=100.64.0.10
#   SAMBA_HOSTS_ALLOW="192.168.1.20 100.64.0.0/10"   (Drucker und VPN-Geräte)
#   SAMBA_ARCHIVE_USERS="alex sam"
docker compose --profile smb up -d
```

- **Nur Schreiben für den Drucker:** Der Ordner `eingang/quellen/smb` gehört DocuShelf und hat den
  Modus `1733` (setzt der Dienst `init`). Das Konto `scanner` darf darin Dateien anlegen, den
  Ordner aber weder auflisten noch fremde Dateien lesen oder löschen – das erzwingt der
  Kernel, unabhängig von Samba-Einstellungen. DocuShelf holt neue Dateien nach wenigen Sekunden ab.
  Warum nicht „write list“ + „hide unreadable“? Dann müsste der Drucker auflisten dürfen, und
  versteckt würden nur Dateien, die er nicht lesen kann – seine eigenen Scans könnte er trotzdem
  sehen. Die Verzeichnisrechte sind einfacher und strenger.
- Manche Drucker testen beim Einrichten, ob sie den Ordner auflisten können, und melden sonst
  einen Fehler. Dann in der .env `SAMBA_SCAN_DIR_MODE=2770` setzen: Der Drucker sieht danach seine
  eigenen, noch nicht abgeholten Scans – sonst nichts.
- **Archiv-Freigabe:** Alle Dateien gehören dem Dienstkonto `docushelf` (UID 1000), egal wer sie
  anlegt – DocuShelf kann sie so umbenennen und mit Metadaten versehen. macOS-Metadaten landen
  in erweiterten Attributen (`vfs_fruit`, `streams_xattr`); `._*`, `.DS_Store`, `Thumbs.db` und
  `desktop.ini` werden abgewiesen, damit das Archiv sauber bleibt.
- Mindestens SMB 2.1, **kein SMB1**, kein NetBIOS, keine Drucker; Zugriff nur von
  `SAMBA_HOSTS_ALLOW`.
- Verbinden: macOS Finder → *Gehe zu → Mit Server verbinden* → `smb://100.64.0.10/archiv`;
  Windows Explorer → `\\100.64.0.10\archiv`.
- Nach dem ersten Start die erzeugte Konfiguration prüfen: `docker compose exec samba testparm -s`
  (Freigaben, `server min protocol`, `hosts allow`) und `docker compose exec samba id docushelf`
  (muss `uid=1000 gid=1000` zeigen).
- Kann der Drucker nur SMB1 oder NTLMv1: für ihn nicht SMB verwenden, sondern FTP oder Mail.

Im Drucker eintragen: Server **als IP-Adresse** (`100.64.0.10`), Freigabe/Pfad `scan`,
Benutzer `scanner`, Passwort aus `samba.env`.

### FTP (Scan-to-FTP) – Profil `ftp`

Ein kleiner vsftpd-Container (`deploy/ftp/`): ein Konto (`FTP_USER`, Standard `scanner`), auf
`eingang/quellen/ftp` eingesperrt, nur Upload (kein Download, kein Löschen, Verzeichnislisten
bleiben leer), kein anonymer Zugang. Hochgeladene Dateien gehören UID 1000.

```sh
# in der .env:
#   FTP_PASSWORD='…'
#   DOCUSHELF_PRIVATE_IP=100.64.0.10        (auch die Adresse für den passiven Modus)
#   FTP_PASV_MIN_PORT=30000 / FTP_PASV_MAX_PORT=30009
docker compose --profile ftp up -d
```

- **Passiver Modus** ist Pflicht: Der Server nennt dem Drucker `DOCUSHELF_PRIVATE_IP` und einen
  Port aus dem Bereich 30000–30009. Port 21 und der Bereich müssen im VPN freigegeben sein.
- Klartext-FTP ist vertretbar, weil die Strecke ab dem Subnetz-Router im VPN-Tunnel läuft.
  **FTPS** (explizit, Port 21) wird aktiv, sobald `tls.crt` und `tls.key` in `deploy/certs/`
  liegen und der Mount in `compose.yaml` einkommentiert ist; `FTP_TLS_REQUIRED=yes` erzwingt es.
- Test: `curl -T test.pdf --user scanner:… ftp://100.64.0.10/`

### SMTP-Empfänger (Scan-to-Mail direkt vom Drucker)

Läuft im App-Container auf Port 2525, veröffentlicht auf `DOCUSHELF_SMTP_BIND` (Standard
`127.0.0.1`, also von außen nicht erreichbar). Für den Drucker die VPN-Adresse eintragen:

```sh
DOCUSHELF_SMTP_ENABLED=true
DOCUSHELF_SMTP_BIND=100.64.0.10
DOCUSHELF_SMTP_RECIPIENT=scan@docs.example.net
DOCUSHELF_SMTP_ALLOWED_NETWORKS=192.168.1.20/32
```

Mit gesetztem `DOCUSHELF_SMTP_BIND` startet auch die Weboberfläche erst, wenn das VPN oben ist
(siehe Abschnitt 2 zur Startreihenfolge).

- Angenommen wird nur genau diese Empfängeradresse, nur von den erlaubten Adressen – **kein Relay**.
- Im Drucker: SMTP-Server `100.64.0.10`, Port `2525`, ohne TLS; Absender beliebig; Empfänger wie
  oben. Verlangt der Drucker eine Anmeldung: `DOCUSHELF_SMTP_USER`/`_PASSWORD` setzen.

### Mail-Abruf (IMAP)

Ein eigenes Postfach (z. B. `dokumente@example.net`), in das Rechnungen weitergeleitet werden.
Anhänge (PDFs, Bilder) landen im Eingang, die Mail wird danach in einen IMAP-Ordner verschoben –
nie gelöscht. Nur Absender aus `DOCUSHELF_IMAP_ALLOWED_SENDERS` werden angenommen.

```sh
DOCUSHELF_IMAP_ENABLED=true
DOCUSHELF_IMAP_HOST=imap.example.net
DOCUSHELF_IMAP_USER=dokumente@example.net
DOCUSHELF_IMAP_PASSWORD='…'
DOCUSHELF_IMAP_ALLOWED_SENDERS=alex@example.net,sam@example.net,@example.net
```

Das ist auch der robuste Ausweichweg für den Drucker: Scan-to-Mail über einen gewöhnlichen
Mailanbieter funktioniert unabhängig vom VPN. Beide Wege können gleichzeitig eingerichtet sein.

### Eingangskanäle in der Weboberfläche (`channels.yaml`)

Unter **Einstellungen → Eingangskanäle** (`/einstellungen/kanaele`) schalten Admins
die Kanäle eines Haushalts ein und aus und richten sie ein – ohne Neustart. Admins sind mit einem
Haushalt das lokale Notfallkonto sowie die Konten aus `DOCUSHELF_ADMIN_USERS` und die Mitglieder
der Gruppen aus `DOCUSHELF_ADMIN_GROUPS`; mit mehreren Haushalten die Admins aus Abschnitt 13
(`admins`/`admin_groups` in `households.yaml`). Andere sehen den Knopf nicht und bekommen 403.

| Kanal | In der Oberfläche |
|---|---|
| Hochladen | immer an (nur zur Info) |
| Mail-Postfächer (IMAP) | beliebig viele, je Name, Server, Port, SSL/TLS oder STARTTLS, Benutzer, Passwort, Ordner (abrufen, Verarbeitetes, Abgelehntes, ohne Anhang), erlaubte Absender, Intervall, an/aus, optional eine **Person**; Knopf **Verbindung testen** (anmelden, Ordner nur lesend öffnen, abmelden) |
| Scan per Mail (SMTP) | an/aus, Empfängeradressen des Haushalts, je Adresse optional eine Person (z. B. `scan-erika@…`) |
| Scanner-Ordner (SMB/FTP) | je Kanal an/aus, Unterordner je Person unter `eingang/quellen/<smb\|ftp>/<ordner>/`, je Ordner an/aus |
| Watch-Ordner | Name, Pfad, „Dateien nach der Übernahme entfernen“, an/aus |

- **Gespeichert** wird je Haushalt in `config/channels.yaml` (bei mehreren Haushalten
  `haushalte/<id>/config/channels.yaml`), atomar, die vorige Fassung als `channels.yaml.bak`.
  Vor dem Ersetzen wird die neue Datei mit demselben Prüfer gelesen wie beim Start; ist etwas
  ungültig, bleibt alles, wie es war, und die Seite zeigt den Fehler auf Deutsch.
- **Passwörter** (IMAP) stehen nie in `channels.yaml`, sondern je Postfach in
  `config/secrets/imap-<name>` (Datei `600`, Ordner `700`). Die Oberfläche zeigt nur „gesetzt“;
  ein neues Passwort ersetzt das alte, ein leeres Feld behält es.
- **Umgebungsvariablen bleiben Voreinstellung:** Solange es keine `channels.yaml` gibt, gilt alles
  aus der `.env` (und `households.yaml`) wie bisher. Mit der ersten Änderung in der Oberfläche
  übernimmt DocuShelf diese Werte in die Datei – auch das IMAP-Passwort aus der `.env` wandert
  dabei nach `config/secrets/`. Ab dann gilt für diesen Haushalt **nur noch die Datei**:
  `DOCUSHELF_IMAP_*`, `DOCUSHELF_WATCH_FOLDERS`, `DOCUSHELF_SMTP_RECIPIENT` bzw.
  `imap`/`smtp_recipients`/`watch_folders` aus `households.yaml` werden für ihn ignoriert. Zurück
  zu den Umgebungsvariablen: Datei löschen und neu starten.
- **Was die Oberfläche nicht kann:** Den SMTP-Server selbst schaltet weiterhin
  `DOCUSHELF_SMTP_ENABLED` ein (Port, `DOCUSHELF_SMTP_ALLOWED_NETWORKS`, `DOCUSHELF_SMTP_BIND`); ist
  er aus, zeigt die Seite einen deutlichen Hinweis. Samba- und FTP-Container laufen außerhalb von
  DocuShelf (Profile `smb`/`ftp`); die Seite zeigt nur, was am Drucker einzutragen ist. Neue
  Watch-Ordner muss der Container sehen können (Mount in `compose.yaml`); Pfade in den Daten von
  DocuShelf oder eines anderen Haushalts werden abgelehnt.
- **Personen und Endpunkte:** Eine Person ist ein Benutzer des Haushalts (wie unter `users`; ohne
  eingetragene Benutzer ist jeder Name erlaubt). Dokumente aus ihrem Postfach, an ihre Scan-Adresse
  oder aus ihrem Scanner-Ordner bekommen sie als `owner` im Kontext (`item.json`) – für spätere
  Regeln – und erscheinen im Eingang als „von Erika · Scanner“. Ein Scanner-Ordner wird mit
  denselben Rechten angelegt wie `eingang/quellen/<kanal>` selbst (bei SMB also `1733`), damit
  das Konto `scanner` hineinschreiben kann. Ist ein Ordner ausgeschaltet, bleiben seine Dateien
  liegen, bis er wieder an ist; entfernt man ihn, bleibt der Ordner bestehen und Dateien darin
  werden ohne Person übernommen.
  Am Drucker: SMB `\\100.64.0.10\scan\erika`, FTP `ftp://100.64.0.10/erika`.
- **Live:** Speichern oder Umschalten startet nur die Mail-Abrufe und die Eingangsüberwachung
  dieses Haushalts neu; Adressen für Scan per Mail gelten sofort. Jede Änderung steht im Protokoll
  („Eingangskanäle geändert“, Aktion `channels_change`) – ohne Passwörter.
- Eine von Hand kaputt editierte `channels.yaml` verhindert den Start mit einer deutschen
  Fehlermeldung (wie `households.yaml`); `channels.yaml.bak` ist die letzte gute Fassung.

### iPhone und iPad: Kurzbefehl ohne Mac

Unter **Einstellungen → Handy & Tokens** gibt es für iPhone/iPad den Knopf
**Kurzbefehl installieren** (`/kurzbefehl`, nur angemeldet), daneben die Serveradresse zum
Kopieren und **Token für dieses Handy erstellen** (ein Token `iPhone-Kurzbefehl` für das eigene
Konto, wird einmal angezeigt). Nutzer brauchen dafür keinen Mac. Der Betreiber stellt den
Kurzbefehl einmal bereit – eine der beiden Möglichkeiten:

- **Datei:** auf irgendeinem Mac einmal erzeugen und signieren (`contrib/ios/README.md`) und als
  `config/shortcuts/DocuShelf senden.shortcut` im Datenverzeichnis ablegen. `/kurzbefehl` liefert
  sie als Download aus.
- **iCloud-Link:** Kurzbefehl in der App Kurzbefehle teilen → „iCloud-Link kopieren“ und in die
  `.env`: `DOCUSHELF_IOS_SHORTCUT_URL=https://www.icloud.com/shortcuts/…` (nur `https://`).
  `/kurzbefehl` leitet dorthin weiter.

Liegt eine Datei vor, hat sie Vorrang. Ohne beides fehlt der Knopf, und die Seite verweist auf die
Anleitung (dort steht auch, wie man den Kurzbefehl ganz ohne Mac direkt am iPhone baut).

## 7. Sprachmodell (KI) und automatische Ablage

Beides ist optional und zunächst ausgeschaltet. DocuShelf macht Ablagevorschläge auch ohne KI –
aus Regeln, aus der Ähnlichkeit zu bereits abgelegten Dokumenten und aus dem Absender.

### Sprachmodell

Ein Sprachmodell hilft vor allem bei Titeln und bei Dokumenten, zu denen es im Archiv noch nichts
Ähnliches gibt. Es bekommt den Text des Dokuments, die Ordnerliste mit der Zahl der Dokumente je
Ordner, Beispiel-Dateinamen aus den wahrscheinlich passenden Ordnern, die ähnlichsten abgelegten
Dokumente und die automatisch erkannten Angaben (Datumsangaben, Absender, Dokumenttyp, Beträge,
Kunden- und Vertragsnummern). Es antwortet mit Ordner, Titel, Datum, Absender und einer
Selbsteinschätzung. DocuShelf prüft jede Antwort: Der Ordner muss existieren oder sich als neuer
Ordner in die vorhandene Struktur einfügen, das Datum muss im Dokument stehen, der Titel wird
bereinigt („Dokumentart Absender Zeitraum“, ohne Datum – das steht vorn im Dateinamen).

> **Wohin der Text geht:** Der Inhalt jedes Dokuments wird an die Adresse in `DOCUSHELF_LLM_URL`
> geschickt – und nur dorthin. Mit dem Standard, einem Ollama auf deinem eigenen Server, verlässt
> nichts das Haus. Trägst du einen Dienst im Internet ein, liest dieser Dienst jedes Dokument mit;
> das widerspricht dem Grundsatz „Daten bleiben lokal“. Wenn Dokumente das Haus nicht verlassen
> dürfen: nur einen lokalen Server verwenden.

**Ollama im selben Stack:** eine Datei `compose.override.yaml` neben `compose.yaml` anlegen
(Compose liest sie automatisch mit):

```yaml
services:
  ollama:
    image: ollama/ollama:latest
    restart: unless-stopped
    volumes:
      - ./ollama:/root/.ollama
```

```sh
docker compose up -d ollama
docker compose exec ollama ollama pull qwen2.5:7b
```

In der `.env`:

```sh
DOCUSHELF_LLM_ENABLED=true
# DOCUSHELF_LLM_URL=http://ollama:11434     (Standard)
DOCUSHELF_LLM_MODEL=qwen2.5:7b
```

Ollama braucht keinen veröffentlichten Port: DocuShelf erreicht es im Docker-Netz unter
`http://ollama:11434`. Auf einer reinen CPU braucht ein 7B-Modell je Dokument gern ein bis drei
Minuten – das läuft im Hintergrund, `DOCUSHELF_LLM_TIMEOUT` (Standard 180 s) begrenzt die
Wartezeit. Kleinere Modelle (z. B. `qwen2.5:3b`) sind schneller, liegen aber öfter daneben.
`DOCUSHELF_LLM_CONTEXT` (Standard 8192 Tokens) muss Ordnerliste und Text fassen; Ollamas eigener,
kleinerer Standard würde beides stillschweigend abschneiden.

**Andere Server:** llama.cpp-Server, vLLM, LocalAI, LM Studio und ähnliche bieten die
Chat-Schnittstelle von OpenAI an:

```sh
DOCUSHELF_LLM_ENABLED=true
DOCUSHELF_LLM_PROVIDER=openai
DOCUSHELF_LLM_URL=http://llama:8080/v1          # mit oder ohne /v1
DOCUSHELF_LLM_MODEL=qwen2.5-7b-instruct         # Modellname, wie ihn der Server kennt
# DOCUSHELF_LLM_API_KEY_FILE=/run/secrets/llm_api_key   (falls der Server einen Schlüssel verlangt)
```

DocuShelf fragt nach strukturierter Ausgabe (JSON-Schema); Server, die das nicht können, bekommen
automatisch den einfachen JSON-Modus. Der API-Schlüssel geht als Bearer-Token an den Server und
wird nie angezeigt oder protokolliert.

**Prüfen:** *Einstellungen → KI → Verbindung testen* lässt das Modell einen
eingebauten, erfundenen Beispielbrief einordnen und zeigt Antwort und Dauer. Im Protokoll jedes
Eingangsdokuments steht, ob die KI etwas vorgeschlagen hat.

### Automatische Ablage

Normalerweise wartet jedes Dokument im Eingang auf deine Bestätigung. Mit der automatischen Ablage
legt DocuShelf Dokumente, bei denen es sich sicher ist, selbst ab – Brief scannen, fertig. Sie ist
aus, bis du sie einschaltest:

| `DOCUSHELF_AUTO_FILE` | Was automatisch abgelegt wird |
|---|---|
| `off` / `aus` (Standard) | nichts |
| `rules` / `regeln` | nur Dokumente, auf die eine Regel mit `auto: true` passt (Seite *Regeln* bzw. `config/rules.yaml`) |
| `confident` / `sicher` | zusätzlich alles, was mindestens so sicher ist wie `DOCUSHELF_AUTO_FILE_THRESHOLD` (Standard `0.75` = 75 %); der Rest bleibt im Eingang |
| `always` / `immer` | alles; was unter der Schwelle liegt, wird als „unsicher“ markiert, damit du es prüfst |

Wie sicher eine Ablage ist, ergibt sich aus den Vorschlägen: Eine passende Regel zählt voll, ebenso
eine Entscheidung aus dem Paperless-Import; sehr ähnliche Dokumente im selben Ordner zählen fast
voll, die KI etwas weniger, Vermutungen nach Absender und Ordnernamen nur halb. Sind sich mehrere
Quellen einig, steigt die Sicherheit. Sie sinkt, wenn zwei Ordner fast gleich gut passen, wenn der
Ordner erst angelegt werden müsste oder wenn im Dokument kein Datum zu finden war (dann gilt das
Eingangsdatum). Der Titel kommt aus der Regel, sonst von der KI, sonst vom ähnlichsten Dokument;
Schlagwörter nur aus Regeln.

Nie automatisch abgelegt werden mögliche Duplikate, Dokumente aus Eingängen, die nicht in
`DOCUSHELF_AUTO_FILE_SOURCES` stehen (Standard: alle Kanäle außer dem Paperless-Import), und
Dokumente, die jemand von Hand erneut verarbeitet oder bearbeitet hat. Mit
`DOCUSHELF_AUTO_FILE_NEW_FOLDERS=false` legt die automatische Ablage keine neuen Ordner an. Ist das
Sprachmodell eingeschaltet, entscheidet sie erst nach dessen Antwort
(`DOCUSHELF_AUTO_FILE_WAIT_FOR_LLM`).

**Sehen und korrigieren:** Jede automatische Ablage steht im Audit-Protokoll (`protokoll/`, Teil
der Sicherung) und in der Oberfläche unter *Eingang → Automatisch abgelegt* (die letzten 60 Tage):
mit Ordner, Sicherheit, den Hinweisen, auf die sie sich stützt (Regel, Ähnlichkeit, KI, Paperless),
und den Gründen. Mit „Passt“ bestätigst du eine Ablage, mit „Ändern“ korrigierst du Titel oder
Ordner auf der Dokumentseite. „Nur unsichere“ zeigt, was noch geprüft werden sollte; solange davon
etwas offen ist, trägt „Eingang“ in der Navigation einen kleinen Punkt. Was zurückgehalten wurde,
bleibt im Eingang und nennt den Grund, z. B. „Nicht automatisch: unsicher (58 %)“. Gelöscht oder
überschrieben wird dabei nie etwas.

Mit `DOCUSHELF_NOTIFY_URL` meldet DocuShelf jede automatische Ablage („Abgelegt: …“ mit Ordner und
Sicherheit); abschalten mit `DOCUSHELF_AUTO_FILE_NOTIFY=false`.

**Zum Einstieg:** ein paar Wochen `rules` mit Regeln `auto: true` für die regelmäßigen Briefe
(Kontoauszüge, Abschläge, Gehaltsabrechnungen), danach `confident`. Je mehr Dokumente im Archiv
liegen, desto besser treffen die Vorschläge.

## 8. Netzwerk: vom Drucker zum Server über das VPN

### Ausgangslage

- Der Drucker steht im Heimnetz (`192.168.1.0/24`, Router z. B. eine FritzBox) und kann selbst
  kein VPN.
- Der Server ist nur über das VPN erreichbar (z. B. Tailscale mit Headscale als
  Steuerungsserver), Adresse `100.64.0.10`.
- Ein Gerät im Heimnetz ist **Subnetz-Router**: Es ist im VPN und gibt das Heimnetz
  `192.168.1.0/24` frei (z. B. ein Raspberry Pi, `192.168.1.2`).

**Paketweg:** Drucker → Heimrouter (Gateway) → statische Route → Subnetz-Router → VPN → Server,
und zurück: Server → VPN → Subnetz-Router → Drucker.

### Schritte

1. **Feste IP für den Drucker** – DHCP-Reservierung im Heimrouter (`192.168.1.20`).
2. **Statische Route im Heimrouter:** Netz `100.64.0.0/10` → Gateway `192.168.1.2`
   (LAN-Adresse des Subnetz-Routers). Bei einer FritzBox unter *Heimnetz → Netzwerk →
   Netzwerkeinstellungen → Statische Routingtabelle*. Erreichen andere Geräte im Heimnetz das VPN
   schon über den Subnetz-Router, existiert die Route bereits.
3. **Subnetz-Router leitet im Kernel weiter:** IP-Forwarding an, Route `192.168.1.0/24`
   ankündigen (`tailscale set --advertise-routes=192.168.1.0/24`) und im Steuerungsserver
   freigeben (Headscale: Route aktivieren). Beim Tailscale-Add-on von Home Assistant
   `userspace_networking: false` setzen – im Userspace-Modus leitet es Verkehr aus dem Heimnetz
   nicht ins VPN weiter.
4. **Rückweg auf dem Server:** `tailscale set --accept-routes`, damit Antworten an
   `192.168.1.x` über den Subnetz-Router zurückgehen. Vorher prüfen, dass kein Docker-Netz auf
   dem Server `192.168.1.0/24` verwendet (`docker network inspect $(docker network ls -q) | grep Subnet`),
   sonst landen die Antworten im falschen Netz.
5. **ACL im VPN:** nur der Drucker zum Server und nur auf die benötigten Ports, z. B. für Headscale:

   ```json
   {
     "hosts": { "drucker": "192.168.1.20/32", "docserver": "100.64.0.10/32" },
     "acls": [
       { "action": "accept", "src": ["drucker"],
         "dst": ["docserver:445", "docserver:21", "docserver:30000-30009", "docserver:2525"] }
     ]
   }
   ```

   Nur die Ports der Kanäle aufnehmen, die tatsächlich aktiv sind.
6. **Ports nur auf der VPN-Adresse binden:** `DOCUSHELF_PRIVATE_IP=100.64.0.10` (SMB, FTP) und,
   falls Scan-to-Mail genutzt wird, `DOCUSHELF_SMTP_BIND=100.64.0.10` – Compose veröffentlicht dann
   z. B. `100.64.0.10:445:445`. Beim Booten muss das VPN vor den Containern oben sein, sonst
   schlägt das Binden fehl („cannot assign requested address“); wie man das sicherstellt, steht
   in Abschnitt 2.
7. **Im Drucker IP-Adressen eintragen, keine Hostnamen** – er kann VPN-Namen nicht auflösen.

### Vorher am Drucker prüfen

- Welche SMB-Version spricht er (SMB2/3 nötig)?
- Kann er FTP im passiven Modus?
- Ist bei Scan-to-Mail der SMTP-Port frei wählbar, und wie verhalten sich Anmeldung und TLS?
- Viele Geräte bieten auch WebDAV als Ziel an – das ginge über HTTPS und den Reverse Proxy und
  wäre netzwerkseitig am einfachsten (in DocuShelf derzeit nicht eingebaut).

### Prüfen

```sh
# auf dem Subnetz-Router: kommen Pakete vom Drucker an?
sudo tcpdump -ni any host 192.168.1.20 and host 100.64.0.10
# auf dem Server: lauschen die Dienste auf der VPN-Adresse?
ss -ltnp | grep 100.64.0.10
# vom Server aus (Absender ist dann nicht der Drucker – hosts allow ggf. vorübergehend erweitern):
smbclient //100.64.0.10/scan -U scanner -m SMB3 -c 'put test.pdf'
```

**Ausfallsicherheit:** Fällt der Subnetz-Router aus, gehen SMB, FTP und SMTP direkt nicht mehr.
Scan-to-Mail über einen externen Mailanbieter mit IMAP-Abruf (Abschnitt 6) funktioniert weiter.

## 9. Migration von Paperless-ngx

1. **Export in Paperless-ngx** (im Paperless-Stack, Exportordner ist dort `../export`):

   ```sh
   docker compose exec -T webserver document_exporter ../export --use-folder-prefix --no-thumbnail
   ```

   Gelesen werden auch flache Exporte und `--split-manifest`. Ein ZIP-Export (`--zip`) muss
   vorher entpackt werden. Die Archivfassungen (PDF/A mit Text) nicht weglassen – DocuShelf spart
   sich damit die erneute Texterkennung.
2. **Export einbinden** – nur lesend; er wird nie verändert. In `compose.yaml` beim Dienst
   `docushelf`:

   ```yaml
       volumes:
         - ${DOCUSHELF_DATA_PATH:-./data}:/data
         - /pfad/zum/paperless/export:/import:ro
   ```

   Die Dateien müssen für UID 1000 lesbar sein (`chmod -R a+rX /pfad/zum/paperless/export`).
   Dann `docker compose up -d`.
3. **Probelauf** – zeigt für jedes Dokument Zielordner und Titel, ändert nichts:

   ```sh
   docker compose exec docushelf docushelf import-paperless /import --dry-run
   ```

   Auswahl einschränken: `--storage-path "Name"` bzw. `--owner name` (mehrfach möglich, `-` steht für
   „ohne Speicherpfad/Besitzer“). Die Übersicht am Anfang zeigt, welche es gibt.
4. **Ordnerzuordnung anpassen:** `config/paperless-import.yaml` entsteht beim ersten Lauf mit
   Vorgaben (Dokumenttyp → Ordnervorlage mit `{correspondent}`, `{document_type}`, `{year}`,
   `{storage_path}`; Umbenennen von Korrespondenten; Tags, die wegfallen; welche Datei übernommen
   wird). Danach erneut den Probelauf starten. Auch nach dem Import lässt sich die Zuordnung noch
   ändern: *Eingang → Vorschläge neu berechnen* bestimmt die Ordner der importierten Dokumente
   dann mit der aktuellen Datei neu; Titel, Datum, Tags und Absender bleiben, wie sie importiert
   wurden.
5. **Import in den Eingang** (Standard, entspricht „nichts ohne Bestätigung“):

   ```sh
   docker compose exec docushelf docushelf import-paperless /import
   ```

   Jedes Dokument kommt mit Ablagevorschlag in den Eingang; die Sammelablage übernimmt alle
   Vorschläge mit einem Klick – und zwar so, wie sie auf der Seite angezeigt werden: Hat sich ein
   Vorschlag inzwischen geändert (etwa durch „Vorschläge neu berechnen“), bleibt das Dokument im
   Eingang; nach dem Neuladen der Seite steht dort der neue Vorschlag.

   Was mitkommt: Titel und Datum (Dateiname und XMP), der Korrespondent als Absender, Tags, ASN
   und die Paperless-ID. Notizen und eigene Felder stehen im Eingang unter „Erkannt“ und werden beim
   Ablegen als Text in die XMP-Metadaten der PDF geschrieben (`PaperlessNotes`,
   `PaperlessFields`); die Dokumentseite zeigt sie unter „Weitere Angaben“. Ausnahme: Digital
   signierte oder mit Passwort geschützte PDFs werden unverändert abgelegt – bei ihnen landen nur
   Titel und Datum im Dateinamen, Tags, Absender, Notizen und eigene Felder nicht in der Datei.
   Den Paperless-Export deshalb aufheben, bis alles abgelegt und geprüft ist.
6. **Oder direkt ablegen** (`--mode archive`): Dabei verarbeitet der Befehl selbst – deshalb vorher
   den Server stoppen:

   ```sh
   docker compose stop docushelf
   docker compose run --rm docushelf docushelf import-paperless /import --mode archive
   docker compose start docushelf
   ```

   Dokumente, die nicht sauber abgelegt werden können (Fehler, mögliche Duplikate), bleiben im
   Eingang.

Ein abgebrochener Import kann einfach erneut gestartet werden: Bereits übernommene Dokumente
(gleicher SHA-256 des Originals in Archiv oder Eingang) werden übersprungen. Paperless-ngx kann
bis zum Abschluss weiterlaufen.

## 10. Updates und Neuaufbau des Index

```sh
cd /opt/docushelf
git pull
cd deploy
docker compose build --pull
docker compose up -d
docker compose --profile smb --profile ftp up -d --build   # falls die Kanäle genutzt werden
```

Ändert sich das Format des Index, baut DocuShelf ihn beim Start selbst neu auf. Die Daten
(Archiv, Eingang, Konfiguration) bleiben unberührt.

**Index von Hand neu aufbauen** (etwa nach dem Zurückspielen eines Backups):

- im laufenden Betrieb: Oberfläche → *Einstellungen* → *Index neu aufbauen* (oder
  `POST /api/v1/admin/rebuild`);
- auf der Kommandozeile bei gestopptem Server:

  ```sh
  docker compose stop docushelf
  docker compose run --rm docushelf docushelf rebuild
  docker compose start docushelf
  ```

`rebuild` und `reconcile` verweigern die Arbeit, solange ein Server dasselbe Datenverzeichnis
benutzt (auch aus einem anderen Container); `--force` übergeht das auf eigene Gefahr. Ist die
Indexdatei beschädigt, legt `rebuild` sie beiseite (`docushelf.sqlite3.defekt-…`) und beginnt neu.

Weitere Befehle im Container: `docushelf status`, `docushelf reconcile`, `docushelf process <ID>`
(ein Eingangsdokument sofort verarbeiten), `docushelf token …` – Übersicht mit `docushelf --help`.

## 11. Fehlersuche

Erste Schritte:

```sh
docker compose ps                                   # Zustand und Healthchecks
docker compose logs -f docushelf                    # Protokoll der App
docker compose exec docushelf docushelf status      # Index, Eingang, Aufträge, OCR, Kanäle
docker inspect --format '{{json .State.Health}}' docushelf-docushelf-1
```

Dokumente, deren Verarbeitung fehlschlug, stehen in der Oberfläche unter *Eingang → Fehler*; das
Protokoll je Dokument liegt in `eingang/fehler/<ID>/log.txt`. Nach einer Korrektur:
„Erneut verarbeiten“ oder `docushelf process <ID>`.

| Symptom | Ursache und Abhilfe |
|---|---|
| Start bricht ab: „DocuShelf läuft als UID 1000 … und kann hier nicht schreiben“ | Verzeichnis gehört jemand anderem. Auf dem Host `sudo chown -R 1000:1000 <Datenverzeichnis>`. |
| „Archivverzeichnis … existiert nicht“ | `DOCUSHELF_ARCHIVE_DIR` zeigt auf einen nicht eingehängten Pfad. Mount prüfen. |
| „Ein anderer DocuShelf-Server verwendet dieses Datenverzeichnis bereits“ | Zweite Instanz (z. B. ein vergessener `docker compose run`). Beenden. |
| Container „unhealthy“ | `/healthz` prüft Index-Datenbank, Schreibrechte im Datenverzeichnis und den Archivordner; die Meldung steht in `docker inspect` (siehe oben). |
| Anmeldung landet immer wieder auf der Login-Seite | `DOCUSHELF_BASE_URL` mit `https://`, aber Aufruf über `http://` – Cookies werden dann nicht gesendet. Über HTTPS aufrufen oder für Tests `http://` eintragen. |
| SSO: „redirect_uri mismatch“ o. ä. | Redirect-URI in Authentik muss exakt `https://docs.example.net/auth/callback` sein und zu `DOCUSHELF_BASE_URL` passen. |
| SSO: Anmeldung klappt, aber „kein Zugriff“ | Benutzer/Gruppe fehlt in `DOCUSHELF_ALLOWED_USERS`/`_GROUPS`; Gruppen kommen nur mit Scope `profile`. |
| SSO: Fehler beim Abruf der Konfiguration | Der Container erreicht den Issuer nicht (DNS, Firewall) oder der Issuer stimmt nicht (Schrägstrich am Ende beachten). |
| OCR fehlt oder Sprache nicht installiert | `docushelf status` zeigt den OCR-Zustand. Im Image sind Deutsch und Englisch; für weitere Sprachen ein eigenes Image ableiten (`FROM docushelf:local`, `apt-get install tesseract-ocr-fra`) und `DOCUSHELF_OCR_LANGUAGES` anpassen. |
| Drucker erreicht SMB nicht | Spricht er nur SMB1? Dann FTP/Mail nutzen. `SAMBA_HOSTS_ALLOW` muss seine IP enthalten; `docker compose logs samba`. Netzwerkweg prüfen (Abschnitt 8). |
| SMB: Drucker meldet beim Verbindungstest einen Fehler, Scans kommen aber an | Er darf den Ordner nicht auflisten (gewollt). Falls er deshalb gar nicht sendet: `SAMBA_SCAN_DIR_MODE=2770`. |
| SMB: Scans bleiben in `eingang/quellen/smb` liegen | DocuShelf kann sie nicht lesen. `docker compose exec samba id docushelf` muss `uid=1000 gid=1000` zeigen (Konto `docushelf` in `samba.env`). |
| SMB/SMTP: Zugriff abgelehnt, obwohl die Drucker-IP erlaubt ist | Kommt die Verbindung über Dockers Userland-Proxy, sieht der Container die Gateway-Adresse des Docker-Netzes statt der Drucker-IP (z. B. bei Zugriff über `localhost`). Mit `docker compose logs samba` bzw. `docker compose logs docushelf` die gesehene Adresse prüfen. |
| FTP: Anmeldung klappt, Übertragung nicht | Passiver Modus: `DOCUSHELF_PRIVATE_IP` muss die Adresse sein, die der Drucker erreicht, und Port 30000–30009 müssen in der VPN-ACL freigegeben sein. |
| SMTP: Mail abgelehnt | Empfänger muss genau `DOCUSHELF_SMTP_RECIPIENT` sein, Absender-IP in `DOCUSHELF_SMTP_ALLOWED_NETWORKS`, Größe unter `DOCUSHELF_SMTP_MAX_MESSAGE_MB`. |
| Container starten nicht: „cannot assign requested address“ | `DOCUSHELF_PRIVATE_IP` oder `DOCUSHELF_SMTP_BIND` nennt eine Adresse, die der Host (noch) nicht hat – beim Booten war das VPN noch nicht oben, oder die Adresse stimmt nicht. Für einen Test ohne Scan-Kanäle beide auskommentieren; zur Startreihenfolge siehe Abschnitt 2. |
| API-Token wird abgelehnt (401), obwohl nicht widerrufen | In der Oberfläche erstellte Tokens gelten nur, solange ihr Konto nach `DOCUSHELF_ALLOWED_USERS`/`_GROUPS` (bzw. als Notfallzugang) hinein darf. Das Server-Protokoll nennt den Grund. Neues Token mit einem freigeschalteten Konto erstellen. |
| Endgültig löschen per API: „nur nach einer Anmeldung im Browser möglich“ | Gewollt: Mit einem API-Token lassen sich Papierkorb-Einträge nicht endgültig löschen. |
| Vorschau zeigt „Keine Vorschau möglich“ | Die PDF ist mit einem Passwort geschützt oder beschädigt. Herunterladen und mit einem PDF-Programm öffnen. |
| Sammelablage: „Der Vorschlag hat sich inzwischen geändert“ | Die Vorschläge wurden nach dem Laden der Seite neu berechnet. Seite neu laden, prüfen, erneut ablegen. |
| Dokument bearbeiten: „Die Datei wurde inzwischen geändert …“ | Jemand hat dieselbe Angabe zwischendurch geändert (in der App oder im Finder). Die eigenen Eingaben stehen noch im Formular; prüfen und erneut speichern. |
| Suche findet nichts mehr, Zahlen stimmen nicht | Index neu aufbauen (Abschnitt 10). Das ist immer gefahrlos. |
| Kein KI-Vorschlag | Das Protokoll des Dokuments nennt den Grund. *Einstellungen → KI → Verbindung testen*; Adresse und Modellname prüfen (`docker compose exec ollama ollama list`); auf einer langsamen CPU `DOCUSHELF_LLM_TIMEOUT` erhöhen. |
| Automatische Ablage legt nichts ab | `DOCUSHELF_AUTO_FILE` und `DOCUSHELF_AUTO_FILE_SOURCES` prüfen; jedes zurückgehaltene Dokument zeigt im Eingang „Nicht automatisch: …“ mit dem Grund. Im Modus `rules` braucht die Regel `auto: true`. |

## 12. Sicherheit im Überblick

- Weboberfläche nur über den Reverse Proxy und nur aus dem VPN; Anmeldung per OIDC, lokaler
  Notfallzugang mit scrypt-Hash und gebremsten Anmeldeversuchen.
- Zugang über die Gruppe in Authentik (Gruppen-Bindung der Anwendung plus
  `DOCUSHELF_ALLOWED_GROUPS`); `DOCUSHELF_ALLOWED_USERS` nur ergänzend (Abschnitt 4).
- Sitzungs-Cookies `HttpOnly`, `SameSite=Lax`, bei HTTPS `Secure`; CSRF-Schutz für alle
  schreibenden Formulare; strenge Content-Security-Policy. Sitzungen gelten bis
  `DOCUSHELF_SESSION_MAX_AGE`; ein neuer Schlüssel (`config/secret.key`) meldet alle ab, ein neues
  Notfallpasswort alle Sitzungen des Notfallzugangs.
- API-Tokens werden nur als SHA-256-Hash gespeichert (`config/tokens.yaml`). In der Oberfläche
  erstellte Tokens gelten nur, solange ihr Konto freigeschaltet ist; endgültiges Löschen und das
  Verwalten von Tokens gehen nur im Browser.
- Technische Konten (`scanner` für SMB/FTP, Mail-Zugang) sind von Personenkonten getrennt.
- Scan-Kanäle nur auf der VPN-Adresse, zusätzlich mit Allowlist der Absender; SMB ohne SMB1; der
  SMTP-Empfänger nimmt nur eine Adresse an und leitet nie weiter.
- Der App-Container läuft ohne Root und ohne Linux-Capabilities (`cap_drop: ALL`,
  `no-new-privileges`).
- Keine Cloud-Dienste für Dokumentinhalte: Ein Sprachmodell ist optional und bekommt Dokumenttexte
  nur unter der eingestellten Adresse – standardmäßig ein Ollama auf dem eigenen Server (Abschnitt 7).
- Jede Ablage, Verschiebung und Löschung steht im Audit-Log (`protokoll/`), jede automatische
  Ablage zusätzlich mit Sicherheit und Gründen.

---

## 13. Mehrere Haushalte

Mehrere Familien (Eltern, Geschwister …) können eine DocuShelf-Installation teilen. Jeder
**Haushalt** hat sein eigenes Archiv, seinen eigenen Eingang, eigene Regeln, Tokens, Papierkorb,
Protokoll und Kanäle; niemand sieht Dokumente eines anderen Haushalts. Ohne die Datei
`households.yaml` ändert sich nichts: Dann gibt es genau einen Haushalt (Name aus
`DOCUSHELF_HOUSEHOLD_NAME`, Standard „Zuhause“) und alles in diesem Dokument davor gilt unverändert.

### `households.yaml`

Die Datei liegt in `<daten>/config/households.yaml`, neben `settings.yaml` und `secret.key`.
Eine kommentierte Vorlage steht in `deploy/households.example.yaml`:

```yaml
admins: ["erika@example.org"]    # dürfen Mitglieder in der Weboberfläche pflegen (s. u.)
admin_groups: [software-admins]  # alle in diesen Gruppen sind ebenfalls Admins (s. u.)
households:
  - id: familie-a                # a-z, 0-9, „-“; stabil – steckt in Pfaden und Tokens
    name: "Familie Mustermann"
    path: "."                    # die bisherige Installation, siehe „Migration“
    groups: [familie-a]          # OIDC-Gruppen mit Zugriff
    smtp_recipients: [scan-familie-a@docushelf.example.org]
  - id: eltern
    name: "Eltern Mustermann"
    groups: [docushelf-eltern]
    users: ["opa@example.org"]   # einzelne Konten: Benutzername oder bestätigte E-Mail
    smtp_recipients: [scan-eltern@docushelf.example.org]
    imap:                        # eigenes Postfach, nur für diesen Haushalt
      host: imap.example.org
      user: dokumente-eltern@example.org
      password_file: /run/secrets/imap-eltern
      allowed_senders: [opa@example.org]
```

Je Haushalt lassen sich außerdem `owner_names`, `notify_url`, `auto_file` (Überschreibungen von
`DOCUSHELF_OWNER_NAMES`, `_NOTIFY_URL`, `_AUTO_FILE_*`) und `watch_folders` setzen.

- DocuShelf prüft die Datei beim Start streng und bricht mit einer deutschen Meldung ab bei
  doppelten Kennungen, ungültigen Kennungen, doppelten Pfaden oder Adressen, einem Pfad innerhalb
  des Archivs/Eingangs eines anderen Haushalts, mehr als einem Haushalt mit `path: "."`,
  fehlenden IMAP-Angaben und **unbekannten Einträgen** (ein Tippfehler bei `groups` soll nie
  stillschweigend den Zugriff ändern).
- Passwörter für IMAP am besten als `password_file` (Docker-Secret) oder `password_env`; sie
  werden nie protokolliert oder angezeigt.
- Änderungen an der Datei wirken nach einem Neustart: `docker compose up -d --force-recreate docushelf`.
  Ausnahme: die `users` der Haushalte, die Admins in der Weboberfläche pflegen (nächster Abschnitt).
- Mit `households.yaml` gelten `DOCUSHELF_ALLOWED_USERS`, `_ALLOWED_GROUPS` und
  `_ALLOW_ALL_OIDC_USERS` **nicht mehr**: Zugriff geben allein die `groups` und `users` der
  Haushalte (bisherige Gruppe also beim bisherigen Haushalt eintragen). Wer zu keinem Haushalt
  gehört, bekommt nach der Anmeldung die Seite „Kein Haushalt freigegeben“. Das lokale
  Notfallkonto (Abschnitt 4) darf in alle Haushalte.
- `DOCUSHELF_ARCHIVE_DIR` gilt nur mit genau einem Haushalt; mit mehreren liegt das Archiv jedes
  Haushalts unter `<haushalt>/archiv` (die Datei-Wahrheit bleibt also je Haushalt ein Ordnerbaum).
- `DOCUSHELF_OCR_PARALLEL` (Standard 1) begrenzt, wie viele OCR-Läufe **insgesamt** gleichzeitig
  laufen, damit die Haushalte den Server nicht gemeinsam überlasten.

### Anmeldung: Gruppen je Haushalt

In Authentik je Haushalt eine Gruppe anlegen (`familie-a`, `docushelf-eltern`) und die Personen
hineinsetzen. Die Anwendung wird an **alle** diese Gruppen gebunden (Policy-Bindings in der
Anwendung, Abschnitt 4), den Haushalten ordnet DocuShelf die Gruppen aus dem Claim `groups` zu.
Wer in mehreren Gruppen ist, sieht in der Kopfzeile einen Umschalter und landet beim Anmelden im
ersten seiner Haushalte; wer nur in einer ist, merkt nichts davon. Gruppenänderungen wirken wie
immer erst bei der nächsten Anmeldung.

### Mitglieder in der Weboberfläche pflegen

Statt jede Person in eine Gruppe beim Anmeldedienst zu setzen, kann ein Admin die einzelnen
Konten (`users`) direkt in DocuShelf pflegen: **Einstellungen → Haushalte** zeigt für jeden
Haushalt Name, Kennung, die eingetragenen Benutzer (hinzufügen und entfernen) und die Gruppen
(nur zur Ansicht – die kommen aus dem Anmeldedienst bzw. aus `households.yaml`).

- **Admins** sind das lokale Notfallkonto, die Einträge unter `admins:` in `households.yaml` und
  die Konten in `DOCUSHELF_ADMIN_USERS` (kommagetrennt). Gemeint ist jeweils der Benutzername
  aus dem Anmeldedienst oder die bestätigte E-Mail-Adresse, Groß-/Kleinschreibung egal. Andere
  sehen den Abschnitt nicht und bekommen beim Ändern „Keine Berechtigung“ (403).
- **Admins per Gruppe:** Zusätzlich ist jedes SSO-Konto Admin, das in einer Gruppe unter
  `admin_groups:` in `households.yaml` oder in `DOCUSHELF_ADMIN_GROUPS` (kommagetrennt) steht,
  z. B. `admin_groups: [software-admins]`. Maßgeblich ist der Claim `groups` der Anmeldung
  (Groß-/Kleinschreibung egal); wer in Authentik in die Gruppe kommt oder herausfällt, ist das ab
  der nächsten Anmeldung. Die Admin-Gruppe gibt **keinen** Zugriff auf einen Haushalt – dafür
  braucht es weiterhin `groups`/`users` eines Haushalts.
- Ein Admin muss selbst zu einem Haushalt gehören, um die Einstellungen öffnen zu können; wer nur
  Admin ist, aber zu keinem Haushalt gehört, wird bei der SSO-Anmeldung abgewiesen wie jeder ohne
  Haushalt (das lokale Notfallkonto darf immer in alle Haushalte). Bindet die
  Anwendung in Authentik nur bestimmte Gruppen, muss ein Admin außerdem darüber überhaupt
  anmelden dürfen (Policy-Binding).
- Eingetragen wird ein Benutzername oder eine E-Mail-Adresse (höchstens 200 Zeichen); doppelte
  Einträge im selben Haushalt werden abgelehnt, eine Person darf aber in mehreren Haushalten sein.
- Die Änderung wirkt **sofort**: Wer hinzugefügt wird, kann sich gleich anmelden bzw. umschalten;
  wer entfernt wird, verliert beim nächsten Seitenaufruf den Zugriff (außer über eine Gruppe).
  Sie steht im Protokoll des Haushalts („Haushaltsmitglieder geändert“).
- DocuShelf schreibt dafür `households.yaml` neu. Die vorige Fassung liegt danach als
  `households.yaml.bak` daneben. Alle anderen Einträge und die Reihenfolge der Haushalte bleiben
  erhalten, **Kommentare gehen verloren**. Vor dem Speichern wird die ganze Datei wie beim Start
  geprüft; ist sie ungültig (z. B. nach einer Handänderung mit Tippfehler), wird nichts
  geschrieben und die Seite zeigt die Fehlermeldung.
- Haushalte hinzufügen, entfernen oder umbenennen geht weiterhin nur in der Datei und mit Neustart.

Für einen einzigen Haushalt (ohne `households.yaml`) gibt es diesen Abschnitt nicht; dort gelten
`DOCUSHELF_ALLOWED_USERS` und `_ALLOWED_GROUPS` wie bisher.

### Datenablage

```
<DOCUSHELF_DATA_PATH>/
├── config/
│   ├── households.yaml      global: die Haushalte
│   ├── settings.yaml        global
│   ├── secret.key           global
│   └── rules.yaml, tokens.yaml …   Regeln/Tokens des Haushalts mit path "."
├── archiv/ eingang/ papierkorb/ protokoll/ index/    Haushalt „familie-a“ (path ".")
└── haushalte/
    └── eltern/              Haushalt „eltern“ (Standardpfad haushalte/<id>)
        ├── archiv/
        ├── eingang/quellen/{upload,smb,ftp,mail,smtp}/
        ├── config/          rules.yaml, tokens.yaml …
        ├── protokoll/
        ├── papierkorb/
        └── index/
```

Jeder Haushalt hat also seine **eigenen Drop-Zonen** unter `<haushalt>/eingang/quellen/…`. Der
Index ist je Haushalt getrennt; Dokument-IDs eines anderen Haushalts liefern immer „nicht
gefunden“. Der Server hält eine globale Sperre (`server.lock`) für das ganze Datenverzeichnis.

### Migration: bestehende Daten bleiben liegen

Eine vorhandene Installation wird **nicht verschoben**: Ihr Datenverzeichnis wird selbst zum
Haushalt mit `path: "."`.

1. Sicherung des Datenverzeichnisses (Abschnitt 5).
2. `config/households.yaml` anlegen: den bisherigen Haushalt mit `path: "."` und der bisherigen
   Gruppe aus `DOCUSHELF_ALLOWED_GROUPS` (bzw. den Konten aus `DOCUSHELF_ALLOWED_USERS`) eintragen,
   darunter die neuen Haushalte. Ihre Verzeichnisse legt DocuShelf beim Start an.
3. Neustart; danach prüfen:
   ```sh
   docker compose up -d --force-recreate docushelf
   docker compose exec docushelf docushelf households
   ```
   Die Liste zeigt Kennung, Namen, Pfad, Zugriffsregeln und Anzahl der Dokumente.
4. Die globalen Einstellungen für IMAP (`DOCUSHELF_IMAP_*`), `DOCUSHELF_WATCH_FOLDERS` und
   `DOCUSHELF_SMTP_RECIPIENT` gelten weiter – aber nur für den Haushalt mit `path: "."`. Alle
   anderen Haushalte tragen ihre eigenen Angaben in `households.yaml` ein.
5. Bestehende API-Tokens (`config/tokens.yaml`) gehören zum bisherigen Haushalt und bleiben gültig;
   iPhone-Kurzbefehle usw. müssen nicht angefasst werden.

Der Weg zurück: `households.yaml` entfernen (oder umbenennen) – der Haushalt mit `path: "."`
läuft dann wie vorher; die Daten der anderen Haushalte bleiben unter `haushalte/` unberührt liegen.

### SMB und FTP: eine Freigabe und ein Konto je Haushalt

Samba und FTP unterscheiden die Haushalte über **Konten und Freigaben**: Jeder Haushalt bekommt
einen eigenen Drucker-Zugang (`scan_<id>` → `<haushalt>/eingang/quellen/smb`) und eine eigene
Archiv-Freigabe (`archiv_<id>` → `<haushalt>/archiv`) mit eigenen Konten. Das Konto eines
Haushalts kann keine Freigabe eines anderen öffnen (`valid users`). Der Haushalt mit `path: "."`
behält die Freigaben `scan` und `archiv` aus Abschnitt 6 unverändert.

Vorbereitung für die Haushalte mit eigenem Pfad: Zuerst DocuShelf einmal mit der neuen
`households.yaml` starten (legt `haushalte/eltern/…` mit dem richtigen Besitzer an), danach dem
SMB-Briefkasten den Modus geben (der Dienst `init` kümmert sich nur um den Haushalt im
Datenverzeichnis selbst):

```sh
sudo chmod 1733 /srv/docushelf/data/haushalte/eltern/eingang/quellen/smb
```

Konten in `samba.env` (Namen klein, Unterstrich statt Bindestrich; UIDs eindeutig, nicht die von
DocuShelf):

```sh
ACCOUNT_scanner_eltern='ein-langes-zufaelliges-passwort'
UID_scanner_eltern=1003
ACCOUNT_opa='passwort-fuer-opa'
UID_opa=1103
```

Als Ergänzung `deploy/compose.override.yaml` (Compose liest sie automatisch dazu; Umgebung und
Volumes werden mit denen des Dienstes `samba` zusammengeführt):

```yaml
services:
  samba:
    environment:
      # Briefkasten des Haushalts „eltern“: wie „scan“, nur eigenes Konto und eigener Ordner
      SAMBA_VOLUME_CONFIG_scan_eltern: >-
        [scan_eltern];
        path = /shares/scan-eltern;
        comment = DocuShelf Scan-Eingang Eltern;
        valid users = scanner_eltern;
        invalid users = root docushelf;
        read only = no;
        browseable = no;
        guest ok = no;
        hosts allow = 127.0.0.1 ${SAMBA_HOSTS_ALLOW:-};
        hosts deny = 0.0.0.0/0;
        force group = docushelf;
        create mask = 0640;
        force create mode = 0640;
        directory mask = 2770;
        force directory mode = 2770;
        hide unreadable = yes;
        follow symlinks = no;
        wide links = no
      # Archiv des Haushalts „eltern“ zum Bearbeiten im Finder/Explorer
      SAMBA_VOLUME_CONFIG_archiv_eltern: >-
        [archiv_eltern];
        path = /shares/archiv-eltern;
        comment = DocuShelf Archiv Eltern;
        valid users = ${SAMBA_ARCHIVE_USERS_ELTERN:-nobody};
        invalid users = root scanner scanner_eltern docushelf;
        read only = no;
        browseable = yes;
        guest ok = no;
        hosts allow = 127.0.0.1 ${SAMBA_HOSTS_ALLOW:-};
        hosts deny = 0.0.0.0/0;
        force user = docushelf;
        force group = docushelf;
        create mask = 0644;
        directory mask = 0755;
        nt acl support = no;
        vfs objects = fruit streams_xattr;
        fruit:metadata = stream;
        fruit:veto_appledouble = yes;
        fruit:posix_rename = yes;
        fruit:wipe_intentionally_left_blank_rfork = yes;
        fruit:delete_empty_adfiles = yes;
        veto files = /._*/.DS_Store/Thumbs.db/desktop.ini/.Trashes/.TemporaryItems/.Spotlight-V100/.fseventsd/;
        delete veto files = yes;
        hide dot files = yes;
        follow symlinks = no;
        wide links = no
    volumes:
      - ${DOCUSHELF_DATA_PATH:-./data}/haushalte/eltern/eingang/quellen/smb:/shares/scan-eltern
      - ${DOCUSHELF_DATA_PATH:-./data}/haushalte/eltern/archiv:/shares/archiv-eltern

  # FTP: ein eigener Container je Haushalt, mit eigenem Konto, Port und
  # eigenem Bereich für passive Verbindungen (Drucker, die einen anderen
  # Port als 21 nicht einstellen können, nehmen SMB oder Mail)
  ftp-eltern:
    image: ${DOCUSHELF_FTP_IMAGE:-docushelf-ftp:local}
    profiles: ["ftp"]
    restart: unless-stopped
    depends_on:
      init:
        condition: service_completed_successfully
    environment:
      FTP_USER: scanner_eltern
      FTP_PASSWORD: ${FTP_PASSWORD_ELTERN:-}
      FTP_UID: ${DOCUSHELF_UID:-1000}
      FTP_GID: ${DOCUSHELF_GID:-1000}
      FTP_PASV_ADDRESS: ${DOCUSHELF_PRIVATE_IP:-}
      FTP_PASV_MIN_PORT: 30010
      FTP_PASV_MAX_PORT: 30019
      FTP_HIDE_FILES: "yes"
    volumes:
      - ${DOCUSHELF_DATA_PATH:-./data}/haushalte/eltern/eingang/quellen/ftp:/srv/ftp
    ports:
      - "${DOCUSHELF_PRIVATE_IP:-127.0.0.1}:2121:21"
      - "${DOCUSHELF_PRIVATE_IP:-127.0.0.1}:30010-30019:30010-30019"
    security_opt: ["no-new-privileges:true"]
```

In der `.env` dazu `SAMBA_ARCHIVE_USERS_ELTERN="opa"` und `FTP_PASSWORD_ELTERN='…'`; die
Konten der anderen Haushalte gehören **nicht** in `SAMBA_ARCHIVE_USERS` des Haushalts mit
`path: "."`. Im Drucker der Eltern: Freigabe `scan_eltern`, Benutzer `scanner_eltern` (bzw. FTP
auf Port 2121). Den FTP-Ordner `haushalte/eltern/eingang/quellen/ftp` legt DocuShelf beim Start an;
der Port-Bereich 30010–30019 muss im VPN freigegeben sein. Prüfen wie in Abschnitt 6
(`testparm -s`).

Mit der Compose-Datei in `deploy/` wird die Ergänzung ohne weitere Option geladen
(`docker compose --profile smb --profile ftp up -d`). Ein Ordner **außerhalb** des Datenverzeichnisses
(z. B. ein überwachter Ordner einer NAS) ist ebenfalls möglich: in `households.yaml` unter
`watch_folders` eintragen und in `compose.yaml` einbinden.

### SMTP-Adressen je Haushalt

Es gibt **einen** SMTP-Empfänger (Port 2525, Abschnitt 6) für die ganze Installation. Er ordnet jede
Mail anhand der Empfängeradresse einem Haushalt zu (`smtp_recipients` in `households.yaml`):

- Die Drucker der Haushalte bekommen je eine eigene Empfängeradresse
  (`scan-familie-a@…`, `scan-eltern@…`); Groß-/Kleinschreibung spielt keine Rolle.
- Eine Mail an Adressen mehrerer Haushalte wird in jedem dieser Haushalte genau einmal abgelegt.
- Eine unbekannte Adresse wird mit `550` und einem deutschen Text abgewiesen
  („Diese Adresse ist keinem Haushalt zugeordnet“); es gibt weiterhin **kein Relay**.
- `DOCUSHELF_SMTP_RECIPIENT` ist optional und bedient weiter den Haushalt mit `path: "."`.
- Adressen lassen sich auch in der Oberfläche pflegen (*Einstellungen → Eingangskanäle*,
  Abschnitt 6), auch je Person. Sobald ein Haushalt eine eigene `config/channels.yaml` hat, gelten
  für ihn nur deren Adressen; `smtp_recipients` aus `households.yaml` (und für den Haushalt mit
  `path: "."` `DOCUSHELF_SMTP_RECIPIENT`) zählen dann nicht mehr. Eine Adresse, die schon ein anderer
  Haushalt empfängt, lehnt die Oberfläche ab.
- Die erlaubten Absender-Netze (`DOCUSHELF_SMTP_ALLOWED_NETWORKS`) und die Anmeldung
  (`DOCUSHELF_SMTP_USER`) gelten für alle Haushalte gemeinsam. Die Adresse ist also nur eine
  **Zuordnung**, kein Zugriffsschutz: Ein Gerät im erlaubten Netz, das die Adresse eines fremden
  Haushalts kennt, könnte dorthin Scans schicken (und nur das – lesen kann es nichts). Adressen
  deshalb nicht erratbar wählen, und Drucker verschiedener Haushalte nach Möglichkeit
  in getrennte Netze legen.

### IMAP und Handy-Teilen

- **IMAP** ruft je Haushalt ein eigenes Postfach ab (`imap:` im Haushalt). Jeder Abruf schreibt nur
  in die Drop-Zone seines Haushalts (`<haushalt>/eingang/quellen/mail`); verarbeitete Mails
  wandern wie gewohnt in IMAP-Ordner, die Absender-Erlaubnisliste gilt je Haushalt. In der
  Oberfläche kann ein Haushalt mehrere Postfächer bekommen, auch eins pro Person (Abschnitt 6);
  die Passwörter liegen dann in `<haushalt>/config/secrets/`.
- **Admins** (`admins`, `admin_groups`) verwalten in der Oberfläche die Eingangskanäle des
  Haushalts, in dem sie gerade arbeiten – für einen anderen Haushalt erst umschalten (dafür
  müssen sie selbst Mitglied sein).
- **API-Tokens gehören zu einem Haushalt** (`<haushalt>/config/tokens.yaml`). Ein Token, das ein
  Haushalt erstellt, lädt immer in diesen Haushalt hoch – der iPhone-Kurzbefehl und das Android-Teilen
  landen also automatisch im richtigen Archiv, ohne dass der Haushalt angegeben werden muss. Das
  Token also im Haushalt erstellen, für den es gelten soll (in der Oberfläche den Haushalt
  umschalten, dann *Einstellungen → Handy & Tokens*; auf der Kommandozeile mit `--haushalt`).

### Sicherung

- `<DOCUSHELF_DATA_PATH>` enthält alle Haushalte: Die Sicherung aus Abschnitt 5 deckt sie ab.
  Zusätzlich auszuschließen sind die Indizes aller Haushalte: `/srv/docushelf/data/index` **und**
  `/srv/docushelf/data/haushalte/*/index`.
- `config/households.yaml` gehört zur Sicherung (sie legt fest, wer wohin darf); ebenso die
  Datei- oder Umgebungsgeheimnisse für IMAP.
- Einen einzelnen Haushalt kann man getrennt sichern und zurückspielen: sein Ordner unter
  `haushalte/<id>/` (bzw. beim Haushalt mit `path: "."` die Verzeichnisse `archiv/`, `eingang/`,
  `config/` ohne `households.yaml`, `protokoll/`, `papierkorb/`). Besitzer prüfen (`chown -R 1000:1000 …`).
- Verwaltet jeder Haushalt sein Archiv per SMB selbst, ist ein getrenntes Backup je Haushalt oft
  sinnvoll – etwa als eigenes borgmatic-Repository je Pfad.

### Kommandozeile

```sh
docker compose exec docushelf docushelf households              # Haushalte auflisten (auch: haushalte)
docker compose exec docushelf docushelf households --json
docker compose exec docushelf docushelf --haushalt eltern status
docker compose exec docushelf docushelf --haushalt eltern token create "Handy" --user opa
docker compose exec docushelf docushelf --haushalt eltern rebuild
```

Mit mehreren Haushalten verlangen alle Befehle, die auf Daten wirken (`status`, `rebuild`,
`reconcile`, `import-paperless`, `process`, `token`), die Angabe `--haushalt ID` (auch
`--household`); fehlt sie, nennt die Fehlermeldung die vorhandenen Kennungen. Ohne
`households.yaml` ist die Angabe nie nötig. `serve`, `healthcheck`, `households` und
`hash-password` brauchen sie nicht – `serve` bedient immer alle Haushalte.

---

## 14. Einstellungen in der Weboberfläche

Fast alles, was in der `.env` steht, lässt sich auch im Browser unter **Einstellungen** pflegen –
eine Seite pro Thema: Haushalte, Eingangskanäle, Ablage & Auto-Ablage, KI, Verarbeitung,
Benachrichtigungen, Handy & Tokens, Anmeldung & Sicherheit, Protokoll und System.

**Wer darf was?** Die Übersicht, *Handy & Tokens* (eigene Tokens, Kurzbefehl, Android-Hilfe),
das *Protokoll* und Index/Abgleich unter *System* sieht jede angemeldete Person. Alles, was die
ganze Installation betrifft, sehen und ändern nur Admins: der lokale Notfallzugang,
`DOCUSHELF_ADMIN_USERS` / `DOCUSHELF_ADMIN_GROUPS` und – mit mehreren Haushalten – `admins:` /
`admin_groups:` aus `households.yaml`.

**Wohin wird gespeichert?**

| Was | Datei |
|---|---|
| Einstellungen der Installation | `<daten>/config/settings.yaml` (vorige Fassung: `settings.yaml.bak`) |
| Automatische Ablage, Namen, Benachrichtigungen eines Haushalts (bei mehreren Haushalten) | sein Eintrag in `households.yaml` (`.bak` daneben) |
| Mitglieder, Gruppen, Admins, Name eines Haushalts | `households.yaml` |
| Postfächer, Scan-Adressen, Scanner- und Watch-Ordner | `<haushalt>/config/channels.yaml` |
| Geheimnisse (OIDC-Client-Secret, API-Schlüssel des Sprachmodells, SMTP-Passwort, IMAP-Passwörter) | `config/secrets/…` (Datei 600, Ordner 700), in `settings.yaml` nur als `…_file: secrets/…` |

Jede Änderung wird vor dem Schreiben mit demselben Lader geprüft wie beim Start – eine Eingabe,
mit der DocuShelf nicht mehr starten würde, wird abgelehnt. Jede Änderung steht im Protokoll
(„Einstellungen geändert“), Geheimnisse werden nie angezeigt und nie protokolliert.

**Umgebungsvariablen haben Vorrang.** Ist ein Wert in der `.env` gesetzt, zeigt die Seite ihn
gesperrt mit „festgelegt über Umgebungsvariable DOCUSHELF_…“. Wer etwas lieber im Browser
pflegt, nimmt die Zeile aus der `.env` und startet neu.

**Sofort oder nach dem Neustart?** Automatische Ablage, Benachrichtigungs-Adresse, Namen im
Haushalt, Papierkorb- und Aufbewahrungsfristen, Haushaltsmitglieder und alle Eingangskanäle
gelten sofort. Alles andere (KI, Texterkennung, Anmeldung, Zeitzone …) wirkt nach einem Neustart;
bis dahin zeigt jede Einstellungsseite „Neustart nötig, damit … wirkt“ mit dem Knopf
**Jetzt neu starten**. Er beendet DocuShelf sauber; die Neustart-Regel des Containers
(`restart: unless-stopped` in `compose.yaml`) startet es wieder. Ohne eine solche Regel (z. B.
`docushelf serve` von Hand oder ein systemd-Dienst mit `Restart=on-failure`) muss es danach von
Hand bzw. mit `Restart=always` gestartet werden.

**Aussperren verhindern.** Die Abschnitte unter *Anmeldung & Sicherheit* speichern erst nach
einem Haken „Ich habe geprüft, dass ich mich danach noch anmelden kann“. Abgelehnt wird außerdem,
was die Anmeldung ganz abschalten würde (kein Anmeldedienst und kein Notfallzugang), was dich als
SSO-Konto aussperren oder dir selbst die Administratorrechte nehmen würde. Der Notfallzugang
selbst lässt sich bewusst nur über `DOCUSHELF_ADMIN_PASSWORD_HASH` ändern.

**Notfall:** Startet DocuShelf nach einer Änderung nicht mehr (etwa wegen einer von Hand
bearbeiteten Datei), `config/settings.yaml.bak` zurück nach `settings.yaml` kopieren oder den
störenden Eintrag löschen – ohne Eintrag gilt der Standardwert.
