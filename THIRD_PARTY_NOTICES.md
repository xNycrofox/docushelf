# Third-party notices

DocuShelf ships the following third-party code in its web assets. Python
dependencies are installed from PyPI and keep their own license files.

## PDF.js (pdfjs-dist)

- Project: Mozilla PDF.js, <https://github.com/mozilla/pdf.js>
- Package: `pdfjs-dist` from the npm registry (official prebuilt release)
- License: Apache License 2.0
- Location: `docushelf/static/vendor/pdfjs/`
  - `pdf.min.mjs` – the library (loaded by `docushelf/static/viewer.js`)
  - `pdf.worker.min.mjs` – the parser worker
  - `wasm/` – optional image decoders and their JavaScript fallbacks
  - `LICENSE` – the full license text from the package
- Only the minified build is shipped; the PDF.js viewer application is not.
- The text-layer rules in `docushelf/static/app.css` (section "Text layer of
  PDF.js pages") are adapted from `pdf_viewer.css` of the same package.

Without these files the document viewer shows the page images rendered by the
server instead (no text selection in the preview).

### Updating

Use the official package only, never a CDN copy:

```sh
mkdir -p /tmp/pdfjs && cd /tmp/pdfjs
npm pack pdfjs-dist@5            # downloads pdfjs-dist-<version>.tgz from the npm registry
tar -xzf pdfjs-dist-*.tgz
target=/path/to/docushelf/docushelf/static/vendor/pdfjs
mkdir -p "$target/wasm"
cp package/build/pdf.min.mjs package/build/pdf.worker.min.mjs package/LICENSE "$target/"
cp package/wasm/* "$target/wasm/"   # if the release has a wasm/ folder
```

Then note the version here, run the test suite and open a document in the
browser: the pages must render, text must be selectable, and the browser console
must not report Content-Security-Policy violations.
