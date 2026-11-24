# DocuShelf internals – module contracts

This document is the implementation contract between modules. `ARCHITECTURE.md`
explains the *why*; this file defines the *what* (names, signatures, behaviour).
Read `docs/concept.md` (idea and principles) first.

## Conventions (all modules)

- Python 3.12, `from __future__ import annotations`, type hints, dataclasses.
- Code comments and docstrings in **English**, natural and generic. No references
  to a specific server, homelab, person or to "the request/prompt". No AI tone.
- Everything a user sees (UI text, error messages passed to the UI, log lines in
  item logs) is **German**.
- Logging: `log = logging.getLogger(__name__)`.
- Components take the service container in the constructor: `Component(services)`.
  Background components have idempotent `start()` / `stop()`, use daemon threads,
  a `threading.Event` for stopping and join with a timeout.
- One `Services` container exists per household (see "Households"). Components only
  use their own container and never look into another household's data. Process-wide
  state is limited to the OCR semaphore, the global SMTP receiver and the server lock.
- Never write into the archive except through `ArchiveOps` and the helpers in
  `docushelf.util.fsops` (`publish_no_clobber`, `place_exact`, `replace_if_unchanged`,
  `move_file`). Never overwrite an existing file. Never create helper files,
  sidecars or hidden folders in the archive (transient `.docushelf-*.part`
  files during a copy are the only exception and are cleaned up).
- Every app-initiated mutation of the archive and every reconciliation pass holds
  `services.archive_lock` (an `RLock`).
- The SQLite index is a cache. Anything that cannot be rebuilt from files must
  not live only in the index.
- Tests: pytest under `tests/`, real files in `tmp_path`, fixtures from
  `tests/conftest.py` (`settings`, `env`, `layout`, `base_services`, `services`,
  `samples`, marker `requires_ocr`). Name test files after your module
  (`tests/test_reconcile.py` …). Do not edit tests owned by other modules.

## Foundation (implemented, do not break)

| Module | Contents |
|---|---|
| `config.py` | `Settings`, `SourceConfig`, `EndpointConfig`, `ImapConfig`, `SmtpConfig`, `LlmConfig`, `AutoFileConfig`, `HOUSEHOLDS_FILE`, `load_settings(env=None, **overrides)` – see "Configuration" below |
| `households.py` | `Household`, `Registry`, `load_households`, `settings_for`, `default_household` – see "Households" below |
| `layout.py` | `Layout` (paths of the data volume, `archive_path(rel)`, `rel_archive(path)`, `state_dir(state)`, `source_dir(name)`, `data_dirs()` – the directories a household owns) |
| `errors.py` | `DocuShelfError`, `ConfigError`, `PathError`, `NotFound`, `Conflict`, `ProcessingError(message, stage=, transient=)`, `UnsupportedFile`, `AuthError` |
| `models.py` | `DocMeta`, `DocumentRecord`, `Suggestion`, `DateCandidate`, `Analysis`, `FilingRequest`, `MetadataUpdate`, `ScanStats` |
| `util/names.py` | `sanitize_component`, `sanitize_folder`, `check_relative`, `format_filename`, `parse_filename`, `unique_filename`, `is_ignored`, `title_from_original_name`, `casefold_key`, `OWN_TEMP_PREFIX` |
| `util/fsops.py` | `atomic_write_json/text/bytes`, `read_json`, `sha256_file`, `sha256_files`, `file_signature`, `copy_file_fsync`, `publish_no_clobber`, `place_exact`, `move_file`, `move_dir`, `replace_if_unchanged`, `fsync_dir`, `remove_tree`, `dir_is_empty`, `temp_name_for` |
| `util/textnorm.py` | `normalize`, `normalize_with_map`, `tokenize`, `content_words`, `STOPWORDS`, `bounded_levenshtein`, `joined`, `join_map`, `joined_with_map`, `joined_fragments`, `spellings` |
| `util/ids.py` | `new_item_id`, `is_item_id`, `new_doc_id`, `provisional_id`, `is_provisional`, `is_doc_id`, `new_token` |
| `util/timeutil.py` | `now`, `now_iso`, `today`, `parse_date`, `format_date`, `format_date_de`, `parse_iso_datetime`, `set_timezone` |
| `pdf/__init__.py` | `PDFIUM_LOCK` – hold it for every pypdfium2 call |
| `pdf/xmp.py` | `read_meta(path) -> DocMeta`, `write_meta(src, dst, meta)`, `apply_meta(pdf, meta)`, `build_packet`, `merge_meta(meta, **changes)`, `pdf_protection(path) -> "signed" \| "encrypted" \| None`, `is_signed(pdf)`, `xml_safe(text)`. `write_meta` raises `UnsupportedFile` for signed and encrypted PDFs. Extra properties include `PaperlessNotes`, `PaperlessFields`. |
| `pdf/text.py` | `extract_pages(path) -> list[str]`, `page_count(path)`, `has_text(pages)`, `clean_text` |
| `index/db.py` | `Index` (see docstrings: folders, documents, pages/FTS, vocabulary, jobs, `mail_seen`, `hash_seen`, `stats`, `generation`) and `Job`. One SQLite connection per live thread; `close()` closes all. `SCHEMA_VERSION` is `"2"` (adds the `joined` columns and the `fts_joined` / `fts_docs_joined` tables). On a version mismatch `init_schema()` drops and rebuilds only the tables derived from the archive, keeps `jobs`, `mail_seen`, `hash_seen`, and returns True (full scan needed); `reset()` drops everything. |
| `inbox/items.py` | `InboxItem`, `InboxStore` (`create`, `save`, `load`, `find`, `list`, `iter_state`, `move`, `log`, `read_log`, `read_text`, `write_text`, `recover_orphans`, `counts`), `STAGES`, `STATES`. `create` writes originals as `original/.docushelf-*.part` and renames them when complete; with `move=False` a marker `.docushelf-creating` exists until `item.json` is written, and a failed `create` removes the partial item. `recover_orphans` discards directories whose creator is gone, deletes stale `.part` files and adopts moved-in originals (source `unbekannt`). |
| `channels/sendung.py` | `write_sendung(source_dir, documents, context=, options=, titles=)`, `read_sendung(dir)`, `MANIFEST` |
| `audit.py` | `AuditLog.write(user, action, **details)`, `AuditLog.tail(limit)`, `AuditLog.entries(actions=None, since=None, *, limit=None)` (newest first, reads only the monthly files in range) |
| `notify.py` | `Notifier.send(title, message, *, tags="warning", click=None)` – fire and forget; `click` is a link opened by the notification |
| `services.py` | `Services` – attribute names below are binding |

### Service container attributes

`settings, household (Household), layout, channels (ChannelManager), index, audit, inbox, notifier,
archive_lock, needs_full_scan, rules (RuleStore), tokens (TokenStore), reconciler (Reconciler),
searcher (Searcher), trash (Trash), ops (ArchiveOps), suggester (SuggestionEngine),
pipeline (Pipeline), worker (Worker), intake (Intake), watcher (ArchiveWatcher),
imap (ImapFetcher|None – the first of `channels.fetchers`), smtp (SmtpReceiver|None)`; methods `start()`, `stop()`,
`recover()` (also removes stale OCR work directories), `rebuild(user=)`.
`Services(settings, *, household=None)` – without a household the `default` one is used.
`smtp` is only set for a single household; with several households the registry owns
the SMTP receiver.

Construction order: layout, channels (reads `config/channels.yaml`; `settings` becomes
`channels.effective_settings()`), rules, tokens, reconciler, searcher, trash, ops, suggester,
pipeline, worker, intake, watcher, then `channels.build()` (IMAP fetchers, scanner folders).
`start()` calls `channels.start()` (the fetchers), `stop()` `channels.stop()`. `services.settings`
may be replaced at run time by the channel manager (only `sources` and `imap` change). Constructors must not do I/O beyond cheap setup
and must not call collaborators that are constructed later.

### Configuration (config.py)

- `Settings` is frozen; `with_overrides(**changes)` returns a copy. `data_dir` is the
  data directory of the household the settings belong to, `root_dir` the installation's
  data root (`data_root` is only set for a household living below it), `household_name`
  and `ocr_parallel` (`DOCUSHELF_OCR_PARALLEL`, default 1) are new. Secrets can be given
  as files with the `_FILE` suffix.
- `AutoFileConfig(mode="off", threshold=0.75, sources, new_folders=True, wait_for_llm=True,
  notify=True)` in `Settings.auto_file` (never None). `mode`: `off`, `rules`, `confident`,
  `always` (German aliases `aus`, `regeln`, `sicher`, `immer`); `sources` is a tuple of
  item sources, a trailing `*` is a prefix wildcard, default `upload, smb, ftp, mail,
  smtp, watch-*` – `paperless` and `unbekannt` only when listed. Env:
  `DOCUSHELF_AUTO_FILE`, `_AUTO_FILE_THRESHOLD`, `_SOURCES`, `_NEW_FOLDERS`,
  `_WAIT_FOR_LLM`, `_NOTIFY`. Invalid values raise `ConfigError` (German). Property `enabled`.
