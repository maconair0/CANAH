"""Digital twins behind the canah.twin/1 contract: the registry, a twin over HTTP (examples/custom_twin.py),
a twin answering outside the contract, the agent's tools, switching twins, and the gate on controller writes."""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from canah import FunctionProvider, build_agent, load_settings
from canah.identity import Accounts
from canah.memory import open_memory
from canah.service import create_app
from canah.twins import contract as C
from canah.twins.registry import Registry
from tests_canah.fakes import ScriptedModel, call, say

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "examples"))
import custom_twin  # noqa: E402

SENT = []


class Toy:
    """An in-process twin: holds every write to `ctl__set_rate` above 100 (reject), lets others go."""

    def describe(self):
        return {"contract": C.CONTRACT, "name": "toy", "version": "1",
                "domains": [{"domain": "toy", "answers": "anything", "kpis": ["x"], "example": {"domain": "toy"}}],
                "capabilities": ["validate", "predict", "check_write", "extras"],
                "extras": [{"name": "hello", "description": "says hello"}]}

    def validate(self, sc):
        return {"ok": True, "problems": []}

    def predict(self, sc):
        return {"contract": C.CONTRACT, "ok": True, "twin": {"name": "toy"}, "domain": "toy", "run_id": "r1",
                "decision": {"action": "met", "reasons": ["toy"]}, "probability": {"p": 1.0, "interval": [0.9, 1.0], "members": 10},
                "trust": {"tier": "B", "why": "toy"}, "figures": [{"entity": "e", "kpi": "x", "changed": {"p50": 1.23456789}}]}

    def extra(self, name, args):
        return {"hello": args.get("who", "world")}

    def check_write(self, controller, tool, arguments, answers):
        rate = float(arguments.get("rate") or 0)
        action = "reject" if rate > 100 else "propose"
        return {"check_id": f"c{rate:g}", "controller": controller, "tool": tool, "action": action, "arguments": arguments,
                "reasons": [f"rate {rate:g}"], "next": "do it" if action == "propose" else "do not",
                "change_ran": {"rate": rate}}


class Bad:
    def describe(self):
        return {"name": "bad", "domains": [{"domain": "x"}], "capabilities": ["validate", "predict"]}

    def validate(self, sc):
        return {"ok": "maybe"}

    def predict(self, sc):
        return {"decision": {"action": "go for it"}}


TOY, BAD = Toy(), Bad()


def set_rate(rate: float) -> str:
    """Set the rate."""
    SENT.append(rate)
    return f"rate is {rate}"


