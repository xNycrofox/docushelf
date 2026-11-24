"""Tests for the text analysis (dates, sender, amounts, IBANs, references, type)
on realistic German household documents."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from docushelf.analysis import doctypes
from docushelf.analysis.extract import (
    analyze,
    entity_key,
    format_amount,
    iban_valid,
    name_from_domain,
    name_from_mail,
    parse_date_text,
    short_sender,
)
from docushelf.models import Analysis

TODAY = date(2025, 11, 20)

# Valid IBANs (checksums computed for these test account numbers).
IBAN_COMMERZ = "DE89370400440532013000"
IBAN_OWN = "DE38850503001234567890"
IBAN_SWL = "DE24860555921100223344"
IBAN_SPK_L = "DE07120300001055667788"

SHOP_INVOICE = """Muster Elektronik GmbH · Industriestr. 5 · 04109 Leipzig
Rechnungsadresse:
Max Mustermann
Musterweg 5
01234 Dresden
Rechnung
Rechnungsnummer: RE-2025-004711
Rechnungsdatum: 14.03.2025
Bestellnummer: 302-1234567-7654321
Bestelldatum: 10.03.2025
Kundennummer: K-98765
Pos. Artikel Menge Einzelpreis Gesamt
1 USB-C Ladegerät 65 W 1 49,99 € 49,99 €
2 HDMI-Kabel 2 m 2 25,00 € 50,00 €
Zwischensumme (netto) 84,03 €
zzgl. 19 % MwSt. 15,97 €
Gesamtbetrag 100,00 €
Zahlbar bis 28.03.2025 ohne Abzug.
Vielen Dank für Ihren Einkauf!
Muster Elektronik GmbH · Sitz Leipzig · Amtsgericht Leipzig HRB 12345 · USt-IdNr. DE123456789
Bankverbindung: Commerzbank AG · IBAN DE89 3704 0044 0532 0130 00 · BIC COBADEFFXXX
www.muster-elektronik.de · service@muster-elektronik.de"""

STADTWERKE = """Stadtwerke Leipzig GmbH
Augustusplatz 7 · 04109 Leipzig
Stadtwerke Leipzig GmbH · Postfach 10 06 26 · 04006 Leipzig
Herrn
Max Mustermann
Musterweg 5
04229 Leipzig
Abschlagsrechnung Strom
Rechnungsdatum: 03.02.2025
Kundennummer: 4711 0815
Vertragskonto: 2000 1234 5678
Zählernummer: 1ESY1161234567
Lieferstelle: Musterweg 5, 04229 Leipzig
Leistungszeitraum: 01.02.2025 - 31.01.2026
Sehr geehrter Herr Mustermann,
für Ihre Stromlieferung berechnen wir folgende Abschläge:
Monatlicher Abschlag ab 01.03.2025: 85,00 €
Der Abschlag wird am 15.03.2025 von Ihrem Konto DE24 8605 5592 1100 2233 44 abgebucht (Mandatsreferenz SWL-998877).
Zahlbar bis 15.02.2025.
Mit freundlichen Grüßen
Ihre Stadtwerke Leipzig
Stadtwerke Leipzig GmbH · Sitz Leipzig · Amtsgericht Leipzig HRB 4711 · Geschäftsführer: Max Muster
Bankverbindung: Sparkasse Leipzig · IBAN DE07 1203 0000 1055 6677 88 · BIC WELADE8LXXX"""

KONTOAUSZUG = """Sparkasse Dresden
Kontoauszug 3/2025                Seite 1 von 1
Kontoauszug Nr. 3 vom 31.03.2025
Herrn
Max Mustermann
Musterweg 5
01234 Dresden
Girokonto IBAN DE38 8505 0300 1234 5678 90 BIC OSDDDE81XXX
Buchungstag Wertstellung Verwendungszweck Betrag
Alter Kontostand vom 28.02.2025 1.234,56 EUR
03.03.2025 03.03.2025 Lastschrift AMAZON PAYMENTS EUROPE S.C.A. -45,99
05.03.2025 05.03.2025 Gutschrift Gehalt MUSTER SOFTWARE GMBH 2.890,12
10.03.2025 10.03.2025 Kartenzahlung REWE SAGT DANKE -63,20
Neuer Kontostand vom 31.03.2025 4.015,49 EUR
Sparkasse Dresden · Güntzplatz 5 · 01307 Dresden"""

STEUERBESCHEID = """Finanzamt Dresden-Nord
Rabenerstraße 1, 01069 Dresden
Finanzamt Dresden-Nord · Postfach 10 06 54 · 01076 Dresden
Herrn und Frau
Max und Anna Mustermann
Musterweg 5
01234 Dresden
Steuernummer 202/123/45678
Datum 14.05.2025
Bescheid für 2024
über Einkommensteuer, Solidaritätszuschlag und Kirchensteuer
Festsetzung
Die Steuer wird festgesetzt auf:
Einkommensteuer 8.123,00 €
Solidaritätszuschlag 0,00 €
zu versteuerndes Einkommen 52.345 €
Abrechnung
Erstattung 1.234,56 €
Der Betrag wird auf das Konto DE89 3704 0044 0532 0130 00 überwiesen.
Rechtsbehelfsbelehrung
Der Bescheid kann mit dem Einspruch angefochten werden. Der Einspruch ist innerhalb eines Monats
nach Bekanntgabe des Bescheids beim Finanzamt Dresden-Nord schriftlich einzulegen."""

VERSICHERUNGSSCHEIN = """Allianz Versicherungs-AG
Königinstraße 28 · 80802 München
Allianz Versicherungs-AG · 10850 Berlin
Herrn
Max Mustermann
Musterweg 5
01234 Dresden
Versicherungsschein
Privathaftpflichtversicherung
Versicherungsscheinnummer: AS-123/456789
Datum: 05.12.2024
Versicherungsnehmer: Max Mustermann, geb. 01.02.1985
Versicherungsbeginn: 01.01.2025, 00:00 Uhr
Versicherungsablauf: 01.01.2026
Hauptfälligkeit: 01.01.
Deckungssumme: 50.000.000 €
Jahresbeitrag: 65,40 €
Der Beitrag wird am 01.01.2025 von Ihrem Konto DE89 3704 0044 0532 0130 00 abgebucht."""

ENTGELTABRECHNUNG = """Muster Software GmbH · Hauptstraße 10 · 01067 Dresden
Herrn
Max Mustermann
Musterweg 5
01234 Dresden
Entgeltabrechnung für Oktober 2025
Personal-Nr. 00123   Steuerklasse 1   Geb.-Datum 01.02.1985   Eintritt 01.04.2020
Bezeichnung Betrag
Gehalt 4.500,00
Gesamtbrutto 4.500,00
Lohnsteuer 812,41
Solidaritätszuschlag 0,00
Kirchensteuer 64,99
Krankenversicherung 365,63
Rentenversicherung 418,50
Arbeitslosenversicherung 58,50
Pflegeversicherung 76,50
Nettoverdienst 2.703,47
Auszahlungsbetrag 2.703,47
Überweisung auf DE38 8505 0300 1234 5678 90"""

ARZTBRIEF = """Praxis Dr. med. Anna Schmidt
Fachärztin für Innere Medizin
Hauptstraße 5 · 01067 Dresden · Tel. 0351 123456
Praxis Dr. med. Schmidt · Hauptstraße 5 · 01067 Dresden
Herrn
Dr. med. Thomas Weber
Facharzt für Allgemeinmedizin
Bahnhofstraße 12
01069 Dresden
Dresden, 12.03.2025
Arztbrief
Patient: Max Mustermann, geb. 01.02.1985
Sehr geehrter Herr Kollege Weber,
wir berichten über o. g. Patienten, der sich am 10.03.2025 in unserer Sprechstunde vorstellte.
Diagnose: Arterielle Hypertonie (I10.90)
Befund: RR 150/95 mmHg, EKG unauffällig.
Therapieempfehlung: Ramipril 5 mg 1-0-0
Mit freundlichen kollegialen Grüßen
Dr. med. Anna Schmidt"""

NEBENKOSTEN = """Hausverwaltung Meier GmbH · Lindenallee 7 · 04109 Leipzig
Frau und Herrn
Anna und Max Mustermann
Musterweg 5
01234 Dresden
Leipzig, den 15.06.2025
Betriebskostenabrechnung 2024
Objekt: Musterweg 5, 01234 Dresden, Wohnung Nr. 3
Abrechnungszeitraum: 01.01.2024 bis 31.12.2024
Sehr geehrte Frau Mustermann, sehr geehrter Herr Mustermann,
nachfolgend erhalten Sie die Abrechnung der Betriebskosten für das Jahr 2024.
Kostenart Gesamtkosten Umlageschlüssel Ihr Anteil
Grundsteuer 1.200,00 € Wohnfläche 120,00 €
Müllabfuhr 800,00 € Personen 80,00 €
Hausmeister 2.400,00 € Wohnfläche 240,00 €
Summe Ihrer Kosten 1.434,56 €
abzüglich Ihrer Vorauszahlungen 1.200,00 €
Nachzahlung 234,56 €
Bitte überweisen Sie den Betrag bis zum 15.07.2025 auf unser Konto DE07 1203 0000 1055 6677 88.
Mit freundlichen Grüßen
Hausverwaltung Meier GmbH"""

FORWARDED_MAIL = """Von: Max Mustermann <max@example.org>
Gesendet: Montag, 3. März 2025 09:12
An: dokumente@example.org
Betreff: WG: Ihre Rechnung von Alza

