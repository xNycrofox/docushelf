"""Tests for the built-in SMTP receiver (channels/smtp.py) with a real aiosmtpd server."""

from __future__ import annotations

import os
import smtplib
import socket
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timezone

import pytest

from docushelf.channels.sendung import read_sendung
from docushelf.channels.smtp import SmtpReceiver, parse_networks
from docushelf.config import SmtpConfig
from docushelf.errors import ConfigError
from tests.conftest import make_text_pdf

RECIPIENT = "scan@docushelf.local"


class FakeIntake:
    def __init__(self):
        self.triggered = 0

    def trigger(self):
        self.triggered += 1


@pytest.fixture
def start_receiver(base_services):
    started: list[SmtpReceiver] = []

    def build(**overrides) -> SmtpReceiver:
        values = dict(
            recipient=RECIPIENT,
            host="127.0.0.1",
            port=0,
            allowed_networks=("127.0.0.1/32",),
            max_message_bytes=2 * 1024 * 1024,
        )
        values.update(overrides)
        base_services.settings = base_services.settings.with_overrides(smtp=SmtpConfig(**values))
        base_services.intake = FakeIntake()
        receiver = SmtpReceiver(base_services)
        receiver.start()
        started.append(receiver)
        return receiver

    yield build
    for receiver in started:
        receiver.stop()


@pytest.fixture
def scan_mail(tmp_path):
    counter = {"n": 0}

    def build(*, attach=True, extra_bytes=0, message_id=None) -> bytes:
        counter["n"] += 1
        n = counter["n"]
        msg = EmailMessage()
        msg["From"] = "Scanner <scanner@printer.local>"
        msg["To"] = RECIPIENT
        msg["Subject"] = "Gescanntes Dokument"
        msg["Date"] = format_datetime(datetime(2025, 3, 3, 9, n, tzinfo=timezone.utc))
        msg["Message-ID"] = message_id or f"<scan-{n}@printer.local>"
        msg.set_content("Scan")
        if attach:
            pdf = make_text_pdf(tmp_path / f"scan{n}.pdf", [f"Scan {n}"]).read_bytes()
            msg.add_attachment(pdf, maintype="application", subtype="pdf", filename=f"scan_{n:04d}.pdf")
        if extra_bytes:
            msg.add_attachment(os.urandom(extra_bytes), maintype="application", subtype="octet-stream", filename="pad.bin")
        return msg.as_bytes(policy=policy.SMTP)  # CRLF line endings, as on the wire

    return build


def client(receiver: SmtpReceiver) -> smtplib.SMTP:
    return smtplib.SMTP("127.0.0.1", receiver.port, local_hostname="printer.local", timeout=10)


def batches(services) -> list:
    root = services.layout.source_dir("smtp")
    return [read_sendung(path) for path in sorted(root.iterdir())] if root.exists() else []


def test_accepts_mail_for_the_recipient_from_allowed_ip(start_receiver, scan_mail, base_services):
    receiver = start_receiver()
    assert receiver.port and receiver.port != 0
    with client(receiver) as smtp:
        refused = smtp.sendmail("scanner@printer.local", ["Scan@DocuShelf.Local"], scan_mail())
    assert refused == {}
    [batch] = batches(base_services)
    assert [doc.files for doc in batch.documents] == [["scan_0001.pdf"]]
    assert batch.context["mail_from"] == "scanner@printer.local"
    assert batch.context["mail_subject"] == "Gescanntes Dokument"
    assert batch.context["forwarded_by_owner"] is True
    assert base_services.intake.triggered == 1
    assert receiver.accepted == 1


def test_wrong_recipient_is_refused(start_receiver, scan_mail, base_services):
    receiver = start_receiver()
    with client(receiver) as smtp:
        with pytest.raises(smtplib.SMTPRecipientsRefused) as excinfo:
            smtp.sendmail("scanner@printer.local", ["someone@example.com"], scan_mail())
    assert excinfo.value.recipients["someone@example.com"][0] == 550
    assert batches(base_services) == []


def test_never_relays_to_other_recipients(start_receiver, scan_mail, base_services):
    receiver = start_receiver()
    with client(receiver) as smtp:
        refused = smtp.sendmail("scanner@printer.local", [RECIPIENT, "victim@example.com"], scan_mail())
    assert refused["victim@example.com"][0] == 550
    [batch] = batches(base_services)  # stored once, for us only
    assert len(batch.documents) == 1