- `LlmConfig(url, model, timeout=180, max_chars=6000, provider="ollama", api_key=None,
  context_tokens=8192)` – `Settings.llm` is None unless `DOCUSHELF_LLM_ENABLED`.
  `provider`: `ollama`, `openai` (any OpenAI-compatible chat-completions server, `url`
  may end in `/v1`) or `anthropic` (fixed endpoint, model and key from the settings or
  `ANTHROPIC_API_KEY`). `api_key` is never shown or logged. The document text is sent to
  the configured endpoint.
- `ImapConfig.allow_plaintext` (default False): with `ssl=False` STARTTLS is required.
  `ImapConfig.name` / `.owner` (optional): mailbox name from the channel settings and the household
  member its mail belongs to.
- `SourceConfig.endpoints: tuple[EndpointConfig, ...]` and `SourceConfig.endpoint(folder)`
  (case-insensitive). `EndpointConfig(folder, owner=None, enabled=True, label="Scanner")` is a
  per-person subfolder of a drop zone (see `inbox/intake.py`).
- `Settings.ios_shortcut_url` (`DOCUSHELF_IOS_SHORTCUT_URL`, https only, else `ConfigError`).

### Households (households.py)

Several families can share one installation; each household has its own archive, inbox,
trash, rules, tokens, audit log, index and channels. Background and decisions: `ARCHITECTURE.md`.

- **Without** `<data>/config/households.yaml`: exactly one household, id `default`, name
  `DOCUSHELF_HOUSEHOLD_NAME` (default "Zuhause"), living in the data root; access is
  governed by `DOCUSHELF_ALLOWED_*` / `ALLOW_ALL_OIDC_USERS` as before. Nothing moves on disk.
- **With** the file (`configured(settings)`):
  ```yaml
  admins: [erika@example.org]    # may manage the users of every household in the web UI
  admin_groups: [software-admins]  # OIDC groups whose members are admins as well
  households:
    - id: familie-a              # ^[a-z0-9][a-z0-9-]{0,31}$, used in paths
      name: "Familie A"
      path: "."                  # relative to the data root; default haushalte/<id>
      groups: [familie-a]        # OIDC groups with access
      users: []                  # preferred_username or verified e-mail (case-insensitive)
      owner_names: ["Erika Mustermann"]
      notify_url: null
      auto_file: {mode: always, threshold: 0.75}   # keys of AutoFileConfig
      imap: {host: imap.example.org, user: dokumente@example.org,
             password_file: /run/secrets/imap-familie-a}   # or password / password_env
      smtp_recipients: [scan-familie-a@example.org]
      watch_folders: {nas: /watch/familie-a}       # absolute paths, source name watch-<name>
  ```
  Validation (`ConfigError`, German): unknown keys are errors (a typo in `groups` must
  not change who has access); unique ids, resolved paths and SMTP recipients; at most one
  household with `path: "."`; no household directory inside another household's
  `archiv`, `eingang`, `papierkorb`, `config`, `protokoll` or `index` (`Layout.data_dirs()`);
  `imap` needs `host`, `user` and exactly one of `password`, `password_file`, `password_env`;
  paths must stay inside the data root. IMAP passwords are never logged or shown.
- `@dataclass(frozen=True) Household(id, name, path, groups, users, owner_names, notify_url,
  auto_file, imap, smtp_recipients, watch_folders)`; property `access` (German text).
- `load_households(settings, *, env=None) -> list[Household]` (single-household fallback
  included), `parse_admins(raw)`, `parse_admin_groups(raw)` (both casefolded), `default_household(settings)`, `households_file(settings)`,
  `configured(settings)`, `clean_member(value)` (trimmed, non-empty, at most
  `MAX_MEMBER_LENGTH` = 200 characters, no control characters; else `ConfigError`).
- `settings_for(settings, household, *, households=None) -> Settings` – without the file the
  settings unchanged; otherwise `data_dir = household.path`, `archive_dir = <path>/archiv`
  (`DOCUSHELF_ARCHIVE_DIR` only counts with a single household), `owner_names`, `notify_url`,
  `auto_file` (merged into the global one), `imap`, watch folders from the household. The
  global IMAP account, the global watch folders and the global `notify_url` belong to the household
  with path `"."`.
  `smtp` is None (the receiver is global).
- `class Registry(settings, *, services_factory=None, env=None)`: `households`, `default`
  (the only household, None in household mode), `multi`, `smtp`, `get(id)` (`NotFound`),
  `settings_for(id)`, `services(id)` (created lazily, kept), `created()`,
  `for_user(user) -> list[Household]` (local admin: all; SSO: by `groups`/`users`, or the
  classic allow rules without the file; a token user: none, the web layer knows its household),
  `admins` and `admin_groups` (from the file, casefolded), `is_admin(user)` (household mode
  only: the local account; an SSO account whose user name or e-mail is in `admins` or
  `settings.admin_users` (`DOCUSHELF_ADMIN_USERS`), or one of whose login `groups` is in
  `admin_groups` or `settings.admin_groups` (`DOCUSHELF_ADMIN_GROUPS`), all case-insensitive;
  never a token). Admin rights grant no household: the settings page (and the member section on
  it) still needs a household the admin belongs to; without one the SSO login is refused like for
  anybody else (`oidc_login_allowed`).
  Groups come from the session, so a group change applies at the next login. `change_members(id, *, add=|remove=) -> Household`,
  `by_smtp_recipient(address)` (households with a channel manager answer through
  `smtp_route`; `households.yaml` only for the others), `channels(id)` (manager of a created
  container or None), `uses_channel_file(id)`, `recover()`, `start()`, `stop()`, `close()`.
  Creating a container hooks its channel manager in: the global `smtp.recipient` is adopted by the
  household in the data root, and every change re-runs `protect_households` (watch folders). `start()` also
  protects the households' watch folders from each other (`inbox.intake.protect_households`)
  and, in household mode, starts the one global SMTP receiver.
- `change_members` (under the registry lock) reads `households.yaml`, refuses if the current file
  does not validate, edits the household's `users` (duplicates compared case-insensitively →
  `Conflict`; removing an unlisted name → `NotFound`), dumps the YAML (`sort_keys=False`: other
  keys and household order stay, comments are lost), validates the result with
  `parse_households`, refuses if the household ids differ from the running ones (adding or
  removing households needs a restart), then writes `households.yaml.bak` (previous text) and
  the new file with `fsops.atomic_write_text`. Finally `_apply_members` replaces the running
  `Household` objects with copies carrying the new `groups`/`users` (everything else stays as
  started), rebuilds the id/recipient maps, updates `services.household` of created
  containers and takes the new `admins` and `admin_groups`. `for_user`, the SSO login gate and the per-request
  session check use these lists, so access changes with the next request.
- Process-wide OCR limit: `pdf.ocr.OCR_SLOTS` (`BoundedSemaphore`), resized by
  `configure_ocr_slots(size)` (the registry calls it with `settings.ocr_parallel`). Every
  ocrmypdf run holds one slot, so households do not overload the machine together.

## Data on disk

```
<data>/archiv/…                      the truth (folders + files)
<data>/eingang/quellen/<source>/     drop zones (upload, smb, ftp, mail, smtp, watch-*)
<data>/eingang/arbeit/<item>/        being processed
<data>/eingang/bereit/<item>/        waiting for confirmation
<data>/eingang/fehler/<item>/        failed (item.json has "error", log.txt)
<data>/eingang/erledigt/<item>/      filed; originals kept keep_originals_days
<data>/papierkorb/<entry>/           entry.json + files/
<data>/config/rules.yaml, tokens.yaml, settings.yaml, paperless-import.yaml, secret.key
<data>/config/households.yaml        optional, global (see "Households")
<data>/config/channels.yaml(.bak)    optional, per household: channels set in the web UI
<data>/config/secrets/imap-<name>    IMAP passwords of channels.yaml (file 600, directory 700)
<data>/config/shortcuts/DocuShelf senden.shortcut   optional, global: served by GET /kurzbefehl
<data>/protokoll/audit-YYYY-MM.jsonl
<data>/index/docushelf.sqlite3, index/cache/ (thumbnails), index/tmp/ (OCR work dirs),
              index/server.lock (global, held while `serve` runs)
```

With `households.yaml` every household has this whole tree below its own directory
(`<data>/haushalte/<id>/` by default, `"."` = the data root). `settings.yaml`,
`secret.key`, `households.yaml`, `shortcuts/` and `server.lock` stay in the data root and are
global; `rules.yaml`, `tokens.yaml`, `channels.yaml`, `secrets/`, `paperless-import.yaml` and the
rest are per household.
`index/tmp/` is disposable like the rest of `index/`. Inbox item directories may briefly
hold `.docushelf-creating` and `original/.docushelf-*.part` while they are being created.

### Identity and metadata of archived files

- File name schema `YYYY-MM-DD Titel.pdf`, collisions ` (2)`. `format_filename` / `parse_filename`.
- XMP (`pdf/xmp.py`): `DocumentId` (UUID), `OriginHash` (sha256 of what arrived),
  `DocumentDate`, `Sender`, `Source`, `ReceivedAt`, `FiledAt`, `dc:title`, `dc:subject` (tags).
