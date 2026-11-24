"""Tests for XMP metadata: files that must keep their bytes, characters XML cannot
carry, and the Paperless properties with their PDF/A declaration."""

from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest
from lxml import etree

from archive_support import make_encrypted_pdf, make_signed_pdf
from docushelf.errors import UnsupportedFile
from docushelf.models import DocMeta
from docushelf.pdf.xmp import (
    EXTRA_PROPERTIES,
    NS_DS,
    NS_PDFAPROP,
    NS_PDFASCHEMA,
    build_packet,
    is_signed,
    pdf_protection,
    read_meta,
    write_meta,
    xml_safe,
)

PDFA_PACKET = (
    b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    b'<rdf:Description rdf:about="" xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/" pdfaid:part="2" '
    b'pdfaid:conformance="B"/></rdf:RDF></x:xmpmeta>'
)


def _declared_properties(packet: bytes) -> list[list[str]]:
    """Property names per DocuShelf schema declaration found in ``packet``."""
    root = etree.fromstring(packet[packet.index(b"<x:xmpmeta") : packet.rindex(b"</x:xmpmeta>") + 12])
    result = []
    for uri in root.iter(f"{{{NS_PDFASCHEMA}}}namespaceURI"):
        if uri.text == NS_DS:
            item = uri.getparent()
            result.append([(name.text or "") for name in item.iter(f"{{{NS_PDFAPROP}}}name")])
    return result


# ---------------------------------------------------------------------------
# Signed and encrypted PDFs are never rewritten


@pytest.mark.parametrize(
    "variant",
    [
        {"sig_flags": 3},  # SignaturesExist | AppendOnly, as signing tools write it
        {"sig_flags": None},  # no /SigFlags, but a signature field with a value
        {"sig_flags": 0, "perms": True},
    ],
)
def test_write_meta_refuses_signed_pdfs(samples, tmp_path, variant):
    signed = make_signed_pdf(samples, tmp_path / "rechnung.pdf", ["Telekom Rechnung"], **variant)
    before = signed.read_bytes()
    target = tmp_path / "out.pdf"
    with pytest.raises(UnsupportedFile, match="signiert"):
        write_meta(signed, target, DocMeta(title="Rechnung", tags=["Telefon"]))
    assert not target.exists()
    assert signed.read_bytes() == before
    assert pdf_protection(signed) == "signed"


@pytest.mark.parametrize("user_password", ["geheim", ""])
def test_write_meta_refuses_encrypted_pdfs(samples, tmp_path, user_password):
    encrypted = make_encrypted_pdf(samples, tmp_path / "vertrag.pdf", ["Vertrag"], user_password=user_password)
    target = tmp_path / "out.pdf"
    with pytest.raises(UnsupportedFile, match="Verschlüsselt"):
        write_meta(encrypted, target, DocMeta(title="Vertrag"))
    assert not target.exists()
    assert pdf_protection(encrypted) == "encrypted"


def test_unsigned_signature_field_and_plain_forms_are_not_protected(samples, tmp_path):
    """An empty signature field (a form waiting to be signed) is no signature."""
    plain = samples.text_pdf(tmp_path / "formular-plain.pdf", ["Antrag"])
    form_pdf = tmp_path / "formular.pdf"
    with pikepdf.open(plain) as pdf:
        field = pdf.make_indirect(pikepdf.Dictionary(FT=pikepdf.Name.Sig, T=pikepdf.String("Unterschrift")))
        pdf.Root["/AcroForm"] = pdf.make_indirect(pikepdf.Dictionary(Fields=pikepdf.Array([field]), SigFlags=0))
        pdf.save(form_pdf)
    assert pdf_protection(form_pdf) is None
    with pikepdf.open(form_pdf) as pdf:
        assert not is_signed(pdf)
    write_meta(form_pdf, tmp_path / "out.pdf", DocMeta(title="Antrag"))
    assert read_meta(tmp_path / "out.pdf").title == "Antrag"


