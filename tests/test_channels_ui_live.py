"""Channel settings at run time: loader, live restart, owner context, SMTP routing.

The web page is tested in test_channels_ui.py; these tests drive the channel
manager (``services.channels``) directly with fake IMAP fetchers, the real
intake and a real SMTP receiver.
"""

from __future__ import annotations

import smtplib
import stat
from dataclasses import replace
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime

import pytest
import yaml

from docushelf.channels import origin
from docushelf.channels.manager import ChannelManager
from docushelf.channels.sendung import read_sendung
from docushelf.channels.smtp import SmtpReceiver, receiver_for_registry
from docushelf.channels.store import (
    ChannelConfig,
    ChannelStore,
    Endpoint,
    Mailbox,
    Recipient,
    ScannerChannel,
    SmtpChannel,
    WatchFolder,
    dump_channels,
    parse_channels,
)
from docushelf.config import load_settings
from docushelf.errors import ConfigError
from docushelf.households import Registry
from docushelf.services import Services
from tests.conftest import make_text_pdf


class FakeFetcher:
    """Stands in for ImapFetcher: records starts and stops."""

    created: list["FakeFetcher"] = []

    def __init__(self, services, config):
        self.services = services
        self.config = config
        self.started = 0
        self.stopped = 0
        FakeFetcher.created.append(self)

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    @property
    def running(self):
        return self.started > self.stopped

    def status(self):
        return {"running": self.running, "name": self.config.name}


@pytest.fixture(autouse=True)
def _reset_fakes():
    FakeFetcher.created = []


def mailbox(name="familie", **changes):
    values = dict(name=name, host="imap.example.org", user=f"{name}@example.org", allowed_senders=("*",))
    values.update(changes)
    return Mailbox(**values)


def write_channels(config_dir, config: ChannelConfig, passwords: dict[str, str] | None = None):
    store = ChannelStore(config_dir)
    store.save(config, passwords=passwords or {})
    return store


# ---------------------------------------------------------------------------
# Loader


def test_loader_rejects_unknown_keys_and_bad_values():
    with pytest.raises(ConfigError, match="unbekannter Eintrag „imaps“"):
        parse_channels("imaps: []\n")
    with pytest.raises(ConfigError, match="unbekannter Eintrag „passwort“"):
        parse_channels("imap:\n  - {name: a, host: h, user: u, passwort: x}\n")
    with pytest.raises(ConfigError, match="ssl oder starttls"):
        parse_channels("imap:\n  - {name: a, host: h, user: u, tls: none}\n")
    with pytest.raises(ConfigError, match="zweimal"):
        parse_channels("imap:\n  - {name: a, host: h, user: u}\n  - {name: a, host: h, user: u}\n")
    with pytest.raises(ConfigError, match="keine E-Mail-Adresse"):
        parse_channels("smtp: {recipients: [{address: nix}]}\n")
    with pytest.raises(ConfigError, match="doppelt"):
        parse_channels("smtp: {recipients: [a@b.de, A@B.de]}\n")
    with pytest.raises(ConfigError, match="unbekannter Eintrag „usb“"):
        parse_channels("scanner: {usb: {enabled: true}}\n")
    with pytest.raises(ConfigError, match="ungültig"):
        parse_channels("scanner: {smb: {endpoints: [{folder: ../x}]}}\n")
    with pytest.raises(ConfigError, match="absolut"):
        parse_channels("watch: [{name: nas, path: relativ}]\n")
    with pytest.raises(ConfigError, match="ist ungültig"):
        parse_channels("imap: [\n")


def test_dump_and_parse_round_trip():
    config = ChannelConfig(
        imap=(mailbox(owner="erika", tls="starttls", port=143, enabled=False),),
        smtp=SmtpChannel(True, (Recipient("scan@example.org"), Recipient("scan-erika@example.org", "erika"))),
        scanners=(ScannerChannel("smb", True, (Endpoint("erika", "erika"), Endpoint("max", None, False))), ScannerChannel("ftp", False)),
        watch=(WatchFolder("nas", __import__("pathlib").Path("/watch/nas"), remove_after_import=False),),
    )
    assert parse_channels(dump_channels(config)) == config


