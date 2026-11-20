"""
What the agent knows from documents: the RAG, used the deep-agent way.

  search_knowledge   on the main agent: a few short, cited passages, for a quick
                     fact ("what does the standard say the limit is?")
  librarian          a subagent that searches more widely, reads the passages
                     in full and returns a short cited answer. It keeps long
                     passages out of the main agent's context: the main agent
                     sees the librarian's answer, not the twenty passages
                     behind it.
  knowledge_sources  what is in the corpus

Every passage carries its citation, [source p.N], and the agent is told to
cite and to say when the corpus does not hold the answer.

The retrieval itself is behind `Retriever` (search(query, k) -> hits with
text, source, pages, score), so a system can bring its own. The default is
the hybrid retriever in rag/: dense (Chroma, Ollama
embeddings) + BM25, fused, reranked by a cross-encoder, child chunks ranked
and parent passages returned. Index a folder with

  python -m canah.knowledge.rag.refresh --corpus <folder>
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool

logger = logging.getLogger("canah.knowledge")

QUICK_K = int(os.getenv("CANAH_KNOWLEDGE_QUICK_K", "3"))
QUICK_CHARS = int(os.getenv("CANAH_KNOWLEDGE_QUICK_CHARS", "700"))
DEEP_K = int(os.getenv("CANAH_KNOWLEDGE_DEEP_K", "8"))
DEEP_CHARS = int(os.getenv("CANAH_KNOWLEDGE_DEEP_CHARS", "2500"))


@runtime_checkable
class Retriever(Protocol):
    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]: ...


def cite(hit: Dict[str, Any]) -> str:
    meta = hit.get("metadata") or {}
    src = os.path.basename(str(hit.get("source") or meta.get("source") or "unknown"))
    a, b = meta.get("page_start"), meta.get("page_end")
    pages = "" if a is None else (f" p.{a}" if b in (None, a) else f" pp.{a}-{b}")
    return f"[{src}{pages}]"


_STOP = {"what", "which", "when", "where", "who", "whom", "why", "how", "does", "do", "did", "is", "are", "was",
         "the", "a", "an", "of", "for", "to", "in", "on", "and", "or", "cite", "source", "please", "tell", "me",
         "explain", "give", "list", "show", "can", "could", "would", "should", "this", "that", "use", "documents"}


def named_terms(query: str) -> List[str]:
    """The specific things a question names: words with a capital letter after the first, a digit or a
    symbol in them (RFC8345, IEEE 802.3, ProductX2, v1.5, C++), not common question words."""
    out = []
    for w in re.findall(r"[A-Za-z0-9][\w.+/-]*[\w+]|[A-Za-z0-9]", query or ""):
        if w.lower() in _STOP or len(w) < 2:
            continue
        # A capital after the first letter, a digit or a symbol marks a name (not "What", "Explain").
        if re.search(r"[A-Z].*[A-Z]|[a-z][A-Z]|\d|[+./]", w[1:] if w[:1].isupper() else w):
            out.append(w)
    return list(dict.fromkeys(out))


def _mentions(text: str, term: str) -> bool:
    return re.sub(r"[^a-z0-9+]", "", term.lower()) in re.sub(r"[^a-z0-9+]", "", text.lower())


def render(hits: List[Dict[str, Any]], max_chars: int, query: str = "") -> str:
    if not hits:
        return "The corpus holds nothing on this. Say so rather than answering from memory."
    terms = named_terms(query)
    out, any_full = [], False
    for i, h in enumerate(hits, 1):
        text = " ".join(str(h.get("text") or "").split())
        missing = [t for t in terms if not _mentions(text, t)]
        any_full = any_full or not missing
        if len(text) > max_chars:
            text = text[:max_chars] + " ..."
        score = h.get("score")
        out.append(f"{i}. {cite(h)}" + (f" (score {score:.2f})" if isinstance(score, (int, float)) else "")
                   + (f" [does not mention: {', '.join(missing)}]" if missing else "") + f"\n{text}")
    head = ""
    if terms and not any_full:
        head = (f"Note: no passage below mentions all of {', '.join(terms)}. If one of these is a standard, product "
                f"or version, a passage about another one does not answer the question; say the corpus does not "
                f"state it. If it is one of the system's own objects (a device, a link, a service), documents do not "
                f"name it: use the rule or fact they state for its kind, with the object's values from the tools."
                f"\n\n")
    return head + "\n\n".join(out)


class HybridKnowledge:
    """The hybrid retriever in rag/, opened on first use (Chroma and the
    cross-encoder take a few seconds to load)."""

    def __init__(self, collection: str = "corpus"):
        self.collection = collection
        self._r = None

    @property
    def retriever(self):
        if self._r is None:
            from .rag.retriever import HybridRetriever
            self._r = HybridRetriever(collection=self.collection)
        return self._r

    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        return self.retriever.search(query, top_k=top_k)

    def sources(self) -> List[Dict[str, Any]]:
        got = self.retriever.store.all_documents()
        counts: Dict[str, int] = {}
        for m in got.get("metadatas") or []:
            s = os.path.basename(str((m or {}).get("source") or "?"))
            counts[s] = counts.get(s, 0) + 1
        return [{"source": s, "chunks": n} for s, n in sorted(counts.items())]

    def stats(self) -> Dict[str, Any]:
        return self.retriever.stats()


_CITATION = re.compile(r"\[([^\[\]]{3,200}?)\]")
_GUARD = "[citation check]"


def _cited_sources(text: str) -> List[str]:
    """The sources an answer cites: "[file.pdf p.3]" -> "file.pdf". Only things
    that look like a citation (a page, or a file name), not every bracket."""
    out = []
    for inner in _CITATION.findall(text or ""):
        inner = inner.strip()
        if re.search(r"\bpp?\.\s*\d", inner) or re.search(r"\.(pdf|md|txt|html?|docx?|yang|rst)\b", inner, re.I):
            out.append(re.split(r",?\s+pp?\.", inner)[0].strip().lower())
    return out


class CitationGuard(AgentMiddleware):
    """A final answer may cite only sources a tool returned in this
    conversation. One that cites anything else is sent back once, with the
    sources it did see, to search or to drop the claim."""

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: Any, runtime: Any) -> Optional[Dict[str, Any]]:
        msgs = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
        if not msgs:
            return None
        last = msgs[-1]
        if not isinstance(last, AIMessage) or last.tool_calls:
            return None
        cited = _cited_sources(str(last.content))
        if not cited:
            return None
        # The current question's turn: from the last human message on.
        start = max((i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)), default=0)
        turn = msgs[start:]
        if any(isinstance(m, HumanMessage) and _GUARD in str(m.content) for m in turn[1:]):
            return None                      # sent back once already this turn
        seen = " ".join(str(m.content) for m in msgs if isinstance(m, ToolMessage)).lower()
        unseen = [c for c in cited if c and c not in seen]
        if not unseen:
            return None
        return {"jump_to": "model", "messages": [HumanMessage(content=(
            f"{_GUARD} Your answer cites {', '.join(sorted(set(unseen)))}, which no tool returned in this "
            f"conversation. Search the corpus (search_knowledge, or the librarian) and answer only from what it "
            f"returns, citing those sources; if it does not state the answer, say the corpus does not state it."))]}


class KnowledgeProvider:
    """The RAG as tools and a librarian subagent (a ToolProvider)."""

    def __init__(self, retriever: Optional[Retriever] = None, librarian_model: Any = None):
        self.retriever = retriever or HybridKnowledge()
        self.librarian_model = librarian_model
        self._tools = self._make()

    def _search(self, query: str, k: int, chars: int) -> str:
        try:
            hits = self.retriever.search(query, top_k=k)
        except Exception as e:  # noqa: BLE001 - a missing index is said, not raised
            return f"Error: the knowledge base could not be searched: {type(e).__name__}: {e}"
        return render(hits[:k], chars, query)

    def _make(self) -> List[BaseTool]:
        def search_knowledge(query: str) -> str:
            """Search the document corpus (standards, papers, manuals, the system's own docs) for a quick fact.
            Returns a few short passages, each with its citation [source p.N]. Use a passage only if it states the
            fact for exactly the thing asked (the same standard, product, version); a value from another setup is not
            an answer. If none does, say the corpus does not state it. For a question that needs reading and
            combining several documents, delegate to the librarian subagent instead."""
            return self._search(query, QUICK_K, QUICK_CHARS)

        def research_knowledge(query: str, k: int = DEEP_K) -> str:
            """Search the corpus widely and return full passages with citations, for reading and combining.
            Search again with other words when the first passages do not answer the question."""
            return self._search(query, max(1, min(int(k), 12)), DEEP_CHARS)

        def knowledge_sources() -> str:
            """List the documents in the corpus, with how many indexed passages each has."""
            try:
                return json.dumps(self.retriever.sources(), indent=1)  # type: ignore[attr-defined]
            except Exception as e:  # noqa: BLE001
                return f"Error: the corpus could not be listed: {type(e).__name__}: {e}"

        return [StructuredTool.from_function(f) for f in (search_knowledge, research_knowledge, knowledge_sources)]

    def tools(self) -> List[BaseTool]:
        return list(self._tools)

    def writes(self) -> List[str]:
        return []

    def main_tools(self) -> List[str]:
        return ["search_knowledge", "knowledge_sources"]

    def subagents(self) -> List[Dict[str, Any]]:
        spec = {"name": "librarian",
                "description": ("Answers from the document corpus (standards, papers, manuals, the system's own "
                                "documentation). Delegate questions of how something works, what a standard says, "
                                "what the literature reports, or any answer that needs several documents read and "
                                "combined. Returns a short answer with citations."),
                "system_prompt": ("You answer from the document corpus only. Search with research_knowledge, read the "
                                  "passages, search again with different words if they do not answer, then write a "
                                  "short answer in which every claim carries its citation [source p.N] as given. A "
                                  "passage counts only if it is about exactly what was asked (the same standard, "
                                  "product, version); a value from another setup is not the answer. If the corpus does "
                                  "not answer the question, say exactly that; never fill the gap from memory."),
                "tools": [t for t in self._tools if t.name in ("research_knowledge", "knowledge_sources")]}
        if self.librarian_model is not None:
            spec["model"] = self.librarian_model
        return [spec]

    def shared_tools(self) -> List[str]:
        # Any subagent may look up a rule or a definition while it works.
        return ["search_knowledge"]

    def middleware(self) -> List[AgentMiddleware]:
        return [CitationGuard()]

    def instructions(self) -> str:
        return ("Documents (standards, papers, manuals, this system's own docs) are searched with search_knowledge "
                "for a quick fact, or answered by the librarian subagent for anything needing more reading. Cite "
                "what you take from them as [source p.N]. A passage answers only if it states the fact for exactly "
                "what was asked; a number from a different standard, product or experiment is not the answer, and "
                "presenting it as one is a wrong answer. Always search before answering a question about documents, "
                "standards, the literature or how this system works; only after searching may you say the corpus "
                "does not state it. A citation names a source a tool returned, never one you did not see.")


def provider() -> KnowledgeProvider:
    """For `providers: [canah.knowledge:provider]` in canah.yaml; searches settings.knowledge_collection."""
    from ..config import load
    return KnowledgeProvider(retriever=HybridKnowledge(collection=load().knowledge_collection))


def add_documents(paths: List[str], collection: str = "corpus") -> Dict[str, Any]:
    """Copy files into the corpus folder and index them (new or changed files only)."""
    import shutil
    from .rag import refresh
    from .rag.settings import CORPUS_DIR
    os.makedirs(CORPUS_DIR, exist_ok=True)
    for p in paths:
        shutil.copy(p, os.path.join(CORPUS_DIR, os.path.basename(p)))
    return refresh.refresh(CORPUS_DIR, collection, False)
