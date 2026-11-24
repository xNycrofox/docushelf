"""Tests for turning e-mails into inbox documents (channels/mailparse.py)."""

from __future__ import annotations

import base64
import io
import os
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timezone

import pytest
from PIL import Image

from docushelf.channels.mailparse import (
    classify,
    extract_documents,
    looks_forwarded,
    mail_key,
    normalize_address,
    parse_message,
    sender_allowed,
)
from tests.conftest import make_text_pdf, render_page_image


# -- builders -------------------------------------------------------------------------------------


@pytest.fixture
def pdf_bytes(tmp_path):
    counter = {"n": 0}

    def build(text: str = "Rechnung Nr. 4711") -> bytes:
        counter["n"] += 1
        path = make_text_pdf(tmp_path / f"doc{counter['n']}.pdf", [text])
        return path.read_bytes()

    return build


def jpeg_bytes(lines: list[str] | None = None, *, size=(1240, 1754)) -> bytes:
    buffer = io.BytesIO()
    render_page_image(lines or ["Kassenbon", "Summe 12,50 EUR"], size=size).save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def noise_png(size: tuple[int, int]) -> bytes:
    """An incompressible PNG (large in bytes, small in pixels) like a banner graphic."""
    image = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def tiny_logo() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (120, 40), (200, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def base_message(subject: str = "Rechnung", sender: str = "Max Muster <max@example.org>") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "dokumente@example.org"
    msg["Subject"] = subject
    msg["Date"] = format_datetime(datetime(2025, 3, 3, 9, 15, tzinfo=timezone.utc))
    msg["Message-ID"] = "<abc123@example.org>"
    msg.set_content("Hallo,\nanbei die Unterlagen.\n")
    return msg


RAW_HEADER = (
    b"From: =?UTF-8?Q?J=C3=BCrgen_M=C3=BCller?= <Juergen.Mueller@Example.ORG>\r\n"
    b"To: dokumente@example.org\r\n"
    b"Subject: =?UTF-8?Q?Rechnung_M=C3=A4rz?=\r\n"
    b"Date: Mon, 03 Mar 2025 10:15:00 +0100\r\n"
    b"Message-ID: <raw-1@example.org>\r\n"
    b"MIME-Version: 1.0\r\n"
    b"Content-Type: multipart/mixed; boundary=XX\r\n\r\n"
    b"--XX\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nSiehe Anhang.\r\n"
)


def raw_with_part(header_lines: list[bytes], payload: bytes) -> bytes:
    encoded = base64.encodebytes(payload)
    return (
        RAW_HEADER
        + b"--XX\r\n"
        + b"\r\n".join(header_lines)
        + b"\r\nContent-Transfer-Encoding: base64\r\n\r\n"
        + encoded
        + b"\r\n--XX--\r\n"
    )


# -- documents -----------------------------------------------------------------------------------


def test_pdfs_are_single_documents_and_images_are_combined(pdf_bytes):
    msg = base_message()
    msg.add_attachment(pdf_bytes("Rechnung A"), maintype="application", subtype="pdf", filename="rechnung-a.pdf")
    msg.add_attachment(jpeg_bytes(["Seite 1"]), maintype="image", subtype="jpeg", filename="seite1.jpg")
    msg.add_attachment(pdf_bytes("Rechnung B"), maintype="application", subtype="pdf", filename="rechnung-b.pdf")
    msg.add_attachment(jpeg_bytes(["Seite 2"]), maintype="image", subtype="jpeg", filename="seite2.jpg")
    msg.add_attachment(tiny_logo(), maintype="image", subtype="png", filename="logo.png")

    documents, context = extract_documents(msg)

    names = [[name for name, _ in doc] for doc in documents]
    assert names == [["rechnung-a.pdf"], ["seite1.jpg", "seite2.jpg"], ["rechnung-b.pdf"]]
    assert documents[0][0][1].startswith(b"%PDF-")
    assert context["mail_subject"] == "Rechnung"


def test_context_fields_are_decoded():
    raw = RAW_HEADER + b"--XX--\r\n"
    documents, context = extract_documents(raw)
    assert documents == []
    assert context == {
        "mail_from": "juergen.mueller@example.org",
        "mail_from_name": "Jürgen Müller",
        "mail_subject": "Rechnung März",
        "mail_date": "2025-03-03T10:15:00+01:00",
        "message_id": "<raw-1@example.org>",
    }


def test_mail_date_is_converted_to_local_time():
    msg = base_message()
    del msg["Date"]
    msg["Date"] = "Tue, 01 Jul 2025 22:30:00 +0000"
    _, context = extract_documents(msg)
    assert context["mail_date"] == "2025-07-02T00:30:00+02:00"


@pytest.mark.parametrize(
    "header_lines,expected",
    [
        (  # RFC 2047 encoded word inside the quoted parameter (Outlook, Thunderbird)
            [
                b'Content-Type: application/pdf; name="=?UTF-8?B?UmVjaG51bmcgTcOkcnoucGRm?="',
                b'Content-Disposition: attachment; filename="=?UTF-8?B?UmVjaG51bmcgTcOkcnoucGRm?="',
            ],
            "Rechnung März.pdf",
        ),
        (  # RFC 2231
            [b"Content-Type: application/pdf", b"Content-Disposition: attachment; filename*=UTF-8''Rechnung%20M%C3%A4rz.pdf"],
            "Rechnung März.pdf",
        ),
        (  # RFC 2231 continuations
            [
                b"Content-Type: application/pdf",
                b"Content-Disposition: attachment; filename*0*=UTF-8''Stromrechnung%20f%C3%BCr; filename*1*=%20M%C3%A4rz.pdf",
            ],
            "Stromrechnung für März.pdf",
        ),
        (  # unquoted encoded word: the standard parser gives up here
            [b"Content-Type: application/pdf", b"Content-Disposition: attachment; filename==?iso-8859-1?Q?Rechnung_M=E4rz.pdf?="],
            "Rechnung März.pdf",
        ),
        (  # raw UTF-8 bytes
            [b"Content-Type: application/pdf", 'Content-Disposition: attachment; filename="Rechnung März.pdf"'.encode()],
            "Rechnung März.pdf",
        ),
        (  # raw Latin-1 bytes from old clients
            [b"Content-Type: application/pdf", 'Content-Disposition: attachment; filename="Rechnung März.pdf"'.encode("latin-1")],
            "Rechnung März.pdf",
        ),
        (  # only the name parameter of Content-Type
            [b'Content-Type: application/pdf; name="Kontoauszug 03.2025"'],
            "Kontoauszug 03.2025.pdf",
        ),
        (  # path sent along by the client
            [b"Content-Type: application/pdf", b'Content-Disposition: attachment; filename="C:\\\\Users\\\\max\\\\scan.pdf"'],
            "scan.pdf",
        ),
    ],
)
def test_attachment_file_names(header_lines, expected, pdf_bytes):
    raw = raw_with_part(header_lines, pdf_bytes())
    documents, _ = extract_documents(raw)
    assert [[name for name, _ in doc] for doc in documents] == [[expected]]


def test_octet_stream_is_recognized_by_content(pdf_bytes):
    msg = base_message()
    msg.add_attachment(pdf_bytes(), maintype="application", subtype="octet-stream", filename="Scan")
    msg.add_attachment(jpeg_bytes(), maintype="application", subtype="octet-stream", filename="IMG_0001.dat")
    msg.add_attachment(b"PK\x03\x04" + os.urandom(30_000), maintype="application", subtype="octet-stream", filename="archiv.zip")
    documents, _ = extract_documents(msg)
    assert [[name for name, _ in doc] for doc in documents] == [["Scan.pdf"], ["IMG_0001.jpg"]]


def test_unsupported_and_animated_attachments_are_skipped():
    msg = base_message()
    gif = b"GIF89a" + os.urandom(40_000)
    msg.add_attachment(gif, maintype="image", subtype="gif", filename="banner.gif")
    msg.add_attachment("a;b\n1;2\n", subtype="csv", filename="daten.csv")
    msg.add_attachment(b"<svg/>" * 5000, maintype="image", subtype="svg+xml", filename="logo.svg")
    documents, _ = extract_documents(msg)
    assert documents == []


def test_single_part_mail_with_pdf_body(pdf_bytes):
    msg = EmailMessage()
    msg["From"] = "scanner@printer.local"
    msg["Subject"] = "Scan"
    msg.set_content(pdf_bytes(), maintype="application", subtype="pdf", filename="scan.pdf")
    documents, context = extract_documents(msg.as_bytes())
    assert [[name for name, _ in doc] for doc in documents] == [["scan.pdf"]]
    assert context["mail_from"] == "scanner@printer.local"
    assert context["mail_date"] is None and context["message_id"] is None


def test_identical_attachments_are_imported_once(pdf_bytes):
    data = pdf_bytes()
    msg = base_message()
    msg.add_attachment(data, maintype="application", subtype="pdf", filename="a.pdf")
    msg.add_attachment(data, maintype="application", subtype="pdf", filename="a-kopie.pdf")
    documents, _ = extract_documents(msg)
    assert len(documents) == 1


def test_missing_file_names_get_german_defaults(pdf_bytes):
    msg = base_message()
    msg.add_attachment(pdf_bytes(), maintype="application", subtype="pdf")
    msg.add_attachment(jpeg_bytes(), maintype="image", subtype="jpeg")
    documents, _ = extract_documents(msg)
    names = [name for doc in documents for name, _ in doc]
    assert names[0].startswith("Anhang-") and names[0].endswith(".pdf")
    assert names[1].startswith("Bild-") and names[1].endswith(".jpg")


# -- logos and embedded images -------------------------------------------------------------------------


def html_mail(images: list[tuple[bytes, str, str]], *, attach_photo: bytes | None = None) -> EmailMessage:
    """HTML mail with images in a multipart/related part referenced by cid."""
    msg = base_message("Ihre Rechnung", "Shop <service@shop.example>")
    refs = "".join(f'<img src="cid:{cid}">' for _, cid, _ in images)
    msg.add_alternative(f"<html><body><p>Danke für Ihren Einkauf</p>{refs}</body></html>", subtype="html")
    html_part = msg.get_payload()[1]
    for data, cid, subtype in images:
        html_part.add_related(
            data, maintype="image", subtype=subtype, cid=f"<{cid}>", filename=f"{cid}.{subtype}", disposition="inline"
        )
    if attach_photo is not None:
        msg.add_attachment(attach_photo, maintype="image", subtype="jpeg", filename="beleg.jpg")
    return msg


def test_images_referenced_by_html_are_skipped(pdf_bytes):
    msg = html_mail([(noise_png((600, 200)), "banner", "png"), (tiny_logo(), "logo", "png")])
    msg.add_attachment(pdf_bytes(), maintype="application", subtype="pdf", filename="rechnung.pdf")
    documents, _ = extract_documents(msg)
    assert [[name for name, _ in doc] for doc in documents] == [["rechnung.pdf"]]


def test_photo_sized_embedded_image_is_kept():
    # Phone mail apps embed attached photos in the HTML body via cid.
    msg = html_mail([(jpeg_bytes(), "photo1", "jpeg"), (noise_png((300, 300)), "signature", "png")])
    documents, _ = extract_documents(msg)
    assert [[name for name, _ in doc] for doc in documents] == [["photo1.jpeg"]]


def test_small_images_are_skipped_even_as_attachments():
    msg = base_message()
    msg.add_attachment(tiny_logo(), maintype="image", subtype="png", filename="logo.png")
    documents, _ = extract_documents(msg)
    assert documents == []
    documents, _ = extract_documents(msg, min_image_bytes=0)
    assert [[name for name, _ in doc] for doc in documents] == [["logo.png"]]


def test_cid_image_marked_as_attachment_is_kept():
    banner = noise_png((600, 200))
    msg = html_mail([(banner, "banner", "png")])
    related = msg.get_payload()[1].get_payload()[1]
    related.replace_header("Content-Disposition", 'attachment; filename="banner.png"')
    documents, _ = extract_documents(msg)
    assert [[name for name, _ in doc] for doc in documents] == [["banner.png"]]


# -- forwarded mails --------------------------------------------------------------------------------------


def test_forwarded_message_attachment_is_searched(pdf_bytes):
    original = base_message("Faktura 2025001", "Alza.cz a.s. <faktury@alza.cz>")
    original.add_attachment(pdf_bytes("Faktura"), maintype="application", subtype="pdf", filename="faktura.pdf")
    outer = base_message("WG: Faktura 2025001", "Max <max@example.org>")
    outer.add_attachment(original)  # message/rfc822

    documents, context = extract_documents(outer)

    assert [[name for name, _ in doc] for doc in documents] == [["faktura.pdf"]]
    assert context["mail_from"] == "max@example.org"
    assert context["forwarded_from"] == "faktury@alza.cz"
    assert context["forwarded_from_name"] == "Alza.cz a.s."
    assert looks_forwarded(parse_message(outer.as_bytes()), context)


def test_base64_encoded_message_attachment(pdf_bytes):
    original = base_message("Rechnung", "Stadtwerke <rechnung@stadtwerke.example>")
    original.add_attachment(pdf_bytes(), maintype="application", subtype="pdf", filename="strom.pdf")
    inner = original.as_bytes()
    raw = (
        b"From: max@example.org\r\nSubject: Fwd: Rechnung\r\nMIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=XX\r\n\r\n"
        b"--XX\r\nContent-Type: text/plain\r\n\r\nsiehe Anhang\r\n"
        b"--XX\r\nContent-Type: message/rfc822\r\nContent-Transfer-Encoding: base64\r\n\r\n"
        + base64.encodebytes(inner)
        + b"\r\n--XX--\r\n"
    )
    documents, context = extract_documents(raw)
    assert [[name for name, _ in doc] for doc in documents] == [["strom.pdf"]]
    assert context["forwarded_from"] == "rechnung@stadtwerke.example"


def test_eml_file_attachment_is_searched(pdf_bytes):
    original = base_message("Police", "Versicherung <post@versicherung.example>")
    original.add_attachment(pdf_bytes(), maintype="application", subtype="pdf", filename="police.pdf")
    outer = base_message("Fwd: Police")
    outer.add_attachment(original.as_bytes(), maintype="application", subtype="octet-stream", filename="Police.eml")
    documents, context = extract_documents(outer)
    assert [[name for name, _ in doc] for doc in documents] == [["police.pdf"]]
    assert context["forwarded_from"] == "post@versicherung.example"


@pytest.mark.parametrize(
    "body,expected_address,expected_name",
    [
        (
            "Bitte ablegen.\n\n---------- Weitergeleitete Nachricht ---------\n"
            "Von: Alza.cz <faktury@alza.cz>\nDate: Mo., 3. März 2025 um 10:15\nSubject: Faktura\n",
            "faktury@alza.cz",
            "Alza.cz",
        ),
        (
            "\n\nAnfang der weitergeleiteten Nachricht:\n\n"
            "Von: Stadtwerke Dresden <rechnung@stadtwerke.example>\nBetreff: Abschlag\n",
            "rechnung@stadtwerke.example",
            "Stadtwerke Dresden",
        ),
        (
            "-----Ursprüngliche Nachricht-----\n"
            "Von: Hausverwaltung Meier [mailto:info@hv-meier.example]\nGesendet: Montag, 3. März 2025 10:15\n",
            "info@hv-meier.example",
            "Hausverwaltung Meier",
        ),
        (
            "---------- Forwarded message ---------\nFrom: billing@provider.example\nDate: Mon, Mar 3, 2025\n",
            "billing@provider.example",
            None,
        ),
    ],
)
def test_inline_forward_sender_is_found(body, expected_address, expected_name, pdf_bytes):
    msg = base_message("WG: Rechnung", "Max <max@example.org>")
    msg.set_content(body)
    msg.add_attachment(pdf_bytes(), maintype="application", subtype="pdf", filename="rechnung.pdf")
    _, context = extract_documents(msg)
    assert context["forwarded_from"] == expected_address
    assert context.get("forwarded_from_name") == expected_name


def test_inline_forward_in_html_only_mail():
    msg = base_message("Fwd: Abrechnung", "max@example.org")
    msg.clear_content()
    msg.set_content(
        "<html><body><p>FYI</p><div>-------- Weitergeleitete Nachricht --------</div>"
        "<div><b>Von:</b> Netz GmbH &lt;abrechnung@netz.example&gt;</div></body></html>",
        subtype="html",
    )
    _, context = extract_documents(msg)
    assert context["forwarded_from"] == "abrechnung@netz.example"


def test_plain_mail_is_not_forwarded():
    msg = base_message("Rechnung", "service@shop.example")
    msg.set_content("Von: uns für Sie – vielen Dank!\n")
    _, context = extract_documents(msg)
    assert "forwarded_from" not in context
    assert not looks_forwarded(msg, context)


# -- helpers ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address,allowed,expected",
    [
        ("Max <Max@Example.org>", ["max@example.org"], True),
        ("max@example.org", ["@example.org"], True),
        ("max@mail.example.org", ["@example.org"], False),
        ("max@example.org", [], False),
        ("max@example.org", ["*"], True),
        (None, ["*"], True),
        (None, ["@example.org"], False),
        ("eve@evil.example", ["max@example.org", "@example.org"], False),
        ("MAX@EXAMPLE.ORG", [" <Max@example.org> "], True),
    ],
)
def test_sender_allowed(address, allowed, expected):
    assert sender_allowed(address, allowed) is expected


