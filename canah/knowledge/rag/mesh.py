"""
The second brain: the documents the agent cites, as a graph of what is about what (ported from
edgeLM's central console).

- **Documents, not chunks.** A node per document, placed by the mean of its chunks' embeddings: one
  streamed pass over vectors that already exist, no embedding calls, cached against a fingerprint of
  the corpus.
- **An edge** is cosine similarity between two documents' means, above a floor, keeping each one's
  nearest few: "about similar things", which is what retrieval runs on. Not citation, dependency or
  agreement.
- **Clusters** are k-means over the means, named after the words common inside a cluster and rare
  outside it: a label for a region, not a taxonomy.
"""
import json
import logging
import math
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("rag.mesh")

from .settings import CORPUS_DIR, KNOWLEDGE_DIR  # noqa: E402

MESH_PATH = os.getenv("RAG_MESH_FILE", os.path.join(KNOWLEDGE_DIR, "mesh.json"))

# How many neighbours each document keeps, and how alike two documents must be
# before an edge is drawn at all. Four is enough to connect the graph without
# turning it into a mat; the floor is what keeps "everything is a bit like
# everything" out of it.
NEIGHBOURS = int(os.getenv("RAG_MESH_NEIGHBOURS", "4"))
MIN_SIMILARITY = float(os.getenv("RAG_MESH_MIN_SIMILARITY", "0.55"))
# Page size is a memory knob, not a speed one. Chroma hands back Python lists
# of floats before numpy sees them, so a page of 2000 x 1024 peaked at 1.1 GB on
# this corpus against 540 MB at 1000 — on a host whose GPU already holds a 21 GB
# model, that is the number that matters.
PAGE = int(os.getenv("RAG_MESH_PAGE", "1000"))

# Words that name nothing in any corpus. Extended at build time by the "in most of
# the documents" rule; this list only covers the ones that are structural rather
# than topical.
STOP = {
    "the", "and", "for", "with", "from", "that", "this", "are", "was", "has",
    "have", "its", "can", "not", "but", "all", "any", "one", "two", "use",
    "used", "using", "based", "new", "pdf", "txt", "md", "docx", "org", "com",
    "www", "http", "https", "ieee", "acm", "doi", "vol", "pp", "fig", "table",
    "section", "chapter", "paper", "draft", "final", "copy", "version",
}


def _tokens(text: str) -> List[str]:
    return [t for t in re.split(r"[^a-z0-9]+", (text or "").lower())
            if len(t) > 2 and t not in STOP and not t.isdigit()]


def title_of(source: str) -> str:
    """A readable name for a file, for a label that has to fit in a circle."""
    stem = os.path.splitext(os.path.basename(source or ""))[0]
    stem = re.sub(r"^\d{4}\.\d{4,5}v?\d*[_-]?", "", stem)   # arXiv id prefix
    stem = re.sub(r"[_-]+", " ", stem).strip()
    return (stem[:60] or source or "untitled").strip()


def _fingerprint(store: Any, corpus_dir: Optional[str] = None) -> str:
    """What the mesh was built from, so a stale cache is detectable.

    From the chunk count and the corpus directory — 58 stat calls — rather than
    from the chunks' metadata. Asking Chroma for every row's metadata to decide
    whether to skip the work cost 600 MB and most of a second, which is most of
    what the work itself costs: a cache check has to be cheaper than a cache
    miss or it is not a cache.

    Chunk count alone would miss a document replaced by one that chunked to the
    same length, so names and modification times go in with it.
    """
    # Read at call time, not bound as a default: a default argument freezes the
    # directory at import, so pointing the module at another corpus silently
    # went on fingerprinting the first one.
    corpus_dir = corpus_dir or CORPUS_DIR
    try:
        count = store.collection.count()
    except Exception:  # noqa: BLE001
        count = -1
    files = []
    try:
        for name in sorted(os.listdir(corpus_dir)):
            path = os.path.join(corpus_dir, name)
            if os.path.isfile(path):
                stat = os.stat(path)
                files.append((name, stat.st_size, int(stat.st_mtime)))
    except OSError:
        pass
    return f"{count}:{len(files)}:{hash(tuple(files)) & 0xFFFFFFFF:08x}"


