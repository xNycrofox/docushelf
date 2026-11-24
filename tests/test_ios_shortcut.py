"""Tests for the shortcut generator in contrib/ios/build_shortcut.py.

The generated file is checked as a property list (WFWorkflow keys, import
questions, variables, text attachments, control flow). Then a small
interpreter runs it with the documented behaviour of exactly the Shortcuts
actions it uses, against the real upload API: this checks that the shortcut
sends what DocuShelf accepts and reports what DocuShelf answers.
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import io
import json
import plistlib
import re
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator

import pytest

from docushelf.channels.sendung import MANIFEST, read_sendung
from web_support import client_for, make_app

SCRIPT = Path(__file__).resolve().parent.parent / "contrib" / "ios" / "build_shortcut.py"
PLACEHOLDER = "\ufffc"
SIMULATED_ACTIONS = {
    "comment",
    "gettext",
    "text.replace",
    "setvariable",
    "repeat.each",
    "downloadurl",
    "conditional",
    "detect.dictionary",
    "getvalueforkey",
    "notification",
    "exit",
}


def _load_builder():
    spec = importlib.util.spec_from_file_location("docushelf_ios_build_shortcut", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = _load_builder()


@pytest.fixture(scope="module")
def shortcut_path(tmp_path_factory) -> Path:
    return builder.write_shortcut(tmp_path_factory.mktemp("shortcut") / "DocuShelf senden.shortcut")


@pytest.fixture(scope="module")
def workflow(shortcut_path) -> dict[str, Any]:
    with open(shortcut_path, "rb") as handle:
        return plistlib.load(handle)


# ---------------------------------------------------------------------------
# Reading the property list


def short_id(action: dict[str, Any]) -> str:
    return action["WFWorkflowActionIdentifier"].removeprefix("is.workflow.actions.")


def parameters(action: dict[str, Any]) -> dict[str, Any]:
    return action["WFWorkflowActionParameters"]


def find(workflow: dict[str, Any], identifier: str) -> list[tuple[int, dict[str, Any]]]:
    return [
        (index, parameters(action))
        for index, action in enumerate(workflow["WFWorkflowActions"])
        if short_id(action) == identifier
    ]


def walk(value: Any) -> Iterator[Any]:
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def is_reference(value: Any) -> bool:
    """A variable reference (not the ``{"Type": "Variable", "Variable": …}`` wrapper of an If input)."""
    if not isinstance(value, dict):
        return False
    kind = value.get("Type")
    return (kind == "Variable" and "VariableName" in value) or kind in ("ActionOutput", "ExtensionInput")


def field_items(dictionary_field: dict[str, Any]) -> list[dict[str, Any]]:
    assert dictionary_field["WFSerializationType"] == "WFDictionaryFieldValue"
    return dictionary_field["Value"]["WFDictionaryFieldValueItems"]


def render(value: Any, name_for: Callable[[dict[str, Any]], str]) -> str:
    """A text parameter with every inline variable replaced by ``name_for(reference)``."""
    if isinstance(value, str):
        return value
    kind = value["WFSerializationType"]
    if kind == "WFTextTokenAttachment":
        return name_for(value["Value"])
    assert kind == "WFTextTokenString", value
    positions: dict[int, dict[str, Any]] = {}
    for key, reference in value["Value"].get("attachmentsByRange", {}).items():
        match = re.fullmatch(r"\{(\d+), 1\}", key)
        assert match, key
        positions[int(match.group(1))] = reference
    pieces = []
    offset = 0  # in UTF-16 code units, like the range keys
    for char in value["Value"]["string"]:
        if offset in positions:
            assert char == PLACEHOLDER
            pieces.append(name_for(positions.pop(offset)))
        else:
            pieces.append(char)
        offset += len(char.encode("utf-16-le")) // 2
    assert not positions, "attachment beyond the end of the text"
    return "".join(pieces)


def label(reference: dict[str, Any]) -> str:
    name = reference.get("VariableName") or reference.get("OutputName") or reference["Type"]
    return "{" + name + "}"


# ---------------------------------------------------------------------------
# A small interpreter for the actions of the shortcut


@dataclass
class SharedFile:
    name: str
    data: bytes
    content_type: str


@dataclass
class HttpAnswer:
    status: int
    text: str


@dataclass
class Run:
    requests: list[dict[str, Any]] = field(default_factory=list)
    notifications: list[tuple[str, str]] = field(default_factory=list)
    variables: dict[str, Any] = field(default_factory=dict)
    stopped: bool = False


Transport = Callable[[str, str, dict[str, str], list[tuple[str, str]], list[tuple[str, SharedFile]]], HttpAnswer]


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(as_text(item) for item in value)
    if isinstance(value, HttpAnswer):
        return value.text
    if isinstance(value, SharedFile):
        return value.name
    return str(value)


def as_dictionary(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    try:
        data = json.loads(as_text(value))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def replace_text(text: str, params: dict[str, Any]) -> str:
    flags = 0 if params.get("WFReplaceTextCaseSensitive", True) else re.IGNORECASE
    find_text, replacement = params["WFReplaceTextFind"], params["WFReplaceTextReplace"]
    if params.get("WFReplaceTextRegularExpression"):
        # ICU writes groups in the replacement as $1; Python as \g<1>.
        template = re.sub(r"\$(\d+)", r"\\g<\1>", replacement.replace("\\", "\\\\"))
        return re.sub(find_text, template, text, flags=flags)
    return re.sub(re.escape(find_text), lambda _match: replacement, text, flags=flags)


class ShortcutRunner:
    """Runs the shortcut's actions the way the Shortcuts app documents them."""

    def __init__(self, workflow: dict[str, Any], answers: list[str], transport: Transport) -> None:
        self.actions = [(short_id(action), parameters(action)) for action in copy.deepcopy(workflow["WFWorkflowActions"])]
        for question, answer in zip(workflow["WFWorkflowImportQuestions"], answers, strict=True):
            self.actions[question["ActionIndex"]][1][question["ParameterKey"]] = answer
        self.transport = transport
        self.blocks: dict[str, dict[int, int]] = {}
        for index, (_identifier, params) in enumerate(self.actions):
            if "GroupingIdentifier" in params:
                self.blocks.setdefault(params["GroupingIdentifier"], {})[params["WFControlFlowMode"]] = index

    def run(self, shared: list[SharedFile]) -> Run:
        result = Run()
        variables = result.variables
        outputs: dict[str, Any] = {}
        loops: list[list[Any]] = []  # [items, position, index of the loop start]

        def resolve(reference: dict[str, Any]) -> Any:
            kind = reference["Type"]
            if kind == "ExtensionInput":
                value: Any = list(shared)
            elif kind == "Variable":
                value = variables.get(reference["VariableName"])
            elif kind == "ActionOutput":
                value = outputs[reference["OutputUUID"]]
            else:
                raise AssertionError(f"unknown variable type {kind}")
            for aggrandizement in reference.get("Aggrandizements", []):
                assert aggrandizement == {
                    "Type": "WFCoercionVariableAggrandizement",
                    "CoercionItemClass": "WFStringContentItem",
                }
                value = as_text(value)
            return value

        def text(value: Any) -> str:
            return render(value, lambda reference: as_text(resolve(reference)))

        def single(value: dict[str, Any]) -> Any:
            assert value["WFSerializationType"] == "WFTextTokenAttachment"
            return resolve(value["Value"])

        index = 0
        while index < len(self.actions):
            identifier, params = self.actions[index]
            block = self.blocks.get(params.get("GroupingIdentifier", ""), {})
            mode = params.get("WFControlFlowMode")
            if identifier == "comment":
                pass
            elif identifier == "gettext":
                outputs[params["UUID"]] = text(params["WFTextActionText"])
            elif identifier == "text.replace":
                outputs[params["UUID"]] = replace_text(text(params["WFInput"]), params)
            elif identifier == "setvariable":
                variables[params["WFVariableName"]] = single(params["WFInput"])
            elif identifier == "repeat.each" and mode == builder.FLOW_START:
                items = as_list(single(params["WFInput"]))
                if not items:
                    index = block[builder.FLOW_END] + 1
                    continue
                loops.append([items, 0, index])
                variables["Repeat Item"] = items[0]
            elif identifier == "repeat.each":
                items, position, start = loops[-1]
                if position + 1 < len(items):
                    loops[-1][1] = position + 1
                    variables["Repeat Item"] = items[position + 1]
                    index = start + 1
                    continue
                loops.pop()
            elif identifier == "downloadurl":
                outputs[params["UUID"]] = self._request(params, text, single, result)
            elif identifier == "conditional" and mode == builder.FLOW_START:
                if not self._condition(params, resolve):
                    index = block.get(builder.FLOW_OTHERWISE, block[builder.FLOW_END]) + 1
                    continue
            elif identifier == "conditional" and mode == builder.FLOW_OTHERWISE:
                index = block[builder.FLOW_END] + 1
                continue
            elif identifier == "conditional":
                pass
            elif identifier == "detect.dictionary":
                outputs[params["UUID"]] = as_dictionary(single(params["WFInput"]))
            elif identifier == "getvalueforkey":
                assert params["WFGetDictionaryValueType"] == "Value"
                dictionary = as_dictionary(single(params["WFInput"]))
                value = dictionary.get(text(params["WFDictionaryKey"])) if dictionary else None
                outputs[params["UUID"]] = value
            elif identifier == "notification":
                title = text(params.get("WFNotificationActionTitle", ""))
                result.notifications.append((title, text(params["WFNotificationActionBody"])))
            elif identifier == "exit":
                result.stopped = True
                break
            else:
                raise AssertionError(f"the interpreter does not know {identifier}")
            index += 1
        return result

    def _request(self, params, text, single, result: Run) -> HttpAnswer:
        assert params["WFHTTPBodyType"] == "Form"
        headers = {text(item["WFKey"]): text(item["WFValue"]) for item in field_items(params["WFHTTPHeaders"])}
        form_fields: list[tuple[str, str]] = []
        files: list[tuple[str, SharedFile]] = []
        for item in field_items(params["WFFormValues"]):
            key = text(item["WFKey"])
            if item["WFItemType"] == builder.ITEM_FILE:
                state = item["WFValue"]
                assert state["WFSerializationType"] == "WFTokenAttachmentParameterState"
                files.extend((key, shared) for shared in as_list(single(state["Value"])))
            else:
                assert item["WFItemType"] == builder.ITEM_TEXT
                form_fields.append((key, text(item["WFValue"])))
        url = text(params["WFURL"])
        answer = self.transport(params["WFHTTPMethod"], url, headers, form_fields, files)
        result.requests.append(
            {
                "method": params["WFHTTPMethod"],
                "url": url,
                "headers": headers,
                "fields": form_fields,
                "files": [(key, shared.name) for key, shared in files],
                "status": answer.status,
                "json": as_dictionary(answer),
            }
        )
        return answer

    @staticmethod
    def _condition(params, resolve) -> bool:
        wrapper = params["WFInput"]
        assert wrapper["Type"] == "Variable" and wrapper["Variable"]["WFSerializationType"] == "WFTextTokenAttachment"
        items = as_list(resolve(wrapper["Variable"]["Value"]))
        condition = params["WFCondition"]
        if condition == 100:  # has any value
            return bool(items)
        if condition == 101:  # does not have any value
            return not items
        wanted = params["WFConditionalActionString"].lower()
        texts = [as_text(item).lower() for item in items]
        if condition == builder.CONDITION_CONTAINS:
            return any(wanted in value for value in texts)
        if condition == 999:  # does not contain
            return bool(texts) and not any(wanted in value for value in texts)
        raise AssertionError(f"the interpreter does not know condition {condition}")