---------- Weitergeleitete Nachricht ----------
Von: Alza.de <rechnung@alza.de>
Datum: So., 2. März 2025 um 18:40
Betreff: Ihre Rechnung Nr. 2025123456
An: Max Mustermann <max@example.org>

Sehr geehrter Herr Mustermann,
vielen Dank für Ihren Einkauf bei Alza.de. Im Anhang finden Sie Ihre Rechnung Nr. 2025123456.
Rechnungsbetrag: 249,90 €
Der Betrag wurde von Ihrer Kreditkarte abgebucht.
Ihr Alza.de Team"""
FORWARDED_CONTEXT = {
    "mail_from": "max@example.org",
    "mail_from_name": "Max Mustermann",
    "mail_subject": "WG: Ihre Rechnung von Alza",
    "forwarded_by_owner": True,
}


def run(text: str | list[str], context=None, known=None) -> Analysis:
    pages = text if isinstance(text, list) else [text]
    return analyze(pages, context, known_senders=known, today=TODAY)


# ---------------------------------------------------------------------------
# Complete documents

DOCUMENTS = [
    pytest.param(
        SHOP_INVOICE, None,
        dict(doc_type="Rechnung", doc_date="2025-03-14", sender="Muster Elektronik GmbH", total="100,00 €",
             ibans=[IBAN_COMMERZ],
             references={"Rechnungsnummer": ["RE-2025-004711"], "Bestellnummer": ["302-1234567-7654321"],
                         "Kundennummer": ["K-98765"]}),
        id="online-shop-invoice",
    ),
    pytest.param(
        STADTWERKE, None,
        dict(doc_type="Rechnung", doc_date="2025-02-03", sender="Stadtwerke Leipzig GmbH", total="85,00 €",
             ibans=[IBAN_SWL, IBAN_SPK_L],
             references={"Kundennummer": ["4711 0815"], "Vertragsnummer": ["2000 1234 5678"],
                         "Zählernummer": ["1ESY1161234567"], "Mandatsreferenz": ["SWL-998877"]}),
        id="stadtwerke-abschlag",
    ),
    pytest.param(
        KONTOAUSZUG, None,
        dict(doc_type="Kontoauszug", doc_date="2025-03-31", sender="Sparkasse Dresden", total="4.015,49 €",
             ibans=[IBAN_OWN], references={}),
        id="sparkasse-kontoauszug",
    ),
    pytest.param(
        STEUERBESCHEID, None,
        dict(doc_type="Steuerbescheid", doc_date="2025-05-14", sender="Finanzamt Dresden-Nord", total="1.234,56 €",
             ibans=[IBAN_COMMERZ], references={"Steuernummer": ["202/123/45678"]}),
        id="steuerbescheid",
    ),
    pytest.param(
        VERSICHERUNGSSCHEIN, None,
        dict(doc_type="Versicherungsschein", doc_date="2024-12-05", sender="Allianz Versicherungs-AG",
             total="65,40 €", ibans=[IBAN_COMMERZ], references={"Versicherungsnummer": ["AS-123/456789"]}),
        id="versicherungsschein",
    ),
    pytest.param(
        ENTGELTABRECHNUNG, None,
        dict(doc_type="Gehaltsabrechnung", doc_date="2025-10-01", sender="Muster Software GmbH",
             total="2.703,47 €", ibans=[IBAN_OWN], references={"Personalnummer": ["00123"]}),
        id="entgeltabrechnung",
    ),
    pytest.param(
        ARZTBRIEF, None,
        dict(doc_type="Arztbrief", doc_date="2025-03-12", sender="Praxis Dr. med. Anna Schmidt", total=None,
             ibans=[], references={}),
        id="arztbrief",
    ),
    pytest.param(
        NEBENKOSTEN, None,
        dict(doc_type="Nebenkostenabrechnung", doc_date="2025-06-15", sender="Hausverwaltung Meier GmbH",
             total="234,56 €", ibans=[IBAN_SPK_L], references={}),
        id="nebenkostenabrechnung",
    ),
    pytest.param(
        FORWARDED_MAIL, FORWARDED_CONTEXT,
        dict(doc_type="Rechnung", doc_date="2025-03-02", sender="Alza", total="249,90 €", ibans=[],
             references={"Rechnungsnummer": ["2025123456"]}),
        id="forwarded-mail-invoice",
    ),
]


@pytest.mark.parametrize("text,context,expected", DOCUMENTS)
def test_realistic_documents(text, context, expected):
    result = run(text, context)
    assert result.doc_type == expected["doc_type"]
    assert result.doc_date == expected["doc_date"]
    assert result.sender == expected["sender"]
    assert result.total_amount == expected["total"]
    assert result.ibans == expected["ibans"]
    assert result.references == expected["references"]
    assert result.text_chars > 100


@pytest.mark.parametrize("text,context,expected", DOCUMENTS)
def test_recipient_is_never_the_sender(text, context, expected):
    result = run(text, context)
    for name in result.sender_candidates:
        assert "Mustermann" not in name and "Weber" not in name
    assert all("mustermann" not in word and "musterweg" not in word for word in result.keywords)


def test_analysis_serializes_round_trip():
    result = run(SHOP_INVOICE)
    again = Analysis.from_dict(result.to_dict())
    assert again == result


# ---------------------------------------------------------------------------
# Dates


def test_due_dates_birth_dates_and_periods_lose():
    invoice = run(SHOP_INVOICE)
    assert invoice.doc_date == "2025-03-14"
    due = next(d for d in invoice.dates if d.value == "2025-03-28")
    assert due.score < invoice.dates[0].score and due.label == "Zahlbar bis"

    policy = run(VERSICHERUNGSSCHEIN)
    birth = next(d for d in policy.dates if d.value == "1985-02-01")
    assert birth.score < 0
    start = next(d for d in policy.dates if d.value == "2025-01-01")
    assert start.score < 0.3

    utility = run(STADTWERKE)
    values = [d.value for d in utility.dates]
    assert values[0] == "2025-02-03"
    period = [d for d in utility.dates if d.value in ("2025-02-01", "2026-01-31")]
    assert period and all(d.score < 0.25 for d in period)


def test_only_a_due_date_gives_no_document_date():
    text = "Muster Online GmbH\nRechnung\nBitte zahlen Sie 19,99 € bis zum 05.12.2025.\nFällig am 05.12.2025"
    result = run(text)
    assert result.doc_date is None
    assert result.dates and result.dates[0].value == "2025-12-05"


def test_only_a_birth_date_gives_no_document_date():
    result = run("Mitgliedsausweis\nName: Max Mustermann\ngeb. 01.02.1985")
    assert result.doc_date is None


@pytest.mark.parametrize(
    "line,expected",
    [
        ("Rechnungsdatum: 12.3.25", "2025-03-12"),
        ("Datum: 12. März 2025", "2025-03-12"),
        ("Datum: 12. Mrz. 2025", "2025-03-12"),
        ("Datum: 3. Sept. 2025", "2025-09-03"),
        ("Datum: 2025-03-12", "2025-03-12"),
        ("Datum: 12.O3.2O25", "2025-03-12"),  # OCR read the zeros as letters
        ("Invoice date: March 14, 2025", "2025-03-14"),
        ("Dresden, den 7. Juli 2025", "2025-07-07"),
        ("Stand: 01.06.2025", "2025-06-01"),
        ("Bescheid vom 30.04.2025", "2025-04-30"),
    ],
)
def test_date_formats(line, expected):
    assert run(f"Muster GmbH\n{line}\nText").doc_date == expected


def test_labelled_date_beats_frequent_unlabelled_dates():
    text = "\n".join(
        ["Muster GmbH", "Lieferung 02.01.2025", "Lieferung 02.01.2025", "Lieferung 02.01.2025",
         "Ausstellungsdatum: 10.01.2025"]
    )
    assert run(text).doc_date == "2025-01-10"


def test_month_year_is_first_of_month_with_low_score():
    result = run("Gehaltsabrechnung März 2025\nBrutto 3.000,00")
    assert result.doc_date == "2025-03-01"
    assert result.dates[0].score < 0.5


def test_far_future_and_ancient_dates_are_dropped():
    result = run("Muster GmbH\nDatum: 01.06.2026\nGültig seit 01.01.1890\nDatum: 01.12.2025")
    values = [d.value for d in result.dates]
    assert "2026-06-01" not in values  # more than 60 days after "today"
    assert "1890-01-01" not in values
    assert result.doc_date == "2025-12-01"  # within 60 days: allowed


def test_reference_to_another_letter_is_not_the_document_date():
    text = """Telekom Deutschland GmbH
