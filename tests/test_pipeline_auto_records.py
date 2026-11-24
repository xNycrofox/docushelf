"""Tests for the records of automatic filing: reading the audit log by action
and time (AuditLog.entries) and the link in push notifications."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from docushelf import notify as notify_module
from docushelf.audit import AuditLog
from docushelf.notify import Notifier

BERLIN = ZoneInfo("Europe/Berlin")


def write_month(directory: Path, month: str, lines: list[object]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    text = "".join((line if isinstance(line, str) else json.dumps(line, ensure_ascii=False)) + "\n" for line in lines)
    (directory / f"audit-{month}.jsonl").write_text(text, encoding="utf-8")


def entry(at: str, action: str, **details) -> dict:
    return {"at": at, "user": "automatisch", "action": action, **details}


@pytest.fixture
def audit(tmp_path) -> AuditLog:
    directory = tmp_path / "protokoll"
    write_month(directory, "2026-09", [
        entry("2026-09-20T10:00:00+02:00", "auto_file", doc_id="sep"),
        entry("2026-09-21T10:00:00+02:00", "file", doc_id="sep"),
    ])
    write_month(directory, "2026-10", [
        entry("2026-10-05T08:00:00+02:00", "auto_file", doc_id="oct"),
        entry("2026-10-31T23:59:59+01:00", "auto_review", doc_id="oct", status="ok"),
    ])
    write_month(directory, "2026-11", [
        entry("2026-11-01T00:00:01+01:00", "auto_file", doc_id="nov-1"),
        "{kaputt",
        "[1, 2, 3]",
        {"user": "x", "action": "auto_file", "doc_id": "no-time"},
        entry("2026-11-24T09:00:00+01:00", "file", doc_id="nov-24"),
        # Stamped before, written after the line above (two threads).
        entry("2026-11-24T08:59:59+01:00", "auto_file", doc_id="nov-24-early"),
    ])
    return AuditLog(directory)


def ids(entries: list[dict]) -> list[str]:
    return [e["doc_id"] for e in entries]


def test_entries_by_action_since_a_moment_newest_first(audit):
    since = datetime(2026, 10, 1, tzinfo=BERLIN)
    found = audit.entries({"auto_file", "auto_review"}, since)
    assert ids(found) == ["nov-24-early", "nov-1", "oct", "oct"]
    assert [e["action"] for e in found] == ["auto_file", "auto_file", "auto_review", "auto_file"]


def test_naive_since_is_local_time(audit):
    assert ids(audit.entries({"auto_review"}, datetime(2026, 10, 31, 23, 59, 59))) == ["oct"]
    assert audit.entries({"auto_review"}, datetime(2026, 11, 1)) == []


def test_without_filters_everything_with_a_time_comes_newest_first(audit):
    found = audit.entries()
    assert ids(found) == ["nov-24", "nov-24-early", "nov-1", "oct", "oct", "sep", "sep", "no-time"]
    assert ids(audit.entries([], None, limit=2)) == ["nov-24", "nov-24-early"]


def test_entries_without_time_are_left_out_when_a_moment_is_given(audit):
    assert "no-time" not in ids(audit.entries({"auto_file"}, datetime(2000, 1, 1)))


def test_only_month_files_that_can_hold_matches_are_read(audit, monkeypatch):
    read: list[str] = []
    real = AuditLog._read_lines

    def spy(path):
        read.append(path.name)
        return real(path)

    monkeypatch.setattr(AuditLog, "_read_lines", staticmethod(spy))
    audit.entries({"auto_file"}, datetime(2026, 11, 10, tzinfo=BERLIN))
    assert read == ["audit-2026-11.jsonl"]

    read.clear()
    # Shortly after a month began, the previous month is read too (time zones, late writes).
    found = audit.entries({"auto_file", "auto_review"}, datetime(2026, 11, 1, 0, 30, tzinfo=BERLIN))
    assert read == ["audit-2026-11.jsonl", "audit-2026-10.jsonl"]
    assert ids(found) == ["nov-24-early"]


def test_since_in_another_time_zone(audit):
    since = datetime(2026, 10, 31, 22, 59, 59, tzinfo=ZoneInfo("UTC"))  # 23:59:59 in Berlin
    assert ids(audit.entries({"auto_review"}, since)) == ["oct"]


def test_written_entries_are_found(tmp_path):
    log = AuditLog(tmp_path / "protokoll")
    started = datetime.now(BERLIN) - timedelta(seconds=2)
    log.write("automatisch", "auto_file", doc_id="a", review=False, reasons=["Regel „Strom“"])
    log.write("max", "auto_review", doc_id="a", status="ok")
    log.write("max", "file", doc_id="b")

    found = log.entries({"auto_file", "auto_review"}, started)

    assert [e["action"] for e in found] == ["auto_review", "auto_file"]
    assert found[1]["review"] is False and found[1]["reasons"] == ["Regel „Strom“"]
    assert found[0]["user"] == "max" and found[0]["status"] == "ok"


def test_missing_directory_means_no_entries(tmp_path):
    assert AuditLog(tmp_path / "nichts").entries({"auto_file"}, datetime.now(BERLIN)) == []


# -- notifications ---------------------------------------------------------------------------------


@pytest.fixture
def posted(monkeypatch) -> SimpleNamespace:
    """Captures what the notifier posts (it posts from a background thread)."""
    sent = SimpleNamespace(calls=[], done=threading.Event())

    def post(url, *, content, headers, timeout):
        sent.calls.append({"url": url, "content": content, "headers": headers})
        sent.done.set()

    monkeypatch.setattr(notify_module.httpx, "post", post)
    return sent


def test_notification_with_a_link_to_the_document(posted):
    Notifier("http://ntfy.invalid/topic").send(
        "Abgelegt: Stromrechnung Januar", "Wohnung/Strom · 82 %", tags="file_folder",
        click="http://docushelf.invalid/dokument/abc",
    )
    assert posted.done.wait(5)
    (call,) = posted.calls
    assert call["url"] == "http://ntfy.invalid/topic"
    assert call["content"] == "Wohnung/Strom · 82 %".encode()
    assert call["headers"]["Tags"] == "file_folder"
    assert call["headers"]["Click"] == "http://docushelf.invalid/dokument/abc"
    assert call["headers"]["Title"] == "Abgelegt: Stromrechnung Januar"


def test_notification_without_or_with_unusable_link(posted):
    Notifier("http://ntfy.invalid/topic").send("Prüfen", "Text", click="http://dokument/ä")
    assert posted.done.wait(5)
    (call,) = posted.calls
    assert "Click" not in call["headers"] and call["headers"]["Tags"] == "warning"
    assert call["headers"]["Title"].startswith("=?utf-8?")


def test_no_notification_without_url(posted):
    Notifier(None).send("Abgelegt: x", "y", click="http://docushelf.invalid/")
    assert not posted.done.wait(0.2)
    assert posted.calls == []
