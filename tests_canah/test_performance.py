"""Performance: what the controllers measure (read_metrics), judged against limits, kept over time, watched."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from canah.incidents import Incidents, Watcher
from canah.notify import Notifier
from canah.performance import Store, findings, judge, rows_of
from testbed.domains import all_domains, pre_fec_ber


def metrics(d, key):
    return json.loads({t.name: t for t in d[key].tools()}["read_metrics"].fn())


class Judging(unittest.TestCase):
    def test_rows_of_any_shape_and_the_numbers_in_them(self):
        r = rows_of({"performance": [{"name": "LP-1", "type": "lightpath", "pre_fec_ber": 1e-4, "state": "up", "vendor": "x"},
                                     {"interface": "eth1", "metrics": {"drops_per_s": 3}}, {"no": "id"}]})
        self.assertEqual([x["object"] for x in r], ["LP-1", "eth1"])
        self.assertEqual(r[0]["metrics"], {"pre_fec_ber": 1e-4})
        self.assertEqual(r[1]["metrics"], {"drops_per_s": 3.0})

    def test_limits_either_way_and_a_controllers_own(self):
        self.assertEqual(judge("pre_fec_ber", 1e-5), "ok")
        self.assertEqual(judge("pre_fec_ber", 5e-3), "warn")
        self.assertEqual(judge("pre_fec_ber", 3e-2), "bad")
        self.assertEqual(judge("osnr_db", 12.0), "bad")             # lower is worse
        self.assertEqual(judge("cd_ps_nm", 99999), "")              # measured, no limit
        self.assertEqual(judge("pre_fec_ber", 5e-3, {"pre_fec_ber": {"warn": 1e-2, "bad": 2e-2}}), "ok")

    def test_a_value_past_its_limit_is_a_finding(self):
        f = findings("optical", "OptiCtl", rows_of([{"object": "LP-1", "kind": "lightpath", "pre_fec_ber": 0.04, "osnr_db": 21}]))
        self.assertEqual(len(f), 1)
        self.assertEqual((f[0]["fp"], f[0]["severity"], f[0]["kind"]), ("optical:perf:LP-1:pre_fec_ber", "major", "performance"))
        self.assertIn("pre-FEC BER", f[0]["title"])


class Grouping(unittest.TestCase):
    def test_a_degraded_lightpath_and_the_drops_on_the_link_it_carries_are_one_event(self):
        from canah.incidents import _object, related_groups
        a = {"fp": "optical:perf:LP-1:pre_fec_ber", "kind": "performance"}
        b = {"fp": "packet:perf:L-ab:drop_pct", "kind": "performance"}
        c = {"fp": "fabric:perf:leaf-1:cpu_pct", "kind": "performance"}
        self.assertEqual(_object(a), "LP-1")
        rel = [{"from": "optical:LP-1", "relation": "carries", "to": "packet:L-ab"}]
        parts = sorted(sorted(i["fp"] for i in g) for g in related_groups([a, b, c], rel))
        self.assertEqual(parts, [["fabric:perf:leaf-1:cpu_pct"], ["optical:perf:LP-1:pre_fec_ber", "packet:perf:L-ab:drop_pct"]])


class TestBed(unittest.TestCase):
    def test_a_degraded_lightpath_raises_its_ber_and_drops_packets_on_the_link_it_carries(self):
        d = all_domains()
        lp = {r["object"]: r for r in metrics(d, "optical")["metrics"]}["LP-1"]
        self.assertLess(lp["pre_fec_ber"], 2e-3)
        self.assertEqual({r["object"]: r for r in metrics(d, "packet")["metrics"]}["L-ab"]["crc_errors_per_s"], 0)
        d["optical"]._fault("degrade", "LP-1")
        lp = {r["object"]: r for r in metrics(d, "optical")["metrics"]}["LP-1"]
        self.assertGreater(lp["pre_fec_ber"], 1e-2)
        self.assertLess(lp["q_margin_db"], 0)
        link = {r["object"]: r for r in metrics(d, "packet")["metrics"]}["L-ab"]
        self.assertGreater(link["drop_pct"], 1)
        self.assertGreater(pre_fec_ber(12, 100), pre_fec_ber(20, 100))


class Watched(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)

    def test_the_watcher_records_measurements_and_opens_an_incident_past_a_limit(self):
        d = all_domains()
        d["optical"]._fault("degrade", "LP-1")

        class Svc:
            url = "http://fake"

            def tools(self, key):
                return [{"name": "read_metrics", "verb": "read_metrics"}] if key != "fabric" else []

            def get(self, *a, **k):
                return {"sessions": []}

            def call(self, key, tool, args, approved_by=""):
                return {"text": json.dumps(metrics(d, key))}

        class Topo:
            service = Svc()

            def read(self, refresh=False):
                return {"ok": True, "controllers": [{"key": k, "label": k, "ok": True, "devices": [], "links": []}
                                                    for k in ("optical", "packet", "fabric")]}

        store = Store(self.home)
        w = Watcher(Topo(), Incidents(self.home / "i.sqlite"), Notifier([]), perf=store)
        w.poll()
        w.poll()
        kinds = {r["fp"] for r in w.store.list("active") if r["kind"] == "performance"}
        self.assertIn("optical:perf:LP-1:pre_fec_ber", kinds)
        self.assertIn("packet:perf:L-ab:drop_pct", kinds)
        latest = store.latest()
        self.assertFalse(latest["fabric"]["published"])               # no read_metrics: said, not invented
        self.assertEqual(len(store.series("optical", "LP-1", "pre_fec_ber")["LP-1|pre_fec_ber"]), 2)


if __name__ == "__main__":
    unittest.main()
