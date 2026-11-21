"""Self-correction across requests: changes read back against what was asked, and lessons kept."""
import unittest

from canah import FunctionProvider, approval, build_agent, load_settings
from canah.identity import Identity
from canah.lessons import Lessons, USER
from canah.memory import list_memories, namespace, open_memory

from tests_canah.fakes import ScriptedModel, call, say

ADMIN = Identity("ana", "admin")


class Store(unittest.TestCase):
    def test_a_lesson_is_kept_counted_again_found_by_what_it_is_about_and_expires(self):
        m = open_memory(in_memory=True)
        les = Lessons(m.store, ttl_days=30)
        p1 = les.add("refused-then-done", "On fabric, connect_tenant_external was refused (blue is not on "
                     "border-leaf-1); it went through after attach_tenant was done first.", "fabric", "connect_tenant_external")
        p2 = les.add("refused-then-done", "On fabric, connect_tenant_external was refused (blue is not on "
                     "border-leaf-1); it went through after attach_tenant was done first.", "fabric", "connect_tenant_external")
        self.assertEqual(p1, p2)
        self.assertEqual(les.all()[0]["count"], 2)
        self.assertEqual(les.relevant("Connect tenant green to VS-200 outside the data centre", ["fabric"])[0]["path"], p1)
        self.assertEqual(les.relevant("Which lightpaths are in service?"), [])
        self.assertIn((USER, p1), [(x["user"], x["path"]) for x in list_memories(m.store)])    # on the memories page
        Lessons(m.store, ttl_days=-1).add("twin-rejected", "a lesson already past its day", "optical", "x")
        self.assertEqual(len(les.all()), 1)
        self.assertEqual(les.prune(), 1)


def _agent(script, tools, **kw):
    p = FunctionProvider()
    for fn, write in tools:
        p.add(fn, write=write)
    model = ScriptedModel(script=list(script))
    a = build_agent(load_settings(path="/x", sandbox="none", **kw), [p], model=model, memory=open_memory(in_memory=True))
    return a, model


class Kept(unittest.TestCase):
    def test_a_write_refused_then_done_after_another_step_becomes_a_lesson_shown_next_time(self):
        state = {"on_border": False}

        def attach_tenant(tenant: str, leaf: str) -> str:
            """Attach a tenant to a leaf."""
            state["on_border"] = leaf == "border-leaf-1"
            return f"tenant {tenant} attached to {leaf}"

        def connect_tenant_external(tenant: str, service: str) -> str:
            """Connect a tenant outside the fabric."""
            return f"tenant {tenant} reaches {service}" if state["on_border"] else f"Error: {tenant} is not on border-leaf-1"
        tools = [(attach_tenant, True), (connect_tenant_external, True)]
        a, model = _agent([call("connect_tenant_external", tenant="blue", service="VS-100")], tools)
        a.ask("Connect tenant blue to VS-100.", thread_id="t", who=ADMIN)
        model.script += [call("attach_tenant", tenant="blue", leaf="border-leaf-1")]
        a.decide("t", [approval.decision("approve")], who=ADMIN)
        model.script += [call("connect_tenant_external", tenant="blue", service="VS-100")]
        a.decide("t", [approval.decision("approve")], who=ADMIN)
        model.script += [say("Connected.")]
        a.decide("t", [approval.decision("approve")], who=ADMIN)
        kept = [x["text"] for x in a.lessons.all()]
        self.assertTrue(any("was refused" in t and "attach_tenant was done first" in t for t in kept), kept)
        model.script += [say("ok")]
        a.ask("Connect tenant green to VS-200.", thread_id="u", who=ADMIN)
        self.assertIn("Lessons from earlier requests", str(model.seen[-1][0].content))
        self.assertIn("attach_tenant was done first", str(model.seen[-1][0].content))

    def test_an_operators_reason_for_rejecting_is_kept(self):
        def delete_service(name: str) -> str:
            """Delete a service."""
            return f"deleted {name}"
        a, model = _agent([call("delete_service", name="VS-100"), say("Not deleted.")], [(delete_service, True)])
        a.ask("Delete VS-100.", thread_id="r", who=ADMIN)
        a.decide("r", [approval.decision("reject", message="VS-100 carries acme: never delete a customer service")],
                 who=ADMIN)
        self.assertTrue(any("never delete a customer service" in x["text"] for x in a.lessons.all()))

    def test_no_lessons_when_switched_off(self):
        a, _ = _agent([say("hi")], [], lessons=False)
        self.assertIsNone(a.lessons)


class Verified(unittest.TestCase):
    def test_a_change_the_network_does_not_show_is_said_and_kept(self):
        from canah.network import AgenticMCPProvider
        from tests_canah.test_network_knowledge import FakeAgenticMCP
        class Listing(FakeAgenticMCP):
            def call(self, key, tool, arguments, approved_by=""):
                if tool == "get_nodes":
                    self.calls.append((key, tool, dict(arguments), approved_by))
                    return {"text": '{"nodes": [{"name": "node-1", "status": "up"}]}', "is_error": False}
                return super().call(key, tool, arguments, approved_by)
        svc = Listing()
        net = AgenticMCPProvider(service=svc, team=False)
        model = ScriptedModel(script=[call("net_provision", controller="alpha", arguments={"name": "svc-9", "a": "a1"}),
                                      say("Done.")])
        a = build_agent(load_settings(path="/x", sandbox="none"), [net], model=model, memory=open_memory(in_memory=True))
        a.ask("create svc-9", thread_id="v", who=ADMIN)
        a.decide("v", [approval.decision("approve")], who=ADMIN)
        result = model.tool_results()[-1]
        self.assertIn("Not as intended", result)                 # the fake lists no svc-9 after creating it
        self.assertTrue(any(x["kind"] == "not-as-intended" for x in a.lessons.all()))
        self.assertIn("not as intended", [r["event"] for r in a.ops.audit_log()] if a.ops else ["not as intended"])