Telekom Deutschland GmbH · 53171 Bonn
Herrn Max Mustermann, Musterweg 5, 01234 Dresden
Zahlungserinnerung
Datum 02.06.2025
Kundennummer 1234567890
leider haben wir für die Rechnung vom 05.05.2025 noch keinen Zahlungseingang festgestellt.
Offener Betrag: 39,95 €
Bitte überweisen Sie den Betrag bis zum 16.06.2025."""
    result = run(text)
    assert result.doc_date == "2025-06-02"
    assert result.doc_type == "Mahnung"
    assert result.sender == "Telekom Deutschland GmbH"
    assert result.total_amount == "39,95 €"


def test_table_header_label_applies_to_values_below():
    text = "Muster GmbH\nRechnungsdatum Rechnungsnummer Kundennummer\n14.03.2025 RE-2025-0042 K-98765\nLieferung 02.03.2025"
    result = run(text)
    assert result.doc_date == "2025-03-14"
    assert result.references["Rechnungsnummer"] == ["RE-2025-0042"]
    assert result.references["Kundennummer"] == ["K-98765"]


def test_parse_date_text():
    assert parse_date_text("2025-03-12", today=TODAY) == date(2025, 3, 12)
    assert parse_date_text("12.03.2025", today=TODAY) == date(2025, 3, 12)
    assert parse_date_text("12. März 2025", today=TODAY) == date(2025, 3, 12)
    assert parse_date_text("2031-01-01", today=TODAY) is None
    assert parse_date_text("unbekannt", today=TODAY) is None
    assert parse_date_text("", today=TODAY) is None


# ---------------------------------------------------------------------------
# Sender


def test_known_sender_in_letterhead_gives_canonical_spelling():
    result = run(STADTWERKE, known=["Stadtwerke Leipzig"])
    assert result.sender == "Stadtwerke Leipzig"


def test_merchants_in_bank_statement_do_not_become_sender():
    result = run(KONTOAUSZUG, known=["Amazon", "REWE", "Sparkasse Dresden"])
    assert result.sender == "Sparkasse Dresden"
    assert "Amazon" in result.sender_candidates  # still offered, but not chosen


def test_own_name_as_known_sender_is_ignored_when_it_is_the_addressee():
    result = run(VERSICHERUNGSSCHEIN, known=["Max Mustermann", "Allianz"])
    assert result.sender == "Allianz"  # the known spelling wins
    assert "Max Mustermann" not in result.sender_candidates


def test_bank_and_register_lines_are_not_senders():
    result = run(SHOP_INVOICE)
    joined = " ".join(result.sender_candidates)
    assert "Commerzbank" not in joined and "Amtsgericht" not in joined


def test_doctor_recipient_with_title_is_not_the_sender():
    result = run(ARZTBRIEF)
    assert result.sender == "Praxis Dr. med. Anna Schmidt"
    assert not any("Weber" in name for name in result.sender_candidates)


def test_private_sender_address_block_above_recipient():
    text = """Hans Meier
