"""The agentic MCP provider and the knowledge provider, on fakes."""
import shutil
import tempfile
import unittest

from canah import approval, build_agent, load_settings
from canah.identity import Identity
from canah.knowledge import KnowledgeProvider
from canah.memory import open_memory
from canah.network import AgenticMCPProvider

from tests_canah.fakes import ScriptedModel, call, say

ADMIN = Identity("ana", "admin")


class FakeAgenticMCP:
    url = "http://fake:8400"

    def __init__(self):
        self.calls = []

    def controllers(self):
        return [{"key": "alpha", "label": "Alpha", "domain": "domain-a", "tools": 2,
                 "conformance": {"verdict": "accepted", "domains": [{"domain": "inventory", "status": "covered"}]}},
                {"key": "beta", "label": "Beta", "domain": "domain-b", "tools": 1}]

    def tools(self, key):
        if key == "alpha":
            return [{"name": "get_nodes", "description": "List the nodes.", "write": False,
                     "schema": {"type": "object", "properties": {}}},
                    {"name": "create_service", "description": "Create a service.", "write": True,
                     "schema": {"type": "object", "properties": {"a": {"type": "string"}, "z": {"type": "string"}},
                                "required": ["a", "z"]}}]
        return [{"name": "list_items", "description": "List items.", "write": False,
                 "schema": {"type": "object", "properties": {}}}]

    def call(self, key, tool, arguments, approved_by=""):
        self.calls.append((key, tool, dict(arguments), approved_by))
        if tool == "get_nodes":
            return {"text": '["node-1", "node-2"]', "is_error": False}
        return {"text": f"{tool} done", "is_error": False}

    def get(self, path, timeout=None):
        return {"path": path}


class FakeRetriever:
    def search(self, query, top_k=5):
        return [{"text": "Spec-X sets the error limit at 2e-2.", "source": "/c/spec-x.pdf",
                 "metadata": {"page_start": 12}, "score": 0.93}][:top_k]

    def sources(self):
        return [{"source": "spec-x.pdf", "chunks": 40}]


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.service = FakeAgenticMCP()

    def agent(self, *script, providers=None):
        self.model = ScriptedModel(script=list(script))
        a = build_agent(load_settings(path="/x", home=self.home),
                        providers if providers is not None else [AgenticMCPProvider(service=self.service),
                                                                  KnowledgeProvider(retriever=FakeRetriever())],
                        model=self.model, memory=open_memory(in_memory=True))
        self.addCleanup(a.close)
        return a


class Network(Base):
    def test_a_subagent_per_controller_and_their_tools_stay_off_the_main_agent(self):
        a = self.agent(say("ok"))
        self.assertIn("alpha", a.info["subagents"])
        self.assertIn("beta", a.info["subagents"])
        a.ask("hi", who=ADMIN)
        main = self.model.offered[-1]
        self.assertIn("network_controllers", main)
        self.assertNotIn("alpha__get_nodes", main)
        self.assertEqual(a.writes, ["alpha__create_service"])

    def test_a_read_goes_through_the_controllers_subagent(self):
        a = self.agent(call("task", description="list the nodes", subagent_type="alpha"),
                       call("alpha__get_nodes"), say("node-1 and node-2"), say("Two nodes."))
        r = a.ask("which nodes are there?", who=ADMIN)
        self.assertEqual(r.text, "Two nodes.")
        self.assertEqual(self.service.calls, [("alpha", "get_nodes", {}, "")])

    def test_a_write_waits_and_carries_the_approvers_name(self):
        a = self.agent(call("task", description="create a service a1 to z1", subagent_type="alpha"),
                       call("alpha__create_service", a="a1", z="z1"), say("created"), say("Service created."))
        r = a.ask("create a service from a1 to z1", thread_id="w", who=ADMIN)
        self.assertTrue(r.waiting, r)
        self.assertEqual(r.pending[0]["name"], "alpha__create_service")
        self.assertEqual(self.service.calls, [])
        a.decide("w", [approval.decision("approve")], who=ADMIN)
        self.assertEqual(self.service.calls, [("alpha", "create_service", {"a": "a1", "z": "z1"},
                                               "ana (approved in canah)")])

    def test_an_unreachable_agentic_mcp_still_lets_the_agent_start(self):
        class Down(FakeAgenticMCP):
            def controllers(self):
                raise ConnectionError("refused")
        p = AgenticMCPProvider(service=Down())
        a = self.agent(say("The network is out of reach."), providers=[p])
        self.assertFalse(p.status["ok"])
        a.ask("hi")
        self.assertIn("not answering", self.model.system_text())


class Knowledge(Base):
    def test_a_quick_fact_is_cited(self):
        a = self.agent(call("search_knowledge", query="Spec-X error limit"), say("2e-2 [spec-x.pdf p.12]"))
        a.ask("what is the Spec-X error limit?")
        self.assertIn("[spec-x.pdf p.12]", " ".join(self.model.tool_results()))

    def test_the_librarian_reads_deeper_and_the_main_agent_gets_its_answer(self):
        a = self.agent(call("task", description="what error limit does Spec-X set", subagent_type="librarian"),
                       call("research_knowledge", query="Spec-X error limit"),
                       say("An error limit of 2e-2 [spec-x.pdf p.12]."), say("2e-2, per Spec-X."))
        r = a.ask("explain the Spec-X error limit")
        self.assertEqual(r.text, "2e-2, per Spec-X.")
        self.assertIn("librarian", a.info["subagents"])
        main = self.model.offered[0]
        self.assertIn("search_knowledge", main)
        self.assertNotIn("research_knowledge", main)

    def test_an_invented_citation_is_sent_back_to_search(self):
        a = self.agent(say("It is 1e-3 [Spec-X Standard p.12]."),           # cites what no tool returned
                       call("search_knowledge", query="Spec-X error limit"),
                       say("2e-2 [spec-x.pdf p.12]."))
        r = a.ask("what is the Spec-X error limit?")
        self.assertEqual(r.text, "2e-2 [spec-x.pdf p.12].")
        self.assertTrue(any("[citation check]" in str(m.content) for m in self.model.seen[1]))

    def test_a_citation_a_tool_returned_passes(self):
        a = self.agent(call("search_knowledge", query="x"), say("2e-2 [spec-x.pdf p.12]."))
        self.assertEqual(a.ask("limit?").text, "2e-2 [spec-x.pdf p.12].")
        self.assertEqual(len(self.model.seen), 2)

    def test_nothing_found_is_said(self):
        class Empty(FakeRetriever):
            def search(self, query, top_k=5):
                return []
        p = KnowledgeProvider(retriever=Empty())
        self.assertIn("holds nothing", p.tools()[0].invoke({"query": "x"}))


if __name__ == "__main__":
    unittest.main()
