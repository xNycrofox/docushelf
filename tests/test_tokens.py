"""Tests for the API token store (config/tokens.yaml)."""

from __future__ import annotations

import os
import stat
import threading

import pytest
import yaml

from docushelf.errors import ConfigError, NotFound
from docushelf.web.tokens import PREFIX_LENGTH, TokenStore, hash_token


@pytest.fixture
def store(tmp_path):
    return TokenStore(tmp_path / "config" / "tokens.yaml")


def test_create_stores_only_the_hash(store):
    token, entry = store.create("iPhone Kurzbefehl", "anna")
    assert token.startswith("dsk_") and len(token) > 40
    assert entry["name"] == "iPhone Kurzbefehl"
    assert entry["user"] == "anna"
    assert entry["prefix"] == token[:PREFIX_LENGTH]
    assert "hash" not in entry
    text = store.path.read_text(encoding="utf-8")
    assert token not in text
    assert hash_token(token) in text
    data = yaml.safe_load(text)
    assert data[0]["id"] == entry["id"] and data[0]["hash"] == hash_token(token)
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600


def test_verify_accepts_only_the_exact_token(store):
    token, entry = store.create("Skript", "max")
    assert store.verify(token) == entry
    assert store.verify(token + "x") is None
    assert store.verify(token[:-1]) is None
    assert store.verify("") is None
    assert store.verify(None) is None  # type: ignore[arg-type]
    assert store.verify("x" * 10_000) is None


def test_list_and_revoke(store):
    first, one = store.create("Eins", "anna")
    second, two = store.create("Zwei", "max")
    listed = store.list()
    assert [e["id"] for e in listed] == [one["id"], two["id"]]
    assert all("hash" not in e for e in listed)
    assert store.get(two["id"])["name"] == "Zwei"

    revoked = store.revoke(one["id"])
    assert revoked["name"] == "Eins"
    assert store.verify(first) is None
    assert store.verify(second) == two
    with pytest.raises(NotFound):
        store.revoke(one["id"])
    with pytest.raises(NotFound):
        store.get("does-not-exist")


def test_a_second_store_reads_the_same_file(store):
    token, entry = store.create("Scanner-Skript", "anna")
    other = TokenStore(store.path)
    assert other.verify(token) == entry
    other.revoke(entry["id"])
    # The first instance notices the change through the file signature.
    assert store.verify(token) is None


def test_removing_an_entry_by_hand_revokes_the_token(store):
    token, entry = store.create("Alt", "anna")
    keep, kept = store.create("Neu", "anna")
    data = yaml.safe_load(store.path.read_text(encoding="utf-8"))
    data = [item for item in data if item["id"] != entry["id"]]
    store.path.write_text(yaml.safe_dump(data), encoding="utf-8")
    os.utime(store.path, ns=(1, 1))  # make sure the signature changes even on coarse clocks
    assert store.verify(token) is None
    assert store.verify(keep) == kept


def test_invalid_file_fails_closed_and_is_not_overwritten(store):
    token, _entry = store.create("Skript", "anna")
    broken = "- id: abc\n  hash: [kaputt\n"
    store.path.write_text(broken, encoding="utf-8")
    assert store.verify(token) is None
    assert store.list() == []
    assert store.error and "tokens.yaml" in store.error
    with pytest.raises(ConfigError):
        store.create("Neu", "anna")
    with pytest.raises(ConfigError):
        store.revoke("abc")
    assert store.path.read_text(encoding="utf-8") == broken


@pytest.mark.parametrize(
    "content",
    [
        "- id: a\n  user: anna\n  hash: zz\n",  # not a sha256
        "- id: a\n  hash: " + "a" * 64 + "\n",  # no user
        "- user: anna\n  hash: " + "a" * 64 + "\n",  # no id
        "tokens: 5\n",
        "- id: a\n  user: x\n  hash: " + "a" * 64 + "\n- id: a\n  user: y\n  hash: " + "b" * 64 + "\n",
    ],
)
def test_invalid_entries_are_reported(store, content):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(content, encoding="utf-8")
    assert store.list() == []
    assert store.error


def test_hand_written_entry_with_timestamp_works(store):
    token = "dsk_handwritten-token-value-0123456789"
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        f"tokens:\n  - id: manual\n    name: Hand\n    user: max\n    hash: {hash_token(token)}\n"
        "    created_at: 2026-11-23T10:00:00+01:00\n",
        encoding="utf-8",
    )
    entry = store.verify(token)
    assert entry is not None and entry["user"] == "max" and entry["created_at"].startswith("2026-11-23")
    assert store.error is None


def test_names_are_validated(store):
    with pytest.raises(ValueError):
        store.create("   ", "anna")
    with pytest.raises(ValueError):
        store.create("x" * 101, "anna")
    with pytest.raises(ValueError):
        store.create("Name", "")
    _token, entry = store.create("  Kurz\tbefehl\n ", "anna")
    assert entry["name"] == "Kurz befehl"
    assert not store.path.with_name("tokens.yaml.tmp").exists()


def test_missing_file_means_no_tokens(store):
    assert store.list() == []
    assert store.verify("dsk_anything") is None
    assert store.error is None


def test_concurrent_creates_keep_every_token(store):
    tokens: list[str] = []
    lock = threading.Lock()

    def worker(number: int) -> None:
        token, _entry = store.create(f"Token {number}", "anna")
        with lock:
            tokens.append(token)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    fresh = TokenStore(store.path)
    assert len(fresh.list()) == 12
    assert all(fresh.verify(token) for token in tokens)
    assert not [p for p in store.path.parent.iterdir() if p.name.startswith(".docushelf-")]
