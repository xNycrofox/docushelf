"""Tests for the language model client (Ollama and OpenAI-compatible servers, mocked with respx)."""

from __future__ import annotations

import json
from datetime import date, timedelta

import httpx
import pytest
import respx

from docushelf.config import LlmConfig
from docushelf.models import Analysis, DateCandidate
from docushelf.suggest.llm import (
    ANSWER_KEYS,
    ANSWER_SCHEMA,
    MAX_FOLDERS,
    SAMPLE_LETTER,
    SYSTEM_PROMPT,
    LlmClient,
    LlmRequest,
    SimilarDocument,
    acceptable_new_folder,
    build_prompt,
    clean_title,
    date_in_text,
    document_text,
    parse_confidence,
    resolve_folder,
    select_folders,
)
from docushelf.util import timeutil

URL = "http://ollama.test:11434"
CHAT = f"{URL}/api/chat"
OPENAI_URL = "http://llama.test:8080"
COMPLETIONS = f"{OPENAI_URL}/v1/chat/completions"
FOLDERS = ["Finanzen/Bank", "Wohnung/Strom", "Versicherungen/Haftpflicht", "Arbeit/Gehalt"]
TEXT = "Stadtwerke Leipzig GmbH\nAbschlagsrechnung Strom\nRechnungsdatum 03.02.2025\nAbschlag 85,00 €"


@pytest.fixture(autouse=True)
def no_retry_pause(monkeypatch):
    monkeypatch.setattr(LlmClient, "retry_delay", 0.0)


def config(**changes) -> LlmConfig:
    values = dict(url=URL, model="qwen2.5:3b", timeout=5, max_chars=6000)
    values.update(changes)
    return LlmConfig(**values)


def openai_config(**changes) -> LlmConfig:
    values = dict(url=OPENAI_URL, model="local-model", timeout=5, provider="openai")
    values.update(changes)
    return LlmConfig(**values)


def answer(content: dict | str) -> httpx.Response:
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return httpx.Response(200, json={"model": "qwen2.5:3b", "message": {"role": "assistant", "content": text},
                                     "done": True})


def openai_answer(content: dict | str) -> httpx.Response:
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return httpx.Response(200, json={
        "id": "chatcmpl-1", "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
    })


GOOD = {
    "dokumenttyp": "Rechnung",
    "absender": "Stadtwerke Leipzig GmbH",
    "datum": "2025-02-03",
    "titel": "Abschlagsrechnung Strom Stadtwerke",
    "begruendung": "Stromabschlag der Stadtwerke gehört zu den Stromunterlagen.",
    "ordner": "Wohnung/Strom",
    "neuer_ordner": False,
    "sicherheit": 0.8,
    "tags": ["Strom", "Abschlag"],
}


def body(route) -> dict:
    return json.loads(route.calls.last.request.content)


def user_prompt(route) -> str:
    return body(route)["messages"][1]["content"]


def request(**changes) -> LlmRequest:
    values = dict(text=TEXT, folders=list(FOLDERS))
    values.update(changes)
    return LlmRequest(**values)


# ---------------------------------------------------------------------------
# Ollama