def test_store_writes_backup_secrets_and_refuses_invalid_results(tmp_path):
    store = ChannelStore(tmp_path / "config")
    first = ChannelConfig(imap=(mailbox(),))
    with pytest.raises(ConfigError, match="kein Passwort"):
        store.save(first)
    assert not store.exists()

    store.save(first, passwords={"familie": "geheim"})
    secret = store.secret_path("familie")
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600 and secret.read_text() == "geheim"
    assert not store.backup_path.exists()
    text = store.path.read_text()
    assert "geheim" not in text

    second = ChannelConfig(imap=(mailbox(poll_seconds=600),))
    store.save(second)
    assert store.backup_path.read_text() == text
    assert store.load() == second

    store.save(ChannelConfig(), remove_secrets=["familie"])
    assert not secret.exists()


def test_store_load_needs_the_password_of_enabled_mailboxes(tmp_path):
    store = write_channels(tmp_path / "config", ChannelConfig(imap=(mailbox(),)), {"familie": "pw"})
    store.secret_path("familie").unlink()
    with pytest.raises(ConfigError, match="kein Passwort"):
        store.load()
    disabled = ChannelConfig(imap=(mailbox(enabled=False),))
    store.path.write_text(dump_channels(disabled))
    assert store.load() == disabled


def test_loose_secret_permissions_are_tightened(tmp_path):
    store = write_channels(tmp_path / "config", ChannelConfig(imap=(mailbox(),)), {"familie": "pw"})
    store.secret_path("familie").chmod(0o644)
    assert store.password("familie") == "pw"
    assert stat.S_IMODE(store.secret_path("familie").stat().st_mode) == 0o600


def test_invalid_channels_file_stops_the_start(settings):
    config_dir = settings.data_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "channels.yaml").write_text("imap: [{name: x}]\n")
    with pytest.raises(ConfigError, match="Server"):
        Services(settings)


# ---------------------------------------------------------------------------
# Environment-only installations


def test_without_the_file_everything_stays_as_configured(env):
    settings = load_settings(
        {
            **env,
            "DOCUSHELF_IMAP_ENABLED": "true",
            "DOCUSHELF_IMAP_HOST": "imap.env.example",
            "DOCUSHELF_IMAP_USER": "env@example.org",
            "DOCUSHELF_IMAP_PASSWORD": "env-pw",
            "DOCUSHELF_WATCH_FOLDERS": "NAS=/watch/nas",
        }
    )
    services = Services(settings)
    try:
        assert services.settings is settings
        assert services.settings.sources == settings.sources
        manager = services.channels
        assert not manager.from_file
        assert [box.host for box in manager.config.imap] == ["imap.env.example"]
        assert services.imap is not None and services.imap.config is settings.imap
        assert [w.name for w in manager.config.watch] == ["NAS"]
        assert not (settings.data_dir / "config" / "channels.yaml").exists()
    finally:
        services.stop()
        services.index.close()


def test_first_save_takes_over_the_environment_password(env):
    settings = load_settings(
        {
            **env,
            "DOCUSHELF_IMAP_ENABLED": "true",
            "DOCUSHELF_IMAP_HOST": "imap.env.example",
            "DOCUSHELF_IMAP_USER": "env@example.org",
            "DOCUSHELF_IMAP_PASSWORD": "env-pw",
        }
    )
    services = Services(settings)
    try:
        manager = services.channels
        manager.fetcher_factory = FakeFetcher
        manager.update(lambda c: replace(c, smtp=SmtpChannel(True, (Recipient("scan@example.org"),))))
        assert manager.from_file
        assert manager.store.password("postfach") == "env-pw"
        assert services.settings.imap.password == "env-pw" and services.settings.imap.name == "postfach"
        assert ChannelStore(services.layout.config).load() == manager.config
    finally:
        services.stop()
        services.index.close()