Lindenallee 7
04109 Leipzig
Herrn
Max Mustermann
Musterweg 5
01234 Dresden
Leipzig, 01.03.2025
Mieterhöhung zum 01.06.2025"""
    result = run(text)
    assert result.sender == "Hans Meier"
    assert result.doc_date == "2025-03-01"


def test_direct_vendor_mail_uses_mail_sender():
    text = "Rechnung\nRechnungsnummer 2025-1001\nRechnungsdatum 01.04.2025\nGesamtbetrag 12,99 €"
    result = run(text, {"mail_from": "Vodafone Rechnungsservice <rechnung@vodafone.de>"})
    assert result.sender == "Vodafone"
    result = run(text, {"mail_from": "noreply@telekom.de"})
    assert result.sender == "Telekom"


def test_scanner_mail_sender_is_ignored():
    text = "Rechnung\nRechnungsdatum 01.04.2025"
    assert run(text, {"mail_from": "scanner@printer.local"}).sender is None
    assert run(text, {"mail_from": "Canon MF667 <scan@example.org>"}).sender is None


def test_forwarded_mail_from_context():
    text = "Ihre Rechnung\nRechnungsdatum 01.04.2025\nBetrag 12,99 €"
    context = {"mail_from": "max@example.org", "forwarded_by_owner": ["max@example.org"],
               "forwarded_from": "Otto GmbH & Co KG <service@otto.de>"}
    result = run(text, context)
    assert result.sender == "Otto GmbH & Co KG"
    # As written by the mail channels: address and name separately, flag as bool.
    context = {"mail_from": "max@example.org", "mail_from_name": "Max Mustermann", "forwarded_by_owner": True,
               "forwarded_from": "service@otto.de", "forwarded_from_name": "OTTO Kundenservice"}
    assert run(text, context).sender == "OTTO"


def test_allowlisted_vendor_address_does_not_hide_the_vendor():
    # The mail channel sets forwarded_by_owner for every individually allowlisted address.
    context = {"mail_from": "rechnung@vodafone.de", "mail_from_name": "Vodafone", "forwarded_by_owner": True}
    text = "Vodafone GmbH\nRechnung\nRechnungsdatum 01.04.2025\nGesamtbetrag 39,99 €"
    assert run(text, context).sender == "Vodafone GmbH"


def test_scan_to_mail_context_from_smtp():
    context = {"mail_from": "scanner@example.org", "mail_from_name": "Canon MF667", "forwarded_by_owner": True,
               "mail_subject": "Scan"}
    result = run(STADTWERKE, context)
    assert result.sender == "Stadtwerke Leipzig GmbH"


def test_all_caps_names_are_softened_and_ocr_noise_tolerated():
    text = """STADTWERKE DRESDEN GmbH
