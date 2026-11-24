"""Tests for the shared foundation: names, file operations, text normalization,
XMP metadata, the SQLite index, inbox items, batches and configuration."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pikepdf
import pytest

from docushelf.channels.sendung import MANIFEST, read_sendung, write_sendung
from docushelf.config import AutoFileConfig, LlmConfig, load_settings
from docushelf.errors import ConfigError, Conflict, NotFound, PathError
from docushelf.inbox.items import InboxStore
from docushelf.index.db import Index
from docushelf.models import DocMeta, DocumentRecord
from docushelf.pdf.text import extract_pages
from docushelf.pdf.xmp import build_packet, read_meta, write_meta
from docushelf.util import fsops
from docushelf.util.names import (
    check_relative,
    format_filename,
    is_ignored,
    parse_filename,
    sanitize_component,
    sanitize_folder,
    title_from_original_name,
    unique_filename,
)
from docushelf.util.textnorm import bounded_levenshtein, normalize, normalize_with_map

# -- names --------------------------------------------------------------------


def test_format_and_parse_filename_roundtrip():
    name = format_filename("Stromrechnung: Januar 01/2026", date(2026, 1, 5))
    assert name == "2026-01-05 Stromrechnung - Januar 01-2026.pdf"
    parsed = parse_filename(name)
    assert parsed.doc_date == date(2026, 1, 5)
    assert parsed.title == "Stromrechnung - Januar 01-2026"
    assert parsed.ext == ".pdf"


def test_parse_filename_without_schema_and_collision():
    assert parse_filename("Scan 0001.pdf").doc_date is None
    assert parse_filename("Scan 0001.pdf").title == "Scan 0001"
    parsed = parse_filename("2025-12-31 Kontoauszug (2).pdf")
    assert parsed.title == "Kontoauszug" and parsed.collision == 2
    assert parse_filename("2025-13-45 Kaputt.pdf").doc_date is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  Rechnung  ", "Rechnung"),
        ("a/b\\c", "a-b-c"),
        ('Wer? "Ich" <da>', "Wer Ich da"),
        ("CON", "CON_"),
        ("...versteckt", "versteckt"),
    ],
)
def test_sanitize_component(raw, expected):
    assert sanitize_component(raw) == expected


def test_sanitize_component_rejects_empty():
    with pytest.raises(PathError):
        sanitize_component(" ?? ")


def test_sanitize_folder_and_check_relative():
    assert sanitize_folder(" Finanzen / Bank/ ") == "Finanzen/Bank"
    assert sanitize_folder("") == ""
    with pytest.raises(PathError):
        sanitize_folder("Finanzen/../etc")
    with pytest.raises(PathError):
        check_relative("../x")
    with pytest.raises(PathError):
        check_relative("a/.hidden/b")
    assert check_relative("a/b.pdf") == "a/b.pdf"


def test_unique_filename_is_case_insensitive(tmp_path):
    (tmp_path / "Rechnung.pdf").write_bytes(b"x")
    assert unique_filename(tmp_path, "rechnung.pdf") == "rechnung (2).pdf"
    (tmp_path / "rechnung (2).pdf").write_bytes(b"x")
    assert unique_filename(tmp_path, "Rechnung.pdf") == "Rechnung (3).pdf"


def test_is_ignored():
    for name in [".DS_Store", "._foo.pdf", "~$brief.docx", "Thumbs.db", "x.pdf.part", ".docushelf-abc.part"]:
        assert is_ignored(name)
    assert not is_ignored("2026-01-01 Rechnung.pdf")


def test_title_from_original_name():
    assert title_from_original_name("scan_0001.pdf") is None
    assert title_from_original_name("IMG_2041.JPG") is None
    assert title_from_original_name("Rechnung_Telekom_Mai.pdf") == "Rechnung Telekom Mai"


# -- file operations --------------------------------------------------------------


def test_publish_no_clobber_never_overwrites(tmp_path):
    src = tmp_path / "src.pdf"
    src.write_bytes(b"new")
    target_dir = tmp_path / "archiv" / "A"
    target_dir.mkdir(parents=True)
    (target_dir / "Doc.pdf").write_bytes(b"old")
    final = fsops.publish_no_clobber(src, target_dir, "Doc.pdf", move=True)
    assert final.name == "Doc (2).pdf"
    assert (target_dir / "Doc.pdf").read_bytes() == b"old"
    assert final.read_bytes() == b"new"
    assert not src.exists()


def test_replace_if_unchanged_detects_concurrent_change(tmp_path):
    original = tmp_path / "a.pdf"
    original.write_bytes(b"one")
    signature = fsops.file_signature(original)
    replacement = tmp_path / ".docushelf-x.part"
    replacement.write_bytes(b"two")
    os.utime(original, ns=(1, 1))  # simulate a change by someone else
    with pytest.raises(Conflict):
        fsops.replace_if_unchanged(original, replacement, signature)
    assert original.read_bytes() == b"one"


def test_atomic_write_json_and_sha(tmp_path):
    path = tmp_path / "x" / "state.json"
    fsops.atomic_write_json(path, {"ä": 1})
    assert fsops.read_json(path) == {"ä": 1}
    assert len(fsops.sha256_file(path)) == 64
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".docushelf-")]


# -- text normalization -------------------------------------------------------------


def test_normalize_umlauts_and_hyphenation():
    assert normalize("Müller MUELLER Straße STRASSE") == "mueller mueller strasse strasse"
    assert normalize("Neben-\nkostenabrechnung") == "nebenkostenabrechnung"
    assert normalize("Café, 12,50 €") == "cafe 12 50"


def test_normalize_with_map_matches_normalize():
    samples = ["Müller-\nLüdenscheidt GmbH", "Grüße aus der Straße ½", "Neben-\nkosten  2025!", "ÄÖÜ äöü ß ẞ"]
    for text in samples:
        norm, offsets = normalize_with_map(text)
        assert norm == normalize(text)
        assert len(norm) == len(offsets)


def test_bounded_levenshtein():
    assert bounded_levenshtein("rechnung", "rechnnug", 2) == 2
    assert bounded_levenshtein("rechnung", "abrechnung", 1) == 2  # exceeds limit
    assert bounded_levenshtein("haftpflicht", "haftpflicht", 1) == 0


# -- XMP -----------------------------------------------------------------------------------


def test_xmp_roundtrip_and_docinfo(tmp_path, samples):
    src = samples.text_pdf(tmp_path / "a.pdf", ["Hallo"])
    dst = tmp_path / "b.pdf"
    meta = DocMeta(
        doc_id="1b4e28ba-2fa1-11d2-883f-0016d3cca427",
        title="Rechnung Strom",
        doc_date=date(2026, 1, 5),
        tags=["Strom", "steuerrelevant 2026"],
        sender="Stadtwerke",
        origin_hash="ab" * 32,
        extra={"ArchiveSerialNumber": "7"},
    )
    write_meta(src, dst, meta)
    back = read_meta(dst)
    assert back.doc_id == meta.doc_id and back.title == meta.title and back.doc_date == meta.doc_date
    assert back.tags == meta.tags and back.sender == "Stadtwerke" and back.extra == {"ArchiveSerialNumber": "7"}
    with pikepdf.open(dst) as pdf:
        assert str(pdf.docinfo["/Title"]) == "Rechnung Strom"
        assert "/Filter" not in pdf.Root.Metadata
    assert extract_pages(dst) == ["Hallo"]


def test_xmp_pdfa_extension_schema_added_once():
    existing = (
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description rdf:about="" xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/" pdfaid:part="2" '
        b'pdfaid:conformance="B"/></rdf:RDF></x:xmpmeta>'
    )
    once = build_packet(existing, DocMeta(doc_id="x", title="T"))
    twice = build_packet(once, DocMeta(doc_id="x", title="T2"))
    text = twice.decode()
    assert text.count("<pdfaSchema:namespaceURI>") == 1
    assert "T2" in text and ">T<" not in text


# -- index ---------------------------------------------------------------------------------------


def _record(doc_id: str, path: str, title: str = "Titel") -> DocumentRecord:
    folder = path.rsplit("/", 1)[0] if "/" in path else ""
    return DocumentRecord(
        id=doc_id, path=path, folder=folder, filename=path.rsplit("/", 1)[-1], ext=".pdf", title=title,
        doc_date=date(2026, 1, 1), sender="Sparkasse", tags=["Bank"], origin_hash="o-" + doc_id,
        content_hash="c-" + doc_id, xmp_id=None, has_xmp=False, size=1, mtime_ns=1, inode=None,
        page_count=1, text_status="ok", indexed_at="",
    )


def test_index_fts_follows_upserts_and_deletes(tmp_path):
    index = Index(tmp_path / "i.sqlite3")
    index.init_schema()
    index.upsert(_record("a", "Bank/2026-01-01 Auszug.pdf", "Auszug"), ["Kontoauszug Nebenkosten Müller"])
    conn = index.conn()
    hit = lambda q: conn.execute("SELECT rowid FROM fts_pages WHERE fts_pages MATCH ?", (q,)).fetchall()  # noqa: E731
    assert hit('"neben"') and hit('"mueller"')
    index.upsert(_record("a", "Bank/2026-01-01 Auszug.pdf", "Auszug"), ["anderer Text"])
    assert not hit('"neben"')
    index.delete("a")
    assert not hit('"anderer"')
    conn.execute("INSERT INTO fts_pages(fts_pages) VALUES('integrity-check')")
    conn.execute("INSERT INTO fts_docs(fts_docs) VALUES('integrity-check')")
    index.close()


def test_index_jobs_dedupe_and_retry(tmp_path):
    index = Index(tmp_path / "i.sqlite3")
    index.init_schema()
    assert index.enqueue("process", "item-1")
    assert not index.enqueue("process", "item-1")
    job = index.claim(["process"], "w")
    assert job and job.attempts == 1
    index.fail(job.id, "boom", retry_in=0)
    again = index.claim(["process"], "w")
    assert again and again.attempts == 2
    index.complete(again.id)
    assert index.claim(["process"], "w") is None
    index.close()


# -- inbox items and batches -------------------------------------------------------------------------


def test_inbox_item_lifecycle(layout, tmp_path):
    store = InboxStore(layout)
    drop = layout.source_dir("upload")
    drop.mkdir(parents=True, exist_ok=True)
    file = drop / "Scan 1.pdf"
    file.write_bytes(b"%PDF-1.4 test")
    item = store.create("upload", [file], context={"uploader": "max"})
    assert not file.exists()
    assert item.state == "arbeit" and item.original_paths()[0].read_bytes() == b"%PDF-1.4 test"
    moved = store.move(item, "bereit")
    assert store.load(item.id).state == "bereit" and moved.dir.parent == layout.ready
    with pytest.raises(NotFound):
        store.load("../../etc")


def test_inbox_recovers_directory_without_item_json(layout):
    store = InboxStore(layout)
    orphan = layout.work / "20260101-120000-abcdef" / "original"
    orphan.mkdir(parents=True)
    (orphan / "a.pdf").write_bytes(b"%PDF")
    recovered = store.recover_orphans()
    assert [i.id for i in recovered] == ["20260101-120000-abcdef"]
    assert store.load("20260101-120000-abcdef").origin_hash


def test_sendung_roundtrip(tmp_path):
    batch = write_sendung(
        tmp_path,
        [[("rechnung.pdf", b"%PDF")], [("a.jpg", b"jpg1"), ("a.jpg", b"jpg2")]],
        context={"mail_subject": "Rechnung"},
    )
    assert (batch / MANIFEST).exists()
    sendung = read_sendung(batch)
    assert sendung is not None
    assert [d.files for d in sendung.documents] == [["rechnung.pdf"], ["a.jpg", "a (2).jpg"]]
    assert sendung.context["mail_subject"] == "Rechnung"


# -- configuration ----------------------------------------------------------------------------------


def test_settings_from_env_and_file_secret(tmp_path):
    secret_file = tmp_path / "secret"
    secret_file.write_text("from-file\n")
    settings = load_settings(
        {
            "DOCUSHELF_DATA_DIR": str(tmp_path / "data"),
            "DOCUSHELF_OIDC_CLIENT_SECRET_FILE": str(secret_file),
            "DOCUSHELF_ALLOWED_USERS": "Max, erika",
            "DOCUSHELF_WATCH_FOLDERS": "nas=/mnt/nas",
        }
    )
    assert settings.oidc_client_secret == "from-file"
    assert settings.allowed_users == ("max", "erika")
    assert any(s.name == "watch-nas" and s.path == Path("/mnt/nas") for s in settings.sources)
    assert settings.secret_key  # generated and persisted
    assert (tmp_path / "data" / "config" / "secret.key").exists()


# -- follow-ups from integration --------------------------------------------------------------------


def test_folder_prefix_matching_is_case_sensitive(tmp_path):
    from docushelf.index.db import Index

    index = Index(tmp_path / "i.sqlite3")
    index.init_schema()
    index.add_folder_with_parents("Bank/Konto")
    index.add_folder_with_parents("bank/Depot")
    index.remove_folder_tree("Bank")
    assert index.list_folders() == ["bank", "bank/Depot"]

    index.add_folder_with_parents("Bank/Konto")
    index.rename_folder_tree("Bank", "Bankkonten")
    assert sorted(index.list_folders()) == ["Bankkonten", "Bankkonten/Konto", "bank", "bank/Depot"]


def test_folder_prefix_matching_handles_like_wildcards(tmp_path):
    from docushelf.index.db import Index

    index = Index(tmp_path / "i.sqlite3")
    index.init_schema()
    index.add_folder_with_parents("A_B/x")
    index.add_folder_with_parents("AxB/y")
    index.add_folder_with_parents("100%/z")
    index.remove_folder_tree("A_B")
    index.remove_folder_tree("100%")
    assert index.list_folders() == ["AxB", "AxB/y"]


def test_scan_generation_never_goes_back(tmp_path):
    from docushelf.index.db import Index

    index = Index(tmp_path / "i.sqlite3")
    index.init_schema()
    index.add_folder_with_parents("A/B", scan_gen=5)
    index.add_folder_with_parents("A/B/C")  # default generation 0 must not reset A and A/B
    assert index.delete_unseen_folders(5) == ["A/B/C"]
    assert sorted(index.list_folders()) == ["A", "A/B"]


def test_release_returns_a_claimed_job_without_counting_the_attempt(tmp_path):
    from docushelf.index.db import Index

    index = Index(tmp_path / "i.sqlite3")
    index.init_schema()
    assert index.next_run_after(["process"]) is None
    index.enqueue("process", "item-1")
    assert index.next_run_after(["process"]) is not None
    assert index.next_run_after(["refresh"]) is None
    job = index.claim(["process"], "w1")
    assert job.attempts == 1
    index.release(job.id)
    again = index.claim(["process"], "w1")
    assert again.id == job.id and again.attempts == 1


def test_inbox_save_never_recreates_a_vanished_item(layout, tmp_path):
    from docushelf.errors import NotFound
    from docushelf.inbox.items import InboxStore
    from docushelf.util import fsops

    store = InboxStore(layout)
    source = tmp_path / "a.pdf"
    source.write_bytes(b"%PDF-1.4 test")
    item = store.create("upload", [source], move=False)
    fsops.remove_tree(item.dir)

    with pytest.raises(NotFound):
        store.save(item)
    with pytest.raises(NotFound):
        store.write_text(item, ["text"])
    store.log(item, "late log line")
    assert not item.dir.exists()


def test_inbox_create_cleans_up_after_a_failed_copy(layout, tmp_path):
    from docushelf.inbox.items import InboxStore

    store = InboxStore(layout)
    good = tmp_path / "a.pdf"
    good.write_bytes(b"%PDF-1.4 a")
    missing = tmp_path / "missing.pdf"
    with pytest.raises(OSError):
        store.create("upload", [good, missing], move=False, origin_hash="x" * 64)
    assert list(layout.work.iterdir()) == []
    assert good.exists()


@pytest.mark.parametrize("content", ["[1, 2]", '"text"', '{"documents": {"a": 1}}', '{"documents": [1, {"files": "x"}]}'])
def test_read_sendung_tolerates_odd_manifests(tmp_path, content):
    from docushelf.channels.sendung import MANIFEST, read_sendung

    (tmp_path / MANIFEST).write_text(content, encoding="utf-8")
    result = read_sendung(tmp_path)
    assert result is None or result.documents == []


# -- automatic filing and language model settings ---------------------------------------------------


def settings_with(tmp_path, **env):
    values = {"DOCUSHELF_DATA_DIR": str(tmp_path / "data")}
    values.update({f"DOCUSHELF_{key.upper()}": value for key, value in env.items()})
    return load_settings(values)


def test_auto_file_is_off_by_default(tmp_path):
    config = settings_with(tmp_path).auto_file
    assert config == AutoFileConfig()
    assert config.mode == "off" and not config.enabled
    assert config.threshold == 0.75
    assert config.sources == ("upload", "smb", "ftp", "mail", "smtp", "watch-*")
    assert config.new_folders is True and config.wait_for_llm is True and config.notify is True


@pytest.mark.parametrize(
    "value,mode",
    [
        ("off", "off"), ("rules", "rules"), ("confident", "confident"), ("always", "always"),
        ("aus", "off"), ("regeln", "rules"), ("sicher", "confident"), ("immer", "always"),
        ("RULES", "rules"), (" Sicher ", "confident"), ("Immer", "always"), ("false", "off"),
    ],
)
def test_auto_file_modes_and_german_names(tmp_path, value, mode):
    config = settings_with(tmp_path, auto_file=value).auto_file
    assert config.mode == mode and config.enabled is (mode != "off")


def test_auto_file_settings_from_the_environment(tmp_path):
    config = settings_with(
        tmp_path,
        auto_file="sicher",
        auto_file_threshold="0,8",
        auto_file_sources="upload, Watch-*,smb",
        auto_file_new_folders="nein",
        auto_file_wait_for_llm="false",
        auto_file_notify="0",
    ).auto_file
    assert config == AutoFileConfig(mode="confident", threshold=0.8, sources=("upload", "watch-*", "smb"),
                                    new_folders=False, wait_for_llm=False, notify=False)
    assert settings_with(tmp_path, auto_file_threshold="90 %").auto_file.threshold == 0.9
    assert settings_with(tmp_path, auto_file_threshold="1").auto_file.threshold == 1.0


@pytest.mark.parametrize(
    "key,value,fragment",
    [
        ("auto_file", "manchmal", "DOCUSHELF_AUTO_FILE: „manchmal“ ist ungültig"),
        ("auto_file", "true", "aus, regeln, sicher, immer"),
        ("auto_file_threshold", "1.5", "zwischen 0 und 1"),
        ("auto_file_threshold", "75", "zwischen 0 und 1"),
        ("auto_file_threshold", "viel", "muss eine Zahl"),
        ("auto_file_sources", "wa*tch", "nur am Ende"),
        ("auto_file_new_folders", "vielleicht", "DOCUSHELF_AUTO_FILE_NEW_FOLDERS muss true oder false sein"),
        ("auto_file_wait_for_llm", "jein", "true oder false"),
        ("auto_file_notify", "bitte", "true oder false"),
    ],
)
def test_invalid_auto_file_settings_are_rejected_in_german(tmp_path, key, value, fragment):
    with pytest.raises(ConfigError) as excinfo:
        settings_with(tmp_path, **{key: value})
    assert fragment in str(excinfo.value)


def test_auto_file_in_settings_yaml(tmp_path):
    config_dir = tmp_path / "data" / "config"
    config_dir.mkdir(parents=True)
    # YAML reads a bare "off" as false.
    (config_dir / "settings.yaml").write_text(
        "auto_file: off\nauto_file_threshold: 0.9\nauto_file_sources: [upload, smb]\nauto_file_notify: false\n",
        encoding="utf-8",
    )
    config = settings_with(tmp_path).auto_file
    assert config.mode == "off" and config.threshold == 0.9 and config.sources == ("upload", "smb")
    assert config.notify is False
    (config_dir / "settings.yaml").write_text("auto_file: immer\n", encoding="utf-8")
    assert settings_with(tmp_path).auto_file.mode == "always"
    # The environment wins.
    assert settings_with(tmp_path, auto_file="regeln").auto_file.mode == "rules"


def test_auto_file_config_normalizes_values_from_code():
    config = AutoFileConfig(mode="Immer", threshold="80 %", sources=["Upload", "watch-*", "upload"])
    assert config.mode == "always" and config.threshold == 0.8 and config.sources == ("upload", "watch-*")
    assert AutoFileConfig(sources="smb").sources == ("smb",)
    with pytest.raises(ConfigError):
        AutoFileConfig(mode="bald")
    with pytest.raises(ConfigError):
        AutoFileConfig(threshold=2)


def test_language_model_defaults(tmp_path):
    assert settings_with(tmp_path).llm is None
    # Other model settings are not looked at while the model is off.
    assert settings_with(tmp_path, llm_provider="quatsch", llm_context="1").llm is None
    llm = settings_with(tmp_path, llm_enabled="true").llm
    assert llm == LlmConfig(url="http://ollama:11434", model="qwen2.5:7b")
    assert llm.provider == "ollama" and llm.context_tokens == 8192 and llm.api_key is None
    assert llm.timeout == 180 and llm.max_chars == 6000


def test_openai_compatible_model_with_key_file(tmp_path):
    key_file = tmp_path / "llm_key"
    key_file.write_text("sk-geheim\n", encoding="utf-8")
    llm = settings_with(
        tmp_path,
        llm_enabled="true",
        llm_provider="OpenAI",
        llm_url="http://llama:8080/v1/",
        llm_model="qwen2.5-7b-instruct",
        llm_api_key_file=str(key_file),
        llm_context="16384",
        llm_timeout="60",
    ).llm
    assert llm.provider == "openai" and llm.url == "http://llama:8080/v1"
    assert llm.model == "qwen2.5-7b-instruct" and llm.api_key == "sk-geheim"
    assert llm.context_tokens == 16384 and llm.timeout == 60
    assert "sk-geheim" not in repr(llm)
    assert settings_with(tmp_path, llm_enabled="true", llm_api_key="direkt").llm.api_key == "direkt"


@pytest.mark.parametrize(
    "env,fragment",
    [
        ({"llm_provider": "gemini"}, "DOCUSHELF_LLM_PROVIDER: „gemini“ ist ungültig"),
        ({"llm_provider": "openai"}, "DOCUSHELF_LLM_URL fehlt"),
        ({"llm_url": "ollama:11434"}, "http:// oder https://"),
        ({"llm_context": "512"}, "DOCUSHELF_LLM_CONTEXT muss mindestens 1024 sein"),
        ({"llm_context": "viel"}, "DOCUSHELF_LLM_CONTEXT muss eine Zahl sein"),
        ({"llm_timeout": "0"}, "DOCUSHELF_LLM_TIMEOUT"),
        ({"llm_max_chars": "100"}, "DOCUSHELF_LLM_MAX_CHARS"),
    ],
)
def test_invalid_language_model_settings(tmp_path, env, fragment):
    with pytest.raises(ConfigError) as excinfo:
        settings_with(tmp_path, llm_enabled="true", **env)
    assert fragment in str(excinfo.value)


def test_llm_config_from_code_is_normalized():
    assert LlmConfig(url="http://x", model="m", provider="OpenAI").provider == "openai"
    assert LlmConfig(url="http://x", model="m", api_key="  ").api_key is None
    with pytest.raises(ConfigError):
        LlmConfig(url="http://x", model="m", provider="gpt")