# ---------------------------------------------------------------------------
# Live restart


def test_update_restarts_only_the_changed_fetchers(settings):
    store = write_channels(
        settings.data_dir / "config",
        ChannelConfig(imap=(mailbox("a"), mailbox("b"))),
        {"a": "pw-a", "b": "pw-b"},
    )
    services = Services(settings)
    try:
        manager = services.channels
        assert manager.from_file and set(manager.fetchers) == {"a", "b"}
        # Replace the real fetchers by fakes, as if they had been created by the factory.
        manager.fetcher_factory = FakeFetcher
        manager.fetchers = {name: FakeFetcher(services, f.config) for name, f in manager.fetchers.items()}
        manager.start()
        a, b = manager.fetchers["a"], manager.fetchers["b"]
        assert a.running and b.running

        manager.update(lambda c: replace(c, imap=(c.imap[0], replace(c.imap[1], poll_seconds=900))))
        assert manager.fetchers["a"] is a and a.stopped == 0  # untouched
        assert b.stopped == 1 and manager.fetchers["b"] is not b and manager.fetchers["b"].running
        assert manager.fetchers["b"].config.poll_seconds == 900

        manager.update(lambda c: replace(c, imap=(replace(c.imap[0], enabled=False), c.imap[1])), passwords={"b": "neu"})
        assert a.stopped == 1 and set(manager.fetchers) == {"b"}
        assert manager.fetchers["b"].config.password == "neu"
        assert store.password("b") == "neu"
        assert services.imap is manager.fetchers["b"]
    finally:
        services.stop()
        services.index.close()


def test_update_restarts_the_running_intake_and_changes_its_sources(settings, tmp_path):
    services = Services(settings)
    try:
        manager = services.channels
        services.intake.start()
        thread = services.intake._thread
        nas = tmp_path / "nas"
        nas.mkdir()
        manager.update(lambda c: replace(c, watch=(WatchFolder("nas", nas),)))
        assert services.intake.running and services.intake._thread is not thread
        assert any(s.name == "watch-nas" and s.path == nas for s in services.settings.sources)
    finally:
        services.stop()
        services.index.close()


def test_failed_update_changes_nothing(settings):
    services = Services(settings)
    try:
        manager = services.channels
        before = manager.config

        def broken(config):
            return replace(config, imap=(mailbox("x"),))  # enabled without password

        with pytest.raises(ConfigError, match="kein Passwort"):
            manager.update(broken)
        assert manager.config is before and not manager.from_file
        assert not manager.store.exists()
    finally:
        services.stop()
        services.index.close()


def test_listeners_are_told_about_changes(settings):
    services = Services(settings)
    try:
        calls = []
        services.channels.listeners.append(lambda: calls.append(1))
        services.channels.update(lambda c: c)
        assert calls == [1]
    finally:
        services.stop()
        services.index.close()


# ---------------------------------------------------------------------------
# Owner context


def test_intake_puts_the_owner_of_a_scanner_folder_into_the_context(settings, samples):
    write_channels(
        settings.data_dir / "config",
        ChannelConfig(scanners=(ScannerChannel("smb", True, (Endpoint("erika", "erika.m@example.org"), Endpoint("aus", "max", False))),)),
    )
    services = Services(settings)
    try:
        smb = services.layout.source_dir("smb")
        assert (smb / "erika").is_dir() and (smb / "aus").is_dir()
        samples.text_pdf(smb / "erika" / "scan1.pdf", ["Rechnung"])
        samples.text_pdf(smb / "aus" / "scan2.pdf", ["Brief"])
        samples.text_pdf(smb / "lose.pdf", ["Lose"])
        services.intake.subdir_stable_seconds = 0
        created = services.intake.scan_once()
        items = {services.inbox.load(item_id).original_files[0].rsplit("/", 1)[-1]: services.inbox.load(item_id) for item_id in created}
        assert set(items) == {"scan1.pdf", "lose.pdf"}  # the disabled folder waits
        context = items["scan1.pdf"].context
        assert context["owner"] == "erika.m@example.org" and context["owner_label"] == "Erika M"
        assert context["endpoint"] == "Scanner"
        assert origin.origin_label(context) == "von Erika M · Scanner"
        assert "owner" not in items["lose.pdf"].context
        assert (smb / "aus" / "scan2.pdf").exists()

        # Switching the folder on lets the intake take the waiting file.
        services.channels.update(
            lambda c: c.with_scanner(replace(c.scanner("smb"), endpoints=tuple(replace(e, enabled=True) for e in c.scanner("smb").endpoints)))
        )
        created = services.intake.scan_once()
        assert len(created) == 1
        assert services.inbox.load(created[0]).context["owner"] == "max"
    finally:
        services.stop()
        services.index.close()


