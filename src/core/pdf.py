"""Local PDF text extraction with pypdfium2.

pdfium is not thread-safe, so extractions are serialised; callers run ``extract_pdf``
in a worker thread.
"""

from __future__ import annotations

import threading

from .errors import UNSUPPORTED_CONTENT, Fail

MAX_TEXT_CHARS = 8 * 1024 * 1024
_LOCK = threading.Lock()


def extract_pdf(data: bytes) -> str:
    """Text of every page, pages separated by blank lines."""
    import pypdfium2 as pdfium

    with _LOCK:
        try:
            pdf = pdfium.PdfDocument(data)
        except pdfium.PdfiumError:
            raise Fail(UNSUPPORTED_CONTENT, "PDF is damaged or password-protected") from None
        try:
            pages, size = [], 0
            for index in range(len(pdf)):
                page = pdf[index]
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_range().replace("\r\n", "\n").replace("\r", "\n")
                    text = text.replace("￾", "-").replace("\x00", "")  # U+FFFE: joined line-end hyphen
                finally:
                    textpage.close()
                    page.close()
                text = "\n".join(line.rstrip() for line in text.split("\n")).strip()
                if text:
                    pages.append(text)
                    size += len(text)
                if size > MAX_TEXT_CHARS:
                    break
        finally:
            pdf.close()
    if not pages:
        raise Fail(UNSUPPORTED_CONTENT, "PDF has no extractable text (scanned images?)")
    return "\n\n".join(pages)
