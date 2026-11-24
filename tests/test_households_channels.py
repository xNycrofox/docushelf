"""Channels with several households: SMTP routing, IMAP per household, drop folders and intake."""

from __future__ import annotations

import smtplib
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime

import pytest
import yaml

from docushelf.channels.imap import ImapFetcher
from docushelf.channels.sendung import read_sendung
from docushelf.channels.smtp import SmtpReceiver, receiver_for_registry
from docushelf.config import load_settings
from docushelf.households import Registry
from tests.conftest import make_text_pdf
from tests.test_imap import FakeImapServer

HOUSEHOLDS = [
    {
        "id": "familie-a",
        "name": "Familie A",
        "path": ".",
        "groups": ["familie-a"],
        "smtp_recipients": ["Scan-A@docushelf.local"],
        "imap": {
            "host": "imap-a.example.org",
            "user": "dokumente-a@example.org",
            "password": "secret-a",
            "allowed_senders": ["erika@example.org"],
        },
    },
    {
        "id": "eltern",
        "name": "Eltern",
        "groups": ["docushelf-eltern"],
        "smtp_recipients": ["scan-eltern@docushelf.local", "post-eltern@docushelf.local"],
        "imap": {
            "host": "imap-eltern.example.org",
            "user": "dokumente-eltern@example.org",
            "password": "secret-eltern",
            "allowed_senders": ["opa@example.org"],
        },
    },
]


def write_households(data_dir, households) -> None:
    path = data_dir / "config" / "households.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"households": households}), encoding="utf-8")


@pytest.fixture
def registry(env, data_dir):
    write_households(data_dir, HOUSEHOLDS)
    settings = load_settings(
        {
            **env,
            "DOCUSHELF_SMTP_ENABLED": "true",
            "DOCUSHELF_SMTP_HOST": "127.0.0.1",
            "DOCUSHELF_SMTP_PORT": "0",
            "DOCUSHELF_SMTP_ALLOWED_NETWORKS": "127.0.0.1/32",
        }
    )
    reg = Registry(settings)
    yield reg
    reg.stop()
    reg.close()


@pytest.fixture
def receiver(registry):
    rec = receiver_for_registry(registry, registry.settings.smtp)
    rec.start()
    yield rec
    rec.stop()


@pytest.fixture
def scan_mail(tmp_path):
    counter = {"n": 0}

    def build(to, *, sender="Scanner <scanner@printer.local>") -> bytes:
        counter["n"] += 1
        n = counter["n"]
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = ", ".join(to) if isinstance(to, (list, tuple)) else to
        msg["Subject"] = "Gescanntes Dokument"
        msg["Date"] = format_datetime(datetime(2025, 3, 3, 9, n, tzinfo=timezone.utc))
        msg["Message-ID"] = f"<scan-{n}@printer.local>"
        msg.set_content("Scan")
        pdf = make_text_pdf(tmp_path / f"scan{n}.pdf", [f"Scan {n}"]).read_bytes()
        msg.add_attachment(pdf, maintype="application", subtype="pdf", filename=f"scan_{n:04d}.pdf")
        return msg.as_bytes(policy=policy.SMTP)

    return build


def send(receiver: SmtpReceiver, recipients, raw: bytes):
    with smtplib.SMTP("127.0.0.1", receiver.port, local_hostname="printer.local", timeout=10) as smtp:
        return smtp.sendmail("scanner@printer.local", recipients, raw)


def batches(services, source: str) -> list:
    root = services.layout.source_dir(source)
    return [read_sendung(path) for path in sorted(root.iterdir())] if root.exists() else []


# -- SMTP routing ---------------------------------------------------------------------------------------


def test_receiver_is_global_and_services_have_none(registry, receiver):
    assert receiver.multi_household
    assert registry.services("familie-a").smtp is None
    assert registry.services("eltern").smtp is None


