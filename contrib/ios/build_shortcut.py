#!/usr/bin/env python3
"""Build the shortcut "DocuShelf senden" for iPhone, iPad and Mac.

The shortcut appears in the share sheet for PDFs, images and files. When it
is added, the Shortcuts app asks for the server address and an API token.
Each shared item is then uploaded on its own to ``<server>/api/v1/upload``
(multipart field ``files``, header ``Authorization: Bearer <token>``), so
every file becomes its own document in the inbox. At the end a notification
says "An DocuShelf gesendet"; if the server refuses a file, the notification
shows the server's error text instead and the shortcut stops.

The result is an *unsigned* binary property list in the WFWorkflow format.
Current iOS and macOS versions only import signed shortcuts, so sign the file
on a Mac before importing it (see README.md next to this script)::

    python3 build_shortcut.py --output "DocuShelf senden.shortcut"
    shortcuts sign --mode anyone --input "DocuShelf senden.shortcut" \\
        --output "signed/DocuShelf senden.shortcut"

Only the standard library is used, so the python3 that ships with macOS is
enough. The build is reproducible: the same name always gives the same bytes.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

DEFAULT_NAME = "DocuShelf senden"
FILE_SUFFIX = ".shortcut"

# The DocuShelf upload API (see docs/internals.md, "API /api/v1").
UPLOAD_PATH = "/api/v1/upload"
UPLOAD_FIELD = "files"
SUCCESS_KEY = "batch_id"  # present in every successful upload answer
ERROR_KEY = "detail"  # the error text of every refused request

# Named variables inside the shortcut.
SERVER_VARIABLE = "Server"
TOKEN_VARIABLE = "Token"
REPEAT_ITEM = "Repeat Item"

# Texts shown in the Shortcuts app (German, like the rest of the interface).
INTRO_COMMENT = (
    "Schickt geteilte PDFs, Bilder und Dateien an den Eingang von DocuShelf – jede Datei wird ein "
    "eigenes Dokument. Adresse und API-Token stehen in den beiden Text-Aktionen darunter."
)
SERVER_QUESTION = "Adresse deines DocuShelf, so wie du es im Browser öffnest – z. B. https://docs.example.net"
TOKEN_QUESTION = (
    "Dein API-Token aus DocuShelf (Einstellungen → API-Tokens → „Token erstellen“). Er beginnt mit dsk_."
)
UPLOADED_COMMENT = "Angekommen – weiter mit der nächsten Datei."
ERROR_TITLE = "Fehler beim Senden an DocuShelf"
DONE_TITLE = "An DocuShelf gesendet"
DONE_BODY = "Gleich findest du alles im Eingang."

# The address is used the way it was typed or pasted. Two regular expressions
# (ICU syntax, case-insensitive) make it robust: "https://" is added when the
# scheme is missing, and everything after host and port is dropped, so a
# trailing slash or a copied page address ("…/eingang") still gives the right
# upload URL. DocuShelf always runs at the root of its host.
ADD_SCHEME_PATTERN = r"^\s*+(?!https?://)"
ADD_SCHEME_REPLACEMENT = "https://"
ORIGIN_PATTERN = r"(?s)^\s*(https?://[^/?#\s]+).*"
ORIGIN_REPLACEMENT = "$1"

# --- WFWorkflow format -------------------------------------------------------

ACTION_PREFIX = "is.workflow.actions."
# Marks the position of an inline variable in a text; attachmentsByRange maps
# "{location, length}" (in UTF-16 code units) to the variable.
OBJECT_REPLACEMENT_CHARACTER = "\ufffc"

# Written as if by Shortcuts on iOS 17. Newer versions migrate shortcuts from
# older client versions on import; the minimum keeps very old versions out.
CLIENT_VERSION = "2106.0.3"
MINIMUM_CLIENT_VERSION = 900

# "Show in Share Sheet" for these kinds of input.
WORKFLOW_TYPES = ("ActionExtension",)
INPUT_CONTENT_CLASSES = ("WFPDFContentItem", "WFImageContentItem", "WFGenericFileContentItem")
# Started without input (from the Shortcuts app or a Home Screen icon): pick files.
NO_INPUT_BEHAVIOR = {
    "Name": "WFWorkflowNoInputBehaviorAskForInput",
    "Parameters": {"ItemClass": "WFGenericFileContentItem"},
}

ICON_GLYPH_UPLOAD = 59708  # arrow out of a tray
ICON_COLOR_DARK_BLUE = 946986751  # RGBA 0x3871DEFF, the palette colour closest to DocuShelf's blue

# Types of the items of a dictionary field (headers, form values).
ITEM_TEXT = 0
ITEM_FILE = 5

# Conditions of the "If" action.
CONDITION_CONTAINS = 99

# Parts of a control flow block (If/Otherwise/End If, Repeat/End Repeat).
FLOW_START = 0
FLOW_OTHERWISE = 1
FLOW_END = 2

# Output names of the actions, as the Shortcuts editor shows them.
OUTPUT_TEXT = "Text"
OUTPUT_UPDATED_TEXT = "Updated Text"
OUTPUT_CONTENTS_OF_URL = "Contents of URL"
OUTPUT_DICTIONARY = "Dictionary"
OUTPUT_DICTIONARY_VALUE = "Dictionary Value"

# Fixed namespace for the action UUIDs: they only have to be unique within the
# shortcut, and deriving them from the name keeps the build reproducible.
_UUID_NAMESPACE = uuid.UUID("7aadc6a3-835e-4e6e-8cab-f98f441fdc49")

Reference = Dict[str, Any]
TextPart = Union[str, Reference]


# --- Values ---------------------------------------------------------------------


def named_variable(name: str) -> Reference:
    """A variable set with "Set Variable" (or the loop's "Repeat Item")."""
    return {"Type": "Variable", "VariableName": name}


def action_output(output_uuid: str, output_name: str, *, as_text: bool = False) -> Reference:
    """The output ("magic variable") of the action with ``UUID == output_uuid``."""
    reference: Reference = {"Type": "ActionOutput", "OutputUUID": output_uuid, "OutputName": output_name}
    if as_text:
        reference["Aggrandizements"] = [
            {"Type": "WFCoercionVariableAggrandizement", "CoercionItemClass": "WFStringContentItem"}
        ]
    return reference


def shortcut_input() -> Reference:
    """What was shared with the shortcut."""
    return {"Type": "ExtensionInput"}


def attachment(reference: Reference) -> Dict[str, Any]:
    """A parameter that holds exactly one variable."""
    return {"Value": reference, "WFSerializationType": "WFTextTokenAttachment"}


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def token_string(*parts: TextPart) -> Dict[str, Any]:
    """Text with inline variables: ``parts`` are literal strings and variable references."""
    pieces: List[str] = []
    attachments: Dict[str, Reference] = {}
    offset = 0
    for part in parts:
        if isinstance(part, str):
            pieces.append(part)
            offset += utf16_length(part)
        else:
            attachments["{%d, 1}" % offset] = part
            pieces.append(OBJECT_REPLACEMENT_CHARACTER)
            offset += 1
    value: Dict[str, Any] = {"string": "".join(pieces)}
    if attachments:
        value["attachmentsByRange"] = attachments
    return {"Value": value, "WFSerializationType": "WFTextTokenString"}


def dictionary_field(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Key/value pairs as in the header and form sections of "Get Contents of URL"."""
    return {"Value": {"WFDictionaryFieldValueItems": items}, "WFSerializationType": "WFDictionaryFieldValue"}


def text_item(key: str, *value: TextPart) -> Dict[str, Any]:
    return {"WFItemType": ITEM_TEXT, "WFKey": token_string(key), "WFValue": token_string(*value)}


def file_item(key: str, reference: Reference) -> Dict[str, Any]:
    """A form field of type "File" (one multipart part per file)."""
    return {
        "WFItemType": ITEM_FILE,
        "WFKey": token_string(key),
        "WFValue": {"Value": attachment(reference), "WFSerializationType": "WFTokenAttachmentParameterState"},
    }


# --- Building ---------------------------------------------------------------------


class _Builder:
    """Collects the actions and import questions of one shortcut."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.actions: List[Dict[str, Any]] = []
        self.questions: List[Dict[str, Any]] = []

    def new_uuid(self, label: str) -> str:
        """A stable UUID for one action or block of this shortcut."""
        return str(uuid.uuid5(_UUID_NAMESPACE, "%s\n%s" % (self.name, label))).upper()

    def add(self, identifier: str, **parameters: Any) -> int:
        self.actions.append(
            {"WFWorkflowActionIdentifier": ACTION_PREFIX + identifier, "WFWorkflowActionParameters": parameters}
        )
        return len(self.actions) - 1

    def ask(self, action_index: int, parameter_key: str, text: str) -> None:
        """Ask for the value of a parameter when the shortcut is added."""
        assert parameter_key in self.actions[action_index]["WFWorkflowActionParameters"]
        self.questions.append(
            {
                "ActionIndex": action_index,
                "Category": "Parameter",
                "DefaultValue": "",
                "ParameterKey": parameter_key,
                "Text": text,
            }
        )


def build_workflow(name: str = DEFAULT_NAME) -> Dict[str, Any]:
    """The shortcut as a property list (a dict ready for ``plistlib``)."""
    b = _Builder(name)
    b.add("comment", WFCommentActionText=INTRO_COMMENT)

    # Server address: asked on import, then normalised to scheme://host[:port].
    server_text = b.new_uuid("server-text")
    b.ask(b.add("gettext", UUID=server_text, WFTextActionText=""), "WFTextActionText", SERVER_QUESTION)
    with_scheme = b.new_uuid("server-with-scheme")
    b.add(
        "text.replace",
        UUID=with_scheme,
        WFInput=token_string(action_output(server_text, OUTPUT_TEXT)),
        WFReplaceTextFind=ADD_SCHEME_PATTERN,
        WFReplaceTextReplace=ADD_SCHEME_REPLACEMENT,
        WFReplaceTextRegularExpression=True,
        WFReplaceTextCaseSensitive=False,
    )
    origin = b.new_uuid("server-origin")
    b.add(
        "text.replace",
        UUID=origin,
        WFInput=token_string(action_output(with_scheme, OUTPUT_UPDATED_TEXT)),
        WFReplaceTextFind=ORIGIN_PATTERN,
        WFReplaceTextReplace=ORIGIN_REPLACEMENT,
        WFReplaceTextRegularExpression=True,
        WFReplaceTextCaseSensitive=False,
    )
    b.add("setvariable", WFVariableName=SERVER_VARIABLE, WFInput=attachment(action_output(origin, OUTPUT_UPDATED_TEXT)))

    # API token: asked on import.
    token_text = b.new_uuid("token-text")
    b.ask(b.add("gettext", UUID=token_text, WFTextActionText=""), "WFTextActionText", TOKEN_QUESTION)
    b.add("setvariable", WFVariableName=TOKEN_VARIABLE, WFInput=attachment(action_output(token_text, OUTPUT_TEXT)))

    # One upload per shared item.
    loop = b.new_uuid("loop")
    b.add(
        "repeat.each",
        UUID=b.new_uuid("loop-start"),
        GroupingIdentifier=loop,
        WFControlFlowMode=FLOW_START,
        WFInput=attachment(shortcut_input()),
    )
    response = b.new_uuid("upload")
    b.add(
        "downloadurl",
        UUID=response,
        WFURL=token_string(named_variable(SERVER_VARIABLE), UPLOAD_PATH),
        WFHTTPMethod="POST",
        Advanced=True,
        ShowHeaders=True,
        WFHTTPHeaders=dictionary_field([text_item("Authorization", "Bearer ", named_variable(TOKEN_VARIABLE))]),
        WFHTTPBodyType="Form",
        WFFormValues=dictionary_field([file_item(UPLOAD_FIELD, named_variable(REPEAT_ITEM))]),
    )

    # Success is recognised by the answer, not by the status code: Shortcuts
    # does not stop on HTTP errors, and a proxy or login page in front of
    # DocuShelf must not count as "sent".
    check = b.new_uuid("check")
    b.add(
        "conditional",
        GroupingIdentifier=check,
        WFControlFlowMode=FLOW_START,
        WFCondition=CONDITION_CONTAINS,
        WFConditionalActionString=SUCCESS_KEY,
        WFInput={"Type": "Variable", "Variable": attachment(action_output(response, OUTPUT_CONTENTS_OF_URL, as_text=True))},
    )
    b.add("comment", WFCommentActionText=UPLOADED_COMMENT)
    b.add("conditional", GroupingIdentifier=check, WFControlFlowMode=FLOW_OTHERWISE)
    # Read the JSON answer explicitly as a dictionary instead of relying on the
    # automatic conversion of "Get Dictionary Value".
    answer = b.new_uuid("error-answer")
    b.add("detect.dictionary", UUID=answer, WFInput=attachment(action_output(response, OUTPUT_CONTENTS_OF_URL)))
    detail = b.new_uuid("error-detail")
    b.add(
        "getvalueforkey",
        UUID=detail,
        WFInput=attachment(action_output(answer, OUTPUT_DICTIONARY)),
        WFGetDictionaryValueType="Value",
        WFDictionaryKey=ERROR_KEY,
    )
    b.add(
        "notification",
        WFNotificationActionTitle=ERROR_TITLE,
        WFNotificationActionBody=token_string(action_output(detail, OUTPUT_DICTIONARY_VALUE)),
    )
    b.add("exit")
    b.add("conditional", GroupingIdentifier=check, WFControlFlowMode=FLOW_END)
    b.add("repeat.each", UUID=b.new_uuid("loop-end"), GroupingIdentifier=loop, WFControlFlowMode=FLOW_END)

    b.add("notification", WFNotificationActionTitle=DONE_TITLE, WFNotificationActionBody=DONE_BODY)

    return {
        "WFWorkflowName": name,
        "WFWorkflowActions": b.actions,
        "WFWorkflowImportQuestions": b.questions,
        "WFWorkflowTypes": list(WORKFLOW_TYPES),
        "WFWorkflowInputContentItemClasses": list(INPUT_CONTENT_CLASSES),
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowHasShortcutInputVariables": True,
        "WFWorkflowHasOutputFallback": False,
        "WFWorkflowNoInputBehavior": NO_INPUT_BEHAVIOR,
        "WFWorkflowIcon": {
            "WFWorkflowIconGlyphNumber": ICON_GLYPH_UPLOAD,
            "WFWorkflowIconStartColor": ICON_COLOR_DARK_BLUE,
        },
        "WFWorkflowClientVersion": CLIENT_VERSION,
        "WFWorkflowMinimumClientVersion": MINIMUM_CLIENT_VERSION,
        "WFWorkflowMinimumClientVersionString": str(MINIMUM_CLIENT_VERSION),
    }


def shortcut_bytes(name: str = DEFAULT_NAME) -> bytes:
    """The unsigned shortcut file (binary property list)."""
    return plistlib.dumps(build_workflow(name), fmt=plistlib.FMT_BINARY, sort_keys=True)


def write_shortcut(path: Union[str, Path], name: str = DEFAULT_NAME) -> Path:
    """Write the unsigned shortcut to ``path`` (atomically) and return the path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = shortcut_bytes(name)
    fd, tmp = tempfile.mkstemp(prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return path


# --- Command line -------------------------------------------------------------------


def _name_problem(name: str) -> Optional[str]:
    if not name:
        return "Der Name darf nicht leer sein."
    if "/" in name or "\\" in name or any(not char.isprintable() for char in name):
        return "Der Name darf keine Schrägstriche und keine Steuerzeichen enthalten."
    return None


def output_path(raw: Optional[str], name: str) -> Path:
    """The file to write: ``--output`` itself, or ``<name>.shortcut`` in it if it is a directory."""
    if not raw:
        return Path(name + FILE_SUFFIX)
    path = Path(raw).expanduser()
    if path.is_dir():
        return path / (name + FILE_SUFFIX)
    return path


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Erzeugt den Kurzbefehl „%s“ (unsigniert) für iPhone, iPad und Mac: Teilen-Menü für PDFs, "
            "Bilder und Dateien, Upload an <Server>%s mit API-Token." % (DEFAULT_NAME, UPLOAD_PATH)
        ),
        epilog=(
            "Danach auf einem Mac signieren, z. B.: shortcuts sign --mode anyone "
            "--input \"%s%s\" --output \"signed/%s%s\" (siehe contrib/ios/README.md)."
            % (DEFAULT_NAME, FILE_SUFFIX, DEFAULT_NAME, FILE_SUFFIX)
        ),
        add_help=False,
    )
    parser.add_argument("-h", "--help", action="help", default=argparse.SUPPRESS, help="diese Hilfe anzeigen")
    parser.add_argument(
        "--output",
        "-o",
        metavar="DATEI",
        help="Zieldatei oder vorhandener Ordner (Standard: „<Name>.shortcut“ im aktuellen Ordner)",
    )
    parser.add_argument(
        "--name",
        default=DEFAULT_NAME,
        help="Name des Kurzbefehls, so erscheint er im Teilen-Menü (Standard: „%s“)" % DEFAULT_NAME,
    )
    args = parser.parse_args(argv)
    name = args.name.strip()
    problem = _name_problem(name)
    if problem:
        parser.error(problem)
    target = output_path(args.output, name)
    try:
        write_shortcut(target, name)
    except OSError as exc:
        print("Kann %s nicht schreiben: %s" % (target, exc.strerror or exc), file=sys.stderr)
        return 1
    print("Kurzbefehl „%s“ unsigniert geschrieben: %s" % (name, target))
    print(
        "Zum Importieren erst auf einem Mac signieren: shortcuts sign --mode anyone --input \"%s\" "
        "--output \"signed/%s\"" % (target, target.name)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
