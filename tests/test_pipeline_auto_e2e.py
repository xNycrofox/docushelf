"""End to end: scans arrive in the SMB drop zone and are filed automatically.

Real service container (intake, worker, pipeline, suggestion engine, decision,
archive operations, audit log), mode "confident", no language model. The
archive already holds a few documents, so the folder is learned by similarity.
"""

from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path

import pytest

from docushelf.config import load_settings
from docushelf.util.timeutil import now

LIABILITY = """Muster Versicherung AG
Postfach 12 34, 80333 München

Herrn Max Mustermann
Musterweg 1
01234 Musterstadt

München, den 15.01.{year}
Versicherungsschein-Nr. HV-4711-0815

Beitragsrechnung Privathaftpflichtversicherung {year}
Sehr geehrter Herr Mustermann,
für Ihre Privathaftpflichtversicherung berechnen wir den Jahresbeitrag {year}.
Versicherungsumfang: Privathaftpflicht Single Komfort, Deckungssumme 10 Mio. EUR,
Schlüsselverlust und Mietsachschäden mitversichert.
Jahresbeitrag: 68,40 EUR. Der Betrag wird am 01.02.{year} von Ihrem Konto abgebucht.
Mit freundlichen Grüßen
Ihre Muster Versicherung AG"""

STATEMENT = """Sparkasse Musterstadt
Kontoauszug Nr. {number}/2025 vom 30.{month:02d}.2025
Girokonto IBAN DE02 1203 0000 0000 2020 51
Buchungstag Wertstellung Verwendungszweck Betrag
02.{month:02d}.2025 Lastschrift Supermarkt Einkauf -54,20
05.{month:02d}.2025 Gutschrift Gehalt Arbeitgeber GmbH 2.450,00
12.{month:02d}.2025 Dauerauftrag Miete Wohnung -820,00
Kontostand alt 1.234,56 Kontostand neu 2.810,36"""

ELECTRICITY = """Stadtwerke Musterstadt GmbH
Jahresabrechnung Strom {year}
Zählernummer 1ESY1160123456, Vertragskonto 300012345
Abrechnungszeitraum 01.01.{year} bis 31.12.{year}
Verbrauch 2.345 kWh, Arbeitspreis 32,10 ct/kWh, Grundpreis 12,50 EUR/Monat
Neuer monatlicher Abschlag ab Februar: 75,00 EUR"""

UNRELATED = """Grundschule am Lindenplatz
Einladung zum Elternabend der Klasse 2b
Liebe Eltern, wir laden Sie herzlich zum Elternabend ein.
Themen: Klassenfahrt ins Schullandheim, Schwimmunterricht, Laternenumzug, Basteln für den Herbstmarkt.
Bitte bringen Sie Hausschuhe für die Kinder mit. Ihr Klassenteam"""


def wait_until(predicate, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, title: str, message: str, **kwargs) -> None:
        self.sent.append({"title": title, "message": message, **kwargs})


@pytest.fixture
def auto_services(env):
    pytest.importorskip("docushelf.suggest.auto")
    pytest.importorskip("docushelf.pdf.normalize")
    from docushelf.services import Services

    env.update(
        {
            "DOCUSHELF_AUTO_FILE": "confident",
            "DOCUSHELF_OCR_ENABLED": "false",
            "DOCUSHELF_NOTIFY_URL": "http://ntfy.invalid/docushelf",
        }
    )
    settings = load_settings(env)
    assert settings.auto_file.mode == "confident" and settings.llm is None
    services = Services(settings)
    services.notifier = RecordingNotifier()
    yield services
    services.worker.stop()
    services.stop()
    services.index.close()


def fill_archive(services, samples) -> None:
    archive = services.layout.archive
    for year in (2023, 2024, 2025):
        samples.text_pdf(
            archive / "Versicherungen" / "Haftpflicht" / f"{year}-01-15 Beitragsrechnung Haftpflicht {year}.pdf",
            LIABILITY.format(year=year).split("\n\n"),
        )
    for number, month in ((1, 1), (2, 2), (3, 3)):
        samples.text_pdf(
            archive / "Finanzen" / "Bank" / f"2025-{month:02d}-30 Kontoauszug {number}-2025.pdf",
            [STATEMENT.format(number=number, month=month)],
        )
    for year in (2023, 2024):
        samples.text_pdf(
            archive / "Wohnung" / "Strom" / f"{year + 1}-01-20 Jahresabrechnung Strom {year}.pdf",
            [ELECTRICITY.format(year=year)],
        )
    stats = services.reconciler.scan(full=True)
    assert stats.added == 8


