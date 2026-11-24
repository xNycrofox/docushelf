"""Document metadata in XMP.

Metadata lives inside the PDF so the archive needs no database:
``dc:title``, ``dc:subject`` (tags), ``pdf:Keywords`` and a small private
namespace for the document id, origin hash, document date and sender.

PDF/A files stay valid: custom properties are declared in a PDF/A extension
schema, and the document info dictionary is kept in sync with ``dc:title`` and
``pdf:Keywords``.

Files whose bytes must stay exactly as they are (digitally signed or
encrypted PDFs) are never rewritten: :func:`write_meta` refuses them with
``UnsupportedFile`` and :func:`pdf_protection` tells callers beforehand.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

import pikepdf
from lxml import etree

from ..errors import UnsupportedFile
from ..models import DocMeta
from ..util.timeutil import now_iso, parse_date

log = logging.getLogger(__name__)

NS_DS = "http://ns.docushelf.org/xmp/1.0/"
NS_X = "adobe:ns:meta/"
NS_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
NS_DC = "http://purl.org/dc/elements/1.1/"
NS_PDF = "http://ns.adobe.com/pdf/1.3/"
NS_XMP = "http://ns.adobe.com/xap/1.0/"
NS_PDFAID = "http://www.aiim.org/pdfa/ns/id/"
NS_PDFAEXT = "http://www.aiim.org/pdfa/ns/extension/"
NS_PDFASCHEMA = "http://www.aiim.org/pdfa/ns/schema#"
NS_PDFAPROP = "http://www.aiim.org/pdfa/ns/property#"
NS_XML = "http://www.w3.org/XML/1998/namespace"

# DocMeta attribute -> property name in the private namespace.
FIELD_PROPERTIES = {
    "doc_id": "DocumentId",
    "origin_hash": "OriginHash",
    "doc_date": "DocumentDate",
    "sender": "Sender",
    "source": "Source",
    "received_at": "ReceivedAt",
    "filed_at": "FiledAt",
}
# Further simple text properties that may be stored via DocMeta.extra.
EXTRA_PROPERTIES = (
    "ArchiveSerialNumber",
    "PaperlessId",
    "MailSubject",
    "MailFrom",
    "PaperlessNotes",
    "PaperlessFields",
)

_PROPERTY_DESCRIPTIONS = {
    "DocumentId": "Stable identifier of the document",
    "OriginHash": "SHA-256 of the file as originally received",
    "DocumentDate": "Date of the document (YYYY-MM-DD)",
    "Sender": "Sender or issuer of the document",
    "Source": "Channel the document arrived through",
    "ReceivedAt": "Time the document was received",
    "FiledAt": "Time the document was filed",
    "ArchiveSerialNumber": "Number of the paper original in a physical archive",
    "PaperlessId": "Document id in a previous Paperless-ngx installation",
    "MailSubject": "Subject of the e-mail the document came with",
    "MailFrom": "Sender address of the e-mail the document came with",
    "PaperlessNotes": "Notes on the document from a previous Paperless-ngx installation",
    "PaperlessFields": "Custom field values from a previous Paperless-ngx installation",
}

SIGNED_MESSAGE = "Digital signierte PDFs werden nicht verändert, damit die Signatur gültig bleibt."
ENCRYPTED_MESSAGE = "Verschlüsselte PDFs können nicht mit Metadaten versehen werden."

# Characters XML 1.0 cannot carry. Control characters become spaces (tab and
# line feed are allowed); lone surrogates and the noncharacters U+FFFE/U+FFFF
# are dropped. lxml refuses text with any of them.
_XML_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_XML_NONCHAR_RE = re.compile(r"[\ud800-\udfff\ufffe\uffff]")

# Upper bound for walking the form field tree of a PDF.
_MAX_FORM_FIELDS = 10_000

_PACKET_BEGIN = b'<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
_PACKET_END = b'<?xpacket end="w"?>'
_PADDING = (b" " * 99 + b"\n") * 20

_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, remove_blank_text=False)


def _q(namespace: str, local: str) -> str:
    return f"{{{namespace}}}{local}"


RDF_DESCRIPTION = _q(NS_RDF, "Description")
RDF_LI = _q(NS_RDF, "li")
RDF_ABOUT = _q(NS_RDF, "about")


def xml_safe(text: str) -> str:
    """``text`` without the characters XML 1.0 cannot carry.

    Tabs and line breaks stay (a carriage return becomes a line feed), other
    control characters become spaces, lone surrogates and U+FFFE/U+FFFF are
    dropped. Everything else is left as it is.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _XML_CONTROL_RE.sub(" ", text)
    return _XML_NONCHAR_RE.sub("", text)