def test_mail_goes_to_the_household_of_the_recipient(registry, receiver, scan_mail):
    a, b = registry.services("familie-a"), registry.services("eltern")
    assert send(receiver, ["scan-eltern@docushelf.local"], scan_mail("scan-eltern@docushelf.local")) == {}
    assert batches(a, "smtp") == []
    [batch] = batches(b, "smtp")
    assert batch.context["forwarded_by_owner"] is True
    # the second address of the same household
    send(receiver, ["post-eltern@docushelf.local"], scan_mail("post-eltern@docushelf.local"))
    assert len(batches(b, "smtp")) == 2 and batches(a, "smtp") == []
    # the batch lies below the household's own drop zone
    assert batch.directory.is_relative_to(b.layout.data / "eingang" / "quellen" / "smtp")


def test_recipient_matching_ignores_case_and_display_names(registry, receiver, scan_mail):
    a = registry.services("familie-a")
    send(receiver, ["SCAN-a@DocuShelf.Local"], scan_mail("scan-a@docushelf.local"))
    assert len(batches(a, "smtp")) == 1
    assert receiver.resolve("Scanner <scan-A@docushelf.local>") is a


def test_unknown_recipient_is_refused_with_german_text(registry, receiver, scan_mail):
    with pytest.raises(smtplib.SMTPRecipientsRefused) as excinfo:
        send(receiver, ["fremd@docushelf.local"], scan_mail("fremd@docushelf.local"))
    code, text = excinfo.value.recipients["fremd@docushelf.local"]
    assert code == 550
    assert b"keinem Haushalt zugeordnet" in text
    for services in registry.created():
        assert batches(services, "smtp") == []


def test_known_and_unknown_recipient_only_delivers_to_the_known(registry, receiver, scan_mail):
    a, b = registry.services("familie-a"), registry.services("eltern")
    refused = send(receiver, ["scan-a@docushelf.local", "fremd@example.com"], scan_mail("scan-a@docushelf.local"))
    assert refused["fremd@example.com"][0] == 550
    assert len(batches(a, "smtp")) == 1 and batches(b, "smtp") == []


def test_mail_to_several_households_is_delivered_once_per_household(registry, receiver, scan_mail):
    a, b = registry.services("familie-a"), registry.services("eltern")
    both = ["scan-a@docushelf.local", "scan-eltern@docushelf.local", "post-eltern@docushelf.local"]
    assert send(receiver, both, scan_mail(both)) == {}
    [batch_a] = batches(a, "smtp")
    [batch_b] = batches(b, "smtp")  # two addresses of one household: still one copy
    assert len(batch_a.documents) == 1 and len(batch_b.documents) == 1
    assert receiver.accepted == 2
    send(receiver, both, scan_mail(both))  # a new mail, not a copy
    assert len(batches(a, "smtp")) == 2


def test_resending_the_same_mail_is_stored_once_per_household(registry, receiver, scan_mail):
    a, b = registry.services("familie-a"), registry.services("eltern")
    raw = scan_mail(["scan-a@docushelf.local", "scan-eltern@docushelf.local"])
    both = ["scan-a@docushelf.local", "scan-eltern@docushelf.local"]
    send(receiver, both, raw)
    send(receiver, both, raw)
    assert len(batches(a, "smtp")) == 1 and len(batches(b, "smtp")) == 1


def test_global_recipient_keeps_working_for_the_root_household(env, data_dir, scan_mail):
    write_households(data_dir, HOUSEHOLDS)
    settings = load_settings(
        {
            **env,
            "DOCUSHELF_SMTP_ENABLED": "true",
            "DOCUSHELF_SMTP_RECIPIENT": "Scan@docushelf.local",
            "DOCUSHELF_SMTP_HOST": "127.0.0.1",
            "DOCUSHELF_SMTP_PORT": "0",
            "DOCUSHELF_SMTP_ALLOWED_NETWORKS": "127.0.0.1/32",
        }
    )
    reg = Registry(settings)
    rec = receiver_for_registry(reg, settings.smtp)
    rec.start()
    try:
        send(rec, ["scan@docushelf.local"], scan_mail("scan@docushelf.local"))
        assert len(batches(reg.services("familie-a"), "smtp")) == 1
        assert batches(reg.services("eltern"), "smtp") == []
    finally:
        rec.stop()
        reg.close()


