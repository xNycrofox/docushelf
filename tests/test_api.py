"""Tests for the JSON API (/api/v1).

Most tests use the real foundation (index, inbox, audit, token store) with
small fakes for the collaborators, so each endpoint's request handling, error
mapping and responses are checked in isolation. The last tests run the whole
flow against the real service container.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import threading
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import unquote

import pytest

from docushelf.channels.sendung import MANIFEST, read_sendung
from docushelf.errors import Conflict, NotFound, PathError, ProcessingError
from docushelf.models import DocumentRecord, ScanStats, Suggestion
from docushelf.util.fsops import sha256_file
from docushelf.util.timeutil import now_iso
from docushelf.web.routes_api import content_disposition, router
from web_support import client_for, login_local, make_app, session_data

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
ITEM_PLACEHOLDER = "20261123-120000-abcdef"


# ---------------------------------------------------------------------------
# Fakes


def make_record(doc_id: str = "11111111-2222-4333-8444-555555555555", path: str = "Finanzen/Bank/2026-01-05 Kontoauszug.pdf", **changes: Any) -> DocumentRecord:
    folder, _, filename = path.rpartition("/")
    record = DocumentRecord(
        id=doc_id,
        path=path,
        folder=folder,
        filename=filename,
        ext=os.path.splitext(filename)[1].lower(),
        title="Kontoauszug",
        doc_date=date(2026, 1, 5),
        sender="Sparkasse",
        tags=["Bank"],
        origin_hash=None,
        content_hash="ab" * 32,
        xmp_id=None,
        has_xmp=False,
        size=100,
        mtime_ns=1,
        inode=None,
        page_count=1,
        text_status="ok",
        indexed_at=now_iso(),
    )
    return dataclasses.replace(record, **changes)


class FakeOps:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.fail: dict[str, Exception] = {}

    def _call(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, *args, kwargs.get("user")))
        if name in self.fail:
            raise self.fail[name]

    def file_item(self, item_id, request, *, user=None):
        self._call("file_item", item_id, request, user=user)
        return make_record(doc_id=f"doc-{item_id}", path=f"{request.folder}/{request.title}.pdf", title=request.title)

    def update_document(self, doc_id, update, *, user=None):
        self._call("update_document", doc_id, update, user=user)
        return make_record(doc_id="22222222-2222-4333-8444-555555555555", title=update.title or "Kontoauszug")

    def delete_document(self, doc_id, *, user=None):
        self._call("delete_document", doc_id, user=user)
        return "20261123-130000-aaaaaa"

    def create_folder(self, rel, *, user=None):
        self._call("create_folder", rel, user=user)
        return rel.strip("/")

    def rename_folder(self, rel, new_name, *, user=None):
        self._call("rename_folder", rel, new_name, user=user)
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        return f"{parent}/{new_name}" if parent else new_name

    def delete_folder(self, rel, *, user=None):
        self._call("delete_folder", rel, user=user)

    def delete_inbox_item(self, item_id, *, user=None):
        self._call("delete_inbox_item", item_id, user=user)
        return "20261123-130001-bbbbbb"


class FakePipeline:
    def __init__(self, services) -> None:
        self.services = services
        self.calls: list[tuple[Any, ...]] = []

    def retry(self, item_id, *, skip_blank_removal=None, skip_ocr=None):
        self.calls.append(("retry", item_id, skip_blank_removal, skip_ocr))
        return self.services.inbox.load(item_id)

    def refresh_suggestions(self, item_id):
        self.calls.append(("refresh", item_id))
        return self.services.inbox.load(item_id)


class FakeSearcher:
    def __init__(self) -> None:
        self.queries: list[Any] = []

    def search(self, query):
        from docushelf.index.search import SearchHit, SearchResult, Snippet

        self.queries.append(query)
        hit = SearchHit(doc=make_record(), score=1.0, snippets=[Snippet(page=1, html="<mark>Strom</mark>")], matched_in=["text"], pages=[1])
        return SearchResult(hits=[hit], total=1, query=query, corrections={"strm": ["strom"]}, took_ms=1)

    def suggest_terms(self, prefix, limit=8):
        return [prefix + "rechnung", prefix + "abrechnung"][:limit]


class FakeTrash:
    def __init__(self, tmp_path: Path) -> None:
        from docushelf.archive.trash import TrashEntry

        self.entry = TrashEntry(
            id="20261120-100000-cccccc",
            kind="document",
            deleted_at="2026-11-20T10:00:00+01:00",
            deleted_by="anna",
            title="Alte Rechnung",
            original_path="Finanzen/2025-01-01 Alte Rechnung.pdf",
            item_id=None,
            doc_id="33333333-2222-4333-8444-555555555555",
            files=[tmp_path / "papierkorb" / "x" / "files" / "2025-01-01 Alte Rechnung.pdf"],
            meta={"sender": "Stadtwerke"},
        )
        self.calls: list[tuple[Any, ...]] = []
        self.restore_result: Any = make_record()

    def list(self):
        return [self.entry]

    def expires_at(self, entry):
        from datetime import datetime

        return datetime(2026, 12, 20, 10, 0)

    def restore(self, entry_id, *, user=None):
        self.calls.append(("restore", entry_id, user))
        if entry_id != self.entry.id:
            raise NotFound("Eintrag im Papierkorb nicht gefunden.")
        return self.restore_result

    def purge(self, entry_id, *, user=None):
        self.calls.append(("purge", entry_id, user))
        if entry_id != self.entry.id:
            raise NotFound("Eintrag im Papierkorb nicht gefunden.")


class FakeRebuild:
    def __init__(self) -> None:
        self.calls: list[str | None] = []
        self.gate: threading.Event | None = None

    def __call__(self, *, user=None):
        self.calls.append(user)
        if self.gate is not None:
            self.gate.wait(10)
        return ScanStats(files_seen=3, added=3)


@pytest.fixture
def api(base_services, tmp_path):
    from docushelf.suggest.rules import RuleStore

    services = base_services
    services.ops = FakeOps()
    services.pipeline = FakePipeline(services)
    services.searcher = FakeSearcher()
    services.trash = FakeTrash(tmp_path)
    services.intake = SimpleNamespace(triggered=0, status=lambda: {"running": False})
    services.intake.trigger = lambda: setattr(services.intake, "triggered", services.intake.triggered + 1)
    services.watcher = SimpleNamespace(reasons=[], status=lambda: {"scanning": False})
    services.watcher.request_scan = lambda reason="": services.watcher.reasons.append(reason)
    services.reconciler = SimpleNamespace(calls=[])
    services.reconciler.scan = lambda **kwargs: services.reconciler.calls.append(kwargs) or ScanStats(files_seen=2)
    services.rebuild = FakeRebuild()
    services.worker = SimpleNamespace(status=lambda: {"running": False}, kick=lambda: None)
    services.rules = RuleStore(services.layout.rules_file)
    app = make_app(services)
    client = client_for(app)
    token, _entry = services.tokens.create("Tests", "anna")
    client.headers["Authorization"] = f"Bearer {token}"
    return SimpleNamespace(app=app, client=client, services=services, token=token)


def ready_item(services, samples, tmp_path, *, name: str = "Rechnung März.pdf", suggestions: list | None = None, document: bool = True):
    """An inbox item in ``bereit`` with a processed document and suggestions."""
    source = samples.text_pdf(tmp_path / "quelle" / name, ["Stadtwerke Musterstadt\nRechnung"])
    item = services.inbox.create("upload", [source], move=False, context={"uploader": "anna"})
    if document:
        shutil.copyfile(source, item.document_path)
    item.stage = "ready"
    item.page_count = 1
    if suggestions is None:
        suggestions = [
            Suggestion(folder="Wohnung/Strom", title="Stromrechnung", doc_date="2026-03-01", tags=["Strom"], sender="Stadtwerke", score=0.9, source="rule").to_dict(),
            Suggestion(folder="Finanzen", title="Rechnung", score=0.4).to_dict(),
        ]
    item.suggestions = suggestions
    services.inbox.save(item)
    services.inbox.move(item, "bereit")
    services.inbox.log(item, "Bereit zur Ablage")
    return item


def archived_pdf(services, samples, rel: str = "Finanzen/Bank/2026-01-05 Kontoauszug März.pdf", *, pages=None) -> DocumentRecord:
    """A real PDF in the archive, registered in the index."""
    path = services.layout.archive / rel
    samples.text_pdf(path, pages or ["Kontoauszug Seite 1", "Seite 2"])
    st = path.stat()
    record = make_record(
        doc_id="44444444-2222-4333-8444-555555555555",
        path=rel,
        title="Kontoauszug März",
        content_hash=sha256_file(path),
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
        page_count=2,
    )
    services.index.add_folder_with_parents(record.folder)
    services.index.upsert(record, pages or ["Kontoauszug Seite 1", "Seite 2"])
    return record


# ---------------------------------------------------------------------------
# Authentication on every endpoint


def _concrete(path: str) -> str:
    values = {"item_id": ITEM_PLACEHOLDER, "doc_id": "x", "number": "1", "page": "1", "entry_id": "y", "token_id": "z"}
    for key, value in values.items():
        path = path.replace("{" + key + "}", value)
    return path


def test_every_endpoint_requires_authentication(api):
    anonymous = client_for(api.app)
    checked = 0
    for route in router.routes:
        for method in sorted(route.methods):
            response = anonymous.request(method, _concrete(route.path))
            assert response.status_code == 401, (method, route.path, response.text)
            assert response.headers["content-type"].startswith("application/json")
            checked += 1
    assert checked >= 34


def test_session_writes_need_the_csrf_header(api, samples, tmp_path):
    item = ready_item(api.services, samples, tmp_path)
    browser = client_for(api.app)
    login_local(browser)
    assert browser.get("/api/v1/status").status_code == 200
    response = browser.post(f"/api/v1/inbox/{item.id}/refresh")
    assert response.status_code == 403
    assert "CSRF" in response.json()["detail"]
    token = session_data(browser)["csrf"]
    assert browser.post(f"/api/v1/inbox/{item.id}/refresh", headers={"X-CSRF-Token": token}).status_code == 200


def test_status(api):
    data = api.client.get("/api/v1/status").json()
    assert data["user"]["username"] == "anna" and data["user"]["method"] == "token"
    assert data["inbox"] == {"arbeit": 0, "bereit": 0, "fehler": 0, "erledigt": 0}
    assert data["index"]["documents"] == 0
    assert data["worker"] == {"running": False}
    assert data["features"]["local_login"] is True
    assert data["ocr"]["enabled"] in (True, False)
    assert data["max_upload_mb"] == api.services.settings.max_upload_mb


# ---------------------------------------------------------------------------
# Upload


def _manifests(services) -> list[Path]:
    return sorted(services.layout.source_dir("upload").glob(f"*/{MANIFEST}"))


def test_upload_single_file(api, samples, tmp_path):
    pdf = samples.text_pdf(tmp_path / "Stromrechnung Januar.pdf", ["Rechnung"])
    with open(pdf, "rb") as handle:
        response = api.client.post("/api/v1/upload", files={"files": ("Stromrechnung Januar.pdf", handle, "application/pdf")})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["documents"] == 1 and body["files"] == ["Stromrechnung Januar.pdf"]
    manifests = _manifests(api.services)
    assert len(manifests) == 1 and manifests[0].parent.name == body["batch_id"]
    sendung = read_sendung(manifests[0].parent)
    assert sendung.context == {"uploader": "anna", "original_names": ["Stromrechnung Januar.pdf"]}
    assert [doc.files for doc in sendung.documents] == [["Stromrechnung Januar.pdf"]]
    assert (manifests[0].parent / "Stromrechnung Januar.pdf").read_bytes() == pdf.read_bytes()
    assert api.services.intake.triggered == 1
    assert api.services.audit.tail(1)[0]["action"] == "upload"


def test_upload_separate_and_combined(api, samples, tmp_path):
    first = samples.image(tmp_path / "seite1.jpg", ["Seite 1"])
    second = samples.image(tmp_path / "seite2.png", ["Seite 2"])
    files = [("files", ("seite1.jpg", first.read_bytes(), "image/jpeg")), ("files", ("seite2.png", second.read_bytes(), "image/png"))]
    separate = api.client.post("/api/v1/upload", files=files)
    assert separate.json()["documents"] == 2
    combined = api.client.post("/api/v1/upload", files=files, data={"combine": "true"})
    assert combined.json()["documents"] == 1
    batch = read_sendung(api.services.layout.source_dir("upload") / combined.json()["batch_id"])
    assert [doc.files for doc in batch.documents] == [["seite1.jpg", "seite2.png"]]
    assert batch.options == {"combine": True}
    separate_batch = read_sendung(api.services.layout.source_dir("upload") / separate.json()["batch_id"])
    assert [doc.files for doc in separate_batch.documents] == [["seite1.jpg"], ["seite2.png"]]


def test_upload_rejects_unsupported_and_empty_files(api, samples, tmp_path):
    pdf = samples.text_pdf(tmp_path / "ok.pdf", ["ok"]).read_bytes()
    response = api.client.post(
        "/api/v1/upload",
        files=[("files", ("ok.pdf", pdf, "application/pdf")), ("files", ("brief.docx", b"PK\x03\x04 kein pdf", "application/octet-stream"))],
    )
    assert response.status_code == 415
    assert "brief.docx" in response.json()["detail"]
    empty = api.client.post("/api/v1/upload", files={"files": ("leer.pdf", b"", "application/pdf")})
    assert empty.status_code == 400 and "Leere Datei" in empty.json()["detail"]
    nothing = api.client.post("/api/v1/upload", files={"anderes": ("x.pdf", pdf, "application/pdf")})
    assert nothing.status_code == 400
    wrong_type = api.client.post("/api/v1/upload", json={"files": []})
    assert wrong_type.status_code == 415
    assert _manifests(api.services) == []
    assert list(api.services.layout.source_dir("upload").iterdir()) == []


def test_upload_size_limit(base_services, samples, tmp_path):
    services = base_services
    services.intake = SimpleNamespace(trigger=lambda: None)
    app = make_app(services, max_upload_mb=1)
    client = client_for(app)
    token, _entry = services.tokens.create("Tests", "anna")
    client.headers["Authorization"] = f"Bearer {token}"
    big = b"%PDF-1.4\n" + b"0" * (1024 * 1024 + 100)
    response = client.post("/api/v1/upload", files={"files": ("gross.pdf", big, "application/pdf")})
    assert response.status_code == 413
    assert "zu groß" in response.json()["detail"]
    huge = b"%PDF-1.4\n" + b"0" * (3 * 1024 * 1024)
    response = client.post("/api/v1/upload", files={"files": ("riesig.pdf", huge, "application/pdf")})
    assert response.status_code == 413
    assert "zu groß" in response.json()["detail"]
    assert not list(services.layout.source_dir("upload").iterdir())


def test_upload_from_a_browser_form_with_csrf_field(api, samples, tmp_path):
    browser = client_for(api.app)
    login_local(browser)
    token = session_data(browser)["csrf"]
    pdf = samples.text_pdf(tmp_path / "scan.pdf", ["x"]).read_bytes()
    refused = browser.post("/api/v1/upload", files={"files": ("scan.pdf", pdf, "application/pdf")})
    assert refused.status_code == 403
    accepted = browser.post("/api/v1/upload", files={"files": ("scan.pdf", pdf, "application/pdf")}, data={"csrf_token": token})
    assert accepted.status_code == 202
    assert read_sendung(api.services.layout.source_dir("upload") / accepted.json()["batch_id"]).context["uploader"] == "admin"


def test_uploaded_batch_is_taken_by_the_intake(api, samples, tmp_path):
    from docushelf.inbox.intake import Intake

    files = [
        ("files", ("Brief.pdf", samples.text_pdf(tmp_path / "Brief.pdf", ["Brief"]).read_bytes(), "application/pdf")),
        ("files", ("Foto.jpg", samples.image(tmp_path / "Foto.jpg", ["Foto"]).read_bytes(), "image/jpeg")),
    ]
    assert api.client.post("/api/v1/upload", files=files).status_code == 202
    created = Intake(api.services).scan_once()
    assert len(created) == 2
    items = [api.services.inbox.load(item_id) for item_id in created]
    assert sorted(item.original_files[0] for item in items) == ["original/Brief.pdf", "original/Foto.jpg"]
    assert all(item.source == "upload" and item.context["uploader"] == "anna" for item in items)
    assert list(api.services.layout.source_dir("upload").iterdir()) == []


# ---------------------------------------------------------------------------
# Inbox


def test_inbox_list_and_detail(api, samples, tmp_path):
    item = ready_item(api.services, samples, tmp_path)
    failed_source = samples.text_pdf(tmp_path / "kaputt.pdf", ["x"])
    failed = api.services.inbox.create("mail", [failed_source], move=False)
    failed.error = {"stage": "ocr_done", "message": "OCR fehlgeschlagen", "at": now_iso()}
    api.services.inbox.save(failed)
    api.services.inbox.move(failed, "fehler")

    data = api.client.get("/api/v1/inbox").json()
    assert data["total"] == 2
    assert data["counts"]["bereit"] == 1 and data["counts"]["fehler"] == 1
    by_id = {entry["id"]: entry for entry in data["items"]}
    assert by_id[item.id]["state"] == "bereit"
    assert by_id[item.id]["display_name"] == "Rechnung März.pdf"
    assert by_id[item.id]["top_suggestion"]["folder"] == "Wohnung/Strom"
    assert by_id[item.id]["has_document"] is True
    assert by_id[failed.id]["error"]["message"] == "OCR fehlgeschlagen"

    only_ready = api.client.get("/api/v1/inbox?state=bereit").json()
    assert [entry["id"] for entry in only_ready["items"]] == [item.id]
    assert api.client.get("/api/v1/inbox?state=erledigt").json()["total"] == 0
    assert api.client.get("/api/v1/inbox?state=all&limit=1").json()["total"] == 2
    assert api.client.get("/api/v1/inbox?state=kaputt").status_code == 400

    detail = api.client.get(f"/api/v1/inbox/{item.id}").json()
    assert detail["id"] == item.id and detail["state"] == "bereit"
    assert detail["suggestions"][1]["folder"] == "Finanzen"
    assert "Bereit zur Ablage" in detail["log"]
    assert detail["original_names"] == ["Rechnung März.pdf"]


@pytest.mark.parametrize("item_id", [ITEM_PLACEHOLDER, "kein-id", "..%2F..%2Fetc", "..", "%2E%2E"])
def test_inbox_item_not_found(api, item_id):
    response = api.client.get(f"/api/v1/inbox/{item_id}")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    if item_id in (ITEM_PLACEHOLDER, "kein-id"):
        assert response.json()["detail"] == "Eingangsdokument nicht gefunden."


def test_file_with_explicit_values(api, samples, tmp_path):
    item = ready_item(api.services, samples, tmp_path)
    response = api.client.post(
        f"/api/v1/inbox/{item.id}/file",
        json={"folder": "Wohnung/Strom", "title": "Strom März", "doc_date": "2026-03-02", "tags": "Strom, 2026", "sender": "SW", "create_rule": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["document"]["title"] == "Strom März"
    name, item_id, request, user = api.services.ops.calls[-1]
    assert (name, item_id, user) == ("file_item", item.id, "anna")
    assert request.folder == "Wohnung/Strom" and request.title == "Strom März"
    assert request.doc_date == date(2026, 3, 2)
    assert request.tags == ["Strom", "2026"] and request.sender == "SW" and request.create_rule is True


def test_file_with_suggestions(api, samples, tmp_path):
    item = ready_item(api.services, samples, tmp_path)
    assert api.client.post(f"/api/v1/inbox/{item.id}/file").status_code == 200
    request = api.services.ops.calls[-1][2]
    assert (request.folder, request.title, request.doc_date, request.tags, request.sender) == (
        "Wohnung/Strom",
        "Stromrechnung",
        date(2026, 3, 1),
        ["Strom"],
        "Stadtwerke",
    )
    assert api.client.post(f"/api/v1/inbox/{item.id}/file", json={"suggestion": 1, "title": "Eigener Titel"}).status_code == 200
    request = api.services.ops.calls[-1][2]
    assert (request.folder, request.title, request.doc_date) == ("Finanzen", "Eigener Titel", None)
    assert api.client.post(f"/api/v1/inbox/{item.id}/file", json={"suggestion": 7}).status_code == 400


def test_file_validation_errors(api, samples, tmp_path):
    item = ready_item(api.services, samples, tmp_path, suggestions=[])
    response = api.client.post(f"/api/v1/inbox/{item.id}/file", json={"title": "Ohne Ordner"})
    assert response.status_code == 400 and "Zielordner" in response.json()["detail"]
    response = api.client.post(f"/api/v1/inbox/{item.id}/file", json={"folder": "A", "title": "T", "doc_date": "31.02.2026"})
    assert response.status_code == 400 and "Datum" in response.json()["detail"]
    response = api.client.post(f"/api/v1/inbox/{item.id}/file", json={"folder": "A", "title": "  "})
    assert response.status_code == 400
    response = api.client.post(f"/api/v1/inbox/{item.id}/file", json={"folder": ["A"], "title": "T"})
    assert response.status_code == 400
    response = api.client.post(f"/api/v1/inbox/{item.id}/file", content=b"{kaputt", headers={"Content-Type": "application/json"})
    assert response.status_code == 400 and "JSON" in response.json()["detail"]
    response = api.client.post(f"/api/v1/inbox/{item.id}/file", content=b"[1]", headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    response = api.client.post(f"/api/v1/inbox/{item.id}/file", content=b"folder=A", headers={"Content-Type": "text/plain"})
    assert response.status_code == 415
    assert api.services.ops.calls == []


@pytest.mark.parametrize(
    "error,status",
    [
        (Conflict("Für dieses Dokument wurde eine Ablage unterbrochen."), 409),
        (PathError("Ungültiger Ordnername"), 400),
        (ProcessingError("Das verarbeitete Dokument fehlt."), 422),
        (NotFound("Eingangsdokument nicht gefunden."), 404),
        (ValueError("Ungültiger Wert"), 400),
    ],
)
def test_errors_from_collaborators_are_mapped(api, samples, tmp_path, error, status):
    item = ready_item(api.services, samples, tmp_path)
    api.services.ops.fail["file_item"] = error
    response = api.client.post(f"/api/v1/inbox/{item.id}/file")
    assert response.status_code == status
    assert response.json() == {"detail": str(error)}


def test_file_batch(api, samples, tmp_path):
    good = ready_item(api.services, samples, tmp_path / "a")
    without = ready_item(api.services, samples, tmp_path / "b", suggestions=[])
    conflict = ready_item(api.services, samples, tmp_path / "c")
    original_file_item = api.services.ops.file_item

    def file_item(item_id, request, *, user=None):
        if item_id == conflict.id:
            raise Conflict("Das Dokument wurde bereits abgelegt")
        return original_file_item(item_id, request, user=user)

    api.services.ops.file_item = file_item
    response = api.client.post(
        "/api/v1/inbox/file-batch",
        json={"items": [good.id, without.id, {"id": conflict.id}, good.id, ITEM_PLACEHOLDER]},
    )
    assert response.status_code == 200
    body = response.json()
    assert [entry["item"] for entry in body["filed"]] == [good.id]
    assert body["filed"][0]["document"]["title"] == "Stromrechnung"
    errors = {entry["item"]: entry["error"] for entry in body["failed"]}
    assert errors == {
        without.id: "Für dieses Dokument gibt es keinen Ablagevorschlag.",
        conflict.id: "Das Dokument wurde bereits abgelegt",
        ITEM_PLACEHOLDER: "Eingangsdokument nicht gefunden.",
    }
    assert (body["filed_count"], body["failed_count"]) == (1, 3)
    assert api.client.post("/api/v1/inbox/file-batch", json={"items": []}).status_code == 400
    assert api.client.post("/api/v1/inbox/file-batch", json={"items": [5]}).status_code == 400


def test_retry_refresh_and_delete(api, samples, tmp_path):
    item = ready_item(api.services, samples, tmp_path)
    response = api.client.post(f"/api/v1/inbox/{item.id}/retry", json={"skip_ocr": True})
    assert response.status_code == 200 and response.json()["id"] == item.id
    assert api.services.pipeline.calls[-1] == ("retry", item.id, None, True)
    api.client.post(f"/api/v1/inbox/{item.id}/retry")
    assert api.services.pipeline.calls[-1] == ("retry", item.id, None, None)
    assert api.client.post(f"/api/v1/inbox/{item.id}/retry", json={"skip_ocr": "vielleicht"}).status_code == 400
    refreshed = api.client.post(f"/api/v1/inbox/{item.id}/refresh").json()
    assert refreshed["suggestions"] and api.services.pipeline.calls[-1] == ("refresh", item.id)
    deleted = api.client.delete(f"/api/v1/inbox/{item.id}").json()
    assert deleted == {"item": item.id, "trash_id": "20261123-130001-bbbbbb"}
    assert api.services.ops.calls[-1] == ("delete_inbox_item", item.id, "anna")
    assert api.client.post(f"/api/v1/inbox/{ITEM_PLACEHOLDER}/retry").status_code == 404


def test_inbox_document_original_and_preview(api, samples, tmp_path):
    item = ready_item(api.services, samples, tmp_path)
    document = api.client.get(f"/api/v1/inbox/{item.id}/document")
    assert document.status_code == 200
    assert document.headers["content-type"] == "application/pdf"
    assert document.content == item.document_path.read_bytes()
    disposition = document.headers["content-disposition"]
    assert disposition.startswith("inline; ")
    assert "filename*=UTF-8''Rechnung%20M%C3%A4rz.pdf" in disposition
    assert 'filename="Rechnung Maerz.pdf"' in disposition
    download = api.client.get(f"/api/v1/inbox/{item.id}/document?download=1")
    assert download.headers["content-disposition"].startswith("attachment; ")

    original = api.client.get(f"/api/v1/inbox/{item.id}/original/1")
    assert original.status_code == 200 and original.content == (item.dir / item.original_files[0]).read_bytes()
    assert api.client.get(f"/api/v1/inbox/{item.id}/original/0").status_code == 404
    assert api.client.get(f"/api/v1/inbox/{item.id}/original/2").status_code == 404

    preview = api.client.get(f"/api/v1/inbox/{item.id}/pages/1.png?w=120")
    assert preview.status_code == 200
    assert preview.content.startswith(PNG_MAGIC)
    assert preview.headers["content-type"] == "image/png"
    assert preview.headers["cache-control"].startswith("private")
    again = api.client.get(f"/api/v1/inbox/{item.id}/pages/1.png?w=120", headers={"If-None-Match": preview.headers["etag"]})
    assert again.status_code == 304
    assert api.client.get(f"/api/v1/inbox/{item.id}/pages/9.png").status_code == 404
    assert api.client.get(f"/api/v1/inbox/{item.id}/pages/0.png").status_code == 404


def test_preview_falls_back_to_the_original_pdf(api, samples, tmp_path):
    item = ready_item(api.services, samples, tmp_path, document=False)
    assert api.client.get(f"/api/v1/inbox/{item.id}/document").status_code == 404
    preview = api.client.get(f"/api/v1/inbox/{item.id}/pages/1.png")
    assert preview.status_code == 200 and preview.content.startswith(PNG_MAGIC)


def test_unsafe_originals_are_downloaded_not_shown(api, tmp_path):
    page = tmp_path / "seite.html"
    page.write_text("<script>alert(1)</script>", encoding="utf-8")
    item = api.services.inbox.create("mail", [page], move=False)
    response = api.client.get(f"/api/v1/inbox/{item.id}/original/1")
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment; ")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert api.client.get(f"/api/v1/inbox/{item.id}/pages/1.png").status_code == 404


def test_original_paths_cannot_leave_the_item(api, tmp_path):
    secret = tmp_path / "geheim.txt"
    secret.write_text("geheim", encoding="utf-8")
    source = tmp_path / "a.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    item = api.services.inbox.create("upload", [source], move=False)
    item.original_files = ["../../../../geheim.txt"]
    api.services.inbox.save(item)
    response = api.client.get(f"/api/v1/inbox/{item.id}/original/1")
    assert response.status_code == 400
    assert "geheim" not in response.text


# ---------------------------------------------------------------------------
# Search


def test_search_merges_inline_and_explicit_filters(api):
    response = api.client.get(
        "/api/v1/search",
        params=[("q", "Strom von:Stadtwerke"), ("folder", "Wohnung/"), ("from", "2025-12-31"), ("to", "2025-01-01"), ("tag", "Garantie"), ("tag", "Strom"), ("limit", "10"), ("offset", "5")],
    )
    assert response.status_code == 200, response.text
    query = api.services.searcher.queries[-1]
    assert query.text == "Strom" and query.sender == "Stadtwerke"
    assert query.folder == "Wohnung"
    assert (query.date_from, query.date_to) == (date(2025, 1, 1), date(2025, 12, 31))
    assert query.tags == ["Garantie", "Strom"]
    assert (query.limit, query.offset) == (10, 5)
    body = response.json()
    assert body["total"] == 1 and body["hits"][0]["snippets"][0]["html"] == "<mark>Strom</mark>"
    assert body["corrections"] == {"strm": ["strom"]}


def test_search_validation(api):
    assert api.client.get("/api/v1/search?from=gestern").status_code == 400
    assert api.client.get("/api/v1/search?limit=0").status_code == 422
    assert api.client.get("/api/v1/search?folder=../x").status_code == 400


def test_suggest(api):
    assert api.client.get("/api/v1/suggest?prefix=Strom&limit=1").json() == {"prefix": "Strom", "terms": ["Stromrechnung"]}


# ---------------------------------------------------------------------------
# Documents


def test_document_get_file_and_pages(api, samples):
    record = archived_pdf(api.services, samples)
    data = api.client.get(f"/api/v1/documents/{record.id}").json()
    assert data["title"] == "Kontoauszug März" and data["doc_date"] == "2026-01-05"
    assert "pages" not in data
    assert api.client.get(f"/api/v1/documents/{record.id}?text=true").json()["pages"] == ["Kontoauszug Seite 1", "Seite 2"]

    file = api.client.get(f"/api/v1/documents/{record.id}/file")
    assert file.status_code == 200 and file.headers["content-type"] == "application/pdf"
    assert file.content == (api.services.layout.archive / record.path).read_bytes()
    assert unquote(file.headers["content-disposition"].split("filename*=UTF-8''")[1]) == record.filename

    page = api.client.get(f"/api/v1/documents/{record.id}/pages/2.png")
    assert page.status_code == 200 and page.content.startswith(PNG_MAGIC)
    assert page.headers["cache-control"] == "private, no-cache"
    versioned = api.client.get(f"/api/v1/documents/{record.id}/pages/2.png?v={record.content_hash}")
    assert "immutable" in versioned.headers["cache-control"]
    assert api.client.get(f"/api/v1/documents/{record.id}/pages/3.png").status_code == 404
    assert api.client.get("/api/v1/documents/gibt-es-nicht").json() == {"detail": "Dokument nicht gefunden."}


def test_document_file_missing_on_disk_triggers_a_scan(api, samples):
    record = archived_pdf(api.services, samples)
    (api.services.layout.archive / record.path).unlink()
    response = api.client.get(f"/api/v1/documents/{record.id}/file")
    assert response.status_code == 404
    assert api.services.watcher.reasons


def test_non_pdf_documents(api):
    path = api.services.layout.archive / "Fotos" / "Quittung.html"
    path.parent.mkdir(parents=True)
    path.write_text("<h1>x</h1>", encoding="utf-8")
    record = make_record(doc_id="55555555-2222-4333-8444-555555555555", path="Fotos/Quittung.html", text_status="unsupported")
    api.services.index.upsert(record, [])
    response = api.client.get(f"/api/v1/documents/{record.id}/file")
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment")
    assert api.client.get(f"/api/v1/documents/{record.id}/pages/1.png").status_code == 404


def test_document_update_and_delete(api, samples):
    record = archived_pdf(api.services, samples)
    response = api.client.patch(
        f"/api/v1/documents/{record.id}",
        json={"title": "Neuer Titel", "doc_date": None, "tags": ["a", "b"], "sender": None, "folder": "Finanzen"},
    )
    assert response.status_code == 200
    assert response.json()["id"] == "22222222-2222-4333-8444-555555555555"  # the id returned by ops wins
    _name, doc_id, update, user = api.services.ops.calls[-1]
    assert (doc_id, user) == (record.id, "anna")
    assert update.title == "Neuer Titel" and update.clear_date is True and update.doc_date is None
    assert update.tags == ["a", "b"] and update.sender == "" and update.folder == "Finanzen"

    api.client.patch(f"/api/v1/documents/{record.id}", json={"date": "2026-02-01"})
    update = api.services.ops.calls[-1][2]
    assert update.doc_date == date(2026, 2, 1) and update.title is None and update.tags is None and update.sender is None
    assert api.client.patch(f"/api/v1/documents/{record.id}", json={"doc_date": "morgen"}).status_code == 400
    assert api.client.patch("/api/v1/documents/unbekannt", json={"title": "x"}).status_code == 404

    deleted = api.client.delete(f"/api/v1/documents/{record.id}").json()
    assert deleted == {"document": record.id, "trash_id": "20261123-130000-aaaaaa"}


# ---------------------------------------------------------------------------
# Folders


def test_folders(api, samples):
    archived_pdf(api.services, samples)
    api.services.index.add_folder_with_parents("Wohnung/Strom")
    data = api.client.get("/api/v1/folders").json()
    paths = {entry["path"]: entry for entry in data["folders"]}
    assert set(paths) == {"Finanzen", "Finanzen/Bank", "Wohnung", "Wohnung/Strom"}
    assert paths["Finanzen/Bank"]["documents"] == 1 and paths["Finanzen/Bank"]["parent"] == "Finanzen"
    children = api.client.get("/api/v1/folders?parent=Wohnung").json()["folders"]
    assert [entry["path"] for entry in children] == ["Wohnung/Strom"]
    assert api.client.get("/api/v1/folders?parent=../x").status_code == 400

    assert api.client.post("/api/v1/folders", json={"path": "Versicherungen/Haftpflicht"}).json() == {"path": "Versicherungen/Haftpflicht"}
    assert api.client.post("/api/v1/folders", json={}).status_code == 400
    renamed = api.client.patch("/api/v1/folders", json={"path": "Wohnung/Strom", "name": "Energie"}).json()
    assert renamed == {"path": "Wohnung/Energie", "old_path": "Wohnung/Strom"}
    assert api.client.delete("/api/v1/folders?path=Wohnung/Energie").json() == {"path": "Wohnung/Energie", "deleted": True}
    assert api.client.request("DELETE", "/api/v1/folders", json={"path": "Alt"}).json()["path"] == "Alt"
    assert api.client.delete("/api/v1/folders").status_code == 400
    assert api.client.delete("/api/v1/folders?path=../../x").status_code == 400
    api.services.ops.fail["delete_folder"] = Conflict("Der Ordner „Alt“ ist nicht leer.")
    response = api.client.delete("/api/v1/folders?path=Alt")
    assert response.status_code == 409 and response.json()["detail"] == "Der Ordner „Alt“ ist nicht leer."
    names = [call[0] for call in api.services.ops.calls]
    assert names == ["create_folder", "rename_folder", "delete_folder", "delete_folder", "delete_folder"]


# ---------------------------------------------------------------------------
# Trash


def test_trash(api, samples, tmp_path):
    items = api.client.get("/api/v1/trash").json()["items"]
    assert items == [
        {
            "id": "20261120-100000-cccccc",
            "kind": "document",
            "deleted_at": "2026-11-20T10:00:00+01:00",
            "deleted_by": "anna",
            "title": "Alte Rechnung",
            "original_path": "Finanzen/2025-01-01 Alte Rechnung.pdf",
            "folder": "Finanzen",
            "item_id": None,
            "doc_id": "33333333-2222-4333-8444-555555555555",
            "files": ["2025-01-01 Alte Rechnung.pdf"],
            "expires_at": "2026-12-20T10:00:00",
            "meta": {"sender": "Stadtwerke"},
        }
    ]
    restored = api.client.post("/api/v1/trash/20261120-100000-cccccc/restore").json()
    assert restored["kind"] == "document" and restored["document"]["id"] == make_record().id
    api.services.trash.restore_result = ready_item(api.services, samples, tmp_path)
    restored = api.client.post("/api/v1/trash/20261120-100000-cccccc/restore").json()
    assert restored["kind"] == "inbox" and restored["item"]["state"] == "bereit"
    # Deleting for good needs a browser login; an API token can only move things into the trash.
    refused = api.client.delete("/api/v1/trash/20261120-100000-cccccc")
    assert refused.status_code == 403 and "Browser" in refused.json()["detail"]
    assert not any(call[0] == "purge" for call in api.services.trash.calls)
    browser = client_for(api.app)
    login_local(browser)
    csrf = {"X-CSRF-Token": session_data(browser)["csrf"]}
    purged = browser.delete("/api/v1/trash/20261120-100000-cccccc", headers=csrf)
    assert purged.json() == {"id": "20261120-100000-cccccc", "purged": True}
    assert browser.delete("/api/v1/trash/20990101-000000-ffffff", headers=csrf).status_code == 404
    assert api.services.trash.calls[-1] == ("purge", "20990101-000000-ffffff", "admin")


# ---------------------------------------------------------------------------
# Rules


VALID_RULES = """\
rules:
  - name: Stadtwerke Strom
    match:
      sender: [Stadtwerke]
    action:
      folder: Wohnung/Strom