class Twins(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = custom_twin.serve(0)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        SENT.clear()
        os.environ["CANAH_ADMIN_PASSWORD"] = "adm1n"
        os.environ["CANAH_ROOT_PASSWORD"] = "r00t"
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.declared = [{"name": "toy", "kind": "python", "spec": "tests_canah.test_twins:TOY"},
                         {"name": "bad", "kind": "python", "spec": "tests_canah.test_twins:BAD"}]

    def agent(self, *script):
        p = FunctionProvider()
        p.add(set_rate, write=True, name="ctl__set_rate")
        model = ScriptedModel(script=list(script))
        a = build_agent(load_settings(path="/x", home=self.home, sandbox="none", twins=self.declared, twin_active="toy"),
                        [p], model=model, memory=open_memory(in_memory=True))
        self.addCleanup(a.close)
        return a, model

    # -- the registry and the contract
    def test_a_twin_over_http_answers_in_the_contract(self):
        reg = Registry(Path(self.home), self.declared)
        reg.add({"name": "flow", "kind": "http", "url": self.url})
        t = reg.get("flow")
        self.assertEqual(t.describe().domains[0].domain, "flow")
        r = t.predict(custom_twin.EXAMPLE)
        self.assertEqual(r.decision.action, "reject")          # acme and globex do not fit the detour
        self.assertFalse(t.can_check_writes())

    def test_an_answer_outside_the_contract_is_the_twins_error(self):
        reg = Registry(Path(self.home), self.declared)
        with self.assertRaisesRegex(ValueError, "outside the canah.twin/1 contract"):
            reg.get("bad").predict({})
        st = {x["name"]: x for x in reg.status()}
        self.assertTrue(st["toy"]["ok"])
        self.assertTrue(st["bad"]["ok"])                         # its description happens to fit

    def test_what_may_be_added_and_removed(self):
        reg = Registry(Path(self.home), self.declared)
        for bad in ({"name": "X y", "url": "http://a"}, {"name": "ok", "url": "ftp://a"},
                    {"name": "ok", "url": "http://a", "token_env": "s3cret-token"}, {"name": "toy", "url": "http://a"}):
            with self.assertRaises(ValueError):
                reg.add(bad)
        reg.add({"name": "lab", "kind": "http", "url": "http://127.0.0.1:1", "token_env": "LAB_TWIN_TOKEN"})
        saved = json.loads(reg.path.read_text())["added"][0]
        self.assertEqual(saved["token_env"], "LAB_TWIN_TOKEN")                # the variable's name, nothing else
        with self.assertRaises(ValueError):
            reg.remove("toy")                                    # declared in the settings
        reg.set_active("lab")
        reg.remove("lab")
        self.assertEqual(reg.active_name(), "toy")

    # -- the agent's tools, and switching twins without a restart
    def test_the_tools_use_whichever_twin_is_active(self):
        a, _ = self.agent(call("twin_list"), say("ok"), call("twin_extra", name="hello", arguments={"who": "you"}), say("ok"),
                          call("twin_predict", scenario=custom_twin.EXAMPLE), say("rejected"))
        r = a.ask("which twin?", "t1")
        self.assertEqual(json.loads(r.tool_calls[0]["result"])["in_use"], "toy")
        r = a.ask("hello?", "t2")
        self.assertEqual(json.loads(r.tool_calls[0]["result"]), {"hello": "you"})
        a.twins.add({"name": "flow", "kind": "http", "url": self.url})
        a.twins.set_active("flow")                               # the same agent: no rebuild
        r = a.ask("does acme fit?", "t3")
        got = json.loads(r.tool_calls[0]["result"])
        self.assertEqual((got["twin"]["name"], got["decision"]["action"]), ("flow-example", "reject"))

    # -- the gate on controller writes
    @staticmethod
    def tool_texts(a, thread, who):
        msgs = a.graph.get_state(a.config(thread, who)).values.get("messages", [])
        return " ".join(str(m.content) for m in msgs if m.type == "tool")

    def send_write(self, a, thread, text):
        from canah.identity import Identity
        root = Identity("root", "root")
        r = a.ask(text, thread, who=root)
        for _ in range(3):
            if not r.pending:
                break
            r = a.decide(thread, [{"type": "approve"}], who=root)
        return self.tool_texts(a, thread, root)

    def test_a_write_the_twin_rejects_is_held_until_an_operator_overrides_it(self):
        a, model = self.agent(call("ctl__set_rate", rate=200), say("held"))
        said = self.send_write(a, "g1", "set it to 200")
        self.assertEqual(SENT, [])
        self.assertIn("Not sent: the digital twin 'toy' holds this write (reject)", said)
        a.twin_checks.override("c200", "maintenance window", "root")
        model.script[:] = [call("ctl__set_rate", rate="200.0"), say("sent")]    # the same change, written differently
        said = self.send_write(a, "g2", "send it now")
        self.assertEqual(SENT, ["200.0"] if isinstance(SENT[0], str) else [200.0])
        self.assertIn("overridden by root: maintenance window", said)

    def test_a_write_the_twin_supports_goes_with_a_note(self):
        a, _ = self.agent(call("ctl__set_rate", rate=50), say("done"))
        said = self.send_write(a, "g3", "set it to 50")
        self.assertEqual(SENT, [50])
        self.assertIn("Checked in the digital twin 'toy' first: check c50, propose", said)

    # -- the service
    def test_the_twins_page_api(self):
        a, _ = self.agent()
        client = TestClient(create_app(a, Accounts()))
        got = client.get("/twins").json()
        self.assertEqual((got["active"], [t["name"] for t in got["twins"]]), ("toy", ["toy", "bad"]))
        self.assertEqual(client.put("/twins/active", json={"name": "bad"}).status_code, 403)     # observer
        h = {"Authorization": "Bearer " + client.post("/session", json={"username": "admin", "password": "adm1n"}).json()["token"]}
        r = client.post("/twins", headers=h, json={"name": "flow", "kind": "http", "url": self.url})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["status"]["ok"])
        self.assertTrue(client.post("/twins/flow/test", headers=h).json()["ok"])
        self.assertEqual(client.put("/twins/active", headers=h, json={"name": "flow"}).json()["active"], "flow")
        self.assertEqual(client.delete("/twins/flow", headers=h).status_code, 403)               # root only
        hr = {"Authorization": "Bearer " + client.post("/session", json={"username": "root", "password": "r00t"}).json()["token"]}
        self.assertEqual(client.delete("/twins/flow", headers=hr).json()["active"], "toy")


if __name__ == "__main__":
    unittest.main()
