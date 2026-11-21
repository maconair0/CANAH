"""An answer states only values a tool returned (canah.grounding)."""
import unittest

from canah import FunctionProvider, build_agent, load_settings
from canah.grounding import specifics
from canah.memory import open_memory

from tests_canah.fakes import ScriptedModel, call, say


def read_power() -> str:
    """Read the transmit power."""
    return "tx-power: -2.37, serial 9f3a1c2b7e4d"


def agent(*script):
    p = FunctionProvider()
    p.add(read_power)
    model = ScriptedModel(script=list(script))
    a = build_agent(load_settings(path="/x", sandbox="none"), [p], model=model, memory=open_memory(in_memory=True))
    return a, model


class Grounding(unittest.TestCase):
    def test_what_counts_as_a_checkable_value(self):
        self.assertEqual(specifics("6 nodes at 100G, -2.37, 18446744073709551616, id 9f3a1c2b7e4d"),
                         ["-2.37", "18446744073709551616", "9f3a1c2b7e4d"])

    def test_an_invented_value_is_sent_back(self):
        a, model = agent(call("read_power"), say("Power is -3.15."), say("Power is -2.37."))
        self.assertEqual(a.ask("tx power?").text, "Power is -2.37.")
        self.assertIn("[grounding check]", str(model.seen[-1][-1].content))
        self.assertIn("-3.15", str(model.seen[-1][-1].content))

    def test_a_grounded_answer_passes(self):
        a, model = agent(call("read_power"), say("Power is -2.37 (serial 9f3a1c2b7e4d)."))
        self.assertEqual(a.ask("tx power?").text, "Power is -2.37 (serial 9f3a1c2b7e4d).")
        self.assertEqual(len(model.seen), 2)

    def test_it_is_sent_back_only_once(self):
        a, model = agent(call("read_power"), say("It is 1234.5."), say("Still 1234.5."))
        self.assertEqual(a.ask("tx power?").text, "Still 1234.5.")

    def test_an_invented_device_name_is_sent_back(self):
        a, model = agent(call("read_power"), say("Read from node-99: -2.37."), say("-2.37."))
        self.assertEqual(a.ask("tx power?").text, "-2.37.")
        self.assertIn("node-99", str(model.seen[-1][-1].content))

    def test_a_name_from_earlier_in_the_conversation_passes(self):
        a, model = agent(say("ok"), call("read_power"), say("node-17 transmits at -2.37."))
        a.ask("we are looking at node-17", thread_id="c")
        self.assertEqual(a.ask("its tx power?", thread_id="c").text, "node-17 transmits at -2.37.")

    def test_quoting_its_own_call_passes(self):
        a, model = agent(call("read_power"), say("I ran `read_power`: -2.37."))
        self.assertEqual(a.ask("tx power?").text, "I ran `read_power`: -2.37.")
        self.assertEqual(len(model.seen), 2)

    def test_names_without_a_digit_in_output_position(self):
        from canah.grounding import names
        self.assertEqual(names("Running as: canah-user"), ["canah-user"])
        self.assertEqual(names("The command ran as user 'agent'."), ["agent"])
        self.assertEqual(names("Saved to: /workspace/nodes.json"), ["/workspace/nodes.json"])
        for prose in ("Status: up", "It is read-only and end-to-end, multi-domain.", "Note: this is fine.",
                      'He said "hello there" twice.', "It's the operator's choice."):
            self.assertEqual(names(prose), [], prose)

    def test_an_invented_user_name_is_sent_back(self):
        def whoami() -> str:
            """Who runs the commands."""
            return "agent"
        from canah import FunctionProvider
        p = FunctionProvider()
        p.add(whoami)
        model = ScriptedModel(script=[call("whoami"), say("Running as: canah-user"), say("Running as: agent")])
        a = build_agent(load_settings(path="/x", sandbox="none"), [p], model=model, memory=open_memory(in_memory=True))
        self.assertEqual(a.ask("who runs it?").text, "Running as: agent")
        self.assertIn("canah-user", str(model.seen[-1][-1].content))

    def test_a_quoted_word_the_tool_returned_passes(self):
        def whoami() -> str:
            """Who runs the commands."""
            return "agent"
        from canah import FunctionProvider
        p = FunctionProvider()
        p.add(whoami)
        model = ScriptedModel(script=[call("whoami"), say("It ran as user 'agent'.")])
        a = build_agent(load_settings(path="/x", sandbox="none"), [p], model=model, memory=open_memory(in_memory=True))
        self.assertEqual(a.ask("who runs it?").text, "It ran as user 'agent'.")

    def test_a_suggested_command_is_not_a_claim(self):
        from canah.grounding import names
        self.assertEqual(names('Corrected command: `echo "today is $(date +%Y)"`'), [])
        self.assertEqual(names("It was saved as `nodes-17.json`."), ["nodes-17.json"])

    def test_an_announced_step_is_taken(self):
        a, model = agent(say("I have the data. Let me first check the power."), call("read_power"),
                         say("Power is -2.37."))
        self.assertEqual(a.ask("tx power?").text, "Power is -2.37.")
        self.assertIn("[follow-through check]", str(model.seen[1][-1].content))

    def test_a_stall_is_nudged(self):
        a, model = agent(say("I now have all the necessary information to do this. The power is read below."),
                         call("read_power"), say("-2.37."))
        self.assertEqual(a.ask("tx power?").text, "-2.37.")

    def test_saying_it_lacks_a_value_its_tools_can_get_is_nudged(self):
        a, model = agent(say("The limit is 22.0, but I don't have the current power measurement for it."),
                         call("read_power"), say("-2.37."))
        self.assertEqual(a.ask("tx power vs limit?").text, "-2.37.")

    def test_asking_to_confirm_in_text_is_sent_back_for_someone_who_may_approve(self):
        from canah.identity import Identity
        a, model = agent(say("VS-1 serves a customer. Should I delete it? Please confirm."), call("read_power"),
                         say("ok"))
        a.ask("delete VS-1", who=Identity("ana", "admin"))
        self.assertIn("That pause is the confirmation", str(model.seen[1][-1].content))

    def test_the_ways_of_asking_are_recognised(self):
        from canah.grounding import _ASKS
        for t in ("Would you like to proceed with the deletion despite these dependencies? If so, I'll need your approval.",
                  "Do you still want me to delete it?", "Shall I go ahead?", "Please confirm.", "Should I proceed?"):
            self.assertTrue(_ASKS.search(t), t)
        for t in ("It is deleted.", "The service is up and serves globex."):
            self.assertFalse(_ASKS.search(t), t)

    def test_an_observer_is_not_pushed_to_change_anything(self):
        a, model = agent(say("That needs an admin. Would you like me to proceed once one signs in? Please confirm."))
        a.ask("delete VS-1")
        self.assertEqual(len(model.seen), 1)

    def test_a_plain_answer_is_not_nudged(self):
        a, model = agent(say("Power cannot be read on this device; ask its owner."))
        a.ask("tx power?")
        self.assertEqual(len(model.seen), 1)

    def test_a_long_answer_without_any_lookup_is_sent_back_once(self):
        long = "The tenant leaves through a border switch and then a router; " * 3
        a, model = agent(say(long), call("read_power"), say("Read it: -2.37."))
        self.assertEqual(a.ask("how does it get out?").text, "Read it: -2.37.")
        self.assertIn("[look-first check]", str(model.seen[1][-1].content))

    def test_it_may_answer_the_same_again(self):
        long = "Recursion is when a function calls itself, with a case that ends it; " * 3
        a, model = agent(say(long), say(long))
        self.assertEqual(a.ask("what is recursion?").text, long)
        self.assertEqual(len(model.seen), 2)

    def test_keep_keeps_the_first_answer_without_writing_it_again(self):
        long = "Recursion is when a function calls itself, with a case that ends it; " * 3
        a, model = agent(say(long), say("KEEP"))
        self.assertEqual(a.ask("what is recursion?", thread_id="k1").text, long)
        self.assertEqual(len(model.seen), 2)
        msgs = a.graph.get_state(a.config("k1")).values["messages"]
        self.assertEqual([type(m).__name__ for m in msgs], ["HumanMessage", "AIMessage"])   # no check, no KEEP

    def test_keep_never_reaches_the_stream(self):
        long = "Recursion is when a function calls itself, with a case that ends it; " * 3
        a, model = agent(say(long), say("KEEP"))
        events = list(a.stream("what is recursion?", thread_id="k2"))
        tokens = "".join(e["text"] for e in events if e["type"] == "token")
        self.assertNotIn("KEEP", tokens)
        self.assertEqual(events[-1]["text"], long)

    def test_a_run_at_its_step_limit_answers_instead_of_raising(self):
        p = FunctionProvider()
        p.add(read_power)
        model = ScriptedModel(script=[call("read_power") for _ in range(40)])
        a = build_agent(load_settings(path="/x", sandbox="none", recursion_limit=8), [p], model=model,
                        memory=open_memory(in_memory=True))
        r = a.ask("loop")
        self.assertIn("stopped after 8 steps", r.text)

    def test_a_request_past_its_time_budget_stops(self):
        import time
        def slow() -> str:
            """Slow."""
            time.sleep(1.2)
            return "done"
        p = FunctionProvider()
        p.add(slow)
        model = ScriptedModel(script=[call("slow") for _ in range(20)])
        a = build_agent(load_settings(path="/x", sandbox="none", max_seconds=2), [p], model=model,
                        memory=open_memory(in_memory=True))
        t = time.time()
        r = a.ask("go")
        self.assertIn("ran out of time", r.text)
        self.assertLess(time.time() - t, 8)

    def test_an_answer_without_tools_is_not_held_to_them(self):
        a, model = agent(say("2**10 is 1024."))
        self.assertEqual(a.ask("what is 2**10?").text, "2**10 is 1024.")


