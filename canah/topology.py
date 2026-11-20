"""
The networks' shape, for the web page: each controller the agentic MCP holds, its devices and the links
between them, read through the common contract's verbs and drawn by the page.

  Topology(service).read()  -> {"at", "controllers": [{key, label, domain, family, ok, error, verdict, gaps,
                                 verbs, devices: [{id, label, kind, status}], links: [{id, a, z, state}]}],
                                "relations": [{from, relation, to}]}

Controller-neutral: which tool serves `read_devices` and `read_links` comes from the controller's
conformance (as the agentic MCP's onboarding mapped it), and the answers are read whatever their shape:
the list of records that looks like devices, the one that looks like links (ONOS, Ryu, TeraFlowSDN,
TransportPCE's ietf-network, an emulator's own). Reads only. A controller that cannot be read is listed
with its error, never hidden.
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Optional, Tuple

_ID_KEYS = ("id", "device_id", "deviceId", "dpid", "node-id", "node_id", "name", "uuid", "device_uuid", "router",
            "switch", "key", "link-id", "link_id")
_A_KEYS = ("src", "source", "a", "a_end", "aEnd", "from", "src_device", "source-node", "local", "left")
_Z_KEYS = ("dst", "destination", "target", "z", "z_end", "zEnd", "to", "dst_device", "dest-node", "remote", "right")
_PORT_KEYS = ("port", "port_no", "portNumber", "interface", "ifname", "endpoint", "port_id", "source-tp", "dest-tp",
              "endpoint_uuid")
MAX_DEVICES = 400
MAX_LINKS = 800


def _parse(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _scalar(v: Any, depth: int = 0) -> Optional[str]:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (str, int, float)):
        return str(v) if str(v) != "" else None
    if isinstance(v, dict) and depth < 4:
        for k in (*_ID_KEYS, "elementId", "device", "node"):
            if k in v:
                got = _scalar(v[k], depth + 1)
                if got:
                    return got
    return None


def _lists(node: Any, depth: int = 0) -> Iterable[Tuple[str, List[Dict[str, Any]]]]:
    """Every list of records in an answer, with the key it sits under."""
    if depth > 5:
        return
    if isinstance(node, list):
        if node and all(isinstance(x, dict) for x in node):
            yield "", node
        for x in node[:50]:
            yield from _lists(x, depth + 1)
    elif isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                yield k, v
            if isinstance(v, (dict, list)):
                yield from _lists(v, depth + 1)
        # a dict of records keyed by id (an emulator's {"pe-a": {...}})
        if node and len(node) > 1 and all(isinstance(v, dict) for v in node.values()):
            yield "", [{"id": k, **v} for k, v in node.items()]


def _ends(r: Dict[str, Any]) -> Tuple[Any, Any]:
    a = next((r[k] for k in _A_KEYS if r.get(k) not in (None, "")), None)
    z = next((r[k] for k in _Z_KEYS if r.get(k) not in (None, "")), None)
    if a is None or z is None:
        eps = r.get("link_endpoint_ids") or r.get("endpoints") or r.get("endpoint_ids")
        if isinstance(eps, list) and len(eps) >= 2:
            a, z = eps[0], eps[1]
    return a, z


def _end_device(v: Any, devices: set) -> Optional[str]:
    if isinstance(v, dict):
        d = _scalar({k: v[k] for k in v if k not in _PORT_KEYS})
    else:
        d = _scalar(v)
    if d is None:
        return None
    if d in devices:
        return d
    for sep in ("/", ":", "-"):
        if sep in d:
            head = d.rsplit(sep, 1)[0]
            if head in devices:
                return head
    canon = _canon(d)
    if canon is not None:
        for x in devices:
            if _canon(x) == canon:
                return x
    return d


def _canon(raw: str) -> Optional[int]:
    t = raw[3:] if raw.startswith("of:") else raw
    if re.fullmatch(r"[0-9a-fA-F]{16}", t):
        return int(t, 16)
    if re.fullmatch(r"[0-9]{1,15}", t):
        return int(t)
    return None


class _Counted(list):
    """A list that also knows how many there were before the cap."""
    total = 0


def devices_of(data: Any) -> List[Dict[str, Any]]:
    """The records that look most like devices: the longest list whose records carry an id."""
    best: List[Dict[str, Any]] = []
    for key, rows in _lists(data):
        if any(w in key.lower() for w in ("link", "port", "flow", "endpoint", "service", "intent")):
            continue
        ids = [r for r in rows if _scalar(r)]
        if len(ids) > len(best):
            best = ids
    if not best and isinstance(data, list) and data and not isinstance(data[0], dict):
        best = [{"id": x} for x in data]                     # a bare list of ids (Ryu's switches)
    out, seen = _Counted(), set()
    out.total = len(best)
    for r in best[:MAX_DEVICES]:
        did = _scalar(r)
        if did in seen:
            continue
        seen.add(did)
        label = str(r.get("name") or r.get("label") or r.get("hostname") or did)
        if isinstance(r.get("name"), dict):
            label = did
        kind = str(r.get("type") or r.get("device_type") or r.get("role") or r.get("node-type") or "device")
        status = r.get("available", r.get("status", r.get("state", r.get("device_operational_status"))))
        out.append({"id": did, "label": label[:48], "kind": kind.lower()[:32],
                    "status": "down" if str(status).lower() in ("false", "down", "inactive", "disabled", "unavailable",
                                                               "device_operational_status_disabled") else "up"})
    return out


def links_of(data: Any, devices: set) -> List[Dict[str, Any]]:
    """The records that look most like links: the longest list whose records have two ends."""
    best: List[Dict[str, Any]] = []
    for key, rows in _lists(data):
        withends = [r for r in rows if all(x is not None for x in _ends(r))]
        if len(withends) > len(best):
            best = withends
    out, seen = [], set()
    for r in best[:MAX_LINKS]:
        a, z = _ends(r)
        da, dz = _end_device(a, devices), _end_device(z, devices)
        if not da or not dz or da == dz:
            continue
        key = tuple(sorted((da, dz))) + (str(r.get("name") or r.get("id") or r.get("link-id") or ""),)
        pair = tuple(sorted((da, dz)))
        if key in seen or (not key[2] and pair in {k[:2] for k in seen}):
            continue                                   # both directions of one link
        seen.add(key)
        state = str(r.get("state") or r.get("status") or r.get("operational-state") or "up").lower()
        lid = _scalar({k: r[k] for k in ("name", "id", "link-id", "link_id", "link_uuid") if k in r}) or f"{da}~{dz}"
        out.append({"id": lid[:64], "a": da, "z": dz,
                    "state": "down" if state in ("down", "inactive", "failed", "disabled") else "up"})
    return out


class Topology:
    def __init__(self, service: Any = None, ttl_s: float = 20.0, timeout_s: float = 25.0):
        if service is None:
            from .network import AgenticMCP
            service = AgenticMCP(timeout=timeout_s)
        self.service = service
        self.ttl_s = ttl_s
        self._cache: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()

    def read(self, refresh: bool = False) -> Dict[str, Any]:
        with self._lock:
            if not refresh and self._cache and time.time() - self._cache["at"] < self.ttl_s:
                return self._cache
        try:
            rows = self.service.controllers()
        except Exception as e:  # noqa: BLE001 - said on the page
            return {"at": time.time(), "ok": False, "error": f"the agentic MCP did not answer: {type(e).__name__}: {e}",
                    "url": getattr(self.service, "url", ""), "controllers": [], "relations": []}
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(rows)))) as pool:
            ctls = list(pool.map(self._one, rows))
        relations: List[Dict[str, Any]] = []
        for c in ctls:
            relations += c.pop("_relations", [])
        got = {"at": time.time(), "ok": True, "url": getattr(self.service, "url", ""), "controllers": ctls,
               "relations": relations[:500]}
        with self._lock:
            self._cache = got
        return got

    def _one(self, row: Dict[str, Any]) -> Dict[str, Any]:
        conf = row.get("conformance") or {}
        verbs: Dict[str, str] = {}
        for d in conf.get("domains") or []:
            for verb, how in (d.get("features") or {}).items():
                m = re.match(r"tool (\S+)", str(how))
                if m:
                    verbs[verb] = m.group(1)
        out = {"key": row.get("key"), "label": row.get("label") or row.get("key"), "domain": row.get("domain") or "",
               "family": row.get("family") or "", "tools": row.get("tools"), "verdict": conf.get("verdict"),
               "gaps": conf.get("gaps") or [], "verbs": verbs, "ok": not row.get("error"), "error": row.get("error"),
               "devices": [], "links": [], "_relations": []}
        if row.get("error"):
            return out
        key = row["key"]
        try:
            tools = {t["name"]: t for t in self.service.tools(key)}
        except Exception as e:  # noqa: BLE001
            out.update(ok=False, error=f"no tool list: {e}")
            return out
        # No conformance mapping (an emulator, a fresh seat): the contract's own names, then plain ones.
        for verb, pattern in (("read_devices", r"^(list|get)_(devices|routers|switches|nodes|boxes)$"),
                              ("read_links", r"^(list|get)_(links|topology|wiring|fibres|fibers|cabling|connections|lightpaths)$")):
            if verb not in verbs:
                named = sorted(t for t in tools if re.match(pattern, t) and not (tools[t].get("schema") or {}).get("required"))
                if named:
                    verbs[verb] = named[0]

        def call(tool: str) -> Any:
            if (tools.get(tool, {}).get("schema") or {}).get("required"):
                raise RuntimeError(f"{tool} needs arguments")
            got = self.service.call(key, tool, {})
            text = str(got.get("text", ""))
            if got.get("is_error") or text.lower().startswith("error"):
                raise RuntimeError(text[:240] or "error")
            return _parse(text)

        try:
            dev_data = call(verbs["read_devices"]) if "read_devices" in verbs else None
            link_data = call(verbs["read_links"]) if "read_links" in verbs else None
        except Exception as e:  # noqa: BLE001 - this controller is shown with why it could not be read
            out.update(ok=False, error=str(e)[:300])
            return out
        devices = devices_of(dev_data) if dev_data is not None else []
        total = _total if (_total := getattr(devices, "total", None)) else len(devices)
        if link_data is not None and not devices:
            devices = devices_of(link_data)
        ids = {d["id"] for d in devices}
        links = links_of(link_data, ids) if link_data is not None else []
        for l in links:                                  # ends the device list did not name (hosts, say)
            for end in (l["a"], l["z"]):
                if end not in ids:
                    ids.add(end)
                    devices.append({"id": end, "label": end[:48], "kind": "endpoint", "status": "up"})
        out.update(devices=devices[:MAX_DEVICES], links=links, devices_total=max(total, len(devices)))
        if "list_relations" in tools:
            try:
                rel = call("list_relations")
                out["_relations"] = [r for r in (rel if isinstance(rel, list) else []) if isinstance(r, dict)]
            except Exception:  # noqa: BLE001 - relations are extra
                pass
        return out
