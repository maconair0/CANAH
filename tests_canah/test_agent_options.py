"""Middleware and interrupts given to the agent itself (build_agent and the settings)."""
import unittest

from langchain.agents.middleware import AgentMiddleware

from canah import FunctionProvider, approval, build_agent, load_settings
from canah.identity import Identity
from canah.memory import open_memory

from tests_canah.fakes import ScriptedModel, call, say

ADMIN = Identity("ana", "admin")
RAN = []


def list_items() -> str:
    """List the items."""
    RAN.append("list")
    return "a, b"


def export_items() -> str:
    """Export every item to a file."""
    RAN.append("export")
    return "exported"


def delete_item(name: str) -> str:
    """Delete one item."""
    RAN.append(f"delete {name}")
    return "deleted"


class Counter(AgentMiddleware):
    """Counts model calls: stands for any middleware a system adds."""
    calls = 0

    def wrap_model_call(self, request, handler):
        Counter.calls += 1
        return handler(request)


def provider():
    p = FunctionProvider()
    p.add(list_items)
    p.add(export_items)
    p.add(delete_item, write=True)
    return p


def agent(*script, **kw):
    model = ScriptedModel(script=list(script))
    s = load_settings(path="/x", sandbox="none", **kw.pop("settings", {}))
    a = build_agent(s, [provider()], model=model, memory=open_memory(in_memory=True), **kw)
    return a, model


class Middleware(unittest.TestCase):
    def setUp(self):
        Counter.calls = 0

    def test_given_to_build_agent_it_runs_on_every_model_call(self):
        a, _ = agent(call("list_items"), say("a and b"), middleware=[Counter()])
        a.ask("list", who=ADMIN)
        self.assertEqual(Counter.calls, 2)
        self.assertIn("Counter", a.info["middleware"])

    def test_named_in_the_settings_it_is_loaded(self):
        a, _ = agent(say("ok"), settings={"middleware": ["tests_canah.test_agent_options:Counter"]})
        a.ask("hi")
        self.assertEqual(Counter.calls, 1)

    def test_subagents_run_it_too(self):
        spec = {"name": "lister", "description": "Lists items.", "tools": ["list_items"]}
        a, _ = agent(call("task", description="list", subagent_type="lister"), call("list_items"), say("a, b"),
                     say("Done."), middleware=[Counter()], extra_subagents=[spec])
        a.ask("list them", who=ADMIN)
        self.assertEqual(Counter.calls, 4)                 # 2 main + 2 subagent


class Interrupts(unittest.TestCase):
    def setUp(self):
        RAN.clear()

    def test_a_read_can_be_made_to_pause(self):
        a, _ = agent(call("export_items"), say("exported"), interrupt_on={"export_*": True})
        r = a.ask("export", thread_id="e", who=ADMIN)
        self.assertEqual([p["name"] for p in r.pending], ["export_items"])
        self.assertEqual(RAN, [])
        a.decide("e", [approval.decision("approve")], who=ADMIN)
        self.assertEqual(RAN, ["export"])
        self.assertEqual(a.needs("export_items"), "admin")

    def test_from_the_settings_with_its_own_decisions(self):
        a, _ = agent(call("delete_item", name="a"), say("kept"),
                     settings={"interrupt_on": {"delete_item": {"allowed_decisions": ["approve", "reject"],
                                                                "description": "Deleting is final."}}})
        self.assertEqual(a.interrupt_config("delete_item")["description"], "Deleting is final.")
        r = a.ask("delete a", thread_id="d", who=ADMIN)
        self.assertEqual(r.pending[0]["description"], "Deleting is final.")
        with self.assertRaises(ValueError):
            a.decide("d", [approval.decision("edit", name="delete_item", args={"name": "b"})], who=ADMIN)
        a.decide("d", [approval.decision("reject")], who=ADMIN)
        self.assertEqual(RAN, [])

    def test_a_write_cannot_be_switched_off(self):
        with self.assertRaises(ValueError):
            agent(interrupt_on={"delete_item": False})

    def test_reject_cannot_be_taken_away(self):
        with self.assertRaises(ValueError):
            agent(interrupt_on={"delete_item": {"allowed_decisions": ["approve"]}})

    def test_an_observer_is_not_offered_a_paused_read(self):
        a, model = agent(say("an admin can export"), interrupt_on={"export_items": True})
        a.ask("export")
        self.assertNotIn("export_items", model.offered[-1])
        self.assertIn("list_items", model.offered[-1])


if __name__ == "__main__":
    unittest.main()
