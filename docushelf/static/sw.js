// DocuShelf service worker.
//
// It does exactly one thing. Files shared to the installed app arrive as
// POST /teilen (see "share_target" in /manifest.webmanifest). The worker keeps
// them in the Cache Storage and sends the browser on to GET /teilen, where
// share.js lists them and uploads them with the session's CSRF token.
//
// Pages, API answers and static files are neither cached nor touched: every
// other request goes to the network as if there were no worker.

"use strict";

const SHARE_CACHE = "docushelf-teilen";
const SHARE_PATH = "/teilen";
// Cache keys of the kept files; these URLs only name entries and are never requested.
const FILE_PATH = "/teilen/datei/";
const NAME_HEADER = "X-DocuShelf-Name";
const FILE_FIELD = "files";

self.addEventListener("install", () => {
  // A new version takes over right away; it keeps no state besides the shared files.
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "POST") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || url.pathname !== SHARE_PATH) return;
  event.respondWith(keepSharedFiles(request));
});

function siteUrl(path) {
  return new URL(path, self.location.href).href;
}

// 303: the browser follows with a GET, so reloading the page never posts the files again.
function redirectTo(notice) {
  return Response.redirect(siteUrl(notice ? `${SHARE_PATH}?hinweis=${notice}` : SHARE_PATH), 303);
}

async function keepSharedFiles(request) {
  let form;
  try {
    form = await request.formData();
  } catch {
    return redirectTo("fehler");
  }
  // Shared texts and links ("title", "text") cannot be filed; only files count.
  const files = form.getAll(FILE_FIELD).filter((value) => typeof value !== "string" && value.size > 0);
  if (!files.length) return redirectTo("leer");
  try {
    const cache = await caches.open(SHARE_CACHE);
    const batch = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    let position = 0;
    for (const file of files) {
      position += 1;
      const headers = new Headers({
        "Content-Type": file.type || "application/octet-stream",
        [NAME_HEADER]: encodeURIComponent(file.name || `datei-${position}`),
      });
      const key = new Request(siteUrl(`${FILE_PATH}${batch}-${String(position).padStart(3, "0")}`));
      await cache.put(key, new Response(file, { headers }));
    }
  } catch {
    return redirectTo("fehler");
  }
  return redirectTo("");
}