if __name__ == "__main__":
    unittest.main()


class Derived(unittest.TestCase):
    def test_a_rounded_figure_or_a_percentage_of_a_tool_number_is_grounded(self):
        from canah.grounding import _derived
        said = '{"p": 0.958, "margin_db": 4.83265, "loss": 25}'
        self.assertTrue(_derived("95.8", said))          # 0.958 as a percentage
        self.assertTrue(_derived("4.83", said))          # rounded to what the answer shows
        self.assertTrue(_derived("0.25", said))          # 25 % as a fraction
        self.assertFalse(_derived("4.84", said))         # not a rounding of anything returned
        self.assertFalse(_derived("96.1", said))


class StaysOnTheQuestion(unittest.TestCase):
    """What the long conversation of 2026-11-20 showed: an answer sent back by a check came back as a different
    answer, to an earlier question; and one tool was called 29 times with the same arguments."""

    def test_let_me_know_is_a_courtesy_not_an_announced_step(self):
        from canah.grounding import _ANNOUNCE
        self.assertIsNone(_ANNOUNCE.search("Here is the diagram. Let me know if you want it as a PNG."))
        self.assertIsNotNone(_ANNOUNCE.search("Let me check the links first."))

    def test_a_sent_back_answer_is_told_the_question_of_this_turn_and_may_stand(self):
        ans = "Mermaid renders it. " * 3 + "I'll check the ports next."
        a, model = agent(say("Earlier answer about TransportPCE."), say(ans), say("KEEP"))
        a.ask("is TransportPCE ready?", thread_id="q")
        r = a.ask("can we use mermaid locally?", thread_id="q")
        nudge = str(model.seen[2][-1].content)
        self.assertIn("[follow-through check]", nudge)
        self.assertIn("can we use mermaid locally?", nudge)          # anchored to this turn's question
        self.assertNotIn("TransportPCE", nudge)
        self.assertEqual(r.text, ans)                                 # KEEP: the answer stands, not rewritten
        msgs = a.graph.get_state(a.config("q")).values["messages"]
        self.assertFalse(any("check]" in str(m.content) for m in msgs))

    def test_the_same_call_a_third_time_is_not_run(self):
        calls = []
        p = FunctionProvider()

        def inspect_it(network: str) -> str:
            """Inspect a network."""
            calls.append(network)
            return "empty"
        p.add(inspect_it)
        model = ScriptedModel(script=[call("inspect_it", network="x") for _ in range(4)] + [say("It is empty.")])
        a = build_agent(load_settings(path="/x", sandbox="none"), [p], model=model, memory=open_memory(in_memory=True))
        self.assertEqual(a.ask("what is in x?").text, "It is empty.")
        self.assertEqual(len(calls), 2)
        self.assertIn("Not run again", model.tool_results(-1)[-1])


