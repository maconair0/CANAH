"""Subagents made from the chat: run for a task and gone, or kept and removed; read tools only."""
import shutil
import tempfile
import time
import unittest

from canah import FunctionProvider, build_agent, load_settings
from canah import user_subagents as usub
from canah.identity import Identity
from canah.memory import open_memory
from tests_canah.fakes import ScriptedModel, call, say

ADMIN, OBS = Identity("ana", "admin"), Identity("obs", "observer")
RAN = []


def list_items() -> str:
    """List the items."""
    RAN.append("list")
    return "a, b"


def delete_item(name: str) -> str:
    """Delete one item."""
    RAN.append(f"delete {name}")
    return "deleted"


class Made(unittest.TestCase):
    def setUp(self):
        RAN.clear()
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)

    def agent(self, *script):
        p = FunctionProvider()
        p.add(list_items)
        p.add(delete_item, write=True)
        model = ScriptedModel(script=list(script))
        a = build_agent(load_settings(path="/x", home=self.home, sandbox="none"), [p], model=model,
                        memory=open_memory(in_memory=True))
        self.addCleanup(a.close)
        return a, model

    def test_one_for_a_task_runs_reports_and_is_gone(self):
        a, _ = self.agent(call("spawn_subagent", name="item-auditor", instructions="You audit items.",
                               task="List the items.", tools=["list_items", "delete_item"]),
                          call("list_items"), say("Items: a, b."), say("The auditor found a and b."))
        r = a.ask("Use a subagent to audit the items.", "t1", who=ADMIN)
        self.assertEqual(RAN, ["list"])
        self.assertIn("a and b", r.text)
        self.assertEqual(usub.Store(self.home).list(), [])                     # nothing kept

    def test_tools_as_one_string_and_made_up_names_still_run(self):
        a, _ = self.agent(call("spawn_subagent", name="Item Auditor", instructions="You audit items.",
                               task="List the items.", tools="list_items, delete_item", keep="false"),
                          call("list_items"), say("a, b"), say("done"))
        a.ask("Use a subagent to audit the items.", "t1", who=ADMIN)
        self.assertEqual(RAN, ["list"])
        RAN.clear()
        b, m = self.agent(call("spawn_subagent", name="item-auditor", instructions="You audit items.",
                               task="List the items.", tools=["read_inventory"]),
                          say("nothing to read"), say("done"))
        r = b.ask("Use a subagent to audit the items.", "t2", who=ADMIN)
        self.assertEqual(r.text, "done")                       # it ran, with the usual reads instead of a made-up one
        self.assertNotIn("delete_item", m.tools_bound[1])

    def test_the_operators_words_decide_whether_it_is_kept(self):
        usub.RELOAD_SOON = lambda: None
        a, _ = self.agent(call("spawn_subagent", name="item-auditor", instructions="You audit items.", task="List."),
                          call("list_items"), say("a, b"), say("done"))
        a.ask("Create a subagent to audit the items, and keep it for 3 hours.", "t1", who=ADMIN)
        kept = usub.Store(self.home).list()
        self.assertEqual([k["name"] for k in kept], ["item-auditor"])
        self.assertAlmostEqual(kept[0]["expires_at"] - time.time(), 3 * 3600, delta=60)

    def test_making_or_removing_a_subagent_is_not_a_network_change(self):
        from canah.grounding import asks_change
        self.assertFalse(asks_change("Create a subagent called span-checker that reads the fibres."))
        usub.KNOWN.add("span-checker")
        self.addCleanup(usub.KNOWN.discard, "span-checker")
        self.assertFalse(asks_change("The span-checker's use is done: remove it."))
        self.assertTrue(asks_change("Create LP-3 from xpdr-a1 to xpdr-b1."))

    def test_saying_it_is_removed_is_not_removing_it(self):
        usub.RELOAD_SOON = lambda: None
        usub.Store(self.home).save({"name": "span-checker", "description": "d", "system_prompt": "p", "tools": ["list_items"]})
        a, _ = self.agent(say("The span-checker subagent has been removed."),
                          call("remove_subagent", name="span-checker"), say("Removed span-checker."))
        r = a.ask("The span-checker's use is done: remove it.", "t1", who=ADMIN)
        self.assertEqual(usub.Store(self.home).list(), [])           # removed, as the operator asked
        self.assertEqual(r.text, "The span-checker subagent has been removed.")   # and the answer did not change
        usub.Store(self.home).save({"name": "span-checker", "description": "d", "system_prompt": "p", "tools": ["list_items"]})
        b, _ = self.agent(say("Removed."), call("remove_subagent", name="span-checker"), say("Removed span-checker."))
        b.ask("The span-checker's use is done: remove it.", "t2", who=OBS)
        self.assertEqual([k["name"] for k in usub.Store(self.home).list()], ["span-checker"])   # an observer may not

    def test_it_never_gets_a_write(self):
        chosen = usub.choose(["*"], [t for t in FunctionProvider().tools()], ["delete_item"])
        self.assertNotIn("delete_item", [t.name for t in chosen])
        p = FunctionProvider()
        p.add(list_items)
        p.add(delete_item, write=True)
        names = [t.name for t in usub.choose(["*"], p.tools(), ["delete_item"])]
        self.assertEqual(names, ["list_items"])

    def test_kept_it_is_a_subagent_from_the_next_build_and_removed_it_is_gone(self):
        reloads = []
        usub.RELOAD_SOON = lambda: reloads.append(1)
        self.addCleanup(setattr, usub, "RELOAD_SOON", lambda: None)
        a, _ = self.agent(call("spawn_subagent", name="item-auditor", instructions="You audit items.",
                               task="List the items.", tools=["list_items"], keep=True, keep_hours=2),
                          call("list_items"), say("a, b"), say("Kept it."))
        a.ask("Make an item-auditor subagent, run it and keep it for 2 hours.", "t1", who=ADMIN)
        kept = usub.Store(self.home).list()
        self.assertEqual([k["name"] for k in kept], ["item-auditor"])
        self.assertAlmostEqual(kept[0]["expires_at"] - time.time(), 7200, delta=60)
        self.assertEqual(reloads, [1])
        b, _ = self.agent(say("ok"))
        self.assertIn("item-auditor", b.info["subagents"])
        c, _ = self.agent(call("remove_subagent", name="item-auditor"), say("Removed."))
        c.ask("Its use is done: remove the item-auditor.", "t2", who=ADMIN)
        self.assertEqual(usub.Store(self.home).list(), [])

    def test_keeping_or_removing_needs_admin(self):
        a, _ = self.agent(call("spawn_subagent", name="x-agent", instructions="You list.", task="List.", keep=True),
                          say("Could not keep it."))
        a.ask("make one and keep it", "t1", who=OBS)
        self.assertEqual(usub.Store(self.home).list(), [])
        self.assertEqual(RAN, [])

    def test_one_past_its_time_is_removed(self):
        s = usub.Store(self.home)
        s.save({"name": "old-one", "description": "d", "system_prompt": "p", "tools": [], "expires_at": time.time() - 1})
        self.assertEqual(s.list(), [])


if __name__ == "__main__":
    unittest.main()
