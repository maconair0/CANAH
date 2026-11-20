"""
Chunking (ARCHITECTURE_V2 §5.2).

Plain text gets a recursive splitter. `.yang` files get a structure-aware one:
a YANG statement block split down the middle is worse than useless to the
Generator — it produces a fragment that looks like a valid leaf definition but
has lost its container, its type, or its config/state context.
"""
import os
import re
from typing import Any, Dict, List, Optional

from . import quality

# mxbai-embed-large tops out at 512 tokens. Prose runs ~4 chars/token, but the
# technical PDFs this corpus holds — equations, citation markers, tables — run
# closer to 2, so the old 1600-char cap still overflowed. These values target
# ~512 tokens at the *dense* ratio; RagStore.embed shrinks anything that still
# doesn't fit rather than failing the ingest.
DEFAULT_CHUNK_CHARS = 900
DEFAULT_OVERLAP = 120
HARD_MAX_CHARS = 1100

# A top-level-ish YANG statement that's worth keeping whole.
_YANG_BLOCK_START = re.compile(
    r"^\s*(container|list|leaf-list|leaf|grouping|typedef|identity|augment|rpc|notification)\s+\S+",
    re.MULTILINE,
)


def chunk_text(text: str, chunk_chars: int = DEFAULT_CHUNK_CHARS,
               overlap: int = DEFAULT_OVERLAP) -> List[str]:
    """Recursive split on progressively weaker boundaries."""
    text = text.strip()
    if len(text) <= chunk_chars:
        return [text] if text else []

    for sep in ("\n\n\n", "\n\n", "\n", ". ", " "):
        if sep not in text:
            continue
        parts = text.split(sep)
        chunks: List[str] = []
        current = ""
        for part in parts:
            candidate = f"{current}{sep}{part}" if current else part
            if len(candidate) > chunk_chars and current:
                chunks.append(current)
                # Carry a tail of the previous chunk so a boundary doesn't
                # sever a sentence from the context that explains it.
                current = (current[-overlap:] + sep + part) if overlap else part
            else:
                current = candidate
        if current:
            chunks.append(current)
        if all(len(c) <= chunk_chars * 1.5 for c in chunks):
            return [c.strip() for c in chunks if c.strip()]

    return [text[i:i + chunk_chars] for i in range(0, len(text), chunk_chars - overlap)]


def chunk_yang(text: str, chunk_chars: int = DEFAULT_CHUNK_CHARS) -> List[str]:
    """Split a YANG module on statement boundaries, keeping blocks intact.

    Each chunk is prefixed with the module name so a retrieved fragment still
    says which model it came from — without that, a `leaf enabled` chunk is
    ambiguous across half a dozen modules.
    """
    module_match = re.search(r"^\s*module\s+(\S+)", text, re.MULTILINE)
    module = module_match.group(1).strip("{ ") if module_match else "unknown-module"

    starts = [m.start() for m in _YANG_BLOCK_START.finditer(text)]
    if not starts:
        return [f"[module {module}]\n{c}" for c in chunk_text(text, chunk_chars)]

    # Preamble (namespace, prefix, imports) is genuinely useful context.
    blocks: List[str] = []
    if starts[0] > 0:
        preamble = text[: starts[0]].strip()
        if preamble:
            blocks.append(preamble)

    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        block = text[start:end].strip()
        if block:
            blocks.append(block)

    # Merge small adjacent blocks up to the target size rather than emitting
    # one chunk per two-line leaf.
    merged: List[str] = []
    current = ""
    for block in blocks:
        if len(block) > chunk_chars:
            if current:
                merged.append(current)
                current = ""
            merged.extend(chunk_text(block, chunk_chars))
            continue
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > chunk_chars and current:
            merged.append(current)
            current = block
        else:
            current = candidate
    if current:
        merged.append(current)

    return [f"[module {module}]\n{c}" for c in merged]


