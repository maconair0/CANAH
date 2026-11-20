"""
RAG corpus refresh (ARCHITECTURE_V2 §5.2).

The workflow this exists for:

    1. drop a file into the corpus folder (RAG_CORPUS_DIR)
    2. python -m canah.knowledge.rag.refresh
    3. it's embedded and searchable

Incremental by content hash. Re-running after adding one file embeds that one
file — unchanged files are skipped, changed files have their old chunks
replaced, deleted files have their chunks removed. Running it twice in a row
does nothing the second time.

The corpus ships empty on purpose. Nothing is auto-seeded.
"""
import argparse
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from .chunker import chunk_file, make_children, section_counts
from .parents import ParentStore
from .loaders import (
    EmptyExtraction, SUPPORTED_EXTENSIONS, UnsupportedFile, is_supported, load_file,
)
from .store import RagStore, CORPUS_DIR, MANIFEST_PATH

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("rag.refresh")

# One embedding call per chunk means one httpx INFO line per chunk — thousands
# of them for a PDF corpus, burying the actual progress.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("chromadb").setLevel(logging.WARNING)

# Supported types live in loaders.py, which is also what knows how to read
# each one. Keeping the list in one place is why a .pdf dropped in the corpus
# now gets ingested instead of silently ignored.
TEXT_EXTENSIONS = SUPPORTED_EXTENSIONS


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def load_manifest(path: str = MANIFEST_PATH) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"files": {}}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"manifest unreadable ({e}); treating corpus as new")
        return {"files": {}}


def save_manifest(manifest: Dict[str, Any], path: str = MANIFEST_PATH) -> None:
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(manifest, f, indent=2)


def scan_corpus(corpus_dir: str = CORPUS_DIR) -> Tuple[Dict[str, str], List[str]]:
    """({relative path: content hash}, [skipped paths]).

    Returns skips explicitly rather than swallowing them. Silently ignoring a
    file the user deliberately put in the corpus is the worst possible
    behaviour here — they believe it was ingested and it never was.
    """
    found: Dict[str, str] = {}
    skipped: List[str] = []
    if not os.path.isdir(corpus_dir):
        return found, skipped
    for root, _dirs, files in os.walk(corpus_dir):
        for name in sorted(files):
            if name.startswith("."):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, corpus_dir)
            if not is_supported(full):
                skipped.append(rel)
                continue
            found[rel] = sha256_file(full)
    return found, skipped


# Parent-child retrieval. On by default; set RAG_PARENT_CHILD=0 to index whole
# passages as before.
PARENT_CHILD = os.getenv("RAG_PARENT_CHILD", "1") == "1"


