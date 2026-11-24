"""Turn e-mail messages into documents for the inbox.

Shared by the IMAP fetcher and the SMTP receiver. :func:`extract_documents`
returns documents in the shape :func:`docushelf.channels.sendung.write_sendung`
expects (each document a list of ``(filename, bytes)``) plus the mail context
the suggestions work with:

* every PDF becomes a document of its own,
* all images of one message are combined into one document (in order),
* attached (forwarded) messages are searched as well,
* small images and images that belong to the HTML body (logos, signatures,
  tracking pixels) are skipped,
* ``application/octet-stream`` attachments are recognized by their content.

Parsing is deliberately forgiving: real mail from scanners, phones and old
mail clients violates the standards in many small ways.
"""

from __future__ import annotations

import base64
import binascii
import email
import hashlib
import html
import io
import logging
import os
import quopri
import re
import urllib.parse
from datetime import datetime, timezone
from email import policy
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Any, Iterable, Iterator

from ..util import timeutil

log = logging.getLogger(__name__)

Document = list[tuple[str, bytes]]

PDF_TYPES = frozenset(
    {
        "application/pdf",
        "application/x-pdf",
        "application/acrobat",
        "application/vnd.pdf",
        "applications/vnd.pdf",
        "text/pdf",
        "text/x-pdf",
    }
)
# Image formats the pipeline can turn into PDF pages (by file extension).
IMAGE_EXTENSIONS = {
    ".jpg": ".jpg",
    ".jpeg": ".jpg",
    ".jpe": ".jpg",
    ".png": ".png",
    ".tif": ".tif",
    ".tiff": ".tif",
    ".heic": ".heic",
    ".heif": ".heic",
    ".webp": ".webp",
    ".bmp": ".bmp",
}
IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/pjpeg": ".jpg",
    "image/png": ".png",
    "image/x-png": ".png",
    "image/tiff": ".tif",
    "image/tif": ".tif",
    "image/x-tiff": ".tif",
    "image/heic": ".heic",
    "image/heif": ".heic",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/x-bmp": ".bmp",
    "image/x-ms-bmp": ".bmp",
}
# Never documents: animations, vector graphics and icons.
IGNORED_IMAGE_TYPES = frozenset({"image/gif", "image/svg+xml", "image/x-icon", "image/vnd.microsoft.icon"})

_BODY_TYPES = frozenset({"text/plain", "text/html", "text/enriched", "text/richtext", "text/calendar"})
# Extensions that say nothing about the content and may be replaced.
_MEANINGLESS_EXTENSIONS = frozenset({".dat", ".bin", ".tmp", ".att", ".file", ".octet-stream"})

# Images embedded in an HTML body are treated as decoration unless they are
# at least this large on their shorter side (a photo or a scanned page).
PHOTO_MIN_SIDE = 1000
MAX_NESTING = 5

