"""The scripts for sharing from phones (static/sw.js, static/share.js), run with Node.js.

The service worker is loaded into a sandbox with an in-memory Cache Storage and
receives the requests a share sheet sends. ``share.js`` then reads the same
cache, so the tests also cover the contract between the two scripts (cache
name, file name header). Skipped when ``node`` is not installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "docushelf" / "static"
SCRIPTS = (STATIC / "sw.js", STATIC / "share.js")
NODE = shutil.which("node")

requires_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

HARNESS = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");

const [swPath, sharePath] = process.argv.slice(2);

class MemoryCache {
  constructor() { this.entries = new Map(); }
  async put(request, response) {
    const body = await response.arrayBuffer();
    this.entries.set(request.url, { request, status: response.status, headers: new Headers(response.headers), body });
  }
  async keys() { return [...this.entries.values()].map((entry) => entry.request); }
  async match(request) {
    const entry = this.entries.get(typeof request === "string" ? request : request.url);
    return entry ? new Response(entry.body.slice(0), { status: entry.status, headers: entry.headers }) : undefined;
  }
  async delete(request) { return this.entries.delete(typeof request === "string" ? request : request.url); }
}

const stores = new Map();
const caches = {
  async open(name) {
    if (!stores.has(name)) stores.set(name, new MemoryCache());
    return stores.get(name);
  },
};

const calls = [];
const listeners = {};
const worker = {
  location: new URL("https://docs.example.test/sw.js"),
  addEventListener(type, handler) { (listeners[type] = listeners[type] || []).push(handler); },
  skipWaiting() { calls.push("skipWaiting"); return Promise.resolve(); },
  clients: { claim() { calls.push("claim"); return Promise.resolve(); } },
};
const host = { caches, Request, Response, Headers, URL, FormData, File, Blob, console };
const swContext = vm.createContext({ ...host, self: worker });
vm.runInContext(fs.readFileSync(swPath, "utf8"), swContext, { filename: "sw.js" });

function lifecycle(type) {
  const waits = [];
  for (const handler of listeners[type] || []) handler({ waitUntil: (promise) => waits.push(promise) });
  return Promise.all(waits);
}

// Returns null when the worker lets the request through to the network.
async function dispatch(request) {
  let answer = null;
  for (const handler of listeners.fetch || []) handler({ request, respondWith: (promise) => { answer = promise; } });
  if (!answer) return null;
  const response = await answer;
  return { status: response.status, location: response.headers.get("Location") };
}

function sharePost(form, init = {}) {
  return new Request("https://docs.example.test/teilen", { method: "POST", body: form, ...init });
}

async function main() {
  const result = {};
  await lifecycle("install");
  await lifecycle("activate");
  result.lifecycle = calls;

  result.passThrough = {
    get: await dispatch(new Request("https://docs.example.test/teilen")),
    page: await dispatch(new Request("https://docs.example.test/eingang")),
    upload: await dispatch(new Request("https://docs.example.test/api/v1/upload", { method: "POST", body: new FormData() })),
    otherSite: await dispatch(new Request("https://elsewhere.example.test/teilen", { method: "POST", body: new FormData() })),
  };

  const form = new FormData();
  form.append("title", "Rechnung");
  form.append("text", "von der Mail-App");
  form.append("files", new File([new Uint8Array([37, 80, 68, 70, 45])], "Brief März.pdf", { type: "application/pdf" }));
  form.append("files", new File([new Uint8Array([255, 216, 255, 224])], "image.jpeg", { type: "image/jpeg" }));
  form.append("files", new File([new Uint8Array([255, 216, 255, 224, 1])], "image.jpeg", { type: "image/jpeg" }));
  form.append("files", new File([], "leer.pdf", { type: "application/pdf" }));
  result.share = await dispatch(sharePost(form));

  const textOnly = new FormData();
  textOnly.append("title", "Ein Link");
  textOnly.append("text", "https://example.org");
  result.textOnly = await dispatch(sharePost(textOnly));
  result.broken = await dispatch(
    sharePost("kein multipart", { headers: { "Content-Type": "multipart/form-data; boundary=fehlt" } }),
  );

  const stored = stores.get("docushelf-teilen");
  result.cacheNames = [...stores.keys()];
  result.stored = [];
  for (const [url, entry] of stored ? stored.entries : []) {
    result.stored.push({ url, type: entry.headers.get("Content-Type"), name: entry.headers.get("X-DocuShelf-Name"), size: entry.body.byteLength });
  }

  // The share page reads what the worker kept (no document: share.js does not start its UI).
  const pageContext = vm.createContext({ ...host });
  vm.runInContext(fs.readFileSync(sharePath, "utf8"), pageContext, { filename: "share.js" });
  const { entries } = await pageContext.readShared("docushelf-teilen");
  result.read = entries.map(({ request, file }) => ({ url: request.url, name: file.name, type: file.type, size: file.size }));

  const files = [
    { file: { name: "Brief.pdf", type: "application/pdf" } },
    { file: { name: "IMG_0001.HEIC", type: "" } },
    { file: { name: "image.jpeg", type: "image/jpeg" } },
    { file: { name: "scan.tif", type: "" } },
  ];
  const names = (batches) => batches.map(([batch, combined]) => [batch.map((entry) => entry.file.name), combined]);
  result.batches = {
    separate: names(pageContext.uploadBatches(files, false)),
    combined: names(pageContext.uploadBatches(files, true)),
    onlyOneImage: names(pageContext.uploadBatches(files.slice(0, 2), true)),
    onlyImages: names(pageContext.uploadBatches(files.slice(1), true)),
    none: names(pageContext.uploadBatches([], true)),
  };
  result.fileName = [pageContext.fileName("Brief%20M%C3%A4rz.pdf", "x"), pageContext.fileName("%E0%A4%A", "x"), pageContext.fileName(null, "datei-1")];
  process.stdout.write(JSON.stringify(result));
}

main().catch((error) => {
  process.stderr.write(String(error && error.stack || error));
  process.exit(1);
});
"""


