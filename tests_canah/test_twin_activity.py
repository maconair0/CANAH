"""What the twin is doing, as it does it: scenarios and live networks as graphs, and the feed of steps."""
import unittest

from canah.twins.activity import Activity, controller_view, outcome, scenario_view

OPTICAL = {"domain": "optical", "question": "if F-ab is cut, does LP-1 keep 3 dB?",
           "topology": {"nodes": [{"id": "A"}, {"id": "B"}, {"id": "C"}],
                        "links": [{"id": "F-ab", "a": "A", "z": "B", "length_km": 240}, {"id": "F-ac", "a": "A", "z": "C", "length_km": 400},
                                  {"id": "F-cb", "a": "C", "z": "B", "length_km": 380}]},
           "traffic": {"lightpaths": [{"id": "LP-1", "src": "A", "dst": "B", "rate_gbps": 200, "path": ["F-ab"]}]},
           "change": {"events": [{"t": 0, "type": "link_down", "target": "F-ab"}]},
           "requirements": [{"demand": "LP-1", "kpi": "margin_db", "op": ">=", "value": 3}]}


class Views(unittest.TestCase):
    def test_a_scenario_is_its_nodes_links_failures_routes_and_requirements(self):
        v = scenario_view(OPTICAL)
        self.assertEqual(v["domain"], "optical")
        self.assertEqual([n["id"] for n in v["nodes"]], ["A", "B", "C"])
        self.assertEqual(v["links"][0]["label"], "240 km")
        self.assertEqual(v["failed"], ["F-ab"])
        self.assertEqual(v["paths"][0]["links"], ["F-ab"])
        self.assertEqual(v["requirements"][0]["kpi"], "margin_db")

    def test_wifi_and_satellite_scenarios_are_drawn_from_their_own_shapes(self):
        w = scenario_view({"domain": "wifi", "topology": {"aps": [{"id": "ap-1", "x": 0, "y": 0}, {"id": "ap-2", "x": 30, "y": 0}],
                                                          "stations": [{"id": "cam", "x": 8, "y": 3}]}})
        self.assertEqual({l["z"] for l in w["links"]}, {"ap-1"})            # the nearest access point
        n = scenario_view({"domain": "ntn", "topology": {"sites": [{"id": "T", "lat_deg": 45, "lon_deg": 7}],
                                                         "constellations": [{"id": "LEO", "planes": 24, "per_plane": 22}]}})
        self.assertEqual({x["kind"] for x in n["nodes"]}, {"site", "constellation"})

    def test_no_network_no_drawing(self):
        self.assertIsNone(scenario_view({"domain": "ip"}))

    def test_a_write_is_drawn_on_the_controllers_network_with_what_it_touches(self):
        topo = {"controllers": [{"key": "optical", "domain": "optical",
                                 "devices": [{"id": "xpdr-a1"}, {"id": "xpdr-c1"}, {"id": "roadm-a"}],
                                 "links": [{"id": "F-1", "a": "roadm-a", "z": "xpdr-a1", "state": "down"}]}]}
        v = controller_view(topo, "optical", {"name": "LP-9", "a": "xpdr-a1", "z": "xpdr-c1", "rate_gbps": 100})
        self.assertEqual(set(v["touched"]), {"xpdr-a1", "xpdr-c1"})
        self.assertEqual(v["proposed"][0], {"id": "LP-9", "a": "xpdr-a1", "z": "xpdr-c1", "label": "100G"})
        self.assertEqual(v["failed"], ["F-1"])
        self.assertIsNone(controller_view(topo, "packet", {}))


class Feed(unittest.TestCase):
    def test_steps_run_and_finish_and_are_seen_only_by_their_user(self):
        a = Activity()
        op = a.begin("predict", "NetTwin", view=scenario_view(OPTICAL), detail="q")
        got = a.since(0, "", True)
        self.assertEqual(got["events"][0]["state"], "running")
        self.assertEqual(len(got["running"]), 1)
        a.end(op, True, {"ok": True, "decision": {"action": "not_met", "reasons": ["x"]}, "probability": {"p": 0.1}, "run_id": "r1"})
        got = a.since(got["seq"], "", True)
        self.assertEqual(got["events"][0]["state"], "done")
        self.assertEqual(got["events"][0]["outcome"]["decision"], "not_met")
        self.assertEqual(got["running"], [])
        for e in a._events:
            e["user"] = "alice"
        self.assertEqual(a.since(0, "bob", False)["events"], [])
        self.assertEqual(len(a.since(0, "alice", False)["events"]), 2)

    def test_outcome_of_a_check(self):
        o = outcome({"action": "reject", "prediction": {"decision": {"action": "reject"}, "probability": {"p": 0.3}}})
        self.assertEqual((o["action"], o["decision"], o["p"]), ("reject", "reject", 0.3))


class NoLatency(unittest.TestCase):
    def test_trying_a_change_never_holds_up_the_reply(self):
        import threading
        import time
        from canah.twins.provider import Checks, Precheck, _key

        release = threading.Event()

        class SlowTwin:
            name = "slow"
            _desc = None

            def can_check_writes(self):
                release.wait(5)                       # a twin far away: even its description takes a while
                return True

            def check_write(self, controller, tool, args, answers):
                from canah.twins import contract as C
                return C.Check(check_id="c1", controller=controller, tool=tool, action="propose", arguments=args)

        class Reg:
            def active(self):
                return SlowTwin()

        checks = Checks()
        pre = Precheck(Reg(), checks, None, lambda: {"ctl__set"})
        t0 = time.time()
        self.assertTrue(pre.start("ctl__set", {"rate": 1}))
        self.assertLess(time.time() - t0, 0.05)       # back at once: the reply is not kept waiting
        self.assertEqual(pre.state("ctl__set", {"rate": 1})["state"], "running")   # and the card's refresh neither
        self.assertFalse(pre.start("ctl__set", {"rate": 1}))     # the same try is not started twice
        release.set()
        checks.wait_running(_key("ctl", "set", {"rate": 1}), 5)
        self.assertEqual(pre.state("ctl__set", {"rate": 1})["action"], "propose")


if __name__ == "__main__":
    unittest.main()