def test_signature_in_nested_field_with_inherited_type(samples, tmp_path):
    plain = samples.text_pdf(tmp_path / "plain.pdf", ["Vertrag"])
    nested = tmp_path / "nested.pdf"
    with pikepdf.open(plain) as pdf:
        signature = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Sig))
        kid = pdf.make_indirect(pikepdf.Dictionary(T=pikepdf.String("b"), V=signature))
        parent = pdf.make_indirect(pikepdf.Dictionary(FT=pikepdf.Name.Sig, T=pikepdf.String("a"), Kids=pikepdf.Array([kid])))
        kid["/Parent"] = parent
        pdf.Root["/AcroForm"] = pdf.make_indirect(pikepdf.Dictionary(Fields=pikepdf.Array([parent])))
        pdf.save(nested)
    assert pdf_protection(nested) == "signed"


def test_pdf_protection_of_plain_damaged_and_missing_files(samples, tmp_path):
    assert pdf_protection(samples.text_pdf(tmp_path / "plain.pdf", ["Brief"])) is None
    damaged = tmp_path / "kaputt.pdf"
    damaged.write_bytes(b"%PDF-1.4 nothing useful")
    assert pdf_protection(damaged) is None
    assert pdf_protection(tmp_path / "fehlt.pdf") is None


# ---------------------------------------------------------------------------
# Characters XML cannot carry


def test_xml_safe():
    assert xml_safe("Rechnung ￿ März￾") == "Rechnung  März"
    assert xml_safe("a\ud800b\udfffc") == "abc"
    assert xml_safe("a\x00b\x0bc\x1fd") == "a b c d"
    assert xml_safe("Zeile 1\r\nZeile 2\rZeile 3\tTab") == "Zeile 1\nZeile 2\nZeile 3\tTab"
    # Allowed by XML 1.0 and kept: other noncharacters, C1 controls, astral characters.
    assert xml_safe("﷐\x85\U0001ffff😀") == "﷐\x85\U0001ffff😀"


def test_build_packet_cleans_every_value():
    meta = DocMeta(
        doc_id="1b4e28ba-2fa1-11d2-883f-0016d3cca427",
        title="Rechnung￿ März\x01",
        tags=["Strom￾", "\ud83d", "steuer\x0brelevant"],
        sender="Stadt\udc00werke",
        source="mail\x00",
        extra={"MailSubject": "Ihre Rechnung ￿", "MailFrom": "a@b.de\x02", "PaperlessNotes": "Zeile\r\nzwei"},
    )
    packet = build_packet(None, meta, metadata_date="2026-01-01T00:00:00+01:00￿")
    root = etree.fromstring(packet[packet.index(b"<x:xmpmeta") : packet.rindex(b"</x:xmpmeta>") + 12])
    text = etree.tostring(root, encoding="unicode")
    assert "￿" not in text and "￾" not in text
    assert "Rechnung März" in text and "Stadtwerke" in text and "steuer relevant" in text


def test_write_meta_accepts_values_with_invalid_characters(samples, tmp_path):
    """Every caller of write_meta is covered, not only the filing transaction."""
    src = samples.text_pdf(tmp_path / "a.pdf", ["Hallo"])
    dst = tmp_path / "b.pdf"
    write_meta(src, dst, DocMeta(doc_id="x", title="Abo ￿Netflix", tags=["TV￾"], sender="Netflix\ud800",
                                 extra={"MailSubject": "Re: ￿"}))
    back = read_meta(dst)
    assert back.title == "Abo Netflix" and back.tags == ["TV"] and back.sender == "Netflix"
    assert back.extra == {"MailSubject": "Re:"}
    with pikepdf.open(dst) as pdf:
        assert str(pdf.docinfo["/Title"]) == "Abo Netflix"


# ---------------------------------------------------------------------------
# Paperless notes and custom fields