Rosenstraße 32 · 01067 Dresden
SW Dresden GmbH, Postfach 120345, 01005 Dresden
Herrn
Max Mustermarm
Musterweg 5
01234 Dresden
Datum: 12.O3.2O25
Kunden-Nr.: 1234567
Jahresverbrauchsabrechnung Strom
Abrechnungszeitraum 01.01.2024 - 31.12.2024
Rechnungsbetrag 1.023,45 €
abzüglich geleistete Abschläge 960,00 €
Nachzahlung 63,45 €"""
    result = run(text)
    assert result.sender == "Stadtwerke Dresden GmbH"
    assert result.doc_date == "2025-03-12"
    assert result.doc_type == "Abrechnung"
    assert result.total_amount == "63,45 €"
    assert result.references["Kundennummer"] == ["1234567"]


def test_sender_helpers():
    assert short_sender("Muster Elektronik GmbH & Co. KG") == "Muster Elektronik"
    assert short_sender("Allianz Versicherungs-AG") == "Allianz"
    assert short_sender("AOK PLUS") == "AOK PLUS"
    assert entity_key("Stadtwerke Leipzig GmbH") == entity_key("STADTWERKE LEIPZIG") == "stadtwerke leipzig"
    assert name_from_domain("stadtwerke-leipzig.de") == "Stadtwerke Leipzig"
    assert name_from_domain("www.amazon.co.uk") == "Amazon"
    assert name_from_domain("gmx.de") is None and name_from_domain("printer.local") is None
    assert name_from_mail("Alza.de <rechnung@alza.de>") == "Alza"
    assert name_from_mail("info@hausverwaltung-meier.de") == "Hausverwaltung Meier"
    assert name_from_mail("max.mustermann@gmail.com") is None


# ---------------------------------------------------------------------------
# Amounts, IBANs, references


def test_amounts_and_total_priorities():
    result = run(SHOP_INVOICE)
    assert result.amounts[:3] == ["49,99 €", "25,00 €", "50,00 €"]
    assert result.total_amount == "100,00 €"  # not the subtotal, not the VAT
    english = run("ACME Ltd.\nInvoice\nInvoice date: March 14, 2025\nTotal: EUR 1,234.50")
    assert english.total_amount == "1.234,50 €"
    assert english.sender == "ACME Ltd."


def test_thousands_separators_and_table_columns():
    # A quantity in front of a price must not be read as a thousands group; NBSP separators are.
    result = run("Muster GmbH\n1 199,00 €\nSumme 1\u00a0234,56 €")
    assert result.amounts == ["199,00 €", "1.234,56 €"]
    assert result.total_amount == "1.234,56 €"
    assert run("IBAN DE89\u00a03704\u00a00044\u00a00532\u00a00130\u00a000").ibans == [IBAN_COMMERZ]


def test_format_amount():
    assert format_amount(Decimal("1234.5")) == "1.234,50 €"
    assert format_amount(Decimal("-45.99")) == "-45,99 €"
    assert format_amount(Decimal("0")) == "0,00 €"


def test_iban_validation():
    assert iban_valid(IBAN_COMMERZ)
    assert iban_valid("DE89 3704 0044 0532 0130 00")
    assert not iban_valid("DE88370400440532013000")  # wrong checksum
    assert not iban_valid("DE8937040044053201300")  # wrong length
    text = "IBAN DE88 3704 0044 0532 0130 00\nIBAN: AT61 1904 3002 3457 3201\nGläubiger-ID DE98ZZZ09999999999"
    assert run(text).ibans == ["AT611904300234573201"]


def test_reference_labels():
    text = """Kd.-Nr.: 778899
