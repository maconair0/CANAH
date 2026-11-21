"""
Watching the networks: incidents, opened and resolved by themselves, and runbooks that respond to them.

The watcher reads every controller the agentic MCP holds through the common contract, never a vendor's own
format, every `watch_interval_s`:
  - the agentic MCP not answering, a controller that cannot be read          (critical / major)
  - a device or a link down (the topology reader's normalised status)        (major)
  - a service whose status reads as down (read_services)                     (major)
  - an alarm (read_alarms), with the severity the controller gave it         (as given, else major)
Each finding has a fingerprint. A new one opens an incident; seen again, it is the same incident (its count
grows); missing for `resolve_after` polls in a row, it is resolved. Incidents persist across restarts.

Runbooks (YAML: canah/runbooks/, and CANAH_HOME/runbooks/) say what to do when one opens:
  name: link-down
  when: {kind: link_down, severity: [major, critical], controller: "*", text: "regex"}
  mode: notify | investigate | propose
  instructions: what CANAH should look at and how to fix it
`investigate`: CANAH looks into it in a thread of its own and writes a report. `propose`: it also rehearses a fix
in the digital twin and proposes it; the fix waits for a person's approval like any change. Nothing is applied
without one.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from .notify import Notifier, rank

logger = logging.getLogger("canah.incidents")

BUILTIN_RUNBOOKS = Path(__file__).resolve().parent / "runbooks"


# ----------------------------------------------------------------------------- the store

class Incidents:
    COLS = ("id", "fp", "controller", "severity", "kind", "title", "details", "first_seen", "last_seen", "status",
            "count", "missed", "resolved_at", "acked_by", "runbook", "mode", "thread", "report", "investigated_at", "note")

    def __init__(self, path: Any):
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("""create table if not exists incidents (id integer primary key, fp text, controller text,
            severity text, kind text, title text, details text, first_seen real, last_seen real, status text, count integer,
            missed integer, resolved_at real, acked_by text, runbook text, mode text, thread text, report text,
            investigated_at real, note text)""")
        self._db.execute("create index if not exists incidents_fp on incidents(fp, status)")
        self._lock = threading.Lock()

    def _rows(self, sql: str, args=()) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._db.execute(sql, args)
            return [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]

    def get(self, iid: int) -> Optional[Dict[str, Any]]:
        got = self._rows("select * from incidents where id = ?", (iid,))
        return got[0] if got else None

    def list(self, status: str = "", limit: int = 200, controllers: tuple = ()) -> List[Dict[str, Any]]:
        if status == "active":
            rows = self._rows("select * from incidents where status in ('open','acked') order by last_seen desc limit ?", (limit,))
        elif status:
            rows = self._rows("select * from incidents where status = ? order by last_seen desc limit ?", (status, limit))
        else:
            rows = self._rows("select * from incidents order by last_seen desc limit ?", (limit,))
        if controllers:
            rows = [r for r in rows if r["controller"] in controllers or r["controller"] == "agentic-mcp"]
        return rows

    def update(self, findings: List[Dict[str, Any]], resolve_after: int = 2, now: Optional[float] = None) -> Dict[str, List[Dict[str, Any]]]:
        """Fold one poll's findings in: what opened and what resolved."""
        now = now or time.time()
        opened, resolved = [], []
        seen = set()
        with self._lock:
            for f in findings:
                seen.add(f["fp"])
                row = self._db.execute("select id, severity from incidents where fp = ? and status in ('open','acked')", (f["fp"],)).fetchone()
                if row:
                    sev = f["severity"] if rank(f["severity"]) > rank(row[1]) else row[1]
                    self._db.execute("update incidents set last_seen = ?, count = count + 1, missed = 0, severity = ?, details = ? "
                                     "where id = ?", (now, sev, f.get("details", ""), row[0]))
                else:
                    cur = self._db.execute("insert into incidents (fp, controller, severity, kind, title, details, first_seen, "
                                           "last_seen, status, count, missed) values (?,?,?,?,?,?,?,?, 'open', 1, 0)",
                                           (f["fp"], f["controller"], f["severity"], f["kind"], f["title"], f.get("details", ""), now, now))
                    opened.append({**f, "id": cur.lastrowid})
            for iid, fp, missed in self._db.execute("select id, fp, missed from incidents where status in ('open','acked')").fetchall():
                if fp in seen:
                    continue
                if missed + 1 >= resolve_after:
                    self._db.execute("update incidents set status = 'resolved', resolved_at = ?, missed = ? where id = ?",
                                     (now, missed + 1, iid))
                    resolved.append(iid)
                else:
                    self._db.execute("update incidents set missed = ? where id = ?", (missed + 1, iid))
        return {"opened": opened, "resolved": [self.get(i) for i in resolved]}

    def set(self, iid: int, **fields: Any) -> Optional[Dict[str, Any]]:
        keep = {k: v for k, v in fields.items() if k in self.COLS and k not in ("id", "fp")}
        if keep:
            with self._lock:
                self._db.execute(f"update incidents set {', '.join(f'{k} = ?' for k in keep)} where id = ?", (*keep.values(), iid))
        return self.get(iid)

    def counts(self, controllers: tuple = ()) -> Dict[str, Any]:
        rows = self.list("active", 1000, controllers)
        by = {}
        for r in rows:
            by[r["severity"]] = by.get(r["severity"], 0) + 1
        return {"active": len(rows), "by_severity": by,
                "worst": max((r["severity"] for r in rows), key=rank, default=None)}