# -- single household: unchanged ------------------------------------------------------------------------


def test_single_household_keeps_the_recipient_rule_and_its_own_receiver(env, scan_mail):
    settings = load_settings(
        {
            **env,
            "DOCUSHELF_SMTP_ENABLED": "true",
            "DOCUSHELF_SMTP_RECIPIENT": "scan@docushelf.local",
            "DOCUSHELF_SMTP_HOST": "127.0.0.1",
            "DOCUSHELF_SMTP_PORT": "0",
            "DOCUSHELF_SMTP_ALLOWED_NETWORKS": "127.0.0.1/32",
        }
    )
    reg = Registry(settings)
    try:
        services = reg.services("default")
        assert isinstance(services.smtp, SmtpReceiver) and reg.smtp is None
        assert not services.smtp.multi_household
        services.smtp.start()
        try:
            assert send(services.smtp, ["Scan@docushelf.local"], scan_mail("scan@docushelf.local")) == {}
            with pytest.raises(smtplib.SMTPRecipientsRefused) as excinfo:
                send(services.smtp, ["other@docushelf.local"], scan_mail("other@docushelf.local"))
            assert excinfo.value.recipients["other@docushelf.local"][0] == 550
            assert b"Recipient address rejected" in excinfo.value.recipients["other@docushelf.local"][1]
        finally:
            services.smtp.stop()
        assert len(batches(services, "smtp")) == 1
    finally:
        reg.close()


# -- IMAP per household -----------------------------------------------------------------------------------


@pytest.fixture
def imap_mail(tmp_path):
    counter = {"n": 0}

    def build(sender: str, to: str) -> bytes:
        counter["n"] += 1
        n = counter["n"]
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = to
        msg["Subject"] = "Rechnung"
        msg["Date"] = format_datetime(datetime(2025, 3, n % 28 + 1, 9, 0, tzinfo=timezone.utc))
        msg["Message-ID"] = f"<imap-{n}@example.org>"
        msg.set_content("Hallo")
        pdf = make_text_pdf(tmp_path / f"i{n}.pdf", [f"Rechnung {n}"]).read_bytes()
        msg.add_attachment(pdf, maintype="application", subtype="pdf", filename=f"Rechnung {n}.pdf")
        return msg.as_bytes()

    return build


def test_every_household_has_its_own_mailbox_configuration(registry):
    a, b = registry.services("familie-a"), registry.services("eltern")
    assert a.imap is not None and b.imap is not None and a.imap is not b.imap
    assert (a.settings.imap.host, a.settings.imap.username) == ("imap-a.example.org", "dokumente-a@example.org")
    assert (b.settings.imap.host, b.settings.imap.username) == ("imap-eltern.example.org", "dokumente-eltern@example.org")
    assert a.imap.household_id == "familie-a" and b.imap.household_id == "eltern"
    assert a.imap.status()["household"] == "familie-a"


def test_global_imap_settings_only_apply_to_the_root_household(env, data_dir):
    write_households(
        data_dir,
        [
            {"id": "familie-a", "name": "Familie A", "path": ".", "groups": ["a"]},
            {"id": "eltern", "name": "Eltern", "groups": ["e"]},
        ],
    )
    settings = load_settings(
        {
            **env,
            "DOCUSHELF_IMAP_ENABLED": "true",
            "DOCUSHELF_IMAP_HOST": "imap.example.org",
            "DOCUSHELF_IMAP_USER": "dokumente@example.org",
            "DOCUSHELF_IMAP_PASSWORD": "secret",
        }
    )
    reg = Registry(settings)
    try:
        assert reg.services("familie-a").imap is not None
        assert reg.services("eltern").imap is None
    finally:
        reg.close()


