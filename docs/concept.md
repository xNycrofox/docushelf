# Concept: a folder-first document archive

This document describes the idea, the principles and the requirements behind DocuShelf. How they
are implemented is described in [`ARCHITECTURE.md`](../ARCHITECTURE.md) and
[`docs/internals.md`](internals.md); installation and operation in [`README.md`](../README.md) and
[`DEPLOY.md`](../DEPLOY.md).

## 1. Motivation

Classic document management systems keep documents inside a database: order exists only through
tags and filters, and the media folder must not be touched by hand. That does not fit how many
people think about paper.

DocuShelf is an archive in which **an ordinary folder tree is the single source of truth**. The
application is only a helper on top: it accepts documents from several sources, runs OCR, suggests
where to file them, files them and makes everything searchable. If the application is switched
off, a clean, readable folder tree with PDF files remains.

Target audience: households and families, not enterprises.

## 2. Core principles

1. **Folders are the truth.** Every filed document is a normal file in a normal folder. There are
   no "virtual folders" in a database.
2. **The index is disposable.** The database (SQLite) is only a search cache and job state. It can
   be rebuilt completely from the file system at any time (`docushelf rebuild`). Nothing that cannot
   be reconstructed from the files may live only in the database.
3. **Changes from outside are normal.** Users move, rename or delete files with their file manager
   over SMB. The application detects that and updates its index – not the other way round.
4. **Filing is under the user's control.** By default everything lands in the inbox with
   suggestions first. Automatic filing is opt-in, configurable per source and confidence level,
   always logged, and every automatic decision can be reviewed and undone.
5. **Data stays local by default.** Document contents are only sent to a language model if one is
   configured. The default is a local model (e.g. Ollama on the same server); external APIs are
   possible but must be enabled explicitly.
6. **Originals are never lost.** Every processing step is non-destructive until filing succeeded.
   Documents that fail end up in an error area with a log, never in nowhere.
7. **No dependency on the application for reading.** File and folder names are human-readable.

## 3. Directory layout (inside the data volume)

```
/data
├── archiv/                 # THE truth. Free folder tree maintained by the users.
├── eingang/
│   ├── quellen/<source>/   # raw drop zones per channel (upload, smb, ftp, mail, smtp, watch)
│   ├── bereit/             # processed (OCR etc.), waiting for confirmation
│   └── fehler/             # processing failed, with a log per document
├── papierkorb/             # documents deleted in the app (retention configurable)
├── config/                 # rules and settings as readable files (YAML)
└── index/                  # SQLite (search index + job queue). May be deleted → rebuild.
```

- `archiv/` can be a separate mount.
- The application stores **no** helper files inside `archiv/` (no sidecars, no hidden folders).
- With several households each household has its own copy of this layout.

## 4. Metadata without a database

| Information        | Where it lives                                               |
|--------------------|--------------------------------------------------------------|
| Location/category  | folder path                                                  |
| Document date      | file name prefix `YYYY-MM-DD` + XMP                          |
| Title              | file name + XMP `dc:title`                                   |
| Tags (optional)    | XMP `dc:subject` / PDF keywords                              |
| Sender             | XMP (own namespace)                                          |
| Document id        | XMP (own namespace), UUID – survives moving and renaming     |
| Origin hash        | XMP – SHA-256 of the original at intake, for duplicate checks|
| Full text          | OCR text layer in the PDF (PDF/A)                            |

File name scheme: `YYYY-MM-DD Title.pdf`; on collision a suffix ` (2)`. Changing the title in the
app renames the file. Foreign files placed directly into `archiv/` are indexed too (title and
date derived from the file name); XMP is only written when metadata is edited in the app.

Tags are secondary: folders are the primary order.

## 5. Reconciliation with the file system

- **Live:** a file system watcher on `archiv/` with debouncing (SMB clients write in steps).
- **Periodic:** full reconciliation at start and regularly, because watchers miss events.
- **Identity:** the document id from XMP, so a move is recognised as a move; fallback: content hash.
- **Empty folders** are valid filing targets.
- **Conflicts** (file locked or changed concurrently): retry, never overwrite data.
- `rebuild` is idempotent.

## 6. Processing pipeline

All intake channels only write into `eingang/quellen/<source>/`. From there one shared pipeline runs:

1. detect complete files (stable size), 2. duplicate check, 3. normalise images to PDF,
4. remove blank pages, 5. OCR (OCRmyPDF/Tesseract, PDF/A, existing text layers are kept),
6. analysis (date, sender, amounts, reference numbers), 7. suggestions → `eingang/bereit/`,
8. confirmation (or automatic filing) → write XMP, rename, move atomically into `archiv/`, index.

Errors move the document and its log to `eingang/fehler/`, from where it can be retried. Jobs are
idempotent and survive a restart.

## 7. Filing suggestions

Staged, every stage optional, each result comes with a reason:

1. **Rules** (deterministic, in `config/`): keywords, sender, IBAN, customer/contract numbers →
   target folder + title template.
2. **Similarity to the existing archive**: the folder tree *is* the training material; the app
   learns from every filing decision without extra maintenance.
3. **Language model (optional)**: gets the OCR text and the list of folders, returns title, date
   and folder. Must work with small CPU models (timeouts, asynchronous); the app works fully
   without it.

New folders are only suggested when no existing folder fits, and clearly marked as new.

## 8. Search

Full text over title, folder path and OCR text (SQLite FTS5), tolerant to partial words and small
typos, aware of German umlauts and compound words; filters for folder subtree, period, tags and
sender; hits show where in the document the term occurs.

## 9. Intake channels

Web upload, watch folders, SMB and FTP (scan-to-folder from a printer), IMAP mailbox, built-in
SMTP receiver (scan-to-mail), sharing from phones (PWA share target on Android, Shortcuts on iOS),
REST API with tokens, and an importer for Paperless-ngx exports. Every channel is optional and only
writes into its drop zone; none has access to `archiv/`.

## 10. Access and security

- Login via OpenID Connect; a local account only as an emergency login.
- Households: several families can share one installation; each household has its own archive,
  inbox, rules, tokens and channels, and nobody sees another household's documents.
- Technical accounts (scanner, mailbox) are separate from personal accounts; secrets come from
  environment variables or secret files.
- An audit log records who filed, moved or deleted what.

## 11. Out of scope

- No virtual folder system, no database as storage.
- No workflows/approval processes, no versioning, no digital signatures.
- No office editor, no collaborative editing.
