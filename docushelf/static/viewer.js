// DocuShelf – embedded document viewer.
//
// Shows every page of a document as a sheet in one scrolling area: fit to the
// width by default, zoom in and out, page counter, keyboard shortcuts + - 0.
//
// The server renders the pages as images and puts them into the page, so the
// viewer works without JavaScript. When the vendored PDF.js build is present,
// the pages are drawn by PDF.js instead: sharper at every zoom level and with a
// text layer, so text can be selected and found with the browser's search.
// Canvases are drawn when a page comes near the visible area and released when
// many pages are drawn; the text layers stay (they are small) so the browser's
// search finds text on every page. If PDF.js is missing or cannot open the
// file (password protected, damaged), the page images simply stay.
//
// No inline code or styles: the Content-Security-Policy forbids them. Sizes
// are set through the CSS object model, which the policy allows.

const ZOOM_STEPS = [0.25, 0.33, 0.5, 0.67, 0.75, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3, 4];
const CSS_UNITS = 96 / 72; // PDF points to CSS pixels at 100 %
const A4 = { width: 595.28, height: 841.89 }; // page size assumed for page images
const MIN_COLUMN = 820; // fit to width: wide viewers show pages side by side, each at least this wide
const MAX_COLUMNS = 3;
const MAX_DRAWN = 12; // canvases kept at once
const MAX_CANVAS_PIXELS = 16 * 1024 * 1024; // larger canvases fail on some browsers
const RERENDER_DELAY = 160;

const doc = document;

function householdHeaders() {
  const id = doc.querySelector('meta[name="household"]')?.content;
  return id ? { "X-DocuShelf-Household": id } : {};
}

function isTyping(target) {
  return (
    target instanceof HTMLElement && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))
  );
}

function idle(callback) {
  if ("requestIdleCallback" in window) window.requestIdleCallback(callback, { timeout: 500 });
  else window.setTimeout(callback, 16);
}

class Viewer {
  constructor(root) {
    this.root = root;
    this.scroll = root.querySelector("[data-viewer-scroll]");
    this.list = root.querySelector("[data-viewer-pages]");
    this.counter = root.querySelector("[data-viewer-count]");
    this.zoomBar = root.querySelector("[data-viewer-zoom]");
    this.zoomLabel = root.querySelector("[data-zoom-label]");
    this.mode = "fit"; // "fit" or a zoom factor (1 = 100 %)
    this.scale = 1; // CSS pixels per PDF point of the widest page, for the label
    this.pages = [];
    this.pdf = null;
    this.pdfjs = null;
    this.drawn = new Set();
    this.timer = 0;
    this.frame = 0;
    this.current = 0;
  }

