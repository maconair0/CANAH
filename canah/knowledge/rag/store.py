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
from collections import OrderedDict
from typing import Any, Dict, List, Optional

import chromadb
import ollama

logger = logging.getLogger("rag.store")

from .settings import CHROMA_DIR, CORPUS_DIR, MANIFEST_PATH  # noqa: E402
EMBED_MODEL = os.getenv("EMBED_MODEL", "mxbai-embed-large")

# Where the embeddings are computed:
#   auto     in-process on the GPU when there is one and the Ollama model has a known twin on the Hugging
#            Face hub, else Ollama. In-process takes ~19 ms a question against ~75 ms for an Ollama round
#            trip, and gives the same vectors (cosine 0.9999 against an index built by Ollama), so an index
#            built either way is searched the same.
#   local    in-process (sentence-transformers), on RERANK_DEVICE's choice of device
#   ollama   always Ollama
EMBED_BACKEND = os.getenv("EMBED_BACKEND", "auto")
LOCAL_TWINS = {"mxbai-embed-large": "mixedbread-ai/mxbai-embed-large-v1",
               "nomic-embed-text": "nomic-ai/nomic-embed-text-v1.5",
               "all-minilm": "sentence-transformers/all-MiniLM-L6-v2"}
QUERY_CACHE = int(os.getenv("RAG_QUERY_CACHE", "512"))   # question embeddings kept, most recent first

_local_models: Dict[str, Any] = {}
_local_lock = __import__("threading").Lock()


def _local_embedder(ollama_name: str) -> Optional[Any]:
    """The in-process twin of an Ollama embedding model, loaded once per process; None when not to be used."""
    if EMBED_BACKEND == "ollama":
        return None
    name = LOCAL_TWINS.get(ollama_name.split(":")[0])
    if not name:
        if EMBED_BACKEND == "local":
            logger.warning(f"no in-process twin known for {ollama_name}; embedding through Ollama")
        return None
    with _local_lock:
        if name in _local_models:
            return _local_models[name]
        try:
            import torch
            from .reranker import RERANK_DEVICE
            dev = RERANK_DEVICE if RERANK_DEVICE != "auto" else ("cuda" if torch.cuda.is_available() else "cpu")
            if EMBED_BACKEND == "auto" and not dev.startswith("cuda"):
                _local_models[name] = None      # on a CPU, Ollama (on its GPU or not) is no slower
                return None
            from sentence_transformers import SentenceTransformer
            kw = {"model_kwargs": {"torch_dtype": torch.float16}} if dev.startswith("cuda") else {}
            m = SentenceTransformer(name, device=dev, **kw)
            logger.info(f"embedding in-process: {name} on {dev}")
        except Exception as e:  # noqa: BLE001 - Ollama is always there to fall back on
            logger.warning(f"in-process embedder {name} unavailable ({type(e).__name__}: {e}); using Ollama")
            m = None
        _local_models[name] = m
        return m

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
        self._qcache: "OrderedDict[str, List[float]]" = OrderedDict()
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
        local = _local_embedder(self.embed_model)
        if local is not None:
            try:
                # The model truncates at its own 512 tokens, which is what the shrink-and-retry below
                # amounts to; one batched call instead of one round trip per text.
                return [v.tolist() for v in local.encode(list(texts), batch_size=32, show_progress_bar=False)]
            except Exception as e:  # noqa: BLE001 - a full GPU: Ollama for this call
                logger.warning(f"in-process embedding failed ({type(e).__name__}); using Ollama")
        out: List[List[float]] = []
        for text in texts:
            out.append(self._embed_one(text))
        return out

    def embed_queries(self, queries: List[str]) -> List[List[float]]:
        """Question embeddings, through a small cache: an agent often asks the same thing twice in a turn
        (the librarian, a subagent, a retry), and the corrective pass re-embeds the original question."""
        missing = [q for q in dict.fromkeys(queries) if q not in self._qcache]
        if missing:
            for q, v in zip(missing, self.embed(missing)):
                self._qcache[q] = v
                while len(self._qcache) > QUERY_CACHE:
                    self._qcache.popitem(last=False)
        for q in queries:
            self._qcache.move_to_end(q)
        return [self._qcache[q] for q in queries]

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
        got = self.query_dense_many([query], top_n, where)
        return got[0] if got else []

    def query_dense_many(self, queries: List[str], top_n: int = 20,
                         where: Optional[Dict[str, Any]] = None) -> List[List[Dict[str, Any]]]:
        """Several questions in one embedding batch and one Chroma query (the corrective pass's rewrites)."""
        n = self.count()
        if n == 0 or not queries:
            return [[] for _ in queries]
        kwargs: Dict[str, Any] = {
            "query_embeddings": self.embed_queries(queries),
            "n_results": min(top_n, n),
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where
        res = self.collection.query(**kwargs)
        lists: List[List[Dict[str, Any]]] = []
        for qi in range(len(queries)):
            out: List[Dict[str, Any]] = []
            for i, doc_id in enumerate(res.get("ids", [[]] * len(queries))[qi]):
                out.append({
                    "id": doc_id,
                    "text": res["documents"][qi][i],
                    "metadata": res["metadatas"][qi][i],
                    "distance": res["distances"][qi][i],
                    "source": res["metadatas"][qi][i].get("source"),
                })
            lists.append(out)
        return lists