- **Title/date shown and indexed** (`derive_title_date`): if the file name follows the
  schema → date and title from the file name (the user may rename in Finder; the name wins).
  Otherwise title = XMP title if the file carries a `DocumentId`, else the file stem;
  date = XMP `DocumentDate` or None. A collision suffix is dropped from the title.
- **Document id**: XMP `DocumentId` if present and not already used by another live file
  (copies made in Finder share the id → the second one gets a provisional id);
  otherwise `provisional_id(content_hash)` (`h-<32 hex>`), plus `-2`, `-3` … if taken.
- Non-PDF files in the archive (images, office files) are indexed by name/folder only
  (`text_status="unsupported"`), never modified by metadata edits except renames.

## Modules to implement

### pdf/render.py
- `render_page_png(path, page: int, *, width: int = 300) -> bytes` – page is 1-based;
  width clamped to 64..2000; holds `PDFIUM_LOCK`; raises `NotFound` for pages out of range
  and `UnsupportedFile` for PDFs that need a password (`render_page_image`, `render_page_png`
  and `ThumbnailCache.get` alike); other unreadable files raise `ProcessingError`.
- `class ThumbnailCache(cache_dir: Path)`:
  `get(path: Path, page: int, width: int, *, key: str) -> Path` – returns a cached PNG
  (`<cache>/<key[:2]>/<key>-p<page>-w<width>.png`), renders on miss, thread-safe
  (atomic write). `key` is chosen by the caller (content hash, or item id + mtime).
  `prune(max_bytes: int = 500 MB) -> int` deletes oldest files.

### pdf/normalize.py
- `SUPPORTED_IMAGE_EXT`, `SUPPORTED_EXT`; `detect_kind(path) -> "pdf" | "image" | None`
  (magic bytes first, extension second).
- `normalize_to_pdf(files: list[Path], out: Path) -> int` – one PDF with all pages of
  all files in order; returns the page count. PDFs are copied page by page (pikepdf);
  images are embedded losslessly with img2pdf where possible (JPEG, PNG without alpha,
  TIFF), otherwise converted with Pillow (alpha → white, palette/CMYK handled, HEIC
  via pillow-heif, EXIF orientation applied). Images without DPI (phone photos) are
  scaled to fit A4. Multi-page TIFF → multiple pages. Raises `UnsupportedFile`
  (German message) for unknown types and `ProcessingError` for broken PDFs. A single
  signed or password-protected PDF is copied byte for byte (page count 0 for a password
  PDF); a password PDF that has to be merged with other files is a `ProcessingError`.
- `pdf_protection(path) -> "signed" | "encrypted" | None` (never raises; `encrypted` = needs
  a user password), `is_signed(pdf)`, `PROTECTION_SIGNED`, `PROTECTION_ENCRYPTED`.
- `detect_blank_pages(pdf: Path, *, threshold: float, text_pages: list[str] | None = None) -> list[int]`
  – 1-based numbers of blank pages: render ~40 dpi grayscale, ignore ~6 % margins,
  count dark pixels after a small blur to suppress scanner noise; ratio < threshold →
  blank. Pages with ≥ 15 non-space characters of text are never blank.
- `remove_pages(src: Path, dst: Path, pages: list[int]) -> None` – never removes all
  pages (raises `ValueError`).

### pdf/ocr.py
- `@dataclass OcrResult(performed: bool, output: Path, skipped_reason: str | None,
  warnings: list[str], returncode: int, duration_s: float)`
- `run_ocr(src, dst, settings, *, log=None) -> OcrResult` – `ocrmypdf` CLI in a
  subprocess: `--skip-text --rotate-pages --deskew --output-type <settings.ocr_output_type>
  -l <ocr_languages> --jobs <ocr_jobs> --optimize 1 --tesseract-timeout 180
  <ocr_extra_args> src dst`. Each run first acquires one slot of the process-wide
  `OCR_SLOTS` semaphore (`DOCUSHELF_OCR_PARALLEL`, shared by all households; waiting is
  logged to the item log). Time limit `time_limit(settings, pages) = max(settings.ocr_timeout,
  pages * SECONDS_PER_PAGE)` with `SECONDS_PER_PAGE = 10`; exceeding it raises
  `OcrTimeout(ProcessingError, transient=False)` with a German hint; a killed process stays
  transient. Exit 0 ok; 10 (PDF/A conversion failed, output exists) → accept with warning;
  6 → copy src; 8 (encrypted) → `ProcessingError("PDF ist verschlüsselt")`;
  others → `ProcessingError` with the stderr tail. OCR disabled, binary missing, digitally
  signed PDF → copy src, `performed=False`, reason in German.
- Work directory `<data>/index/tmp/ocr-<pid>-<random>` (`workdir_root(settings)`), touched every
  `HEARTBEAT_SECONDS` (60) and removed afterwards. `remove_stale_workdirs(settings, *, now=None)
  -> int` removes `ocr-*` directories unchanged for `ocr_timeout + STALE_MARGIN_SECONDS` (600)
  unless a live process owns them (called by `Services.recover()` and the hourly maintenance).
- `ocr_available() -> bool`, `ocr_version() -> str | None`.

### index/search.py
- `@dataclass SearchQuery(text="", folder: str | None = None, date_from: date | None = None,
  date_to: date | None = None, tags: list[str] = [], sender: str | None = None,
  limit: int = 50, offset: int = 0)`
- `parse_query(raw: str) -> SearchQuery` – free text plus inline filters
  `ordner:"Finanzen/Bank"`, `tag:Garantie`, `von:Sparkasse`, `jahr:2025`,
  `ab:2025-01-01`, `bis:2025-12-31` (also `datum:`; years, months and day formats incl.
  German `12.03.2025`); quotes keep phrases together.
- `@dataclass Snippet(page: int, html: str)` – HTML-escaped text with `<mark>…</mark>`.
- `@dataclass SearchHit(doc: DocumentRecord, score: float, snippets: list[Snippet],
  matched_in: list[str], pages: list[int])` – `matched_in` ⊆ {"title","sender","folder","tags","filename","text"}.
- `@dataclass SearchResult(hits: list[SearchHit], total: int, query: SearchQuery,
  corrections: dict[str, list[str]], took_ms: int)` – `corrections` maps a query term
  to the similar vocabulary words that were used instead ("Meinten Sie …").
- `class Searcher(services)`: `search(query) -> SearchResult`,
  `suggest_terms(prefix: str, limit: int = 8) -> list[str]`.
- Semantics: all terms must match (AND). A term matches as a substring (≥ 3 normalized
  chars: FTS5 trigram MATCH on `fts_docs` or `fts_pages`; shorter terms: `instr()` on the
  normalized columns). Phrases match as substrings of the normalized text. Typo
  tolerance: a term of ≥ 5 chars without any hit is replaced by vocabulary words within
  Levenshtein 1 (2 for ≥ 9 chars) – reported in `corrections`. Filters in SQL (folder
  subtree, date range, tags all present, sender substring). Ranking: hits in title and
  sender weigh most, then folder/tags/filename, then bm25 of pages and number of
  matching pages; newer documents win ties. Up to 3 snippets per document (~160
  chars, page numbers) built with `normalize_with_map`. Empty text with filters lists
  matching documents by date (newest first).
- Joined text: an unquoted term also matches the "joined" form of the text (`textnorm.joined`:
  digit groups and hyphenated words written together, tables `fts_joined`, `fts_docs_joined`)
  when it contains a digit and has ≥ 6 chars, when it normalizes to several words and its
  joined form has ≥ 6 chars, or when it is a letter word of ≥ 3 chars. Before typo correction a
  closed word of ≥ 6 chars without a hit is also tried with one space inserted (parts ≥ 3);
  such spellings are reported in `corrections` ("Haftpflichtversicherung" →
  "Haftpflicht Versicherung"). `corrections` and `suggest_terms` return words as spelled in
  the archive, not normalized.

### index/reconcile.py
- `class Reconciler(services)`:
  - `scan(*, full: bool = True, verify: bool = False, cancel: threading.Event | None = None)
    -> ScanStats` – holds
    `archive_lock`. Walk the archive (no symlinks followed, `is_ignored` names and
    `.docushelf-*` files skipped; stale `.docushelf-*.part` older than 1 h deleted).
    Upsert every directory into `folders` (empty folders count). For each file: skip if
    indexed with identical size, mtime_ns and inode (unless `verify`). Changed or new
    files: read identity (XMP id, content hash). New paths are matched against
    documents whose path vanished in this pass – by XMP id first, then content hash –
    and treated as **move/rename** (id, text and tags kept). Unmatched new files are
    added; vanished, unmatched documents are removed. Content changed in place →
    re-extract text, keep id unless the XMP id changed. Transient read errors
    (`EACCES`, `EBUSY`, file vanished mid-scan) never drop a document from the index;
    they count as `errors`. Files whose size/mtime still change between two stats are
    skipped this round. Sets index meta `last_scan` (and `last_full_scan`).
  - `index_file(path: Path) -> DocumentRecord` – (re)index one archive file now (used
    by ArchiveOps); keeps the existing id of the record at that path if the identity
    matches.
  - `forget(rel_path: str) -> None`.
  - `derive_title_date(filename: str, meta: DocMeta) -> tuple[str, date | None]` (module-level function).
  - `ensure_folder(rel: str) -> None` (index-only, used after mkdir).
