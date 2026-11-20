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