Steuer-ID: 12 345 678 901
St.-Nr. 203/456/78901
Versichertennummer: A123456789
Aktenzeichen: 3 C 123/25
Kassenzeichen 1234.5678.9
Auftragsnummer: A-2025-77
Mitgliedsnummer 556677"""
    refs = run(text).references
    assert refs["Kundennummer"] == ["778899"]
    assert refs["Steuer-ID"] == ["12 345 678 901"]
    assert refs["Steuernummer"] == ["203/456/78901"]
    assert refs["Versicherungsnummer"] == ["A123456789"]
    assert refs["Aktenzeichen"] == ["3 C 123/25"]
    assert refs["Kassenzeichen"] == ["1234.5678.9"]
    assert refs["Auftragsnummer"] == ["A-2025-77"]
    assert refs["Mitgliedsnummer"] == ["556677"]


def test_customer_label_is_not_a_recipient_block():
    text = "Muster GmbH\nKunde Nr. 4711\nDatum 01.02.2025\nRechnung"
    result = run(text)
    assert result.sender == "Muster GmbH"
    assert result.references == {"Kundennummer": ["4711"]}


# ---------------------------------------------------------------------------
# Document types and keywords


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Fitness First Germany GmbH\nKündigungsbestätigung\nhiermit bestätigen wir Ihre Kündigung zum 31.12.2025.",
         "Kündigung"),
        ("Muster GmbH\n2. Mahnung\nTrotz Zahlungserinnerung ist der offene Betrag noch nicht beglichen. Mahngebühr 5,00 €",
         "Mahnung"),
        ("Ausdruck der elektronischen Lohnsteuerbescheinigung für 2024\nBruttoarbeitslohn 54.000,00", "Bescheinigung"),
        ("Privatrezept\nRp. Ibuprofen 600 mg N2 PZN 12345678\nApotheke", "Rezept"),
        ("Arbeitszeugnis\nHerr Mustermann erledigte seine Aufgaben stets zu unserer vollsten Zufriedenheit.", "Zeugnis"),
        ("Kostenvoranschlag\nAngebotsnummer 2025-17\nDas Angebot ist freibleibend und gültig bis 31.12.2025.",
         "Angebot"),
        ("Lieferschein\nSendungsnummer 00340434161234567890\nLieferung 3 Positionen", "Lieferschein"),
        ("Mietvertrag\nzwischen Hans Meier (Vermieter) und Max Mustermann (Mieter)\nVertragsbeginn 01.04.2020", "Vertrag"),
        ("Kassenbon\nREWE Markt GmbH\nSUMME EUR 12,34\nGegeben 20,00\nRückgeld 7,66\nKartenzahlung girocard",
         "Quittung"),
    ],
)
def test_doc_types(text, expected):
    assert run(text).doc_type == expected


def test_public_bodies_and_compound_tax_notices():
    kfz = run("Hauptzollamt Dresden\nHauptzollamt Dresden, Postfach 10 01 51, 01071 Dresden\nHerrn\nMax Mustermann\n"
              "Musterweg 5\n01234 Dresden\nKraftfahrzeugsteuerbescheid\nDatum: 21.08.2025\n"
              "Die Kraftfahrzeugsteuer wird ab 01.09.2025 festgesetzt.\nFälligkeit: 01.09.2025")
    assert (kfz.doc_type, kfz.sender, kfz.doc_date) == ("Steuerbescheid", "Hauptzollamt Dresden", "2025-08-21")
    broadcast = run("ARD ZDF Deutschlandradio\nBeitragsservice\n50656 Köln\nBeitragsnummer 123 456 789\n"
                    "Datum 15.01.2025\nZahlungsaufforderung\nRundfunkbeitrag für 01.01.2025 bis 31.03.2025: 55,08 €")
    assert broadcast.doc_type == "Beitragsrechnung"  # a regular bill, not a dunning letter
    assert broadcast.references == {"Beitragsnummer": ["123 456 789"]}
    assert broadcast.doc_date == "2025-01-15"
    pension = run("Deutsche Rentenversicherung Bund\n10704 Berlin\nVersicherungsnummer 12 010285 M 123\n"
                  "Renteninformation 2025\nBerlin, 12.05.2025")
    assert pension.doc_type == "Information" and pension.sender == "Deutsche Rentenversicherung Bund"
    assert pension.references == {"Versicherungsnummer": ["12 010285 M 123"]}


def test_seller_line_in_body_does_not_change_the_letterhead_name():
    text = """Amazon EU S.à r.l., Niederlassung Deutschland