def centroids(store: Any, page: int = PAGE) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """One streamed pass: a mean embedding per document.

    Paged so that peak memory is one page of vectors rather than the corpus.
    Returns the per-document accumulation and what it cost, because "is this
    affordable" is a question the console should be able to answer with a
    number rather than a shrug.
    """
    import numpy as np

    started = time.time()
    sums: Dict[str, Any] = {}
    counts: Dict[str, int] = {}
    words: Dict[str, Dict[str, int]] = {}
    seen = 0
    offset = 0
    while True:
        got = store.collection.get(include=["metadatas", "embeddings", "documents"],
                                   limit=page, offset=offset)
        metas = got.get("metadatas") or []
        vectors = got.get("embeddings")
        if vectors is None or len(metas) == 0:
            break
        vectors = np.asarray(vectors, dtype="float32")
        texts = got.get("documents") or [""] * len(metas)
        for i, meta in enumerate(metas):
            source = (meta or {}).get("source") or "?"
            if source not in sums:
                sums[source] = np.zeros(vectors.shape[1], dtype="float32")
                counts[source] = 0
                words[source] = {}
            sums[source] += vectors[i]
            counts[source] += 1
            # Only the first few chunks of each document feed the vocabulary:
            # a label comes from what a document opens with — its title, its
            # abstract — not from its fortieth page of tables.
            if counts[source] <= 6:
                for token in _tokens(texts[i])[:400]:
                    words[source][token] = words[source].get(token, 0) + 1
        seen += len(metas)
        offset += len(metas)
        if len(metas) < page:
            break
    cost = {"chunks_read": seen, "documents": len(sums),
            "seconds": round(time.time() - started, 2),
            "page": page, "embedding_calls": 0}
    logger.info("mesh: folded %d chunks into %d documents in %.1fs",
                seen, len(sums), cost["seconds"])
    return {"sums": sums, "counts": counts, "words": words}, cost


def _normalise(matrix: Any) -> Any:
    import numpy as np
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def _kmeans(points: Any, k: int, rounds: int = 25, seed: int = 7) -> Any:
    """Plain k-means on unit vectors, so cosine and euclidean agree.

    Written out rather than pulled in: scikit-learn is not a dependency of this
    tier, and this is twenty lines over at most a few hundred points.
    """
    import numpy as np
    rng = np.random.default_rng(seed)
    n = len(points)
    k = max(1, min(k, n))
    centres = points[rng.choice(n, size=k, replace=False)].copy()
    labels = np.zeros(n, dtype=int)
    for _ in range(rounds):
        similarity = points @ centres.T
        new = similarity.argmax(axis=1)
        if (new == labels).all():
            break
        labels = new
        for j in range(k):
            members = points[labels == j]
            if len(members):
                centres[j] = members.mean(axis=0)
        centres = _normalise(centres)
    return labels


def _cluster_names(words: Dict[str, Dict[str, int]], members: List[str],
                   everywhere: Dict[str, int], total_docs: int) -> str:
    """Words common inside this cluster and rare outside it.

    Frequency alone names every cluster after the corpus's own subject. What
    distinguishes a region is what it says that the rest of the corpus does not.
    """
    inside: Dict[str, int] = {}
    for source in members:
        for token, n in (words.get(source) or {}).items():
            inside[token] = inside.get(token, 0) + 1     # documents, not hits
    scored = []
    for token, docs_here in inside.items():
        if docs_here < 2 and len(members) > 2:
            continue
        share_here = docs_here / max(1, len(members))
        share_all = everywhere.get(token, 1) / max(1, total_docs)
        if share_all > 0.6:            # in most of the corpus: names nothing
            continue
        scored.append((share_here / (share_all + 0.15), token))
    scored.sort(reverse=True)
    picked = [token for _, token in scored[:3]]
    return " · ".join(picked) if picked else "mixed"


