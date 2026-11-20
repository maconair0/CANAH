"""The HTTP service: sign-in, threads, streaming, approvals, isolation between users."""
import json
import os
import shutil
import tempfile
import threading
import unittest

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
        self.client = TestClient(create_app(self.agent, Accounts()))

    def script(self, *turns):
        self.model.script[:] = list(turns)

    def sign_in(self, user="admin", password="adm1n"):
        r = self.client.post("/session", json={"username": user, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['token']}"}

    def test_health_info_and_page(self):
        self.assertTrue(self.client.get("/health").json()["ok"])
        self.assertEqual(self.client.get("/info").json()["tools"], 2)
        self.assertIn("<title>CANAH</title>", self.client.get("/").text)

    def test_sign_in_and_out(self):
        self.assertEqual(self.client.get("/session").json()["level"], "observer")
        self.assertEqual(self.client.post("/session", json={"username": "admin", "password": "no"}).status_code, 401)
        h = self.sign_in()
        self.assertEqual(self.client.get("/session", headers=h).json(), {"user": "admin", "level": "admin",
                                                                         "signed_in": True})
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
