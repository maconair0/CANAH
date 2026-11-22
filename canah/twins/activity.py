"""
What the digital twin is doing, as it does it: every twin step an agent takes (the example it asks for, the scenario
it validates, the run it predicts, a write it checks before sending), as events a page can follow.

  Activity.begin(step, ...) -> op id      a step started: which thread, which agent, which domain, the network
  Activity.end(op, ...)                   it finished: ok or not, the twin's answer in brief
  Activity.since(seq, who, thread)        the events after `seq`, and the sessions still running

Each event carries a `view`: the network the step is about, as a graph a page can draw by domain (nodes, links,
what fails, the lightpaths' routes, the demands, which requirements held). From a scenario for a what-if; from the
controller's own topology, with the objects the write touches marked, for a write check. Nothing is invented: a
step whose network is not known (the twin's own mirror, an example not yet filled in) says so and has no graph.
"""
from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional

KEEP = 600            # events kept in memory
IDLE_S = 6.0          # a session with nothing running for this long is over

STEP_TITLE = {"precheck": "tries the proposed change, before it is approved", "schema": "reads the example scenario", "validate": "checks the scenario", "predict": "runs the scenario",
              "check_write": "tries the change before it is sent", "gate": "checks the change as it is sent",
              "extra": "uses a helper", "list": "says what it covers"}


def _ctx() -> Dict[str, str]:
    """The thread, user and agent of the tool call running now (empty outside one)."""
    out = {"thread": "", "user": "", "agent": ""}
    try:
        from langgraph.config import get_config
        c = (get_config() or {}).get("configurable") or {}
        out["thread"] = str(c.get("thread_id") or "")
        out["user"] = str(c.get("user_id") or "")
    except Exception:  # noqa: BLE001
        pass
    try:
        from ..safety import _DEPTH
        out["agent"] = str(_DEPTH.get() or "")
    except Exception:  # noqa: BLE001
        pass
    return out


# ---------- a network, as a graph to draw ----------

def _ids(xs: Any) -> List[Dict[str, Any]]:
    return [x for x in (xs or []) if isinstance(x, dict) and x.get("id") not in (None, "")]