  start() {
    if (!this.scroll || !this.list) return;
    this.useImages();
    this.bind();
    this.layout();
    // Search hits link to "#page=3": open the document there.
    const match = /(?:^#|&)page=(\d+)/.exec(window.location.hash);
    this.target = match ? Number(match[1]) : 0;
    if (this.target) this.goTo(this.target);
    this.root.classList.add("is-ready");
    if (this.root.dataset.pdfjs) this.loadPdf().catch(() => this.root.classList.add("is-fallback"));
  }

  // ------------------------------------------------------------- pages

  useImages() {
    const items = Array.from(this.list.children);
    this.pages = items.map((el, index) => ({ el, number: index + 1, width: A4.width, height: A4.height, image: true }));
  }

  async loadPdf() {
    const pdfjs = await import(this.root.dataset.pdfjs);
    pdfjs.GlobalWorkerOptions.workerSrc = this.root.dataset.pdfjsWorker;
    const task = pdfjs.getDocument({
      url: this.root.dataset.src,
      httpHeaders: householdHeaders(),
      isEvalSupported: false,
      enableXfa: false,
      wasmUrl: this.root.dataset.pdfjsWasm || undefined,
    });
    // Never ask for a password: protected files keep the server's page images.
    const refused = new Promise((_resolve, reject) => {
      task.onPassword = () => {
        reject(new Error("password"));
        task.destroy();
      };
    });
    const pdf = await Promise.race([task.promise, refused]);
    const sizes = [];
    for (let number = 1; number <= pdf.numPages; number += 1) {
      const page = await pdf.getPage(number);
      const view = page.getViewport({ scale: 1 });
      sizes.push({ page, width: view.width, height: view.height });
    }
    this.pdf = pdf;
    this.pdfjs = pdfjs;
    this.switchToPdf(sizes);
  }

  switchToPdf(sizes) {
    const anchor = this.anchor();
    this.scroll.querySelector("[data-viewer-more]")?.remove();
    const existing = Array.from(this.list.children);
    this.pages = sizes.map((size, index) => {
      let el = existing[index];
      if (!el) {
        el = doc.createElement("li");
        el.className = "viewer-page";
        el.dataset.page = String(index + 1);
        this.list.append(el);
      }
      // Images still loading would only compete with PDF.js; finished ones stay until the canvas is ready.
      const img = el.querySelector("img");
      if (img && !img.complete) img.remove();
      return { el, number: index + 1, width: size.width, height: size.height, page: size.page, image: false };
    });
    for (const extra of existing.slice(sizes.length)) extra.remove();
    this.root.classList.add("is-pdf");
    this.layout(anchor);
    if (this.target && this.current === this.target - 1) this.goTo(this.target);
    this.observe();
    this.buildTextLayers();
  }

  // ------------------------------------------------------------ layout

  available() {
    const style = getComputedStyle(this.scroll);
    const padding = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
    return Math.max(120, this.scroll.clientWidth - padding);
  }

  gap() {
    return parseFloat(getComputedStyle(this.list).columnGap) || 0;
  }

  // CSS pixels per PDF point for a page.
  scaleFor(page, columnWidth) {
    if (this.mode === "fit") return columnWidth / page.width;
    return this.mode * CSS_UNITS;
  }

  layout(anchor = this.anchor()) {
    if (!this.pages.length) {
      this.updateLabel();
      return;
    }
    const width = this.available();
    const gap = this.gap();
    const widest = Math.max(...this.pages.map((page) => page.width));
    let columns = Math.floor((width + gap) / (MIN_COLUMN + gap));
    // Not limited by the page count: a single page on a very wide screen stays a readable size.
    columns = Math.max(1, Math.min(MAX_COLUMNS, columns));
    const columnWidth = (width - gap * (columns - 1)) / columns;
    this.scale = this.mode === "fit" ? columnWidth / widest : this.mode * CSS_UNITS;
    for (const page of this.pages) {
      // In fit mode pages share one scale, so a landscape page is not blown up next to portrait ones.
      const scale = this.mode === "fit" ? this.scale : this.scaleFor(page, columnWidth);
      page.scale = scale;
      const style = page.el.style;
      style.width = `${Math.floor(page.width * scale)}px`;
      if (page.image) style.removeProperty("height");
      else style.height = `${Math.floor(page.height * scale)}px`;
      style.setProperty("--scale-factor", String(scale));
      style.setProperty("--total-scale-factor", String(scale));
      style.setProperty("--user-unit", "1");
    }
    this.root.classList.toggle("is-zoomed", this.mode !== "fit");
    this.updateLabel();
    this.restore(anchor);
    this.updateCounter();
    if (this.pdf) this.scheduleRender();
  }

  updateLabel() {
    if (!this.zoomLabel) return;
    this.zoomLabel.textContent = this.mode === "fit" ? "Breite" : `${Math.round(this.mode * 100)} %`;
  }

  // Page at the top of the view and how far into it, to keep the place when the size changes.
  anchor() {
    const page = this.pages[this.currentIndex()];
    if (!page) return null;
    const top = this.offsetOf(page.el);
    const height = page.el.offsetHeight || 1;
    return { page, fraction: (this.scroll.scrollTop - top) / height };
  }

  restore(anchor) {
    if (!anchor || !anchor.page) return;
    const top = this.offsetOf(anchor.page.el);
    this.scroll.scrollTop = top + anchor.fraction * anchor.page.el.offsetHeight;
  }

  offsetOf(el) {
    return el.getBoundingClientRect().top - this.scroll.getBoundingClientRect().top + this.scroll.scrollTop;
  }

  currentIndex() {
    const line = this.scroll.getBoundingClientRect().top + Math.min(80, this.scroll.clientHeight / 4);
    let index = 0;
    for (let i = 0; i < this.pages.length; i += 1) {
      const rect = this.pages[i].el.getBoundingClientRect();
      if (rect.top <= line) index = i;
      else break;
      if (rect.bottom > line) break;
    }
    return index;
  }

  updateCounter() {
    if (!this.counter || !this.pages.length) return;
    const index = this.currentIndex();
    const total = this.pdf ? this.pdf.numPages : Number(this.root.dataset.pageCount) || this.pages.length;
    this.counter.textContent = `${index + 1} / ${total}`;
    this.counter.title = `Seite ${index + 1} von ${total}`;
    this.current = index;
  }

  goTo(number) {
    const page = this.pages[Math.min(Math.max(number, 1), this.pages.length) - 1];
    if (!page) return;
    this.scroll.scrollTop = this.offsetOf(page.el);
    this.updateCounter();
  }

  // -------------------------------------------------------------- zoom

  zoom(direction) {
    const now = this.mode === "fit" ? this.scale / CSS_UNITS : this.mode;
    if (direction === "fit") this.mode = "fit";
    else if (direction === "in") this.mode = ZOOM_STEPS.find((step) => step > now + 0.001) ?? ZOOM_STEPS.at(-1);
    else this.mode = [...ZOOM_STEPS].reverse().find((step) => step < now - 0.001) ?? ZOOM_STEPS[0];
    this.layout();
  }

  bind() {
    if (this.zoomBar && this.pages.length) this.zoomBar.hidden = false;
    this.zoomBar?.addEventListener("click", (event) => {
      const button = event.target instanceof Element ? event.target.closest("[data-zoom]") : null;
      if (button) this.zoom(button.dataset.zoom);
    });
    this.scroll.addEventListener(
      "scroll",
      () => {
        if (this.frame) return;
        this.frame = window.requestAnimationFrame(() => {
          this.frame = 0;
          this.updateCounter();
        });
      },
      { passive: true },
    );
    doc.addEventListener("keydown", (event) => {
      if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey || isTyping(event.target)) return;
      if (event.target instanceof Element && event.target.closest("dialog[open], details[open] .menu")) return;
      const action = { "+": "in", "=": "in", "-": "out", "0": "fit" }[event.key];
      if (!action) return;
      event.preventDefault();
      this.zoom(action);
    });
    if ("ResizeObserver" in window) {
      let width = this.scroll.clientWidth;
      new ResizeObserver(() => {
        if (this.scroll.clientWidth === width) return;
        width = this.scroll.clientWidth;
        if (this.mode === "fit") this.layout();
      }).observe(this.scroll);
    }
  }