def success_answer(*_args: Any) -> HttpAnswer:
    return HttpAnswer(202, json.dumps({"batch_id": "upload-1", "documents": 1, "files": ["x.pdf"]}))


def a_pdf() -> SharedFile:
    return SharedFile("Scan.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf")


# ---------------------------------------------------------------------------
# The file and its structure


def test_file_is_an_unsigned_binary_property_list(shortcut_path):
    data = shortcut_path.read_bytes()
    assert data.startswith(b"bplist00")
    assert plistlib.loads(data)["WFWorkflowName"] == "DocuShelf senden"


def test_shortcut_is_offered_in_the_share_sheet_for_pdfs_images_and_files(workflow):
    assert workflow["WFWorkflowTypes"] == ["ActionExtension"]
    assert sorted(workflow["WFWorkflowInputContentItemClasses"]) == [
        "WFGenericFileContentItem",
        "WFImageContentItem",
        "WFPDFContentItem",
    ]
    assert workflow["WFWorkflowHasShortcutInputVariables"] is True
    assert workflow["WFWorkflowNoInputBehavior"] == {
        "Name": "WFWorkflowNoInputBehaviorAskForInput",
        "Parameters": {"ItemClass": "WFGenericFileContentItem"},
    }
    assert workflow["WFWorkflowMinimumClientVersion"] == 900
    assert workflow["WFWorkflowMinimumClientVersionString"] == "900"
    assert isinstance(workflow["WFWorkflowClientVersion"], str) and workflow["WFWorkflowClientVersion"]
    icon = workflow["WFWorkflowIcon"]
    assert isinstance(icon["WFWorkflowIconGlyphNumber"], int)
    assert 0 < icon["WFWorkflowIconStartColor"] < 2**32
    actions = workflow["WFWorkflowActions"]
    assert all(action["WFWorkflowActionIdentifier"].startswith("is.workflow.actions.") for action in actions)
    assert all(isinstance(parameters(action), dict) for action in actions)
    assert {short_id(action) for action in actions} <= SIMULATED_ACTIONS