- Required tests (real files): new, rename, move, move+rename, delete, copy (same XMP
  id), foreign file without XMP, in-place modification, empty folder, NFD file names,
  ignored files, file growing during scan, scan after the index was deleted gives the
  same ids for files with XMP, idempotent repeated scans.

### index/watcher.py
- `class ArchiveWatcher(services)`: `start()` (initial full scan in a thread, then
  watchdog observer on the archive, recursive), `stop()`, `request_scan(reason: str = "")`,
  attributes `last_result: ScanStats | None`, `last_error: str | None`, `scanning: bool`.
  Debounce: scan after 2 s of quiet, at most 20 s after the first event. Periodic full
  scan every `settings.reconcile_interval`. If the observer cannot start (inotify
  limits, unsupported FS) fall back to polling every 60 s and log a warning. After a pass
  that moved or removed documents it calls `pipeline.queue_refresh(only_stale=True)`.

### archive/ops.py – `class ArchiveOps(services)`
All methods take keyword `user: str | None` for the audit log and hold `archive_lock`.
Transient OS errors (`EBUSY`, `EACCES`, `ETXTBSY`) are retried 3× with 0.5 s pause.
- `file_item(item_id, request: FilingRequest, *, user, auto: dict | None = None) -> DocumentRecord`
  – the filing transaction (`auto` is the record of an automatic decision, see
  `suggest/auto.py`; it is kept in the journal):
  1. load item (must be in `bereit`, stage `ready`); validate the folder with
     `resolve_folder(root, raw)`: existing on-disk components are reused verbatim (exact,
     NFC, casefold, then loosely: whitespace collapsed, trailing dots/spaces ignored), only new
     components are sanitized, hidden/ignored names are never reused, files and symlinks are
     refused (same for `update_document`, `move_document`, `create_folder`); validate the title;
     refuse if `item.filing` shows an unfinished filing (run `recover()` first).
  2. `doc_id = new_doc_id()`; write `work/filing.pdf` = `document.pdf` + XMP
     (title, date, tags, sender, origin hash, source, received_at, filed_at, doc id,
     extras like `ArchiveSerialNumber`/`PaperlessId`/`MailSubject`/`PaperlessNotes`/
     `PaperlessFields` from the item context, see `context_extras`). A protected document
     (`context["pdf_protection"]`: signed or encrypted PDF) is copied byte for byte without
     XMP; the journal gets `"protected": kind` and `"unstored": [keys]` (extras that could not
     be stored); the record gets a provisional id.
  3. journal: `item.filing = {"state": "publishing", "doc_id", "folder", "filename",
     "sha256": <of filing.pdf>, "started_at"}`; save item.json.
  4. `publish_no_clobber(filing.pdf, archive/folder, format_filename(title, date), move=True)`;
     record the final path in the journal (`"state": "published", "path"`), save.
  5. index (`reconciler.index_file`), audit `file` (plus `auto_file` with `{doc_id, path, title,
     folder, confidence, review, sources, reasons, item}` for an automatic filing, and
     `item.context["auto"]`), optional rule (`services.rules`), move the item to `erledigt`
     (delete `document.pdf`, `text.json`, `work/`; keep `original/`, `item.json`, `log.txt`),
     `item.filing["state"] = "done"`.
- `recover() -> int` – for items with an unfinished journal: if a file with the journal
  sha256 exists at the recorded path or anywhere in the target folder → finish steps 5;
  otherwise reset the journal so the user can file again. Also deletes stale
  `.docushelf-*.part` files in inbox work dirs. Returns the number of repaired items.
- `update_document(doc_id, update: MetadataUpdate, *, user) -> DocumentRecord` – PDF:
  write the new XMP into a temp file in the same directory, `replace_if_unchanged`
  (Conflict → German message "Datei wurde zwischenzeitlich geändert"), then rename/move
  without clobbering if title, date or folder changed. A provisional id is replaced by a
  new UUID at the first metadata write (the returned record has the new id).
  Non-PDF and signed/encrypted PDFs: only title/date (rename) and folder (move);
  tags/sender raise `UnsupportedFile` (a `ProcessingError`) with a German message.
- `move_document(doc_id, folder, *, user) -> DocumentRecord`
- `delete_document(doc_id, *, user) -> str` – moves the file to the trash (via `services.trash`), returns the entry id.
- `create_folder(rel, *, user) -> str`, `rename_folder(rel, new_name, *, user) -> str`
  (new rel; documents re-indexed by path prefix; folder prefixes in the suggestions and the
  import suggestion of `bereit` items are rewritten and `pipeline.queue_refresh(only_stale=True)`
  runs), `delete_folder(rel, *, user)` (only empty folders, else `Conflict`).
- `delete_inbox_item(item_id, *, user) -> str` – trash entry id.
- Required tests: crash after each step of the filing transaction (monkeypatch to raise)
  followed by `recover()` ⇒ exactly one archive file, no lost original; collision
  suffix; concurrent modification detection; provisional → UUID; non-PDF restrictions.

### archive/trash.py – `class Trash(services)`
- Entry: `papierkorb/<entry_id>/entry.json` + `files/<name>`; `entry.json` =
  `{"id","kind": "document"|"inbox","deleted_at","deleted_by","original_path"|"item_id",
  "title","doc_id","meta": {...}}`.
- `put_document(abs_path: Path, record: DocumentRecord, *, user) -> str`,
  `put_inbox_item(item: InboxItem, *, user) -> str`,
  `list() -> list[TrashEntry]` (newest first), `get(entry_id) -> TrashEntry`,
  `restore(entry_id, *, user) -> DocumentRecord | InboxItem` (documents go back to the
  original folder, recreated if needed, no clobber; inbox items back to `bereit`),
  `purge(entry_id, *, user)`, `purge_expired() -> int` (`trash_retention_days`),
  `purge_done_items() -> int` (`eingang/erledigt` older than `keep_originals_days`; never
  items whose filing journal is not `done` or has `unstored` keys).
- `@dataclass TrashEntry(id, kind, deleted_at, deleted_by, title, original_path, item_id, doc_id, files: list[Path], meta: dict)`.

### inbox/intake.py – `class Intake(services)`
- `start()/stop()`; polls all sources every 2 s; `trigger()` wakes it immediately. Every
  household has its own intake (own drop zones and watch folders, own inbox);
  `protected_paths` (set by `protect_households`) lists other households' data directories,
  and a watch folder overlapping them is ignored.
- `scan_once() -> list[str]` – ids of created items (used by tests).
- Source dirs: `settings.sources` (`path or layout.source_dir(name)`).
- Plain files: taken once `(size, mtime_ns)` stayed unchanged for `stable_seconds`
  (and at least one earlier observation when `stable_seconds > 0`).
- Batch dirs with `_sendung.json`: one item per manifest document, `context` and
  `options` copied; the batch dir is removed afterwards. Directories without manifest:
  their files are taken one by one (recursive); emptied subdirectories are kept
  (never the source root).
- External watch folders with `remove_after_import=False`: copy, remember the hash
  (`index.mark_hash_seen`) and skip known hashes.
- Scanner endpoints (`source.endpoints`): a first-level subfolder matching an endpoint
  (case-insensitive) belongs to that person. Disabled endpoints are not entered (files wait);
  files (and batches) below an enabled one get `owner`, `owner_label` and `endpoint` in their
  context (`channels.origin.mark`, existing values win). Endpoint folders are never removed.
- After creating an item: `index.enqueue("process", item.id)` and `worker.kick()`.

### inbox/pipeline.py – `class Pipeline(services)`
- `process(item_id) -> InboxItem` – runs the remaining stages (see `STAGES`), saves
  item.json after each, logs German lines to the item log:
  1. `received → normalized`: `normalize_to_pdf(originals, work/normalized.pdf)`. For a single
     file, `item.context["pdf_protection"]` = `"signed"` | `"encrypted"` is set (removed
     otherwise, recomputed on retry).
  2. `normalized → deblanked`: blank page removal unless disabled
     (`settings.blank_page_removal`, `item.options["skip_blank_removal"]`) → `work/deblanked.pdf`, `pages_removed`.
  3. `deblanked → ocr_done`: `run_ocr(work/deblanked.pdf, document.pdf)` unless
     `item.options["skip_ocr"]` (copy). For protected items blank-page removal and OCR are
     skipped, `document.pdf` stays byte-identical to the original; if its text cannot be read,
     `text.json` is `[]` and the item log says so.
  4. `ocr_done → analyzed`: `extract_pages(document.pdf)` → `text.json`;
     `analysis.extract.analyze(pages, item.context, known_senders=suggester.known_senders())`
     → `item.analysis`; duplicate check: same `origin_hash` in the index
     (`kind: "archive"`) or in another inbox item (`kind: "inbox"`) → `item.duplicate_of`.
  5. `analyzed → ready`: `suggester.suggest(item, pages, analysis)` → `item.suggestions`
     (dicts); move to `bereit`; delete `work/`; then follow-ups: with automatic filing on and the
     source eligible (`AutoFileConfig.sources`), `auto_file` is enqueued; with `settings.llm`,
     `llm_suggest` is enqueued. With the model on and `wait_for_llm`, only `llm_suggest` is
     enqueued, carrying `payload["auto_file"] = True`; the worker enqueues `auto_file` when it
     ends (successfully or not).
  - `ProcessingError(transient=True)` propagates (the worker retries). Any other
    exception → `fail(item_id, exc)`.