Marcel-Breuer-Straße 12, 80807 München
Rechnung
Rechnungsdatum / Lieferdatum 05.11.2025
Bestelldatum 28.10.2025
Verkauft von Amazon EU S.à r.l.
Rechnungsbetrag 23,99 €
Lieferadresse
Max Mustermann
Musterweg 5
01234 Dresden"""
    result = run(text)
    assert result.sender == "Amazon EU S.à r.l."
    assert result.doc_date == "2025-11-05" and result.total_amount == "23,99 €"


def test_unclear_text_has_no_type():
    assert run("Hallo zusammen,\nbis morgen beim Grillen.\nViele Grüße").doc_type is None


def test_doctype_catalogue_is_consistent():
    names = [doc_type.name for doc_type in doctypes.DOC_TYPES]
    assert len(names) == len(set(names))
    for required in ("Rechnung", "Kontoauszug", "Vertrag", "Bescheid", "Steuerbescheid", "Mahnung", "Kündigung",
                     "Gehaltsabrechnung", "Versicherungsschein", "Beitragsrechnung", "Nebenkostenabrechnung",
                     "Arztbrief", "Rezept", "Zeugnis", "Bescheinigung", "Angebot", "Auftragsbestätigung",
                     "Lieferschein", "Quittung", "Information"):
        assert required in names
    assert doctypes.canonical_name("kuendigung") == "Kündigung"
    assert doctypes.canonical_name("unbekannt") is None
    assert "{sender}" in doctypes.new_folder_template("Versicherungsschein")


def test_keywords_describe_content_not_addresses():
    result = run(STEUERBESCHEID)
    assert "einkommensteuer" in result.keywords
    assert not {"max", "anna", "musterweg"} & set(result.keywords)


# ---------------------------------------------------------------------------
# Robustness


def test_empty_and_odd_input_never_raises():
    assert run([]).doc_date is None
    assert run(["", "   "]).sender is None
    assert analyze([None, ""], None).text_chars == 0  # type: ignore[list-item]
    weird = "\x00﻿€€€ 12,, 3..4 –– IBAN DE00 ::::\n" * 50
    run(weird)
    huge = (SHOP_INVOICE + "\n") * 200
    result = analyze([huge] * 3, None, today=TODAY)
    assert result.doc_date == "2025-03-14"


def test_known_senders_may_be_a_generator():
    result = analyze([STADTWERKE], None, known_senders=(name for name in ["Stadtwerke Leipzig"]), today=TODAY)
    assert result.sender == "Stadtwerke Leipzig"