class KeepAfterALookup(unittest.TestCase):
    def test_keep_after_reading_is_sent_back_and_the_reply_is_never_keep(self):
        old = "The harness has no audit log and no roles, and nothing watches the network. " * 2
        a, model = agent(say(old), call("read_power"), say("KEEP"), say("Read it: the power is -2.37 dBm."))
        r = a.ask("what is the transmit power?", thread_id="kp")
        self.assertEqual(r.text, "Read it: the power is -2.37 dBm.")
        self.assertIn("KEEP no longer applies", str(model.seen[3][-1].content))

    def test_a_keep_tacked_on_an_answer_is_cut(self):
        a, model = agent(say("Two routers are down.\n\nKEEP"))
        self.assertEqual(a.ask("which routers are down?").text, "Two routers are down.")


class Paging(unittest.TestCase):
    def test_paging_through_one_file_stops_after_a_few_pages(self):
        p = FunctionProvider()

        def read_file(file_path: str, offset: int = 0) -> str:
            """Read a file."""
            return f"page at {offset}"
        p.add(read_file)
        model = ScriptedModel(script=[call("read_file", file_path="/big.txt", offset=i * 100) for i in range(10)] + [say("Done.")])
        a = build_agent(load_settings(path="/x", sandbox="none"), [p], model=model, memory=open_memory(in_memory=True))
        a.ask("read it all")
        self.assertIn("Compute it instead", model.tool_results(-1)[-1])


