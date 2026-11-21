"""Operations: incidents, the watcher, runbooks, the networks' history, notifications, scopes, canah_status."""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from canah.history import History, diff, services_of
from canah.identity import Accounts, Identity
from canah.incidents import Incidents, Watcher, alarms_of, load_runbooks, match, BUILTIN_RUNBOOKS
from canah.notify import Notifier


class FakeService:
    """An agentic MCP whose controllers serve services and alarms through contract verbs."""
    url = "http://fake"

    def __init__(self):
        self.services = {"opt": [{"id": "LP1", "status": "inService"}, {"id": "LP2", "status": "outOfService"}]}
        self.alarms = {"opt": [{"id": "A1", "severity": "critical", "description": "loss of signal", "node-id": "roadm-1"}]}

    def tools(self, key):
        return [{"name": f"{key}_services", "verb": "read_services"}, {"name": f"{key}_alarms", "verb": "read_alarms"}]

    def get(self, path, timeout=None):
        return {"sessions": [{"controller": "pkt", "state": "down", "last_error": "refused"},
                             {"controller": "opt", "state": "degraded", "last_error": "slow"}]}

    def call(self, key, tool, args, approved_by=""):
        if tool.endswith("_services"):
            return {"text": json.dumps({"services": self.services.get(key, [])})}
        return {"text": json.dumps({"alarms": self.alarms.get(key, [])})}


class FakeTopology:
    def __init__(self):
        self.service = FakeService()
        self.state = {"ok": True, "controllers": [
            {"key": "opt", "label": "Optical", "ok": True, "devices": [{"id": "roadm-1", "status": "up"}, {"id": "roadm-2", "status": "down"}],
             "links": [{"id": "L1", "a": "roadm-1", "z": "roadm-2", "state": "down"}]},
            {"key": "pkt", "label": "Packet", "ok": False, "error": "connection refused", "devices": [], "links": []}]}

    def read(self, refresh=False):
        return self.state


class Base(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)


class IncidentStore(Base):
    def test_open_dedup_and_resolve(self):
        s = Incidents(self.home / "i.sqlite")
        f = {"fp": "x:link:L1", "controller": "x", "severity": "major", "kind": "link_down", "title": "L1 down"}
        self.assertEqual(len(s.update([f])["opened"]), 1)
        self.assertEqual(s.update([f])["opened"], [])                 # the same incident, seen again
        self.assertEqual(s.list("active")[0]["count"], 2)
        self.assertEqual(s.update([], resolve_after=2)["resolved"], [])   # missing once
        gone = s.update([], resolve_after=2)["resolved"]
        self.assertEqual(gone[0]["status"], "resolved")
        self.assertEqual(len(s.update([f])["opened"]), 1)             # back again: a new incident


class Watching(Base):
    def watcher(self, respond=None):
        sent = []
        n = Notifier([])
        n.send = lambda *a, **k: sent.append(a)
        w = Watcher(FakeTopology(), Incidents(self.home / "i.sqlite"), n, History(self.home),
                    runbooks=lambda: load_runbooks([BUILTIN_RUNBOOKS]), respond=respond)
        return w, sent

    def test_what_it_finds_through_the_contract(self):
        w, sent = self.watcher()
        got = {f["kind"]: f for f in w.findings()["findings"]}
        self.assertEqual(set(got), {"device_down", "link_down", "controller_unreachable", "controller_degraded", "service_down", "alarm"})
        self.assertEqual(len([f for f in w.findings()["findings"] if f["fp"] == "pkt:unreachable"]), 1)   # one, though two sources say it
        self.assertEqual(got["alarm"]["severity"], "critical")
        self.assertIn("LP2", got["service_down"]["title"])

    def test_a_poll_opens_incidents_matches_runbooks_and_investigates(self):
        asked = []
        w, sent = self.watcher(respond=lambda group, evidence: asked.append((group, evidence)))
        w.poll()
        rows = {r["kind"]: r for r in w.store.list("active")}
        self.assertEqual(rows["link_down"]["runbook"], "link-down")
        self.assertEqual(rows["link_down"]["mode"], "propose")
        self.assertEqual(rows["device_down"]["mode"], "investigate")
        # What depends on each other is one investigation (the link and the device at its end); a controller that
        # stopped answering, and faults linked to nothing here, are each their own.
        kinds = sorted(sorted(a["kind"] for a in g) for g, _ in asked)
        self.assertIn(["device_down", "link_down"], kinds)
        self.assertIn(["controller_unreachable"], kinds)
        self.assertEqual(sum(len(k) for k in kinds), 5)
        group, evidence = next((g, e) for g, e in asked if any(a["kind"] == "link_down" for a in g))
        self.assertIn("L1", evidence["objects"])
        self.assertEqual(evidence["health_now"]["opt"]["links_down"], 1)
        self.assertEqual(len([s for s in sent if s[0] == "incident_opened"]), 6)       # the degraded one too (no runbook)
        w.poll()
        self.assertEqual(len([s for s in sent if s[0] == "incident_opened"]), 6)       # nothing new: nothing sent
        self.assertTrue((self.home / "history" / "opt").is_dir())                    # history from the first poll

    def test_alarms_and_services_of_any_shape(self):
        a = alarms_of({"ietf-alarms:alarm-list": {"alarm": [{"alarm-type": "LOS", "perceived-severity": "major", "resource": "port-3"}]}})
        self.assertEqual((a[0]["severity"], a[0]["resource"]), ("major", "port-3"))
        s = services_of({"data": {"services": [{"service-name": "S1", "operational-state": "down"}, {"name": "S2", "status": "ACTIVE"}]}})
        self.assertEqual({x["id"]: x["up"] for x in s}, {"S1": False, "S2": True})


