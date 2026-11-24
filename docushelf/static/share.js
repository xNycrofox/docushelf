// DocuShelf – sending documents from phones.
//
// Files shared to the installed app are kept by the service worker (sw.js) in
// the Cache Storage and shown on /teilen. This script lists them there,
// uploads them through /api/v1/upload (session cookie plus CSRF header) and
// removes them from the cache once the server has them. On every page that
// loads it, it also registers the service worker and offers the browser's
// install dialog where the browser has one.
//
// No dependencies and no inline code: the Content-Security-Policy forbids it.

"use strict";

const SHARE_CACHE = "docushelf-teilen";
const NAME_HEADER = "X-DocuShelf-Name";
const WORKER_URL = "/sw.js";
// app.js watches the processing of new uploads on the inbox page when this is set.
const UPLOAD_KEY = "docushelf:uploaded-at";
const COMBINE_KEY = "docushelf:share-combine";
const IMAGE_NAME = /\.(jpe?g|jpe|jfif|png|tiff?|heic|heif|hif|webp|gif|bmp|avif)$/i;
const PREVIEW_TYPES = /^image\/(jpeg|png|gif|webp|avif)$/;
const SVG_NS = "http://www.w3.org/2000/svg";

// ------------------------------------------------------------------ data

function isImage(file) {
  return String(file.type || "").startsWith("image/") || IMAGE_NAME.test(String(file.name || ""));
}

// The requests of one upload. With "combine" all images become one document and
// every other file stays a document of its own – the same rule as the upload area.
function uploadBatches(entries, combine) {
  const images = entries.filter((entry) => isImage(entry.file));
  const others = entries.filter((entry) => !isImage(entry.file));
  if (combine && images.length > 1) {
    return [
      [others, false],
      [images, true],
    ].filter(([batch]) => batch.length > 0);
  }
  return entries.length ? [[entries, false]] : [];
}

function fileName(header, fallback) {
  if (!header) return fallback;
  try {
    return decodeURIComponent(header) || fallback;
  } catch {
    return header;
  }
}

// The files the service worker has kept, oldest first: [{request, file}].
async function readShared(cacheName = SHARE_CACHE) {
  const cache = await caches.open(cacheName);
  const entries = [];
  for (const request of await cache.keys()) {
    const response = await cache.match(request);
    if (!response) continue;
    const blob = await response.blob();
    const type = response.headers.get("Content-Type") || blob.type || "";
    const name = fileName(response.headers.get(NAME_HEADER), `datei-${entries.length + 1}`);
    entries.push({ request, file: new File([blob], name, { type }) });
  }
  return { cache, entries };
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} Bytes`;
  let size = bytes;
  let unit = "";
  for (const candidate of ["KB", "MB", "GB"]) {
    size /= 1024;
    unit = candidate;
    if (size < 1024) break;
  }
  return `${(size < 10 ? size.toFixed(1) : Math.round(size)).toString().replace(".", ",")} ${unit}`;
}

function plural(count, singular, pluralForm) {
  return `${count} ${count === 1 ? singular : pluralForm}`;
}

async function errorText(response, fallback) {
  try {
    const data = await response.json();
    if (typeof data.detail === "string" && data.detail) return data.detail;
  } catch {
    /* not JSON */
  }
  return `${fallback} (HTTP ${response.status})`;
}

function recall(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function remember(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* storage unavailable */
  }
}

// ------------------------------------------------------------------- DOM

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function icon(name, cls = "") {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", `icon ${cls}`.trim());
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", `${document.body.dataset.icons || "/static/icons.svg"}#${name}`);
  svg.append(use);
  return svg;
}

function csrfToken() {
  return document.querySelector('meta[name="csrf-token"]')?.content || "";
}

// Several households: the household the page belongs to.
function householdHeaders() {
  const id = document.querySelector('meta[name="household"]')?.content;
  return id ? { "X-DocuShelf-Household": id } : {};
}

// The confirmation dialog of the layout (base.html); window.confirm without it.
function confirmAction(message, ok) {
  const dialog = document.getElementById("confirm-dialog");
  if (!dialog || typeof dialog.showModal !== "function") return Promise.resolve(window.confirm(message));
  dialog.querySelector(".confirm-message").textContent = message;
  dialog.querySelector(".confirm-ok").textContent = ok;
  dialog.classList.add("is-danger");
  dialog.returnValue = "";
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
    dialog.showModal();
    dialog.querySelector('button[value="cancel"]')?.focus();
  });
}

// ------------------------------------------------- worker and installation

async function registerWorker() {
  if (!("serviceWorker" in navigator) || !window.isSecureContext) return null;
  try {
    return await navigator.serviceWorker.register(WORKER_URL, { scope: "/" });
  } catch (error) {
    console.warn("DocuShelf: the service worker could not be registered", error);
    return null;
  }
}