@respx.mock
def test_ollama_request_uses_the_json_schema_and_a_large_context():
    route = respx.post(CHAT).mock(return_value=answer(GOOD))
    suggestion = LlmClient(config(context_tokens=12288)).suggest(TEXT, FOLDERS, {"mail_subject": "Ihre Abschlagsrechnung"})
    assert suggestion is not None
    sent = body(route)
    assert sent["model"] == "qwen2.5:3b" and sent["stream"] is False
    assert sent["format"] == ANSWER_SCHEMA
    assert sent["options"] == {"temperature": 0, "num_ctx": 12288}
    assert sent["think"] is False
    assert sent["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    user = sent["messages"][1]["content"]
    assert "- Wohnung/Strom (0)" in user and "Abschlagsrechnung Strom" in user
    assert "Betreff der E-Mail: Ihre Abschlagsrechnung" in user
    assert "authorization" not in route.calls.last.request.headers


def test_answer_schema_lists_every_key_as_required():
    assert set(ANSWER_SCHEMA["properties"]) == set(ANSWER_KEYS) == {
        "ordner", "neuer_ordner", "titel", "datum", "absender", "dokumenttyp", "tags", "sicherheit", "begruendung"}
    assert ANSWER_SCHEMA["required"] == list(ANSWER_KEYS)
    assert ANSWER_SCHEMA["additionalProperties"] is False
    assert ANSWER_SCHEMA["properties"]["datum"]["type"] == ["string", "null"]


@respx.mock
def test_valid_answer_becomes_a_suggestion():
    respx.post(CHAT).mock(return_value=answer(GOOD))
    result = LlmClient(config()).classify(request())
    suggestion = result.suggestion
    assert result.error is None and result.answer == GOOD and result.confidence == 0.8
    assert suggestion.folder == "Wohnung/Strom" and suggestion.new_folder is False
    assert suggestion.title == "Abschlagsrechnung Strom Stadtwerke"
    assert suggestion.doc_date == "2025-02-03"
    assert suggestion.sender == "Stadtwerke Leipzig GmbH"
    assert suggestion.tags == ["Strom", "Abschlag"]
    assert suggestion.source == "llm"
    # The model's self-assessment is not trusted fully.
    assert suggestion.score == pytest.approx(0.72)
    assert suggestion.reasons == ["Stromabschlag der Stadtwerke gehört zu den Stromunterlagen."]


@respx.mock
def test_ollama_falls_back_to_plain_json_mode_and_remembers_it():
    route = respx.post(CHAT).mock(side_effect=[
        httpx.Response(400, json={"error": "invalid format: expected \"json\""}),
        answer(GOOD),
        answer(GOOD),
    ])
    client = LlmClient(config())
    assert client.suggest(TEXT, FOLDERS).folder == "Wohnung/Strom"
    first, second = (json.loads(call.request.content) for call in route.calls)
    assert first["format"] == ANSWER_SCHEMA
    assert second["format"] == "json" and "think" not in second
    assert second["options"]["temperature"] == 0
    # The next request starts with the format that worked.
    assert client.suggest(TEXT, FOLDERS).folder == "Wohnung/Strom"
    assert route.call_count == 3 and body(route)["format"] == "json"


@respx.mock
def test_rejected_fallback_gives_a_german_error():
    respx.post(CHAT).mock(return_value=httpx.Response(400, json={"error": "kaputte Anfrage"}))
    result = LlmClient(config()).classify(request())
    assert result.suggestion is None
    assert "Fehler 400" in result.error and "kaputte Anfrage" in result.error


@respx.mock
@pytest.mark.parametrize(
    "response,fragment",
    [
        (httpx.Response(500, text="model crashed"), "Fehler 500: model crashed"),
        (httpx.Response(404, json={"error": "model 'qwen2.5:3b' not found"}), "Modell „qwen2.5:3b“"),
        (httpx.Response(401, json={"error": "unauthorized"}), "API-Schlüssel"),
        (httpx.Response(200, text="kein json"), "kein JSON"),
        (httpx.Response(200, json={"message": {"content": "leider weiß ich das nicht"}}), "kein JSON-Objekt"),
        (httpx.Response(200, json={"message": {"content": "[1, 2, 3]"}}), "kein JSON-Objekt"),
        (httpx.Response(200, json={"message": {"content": ""}}), "leer"),
        (httpx.Response(200, json=["unexpected"]), "unerwartetes Format"),
    ],
)
def test_errors_mean_no_suggestion(response, fragment):
    respx.post(CHAT).mock(return_value=response)
    result = LlmClient(config()).classify(request())
    assert result.suggestion is None
    assert fragment in result.error


@respx.mock
def test_timeout_is_not_retried():
    route = respx.post(CHAT).mock(side_effect=httpx.ReadTimeout("too slow"))
    client = LlmClient(config(timeout=7))
    result = client.classify(request())
    assert result.suggestion is None and "7 s" in result.error
    assert route.call_count == 1
    assert client.last_status["reachable"] is True and client.last_status["ok"] is False


@respx.mock
def test_connection_errors_are_retried_once():
    route = respx.post(CHAT).mock(side_effect=[httpx.ConnectError("refused"), answer(GOOD)])
    assert LlmClient(config()).suggest(TEXT, FOLDERS).folder == "Wohnung/Strom"
    assert route.call_count == 2

    route = respx.post(CHAT).mock(side_effect=httpx.ConnectError("refused"))
    route.calls.clear()
    client = LlmClient(config())
    result = client.classify(request())
    assert result.suggestion is None and route.call_count == 2
    assert result.error == "Keine Verbindung zum Sprachmodell unter ollama.test:11434."
    assert client.last_status["reachable"] is False


@respx.mock
def test_empty_text_sends_nothing():
    route = respx.post(CHAT).mock(return_value=answer(GOOD))
    client = LlmClient(config())
    result = client.classify(request(text="   "))
    assert result.suggestion is None and "keinen Text" in result.error
    assert not route.called and client.last_status is None


def test_configured_url_is_used_with_trailing_slash():
    assert LlmClient(config(url=URL + "/")).endpoint == CHAT
    assert LlmClient(config()).host == "ollama.test:11434"


@respx.mock
def test_api_key_is_sent_as_bearer_token_and_never_shown():
    route = respx.post(CHAT).mock(return_value=answer(GOOD))
    cfg = config(api_key="geheim-123")
    LlmClient(cfg).suggest(TEXT, FOLDERS)
    assert route.calls.last.request.headers["authorization"] == "Bearer geheim-123"
    assert "geheim-123" not in repr(cfg)


# ---------------------------------------------------------------------------
# OpenAI-compatible servers


@pytest.mark.parametrize(
    "url,endpoint",
    [
        ("http://llama.test:8080", "http://llama.test:8080/v1/chat/completions"),
        ("http://llama.test:8080/", "http://llama.test:8080/v1/chat/completions"),
        ("http://llama.test:8080/v1", "http://llama.test:8080/v1/chat/completions"),
        ("http://llama.test:8080/v1/", "http://llama.test:8080/v1/chat/completions"),
        ("http://gpu.test/api/v1/chat/completions", "http://gpu.test/api/v1/chat/completions"),
    ],
)
def test_openai_endpoint(url, endpoint):
    assert LlmClient(openai_config(url=url)).endpoint == endpoint


@respx.mock
def test_openai_request_uses_strict_json_schema():
    route = respx.post(COMPLETIONS).mock(return_value=openai_answer(GOOD))
    suggestion = LlmClient(openai_config()).suggest(TEXT, FOLDERS)
    assert suggestion.folder == "Wohnung/Strom" and suggestion.score == pytest.approx(0.72)
    sent = body(route)
    assert sent["model"] == "local-model" and sent["temperature"] == 0 and sent["stream"] is False
    assert sent["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "ablage", "schema": ANSWER_SCHEMA, "strict": True},
    }
    assert sent["messages"][0]["content"] == SYSTEM_PROMPT
    assert "num_ctx" not in json.dumps(sent) and "format" not in sent
    assert "authorization" not in route.calls.last.request.headers


@respx.mock
def test_openai_sends_the_api_key():
    route = respx.post(COMPLETIONS).mock(return_value=openai_answer(GOOD))
    LlmClient(openai_config(api_key="sk-test")).suggest(TEXT, FOLDERS)
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-test"


@respx.mock
def test_openai_falls_back_to_json_object_and_then_to_plain_text():
    route = respx.post(COMPLETIONS).mock(side_effect=[
        httpx.Response(400, json={"error": {"message": "response_format json_schema not supported"}}),
        openai_answer(GOOD),
    ])
    client = LlmClient(openai_config())
    assert client.suggest(TEXT, FOLDERS).folder == "Wohnung/Strom"
    assert [json.loads(c.request.content)["response_format"]["type"] for c in route.calls] == ["json_schema",
                                                                                               "json_object"]
    route = respx.post(COMPLETIONS).mock(side_effect=[
        httpx.Response(400, json={"error": {"message": "unsupported"}}),
        openai_answer("Hier: " + json.dumps(GOOD)),
    ])
    route.calls.clear()
    assert client.suggest(TEXT, FOLDERS).folder == "Wohnung/Strom"
    # json_object is now the first try; the last fallback has no response_format at all.
    assert json.loads(route.calls[0].request.content)["response_format"] == {"type": "json_object"}
    assert "response_format" not in json.loads(route.calls[1].request.content)


@respx.mock
def test_openai_errors():
    respx.post(COMPLETIONS).mock(return_value=httpx.Response(401, json={"error": {"message": "invalid api key"}}))
    result = LlmClient(openai_config()).classify(request())
    assert result.suggestion is None and "API-Schlüssel" in result.error and "invalid api key" in result.error
    respx.post(COMPLETIONS).mock(return_value=httpx.Response(200, json={"choices": []}))
    assert "leer" in LlmClient(openai_config()).classify(request()).error


@respx.mock
def test_openai_content_parts_are_joined():
    parts = [{"type": "text", "text": json.dumps(GOOD)[:20]}, {"type": "text", "text": json.dumps(GOOD)[20:]}]
    respx.post(COMPLETIONS).mock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": parts}}]}))
    assert LlmClient(openai_config()).suggest(TEXT, FOLDERS).folder == "Wohnung/Strom"


# ---------------------------------------------------------------------------
# Validation of the answer


@respx.mock
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("wohnung/strom", "Wohnung/Strom"),
        (" Wohnung / Strom/ ", "Wohnung/Strom"),
        ("Wohnung/Strom (12)", "Wohnung/Strom"),
        ("Haftpflicht", "Versicherungen/Haftpflicht"),
        ("Finanzen\\Bank", "Finanzen/Bank"),
    ],
)
def test_folder_spelling_is_resolved_to_existing_folder(raw, expected):
    respx.post(CHAT).mock(return_value=answer(dict(GOOD, ordner=raw)))
    suggestion = LlmClient(config()).suggest(TEXT, FOLDERS, {})
    assert suggestion.folder == expected and suggestion.new_folder is False