class Delegation(unittest.TestCase):
    def test_the_whatif_pattern(self):
        from canah.grounding import _WHATIF
        for q in ["What if fibre F-ab is cut?", "If the fibre carrying LP3 were cut, would LP3 keep a 3 dB margin?",
                  "Rehearse it in the digital twin.", "Is it safe to raise the rate to 400G?"]:
            self.assertTrue(_WHATIF.search(q), q)
        for q in ["Which routers are down?", "List the lightpaths.", "describe yourself"]:
            self.assertFalse(_WHATIF.search(q), q)

    def test_a_plain_change_handed_to_qot_is_turned_back_to_the_config_agent(self):
        from types import SimpleNamespace
        from langchain_core.messages import HumanMessage
        from canah.grounding import DelegationGuard
        g = DelegationGuard(config_agents=["optical-config"])
        ran = []

        def ask(question, kind="qot"):
            req = SimpleNamespace(tool_call={"name": "task", "id": "t1", "args": {"subagent_type": kind, "description": "x"}},
                                  state={"messages": [HumanMessage(content=question)]})
            return g.wrap_tool_call(req, lambda r: ran.append(question) or "ran")

        out = ask("Create a 100G lightpath named LP-3 between xpdr-c2 and xpdr-b2.")
        self.assertIn("optical-config", out.content)
        self.assertEqual(ran, [])
        self.assertEqual(ask("What if fibre F-ab is cut?"), "ran")                     # a what-if is qot's
        self.assertEqual(ask("Is it safe to create LP-3 at 400G?"), "ran")
        self.assertEqual(ask("Create LP-3.", kind="optical-config"), "ran")             # the right agent: untouched


class TaskShare(unittest.TestCase):
    def test_a_delegated_task_ends_at_its_share_of_the_time(self):
        import time
        from types import SimpleNamespace
        from canah.grounding import TimeBudget, _TASK_DEADLINE
        b = TimeBudget(task_seconds=0.01)
        seen = []

        def subagent(_):
            time.sleep(0.03)
            seen.append(b.before_model({"messages": []}, None))
            return "done"

        b.wrap_tool_call(SimpleNamespace(tool_call={"name": "task"}), subagent)
        self.assertEqual(seen[0]["jump_to"], "end")
        self.assertIn("share of time", seen[0]["messages"][0].content)
        self.assertEqual(_TASK_DEADLINE.get(), 0.0)                                     # the supervisor's own steps go on
        self.assertIsNone(b.before_model({"messages": []}, None))


