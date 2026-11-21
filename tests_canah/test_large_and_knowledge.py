"""Large tool results are kept whole and previewed; passages say what they do not mention."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from canah import FunctionProvider, build_agent, load_settings
from canah.identity import Identity
from canah.knowledge import KnowledgeProvider, named_terms, render
from canah.memory import open_memory

from tests_canah.fakes import ScriptedModel, call, say

BIG = json.dumps([{"name": f"node-{i}", "kind": "a" if i % 3 else "b"} for i in range(900)])


def list_nodes() -> str:
    """List every node."""
    return BIG


class LargeResults(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)

    def test_a_large_result_is_kept_whole_and_the_model_told(self):
        p = FunctionProvider()
        p.add(list_nodes)
        model = ScriptedModel(script=[call("list_nodes"), say("The list is kept in full; I will count it there.")])
        a = build_agent(load_settings(path="/x", home=self.home, sandbox="none"), [p], model=model,
                        memory=open_memory(in_memory=True))
        self.addCleanup(a.close)
        a.ask("how many nodes?", who=Identity("ana", "admin"))
        shown = model.tool_results()[-1]
        self.assertLess(len(shown), 3600)                       # a 2,500-character preview and its note
        self.assertIn(f"of {len(BIG)} characters", shown)
        self.assertIn("incomplete", shown)
        kept = list((Path(self.home) / "results").glob("*.txt"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_text(), BIG)

    def test_the_model_can_read_the_kept_result(self):
        p = FunctionProvider()
        p.add(list_nodes)
        model = ScriptedModel(script=[call("list_nodes"), say("placeholder")])
        a = build_agent(load_settings(path="/x", home=self.home, sandbox="none"), [p], model=model,
                        memory=open_memory(in_memory=True))
        self.addCleanup(a.close)
        a.ask("nodes?", who=Identity("ana", "admin"))
        path = model.tool_results()[-1].split("The full result is in ")[1].split(":")[0]
        model.script[:] = [call("read_file", file_path=path, offset=0, limit=5), say("ok")]
        a.ask("read it", who=Identity("ana", "admin"))
        self.assertIn("node-0", model.tool_results()[-1])

    def test_a_small_result_is_untouched(self):
        def small() -> str:
            """Small."""
            return "fine"
        p = FunctionProvider()
        p.add(small)
        model = ScriptedModel(script=[call("small"), say("fine")])
        a = build_agent(load_settings(path="/x", home=self.home, sandbox="none"), [p], model=model,
                        memory=open_memory(in_memory=True))
        self.addCleanup(a.close)
        a.ask("x", who=Identity("ana", "admin"))
        self.assertEqual(model.tool_results()[-1], "fine")


class Shape(unittest.TestCase):
    def test_labels_are_summarised_and_ids_and_numbers_are_not(self):
        from canah.large_results import shape
        data = json.dumps({"items": [{"id": f"x-{i}", "kind": "A" if i % 3 else "B", "loss": str(1.5 + i % 2)}
                                     for i in range(30)]})
        got = shape(data)
        self.assertIn("kind: A 20, B 10", got)
        self.assertNotIn("id:", got)
        self.assertNotIn("loss:", got)
        self.assertEqual(shape("not json"), "")

    def test_the_summary_reaches_the_model(self):
        p = FunctionProvider()
        p.add(list_nodes)
        model = ScriptedModel(script=[call("list_nodes"), say("ok")])
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        a = build_agent(load_settings(path="/x", home=home, sandbox="none"), [p], model=model,
                        memory=open_memory(in_memory=True))
        self.addCleanup(a.close)
        a.ask("kinds?", who=Identity("ana", "admin"))
        self.assertIn("kind: a 600, b 300", model.tool_results()[-1])


class Passages(unittest.TestCase):
    def test_named_terms(self):
        self.assertEqual(named_terms("What threshold does Spec-X2 set for eMode? Cite the source."), ["Spec-X2", "eMode"])
        self.assertEqual(named_terms("how does a cache decide what to keep"), [])
        self.assertNotIn("1200", named_terms("Can I use VLAN 1200 for a new customer service?")) # a value, not a name

    def test_a_passage_about_something_else_is_marked(self):
        text = render([{"text": "a threshold of 4e-3 triggers reconfiguration", "source": "x.pdf",
                        "metadata": {"page_start": 2}}], 300, "What threshold does Spec-X2 set?")
        self.assertIn("does not mention: Spec-X2", text)
        self.assertTrue(text.startswith("Note: no passage below mentions"))

    def test_a_passage_about_it_is_not(self):
        text = render([{"text": "Spec-X2 sets the threshold at 2e-2.", "source": "s.pdf", "metadata": {}}], 300,
                      "What threshold does Spec-X2 set?")
        self.assertNotIn("does not mention", text)
        self.assertFalse(text.startswith("Note:"))

    def test_the_tool_passes_the_question_through(self):
        class R:
            def search(self, q, top_k=5):
                return [{"text": "unrelated value 4e-3", "source": "x.pdf", "metadata": {}}]
        out = KnowledgeProvider(retriever=R()).tools()[0].invoke({"query": "What does Spec-X2 allow?"})
        self.assertIn("does not mention: Spec-X2", out)

    def test_a_keyword_search_that_finds_nothing_tries_the_operators_question(self):
        class R:
            asked = []

            def retrieve(self, q, top_k=5):
                self.asked.append(q)
                if "rules" in q:
                    return {"abstained": True, "best_relevance": 0.01, "near_misses": [], "rewrites": []}
                return {"abstained": False, "hits": [{"text": "VLAN ids 1000 and above are reserved", "source": "rules.md",
                                                      "metadata": {}}]}
        model = ScriptedModel(script=[call("search_knowledge", query="VLAN design rules 1200"), say("Reserved [rules.md].")])
        a = build_agent(load_settings(path="/x", sandbox="none"), [KnowledgeProvider(retriever=R())], model=model,
                        memory=open_memory(in_memory=True))
        a.ask("Can I use VLAN 1200 for a customer?", who=Identity("ana", "admin"))
        self.assertEqual(R.asked, ["VLAN design rules 1200", "Can I use VLAN 1200 for a customer?"])
        self.assertIn("1000 and above are reserved", model.tool_results()[-1])


if __name__ == "__main__":
    unittest.main()
