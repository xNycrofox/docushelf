# Contributing to DocuShelf

Thanks for your interest! Bug reports, ideas and pull requests are welcome.

## Development setup

```sh
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

Without Ghostscript/Tesseract installed, OCR falls back to storing the original PDF; the
corresponding tests adapt automatically. The CI runs the full suite with the OCR toolchain.

A local instance with test data: see `deploy/compose.dev.yaml` and `DEPLOY.md`.

## Guidelines

- **The folder tree is the source of truth.** Every feature must keep the archive usable as
  plain folders and PDF files without DocuShelf (see `docs/concept.md`).
- Never lose a document: file operations are atomic, every move is logged, deletions go to the
  trash.
- The user interface is German (informal "du"); code, comments and commit messages are English.
- No external services by default. Anything that sends document content elsewhere must be
  opt-in and clearly documented.
- Add tests for every change (`tests/`). Keep the suite green: `python -m pytest -q`.
- Keep pull requests focused; describe the motivation and how you tested it.

## Reporting bugs

Open an issue with the steps to reproduce, the expected and the actual behaviour, the DocuShelf
version (`docushelf --version`) and relevant log lines (remove personal data first).

Security problems: see [SECURITY.md](SECURITY.md).

## License

By contributing you agree that your contributions are licensed under the
[GNU Affero General Public License v3.0](LICENSE).