def test_imap_fetcher_writes_only_into_its_own_drop_zone(registry, imap_mail):
    a, b = registry.services("familie-a"), registry.services("eltern")
    server_a, server_b = FakeImapServer(username="dokumente-a@example.org"), FakeImapServer(username="dokumente-eltern@example.org")
    server_a.password, server_b.password = "secret-a", "secret-eltern"
    server_a.deliver(imap_mail("Erika <erika@example.org>", "dokumente-a@example.org"))
    server_b.deliver(imap_mail("Opa <opa@example.org>", "dokumente-eltern@example.org"))
    fetcher_a = ImapFetcher(a, connection_factory=server_a.connect)
    fetcher_b = ImapFetcher(b, connection_factory=server_b.connect)

    assert fetcher_a.fetch_once() == 1
    assert batches(b, "mail") == []
    [batch_a] = batches(a, "mail")
    assert batch_a.context["mail_from"] == "erika@example.org"
    assert batch_a.directory.is_relative_to(a.layout.data / "eingang" / "quellen" / "mail")

    assert fetcher_b.fetch_once() == 1
    [batch_b] = batches(b, "mail")
    assert batch_b.context["mail_from"] == "opa@example.org"
    assert batch_b.directory.is_relative_to(b.layout.data / "eingang" / "quellen" / "mail")
    assert len(batches(a, "mail")) == 1
    # the households' folders are separate trees
    assert a.layout.data != b.layout.data and not b.layout.data.is_relative_to(a.layout.sources)


def test_imap_allowlist_is_per_household(registry, imap_mail):
    a = registry.services("familie-a")
    server = FakeImapServer(username="dokumente-a@example.org")
    server.password = "secret-a"
    raw = imap_mail("Opa <opa@example.org>", "dokumente-a@example.org")  # allowed for Eltern only
    server.deliver(raw)
    assert ImapFetcher(a, connection_factory=server.connect).fetch_once() == 0
    assert batches(a, "mail") == []
    assert server.raws("DocuShelf/Abgelehnt") == [raw]


# -- drop folders and intake per household ----------------------------------------------------------------


def test_drop_folders_lie_below_every_household(registry):
    a, b = registry.services("familie-a"), registry.services("eltern")
    assert a.layout.data == registry.settings.data_dir
    assert b.layout.data == registry.settings.data_dir / "haushalte" / "eltern"
    for services in (a, b):
        for name in ("upload", "smb", "ftp", "mail", "smtp"):
            assert services.layout.source_dir(name) == services.layout.data / "eingang" / "quellen" / name
            assert services.layout.source_dir(name).is_dir()


def test_intake_only_takes_files_of_its_own_household(registry, samples):
    a, b = registry.services("familie-a"), registry.services("eltern")
    samples.text_pdf(a.layout.source_dir("smb") / "Strom.pdf", ["Strom A"])
    samples.text_pdf(b.layout.source_dir("ftp") / "Wasser.pdf", ["Wasser Eltern"])

    created_a = a.intake.scan_once()
    assert len(created_a) == 1
    assert a.inbox.load(created_a[0]).source == "smb"
    assert [p.name for p in b.layout.source_dir("ftp").iterdir()] == ["Wasser.pdf"]  # untouched

    created_b = b.intake.scan_once()
    assert len(created_b) == 1
    item_b = b.inbox.load(created_b[0])
    assert item_b.source == "ftp" and [p.name for p in item_b.original_paths()] == ["Wasser.pdf"]
    assert a.inbox.find(created_b[0]) is None and b.inbox.find(created_a[0]) is None
    assert sum(a.inbox.counts().values()) == 1 and sum(b.inbox.counts().values()) == 1


def test_watch_folders_are_per_household(env, data_dir, tmp_path, samples):
    watch_a, watch_b = tmp_path / "watch-a", tmp_path / "watch-b"
    write_households(
        data_dir,
        [
            {"id": "familie-a", "name": "Familie A", "groups": ["a"], "watch_folders": {"nas": str(watch_a)}},
            {"id": "eltern", "name": "Eltern", "groups": ["e"], "watch_folders": {"nas": str(watch_b)}},
        ],
    )
    for folder in (watch_a, watch_b):
        folder.mkdir()
    reg = Registry(load_settings({**env, "DOCUSHELF_WATCH_FOLDERS": f"global={tmp_path / 'global'}"}))
    try:
        a, b = reg.services("familie-a"), reg.services("eltern")
        # the global watch folders belong to the household living in the data root (none here)
        assert [s.name for s in a.settings.sources if s.name.startswith("watch-")] == ["watch-nas"]
        assert [s.name for s in b.settings.sources if s.name.startswith("watch-")] == ["watch-nas"]
        samples.text_pdf(watch_a / "Brief.pdf", ["Brief A"])
        created = a.intake.scan_once()
        assert len(created) == 1 and a.inbox.load(created[0]).source == "watch-nas"
        assert b.intake.scan_once() == []
        assert sum(b.inbox.counts().values()) == 0
    finally:
        reg.close()