@pytest.fixture(scope="module")
def outcome(tmp_path_factory) -> dict:
    if NODE is None:
        pytest.skip("node is not installed")
    harness = tmp_path_factory.mktemp("node") / "harness.cjs"
    harness.write_text(HARNESS, encoding="utf-8")
    done = subprocess.run(
        [NODE, str(harness), *(str(path) for path in SCRIPTS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@requires_node
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_scripts_are_valid_javascript(script):
    done = subprocess.run([NODE, "--check", str(script)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


def test_scripts_use_no_inline_or_external_code():
    for script in SCRIPTS:
        text = script.read_text(encoding="utf-8")
        assert "eval(" not in text and "new Function" not in text and "innerHTML" not in text
        # No external resources (the SVG namespace is a name, not something that is loaded).
        text = text.replace('"http://www.w3.org/2000/svg"', "")
        assert "http://" not in text and "https://" not in text


def test_worker_takes_over_at_once(outcome):
    assert outcome["lifecycle"] == ["skipWaiting", "claim"]


def test_worker_only_handles_shares(outcome):
    assert outcome["passThrough"] == {"get": None, "page": None, "upload": None, "otherSite": None}


def test_worker_keeps_shared_files_and_redirects(outcome):
    assert outcome["share"] == {"status": 303, "location": "https://docs.example.test/teilen"}
    assert outcome["cacheNames"] == ["docushelf-teilen"]
    stored = outcome["stored"]
    assert [(entry["type"], entry["name"], entry["size"]) for entry in stored] == [
        ("application/pdf", "Brief%20M%C3%A4rz.pdf", 5),
        ("image/jpeg", "image.jpeg", 4),
        ("image/jpeg", "image.jpeg", 5),
    ]  # the empty file is dropped, texts are not kept
    assert all(entry["url"].startswith("https://docs.example.test/teilen/datei/") for entry in stored)
    assert len({entry["url"] for entry in stored}) == 3


def test_worker_explains_shares_without_files(outcome):
    assert outcome["textOnly"] == {"status": 303, "location": "https://docs.example.test/teilen?hinweis=leer"}
    assert outcome["broken"] == {"status": 303, "location": "https://docs.example.test/teilen?hinweis=fehler"}


def test_share_page_reads_what_the_worker_kept(outcome):
    assert outcome["read"] == [
        {"url": entry["url"], "name": name, "type": entry["type"], "size": entry["size"]}
        for entry, name in zip(outcome["stored"], ["Brief März.pdf", "image.jpeg", "image.jpeg"])
    ]
    assert outcome["fileName"] == ["Brief März.pdf", "%E0%A4%A", "datei-1"]


def test_upload_batches_combine_only_images(outcome):
    batches = outcome["batches"]
    assert batches["separate"] == [[["Brief.pdf", "IMG_0001.HEIC", "image.jpeg", "scan.tif"], False]]
    assert batches["combined"] == [[["Brief.pdf"], False], [["IMG_0001.HEIC", "image.jpeg", "scan.tif"], True]]
    assert batches["onlyOneImage"] == [[["Brief.pdf", "IMG_0001.HEIC"], False]]
    assert batches["onlyImages"] == [[["IMG_0001.HEIC", "image.jpeg", "scan.tif"], True]]
    assert batches["none"] == []
