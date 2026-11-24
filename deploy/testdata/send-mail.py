"""Send a document to the DocuShelf SMTP receiver, the way a scanner with scan-to-mail does.

Standard library only, so it runs in a plain ``python`` container. Settings
come from the environment:

    SMTP_HOST        receiver host            (default: docushelf)
    SMTP_PORT        receiver port            (default: 2525)
    SMTP_RECIPIENT   accepted recipient       (default: scan@docushelf.test)
    SMTP_SENDER      sender address           (default: scanner@docushelf.test)
    SMTP_FILE        file to attach           (default: beispiel-rechnung.pdf next to this script)
    SMTP_USER, SMTP_PASSWORD   optional login (AUTH PLAIN/LOGIN)
    SMTP_STARTTLS    1: switch to TLS before sending
"""

from __future__ import annotations

import mimetypes
import os
import smtplib
import sys
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path


def main() -> int:
    host = os.environ.get("SMTP_HOST", "docushelf")
    port = int(os.environ.get("SMTP_PORT", "2525"))
    recipient = os.environ.get("SMTP_RECIPIENT", "scan@docushelf.test")
    sender = os.environ.get("SMTP_SENDER", "scanner@docushelf.test")
    path = Path(os.environ.get("SMTP_FILE") or Path(__file__).with_name("beispiel-rechnung.pdf"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    starttls = os.environ.get("SMTP_STARTTLS", "").lower() in ("1", "yes", "true", "ja")

    if not path.is_file():
        print(f"SMTP: Datei {path} nicht gefunden", file=sys.stderr)
        return 1
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    maintype, subtype = mime_type.split("/", 1)

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = f"Scan vom Testdrucker: {path.name}"
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain="docushelf.test")
    message.set_content("Gescanntes Dokument im Anhang.\n")
    message.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)

    try:
        with smtplib.SMTP(host, port, timeout=30) as client:
            client.ehlo()
            if starttls:
                client.starttls()
                client.ehlo()
            if user:
                client.login(user, password or "")
            client.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        print(f"SMTP: Senden an {host}:{port} fehlgeschlagen: {exc}", file=sys.stderr)
        return 1
    print(f"SMTP: {path.name} an {recipient} über {host}:{port} gesendet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