def test_origin_labels():
    assert origin.owner_label("erika") == "Erika"
    assert origin.owner_label("erika.mustermann@example.org") == "Erika Mustermann"
    assert origin.owner_label("Erika Mustermann") == "Erika Mustermann"
    assert origin.origin_label({"owner": "erika", "endpoint": "Scanner"}) == "von Erika · Scanner"
    assert origin.origin_label({"endpoint": "Scanner-Ordner „buero“"}) == "Scanner-Ordner „buero“"
    assert origin.origin_label({}) is None and origin.origin_label(None) is None
    context = origin.mark({"owner": "anna"}, owner="erika", endpoint="Scanner")
    assert context["owner"] == "anna" and context["endpoint"] == "Scanner"


# ---------------------------------------------------------------------------
# SMTP routing by address


def _scan_mail(tmp_path, to: str, n: int = 1) -> bytes:
    msg = EmailMessage()
    msg["From"] = "Scanner <scanner@printer.local>"
    msg["To"] = to
    msg["Subject"] = "Scan"
    msg["Date"] = format_datetime(datetime(2025, 3, 3, 9, n, tzinfo=timezone.utc))
    msg["Message-ID"] = f"<scan-{n}-{to}@printer.local>"
    msg.set_content("Scan")
    pdf = make_text_pdf(tmp_path / f"scan{n}.pdf", [f"Scan {n}"]).read_bytes()
    msg.add_attachment(pdf, maintype="application", subtype="pdf", filename=f"scan_{n}.pdf")
    return msg.as_bytes(policy=policy.SMTP)


def _batches(services):
    root = services.layout.source_dir("smtp")
    return [read_sendung(path) for path in sorted(root.iterdir())] if root.exists() else []


def test_single_household_routes_per_user_addresses(env, tmp_path):
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
    services = Services(settings)
    receiver: SmtpReceiver = services.smtp
    receiver.start()
    try:
        # Without channels.yaml only the configured recipient is accepted, as always.
        assert receiver.recipient_allowed("scan@docushelf.local")
        assert not receiver.recipient_allowed("scan-erika@docushelf.local")

        services.channels.update(
            lambda c: replace(
                c, smtp=SmtpChannel(True, (*c.smtp.recipients, Recipient("scan-erika@docushelf.local", "erika")))
            )
        )
        with smtplib.SMTP("127.0.0.1", receiver.port, timeout=10) as client:
            client.sendmail("scanner@printer.local", ["Scan-Erika@docushelf.local"], _scan_mail(tmp_path, "scan-erika@docushelf.local"))
            client.sendmail("scanner@printer.local", ["scan@docushelf.local"], _scan_mail(tmp_path, "scan@docushelf.local", 2))
        contexts = [batch.context for batch in _batches(services)]
        owners = sorted(str(c.get("owner")) for c in contexts)
        assert owners == ["None", "erika"]
        erika = next(c for c in contexts if c.get("owner"))
        assert erika["endpoint"] == "Scan per Mail"

        # Switched off: every address is refused.
        services.channels.update(lambda c: replace(c, smtp=replace(c.smtp, enabled=False)))
        assert not receiver.recipient_allowed("scan@docushelf.local")
        with smtplib.SMTP("127.0.0.1", receiver.port, timeout=10) as client:
            with pytest.raises(smtplib.SMTPRecipientsRefused):
                client.sendmail("scanner@printer.local", ["scan@docushelf.local"], _scan_mail(tmp_path, "x", 3))
    finally:
        receiver.stop()
        services.stop()
        services.index.close()


