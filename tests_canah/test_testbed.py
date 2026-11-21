"""The test bed: its emulated agentic MCP behaves like the real one, and the agent works through it."""
import shutil
import tempfile
import unittest

import requests

from canah import approval, build_agent, load_settings
from canah.identity import Identity
from canah.memory import open_memory
from canah.network import AgenticMCP, AgenticMCPProvider
from testbed.emulator import Running

from tests_canah.fakes import ScriptedModel, call, say


class Emulator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.emu = Running()

    @classmethod
    def tearDownClass(cls):
        cls.emu.stop()

    def setUp(self):
        requests.post(self.emu.url + "/reset")

    def test_three_controllers_with_tools_and_write_flags(self):
        keys = [c["key"] for c in requests.get(self.emu.url + "/controllers").json()["controllers"]]
        self.assertEqual(keys, ["packet", "optical", "fabric"])
        tools = requests.get(self.emu.url + "/controllers/optical/tools").json()["tools"]
        self.assertTrue({t["name"]: t["write"] for t in tools}["create_lightpath"])

    def test_a_write_without_approval_is_refused_before_the_domain(self):
        got = requests.post(self.emu.url + "/controllers/packet/call",
                            json={"tool": "delete_service", "arguments": {"name": "VS-100"}}).json()
        self.assertTrue(got["is_error"])
        self.assertIn("VS-100", self.emu.domains["packet"].state["services"])

    def test_reset_puts_the_start_back(self):
        self.emu.domains["optical"].degrade("LP-1", 1.0)
        requests.post(self.emu.url + "/reset")
        self.assertEqual(self.emu.domains["optical"].state["lightpaths"]["LP-1"]["osnr_db"], 21.4)

    def test_the_agent_changes_a_domain_only_after_approval(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        model = ScriptedModel(script=[call("task", description="create LP-3", subagent_type="optical-config"),
                                      call("net_provision", controller="optical",
                                           arguments={"name": "LP-3", "a": "xpdr-c2", "z": "xpdr-b2", "rate_gbps": 100}),
                                      say("LP-3 created"), say("Done: LP-3 is in service.")])
        agent = build_agent(load_settings(path="/x", home=home, sandbox="none"),
                            [AgenticMCPProvider(service=AgenticMCP(self.emu.url))], model=model,
                            memory=open_memory(in_memory=True))
        self.addCleanup(agent.close)
        admin = Identity("op", "admin")
        r = agent.ask("create LP-3", thread_id="x", who=admin)
        self.assertEqual(r.pending[0]["name"], "net_provision")
        self.assertNotIn("LP-3", self.emu.domains["optical"].state["lightpaths"])
        agent.decide("x", [approval.decision("approve")], who=admin)
        self.assertIn("LP-3", self.emu.domains["optical"].state["lightpaths"])
        self.assertEqual([w["tool"] for w in self.emu.domains["optical"].writes], ["create_lightpath"])


if __name__ == "__main__":
    unittest.main()


class WhereItIs(unittest.TestCase):
    def test_a_write_refused_for_an_object_elsewhere_says_where_it_is(self):
        from canah.network import AgenticMCP, AgenticMCPProvider
        from testbed.emulator import Running
        e = Running()
        try:
            p = AgenticMCPProvider(service=AgenticMCP(e.url, token=""))
            tools = {t.name: t for t in next(a for a in p.subagents() if a["name"] == "packet-config")["tools"]}
            out = tools["net_provision"].invoke({"controller": "packet", "arguments": {
                "name": "X", "vlan": 301, "a": "border-leaf-1", "z": "pe-b:eth4"}}, config={"configurable": {}})
            self.assertIn("it is on fabric (changed by data-centre-config)", out)
            self.assertIn("only a value in this change", out)
        finally:
            e.stop()


class IpOverOptical(unittest.TestCase):
    def test_the_plan_names_the_missing_lightpath_and_the_ping_proves_the_chain(self):
        import json
        from canah.network import AgenticMCP, AgenticMCPProvider
        from testbed.emulator import Running
        e = Running()
        try:
            p = AgenticMCPProvider(service=AgenticMCP(e.url, token=""))
            tools = {t.name: t for t in p.tools()}
            self.assertTrue({"net_ping", "net_traceroute", "net_read_hosts", "ip_over_optical"} <= set(tools))
            self.assertNotIn("net_ping", p.writes())                                  # a probe, not a change
            ok = json.loads(tools["ip_over_optical"].invoke({"a": "site-a", "z": "site-b"}))
            self.assertTrue(ok["already"])
            e.domains["optical"].state["lightpaths"].pop("LP-1")
            ping = tools["net_ping"].invoke({"controller": "packet", "arguments": {"src": "h-a1", "dst": "10.0.2.10"}},
                                            config={"configurable": {}})
            self.assertIn("0 received", ping)
            self.assertIn("no lightpath", ping)
            plan = json.loads(tools["ip_over_optical"].invoke({"a": "pe-a", "z": "pe-b"}))
            first = plan["steps"][0]
            self.assertEqual((first["agent"], first["arguments"]["a"], first["arguments"]["z"]),
                             ("optical-config", "xpdr-a1", "xpdr-b1"))
            e.domains["optical"].state["lightpaths"]["LP-9"] = {"a": "xpdr-a1", "z": "xpdr-b1", "rate_gbps": 100,
                                                                "osnr_db": 22, "state": "in-service", "client": {}, "path": []}
            ping = tools["net_ping"].invoke({"controller": "packet", "arguments": {"src": "h-a1", "dst": "10.0.2.10"}},
                                            config={"configurable": {}})
            self.assertIn("3 received", ping)
        finally:
            e.stop()