def test_import_questions_ask_for_address_and_token(workflow):
    actions = workflow["WFWorkflowActions"]
    questions = workflow["WFWorkflowImportQuestions"]
    assert len(questions) == 2
    for question in questions:
        assert question["Category"] == "Parameter"
        assert isinstance(question["ActionIndex"], int) and 0 <= question["ActionIndex"] < len(actions)
        target = actions[question["ActionIndex"]]
        assert short_id(target) == "gettext"
        assert question["ParameterKey"] == "WFTextActionText"
        assert question["ParameterKey"] in parameters(target)
        # Nothing to delete before pasting, and no address or token in the file.
        assert question["DefaultValue"] == ""
        assert parameters(target)[question["ParameterKey"]] == ""
    server, token = questions
    assert server["ActionIndex"] < token["ActionIndex"]
    assert "Adresse" in server["Text"] and "https://" in server["Text"]
    assert "API-Token" in token["Text"] and "dsk_" in token["Text"]

    run = ShortcutRunner(workflow, ["https://docs.example.net", "dsk_geheim"], success_answer).run([])
    assert run.variables[builder.SERVER_VARIABLE] == "https://docs.example.net"
    assert run.variables[builder.TOKEN_VARIABLE] == "dsk_geheim"


def test_upload_request_url_headers_and_form(workflow):
    [(index, request)] = find(workflow, "downloadurl")
    assert request["WFHTTPMethod"] == "POST"
    assert request["WFHTTPBodyType"] == "Form"
    assert render(request["WFURL"], label) == "{Server}/api/v1/upload"
    headers = [
        (item["WFItemType"], render(item["WFKey"], label), render(item["WFValue"], label))
        for item in field_items(request["WFHTTPHeaders"])
    ]
    assert headers == [(0, "Authorization", "Bearer {Token}")]
    [form_field] = field_items(request["WFFormValues"])
    assert form_field["WFItemType"] == 5
    assert render(form_field["WFKey"], label) == "files"
    assert form_field["WFValue"] == {
        "Value": {
            "Value": {"Type": "Variable", "VariableName": "Repeat Item"},
            "WFSerializationType": "WFTextTokenAttachment",
        },
        "WFSerializationType": "WFTokenAttachmentParameterState",
    }
    # The request sits inside the loop over the shared items.
    [(start, loop)] = [(i, p) for i, p in find(workflow, "repeat.each") if p["WFControlFlowMode"] == 0]
    [(end, _)] = [(i, p) for i, p in find(workflow, "repeat.each") if p["WFControlFlowMode"] == 2]
    assert start < index < end
    assert loop["WFInput"] == {"Value": {"Type": "ExtensionInput"}, "WFSerializationType": "WFTextTokenAttachment"}


