// DocuShelf – progressive enhancement for the server-rendered pages.
//
// Every page works without this file. It adds drag and drop uploads, live
// progress of the inbox, the folder combobox, tag chips and small comforts.
// No dependencies and no inline code: the Content-Security-Policy forbids it.

const doc = document;
doc.documentElement.classList.add("js");

const $ = (selector, root = doc) => root.querySelector(selector);
const $$ = (selector, root = doc) => Array.from(root.querySelectorAll(selector));
const SVG_NS = "http://www.w3.org/2000/svg";
const TOAST_KEY = "docushelf:toasts";
const UPLOAD_KEY = "docushelf:uploaded-at";
const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || "");

const csrfToken = () => $('meta[name="csrf-token"]')?.content || "";
// Several households: the household this page belongs to (the server refuses the
// request if the session has switched to another household in the meantime).
const householdHeaders = () => {
  const id = $('meta[name="household"]')?.content;
  return id ? { "X-DocuShelf-Household": id } : {};
};
const iconHref = (name) => `${doc.body.dataset.icons || "/static/icons.svg"}#${name}`;

function icon(name, cls = "") {
  const svg = doc.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", `icon ${cls}`.trim());
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  const use = doc.createElementNS(SVG_NS, "use");
  use.setAttribute("href", iconHref(name));
  svg.append(use);
  return svg;
}

function el(tag, attrs = {}, ...children) {
  const node = doc.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : doc.createTextNode(String(child)));
  }
  return node;
}

// Same idea as the server's text normalization: case, umlauts and accents do not matter.
function fold(text) {
  return String(text || "")
    .toLowerCase()
    .replace(/ä/g, "ae").replace(/ö/g, "oe").replace(/ü/g, "ue").replace(/ß/g, "ss")
    .normalize("NFKD").replace(/[̀-ͯ]/g, "");
}

