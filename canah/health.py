"""
The networks' health over time: at every watcher reading, each controller's devices, links and services, how
many of each are down, and the incidents open. From the contract's reads, whatever the vendor.

  Series(home).add(at, rows)                 rows: [{controller, devices, devices_down, links, links_down,
                                             services, services_down, reachable}] and incidents_active
  Series(home).get(hours, controller)        the samples in the window, oldest first, thinned to at most `points`
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

COLS = ("devices", "devices_down", "links", "links_down", "services", "services_down", "reachable", "incidents")
KEEP_DAYS = 30


class Series:
    def __init__(self, home: Any):
        self._db = sqlite3.connect(str(Path(home) / "health.sqlite"), check_same_thread=False, isolation_level=None)
        self._db.execute(f"create table if not exists health (at real, controller text, {', '.join(c + ' integer' for c in COLS)})")
        self._db.execute("create index if not exists health_at on health(controller, at)")
        self._lock = threading.Lock()
        self._added = 0

    def add(self, at: float, rows: List[Dict[str, Any]], incidents: Dict[str, int]) -> None:
        with self._lock:
            for r in rows:
                self._db.execute(f"insert into health values (?, ?, {', '.join('?' for _ in COLS)})",
                                 (at, r["controller"], *[int(r.get(c) or 0) for c in COLS[:-1]],
                                  int(incidents.get(r["controller"], 0))))
            self._added += 1
            if self._added % 100 == 0:
                self._db.execute("delete from health where at < ?", (time.time() - KEEP_DAYS * 86400,))

    def get(self, hours: float = 24.0, controller: str = "", points: int = 240) -> Dict[str, List[Dict[str, Any]]]:
        cut = time.time() - hours * 3600
        with self._lock:
            q = "select * from health where at >= ?" + (" and controller = ?" if controller else "") + " order by at"
            cur = self._db.execute(q, (cut, controller) if controller else (cut,))
            names = [d[0] for d in cur.description]
            rows = [dict(zip(names, r)) for r in cur.fetchall()]
        out: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            out.setdefault(r.pop("controller"), []).append(r)
        for k, v in out.items():              # at most `points` per controller: every n-th, and always the last
            if len(v) > points:
                step = len(v) / points
                out[k] = [v[int(i * step)] for i in range(points - 1)] + [v[-1]]
        return out


def sample(topology: Dict[str, Any], services: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Each controller's counts from one topology read and its services."""
    rows = []
    for c in topology.get("controllers") or []:
        devs, links, svcs = c.get("devices") or [], c.get("links") or [], services.get(c["key"]) or []
        rows.append({"controller": c["key"], "reachable": 1 if c.get("ok") else 0,
                     "devices": c.get("devices_total") or len(devs), "devices_down": sum(d.get("status") == "down" for d in devs),
                     "links": len(links), "links_down": sum(l.get("state") == "down" for l in links),
                     "services": len(svcs), "services_down": sum(not s.get("up", True) for s in svcs)})
    return rows
