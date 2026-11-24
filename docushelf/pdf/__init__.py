"""PDF helpers (metadata, text, rendering, normalization, OCR)."""

import threading

# PDFium is not thread-safe. Every call into pypdfium2 must hold this lock.
PDFIUM_LOCK = threading.RLock()
