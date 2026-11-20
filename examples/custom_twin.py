"""
A custom digital twin, as small as one can be: the reference for plugging your own twin into CANAH.

It speaks the canah.twin/1 contract (docs/TWIN_CONTRACT.md) over HTTP, with the standard library only. Its
model is deliberately simple and says so: links with a capacity, demands on their shortest path (by hop
count) sharing each link in proportion to their rates. No queues, no protocols, no randomness. Replace
`run()` with your own model and keep the rest.

  python examples/custom_twin.py --port 8430
  # then, in CANAH's Digital twins page: add an HTTP twin at http://127.0.0.1:8430

Scenario (domain "flow"):
  {"domain": "flow",
   "links":   [{"id": "L1", "a": "A", "z": "B", "capacity_mbps": 100}],
   "demands": [{"id": "D1", "src": "A", "dst": "B", "rate_mbps": 30}],
   "change":  {"links_down": ["L1"], "set_capacity": {"L2": 200}, "add_demands": [...]},
   "requirements": [{"demand": "D1", "kpi": "loss_ratio", "op": "<", "value": 0.01}]}
"""
from __future__ import annotations

import argparse
import copy
import json
import operator
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

CONTRACT = "canah.twin/1"
OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}
EXAMPLE = {"domain": "flow",
           "links": [{"id": "L-ab", "a": "A", "z": "B", "capacity_mbps": 100},
                     {"id": "L-ac", "a": "A", "z": "C", "capacity_mbps": 40},
                     {"id": "L-cb", "a": "C", "z": "B", "capacity_mbps": 40}],
           "demands": [{"id": "acme", "src": "A", "dst": "B", "rate_mbps": 30},
                       {"id": "globex", "src": "A", "dst": "C", "rate_mbps": 20}],
           "change": {"links_down": ["L-ab"]},
           "requirements": [{"demand": "acme", "kpi": "loss_ratio", "op": "<", "value": 0.01}]}


def describe() -> Dict[str, Any]:
    return {"contract": CONTRACT, "name": "flow-example", "version": "1",
            "about": "a fluid model: capacities and shortest paths, nothing else (CANAH's reference custom twin)",
            "domains": [{"domain": "flow", "title": "Fluid flows", "answers": "whether demands still fit after a link "
                         "fails, a capacity changes or traffic is added", "kpis": ["throughput_mbps", "loss_ratio"],
                         "example": EXAMPLE, "fidelity": "fluid: proportional sharing on shortest paths",
                         "not_modelled": "queues, delay, protocols, rerouting policy other than shortest path, any randomness"}],
            "capabilities": ["validate", "predict"]}


def validate(sc: Any) -> Dict[str, Any]:
    p: List[str] = []
    if not isinstance(sc, dict) or sc.get("domain") != "flow":
        return {"ok": False, "problems": ['the scenario is a JSON object with "domain": "flow"']}
    links = {l.get("id"): l for l in sc.get("links") or []}
    nodes = {n for l in links.values() for n in (l.get("a"), l.get("z"))}
    for d in sc.get("demands") or []:
        for end in (d.get("src"), d.get("dst")):
            if end not in nodes:
                p.append(f"demand {d.get('id')}: no node {end!r}")
    for lid in (sc.get("change") or {}).get("links_down") or []:
        if lid not in links:
            p.append(f"change: no link {lid!r}")
    ids = {d.get("id") for d in (sc.get("demands") or []) + ((sc.get("change") or {}).get("add_demands") or [])}
    for r in sc.get("requirements") or []:
        if r.get("demand") not in ids:
            p.append(f"requirement: no demand {r.get('demand')!r}")
        if r.get("kpi") not in ("throughput_mbps", "loss_ratio") or r.get("op") not in OPS:
            p.append(f"requirement on {r.get('demand')}: kpi throughput_mbps or loss_ratio, op one of {list(OPS)}")
    return {"ok": not p, "problems": p}


