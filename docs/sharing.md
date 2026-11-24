# Sending documents from phones

DocuShelf accepts documents from phones in two ways:

| Platform | How | Authentication |
|---|---|---|
| Android (Chrome, Edge, Samsung Internet), desktop Chrome/Edge | DocuShelf installed as a web app appears in the system share sheet | browser session (login as usual) |
| iPhone / iPad | Shortcut "DocuShelf senden" in the share sheet (see `contrib/ios/README.md`) | API token |

Both end in the same place: an upload batch in `eingang/quellen/upload/`, picked up by
the intake like any other upload. Nothing is filed automatically by this step; the
documents show up in the inbox (or are filed by the automatic filing, if enabled).

## Requirements

- **HTTPS.** Installing a web app and service workers need a secure context. Over plain
  `http://` (other than `localhost`) the browser offers neither the installation nor the
  share target. Serve DocuShelf through the reverse proxy with a valid certificate, as
  described in `DEPLOY.md`.
- The phone must reach DocuShelf (e.g. through the VPN) when sharing.

## Android: installable web app with share target

### For users

1. Open DocuShelf in Chrome on the phone and log in.
2. Browser menu (⋮) → "App installieren" / "Zum Startbildschirm hinzufügen".
   The settings page ("Handy & Tablet") and the share page also show an
   "App installieren" button when the browser offers its install dialog.
3. In the gallery, the file manager or the mail app: select PDFs or photos →
   "Teilen" → "DocuShelf".
4. DocuShelf opens the page "Dateien teilen" with the shared files. Several photos of one
   letter can be combined into one document ("Bilder zu einem Dokument zusammenfügen",
   PDFs always stay documents of their own); "Hochladen" sends them to the inbox.

If the session has expired, DocuShelf asks for the login first; the shared files wait in
the browser until the upload is done.

### How it works

```
share sheet ──POST /teilen (multipart)──▶ service worker (sw.js)
                                           │ keeps the files in Cache Storage "docushelf-teilen"
                                           ▼
                                  303 → GET /teilen  (login required)
                                           │ share.js lists the files
                                           ▼
                     POST /api/v1/upload (session cookie + X-CSRF-Token)
                                           │ success: cache entries deleted
                                           ▼
                                  eingang/quellen/upload/<batch>/
```

- `GET /manifest.webmanifest` (`application/manifest+json`, no login – browsers fetch the
  manifest without cookies): name, `start_url` `/eingang`, `scope` `/`, `display`
  `standalone`, colours of the light palette, icons and the `share_target`
  (`action` `/teilen`, `POST`, `multipart/form-data`, files in the field `files`;
  `title` and `text` are declared too, but only files are kept).
- `GET /sw.js` (`text/javascript`, `Cache-Control: no-cache`, no login) is served from the
  root so that its scope is the whole site. The worker caches **nothing** of the
  application: it only answers `POST /teilen`. Every other request goes to the network
  untouched. Each shared file becomes one Cache Storage entry
  (`/teilen/datei/<batch>-<n>`) with its type in `Content-Type` and its URI-encoded name in
  `X-DocuShelf-Name`. Shares without files redirect to `/teilen?hinweis=leer`, errors
  while keeping them to `/teilen?hinweis=fehler`.
- `static/share.js` is loaded by the share page and by the help on the settings page. It
  registers the service worker, offers the browser's install dialog
  (`beforeinstallprompt`) and, on `/teilen`, lists and uploads the kept files. Uploads use
  the same rule as the upload area: with "combine", all images go into one request with
  `combine=true`, every other file is sent without it.
- The Content-Security-Policy allows `manifest-src 'self'` and `worker-src 'self'`;
  everything else stays as strict as before (no inline scripts or styles).

### Without service worker

The very first share right after installing can reach the server before the service
worker is active (it is registered by `share.js` on the share page and the settings page).
`POST /teilen` is then handled on the server as a normal upload (one document per file)
when both hold:

