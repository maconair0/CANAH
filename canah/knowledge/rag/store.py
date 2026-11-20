"""
Chroma-backed chunk store (ARCHITECTURE_V2 §5.2).

Embedded, local, no separate service — same stance as the rest of this stack.
Embeddings come from Ollama so nothing leaves the host.

Two collections live here, mechanically identical but semantically distinct
(Principle 3.5): `corpus` is RAG (reference knowledge), `episodes` is ICL
(past resolutions that worked). They must not be merged.
"""
import logging
import os
from typing import Any, Dict, List, Optional

import chromadb
import ollama

logger = logging.getLogger("rag.store")

from .settings import CHROMA_DIR, CORPUS_DIR, MANIFEST_PATH  # noqa: E402
EMBED_MODEL = os.getenv("EMBED_MODEL", "mxbai-embed-large")

# Rows per page when reading the whole collection. Well under SQLite's
# variable ceiling, and large enough that a full corpus read is a handful of
# queries rather than hundreds.
ALL_DOCUMENTS_PAGE = int(os.getenv("RAG_ALL_DOCS_PAGE", "5000"))
# Chroma rejects an upsert above ~5461 records. Well under it, and small enough
# that progress is visible on a long document.
BATCH_SIZE = int(os.getenv("RAG_BATCH_SIZE", "400"))
from .settings import OLLAMA_HOST  # noqa: E402


