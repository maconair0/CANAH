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
            return [{"name": "get_nodes", "description": "List the nodes.", "write": False, "verb": "read_devices",
                     "schema": {"type": "object", "properties": {}}},
                    {"name": "create_service", "description": "Create a service.", "write": True, "verb": "provision",
                     "schema": {"type": "object", "properties": {"a": {"type": "string"}, "z": {"type": "string"}},
                                "required": ["a", "z"]}}]
        return [{"name": "list_items", "description": "List items.", "write": False, "verb": "read_devices",
                 "schema": {"type": "object", "properties": {}}},
                {"name": "set_meter", "description": "Set a meter on a port.", "write": True, "verb": "",
                 "schema": {"type": "object", "properties": {"port": {"type": "string"}}, "required": ["port"]}}]

    def call(self, key, tool, arguments, approved_by=""):
        self.calls.append((key, tool, dict(arguments), approved_by))
        if tool == "get_nodes":
            return {"text": '["node-1", "node-2"]', "is_error": False}
        if tool == "list_items":
            return {"text": '["item-1"]', "is_error": False}
        return {"text": f"{tool} done", "is_error": False}

    def get(self, path, timeout=None):
        if path == "/contract":
            return {"contract": {"version": 1, "domains": [
                {"name": "inventory", "verbs": {"read_devices": {"methods": ["GET"], "help": "what is in the network"}}},
                {"name": "provisioning", "verbs": {"provision": {"methods": ["POST"], "writes": True}}}]}}
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
    def test_the_supervisor_reads_and_its_team_changes(self):
        a = self.agent(say("ok"))
        self.assertNotIn("alpha", a.info["subagents"])            # the controllers are the agentic MCP's
        self.assertTrue({"qot", "domain-a-config", "domain-b-config"} <= set(a.info["subagents"]))
        roles = {d["name"]: d for d in a.info["subagent_detail"]}
        self.assertEqual((roles["domain-a-config"]["kind"], roles["domain-a-config"]["controllers"]), ("config", ["alpha"]))
        self.assertEqual(roles["qot"]["kind"], "qot")
        a.ask("hi", who=ADMIN)
        main = self.model.offered[-1]
        for t in ("network_controllers", "net_read_devices", "network_tools", "controller_read"):
            self.assertIn(t, main)
        for t in ("net_provision", "controller_write"):
            self.assertNotIn(t, main)                                 # the supervisor changes nothing itself
        self.assertEqual(sorted(a.writes), ["controller_write", "net_provision"])

    def test_each_config_agent_sees_its_own_domain_only(self):
        p = AgenticMCPProvider(service=self.service)
        agents = {x["name"]: {t.name: t for t in x["tools"]} for x in p.subagents()}
        self.assertIn("net_provision", agents["domain-a-config"])
        self.assertNotIn("net_provision", agents["domain-b-config"])  # beta serves no provision
        out = agents["domain-b-config"]["net_read_devices"].invoke({}, config={"configurable": {}})
        self.assertIn("beta", out)
        self.assertNotIn("alpha", out)
        refused = agents["domain-b-config"]["controller_read"].invoke({"controller": "alpha", "tool": "get_nodes"})
        self.assertIn("has no tool", refused)

    def test_match_keeps_only_the_objects_asked_about(self):
        from canah.network import filter_answer
        text = '{"nodes": [{"node-id": "xpdr-b2", "site": "site-b"}, {"node-id": "roadm-a"}, {"node-id": "xpdr-c2"}], "count": 3}'
        got = filter_answer(text, "xpdr-b2, xpdr-c2")
        self.assertIn("xpdr-b2", got)
        self.assertIn("xpdr-c2", got)
        self.assertNotIn("roadm-a", got)
        self.assertIn('"count": 3', got)
        self.assertIn("Nothing", filter_answer(text, "amp-9"))
        self.assertEqual(filter_answer("Error: down", "x"), "Error: down")

    def test_a_match_that_misses_on_one_controller_says_where_it_is(self):
        p = AgenticMCPProvider(service=self.service)
        out = {t.name: t for t in p.tools()}["net_read_devices"].invoke({"controller": "beta", "match": "node-2"},
                                                                        config={"configurable": {}})
        self.assertIn("Found on alpha", out)
        self.assertIn("node-2", out)

    def test_the_supervisor_is_told_where_a_change_goes(self):
        p = AgenticMCPProvider(service=self.service)
        read = {t.name: t for t in p.tools()}["controller_read"]
        out = read.invoke({"controller": "alpha", "tool": "create_service", "arguments": {"a": "a1"}})
        self.assertIn("domain-a-config", out)
        self.assertEqual(self.service.calls, [])

    def test_a_read_without_a_controller_asks_every_one_that_serves_the_verb(self):
        a = self.agent(call("net_read_devices"), say("Alpha has two nodes, beta one item."))
        a.ask("what is in the network?", who=ADMIN)
        self.assertEqual(sorted(c[:2] for c in self.service.calls), [("alpha", "get_nodes"), ("beta", "list_items")])
        got = self.model.tool_results()[-1]
        self.assertIn('"alpha"', got)
        self.assertIn("node-2", got)

    def test_a_change_goes_through_the_domains_config_agent_and_waits(self):
        a = self.agent(call("task", description="create a service a1 to z1 on alpha", subagent_type="domain-a-config"),
                       call("net_provision", controller="alpha", arguments={"a": "a1", "z": "z1"}),
                       say("created"), say("Service created."))
        r = a.ask("create a service from a1 to z1", thread_id="w", who=ADMIN)
        self.assertTrue(r.waiting, r)
        self.assertEqual(r.pending[0]["name"], "net_provision")
        self.assertEqual(self.service.calls, [])
        a.decide("w", [approval.decision("approve")], who=ADMIN)
        writes = [c for c in self.service.calls if c[1] == "create_service"]
        self.assertEqual(writes, [("alpha", "create_service", {"a": "a1", "z": "z1"}, "ana (approved in canah)")])
        self.assertTrue(any(c[1] == "get_nodes" for c in self.service.calls))       # read back after the change
        report = [m for m in a.graph.get_state(a.config("w", ADMIN)).values["messages"]
                  if m.type == "tool" and m.name == "task"][-1].content
        self.assertIn("Writes sent in this task", report)                   # what was sent, from the harness
        self.assertIn('"a": "a1"', report)

    def test_a_tool_beyond_the_contract_through_the_config_agent(self):
        a = self.agent(call("task", description="put a meter on beta port 1", subagent_type="domain-b-config"),
                       call("network_tools", query="meter"),
                       call("controller_write", controller="beta", tool="set_meter", arguments={"port": "1"}),
                       say("Meter set."), say("Done."))
        r = a.ask("put a meter on beta port 1", thread_id="m", who=ADMIN)
        self.assertEqual(r.pending[0]["name"], "controller_write")
        a.decide("m", [approval.decision("approve")], who=ADMIN)
        self.assertIn(("beta", "set_meter", {"port": "1"}), [c[:3] for c in self.service.calls])

    def test_without_the_team_the_agent_does_it_all_itself(self):
        a = self.agent(call("net_provision", controller="alpha", arguments={"a": "a1", "z": "z1"}), say("ok"),
                       providers=[AgenticMCPProvider(service=self.service, team=False)])
        r = a.ask("create it", thread_id="solo", who=ADMIN)
        self.assertEqual(r.pending[0]["name"], "net_provision")
        self.assertFalse([n for n in a.info["subagents"] if n.endswith("-config")])

    def test_the_twin_gate_sees_the_controller_and_tool_behind_a_write(self):
        from canah.network import AgenticMCPProvider as P
        prov = P(service=self.service)
        self.assertEqual(prov.write_target("net_provision", {"controller": "alpha", "arguments": {"a": "1"}}),
                         ("alpha", "create_service", {"a": "1"}))
        self.assertEqual(prov.write_target("controller_write", {"controller": "beta", "tool": "set_meter", "arguments": {}}),
                         ("beta", "set_meter", {}))
        self.assertIsNone(prov.write_target("net_read_devices", {}))

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