def run(links: List[Dict[str, Any]], demands: List[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    """Each demand on its shortest path, links shared in proportion to the rates on them."""
    adj: Dict[str, List[tuple]] = {}
    for l in links:
        adj.setdefault(l["a"], []).append((l["z"], l["id"]))
        adj.setdefault(l["z"], []).append((l["a"], l["id"]))
    paths: Dict[str, Optional[List[str]]] = {}
    for d in demands:
        prev = {d["src"]: None}
        q = deque([d["src"]])
        while q:
            n = q.popleft()
            for m, lid in adj.get(n, []):
                if m not in prev:
                    prev[m] = (n, lid)
                    q.append(m)
        if d["dst"] not in prev:
            paths[d["id"]] = None
            continue
        path, n = [], d["dst"]
        while prev[n]:
            path.append(prev[n][1])
            n = prev[n][0]
        paths[d["id"]] = path
    load: Dict[str, float] = {}
    for d in demands:
        for lid in paths[d["id"]] or []:
            load[lid] = load.get(lid, 0.0) + d["rate_mbps"]
    cap = {l["id"]: l["capacity_mbps"] for l in links}
    out = {}
    for d in demands:
        path = paths[d["id"]]
        share = 0.0 if path is None else min([1.0] + [cap[l] / load[l] for l in path])
        out[d["id"]] = {"throughput_mbps": d["rate_mbps"] * share, "loss_ratio": 1 - share}
    return out


def apply(sc: Dict[str, Any], change: Dict[str, Any]) -> tuple:
    links = [l for l in copy.deepcopy(sc["links"]) if l["id"] not in set(change.get("links_down") or [])]
    for l in links:
        if l["id"] in (change.get("set_capacity") or {}):
            l["capacity_mbps"] = float(change["set_capacity"][l["id"]])
    return links, copy.deepcopy(sc.get("demands") or []) + copy.deepcopy(change.get("add_demands") or [])


def predict(sc: Any) -> Dict[str, Any]:
    v = validate(sc)
    if not v["ok"]:
        return {"contract": CONTRACT, "ok": False, "problems": v["problems"]}
    change = sc.get("change")
    base = run(sc["links"], sc.get("demands") or [])
    now = run(*apply(sc, change)) if change else base
    reqs = sc.get("requirements") or []
    holds = all(r["demand"] in now and OPS[r["op"]](now[r["demand"]][r["kpi"]], r["value"]) for r in reqs)
    held_before = all(r["demand"] in base and OPS[r["op"]](base[r["demand"]][r["kpi"]], r["value"]) for r in reqs)
    if not reqs:
        action, why = "gather_more_data", ["no requirement was stated: add one"]
    elif change:
        action, why = ("propose", ["every requirement holds with the change"]) if holds else ("reject", ["a requirement breaks with the change"])
    else:
        action, why = ("met", ["every requirement holds today"]) if holds else ("not_met", ["a requirement does not hold today"])
    fig = lambda v: {"mean": v, "p5": v, "p50": v, "p95": v}  # noqa: E731 - deterministic: one value
    figures = [{"entity": r["demand"], "kpi": r["kpi"], "changed": fig(now[r["demand"]][r["kpi"]]),
                "baseline": fig(base[r["demand"]][r["kpi"]]) if change and r["demand"] in base else None,
                "effect": fig(now[r["demand"]][r["kpi"]] - base[r["demand"]][r["kpi"]]) if change and r["demand"] in base else None}
               for r in reqs]
    return {"contract": CONTRACT, "ok": True, "twin": {"name": "flow-example", "version": "1", "engine": "fluid"},
            "domain": "flow", "run_id": uuid.uuid4().hex[:12], "decision": {"action": action, "reasons": why},
            "probability": {"p": 1.0 if holds else 0.0, "interval": None, "members": 1},
            "baseline_probability": {"p": 1.0 if held_before else 0.0, "members": 1} if change else None,
            "trust": {"tier": "C", "why": "an uncalibrated analytic model"}, "figures": figures, "requirements": reqs,
            "change_ran": change, "not_modelled": describe()["domains"][0]["not_modelled"],
            "fidelity": "fluid"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        return self._send(200, describe()) if self.path == "/twin" else self._send(404, {"error": "not found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/twin/validate":
            return self._send(200, validate(body.get("scenario")))
        if self.path == "/twin/predict":
            return self._send(200, predict(body.get("scenario")))
        return self._send(404, {"error": "not found"})


def serve(port: int = 8430, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8430)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    print(f"flow-example twin (canah.twin/1) on http://{a.host}:{a.port}/twin", flush=True)
    serve(a.port, a.host).serve_forever()