- `fail(item_id, exc) -> InboxItem` – `item.error = {"stage","message","at"}`, move to
  `fehler`, log, `notifier.send(...)`.
- `retry(item_id, *, skip_blank_removal: bool | None = None, skip_ocr: bool | None = None) -> InboxItem`
  – from `fehler` or `bereit` back to `arbeit`, stage `received`, error cleared, enqueue.
  While automatic filing is on this marks the item `options["no_auto"] = True`.
- `refresh_suggestions(item_id, *, manual=True) -> InboxItem` – recompute analysis, duplicate
  check and suggestions of a `bereit` item (LLM suggestions are kept). `manual=True` (web, API)
  sets `no_auto` while automatic filing is on; queued `refresh` jobs pass `manual=False`.
- `queue_refresh(*, only_stale=False) -> int` – enqueue `refresh` for `bereit` items; with
  `only_stale` only those whose top suggestion names a folder that does not exist in the index
  now. Items with an unfinished filing journal are skipped.
- `run_llm(item_id) -> InboxItem` – append the LLM suggestion (source "llm") if any; the
  German reason for a missing one goes to the item log.
- `auto_file(item_id) -> AutoDecision` – loads the `bereit` item under the pipeline lock,
  calls `suggest.auto.decide`, and on `file` calls `services.ops.file_item(item_id, request,
  user="automatisch", auto=<decision record>)`, then notifies (`Abgelegt: <Titel>`, message
  `<Ordner> · <n> %`, plus ` – bitte prüfen` when `review`; `click` links to the document).
  On `hold` it writes `item.context["auto"] = {"decision": "hold", "confidence", "reasons",
  "at"}` and a German item log line; the item stays in `bereit`. A failing filing is a `hold`
  and is never retried in a loop; errors are not raised. `NotFound`/`Conflict` if the item is
  not (or no longer) in `bereit`. Items with `no_auto` are never filed automatically.

### inbox/worker.py – `class Worker(services)`
- `start()/stop()`, `kick()`, `resume_pending()` (enqueue `process` for every item in
  `arbeit`).
- `settings.worker_threads` threads claim job kinds `process`, `llm_suggest`,
  `refresh`, `auto_file` (`JOB_KINDS`). Transient errors: retry after 30 s, 2 min, 10 min
  (max 3 attempts), then `pipeline.fail`; `auto_file` is never retried. A job claimed again and
  again without finishing (crash) is not started once more; its item moves to `fehler`. OCR
  timeouts are not transient. A maintenance thread runs every 60 s: hourly
  `trash.purge_expired()`, `trash.purge_done_items()` and `remove_stale_workdirs`,
  `ThumbnailCache.prune()` daily, and every 10 minutes `resume_pending()` re-queues `arbeit`
  items whose job got lost. Each household has its own worker and maintenance thread.
- `status() -> dict` for the UI (threads alive, current jobs).

### analysis/extract.py and analysis/doctypes.py
- `analyze(pages, context=None, *, known_senders=None, today=None) -> Analysis`.
- Dates: `12.03.2025`, `12.3.25`, `12. März 2025` (also `Mrz`, `Sept.`), `2025-03-12`,
  `März 2025` (→ 1st of month, lower score). Labelled dates win ("Rechnungsdatum",
  "Datum", "<Ort>, den", "Bescheid vom", "Stand", "Ausstellungsdatum"); due/period/birth
  dates lose ("fällig", "zahlbar bis", "Zeitraum", "Leistungszeitraum", "geb.");
  dates more than 60 days in the future or before 1900 are dropped.
- Date refinements: `Rechnungsdatum / Lieferdatum`, `Rechnungs- und Leistungsdatum` are document
  date labels; `bis`/`zum` after a deadline label keep it a deadline; `doc_date` is only set when
  the best score is above `MIN_DATE_SCORE` (0.25).
- Sender: known senders count (0.95) only in the letterhead (page 1, first `LETTERHEAD_LINES`
  = 5 lines) or the return line, otherwise 0.5. Bank details and third-party fields
  ("Versichert bei:", "Arbeitgeber" …) are never sender evidence; `context["owner_names"]` are
  excluded. Public `letterhead_text(pages) -> str` (normalized page-1 letterhead without
  recipient block, bank details and mail header lines). Order: known senders found in page 1
  first, then letterhead lines with company
  markers (GmbH, AG, SE, KG, e.V., Bank, Sparkasse, Versicherung, Stadtwerke,
  Finanzamt, Krankenkasse, AOK, Landeshauptstadt …), then mail domains / URLs. For
  mail items the mail sender is used only if it is not one of our own forwarding
  addresses (`context["forwarded_by_owner"]`) – forwarded mails: look for "Von:"/"From:".
- Amounts (`1.234,56 €`, `EUR 12,00`), total near "Gesamt|Summe|Rechnungsbetrag|zu zahlen|Endbetrag".
- IBANs (mod-97 validated), references (Kunden-, Vertrags-, Rechnungs-, Versicherungs-/
  Policen-, Mitglieds-, Steuer-, Personal-, Zähler-, Bestell-, Auftragsnummer, Aktenzeichen).
- `doc_type` by keyword scoring (`doctypes.DOC_TYPES`: Rechnung, Kontoauszug, Vertrag,
  Bescheid, Steuerbescheid, Mahnung, Kündigung, Gehaltsabrechnung, Versicherungsschein,
  Beitragsrechnung, Nebenkostenabrechnung, Arztbrief, Rezept, Zeugnis, Bescheinigung,
  Angebot, Auftragsbestätigung, Lieferschein, Quittung, Information …).
- `keywords`: top content words.

### suggest/rules.py
- `config/rules.yaml`:
  ```yaml
  rules:
    - name: Stadtwerke Strom
      match:            # all given conditions must hold; lists inside a condition are OR
        sender: [Stadtwerke Dresden, DREWAG]   # word-start match in the detected sender; page-1 letterhead only if none was detected
        keywords_any: [Abschlag]
        keywords_all: []
        iban: []
        reference: ["123456789"]               # appears anywhere in the text
        source: [mail]
        mail_from: [rechnung@example.com]
        doc_type: [Rechnung]
      action:
        folder: Wohnung/Strom
        title: "{sender} {type} {date:%m/%Y}"   # optional template
        tags: ["steuerrelevant {year}"]
      auto: false                               # true: file automatically (modes rules/confident/always)
  ```
- `@dataclass RuleMatch`, `@dataclass RuleAction(folder, title=None, tags=[])`,
  `@dataclass Rule(name, match, action, auto=False)`.
- `@dataclass RuleHit(rule, reasons, page1_only=False)`.
- `class RuleStore(path)`: `load() -> list[Rule]` (cached by mtime; on an invalid file
  keep the last good rules and expose the message in `.error`), `save(rules)`,
  `add(rule)` (appends to the file text, keeping comments and layout), `replace(index, rule)`,
  `delete(index)`, `raw_text()`, `save_raw_text(text)` (validate first, raise `ConfigError` with
  German message), `matches(analysis, pages, context, source) -> list[RuleHit]`,
  `match(...) -> list[tuple[Rule, list[str]]]` (rule + German reasons). A structured save of a
  file with user comments writes `rules.yaml.bak` first (`backup_path`, `last_backup`).
- `render_title(template, *, analysis, context, doc_date) -> str` – placeholders
  `{sender} {type} {date:%Y-%m} {year} {month} {month_name} {subject} {original}`;
  missing values collapse cleanly.
- `rule_from_filing(request: FilingRequest, analysis: Analysis, source: str) -> Rule` – matches
  on `request.sender` (never the analysis sender), else a reference, then an IBAN, then a
  dedicated watch source, else `ConfigError`; no title template when the title names another
  month or year than the document date.

### suggest/similarity.py – `class SimilarityModel(services)`
- Lazily (re)built from `index.iter_corpus()` when `index.generation` changed (at most
  every 30 s, thread-safe). TF-IDF over `content_words`, cosine similarity.
- `suggest(normalized_text, *, sender=None, limit=3) -> list[Suggestion]` – top-7
  neighbours aggregated per folder, score 0..1; reasons like
  `ähnlich zu „2025-11-03 Kontoauszug November“ (82 %)` and
  `gleicher Absender wie 12 Dokumente in diesem Ordner`. Title proposal from the
  nearest neighbour (month names/years replaced by the new date) when similarity ≥ 0.5.

