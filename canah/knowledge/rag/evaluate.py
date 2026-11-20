"""
How good the retrieval is, measured on the corpus itself, so every change to it is proven and not assumed.

  golden set   questions with a known answer passage: the local model writes one specific question per
               sampled parent passage (it must be answerable from that passage alone), plus questions the
               corpus does not cover, so abstention is measured too. Kept in <KNOWLEDGE_DIR>/eval/golden.json;
               edit it by hand to add real operators' questions.
  metrics      for answerable questions: hit@1, hit@5 (the right passage among the first k), MRR, nDCG@5;
               for questions the corpus does not cover: how often the retriever says so (abstention);
               for answerable ones, how often it wrongly abstains; latency p50 / p95.

  python -m canah.knowledge.rag.evaluate build [--n 40] [--seed 7]
  python -m canah.knowledge.rag.evaluate run [--compare]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional

from . import settings as mc

EVAL_DIR = os.path.join(mc.KNOWLEDGE_DIR, "eval")
GOLDEN = os.path.join(EVAL_DIR, "golden.json")

# Questions no network corpus answers by accident: what the retriever should decline, not answer badly.
OFF_TOPIC = [
    "What is the boiling point of ethanol at sea level?",
    "Who painted the ceiling of the Sistine Chapel?",
    "How many players are on a rugby union team?",
    "What is the recommended daily intake of vitamin C for adults?",
    "Which planet has the longest day in the solar system?",
    "How do I bake sourdough bread with a cold overnight proof?",
    "What is the capital of Mongolia?",
    "How long is the warranty on a typical washing machine motor?",
]
# Network questions this corpus (an intent-based networking paper) does not answer: the hard case, same
# field, shared words. Edit both lists to fit your own corpus.
NEAR_OFF_TOPIC = [
    "What is the default OSPF hello interval on Juniper routers?",
    "How many channels fit in the C-band on a 50 GHz ITU grid?",
    "What is the maximum transmit power of a Wi-Fi 6 access point in the 5 GHz band in Europe?",
    "Which BGP attribute is compared first in Cisco's best-path selection?",
    "What fibre attenuation in dB/km does standard G.652 single-mode fibre have at 1550 nm?",
    "What port does NETCONF over SSH use by default?",
]

_ASK = """You write test questions for a search engine over technical documents.

Here is one passage from the document "{source}" (page {page}):
---
{text}
---

Write ONE question that:
- can be answered from this passage alone, with a specific fact, figure, name or mechanism it states;
- uses different words from the passage where it can (paraphrase; do not copy a sentence);
- makes sense to someone who has not seen the passage (no "this passage", "the authors", "the table above").

