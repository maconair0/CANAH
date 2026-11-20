"""The production retrieval stages: one passage per parent before top_k, the relevance gate, corrective
retrieval judged against the original question, and what the agent reads when the documents say nothing."""
import unittest
from unittest import mock

from canah.knowledge import KnowledgeProvider, render
from canah.knowledge.rag import settings as mc
from canah.knowledge.rag.retriever import HybridRetriever


def hit(i, parent, text="a passage about the thing asked", source="doc.pdf"):
    return {"id": f"c{i}", "text": text, "source": source,
            "metadata": {"source": source, "parent_index": parent, "chunk_type": "child"}}


class FakeStore:
    def __init__(self, lists):
        self.lists, self.asked = lists, []

    def count(self):
        return 1

    def query_dense(self, q, n, where=None):
        self.asked.append(q)
        return self.lists.get(q, self.lists.get("*", []))

    def query_dense_many(self, qs, n, where=None):
        return [self.query_dense(q, n, where) for q in qs]


class FakeReranker:
    """Relevance by a table on (query, id); records what each call was judged against."""
    backend = "cross_encoder"
    model = "fake"

    def __init__(self, table):
        self.table, self.queries = table, []

    def rerank(self, query, cands, k):
        self.queries.append(query)
        for c in cands:
            c["relevance"] = self.table.get(c["id"], 0.0)
            c["rerank_score"] = c["relevance"]
        return sorted(cands, key=lambda c: c["relevance"], reverse=True)[:k]


def retriever(dense, table, rewrites=()):
    r = HybridRetriever.__new__(HybridRetriever)
    r.store, r.reranker = FakeStore(dense), FakeReranker(table)
    r.dense_top_n = r.sparse_top_n = 20
    r._where = lambda: None
    r.query_sparse = lambda q, n: []
    r._check_labelled = lambda: None
    r._admissible = lambda h: True
    r._passage_chars = lambda h: 800
    # each child expands to its parent's id, so two children of one parent are one result
    r._expand = lambda hits: list({(h["metadata"]["parent_index"]): dict(h, parent=h["metadata"]["parent_index"])
                                   for h in reversed(hits)}.values())[::-1]
    r._rewrite = lambda q: list(rewrites)
    return r


class Production(unittest.TestCase):
    def test_two_children_of_one_parent_do_not_crowd_out_a_third_result(self):
        hits = [hit(1, 0), hit(2, 0), hit(3, 1), hit(4, 2)]
        r = retriever({"*": hits}, {"c1": .9, "c2": .8, "c3": .7, "c4": .6})
        got = r.retrieve("q", top_k=3, corrective=False)["hits"]
        self.assertEqual([h["parent"] for h in got], [0, 1, 2])

    def test_passages_below_the_bar_are_not_evidence(self):
        r = retriever({"*": [hit(1, 0), hit(2, 1)]}, {"c1": .9, "c2": mc.MIN_RELEVANCE / 2})
        got = r.retrieve("q", top_k=5, corrective=False)
        self.assertFalse(got["abstained"])
        self.assertEqual([h["id"] for h in got["hits"]], ["c1"])

    def test_nothing_relevant_abstains_with_the_near_misses(self):
        r = retriever({"*": [hit(1, 0), hit(2, 1)]}, {"c1": .01, "c2": .0})
        got = r.retrieve("boiling point of ethanol", top_k=5, corrective=False)
        self.assertTrue(got["abstained"])
        self.assertEqual(got["hits"], [])
        self.assertEqual(got["near_misses"][0]["id"], "c1")

    def test_corrective_pass_finds_it_and_is_judged_against_the_original_question(self):
        dense = {"original": [hit(1, 0)], "rewrite one": [hit(9, 5)]}
        r = retriever(dense, {"c1": .01, "c9": .95}, rewrites=["rewrite one"])
        got = r.retrieve("original", top_k=5, corrective=True)
        self.assertFalse(got["abstained"])
        self.assertEqual(got["hits"][0]["id"], "c9")
        self.assertEqual(got["rewrites"], ["rewrite one"])
        self.assertEqual(set(r.reranker.queries), {"original"})

    def test_ungated_search_keeps_the_old_behaviour(self):
        r = retriever({"*": [hit(1, 0)]}, {"c1": .0})
        self.assertEqual(len(r.search("q", top_k=5, gate=False, corrective=False)), 1)


class WhatTheAgentReads(unittest.TestCase):
    class R:
        def __init__(self, got):
            self.got = got

        def search(self, q, top_k=5):
            return self.got["hits"]

        def retrieve(self, q, top_k=5):
            return dict(self.got, min_relevance=0.1)

    def tool(self, got):
        return {t.name: t for t in KnowledgeProvider(retriever=self.R(got)).tools()}["search_knowledge"]

    def test_an_abstention_says_the_documents_do_not_cover_it(self):
        out = self.tool({"hits": [], "abstained": True, "best_relevance": 0.004,
                         "near_misses": [hit(1, 0, source="paper.pdf")],
                         "rewrites": ["port 830 is the NETCONF default"]}).invoke({"query": "NETCONF port?"})
        self.assertIn("NOT IN THE DOCUMENTS", out)
        self.assertIn("[paper.pdf]", out)
        self.assertIn("1 rewritten", out)
        self.assertNotIn("830", out)      # a hypothetical answer never reads as evidence

    def test_hits_show_their_relevance(self):
        h = dict(hit(1, 0), relevance=0.93)
        out = self.tool({"hits": [h], "abstained": False}).invoke({"query": "thing"})
        self.assertIn("relevance 0.93", out)

    def test_a_plain_retriever_still_works(self):
        class Plain:
            def search(self, q, top_k=5):
                return [hit(1, 0)]
        out = {t.name: t for t in KnowledgeProvider(retriever=Plain()).tools()}["search_knowledge"].invoke({"query": "x"})
        self.assertIn("[doc.pdf]", out)
        self.assertEqual(render([], 100), "The corpus holds nothing on this. Say so rather than answering from memory.")


class GpuFallback(unittest.TestCase):
    def test_a_full_gpu_moves_the_reranker_to_the_cpu_instead_of_failing(self):
        from canah.knowledge.rag import reranker as rr

        class Full:
            def predict(self, pairs, show_progress_bar=False):
                raise RuntimeError("CUDA out of memory. Tried to allocate 20.00 MiB")

        class Cpu:
            def predict(self, pairs, show_progress_bar=False):
                return [3.0 for _ in pairs]

        r = rr.Reranker.__new__(rr.Reranker)
        r.backend, r.model, r.device, r._cross_encoder = "cross_encoder", "m", "cuda", Full()

        def to_cpu():
            r._cross_encoder, r.device = Cpu(), "cpu"
        with mock.patch.object(r, "_to_cpu", side_effect=to_cpu):
            got = r._rerank_cross_encoder("q", [{"text": "t"}], 1)
        self.assertEqual(r.device, "cpu")
        self.assertGreater(got[0]["relevance"], 0.9)

    def test_other_errors_are_not_hidden(self):
        from canah.knowledge.rag import reranker as rr

        class Broken:
            def predict(self, pairs, show_progress_bar=False):
                raise ValueError("bad input")
        r = rr.Reranker.__new__(rr.Reranker)
        r.backend, r.model, r.device, r._cross_encoder = "cross_encoder", "m", "cuda", Broken()
        with self.assertRaises(ValueError):
            r._rerank_cross_encoder("q", [{"text": "t"}], 1)


if __name__ == "__main__":
    unittest.main()