### suggest/llm.py – `class LlmClient(config: LlmConfig, *, transport=None)`
- Providers (`config.provider`): `ollama` – `POST {url}/api/chat` with `stream: false`,
  `think: false`, `format: <ANSWER_SCHEMA>` (falls back to `"json"` on HTTP 400) and
  `options: {temperature: 0, num_ctx: context_tokens}`; `openai` – `POST {url}/v1/chat/completions`
  (not added twice if `url` ends in `/v1`) with `response_format` `json_schema` strict, then
  `json_object`, then plain on HTTP 400, `Authorization: Bearer` only with an `api_key`;
  `anthropic` – Messages API through the `anthropic` SDK with `output_config` json_schema, low
  effort. Formats a server rejected are skipped next time. One retry (after `retry_delay`) on
  connection errors; `timeout` per request.
- German system prompt (title rule `<Dokumentart> <Absender kurz> <Zeitraum oder Betreff>`
  without date, existing folder preferred, a new folder only following the existing structure).
  The user prompt (`build_prompt(LlmRequest, max_chars=)`) holds the folder list with document
  counts (at most `MAX_FOLDERS` = 150, candidates first), example file names of the top
  candidate folders, the automatically detected facts (marked as possibly wrong), similar
  documents with folder and score, and the document text (`max_chars`, plus the last
  `TAIL_CHARS` if longer). Answer keys: `dokumenttyp, absender, datum, titel, begruendung,
  ordner, neuer_ordner, sicherheit, tags`.
- `@dataclass LlmRequest(text, folders, counts, candidates, examples, similar, analysis, context)`,
  `@dataclass LlmResult(suggestion, confidence, answer, error, duration_s)`.
- `classify(request) -> LlmResult` never raises; `error` is a German reason when there is no
  suggestion. `suggest(text, folders, context=None, **details) -> Suggestion | None` wraps it.
  `probe() -> dict` classifies a built-in sample letter (`ok, provider, model, duration_s,
  answer, error`) for the settings page. `last_status` keeps the outcome of the last request.
  Properties `provider`, `endpoint`, `host`.
- Validation: `ordner` is matched to an existing folder (on-disk spelling, case-insensitive);
  a new folder only if `neuer_ordner`, its parent exists (or the archive has fewer than 3
  folders) and it is at most one level deeper than its deepest sibling. `datum` must parse and
  occur in the text or equal a detected candidate, else dropped. `titel` is cleaned (no date
  prefix, extension, quotes; ≤ 100 chars). `sicherheit` is clamped to 0..1 (missing: 0.5). The
  suggestion has `source="llm"`, `score = sicherheit * 0.9`, `reasons=[begruendung]`.

### suggest/engine.py – `class SuggestionEngine(services)`
- `suggest(item, pages, analysis) -> list[Suggestion]` – merges: import suggestion
  (`item.context["import_suggestion"]`, score 0.95; its folder is re-rendered from
  `config/paperless-import.yaml` on every call via `importers.paperless.recompute_import_suggestion`;
  an import for a folder missing from the index yields to an existing folder with similarity
  ≥ 0.9 or the same sender's renamed sibling folder), rules (`RULE_SCORE` 1.0, letterhead-only
  sender hits `WEAK_RULE_SCORE` 0.9), similarity, folders of the same sender, heuristic fallback
  (existing folder whose name matches sender or document type; else a new folder proposal
  `new_folder=True`, low score, only if nothing fits). Deduplicate by folder (merge reasons),
  sort by score, at most 5. Every suggestion carries title, date and sender (fall back to
  analysis values / `title_from_original_name` / "Dokument vom …"). Suggestions are
  `RankedSuggestion`s: extra keys `rule_auto` (rule marked `auto: true`), `signals` (score per
  source), `title_source`, `date_source`, `confidence` (LLM only) end up in `item.json`.
- `llm_suggest(item, pages, analysis, *, log=None) -> Suggestion | None` – builds the request
  with `llm_request(...)` (folders with counts, candidate folders and example file names,
  similar documents) and calls `LlmClient.classify`; `log` receives `KI: <German reason>`.
- `llm_status() -> dict | None` (`provider, model, host, endpoint, reachable, ok, error, at,
  duration_s`; None without a model) and `llm_probe() -> dict | None` for the settings page.
- `known_senders() -> list[str]` (index senders + rule senders).

### suggest/auto.py – automatic filing decision
- `decide(item, *, settings, folder_exists: Callable[[str], bool]) -> AutoDecision` – never
  files anything. `@dataclass AutoDecision(action: "file"|"hold", request: FilingRequest | None,
  confidence, review=False, sources, reasons)` with `to_dict()`.
- Order: (1) `hold` if mode is `off`, the source is not in `sources`, the item is not in
  `bereit`, `duplicate_of` is set, there is no suggestion, or `options["no_auto"]` is set;
  (2) a suggestion with `rule_auto` → `file`, confidence 1.0; (3) mode `rules` → `hold`;
  (4) combine per folder with `SOURCE_WEIGHTS` (rule 1.0, import 1.0, similarity 1.0, llm 0.9,
  heuristic 0.5): `1 − Π(1 − weight · score)`; (5) a runner-up within 0.1 costs 20 %
  ("zwei Ordner kommen in Frage"); (6) a folder that does not exist: `hold` unless
  `new_folders`, then 10 % less unless a rule or the import named it; (7) title (rule > import
  > llm > similarity), date (a source agreeing with the analysis, else `analysis.doc_date`,
  else the received date at 85 %), sender, tags (rule tags only) – no usable title → `hold`;
  (8) mode `confident` files at or above `threshold`, mode `always` files everything and sets
  `review` below it. Confidence is rounded to three places before the comparison.
- Helpers: `source_allowed(source, sources)`, `describe_sources(sources)` (German labels),
  `confidence_percent(value)` (rounds down), `SOURCE_LABELS`.
- Records: audit actions `auto_file` (written by `ArchiveOps`) and `auto_review`
  (`{doc_id, status: "ok"}`, written when a person confirms with "Passt").

### channels/store.py, channels/manager.py, channels/origin.py – channel settings
- `config/channels.yaml` of a household (written by the web UI; format in the module docstring of
  `store.py`): `imap` (list of `Mailbox(name, host, user, port, tls "ssl"|"starttls", folder,
  processed_folder, rejected_folder, no_attachment_folder, allowed_senders, poll_seconds, owner,
  enabled)`), `smtp` (`SmtpChannel(enabled, recipients: Recipient(address, owner))`), `scanner`
  (`smb`/`ftp`: `ScannerChannel(kind, enabled, endpoints: Endpoint(folder, owner, enabled))`), `watch`
  (`WatchFolder(name, path, remove_after_import, enabled)`), all in `ChannelConfig` (frozen,
  `to_dict()`). `parse_channels(text)` is the only loader (start-up and before every write):
  unknown keys, bad names (`[a-z0-9-]`, ≤ 32), folders (`[A-Za-z0-9._-]`, no item ids), addresses,
  sender lists, ports, intervals (10 s … 24 h), duplicates → `ConfigError` (German).
  `dump_channels(config)`; helpers `build_mailbox(values, name=, where=)`, `build_watch(values)`,
  `clean_address`, `clean_folder`, `clean_name`, `clean_owner`.
- `ChannelStore(config_dir)`: `path`, `backup_path`, `secrets_dir`, `exists()`, `load()` (also
  requires a password for every enabled mailbox), `password(name)` (tightens loose permissions to
  600), `has_password`, `secret_path(name)` (`secrets/imap-<name>`), `save(config, *, passwords=,
  remove_secrets=)`: dump, parse again, check passwords, write new secrets (600, directory 700),
  previous file → `channels.yaml.bak`, atomic write; secrets of mailboxes that are gone are deleted.
  Nothing is written if validation fails.
- `ChannelManager(services, settings, *, fetcher_factory=None)` = `services.channels`:
  `from_file`, `config` (without the file: `defaults_from_settings(settings, household)` – the
  environment mailbox as `postfach`, `households.yaml` recipients plus `smtp.recipient`, smb/ftp
  from the sources, watch folders), `effective_settings()` (without the file the base settings
  unchanged; with it `sources` = builtin drop zones + enabled scanners with `EndpointConfig`s +
  enabled watch folders, `imap` = first enabled mailbox), `imap_configs()`, `fetchers`
  (`name → ImapFetcher`), `password(name)`, `has_password(name)`, `smtp_route(address) ->
  Recipient | None` (None when switched off or unknown), `mark_smtp(context, address)`,
  `adopt_global_recipient(address)` (defaults only), `build()`, `start()`, `stop()`,
  `update(change, *, passwords=, renamed=)` (under a lock: `change(config) -> config`, validate,
  carry over passwords of the environment or of a renamed mailbox, `store.save`, then `apply()`;
  `ConfigError` leaves everything unchanged), `reload()`, `apply()` (replace
  `services.settings`, create scanner folders with the mode of the drop zone, stop/start only
  fetchers whose `ImapConfig` changed, restart a running intake, call `listeners`),
  `fetcher_status(name)`.