def test_connection_from_disallowed_network_is_refused(start_receiver, base_services):
    receiver = start_receiver(allowed_networks=("192.168.100.0/24",))
    with pytest.raises(smtplib.SMTPConnectError) as excinfo:
        client(receiver)
    assert excinfo.value.smtp_code == 554
    assert batches(base_services) == []


def test_empty_network_list_refuses_everything(start_receiver):
    receiver = start_receiver(allowed_networks=())
    with pytest.raises(smtplib.SMTPConnectError):
        client(receiver)


def test_oversized_mail_is_refused_with_552(start_receiver, scan_mail, base_services):
    receiver = start_receiver(max_message_bytes=60_000)
    big = scan_mail(extra_bytes=80_000)
    # ESMTP clients announce the size: refused at MAIL FROM.
    with client(receiver) as smtp:
        with pytest.raises(smtplib.SMTPSenderRefused) as excinfo:
            smtp.sendmail("scanner@printer.local", [RECIPIENT], big)
    assert excinfo.value.smtp_code == 552
    # Plain SMTP clients do not: refused after DATA.
    with client(receiver) as smtp:
        smtp.helo("printer.local")
        with pytest.raises(smtplib.SMTPDataError) as excinfo:
            smtp.sendmail("scanner@printer.local", [RECIPIENT], big)
    assert excinfo.value.smtp_code == 552
    assert batches(base_services) == []


def test_mail_without_document_is_refused(start_receiver, scan_mail, base_services):
    receiver = start_receiver()
    with client(receiver) as smtp:
        with pytest.raises(smtplib.SMTPDataError) as excinfo:
            smtp.sendmail("scanner@printer.local", [RECIPIENT], scan_mail(attach=False))
    assert excinfo.value.smtp_code == 550
    assert batches(base_services) == []


def test_duplicate_delivery_is_stored_once(start_receiver, scan_mail, base_services):
    receiver = start_receiver()
    raw = scan_mail()
    with client(receiver) as smtp:
        smtp.sendmail("scanner@printer.local", [RECIPIENT], raw)
        smtp.sendmail("scanner@printer.local", [RECIPIENT], raw)  # e.g. a retry after a lost reply
    assert len(batches(base_services)) == 1


def test_auth_is_required_when_configured(start_receiver, scan_mail, base_services):
    receiver = start_receiver(username="scanner", password="geheim")
    with client(receiver) as smtp:
        smtp.ehlo()
        assert "PLAIN" in smtp.esmtp_features.get("auth", "")
        with pytest.raises(smtplib.SMTPSenderRefused) as excinfo:
            smtp.sendmail("scanner@printer.local", [RECIPIENT], scan_mail())
        assert excinfo.value.smtp_code == 530
    with client(receiver) as smtp:
        with pytest.raises(smtplib.SMTPAuthenticationError):
            smtp.login("scanner", "falsch")
    with client(receiver) as smtp:
        smtp.login("scanner", "geheim")
        assert smtp.sendmail("scanner@printer.local", [RECIPIENT], scan_mail()) == {}
    assert len(batches(base_services)) == 1


def test_auth_login_mechanism(start_receiver, scan_mail, base_services):
    receiver = start_receiver(username="scanner", password="Passwort-123")
    with client(receiver) as smtp:
        smtp.ehlo()
        smtp.user, smtp.password = "scanner", "Passwort-123"
        code, _ = smtp.auth("LOGIN", smtp.auth_login)
        assert code == 235
        smtp.sendmail("scanner@printer.local", [RECIPIENT], scan_mail())
    assert len(batches(base_services)) == 1


def test_auth_not_offered_without_credentials(start_receiver):
    receiver = start_receiver()
    with client(receiver) as smtp:
        smtp.ehlo()
        assert "auth" not in smtp.esmtp_features


def test_storage_errors_give_a_temporary_failure(start_receiver, scan_mail, base_services, monkeypatch):
    receiver = start_receiver()

    def disk_full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("docushelf.channels.smtp.write_sendung", disk_full)
    with client(receiver) as smtp:
        with pytest.raises(smtplib.SMTPDataError) as excinfo:
            smtp.sendmail("scanner@printer.local", [RECIPIENT], scan_mail())
    assert excinfo.value.smtp_code == 451
    assert receiver.last_error