# ---------------------------------------------------------------------------
# Files that must not be rewritten


def is_signed(pdf: pikepdf.Pdf) -> bool:
    """True if the PDF carries a digital signature that rewriting the file would break.

    Signs of a signature: the ``SignaturesExist`` bit of the form's
    ``/SigFlags``, a signature field (``/FT /Sig``, also inherited) with a
    value, or a ``/Perms`` dictionary (certification or usage rights).
    """
    try:
        root = pdf.Root
        if "/Perms" in root:
            return True
        acroform = root.get("/AcroForm")
        if not isinstance(acroform, pikepdf.Dictionary):
            return False
        try:
            if int(acroform.get("/SigFlags", 0)) & 1:
                return True
        except (TypeError, ValueError):
            pass
        fields = acroform.get("/Fields")
        return isinstance(fields, pikepdf.Array) and _has_signed_field(fields)
    except Exception:  # damaged form structure: no signature that we could tell
        log.debug("cannot inspect the form fields of a PDF", exc_info=True)
        return False


def _has_signed_field(fields: pikepdf.Array) -> bool:
    """Walk the field tree (type and value are inheritable) looking for a filled signature field."""
    stack: list[tuple[object, object, object]] = [(field, None, None) for field in fields]
    seen: set[tuple[int, int]] = set()
    visited = 0
    while stack and visited < _MAX_FORM_FIELDS:
        field, parent_kind, parent_value = stack.pop()
        if not isinstance(field, pikepdf.Dictionary):
            continue
        if field.is_indirect:
            if field.objgen in seen:
                continue
            seen.add(field.objgen)
        visited += 1
        kind = field.get("/FT", parent_kind)
        value = field.get("/V", parent_value)
        if kind == pikepdf.Name.Sig and value is not None:
            return True
        kids = field.get("/Kids")
        if isinstance(kids, pikepdf.Array):
            stack.extend((kid, kind, value) for kid in kids)
    return False


def _protection(pdf: pikepdf.Pdf) -> str | None:
    if is_signed(pdf):
        return "signed"
    if pdf.is_encrypted:
        return "encrypted"
    return None