- `origin.py`: context keys `owner`, `owner_label`, `endpoint`; `mark(context, *, owner,
  endpoint)`, `owner_label(owner)` (`erika.mustermann@x` → `Erika Mustermann`),
  `origin_label(context)` → `von Erika · Scanner` (Jinja filter `origin_label`).

### channels/mailparse.py, channels/imap.py, channels/smtp.py
- `extract_documents(message, *, min_image_bytes=20_000) -> tuple[list[list[tuple[str, bytes]]], dict]`
  – every PDF is its own document, all images of one mail form one document;
  forwarded `message/rfc822` parts are searched too; small inline images (logos)
  skipped. Context: `mail_from`, `mail_from_name`, `mail_subject`, `mail_date`,
  `message_id`, and for forwarded mails `forwarded_from`.
- `sender_allowed(address, allowed) -> bool` – entries are addresses or `@domain`;
  empty allowlist ⇒ nothing allowed; `*` ⇒ everything.
- `ImapFetcher(services)`: one per household (its mailbox is `services.settings.imap`, see
  `households.settings_for`), writes only into its own household's drop zone; poll every
  `poll_seconds`; with `ssl=False` STARTTLS is required before login (`ImapError` unless
  `allow_plaintext`), certificates are verified; `fetch_once() -> int`; folders
  created if missing; allowlist (else move to `rejected_folder`); de-duplicate by
  Message-ID hash (`index.mail_seen`); `write_sendung(layout.source_dir("mail"), …)`;
  then move the mail to `processed_folder` (UID MOVE or COPY+STORE+EXPUNGE); mails
  without documents → `no_attachment_folder`. Never deletes mail. A fetcher may get its
  `config=` directly (one per mailbox, see `ChannelManager`); with `config.owner` the batch context
  gets owner and endpoint `Postfach „<name>“`.
- `probe_connection(config, *, connection_factory=None) -> ImapProbe(ok, message, messages)` –
  log in, `SELECT` the folder read-only, log out; never raises, German message, changes nothing.
- `SmtpReceiver(services)` (single household) or `SmtpReceiver(config=, resolver=,
  multi_household=)`: aiosmtpd `Controller` on `smtp.host:smtp.port`; peer IP must be in
  `allowed_networks` (empty ⇒ reject all); size limit; optional AUTH (PLAIN/LOGIN) when
  user/password set; never relays. Recipients are resolved to a household: `resolve(address)
  -> Services | None` (single household: only `smtp.recipient`); unknown recipients are
  refused (550; in household mode with a German text). A batch is written to the `quellen/smtp`
  drop zone of the resolved household. With several households there is one global receiver:
  `receiver_for_registry(registry, config)` routes through `registry_resolver` – addresses from
  `smtp_recipients` of `households.yaml`, plus the global `smtp.recipient` for the household
  living in the data root. The registry starts and stops it. When the household's services have a
  channel manager, it decides (`channels.smtp_route`, also for the single-household receiver), so
  a household with `channels.yaml` accepts exactly the addresses listed there.
  `store(..., recipient=)` marks the owner of a per-person address (`channels.mark_smtp`).

### importers/paperless.py – `class PaperlessImporter(services)`
- Reads a `document_exporter` directory (`manifest.json`, or split manifests): models
  `documents.document/correspondent/documenttype/tag/storagepath/note/customfield/
  customfieldinstance`, `auth.user`; file keys `__exported_file_name__`,
  `__exported_archive_name__`.
- `load(export_dir) -> PaperlessExport`; `plan(export, *, storage_paths=None,
  owners=None, include_deleted=False, overrides=None, only_planned=False) ->
  list[PlannedDocument]`; `run(plan, *, mode="inbox"|"archive", dry_run=False, user=None,
  progress=None) -> ImportReport`; `import_export(export_dir, ...)` = load + plan + run.
  Documents whose origin hash is in the trash (`TrashEntry.meta["origin_hash"]`) are skipped
  ("im Papierkorb (gelöscht)") unless `include_deleted`. `overrides` come from an import plan
  file (`load_import_plan(path) -> dict[int, PlanOverride]`: reviewed folder, title, date,
  sender, tags per Paperless id). Unsupported file types are skipped and listed for manual
  handling. `ImportEntry` has `processing`, `ocr`, `manual`, `source_file`; `ImportReport` has
  `manual_entries`, `with_ocr`. Public helpers: `inspect_pdf(path) -> PdfFacts`,
  `map_folder(...)`, `cached_mapping(path)`, `recompute_import_suggestion(context, config_dir)`.
- Inbox mode: `InboxStore.create(source="paperless", files=[archive version or
  original], move=False, origin_hash=<sha256 of the original>, context={"paperless": {...},
  "import_suggestion": Suggestion.to_dict(), "title": ...}, options={"skip_ocr": <every page of the chosen file has text>,
  "skip_blank_removal": True})` and enqueue `process`. File choice: the original only if every page
  has text, else the archive version, else the original with OCR; encrypted originals use the
  archive version if present; signed originals with text are kept byte-identical. Documents whose origin hash is
  already known (index or inbox) are skipped.
- Folder mapping from `config/paperless-import.yaml` (created with defaults): document
  type → folder template (`{correspondent}`, `{document_type}`, `{year}`), default
  template, titles cleaned of leading date/correspondent prefixes.

### web (FastAPI)
Owned by two modules sets; the boundary:

**web-core**: `web/app.py`, `web/security.py`, `web/tokens.py`, `web/auth.py`, `web/routes_api.py`.
**web-ui**: `web/templating.py`, `web/routes_ui.py`, `web/routes_share.py`, `web/views.py`, `templates/**`, `static/**`.