def test_household_mode_routes_by_channels_file(env, data_dir, tmp_path):
    path = data_dir / "config" / "households.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "households": [
                    {"id": "familie-a", "path": ".", "groups": ["a"], "smtp_recipients": ["scan-a@docushelf.local"]},
                    {"id": "eltern", "groups": ["e"], "smtp_recipients": ["scan-eltern@docushelf.local"]},
                ]
            }
        )
    )
    settings = load_settings(
        {
            **env,
            "DOCUSHELF_SMTP_ENABLED": "true",
            "DOCUSHELF_SMTP_RECIPIENT": "scan-alt@docushelf.local",
            "DOCUSHELF_SMTP_HOST": "127.0.0.1",
            "DOCUSHELF_SMTP_PORT": "0",
            "DOCUSHELF_SMTP_ALLOWED_NETWORKS": "127.0.0.1/32",
        }
    )
    registry = Registry(settings)
    receiver = receiver_for_registry(registry, settings.smtp)
    receiver.start()
    try:
        a, eltern = registry.services("familie-a"), registry.services("eltern")
        # Defaults: households.yaml plus the global recipient for the household in the data root.
        assert receiver.resolve("scan-a@docushelf.local") is a
        assert receiver.resolve("scan-alt@docushelf.local") is a
        assert receiver.resolve("scan-eltern@docushelf.local") is eltern

        eltern.channels.update(
            lambda c: replace(c, smtp=SmtpChannel(True, (Recipient("scan-opa@docushelf.local", "opa"),)))
        )
        # channels.yaml wins for that household: the old address is gone, the new one is routed.
        assert receiver.resolve("scan-eltern@docushelf.local") is None
        assert receiver.resolve("scan-opa@docushelf.local") is eltern
        assert receiver.resolve("scan-a@docushelf.local") is a

        with smtplib.SMTP("127.0.0.1", receiver.port, timeout=10) as client:
            client.sendmail("scanner@printer.local", ["scan-opa@docushelf.local"], _scan_mail(tmp_path, "scan-opa@docushelf.local"))
        [batch] = _batches(eltern)
        assert batch.context["owner"] == "opa" and _batches(a) == []

        # The household in the data root manages its addresses itself: the global one is gone then.
        a.channels.update(lambda c: replace(c, smtp=SmtpChannel(True, (Recipient("scan-a@docushelf.local"),))))
        assert receiver.resolve("scan-alt@docushelf.local") is None
        assert receiver.resolve("scan-a@docushelf.local") is a
    finally:
        receiver.stop()
        registry.stop()
        registry.close()


def test_registry_reprotects_watch_folders_after_a_change(env, data_dir, tmp_path):
    path = data_dir / "config" / "households.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"households": [{"id": "a", "path": ".", "groups": ["a"]}, {"id": "b", "groups": ["b"]}]}))
    settings = load_settings(env)
    registry = Registry(settings)
    registry.start()
    try:
        watch = tmp_path / "watch-b"
        watch.mkdir()
        registry.services("b").channels.update(lambda c: replace(c, watch=(WatchFolder("b", watch),)))
        assert watch in registry.services("a").intake.foreign_watch_folders
    finally:
        registry.stop()
        registry.close()


def test_channel_manager_works_on_a_container_without_file(base_services):
    from docushelf.households import default_household

    base_services.household = default_household(base_services.settings)
    manager = ChannelManager(base_services, base_services.settings, fetcher_factory=FakeFetcher)
    manager.build()
    assert manager.fetchers == {} and manager.effective_settings() is base_services.settings