def test_answer_check_and_notifications(workflow):
    [(_, request)] = find(workflow, "downloadurl")
    [check] = [p for _, p in find(workflow, "conditional") if p["WFControlFlowMode"] == 0]
    assert check["WFCondition"] == 99 and check["WFConditionalActionString"] == "batch_id"
    assert check["WFInput"]["Variable"]["Value"]["OutputUUID"] == request["UUID"]
    [(_, answer)] = find(workflow, "detect.dictionary")
    assert answer["WFInput"]["Value"]["OutputUUID"] == request["UUID"]
    [(_, detail)] = find(workflow, "getvalueforkey")
    assert detail["WFDictionaryKey"] == "detail"
    assert detail["WFInput"]["Value"]["OutputUUID"] == answer["UUID"]
    (error_index, error), (done_index, done) = find(workflow, "notification")
    assert render(error["WFNotificationActionBody"], label) == "{Dictionary Value}"
    assert error["WFNotificationActionBody"]["Value"]["attachmentsByRange"]["{0, 1}"]["OutputUUID"] == detail["UUID"]
    assert short_id(workflow["WFWorkflowActions"][error_index + 1]) == "exit"
    assert done_index == len(workflow["WFWorkflowActions"]) - 1
    assert (done["WFNotificationActionTitle"], done["WFNotificationActionBody"]) == (
        "An DocuShelf gesendet",
        builder.DONE_BODY,
    )