def test_paperless_notes_and_fields_roundtrip(samples, tmp_path):
    assert "PaperlessNotes" in EXTRA_PROPERTIES and "PaperlessFields" in EXTRA_PROPERTIES
    notes = " | ".join(f"2024-03-{n:02d} max: Notiz {n} " + "x" * 200 for n in range(1, 20))
    assert len(notes) > 3000
    fields = "Betrag: EUR199.99; Status: bezahlt"
    src = samples.text_pdf(tmp_path / "a.pdf", ["Hallo"])
    dst = tmp_path / "b.pdf"
    write_meta(src, dst, DocMeta(doc_id="x", extra={"PaperlessNotes": notes, "PaperlessFields": fields}))
    assert read_meta(dst).extra == {"PaperlessNotes": notes, "PaperlessFields": fields}


def test_pdfa_extension_schema_declares_every_property():
    packet = build_packet(PDFA_PACKET, DocMeta(doc_id="x", extra={"PaperlessNotes": "n", "PaperlessFields": "f"}))
    [declared] = _declared_properties(packet)
    assert {"PaperlessNotes", "PaperlessFields", "DocumentId", "MailSubject"} <= set(declared)


def test_outdated_extension_schema_is_replaced_once():
    """A file filed by an older version declares fewer properties; the declaration is completed."""
    old = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/" pdfaid:part="2"/>'
        '<rdf:Description rdf:about="" xmlns:pdfaExtension="http://www.aiim.org/pdfa/ns/extension/" '
        'xmlns:pdfaSchema="http://www.aiim.org/pdfa/ns/schema#" xmlns:pdfaProperty="http://www.aiim.org/pdfa/ns/property#">'
        "<pdfaExtension:schemas><rdf:Bag>"
        '<rdf:li rdf:parseType="Resource"><pdfaSchema:schema>Fremd</pdfaSchema:schema>'
        "<pdfaSchema:namespaceURI>http://example.org/fremd/</pdfaSchema:namespaceURI>"
        "<pdfaSchema:prefix>fremd</pdfaSchema:prefix></rdf:li>"
        '<rdf:li rdf:parseType="Resource"><pdfaSchema:schema>DocuShelf document metadata</pdfaSchema:schema>'
        f"<pdfaSchema:namespaceURI>{NS_DS}</pdfaSchema:namespaceURI><pdfaSchema:prefix>docushelf</pdfaSchema:prefix>"
        '<pdfaSchema:property><rdf:Seq><rdf:li rdf:parseType="Resource"><pdfaProperty:name>DocumentId</pdfaProperty:name>'
        "<pdfaProperty:valueType>Text</pdfaProperty:valueType></rdf:li></rdf:Seq></pdfaSchema:property></rdf:li>"
        "</rdf:Bag></pdfaExtension:schemas></rdf:Description></rdf:RDF></x:xmpmeta>"
    ).encode()
    once = build_packet(old, DocMeta(doc_id="x", extra={"PaperlessNotes": "Mit Karte bezahlt"}))
    declarations = _declared_properties(once)
    assert len(declarations) == 1 and {"DocumentId", "PaperlessNotes", "PaperlessFields"} <= set(declarations[0])
    assert b"http://example.org/fremd/" in once  # other schemas stay
    twice = build_packet(once, DocMeta(doc_id="x", title="T"))
    assert twice.count(b"<pdfaSchema:namespaceURI>" + NS_DS.encode()) == 1


def test_read_meta_exposes_paperless_extras(samples, tmp_path):
    src = samples.text_pdf(tmp_path / "a.pdf", ["Hallo"])
    dst = Path(tmp_path / "b.pdf")
    write_meta(src, dst, DocMeta(doc_id="x", extra={"PaperlessNotes": "2024-03-01 max: Mit Karte bezahlt"}))
    assert read_meta(dst).extra["PaperlessNotes"] == "2024-03-01 max: Mit Karte bezahlt"