def test_existing_folder_is_matched_by_umlaut_spelling_and_odd_names():
    folders = ["Gebühren/Rundfunk", "Verein e.V.", "Projekte (2)"]
    assert resolve_folder("Gebuehren/Rundfunk", folders, wants_new=False) == ("Gebühren/Rundfunk", False)
    assert resolve_folder("verein e.v.", folders, wants_new=False) == ("Verein e.V.", False)
    assert resolve_folder("Projekte (2)", folders, wants_new=False) == ("Projekte (2)", False)
    # A leaf name only matches when it is unique; a path never matches a leaf elsewhere.
    assert resolve_folder("Bank", ["Finanzen/Bank", "Alt/Bank"], wants_new=False) == (None, False)
    assert resolve_folder("Versicherungen/Bank", ["Finanzen/Bank"], wants_new=False) == (None, False)


@respx.mock
def test_invented_folder_is_dropped_unless_marked_new():
    respx.post(CHAT).mock(return_value=answer(dict(GOOD, ordner="Wohnung/Gas")))
    suggestion = LlmClient(config()).suggest(TEXT, FOLDERS, {})
    assert suggestion.folder is None and suggestion.title  # title and date are still useful
    assert suggestion.score == 0.0

    respx.post(CHAT).mock(return_value=answer(dict(GOOD, ordner="Wohnung/Gas", neuer_ordner="true")))
    suggestion = LlmClient(config()).suggest(TEXT, FOLDERS, {})
    assert suggestion.folder == "Wohnung/Gas" and suggestion.new_folder is True

    respx.post(CHAT).mock(return_value=answer(dict(GOOD, ordner="../../etc", neuer_ordner=True, titel=None,
                                                   datum=None)))
    assert LlmClient(config()).suggest(TEXT, FOLDERS, {}) is None