def test_text_attachments_sit_on_placeholders(workflow):
    token_strings = [
        value
        for value in walk(workflow)
        if isinstance(value, dict) and value.get("WFSerializationType") == "WFTextTokenString"
    ]
    assert any(value["Value"].get("attachmentsByRange") for value in token_strings)
    for token_string in token_strings:
        text = token_string["Value"]["string"]
        attachments = token_string["Value"].get("attachmentsByRange", {})
        assert text.count(PLACEHOLDER) == len(attachments)
        units = text.encode("utf-16-le")
        for key, reference in attachments.items():
            location = int(re.fullmatch(r"\{(\d+), 1\}", key).group(1))
            assert units[2 * location : 2 * location + 2] == PLACEHOLDER.encode("utf-16-le")
            assert is_reference(reference)


def test_references_point_at_earlier_actions_and_variables_are_set_first(workflow):
    actions = workflow["WFWorkflowActions"]
    uuids = [parameters(action)["UUID"] for action in actions if "UUID" in parameters(action)]
    assert len(uuids) == len(set(uuids))
    assert all(re.fullmatch(r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}", value) for value in uuids)
    seen: set[str] = set()
    assigned: set[str] = set()
    loop_depth = 0
    for action in actions:
        params = parameters(action)
        is_loop_end = short_id(action) == "repeat.each" and params["WFControlFlowMode"] == 2
        for reference in filter(is_reference, walk(params)):
            if reference["Type"] == "ActionOutput":
                assert reference["OutputUUID"] in seen, reference
            elif reference["Type"] == "Variable" and reference["VariableName"] == "Repeat Item":
                assert loop_depth > 0
            elif reference["Type"] == "Variable":
                assert reference["VariableName"] in assigned, reference
        if short_id(action) == "repeat.each":
            loop_depth += -1 if is_loop_end else 1
        if short_id(action) == "setvariable":
            assigned.add(params["WFVariableName"])
        if "UUID" in params:
            seen.add(params["UUID"])
    assert assigned == {builder.SERVER_VARIABLE, builder.TOKEN_VARIABLE}


def test_control_flow_blocks_are_balanced(workflow):
    open_blocks: list[tuple[str, str]] = []
    for action in workflow["WFWorkflowActions"]:
        params = parameters(action)
        identifier = short_id(action)
        if identifier not in ("repeat.each", "conditional"):
            assert "WFControlFlowMode" not in params and "GroupingIdentifier" not in params
            continue
        block = (identifier, params["GroupingIdentifier"])
        if params["WFControlFlowMode"] == 0:
            open_blocks.append(block)
        elif params["WFControlFlowMode"] == 1:
            assert identifier == "conditional" and open_blocks[-1] == block
        else:
            assert params["WFControlFlowMode"] == 2 and open_blocks.pop() == block
    assert open_blocks == []


# ---------------------------------------------------------------------------
# Running the shortcut


@pytest.fixture
def server(base_services):
    services = base_services
    services.intake = SimpleNamespace(trigger=lambda: None)
    client = client_for(make_app(services))
    token, _entry = services.tokens.create("iPhone Kurzbefehl", "anna")

    def transport(method, url, headers, form_fields, files):
        response = client.request(
            method,
            url,
            headers=headers,
            data=dict(form_fields) or None,
            files=[(key, (shared.name, shared.data, shared.content_type)) for key, shared in files],
        )
        return HttpAnswer(response.status_code, response.text)

    return SimpleNamespace(services=services, token=token, transport=transport, url="http://testserver")


def uploaded(services) -> list[list[str]]:
    batches = [read_sendung(path.parent) for path in services.layout.source_dir("upload").glob(f"*/{MANIFEST}")]
    return sorted(document.files for batch in batches for document in batch.documents)


def heic_image() -> bytes:
    import pillow_heif
    from PIL import Image

    pillow_heif.register_heif_opener()
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), "white").save(buffer, format="HEIF")
    return buffer.getvalue()