"""


def test_rules_read_and_write(api):
    data = api.client.get("/api/v1/rules").json()
    assert data["rules"] == [] and data["error"] is None and isinstance(data["text"], str)
    saved = api.client.put("/api/v1/rules", json={"text": VALID_RULES})
    assert saved.status_code == 200, saved.text
    assert saved.json()["rules"][0]["name"] == "Stadtwerke Strom"
    assert api.services.layout.rules_file.read_text(encoding="utf-8") == VALID_RULES
    assert api.services.audit.tail(1)[0]["action"] == "rules_update"

    broken = api.client.put("/api/v1/rules", json={"text": "rules:\n  - name: [kaputt\n"})
    assert broken.status_code == 400
    assert broken.json()["detail"]
    assert api.services.layout.rules_file.read_text(encoding="utf-8") == VALID_RULES

    as_text = api.client.put("/api/v1/rules", content=VALID_RULES.replace("Strom", "Gas").encode(), headers={"Content-Type": "text/yaml"})
    assert as_text.status_code == 200 and as_text.json()["rules"][0]["action"]["folder"] == "Wohnung/Gas"
    assert api.client.put("/api/v1/rules", json={"rules": []}).status_code == 400
    assert api.client.put("/api/v1/rules", data={"text": VALID_RULES}).status_code == 415


# ---------------------------------------------------------------------------
# Maintenance


def test_reconcile(api):
    queued = api.client.post("/api/v1/admin/reconcile")
    assert queued.status_code == 202 and queued.json() == {"queued": True}
    assert api.services.watcher.reasons and "anna" in api.services.watcher.reasons[-1]
    waited = api.client.post("/api/v1/admin/reconcile?wait=true&verify=true")
    assert waited.status_code == 200 and waited.json()["result"]["files_seen"] == 2
    assert api.services.reconciler.calls[-1] == {"full": True, "verify": True}
    started = api.client.post("/api/v1/admin/reconcile?verify=true")
    assert started.status_code == 202
    jobs = api.app.state.background_jobs
    for thread in list(jobs._threads.values()):
        thread.join(5)
    assert api.client.get("/api/v1/status").json()["jobs"]["reconcile"]["result"]["files_seen"] == 2


def test_rebuild(api):
    waited = api.client.post("/api/v1/admin/rebuild?wait=true")
    assert waited.status_code == 200 and waited.json()["result"]["added"] == 3
    assert api.services.rebuild.calls == ["anna"]

    gate = threading.Event()
    api.services.rebuild.gate = gate
    try:
        assert api.client.post("/api/v1/admin/rebuild").status_code == 202
        busy = api.client.post("/api/v1/admin/rebuild")
        assert busy.status_code == 409 and "bereits" in busy.json()["detail"]
        assert api.client.get("/api/v1/status").json()["jobs"]["rebuild"]["running"] is True
    finally:
        gate.set()
    api.app.state.background_jobs.stop(timeout=5)
    status = api.client.get("/api/v1/status").json()["jobs"]["rebuild"]
    assert status["running"] is False and status["result"]["added"] == 3 and status["error"] is None


# ---------------------------------------------------------------------------
# Tokens and audit


def test_tokens_need_a_browser_session_to_change(api):
    listed = api.client.get("/api/v1/tokens").json()
    assert [entry["name"] for entry in listed["tokens"]] == ["Tests"]
    assert all("hash" not in entry for entry in listed["tokens"])
    assert api.client.post("/api/v1/tokens", json={"name": "Neu"}).status_code == 403
    first_id = listed["tokens"][0]["id"]
    assert api.client.delete(f"/api/v1/tokens/{first_id}").status_code == 403

    browser = client_for(api.app)
    login_local(browser)
    csrf = {"X-CSRF-Token": session_data(browser)["csrf"]}
    created = browser.post("/api/v1/tokens", json={"name": "iPhone"}, headers=csrf)
    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    token = created.json()["token"]
    entry = created.json()["entry"]
    assert entry["user"] == "admin" and entry["prefix"] == token[:12]
    assert client_for(api.app).get("/api/v1/status", headers={"Authorization": f"Bearer {token}"}).json()["user"]["username"] == "admin"
    assert browser.post("/api/v1/tokens", json={"name": " "}, headers=csrf).status_code == 400
    revoked = browser.delete(f"/api/v1/tokens/{entry['id']}", headers=csrf)
    assert revoked.status_code == 200 and revoked.json()["revoked"] is True
    assert client_for(api.app).get("/api/v1/status", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert browser.delete(f"/api/v1/tokens/{entry['id']}", headers=csrf).status_code == 404
    actions = [line["action"] for line in api.services.audit.tail(10)]
    assert "token_create" in actions and "token_revoke" in actions


def test_audit(api):
    for number in range(5):
        api.services.audit.write("anna", "file", number=number)
    entries = api.client.get("/api/v1/audit?limit=3").json()["entries"]
    assert [entry["number"] for entry in entries] == [4, 3, 2]
    assert api.client.get("/api/v1/audit?limit=0").status_code == 422


def test_json_body_limits(api, samples, tmp_path):
    item = ready_item(api.services, samples, tmp_path)
    huge = json.dumps({"folder": "A", "title": "x" * (3 * 1024 * 1024)})
    response = api.client.post(f"/api/v1/inbox/{item.id}/file", content=huge, headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert api.services.ops.calls == []


# ---------------------------------------------------------------------------
# Helpers


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Rechnung.pdf", "inline; filename=\"Rechnung.pdf\"; filename*=UTF-8''Rechnung.pdf"),
        ("2026-01-05 Grüße.pdf", "inline; filename=\"2026-01-05 Gruesse.pdf\"; filename*=UTF-8''2026-01-05%20Gr%C3%BC%C3%9Fe.pdf"),
        ("Café Ärger.PDF", "inline; filename=\"Cafe Aerger.PDF\"; filename*=UTF-8''Caf%C3%A9%20%C3%84rger.PDF"),
        ("日本", "inline; filename=\"dokument\"; filename*=UTF-8''%E6%97%A5%E6%9C%AC"),
        (".pdf", "inline; filename=\"dokument.pdf\"; filename*=UTF-8''.pdf"),
        ('a"b;c%.pdf', "inline; filename=\"a_b_c_.pdf\"; filename*=UTF-8''a%22b%3Bc%25.pdf"),
        ("日本.pdf", "inline; filename=\"dokument.pdf\"; filename*=UTF-8''%E6%97%A5%E6%9C%AC.pdf"),
        ("x\r\ny.pdf", "inline; filename=\"xy.pdf\"; filename*=UTF-8''xy.pdf"),
    ],
)
def test_content_disposition(name, expected):
    assert content_disposition("inline", name) == expected


# ---------------------------------------------------------------------------
# End to end with the real service container


def test_end_to_end_with_real_services(services, samples, tmp_path):
    app = make_app(services)
    client = client_for(app)
    assert login_local(client).status_code == 303
    csrf = {"X-CSRF-Token": session_data(client)["csrf"]}

    pdf = samples.text_pdf(
        tmp_path / "Stromrechnung.pdf",
        ["Stadtwerke Musterstadt GmbH\nRechnung\nRechnungsdatum: 05.01.2026\nKundennummer 4711\nGesamtbetrag 123,45 EUR"],
    )
    with open(pdf, "rb") as handle:
        uploaded = client.post("/api/v1/upload", files={"files": ("Stromrechnung.pdf", handle, "application/pdf")}, headers=csrf)
    assert uploaded.status_code == 202, uploaded.text

    created = services.intake.scan_once()
    assert len(created) == 1
    item_id = created[0]
    services.pipeline.process(item_id)
    listed = client.get("/api/v1/inbox?state=bereit").json()
    assert [entry["id"] for entry in listed["items"]] == [item_id]
    assert listed["items"][0]["has_document"] is True
    assert client.get(f"/api/v1/inbox/{item_id}/pages/1.png").content.startswith(PNG_MAGIC)

    filed = client.post(
        f"/api/v1/inbox/{item_id}/file",
        json={"folder": "Wohnung/Strom", "title": "Stromrechnung Januar", "doc_date": "2026-01-05", "tags": ["Strom"]},
        headers=csrf,
    )
    assert filed.status_code == 200, filed.text
    document = filed.json()["document"]
    archived = services.layout.archive / "Wohnung" / "Strom" / "2026-01-05 Stromrechnung Januar.pdf"
    assert archived.is_file()
    assert document["path"] == "Wohnung/Strom/2026-01-05 Stromrechnung Januar.pdf"

    detail = client.get(f"/api/v1/documents/{document['id']}?text=true").json()
    assert detail["tags"] == ["Strom"] and "Stadtwerke" in detail["pages"][0]
    file = client.get(f"/api/v1/documents/{document['id']}/file")
    assert file.content == archived.read_bytes()
    assert client.get(f"/api/v1/documents/{document['id']}/pages/1.png").content.startswith(PNG_MAGIC)
    found = client.get("/api/v1/search", params={"q": "Stadtwerke"}).json()
    assert found["total"] >= 1 and found["hits"][0]["doc"]["id"] == document["id"]
    folders = {entry["path"] for entry in client.get("/api/v1/folders").json()["folders"]}
    assert {"Wohnung", "Wohnung/Strom"} <= folders

    renamed = client.patch(f"/api/v1/documents/{document['id']}", json={"title": "Strom Januar"}, headers=csrf)
    assert renamed.status_code == 200, renamed.text
    assert (archived.parent / "2026-01-05 Strom Januar.pdf").is_file() and not archived.exists()

    trash_id = client.delete(f"/api/v1/documents/{renamed.json()['id']}", headers=csrf).json()["trash_id"]
    assert not (archived.parent / "2026-01-05 Strom Januar.pdf").exists()
    assert [entry["id"] for entry in client.get("/api/v1/trash").json()["items"]] == [trash_id]
    restored = client.post(f"/api/v1/trash/{trash_id}/restore", headers=csrf).json()
    assert restored["kind"] == "document"
    assert (archived.parent / "2026-01-05 Strom Januar.pdf").is_file()

    assert client.get("/healthz").json()["status"] == "ok"
    actions = {entry["action"] for entry in client.get("/api/v1/audit").json()["entries"]}
    assert {"login", "upload"} <= actions


def test_app_lifespan_starts_and_stops_services(settings):
    from starlette.testclient import TestClient

    from docushelf.web.app import create_app

    app = create_app(settings.with_overrides(start_background=True))
    services = app.state.services
    with TestClient(app) as client:
        assert services.worker.running
        assert client.get("/healthz").status_code == 200
    assert not services.worker.running
