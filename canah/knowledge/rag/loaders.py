"""
Turning a corpus file into text.

Plain-text formats are read as-is. PDFs get real extraction, because the
corpus this system actually wants — vendor manuals, standards, published
papers on QoT and margin engineering — arrives as PDF and nothing else.

Two things matter for retrieval quality beyond just getting characters out:

  - **Running heads and page numbers** repeat on every page. Left in, they end
    up in every chunk, and a search for a paper's title matches all 40 of its
    pages equally. They're detected by repetition and dropped.
  - **Page provenance.** A retrieved passage is much more useful when it can
    say "p.417 of the Springer handbook" than "somewhere in a 1182-page book",
    so page numbers are carried through to the chunk text.
"""
import logging
import os
import re
from collections import Counter
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("rag.loaders")

PLAIN_EXTENSIONS = {".md", ".txt", ".yang", ".json", ".rst", ".csv", ".log", ".xml", ".yaml", ".yml"}
PDF_EXTENSIONS = {".pdf"}
SUPPORTED_EXTENSIONS = PLAIN_EXTENSIONS | PDF_EXTENSIONS

# A line repeated on at least this share of pages is furniture, not content.
_FURNITURE_RATIO = 0.55
# Only short lines are considered furniture; a repeated paragraph is content.
_FURNITURE_MAX_CHARS = 90


class UnsupportedFile(Exception):
    pass


class EmptyExtraction(Exception):
    """The file parsed but yielded no usable text — typically a scanned PDF."""


def _pdf_backend():
    """Prefer PyMuPDF; fall back to pypdf. Neither means no PDF support."""
    try:
        import fitz  # PyMuPDF
        return "fitz", fitz
    except ImportError:
        pass
    try:
        import pypdf
        return "pypdf", pypdf
    except ImportError:
        pass
    return None, None


def _extract_pages(path: str) -> List[str]:
    backend, mod = _pdf_backend()
    if backend is None:
        raise UnsupportedFile(
            "no PDF library installed — `pip install pymupdf` (or pypdf)"
        )

    if backend == "fitz":
        doc = mod.open(path)
        try:
            # "text" preserves reading order well enough for two-column papers,
            # which is most of this corpus.
            return [page.get_text("text") or "" for page in doc]
        finally:
            doc.close()

    reader = mod.PdfReader(path)
    return [(page.extract_text() or "") for page in reader.pages]


def _find_furniture(pages: List[str]) -> set:
    """Lines that repeat across most pages — running heads, footers, page numbers."""
    if len(pages) < 4:
        return set()

    counts: Counter = Counter()
    for page in pages:
        lines = [l.strip() for l in page.splitlines() if l.strip()]
        # Only the top and bottom of a page carry furniture.
        for line in lines[:3] + lines[-3:]:
            if len(line) <= _FURNITURE_MAX_CHARS:
                counts[line] += 1

    threshold = max(3, int(len(pages) * _FURNITURE_RATIO))
    furniture = {line for line, n in counts.items() if n >= threshold}

    # A bare number on its own line is a page number wherever it appears.
    return furniture


def _clean_page(text: str, furniture: set) -> str:
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append("")
            continue
        if stripped in furniture:
            continue
        if re.fullmatch(r"[ivxlcdm]{1,7}|\d{1,4}", stripped, re.IGNORECASE):
            continue  # standalone page number
        out.append(line)

    text = "\n".join(out)
    # Rejoin words broken across a line by hyphenation.
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    # Collapse the blank-line runs that extraction leaves behind.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def load_pdf(path: str) -> Tuple[str, Dict]:
    """Extract a PDF into text with page markers.

    Page markers survive into chunks, so a retrieved passage can cite its page.
    """
    pages = _extract_pages(path)
    if not pages:
        raise EmptyExtraction("PDF has no pages")

    furniture = _find_furniture(pages)
    cleaned: List[str] = []
    kept_pages = 0
    for i, raw in enumerate(pages, start=1):
        text = _clean_page(raw, furniture)
        if not text:
            continue
        kept_pages += 1
        cleaned.append(f"[p.{i}]\n{text}")

    if not cleaned:
        raise EmptyExtraction(
            "no text layer — this looks like a scanned PDF and would need OCR"
        )

    body = "\n\n".join(cleaned)
    meta = {
        "pages": len(pages),
        "pages_with_text": kept_pages,
        "furniture_lines_removed": len(furniture),
        "chars": len(body),
    }
    if furniture:
        logger.debug(f"{os.path.basename(path)}: dropped {len(furniture)} repeated line(s)")
    return body, meta


def load_plain(path: str) -> Tuple[str, Dict]:
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    return text, {"chars": len(text)}


def load_file(path: str) -> Tuple[str, Dict]:
    """Read any supported corpus file into text plus a little metadata."""
    ext = os.path.splitext(path)[1].lower()
    if ext in PDF_EXTENSIONS:
        return load_pdf(path)
    if ext in PLAIN_EXTENSIONS:
        return load_plain(path)
    raise UnsupportedFile(f"'{ext or 'no extension'}' is not a supported file type")


def is_supported(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in SUPPORTED_EXTENSIONS