# ----------------------------------------------------------------------------- runbooks

def load_runbooks(dirs: List[Any]) -> List[Dict[str, Any]]:
    """Every runbook in these folders (the later ones override a runbook of the same name)."""
    found: Dict[str, Dict[str, Any]] = {}
    for d in dirs:
        d = Path(d).expanduser()
        if not d.is_dir():
            continue
        for f in sorted(list(d.glob("*.yaml")) + list(d.glob("*.yml"))):
            try:
                rb = yaml.safe_load(f.read_text()) or {}
            except yaml.YAMLError as e:
                logger.warning("runbook %s is not YAML: %s", f, e)
                continue
            if not isinstance(rb, dict) or not rb.get("name"):
                continue
            rb["mode"] = rb.get("mode") if rb.get("mode") in ("notify", "investigate", "propose") else "notify"
            rb["file"] = str(f)
            found[rb["name"]] = rb
    return list(found.values())


def match(runbooks: List[Dict[str, Any]], inc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The first runbook whose `when` fits the incident (kind, severity, controller glob, text regex)."""
    for rb in runbooks:
        w = rb.get("when") or {}
        kinds = w.get("kind")
        if kinds and inc.get("kind") not in (kinds if isinstance(kinds, list) else [kinds]):
            continue
        sev = w.get("severity")
        if sev and inc.get("severity") not in (sev if isinstance(sev, list) else [sev]):
            continue
        if w.get("min_severity") and rank(inc.get("severity")) < rank(w["min_severity"]):
            continue
        if w.get("controller") and not fnmatch.fnmatch(inc.get("controller", ""), str(w["controller"])):
            continue
        if w.get("text") and not re.search(str(w["text"]), f"{inc.get('title', '')} {inc.get('details', '')}", re.I):
            continue
        return rb
    return None


# ----------------------------------------------------------------------------- the watcher

_SEV_WORDS = {"critical": "critical", "crit": "critical", "major": "major", "minor": "minor", "warning": "warning",
              "warn": "warning", "info": "info", "informational": "info", "cleared": "info", "indeterminate": "minor"}


def alarms_of(data: Any) -> List[Dict[str, Any]]:
    """The alarms in a controller's answer to read_alarms, whatever its shape: {id, severity, text, resource}."""
    from .topology import _lists, _scalar
    best: List[Dict[str, Any]] = []
    for _, rows in _lists(data):
        if len(rows) > len(best):
            best = rows
    out = []
    for r in best[:500]:
        if not isinstance(r, dict):
            continue
        sev_raw = str(next((r[k] for k in ("severity", "perceived-severity", "perceivedSeverity", "level", "priority") if r.get(k)), "major")).lower()
        sev = next((v for k, v in _SEV_WORDS.items() if k in sev_raw), "major")
        if sev == "info" and "clear" in sev_raw:
            continue
        text = str(next((r[k] for k in ("text", "description", "message", "probable-cause", "probableCause", "alarm-type",
                                        "type", "name") if r.get(k)), "an alarm"))
        res = _scalar(next((r[k] for k in ("resource", "node-id", "node_id", "device", "circuit-pack-name", "source", "entity")
                            if r.get(k)), "")) or ""
        aid = _scalar(r.get("id") or r.get("alarm-id") or r.get("alarmId") or "") or hashlib.sha1(f"{text}|{res}".encode()).hexdigest()[:10]
        out.append({"id": aid, "severity": sev, "text": text[:300], "resource": res})
    return out


def _object(inc: Dict[str, Any]) -> str:
    fp = str(inc.get("fp") or "")
    return fp.split(":", 2)[-1] if fp.count(":") >= 2 else ""


def related_groups(group: List[Dict[str, Any]], relations: List[Dict[str, Any]],
                   controllers: Optional[List[Dict[str, Any]]] = None) -> List[List[Dict[str, Any]]]:
    """The incidents, split into the sets whose objects are linked through the published relations (what carries
    what, across controllers) and the topology (a link and the devices at its ends). An incident with no network
    object (a controller unreachable) stands alone."""
    relations = list(relations)
    for c in controllers or []:
        for l in c.get("links") or []:
            for end in (l.get("a"), l.get("z")):
                if l.get("id") and end:
                    relations.append({"from": f"{c.get('key')}:{l['id']}", "to": f"{c.get('key')}:{str(end).split(':')[0]}"})
    if len(group) < 2:
        return [group] if group else []
    parent: Dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for r in relations:
        a, b = str(r.get("from") or ""), str(r.get("to") or "")
        if a and b:
            parent[find(a)] = find(b)
    nodes = list(parent)

    def touches(obj: str) -> set:
        if not obj:
            return set()
        hit = {find(n) for n in nodes if obj == n or n.endswith(":" + obj) or n.endswith(obj) or (":" + obj + ":") in n}
        return hit or {"obj:" + obj}

    sets = [(inc, touches(_object(inc)) if inc.get("kind") != "controller_unreachable" else set()) for inc in group]
    out: List[List[Dict[str, Any]]] = []
    used = [False] * len(sets)
    for i, (inc, roots) in enumerate(sets):
        if used[i]:
            continue
        used[i] = True
        part, seen = [inc], set(roots)
        if roots:
            grew = True
            while grew:
                grew = False
                for j, (other, r2) in enumerate(sets):
                    if not used[j] and r2 and (r2 & seen):
                        used[j], grew = True, True
                        part.append(other)
                        seen |= r2
        out.append(part)
    return out


class Watcher:
    """Polls the networks through the contract; incidents, notifications, runbooks and history follow."""

    def __init__(self, topology: Any, store: Incidents, notifier: Notifier, history: Any = None,
                 runbooks: Callable[[], List[Dict[str, Any]]] = lambda: [], respond: Optional[Callable[[Dict[str, Any]], None]] = None,
                 interval_s: float = 60.0, resolve_after: int = 2, history_every: int = 5, series: Any = None,
                 heartbeat_s: float = 5.0):
        self.topology, self.store, self.notifier, self.history, self.series = topology, store, notifier, history, series
        self.runbooks, self.respond = runbooks, respond
        self.interval_s, self.resolve_after, self.history_every = interval_s, resolve_after, max(1, history_every)
        self.polls = 0
        self.last: Dict[str, Any] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.heartbeat_s = float(heartbeat_s or 0)
        self._states: Dict[str, str] = {}
        self._poll_lock = threading.Lock()
        self.beats = 0
        self.last_beat: Dict[str, Any] = {}

    # -- what the networks say

    def _verb_tools(self, svc: Any, key: str) -> Dict[str, str]:
        try:
            return {t["verb"]: t["name"] for t in svc.tools(key) if t.get("verb")}
        except Exception:  # noqa: BLE001
            return {}

    def findings(self) -> Dict[str, Any]:
        from .topology import _parse
        t = self.topology.read(refresh=True)
        out: List[Dict[str, Any]] = []
        services: Dict[str, List[Dict[str, Any]]] = {}

        def add(fp, controller, severity, kind, title, details=""):
            out.append({"fp": fp, "controller": controller, "severity": severity, "kind": kind, "title": title,
                        "details": str(details)[:1500]})
        if t.get("ok") is False:
            add("agentic-mcp:down", "agentic-mcp", "critical", "agentic_mcp_down", "The agentic MCP is not answering",
                t.get("error", ""))
            return {"findings": out, "topology": t, "services": services}
        svc = self.topology.service
        # The agentic MCP's own view of each controller: a session down or degraded, even when a read came
        # back as an answer that only says so inside it.
        try:
            sess = svc.get("/sessions", timeout=10)
            for x in (sess.get("sessions") if isinstance(sess, dict) else sess) or []:
                key, state = x.get("controller"), str(x.get("state") or "").lower()
                if not key:
                    continue
                if state == "down":
                    add(f"{key}:unreachable", key, "major", "controller_unreachable", f"{key} is not answering the agentic MCP",
                        x.get("last_error", ""))
                elif state == "degraded":
                    add(f"{key}:degraded", key, "minor", "controller_degraded", f"{key} answers the agentic MCP badly",
                        x.get("last_error", ""))
        except Exception as e:  # noqa: BLE001 - the topology read still stands
            logger.debug("sessions: %s", e)
        for c in t.get("controllers") or []:
            key, label = c["key"], c.get("label") or c["key"]
            if not c.get("ok"):
                add(f"{key}:unreachable", key, "major", "controller_unreachable", f"{label} cannot be read", c.get("error", ""))
                continue
            for d in c.get("devices") or []:
                if d.get("status") == "down":
                    add(f"{key}:device:{d['id']}", key, "major", "device_down", f"{label}: device {d.get('label') or d['id']} is down",
                        json.dumps(d)[:500])
            for l in c.get("links") or []:
                if l.get("state") == "down":
                    add(f"{key}:link:{l['id']}", key, "major", "link_down", f"{label}: link {l['id']} ({l.get('a')} - {l.get('z')}) is down",
                        json.dumps(l)[:500])
            verbs = self._verb_tools(svc, key)
            if "read_services" in verbs:
                try:
                    from .history import services_of
                    got = svc.call(key, verbs["read_services"], {})
                    if not got.get("is_error"):
                        rows = services_of(_parse(got.get("text")))
                        services[key] = rows
                        for s in rows:
                            if not s["up"]:
                                add(f"{key}:service:{s['id']}", key, "major", "service_down",
                                    f"{label}: service {s['id']} is {s['status']}", json.dumps(s))
                except Exception as e:  # noqa: BLE001 - a read that fails is not an incident by itself
                    logger.debug("services of %s: %s", key, e)
            if "read_alarms" in verbs:
                try:
                    got = svc.call(key, verbs["read_alarms"], {})
                    if not got.get("is_error"):
                        for a in alarms_of(_parse(got.get("text"))):
                            add(f"{key}:alarm:{a['id']}", key, a["severity"], "alarm",
                                f"{label}: {a['text']}" + (f" on {a['resource']}" if a["resource"] else ""), json.dumps(a))
                except Exception as e:  # noqa: BLE001
                    logger.debug("alarms of %s: %s", key, e)
        seen, unique = set(), []
        for f in out:                     # one finding per fingerprint (a session down and a failed read agree)
            if f["fp"] not in seen:
                seen.add(f["fp"])
                unique.append(f)
        return {"findings": unique, "topology": t, "services": services}

    # -- one poll

    def poll(self) -> Dict[str, Any]:
        self.polls += 1
        got = self.findings()
        changes = self.store.update(got["findings"], self.resolve_after)
        group: List[Dict[str, Any]] = []
        for inc in changes["opened"]:
            rb = match(self.runbooks(), inc)
            if rb:
                self.store.set(inc["id"], runbook=rb["name"], mode=rb["mode"])
            self.notifier.send("incident_opened", inc["severity"], inc["title"],
                               (inc.get("details") or "")[:600] + (f"\nRunbook: {rb['name']} ({rb['mode']})" if rb else ""),
                               link=f"/#incident-{inc['id']}", data={"incident": inc["id"], "controller": inc["controller"]})
            if rb and rb["mode"] in ("investigate", "propose"):
                group.append({**self.store.get(inc["id"]), "runbook_spec": rb})
        # What opened together and depends on each other is one event (a fibre cut opens the fibre, the lightpath on
        # it, the packet link it carries and the service over that): one investigation, with the evidence gathered
        # here. What is not linked (a controller that stops answering, an alarm elsewhere) is investigated on its own,
        # so one cause is not read into unrelated faults.
        for part in related_groups(group, got["topology"].get("relations") or [], got["topology"].get("controllers")):
            if not self.respond:
                break
            try:
                self.respond(part, self.evidence(part, got))
            except Exception:  # noqa: BLE001
                logger.exception("responding to incidents %s failed", [i["id"] for i in part])
        for inc in changes["resolved"]:
            if inc:
                self.notifier.send("incident_resolved", inc["severity"], f"Resolved: {inc['title']}",
                                   f"Open for {int((inc['resolved_at'] or time.time()) - inc['first_seen'])} s, seen {inc['count']} times.",
                                   link=f"/#incident-{inc['id']}", data={"incident": inc["id"]})
        if self.series is not None:
            from .health import sample
            open_by: Dict[str, int] = {}
            for r in self.store.list("active", 5000):
                open_by[r["controller"]] = open_by.get(r["controller"], 0) + 1
            self.series.add(time.time(), sample(got["topology"], got["services"]), open_by)
        recorded = []
        if self.history is not None and (self.polls == 1 or self.polls % self.history_every == 0):
            from .history import snapshot_of
            for c in got["topology"].get("controllers") or []:
                if c.get("ok"):
                    d = self.history.record(c["key"], snapshot_of(c, got["services"].get(c["key"])))
                    if d:
                        recorded.append(c["key"])
        self.last = {"at": time.time(), "findings": len(got["findings"]), "opened": len(changes["opened"]),
                     "resolved": len(changes["resolved"]), "history": recorded}
        return self.last

    def evidence(self, group: List[Dict[str, Any]], got: Dict[str, Any]) -> Dict[str, Any]:
        """What the watcher already knows about these incidents, so the investigation starts from facts rather than
        from reading everything again: the objects, what depends on them, what changed in the last hour, health now."""
        objects = sorted({i["fp"].split(":", 2)[-1] for i in group if i["fp"].count(":") >= 2})
        ctls = sorted({i["controller"] for i in group})
        rel = [r for r in (got["topology"].get("relations") or [])
               if any(o and (o in str(r.get("from", "")) or o in str(r.get("to", ""))) for o in objects)][:40]
        changes = []
        if self.history is not None:
            for c in ctls:
                for ch in self.history.changes(c, 3600)[:5]:
                    changes.append({"controller": c, "at": time.strftime("%H:%M:%S", time.localtime(ch["at"])),
                                    "changes": ch["changes"][:12]})
        health = {}
        from .health import sample
        for r in sample(got["topology"], got["services"]):
            if r["controller"] in ctls:
                health[r["controller"]] = {k: r[k] for k in ("reachable", "devices", "devices_down", "links", "links_down",
                                                             "services", "services_down")}
        return {"objects": objects, "controllers": ctls, "relations": rel, "recent_changes": changes, "health_now": health}

    # -- the heartbeat: is every controller still there, every few seconds, at almost no cost

    def heartbeat(self) -> Dict[str, Any]:
        """Each controller's health check (the contract's health verb, a light call that reads no devices), at once
        and in parallel, then the agentic MCP's view of each session. What changed since the last beat (a controller
        down, back, the agentic MCP itself gone) is returned; the loop then reads everything at once instead of
        waiting for the next full poll. Sessions are only updated by calls, so the calls are what make it current."""
        self.beats += 1
        svc = self.topology.service
        states: Dict[str, str] = {}
        try:
            keys = [c.get("key") for c in svc.controllers() if c.get("key")]
        except Exception as e:  # noqa: BLE001
            states["agentic-mcp"] = "down"
            keys = []
            logger.debug("heartbeat: agentic MCP: %s", e)
        else:
            states["agentic-mcp"] = "up"

        def ping(key):
            tool = self._verb_tools(svc, key).get("health")
            if tool:
                try:
                    svc.call(key, tool, {})
                except Exception:  # noqa: BLE001 - the session records it
                    pass
        threads = [threading.Thread(target=ping, args=(k,), daemon=True) for k in keys]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        try:
            sess = svc.get("/sessions", timeout=5)
            for x in (sess.get("sessions") if isinstance(sess, dict) else sess) or []:
                if x.get("controller"):
                    states[x["controller"]] = str(x.get("state") or "unknown").lower()
        except Exception as e:  # noqa: BLE001
            logger.debug("heartbeat sessions: %s", e)
        changed = {k: (self._states.get(k), v) for k, v in states.items() if self._states and self._states.get(k) != v}
        self._states = states
        self.last_beat = {"at": time.time(), "states": states, "changed": changed}
        return changed

    def start(self) -> "Watcher":
        def loop():
            time.sleep(5)
            next_poll = 0.0
            while not self._stop.is_set():
                now = time.time()
                changed = {}
                if self.heartbeat_s:
                    try:
                        changed = self.heartbeat()
                    except Exception:  # noqa: BLE001
                        logger.exception("heartbeat failed")
                if changed:
                    logger.info("controller state changed %s: reading now", changed)
                if changed or now >= next_poll:
                    try:
                        with self._poll_lock:
                            self.poll()
                    except Exception:  # noqa: BLE001 - the watcher keeps watching
                        logger.exception("watch poll failed")
                    next_poll = time.time() + self.interval_s
                self._stop.wait(self.heartbeat_s or self.interval_s)
        self._thread = threading.Thread(target=loop, name="canah-watch", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