@respx.mock
def test_parents_of_listed_folders_count_as_existing():
    # Only leaf folders listed: "Wohnung" exists all the same.
    assert resolve_folder("Wohnung/Gas", FOLDERS, wants_new=True) == ("Wohnung/Gas", True)
    assert resolve_folder("wohnung", FOLDERS, wants_new=False) == ("Wohnung", False)


@respx.mock
def test_new_folder_flag_is_ignored_for_existing_folders():
    respx.post(CHAT).mock(return_value=answer(dict(GOOD, neuer_ordner=True)))
    suggestion = LlmClient(config()).suggest(TEXT, FOLDERS, {})
    assert suggestion.folder == "Wohnung/Strom" and suggestion.new_folder is False


@pytest.mark.parametrize(
    "path,folders,ok",
    [
        # Next to existing siblings, below an existing parent.
        ("Versicherungen/HUK", ["Versicherungen", "Versicherungen/Allianz", "Finanzen"], True),
        ("Versicherungen/Allianz/Kfz", ["Versicherungen", "Versicherungen/Allianz", "Finanzen"], True),
        # Parent missing, or a new top-level folder in an established archive.
        ("Versicherungen/HUK/Kfz", ["Versicherungen", "Versicherungen/Allianz", "Finanzen"], False),
        ("Energie", ["Versicherungen", "Versicherungen/Allianz", "Finanzen"], False),
        ("Energie/Strom", ["Versicherungen", "Versicherungen/Allianz", "Finanzen"], False),
        # An (almost) empty archive may start a structure, but not arbitrarily deep.
        ("Versicherungen/Allianz", [], True),
        ("Energie", ["Finanzen"], True),
        ("Versicherungen/Allianz/Kfz", [], False),
        ("Finanzen/Bank/Konten", ["Finanzen", "Steuern"], False),
        ("Finanzen/Bank", ["Finanzen", "Steuern"], True),
    ],
)
def test_new_folders_must_fit_the_structure(path, folders, ok):
    assert acceptable_new_folder(path, folders) is ok


