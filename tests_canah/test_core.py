"""The core agent, end to end, on a scripted model: no live model, no network."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from canah import FunctionProvider, build_agent, load_settings
from canah import approval, mcp
from canah.memory import open_memory
from canah.identity import Identity

ADMIN = Identity("ana", "admin")

from tests_canah.fakes import ScriptedModel, call, say

CHANGES = []


def list_devices() -> str:
    """List the devices in the network."""
    return json.dumps(["r1", "r2"])


def restart_device(name: str) -> str:
    """Restart a device by name."""
    CHANGES.append(name)
    return f"{name} restarted"


def provider():
    p = FunctionProvider()
    p.add(list_devices)
    p.add(restart_device, write=True)
    return p


class Base(unittest.TestCase):
    def setUp(self):
        CHANGES.clear()
        self.home = tempfile.mkdtemp(prefix="canah-test-")
        self.addCleanup(shutil.rmtree, self.home, True)

    def agent(self, *script, settings=None, **kw):
        self.model = ScriptedModel(script=list(script))
        s = settings or load_settings(path="/nonexistent", home=self.home)
        a = build_agent(s, [provider()], model=self.model, memory=kw.pop("memory", None) or open_memory(in_memory=True),
                        **kw)
        self.addCleanup(a.close)
        return a


class AnswerAndTools(Base):
    def test_a_plain_question_is_answered(self):
        a = self.agent(say("hello"))
        r = a.ask("hi")
        self.assertEqual(r.text, "hello")
        self.assertFalse(r.waiting)

    def test_a_read_tool_runs_and_its_result_reaches_the_model(self):
        a = self.agent(call("list_devices"), say("two devices: r1, r2"))
        r = a.ask("which devices are there?")
        self.assertEqual(r.text, "two devices: r1, r2")
        self.assertIn('["r1", "r2"]', " ".join(self.model.tool_results()))
        self.assertEqual([c["name"] for c in r.tool_calls], ["list_devices"])

    def test_the_agent_offers_the_core_tools_and_the_systems(self):
        a = self.agent(say("ok"))
        a.ask("hi", who=ADMIN)
        bound = set(self.model.tools_bound[-1])
        for name in ("list_devices", "restart_device", "read_file", "write_file", "ls", "task"):
            self.assertIn(name, bound)


class Approvals(Base):
    def test_a_write_pauses_until_approved(self):
        a = self.agent(call("restart_device", name="r1"), say("r1 restarted"))
        r = a.ask("restart r1", thread_id="t1", who=ADMIN)
        self.assertTrue(r.waiting)
        self.assertEqual(r.pending[0]["name"], "restart_device")
        self.assertEqual(r.pending[0]["args"], {"name": "r1"})
        self.assertEqual(CHANGES, [])                      # nothing ran yet
        r = a.decide("t1", who=ADMIN, decisions=[approval.decision("approve")])
        self.assertEqual(CHANGES, ["r1"])
        self.assertEqual(r.text, "r1 restarted")
        self.assertFalse(r.waiting)

    def test_a_rejected_write_never_runs(self):
        a = self.agent(call("restart_device", name="r1"), say("left alone"))
        a.ask("restart r1", thread_id="t2", who=ADMIN)
        r = a.decide("t2", who=ADMIN, decisions=[approval.decision("reject", message="not during business hours")])
        self.assertEqual(CHANGES, [])
        self.assertIn("not during business hours", " ".join(self.model.tool_results()))
        self.assertEqual(r.text, "left alone")

    def test_an_edited_write_runs_with_the_operators_arguments(self):
        a = self.agent(call("restart_device", name="r1"), say("done"))
        a.ask("restart r1", thread_id="t3", who=ADMIN)
        a.decide("t3", who=ADMIN, decisions=[approval.decision("edit", name="restart_device", args={"name": "r2"})])
        self.assertEqual(CHANGES, ["r2"])

    def test_a_paused_thread_takes_no_new_message(self):
        a = self.agent(call("restart_device", name="r1"))
        a.ask("restart r1", thread_id="t4", who=ADMIN)
        with self.assertRaises(RuntimeError):
            a.ask("and r2", thread_id="t4", who=ADMIN)

    def test_settings_can_require_approval_for_any_tool(self):
        s = load_settings(path="/nonexistent", home=self.home, approve_tools=["list_devices"])
        a = self.agent(call("list_devices"), settings=s)
        self.assertTrue(a.ask("devices?", thread_id="t5", who=ADMIN).waiting)


class Memory(Base):
    def test_long_term_memory_is_shared_across_threads_of_one_user(self):
        m = open_memory(in_memory=True)
        a = self.agent(call("write_file", file_path="/memories/prefs.md", content="operator prefers short answers"),
                       say("noted"), call("read_file", file_path="/memories/prefs.md"), say("short answers"),
                       memory=m)
        a.ask("remember: short answers", thread_id="a", who=ADMIN)
        a.ask("what do I prefer?", thread_id="b", who=ADMIN)
        self.assertIn("operator prefers short answers", " ".join(self.model.tool_results()))

    def test_one_users_memories_are_not_anothers(self):
        m = open_memory(in_memory=True)
        a = self.agent(call("write_file", file_path="/memories/secret.md", content="ana's note"), say("noted"),
                       call("read_file", file_path="/memories/secret.md"), say("?"), memory=m)
        a.ask("remember", thread_id="a1", who=ADMIN)
        a.ask("what did I note?", thread_id="b1", who=Identity("bo", "admin"))
        self.assertNotIn("ana's note", " ".join(self.model.tool_results()))

    def test_scratch_files_stay_in_their_thread(self):
        a = self.agent(call("write_file", file_path="/notes.md", content="draft"), say("ok"),
                       call("read_file", file_path="/notes.md"), say("?"))
        a.ask("note it", thread_id="a")
        a.ask("read it", thread_id="b")
        self.assertNotIn("draft", " ".join(self.model.tool_results()))

    def test_a_conversation_survives_a_restart(self):
        s = load_settings(path="/nonexistent", home=self.home)
        m1 = open_memory(Path(self.home))
        a = build_agent(s, [provider()], model=ScriptedModel(script=[say("first")]), memory=m1)
        a.ask("one", thread_id="keep")
        a.close()
        model = ScriptedModel(script=[say("second")])
        b = build_agent(s, [provider()], model=model, memory=open_memory(Path(self.home)))
        self.addCleanup(b.close)
        b.ask("two", thread_id="keep")
        said = [str(m.content) for m in model.seen[-1]]
        self.assertIn("one", said)
        self.assertIn("first", said)

    def test_agents_md_is_in_the_prompt(self):
        a = self.agent(say("ok"))
        a.ask("hi")
        self.assertIn("The operator decides every change", self.model.system_text())


class Skills(Base):
    def test_skills_are_listed_in_the_prompt(self):
        a = self.agent(say("ok"))
        a.ask("hi")
        text = self.model.system_text()
        self.assertIn("approvals", text)
        self.assertIn("incident-triage", text)

    def test_a_skill_can_be_read_but_not_changed(self):
        a = self.agent(say("ok"))
        a.ask("hi")
        path = [p for p in a.info["skills"]][0] + "approvals/SKILL.md"
        b = self.agent(call("read_file", file_path=path), call("write_file", file_path=path.replace(
            "SKILL.md", "NEW.md"), content="x"), say("done"))
        b.ask("look")
        results = self.model.tool_results()
        self.assertIn("Proposing a change", results[0])
        self.assertTrue(any("denied" in r.lower() or "permission" in r.lower() for r in results[1:]), results)

    def test_a_systems_own_skills_layer_on_top(self):
        d = Path(self.home) / "myskills" / "vlan-howto"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: vlan-howto\ndescription: How VLANs are added here.\n---\nSteps.\n")
        s = load_settings(path="/nonexistent", home=self.home)
        s.skills_dirs.append(str(d.parent))
        a = self.agent(say("ok"), settings=s)
        a.ask("hi")
        self.assertIn("vlan-howto", self.model.system_text())


class Subagents(Base):
    def test_the_main_agent_delegates_and_the_subagent_sees_only_its_tools(self):
        spec = {"name": "observer", "description": "Reads device state.", "tools": ["list_devices"]}
        a = self.agent(call("task", description="list the devices", subagent_type="observer"),
                       call("list_devices"), say("r1 and r2"), say("The observer found r1 and r2."),
                       extra_subagents=[spec])
        r = a.ask("what devices?")
        self.assertEqual(r.text, "The observer found r1 and r2.")
        sub_tools = [b for b in self.model.tools_bound if "list_devices" in b and "restart_device" not in b]
        self.assertTrue(sub_tools, self.model.tools_bound)

    def test_a_subagent_naming_an_unknown_tool_is_refused(self):
        with self.assertRaises(ValueError):
            self.agent(extra_subagents=[{"name": "x", "description": "y", "tools": ["nope"]}])

    def test_subagents_from_yaml(self):
        y = Path(self.home) / "subagents.yaml"
        y.write_text("subagents:\n  - name: observer\n    description: Reads state.\n    tools: [list_*]\n")
        s = load_settings(path="/nonexistent", home=self.home, subagents_file=str(y))
        a = self.agent(say("ok"), settings=s)
        self.assertIn("observer", a.info["subagents"])


class Access(Base):
    def test_an_observer_is_never_offered_a_write(self):
        a = self.agent(say("an admin would have to approve a restart"))
        a.ask("restart r1")                                   # no identity: observer
        self.assertNotIn("restart_device", self._offered())
        self.assertIn("list_devices", self._offered())
        self.assertIn("cannot change the system", self.model.system_text())

    def _offered(self):
        # The tools the model was actually shown on its last call (the middleware narrows them per call).
        return self.model.offered[-1]

    def test_an_admin_is_offered_writes(self):
        a = self.agent(say("ok"))
        a.ask("hi", who=ADMIN)
        self.assertIn("restart_device", self._offered())

    def test_an_observer_cannot_approve_but_can_reject(self):
        a = self.agent(call("restart_device", name="r1"), say("left alone"))
        a.ask("restart r1", thread_id="o1", who=ADMIN)
        with self.assertRaises(PermissionError):
            a.decide("o1", [approval.decision("approve")])  # observer
        self.assertEqual(CHANGES, [])
        a.decide("o1", [approval.decision("reject")])
        self.assertEqual(CHANGES, [])

    def test_a_root_only_tool_needs_root(self):
        s = load_settings(path="/nonexistent", home=self.home, root_tools=["restart_*"])
        a = self.agent(call("restart_device", name="r1"), say("done"), settings=s)
        r = a.ask("restart r1", thread_id="r1", who=ADMIN)
        self.assertEqual(r.pending[0]["needs"], "root")
        with self.assertRaises(PermissionError):
            a.decide("r1", [approval.decision("approve")], who=ADMIN)
        a.decide("r1", [approval.decision("approve")], who=Identity("rex", "root"))
        self.assertEqual(CHANGES, ["r1"])

    def test_sign_in(self):
        from canah.identity import Accounts
        os.environ["CANAH_ADMIN_PASSWORD"] = "pw-admin"
        self.addCleanup(os.environ.pop, "CANAH_ADMIN_PASSWORD")
        acc = Accounts()
        self.assertEqual(acc.sign_in("admin", "pw-admin"), Identity("admin", "admin"))
        with self.assertRaises(PermissionError):
            acc.sign_in("admin", "nope")
        for _ in range(6):
            try:
                acc.sign_in("admin", "nope")
            except PermissionError as e:
                last = str(e)
        self.assertIn("locked", last)
        with self.assertRaises(PermissionError):
            acc.sign_in("admin", "pw-admin")                  # locked even with the right password


class MCPConfig(unittest.TestCase):
    def test_servers_are_read_and_secrets_come_from_the_environment(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        f = Path(d) / "mcp.json"
        f.write_text(json.dumps({"mcpServers": {
            "ctl": {"url": "http://h:9000/mcp", "headers": {"Authorization": "Bearer ${T_TOKEN}"}},
            "old": {"url": "http://h:1/sse"},
            "local": {"command": "python", "args": ["-m", "x"]},
            "off": {"url": "http://h:2/mcp", "enabled": False}}}))
        os.environ["T_TOKEN"] = "s3cret"
        self.addCleanup(os.environ.pop, "T_TOKEN")
        c = mcp.connections(str(f))
        self.assertEqual(set(c), {"ctl", "old", "local"})
        self.assertEqual(c["ctl"]["transport"], "streamable_http")
        self.assertEqual(c["ctl"]["headers"]["Authorization"], "Bearer s3cret")
        self.assertEqual(c["old"]["transport"], "sse")
        self.assertEqual(c["local"]["transport"], "stdio")

    def test_write_tools_are_left_out_unless_allowed_and_then_need_approval(self):
        from canah.tools import as_tool

        def ctl__list_devices() -> str:
            """List."""
            return ""

        def ctl__create_service(name: str) -> str:
            """Create."""
            return ""

        def ctl__get_state() -> str:
            """Get."""
            return ""
        tools = [as_tool(f) for f in (ctl__list_devices, ctl__create_service, ctl__get_state)]
        kept, writes, dropped = mcp.split(tools, [])
        self.assertEqual([t.name for t in kept], ["ctl__list_devices", "ctl__get_state"])
        self.assertEqual(dropped, ["ctl__create_service"])
        kept, writes, dropped = mcp.split(tools, ["ctl__create_*"])
        self.assertEqual(writes, ["ctl__create_service"])


class Settings(unittest.TestCase):
    def test_yaml_then_environment(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        f = Path(d) / "canah.yaml"
        f.write_text("name: my-agent\nrecursion_limit: 40\napprove_tools: [a, b]\n")
        os.environ["CANAH_RECURSION_LIMIT"] = "55"
        self.addCleanup(os.environ.pop, "CANAH_RECURSION_LIMIT")
        s = load_settings(path=str(f), home=d)
        self.assertEqual((s.name, s.recursion_limit, s.approve_tools), ("my-agent", 55, ["a", "b"]))

    def test_an_unknown_setting_is_an_error(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        f = Path(d) / "canah.yaml"
        f.write_text("modle: x\n")
        with self.assertRaises(ValueError):
            load_settings(path=str(f))


if __name__ == "__main__":
    unittest.main()
