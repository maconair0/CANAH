"""
Reranking (ARCHITECTURE_V2 §5.2).

Two stages, deliberately separated:

  1. Reciprocal Rank Fusion — merges the dense and BM25 candidate lists.
     Purely arithmetic, no model, no tuning knob to get wrong. This is what
     makes hybrid retrieval actually hybrid rather than two lists stapled
     together.

  2. Cross-encoder rerank — scores query+passage jointly, which is reliably
     better than either retriever's own ordering.

The cross-encoder backend is pluggable because it has to be here:
sentence-transformers is installed in this environment but broken (a numpy /
scipy version conflict belonging to an unrelated project). Rather than fix
someone else's venv, this tries the cross-encoder, falls back to an
Ollama-hosted LLM reranker, and falls back again to plain RRF order. Every
path reports which one ran, so a result set is never silently worse than it
looks.
"""
import json
import logging
import os
from typing import Any, Dict, List, Optional

from . import settings as mc

logger = logging.getLogger("rag.reranker")

CROSS_ENCODER_MODEL = os.getenv("RERANK_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
LLM_RERANK_MODEL = mc.DEFAULT_RERANK_MODEL
from .settings import OLLAMA_HOST  # noqa: E402

RRF_K = 60  # standard RRF damping constant


def reciprocal_rank_fusion(
    ranked_lists: List[List[str]], k: int = RRF_K
) -> Dict[str, float]:
    """Fuse several ranked ID lists into one score per ID.

    score(d) = sum over lists of 1/(k + rank(d)). A document ranked well by
    both retrievers beats one ranked brilliantly by only one, which is exactly
    the behaviour hybrid retrieval is for.
    """
    scores: Dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores


class Reranker:
    def __init__(self, prefer: str = "auto"):
        self.backend = "rrf_only"
        self._cross_encoder = None
        self._ollama = None

        if prefer in ("auto", "cross_encoder"):
            self._try_cross_encoder()
        if self.backend == "rrf_only" and prefer in ("auto", "llm"):
            self._try_llm()

        logger.info(f"Reranker backend: {self.backend}")

    @staticmethod
    def _disable_transformers_torchvision() -> None:
        """Stop transformers reaching for a torchvision it cannot import.

        This host has torchvision 0.24 (built against torch 2.9) sitting next
        to torch 2.11, so importing it dies on `operator torchvision::nms does
        not exist`. transformers pulls it in from `image_utils` for PIL
        interpolation constants — of no interest whatsoever to a text
        cross-encoder, which is nonetheless what takes the whole import down.

        Clearing the availability flag before the lazy import happens makes
        transformers take its no-torchvision path. Deliberately preferred to
        re-pinning torch in a shared virtualenv on behalf of an unrelated
        project. If the flag ever moves, the except below keeps this advisory:
        the cross-encoder simply falls back as it did before.
        """
        try:
            from transformers.utils import import_utils
        except Exception:
            return
        if not getattr(import_utils, "_torchvision_available", False):
            return
        try:
            import torchvision  # noqa: F401  — is it actually usable?
        except Exception as e:
            import_utils._torchvision_available = False
            logger.info(
                f"torchvision is installed but not importable ({type(e).__name__}); "
                f"hiding it from transformers so the text cross-encoder can load"
            )

    def _try_cross_encoder(self):
        try:
            self._disable_transformers_torchvision()
            from sentence_transformers import CrossEncoder
            self._cross_encoder = CrossEncoder(CROSS_ENCODER_MODEL)
            self.backend = "cross_encoder"
        except Exception as e:
            # Warning, not info. The cross-encoder is the reranker that
            # actually works; the LLM fallback is a consolation prize and a
            # silent demotion to it looks like a retrieval-quality mystery.
            logger.warning(
                f"cross-encoder unavailable — {type(e).__name__}: {e}. "
                f"Falling back to the LLM reranker, which ranks noticeably "
                f"worse. Fix with: pip install 'numpy>=2.0,<2.8'"
            )

    def _try_llm(self):
        try:
            import ollama
            from .settings import make_client
            self._ollama = make_client("reranker")
            self._ollama.list()
            self.backend = "llm"
        except Exception as e:
            logger.warning(
                f"LLM reranker unavailable ({type(e).__name__}); "
                f"falling back to fusion order only"
            )

    def rerank(self, query: str, candidates: List[Dict[str, Any]],
               top_k: int = 5) -> List[Dict[str, Any]]:
        """Order candidates by relevance to the query, best first.

        Candidates carry at least {"id", "text"} and whatever fusion score the
        retriever computed. Returns the top_k with a `rerank_score` and the
        backend that produced it.
        """
        if not candidates:
            return []

        if self.backend == "cross_encoder":
            return self._rerank_cross_encoder(query, candidates, top_k)
        if self.backend == "llm":
            return self._rerank_llm(query, candidates, top_k)
        return self._rerank_passthrough(candidates, top_k)

    def _rerank_cross_encoder(self, query, candidates, top_k):
        pairs = [(query, c["text"]) for c in candidates]
        # No progress bar: this runs inside an interactive chat turn, and a
        # tqdm line redrawing over the prompt is worse than no feedback.
        scores = self._cross_encoder.predict(pairs, show_progress_bar=False)
        for c, s in zip(candidates, scores):
            c["rerank_score"] = float(s)
            c["reranked_by"] = "cross_encoder"
        return sorted(candidates, key=lambda c: c["rerank_score"], reverse=True)[:top_k]

    def _rerank_llm(self, query, candidates, top_k):
        """Score passages for relevance in small batches.

        Batched, not one call for everything. Asked to score twenty-four
        passages at once, an 8B model stops discriminating: everything lands in
        an 8-10 band, ties fall back to fusion order, and the stage costs a
        second of latency to change nothing. It scored a jacket blurb 10/10 for
        the book's own topic — mentioning the topic is not answering about it,
        and the rubric below exists to say so in as many words.

        A batch that fails to parse falls back to fusion order for its own
        members only, rather than discarding the whole ranking.
        """
        batch = max(1, mc.RERANK_BATCH)
        scored: List[Dict[str, Any]] = []
        for start in range(0, len(candidates), batch):
            group = candidates[start:start + batch]
            by_index = self._score_batch(query, group)
            for i, c in enumerate(group):
                if by_index is None:
                    # Keep this group's own fusion order, scaled below the
                    # scored range so a parse failure can't beat a real 10.
                    c["rerank_score"] = c.get("fusion_score", 0.0)
                    c["reranked_by"] = "rrf_fallback"
                else:
                    c["rerank_score"] = by_index.get(i, 0.0)
                    c["reranked_by"] = "llm"
                    # Break the model's ties with fusion order rather than
                    # leaving them to list position.
                    c["rerank_score"] += min(0.49, c.get("fusion_score", 0.0))
                scored.append(c)
        return sorted(scored, key=lambda c: c["rerank_score"], reverse=True)[:top_k]

    _RUBRIC = (
        "Score each passage 0-10 on whether it ANSWERS the query, not whether "
        "it mentions the topic.\n"
        "  0-2  mentions the subject but explains nothing: praise for a book, "
        "a preface, a table of contents, a heading, a bibliography entry, a "
        "figure caption.\n"
        "  3-5  related background; touches the subject without addressing the "
        "question asked.\n"
        "  6-8  explains part of what was asked.\n"
        "  9-10 directly and substantively answers the query.\n"
        "Most passages are not 9s. Use the low end — a passage that only names "
        "the topic scores 0-2 however many times it names it."
    )

    def _score_batch(self, query, group) -> Optional[Dict[int, float]]:
        listing = "\n\n".join(
            f"[{i}] {c['text'][:mc.RERANK_CANDIDATE_CHARS]}"
            for i, c in enumerate(group)
        )
        prompt = (
            f"QUERY: {query}\n\nPASSAGES:\n{listing}\n\n{self._RUBRIC}\n\n"
            f'Respond ONLY with JSON: {{"scores": [{{"index": 0, "score": 7}}, ...]}} '
            f"— one entry for each of the {len(group)} passages."
        )
        try:
            response = self._ollama.chat(
                model=LLM_RERANK_MODEL,
                messages=[
                    {"role": "system",
                     "content": "You rank passages by how well they answer a "
                                "query. Respond only with JSON."},
                    {"role": "user", "content": prompt},
                ],
                options={"temperature": 0.0,
                         "num_ctx": mc.RERANK_CTX,
                         "num_predict": mc.RERANK_PREDICT},
                format="json",
            )
            raw = response["message"]["content"]
            parsed = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
            return {
                int(s["index"]): max(0.0, min(10.0, float(s["score"])))
                for s in parsed.get("scores", [])
                if "index" in s and "score" in s
            }
        except Exception as e:
            logger.warning(f"LLM rerank batch failed ({e}); keeping fusion order")
            return None

    @staticmethod
    def _rerank_passthrough(candidates, top_k):
        for c in candidates:
            c["rerank_score"] = c.get("fusion_score", 0.0)
            c["reranked_by"] = "rrf_only"
        return sorted(candidates, key=lambda c: c["rerank_score"], reverse=True)[:top_k]