  // ---------------------------------------------------------- drawing

  observe() {
    this.visible = new Set();
    this.observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const page = this.pages.find((candidate) => candidate.el === entry.target);
          if (!page) continue;
          if (entry.isIntersecting) this.visible.add(page);
          else this.visible.delete(page);
        }
        this.scheduleRender(0);
      },
      { root: this.scroll, rootMargin: "75% 0px" },
    );
    for (const page of this.pages) this.observer.observe(page.el);
  }

  scheduleRender(delay = RERENDER_DELAY) {
    window.clearTimeout(this.timer);
    this.timer = window.setTimeout(() => this.renderVisible(), delay);
  }

  renderVisible() {
    if (!this.visible) return;
    const order = [...this.visible].sort(
      (a, b) => Math.abs(a.number - 1 - this.current) - Math.abs(b.number - 1 - this.current),
    );
    for (const page of order) {
      const busy = page.task && page.pendingScale === page.scale;
      if (page.drawnScale !== page.scale && !busy) this.draw(page);
    }
    this.release();
  }

  async draw(page) {
    const scale = page.scale;
    page.task?.cancel();
    const viewport = page.page.getViewport({ scale });
    let ratio = window.devicePixelRatio || 1;
    const pixels = viewport.width * viewport.height * ratio * ratio;
    if (pixels > MAX_CANVAS_PIXELS) ratio *= Math.sqrt(MAX_CANVAS_PIXELS / pixels);
    const canvas = doc.createElement("canvas");
    canvas.className = "viewer-canvas";
    canvas.width = Math.max(1, Math.floor(viewport.width * ratio));
    canvas.height = Math.max(1, Math.floor(viewport.height * ratio));
    canvas.setAttribute("aria-hidden", "true");
    const context = canvas.getContext("2d", { alpha: false });
    const task = page.page.render({
      canvasContext: context,
      canvas,
      viewport,
      transform: ratio === 1 ? undefined : [ratio, 0, 0, ratio, 0, 0],
    });
    page.task = task;
    page.pendingScale = scale;
    try {
      await task.promise;
    } catch {
      return; // cancelled by a newer drawing, or a damaged page: the old canvas or image stays
    } finally {
      if (page.task === task) page.task = null;
    }
    if (page.scale !== scale) return;
    page.el.querySelector(".viewer-canvas")?.remove();
    page.el.querySelector("img")?.remove();
    page.el.prepend(canvas);
    page.drawnScale = scale;
    page.el.classList.add("is-drawn");
    this.drawn.add(page);
  }

  // Keeps memory in check: canvases far from the view are dropped and drawn again when needed.
  release() {
    if (this.drawn.size <= MAX_DRAWN) return;
    const far = [...this.drawn]
      .filter((page) => !this.visible.has(page))
      .sort((a, b) => Math.abs(b.number - 1 - this.current) - Math.abs(a.number - 1 - this.current));
    for (const page of far.slice(0, this.drawn.size - MAX_DRAWN)) {
      const canvas = page.el.querySelector(".viewer-canvas");
      if (canvas) {
        canvas.width = 0;
        canvas.height = 0;
        canvas.remove();
      }
      page.drawnScale = null;
      page.el.classList.remove("is-drawn");
      this.drawn.delete(page);
    }
  }

  // Text layers for every page, one after the other while the browser is idle.
  // They are positioned with CSS variables, so they follow every zoom level.
  buildTextLayers() {
    const TextLayer = this.pdfjs?.TextLayer;
    if (typeof TextLayer !== "function") return;
    const queue = [...this.pages];
    const next = () => {
      const page = queue.shift();
      if (!page) return;
      const container = doc.createElement("div");
      container.className = "textLayer";
      const layer = new TextLayer({
        textContentSource: page.page.streamTextContent(),
        container,
        viewport: page.page.getViewport({ scale: page.scale || 1 }),
      });
      layer
        .render()
        .then(() => page.el.append(container))
        .catch(() => {})
        .finally(() => idle(next));
    };
    idle(next);
  }
}

function start() {
  for (const root of doc.querySelectorAll("[data-viewer]")) {
    if (root.dataset.viewerStarted) continue;
    root.dataset.viewerStarted = "1";
    new Viewer(root).start();
  }
}

if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", start, { once: true });
else start();