function readJson(id) {
  const node = doc.getElementById(id);
  if (!node) return [];
  try {
    const value = JSON.parse(node.textContent || "[]");
    return Array.isArray(value) ? value : [];
  } catch {
    return [];
  }
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} Bytes`;
  const units = ["KB", "MB", "GB"];
  let size = bytes;
  let unit = "";
  for (const candidate of units) {
    size /= 1024;
    unit = candidate;
    if (size < 1024) break;
  }
  return `${(size < 10 ? size.toFixed(1) : Math.round(size)).toString().replace(".", ",")} ${unit}`;
}

async function errorMessage(response, fallback = "Das hat nicht geklappt.") {
  try {
    const data = await response.json();
    if (typeof data.detail === "string") return data.detail;
    if (Array.isArray(data.detail)) return data.detail.map((d) => d.msg || String(d)).join(", ");
    if (typeof data.message === "string") return data.message;
    if (typeof data.error === "string") return data.error;
  } catch {
    /* not JSON */
  }
  return `${fallback} (HTTP ${response.status})`;
}

// ---------------------------------------------------------------- toasts

function dismiss(node) {
  if (!node || node.classList.contains("is-leaving")) return;
  node.classList.add("is-leaving");
  node.addEventListener("animationend", () => node.remove(), { once: true });
  setTimeout(() => node.remove(), 450);
}

function toast(message, category = "success", { timeout } = {}) {
  const box = $("#toasts");
  if (!box || !message) return;
  const name = { success: "check-circle", error: "alert-circle", warning: "alert", info: "info" }[category] || "info";
  const close = el("button", { type: "button", class: "flash-close", "aria-label": "Meldung schließen" }, icon("x"));
  const item = el(
    "div",
    { class: `flash flash-${category}`, role: category === "error" ? "alert" : "status" },
    icon(name, "flash-icon"),
    el("p", { class: "flash-text", text: message }),
    close,
  );
  close.addEventListener("click", () => dismiss(item));
  box.append(item);
  const ms = timeout ?? (category === "error" ? 10000 : 5000);
  if (ms) setTimeout(() => dismiss(item), ms);
}

// Toasts that should appear after the next page load (e.g. after an upload).
function toastAfterReload(message, category = "success") {
  try {
    const queued = JSON.parse(sessionStorage.getItem(TOAST_KEY) || "[]");
    queued.push({ message, category });
    sessionStorage.setItem(TOAST_KEY, JSON.stringify(queued.slice(-4)));
  } catch {
    /* storage unavailable */
  }
}

function showStoredToasts() {
  try {
    const queued = JSON.parse(sessionStorage.getItem(TOAST_KEY) || "[]");
    sessionStorage.removeItem(TOAST_KEY);
    for (const { message, category } of queued) toast(message, category);
  } catch {
    /* storage unavailable */
  }
}

function initFlashes() {
  for (const flash of $$(".flashes .flash")) {
    $("[data-dismiss]", flash)?.addEventListener("click", () => dismiss(flash));
    if (flash.dataset.autohide === "true" && !$("form", flash)) setTimeout(() => dismiss(flash), 7000);
  }
}

// ------------------------------------------------------- confirm dialog

function confirmAction(message, { ok = "Ja", danger = false } = {}) {
  const dialog = $("#confirm-dialog");
  if (!dialog || typeof dialog.showModal !== "function") return Promise.resolve(window.confirm(message));
  $(".confirm-message", dialog).textContent = message;
  const okButton = $(".confirm-ok", dialog);
  okButton.textContent = ok;
  dialog.classList.toggle("is-danger", danger);
  dialog.returnValue = "";
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
    dialog.showModal();
    (danger ? $('button[value="cancel"]', dialog) : okButton).focus();
  });
}

function markBusy(form, submitter) {
  const button = submitter || $('button[type="submit"], button:not([type])', form);
  button?.classList.add("is-busy");
  if (form.dataset.busy) toast(form.dataset.busy, "info", { timeout: 0 });
}

function initForms() {
  // Confirmation for destructive actions (forms with data-confirm).
  doc.addEventListener(
    "submit",
    async (event) => {
      const form = event.target;
      if (!(form instanceof HTMLFormElement) || !form.dataset.confirm || form.dataset.confirmed === "1") return;
      event.preventDefault();
      event.stopImmediatePropagation();
      const okLabel = form.dataset.confirmOk || "Ja";
      const danger = "confirmDanger" in form.dataset || /lösch|widerruf|papierkorb/i.test(okLabel);
      if (!(await confirmAction(form.dataset.confirm, { ok: okLabel, danger }))) return;
      form.dataset.confirmed = "1";
      if (event.submitter && form.requestSubmit) form.requestSubmit(event.submitter);
      else if (form.requestSubmit) form.requestSubmit();
      else form.submit();
    },
    true,
  );

  // Feedback while a normal POST is on its way; prevents double submits.
  doc.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || event.defaultPrevented) return;
    if ((form.method || "").toLowerCase() !== "post") return;
    if (form.dataset.submitting === "1") {
      event.preventDefault();
      return;
    }
    form.dataset.submitting = "1";
    markBusy(form, event.submitter);
  });

  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    for (const node of $$(".is-busy")) node.classList.remove("is-busy");
    for (const form of $$("form")) {
      delete form.dataset.submitting;
      delete form.dataset.confirmed;
    }
  });

  for (const select of $$("select[data-autosubmit]")) {
    select.addEventListener("change", () => select.form?.requestSubmit());
  }

  for (const button of $$("[data-copy]")) {
    button.addEventListener("click", async () => {
      const target = $(button.dataset.copy);
      const text = target?.value ?? target?.textContent ?? "";
      try {
        await navigator.clipboard.writeText(text);
      } catch {
        target?.select?.();
        try {
          doc.execCommand("copy");
        } catch {
          toast("Kopieren ging nicht – bitte von Hand markieren.", "warning");
          return;
        }
      }
      toast("In die Zwischenablage kopiert.", "success", { timeout: 2500 });
    });
  }

  for (const button of $$("[data-reload]")) button.addEventListener("click", () => window.location.reload());
}

// ------------------------------------------------------ menus, shortcuts

function initMenus() {
  const openMenus = () => $$("details.user-menu[open], details.popover[open]");
  doc.addEventListener("click", (event) => {
    for (const details of openMenus()) if (!details.contains(event.target)) details.open = false;
  });
  doc.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    for (const details of openMenus()) {
      details.open = false;
      $("summary", details)?.focus();
    }
  });
  for (const details of $$("details.popover, details.disclosure")) {
    details.addEventListener("toggle", () => {
      if (details.open) $('input:not([type="hidden"]), textarea', details)?.focus();
    });
  }
  // The folder tree takes a lot of room on small screens: start collapsed there.
  if (window.matchMedia("(max-width: 900px)").matches) {
    for (const panel of $$("details.tree-panel")) panel.open = false;
  }
}

function initShortcuts() {
  for (const hint of $$("[data-shortcut-hint]")) hint.textContent = isMac ? "⌘ + Enter" : "Strg + Enter";
  doc.addEventListener("keydown", (event) => {
    const target = event.target;
    const typing =
      target instanceof HTMLElement && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName));
    if (event.key === "/" && !typing && !event.ctrlKey && !event.metaKey && !event.altKey) {
      const search = $("#search-q") || $("#top-search-input");
      if (search) {
        event.preventDefault();
        search.focus();
        search.select();
      }
      return;
    }
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      const form = (target instanceof Element && target.closest("form[data-filing-form]")) || $("form[data-filing-form]");
      if (form) {
        event.preventDefault();
        form.requestSubmit();
      }
    }
  });
}

// --------------------------------------------------------------- upload

function initUpload() {
  const form = $("form[data-upload]");
  if (!form) return;
  const list = $("[data-upload-list]", form);
  const inputs = $$('input[type="file"]', form);
  const submit = $("[data-upload-submit]", form);
  const progressBox = $("[data-upload-progress]", form);
  const progress = $("progress", progressBox);
  const progressText = $(".upload-progress-text", progressBox);
  const combine = $('input[name="combine"]', form);
  const maxMb = Number(form.dataset.maxMb || 200);
  let files = [];
  let busy = false;

  const isImage = (file) =>
    (file.type || "").startsWith("image/") || /\.(jpe?g|jpe|jfif|png|tiff?|heic|heif|hif|webp|gif|bmp|avif)$/i.test(file.name);
  const isPdf = (file) => file.type === "application/pdf" || /\.pdf$/i.test(file.name);

  // The hidden native inputs are opened through their labels; make those keyboard friendly.
  for (const input of inputs) input.tabIndex = -1;
  for (const label of $$("label.link-label, label.camera-button", form)) {
    label.tabIndex = 0;
    label.setAttribute("role", "button");
    label.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        label.click();
      }
    });
  }

  function render() {
    list.replaceChildren(
      ...files.map((file, index) => {
        const thumb = el("span", { class: "upload-thumb" });
        if (/^image\/(jpeg|png|gif|webp)$/.test(file.type) && file.size < 20e6) {
          const url = URL.createObjectURL(file);
          const img = el("img", { src: url, alt: "" });
          img.addEventListener("load", () => URL.revokeObjectURL(url), { once: true });
          thumb.append(img);
        } else {
          thumb.append(icon(isImage(file) ? "camera" : "file-text"));
        }
        const remove = el(
          "button",
          { type: "button", class: "btn btn-ghost btn-icon btn-small", "aria-label": `${file.name} entfernen` },
          icon("x"),
        );
        remove.addEventListener("click", () => {
          files.splice(index, 1);
          render();
        });
        return el(
          "li",
          { class: "upload-item" },
          thumb,
          el("span", { class: "upload-name", text: file.name }),
          el("span", { class: "upload-size", text: formatSize(file.size) }),
          remove,
        );
      }),
    );
    list.hidden = files.length === 0;
    form.classList.toggle("has-files", files.length > 0);
    form.classList.toggle("has-images", files.filter(isImage).length > 1);
    submit.replaceChildren(icon("upload"), files.length > 1 ? `${files.length} Dateien hochladen` : "Hochladen");
  }

  function add(fileList) {
    const rejected = [];
    for (const file of Array.from(fileList || [])) {
      if (!isPdf(file) && !isImage(file)) {
        rejected.push(file.name);
        continue;
      }
      const duplicate = files.some(
        (f) => f.name === file.name && f.size === file.size && f.lastModified === file.lastModified,
      );
      if (!duplicate) files.push(file);
    }
    if (rejected.length) toast(`Nicht unterstützter Dateityp: ${rejected.join(", ")}`, "warning");
    render();
    if (files.length) submit.focus({ preventScroll: true });
  }

  for (const input of inputs) {
    input.addEventListener("change", () => {
      add(input.files);
      input.value = "";
    });
  }

  // Files can be dropped anywhere on the page.
  let depth = 0;
  const hasFiles = (event) => Array.from(event.dataTransfer?.types || []).includes("Files");
  window.addEventListener("dragenter", (event) => {
    if (!hasFiles(event)) return;
    depth += 1;
    form.classList.add("is-dragover");
  });
  window.addEventListener("dragover", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  });
  window.addEventListener("dragleave", (event) => {
    if (!hasFiles(event)) return;
    depth = Math.max(0, depth - 1);
    if (!depth) form.classList.remove("is-dragover");
  });
  window.addEventListener("drop", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    depth = 0;
    form.classList.remove("is-dragover");
    add(event.dataTransfer.files);
  });

  // A click on the empty area of the zone opens the file picker.
  $(".dropzone-inner", form)?.addEventListener("click", (event) => {
    if (event.target.closest("a, button, label, input")) return;
    inputs[0]?.click();
  });

  function classicSubmit() {
    // The API is not reachable: hand the files to the classic form post.
    try {
      const transfer = new DataTransfer();
      for (const file of files) transfer.items.add(file);
      inputs[0].files = transfer.files;
      form.submit();
    } catch {
      fail("Hochladen ist gerade nicht möglich.");
    }
  }

  function fail(message) {
    busy = false;
    submit.disabled = false;
    progressBox.hidden = true;
    toast(message, "error");
  }

  // Sends one request; resolves with the HTTP status (0 = no connection) and the body.
  function send(batch, combined, onProgress) {
    return new Promise((resolve) => {
      const data = new FormData();
      for (const file of batch) data.append("files", file, file.name);
      if (combined) data.append("combine", "true");
      const request = new XMLHttpRequest();
      request.open("POST", form.dataset.api);
      request.setRequestHeader("X-CSRF-Token", csrfToken());
      for (const [name, value] of Object.entries(householdHeaders())) request.setRequestHeader(name, value);
      request.setRequestHeader("Accept", "application/json");
      request.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable) onProgress(event.loaded);
      });
      request.addEventListener("load", () => resolve({ status: request.status, body: request.responseText }));
      request.addEventListener("error", () => resolve({ status: 0, body: "" }));
      request.send(data);
    });
  }

  async function upload() {
    busy = true;
    submit.disabled = true;
    // "Combine" joins the images; PDFs always stay documents of their own.
    const images = files.filter(isImage);
    const others = files.filter((file) => !isImage(file));
    const batches =
      combine?.checked && images.length > 1
        ? [[others, false], [images, true]].filter(([batch]) => batch.length)
        : [[files, false]];
    const total = files.reduce((sum, file) => sum + file.size, 0) || 1;
    let sent = 0;
    progressBox.hidden = false;
    progress.value = 0;
    progressText.textContent = "Wird hochgeladen …";
    for (const [batch, combined] of batches) {
      const size = batch.reduce((sum, file) => sum + file.size, 0);
      const answer = await send(batch, combined, (loaded) => {
        const percent = Math.min(100, Math.round(((sent + loaded) / total) * 100));
        progress.value = percent;
        progressText.textContent = percent < 100 ? `${percent} %` : "Wird gespeichert …";
      });
      if (answer.status === 0) {
        fail("Keine Verbindung zum Server.");
        return;
      }
      if ((answer.status === 404 || answer.status === 405) && sent === 0) {
        classicSubmit();
        return;
      }
      if (answer.status === 401) {
        fail("Deine Anmeldung ist abgelaufen. Bitte die Seite neu laden und erneut anmelden.");
        return;
      }
      if (answer.status < 200 || answer.status >= 300) {
        let message = `Hochladen fehlgeschlagen (HTTP ${answer.status}).`;
        try {
          const body = JSON.parse(answer.body || "{}");
          if (typeof body.detail === "string") message = body.detail;
          else if (typeof body.message === "string") message = body.message;
        } catch {
          /* not JSON */
        }
        fail(sent ? `${message} Ein Teil der Dateien wurde bereits hochgeladen.` : message);
        if (sent) setTimeout(() => window.location.reload(), 2500);
        return;
      }
      sent += size;
    }
    const count = files.length;
    toastAfterReload(
      count === 1 ? "Datei hochgeladen – wird jetzt verarbeitet." : `${count} Dateien hochgeladen – werden jetzt verarbeitet.`,
    );
    try {
      sessionStorage.setItem(UPLOAD_KEY, String(Date.now()));
    } catch {
      /* storage unavailable */
    }
    progress.value = 100;
    progressText.textContent = "Fertig!";
    files = [];
    setTimeout(() => window.location.reload(), 700);
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (busy) return;
    if (!files.length) {
      inputs[0]?.click();
      return;
    }
    const total = files.reduce((sum, file) => sum + file.size, 0);
    if (total > maxMb * 1024 * 1024) {
      toast(`Die Dateien sind zu groß – zusammen höchstens ${maxMb} MB.`, "error");
      return;
    }
    upload();
  });
  render();
}

// ------------------------------------------------------ inbox: batch filing

function batchResult(data, chunk) {
  // Tolerant reading of the API answer: a list of per-item results, or filed/failed lists.
  const result = { filedIds: [], failures: [] };
  const describe = (entry) => {
    const error = entry?.error ?? entry?.message ?? entry?.detail;
    const text = typeof error === "string" ? error : error?.message || "Ablage fehlgeschlagen";
    const title = entry?.title ? `„${entry.title}“: ` : "";
    return title + text;
  };
  const entries = Array.isArray(data) ? data : data?.results || data?.items || null;
  if (Array.isArray(entries)) {
    entries.forEach((entry, index) => {
      const id = (typeof entry === "string" ? entry : entry?.id || entry?.item_id || entry?.item) || chunk[index];
      const failed =
        entry && typeof entry === "object" && (entry.ok === false || entry.error || /error|fail/.test(entry.status || ""));
      if (failed) result.failures.push(describe(entry));
      else result.filedIds.push(id);
    });
    return result;
  }
  if (data && (Array.isArray(data.filed) || Array.isArray(data.failed) || Array.isArray(data.errors))) {
    for (const entry of data.filed || []) {
      result.filedIds.push(typeof entry === "string" ? entry : entry?.item || entry?.id || entry?.item_id);
    }
    for (const entry of [...(data.failed || []), ...(data.errors || [])]) {
      result.failures.push(typeof entry === "string" ? entry : describe(entry));
    }
    return result;
  }
  result.filedIds = [...chunk];
  return result;
}

function initBatch() {
  const form = $("form[data-batch]");
  if (!form) return;
  const boxes = $$("input[data-select]", form).filter((box) => !box.disabled);
  const all = $("[data-select-all]", form);
  const bar = $("[data-bulk-bar]", form);
  const counter = $("[data-selected-count]", form);
  const submit = $("[data-batch-submit]", form);
  let last = null;

  // "Select all" leaves out possible duplicates: they deserve a look of their own.
  const eligible = boxes.filter((box) => !box.closest(".inbox-card")?.classList.contains("has-warning"));
  const selected = () => boxes.filter((box) => box.checked);
  function update() {
    const count = selected().length;
    counter.textContent = String(count);
    bar.classList.toggle("is-visible", count > 0);
    for (const box of boxes) box.closest(".inbox-card")?.classList.toggle("is-selected", box.checked);
    if (all) {
      const allEligible = eligible.length > 0 && eligible.every((box) => box.checked);
      all.checked = count > 0 && allEligible;
      all.indeterminate = count > 0 && !allEligible;
      all.disabled = boxes.length === 0;
    }
    submit.disabled = count === 0;
  }

  form.addEventListener("click", (event) => {
    const box = event.target.closest?.("input[data-select]");
    if (!box) return;
    if (event.shiftKey && last && last !== box) {
      const [from, to] = [boxes.indexOf(last), boxes.indexOf(box)].sort((a, b) => a - b);
      for (const other of boxes.slice(from, to + 1)) other.checked = box.checked;
    }
    last = box;
    update();
  });
  form.addEventListener("change", (event) => {
    if (event.target === all) {
      if (all.checked) for (const box of eligible) box.checked = true;
      else for (const box of boxes) box.checked = false;
    }
    update();
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    // Each item carries the fingerprint of the suggestion shown on this page; the
    // server refuses items whose suggestion changed (or that became duplicates) since.
    const entries = selected().map((box) => {
      const entry = { id: box.dataset.id || box.value };
      if (box.dataset.expect) entry.expect = box.dataset.expect;
      if ("duplicate" in box.dataset) entry.accept_duplicate = true;
      return entry;
    });
    const ids = entries.map((entry) => entry.id);
    if (!ids.length) return;
    const question =
      ids.length === 1
        ? "Das ausgewählte Dokument mit seinem ersten Vorschlag ablegen?"
        : `${ids.length} Dokumente mit ihrem jeweils ersten Vorschlag ablegen?`;
    if (!(await confirmAction(question, { ok: "Ablegen" }))) return;
    submit.classList.add("is-busy");
    const info = $(".bulk-info", form);
    let filed = 0;
    const failures = [];
    try {
      for (let start = 0; start < ids.length; start += 10) {
        const chunk = ids.slice(start, start + 10);
        if (info) info.textContent = `Wird abgelegt … ${start + chunk.length} von ${ids.length}`;
        const response = await fetch(form.dataset.api, {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRF-Token": csrfToken(), ...householdHeaders() },
          body: JSON.stringify({ items: entries.slice(start, start + 10) }),
        });
        if ((response.status === 404 || response.status === 405) && !filed) {
          HTMLFormElement.prototype.submit.call(form);
          return;
        }
        if (!response.ok) {
          failures.push(await errorMessage(response, "Ablage fehlgeschlagen"));
          break;
        }
        const result = batchResult(await response.json().catch(() => null), chunk);
        filed += result.filedIds.length;
        failures.push(...result.failures);
        for (const id of result.filedIds) {
          form.querySelector(`[data-card="${CSS.escape(String(id))}"]`)?.classList.add("is-filed");
        }
      }
    } catch {
      failures.push("Keine Verbindung zum Server.");
    }
    if (filed) toastAfterReload(filed === 1 ? "1 Dokument abgelegt." : `${filed} Dokumente abgelegt.`);
    if (failures.length) {
      const more = failures.length > 3 ? ` (und ${failures.length - 3} weitere)` : "";
      toastAfterReload(`Nicht abgelegt: ${failures.slice(0, 3).join("; ")}${more}`, "error");
    }
    setTimeout(() => window.location.reload(), filed ? 500 : 0);
  });
  update();
}

// --------------------------------------------- inbox: live processing status

const PIPELINE = [
  ["received", "Umwandlung in PDF"],
  ["normalized", "Leerseiten entfernen"],
  ["deblanked", "Texterkennung (OCR)"],
  ["ocr_done", "Analyse"],
  ["analyzed", "Ablagevorschläge"],
];
const SOURCES = {
  upload: ["Upload", "upload"],
  smb: ["Scanner (SMB)", "printer"],
  ftp: ["Scanner (FTP)", "printer"],
  mail: ["E-Mail", "mail"],
  smtp: ["Scan per Mail", "mail"],
  paperless: ["Paperless-Import", "archive"],
};

function stepOf(stage) {
  let done = PIPELINE.findIndex(([name]) => name === stage);
  if (done < 0) done = stage === "ready" ? PIPELINE.length : 0;
  return { done, total: PIPELINE.length, label: PIPELINE[Math.min(done, PIPELINE.length - 1)][1] };
}

function sourceOf(name) {
  if (SOURCES[name]) return SOURCES[name];
  if (String(name || "").startsWith("watch-")) return [`Ordner „${name.slice(6)}“`, "folder"];
  return [name || "Unbekannt", "file"];
}

function itemsFrom(data) {
  const list = Array.isArray(data) ? data : data?.items || data?.results || [];
  return list.filter((item) => item && typeof item === "object" && item.id);
}

function titleOf(item) {
  const top = Array.isArray(item.suggestions) && item.suggestions[0];
  const original = Array.isArray(item.original_files) && item.original_files[0];
  return (
    item.title || item.display_name || item.context?.title || top?.title ||
    (original ? String(original).split("/").pop() : "") || item.id
  );
}

function pollArbeit(url, onUpdate) {
  let timer = null;
  let stopped = false;
  let failures = 0;
  async function poll() {
    timer = null;
    if (stopped) return;
    if (doc.hidden) return; // resumed by visibilitychange
    try {
      const response = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (response.status === 401) {
        stopped = true;
        return;
      }
      if (!response.ok) throw new Error(String(response.status));
      failures = 0;
      if (onUpdate(itemsFrom(await response.json())) === false) {
        stopped = true;
        return;
      }
    } catch {
      failures += 1;
      if (failures > 8) return;
    }
    schedule(failures ? 3000 * Math.min(failures + 1, 5) : 3000);
  }
  function schedule(delay) {
    clearTimeout(timer);
    timer = setTimeout(poll, delay);
  }
  doc.addEventListener("visibilitychange", () => {
    if (!doc.hidden && !timer && !stopped) schedule(250);
  });
  schedule(2500);
}

function processingRow(item) {
  const [label, iconName] = sourceOf(item.source);
  const step = stepOf(item.stage);
  const progress = el("progress", { class: "progress", max: step.total, value: step.done, "data-step-progress": true, "aria-label": "Fortschritt" });
  return el(
    "li",
    { class: "processing-item", "data-id": item.id },
    el("span", { class: "processing-icon", "aria-hidden": "true" }, icon(iconName)),
    el(
      "div",
      { class: "processing-body" },
      el("a", { class: "processing-title", href: `/eingang/${encodeURIComponent(item.id)}`, text: titleOf(item) }),
      el("span", { class: "processing-meta", text: label }),
    ),
    el(
      "div",
      { class: "processing-status" },
      el("span", { class: "processing-step", "data-step-label": true, text: `${step.label} …` }),
      progress,
    ),
  );
}

function userIsBusy() {
  const active = doc.activeElement;
  const typing = active && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName);
  return typing || $$("input[data-select]:checked").length > 0 || $$("details[open].popover, dialog[open]").length > 0;
}

function initProcessing() {
  const section = $("[data-processing]");
  if (!section) return;
  const list = $("[data-processing-list]", section);
  const count = $("[data-processing-count]", section);
  const banner = $("[data-new-ready]");
  let known = new Set((section.dataset.ids || "").split(",").filter(Boolean));
  let uploadedAt = 0;
  try {
    uploadedAt = Number(sessionStorage.getItem(UPLOAD_KEY) || 0);
  } catch {
    uploadedAt = 0;
  }
  const watchUntil = uploadedAt && Date.now() - uploadedAt < 30000 ? uploadedAt + 30000 : 0;
  if (!known.size && !watchUntil) return;
  let done = 0;

  pollArbeit(section.dataset.poll, (items) => {
    const current = new Map(items.map((item) => [String(item.id), item]));
    const finished = [...known].filter((id) => !current.has(id)).length;
    for (const row of $$(".processing-item", list)) {
      const item = current.get(row.dataset.id);
      if (!item) {
        row.classList.add("is-done");
        setTimeout(() => row.remove(), 450);
        continue;
      }
      const step = stepOf(item.stage);
      const labelNode = $("[data-step-label]", row);
      if (labelNode) labelNode.textContent = `${step.label} …`;
      const bar = $("[data-step-progress]", row);
      if (bar) bar.value = step.done;
    }
    for (const [id, item] of current) {
      if (!list.querySelector(`[data-id="${CSS.escape(id)}"]`)) list.append(processingRow(item));
    }
    if (count) count.textContent = String(current.size);
    section.hidden = current.size === 0 && !list.children.length;
    if (current.size === 0) setTimeout(() => (section.hidden = true), 500);
    known = new Set(current.keys());
    if (finished) {
      done += finished;
      // Reload by itself only once everything is through and nobody is in the middle of something.
      if (!current.size && !userIsBusy()) {
        toastAfterReload(done === 1 ? "Ein Dokument ist fertig." : `${done} Dokumente sind fertig.`, "info");
        window.location.reload();
        return false;
      }
      if (banner) banner.hidden = false;
    }
    if (!current.size && Date.now() > watchUntil) return false;
    return true;
  });
}

function initItemWatch() {
  const panel = $("[data-watch-item]");
  if (!panel) return;
  const id = panel.dataset.watchItem;
  pollArbeit(panel.dataset.poll, (items) => {
    const item = items.find((entry) => String(entry.id) === id);
    if (!item) {
      window.location.reload();
      return false;
    }
    const step = stepOf(item.stage);
    $$(".step", panel).forEach((node, index) => {
      node.classList.toggle("step-done", index < step.done);
      node.classList.toggle("step-current", index === step.done);
      node.classList.toggle("step-todo", index > step.done);
    });
    return true;
  });
}

// ---------------------------------------------------- folder combobox

function cleanPath(value) {
  return String(value || "")
    .split("/")
    .map((part) => part.trim())
    .filter(Boolean)
    .join("/");
}

function highlighted(text, query) {
  const words = cleanPath(query).toLowerCase().split(/[\s/]+/).filter(Boolean);
  const word = words[words.length - 1];
  const fragment = doc.createDocumentFragment();
  const index = word ? text.toLowerCase().lastIndexOf(word) : -1;
  if (index < 0) {
    fragment.append(text);
    return fragment;
  }
  fragment.append(text.slice(0, index), el("mark", { text: text.slice(index, index + word.length) }), text.slice(index + word.length));
  return fragment;
}

function setupCombobox(box, index) {
  const input = $("input", box);
  if (!input) return;
  const folders = readJson(box.dataset.source || "folders-data").map(String);
  const allowCreate = !("noCreate" in box.dataset);
  const hint = box.parentElement?.querySelector("[data-folder-hint]");
  const defaultHint = hint?.textContent || "";
  const listId = `${input.id || `combobox-${index}`}-listbox`;
  const list = el("ul", { class: "combobox-list", role: "listbox", id: listId, hidden: true });
  box.append(list);
  input.removeAttribute("list"); // the datalist is only the fallback without JavaScript
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-controls", listId);
  let options = [];
  let active = -1;

  function search(query) {
    const q = fold(cleanPath(query));
    if (!q) return folders.slice(0, 40);
    const words = q.split(/[\s/]+/).filter(Boolean);
    const scored = [];
    for (const path of folders) {
      const folded = fold(path);
      if (!words.every((word) => folded.includes(word))) continue;
      const name = fold(path.split("/").pop());
      let score = 0;
      if (folded === q) score += 100;
      if (name.startsWith(words[words.length - 1])) score += 20;
      if (folded.startsWith(q)) score += 10;
      score -= path.split("/").length;
      scored.push([score, path]);
    }
    scored.sort((a, b) => b[0] - a[0] || a[1].localeCompare(b[1], "de"));
    return scored.slice(0, 12).map(([, path]) => path);
  }

  const exists = (value) => folders.some((path) => fold(path) === fold(cleanPath(value)));

  function updateHint() {
    if (!hint || !allowCreate) return;
    const value = cleanPath(input.value);
    const isNew = Boolean(value) && !exists(value);
    hint.textContent = isNew ? `„${value}“ gibt es noch nicht – der Ordner wird beim Speichern angelegt.` : defaultHint;
    hint.classList.toggle("is-new", isNew);
  }

  function close() {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    active = -1;
  }

  function setActive(position) {
    active = position;
    $$(".combobox-option", list).forEach((node, i) => {
      node.classList.toggle("is-active", i === position);
      node.setAttribute("aria-selected", i === position ? "true" : "false");
      if (i === position) node.scrollIntoView({ block: "nearest" });
    });
    if (position >= 0) input.setAttribute("aria-activedescendant", `${listId}-${position}`);
    else input.removeAttribute("aria-activedescendant");
  }

  function choose(position) {
    const option = options[position];
    if (!option) return;
    input.value = option.value;
    close();
    updateHint();
    input.dispatchEvent(new CustomEvent("folder:chosen", { bubbles: true, detail: option.value }));
  }

  function open() {
    const query = input.value;
    const value = cleanPath(query);
    options = search(query).map((path) => ({ value: path, create: false }));
    if (allowCreate && value && !exists(value)) options.push({ value, create: true });
    if (!options.length) {
      list.replaceChildren(el("li", { class: "combobox-empty", text: folders.length ? "Kein passender Ordner" : "Noch keine Ordner vorhanden" }));
    } else {
      list.replaceChildren(
        ...options.map((option, i) => {
          const node = el("li", {
            class: `combobox-option${option.create ? " combobox-create" : ""}`,
            role: "option",
            id: `${listId}-${i}`,
            "aria-selected": "false",
          });
          node.append(icon(option.create ? "folder-plus" : "folder"));
          if (option.create) {
            node.append(
              el("span", { class: "combobox-path" }, "Neu anlegen: ", el("strong", { text: option.value })),
              el("span", { class: "badge badge-new", text: "neu" }),
            );
          } else {
            node.append(el("span", { class: "combobox-path" }, highlighted(option.value, query)));
          }
          node.addEventListener("mousedown", (event) => {
            event.preventDefault();
            choose(i);
          });
          return node;
        }),
      );
    }
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    setActive(value && options.length ? 0 : -1);
  }

  input.addEventListener("input", () => {
    open();
    updateHint();
  });
  input.addEventListener("focus", () => {
    if (!input.value) open();
  });
  input.addEventListener("blur", () => setTimeout(close, 150));
  input.addEventListener("folder:set", updateHint);
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (list.hidden) open();
      else setActive(Math.min(active + 1, options.length - 1));
    } else if (event.key === "ArrowUp") {
      if (list.hidden) return;
      event.preventDefault();
      setActive(Math.max(active - 1, 0));
    } else if (event.key === "Enter" && !list.hidden && active >= 0 && !event.ctrlKey && !event.metaKey) {
      event.preventDefault();
      choose(active);
    } else if (event.key === "Escape" && !list.hidden) {
      event.preventDefault();
      event.stopPropagation();
      close();
    }
  });
  updateHint();
}

// -------------------------------------------------------------- tag input

function splitTags(value) {
  const seen = new Set();
  const tags = [];
  for (const raw of String(value || "").split(/[,;\n]/)) {
    const tag = raw.trim().replace(/\s+/g, " ");
    const key = tag.toLowerCase();
    if (tag && !seen.has(key)) {
      seen.add(key);
      tags.push(tag);
    }
  }
  return tags;
}

function setupTagInput(input, index) {
  const known = readJson(input.dataset.source || "tags-data").map(String);
  let tags = splitTags(input.value);
  const field = el("input", {
    type: "text",
    class: "tag-input-field",
    autocomplete: "off",
    placeholder: input.placeholder || "",
    "aria-describedby": input.getAttribute("aria-describedby"),
  });
  const id = input.id || `tags-${index}`;
  input.removeAttribute("id");
  input.type = "hidden";
  field.id = id;
  const chips = el("span", { class: "tag-chips", role: "list" });
  const listId = `${id}-listbox`;
  const list = el("ul", { class: "suggest-list", role: "listbox", id: listId, hidden: true });
  const wrapper = el("div", { class: "tag-input" }, chips, field, list);
  input.after(wrapper);
  field.setAttribute("role", "combobox");
  field.setAttribute("aria-expanded", "false");
  field.setAttribute("aria-controls", listId);
  let options = [];
  let active = -1;

  function sync() {
    input.value = tags.join(", ");
    chips.replaceChildren(
      ...tags.map((tag, i) => {
        const remove = el("button", { type: "button", "aria-label": `Tag „${tag}“ entfernen` }, icon("x"));
        remove.addEventListener("click", (event) => {
          event.stopPropagation();
          tags.splice(i, 1);
          sync();
          field.focus();
        });
        return el("span", { class: "tag-chip", role: "listitem" }, tag, remove);
      }),
    );
    field.placeholder = tags.length ? "" : input.placeholder || "";
  }

  function close() {
    list.hidden = true;
    field.setAttribute("aria-expanded", "false");
    field.removeAttribute("aria-activedescendant");
    active = -1;
  }

  function add(raw) {
    for (const tag of splitTags(raw)) {
      if (!tags.some((existing) => existing.toLowerCase() === tag.toLowerCase())) tags.push(tag);
    }
    field.value = "";
    sync();
    close();
  }

  function setActive(position) {
    active = position;
    $$(".suggest-option", list).forEach((node, i) => node.classList.toggle("is-active", i === position));
    if (position >= 0) field.setAttribute("aria-activedescendant", `${listId}-${position}`);
  }

  function open() {
    const q = fold(field.value.trim());
    const chosen = new Set(tags.map((tag) => tag.toLowerCase()));
    options = known.filter((tag) => !chosen.has(tag.toLowerCase()) && (!q || fold(tag).includes(q))).slice(0, 8);
    if (!options.length) {
      close();
      return;
    }
    list.replaceChildren(
      ...options.map((tag, i) => {
        const node = el("li", { class: "suggest-option", role: "option", id: `${listId}-${i}` }, icon("tag"), tag);
        node.addEventListener("mousedown", (event) => {
          event.preventDefault();
          add(tag);
          field.focus();
        });
        return node;
      }),
    );
    list.hidden = false;
    field.setAttribute("aria-expanded", "true");
    setActive(q ? 0 : -1);
  }

  field.addEventListener("input", () => {
    if (/[,;]/.test(field.value)) add(field.value);
    else open();
  });
  field.addEventListener("focus", open);
  field.addEventListener("blur", () => {
    setTimeout(close, 150);
    if (field.value.trim()) add(field.value);
  });
  field.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.ctrlKey && !event.metaKey) {
      if (!list.hidden && active >= 0) {
        event.preventDefault();
        add(options[active]);
      } else if (field.value.trim()) {
        event.preventDefault();
        add(field.value);
      }
    } else if (event.key === "Backspace" && !field.value && tags.length) {
      tags.pop();
      sync();
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      if (list.hidden) open();
      else setActive(Math.min(active + 1, options.length - 1));
    } else if (event.key === "ArrowUp" && !list.hidden) {
      event.preventDefault();
      setActive(Math.max(active - 1, 0));
    } else if (event.key === "Escape" && !list.hidden) {
      event.preventDefault();
      event.stopPropagation();
      close();
    }
  });
  wrapper.addEventListener("click", (event) => {
    if (event.target === wrapper || event.target === chips) field.focus();
  });
  input.form?.addEventListener("submit", () => {
    if (field.value.trim()) add(field.value);
  });
  // Lets other scripts (suggestion chips) replace the tags.
  input.addEventListener("tags:set", (event) => {
    tags = splitTags((event.detail || []).join(","));
    sync();
  });
  sync();
}

// ------------------------------------------------------------ filing form

function sanitizeName(title) {
  return String(title || "")
    .replace(/[\u0000-\u001f]/g, " ")
    .replace(/[/\\|]/g, "-")
    .replace(/:/g, " -")
    .replace(/[*?"<>]/g, "")
    .replace(/\s+/g, " ")
    .replace(/^[\s.]+|[\s.]+$/g, "");
}

function initFilingForm() {
  for (const form of $$("form[data-filing-form]")) {
    const title = $("[data-title-input]", form);
    const date = $("[data-date-input]", form);
    const sender = $("[data-sender-input]", form);
    const folder = $('input[name="folder"]', form);
    const tags = $('input[name="tags"]', form);
    const preview = $("[data-filename-preview]", form);
    const ruleSender = $("[data-rule-sender]", form);
    const dirty = new Set();

    const updatePreview = () => {
      if (!preview || !title) return;
      const name = sanitizeName(title.value);
      const ext = preview.dataset.ext || ".pdf";
      preview.textContent = name ? `${date?.value ? `${date.value} ` : ""}${name}${ext}` : "–";
    };
    const updateRuleSender = () => {
      if (ruleSender && sender) ruleSender.textContent = sender.value.trim() || ruleSender.dataset.empty || "";
    };

    for (const [field, key] of [[title, "title"], [date, "date"], [sender, "sender"]]) {
      field?.addEventListener("input", () => {
        dirty.add(key);
        updatePreview();
        updateRuleSender();
      });
    }
    tags?.form?.addEventListener("input", (event) => {
      if (event.target.classList?.contains("tag-input-field")) dirty.add("tags");
    });
    date?.addEventListener("change", updatePreview);

    // Suggestions: the folder is always taken over, the rest only if not edited yet.
    const chips = $$("[data-suggestion]");
    for (const chip of chips) {
      chip.addEventListener("click", () => {
        let data = {};
        try {
          data = JSON.parse(chip.dataset.suggestion || "{}");
        } catch {
          return;
        }
        if (folder && data.folder) {
          folder.value = data.folder;
          folder.dispatchEvent(new Event("folder:set"));
        }
        if (title && data.title && !dirty.has("title")) title.value = data.title;
        if (date && data.date && !dirty.has("date")) date.value = data.date;
        if (sender && data.sender && !dirty.has("sender")) sender.value = data.sender;
        if (tags && Array.isArray(data.tags) && data.tags.length && !dirty.has("tags")) {
          tags.dispatchEvent(new CustomEvent("tags:set", { detail: data.tags }));
        }
        for (const other of chips) {
          other.classList.toggle("is-selected", other === chip);
          other.setAttribute("aria-pressed", other === chip ? "true" : "false");
        }
        updatePreview();
        updateRuleSender();
        toast(`Ordner „${data.folder}“ übernommen.`, "info", { timeout: 2200 });
      });
    }

    for (const chip of $$("[data-set-date]", form)) {
      chip.addEventListener("click", () => {
        if (!date) return;
        date.value = chip.dataset.setDate;
        dirty.add("date");
        for (const other of $$("[data-set-date]", form)) other.classList.toggle("is-selected", other === chip);
        updatePreview();
      });
    }
    updateRuleSender();
  }
}

// ------------------------------------------------------ search suggestions

function suggestionWords(data) {
  const list = Array.isArray(data) ? data : data?.terms || data?.suggestions || data?.words || [];
  return list.map((entry) => (typeof entry === "string" ? entry : entry?.term || entry?.word || "")).filter(Boolean);
}

function setupSuggest(input, index) {
  const form = input.form;
  if (!form) return;
  const listId = `${input.id || `suggest-${index}`}-listbox`;
  const list = el("ul", { class: "suggest-list", role: "listbox", id: listId, hidden: true });
  input.parentElement.append(list);
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-controls", listId);
  let options = [];
  let active = -1;
  let timer = null;
  let controller = null;

  const close = () => {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    active = -1;
  };
  const replaceLast = (word) => {
    const parts = input.value.replace(/\s+$/, "").split(/\s+/);
    parts[parts.length - 1] = word;
    return `${parts.join(" ")} `;
  };
  const choose = (position) => {
    if (!options[position]) return;
    input.value = replaceLast(options[position]).trim();
    close();
    form.requestSubmit();
  };
  const setActive = (position) => {
    active = position;
    $$(".suggest-option", list).forEach((node, i) => node.classList.toggle("is-active", i === position));
    if (position >= 0) input.setAttribute("aria-activedescendant", `${listId}-${position}`);
    else input.removeAttribute("aria-activedescendant");
  };

  async function load() {
    const value = input.value;
    const last = value.trim().split(/\s+/).pop() || "";
    if (last.length < 2 || last.includes(":") || last.startsWith('"')) {
      close();
      return;
    }
    controller?.abort();
    controller = new AbortController();
    try {
      const response = await fetch(`/api/v1/suggest?prefix=${encodeURIComponent(value)}`, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });
      if (!response.ok) return;
      const lastFolded = fold(last);
      options = suggestionWords(await response.json())
        .filter((word) => fold(word) !== lastFolded)
        .slice(0, 8);
    } catch {
      return;
    }
    if (!options.length || input.value !== value) {
      if (!options.length) close();
      return;
    }
    list.replaceChildren(
      ...options.map((word, i) => {
        const node = el("li", { class: "suggest-option", role: "option", id: `${listId}-${i}` }, icon("search"), highlighted(word, last));
        node.addEventListener("mousedown", (event) => {
          event.preventDefault();
          choose(i);
        });
        return node;
      }),
    );
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    setActive(-1);
  }

  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(load, 160);
  });
  input.addEventListener("blur", () => setTimeout(close, 150));
  input.addEventListener("keydown", (event) => {
    if (list.hidden) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive(Math.min(active + 1, options.length - 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive(Math.max(active - 1, -1));
    } else if (event.key === "Enter" && active >= 0) {
      event.preventDefault();
      choose(active);
    } else if (event.key === "Escape") {
      event.preventDefault();
      close();
    }
  });
}

// ------------------------------------------------------ archive: tiles or list

const VIEW_COOKIE = "ds_ansicht";
const VIEW_COOKIE_MAX_AGE = 365 * 24 * 3600;

// Both views are on the page; the links set the same cookie the server reads,
// so the next page load renders the chosen view right away.
function initViewToggle() {
  const toggle = $("[data-view-toggle]");
  if (!toggle) return;
  toggle.addEventListener("click", (event) => {
    const link = event.target instanceof Element ? event.target.closest("a[data-view]") : null;
    if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const view = link.dataset.view;
    const panels = $$("[data-view-panel]");
    if (!panels.some((panel) => panel.dataset.viewPanel === view)) return;
    event.preventDefault();
    const secure = window.location.protocol === "https:" ? "; Secure" : "";
    doc.cookie = `${VIEW_COOKIE}=${encodeURIComponent(view)}; Max-Age=${VIEW_COOKIE_MAX_AGE}; Path=/; SameSite=Lax${secure}`;
    for (const panel of panels) panel.hidden = panel.dataset.viewPanel !== view;
    for (const other of $$("a[data-view]", toggle)) {
      const active = other === link;
      other.classList.toggle("is-active", active);
      other.setAttribute("aria-current", active ? "true" : "false");
    }
  });
}

// A click anywhere in a row of the list opens its folder or document; the
// name stays the real link for keyboards and screen readers.
function initRowLinks() {
  doc.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    const row = target?.closest("tr[data-row-link]");
    if (!row || event.defaultPrevented || event.button !== 0) return;
    if (target.closest("a, button, input, label, select, textarea, summary")) return;
    if (String(window.getSelection?.() || "").trim()) return; // the user is selecting text
    const link = $("a[data-row-main]", row);
    if (!link) return;
    if (event.metaKey || event.ctrlKey) window.open(link.href, "_blank", "noopener");
    else window.location.assign(link.href);
  });
}

// ------------------------------------------------------------------- start

function start() {
  initFlashes();
  initForms();
  initMenus();
  initShortcuts();
  $$("[data-combobox]").forEach(setupCombobox);
  $$("input[data-tag-input]").forEach(setupTagInput);
  initFilingForm();
  initUpload();
  initBatch();
  initProcessing();
  initItemWatch();
  initViewToggle();
  initRowLinks();
  $$("input[data-suggest]").forEach(setupSuggest);
  showStoredToasts();
  registerServiceWorker();
}

// The service worker receives files shared from the phone (installed web app).
function registerServiceWorker() {
  if (!("serviceWorker" in navigator) || !window.isSecureContext) return;
  navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
}

if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", start, { once: true });
else start();
