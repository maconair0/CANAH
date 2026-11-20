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
        model = ScriptedModel(script=[call("controller_write", controller="optical", tool="create_lightpath",
                                           arguments={"name": "LP-3", "a": "xpdr-c2", "z": "xpdr-b2", "rate_gbps": 100}),
                                      say("Done: LP-3 is in service.")])
        agent = build_agent(load_settings(path="/x", home=home, sandbox="none"),
                            [AgenticMCPProvider(service=AgenticMCP(self.emu.url))], model=model,
                            memory=open_memory(in_memory=True))
        self.addCleanup(agent.close)
        admin = Identity("op", "admin")
        r = agent.ask("create LP-3", thread_id="x", who=admin)
        self.assertEqual(r.pending[0]["name"], "controller_write")
        self.assertNotIn("LP-3", self.emu.domains["optical"].state["lightpaths"])
        agent.decide("x", [approval.decision("approve")], who=admin)
        self.assertIn("LP-3", self.emu.domains["optical"].state["lightpaths"])
        self.assertEqual([w["tool"] for w in self.emu.domains["optical"].writes], ["create_lightpath"])


if __name__ == "__main__":
    unittest.main()
