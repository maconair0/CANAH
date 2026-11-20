"""
Hybrid retriever (ARCHITECTURE_V2 §5.2).

  dense (Chroma, cosine)  ─┐
                           ├─ RRF fusion ─→ rerank ─→ top-k
  sparse (BM25, exact)    ─┘

Why hybrid rather than dense alone: YANG and NETCONF text is dense with exact
identifiers — `operational-mode`, `channel-10101`, `target-output-power`. Dense
embeddings systematically under-match exact tokens, which is precisely the kind
of match that matters most here. BM25 catches them; the fusion keeps the
semantic recall that BM25 alone would lose.

Empty corpus returns an empty list, never an error (§7.7).
"""
import logging
import re
from typing import Any, Dict, List, Optional

from . import settings as mc
from .reranker import Reranker, reciprocal_rank_fusion
from .store import RagStore

logger = logging.getLogger("rag.retriever")


# An identifier is what BM25 is here for: `operational-mode`, `channel-10101`,
# `target-output-power`, a path like `/interfaces/interface`. Natural language
# has none of these.
_IDENTIFIER = re.compile(r"[a-z0-9]+(?:[-_/\.][a-z0-9]+)+|[a-z]+\d|\d+[a-z]")


def looks_like_identifier_query(query: str) -> bool:
    """Is this query hunting for an exact token, or asking a question?

    The distinction decides whether hyphen expansion helps or hurts. For
    `set operational-mode` it is the whole point: the corpus may write the term
    with a different separator and the expansion bridges that. For "what is
    <a topic>?" it just inflates every generic prose match, which is
    how a jacket blurb out-scores a chapter.
    """
    return bool(_IDENTIFIER.search(query.lower()))


def tokenize(text: str, expand: bool = True) -> List[str]:
    """Split on non-alphanumerics but keep hyphenated identifiers whole.

    `operational-mode` must stay one token — splitting it into `operational`
    and `mode` is what makes keyword search miss the exact match it exists for.
    With `expand`, the parts are emitted *alongside* the whole token; without
    it, only the whole token survives.
    """
    lowered = text.lower()
    tokens = re.findall(r"[a-z0-9][a-z0-9\-_/\.]*", lowered)
    if not expand:
        return tokens
    expanded: List[str] = []
    for t in tokens:
        expanded.append(t)
        if "-" in t or "_" in t:
            expanded.extend(p for p in re.split(r"[-_]", t) if p)
    return expanded


def length_prior(text: str) -> float:
    """Multiplier in [0, 1] damping passages too short to answer anything.

    Kept for callers that hold the text itself; retrieval uses
    `length_prior_chars` because with parent-child chunking the text in hand
    is the child and the length that matters is the parent's.
    """
    return length_prior_chars(len(text.strip()))


def length_prior_chars(n: int) -> float:
    """`length_prior`, given a length rather than the text.

    Not a hard cut above the floor: a two-line definition is occasionally the
    best passage in the corpus, so it is ranked lower rather than discarded.
    Below the floor there is nothing to rank — a heading or a caption fragment
    is never the answer.
    """
    if n < mc.MIN_PASSAGE_CHARS:
        return 0.0
    if n >= mc.FULL_CREDIT_CHARS:
        return 1.0
    span = max(1, mc.FULL_CREDIT_CHARS - mc.MIN_PASSAGE_CHARS)
    return 0.5 + 0.5 * (n - mc.MIN_PASSAGE_CHARS) / span