def test_start_and_stop_are_idempotent(start_receiver):
    receiver = start_receiver()
    port = receiver.port
    receiver.start()
    assert receiver.port == port
    receiver.stop()
    receiver.stop()
    assert not receiver.running
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()


def test_busy_port_is_retried_in_the_background(start_receiver, base_services):
    blocker = socket.create_server(("127.0.0.1", 0))
    port = blocker.getsockname()[1]
    try:
        values = dict(recipient=RECIPIENT, host="127.0.0.1", port=port, allowed_networks=("127.0.0.1/32",))
        base_services.settings = base_services.settings.with_overrides(smtp=SmtpConfig(**values))
        receiver = SmtpReceiver(base_services)
        receiver.retry_seconds = 0.05
        receiver.start()  # must not raise
        assert not receiver.running
        assert "nicht öffnen" in receiver.last_error
    finally:
        blocker.close()
    try:
        import time

        deadline = time.monotonic() + 5
        while not receiver.running and time.monotonic() < deadline:
            time.sleep(0.05)
        assert receiver.running and receiver.port == port
    finally:
        receiver.stop()


def test_peer_and_network_rules(base_services):
    base_services.settings = base_services.settings.with_overrides(
        smtp=SmtpConfig(recipient=RECIPIENT, allowed_networks=("192.168.100.0/24", "192.0.2.6", "fd00::/8"))
    )
    receiver = SmtpReceiver(base_services)
    assert receiver.peer_allowed(("192.168.100.20", 4000))
    assert receiver.peer_allowed(("::ffff:192.168.100.20", 4000, 0, 0))
    assert receiver.peer_allowed(("192.0.2.6", 1))
    assert receiver.peer_allowed(("fd00::1", 1, 0, 0))
    assert not receiver.peer_allowed(("192.0.2.7", 1))
    assert not receiver.peer_allowed(None)
    assert receiver.recipient_allowed("<SCAN@docushelf.local>")
    assert not receiver.recipient_allowed("scan@docushelf.local.evil.example")
    with pytest.raises(ConfigError):
        parse_networks(["192.168.100.0/33"])


def test_received_scans_are_taken_over_by_the_real_intake(start_receiver, scan_mail, base_services):
    intake_module = pytest.importorskip("docushelf.inbox.intake")
    receiver = start_receiver()
    with client(receiver) as smtp:
        smtp.sendmail("scanner@printer.local", [RECIPIENT], scan_mail())
    base_services.worker = None
    created = intake_module.Intake(base_services).scan_once()
    assert len(created) == 1
    item = base_services.inbox.load(created[0])
    assert item.source == "smtp"
    assert item.original_files == ["original/scan_0001.pdf"]
    assert item.context["forwarded_by_owner"] is True


# -- addresses of one person (channel settings) ------------------------------------------------


class FakeChannels:
    """The part of the channel manager the receiver uses."""

    def __init__(self, addresses):
        from docushelf.channels.store import Recipient

        self.recipients = {address: Recipient(address, owner) for address, owner in addresses.items()}

    def smtp_route(self, address):
        return self.recipients.get(address)

    def mark_smtp(self, context, address):
        from docushelf.channels import origin

        recipient = self.smtp_route(address)
        if recipient is not None and recipient.owner:
            origin.mark(context, owner=recipient.owner, endpoint="Scan per Mail")


def test_per_person_addresses_route_and_mark_the_owner(start_receiver, scan_mail, base_services):
    base_services.channels = FakeChannels({RECIPIENT: None, "scan-erika@docushelf.local": "erika"})
    receiver = start_receiver()
    assert receiver.recipient_allowed("Scan-Erika@docushelf.local")
    assert not receiver.recipient_allowed("scan-max@docushelf.local")
    with client(receiver) as smtp:
        smtp.sendmail("scanner@printer.local", ["scan-erika@docushelf.local"], scan_mail())
        smtp.sendmail("scanner@printer.local", [RECIPIENT], scan_mail())
    owners = sorted(str(b.context.get("owner")) for b in batches(base_services))
    assert owners == ["None", "erika"]


def test_channel_settings_can_refuse_the_configured_recipient(start_receiver, base_services):
    base_services.channels = FakeChannels({})
    receiver = start_receiver()
    assert not receiver.recipient_allowed(RECIPIENT)
