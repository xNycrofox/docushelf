"""Web interface of automatic filing: the tab "Automatisch abgelegt", the info bar
and badges in the inbox, the notice on the document page, the settings sections
"Automatische Ablage", "Sprachmodell (KI)" and "Handy & Tablet", the API
(``/api/v1/auto``) and the small fixes on the rules page and for encrypted PDFs.

Automatic filings are read from the real audit log; documents are real files
indexed by the stand-in reconciler. The last tests run on the real service
container, filing through the real archive operations.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from archive_support import add_archive_pdf, attach_archive, make_encrypted_pdf, make_ready_item
from docushelf.config import AutoFileConfig, LlmConfig, load_settings
from docushelf.models import DocMeta, FilingRequest, ScanStats
from docushelf.util.ids import new_doc_id
from docushelf.util.timeutil import now
from docushelf.web import routes_settings, routes_ui, security, templating, views
from test_ui import (
    FakeIntake,
    FakePipeline,
    FakeWatcher,
    FakeWorker,
    OcrStub,
    assert_clean_page,
    build_app,
    csrf_of,
    flash_texts,
    parse,
)
from web_support import client_for, login_local, make_app, session_data

AUTO = AutoFileConfig(mode="confident", threshold=0.75)
ADMIN = security.User(username="admin", name="Admin", email=None, groups=(), method="local")


# ---------------------------------------------------------------------------
# Helpers


def text_of(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def write_audit(services, action: str, *, at: datetime, user: str = "automatisch", **details) -> None:
    """An audit entry with a chosen time (the log appends in time order in real life)."""
    entry = {"at": at.isoformat(timespec="seconds"), "user": user, "action": action, **details}
    path = services.layout.audit / f"audit-{at:%Y-%m}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def auto_filed(services, samples, rel: str, *, ago: timedelta = timedelta(hours=1), confidence: float = 0.9,
               review: bool = False, sources=("similarity",), reasons=("ähnlich zu „Vorjahr“ (88 %)",), keep: bool = True):
    """A document filed automatically ``ago`` ago (``keep=False``: it is gone from the archive since)."""
    doc_id = new_doc_id()
    folder, _, name = rel.rpartition("/")
    title = name[11:-4] if name[:4].isdigit() else name[:-4]
    if keep:
        record = add_archive_pdf(services, samples, rel, text=title, meta=DocMeta(doc_id=doc_id, title=title))
        assert record.id == doc_id
    write_audit(services, "auto_file", at=now() - ago, doc_id=doc_id, path=rel, title=title, folder=folder,
                confidence=confidence, review=review, sources=list(sources), reasons=list(reasons), item="20261101-120000-abcdef")
    return SimpleNamespace(doc_id=doc_id, rel=rel, title=title, folder=folder)


@pytest.fixture
def svc(base_services, monkeypatch):
    from docushelf.index.search import Searcher
    from docushelf.suggest.rules import RuleStore
    from docushelf.web.tokens import TokenStore

    services = attach_archive(base_services)
    services.settings = services.settings.with_overrides(auto_file=AUTO)
    services.rules = RuleStore(services.layout.rules_file)
    services.tokens = TokenStore(services.layout.tokens_file)
    services.searcher = Searcher(services)
    services.pipeline = FakePipeline(services)
    services.worker = FakeWorker()
    services.watcher = FakeWatcher()
    services.intake = FakeIntake()
    services.imap = None
    services.smtp = None
    services.rebuild = lambda *, user=None: ScanStats()
    monkeypatch.setattr(routes_ui, "_ocr_info", lambda settings: OcrStub(languages=["deu"], missing_languages=[]))
    return services


@pytest.fixture
def client(svc):
    with TestClient(build_app(svc), base_url="http://testserver") as test_client:
        yield test_client


@pytest.fixture
def admin_client(svc):
    with TestClient(build_app(svc, ADMIN), base_url="http://testserver") as test_client:
        yield test_client


def page(client, url: str, status: int = 200) -> str:
    response = client.get(url)
    assert response.status_code == status, (url, response.status_code, response.text[:600])
    assert_clean_page(response.text, csrf=csrf_of(response.text))
    return response.text


def post(client, url: str, data=None, status: int = 303):
    payload = {"csrf_token": "test", **(data or {})}
    response = client.post(url, data=payload, follow_redirects=False)
    assert response.status_code == status, (url, response.status_code, response.text[:600])
    return response


# ---------------------------------------------------------------------------
# View helpers


def test_auto_settings_follow_the_configuration():
    assert views.auto_settings(SimpleNamespace()) == views.AutoSettings()  # no configuration: off
    assert not views.auto_settings(SimpleNamespace(auto_file=AutoFileConfig())).enabled
    config = views.auto_settings(SimpleNamespace(auto_file=AutoFileConfig(mode="Sicher", threshold=0.8)))
    assert config.mode == "confident" and config.enabled and config.threshold_pct == 80
    assert config.summary == "an (sicher ab 80 %)"
    assert config.source_labels == ["Upload", "Scanner (SMB)", "Scanner (FTP)", "E-Mail", "Scan per Mail", "alle Watch-Ordner"]
    assert views.auto_settings(SimpleNamespace(auto_file=AutoFileConfig(mode="regeln"))).summary == "nur für Regeln mit „automatisch“"
    always = views.auto_settings(SimpleNamespace(auto_file=AutoFileConfig(mode="immer")))
    assert always.summary == "an für alle Dokumente (unter 75 % zum Prüfen markiert)"
    # Odd values from a hand-made object are tolerated.
    odd = views.auto_settings(SimpleNamespace(auto_file=SimpleNamespace(mode="KAPUTT", threshold="x", sources="upload, watch-scan*")))
    assert odd.mode == "off" and odd.threshold == 0.75 and odd.source_labels == ["Upload", "Watch-Ordner „scan…“"]


def test_signal_labels():
    assert views.signal_labels(["similarity", "llm"]) == ["Ähnlichkeit", "KI"]
    assert views.signal_labels(["rule", "import", "heuristic"]) == ["Regel", "Paperless", "Heuristik"]
    assert views.signal_labels([]) == [] and views.signal_labels(None) == []


def test_held_items_explain_why():
    def item(auto):
        return SimpleNamespace(context={"auto": auto} if auto is not None else {})

    hold = views.auto_hold(item({"decision": "hold", "confidence": 0.58, "reasons": ["zwei Ordner kommen in Frage", "unsicher (58 %)"]}))
    assert hold["label"] == "Nicht automatisch: unsicher (58 %)"
    assert hold["reasons"] == ["zwei Ordner kommen in Frage", "unsicher (58 %)"]
    assert views.auto_hold(item({"decision": "hold", "reasons": ["mögliches Duplikat"]}))["label"] == "Nicht automatisch: mögliches Duplikat"
    assert views.auto_hold(item({"decision": "hold", "confidence": 0.4}))["label"] == "Nicht automatisch: unsicher (40 %)"
    assert views.auto_hold(item({"decision": "hold"}))["label"] == "Nicht automatisch: zurückgehalten"
    assert views.auto_hold(item({"decision": "file", "confidence": 0.9})) is None
    assert views.auto_hold(item(None)) is None and views.auto_hold(SimpleNamespace(context=None)) is None
    done = views.auto_done(item({"decision": "file", "confidence": 0.82, "review": False, "sources": ["similarity", "llm"]}))
    assert done["label"] == "82 % – Ähnlichkeit, KI" and not done["review"]
    assert views.auto_done(item({"decision": "hold"})) is None


def test_language_model_endpoint_and_answer():
    assert views.endpoint_host("http://user:geheim@ollama:11434/api?token=1") == "http://ollama:11434"
    assert views.endpoint_host("https://[::1]:8080/v1") == "https://[::1]:8080"
    assert views.endpoint_host("kein url") == "" and views.endpoint_host(None) == ""
    for local in ("http://ollama:11434", "http://localhost:11434", "http://192.168.1.20:8080", "http://10.0.0.5",
                  "http://[::1]:1234", "http://llm.home.arpa", "http://100.101.102.103:11434", "http://gpu.lan"):
        assert views.endpoint_is_local(local) is True, local
    for remote in ("https://api.example.com/v1", "http://8.8.8.8:11434"):
        assert views.endpoint_is_local(remote) is False, remote
    assert views.endpoint_is_local("") is None

    info = views.llm_info(SimpleNamespace(llm=LlmConfig(url="https://api.example.com/v1", model="gpt-x", provider="openai", api_key="sk-geheim")))
    assert info.enabled and info.provider_label == "OpenAI-kompatibel" and info.endpoint == "https://api.example.com"
    assert info.api_key is True and info.local is False and info.context_tokens is None
    assert "sk-geheim" not in repr(info)
    ollama = views.llm_info(SimpleNamespace(llm=LlmConfig(url="http://ollama:11434", model="qwen2.5:7b")))
    assert ollama.provider_label == "Ollama" and ollama.context_tokens == 8192 and ollama.local is True
    assert not views.llm_info(SimpleNamespace(llm=None)).enabled

    rows = views.probe_answer({"titel": "Stromrechnung", "ordner": "Wohnung/Strom", "neuer_ordner": False,
                               "tags": ["Strom", "2026"], "sicherheit": 0.83, "datum": None, "extra": "x" * 500})
    assert [r["label"] for r in rows] == ["Ordner", "Neuer Ordner", "Titel", "Datum", "Tags", "Sicherheit", "extra"]
    values = {r["label"]: r["value"] for r in rows}
    assert values["Neuer Ordner"] == "nein" and values["Tags"] == "Strom, 2026" and values["Sicherheit"] == "83 %"
    assert values["Datum"] == "–" and len(values["extra"]) == views.MAX_ANSWER_CHARS
    assert views.probe_answer(None) == []


def test_language_model_test_runs_in_a_thread_with_a_time_limit():
    config = LlmConfig(url="http://ollama:11434", model="qwen2.5:7b")
    probe = views.LlmProbe()
    status, result = probe.run(config, timeout=5, probe=lambda c: {"ok": True, "model": c.model, "duration_s": 1.5, "answer": {}})
    assert status == "done" and result["ok"] and result["model"] == "qwen2.5:7b" and result["at"]

    gate = threading.Event()

    def slow(c):
        gate.wait(5)
        return {"ok": False, "error": "Zeitüberschreitung", "duration_s": 9.0}

    status, result = probe.run(config, timeout=0.05, probe=slow)
    assert status == "running" and result is None
    assert probe.run(config, timeout=0.05, probe=slow) == ("busy", None)
    assert probe.snapshot()[0] is True
    gate.set()
    for _ in range(100):
        running, last = probe.snapshot()
        if not running:
            break
        time.sleep(0.02)
    assert not running and last["error"] == "Zeitüberschreitung"  # kept for the next visit

    def broken(c):
        raise RuntimeError("kaputt")

    status, result = probe.run(config, timeout=5, probe=broken)
    assert status == "done" and not result["ok"] and "kaputt" in result["error"]


def test_probe_uses_the_language_model_client(monkeypatch):
    from docushelf.suggest import llm

    config = LlmConfig(url="http://ollama:11434", model="qwen2.5:7b")

    class Client:
        def __init__(self, cfg):
            assert cfg is config

        def probe(self):
            return {"ok": True, "provider": "ollama", "model": "qwen2.5:7b", "duration_s": 2.345,
                    "answer": {"ordner": "Wohnung/Strom"}, "error": None}

    monkeypatch.setattr(llm, "LlmClient", Client)
    result = views.probe_llm(config)
    assert result["ok"] and result["duration_s"] == 2.35 and result["answer"] == {"ordner": "Wohnung/Strom"}

    class Failing(Client):
        def probe(self):
            return {"ok": False, "duration_s": 0.1, "answer": None, "error": None}

    monkeypatch.setattr(llm, "LlmClient", Failing)
    assert views.probe_llm(config)["error"] == "Keine verwertbare Antwort."

    class Old:
        def __init__(self, cfg):
            pass

    monkeypatch.setattr(llm, "LlmClient", Old)
    assert not views.probe_llm(config)["ok"]


def test_audit_log_is_read_once_until_it_changes(svc, samples):
    auto_filed(svc, samples, "Wohnung/Strom/2026-01-15 Stromrechnung.pdf")
    calls = []
    original = svc.audit.entries

    def counting(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    svc.audit.entries = counting
    assert len(views.auto_filings(svc)) == 1
    assert len(views.auto_filings(svc)) == 1 and views.auto_pending_count(svc) == 0
    assert len(calls) == 1 and calls[0]["actions"] == set(views.AUTO_ACTIONS)
    svc.audit.write("alex", "auto_review", doc_id="x", status="ok")
    views.auto_filings(svc)
    assert len(calls) == 2


def test_filings_with_reviews_edits_and_gone_documents(svc, samples):
    sure = auto_filed(svc, samples, "Wohnung/Strom/2026-01-15 Stromrechnung.pdf", ago=timedelta(days=1), confidence=0.91,
                      sources=("similarity", "llm"))
    unsure = auto_filed(svc, samples, "Versicherungen/2026-01-10 Beitrag.pdf", ago=timedelta(hours=2), confidence=0.62, review=True)
    edited = auto_filed(svc, samples, "Finanzen/2026-01-11 Brief.pdf", ago=timedelta(hours=3), confidence=0.5, review=True)
    gone = auto_filed(svc, samples, "Alt/2026-01-12 Weg.pdf", ago=timedelta(hours=4), confidence=0.4, review=True, keep=False)
    old = auto_filed(svc, samples, "Alt/2025-01-01 Uralt.pdf", ago=timedelta(days=90), keep=False)
    # Only a person's change counts as checked; the automatic filer's own entries do not.
    write_audit(svc, "update", at=now() - timedelta(hours=2, minutes=30), user="automatisch", doc_id=edited.doc_id)
    write_audit(svc, "update", at=now() - timedelta(hours=1), user="alex", doc_id=edited.doc_id, path=edited.rel)
    write_audit(svc, "auto_review", at=now() - timedelta(minutes=5), user="alex", doc_id=sure.doc_id, status="ok")

    filings = views.auto_filings(svc)
    assert [f.doc_id for f in filings] == [unsure.doc_id, edited.doc_id, gone.doc_id, sure.doc_id]
    assert old.doc_id not in {f.doc_id for f in views.auto_filings(svc)}
    assert old.doc_id in {f.doc_id for f in views.auto_filings(svc, days=120)}
    by_id = {f.doc_id: f for f in filings}
    assert by_id[sure.doc_id].review["status"] == "ok" and by_id[sure.doc_id].review_label == "bestätigt"
    assert by_id[sure.doc_id].label == "91 % – Ähnlichkeit, KI" and not by_id[sure.doc_id].uncertain
    assert by_id[edited.doc_id].review == {"at": by_id[edited.doc_id].review["at"], "user": "alex", "status": "changed"}
    assert by_id[edited.doc_id].review_label == "geändert"
    assert by_id[gone.doc_id].gone and not by_id[gone.doc_id].pending
    assert [f.doc_id for f in filings if f.pending] == [unsure.doc_id]
    assert views.auto_pending_count(svc) == 1
    data = by_id[unsure.doc_id].to_dict()
    assert data["exists"] and data["pending"] and data["current"]["path"] == unsure.rel and data["url"] == f"/dokument/{unsure.doc_id}"
    overview = views.auto_overview(svc)
    assert (overview.total, overview.recent, overview.pending, overview.show_tabs) == (4, 4, 1, True)
    found = views.find_auto_filing(svc, unsure.doc_id)
    assert found.record is not None and found.confidence_pct == 62
    assert views.find_auto_filing(svc, "unbekannt") is None and views.find_auto_filing(svc, "") is None


def test_a_review_flag_wins_over_the_current_threshold(svc, samples):
    entry = auto_filed(svc, samples, "A/2026-01-01 X.pdf", confidence=0.6, review=False)
    assert not views.find_auto_filing(svc, entry.doc_id).uncertain  # it was sure enough when it was filed
    write_audit(svc, "auto_file", at=now(), doc_id="ohne-flag", path="A/y.pdf", confidence=0.6)
    assert views.find_auto_filing(svc, "ohne-flag").uncertain  # older entries: compared with the threshold


# ---------------------------------------------------------------------------
# Inbox


def test_inbox_shows_tabs_info_bar_and_held_items(client, svc, samples, tmp_path):
    auto_filed(svc, samples, "Wohnung/Strom/2026-01-15 Stromrechnung.pdf")
    auto_filed(svc, samples, "Versicherungen/2026-01-10 Beitrag.pdf", confidence=0.6, review=True)
    auto_filed(svc, samples, "Finanzen/Bank/2026-01-11 Auszug.pdf", ago=timedelta(days=2))
    auto_filed(svc, samples, "Finanzen/Bank/2025-12-11 Auszug.pdf", ago=timedelta(days=20))
    held = make_ready_item(svc, samples, tmp_path)
    held.context["auto"] = {"decision": "hold", "confidence": 0.58,
                            "reasons": ["zwei Ordner kommen in Frage", "unsicher (58 %)"], "at": "2026-11-20T10:00:00+01:00"}
    svc.inbox.save(held)
    plain = make_ready_item(svc, samples, tmp_path)

    html = page(client, "/eingang")
    bar = re.search(r'<div class="auto-bar".*?</div>', html, re.S).group(0)
    assert text_of(bar) == "Automatische Ablage: an (sicher ab 75 %) · 3 Dokumente in den letzten 7 Tagen · 1 unsicher Ansehen"
    assert 'href="/eingang/automatisch?unsicher=1"' in bar
    tabs = parse(html)
    links = {a.get("href"): a for t, a in tabs.tags if t == "a" and a.get("class", "").startswith("tab")}
    assert links["/eingang"].get("aria-current") == "page" and "aria-current" not in links["/eingang/automatisch"]
    assert "Zu bestätigen" in html and "Automatisch abgelegt" in html and 'class="tab-dot"' in html
    # The badge on the held item names the reason; all reasons are in its tooltip.
    assert html.count("data-auto-hold") == 1 and "Nicht automatisch: unsicher (58 %)" in html
    assert 'title="zwei Ordner kommen in Frage · unsicher (58 %)"' in html
    card = re.search(rf'data-card="{plain.id}".*?</li>', html, re.S).group(0)
    assert "Nicht automatisch" not in card
    # The navigation shows a dot for the uncertain filing nobody checked yet.
    assert 'data-auto-review' in html and "1 automatisch abgelegtes Dokument zum Prüfen" in html

    detail = page(client, f"/eingang/{held.id}")
    assert "Nicht automatisch: unsicher (58 %)." in detail and "zwei Ordner kommen in Frage" in detail


def test_inbox_without_automatic_filing(client, svc, samples, tmp_path):
    svc.settings = svc.settings.with_overrides(auto_file=AutoFileConfig())
    held = make_ready_item(svc, samples, tmp_path)
    held.context["auto"] = {"decision": "hold", "confidence": 0.5, "reasons": ["unsicher (50 %)"]}
    svc.inbox.save(held)
    html = page(client, "/eingang")
    assert 'class="tabs"' not in html and "auto-bar" not in html and "data-auto-hold" not in html
    assert "data-auto-review" not in html
    assert "data-auto-hold" not in page(client, f"/eingang/{held.id}")
    # Filings from the time it was on still get the tabs (and the dot while unchecked).
    auto_filed(svc, samples, "A/2026-01-01 X.pdf", confidence=0.5, review=True)
    html = page(client, "/eingang")
    assert 'class="tabs"' in html and "auto-bar" not in html and "data-auto-review" in html


def test_automatic_tab_lists_the_filings(client, svc, samples):
    sure = auto_filed(svc, samples, "Wohnung/Strom/2026-01-15 Stromrechnung Januar.pdf", confidence=0.91, ago=timedelta(hours=4),
                      sources=("similarity", "llm"), reasons=("ähnlich zu „Stromrechnung Dezember“ (88 %)", "KI: Stadtwerke"))
    unsure = auto_filed(svc, samples, "Versicherungen/Haftpflicht/2026-01-10 Beitragsrechnung.pdf", confidence=0.62, review=True,
                        ago=timedelta(hours=3), sources=("llm",), reasons=("zwei Ordner kommen in Frage",))
    gone = auto_filed(svc, samples, "Alt/2026-01-12 Weg.pdf", confidence=0.95, sources=("rule",), keep=False, ago=timedelta(hours=2))
    confirmed = auto_filed(svc, samples, "Finanzen/2026-01-02 Auszug.pdf", sources=("import",))
    write_audit(svc, "auto_review", at=now(), user="anna", doc_id=confirmed.doc_id, status="ok")

    html = page(client, "/eingang/automatisch")
    assert "Automatisch abgelegt <span class=\"count\">4</span>" in html
    rows = {doc_id: re.search(rf'<li class="[^"]*" data-auto-row="{doc_id}".*?</li>', html, re.S).group(0)
            for doc_id in (sure.doc_id, unsure.doc_id, gone.doc_id, confirmed.doc_id)}
    assert html.index(confirmed.doc_id) < html.index(sure.doc_id)  # newest first
    row = rows[sure.doc_id]
    assert f'href="/dokument/{sure.doc_id}"' in row and "Stromrechnung Januar" in row
    assert 'href="/archiv/Wohnung/Strom"' in row and "confidence-ok" in row and "91 %" in row
    assert ">Ähnlichkeit<" in row and ">KI<" in row and "ähnlich zu „Stromrechnung Dezember“ (88 %) · KI: Stadtwerke" in row
    assert f'action="/eingang/automatisch/{sure.doc_id}/passt"' in row and "Passt" in row and "Ändern" in row
    row = rows[unsure.doc_id]
    assert "confidence-low" in row and "62 % · unsicher" in row and "is-pending" in row and "btn-primary" in row
    row = rows[gone.doc_id]
    assert "is-gone" in row and "nicht mehr im Archiv" in row and "Alt/2026-01-12 Weg.pdf" in row
    assert "/passt" not in row and "<a " not in row and ">Regel<" in row
    row = rows[confirmed.doc_id]
    assert "bestätigt" in row and "/passt" not in row and ">Paperless<" in row
    page_forms = [f for f in parse(html).forms if f["attrs"].get("action", "").endswith("/passt")]
    assert len(page_forms) == 2 and all(any(i.get("name") == "after" for i in f["inputs"]) for f in page_forms)

    only = page(client, "/eingang/automatisch?unsicher=1")
    assert unsure.doc_id in only and sure.doc_id not in only and gone.doc_id not in only
    assert 'aria-current="page">Nur unsichere' in only
    assert 'name="after" value="/eingang/automatisch?unsicher=1"' in only


def test_confirming_a_filing(client, svc, samples):
    unsure = auto_filed(svc, samples, "Versicherungen/2026-01-10 Beitrag.pdf", confidence=0.6, review=True)
    assert "data-auto-review" in page(client, "/eingang")
    response = post(client, f"/eingang/automatisch/{unsure.doc_id}/passt", {"after": "/eingang/automatisch?unsicher=1"})
    assert response.headers["location"] == "/eingang/automatisch?unsicher=1"
    entry = svc.audit.tail(1)[0]
    assert entry["action"] == "auto_review" and entry["doc_id"] == unsure.doc_id and entry["status"] == "ok"
    assert entry["user"] == "alex" and entry["path"] == unsure.rel
    html = page(client, response.headers["location"])
    assert "Danke! „Beitrag“ ist als geprüft markiert." in flash_texts(html)
    assert "Nichts zu prüfen" in html and "data-auto-review" not in html
    assert "bestätigt" in page(client, "/eingang/automatisch")

    post(client, f"/eingang/automatisch/{unsure.doc_id}/passt")
    assert "„Beitrag“ ist schon geprüft." in flash_texts(page(client, "/eingang/automatisch"))
    assert [e["action"] for e in svc.audit.tail(2)] == ["auto_review", "auto_file"]  # no second entry
    response = post(client, "/eingang/automatisch/gibt-es-nicht/passt", {"after": "https://evil.example"})
    assert response.headers["location"] == "/eingang/automatisch"
    assert any("keine automatische Ablage" in t for t in flash_texts(page(client, "/eingang/automatisch")))


def test_document_page_shows_the_decision_until_it_is_checked(client, svc, samples):
    filed = auto_filed(svc, samples, "Wohnung/Strom/2026-01-15 Stromrechnung.pdf", confidence=0.82, sources=("similarity", "llm"))
    html = page(client, f"/dokument/{filed.doc_id}")
    notice = re.search(r'<div class="alert[^"]*auto-notice".*?</form>', html, re.S).group(0)
    assert "Automatisch abgelegt (82 % – Ähnlichkeit, KI)" in text_of(notice) and "alert-info" in notice
    assert "Ändere unten Titel oder Ordner" in notice
    assert f'action="/eingang/automatisch/{filed.doc_id}/passt"' in notice
    assert f'name="after" value="/dokument/{filed.doc_id}"' in notice
    response = post(client, f"/eingang/automatisch/{filed.doc_id}/passt", {"after": f"/dokument/{filed.doc_id}"})
    assert response.headers["location"] == f"/dokument/{filed.doc_id}"
    assert "data-auto-notice" not in page(client, f"/dokument/{filed.doc_id}")

    # Correcting it on the document page counts as checked as well.
    unsure = auto_filed(svc, samples, "Sonstiges/2026-01-16 Brief.pdf", confidence=0.55, review=True)
    html = page(client, f"/dokument/{unsure.doc_id}")
    assert "alert-warning auto-notice" in html and "– bitte prüfen" in html
    post(client, f"/dokument/{unsure.doc_id}/bearbeiten", {"title": "Brief", "doc_date": "2026-01-16", "folder": "Wohnung"})
    assert "data-auto-notice" not in page(client, f"/dokument/{unsure.doc_id}")
    assert "geändert" in page(client, "/eingang/automatisch") and views.auto_pending_count(svc) == 0
    # Documents filed by a person get no notice.
    other = add_archive_pdf(svc, samples, "Wohnung/2026-01-01 Normal.pdf")
    assert "data-auto-notice" not in page(client, f"/dokument/{other.id}")


def test_empty_states_of_the_automatic_tab(client, svc, samples):
    html = page(client, "/eingang/automatisch")
    assert "Noch nichts automatisch abgelegt" in html and 'aria-current="page"' in html
    svc.settings = svc.settings.with_overrides(auto_file=AutoFileConfig())
    html = page(client, "/eingang/automatisch")
    assert "Automatische Ablage ist aus" in html and "/einstellungen/ablage#automatisch" in html and 'class="tabs"' not in html
    auto_filed(svc, samples, "A/2026-01-01 X.pdf")
    assert "Nichts zu prüfen" in page(client, "/eingang/automatisch?unsicher=1")


def test_filed_inbox_item_says_it_was_filed_automatically(client, svc, samples, tmp_path):
    item = make_ready_item(svc, samples, tmp_path)
    record = svc.ops.file_item(
        item.id, FilingRequest(folder="Wohnung/Strom", title="Stromrechnung", doc_date=date(2026, 1, 15)), user="automatisch",
        auto={"confidence": 0.82, "review": True, "sources": ["similarity", "llm"], "reasons": ["ähnlich zu „Dezember“"]},
    )
    html = page(client, f"/eingang/{item.id}")
    assert "bereits automatisch abgelegt" in html and "Sicherheit 82 % – Ähnlichkeit, KI" in html
    assert "als unsicher zum Prüfen markiert" in html and 'href="/eingang/automatisch"' in html
    # The real archive operations wrote the record the pages read.
    filing = views.find_auto_filing(svc, record.id)
    assert filing is not None and filing.uncertain and filing.current_path == record.path
    plain = make_ready_item(svc, samples, tmp_path)
    svc.ops.file_item(plain.id, FilingRequest(folder="A", title="T"), user="alex")
    assert "data-auto-done" not in page(client, f"/eingang/{plain.id}")


# ---------------------------------------------------------------------------
# Encrypted PDFs, rules page, layout


def test_encrypted_item_gets_one_placeholder_page(client, svc, samples, tmp_path):
    locked = make_encrypted_pdf(samples, tmp_path / "quelle" / "Vertrag.pdf", ["Vertraulich"])
    item = make_ready_item(svc, samples, tmp_path, original=locked, context={"pdf_protection": "encrypted"})
    assert item.page_count == 0
    html = page(client, f"/eingang/{item.id}")
    assert "data-preview-placeholder" in html and "Keine Vorschau: Die PDF ist mit einem Passwort geschützt" in html
    assert "<iframe" not in html and f"/api/v1/inbox/{item.id}/pages/1.png" not in html
    assert "/static/page-unavailable.svg" in html and f'/api/v1/inbox/{item.id}/document?download=1' in html
    inbox = page(client, "/eingang")
    assert "/static/page-unavailable.svg" in inbox and f"/api/v1/inbox/{item.id}/pages/1.png" not in inbox
    # Signed PDFs (and encrypted ones whose pages could be counted) keep the normal preview.
    item.page_count = 2
    svc.inbox.save(item)
    html = page(client, f"/eingang/{item.id}")
    assert "data-viewer" in html and "<iframe" not in html


def test_rules_page_explains_the_sender_condition_and_the_backup(client, svc):
    html = page(client, "/regeln")
    assert '<label for="r-sender">Erkannter Absender enthält</label>' in html
    assert "Ist kein Absender erkannt, zählt nur der Briefkopf auf Seite 1." in html
    svc.rules.path.write_text(
        "# Meine Regeln, bitte nicht anfassen\nrules:\n  - name: Bank\n    match:\n      sender: [Sparkasse]\n"
        "    action:\n      folder: Finanzen/Bank\n",
        encoding="utf-8",
    )
    # Appending keeps the comments: no note.
    post(client, "/regeln/neu", {"sender": "Stadtwerke", "folder": "Wohnung/Strom"})
    assert not any("rules.yaml.bak" in t for t in flash_texts(page(client, "/regeln")))
    # Deleting rewrites the file: the note says where the commented version is.
    post(client, "/regeln/0/loeschen", {"name": "Bank"})
    texts = flash_texts(page(client, "/regeln"))
    assert any("liegt als rules.yaml.bak daneben" in t for t in texts), texts
    assert "Meine Regeln" in svc.rules.backup_path.read_text(encoding="utf-8")


def test_rules_can_be_marked_automatic(client, svc):
    post(client, "/regeln/neu", {"sender": "Sparkasse", "folder": "Finanzen/Bank", "auto": "1"})
    post(client, "/regeln/neu", {"sender": "Stadtwerke", "folder": "Wohnung/Strom"})
    rules = svc.rules.load()
    assert [rule.auto for rule in rules] == [True, False]
    html = page(client, "/regeln")
    assert html.count(">automatisch</span>") == 1 and "außer bei Regeln, die als „automatisch“ markiert sind" in html
    assert 'name="auto"' in html
    response = post(client, "/regeln/neu", {"folder": "X", "auto": "1"}, status=400)
    assert 'name="auto" value="1" checked' in response.text  # kept after an input error


def test_base_page_links_the_web_app_manifest(client):
    html = page(client, "/eingang")
    assert '<link rel="manifest" href="/manifest.webmanifest">' in html
    assert '<link rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png">' in html
    assert '<meta name="theme-color"' in html


def test_navigation_dot_helper():
    entries = templating.build_nav("/eingang", {"bereit": 2, "fehler": 0}, review=3)
    inbox = next(e for e in entries if e["key"] == "eingang")
    assert inbox["badge"] == 2 and inbox["review"] == 3
    assert templating.auto_pending(None) == 0 and templating.auto_pending(SimpleNamespace()) == 0

    class Broken:
        audit = object()
        settings = None

    assert templating.auto_pending(Broken()) == 0


# ---------------------------------------------------------------------------
# Settings


def test_settings_sections(admin_client, svc, samples):
    client = admin_client
    auto_filed(svc, samples, "A/2026-01-01 X.pdf", confidence=0.5, review=True)
    html = page(client, "/einstellungen/ablage")
    auto = re.search(r'<section class="card settings-card" id="automatisch".*?</section>', html, re.S).group(0)
    text = text_of(auto)
    for part in ("Automatische Ablage", "wirkt sofort", "Sicher ab", "Quellen", "Alle Watch-Ordner", "Neue Ordner",
                 "Auf die KI warten", "Benachrichtigen"):
        assert part in text, part
    assert 'value="off" checked' in auto and 'name="auto_file_threshold" value="75"' in auto
    assert "In den letzten 7 Tagen automatisch abgelegt" in html and "1 unsicher" in html
    assert "Das Sprachmodell ist aus" in text
    llm = page(client, "/einstellungen/ki")
    status = re.search(r'id="status".*?</section>', llm, re.S).group(0)
    assert "ausgeschaltet" in status and "Verbindung testen" not in status
    assert 'id="sprachmodell"' in llm and 'name="llm_api_key"' in llm
    mobile = page(client, "/einstellungen/handy")
    card = re.search(r'<section class="card settings-card" id="handy".*?</section>', mobile, re.S).group(0)
    assert "Handy &amp; Tablet" in card and "über „Teilen“" in card
    if (templating.TEMPLATE_DIR / "_share_help.html").exists():
        assert len(text_of(card)) > 200  # the help for Android and iPhone is included

    svc.settings = svc.settings.with_overrides(
        auto_file=AutoFileConfig(mode="immer", threshold=0.6, sources=("upload", "mail"), new_folders=False, notify=False),
        llm=LlmConfig(url="https://api.example.com/v1", model="gpt-4o-mini", provider="openai", api_key="sk-geheim"),
    )
    llm = page(client, "/einstellungen/ki")
    status = re.search(r'id="status".*?</section>', llm, re.S).group(0)
    assert "OpenAI-kompatibel" in status and "https://api.example.com" in status and "/v1" not in status
    assert "gpt-4o-mini" in status and "hinterlegt" in status and "sk-geheim" not in llm
    assert "data-llm-remote" in status and "Achtung:" in status and "noch nicht geprüft" in status
    assert 'action="/einstellungen/sprachmodell/testen"' in status and "Verbindung testen" in status
    assert "Das Sprachmodell ist aus" not in page(client, "/einstellungen/ablage")


def test_language_model_connection_test(admin_client, svc, monkeypatch):
    client = admin_client
    svc.settings = svc.settings.with_overrides(llm=LlmConfig(url="http://ollama:11434", model="qwen2.5:7b", timeout=30))
    answers = [{"ok": True, "provider": "ollama", "model": "qwen2.5:7b", "duration_s": 3.24,
                "answer": {"ordner": "Wohnung/Strom", "titel": "Stromrechnung Stadtwerke Januar", "sicherheit": 0.9}, "error": None}]
    seen = []
    monkeypatch.setattr(views, "probe_llm", lambda config: seen.append(config) or answers[-1])

    response = post(client, "/einstellungen/sprachmodell/testen")
    assert response.headers["location"] == "/einstellungen/ki#status" and seen == [svc.settings.llm]
    html = page(client, "/einstellungen/ki")
    assert "Das Sprachmodell antwortet (3,2 s)." in flash_texts(html)
    llm = re.search(r'id="status".*?</section>', html, re.S).group(0)
    assert "status-ok" in llm and ">ja" in llm and "im lokalen Netz" in llm and "8.192 Tokens" in llm
    assert "Wohnung/Strom" in llm and "Stromrechnung Stadtwerke Januar" in llm and "90 %" in llm

    answers.append({"ok": False, "provider": "ollama", "model": "qwen2.5:7b", "duration_s": 0.1, "answer": None,
                    "error": "Keine Verbindung zu http://ollama:11434"})
    post(client, "/einstellungen/sprachmodell/testen")
    html = page(client, "/einstellungen/ki")
    assert "Das Sprachmodell antwortet nicht: Keine Verbindung zu http://ollama:11434" in flash_texts(html)
    assert "Letzter Fehler" in html and "status-bad" in html

    # A model that is still loading: the page does not wait forever, the result shows up later.
    gate = threading.Event()

    def slow(config):
        gate.wait(5)
        return answers[0]

    monkeypatch.setattr(views, "probe_llm", slow)
    monkeypatch.setattr(routes_settings, "LLM_PROBE_WAIT", 0.05)
    post(client, "/einstellungen/sprachmodell/testen")
    html = page(client, "/einstellungen/ki")
    assert any("Der Test läuft weiter" in t for t in flash_texts(html))
    assert "neuer Test läuft" in html and re.search(r'<button class="btn" type="submit" disabled>', html)
    post(client, "/einstellungen/sprachmodell/testen")
    assert any("Ein Test läuft noch" in t for t in flash_texts(page(client, "/einstellungen/ki")))
    gate.set()
    for _ in range(100):
        if not client.app.state.llm_probe.snapshot()[0]:
            break
        time.sleep(0.02)
    assert "Wohnung/Strom" in page(client, "/einstellungen/ki")

    svc.settings = svc.settings.with_overrides(llm=None)
    post(client, "/einstellungen/sprachmodell/testen")
    assert any("ausgeschaltet" in t for t in flash_texts(page(client, "/einstellungen/ki")))


# ---------------------------------------------------------------------------
# API


@pytest.fixture
def api(svc):
    app = make_app(svc)
    api_client = client_for(app)
    token, _entry = svc.tokens.create("Tests", "anna")
    api_client.headers["Authorization"] = f"Bearer {token}"
    return SimpleNamespace(app=app, client=api_client)


def test_api_lists_and_confirms_automatic_filings(api, svc, samples):
    sure = auto_filed(svc, samples, "Wohnung/Strom/2026-01-15 Stromrechnung.pdf", confidence=0.91, sources=("similarity", "llm"),
                      ago=timedelta(minutes=10))
    unsure = auto_filed(svc, samples, "Versicherungen/2026-01-10 Beitrag.pdf", confidence=0.6, review=True, ago=timedelta(hours=2))
    gone = auto_filed(svc, samples, "Alt/2026-01-12 Weg.pdf", keep=False, ago=timedelta(days=3))
    old = auto_filed(svc, samples, "Alt/2025-06-01 Alt.pdf", keep=False, ago=timedelta(days=100))

    data = api.client.get("/api/v1/auto").json()
    assert data["mode"] == "confident" and data["threshold"] == 0.75 and data["days"] == 60
    assert data["summary"] == {"total": 3, "recent": 3, "pending": 1}
    assert [e["doc_id"] for e in data["entries"]] == [sure.doc_id, unsure.doc_id, gone.doc_id]
    first = data["entries"][0]
    assert first["signals"] == ["Ähnlichkeit", "KI"] and first["confidence"] == 0.91 and first["exists"]
    assert first["current"] == {"path": sure.rel, "folder": "Wohnung/Strom", "title": "Stromrechnung"}
    assert data["entries"][2]["exists"] is False and data["entries"][2]["current"] is None
    assert [e["doc_id"] for e in api.client.get("/api/v1/auto?uncertain=true").json()["entries"]] == [unsure.doc_id]
    assert old.doc_id in {e["doc_id"] for e in api.client.get("/api/v1/auto?days=120").json()["entries"]}
    assert api.client.get("/api/v1/auto?days=0").status_code == 422

    response = api.client.post(f"/api/v1/auto/{unsure.doc_id}/ok")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok" and body["already"] is False and body["review"]["user"] == "anna"
    assert svc.audit.tail(1)[0] | {"at": None} == {"at": None, "user": "anna", "action": "auto_review", "doc_id": unsure.doc_id,
                                                   "status": "ok", "title": "Beitrag", "path": unsure.rel}
    assert api.client.post(f"/api/v1/auto/{unsure.doc_id}/ok").json()["already"] is True
    assert api.client.get("/api/v1/auto").json()["summary"]["pending"] == 0
    # Old filings can still be confirmed, unknown ones cannot.
    assert api.client.post(f"/api/v1/auto/{old.doc_id}/ok").json()["already"] is False
    missing = api.client.post("/api/v1/auto/unbekannt/ok")
    assert missing.status_code == 404 and "keine automatische Ablage" in missing.json()["detail"]
    status = api.client.get("/api/v1/status").json()
    assert status["features"]["auto_file"] == "confident"


def test_api_confirmation_with_a_session_needs_the_csrf_header(svc, samples):
    unsure = auto_filed(svc, samples, "A/2026-01-10 Beitrag.pdf", confidence=0.6, review=True)
    browser = client_for(make_app(svc))
    assert login_local(browser).status_code == 303
    assert browser.post(f"/api/v1/auto/{unsure.doc_id}/ok").status_code == 403
    token = session_data(browser)["csrf"]
    assert browser.post(f"/api/v1/auto/{unsure.doc_id}/ok", headers={"X-CSRF-Token": token}).status_code == 200
    assert client_for(make_app(svc)).get("/api/v1/auto").status_code == 401


# ---------------------------------------------------------------------------
# Through the complete application and on the real service container


def test_automatic_tab_through_the_complete_app(svc, samples):
    unsure = auto_filed(svc, samples, "A/2026-01-10 Beitrag.pdf", confidence=0.6, review=True)
    browser = client_for(make_app(svc))
    assert browser.get("/eingang/automatisch", follow_redirects=False).status_code == 303  # login first
    assert login_local(browser).status_code == 303
    html = browser.get("/eingang/automatisch").text
    token = csrf_of(html)
    assert_clean_page(html, csrf=token)
    assert browser.post(f"/eingang/automatisch/{unsure.doc_id}/passt", follow_redirects=False).status_code == 403
    response = browser.post(f"/eingang/automatisch/{unsure.doc_id}/passt", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303 and svc.audit.tail(1)[0]["action"] == "auto_review"
    manifest_csp = browser.get("/eingang").headers["content-security-policy"]
    assert "default-src 'self'" in manifest_csp


@pytest.fixture
def real(env, monkeypatch):
    """The real service container with automatic filing and a language model configured."""
    from docushelf.services import Services

    settings = load_settings({**env, "DOCUSHELF_AUTO_FILE": "sicher", "DOCUSHELF_LLM_ENABLED": "true",
                              "DOCUSHELF_LLM_URL": "http://127.0.0.1:9", "DOCUSHELF_LLM_TIMEOUT": "2"})
    monkeypatch.setattr(routes_ui, "_ocr_info", lambda s: OcrStub(languages=[], missing_languages=[]))
    instance = Services(settings)
    yield instance
    instance.stop()
    instance.index.close()


def test_new_pages_with_the_real_service_container(real, samples, tmp_path):
    item = make_ready_item(real, samples, tmp_path)
    record = real.ops.file_item(
        item.id, FilingRequest(folder="Wohnung/Strom", title="Stromrechnung Januar", doc_date=date(2026, 1, 15)),
        user="automatisch", auto={"confidence": 0.62, "review": True, "sources": ["similarity", "llm"], "reasons": ["ähnlich zu x"]},
    )
    held = make_ready_item(real, samples, tmp_path)
    held.context["auto"] = {"decision": "hold", "confidence": 0.4, "reasons": ["unsicher (40 %)"]}
    real.inbox.save(held)
    with TestClient(build_app(real, ADMIN)) as test_client:
        for url in ("/eingang", "/eingang/automatisch", "/eingang/automatisch?unsicher=1", f"/eingang/{item.id}",
                    f"/eingang/{held.id}", f"/dokument/{record.id}", "/einstellungen", "/regeln", "/archiv",
                    "/archiv/Wohnung/Strom"):
            page(test_client, url)
        html = page(test_client, "/eingang")
        assert "1 unsicher" in html and "Nicht automatisch: unsicher (40 %)" in html and "data-auto-review" in html
        assert "Automatisch abgelegt (62 % – Ähnlichkeit, KI)" in page(test_client, f"/dokument/{record.id}")
        # The language model is not reachable here: the test says so instead of hanging.
        post(test_client, "/einstellungen/sprachmodell/testen")
        settings_html = page(test_client, "/einstellungen/ki")
        assert any(t.startswith("Das Sprachmodell antwortet nicht") for t in flash_texts(settings_html))
        post(test_client, f"/eingang/automatisch/{record.id}/passt", {"after": f"/dokument/{record.id}"})
        assert "data-auto-notice" not in page(test_client, f"/dokument/{record.id}")
        assert "data-auto-review" not in page(test_client, "/eingang")
        # The confirmation lives in the audit log on the data volume: it survives an index rebuild.
        real.rebuild(user="alex")
        filing = views.find_auto_filing(real, record.id)
        assert filing is not None and filing.reviewed and not filing.gone


def test_scan_from_the_drop_folder_is_filed_automatically_and_confirmed(env, samples, monkeypatch):
    """A scan arrives in a drop folder, is processed, filed by similarity into an existing
    folder, shows up under "Automatisch abgelegt" and is confirmed with "Passt"."""
    from docushelf.services import Services

    settings = load_settings({**env, "DOCUSHELF_AUTO_FILE": "sicher", "DOCUSHELF_AUTO_FILE_THRESHOLD": "0.6",
                              "DOCUSHELF_OCR_ENABLED": "false"})
    monkeypatch.setattr(routes_ui, "_ocr_info", lambda s: OcrStub(languages=[], missing_languages=[]))
    services = Services(settings)
    try:
        letter = ("Stadtwerke Musterstadt GmbH\nStromrechnung {month} 2025\nKundennummer 4711 0815\n"
                  "Verbrauch Zaehler 12345 Abschlag Strom Arbeitspreis Grundpreis\nRechnungsdatum 15.{number:02d}.2025\n"
                  "Gesamtbetrag 84,20 EUR")
        for number, month in ((1, "Januar"), (2, "Februar"), (3, "Maerz"), (4, "April")):
            samples.text_pdf(services.layout.archive / f"Wohnung/Strom/2025-{number:02d}-15 Stromrechnung {month}.pdf",
                             [letter.format(month=month, number=number)])
        samples.text_pdf(services.layout.archive / "Finanzen/Bank/2025-01-31 Kontoauszug.pdf", ["Sparkasse Kontoauszug Saldo"])
        services.reconciler.scan(full=True)
        samples.text_pdf(services.layout.source_dir("smb") / "scan-0001.pdf", [letter.format(month="Mai", number=5)])
        created = services.intake.scan_once() or services.intake.scan_once()
        assert len(created) == 1
        item_id = created[0]
        services.pipeline.process(item_id)
        assert ("auto_file", item_id) in {(job.kind, job.key) for job in services.index.pending_jobs()}
        decision = services.pipeline.auto_file(item_id)
        assert decision.action == "file", decision
        item = services.inbox.find(item_id)
        assert item.state == "erledigt"
        doc_id = item.context["auto"]["doc_id"]
        record = services.index.get(doc_id)
        assert record is not None and record.folder == "Wohnung/Strom"

        with TestClient(build_app(services)) as test_client:
            html = page(test_client, "/eingang/automatisch")
            row = re.search(rf'<li class="[^"]*" data-auto-row="{doc_id}".*?</li>', html, re.S).group(0)
            assert record.title in row and 'href="/archiv/Wohnung/Strom"' in row and ">Ähnlichkeit<" in row
            assert "data-auto-notice" in page(test_client, f"/dokument/{doc_id}")
            post(test_client, f"/eingang/automatisch/{doc_id}/passt")
            assert "bestätigt" in page(test_client, "/eingang/automatisch")
            assert "data-auto-notice" not in page(test_client, f"/dokument/{doc_id}")
            assert "bereits automatisch abgelegt" in page(test_client, f"/eingang/{item_id}")
    finally:
        services.stop()
        services.index.close()