def pdf_protection(path: Path) -> str | None:
    """Why ``path`` must keep its bytes: ``"signed"``, ``"encrypted"`` or None.

    ``"encrypted"`` covers files that need a password to open as well as
    files encrypted with an owner password only; :func:`write_meta` refuses
    both. Files that cannot be opened for other reasons (damaged, missing,
    not a PDF) give None.
    """
    try:
        with pikepdf.open(path) as pdf:
            return _protection(pdf)
    except pikepdf.PasswordError:
        return "encrypted"
    except Exception:
        log.debug("cannot open %s to check for signatures", path, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Parsing


def _parse_packet(data: bytes) -> etree._Element | None:
    data = data.strip(b"\x00 \t\r\n")
    if not data:
        return None
    try:
        return etree.fromstring(data, parser=_PARSER)
    except etree.XMLSyntaxError:
        # Some producers add garbage around the packet; try the xmpmeta element alone.
        start = data.find(b"<x:xmpmeta")
        end = data.rfind(b"</x:xmpmeta>")
        if start >= 0 and end > start:
            try:
                return etree.fromstring(data[start : end + len(b"</x:xmpmeta>")], parser=_PARSER)
            except etree.XMLSyntaxError:
                return None
        return None


def _find_rdf(root: etree._Element | None) -> etree._Element | None:
    if root is None:
        return None
    if root.tag == _q(NS_RDF, "RDF"):
        return root
    return next(root.iter(_q(NS_RDF, "RDF")), None)


def _descriptions(rdf: etree._Element) -> list[etree._Element]:
    return list(rdf.findall(RDF_DESCRIPTION))


def _element_value(element: etree._Element) -> str | None:
    alt = element.find(_q(NS_RDF, "Alt"))
    if alt is not None:
        items = alt.findall(RDF_LI)
        for item in items:
            if item.get(_q(NS_XML, "lang")) == "x-default":
                return (item.text or "").strip()
        return (items[0].text or "").strip() if items else None
    for container in ("Seq", "Bag"):
        node = element.find(_q(NS_RDF, container))
        if node is not None:
            first = node.find(RDF_LI)
            return (first.text or "").strip() if first is not None else None
    return (element.text or "").strip()


def _get_value(rdf: etree._Element, namespace: str, local: str) -> str | None:
    key = _q(namespace, local)
    for desc in _descriptions(rdf):
        if key in desc.attrib:
            return desc.attrib[key].strip()
        element = desc.find(key)
        if element is not None:
            value = _element_value(element)
            if value is not None:
                return value
    return None


def _get_list(rdf: etree._Element, namespace: str, local: str) -> list[str]:
    key = _q(namespace, local)
    for desc in _descriptions(rdf):
        element = desc.find(key)
        if element is None:
            if key in desc.attrib:
                return [part.strip() for part in desc.attrib[key].split(",") if part.strip()]
            continue
        values: list[str] = []
        for container in ("Bag", "Seq", "Alt"):
            node = element.find(_q(NS_RDF, container))
            if node is not None:
                values.extend((li.text or "").strip() for li in node.findall(RDF_LI))
        if not values and element.text and element.text.strip():
            values = [part.strip() for part in element.text.split(",")]
        return [v for v in values if v]
    return []


def _load_root(pdf: pikepdf.Pdf) -> etree._Element | None:
    stream = pdf.Root.get("/Metadata")
    if stream is None:
        return None
    try:
        data = stream.read_bytes()
    except Exception:  # damaged or unsupported filter
        log.debug("cannot read XMP stream", exc_info=True)
        return None
    return _parse_packet(data)


def _meta_from_rdf(rdf: etree._Element | None) -> DocMeta:
    meta = DocMeta()
    if rdf is None:
        return meta
    meta.title = _get_value(rdf, NS_DC, "title") or None
    meta.tags = _get_list(rdf, NS_DC, "subject")
    if not meta.tags:
        keywords = _get_value(rdf, NS_PDF, "Keywords")
        if keywords:
            meta.tags = [k.strip() for k in keywords.replace(";", ",").split(",") if k.strip()]
    for attr, prop in FIELD_PROPERTIES.items():
        value = _get_value(rdf, NS_DS, prop)
        if not value:
            continue
        if attr == "doc_date":
            meta.doc_date = parse_date(value)
        else:
            setattr(meta, attr, value)
    for prop in EXTRA_PROPERTIES:
        value = _get_value(rdf, NS_DS, prop)
        if value:
            meta.extra[prop] = value
    meta.tags = _dedupe(meta.tags)
    return meta


def read_meta(path: Path) -> DocMeta:
    """Read metadata from a PDF. Values the file does not carry stay None/empty."""
    with pikepdf.open(path) as pdf:
        root = _load_root(pdf)
    return _meta_from_rdf(_find_rdf(root))


def read_meta_from_pdf(pdf: pikepdf.Pdf) -> DocMeta:
    return _meta_from_rdf(_find_rdf(_load_root(pdf)))


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for value in values:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


# ---------------------------------------------------------------------------
# Writing


def _new_skeleton() -> tuple[etree._Element, etree._Element]:
    xmpmeta = etree.Element(_q(NS_X, "xmpmeta"), nsmap={"x": NS_X})
    rdf = etree.SubElement(xmpmeta, _q(NS_RDF, "RDF"), nsmap={"rdf": NS_RDF})
    return xmpmeta, rdf


def _managed_keys() -> set[str]:
    keys = {
        _q(NS_DC, "title"),
        _q(NS_DC, "subject"),
        _q(NS_PDF, "Keywords"),
        _q(NS_XMP, "MetadataDate"),
    }
    return keys


def _strip_managed(rdf: etree._Element) -> None:
    managed = _managed_keys()
    for desc in _descriptions(rdf):
        for key in list(desc.attrib):
            if key in managed or key.startswith("{" + NS_DS + "}"):
                del desc.attrib[key]
        for child in list(desc):
            tag = child.tag if isinstance(child.tag, str) else ""
            if tag in managed or tag.startswith("{" + NS_DS + "}"):
                desc.remove(child)
        if len(desc) == 0 and set(desc.attrib) <= {RDF_ABOUT}:
            rdf.remove(desc)


def _is_pdfa(rdf: etree._Element) -> bool:
    return _get_value(rdf, NS_PDFAID, "part") is not None


def _schema_item(element: etree._Element) -> etree._Element | None:
    """The ``rdf:li`` of the extension schema list that contains ``element``."""
    node = element.getparent()
    while node is not None and node.tag != RDF_LI:
        node = node.getparent()
    return node


def _ensure_extension_schema(rdf: etree._Element) -> None:
    """Declare our namespace with all its properties in the PDF/A extension schema.

    A declaration written by an older version may lack properties added
    since; it is replaced, so every property in the packet stays declared.
    """
    ours: list[etree._Element] = []
    for element in list(rdf.iter(_q(NS_PDFASCHEMA, "namespaceURI"))):
        if (element.text or "").strip() == NS_DS:
            item = _schema_item(element)
            if item is not None and not any(item is known for known in ours):
                ours.append(item)
    if len(ours) == 1:
        declared = {(name.text or "").strip() for name in ours[0].iter(_q(NS_PDFAPROP, "name"))}
        if set(_PROPERTY_DESCRIPTIONS) <= declared:
            return
    bag = None
    for item in ours:
        parent = item.getparent()
        if parent is None:
            continue
        if bag is None and parent.tag == _q(NS_RDF, "Bag"):
            bag = parent
        parent.remove(item)
    if bag is None:
        for schemas in rdf.iter(_q(NS_PDFAEXT, "schemas")):
            bag = schemas.find(_q(NS_RDF, "Bag"))
            if bag is None:
                bag = etree.SubElement(schemas, _q(NS_RDF, "Bag"))
            break
    nsmap = {"pdfaExtension": NS_PDFAEXT, "pdfaSchema": NS_PDFASCHEMA, "pdfaProperty": NS_PDFAPROP}
    if bag is None:
        desc = etree.SubElement(rdf, RDF_DESCRIPTION, nsmap=nsmap)
        desc.set(RDF_ABOUT, "")
        schemas = etree.SubElement(desc, _q(NS_PDFAEXT, "schemas"))
        bag = etree.SubElement(schemas, _q(NS_RDF, "Bag"))
    item = etree.SubElement(bag, RDF_LI, nsmap=nsmap)
    item.set(_q(NS_RDF, "parseType"), "Resource")
    etree.SubElement(item, _q(NS_PDFASCHEMA, "schema")).text = "DocuShelf document metadata"
    etree.SubElement(item, _q(NS_PDFASCHEMA, "namespaceURI")).text = NS_DS
    etree.SubElement(item, _q(NS_PDFASCHEMA, "prefix")).text = "docushelf"
    prop = etree.SubElement(item, _q(NS_PDFASCHEMA, "property"))
    seq = etree.SubElement(prop, _q(NS_RDF, "Seq"))
    for name, description in _PROPERTY_DESCRIPTIONS.items():
        entry = etree.SubElement(seq, RDF_LI)
        entry.set(_q(NS_RDF, "parseType"), "Resource")
        etree.SubElement(entry, _q(NS_PDFAPROP, "name")).text = name
        etree.SubElement(entry, _q(NS_PDFAPROP, "valueType")).text = "Text"
        etree.SubElement(entry, _q(NS_PDFAPROP, "category")).text = "external"
        etree.SubElement(entry, _q(NS_PDFAPROP, "description")).text = description


def _property_values(meta: DocMeta) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []

    def add(prop: str, value: object) -> None:
        if value in (None, ""):
            return
        if isinstance(value, date):
            value = value.isoformat()
        text = xml_safe(str(value))
        if text.strip():
            values.append((prop, text))

    for attr, prop in FIELD_PROPERTIES.items():
        add(prop, getattr(meta, attr))
    for prop in EXTRA_PROPERTIES:
        add(prop, meta.extra.get(prop))
    unknown = set(meta.extra) - set(EXTRA_PROPERTIES)
    if unknown:
        log.warning("ignoring unknown XMP properties: %s", ", ".join(sorted(unknown)))
    return values


def _clean_title(title: object) -> str | None:
    if not title:
        return None
    return xml_safe(str(title)).strip() or None


def _clean_tags(tags: list[str] | None) -> list[str]:
    return _dedupe([tag for tag in (xml_safe(str(t)).strip() for t in tags or [] if t) if tag])


def build_packet(existing: bytes | None, meta: DocMeta, *, metadata_date: str | None = None) -> bytes:
    """Return a complete XMP packet: ``existing`` with our properties replaced by ``meta``.

    Characters XML cannot carry are removed from every value (see :func:`xml_safe`).
    """
    root = _parse_packet(existing) if existing else None
    rdf = _find_rdf(root)
    if rdf is None:
        xmpmeta, rdf = _new_skeleton()
    elif root is not None and root.tag == _q(NS_X, "xmpmeta"):
        xmpmeta = root
    else:
        xmpmeta = etree.Element(_q(NS_X, "xmpmeta"), nsmap={"x": NS_X})
        parent = rdf.getparent()
        if parent is not None:
            parent.remove(rdf)
        xmpmeta.append(rdf)

    _strip_managed(rdf)
    desc = etree.SubElement(
        rdf,
        RDF_DESCRIPTION,
        nsmap={"dc": NS_DC, "pdf": NS_PDF, "xmp": NS_XMP, "docushelf": NS_DS},
    )
    desc.set(RDF_ABOUT, "")
    tags = _clean_tags(meta.tags)
    title_text = _clean_title(meta.title)
    if title_text:
        title = etree.SubElement(desc, _q(NS_DC, "title"))
        alt = etree.SubElement(title, _q(NS_RDF, "Alt"))
        item = etree.SubElement(alt, RDF_LI)
        item.set(_q(NS_XML, "lang"), "x-default")
        item.text = title_text
    if tags:
        subject = etree.SubElement(desc, _q(NS_DC, "subject"))
        bag = etree.SubElement(subject, _q(NS_RDF, "Bag"))
        for tag in tags:
            etree.SubElement(bag, RDF_LI).text = tag
        etree.SubElement(desc, _q(NS_PDF, "Keywords")).text = ", ".join(tags)
    etree.SubElement(desc, _q(NS_XMP, "MetadataDate")).text = xml_safe(metadata_date or now_iso())
    for prop, value in _property_values(meta):
        etree.SubElement(desc, _q(NS_DS, prop)).text = value
    if _is_pdfa(rdf):
        _ensure_extension_schema(rdf)

    body = etree.tostring(xmpmeta, encoding="utf-8")
    return _PACKET_BEGIN + body + b"\n" + _PADDING + _PACKET_END


def apply_meta(pdf: pikepdf.Pdf, meta: DocMeta) -> None:
    """Replace our metadata in an open PDF (XMP and the matching info dictionary entries)."""
    existing_stream = pdf.Root.get("/Metadata")
    existing: bytes | None = None
    if existing_stream is not None:
        try:
            existing = existing_stream.read_bytes()
        except Exception:
            existing = None
    packet = build_packet(existing, meta)
    stream = pikepdf.Stream(pdf, packet)
    stream["/Type"] = pikepdf.Name("/Metadata")
    stream["/Subtype"] = pikepdf.Name("/XML")
    pdf.Root["/Metadata"] = pdf.make_indirect(stream)

    tags = _clean_tags(meta.tags)
    title = _clean_title(meta.title)
    info = pdf.docinfo
    if title:
        info["/Title"] = pikepdf.String(title)
    elif "/Title" in info:
        del info["/Title"]
    if tags:
        info["/Keywords"] = pikepdf.String(", ".join(tags))
    elif "/Keywords" in info:
        del info["/Keywords"]


def write_meta(src: Path, dst: Path, meta: DocMeta) -> None:
    """Write a copy of ``src`` with ``meta`` to ``dst`` (which must not be ``src``).

    Raises UnsupportedFile for files that have to keep their bytes: digitally
    signed PDFs (a rewrite breaks the signature) and encrypted PDFs.
    """
    if Path(src).resolve() == Path(dst).resolve():
        raise ValueError("write_meta needs a separate destination file")
    try:
        pdf = pikepdf.open(src)
    except pikepdf.PasswordError:
        raise UnsupportedFile(ENCRYPTED_MESSAGE) from None
    with pdf:
        protection = _protection(pdf)
        if protection == "signed":
            raise UnsupportedFile(SIGNED_MESSAGE)
        if protection is not None:
            raise UnsupportedFile(ENCRYPTED_MESSAGE)
        apply_meta(pdf, meta)
        pdf.save(dst, fix_metadata_version=False)


def merge_meta(current: DocMeta, **changes: object) -> DocMeta:
    """Return a copy of ``current`` with selected fields replaced."""
    data = {
        "doc_id": current.doc_id,
        "title": current.title,
        "doc_date": current.doc_date,
        "tags": list(current.tags),
        "sender": current.sender,
        "origin_hash": current.origin_hash,
        "source": current.source,
        "received_at": current.received_at,
        "filed_at": current.filed_at,
        "extra": dict(current.extra),
    }
    data.update(changes)
    return DocMeta(**data)  # type: ignore[arg-type]
