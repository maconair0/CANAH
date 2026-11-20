"""The HTTP service: sign-in, threads, streaming, approvals, isolation between users."""
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from canah import FunctionProvider, build_agent, load_settings
from canah.identity import Accounts
from canah.memory import open_memory
from canah.service import create_app

from tests_canah.fakes import ScriptedModel, call, say

DONE = []


def list_items() -> str:
    """List the items."""
    return "a, b"


def drop_item(name: str) -> str:
    """Drop one item."""
    DONE.append(name)
    return f"{name} dropped"


def events(text):
    out = []
    for block in text.strip().split("\n\n"):
        kind = [l[7:] for l in block.splitlines() if l.startswith("event: ")][0]
        data = json.loads([l[6:] for l in block.splitlines() if l.startswith("data: ")][0])
        out.append((kind, data))
    return out


class FakeMCP:
    """Two controllers behind an agentic MCP: one with ONOS's shapes, one that cannot be read."""
    url = "http://fake"

    def controllers(self):
        return [{"key": "onos", "label": "ONOS", "domain": "packet", "tools": 2,
                 "conformance": {"verdict": "accepted", "domains": [{"features": {"read_devices": "tool get_devices",
                                                                                  "read_links": "tool get_links"}}]}},
                {"key": "tpce", "label": "TransportPCE", "domain": "optical", "tools": 1, "conformance": {}}]

    def tools(self, key):
        return [{"name": "get_devices"}, {"name": "get_links"}] if key == "onos" else [{"name": "list_nodes"}]

    def call(self, key, tool, args):
        if key == "tpce":
            return {"text": "this needs the agentic MCP's token", "is_error": True}
        if tool == "get_devices":
            return {"text": json.dumps({"devices": [{"id": "of:01", "available": True}, {"id": "of:02", "available": False}]})}
        return {"text": json.dumps({"links": [{"src": {"device": "of:01", "port": "1"}, "dst": {"device": "of:02", "port": "2"}, "state": "ACTIVE"},
                                              {"src": {"device": "of:02", "port": "2"}, "dst": {"device": "of:01", "port": "1"}, "state": "ACTIVE"}]})}

    def get(self, path, timeout=None):
        return {"sessions": []}

    # what onboarding sends, recorded (the agentic MCP's own HTTP surface)
    sent = []

    def _headers(self):
        import os as _os
        t = self.token
        return {"Authorization": f"Bearer {t}"} if t else {}

    @property
    def token(self):
        import os as _os
        return _os.getenv("AGENTIC_MCP_TOKEN", "")

    CONTRACT = {"version": 1, "domains": [{"name": "inventory", "required": True, "verbs": {"read_devices": {"methods": ["GET"]}}}]}

    class _Http:
        def request(self, method, url, **kw):
            FakeMCP.sent.append((method, url, kw.get("json"), kw.get("params"), (kw.get("headers") or {}).get("Authorization")))
            path = url.split("fake", 1)[-1]

            broken = (kw.get("json") or {}).get("key") == "broken"

            class R:
                status_code = 502 if broken else 200
                text = "the controller did not answer" if broken else "{}"
                ok = not broken

                def json(self):
                    if path == "/contract" and method == "GET":
                        return {"contract": FakeMCP.CONTRACT, "controllers": [{"key": "onos", "verdict": "accepted", "gaps": []}]}
                    if path == "/contract/check":
                        return {"ok": True, "problems": [], "controllers": [{"key": "onos", "verdict": "accepted, with gaps", "gaps": ["x"]}]}
                    if path == "/contract" and method == "PUT":
                        return {"version": 2, "domains": 1, "verbs": 1, "added": [], "removed": [], "backup": "/x.bak", "controllers": []}
                    return {"key": (kw.get("json") or {}).get("key"), "conformance": {"verdict": "accepted"}}
            return R()

        def get(self, url, **kw):
            return self.request("GET", url, **kw)

        def post(self, url, **kw):
            return self.request("POST", url, **kw)

    http = _Http()