@respx.mock
@pytest.mark.parametrize(
    "value,expected",
    [
        ("2025-02-03", "2025-02-03"),
        ("03.02.2025", "2025-02-03"),
        ("3. Februar 2025", "2025-02-03"),
        ("2025-02-04", None),  # parses, but is not in the document
        ("2025-13-45", None),
        ("irgendwann", None),
        (None, None),
        (20250203, None),
    ],
)
def test_dates_must_appear_in_the_text(value, expected):
    respx.post(CHAT).mock(return_value=answer(dict(GOOD, datum=value)))
    assert LlmClient(config()).suggest(TEXT, FOLDERS, {}).doc_date == expected


@respx.mock
def test_detected_date_candidates_are_accepted():
    respx.post(CHAT).mock(return_value=answer(dict(GOOD, datum="2025-02-28")))
    analysis = Analysis(dates=[DateCandidate(value="2025-02-28", text="28.O2.2025", score=0.6, label="Datum")])
    result = LlmClient(config()).classify(request(analysis=analysis))
    assert result.suggestion.doc_date == "2025-02-28"  # OCR garbled it in the text, the analysis found it


@pytest.mark.parametrize(
    "text,found",
    [
        ("Datum: 03.02.2025", True),
        ("Datum: 3.2.25", True),
        ("Datum: 03. 02. 2025", True),
        ("Stand 2025-02-03", True),
        ("Dresden, den 3. Februar 2025", True),
        ("am 03. Feb. 2025", True),
        ("February 3, 2025", True),
        ("Datum: 13.02.2025", False),
        ("Datum: 03.02.2024", False),
        ("Kontonummer 103.02.20251", False),
    ],
)
def test_date_in_text(text, found):
    assert date_in_text(date(2025, 2, 3), text) is found


@respx.mock
def test_future_dates_are_rejected():
    future = timeutil.today() + timedelta(days=400)
    respx.post(CHAT).mock(return_value=answer(dict(GOOD, datum=future.isoformat())))
    text = TEXT + f"\nVertragsende {future:%d.%m.%Y}"
    assert LlmClient(config()).suggest(text, FOLDERS, {}).doc_date is None