- the request carries a valid browser session, and
- the browser marks it with `Sec-Fetch-Site: none` (opened by the user or the system, as
  share sheets do) or `same-origin`.

A share sheet cannot send a CSRF token; `Sec-Fetch-Site` takes its place because other
web sites cannot set it. Everything else is refused without storing anything:

- trusted request, but not logged in → login page with the message
  "Bitte melde dich an. Danach kannst du die Dateien noch einmal mit DocuShelf teilen."
  (the files are not kept – share again after logging in);
- any other `Sec-Fetch-Site` (or none at all) → `/teilen?hinweis=abgelehnt`. The session is
  not touched in that response: a cross-site request may arrive without the session cookie,
  and a new session cookie would replace the visitor's real one. The following `GET` shows
  the message, or leads to the login with the message.

API tokens are not accepted on `/teilen`; scripts and the iPhone Shortcut use
`/api/v1/upload`.

### Icons

`docushelf/static/icons/` holds PNG renderings of `static/favicon.svg`:

| File | Size | Use |
|---|---|---|
| `icon-192.png`, `icon-512.png` | 192, 512 | manifest, purpose `any` (rounded square, transparent corners) |
| `icon-maskable-192.png`, `icon-maskable-512.png` | 192, 512 | manifest, purpose `maskable`: full-bleed background, logo scaled to 80 % so it stays inside the safe zone (circle with 40 % radius) |
| `apple-touch-icon.png` | 180 | home screen icon on iOS (full-bleed, iOS rounds the corners) |

They were rendered with an SVG rasterizer (CoreSVG via `sips` on macOS; `rsvg-convert`
works the same way). For the full-bleed variants, the logo's background square is replaced
by an unrounded one and the remaining shapes are wrapped in
`<g transform="translate(16 16) scale(0.8) translate(-16 -16)">` (0.9 for the Apple icon).
Regenerate them when the logo changes; `tests/test_share.py` checks sizes, transparency
and the safe zone.

## iPhone and iPad: Shortcut

iOS does not offer web apps in the share sheet. The Shortcut "DocuShelf senden" fills the
gap; building, signing and importing it is described in `contrib/ios/README.md`.

1. Create an API token in DocuShelf: *Einstellungen → API-Tokens* (e.g. "iPhone Kurzbefehl").
2. Import the Shortcut; it asks for the server address (shown on the settings page under
   "Handy & Tablet") and the token.
3. Share PDFs, photos or files to "DocuShelf senden".

The Shortcut posts to `/api/v1/upload` with `Authorization: Bearer <token>`. The upload API
accepts what it sends: several files in one request in the multipart field `files` (`file`
works too), PDF, JPEG, PNG, TIFF, HEIC/HEIF and WebP, recognised by content rather than by
name; repeated names such as `image.jpeg` are stored as `image.jpeg`, `image (2).jpeg`, ….
Token requests need no CSRF token.

A token has the rights of its owner (except managing tokens and deleting from the trash for
good). Revoke it when the phone is lost.

## Troubleshooting

| Symptom | Cause |
|---|---|
| No "App installieren" in the browser menu | DocuShelf is not served over HTTPS, or the manifest cannot be loaded (check `GET /manifest.webmanifest`) |
| DocuShelf missing in the share sheet | the app is not installed (a bookmark on the home screen is not enough), or the shared type is not a PDF or image |
| "Die geteilten Dateien wurden aus Sicherheitsgründen nicht übernommen" | the share reached the server without the service worker and without a trusted `Sec-Fetch-Site`; open DocuShelf once (registers the worker) and share again |
| Files stay on the share page after "Hochladen" | the upload failed (message above the list); they stay in the browser until uploaded or discarded |
| After an update the old behaviour persists | the service worker updates on the next page load (`Cache-Control: no-cache`); closing and reopening the app is enough |