def refresh(corpus_dir: str = CORPUS_DIR, collection: str = "corpus",
            force: bool = False) -> Dict[str, Any]:
    store = RagStore(collection=collection)
    parent_store = ParentStore()
    manifest = load_manifest()
    known: Dict[str, Any] = {} if force else manifest.get("files", {})
    present, skipped = scan_corpus(corpus_dir)

    for rel in skipped:
        logger.warning(
            f"SKIPPED {rel} — unsupported type. Supported: "
            f"{' '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    added = [p for p in present if p not in known]
    changed = [p for p in present if p in known and known[p]["hash"] != present[p]]
    removed = [p for p in known if p not in present]
    unchanged = [p for p in present if p in known and known[p]["hash"] == present[p]]

    for rel in removed:
        store.delete_by_source(rel)
        logger.info(f"removed {rel} ({known[rel].get('chunks', '?')} chunks)")

    new_manifest_files = {
        rel: known[rel] for rel in unchanged if rel in known
    }

    total_chunks = 0
    failed: List[Dict[str, str]] = []
    work = added + changed

    def _persist():
        # Written in a finally below too: a large corpus takes minutes, and
        # losing every embedded file because the last one failed is needless.
        manifest["files"] = new_manifest_files
        save_manifest(manifest)

    try:
      for n, rel in enumerate(work, start=1):
          full = os.path.join(corpus_dir, rel)

          try:
              text, file_meta = load_file(full)
          except (UnsupportedFile, EmptyExtraction) as e:
              logger.warning(f"SKIPPED {rel} — {e}")
              failed.append({"file": rel, "reason": str(e)})
              continue
          except Exception as e:
              logger.warning(f"SKIPPED {rel} — could not read: {type(e).__name__}: {e}")
              failed.append({"file": rel, "reason": f"{type(e).__name__}: {e}"})
              continue

          if rel in changed or force:
              # On --force the manifest is being ignored, so the old chunks are
              # still in the index. Ids are stable enough that upsert would
              # mostly overwrite them, but "mostly" is not a guarantee once
              # chunking changes — delete first and mean it.
              store.delete_by_source(rel)

          chunks = chunk_file(full, text, file_meta)
          if not chunks:
              logger.warning(f"{rel} produced no chunks; skipped")
              failed.append({"file": rel, "reason": "no chunks produced"})
              continue

          # Big documents take minutes to embed. Saying so beats a silent hang.
          pages = file_meta.get("pages")
          detail = f", {pages} pages" if pages else ""
          counts = section_counts(chunks)
          # Say how the file broke down. A book that classifies as 90% front
          # matter means the heuristics misfired on it, and that is only
          # visible if the split is printed.
          split = ", ".join(
              f"{n_} {k}" for k, n_ in sorted(counts.items()) if k != "body"
          )
          if split:
              detail += f" [{counts.get('body', 0)} body, {split}]"
          logger.info(
              f"[{n}/{len(work)}] embedding {rel} — {len(chunks)} chunks{detail}"
          )
          def _tick(done: int, of: int, _rel=rel):
              if of > 800:   # only worth narrating for genuinely long documents
                  logger.info(f"      {_rel[:44]}: {done}/{of} chunks")

          # Parent-child (Phase 5): embed small children for retrieval
          # precision, keep the full parent for generation context. Opt-out
          # rather than opt-in, but the parents are recorded either way so a
          # switch does not require re-embedding.
          if PARENT_CHILD:
              parent_store.delete_by_source(rel)
              parent_store.put_many(rel, chunks)
              indexed = make_children(chunks)
              logger.info(f"      {rel[:44]}: {len(chunks)} parents -> "
                          f"{len(indexed)} children")
          else:
              indexed = chunks

          store.add_chunks(rel, indexed, on_progress=_tick)
          total_chunks += len(chunks)
          new_manifest_files[rel] = {
              "hash": present[rel],
              "chunks": len(chunks),
              "sections": section_counts(chunks),
              "ingested_at": datetime.now(timezone.utc).isoformat(),
          }
          logger.info(f"{'updated' if rel in changed else 'added'} {rel}: {len(chunks)} chunks")

    finally:
        _persist()

    summary = {
        "added": added,
        "changed": changed,
        "removed": removed,
        "unchanged": len(unchanged),
        "skipped_unsupported": skipped,
        "failed": failed,
        "chunks_embedded": total_chunks,
        "collection_size": store.count(),
        "corpus_dir": corpus_dir,
    }
    if not (added or changed or removed):
        logger.info(
            f"Nothing to do — {len(unchanged)} file(s) already current, "
            f"{store.count()} chunks in the index"
        )
    else:
        logger.info(
            f"Refresh complete: +{len(added)} ~{len(changed)} -{len(removed)}, "
            f"{store.count()} chunks in the index"
        )
    return summary


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Embed new or changed files from the RAG corpus."
    )
    parser.add_argument("--corpus", default=CORPUS_DIR, help="corpus directory")
    parser.add_argument("--collection", default="corpus", help="Chroma collection")
    parser.add_argument("--force", action="store_true",
                        help="re-embed everything, ignoring the manifest")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.corpus):
        os.makedirs(args.corpus, exist_ok=True)

    summary = refresh(args.corpus, args.collection, args.force)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