@respx.mock
def test_title_and_tags_are_cleaned():
    long_title = "2025-02-03 " + "Sehr lange Abschlagsrechnung " * 10 + ".pdf"
    respx.post(CHAT).mock(return_value=answer(dict(GOOD, titel=long_title, tags="Strom, Abschlag; strom,  ",
                                                   absender="null")))
    suggestion = LlmClient(config()).suggest(TEXT, FOLDERS, {})
    assert not suggestion.title.startswith("2025") and not suggestion.title.endswith(".pdf")
    assert len(suggestion.title) <= 100
    assert suggestion.tags == ["Strom", "Abschlag"]
    assert suggestion.sender is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2025-02-03 Stromrechnung.pdf", "Stromrechnung"),
        ("03.02.2025 - Stromrechnung", "Stromrechnung"),
        ("„Stromrechnung“ Stadtwerke", "Stromrechnung Stadtwerke"),
        ('"Rechnung "Premium" Tarif"', "Rechnung Premium Tarif"),
        ("Rechnung Stadtwerke vom 03.02.2025", "Rechnung Stadtwerke"),
        ("Kündigung zum 31.12.2025", "Kündigung zum 31.12.2025"),
        ("Abschlag 03/2025", "Abschlag 03-2025"),
        ("2025 Steuerbescheid", "2025 Steuerbescheid"),
        ("scan.JPG", "scan"),
        ("null", None),
        ("   ", None),
        (["Liste"], None),
    ],
)
def test_clean_title(raw, expected):
    assert clean_title(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        (0.8, 0.8),
        ("0,7", 0.7),
        ("85 %", 0.85),
        (85, 0.85),
        (1, 1.0),
        (1.7, 1.0),
        (-0.2, 0.0),
        (250, 1.0),
        (None, 0.5),
        ("hoch", 0.5),
        (True, 0.5),
        (float("nan"), 0.5),
    ],
)
def test_confidence_is_clamped(raw, expected):
    assert parse_confidence(raw) == pytest.approx(expected)


@respx.mock
def test_missing_confidence_and_reason():
    answer_without = {key: value for key, value in GOOD.items() if key not in ("sicherheit", "begruendung")}
    respx.post(CHAT).mock(return_value=answer(answer_without))
    result = LlmClient(config()).classify(request())
    assert result.confidence == 0.5 and result.suggestion.score == pytest.approx(0.45)
    assert result.suggestion.reasons == ["Vorschlag des Sprachmodells qwen2.5:3b"]


@respx.mock
def test_answer_wrapped_in_code_fence_or_chatter():
    content = "Hier ist das Ergebnis:\n```json\n" + json.dumps(GOOD) + "\n```"
    respx.post(CHAT).mock(return_value=answer(content))
    assert LlmClient(config()).suggest(TEXT, FOLDERS, {}).folder == "Wohnung/Strom"
    respx.post(CHAT).mock(return_value=answer("Antwort: " + json.dumps(GOOD) + " Ende."))
    assert LlmClient(config()).suggest(TEXT, FOLDERS, {}).folder == "Wohnung/Strom"


@respx.mock
def test_english_keys_are_understood():
    english = {"folder": "Wohnung/Strom", "title": "Stromrechnung", "date": "2025-02-03", "confidence": 0.9}
    respx.post(CHAT).mock(return_value=answer(english))
    suggestion = LlmClient(config()).suggest(TEXT, FOLDERS, {})
    assert suggestion.folder == "Wohnung/Strom" and suggestion.score == pytest.approx(0.81)


# ---------------------------------------------------------------------------
# The prompt


def test_prompt_contains_counts_examples_similar_documents_and_facts():
    analysis = Analysis(
        doc_type="Rechnung",
        sender="Stadtwerke Leipzig GmbH",
        sender_candidates=["Stadtwerke Leipzig GmbH", "Netz Leipzig GmbH"],
        doc_date="2025-02-03",
        dates=[DateCandidate(value="2025-02-03", text="03.02.2025", score=0.9, label="Rechnungsdatum"),
               DateCandidate(value="2025-02-15", text="15.02.2025", score=0.1, label="Zahlbar bis")],
        amounts=["85,00 €"],
        total_amount="85,00 €",
        references={"Kundennummer": ["4711 0815"]},
        ibans=["DE89370400440532013000"],
    )
    prompt = build_prompt(
        request(
            counts={"Wohnung/Strom": 12, "Finanzen/Bank": 30},
            candidates=["Wohnung/Strom"],
            examples={"Wohnung/Strom": ["2025-01-03 Abschlag Stadtwerke Januar 2025.pdf"]},
            similar=[SimilarDocument(name="2025-01-03 Abschlag Stadtwerke Januar 2025", folder="Wohnung/Strom",
                                     score=0.82)],
            analysis=analysis,
            context={"original_name": "scan_0001.pdf"},
        ),
        max_chars=6000,
    )
    assert "- Wohnung/Strom (12)\n" in prompt and "- Finanzen/Bank (30)\n" in prompt
    assert "Wohnung/Strom:\n  - 2025-01-03 Abschlag Stadtwerke Januar 2025.pdf" in prompt
    assert "„2025-01-03 Abschlag Stadtwerke Januar 2025“ in Wohnung/Strom (82 % ähnlich)" in prompt
    assert "Automatisch erkannt (kann falsch sein):" in prompt
    assert "Datumsangaben im Text: 03.02.2025 (Rechnungsdatum), 15.02.2025 (Zahlbar bis)" in prompt
    assert "Weitere mögliche Absender: Netz Leipzig GmbH" in prompt
    assert "Beträge: 85,00 € (Gesamtbetrag 85,00 €)" in prompt
    assert "Nummern: Kundennummer 4711 0815" in prompt
    assert "Dokumenttyp: Rechnung" in prompt
    assert "Dateiname: scan_0001.pdf" in prompt
    assert prompt.index("Vorhandene Ordner") < prompt.index("Dokumenttext:")