class Service(unittest.TestCase):
    def setUp(self):
        DONE.clear()
        os.environ["CANAH_ADMIN_PASSWORD"] = "adm1n"
        os.environ["CANAH_ROOT_PASSWORD"] = "r00t"
        self.addCleanup(os.environ.pop, "CANAH_ADMIN_PASSWORD")
        self.addCleanup(os.environ.pop, "CANAH_ROOT_PASSWORD")
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        p = FunctionProvider()
        p.add(list_items)
        p.add(drop_item, write=True)
        self.model = ScriptedModel(script=[])
        self.agent = build_agent(load_settings(path="/x", home=self.home, sandbox="none", root_tools=["*root_only*"]),
                                 [p], model=self.model, memory=open_memory(in_memory=True))
        self.addCleanup(self.agent.close)
        from canah.topology import Topology
        FakeMCP.sent.clear()
        self.rebuilt = []

        def rebuild():
            a = build_agent(load_settings(path="/x", home=self.home, sandbox="none"), [p], model=self.model,
                            memory=open_memory(in_memory=True))
            self.rebuilt.append(a)
            self.addCleanup(a.close)
            return a
        self.client = TestClient(create_app(self.agent, Accounts(), topology=Topology(FakeMCP()), rebuild=rebuild))

    def script(self, *turns):
        self.model.script[:] = list(turns)

    def sign_in(self, user="admin", password="adm1n"):
        r = self.client.post("/session", json={"username": user, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['token']}"}

    def test_health_info_and_page(self):
        self.assertTrue(self.client.get("/health").json()["ok"])
        self.assertEqual(self.client.get("/info").json()["tools"], 8)        # 2 here; draw_/get_diagram; canah_status, incidents, network_changes, health_over_time
        page = self.client.get("/").text
        self.assertIn("Controller Agnostic Network Agent Harness", page)
        self.assertIn('id="login"', page)
        for f in ("app.js", "app.css", "logo-light.png", "logo-dark.png", "fonts/montserrat-latin.woff2"):
            self.assertEqual(self.client.get(f"/static/{f}").status_code, 200, f)

    def test_topology_reads_every_controller_and_says_which_cannot_be_read(self):
        t = self.client.get("/topology").json()
        by = {c["key"]: c for c in t["controllers"]}
        self.assertEqual([d["id"] for d in by["onos"]["devices"]], ["of:01", "of:02"])
        self.assertEqual(by["onos"]["devices"][1]["status"], "down")
        self.assertEqual(len(by["onos"]["links"]), 1)                     # both directions, one link
        self.assertFalse(by["tpce"]["ok"])
        self.assertIn("token", by["tpce"]["error"])
        n = self.client.get("/network").json()
        self.assertEqual([c["key"] for c in n["controllers"]], ["onos", "tpce"])

    def test_an_attached_file_is_read_by_the_agent_and_is_its_owners_only(self):
        import base64
        h = self.sign_in()
        got = self.client.post("/threads/f1/files", headers=h, json={
            "name": "notes.txt", "content_base64": base64.b64encode(b"vlan 300 is reserved for acme").decode()}).json()
        self.assertEqual(got["path"], "/uploads/f1/notes.txt")
        self.assertIn("vlan 300", got["preview"])
        self.script(call("read_file", file_path="/uploads/f1/notes.txt"), say("vlan 300 is reserved"))
        r = self.client.post("/threads/f1/messages", headers=h, json={"text": "what does my file say?"}).json()
        self.assertIn("vlan 300 is reserved for acme", r["tool_calls"][0]["result"])
        # another user (root) reading the same path finds nothing
        self.script(call("read_file", file_path="/uploads/f1/notes.txt"), say("nothing"))
        r = self.client.post("/threads/f2/messages", headers=self.sign_in("root", "r00t"), json={"text": "read it"}).json()
        self.assertNotIn("acme", r["tool_calls"][0]["result"])

    def test_activity_says_what_runs(self):
        a = self.client.get("/activity").json()
        self.assertFalse(a["busy"])
        self.script(call("list_items"), say("a, b"))
        self.client.post("/threads/a1/stream", json={"text": "what is there?"})
        a = self.client.get("/activity").json()
        self.assertEqual(a["used"]["main"]["calls"], 1)
        self.assertEqual(a["recent"][-1]["tool"], "list_items")

    def test_onboarding_goes_to_the_agentic_mcp_and_needs_the_right_level(self):
        body = {"key": "onos2", "url": "http://10.0.0.5:8181/onos/v1", "label": "ONOS 2", "user_env": "ONOS2_USER"}
        self.assertEqual(self.client.post("/network/controllers", json=body).status_code, 403)
        h = self.sign_in()
        bad = self.client.post("/network/controllers", headers=h, json={**body, "password_env": "s3cret!"})
        self.assertEqual(bad.status_code, 400)                                   # a secret, not a variable name
        r = self.client.post("/network/controllers", headers=h, json=body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["reload_needed"])
        method, url, sent, _, _ = FakeMCP.sent[-1]
        self.assertEqual((method, url.endswith("/controllers"), sent["key"], sent["user_env"]),
                         ("POST", True, "onos2", "ONOS2_USER"))
        self.assertEqual(self.client.delete("/network/controllers/onos2", headers=h).status_code, 403)   # root only
        self.assertEqual(self.client.delete("/network/controllers/onos2", headers=self.sign_in("root", "r00t")).status_code, 200)

    def test_reload_swaps_the_agent(self):
        self.assertEqual(self.client.post("/admin/reload").status_code, 403)
        r = self.client.post("/admin/reload", headers=self.sign_in())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self.rebuilt), 1)
        self.script(say("still here"))
        self.assertEqual(self.client.post("/threads/r1/messages", json={"text": "hi"}).json()["text"], "still here")

    def test_an_edit_as_documented_runs_with_the_edited_arguments(self):
        h = self.sign_in()
        self.script(call("drop_item", name="a"), say("dropping"))
        r = self.client.post("/threads/e1/messages", headers=h, json={"text": "drop a"}).json()
        self.assertEqual(r["pending"][0]["name"], "drop_item")
        self.script(say("dropped b"))
        bad = self.client.post("/threads/e1/decide", headers=h, json={"decisions": [{"type": "edit", "name": "drop_item"}]})
        self.assertEqual(bad.status_code, 400)
        ok = self.client.post("/threads/e1/decide", headers=h, json={"decisions": [{"type": "edit", "name": "drop_item", "args": {"name": "b"}}]})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(DONE, ["b"])

    def test_history_gives_back_the_conversation(self):
        self.script(say("hello there"))
        self.client.post("/threads/h1/messages", json={"text": "hi"})
        h = self.client.get("/threads/h1/history").json()["messages"]
        self.assertEqual([m["role"] for m in h], ["user", "agent"])
        self.assertEqual(h[1]["text"], "hello there")
        self.script(call("list_items"), say("a and b"))
        self.client.post("/threads/h1/messages", json={"text": "what is there?"})
        h = self.client.get("/threads/h1/history").json()["messages"]
        self.assertEqual([m["role"] for m in h][-3:], ["user", "tool", "agent"])
        self.assertEqual((h[-2]["name"], h[-2]["result"]), ("list_items", "a, b"))

    def test_sign_in_and_out(self):
        self.assertEqual(self.client.get("/session").json()["level"], "observer")
        self.assertEqual(self.client.post("/session", json={"username": "admin", "password": "no"}).status_code, 401)
        h = self.sign_in()
        self.assertEqual(self.client.get("/session", headers=h).json(), {"user": "admin", "level": "admin",
                                                                         "signed_in": True, "scope": []})
        self.client.delete("/session", headers=h)
        self.assertEqual(self.client.get("/session", headers=h).status_code, 401)

    def test_a_message_and_its_reply(self):
        self.script(call("list_items"), say("a and b"))
        r = self.client.post("/threads/t1/messages", json={"text": "items?"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["text"], "a and b")
        self.assertEqual(r.json()["thread_id"], "t1")

    def test_streaming_gives_calls_tokens_and_the_reply(self):
        self.script(call("list_items"), say("a and b"))
        r = self.client.post("/threads/t2/stream", json={"text": "items?"})
        got = events(r.text)
        kinds = [k for k, _ in got]
        self.assertIn("tool_call", kinds)
        self.assertIn("tool_result", kinds)
        self.assertEqual(got[-1][0], "reply")
        self.assertEqual(got[-1][1]["text"], "a and b")
        self.assertEqual(got[-1][1]["thread_id"], "t2")

    def test_events_leave_as_they_happen_not_at_the_end(self):
        # A real server: Starlette's TestClient collects the whole body before returning, so it cannot see this.
        import socket
        import time
        import requests
        import uvicorn
        got_first = threading.Event()
        waited = {}

        def slow_stream(text, key, who):
            yield {"type": "token", "text": "first"}
            # the next event only once the client has the first: a buffered stream never gets here in time
            waited["ok"] = got_first.wait(5)
            yield {"type": "reply", "text": "first", "pending": [], "tool_calls": [], "thread_id": key}
        self.agent.stream = slow_stream
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        server = uvicorn.Server(uvicorn.Config(self.client.app, host="127.0.0.1", port=port, log_level="error"))
        t = threading.Thread(target=server.run, daemon=True)
        t.start()
        self.addCleanup(t.join, 5)
        self.addCleanup(setattr, server, "should_exit", True)
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        with requests.post(f"http://127.0.0.1:{port}/threads/t9/stream", json={"text": "hi"}, stream=True,
                           headers={"Accept-Encoding": "gzip"}, timeout=10) as r:
            for line in r.iter_lines(decode_unicode=True):
                if line.startswith("event: token"):
                    got_first.set()
        self.assertTrue(waited.get("ok"), "the first event reached the client only after the turn ended")

    def test_the_page_is_sent_compressed(self):
        r = self.client.get("/static/app.js", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(r.headers.get("content-encoding"), "gzip")

    def test_the_agentic_mcp_token_is_set_by_root_kept_private_and_never_sent_back(self):
        import stat
        os.environ.pop("AGENTIC_MCP_TOKEN", None)
        self.addCleanup(os.environ.pop, "AGENTIC_MCP_TOKEN", None)
        r = self.client.put("/network/connection", json={"token": "s3cr3t"}, headers=self.sign_in())
        self.assertEqual(r.status_code, 403)                       # admin is not enough
        r = self.client.put("/network/connection", json={"token": "s3cr3t"}, headers=self.sign_in("root", "r00t"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("s3cr3t", r.text)
        self.assertTrue(r.json()["token_set"])
        f = Path(self.home) / "agentic_mcp.json"
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
        self.assertEqual(os.environ["AGENTIC_MCP_TOKEN"], "s3cr3t")
        self.assertEqual(FakeMCP.sent[-1][-1], "Bearer s3cr3t")    # used from the next call on
        got = self.client.get("/network/connection").json()
        self.assertNotIn("s3cr3t", json.dumps(got))
        r = self.client.put("/network/connection", json={"token": "two words"}, headers=self.sign_in("root", "r00t"))
        self.assertEqual(r.status_code, 400)
        r = self.client.put("/network/connection", json={"clear_token": True}, headers=self.sign_in("root", "r00t"))
        self.assertFalse(r.json()["token_set"])
        self.assertNotIn("AGENTIC_MCP_TOKEN", os.environ)

    def test_the_common_contract_is_read_checked_and_changed_by_root(self):
        self.assertEqual(self.client.get("/network/contract").json()["contract"]["version"], 1)
        self.assertEqual(self.client.post("/network/contract/check", json={"contract": FakeMCP.CONTRACT}).status_code, 403)
        r = self.client.post("/network/contract/check", json={"contract": FakeMCP.CONTRACT}, headers=self.sign_in())
        self.assertEqual(r.json()["controllers"][0]["verdict"], "accepted, with gaps")
        self.assertEqual(self.client.put("/network/contract", json={"contract": FakeMCP.CONTRACT}, headers=self.sign_in()).status_code, 403)
        r = self.client.put("/network/contract", json={"contract": FakeMCP.CONTRACT}, headers=self.sign_in("root", "r00t"))
        self.assertEqual(r.json()["version"], 2)
        self.assertEqual(FakeMCP.sent[-1][0], "PUT")

    def test_several_controllers_from_one_file_each_on_its_own(self):
        import base64
        yml = "controllers:\n  - {key: onos2, url: 'http://10.0.0.5:8181'}\n  - {key: broken, url: 'http://10.0.0.6'}\n  - {key: Bad, url: 'http://x'}\n"
        r = self.client.post("/network/controllers/batch", json={"content_base64": base64.b64encode(yml.encode()).decode()},
                             headers=self.sign_in())
        self.assertEqual(r.status_code, 200, r.text)
        got = {x["key"]: x for x in r.json()["results"]}
        self.assertTrue(got["onos2"]["ok"])
        self.assertFalse(got["broken"]["ok"])
        self.assertFalse(got["Bad"]["ok"])                          # a key the form would refuse
        self.assertEqual(r.json()["onboarded"], 1)

    def test_each_tool_result_carries_how_long_it_took(self):
        self.script(call("list_items"), say("a and b"))
        got = events(self.client.post("/threads/t8/stream", json={"text": "items?"}).text)
        res = [d for k, d in got if k == "tool_result"][0]
        self.assertIsInstance(res["ms"], int)
        hist = self.client.get("/threads/t8/history").json()["messages"]
        self.assertIsInstance([m for m in hist if m["role"] == "tool"][0]["ms"], int)

    def test_the_model_is_changed_by_root_only_if_it_answers_and_its_key_stays_private(self):
        from unittest import mock
        from canah import models as M
        self.addCleanup(M.save_page_choice, self.home, clear=True)
        self.addCleanup(os.environ.pop, "ANTHROPIC_API_KEY", None)
        body = {"provider": "anthropic", "model": "claude-x", "api_key": "sk-test-123"}
        self.assertEqual(self.client.put("/admin/model", json=body, headers=self.sign_in()).status_code, 403)
        root = self.sign_in("root", "r00t")

        class Broken:
            def invoke(self, _):
                raise RuntimeError("401 invalid key")
        with mock.patch.object(M, "build_choice", return_value=Broken()):
            r = self.client.put("/admin/model", json=body, headers=root)
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("ANTHROPIC_API_KEY", os.environ)                    # the tried key is not kept
        self.assertFalse((Path(self.home) / "model.json").exists())

        class Works:
            def invoke(self, _):
                return say("OK")
        with mock.patch.object(M, "build_choice", return_value=Works()):
            r = self.client.put("/admin/model", json=body, headers=root)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("sk-test-123", r.text)
        import stat
        self.assertEqual(stat.S_IMODE((Path(self.home) / "keys.json").stat().st_mode), 0o600)
        info = self.client.get("/admin/model").json()
        self.assertNotIn("sk-test-123", json.dumps(info))
        self.assertTrue([x for x in info["providers"] if x["id"] == "anthropic"][0]["key_set"])
        self.assertEqual(info["choice"]["model"], "claude-x")

    def test_only_real_diagram_names_are_served(self):
        d = Path(self.home) / "diagrams"
        d.mkdir()
        (d / "0123456789abcdef.svg").write_text("<svg/>")
        self.assertEqual(self.client.get("/diagrams/0123456789abcdef.svg").status_code, 200)
        for bad in ("..%2Fusers.json", "users.json", "0123456789abcdef.txt", "0123456789ABCDEF.svg"):
            self.assertEqual(self.client.get(f"/diagrams/{bad}").status_code, 404, bad)

    def test_root_manages_accounts_and_their_scopes(self):
        self.assertEqual(self.client.post("/users", json={"user": "ana", "level": "admin", "password": "password-1"},
                                          headers=self.sign_in()).status_code, 403)
        root = self.sign_in("root", "r00t")
        r = self.client.post("/users", json={"user": "ana", "level": "admin", "password": "password-1", "controllers": ["onos"]},
                             headers=root)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([u for u in r.json()["users"] if u["user"] == "ana"][0]["controllers"], ["onos"])
        self.assertNotIn("password-1", r.text)
        ana = self.sign_in("ana", "password-1")
        self.assertEqual(self.client.get("/session", headers=ana).json()["scope"], ["onos"])
        self.assertEqual([x["key"] for x in self.client.get("/topology", headers=ana).json()["controllers"]], ["onos"])
        self.client.put("/users/ana/scope", json={"controllers": []}, headers=root)
        self.assertEqual(self.client.delete("/users/ana", headers=root).status_code, 200)

    def test_incidents_are_listed_within_the_scope_and_acknowledged_by_admin(self):
        from canah.incidents import Incidents
        store = Incidents(Path(self.home) / "incidents.sqlite")
        store.update([{"fp": "onos:link:L1", "controller": "onos", "severity": "major", "kind": "link_down", "title": "L1 down"},
                      {"fp": "tpce:unreachable", "controller": "tpce", "severity": "major", "kind": "controller_unreachable",
                       "title": "tpce cannot be read"}])
        got = self.client.get("/incidents").json()
        self.assertEqual(got["counts"]["active"], 2)
        iid = [i for i in got["incidents"] if i["controller"] == "onos"][0]["id"]
        self.assertEqual(self.client.post(f"/incidents/{iid}/ack").status_code, 403)            # an observer may not
        r = self.client.post(f"/incidents/{iid}/ack", headers=self.sign_in())
        self.assertEqual(r.json()["incident"]["status"], "acked")
        root = self.sign_in("root", "r00t")
        self.client.post("/users", json={"user": "bo", "level": "admin", "password": "password-2", "controllers": ["tpce"]}, headers=root)
        bo = self.sign_in("bo", "password-2")
        self.assertEqual([i["controller"] for i in self.client.get("/incidents", headers=bo).json()["incidents"]], ["tpce"])
        self.assertEqual(self.client.get(f"/incidents/{iid}", headers=bo).status_code, 404)    # outside bo's scope
        self.assertIn("runbooks", self.client.get("/runbooks").json())

    def test_a_change_waits_and_an_admin_approves_it(self):
        h = self.sign_in()
        self.script(call("drop_item", name="a"), say("a dropped"))
        r = self.client.post("/threads/t3/messages", json={"text": "drop a"}, headers=h).json()
        self.assertEqual(r["pending"][0]["name"], "drop_item")
        p = self.client.get("/threads/t3/pending", headers=h).json()["pending"][0]
        self.assertEqual((p["needs"], p["you_may_approve"]), ("admin", True))
        self.assertEqual(DONE, [])
        r = self.client.post("/threads/t3/decide", json={"decisions": [{"type": "approve"}]}, headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(DONE, ["a"])

    def test_a_streamed_decision(self):
        h = self.sign_in()
        self.script(call("drop_item", name="b"), say("b dropped"))
        self.client.post("/threads/t4/messages", json={"text": "drop b"}, headers=h)
        r = self.client.post("/threads/t4/decide", json={"decisions": [{"type": "approve"}], "stream": True},
                             headers=h)
        self.assertEqual(events(r.text)[-1][1]["text"], "b dropped")
        self.assertEqual(DONE, ["b"])

    def test_an_observer_cannot_approve_and_a_busy_thread_refuses_a_new_message(self):
        h = self.sign_in()
        self.script(call("drop_item", name="a"))
        self.client.post("/threads/t5/messages", json={"text": "drop a"}, headers=h)
        # The same thread id as an observer is someone else's thread: nothing waits there.
        self.assertEqual(self.client.get("/threads/t5/pending").json()["pending"], [])
        r = self.client.post("/threads/t5/messages", json={"text": "and b"}, headers=h)
        self.assertEqual(r.status_code, 409)
        self.assertIn("waiting", r.json()["detail"])

    def test_threads_are_per_user(self):
        h = self.sign_in()
        self.script(say("noted: blue"), say("I have nothing on that."))
        self.client.post("/threads/shared/messages", json={"text": "remember blue"}, headers=h)
        self.client.post("/threads/shared/messages", json={"text": "what colour?"})   # observer, same id
        said = " ".join(str(m.content) for m in self.model.seen[-1])
        self.assertNotIn("remember blue", said)

    def test_a_bad_decision_is_an_error_not_a_crash(self):
        h = self.sign_in()
        self.script(call("drop_item", name="a"))
        self.client.post("/threads/t6/messages", json={"text": "drop a"}, headers=h)
        r = self.client.post("/threads/t6/decide", json={"decisions": [{"type": "maybe"}]}, headers=h)
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/threads/t6/decide", json={"decisions": []}, headers=h)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.post("/threads/bad id!/messages", json={"text": "x"}).status_code, 400)

    def test_approving_above_your_level_is_403(self):
        def root_only_reset() -> str:
            """Reset everything."""
            DONE.append("reset")
            return "reset"
        p = FunctionProvider()
        p.add(root_only_reset, write=True)
        model = ScriptedModel(script=[call("root_only_reset"), say("reset done")])
        agent = build_agent(load_settings(path="/x", home=self.home, sandbox="none", root_tools=["root_only*"]),
                            [p], model=model, memory=open_memory(in_memory=True))
        self.addCleanup(agent.close)
        client = TestClient(create_app(agent, Accounts()))
        admin = {"Authorization": "Bearer " + client.post("/session", json={"username": "admin", "password": "adm1n"}).json()["token"]}
        r = client.post("/threads/r1/messages", json={"text": "reset"}, headers=admin).json()
        self.assertEqual(r["pending"][0]["needs"], "root")
        r = client.post("/threads/r1/decide", json={"decisions": [{"type": "approve"}]}, headers=admin)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(DONE, [])
        r = client.post("/threads/r1/decide", json={"decisions": [{"type": "reject"}]}, headers=admin)
        self.assertEqual(r.status_code, 200)

    def test_an_ended_session_is_401_not_observer(self):
        self.assertEqual(self.client.get("/session", headers={"Authorization": "Bearer nope"}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