def scenario_view(sc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A scenario's network as {domain, nodes, links, failed, paths, demands, requirements}, or None when it holds no
    network yet (an example the agent has not filled in is still drawn: it is what the twin was given)."""
    if not isinstance(sc, dict):
        return None
    dom = str(sc.get("domain") or "")
    topo = sc.get("topology") or {}
    tr = sc.get("traffic") or {}
    nodes: List[Dict[str, Any]] = []
    links: List[Dict[str, Any]] = []
    if dom in ("ip", "optical", "packet") or topo.get("nodes"):
        for n in _ids(topo.get("nodes")):
            nodes.append({"id": str(n["id"]), "kind": str(n.get("kind") or n.get("type") or ("roadm" if dom == "optical" else "router")),
                          **({"lat": n["lat"], "lon": n["lon"]} if "lat" in n and "lon" in n else {})})
        for l in _ids(topo.get("links")):
            lab = (f"{l['length_km']:g} km" if isinstance(l.get("length_km"), (int, float)) else
                   f"{l['rate_mbps']:g} Mb/s" if isinstance(l.get("rate_mbps"), (int, float)) else "")
            links.append({"id": str(l["id"]), "a": str(l.get("a")), "z": str(l.get("z")), "label": lab})
    elif dom == "wifi":
        aps = _ids(topo.get("aps"))
        for a in aps:
            nodes.append({"id": str(a["id"]), "kind": "ap", "x": a.get("x", 0), "y": a.get("y", 0)})
        for s in _ids(topo.get("stations")):
            nodes.append({"id": str(s["id"]), "kind": "station", "x": s.get("x", 0), "y": s.get("y", 0)})
            if aps:      # the nearest access point: the station's likely association, drawn as a radio link
                near = min(aps, key=lambda a: (a.get("x", 0) - s.get("x", 0)) ** 2 + (a.get("y", 0) - s.get("y", 0)) ** 2)
                links.append({"id": f"{s['id']}~{near['id']}", "a": str(s["id"]), "z": str(near["id"]), "kind": "radio", "label": ""})
    elif dom == "ntn":
        cons = _ids(topo.get("constellations"))
        for s in _ids(topo.get("sites")):
            nodes.append({"id": str(s["id"]), "kind": str(s.get("kind") or "site"), "lat": s.get("lat_deg"), "lon": s.get("lon_deg")})
        for c in cons:
            nodes.append({"id": str(c["id"]), "kind": "constellation", "label": f"{c.get('planes', '?')}×{c.get('per_plane', '?')} at {c.get('altitude_km', '?')} km"})
            for s in _ids(topo.get("sites")):
                links.append({"id": f"{s['id']}~{c['id']}", "a": str(s["id"]), "z": str(c["id"]), "kind": "radio", "label": ""})
    known = {n["id"] for n in nodes}
    for l in links:                   # an end named only by a link is still a node
        for end in (l["a"], l["z"]):
            if end and end not in known:
                nodes.append({"id": end, "kind": "node"})
                known.add(end)
    failed = [str(e.get("target")) for e in ((sc.get("change") or {}).get("events") or [])
              if isinstance(e, dict) and e.get("target") is not None]
    events = [{"t": e.get("t"), "type": str(e.get("type") or ""), "target": str(e.get("target") or ""), "value": e.get("value")}
              for e in ((sc.get("change") or {}).get("events") or []) if isinstance(e, dict)]
    paths = [{"id": str(lp["id"]), "src": str(lp.get("src") or ""), "dst": str(lp.get("dst") or ""),
              "links": [str(x) for x in (lp.get("path") or [])], "label": f"{lp['rate_gbps']}G" if lp.get("rate_gbps") else ""}
             for lp in _ids(tr.get("lightpaths"))]
    demands = [{"id": str(d["id"]), "src": str(d.get("src") or ""), "dst": str(d.get("dst") or ""), "over": str(d.get("lightpath") or ""),
                "label": f"{d['rate_mbps']:g} Mb/s" if isinstance(d.get("rate_mbps"), (int, float)) else ""}
               for d in _ids(tr.get("demands")) + _ids(tr.get("flows"))]
    reqs = [{"demand": str(r.get("demand") or ""), "kpi": str(r.get("kpi") or ""), "op": str(r.get("op") or ""), "value": r.get("value")}
            for r in (sc.get("requirements") or []) if isinstance(r, dict)]
    if not nodes:
        return None
    return {"domain": dom or "network", "source": "scenario", "question": str(sc.get("question") or ""),
            "nodes": nodes[:200], "links": links[:400], "failed": failed, "events": events, "paths": paths,
            "demands": demands, "requirements": reqs}


def controller_view(topology: Dict[str, Any], controller: str, arguments: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The controller's own network, from the last topology read, with the objects a write names marked, and a new
    connection drawn dashed when the write names two ends that are both in it."""
    ctl = next((c for c in (topology or {}).get("controllers") or [] if c.get("key") == controller), None)
    if not ctl or not ctl.get("devices"):
        return None
    nodes = [{"id": str(d.get("id")), "kind": str(d.get("type") or d.get("kind") or "device"), "label": d.get("label") or ""}
             for d in ctl["devices"] if d.get("id")][:200]
    ids = {n["id"] for n in nodes}
    links = []
    for l in (ctl.get("links") or [])[:400]:
        a, z = str(l.get("a") or "").split(":")[0], str(l.get("z") or "").split(":")[0]
        if a in ids and z in ids:
            links.append({"id": str(l.get("id")), "a": a, "z": z, "label": "", "down": l.get("state") == "down"})
    words = {str(v) for v in _flat(arguments)}
    touched = [n["id"] for n in nodes if n["id"] in words] + [l["id"] for l in links if l["id"] in words]
    ends = [str(arguments.get(k)) for k in ("a", "src", "source", "a_end", "from") if arguments.get(k)][:1] + \
           [str(arguments.get(k)) for k in ("z", "dst", "destination", "z_end", "to") if arguments.get(k)][:1]
    proposed = []
    if len(ends) == 2 and all(e.split(":")[0] in ids for e in ends):
        proposed.append({"id": str(arguments.get("name") or "new"), "a": ends[0].split(":")[0], "z": ends[1].split(":")[0],
                         "label": f"{arguments['rate_gbps']}G" if arguments.get("rate_gbps") else ""})
    return {"domain": str(ctl.get("domain") or controller), "source": "controller", "controller": controller,
            "nodes": nodes, "links": links, "touched": touched, "proposed": proposed,
            "failed": [l["id"] for l in links if l.get("down")], "paths": [], "demands": [], "requirements": []}


def _flat(x: Any) -> List[Any]:
    if isinstance(x, dict):
        return [y for v in x.values() for y in _flat(v)]
    if isinstance(x, (list, tuple)):
        return [y for v in x for y in _flat(v)]
    return [x]


def outcome(result: Any) -> Dict[str, Any]:
    """The twin's answer in brief, from a compact result or a check."""
    if not isinstance(result, dict):
        return {}
    pred = result.get("prediction") if isinstance(result.get("prediction"), dict) else result
    out = {}
    if result.get("action"):
        out["action"] = result["action"]
    if isinstance(pred.get("decision"), dict):
        out["decision"] = pred["decision"].get("action")
        out["reasons"] = (pred["decision"].get("reasons") or [])[:3]
    if isinstance(pred.get("probability"), dict):
        out["p"] = pred["probability"].get("p")
    if isinstance(pred.get("trust"), dict):
        out["trust"] = pred["trust"].get("tier")
    if pred.get("run_id"):
        out["run_id"] = pred["run_id"]
    figs = []
    for f in (pred.get("figures") or [])[:12]:
        ch = (f.get("changed") or {}) if isinstance(f, dict) else {}
        figs.append({"entity": f.get("entity"), "kpi": f.get("kpi"), "mean": ch.get("mean")})
    if figs:
        out["figures"] = figs
    if result.get("action") and (result.get("reasons") or result.get("questions")):   # a write check's own words
        out["reasons"] = [str(x) for x in (result.get("reasons") or [])][:3]
        out["questions"] = [str(x) for x in (result.get("questions") or [])][:3]
    if result.get("problems"):
        out["problems"] = [str(p) for p in result["problems"]][:4]
    if result.get("ok") is False and "problems" not in out:
        out["problems"] = ["the scenario is not valid"]
    return out


# ---------- the feed ----------

class Activity:
    def __init__(self, topology: Optional[Callable[[], Dict[str, Any]]] = None):
        self.topology = topology        # set by the service: the last topology read, for write checks
        self._events: deque = deque(maxlen=KEEP)
        self._seq = itertools.count(1)
        self._lock = threading.Lock()
        self._running: Dict[str, Dict[str, Any]] = {}

    def _add(self, ev: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            ev["seq"] = next(self._seq)
            self._events.append(ev)
        return ev

    def begin(self, step: str, twin: str = "", domain: str = "", view: Optional[Dict[str, Any]] = None,
              detail: str = "", ctx: Optional[Dict[str, str]] = None) -> str:
        c = {**_ctx(), **{k: v for k, v in (ctx or {}).items() if v}}
        op = f"op{int(time.time() * 1000)}-{len(self._running)}-{step}"
        ev = {"op": op, "at": time.time(), "state": "running", "step": step, "title": STEP_TITLE.get(step, step),
              "twin": twin, "domain": domain or (view or {}).get("domain", ""), "detail": detail[:300], "view": view, **c}
        with self._lock:
            self._running[op] = ev
        self._add(dict(ev))
        return op

    def end(self, op: str, ok: bool = True, result: Any = None, view: Optional[Dict[str, Any]] = None, error: str = "") -> None:
        with self._lock:
            start = self._running.pop(op, None)
        if start is None:
            return
        ev = {**start, "at": time.time(), "state": "done" if ok else "error", "took_s": round(time.time() - start["at"], 2),
              "outcome": outcome(result), "error": error[:300]}
        if view is not None:
            ev["view"] = view
        self._add(ev)

    def since(self, seq: int = 0, user: str = "", admin: bool = False, thread: str = "") -> Dict[str, Any]:
        with self._lock:
            every = list(self._events)
            running = list(self._running.values())
        evs = [e for e in every if e["seq"] > seq]
        last = every[-1] if every else None
        mine = lambda e: (admin or not e.get("user") or e.get("user") == user) and (not thread or e.get("thread") == thread)
        evs = [e for e in evs if mine(e)]
        now = time.time()
        busy = sorted({e.get("thread", "") for e in running if mine(e)})
        recent = sorted({e.get("thread", "") for e in every if mine(e) and now - e["at"] < IDLE_S})
        return {"seq": last["seq"] if last else 0, "events": evs[-200:], "running": busy,
                "active": sorted(set(busy) | set(recent))}


FEED = Activity()       # one per process: the agent is rebuilt on a reload, what the twin did is not forgotten