def enforce_max(pieces: List[str], hard_max: int = HARD_MAX_CHARS) -> List[str]:
    """Guarantee no chunk exceeds the embedder's limit.

    The structure-aware paths above deliberately keep blocks whole, which means
    they can emit something too large. This is the backstop: a block that can't
    be kept whole is split rather than allowed to fail the embedding call.
    """
    out: List[str] = []
    for piece in pieces:
        if len(piece) <= hard_max:
            out.append(piece)
            continue
        for i in range(0, len(piece), hard_max):
            out.append(piece[i:i + hard_max])
    return out


_PAGE_MARKER_RE = re.compile(r"^\[p\.(\d+)\]$", re.MULTILINE)


def chunk_pdf(text: str, source: str = "",
              chunk_chars: int = DEFAULT_CHUNK_CHARS) -> List[Dict]:
    """Chunk extracted PDF text, keeping page provenance on every chunk.

    loaders.load_pdf marks pages as `[p.N]`. Those markers are pulled out here
    and re-attached as a header, so a chunk that lands in the model's context
    still says where it came from — a passage the model can cite beats a
    passage it can only paraphrase.

    The page numbers are also returned as fields rather than only baked into
    the text, because `quality.classify` needs to know where in the document a
    chunk sits and re-parsing the header it just wrote would be silly.

    A chunk spanning a page break reports the range it covers.
    """
    segments: List[tuple] = []   # (page_number, page_text)
    current_page = 1
    buffer: List[str] = []

    for line in text.splitlines():
        m = _PAGE_MARKER_RE.match(line.strip())
        if m:
            if buffer:
                segments.append((current_page, "\n".join(buffer).strip()))
                buffer = []
            current_page = int(m.group(1))
            continue
        buffer.append(line)
    if buffer:
        segments.append((current_page, "\n".join(buffer).strip()))

    if not segments:
        return [{"text": t} for t in chunk_text(text, chunk_chars)]

    label = f"{source} " if source else ""
    out: List[Dict] = []
    carry: List[str] = []
    carry_start: Optional[int] = None

    def flush(end_page: int):
        if not carry:
            return
        body = "\n".join(carry).strip()
        if not body:
            return
        span = (f"p.{carry_start}" if carry_start == end_page
                else f"pp.{carry_start}-{end_page}")
        out.append({
            "text": f"[{label}{span}]\n{body}",
            "page_start": carry_start,
            "page_end": end_page,
        })

    for page_no, page_text in segments:
        if not page_text:
            continue
        if carry_start is None:
            carry_start = page_no

        # A page bigger than one chunk is split on its own; anything already
        # carried is flushed first so page ranges stay truthful.
        if len(page_text) > chunk_chars:
            flush(page_no - 1 if carry_start != page_no else page_no)
            carry, carry_start = [], None
            for piece in chunk_text(page_text, chunk_chars):
                out.append({
                    "text": f"[{label}p.{page_no}]\n{piece.strip()}",
                    "page_start": page_no,
                    "page_end": page_no,
                })
            continue

        pending = "\n".join(carry + [page_text])
        if len(pending) > chunk_chars and carry:
            flush(page_no - 1)
            carry, carry_start = [page_text], page_no
        else:
            carry.append(page_text)

    if carry:
        flush(segments[-1][0])
    return out


# Parent-child chunking (UPGRADE_PLAN Phase 5).
#
# Retrieval and generation want different things from a chunk. Retrieval wants
# it small — a 900-character passage covering three ideas matches a query about
# any of them only weakly, because the embedding is an average. Generation
# wants it large, because a 300-character fragment often cuts off the sentence
# that made it relevant.
#
# So the two are split: children are what gets embedded and ranked, parents are
# what gets read. A hit on a child returns its parent.
CHILD_CHUNK_CHARS = int(os.getenv("RAG_CHILD_CHUNK_CHARS", "320"))
CHILD_OVERLAP = int(os.getenv("RAG_CHILD_OVERLAP", "60"))