_CID_RE = re.compile(r"""cid:([^"'\s>)]+)""", re.IGNORECASE)
_PARAM_RE_TEMPLATE = r"""(?:^|;)\s*{name}\s*=\s*("(?:[^"\\]|\\.)*"|[^;]*)"""
_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]*>")
_BLOCK_TAG_RE = re.compile(r"<\s*(?:br|/p|/div|/tr|/li|/h\d|p|div|tr|li|blockquote|/blockquote)\b[^>]*>", re.IGNORECASE)
_DROP_BLOCK_RE = re.compile(r"<(style|script|head)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)

# Lines that introduce a forwarded message inside the body.
_FORWARD_MARKER_RE = re.compile(
    r"(weitergeleitete\s+nachricht|urspr(?:ü|ue)ngliche\s+nachricht|anfang\s+der\s+weitergeleiteten|"
    r"weitergeleitete\s+e-?mail|forwarded\s+message|original\s+message|begin\s+forwarded\s+message|"
    r"message\s+transf[ée]r[ée])",
    re.IGNORECASE,
)
_FORWARD_FROM_RE = re.compile(r"^[\s>*]*(?:von|from|de)\s*:\s*\**\s*(?P<value>.+?)\s*$", re.IGNORECASE)
_FORWARD_SUBJECT_RE = re.compile(r"^\s*(?:(?:wg|fw|fwd|weitergeleitet|tr)\s*:)", re.IGNORECASE)
_MAILTO_RE = re.compile(r"\[\s*mailto:\s*([^\]\s]+)\s*\]", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Public helpers


def parse_message(raw: bytes | str | Message) -> Message:
    """Parse raw mail data with the modern e-mail policy (no-op for parsed messages)."""
    if isinstance(raw, Message):
        return raw
    if isinstance(raw, str):
        return email.message_from_string(raw, policy=policy.default)
    return email.message_from_bytes(bytes(raw), policy=policy.default)


def sender_allowed(address: str | None, allowed: Iterable[str]) -> bool:
    """Check a sender address against an allowlist.

    Entries are full addresses (``rechnung@example.com``) or domains
    (``@example.com``); ``*`` allows everybody. An empty allowlist allows nobody.
    Comparison ignores case.
    """
    entries = [str(entry).strip().strip("<>").strip().lower() for entry in allowed or () if str(entry).strip()]
    if not entries:
        return False
    if "*" in entries:
        return True
    normalized = normalize_address(address)
    if not normalized or "@" not in normalized:
        return False
    domain = normalized.rsplit("@", 1)[1]
    for entry in entries:
        if entry.startswith("@"):
            if domain == entry[1:]:
                return True
        elif normalized == entry:
            return True
    return False


def normalize_address(address: str | None) -> str | None:
    """``"Name <A@B.de>"`` → ``"a@b.de"``; None for anything without an address."""
    if not address:
        return None
    _, addr = parseaddr(str(address))
    addr = (addr or str(address)).strip().strip("<>").strip().lower()
    return addr or None


def sender_of(message: Message) -> tuple[str | None, str | None]:
    """``(display name, lower-case address)`` of the From header."""
    return _first_address(message, "From")


def mail_key(message: Message, raw: bytes | None = None) -> str:
    """Stable key used to recognize a mail that was already imported.

    Message-ID together with the Date header identifies a message (some
    devices reuse their Message-IDs, the date tells their mails apart).
    Without both the raw message is hashed.
    """
    message_id = _header_text(message, "Message-ID")
    sent = _header_text(message, "Date")
    if message_id and sent:
        basis = f"message-id:{message_id.strip()}\ndate:{sent.strip()}".encode("utf-8", "surrogateescape")
        return hashlib.sha256(basis).hexdigest()
    if raw is None:
        try:
            raw = message.as_bytes()
        except Exception:  # malformed headers can break serialization
            raw = str(message).encode("utf-8", "surrogateescape")
    return hashlib.sha256(raw).hexdigest()


def looks_forwarded(message: Message, context: dict[str, Any] | None = None) -> bool:
    """True if the message is a forward (subject prefix, attached or quoted original)."""
    if context and context.get("forwarded_from"):
        return True
    subject = _header_text(message, "Subject") or ""
    return bool(_FORWARD_SUBJECT_RE.match(subject))


def extract_documents(
    message: Message | bytes | str, *, min_image_bytes: int = 20_000
) -> tuple[list[Document], dict[str, Any]]:
    """Return ``(documents, context)`` for a mail.

    ``documents`` is a list of documents, each a list of ``(filename, content)``.
    ``context`` holds ``mail_from``, ``mail_from_name``, ``mail_subject``,
    ``mail_date`` (ISO 8601), ``message_id`` and, for forwarded mails,
    ``forwarded_from`` (plus ``forwarded_from_name`` when known).
    """
    msg = parse_message(message)
    collector = _Collector(min_image_bytes=min_image_bytes)
    collector.collect(msg, depth=0)

    name, address = sender_of(msg)
    context: dict[str, Any] = {
        "mail_from": address,
        "mail_from_name": name,
        "mail_subject": _clean_header_value(_header_text(msg, "Subject")),
        "mail_date": _mail_date(msg),
        "message_id": _clean_header_value(_header_text(msg, "Message-ID")),
    }
    forwarded = collector.forwarded_from or _inline_forward_sender(msg)
    if forwarded:
        fwd_name, fwd_address = forwarded
        if fwd_address and fwd_address != address:
            context["forwarded_from"] = fwd_address
            if fwd_name:
                context["forwarded_from_name"] = fwd_name
    documents = [doc for doc in collector.documents if doc]
    return documents, context


# ---------------------------------------------------------------------------
# Walking the MIME tree


class _Collector:
    def __init__(self, *, min_image_bytes: int):
        self.min_image_bytes = max(0, int(min_image_bytes))
        self.documents: list[Document] = []
        self.forwarded_from: tuple[str | None, str | None] | None = None
        self._seen: set[str] = set()
        self._counter = 0

    def collect(self, msg: Message, *, depth: int) -> None:
        """Gather the documents of ``msg``; attached messages are handled recursively."""
        parts = list(_own_parts(msg))
        html_refs = _cid_references(part for kind, part in parts if kind == "leaf" and _is_html_body(part))
        image_group: Document | None = None
        for kind, part in parts:
            if kind == "message":
                self._collect_attached(part, depth=depth)
                continue
            if _is_body_text(part):
                continue
            try:
                data = part.get_payload(decode=True)
            except Exception:
                log.warning("cannot decode mail part %s", part.get_content_type(), exc_info=True)
                continue
            if not isinstance(data, (bytes, bytearray)) or not data:
                continue
            data = bytes(data)
            filename = part_filename(part)
            if _is_attached_message(part, filename, data):
                inner = _parse_attached_bytes(data)
                if inner is not None:
                    self._collect_attached_message(inner, depth=depth)
                continue
            classified = classify(data, part.get_content_type(), filename)
            if classified is None:
                log.debug("skipping attachment %r (%s)", filename, part.get_content_type())
                continue
            kind_of_file, ext = classified
            if kind_of_file == "image" and self._is_decoration(part, data, html_refs):
                continue
            digest = hashlib.sha256(data).hexdigest()
            if digest in self._seen:
                continue
            self._seen.add(digest)
            name = self._file_name(filename, ext, kind_of_file)
            if kind_of_file == "pdf":
                self.documents.append([(name, data)])
            else:
                if image_group is None:
                    image_group = []
                    self.documents.append(image_group)
                image_group.append((name, data))

    def _collect_attached(self, part: Message, *, depth: int) -> None:
        inner = _attached_message(part)
        if inner is not None:
            self._collect_attached_message(inner, depth=depth)

    def _collect_attached_message(self, inner: Message, *, depth: int) -> None:
        if depth + 1 > MAX_NESTING:
            log.warning("attached messages nested too deeply, ignoring the rest")
            return
        if self.forwarded_from is None:
            name, address = sender_of(inner)
            if address:
                self.forwarded_from = (name, address)
        self.collect(inner, depth=depth + 1)

    def _is_decoration(self, part: Message, data: bytes, html_refs: set[str]) -> bool:
        """Logos, icons and images that are part of an HTML body are not documents."""
        if len(data) < self.min_image_bytes:
            return True
        content_id = _content_id(part)
        if not content_id or content_id not in html_refs:
            return False
        if (part.get_content_disposition() or "").lower() == "attachment":
            return False
        # Embedded in the HTML body: keep it only if it is photo or page sized
        # (mail apps on phones embed attached photos this way).
        size = image_size(data)
        return size is None or min(size) < PHOTO_MIN_SIDE

    def _file_name(self, filename: str | None, ext: str, kind: str) -> str:
        """A file name whose extension matches the detected content."""
        self._counter += 1
        if not filename:
            base = "Anhang" if kind == "pdf" else "Bild"
            return f"{base}-{self._counter}{ext}"
        stem, current = os.path.splitext(filename)
        current = current.lower()
        if kind == "pdf":
            matches = current == ".pdf"
        else:
            matches = IMAGE_EXTENSIONS.get(current) == ext
        if matches:
            return filename
        if stem and (current in IMAGE_EXTENSIONS or current in _MEANINGLESS_EXTENSIONS or current == ".pdf"):
            return stem + ext
        return filename + ext


def _own_parts(msg: Message) -> Iterator[tuple[str, Message]]:
    """Leaf parts of ``msg`` in order; attached messages are yielded as ("message", part)."""

    def walk(part: Message, is_root: bool) -> Iterator[tuple[str, Message]]:
        ctype = part.get_content_type()
        if not is_root and ctype in ("message/rfc822", "message/global"):
            yield ("message", part)
            return
        if part.is_multipart():
            payload = part.get_payload()
            if isinstance(payload, list):
                for sub in payload:
                    if isinstance(sub, Message):
                        yield from walk(sub, False)
            return
        yield ("leaf", part)

    yield from walk(msg, True)


def _attached_message(part: Message) -> Message | None:
    """The message inside a ``message/rfc822`` part (also when it was wrongly base64 encoded)."""
    cte = (part.get("Content-Transfer-Encoding") or "").strip().lower()
    payload = part.get_payload()
    if cte in ("base64", "quoted-printable"):
        # Not allowed by RFC 2046, but sent by some clients: the parser then
        # treats the encoded text as a message without headers.
        text = None
        if isinstance(payload, list) and payload and isinstance(payload[0], Message):
            body = payload[0].get_payload()
            text = body if isinstance(body, str) else None
        elif isinstance(payload, str):
            text = payload
        if text:
            try:
                if cte == "base64":
                    data = base64.b64decode(text.encode("ascii", "ignore"), validate=False)
                else:
                    data = quopri.decodestring(text.encode("utf-8", "surrogateescape"))
            except (binascii.Error, ValueError):
                data = None
            if data:
                return _parse_attached_bytes(data)
    if isinstance(payload, list) and payload and isinstance(payload[0], Message):
        return payload[0]
    if isinstance(payload, (bytes, str)) and payload:
        return _parse_attached_bytes(payload.encode("utf-8", "surrogateescape") if isinstance(payload, str) else payload)
    return None


def _is_attached_message(part: Message, filename: str | None, data: bytes) -> bool:
    """An ``.eml`` file sent as a generic attachment."""
    if not filename or not filename.lower().endswith(".eml"):
        return False
    head = data[:4096]
    return b":" in head and b"\n" in head


def _parse_attached_bytes(data: bytes) -> Message | None:
    try:
        inner = email.message_from_bytes(data, policy=policy.default)
    except Exception:
        log.warning("cannot parse attached message", exc_info=True)
        return None
    if not inner.keys():
        return None
    return inner


def _is_body_text(part: Message) -> bool:
    """Text and HTML bodies (not attachments that happen to be text)."""
    if part.get_content_type() not in _BODY_TYPES:
        return False
    if (part.get_content_disposition() or "").lower() == "attachment":
        return False
    return part_filename(part) is None


def _is_html_body(part: Message) -> bool:
    return part.get_content_type() == "text/html" and (part.get_content_disposition() or "inline") != "attachment"


def _content_id(part: Message) -> str | None:
    value = part.get("Content-ID")
    if not value:
        return None
    cleaned = str(value).strip().strip("<>").strip().lower()
    return cleaned or None


def _cid_references(parts: Iterable[Message]) -> set[str]:
    refs: set[str] = set()
    for part in parts:
        text = _part_text(part)
        for match in _CID_RE.finditer(text):
            refs.add(urllib.parse.unquote(match.group(1)).strip("<>").strip().lower())
    return refs


def _part_text(part: Message) -> str:
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content
    except Exception:
        pass
    try:
        data = part.get_payload(decode=True) or b""
    except Exception:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Content detection


def sniff(data: bytes) -> str | None:
    """Recognize a file format by its magic bytes."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"):
        return "heic"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if b"%PDF-" in data[:1024]:
        return "pdf"
    return None


_SNIFFED_EXT = {"jpeg": ".jpg", "png": ".png", "tiff": ".tif", "webp": ".webp", "heic": ".heic"}


def classify(data: bytes, content_type: str | None, filename: str | None) -> tuple[str, str] | None:
    """``("pdf" | "image", extension)`` for document attachments, None for anything else.

    The content decides first; the declared type and the file name only count
    when the magic bytes are unknown.
    """
    ctype = (content_type or "").lower()
    ext = os.path.splitext(filename or "")[1].lower()
    detected = sniff(data)
    if detected == "pdf":
        return "pdf", ".pdf"
    if detected == "gif":
        return None
    if detected:
        return "image", _SNIFFED_EXT[detected]
    if ctype in PDF_TYPES or ext == ".pdf":
        return "pdf", ".pdf"
    if ctype in IGNORED_IMAGE_TYPES or ext in (".gif", ".svg", ".ico"):
        return None
    if ctype in IMAGE_TYPES:
        return "image", IMAGE_TYPES[ctype]
    if ext in IMAGE_EXTENSIONS:
        return "image", IMAGE_EXTENSIONS[ext]
    return None


_heif_registered = False


def image_size(data: bytes) -> tuple[int, int] | None:
    """Pixel size of an image without decoding it, or None."""
    global _heif_registered
    try:
        from PIL import Image

        if not _heif_registered:
            _heif_registered = True
            try:
                from pillow_heif import register_heif_opener

                register_heif_opener()
            except Exception:  # optional dependency
                pass
        with Image.open(io.BytesIO(data)) as image:
            return image.size
    except Exception:
        return None


# ---------------------------------------------------------------------------
# File names


def part_filename(part: Message) -> str | None:
    """Decoded file name of a MIME part (RFC 2231, RFC 2047 and raw 8-bit names)."""
    name: str | None = None
    try:
        name = part.get_filename()
    except Exception:
        name = None
    if name and "�" not in name and "=?" not in name and not _has_surrogates(name):
        return _clean_filename(name)
    raw = _raw_filename(part)
    if raw:
        return _clean_filename(raw)
    if name:
        return _clean_filename(_decode_words(_fix_surrogates(name)))
    return None


def _raw_filename(part: Message) -> str | None:
    """Read the file name straight from the raw headers (for non-compliant encodings)."""
    try:
        items = list(part.raw_items())
    except Exception:
        return None
    for header, param in (("content-disposition", "filename"), ("content-type", "name")):
        pattern = re.compile(_PARAM_RE_TEMPLATE.format(name=param), re.IGNORECASE)
        for key, value in items:
            if key.lower() != header:
                continue
            match = pattern.search(str(value))
            if not match:
                continue
            text = match.group(1).strip()
            if len(text) >= 2 and text[0] == text[-1] == '"':
                text = re.sub(r"\\(.)", r"\1", text[1:-1])
            text = _decode_words(_fix_surrogates(text)).strip()
            if text:
                return text
    return None


def _has_surrogates(value: str) -> bool:
    return any("\udc80" <= char <= "\udcff" for char in value)


def _fix_surrogates(value: str) -> str:
    """Re-decode raw 8-bit header bytes (kept as surrogates by the parser)."""
    if not _has_surrogates(value):
        return value
    data = value.encode("utf-8", "surrogateescape")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _decode_words(value: str) -> str:
    if "=?" not in value:
        return value
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _clean_filename(name: str) -> str | None:
    value = name.replace("\x00", "").strip()
    # Some clients send the full local path.
    value = value.replace("\\", "/").rsplit("/", 1)[-1]
    value = _WS_RE.sub(" ", value).strip()
    return value or None


# ---------------------------------------------------------------------------
# Headers and context


def _header_text(message: Message, name: str) -> str | None:
    try:
        value = message.get(name)
    except Exception:
        value = None
        for key, raw in _safe_raw_items(message):
            if key.lower() == name.lower():
                value = raw
                break
    if value is None:
        return None
    text = _fix_surrogates(str(value))
    if "\ufffd" in text:
        # The parser gave up on raw 8-bit bytes; decode them ourselves.
        for key, raw in _safe_raw_items(message):
            if key.lower() == name.lower():
                text = _fix_surrogates(str(raw))
                break
    if "=?" in text:
        text = _decode_words(text)
    return text


def _safe_raw_items(message: Message) -> list[tuple[str, str]]:
    try:
        return list(message.raw_items())
    except Exception:
        return []


def _clean_header_value(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _WS_RE.sub(" ", value).strip()
    return cleaned or None


def _first_address(message: Message, header: str) -> tuple[str | None, str | None]:
    try:
        value = message.get(header)
    except Exception:
        value = None
    addresses = getattr(value, "addresses", None)
    if addresses:
        first = addresses[0]
        address = (first.addr_spec or "").strip().lower()
        name = (first.display_name or "").strip()
        if address and address != "<>":
            return (name or None), address
    text = _header_text(message, header)
    if not text:
        return None, None
    pairs = getaddresses([text])
    for name, address in pairs:
        address = (address or "").strip().lower()
        if address:
            return (name.strip() or None), address
    return None, None


def _mail_date(message: Message) -> str | None:
    value = _header_text(message, "Date")
    if not value:
        return None
    parsed: datetime | None
    try:
        parsed = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timeutil.timezone()).isoformat(timespec="seconds")


def _body_text(message: Message) -> str:
    """Plain text of the message body (HTML converted roughly when there is no text part)."""
    html_text = None
    for kind, part in _own_parts(message):
        if kind != "leaf":
            continue
        disposition = (part.get_content_disposition() or "").lower()
        if disposition == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain":
            return _part_text(part)
        if ctype == "text/html" and html_text is None:
            html_text = _html_to_text(_part_text(part))
    return html_text or ""


def _html_to_text(markup: str) -> str:
    text = _DROP_BLOCK_RE.sub(" ", markup)
    text = _BLOCK_TAG_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    return html.unescape(text)


def _inline_forward_sender(message: Message) -> tuple[str | None, str] | None:
    """Original sender of a mail forwarded inline ("---- Weitergeleitete Nachricht ----")."""
    text = _body_text(message)
    if not text:
        return None
    lines = text.splitlines()
    windows: list[range] = []
    for index, line in enumerate(lines[:400]):
        if _FORWARD_MARKER_RE.search(line):
            windows.append(range(index + 1, min(len(lines), index + 14)))
    subject = _header_text(message, "Subject") or ""
    if not windows and _FORWARD_SUBJECT_RE.match(subject):
        windows.append(range(0, min(len(lines), 60)))
    for window in windows:
        for index in window:
            match = _FORWARD_FROM_RE.match(lines[index])
            if not match:
                continue
            parsed = _parse_from_value(match.group("value"))
            if parsed:
                return parsed
    return None


def _parse_from_value(value: str) -> tuple[str | None, str] | None:
    value = value.strip().strip("*").strip()
    mailto = _MAILTO_RE.search(value)
    if mailto:
        name = value[: mailto.start()].strip().strip('"').strip()
        return (name or None), mailto.group(1).strip().lower()
    name, address = parseaddr(value)
    address = (address or "").strip().lower()
    if "@" not in address:
        return None
    return (name.strip().strip('"') or None), address