class Runbooks(Base):
    def test_a_site_runbook_overrides_a_builtin_and_matches_by_text(self):
        d = self.home / "rb"
        d.mkdir()
        (d / "link.yaml").write_text("name: link-down\nwhen: {kind: link_down, text: 'core'}\nmode: notify\ninstructions: page the NOC\n")
        rbs = load_runbooks([BUILTIN_RUNBOOKS, d])
        self.assertEqual(match(rbs, {"kind": "link_down", "title": "core link down", "severity": "major"})["mode"], "notify")
        self.assertIsNone(match(rbs, {"kind": "link_down", "title": "access link down", "severity": "major"}))


class NetworkHistory(Base):
    def test_kept_only_when_it_changes_and_diffed(self):
        h = History(self.home)
        a = {"devices": [{"id": "d1", "status": "up"}], "links": [], "services": [{"id": "S1", "status": "up"}]}
        b = {"devices": [{"id": "d1", "status": "down"}, {"id": "d2", "status": "up"}], "links": [], "services": []}
        self.assertTrue(h.record("x", a, at=time.time() - 60)["first"])
        self.assertIsNone(h.record("x", a, at=time.time() - 30))
        self.assertIsNotNone(h.record("x", b))
        ch = h.changes("x")[0]["changes"]
        kinds = {(c["kind"], c["id"], c["change"]) for c in ch}
        self.assertEqual(kinds, {("device", "d1", "changed"), ("device", "d2", "added"), ("service", "S1", "removed")})
        self.assertEqual(diff(a, a), {})


class Notifications(Base):
    def test_webhook_and_slack_shapes_and_min_severity(self):
        got = []

        class Hook(BaseHTTPRequestHandler):
            def do_POST(self):
                got.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = HTTPServer(("127.0.0.1", 0), Hook)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_port}"
        os.environ["TEST_SLACK_URL"] = base + "/slack"
        self.addCleanup(os.environ.pop, "TEST_SLACK_URL")
        n = Notifier([{"kind": "webhook", "url": base + "/hook"},
                      {"kind": "slack", "url_env": "TEST_SLACK_URL", "min_severity": "critical"}], console_url="http://canah")
        n.send("incident_opened", "major", "L1 down", "the link", link="/#incident-1", wait=True)
        n.send("incident_opened", "critical", "core down", "the core", wait=True)
        paths = [p for p, _ in got]
        self.assertEqual(paths.count("/hook"), 2)
        self.assertEqual(paths.count("/slack"), 1)                    # only the critical one
        hook = [b for p, b in got if p == "/hook"][0]
        self.assertEqual((hook["event"], hook["link"]), ("incident_opened", "http://canah/#incident-1"))
        self.assertIn("*[CRITICAL] core down*", [b for p, b in got if p == "/slack"][0]["text"])
        self.assertNotIn(base, json.dumps(n.describe()))             # a secret URL is never shown


class Scopes(Base):
    def test_an_account_limited_to_some_controllers(self):
        acc = Accounts(str(self.home / "users.json"))
        acc.add("ana", "admin", "password-1", controllers=["opt"])
        who = acc.sign_in("ana", "password-1")
        self.assertEqual(who.controllers, ("opt",))
        self.assertTrue(who.sees("opt"))
        self.assertFalse(who.sees("pkt"))
        acc.set_scope("ana", [])
        self.assertEqual(acc.sign_in("ana", "password-1").controllers, ())

    def test_the_tools_keep_to_the_scope(self):
        from canah.network import AgenticMCPProvider
        from tests_canah.test_network_knowledge import FakeAgenticMCP
        svc = FakeAgenticMCP()
        p = AgenticMCPProvider(service=svc)
        tools = {t.name: t for t in p.tools()}
        cfg = {"configurable": {"scope": ["beta"]}}
        out = tools["net_read_devices"].invoke({}, config=cfg)
        self.assertNotIn("alpha", json.loads(out))                       # the fan-out keeps to beta
        refused = tools["controller_read"].invoke({"controller": "alpha", "tool": "get_nodes"}, config=cfg)
        self.assertIn("outside what this operator works with", refused)
        self.assertEqual([c[0] for c in svc.calls], ["beta"])


