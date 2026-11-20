"""
The networks' history: what each controller held, kept whenever it changed, and what changed between two times.

A snapshot is the controller's devices (id, status), links (id, ends, state) and services (id, status), read
through the common contract by the topology reader and the read_services verb, whatever the vendor. One is kept
only when it differs from the last (CANAH_HOME/history/<controller>/<time>.json, at most KEEP per controller).

  History(home).record(controller, snapshot)       keep it if it changed; the change, if any
  History(home).changes(controller, since_s)       every change since then: added, removed, changed objects
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

KEEP = 500
_STATUS = ("status", "state", "operational-state", "operational_state", "admin-state", "lifecycle-state", "service_status")
_DOWN = re.compile(r"down|fail|outofservice|out-of-service|inactive|disabled|error|degraded|unavailable|planned", re.I)


def services_of(data: Any) -> List[Dict[str, Any]]:
    """The services in a controller's answer to read_services, whatever its shape: {id, status, up}."""
    from .topology import _lists, _scalar
    best: List[Dict[str, Any]] = []
    for _, rows in _lists(data):
        if len(rows) > len(best) and any(isinstance(r, dict) and (r.get("id") or r.get("name") or r.get("service-name"))
                                         for r in rows):
            best = rows
    out = []
    for r in best[:2000]:
        if not isinstance(r, dict):
            continue
        sid = _scalar(r.get("id") or r.get("service-name") or r.get("name") or r.get("uuid") or r.get("service_id"))
        if not sid:
            continue
        status = next((str(r[k]) for k in _STATUS if r.get(k) not in (None, "")), "")
        out.append({"id": sid, "status": status or "unknown",
                    "up": not status or not _DOWN.search(status.replace(" ", "").replace("_", ""))})
    return out


def snapshot_of(ctl: Dict[str, Any], services: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {"devices": sorted(({"id": d["id"], "status": d.get("status")} for d in ctl.get("devices") or []), key=lambda x: x["id"]),
            "links": sorted(({"id": l["id"], "a": l.get("a"), "z": l.get("z"), "state": l.get("state")} for l in ctl.get("links") or []),
                            key=lambda x: x["id"]),
            "services": sorted(({"id": s["id"], "status": s.get("status")} for s in services or []), key=lambda x: x["id"])}


def diff(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """What changed from snapshot a to snapshot b, per kind: added, removed, changed (field: before -> after)."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for kind in ("devices", "links", "services"):
        old = {x["id"]: x for x in a.get(kind) or []}
        new = {x["id"]: x for x in b.get(kind) or []}
        rows = [{"kind": kind[:-1], "id": i, "change": "added", **{k: v for k, v in new[i].items() if k != "id"}} for i in new if i not in old]
        rows += [{"kind": kind[:-1], "id": i, "change": "removed"} for i in old if i not in new]
        for i in new.keys() & old.keys():
            moved = {k: [old[i].get(k), new[i].get(k)] for k in new[i] if k != "id" and new[i].get(k) != old[i].get(k)}
            if moved:
                rows.append({"kind": kind[:-1], "id": i, "change": "changed", "fields": moved})
        if rows:
            out[kind] = sorted(rows, key=lambda r: (r["change"], r["id"]))
    return out


class History:
    def __init__(self, home: Any):
        self.dir = Path(home) / "history"
        self._lock = threading.Lock()

    def _files(self, controller: str) -> List[Path]:
        d = self.dir / re.sub(r"[^A-Za-z0-9_.-]", "_", controller)
        return sorted(d.glob("*.json")) if d.is_dir() else []

    def record(self, controller: str, snap: Dict[str, Any], at: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Keep the snapshot if it differs from the last one; the difference (None when nothing changed)."""
        at = at or time.time()
        digest = hashlib.sha256(json.dumps(snap, sort_keys=True).encode()).hexdigest()
        with self._lock:
            files = self._files(controller)
            last = json.loads(files[-1].read_text()) if files else None
            if last and last.get("digest") == digest:
                return None
            d = self.dir / re.sub(r"[^A-Za-z0-9_.-]", "_", controller)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{at:.3f}.json").write_text(json.dumps({"at": at, "controller": controller, "digest": digest, **snap}))
            for old in files[: max(0, len(files) + 1 - KEEP)]:
                old.unlink(missing_ok=True)
            return {"at": at, "controller": controller, "first": last is None,
                    "diff": diff(last or {}, snap) if last else {}}

    def controllers(self) -> List[str]:
        return sorted(p.name for p in self.dir.iterdir() if p.is_dir()) if self.dir.is_dir() else []

    def changes(self, controller: str = "", since_s: float = 86400.0) -> List[Dict[str, Any]]:
        """Every change in the window, newest first: {at, controller, changes: [...]}."""
        cut = time.time() - since_s
        out = []
        for ctl in ([controller] if controller else self.controllers()):
            files = self._files(ctl)
            snaps = []
            for f in files:
                try:
                    at = float(f.stem)
                except ValueError:
                    continue
                snaps.append((at, f))
            prev = None
            for at, f in snaps:
                if at < cut:
                    prev = f
                    continue
                cur = json.loads(f.read_text())
                if prev is not None:
                    d = diff(json.loads(prev.read_text()), cur)
                    rows = [r for v in d.values() for r in v]
                    if rows:
                        out.append({"at": at, "controller": ctl, "changes": rows})
                prev = f
        return sorted(out, key=lambda r: -r["at"])

    def latest(self, controller: str) -> Optional[Dict[str, Any]]:
        files = self._files(controller)
        return json.loads(files[-1].read_text()) if files else None