def test_prompt_for_an_empty_archive():
    prompt = build_prompt(request(folders=[]), max_chars=6000)
    assert "noch keine – das Archiv ist leer" in prompt


def test_document_text_keeps_the_beginning_and_the_end():
    text = "A" * 3000 + "B" * 3000
    shown = document_text(text, 1000)
    assert shown.startswith("A" * 1000) and shown.endswith("B" * 1000)
    assert "A" * 1001 not in shown and "B" * 1001 not in shown and "[…]" in shown
    assert document_text("kurz", 1000) == "kurz"
    # Slightly longer than the limit: the tail does not repeat the head.
    shown = document_text("x" * 1200 + "END", 1000)
    assert shown.count("x") == 1200 and shown.endswith("END")


@respx.mock
def test_document_text_is_limited_to_max_chars():
    route = respx.post(CHAT).mock(return_value=answer(GOOD))
    LlmClient(config(max_chars=1000)).suggest("x" * 50_000, FOLDERS, {})
    user = user_prompt(route)
    assert "x" * 1000 in user and "x" * 1001 not in user


def test_folder_selection_for_large_archives():
    folders = [f"Archiv/Jahr {year}/Monat {month:02d}" for year in range(2000, 2026) for month in range(1, 13)]
    folders += ["Archiv", "Wohnung", "Wohnung/Strom", "Tief", "Tief/A", "Tief/A/B", "Tief/A/B/C"]
    folders += [f"Archiv/Jahr {year}" for year in range(2000, 2026)]
    counts = {"Archiv/Jahr 2003/Monat 05": 99, "Tief/A/B/C": 50}
    shown, omitted = select_folders(folders, counts, ["Archiv/Jahr 2001/Monat 01", "Tief/A/B/C"])
    assert len(shown) == MAX_FOLDERS and omitted == len(folders) - MAX_FOLDERS
    assert shown == sorted(shown, key=str.casefold)
    # Candidates (with their parents), then the first two levels, then the fullest folders.
    for folder in ["Archiv/Jahr 2001/Monat 01", "Tief/A/B/C", "Tief/A/B", "Tief", "Wohnung/Strom", "Archiv/Jahr 2025",
                   "Archiv/Jahr 2003/Monat 05"]:
        assert folder in shown
    assert select_folders(FOLDERS, {}, []) == (sorted(FOLDERS, key=str.casefold), 0)


@respx.mock
def test_many_folders_are_listed_partially():
    route = respx.post(CHAT).mock(return_value=answer(GOOD))
    folders = [f"Ordner/{i:04d}" for i in range(450)] + ["Wohnung/Strom"]
    LlmClient(config()).suggest(TEXT, folders, {}, candidates=["Wohnung/Strom"])
    user = user_prompt(route)
    assert f"… und {len(folders) - MAX_FOLDERS} weitere Ordner (nicht aufgeführt)" in user
    assert "- Wohnung/Strom (0)" in user


# ---------------------------------------------------------------------------
# Connection test