class Status(Base):
    def test_canah_status_says_who_asks_and_what_the_harness_does(self):
        from canah.config import load
        from canah.operations import OperationsProvider
        p = OperationsProvider(self.home, load(path="/x", home=str(self.home)),
                               lambda: {"controllers": ["opt", "pkt"], "twin": "netwins", "model": "ollama:x"})
        t = {x.name: x for x in p.tools()}
        got = json.loads(t["canah_status"].invoke({}, config={"configurable": {"user_id": "ana", "level": "admin", "scope": ["opt"]}}))
        self.assertEqual(got["you"]["scope"], ["opt"])
        self.assertEqual(got["controllers"], ["opt"])
        self.assertIn("approvals", got["harness"])
        self.assertIn("No incidents", t["incidents"].invoke({}, config={"configurable": {}}))


if __name__ == "__main__":
    unittest.main()


class HealthOverTime(Base):
    def test_each_reading_is_kept_and_summarised(self):
        from canah.health import Series, sample
        s = Series(self.home)
        topo = FakeTopology().state
        s.add(time.time() - 120, sample(topo, {"opt": [{"id": "LP1", "up": True}]}), {"opt": 0})
        s.add(time.time(), sample(topo, {"opt": [{"id": "LP1", "up": False}]}), {"opt": 2})
        got = s.get(1)
        self.assertEqual([x["services_down"] for x in got["opt"]], [0, 1])
        self.assertEqual(got["pkt"][-1]["reachable"], 0)
        from canah.config import load
        from canah.operations import OperationsProvider
        p = OperationsProvider(self.home, load(path="/x", home=str(self.home)), lambda: {})
        t = {x.name: x for x in p.tools()}
        out = json.loads(t["health_over_time"].invoke({"hours": 1}, config={"configurable": {}}))
        self.assertEqual(out["opt"]["incidents"], {"now": 2, "max": 2, "min": 0})


class SelfDescription(unittest.TestCase):
    def test_an_answer_that_calls_built_features_missing_is_sent_back(self):
        from langchain_core.messages import AIMessage, HumanMessage
        from canah.operations import SelfClaims
        g = SelfClaims.make()
        q = HumanMessage(content="Describe yourself.")
        wrong = AIMessage(content="What I cannot do:\n- Auto-remediation: I cannot apply fixes without human approval.\n"
                                  "- Multi-tenant support: no isolation between domains.\n"
                                  "- RBAC enforcement: cannot block actions based on user role.")
        out = g.after_model({"messages": [q, wrong]}, None)
        self.assertIn("tenant", out["messages"][0].content)
        self.assertIn("roles enforced", out["messages"][0].content)
        right = AIMessage(content="Not built yet: applying a fix without human approval; several agentic MCPs.\n"
                                  "Needs attention: no notification channel is configured.")
        self.assertIsNone(g.after_model({"messages": [q, right]}, None))
        self.assertIsNone(g.after_model({"messages": [HumanMessage(content="List the lightpaths."), wrong]}, None))


class Heartbeat(unittest.TestCase):
    def test_a_controller_going_down_is_seen_at_the_next_beat_and_read_at_once(self):
        import tempfile
        from pathlib import Path
        from canah.incidents import Incidents, Watcher
        from canah.notify import Notifier

        class Svc:
            def __init__(self):
                self.state, self.calls = "up", []

            def controllers(self):
                return [{"key": "pkt"}]

            def tools(self, key):
                return [{"name": "health", "verb": "health", "write": False}]

            def call(self, key, tool, args):
                self.calls.append((key, tool))
                return {"text": "ok"}

            def get(self, path, timeout=None):
                return {"sessions": [{"controller": "pkt", "state": self.state}]}

        svc = Svc()
        topo = type("T", (), {"service": svc, "read": lambda self, refresh=True: {"controllers": []}})()
        home = Path(tempfile.mkdtemp())
        w = Watcher(topo, Incidents(home / "i.sqlite"), Notifier([]), heartbeat_s=5)
        self.assertEqual(w.heartbeat(), {})                       # the first beat only learns the states
        self.assertIn(("pkt", "health"), svc.calls)               # the controller itself was asked, cheaply
        svc.state = "down"
        self.assertEqual(w.heartbeat(), {"pkt": ("up", "down")})
        self.assertEqual(w.heartbeat(), {})                       # said once, not at every beat