def test_watch_folder_inside_another_household_is_ignored(env, data_dir, samples):
    write_households(
        data_dir,
        [
            {"id": "familie-a", "name": "Familie A", "groups": ["a"]},
            {"id": "eltern", "name": "Eltern", "groups": ["e"]},
        ],
    )
    reg = Registry(load_settings(env))
    try:
        a, b = reg.services("familie-a"), reg.services("eltern")
        write_households(
            data_dir,
            [
                {"id": "familie-a", "name": "Familie A", "groups": ["a"], "watch_folders": {"spy": str(b.layout.inbox)}},
                {"id": "eltern", "name": "Eltern", "groups": ["e"]},
            ],
        )
        reg2 = Registry(load_settings(env))
        try:
            from docushelf.inbox.intake import protect_households

            protect_households(reg2)
            spy = reg2.services("familie-a")
            samples.text_pdf(b.layout.inbox / "geheim.pdf", ["Eltern"])
            assert spy.intake.scan_once() == []
            assert (b.layout.inbox / "geheim.pdf").exists()
        finally:
            reg2.close()
    finally:
        reg.close()


def test_watch_folder_never_takes_files_of_another_households_nested_watch_folder(env, data_dir, tmp_path, samples):
    # The root household watches /watch (global setting), the parents /watch/eltern.
    watch = tmp_path / "watch"
    (watch / "eltern").mkdir(parents=True)
    (watch / "scanner").mkdir()
    write_households(
        data_dir,
        [
            {"id": "familie-a", "name": "Familie A", "path": ".", "groups": ["a"]},
            {"id": "eltern", "name": "Eltern", "groups": ["e"], "watch_folders": {"nas": str(watch / "eltern")}},
        ],
    )
    reg = Registry(load_settings({**env, "DOCUSHELF_WATCH_FOLDERS": f"nas={watch}"}))
    try:
        from docushelf.inbox.intake import protect_households

        protect_households(reg)
        a, b = reg.services("familie-a"), reg.services("eltern")
        for intake in (a.intake, b.intake):
            intake.subdir_stable_seconds = 0
        samples.text_pdf(watch / "eltern" / "Rente.pdf", ["Rente Eltern"])
        samples.text_pdf(watch / "scanner" / "Strom.pdf", ["Strom A"])

        created_a = a.intake.scan_once()
        assert [p.name for item in created_a for p in a.inbox.load(item).original_paths()] == ["Strom.pdf"]
        assert (watch / "eltern" / "Rente.pdf").exists()

        created_b = b.intake.scan_once()
        assert [p.name for item in created_b for p in b.inbox.load(item).original_paths()] == ["Rente.pdf"]
    finally:
        reg.close()


def test_the_same_watch_folder_in_two_households_is_ignored_by_both(env, data_dir, tmp_path, samples):
    shared = tmp_path / "scanner"
    shared.mkdir()
    write_households(
        data_dir,
        [
            {"id": "familie-a", "name": "Familie A", "groups": ["a"], "watch_folders": {"nas": str(shared)}},
            {"id": "eltern", "name": "Eltern", "groups": ["e"], "watch_folders": {"nas": str(shared)}},
        ],
    )
    reg = Registry(load_settings(env))
    try:
        from docushelf.inbox.intake import protect_households

        protect_households(reg)
        samples.text_pdf(shared / "Brief.pdf", ["Brief"])
        assert reg.services("familie-a").intake.scan_once() == []
        assert reg.services("eltern").intake.scan_once() == []
        assert (shared / "Brief.pdf").exists()
    finally:
        reg.close()