@respx.mock
def test_probe_classifies_the_sample_letter():
    sample = dict(GOOD, ordner="Wohnung/Strom", datum="2025-03-14", titel="Abschlagsrechnung Strom Stadtwerke Musterstadt")
    route = respx.post(CHAT).mock(return_value=answer(sample))
    outcome = LlmClient(config()).probe()
    assert outcome["ok"] is True and outcome["error"] is None
    assert outcome["provider"] == "ollama" and outcome["model"] == "qwen2.5:3b"
    assert outcome["answer"]["ordner"] == "Wohnung/Strom" and list(outcome["answer"]) == list(ANSWER_KEYS)
    assert isinstance(outcome["duration_s"], float)
    assert set(outcome) == {"ok", "provider", "model", "duration_s", "answer", "error"}
    user = user_prompt(route)
    assert "Erika Mustermann" in user and "- Wohnung/Strom (9)" in user
    assert "Stadtwerke Musterstadt" in SAMPLE_LETTER


@respx.mock
def test_probe_never_raises():
    respx.post(CHAT).mock(side_effect=httpx.ConnectError("refused"))
    outcome = LlmClient(config()).probe()
    assert outcome["ok"] is False and outcome["answer"] is None
    assert outcome["error"] == "Keine Verbindung zum Sprachmodell unter ollama.test:11434."

    respx.post(COMPLETIONS).mock(return_value=openai_answer("ich weiß nicht"))
    outcome = LlmClient(openai_config()).probe()
    assert outcome["ok"] is False and outcome["provider"] == "openai" and "JSON" in outcome["error"]


@respx.mock
def test_probe_reports_an_answer_even_if_it_is_unusable():
    respx.post(CHAT).mock(return_value=answer(dict(GOOD, ordner="Irgendwo/Anders", datum=None, titel="")))
    outcome = LlmClient(config()).probe()
    assert outcome["ok"] is True and outcome["answer"]["ordner"] == "Irgendwo/Anders"


@respx.mock
def test_last_status_after_a_successful_request():
    respx.post(CHAT).mock(return_value=answer(GOOD))
    client = LlmClient(config())
    client.classify(request())
    assert client.last_status["ok"] is True and client.last_status["reachable"] is True
    assert client.last_status["error"] is None and client.last_status["at"]


# ---------------------------------------------------------------------------
# Anthropic API (Claude)


class _FakeMessages:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _anthropic_response(text: str, stop_reason: str = "end_turn"):
    from types import SimpleNamespace

    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])


def anthropic_client(monkeypatch, outcome) -> tuple[LlmClient, _FakeMessages]:
    from types import SimpleNamespace

    client = LlmClient(LlmConfig(url=URL, model="claude-opus-5-5", timeout=5, provider="anthropic", api_key="sk-test"))
    messages = _FakeMessages(outcome)
    monkeypatch.setattr(client, "_anthropic_client", lambda: SimpleNamespace(messages=messages))
    return client, messages


def test_anthropic_answer_becomes_a_suggestion(monkeypatch):
    client, messages = anthropic_client(monkeypatch, _anthropic_response(json.dumps(GOOD, ensure_ascii=False)))
    result = client.classify(request())
    assert result.error is None and result.suggestion.folder == "Wohnung/Strom"
    call = messages.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": ANSWER_SCHEMA}
    assert call["output_config"]["effort"] == "low"
    assert [m["role"] for m in call["messages"]] == ["user"]
    assert "Ordner" in call["system"]
    assert client.host == "api.anthropic.com"


@pytest.mark.parametrize(
    ("stop_reason", "message"),
    [("refusal", "abgelehnt"), ("max_tokens", "abgeschnitten")],
)
def test_anthropic_refusals_and_truncation_are_no_suggestion(monkeypatch, stop_reason, message):
    client, _ = anthropic_client(monkeypatch, _anthropic_response("{}", stop_reason))
    result = client.classify(request())
    assert result.suggestion is None and message in result.error


def test_anthropic_api_errors_are_reported_in_german(monkeypatch):
    import anthropic

    response = httpx.Response(401, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    error = anthropic.AuthenticationError("invalid x-api-key", response=response, body=None)
    client, _ = anthropic_client(monkeypatch, error)
    result = client.classify(request())
    assert result.suggestion is None and "API-Schlüssel" in result.error


def test_anthropic_connection_error_marks_unreachable(monkeypatch):
    import anthropic

    error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    client, _ = anthropic_client(monkeypatch, error)
    client.classify(request())
    assert client.last_status["reachable"] is False
