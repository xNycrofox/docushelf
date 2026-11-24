# "DocuShelf senden" – sending documents from iPhone, iPad and Mac

`build_shortcut.py` builds a shortcut for Apple's Shortcuts app that adds **DocuShelf senden** to
the share sheet. Share a PDF, a photo or any other file with it and it lands in the DocuShelf
inbox (*Eingang*), just like an upload in the browser.

What the shortcut does:

- It appears in the share sheet for PDFs, images and files (Photos, Files, Mail attachments,
  PDFs in Safari, scans from Notes or a scanner app, Finder and Preview on the Mac).
- When you add it, it asks for two things: the address of your DocuShelf and an API token.
- It uploads every shared item on its own to `<address>/api/v1/upload` (multipart field
  `files`, header `Authorization: Bearer <token>`), so every file becomes its own document.
- At the end it shows the notification **"An DocuShelf gesendet"**. If DocuShelf refuses a
  file – wrong or revoked token, unsupported file type, file too large – the notification
  **"Fehler beim Senden an DocuShelf"** shows DocuShelf's message and the shortcut stops.
- Started directly (from the Shortcuts app or a Home Screen icon), it asks you to pick files.

The texts inside the shortcut are German, like the rest of DocuShelf.

## Who needs what

**Users never need a Mac.** Once the operator has made the shortcut available (below), everybody
installs it from DocuShelf itself:

1. Open DocuShelf in Safari on the iPhone or iPad, go to **Einstellungen → Handy & Tablet**.
2. Tap **Kurzbefehl installieren** (`/kurzbefehl`). The Shortcuts app asks whether to add it.
3. Tap **Token für dieses Handy erstellen**, then **Kopieren** – the token is shown only once.
   The server address is shown right above it, with its own copy button.
4. Answer the two questions of the Shortcuts app with the address and the token. Done.