def drop(services, samples, name: str, text: str | None = None, *, copy_of: Path | None = None) -> Path:
    zone = services.layout.source_dir("smb")
    target = zone / name
    if copy_of is not None:
        target.write_bytes(copy_of.read_bytes())
    else:
        samples.text_pdf(target, text.split("\n\n"))
    return target


def settle(services, item_ids: list[str]) -> None:
    """Wait until the items left processing and no job is left."""

    def done() -> bool:
        items = [services.inbox.find(item_id) for item_id in item_ids]
        if any(item is None or item.state == "arbeit" for item in items):
            return False
        if any(item.state == "bereit" and "auto" not in item.context for item in items):
            return False
        return services.index.pending_jobs() == []

    assert wait_until(done), [services.inbox.read_log(services.inbox.find(i)) for i in item_ids]


def test_scan_from_the_drop_zone_is_filed_automatically(auto_services, samples, tmp_path):
    services = auto_services
    started = now() - timedelta(seconds=5)
    fill_archive(services, samples)
    original = samples.text_pdf(tmp_path / "keep" / "scan_0001.pdf", LIABILITY.format(year=2026).split("\n\n"))
    services.worker.start()

    # 1. A letter like the ones in "Versicherungen/Haftpflicht" is filed there without asking.
    drop(services, samples, "scan_0001.pdf", copy_of=original)
    (letter,) = services.intake.scan_once()
    settle(services, [letter])

    filed = services.inbox.load(letter)
    assert filed.state == "erledigt", services.inbox.read_log(filed)
    auto = filed.context["auto"]
    assert auto["decision"] == "file" and auto["confidence"] >= 0.75 and auto["review"] is False
    assert "similarity" in auto["sources"]
    path = auto["path"]
    assert path.startswith("Versicherungen/Haftpflicht/2026-01-15 ") and path.endswith(".pdf")
    assert (services.layout.archive / path).is_file()
    record = services.index.get(auto["doc_id"])
    assert record is not None and record.path == path
    assert filed.filing["user"] == "automatisch"
    assert "Automatisch abgelegt als" in services.inbox.read_log(filed)

    (entry,) = services.audit.entries({"auto_file"}, started)
    assert entry["user"] == "automatisch" and entry["doc_id"] == record.id and entry["path"] == path
    assert entry["folder"] == "Versicherungen/Haftpflicht" and entry["item"] == letter
    assert entry["confidence"] == auto["confidence"] and entry["reasons"]
    assert [sent["title"] for sent in services.notifier.sent] == [f"Abgelegt: {record.title}"]
    assert services.notifier.sent[0]["message"].startswith("Versicherungen/Haftpflicht · ")

    # 2. Something unlike anything in the archive stays in the inbox, and so does
    # 3. the same scan arriving a second time.
    drop(services, samples, "scan_0002.pdf", UNRELATED)
    drop(services, samples, "scan_0003.pdf", copy_of=original)
    created = services.intake.scan_once()
    assert len(created) == 2
    settle(services, created)

    by_name = {services.inbox.load(i).original_files[0].split("/")[-1]: services.inbox.load(i) for i in created}
    unsure = by_name["scan_0002.pdf"]
    assert unsure.state == "bereit" and unsure.filing is None
    assert unsure.context["auto"]["decision"] == "hold"
    assert unsure.context["auto"]["confidence"] < 0.75 and unsure.context["auto"]["reasons"]
    assert "Nicht automatisch abgelegt:" in services.inbox.read_log(unsure)

    twin = by_name["scan_0003.pdf"]
    assert twin.state == "bereit" and twin.duplicate_of["kind"] == "archive" and twin.duplicate_of["path"] == path
    assert twin.context["auto"]["decision"] == "hold"
    assert any("Duplikat" in reason for reason in twin.context["auto"]["reasons"])

    # Nothing else reached the archive, and only the one filing was recorded and announced.
    assert len(services.audit.entries({"auto_file"}, started)) == 1
    assert len(services.notifier.sent) == 1
    pdfs = sorted(p.relative_to(services.layout.archive).as_posix() for p in services.layout.archive.rglob("*.pdf"))
    assert len(pdfs) == 9 and path in pdfs