def make_children(parents: List[Dict[str, Any]],
                  child_chars: int = CHILD_CHUNK_CHARS,
                  overlap: int = CHILD_OVERLAP) -> List[Dict[str, Any]]:
    """Split each parent chunk into smaller retrieval units.

    Every child keeps its parent's `chunk_index` as `parent_index` and inherits
    its classification and page span, so section filtering and citations behave
    exactly as they do without this step. A parent too short to split usefully
    yields a single child that is just the parent — no point indexing a
    fragment of a fragment.
    """
    children: List[Dict[str, Any]] = []
    for parent in parents:
        text = parent.get("text", "")
        parent_index = parent.get("chunk_index")
        if len(text) <= child_chars:
            pieces = [text]
        else:
            pieces = []
            step = max(1, child_chars - overlap)
            for i in range(0, len(text), step):
                piece = text[i:i + child_chars]
                if piece.strip():
                    pieces.append(piece)
                if i + child_chars >= len(text):
                    break
        for j, piece in enumerate(pieces):
            child = dict(parent)
            child["text"] = piece
            child["parent_index"] = parent_index
            child["child_index"] = j
            children.append(child)
    return children


def _as_records(pieces) -> List[Dict]:
    """Accept either plain strings or already-structured chunks."""
    return [p if isinstance(p, dict) else {"text": p} for p in pieces]


def _enforce_max_records(records: List[Dict],
                         hard_max: int = HARD_MAX_CHARS) -> List[Dict]:
    """`enforce_max`, preserving each piece's page fields across a split."""
    out: List[Dict] = []
    for rec in records:
        text = rec["text"]
        if len(text) <= hard_max:
            out.append(rec)
            continue
        for i in range(0, len(text), hard_max):
            part = dict(rec)
            part["text"] = text[i:i + hard_max]
            out.append(part)
    return out


def chunk_file(path: str, text: str,
               file_meta: Optional[Dict] = None) -> List[Dict[str, Any]]:
    """Chunk one file according to its type, labelling each chunk's section.

    `file_meta` is what the loader reported — its `pages` count is what lets
    the position half of the front-matter rule scale between a 60-page paper
    and a 1182-page handbook. Without it only the shape heuristics apply, which
    is a graceful degradation rather than a failure.
    """
    ext = os.path.splitext(path)[1].lower()
    total_pages = (file_meta or {}).get("pages")

    if ext == ".yang":
        records = _as_records(chunk_yang(text))
        kind = "yang"
    elif ext == ".pdf":
        records = chunk_pdf(
            text, source=os.path.splitext(os.path.basename(path))[0][:40]
        )
        kind = "pdf"
    else:
        records = _as_records(chunk_text(text))
        kind = ext.lstrip(".") or "text"

    records = _enforce_max_records(records)

    out: List[Dict[str, Any]] = []
    index = 0
    for rec in records:
        piece = rec["text"]
        if not piece.strip():
            continue
        # Only paginated documents get classified. A YANG module or a note has
        # no front matter to speak of, and a heuristic that fires on one would
        # cost real content for nothing.
        if kind == "pdf":
            section = quality.classify(
                piece, page=rec.get("page_start"), total_pages=total_pages
            )
        else:
            section = "body"
        entry: Dict[str, Any] = {
            "text": piece,
            "chunk_index": index,
            "kind": kind,
            "section_kind": section,
        }
        if rec.get("page_start") is not None:
            entry["page_start"] = rec["page_start"]
            entry["page_end"] = rec.get("page_end", rec["page_start"])
        out.append(entry)
        index += 1
    return out


def section_counts(chunks: List[Dict[str, Any]]) -> Dict[str, int]:
    """How a file's chunks broke down — for the refresh log line."""
    counts: Dict[str, int] = {}
    for c in chunks:
        key = c.get("section_kind", "body")
        counts[key] = counts.get(key, 0) + 1
    return counts