class RagStore:
    def __init__(self, collection: str = "corpus",
                 chroma_dir: str = CHROMA_DIR,
                 embed_model: str = EMBED_MODEL,
                 ollama_host: str = OLLAMA_HOST):
        os.makedirs(chroma_dir, exist_ok=True)
        self.embed_model = embed_model
        from .settings import make_client
        # The embedder cannot be swapped without reindexing, so it resolves
        # separately from the chat models.
        self.ollama = make_client("embed")
        self.client = chromadb.PersistentClient(path=chroma_dir)
        self.collection = self.client.get_or_create_collection(
            name=collection, metadata={"hnsw:space": "cosine"}
        )

    # ----- embedding -----

    def embed(self, texts: List[str]) -> List[List[float]]:
        """Embed texts, shrinking anything the model refuses.

        The chunker targets a character budget, but characters are a poor proxy
        for tokens: dense academic text — equations, citation markers, tables —
        tokenizes far denser than prose, so a chunk inside the character limit
        can still blow the model's 512-token context.

        Rather than guess a safer ratio and still be wrong sometimes, shrink and
        retry on the error itself. One awkward chunk must not kill an ingest
        that is minutes deep.
        """
        out: List[List[float]] = []
        for text in texts:
            out.append(self._embed_one(text))
        return out

    def _embed_one(self, text: str, attempts: int = 5) -> List[float]:
        body = text
        for attempt in range(attempts):
            try:
                r = self.ollama.embeddings(model=self.embed_model, prompt=body)
                return r["embedding"]
            except Exception as e:
                if "context length" not in str(e).lower() or attempt == attempts - 1:
                    raise
                body = body[: max(200, int(len(body) * 0.6))]
                logger.debug(
                    f"chunk over context; retrying at {len(body)} chars"
                )
        raise RuntimeError("unreachable")

    # ----- writes -----

    def add_chunks(self, source: str, chunks: List[Dict[str, Any]],
                   batch_size: int = BATCH_SIZE,
                   on_progress=None) -> int:
        """Embed and store a file's chunks, in batches.

        Batching is not an optimisation here, it's a correctness requirement:
        Chroma refuses an upsert above ~5.4k records, and a 1,182-page book
        chunks well past that. Writing per batch also means a failure partway
        through a large document keeps everything already embedded, instead of
        discarding several minutes of work.
        """
        total = 0
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start:start + batch_size]
            texts = [c["text"] for c in batch]
            # A child's id has to include its position within the parent, or
            # every child of one parent collides on the same id and only the
            # last survives the upsert.
            ids = [
                (f"{source}::{c['chunk_index']}"
                 if c.get("parent_index") is None
                 else f"{source}::{c['parent_index']}::{c.get('child_index', 0)}")
                for c in batch
            ]
            metadatas = [self._metadata(source, c) for c in batch]
            embeddings = self.embed(texts)
            self.collection.upsert(
                ids=ids, documents=texts, metadatas=metadatas, embeddings=embeddings
            )
            total += len(ids)
            if on_progress:
                on_progress(total, len(chunks))
        return total

    @staticmethod
    def _metadata(source: str, chunk: Dict[str, Any]) -> Dict[str, Any]:
        """Chunk metadata, kept flat and scalar — Chroma accepts nothing else.

        `section_kind` and the page fields are what make retrieval filterable
        after the fact: classifying at ingest but storing only the survivors
        would mean a policy change costs a full re-embed of the corpus.
        """
        meta: Dict[str, Any] = {
            "source": source,
            "chunk_index": chunk["chunk_index"],
            "kind": chunk.get("kind", "text"),
            "section_kind": chunk.get("section_kind", "body"),
        }
        if chunk.get("page_start") is not None:
            meta["page_start"] = int(chunk["page_start"])
            meta["page_end"] = int(chunk.get("page_end", chunk["page_start"]))
        # Present only on children (Phase 5 parent-child chunking). Its absence
        # is what tells retrieval this is a whole passage already.
        if chunk.get("parent_index") is not None:
            meta["parent_index"] = int(chunk["parent_index"])
        return meta

    def delete_by_source(self, source: str) -> None:
        try:
            self.collection.delete(where={"source": source})
        except Exception as e:
            logger.warning(f"could not delete chunks for {source}: {e}")

    # ----- reads -----

    def count(self) -> int:
        return self.collection.count()

    def all_documents(self, where: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Every chunk — BM25 needs the full set, not a similarity slice.

        `where` narrows that set at the store rather than after the fact, so
        the BM25 index is built over the retrievable corpus only. Scoring is
        corpus-relative: leaving front matter in the index and dropping its
        hits afterwards would still skew every IDF in it.
        """
        if self.count() == 0:
            return {"ids": [], "documents": [], "metadatas": []}

        # Paged, not one big get. Chroma's SQLite backend binds one variable
        # per row it returns, so a single unbounded `get` over a large
        # collection dies with "too many SQL variables". That threshold sits
        # between 10k and 37k documents, which is exactly the range
        # parent-child chunking moved this corpus across — the whole-corpus
        # read worked right up until children tripled the row count.
        ids: List[str] = []
        documents: List[str] = []
        metadatas: List[Dict[str, Any]] = []
        offset = 0
        while True:
            kwargs: Dict[str, Any] = {
                "include": ["documents", "metadatas"],
                "limit": ALL_DOCUMENTS_PAGE,
                "offset": offset,
            }
            if where:
                kwargs["where"] = where
            got = self.collection.get(**kwargs)
            page_ids = got.get("ids") or []
            if not page_ids:
                break
            ids.extend(page_ids)
            documents.extend(got.get("documents") or [])
            metadatas.extend(got.get("metadatas") or [])
            if len(page_ids) < ALL_DOCUMENTS_PAGE:
                break
            offset += ALL_DOCUMENTS_PAGE
        return {"ids": ids, "documents": documents, "metadatas": metadatas}

    def query_dense(self, query: str, top_n: int = 20,
                    where: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        if self.count() == 0:
            return []
        embedding = self.embed([query])[0]
        kwargs: Dict[str, Any] = {
            "query_embeddings": [embedding],
            "n_results": min(top_n, self.count()),
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where
        res = self.collection.query(**kwargs)
        out: List[Dict[str, Any]] = []
        for i, doc_id in enumerate(res.get("ids", [[]])[0]):
            out.append({
                "id": doc_id,
                "text": res["documents"][0][i],
                "metadata": res["metadatas"][0][i],
                "distance": res["distances"][0][i],
                "source": res["metadatas"][0][i].get("source"),
            })
        return out