def test_shared_files_are_uploaded_one_by_one(server, workflow, samples, tmp_path):
    # Names as iOS hands them over: decomposed umlauts, generic photo names, HEIC.
    pdf_name = unicodedata.normalize("NFD", "Kündigung Müller.pdf")
    shared = [
        SharedFile(pdf_name, samples.text_pdf(tmp_path / "Scan.pdf", ["Stadtwerke\nRechnung"]).read_bytes(), "application/pdf"),
        SharedFile("image.jpeg", samples.image(tmp_path / "image.jpeg", ["Brief"]).read_bytes(), "image/jpeg"),
        SharedFile("IMG_0042.HEIC", heic_image(), "image/heic"),
    ]
    run = ShortcutRunner(workflow, [server.url, server.token], server.transport).run(shared)

    assert [request["status"] for request in run.requests] == [202, 202, 202], run.requests
    for request, item in zip(run.requests, shared, strict=True):
        assert request["method"] == "POST"
        assert request["url"] == "http://testserver/api/v1/upload"
        assert request["headers"] == {"Authorization": f"Bearer {server.token}"}
        assert request["fields"] == []
        assert request["files"] == [("files", item.name)]
        assert request["json"]["documents"] == 1
    assert run.notifications == [("An DocuShelf gesendet", builder.DONE_BODY)]
    assert not run.stopped
    assert uploaded(server.services) == [["IMG_0042.HEIC"], ["Kündigung Müller.pdf"], ["image.jpeg"]]


def test_a_refused_token_is_reported_and_stops_the_shortcut(server, workflow):
    run = ShortcutRunner(workflow, [server.url, "dsk_widerrufen"], server.transport).run([a_pdf(), a_pdf()])
    assert [request["status"] for request in run.requests] == [401]
    detail = run.requests[0]["json"]["detail"]
    assert "API-Token" in detail
    assert run.notifications == [(builder.ERROR_TITLE, detail)]
    assert run.stopped
    assert uploaded(server.services) == []


def test_an_unsupported_file_is_reported_with_the_servers_text(server, workflow):
    shared = [SharedFile("Einkaufsliste.txt", b"Milch, Brot", "text/plain"), a_pdf()]
    run = ShortcutRunner(workflow, [server.url, server.token], server.transport).run(shared)
    assert [request["status"] for request in run.requests] == [415]
    [(title, body)] = run.notifications
    assert title == builder.ERROR_TITLE
    assert "Einkaufsliste.txt" in body and body == run.requests[0]["json"]["detail"]
    assert run.stopped


@pytest.mark.parametrize(
    "answer",
    [
        HttpAnswer(200, "<html><title>Anmelden</title><form action='/login'></form></html>"),
        HttpAnswer(502, "Bad Gateway"),
        HttpAnswer(200, ""),
    ],
)
def test_answers_without_upload_confirmation_count_as_errors(workflow, answer):
    run = ShortcutRunner(workflow, ["https://docs.example.net", "dsk_x"], lambda *_args: answer).run([a_pdf()])
    assert [title for title, _body in run.notifications] == [builder.ERROR_TITLE]
    assert run.stopped