- `create_app(settings=None, *, services=None, registry=None) -> FastAPI` (`web/app.py`):
  `app.state.registry` (a `households.Registry`, or `SingleRegistry` around a ready-made
  `services` – used by tests and when embedded), `app.state.services` (the container of a single
  household; **None with several households**, use `get_services(request)`), `app.state.settings`.
  All containers are created at startup so configuration errors show immediately; the lifespan
  calls `registry.start()` / `stop()` when `settings.start_background`. `SessionMiddleware`
  (cookie `docushelf_session`, `same_site="lax"`, `https_only=settings.cookies_secure`,
  `max_age=settings.session_max_age`); security headers (CSP `default-src 'self'; img-src 'self'
  data: blob:; frame-src 'self'; object-src 'self'; manifest-src 'self'; worker-src 'self';
  frame-ancestors 'self'; base-uri 'none'; form-action 'self'` – the OIDC issuer is added to
  `form-action` –, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`,
  `X-Frame-Options: SAMEORIGIN`); a body size limit from `max_upload_mb`; `/static` from
  `docushelf/static`; routers: auth, share, ui, api (`/api/v1`); `GET /manifest.webmanifest` and
  `GET /sw.js` (no auth, see `web/routes_share.py`); `GET /healthz` (no auth; 200 or 503
  `{"status", "checks"}`; with several households every household is checked and a failure
  names its id, nothing else is revealed); exception handlers (HTML via
  `templating.render(request, "error.html", {...})` or JSON for `/api/`).
- `web/security.py`: `@dataclass(frozen=True) User(username, name, email, groups, method)`;
  `current_user(request) -> User | None` (session or `Authorization: Bearer`); dependency
  `require_user` (HTML → redirect to `/login?next=…` via exception `LoginRequired`; API → 401
  JSON; a user without household → `NoHousehold`) and `require_session_user` (browser session
  only, bearer tokens get 403); `csrf_token(request) -> str`;
  dependency `verify_csrf` (unsafe methods with session auth: header `X-CSRF-Token` or
  form field `csrf_token`; bearer requests exempt); `hash_password` / `verify_password`
  (scrypt, format `scrypt$n$r$p$salt_b64$hash_b64`); login rate limiter. Sessions of the local
  account carry `cred`, a keyed fingerprint of the configured password hash (key
  `app.state.session_secret`); changing the password ends them.
- Household resolution (`web/security.py`): `get_registry(request)`, `multi_household(request)`,
  `get_services(request) -> Services` – the container of the request's household (with a single
  household always `app.state.services`); `current_household(request) -> Household | None`:
  a bearer token belongs to the household whose `tokens.yaml` holds it; a browser session uses
  `session["household"]` while the user still belongs to it, otherwise the user's first household
  (remembered in the session); `user_households(request, user)`, `switchable_households(request)`
  (≥ 2 only), `switch_household(request, household_id) -> Household | None`,
  `is_household_admin(request, user)` (household mode, browser sessions only, `Registry.is_admin`),
  `is_channel_admin(request, user)` (browser sessions only; household mode: `is_household_admin`;
  single household: the local account, `admin_users`, members of `admin_groups`),
  `household_key(request) -> str | None`. `class NoHousehold(HTTPException)` (403, German page
  "Kein Haushalt freigegeben", template `no_household.html`). Every route gets its data only
  through `get_services(request)`; an id from another household is a 404. State kept per
  household (`routes_api._per_household`): the `ThumbnailCache` (`layout.cache`) and the
  background jobs of the admin endpoints.
- `oidc_login_allowed(request, user) -> bool` – login gate for SSO accounts: with a single
  household `allowed_users` / `allowed_groups`; with several the account must belong to at
  least one household (`Registry.for_user`). The local emergency account may use every household.
- `web/tokens.py`: `TokenStore(path)` – YAML list `{id, name, user, hash (sha256),
  prefix, created_at}` plus an optional owner snapshot `{method ("oidc"|"local"), email,
  groups}`; `create(name, user, *, owner=None) -> tuple[str, dict]`, `list()`, `get(id)`,
  `revoke(id)`, `verify(token) -> dict | None` (constant-time). There is one store per
  household (`config/tokens.yaml`); a token only works for its own household. With a snapshot
  a bearer token is refused (401) as soon as its owner would no longer be allowed in
  (`security.token_owner`).
- `web/auth.py` (router): `GET /login`, `GET /login/oidc`, `GET /auth/callback`,
  `POST /login/local`, `POST /logout`. The SSO callback checks `oidc_login_allowed`. OIDC with Authlib (discovery from
  `{issuer}/.well-known/openid-configuration`, PKCE S256, state, nonce). Username =
  `preferred_username` or `email`. Access: `allowed_users` (username or email,
  case-insensitive) or `allowed_groups` (claim `groups`); both empty ⇒ only if
  `allow_all_oidc_users`. Logout redirects to the provider's `end_session_endpoint`
  (with `id_token_hint`, `post_logout_redirect_uri`) when available.
  Renders `login.html` with context `oidc_enabled, oidc_label, local_login_enabled,
  error, next, logged_out`.
- `web/templating.py`: `render(request, name, context=None, *, status_code=200) -> HTMLResponse`
  adding `user`, `csrf_token`, `app_version`, `inbox_counts`, `auto_review_count`, `nav`,
  `household`, `households` (current household and, with ≥ 2, the ones to switch to) to the context;
  Jinja filters `date_de`, `filesize`, `reltime`.
- API `/api/v1` (JSON; session or bearer token; CSRF header for session writes):
  `GET status`; `POST upload` (multipart `files`, `combine`); `GET inbox?state=`;
  `GET inbox/{id}`; `POST inbox/{id}/file`; `POST inbox/file-batch {"items": [...]}`
  (top suggestion each); `POST inbox/{id}/retry`; `POST inbox/{id}/refresh`;
  `DELETE inbox/{id}`; `GET inbox/{id}/document`; `GET inbox/{id}/original/{n}`;
  `GET inbox/{id}/pages/{n}.png?w=`; `GET search` (`q, folder, from, to, tag, sender,
  limit, offset`); `GET suggest?prefix=`; `GET|PATCH|DELETE documents/{id}`;
  `GET documents/{id}/file`; `GET documents/{id}/pages/{n}.png?w=`;
  `GET|POST|PATCH|DELETE folders`; `GET trash`; `POST trash/{id}/restore`;
  `DELETE trash/{id}`; `GET|PUT rules`; `POST admin/reconcile`; `POST admin/rebuild`;
  `GET|POST tokens`, `DELETE tokens/{id}`; `GET audit?limit=`; `GET auto?days=&uncertain=`
  (automatic filings, newest first, with confidence, signals, reasons, `current`, `review`);
  `POST auto/{doc_id}/ok` (confirm, audit `auto_review`).
  `DELETE trash/{id}` and the other token/purge actions need a browser session, not a token.
  `inbox/file-batch` items may carry `expect` (fingerprint of the suggestion the user saw;
  stale → listed in `failed`) and `accept_duplicate`. Page images of files that cannot be
  rendered (password, damaged) are answered with a placeholder SVG (200); a missing page is 404.
- UI routes (German paths, server-rendered, progressive enhancement):
  `/` → `/eingang`; `/eingang`, `/eingang/{id}`, `/eingang/fehler`, `/archiv/{path}`,
  `/dokument/{id}`, `/suche`, `/papierkorb`, `/regeln`, `/einstellungen` plus the POST
  actions for them (each POST uses `verify_csrf` and `require_user`, then redirects).
  Also: `/eingang/automatisch` (automatic filings of the last 60 days, "Passt" =
  `POST /eingang/automatisch/{id}/passt`), `POST /eingang/sammelablage` (checkbox values
  `id[:fingerprint[:dup]]`), `POST /einstellungen/sprachmodell/testen` (runs `llm_probe`),
  `POST /haushalt` (form field `id`; `switch_household`, then redirect to `/eingang`; the header
  shows a switcher only for users with ≥ 2 households),
  `POST /einstellungen/haushalte/{id}/mitglieder` and `…/mitglieder/entfernen` (form field `user`;
  browser login + CSRF; 404 without households, 403 unless `is_household_admin`; calls
  `Registry.change_members`, audits `household_members` with `added`/`removed` in the edited
  household's log; the settings section `#haushalte` is only rendered for admins),
  `GET|POST /teilen`.
  Channel settings (`web/routes_channels.py`, included into the UI router):
  `GET /einstellungen/kanaele` and the POST actions below it – `imap` (create/edit; field
  `aktion=testen` runs `probe_connection` and saves nothing), `imap/{name}/schalten|entfernen`,
  `smtp/schalten`, `smtp/adressen`, `smtp/adressen/entfernen`, `scanner/{kind}/schalten`,
  `scanner/{kind}/endpunkte`, `scanner/{kind}/endpunkte/schalten|entfernen`, `watch`,
  `watch/{name}/schalten|entfernen`. All need a browser login (`require_session_user`), CSRF and
  `is_channel_admin` (else 403 page); toggles carry the target state in the field `an`. Changes go
  through `services.channels.update`; input errors re-render the page (400) with the typed values
  (never the password); every change writes the audit action `channels_change` with a German
  `summary`. Owners must be among the household's `users` when it lists any. A watch folder may
  not overlap the data directories of any household; an SMTP address of another household is
  refused. Template helpers registered by the module: global `channels_admin(request)`, filter
  `origin_label`; templates `channels.html`, `_channel_macros.html`, static `channels.css`,
  `channels.js`.
  The archive page has two views, tiles and list, chosen with `?ansicht=kacheln|liste`
  and remembered in the cookie `ds_ansicht` (read server-side, works without JavaScript).
- `web/routes_share.py` – phone sharing. `GET /manifest.webmanifest` (installable app with a
  `share_target` posting to `/teilen`), `GET /sw.js` (service worker, scope `/`: keeps shared files
  in the Cache Storage `docushelf-teilen` and redirects to `GET /teilen`; `static/share.js` uploads
  them via `/api/v1/upload`). `POST /teilen` without the worker is stored like an upload if the user
  is logged in and `Sec-Fetch-Site` is `none` or `same-origin` (this replaces the CSRF token),
  otherwise it redirects to the login. The iOS shortcut is generated by
  `contrib/ios/build_shortcut.py` (standard library only; the shortcut posts to `/api/v1/upload`
  with a household's bearer token). Details: `docs/sharing.md`.
  `GET /kurzbefehl` (login): `<data>/config/shortcuts/DocuShelf senden.shortcut` as
  `application/octet-stream` attachment, else a 303 to `settings.ios_shortcut_url`, else a German
  404 page; `shortcut_source(settings)` → `"file" | "url" | None` (Jinja global
  `ios_shortcut(request)` hides the button). `POST /kurzbefehl/token` (browser session + CSRF)
  creates a token `iPhone-Kurzbefehl` for the user (owner snapshot like every browser token),
  keeps it in `views.one_time` under the session key `_new_token` with `phone: True` and
  redirects to `/einstellungen#handy`, where `_share_help.html` shows it once.

### cli.py
`docushelf [--data-dir PATH] [--household ID | --haushalt ID] [-v] COMMAND`:
`serve [--host] [--port] [--access-log] | rebuild [--force] | reconcile [--verify] [--force] |
import-paperless EXPORT_DIR [--storage-path NAME]... [--owner NAME]... [--mode inbox|archive]
[--include-deleted] [--plan FILE] [--only-planned] [--dry-run] [-q] [--force] |
hash-password [--stdin] [--env] | token create NAME [--user U] | token list | token revoke ID |
healthcheck [--url] | status [--json] | households [--json] (alias `haushalte`) | process ITEM_ID
[--skip-ocr] [--skip-blank-removal]`. `--household` / `--haushalt` is accepted before and after
the command name.
- Households: commands that act on data (`rebuild`, `reconcile`, `import-paperless`, `token`,
  `status`, `process`) work on one household. Without `households.yaml` or with a single household
  it is chosen automatically; with several the option is required (German error listing the ids).
  `serve`, `healthcheck`, `households` and `hash-password` need none; `serve` validates all
  households (data directories, see `check_data_dirs`) and serves all of them.
- `serve` runs uvicorn with `proxy_headers=True`, `forwarded_allow_ips=settings.trusted_proxies`
  and holds the global advisory lock `index/server.lock` of the data root; `rebuild`, `reconcile`
  and `import-paperless --mode archive` refuse to run beside a running server unless `--force`,
  `process` hands the item to the running server.
- `status` shows German labels for the job kinds (including `auto_file`).