class ChangeAndDocs(unittest.TestCase):
    def test_an_admins_change_answered_with_a_question_is_sent_back_once(self):
        from unittest import mock
        from langchain_core.messages import AIMessage, HumanMessage
        from canah.grounding import DelegationGuard
        g = DelegationGuard(config_agents=["data-centre-config"])
        msgs = [HumanMessage(content="Connect tenant blue to VS-100."), AIMessage(content="Would you like me to proceed?")]
        got = {}
        for level in ("admin", "observer"):
            with mock.patch("langgraph.config.get_config", return_value={"configurable": {"level": level}}):
                got[level] = g.after_model({"messages": msgs}, None)
        self.assertIn("data-centre-config", got["admin"]["messages"][0].content)
        self.assertIsNone(got["observer"])                       # an observer is told it needs an admin instead

    def test_a_question_about_the_rules_answered_without_the_documents_is_sent_back(self):
        from langchain_core.messages import AIMessage, HumanMessage
        from canah.knowledge import DocsFirst
        d = DocsFirst()
        q = HumanMessage(content="Can I use VLAN 1200 for a customer? Use the design rules.")
        out = d.after_model({"messages": [q, AIMessage(content="Yes, it is free.")]}, None)
        self.assertIn("search_knowledge", out["messages"][0].content)
        searched = AIMessage(content="", tool_calls=[{"name": "search_knowledge", "args": {"query": "x"}, "id": "1"}])
        self.assertIsNone(d.after_model({"messages": [q, searched, AIMessage(content="No: reserved.")]}, None))

    def test_a_config_agent_that_made_no_write_is_sent_back_once(self):
        from langchain_core.messages import AIMessage, HumanMessage
        from canah.grounding import MakeTheChange
        g = MakeTheChange(["net_provision", "controller_write"])
        ask = HumanMessage(content="Connect tenant blue to VS-100.")
        out = g.after_model({"messages": [ask, AIMessage(content="The fabric has connect_tenant_external for this.")]}, None)
        self.assertIn("[config check]", out["messages"][0].content)
        tried = AIMessage(content="", tool_calls=[{"name": "controller_write", "args": {}, "id": "1"}])
        self.assertIsNone(g.after_model({"messages": [ask, tried, AIMessage(content="Held by the twin.")]}, None))

    def test_an_observers_change_is_not_delegated(self):
        from types import SimpleNamespace
        from unittest import mock
        from langchain_core.messages import HumanMessage
        from canah.grounding import DelegationGuard
        g = DelegationGuard(config_agents=["data-centre-config"])
        req = SimpleNamespace(tool_call={"name": "task", "id": "t", "args": {"subagent_type": "data-centre-config"}},
                              state={"messages": [HumanMessage(content="Attach tenant green to leaf-1.")]})
        with mock.patch("langgraph.config.get_config", return_value={"configurable": {"level": "observer"}}):
            out = g.wrap_tool_call(req, lambda r: "ran")
        self.assertIn("admin", out.content)

    def test_one_checks_send_back_is_not_a_new_turn_for_another(self):
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
        from canah.grounding import LookFirst
        msgs = [HumanMessage(content="Create LP-4."),
                AIMessage(content="", tool_calls=[{"name": "network_tools", "args": {}, "id": "1"}]),
                ToolMessage(content="create_lightpath", tool_call_id="1"),
                HumanMessage(content="[change check] hand it to the config agent"),
                AIMessage(content="x" * 300)]
        self.assertIsNone(LookFirst().after_model({"messages": msgs}, None))


class ModelHiccup(unittest.TestCase):
    def test_a_model_call_that_fails_once_is_tried_again(self):
        class Flaky(ScriptedModel):
            n: int = 0

            def _generate(self, *a, **k):
                self.n += 1
                if self.n == 1:
                    raise RuntimeError('llama-server returned invalid tool call arguments for "task": '
                                       'unexpected end of JSON input (status code: 500)')
                return super()._generate(*a, **k)
        m = Flaky(script=[say("Recovered.")])
        a = build_agent(load_settings(path="/x", sandbox="none"), [FunctionProvider()], model=m,
                        memory=open_memory(in_memory=True))
        self.assertEqual(a.ask("hi").text, "Recovered.")
        self.assertEqual(m.n, 2)