def test_normalize_address():
    assert normalize_address("Max Muster <Max@Example.org>") == "max@example.org"
    assert normalize_address("") is None


def test_mail_key_uses_message_id_and_date():
    first = base_message()
    same = base_message("anderer Betreff")
    assert mail_key(first) == mail_key(same)
    later = base_message()
    del later["Date"]
    later["Date"] = "Tue, 04 Mar 2025 09:15:00 +0000"
    assert mail_key(later) != mail_key(first)

    without_id = base_message()
    del without_id["Message-ID"]
    raw = without_id.as_bytes()
    assert mail_key(parse_message(raw), raw) == mail_key(parse_message(raw), raw)
    other = base_message("x")
    del other["Message-ID"]
    assert mail_key(other) != mail_key(parse_message(raw), raw)


def test_classify_prefers_content_over_declaration():
    assert classify(b"%PDF-1.7 ...", "image/jpeg", "foto.jpg") == ("pdf", ".pdf")
    assert classify(b"\xff\xd8\xff\xe0rest", "application/pdf", "x.pdf") == ("image", ".jpg")
    assert classify(b"garbage", "application/pdf", None) == ("pdf", ".pdf")
    assert classify(b"garbage", "application/octet-stream", "notes.txt") is None
    assert classify(b"II*\x00rest", None, None) == ("image", ".tif")


def test_broken_parts_do_not_break_extraction(pdf_bytes):
    raw = (
        RAW_HEADER
        + b"--XX\r\nContent-Type: application/pdf; name=a.pdf\r\nContent-Transfer-Encoding: base64\r\n\r\n"
        + b"!!!not base64!!!\r\n"
        + b"--XX\r\nContent-Type: application/pdf; name=b.pdf\r\nContent-Transfer-Encoding: base64\r\n\r\n"
        + base64.encodebytes(pdf_bytes())
        + b"\r\n--XX--\r\n"
    )
    documents, _ = extract_documents(raw)
    names = [name for doc in documents for name, _ in doc]
    assert "b.pdf" in names


def test_message_without_any_headers():
    documents, context = extract_documents(b"\r\n")
    assert documents == []
    assert context["mail_from"] is None and context["mail_subject"] is None