function runsInstalled() {
  return Boolean(window.matchMedia?.("(display-mode: standalone)")?.matches) || navigator.standalone === true;
}

// "App installieren" appears only where the browser offers its install dialog.
function initInstall() {
  const offers = Array.from(document.querySelectorAll("[data-share-install]"));
  const installed = Array.from(document.querySelectorAll("[data-share-installed]"));
  const show = (nodes, visible) => nodes.forEach((node) => (node.hidden = !visible));
  let deferred = null;
  show(installed, runsInstalled());
  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    deferred = event;
    show(offers, true);
  });
  window.addEventListener("appinstalled", () => {
    deferred = null;
    show(offers, false);
    show(installed, true);
  });
  for (const offer of offers) {
    offer.querySelector("button")?.addEventListener("click", async () => {
      if (!deferred) return;
      const prompt = deferred;
      deferred = null;
      show(offers, false);
      try {
        await prompt.prompt();
      } catch {
        /* the browser refused to show it again */
      }
    });
  }
}

// ------------------------------------------------------------ share page

function initSharePage(root) {
  const find = (selector) => root.querySelector(selector);
  const api = root.dataset.api || "/api/v1/upload";
  const cacheName = root.dataset.cache || SHARE_CACHE;
  const maxMb = Number(root.dataset.maxMb || 200);
  const zone = find("[data-share-zone]");
  const status = find("[data-share-status]");
  const message = find("[data-share-message]");
  const list = find("[data-share-list]");
  const count = find("[data-share-count]");
  const combineOption = find("[data-share-combine]");
  const combine = combineOption.querySelector("input");
  const actions = find("[data-share-actions]");
  const uploadButton = find("[data-share-upload]");
  const discardButton = find("[data-share-discard]");
  const progressBox = find("[data-share-progress]");
  const progress = progressBox.querySelector("progress");
  const progressText = progressBox.querySelector(".upload-progress-text");
  const done = find("[data-share-done]");
  const help = document.querySelector("[data-share-help]");
  let cache = null;
  let entries = [];
  let busy = false;

  combine.checked = recall(COMBINE_KEY) === "1";
  combine.addEventListener("change", () => remember(COMBINE_KEY, combine.checked ? "1" : "0"));

  function setStatus(text) {
    status.textContent = text;
    status.hidden = !text;
  }

  function showMessage(kind, text) {
    message.replaceChildren();
    message.hidden = !text;
    if (!text) return;
    const name = { success: "check-circle", error: "alert-circle", warning: "alert", info: "info" }[kind] || "info";
    message.append(
      el("div", { class: `alert alert-${kind}`, role: kind === "error" ? "alert" : "status" }, icon(name), el("p", { text })),
    );
  }

  function row(entry) {
    const { file } = entry;
    const thumb = el("span", { class: "upload-thumb" });
    if (PREVIEW_TYPES.test(file.type) && file.size < 20e6) {
      const url = URL.createObjectURL(file);
      const img = el("img", { src: url, alt: "" });
      const release = () => URL.revokeObjectURL(url);
      img.addEventListener("load", release, { once: true });
      img.addEventListener("error", release, { once: true });
      thumb.append(img);
    } else {
      thumb.append(icon(isImage(file) ? "camera" : "file-text"));
    }
    const remove = el(
      "button",
      { type: "button", class: "btn btn-ghost btn-icon btn-small", "aria-label": `${file.name} entfernen` },
      icon("x"),
    );
    remove.addEventListener("click", async () => {
      if (busy) return;
      await cache.delete(entry.request).catch(() => false);
      entries = entries.filter((other) => other !== entry);
      showMessage("", "");
      render();
    });
    return el(
      "li",
      { class: "upload-item" },
      thumb,
      el("span", { class: "upload-name", text: file.name, title: file.name }),
      el("span", { class: "upload-size", text: formatSize(file.size) }),
      remove,
    );
  }

  function render() {
    const images = entries.filter((entry) => isImage(entry.file)).length;
    list.replaceChildren(...entries.map(row));
    list.hidden = entries.length === 0;
    actions.hidden = entries.length === 0;
    combineOption.hidden = images < 2;
    // The upload area's own rules (app.css) show the actions and the combine option.
    zone.classList.toggle("has-files", entries.length > 0);
    zone.classList.toggle("has-images", images > 1);
    count.textContent = String(entries.length);
    count.hidden = entries.length === 0;
    if (help) help.hidden = entries.length > 0;
    uploadButton.replaceChildren(icon("upload"), entries.length > 1 ? `${entries.length} Dateien hochladen` : "Hochladen");
    if (entries.length === 1) {
      setStatus("Diese Datei wurde mit DocuShelf geteilt. Lade sie hoch, damit sie im Eingang landet.");
    } else if (entries.length > 1) {
      setStatus(`Diese ${entries.length} Dateien wurden mit DocuShelf geteilt. Lade sie hoch, damit sie im Eingang landen.`);
    } else {
      setStatus("Gerade liegen keine geteilten Dateien bereit. Teile PDFs oder Fotos auf dem Handy über „Teilen“ an DocuShelf.");
    }
  }

  async function load() {
    if (!window.isSecureContext) {
      setStatus("");
      showMessage("info", "Das Teilen an DocuShelf braucht eine sichere Verbindung. Öffne DocuShelf über seine HTTPS-Adresse.");
      return;
    }
    if (!("caches" in window)) {
      setStatus("");
      showMessage(
        "info",
        "Dieser Browser kann keine geteilten Dateien an DocuShelf weitergeben. Hochladen kannst du Dateien jederzeit im Eingang.",
      );
      return;
    }
    try {
      ({ cache, entries } = await readShared(cacheName));
    } catch (error) {
      console.warn("DocuShelf: cannot read the shared files", error);
      setStatus("");
      showMessage("error", "Die geteilten Dateien lassen sich nicht lesen. Bitte teile sie noch einmal.");
      return;
    }
    render();
  }

  function setBusy(value) {
    busy = value;
    uploadButton.disabled = value;
    uploadButton.classList.toggle("is-busy", value);
    discardButton.disabled = value;
    combine.disabled = value;
    progressBox.hidden = !value;
  }

  function fail(text) {
    setBusy(false);
    render();
    showMessage("error", text);
  }

  async function send(batch, combined) {
    const data = new FormData();
    for (const entry of batch) data.append("files", entry.file, entry.file.name);
    if (combined) data.append("combine", "true");
    try {
      return await fetch(api, {
        method: "POST",
        body: data,
        credentials: "same-origin",
        headers: { "X-CSRF-Token": csrfToken(), Accept: "application/json", ...householdHeaders() },
      });
    } catch {
      return null;
    }
  }

  async function upload() {
    if (busy || !entries.length) return;
    const total = entries.reduce((sum, entry) => sum + entry.file.size, 0);
    if (total > maxMb * 1024 * 1024) {
      showMessage("error", `Die Dateien sind zu groß – zusammen höchstens ${maxMb} MB. Entferne einzelne Dateien und versuche es noch einmal.`);
      return;
    }
    showMessage("", "");
    setBusy(true);
    progress.value = 0;
    progressText.textContent = "Wird hochgeladen …";
    let sent = 0;
    let uploaded = 0;
    for (const [batch, combined] of uploadBatches(entries, combine.checked)) {
      const response = await send(batch, combined);
      if (!response || !response.ok) {
        let text;
        if (!response) text = "Keine Verbindung zum Server. Die Dateien bleiben hier, bis du es noch einmal versuchst.";
        else if (response.status === 401) {
          text = "Deine Anmeldung ist abgelaufen. Lade die Seite neu und melde dich an – die Dateien bleiben so lange hier.";
        } else text = await errorText(response, "Hochladen fehlgeschlagen.");
        fail(uploaded ? `${text} ${plural(uploaded, "Datei ist", "Dateien sind")} schon hochgeladen.` : text);
        return;
      }
      await Promise.all(batch.map((entry) => cache.delete(entry.request).catch(() => false)));
      entries = entries.filter((entry) => !batch.includes(entry));
      uploaded += batch.length;
      sent += batch.reduce((sum, entry) => sum + entry.file.size, 0);
      progress.value = Math.round((sent / (total || 1)) * 100);
    }
    try {
      sessionStorage.setItem(UPLOAD_KEY, String(Date.now()));
    } catch {
      /* storage unavailable */
    }
    setBusy(false);
    render();
    setStatus("Alles hochgeladen.");
    if (help) help.hidden = true;
    showMessage(
      "success",
      uploaded === 1
        ? "1 Datei hochgeladen – sie wird jetzt verarbeitet und erscheint gleich im Eingang."
        : `${uploaded} Dateien hochgeladen – sie werden jetzt verarbeitet und erscheinen gleich im Eingang.`,
    );
    done.hidden = false;
    done.querySelector("a")?.focus();
  }

  async function discard() {
    if (busy || !entries.length) return;
    const question =
      entries.length === 1
        ? "Die geteilte Datei verwerfen? Auf deinem Gerät bleibt sie erhalten."
        : `Die ${entries.length} geteilten Dateien verwerfen? Auf deinem Gerät bleiben sie erhalten.`;
    if (!(await confirmAction(question, "Verwerfen"))) return;
    await Promise.all(entries.map((entry) => cache.delete(entry.request).catch(() => false)));
    entries = [];
    render();
    showMessage("info", "Die geteilten Dateien wurden verworfen.");
  }

  uploadButton.addEventListener("click", upload);
  discardButton.addEventListener("click", discard);
  load();
}

// ------------------------------------------------------------------ start

function start() {
  registerWorker();
  initInstall();
  const root = document.querySelector("[data-share]");
  if (root) initSharePage(root);
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, { once: true });
  else start();
}
