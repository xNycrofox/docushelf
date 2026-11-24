"""Document types of a household archive and how to recognise them.

Classification is plain keyword scoring over the normalized text
(:func:`docushelf.util.textnorm.normalize`: lower case, umlauts as ae/oe/ue,
ß as ss, punctuation as spaces). Each type has

* ``heading`` – words that name the type in a heading or subject line
  ("Kontoauszug", "Abschlagsrechnung"); a hit there is the strongest signal,
* ``terms`` – weighted patterns that typically occur in the body,
* ``folder_words`` – normalized folder names that fit the type (used to find
  an existing folder when nothing better is known),
* ``new_folder`` – a template for a new folder proposal (``{sender}`` is
  replaced by the short sender name or dropped).

Specific types are listed before generic ones; on equal scores the earlier
type wins.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Classification only looks at the beginning of long documents.
MAX_CLASSIFY_CHARS = 40_000
# A heading hit counts this much on top of the term weights.
HEADING_WEIGHT = 4.0
# Below this score a document has no type.
MIN_SCORE = 3.0


@dataclass(frozen=True)
class DocType:
    name: str
    heading: str
    terms: tuple[tuple[str, float], ...]
    folder_words: tuple[str, ...] = ()
    new_folder: str = "Sonstiges"
    heading_re: re.Pattern[str] = field(init=False, repr=False, compare=False)
    term_res: tuple[tuple[re.Pattern[str], float], ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "heading_re", re.compile(rf"\b(?:{self.heading})\b"))
        object.__setattr__(
            self, "term_res", tuple((re.compile(rf"\b(?:{pattern})\b"), weight) for pattern, weight in self.terms)
        )


# A word ending in "rechnung" that is not an "...abrechnung" (Nebenkostenabrechnung,
# Gehaltsabrechnung) – those have their own types.
_RECHNUNG_WORD = r"(?!\w*abrechnung)\w*rechnung"

DOC_TYPES: tuple[DocType, ...] = (
    DocType(
        "Nebenkostenabrechnung",
        heading=r"nebenkostenabrechnung|betriebskostenabrechnung|heizkostenabrechnung|"
        r"abrechnung (?:der |ueber die )?(?:neben|betriebs|heiz)kosten",
        terms=(
            (r"nebenkostenabrechnung|betriebskostenabrechnung|heizkostenabrechnung", 5.0),
            (r"nebenkosten|betriebskosten|heizkosten", 2.0),
            (r"umlageschluessel|verteilerschluessel|verteilungsschluessel|umlage", 2.0),
            (r"abrechnungszeitraum", 1.0),
            (r"wohnflaeche|nutzflaeche|miteigentumsanteil", 1.0),
            (r"vorauszahlungen|vorauszahlung", 1.0),
            (r"muellabfuhr|muellentsorgung|strassenreinigung|hausmeister|hauswart|treppenhausreinigung|"
             r"gartenpflege|gebaeudeversicherung|allgemeinstrom|schornsteinfeger|wasserversorgung|entwaesserung", 0.6),
            (r"mieter|mieterin|vermieter|vermieterin|mietobjekt|wohnung", 1.0),
            (r"nachzahlung|guthaben", 0.5),
        ),
        folder_words=("nebenkosten", "nebenkostenabrechnung", "nebenkostenabrechnungen", "betriebskosten",
                      "wohnung", "miete", "mietwohnung", "haus"),
        new_folder="Wohnung/Nebenkosten",
    ),
    DocType(
        "Steuerbescheid",
        heading=r"\w*steuerbescheid|bescheid fuer \d{4} ueber|"
        r"bescheid ueber (?:\w+ )?(?:einkommen|grund|kraftfahrzeug|kfz|gewerbe|hunde)steuer",
        terms=(
            (r"\w*steuerbescheid", 4.0),
            (r"bescheid fuer \d{4} ueber", 3.0),
            (r"einkommensteuer|kraftfahrzeugsteuer|grundsteuer|hundesteuer|zweitwohnungsteuer", 2.5),
            (r"finanzamt", 2.0),
            (r"steuernummer|steuer nr|identifikationsnummer", 1.5),
            (r"solidaritaetszuschlag", 1.5),
            (r"zu versteuerndes einkommen|festgesetzte steuer|festgesetzt", 2.0),
            (r"einkuenfte aus", 1.5),
            (r"erstattung|nachzahlung", 0.5),
        ),
        folder_words=("steuer", "steuern", "steuerbescheid", "steuerbescheide", "finanzamt", "einkommensteuer",
                      "steuererklaerung", "steuererklaerungen"),
        new_folder="Finanzen/Steuern",
    ),
    DocType(
        "Gehaltsabrechnung",
        heading=r"entgeltabrechnung|gehaltsabrechnung|lohnabrechnung|verdienstabrechnung|bezuegeabrechnung|"
        r"bezuegemitteilung|abrechnung der brutto netto bezuege|lohn und gehaltsabrechnung|payslip",
        terms=(
            (r"entgeltabrechnung|gehaltsabrechnung|lohnabrechnung|verdienstabrechnung|bezuegemitteilung", 4.0),
            (r"brutto netto bezuege|bezuege", 2.0),
            (r"auszahlungsbetrag|auszahlung", 2.0),
            (r"gesamtbrutto|gesamt brutto|steuerbrutto|sv brutto", 2.0),
            (r"nettobezug|nettoverdienst|netto verdienst|nettolohn|nettogehalt|gesetzliches netto", 2.0),
            (r"lohnsteuer", 1.5),
            (r"steuerklasse|stkl", 2.0),
            (r"personalnummer|personal nr|pers nr", 1.5),
            (r"rentenversicherung|arbeitslosenversicherung|pflegeversicherung|krankenversicherung", 0.8),
            (r"sozialversicherung|sv nummer|rv nummer", 1.0),
            (r"urlaubsanspruch|resturlaub|urlaubstage", 1.0),
            (r"arbeitgeber|eintrittsdatum|eintritt", 0.5),
        ),
        folder_words=("gehalt", "gehaelter", "gehaltsabrechnung", "gehaltsabrechnungen", "lohn",
                      "lohnabrechnung", "lohnabrechnungen", "entgelt", "entgeltabrechnung", "entgeltabrechnungen",
                      "verdienstabrechnungen", "gehaltsnachweise", "arbeit", "arbeitgeber", "job"),
        new_folder="Arbeit/Gehaltsabrechnungen",
    ),
    DocType(
        "Kontoauszug",
        heading=r"kontoauszug|kontoauszuege|kreditkartenabrechnung|kreditkartenauszug|umsatzuebersicht|"
        r"depotauszug|finanzstatus|rechnungsabschluss|account statement|bank statement",
        terms=(
            (r"kontoauszug|kontoauszuege", 4.0),
            (r"kreditkartenabrechnung|kreditkartenauszug|depotauszug|umsatzuebersicht", 3.0),
            (r"alter kontostand|neuer kontostand|kontostand", 2.0),
            (r"buchungstag|buchungsdatum|buchungstext", 1.5),
            (r"wertstellung|valuta", 1.5),
            (r"saldo|anfangssaldo|endsaldo", 1.0),
            (r"umsaetze|umsatz", 0.8),
            (r"girokonto|tagesgeld|tagesgeldkonto|sparkonto|kreditkarte", 1.0),
            (r"lastschrift|gutschrift|ueberweisung|dauerauftrag|kartenzahlung", 0.6),
            (r"dispositionskredit|dispo|kreditrahmen|verfuegungsrahmen", 1.0),
        ),
        folder_words=("kontoauszug", "kontoauszuege", "auszuege", "bank", "banken", "girokonto", "konto",
                      "konten", "kreditkarte", "kreditkarten"),
        new_folder="Finanzen/Bank/{sender}",
    ),
    DocType(
        "Versicherungsschein",
        heading=r"versicherungsschein|versicherungspolice|police|nachtrag zum versicherungsschein|"
        r"versicherungsbestaetigung|deckungsbestaetigung",
        terms=(
            (r"versicherungsschein|versicherungspolice|versicherungsscheinnummer", 4.0),
            (r"police|policennummer", 2.0),
            (r"versicherungsnehmer|versicherungsnehmerin", 2.0),
            (r"versicherungsbeginn|versicherungsdauer|versicherungsablauf|hauptfaelligkeit", 1.5),
            (r"versicherungssumme|deckungssumme|deckung", 1.5),
            (r"versicherte person|versichertes risiko|versicherter|versicherungsumfang", 1.0),
            (r"selbstbeteiligung|selbstbehalt", 1.0),
            (r"tarif|tarifbezeichnung", 0.5),
            (r"haftpflicht\w*|hausrat\w*|rechtsschutz\w*|unfallversicherung|lebensversicherung|"
             r"berufsunfaehigkeit\w*|wohngebaeude\w*|kfz versicherung|kaskoversicherung|teilkasko|vollkasko", 1.0),
            (r"jahresbeitrag|beitrag", 0.5),
        ),
        folder_words=("versicherung", "versicherungen", "policen", "versicherungsscheine"),
        new_folder="Versicherungen/{sender}",
    ),
    DocType(
        "Beitragsrechnung",
        heading=r"beitragsrechnung|beitragsmitteilung|beitragsanpassung|beitragserhoehung|beitragsbescheinigung|"
        r"beitragsinformation|mitgliedsbeitrag",
        terms=(
            (r"beitragsrechnung|beitragsmitteilung|beitragsanpassung|beitragserhoehung", 4.0),
            (r"rundfunkbeitrag|beitragsnummer|beitragsservice", 2.5),
            (r"jahresbeitrag|mitgliedsbeitrag|monatsbeitrag|neuer beitrag", 1.5),
            (r"beitragszeitraum|beitragsjahr|beitragssatz", 1.5),
            (r"beitrag|beitraege", 1.0),
            (r"mitgliedschaft|mitgliedsnummer", 1.0),
        ),
        folder_words=("versicherung", "versicherungen", "beitraege", "mitgliedschaft", "mitgliedschaften",
                      "vereine", "verein"),
        new_folder="Versicherungen/{sender}",
    ),
    DocType(
        "Mahnung",
        heading=r"mahnung|\d mahnung|zahlungserinnerung|letzte mahnung",
        terms=(
            (r"mahnung|zahlungserinnerung", 4.0),
            (r"mahngebuehr|mahngebuehren|mahnkosten|verzugszinsen", 2.0),
            (r"noch nicht (?:beglichen|bezahlt|ausgeglichen|eingegangen)|nicht fristgerecht", 2.0),
            (r"ueberfaellig|ueberschritten|in verzug|verzug", 1.0),
            (r"offener betrag|offene posten|offener posten|offenen betrag|rueckstand", 1.5),
            (r"inkasso|gerichtliches mahnverfahren|mahnbescheid", 2.0),
        ),
        folder_words=("mahnung", "mahnungen"),
        new_folder="Rechnungen/{sender}",
    ),
    DocType(
        "Kündigung",
        heading=r"kuendigung|kuendigungsbestaetigung|bestaetigung (?:ihrer|der) kuendigung",
        terms=(
            (r"kuendigungsbestaetigung|bestaetigung ihrer kuendigung|bestaetigen ihre kuendigung", 4.0),
            (r"kuendigung", 2.5),
            (r"kuendige ich|kuendigen hiermit|kuendige hiermit|hiermit kuendige|kuendigen wir", 3.0),
            (r"zum naechstmoeglichen (?:zeitpunkt|termin)|naechstmoeglichen", 2.0),
            (r"fristgerecht|fristgemaess|ordentliche|ausserordentliche", 1.0),
            (r"vertragsende|beendigung des vertrag\w*|endet am|endet zum", 1.0),
        ),
        folder_words=("kuendigung", "kuendigungen", "vertraege"),
        new_folder="Verträge/{sender}",
    ),
    DocType(
        "Arztbrief",
        heading=r"arztbrief|befundbericht|entlassungsbericht|entlassbrief|entlassungsbrief|"
        r"konsiliarbericht|ambulanzbrief|befund",
        terms=(
            (r"arztbrief|befundbericht|entlassungsbericht|entlassbrief|entlassungsbrief", 4.0),
            (r"diagnose|diagnosen|verdachtsdiagnose", 2.0),
            (r"befund|befunde", 2.0),
            (r"anamnese", 2.0),
            (r"therapie|therapieempfehlung|procedere|empfehlung", 1.0),
            (r"medikation|medikamente", 1.5),
            (r"kollege|kollegin|liebe kollegen", 1.0),
            (r"patient|patientin", 1.0),
            (r"untersuchung|sonographie|roentgen|mrt|ct|ekg|labor|laborwerte", 0.8),
            (r"icd|icd 10", 1.0),
            (r"dr med|facharzt|fachaerztin|praxis|klinik|klinikum|krankenhaus|station", 0.5),
        ),
        folder_words=("gesundheit", "arzt", "aerzte", "arztbriefe", "befunde", "medizin", "medizinisches",
                      "krankheit", "krankenakte"),
        new_folder="Gesundheit/Befunde",
    ),
    DocType(
        "Rezept",
        heading=r"rezept|privatrezept|kassenrezept|e rezept|verordnung|heilmittelverordnung",
        terms=(
            (r"rezept|privatrezept|kassenrezept|e rezept", 3.0),
            (r"verordnung|heilmittelverordnung|verordnet", 2.0),
            (r"arzneimittel|medikament|wirkstoff|darreichungsform|packungsgroesse|n1|n2|n3", 1.0),
            (r"pzn", 2.0),
            (r"apotheke", 1.0),
            (r"zuzahlung|gebuehr frei|gebuehrenfrei|noctu|aut idem", 1.5),
        ),
        folder_words=("gesundheit", "rezepte", "apotheke", "medikamente"),
        new_folder="Gesundheit/Rezepte",
    ),
    DocType(
        "Zeugnis",
        heading=r"\w*zeugnis|halbjahresinformation|jahreszeugnis|abschlusszeugnis|arbeitszeugnis|zwischenzeugnis",
        terms=(
            (r"\w*zeugnis", 4.0),
            (r"versetzung|versetzt|nicht versetzt", 2.0),
            (r"schuljahr|halbjahr|schulhalbjahr|klassenstufe", 2.0),
            (r"klassenleiter\w*|klassenlehrer\w*|schulleiter\w*", 2.0),
            (r"zu unserer vollsten zufriedenheit|vollsten zufriedenheit|stets zu unserer", 3.0),
            (r"noten|note|sehr gut|befriedigend|ausreichend|mangelhaft", 0.5),
            (r"leistungen|verhalten|mitarbeit", 0.5),
        ),
        folder_words=("zeugnis", "zeugnisse", "schule", "ausbildung", "studium", "bewerbung", "bewerbungen"),
        new_folder="Ausbildung/Zeugnisse",
    ),
    DocType(
        "Lieferschein",
        heading=r"lieferschein|packliste|packzettel|versandbestaetigung|delivery note",
        terms=(
            (r"lieferschein|packliste|packzettel", 4.0),
            (r"versandbestaetigung|versandt|versendet", 1.5),
            (r"lieferung|geliefert|liefermenge|lieferadresse|lieferanschrift", 1.0),
            (r"sendungsnummer|sendungsverfolgung|paketnummer|trackingnummer", 1.5),
        ),
        folder_words=("lieferscheine", "bestellungen", "einkaeufe", "einkauf"),
        new_folder="Einkäufe/{sender}",
    ),
    DocType(
        "Auftragsbestätigung",
        heading=r"auftragsbestaetigung|bestellbestaetigung|bestellung|order confirmation",
        terms=(
            (r"auftragsbestaetigung|bestellbestaetigung", 4.0),
            (r"vielen dank fuer ihre bestellung|ihre bestellung|ihr auftrag|ihren auftrag", 1.5),
            (r"bestellnummer|bestell nr|auftragsnummer|auftrags nr", 1.0),
            (r"bestelldatum|auftragsdatum|voraussichtliche lieferung|liefertermin", 1.0),
        ),
        folder_words=("bestellung", "bestellungen", "einkaeufe", "einkauf", "auftraege"),
        new_folder="Einkäufe/{sender}",
    ),
    DocType(
        "Angebot",
        heading=r"angebot|kostenvoranschlag|kostenangebot|quote|quotation",
        terms=(
            (r"kostenvoranschlag|kostenangebot", 4.0),
            (r"angebot", 3.0),
            (r"angebotsnummer|angebots nr|angebotsdatum", 2.0),
            (r"gueltig bis|bindefrist|angebot gilt", 1.0),
            (r"freibleibend|unverbindlich", 1.5),
        ),
        folder_words=("angebot", "angebote", "kostenvoranschlaege"),
        new_folder="Einkäufe/{sender}",
    ),
    DocType(
        "Quittung",
        heading=r"quittung|kassenbon|kassenbeleg|zahlungsbeleg|receipt|barquittung",
        terms=(
            (r"quittung|barquittung", 4.0),
            (r"kassenbon|kassenbeleg|bon nr|bonnummer|beleg nr", 3.0),
            (r"bar bezahlt|barzahlung|bar erhalten|betrag erhalten|dankend erhalten|in bar", 2.0),
            (r"ec karte|girocard|kartenzahlung|kontaktlos|terminal id|trace nr", 1.0),
            (r"rueckgeld|gegeben", 1.5),
            (r"tse|kasse \d+|kassierer\w*|filiale", 0.8),
            (r"summe eur|summe", 1.0),
        ),
        folder_words=("quittung", "quittungen", "belege", "kassenbons", "kassenzettel"),
        new_folder="Einkäufe/Quittungen",
    ),
    DocType(
        "Gutschrift",
        heading=r"gutschrift|rechnungskorrektur|stornorechnung|storno|credit note",
        terms=(
            (r"gutschrift|gutschriftsnummer|rechnungskorrektur|stornorechnung", 4.0),
            (r"erstattung|rueckerstattung|erstatten|gutgeschrieben", 1.5),
            (r"storno|storniert|retoure|ruecksendung", 1.5),
        ),
        folder_words=("gutschrift", "gutschriften", "rechnungen"),
        new_folder="Rechnungen/{sender}",
    ),
    DocType(
        "Abrechnung",
        heading=r"jahresabrechnung|verbrauchsabrechnung|jahresverbrauchsabrechnung|stromabrechnung|"
        r"gasabrechnung|wasserabrechnung|heizungsabrechnung|endabrechnung|schlussabrechnung|turnusabrechnung",
        terms=(
            (r"jahresabrechnung|verbrauchsabrechnung|jahresverbrauchsabrechnung|stromabrechnung|gasabrechnung|"
             r"wasserabrechnung|endabrechnung|schlussabrechnung|turnusabrechnung", 4.0),
            (r"verbrauch|zaehlerstand|zaehlerstaende|ablesung|ablesedatum", 1.5),
            (r"kwh|kilowattstunden|m3|kubikmeter", 1.0),
            (r"grundpreis|arbeitspreis|verbrauchspreis", 1.5),
            (r"abschlagszahlungen|geleistete abschlaege|neuer abschlag", 1.5),
        ),
        folder_words=("abrechnung", "abrechnungen", "strom", "gas", "wasser", "energie", "heizung"),
        new_folder="Wohnung/{sender}",
    ),
    DocType(
        "Rechnung",
        heading=rf"{_RECHNUNG_WORD}|invoice|rechnungsnummer",
        terms=(
            (_RECHNUNG_WORD, 2.0),
            (r"rechnungsnummer|rechnungs nr|rechnung nr|rechnungsdatum|rechnungsbetrag|rechnungssumme", 2.5),
            (r"invoice|invoice number|invoice date", 2.0),
            (r"mwst|mehrwertsteuer|umsatzsteuer|ust|zzgl|inkl", 1.0),
            (r"nettobetrag|netto|bruttobetrag|brutto|zwischensumme", 0.5),
            (r"zahlbar bis|zahlbar innerhalb|zahlungsziel|zahlungsbedingungen|bitte ueberweisen", 1.0),
            (r"gesamtbetrag|endbetrag|zu zahlen|zu zahlender betrag", 1.0),
            (r"lieferdatum|leistungsdatum|leistungszeitraum", 0.8),
            (r"abschlag|abschlagsbetrag|abschlagszahlung", 1.0),
            (r"menge|einzelpreis|stueckpreis|artikel|pos", 0.5),
        ),
        folder_words=("rechnung", "rechnungen", "belege", "einkaeufe", "einkauf"),
        new_folder="Rechnungen/{sender}",
    ),
    DocType(
        "Bescheid",
        heading=r"\w*bescheid|festsetzung|bewilligung|ablehnung",
        terms=(
            (r"\w*bescheid", 2.5),
            (r"rechtsbehelfsbelehrung|rechtsmittelbelehrung", 2.0),
            (r"widerspruch|einspruch|klage", 1.0),
            (r"festgesetzt|festsetzung|bewilligt|bewilligung|abgelehnt|ablehnung", 1.0),
            (r"behoerde|landratsamt|stadtverwaltung|gemeinde|amt|bundesagentur|jobcenter|familienkasse|"
             r"rentenversicherung|krankenkasse", 0.5),
            (r"aktenzeichen|kassenzeichen|geschaeftszeichen", 0.8),
        ),
        folder_words=("bescheid", "bescheide", "behoerden", "aemter", "amt", "behoerde"),
        new_folder="Behörden/{sender}",
    ),
    DocType(
        "Bescheinigung",
        heading=r"\w*bescheinigung|bestaetigung|nachweis|zertifikat|teilnahmebestaetigung|"
        r"zuwendungsbestaetigung|spendenquittung",
        terms=(
            (r"\w*bescheinigung", 3.0),
            (r"zuwendungsbestaetigung|spendenquittung|teilnahmebestaetigung|zertifikat", 3.0),
            (r"hiermit wird bescheinigt|hiermit bescheinigen|hiermit bestaetigen|bescheinigt|wird bestaetigt", 2.0),
            (r"bestaetigung|bestaetigen|bestaetigt", 1.0),
            (r"nachweis|nachweise", 1.5),
        ),
        folder_words=("bescheinigung", "bescheinigungen", "nachweise", "nachweis", "bestaetigungen",
                      "zertifikate"),
        new_folder="Nachweise",
    ),
    DocType(
        "Vertrag",
        heading=r"\w*vertrag|vertragsbestaetigung|vertragsunterlagen|vereinbarung|auftragsbestaetigung zum vertrag",
        terms=(
            (r"\w*vertrag", 2.0),
            (r"mietvertrag|arbeitsvertrag|kaufvertrag|darlehensvertrag|leasingvertrag|dienstvertrag|"
             r"werkvertrag|ausbildungsvertrag|vertragsbestaetigung", 3.0),
            (r"vertragspartner|vertragsparteien|vertragsgegenstand", 2.0),
            (r"vertragsbeginn|vertragslaufzeit|laufzeit|mindestlaufzeit", 1.0),
            (r"vereinbarung|vereinbaren|vereinbart", 1.0),
            (r"unterschrift|unterzeichnet|ort datum", 1.0),
            (r"kuendigungsfrist", 0.5),
        ),
        folder_words=("vertrag", "vertraege"),
        new_folder="Verträge/{sender}",
    ),
    DocType(
        "Information",
        heading=r"information|informationen|kundeninformation|mitteilung|wichtige information|"
        r"renteninformation|newsletter|hinweis|aenderungsmitteilung|preisaenderung|preisanpassung",
        terms=(
            (r"renteninformation", 4.0),
            (r"information|informationen|informieren|mitteilung|mitteilen", 1.0),
            (r"wichtige hinweise|hinweis|hinweise", 0.8),
            (r"aenderung|aenderungen|preisaenderung|preisanpassung", 0.8),
            (r"newsletter", 2.0),
        ),
        folder_words=("korrespondenz", "schriftverkehr", "briefe", "post", "informationen"),
        new_folder="Korrespondenz/{sender}",
    ),
)

BY_NAME: dict[str, DocType] = {doc_type.name: doc_type for doc_type in DOC_TYPES}
NAMES: tuple[str, ...] = tuple(doc_type.name for doc_type in DOC_TYPES)
# Folder for documents of unknown type: with a known sender, the sender gets its own folder.
DEFAULT_NEW_FOLDER = "Korrespondenz/{sender}"
FALLBACK_NEW_FOLDER = "Sonstiges"


def score_types(text_norm: str, heading_norm: str = "") -> list[tuple[str, float]]:
    """Score of every type, best first. Both arguments must be normalized text."""
    text = (text_norm or "")[:MAX_CLASSIFY_CHARS]
    heading = heading_norm or ""
    results: list[tuple[int, str, float]] = []
    for position, doc_type in enumerate(DOC_TYPES):
        score = 0.0
        if heading and doc_type.heading_re.search(heading):
            score += HEADING_WEIGHT
        for regex, weight in doc_type.term_res:
            hits = sum(1 for _ in zip(range(4), regex.finditer(text)))
            if hits:
                # Repetition helps a little, but one strong word should not be outvoted by noise.
                score += weight * (1.0 + 0.25 * (hits - 1))
        results.append((position, doc_type.name, round(score, 3)))
    results.sort(key=lambda entry: (-entry[2], entry[0]))
    return [(name, score) for _, name, score in results]


def classify(text_norm: str, heading_norm: str = "") -> str | None:
    """The most likely document type, or None if nothing is convincing."""
    ranked = score_types(text_norm, heading_norm)
    if not ranked or ranked[0][1] < MIN_SCORE:
        return None
    return ranked[0][0]


def canonical_name(value: str | None) -> str | None:
    """Map user input like "rechnung" or "Kuendigung" to the canonical type name."""
    if not value:
        return None
    from ..util.textnorm import normalize

    wanted = normalize(value)
    for name in NAMES:
        if normalize(name) == wanted:
            return name
    return None


def folder_words(name: str | None) -> tuple[str, ...]:
    doc_type = BY_NAME.get(name or "")
    return doc_type.folder_words if doc_type else ()


def new_folder_template(name: str | None) -> str:
    doc_type = BY_NAME.get(name or "")
    if doc_type is None:
        return DEFAULT_NEW_FOLDER
    return doc_type.new_folder