@pytest.mark.parametrize(
    ("typed", "url"),
    [
        ("https://docs.example.net", "https://docs.example.net/api/v1/upload"),
        ("https://docs.example.net/", "https://docs.example.net/api/v1/upload"),
        (" https://docs.example.net/eingang?ansicht=liste \n", "https://docs.example.net/api/v1/upload"),
        ("docs.example.net", "https://docs.example.net/api/v1/upload"),
        ("docs.example.net/archiv/Finanzen", "https://docs.example.net/api/v1/upload"),
        ("HTTPS://Docs.Example.net//", "HTTPS://Docs.Example.net/api/v1/upload"),
        ("http://100.64.0.10:8000/", "http://100.64.0.10:8000/api/v1/upload"),
        ("http://[fd7a:115c::1]:8000", "http://[fd7a:115c::1]:8000/api/v1/upload"),
    ],
)
def test_server_address_is_normalised(workflow, typed, url):
    run = ShortcutRunner(workflow, [typed, "dsk_x"], success_answer).run([a_pdf()])
    assert [request["url"] for request in run.requests] == [url]
    assert run.notifications == [("An DocuShelf gesendet", builder.DONE_BODY)]


# ---------------------------------------------------------------------------
# The script


def test_build_is_reproducible(tmp_path):
    first = builder.write_shortcut(tmp_path / "eins.shortcut").read_bytes()
    second = builder.write_shortcut(tmp_path / "zwei.shortcut").read_bytes()
    assert first == second
    assert builder.shortcut_bytes("Ab ins Archiv") != first
    assert sorted(path.name for path in tmp_path.iterdir()) == ["eins.shortcut", "zwei.shortcut"]


def test_command_line_name_and_output_directory(tmp_path, capsys):
    assert builder.main(["--output", str(tmp_path), "--name", " Ab ins Archiv "]) == 0
    written = tmp_path / "Ab ins Archiv.shortcut"
    assert plistlib.loads(written.read_bytes())["WFWorkflowName"] == "Ab ins Archiv"
    out = capsys.readouterr().out
    assert str(written) in out and "shortcuts sign --mode anyone" in out

    target = tmp_path / "neu" / "datei.shortcut"
    assert builder.main(["-o", str(target)]) == 0
    assert plistlib.loads(target.read_bytes())["WFWorkflowName"] == "DocuShelf senden"


def test_command_line_help_explains_signing(capsys):
    with pytest.raises(SystemExit) as stopped:
        builder.main(["--help"])
    assert stopped.value.code == 0
    out = capsys.readouterr().out
    assert "diese Hilfe anzeigen" in out and "shortcuts sign --mode anyone" in out


def test_command_line_default_output(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert builder.main([]) == 0
    assert [path.name for path in tmp_path.iterdir()] == ["DocuShelf senden.shortcut"]


@pytest.mark.parametrize("name", ["", "   ", "Scans/Brief", "Zeile\nzwei"])
def test_command_line_rejects_unusable_names(tmp_path, capsys, name):
    with pytest.raises(SystemExit) as stopped:
        builder.main(["--output", str(tmp_path), "--name", name])
    assert stopped.value.code == 2
    assert "Name" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_script_needs_only_the_standard_library():
    # The python3 of macOS (3.9) must be able to run it; the syntax check uses 3.8.
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"), feature_version=(3, 8))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module
            modules.add(node.module.partition(".")[0])
    assert modules <= set(sys.stdlib_module_names)


def test_script_runs_without_site_packages(tmp_path):
    target = tmp_path / "DocuShelf senden.shortcut"
    completed = subprocess.run(
        [sys.executable, "-I", "-S", str(SCRIPT), "--output", str(target)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert target.read_bytes() == builder.shortcut_bytes()


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil only exists on macOS")
def test_plutil_accepts_the_file(shortcut_path):
    completed = subprocess.run(
        [shutil.which("plutil"), "-lint", str(shortcut_path)], capture_output=True, text=True, timeout=60
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip().endswith(": OK")