def build(store: Any, episodes: Any = None, neighbours: int = NEIGHBOURS,
          min_similarity: float = MIN_SIMILARITY) -> Dict[str, Any]:
    """The graph: a node per document, an edge per close pair, named clusters."""
    import numpy as np

    started = time.time()
    acc, cost = centroids(store)
    sources = sorted(acc["sums"])
    if not sources:
        return {"nodes": [], "edges": [], "clusters": [], "built_at": time.time(),
                "cost": cost, "empty": "nothing is embedded yet"}

    matrix = _normalise(np.vstack([acc["sums"][s] / max(1, acc["counts"][s])
                                   for s in sources]))
    similarity = matrix @ matrix.T
    np.fill_diagonal(similarity, -1.0)

    # Each document keeps its nearest few. Undirected: an edge the far end did
    # not pick is still an edge, and dropping it would leave an outlier with no
    # line to anything, which reads as "unrelated" rather than "unusual".
    edges: Dict[Tuple[int, int], float] = {}
    order = np.argsort(-similarity, axis=1)[:, :max(1, neighbours)]
    for i, row in enumerate(order):
        for j in row:
            score = float(similarity[i, j])
            if score < min_similarity:
                continue
            key = (min(i, int(j)), max(i, int(j)))
            edges[key] = max(edges.get(key, 0.0), score)

    # Roughly one cluster per eight documents. sqrt(n/2) put 40 of 58
    # documents in a cluster called "large language models", which is true and
    # tells a reader nothing.
    k = max(2, min(9, int(round(len(sources) / 8)) or 2))
    labels = _kmeans(matrix, k)

    everywhere: Dict[str, int] = {}
    for source in sources:
        for token in (acc["words"].get(source) or {}):
            everywhere[token] = everywhere.get(token, 0) + 1

    clusters = []
    for j in sorted(set(int(x) for x in labels)):
        members = [sources[i] for i in range(len(sources)) if labels[i] == j]
        clusters.append({"id": int(j), "size": len(members),
                         "name": _cluster_names(acc["words"], members, everywhere,
                                                len(sources))})

    degree: Dict[int, int] = {}
    for (a, b) in edges:
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1

    nodes = [{
        "id": source,
        "title": title_of(source),
        "chunks": int(acc["counts"][source]),
        "cluster": int(labels[i]),
        "links": degree.get(i, 0),
        # Nothing links to it above the floor: it is in the corpus and apart
        # from it, which is worth seeing rather than hiding.
        "isolated": degree.get(i, 0) == 0,
    } for i, source in enumerate(sources)]

    out = {
        "nodes": nodes,
        "edges": [{"source": sources[a], "target": sources[b],
                   "similarity": round(score, 3)}
                  for (a, b), score in sorted(edges.items(), key=lambda kv: -kv[1])],
        "clusters": clusters,
        "episodes": _episode_summary(episodes),
        "fingerprint": _fingerprint(store),
        "built_at": time.time(),
        "cost": {**cost, "total_seconds": round(time.time() - started, 2),
                 "neighbours": neighbours, "min_similarity": min_similarity},
        "means": ("an edge is cosine similarity between two documents' mean "
                  "embedding, which is what retrieval itself runs on. It means "
                  "“about similar things” — not citation, not "
                  "dependency, not agreement: two papers that contradict each "
                  "other are neighbours here."),
    }
    return out


def _episode_summary(episodes: Any) -> Dict[str, Any]:
    """What the agent learned from doing, as against from reading.

    Kept as a count rather than drawn into the mesh: an episode is a past
    resolution, its text is a trigger and a proposal, and placing it among
    reference documents by embedding distance would put it next to whatever it
    *mentions* rather than what it taught. Said here instead of implied there.
    """
    if episodes is None:
        return {"available": False}
    try:
        return {"available": True, "count": int(episodes.count()),
                "what": "past resolutions that worked, retrieved as examples "
                        "when a similar trigger comes back"}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc)[:200]}


# ----- caching -----

def load(path: str = MESH_PATH) -> Optional[Dict[str, Any]]:
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def save(mesh: Dict[str, Any], path: str = MESH_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(mesh, fh)
    os.replace(tmp, path)


def get(store: Any, episodes: Any = None, rebuild: bool = False,
        path: str = MESH_PATH) -> Dict[str, Any]:
    """The mesh, from cache when the corpus has not moved since it was built."""
    cached = None if rebuild else load(path)
    if cached is not None:
        try:
            if cached.get("fingerprint") == _fingerprint(store):
                return {**cached, "from_cache": True}
        except Exception:  # noqa: BLE001
            pass
    mesh = build(store, episodes)
    try:
        save(mesh, path)
    except OSError as exc:
        logger.warning("could not cache the mesh: %s", exc)
    return {**mesh, "from_cache": False}