class HybridRetriever:
    def __init__(self, collection: str = "corpus",
                 reranker: Optional[Reranker] = None,
                 dense_top_n: int = 20, sparse_top_n: int = 20,
                 sections: Optional[List[str]] = None):
        self.store = RagStore(collection=collection)
        # Parent passages for parent-child retrieval (Phase 5). Optional: a
        # missing or empty store means hits are returned as indexed, which is
        # exactly the pre-Phase-5 behaviour.
        try:
            from .parents import ParentStore
            self.parents = ParentStore()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"parent store unavailable ({e}); "
                           f"retrieval will return indexed chunks directly")
            self.parents = None
        self.reranker = reranker or Reranker()
        self.dense_top_n = dense_top_n
        self.sparse_top_n = sparse_top_n
        self.sections = list(sections if sections is not None
                             else mc.RETRIEVAL_SECTIONS)
        self._bm25 = None
        self._bm25_ids: List[str] = []
        self._bm25_docs: List[str] = []
        self._bm25_meta: List[Dict[str, Any]] = []
        self._bm25_size = -1
        self._unfiltered = False   # set when the index predates section labels

    # ----- section filter -----

    def _where(self) -> Optional[Dict[str, Any]]:
        """Chroma filter for the sections this retriever will surface.

        None means no filter, which is also what happens once `_unfiltered` is
        set — see `_check_labelled`.
        """
        if not self.sections or self._unfiltered:
            return None
        if len(self.sections) == 1:
            return {"section_kind": self.sections[0]}
        return {"section_kind": {"$in": self.sections}}

    def _check_labelled(self) -> None:
        """Disable the section filter against an index that has no labels.

        `section_kind` arrives with an ingest, so an index embedded before this
        existed carries none and every filtered query returns nothing. Silently
        answering "no match" over ten thousand perfectly good chunks would be a
        far worse failure than the one the filter fixes, so the filter stands
        down and says so, once.
        """
        if self._unfiltered or not self.sections:
            return
        try:
            probe = self.store.collection.get(where=self._where(), limit=1)
            if probe.get("ids"):
                return
        except Exception as e:
            logger.warning(f"section filter unusable ({e}); retrieving unfiltered")
        else:
            if self.store.count() == 0:
                return
            logger.warning(
                "no chunks carry a section_kind label — this index predates "
                "quality classification. Retrieving unfiltered; re-run "
                "`python -m canah.knowledge.rag.refresh --force` to restore it."
            )
        self._unfiltered = True

    # ----- sparse index -----

    def _ensure_bm25(self):
        """Rebuild the BM25 index when the collection size changes.

        BM25 needs the whole corpus in memory; at this scale that's cheap, and
        rebuilding on size change keeps it correct after a refresh without
        needing an explicit invalidation call.

        The index is built over the *filtered* set. BM25 scores are relative to
        the corpus they are computed against, so leaving front matter in and
        discarding its hits afterwards would still let it distort every IDF.
        """
        size = self.store.count()
        if size == self._bm25_size and self._bm25 is not None:
            return
        if size == 0:
            self._bm25, self._bm25_ids, self._bm25_size = None, [], 0
            return

        from rank_bm25 import BM25Okapi

        self._check_labelled()
        got = self.store.all_documents(where=self._where())
        self._bm25_ids = got["ids"]
        self._bm25_docs = got["documents"]
        self._bm25_meta = got["metadatas"]
        if not self._bm25_docs:
            self._bm25, self._bm25_size = None, size
            return
        # The index is built expanded so an identifier query can still match
        # its parts; suppressing expansion is a query-side decision.
        self._bm25 = BM25Okapi([tokenize(d) for d in self._bm25_docs])
        self._bm25_size = size
        logger.info(
            f"BM25 index built over {len(self._bm25_docs)} of {size} chunks"
            f"{'' if self._unfiltered else ' (sections: ' + ','.join(self.sections) + ')'}"
        )

    def query_sparse(self, query: str, top_n: int) -> List[Dict[str, Any]]:
        self._ensure_bm25()
        if self._bm25 is None:
            return []
        # Expand a query only when it is actually hunting an identifier.
        # Expanding a plain topic phrase adds nothing to find and everything to
        # match, which is how short topic-dense passages win.
        expand = looks_like_identifier_query(query)
        scores = self._bm25.get_scores(tokenize(query, expand=expand))
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        out: List[Dict[str, Any]] = []
        for i in ranked[:top_n]:
            if scores[i] <= 0:
                continue
            out.append({
                "id": self._bm25_ids[i],
                "text": self._bm25_docs[i],
                "metadata": self._bm25_meta[i],
                "bm25_score": float(scores[i]),
                "source": self._bm25_meta[i].get("source"),
            })
        return out

    # ----- the pipeline -----

    def _passage_chars(self, hit: Dict[str, Any]) -> int:
        """How long the passage this hit will *deliver* is.

        With parent-child chunking these differ: the hit is a ~320-character
        child, but what reaches the Generator is its ~900-character parent.
        The length rules below ask "can this passage answer a question", which
        is a property of what gets read, so they have to measure the parent.

        Measuring the child instead was a real regression: children are 320
        characters by construction and `SPARSE_ONLY_MIN_CHARS` is 400, so
        every BM25-only hit was rejected and hybrid retrieval quietly became
        dense-only — losing exactly the exact-identifier matching BM25 is for.
        A quarter of the index also fell under the 250-character floor.

        Falls back to the hit's own text when there is no parent, which is
        what a pre-parent-child index looks like.
        """
        meta = hit.get("metadata") or {}
        source = meta.get("source") or hit.get("source")
        parent_index = meta.get("parent_index", hit.get("parent_index"))
        if self.parents is not None and source is not None and parent_index is not None:
            try:
                n = self.parents.length(source, int(parent_index))
                if n:
                    return n
            except Exception:  # noqa: BLE001 - fall back to the child
                pass
        return len(hit.get("text", "").strip())

    def _admissible(self, hit: Dict[str, Any]) -> bool:
        """Reject a candidate that cannot answer a question whatever it scores.

        Two rules, both aimed at the same failure. A passage under the floor is
        a heading or a caption. A *sparse-only* passage under the higher bar is
        a short lexical coincidence: no dense retriever wanted it, and BM25's
        length normalisation is what put it there.

        Both are measured against the parent — see `_passage_chars`.
        """
        n = self._passage_chars(hit)
        if n < mc.MIN_PASSAGE_CHARS:
            return False
        if hit.get("in_sparse") and not hit.get("in_dense"):
            return n >= mc.SPARSE_ONLY_MIN_CHARS
        return True

    def search(self, query: str, top_k: int = 5,
               rerank: bool = True) -> List[Dict[str, Any]]:
        if self.store.count() == 0:
            logger.info("corpus is empty; returning no context")
            return []

        self._check_labelled()
        dense = self.store.query_dense(query, self.dense_top_n, where=self._where())
        sparse = self.query_sparse(query, self.sparse_top_n)

        by_id: Dict[str, Dict[str, Any]] = {}
        for hit in dense + sparse:
            by_id.setdefault(hit["id"], hit).update(
                {k: v for k, v in hit.items() if k not in by_id[hit["id"]]}
            )

        fused = reciprocal_rank_fusion([
            [h["id"] for h in dense],
            [h["id"] for h in sparse],
        ])
        dense_ids = {d["id"] for d in dense}
        sparse_ids = {s["id"] for s in sparse}
        candidates: List[Dict[str, Any]] = []
        for doc_id, score in fused.items():
            hit = dict(by_id[doc_id])
            hit["in_dense"] = doc_id in dense_ids
            hit["in_sparse"] = doc_id in sparse_ids
            if not self._admissible(hit):
                continue
            # The prior scales fusion rather than replacing it, so a strong
            # short passage can still outrank a weak long one.
            # Also the parent's length: a child is short by design, and
            # damping every candidate for that says nothing about any of them.
            hit["passage_chars"] = self._passage_chars(hit)
            hit["length_prior"] = length_prior_chars(hit["passage_chars"])
            hit["fusion_score"] = score * hit["length_prior"]
            hit["raw_fusion_score"] = score
            candidates.append(hit)

        candidates.sort(key=lambda c: c["fusion_score"], reverse=True)

        if not rerank:
            for c in candidates:
                c["score"] = c["fusion_score"]
            return self._expand(candidates)[:top_k]

        # Rerank a bounded shortlist, not the whole fused set.
        shortlist = candidates[: max(top_k * 3, mc.RERANK_CANDIDATES)]
        top = self.reranker.rerank(query, shortlist, top_k)
        for c in top:
            c["score"] = c.get("rerank_score", c.get("fusion_score"))
        # Expand last, on purpose: ranking is the child's job (that is why it
        # is small), and reading is the parent's. Expanding before the rerank
        # would score the parent and throw away the precision the split buys.
        return self._expand(top)

    def _expand(self, hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Swap child hits for their parent passages (Phase 5).

        Silently a no-op for an index built before parent-child chunking, so
        this can ship without re-embedding the corpus first.
        """
        if self.parents is None:
            return hits
        try:
            return self.parents.expand(hits)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"parent expansion failed ({e}); returning children")
            return hits

    def stats(self) -> Dict[str, Any]:
        return {
            "chunks": self.store.count(),
            "parents": (self.parents.count() if self.parents else 0),
            "sections": "all" if self._unfiltered else ",".join(self.sections),
            "rerank_backend": self.reranker.backend,
            "embed_model": self.store.embed_model,
        }
