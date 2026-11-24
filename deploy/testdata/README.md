# Teststack für die Eingangskanäle

Mit `deploy/compose.dev.yaml` lassen sich alle Scan-Kanäle lokal ausprobieren – ganz ohne
Drucker. Jedes Profil startet den Kanal und einen kleinen Client, der
`beispiel-rechnung.pdf` einliefert, so wie es ein Drucker tun würde.

| Profil | Kanal | Testclient |
|---|---|---|
| (immer) | DocuShelf mit Weboberfläche und SMTP-Empfänger | – |
| `smb` | Samba, Freigaben `scan` und `archiv` | `smb-client`: `smbclient … put` in die Freigabe `scan` |
| `ftp` | vsftpd, nur Upload | `ftp-client`: Upload mit `curl -T` |
| `smtp` | SMTP-Empfänger im App-Container | `smtp-client`: `send-mail.py` (Python, nur Standardbibliothek) |

## Starten

Aus dem Wurzelverzeichnis des Repositorys:

```sh
docker compose -f deploy/compose.dev.yaml --profile smb --profile ftp --profile smtp up --build
```

Nach ein paar Sekunden melden die Clients z. B. `SMB: smb-20261123-101500.pdf in die Freigabe scan
gelegt` und beenden sich. In der Weboberfläche <http://localhost:8000> (Notfallzugang `admin` /
`dev-admin-passwort`) erscheinen die drei Dokumente im Eingang, jeweils mit ihrer Quelle.

Einen Kanal erneut testen:

```sh
docker compose -f deploy/compose.dev.yaml --profile smb run --rm smb-client
docker compose -f deploy/compose.dev.yaml --profile ftp run --rm ftp-client
docker compose -f deploy/compose.dev.yaml --profile smtp run --rm smtp-client
```

Vom eigenen Rechner aus:

- Archiv-Freigabe im Finder/Explorer: `smb://localhost:1445/archiv`, Benutzer `dev`,
  Passwort `dev-test-passwort`.
- SMTP: `SMTP_HOST=127.0.0.1 python deploy/testdata/send-mail.py` (Port 2525). Der Empfänger
  nimmt nur `scan@docushelf.test` an.

## Daten und Aufräumen

Alle Daten landen in `dev-data/` im Repository (von git ignoriert). Der Dienst `init` legt die
Ordner an und übergibt sie an UID 1000; auf Linux gehören sie danach UID 1000.

```sh
docker compose -f deploy/compose.dev.yaml --profile smb --profile ftp --profile smtp down
rm -rf dev-data          # unter Linux ggf. mit sudo
```

## Eigene Testdateien

Weitere Dateien (Fotos, mehrseitige Scans …) einfach nach `deploy/testdata/` legen und z. B. mit
`SMTP_FILE=/testdata/foto.jpg` an den SMTP-Client übergeben oder im `smb-client`-Befehl den
Dateinamen ändern.

`beispiel-rechnung.pdf` ist ein erfundenes Dokument mit Textebene (Absender, Rechnungsdatum,
Kundennummer, IBAN, Betrag) – genug, damit Analyse und Ablagevorschläge etwas zu tun haben.