**The operator signs once – or not at all.** iOS only imports signed shortcuts, and the signature
needs a Mac. One Mac, once, is enough for the whole installation; any Mac will do (a friend's, a
colleague's, a borrowed one). Choose one of:

- **File on the server:** build and sign the file (steps 1 and 2 below) and copy it to
  `<data>/config/shortcuts/DocuShelf senden.shortcut` on the server. `GET /kurzbefehl` serves it
  to logged-in users as a download.
- **iCloud link:** whoever has the shortcut in their Shortcuts app (signed file imported, or
  built by hand on an iPhone as described in
  [Building the shortcut by hand](#building-the-shortcut-by-hand) – no Mac at all) opens its
  menu → **Share** → **Copy iCloud Link** and puts the link into the `.env`:
  `DOCUSHELF_IOS_SHORTCUT_URL=https://www.icloud.com/shortcuts/…`. `GET /kurzbefehl` then
  redirects there. Best share the generated shortcut, which asks for address and token when it
  is added. A hand-built shortcut contains your token: never share it as it is – empty the token
  in its request header first, and tell people to enter their own after adding it.

If a file lies on the server, it wins over the link. Without either, the button is hidden and
the settings page points to this guide.

Requirements:

- The device must reach DocuShelf: if DocuShelf is only available in your VPN (Tailscale,
  WireGuard …), the VPN has to be on while sharing. The API checks the token itself, so the
  upload address must not sit behind a login page of a proxy.
- Signing needs a Mac with macOS 12 or later that is signed in with an Apple Account – once, for
  the operator (see above). No Mac anywhere? Use the iCloud link of a hand-built shortcut, or let
  everybody build it by hand, see [Building the shortcut by hand](#building-the-shortcut-by-hand).

## 1. Build the file

The script only needs the Python standard library; the `python3` that comes with macOS is
enough. From the repository root:

```sh
python3 contrib/ios/build_shortcut.py --output "DocuShelf senden.shortcut"
```

Options:

| Option | Meaning |
|---|---|
| `--output`, `-o` | Target file, or an existing folder (the file is then called `<name>.shortcut`). Default: `<name>.shortcut` in the current folder. |
| `--name` | Name of the shortcut as it appears in the share sheet. Default: `DocuShelf senden`. |

The generated file is an *unsigned* binary property list in the Shortcuts format (WFWorkflow).
It contains neither your address nor a token – both are asked for when the shortcut is added –
and the build is reproducible: the same name always gives the same bytes. To look inside:

```sh
plutil -lint "DocuShelf senden.shortcut"
plutil -p "DocuShelf senden.shortcut"
```

## 2. Sign it on a Mac

iOS 15 and macOS 12 or later only import signed shortcuts. Sign the file with the `shortcuts`
command line tool that comes with macOS:

```sh
mkdir -p signed
shortcuts sign --mode anyone --input "DocuShelf senden.shortcut" --output "signed/DocuShelf senden.shortcut"
```

- `--mode anyone` lets every device import the file. Apple signs it through iCloud, so the Mac
  needs an internet connection and has to be signed in with an Apple Account.
  (`--mode people-who-know-me` only works for people who have you in their contacts.)
- Keep the file name: the Shortcuts app may name the imported shortcut after the file.
- If `shortcuts sign` reports "The file doesn't exist" although it does, copy the file to a
  short path without spaces (for example `/tmp/docushelf.shortcut`), sign that and rename the
  result back to `DocuShelf senden.shortcut`. Warnings like "Unrecognized attribute string flag"
  can be ignored.

**Never commit a signed shortcut.** The signature contains a certificate that identifies the
Apple Account that signed it. The repository only contains the generator; build and sign the
file on a Mac and put it on your own server (`<data>/config/shortcuts/`, see
[Who needs what](#who-needs-what)) or pass it on directly (AirDrop, Messages, mail). The file in
`config/shortcuts/` is only handed out to logged-in users.

## 3. Create an API token in DocuShelf

The quickest way is the button **Token für dieses Handy erstellen** under
**Einstellungen → Handy & Tablet**: one tap, the token is named `iPhone-Kurzbefehl` and shown
once with a copy button. Alternatively:

1. Open DocuShelf in the browser, go to **Einstellungen → API-Tokens**.
2. Enter a name for the device, for example `iPhone Anna`, and press **Token erstellen**.
3. Press **Kopieren**. The token starts with `dsk_` and is shown only once.

Easiest is to do this in Safari on the iPhone itself: then the token is already in the clipboard
when you import the shortcut. Create one token per device so you can revoke it on its own. A
token has the same rights as your account (except managing tokens and deleting from the trash
for good) and is valid only as long as your account is allowed to use DocuShelf.

## 4. Import the shortcut

1. Tap **Kurzbefehl installieren** under **Einstellungen → Handy & Tablet** (if the operator has
   set it up), or send `signed/DocuShelf senden.shortcut` to the iPhone or iPad (AirDrop, iCloud
   Drive, mail) and tap it. On a Mac, double-click it.
2. The Shortcuts app opens. Tap **Add Shortcut** (*Kurzbefehl hinzufügen*) or **Set Up
   Shortcut**.
3. Answer the two questions:
   - **Address** – your DocuShelf as you open it in the browser, for example
     `https://docs.example.net`. A trailing slash or a copied page address
     (`https://docs.example.net/eingang`) is fine, and `https://` is added if you leave it out.
     If your DocuShelf only speaks plain HTTP, type `http://` explicitly.
   - **API token** – paste the token from step 3.

To change the address or the token later, open the shortcut in the Shortcuts app: they are the
two **Text** actions at the top.

## 5. Use it

- Tap the share button in Photos, Files, Mail, Safari (with a PDF open), Notes and so on and
  choose **DocuShelf senden**. If it does not show up, scroll to the end of the share sheet and
  add it to the favourites with **Edit Actions** (*Aktionen bearbeiten*).
- On the first run iOS asks whether the shortcut may send files to your server. Allow it
  (choose **Always Allow** if it is offered), otherwise it asks every time.
- Each shared item becomes its own document in the inbox. For a letter with several pages, scan
  it into one PDF first (Files app → **…** → **Scan Documents**, or a scan in Notes), or use the
  upload in the browser with *Bilder zu einem Dokument zusammenfügen*.
- If a file is refused, the files shared before it are already in the inbox; the notification
  shows DocuShelf's message. If DocuShelf cannot be reached at all (VPN off, wrong address), the
  Shortcuts app shows the connection error itself.
- On the Mac the shortcut is in the Share menu of Finder and Preview.

Security notes:

- The token is stored inside the shortcut on your device (and in iCloud if your shortcuts are
  synced). Do not share your set-up shortcut; give others the signed file and let them create
  their own token.
- Lost the device, or someone leaves the household: revoke the token under
  **Einstellungen → API-Tokens → Widerrufen**.

## Building the shortcut by hand

Without a Mac you can build the same shortcut in the Shortcuts app on the iPhone or iPad. This
version has the address and the token typed in directly instead of asking for them. Action names
are given in English with the German name in parentheses; the German names can differ slightly
between iOS versions – type the beginning into the action search.

1. Create a token as in [step 3](#3-create-an-api-token-in-docushelf) and copy it.
2. Open **Shortcuts** (*Kurzbefehle*), tap **+**, and name the new shortcut `DocuShelf senden`.
3. Open the shortcut's details (the **ⓘ** button, or the menu at the shortcut's name) and turn
   on **Show in Share Sheet** (*Im Share-Sheet anzeigen*). The editor now starts with "Receive …
   input from Share Sheet": tap the input types and keep only **Images**, **PDFs** and
   **Files**. Set "If there's no input" to **Ask For** → **Files**.
4. Add **Repeat with Each** (*Wiederholen mit jedem*) and set its input to **Shortcut Input**
   (*Kurzbefehleingabe*).
5. Add **Get Contents of URL** (*Inhalte von URL abrufen*) and drag it *inside* the repeat block:
   - URL: your address followed by `/api/v1/upload`, for example
     `https://docs.example.net/api/v1/upload`.
   - Tap **Show More** and set **Method** to `POST`.
   - **Headers** → **Add new header**: key `Authorization`, value `Bearer ` followed by your
     token (one space after `Bearer`).
   - **Request Body** → **Form** → **Add new field** → **File**: key `files`, value: tap and
     choose the variable **Repeat Item** (*Wiederholungsobjekt*).
6. Still inside the repeat block, below the request, add **If** (*Wenn*): input **Contents of
   URL**, condition **contains**, text `batch_id`. Leave the first branch empty. If **contains**
   is not offered, tap **Contents of URL** and set its type to **Text**.
7. In the **Otherwise** branch add, in this order:
   - **Get Dictionary from Input** with input **Contents of URL**.
   - **Get Dictionary Value** (*Wörterbuchwert abrufen*): value for key `detail` in
     **Dictionary**.
   - **Show Notification** (*Mitteilung anzeigen*): text: the variable **Dictionary Value**;
     under **Show More**, title `Fehler beim Senden an DocuShelf`.
   - **Stop This Shortcut** (search for "stop").
8. Below the end of the repeat block, add **Show Notification**: text
   `Gleich findest du alles im Eingang.`, title `An DocuShelf gesendet`.
9. Tap **Done** and try it: share a PDF from the Files app with **DocuShelf senden**.

Steps 6 and 7 are optional. Without them the shortcut still sends everything, but it also says
"An DocuShelf gesendet" when DocuShelf refused a file.
