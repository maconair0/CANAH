"""The retrieval's tuning and where its files live, in one place.

Every value can be set by the environment variable named beside it, so a tuned
deployment carries over unchanged.

  CANAH_KNOWLEDGE_DIR  root of the index and its side files (default <CANAH_HOME>/knowledge)
  RAG_CORPUS_DIR       the documents to index (default <CANAH_KNOWLEDGE_DIR>/corpus)
  EMBED_MODEL          the Ollama embedding model (default mxbai-embed-large)
  OLLAMA_HOST          where Ollama is (default http://localhost:11434)
"""
import os

import ollama


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


HOME = os.path.abspath(os.path.expanduser(os.getenv("CANAH_HOME", "canah_data")))
KNOWLEDGE_DIR = os.path.abspath(os.path.expanduser(os.getenv("CANAH_KNOWLEDGE_DIR", os.path.join(HOME, "knowledge"))))
CORPUS_DIR = os.getenv("RAG_CORPUS_DIR", os.path.join(KNOWLEDGE_DIR, "corpus"))
CHROMA_DIR = os.getenv("RAG_CHROMA_DIR", os.path.join(KNOWLEDGE_DIR, "chroma"))
MANIFEST_PATH = os.getenv("RAG_MANIFEST", os.path.join(KNOWLEDGE_DIR, "manifest.json"))
PARENTS_DB = os.getenv("RAG_PARENTS_DB", os.path.join(KNOWLEDGE_DIR, "parents.db"))
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
if not OLLAMA_HOST.startswith("http"):
    OLLAMA_HOST = "http://" + OLLAMA_HOST

# The model the LLM reranker uses when the cross-encoder is not installed.
DEFAULT_RERANK_MODEL = os.getenv("RERANK_LLM_MODEL", "qwen3:30b-a3b-instruct-2507-q4_K_M")

RETRIEVAL_SECTIONS = [s.strip() for s in os.getenv("RAG_SECTIONS", "body").split(",") if s.strip()]
MIN_PASSAGE_CHARS = _int("RAG_MIN_PASSAGE_CHARS", 250)
FULL_CREDIT_CHARS = _int("RAG_FULL_CREDIT_CHARS", 500)
SPARSE_ONLY_MIN_CHARS = _int("RAG_SPARSE_ONLY_MIN_CHARS", 400)
RERANK_CANDIDATES = _int("RERANK_CANDIDATES", 24)
RERANK_BATCH = _int("RERANK_BATCH", 6)
RERANK_CANDIDATE_CHARS = _int("RERANK_CANDIDATE_CHARS", 700)
RERANK_CTX = _int("RERANK_NUM_CTX", 16384)
RERANK_PREDICT = _int("RERANK_NUM_PREDICT", 400)


def make_client(role: str = "embed") -> "ollama.Client":
    """An Ollama client (the embedder and the LLM reranker both use one)."""
    return ollama.Client(host=OLLAMA_HOST)

# The production stages (retriever.retrieve):
#   RAG_MIN_RELEVANCE    a passage the re-ranker scores below this (0-1) is not evidence: it is dropped, and
#                        when nothing passes, the search says the documents do not cover the question
#   RAG_CORRECTIVE       on a miss, rewrite the question (keywords, an expansion, a hypothetical answer) and
#                        search again, still judged against the original question (1/0)
#   RAG_REWRITE_MODEL    the model that rewrites (default: the LLM re-ranker's)
MIN_RELEVANCE = float(os.getenv("RAG_MIN_RELEVANCE", "0.1"))   # chosen from the eval, for MiniLM
CORRECTIVE = os.getenv("RAG_CORRECTIVE", "1") == "1"
REWRITE_MODEL = os.getenv("RAG_REWRITE_MODEL", "") or DEFAULT_RERANK_MODEL