Answer with JSON only: {{"question": "...", "answer": "the short answer, from the passage"}}"""


def _parents(limit: int = 0) -> List[Dict[str, Any]]:
    db = sqlite3.connect(mc.PARENTS_DB)
    rows = db.execute("select source, parent_index, text from parents").fetchall()
    db.close()
    return [{"source": s, "parent_index": int(i), "text": t} for s, i, t in rows]


def _page(text: str) -> str:
    m = re.match(r"\[[^\]]*p\.(\d+)", text or "")
    return m.group(1) if m else "?"


def build(n: int = 40, seed: int = 7, model: str = "") -> Dict[str, Any]:
    """A golden set from the corpus: n answerable questions (one per sampled passage) and the off-topic ones."""
    client = mc.make_client("chat")
    model = model or mc.DEFAULT_RERANK_MODEL
    parents = [p for p in _parents() if len(p["text"]) >= 400]
    random.Random(seed).shuffle(parents)
    items: List[Dict[str, Any]] = []
    for p in parents:
        if len(items) >= n:
            break
        prompt = _ASK.format(source=os.path.basename(p["source"]), page=_page(p["text"]), text=p["text"][:2500])
        try:
            r = client.chat(model=model, messages=[{"role": "user", "content": prompt}], format="json",
                            options={"temperature": 0.2, "num_predict": 200})
            got = json.loads(r["message"]["content"])
            q = str(got.get("question", "")).strip()
        except Exception:  # noqa: BLE001 - a passage the model could not ask about is skipped
            continue
        if len(q) < 15 or q.lower().startswith(("what does this", "according to the passage")):
            continue
        items.append({"question": q, "answer": got.get("answer", ""), "kind": "answerable",
                      "source": p["source"], "parent_index": p["parent_index"]})
    items += [{"question": q, "kind": "off_topic"} for q in OFF_TOPIC + NEAR_OFF_TOPIC]
    os.makedirs(EVAL_DIR, exist_ok=True)
    out = {"built_at": time.time(), "model": model, "seed": seed, "items": items}
    with open(GOLDEN, "w") as fh:
        json.dump(out, fh, indent=1)
    return {"answerable": sum(i["kind"] == "answerable" for i in items), "off_topic": len(OFF_TOPIC) + len(NEAR_OFF_TOPIC), "path": GOLDEN}


def _rank(hits: List[Dict[str, Any]], item: Dict[str, Any]) -> Optional[int]:
    for i, h in enumerate(hits):
        meta = h.get("metadata") or h
        if meta.get("source") == item["source"] and meta.get("parent_index") is not None \
                and int(meta["parent_index"]) == int(item["parent_index"]):
            return i + 1
    return None


def run(search, items: Optional[List[Dict[str, Any]]] = None, k: int = 5) -> Dict[str, Any]:
    """Score `search(question, k) -> {"hits": [...], "abstained": bool}` on the golden set."""
    if items is None:
        with open(GOLDEN) as fh:
            items = json.load(fh)["items"]
    ans = [i for i in items if i["kind"] == "answerable"]
    off = [i for i in items if i["kind"] == "off_topic"]
    ranks, lat, wrong_abstain, per, rel_ans, rel_off = [], [], 0, [], [], []
    for it in ans:
        t0 = time.time()
        got = search(it["question"], k)
        lat.append(time.time() - t0)
        r = None if got["abstained"] else _rank(got["hits"], it)
        wrong_abstain += got["abstained"]
        ranks.append(r)
        per.append({"q": it["question"], "rank": r, "abstained": got["abstained"]})
        if got.get("best_relevance") is not None:
            rel_ans.append(got["best_relevance"])
    abstained_off = 0
    for it in off:
        t0 = time.time()
        got = search(it["question"], k)
        lat.append(time.time() - t0)
        abstained_off += got["abstained"]
        if got.get("best_relevance") is not None:
            rel_off.append(got["best_relevance"])
    n = max(1, len(ans))
    lat.sort()
    return {
        "answerable": len(ans), "off_topic": len(off),
        "hit@1": round(sum(1 for r in ranks if r == 1) / n, 3),
        "hit@5": round(sum(1 for r in ranks if r and r <= k) / n, 3),
        "mrr": round(sum(1 / r for r in ranks if r) / n, 3),
        "ndcg@5": round(sum(1 / math.log2(r + 1) for r in ranks if r and r <= k) / n, 3),
        "abstains_when_it_should": round(abstained_off / max(1, len(off)), 3),
        "abstains_when_it_should_not": round(wrong_abstain / n, 3),
        "latency_p50_s": round(lat[len(lat) // 2], 2) if lat else None,
        "latency_p95_s": round(lat[int(len(lat) * 0.95) - 1], 2) if len(lat) > 1 else None,
        "misses": [p["q"] for p in per if p["rank"] is None][:10],
        # what the gate threshold is chosen from: the best relevance on questions the corpus answers vs not
        "best_relevance_answerable": sorted(round(x, 3) for x in rel_ans),
        "best_relevance_off_topic": sorted(round(x, 3) for x in rel_off),
    }


def _searchers() -> Dict[str, Any]:
    """The retriever as it was (no gate) and as it is now, on the same index."""
    from .retriever import HybridRetriever
    r = HybridRetriever()

    def baseline(q, k):
        return {"hits": r.search(q, top_k=k, gate=False, corrective=False), "abstained": False}

    def production(q, k):
        out = r.retrieve(q, top_k=k)
        return {"hits": out["hits"], "abstained": out["abstained"], "best_relevance": out["best_relevance"]}
    return {"baseline": baseline, "production": production}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="canah.knowledge.rag.evaluate")
    ap.add_argument("command", choices=["build", "run"])
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--compare", action="store_true", help="also score the retriever without the production stages")
    a = ap.parse_args(argv)
    if a.command == "build":
        print(json.dumps(build(a.n, a.seed), indent=1))
        return 0
    s = _searchers()
    out = {"production": run(s["production"])}
    if a.compare:
        out["baseline"] = run(s["baseline"])
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
