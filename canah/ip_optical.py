"""
IP connectivity between two sites over the optical network: what exists, what is missing, and the steps to make it.

"Connect A and B over optical, then check they reach each other" is three changes in two domains, in an order the
network dictates, and an agent that guesses it builds a flow rule on one switch (it did) or a lightpath nobody uses.
This reads what the controllers publish, through the agentic MCP, and answers with the chain and the gaps:

  routers     the packet devices at each end (by site, or by name)
  handoffs    the router ports that hand off to an optical transponder ("hands off to" relations)
  lightpath   one in service between the two ends' transponders, or not; if not, transponders free at each end
  IP link     the packet link between the two handoff ports, and whether it is up (it rides the lightpath)
  IP service  a service between the two routers over that link, and whether it is up
  hosts       a host at each end, to ping from and to

and the steps, in order, each for the agent that owns it (optical config agent: the lightpath; packet config
agent: the IP service), then the ping that proves it. It changes nothing. Where the controllers do not publish
what is needed (no relations, no sites), it says so instead of guessing.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple


def _rows(text: Any, keys=()) -> List[Dict[str, Any]]:
    try:
        data = json.loads(text) if isinstance(text, str) else text
    except (TypeError, ValueError):
        return []
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for k in keys:
            if isinstance(data.get(k), list):
                return [r for r in data[k] if isinstance(r, dict)]
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return [r for r in v if isinstance(r, dict)]
    return []


def _name(r: Dict[str, Any]) -> str:
    return str(r.get("name") or r.get("id") or r.get("node-id") or "")


def _bare(x: str) -> str:
    return str(x).split(":", 1)[1] if ":" in str(x) and str(x).split(":", 1)[0] in _DOMAINS else str(x)


_DOMAINS: set = set()


class Planner:
    def __init__(self, provider: Any):
        self.p = provider

    def _agent(self, controller: str) -> str:
        """The config agent that changes this controller (the team), or the supervisor itself (no team)."""
        for name, r in (getattr(self.p, "roles", {}) or {}).items():
            if controller in (r.get("controllers") or []):
                return name
        return "you (a change, through its approval)"

    def _read(self, verb: str, key: str) -> Any:
        vt = self.p.verb_tools.get(f"net_{verb}")
        if vt is None or key not in vt.served:
            return None
        got = self.p.service.call(key, vt.served[key], {})
        return got.get("text") if not got.get("is_error") else None

    def _relations(self) -> List[Dict[str, Any]]:
        out = []
        for key, tools in self.p.catalog.items():
            if "list_relations" in tools:
                try:
                    out += [r for r in _rows(self.p.service.call(key, "list_relations", {}).get("text")) if r.get("from")]
                except Exception:  # noqa: BLE001
                    continue
        return out

    def plan(self, a: str, z: str, rate_gbps: int = 100) -> Dict[str, Any]:
        self.p._load()
        domains = {k: str(v.get("domain") or "").lower() for k, v in (self.p.status.get("controllers") or {}).items()}
        global _DOMAINS
        _DOMAINS = set(domains) | {"packet", "optical", "fabric"}
        packet = [k for k, d in domains.items() if d in ("packet", "ip", "l3")]
        optical = [k for k, d in domains.items() if d in ("optical", "wdm", "photonic")]
        if not packet or not optical:
            return {"ok": False, "why": f"needs a packet and an optical controller; there are packet: {packet or 'none'}, "
                                        f"optical: {optical or 'none'}"}
        rel = self._relations()

        def at(site_or_name: str, rows: List[Dict[str, Any]]) -> List[str]:
            q = site_or_name.lower()
            hit = [_name(r) for r in rows if _name(r).lower() == q]
            if hit:
                return hit
            return [_name(r) for r in rows if q and (q == str(r.get("site", "")).lower()
                                                     or re.search(rf"(^|[_\-.]){re.escape(q)}([_\-.]|$)", _name(r).lower()))]

        devices = {k: _rows(self._read("read_devices", k), ("routers", "devices", "nodes", "switches")) for k in packet + optical}
        prow = [r for k in packet for r in devices[k]]
        orow = [r for k in optical for r in devices[k]]
        ends: Dict[str, Dict[str, Any]] = {}
        gaps: List[str] = []
        for label, q in (("a", a), ("z", z)):
            routers = at(q, prow)
            site = next((str(r.get("site")) for r in prow if _name(r) in routers and r.get("site")), q)
            ends[label] = {"asked": q, "site": site, "routers": routers,
                           "transponders": [n for n in at(site, orow) if re.search(r"xpdr|xponder|-x\d|transp", n, re.I)]
                           or [_name(r) for r in orow if str(r.get("node-type", "")).upper() == "XPONDER"
                               and str(r.get("site", "")).lower() == site.lower()]}
            if not routers:
                gaps.append(f"no packet router found for {q!r} (by name or site)")
        # handoffs: a router port that hands off to an optical transponder
        hand: Dict[str, Tuple[str, str]] = {}            # router -> (port, transponder)
        for r in rel:
            if "hands off" in str(r.get("relation")):
                f, t = _bare(r["from"]), _bare(r["to"])
                for port, xp in ((f, t), (t, f)):
                    router = port.split(":")[0]
                    if any(router in e["routers"] for e in ends.values()) and ":" in port:
                        hand.setdefault(router, (port, xp.split()[0]))
        for label, e in ends.items():
            e["handoff"] = next((dict(router=rt, port=hand[rt][0], transponder=hand[rt][1]) for rt in e["routers"] if rt in hand), None)
            if e["routers"] and not e["handoff"]:
                gaps.append(f"no published handoff from {', '.join(e['routers'])} to an optical transponder: the "
                            f"controllers do not say which transponder serves it")
        steps: List[Dict[str, Any]] = []
        out = {"ok": True, "a": ends["a"], "z": ends["z"], "gaps": gaps}
        if gaps:
            out.update(ok=False, why="; ".join(gaps))
            return out
        ha, hz = ends["a"]["handoff"], ends["z"]["handoff"]
        # the lightpath between the two transponders
        lps = []
        for k in optical:
            for r in _rows(self._read("read_services", k), ("lightpaths", "services")):
                ea, ez = str(r.get("a") or r.get("a_end") or ""), str(r.get("z") or r.get("z_end") or "")
                lps.append({"controller": k, "name": _name(r), "ends": {ea, ez},
                            "state": str(r.get("state") or r.get("status") or ""), "rate": r.get("rate_gbps")})
        joined = next((l for l in lps if l["ends"] == {ha["transponder"], hz["transponder"]}), None)
        busy = {e for l in lps for e in l["ends"]}
        in_service = bool(joined) and re.search(r"in-?service|up|active", joined["state"], re.I) is not None
        out["lightpath"] = ({"name": joined["name"], "state": joined["state"], "controller": joined["controller"]}
                            if joined else None)
        if not in_service:
            blocked = [x for x in (ha["transponder"], hz["transponder"]) if x in busy and not joined]
            if blocked:
                out.update(ok=False, why=f"{', '.join(blocked)} already carr{'ies' if len(blocked) == 1 else 'y'} another "
                                         f"lightpath, so no new one can end there: one lightpath per transponder")
                return out
            steps.append({"step": len(steps) + 1, "agent": self._agent(optical[0]),
                          "do": "create the lightpath", "controller": optical[0], "verb": "provision",
                          "arguments": {"name": f"LP-{ha['router']}-{hz['router']}", "a": ha["transponder"],
                                        "z": hz["transponder"], "rate_gbps": int(rate_gbps)},
                          "why": f"no lightpath in service joins {ha['transponder']} and {hz['transponder']}, the "
                                 f"transponders behind {ha['port']} and {hz['port']}"})
        # the packet link between the two handoff ports
        plink, psvc = None, None
        for k in packet:
            for l in _rows(self._read("read_links", k), ("links",)):
                if {str(l.get("a")), str(l.get("z"))} == {ha["port"], hz["port"]}:
                    plink = {"controller": k, "name": _name(l), "status": str(l.get("status") or l.get("state") or "")}
            for s in _rows(self._read("read_services", k), ("services",)):
                if {str(s.get("a", "")).split(":")[0], str(s.get("z", "")).split(":")[0]} == {ha["router"], hz["router"]}:
                    psvc = {"controller": k, "name": _name(s), "status": str(s.get("status") or ""), "over": s.get("over")}
        out["ip_link"], out["ip_service"] = plink, psvc
        if not plink:
            out.update(ok=False, why=f"no packet link joins {ha['port']} and {hz['port']}: the IP side over this "
                                     f"lightpath is not cabled in the packet controller")
            return out
        if psvc:
            out["note"] = (f"{psvc['name']} already joins {ha['router']} and {hz['router']}"
                           + (f" over {psvc['over']}" if psvc.get("over") else "")
                           + (": it comes up with the lightpath; do not create another." if not in_service else "."))
        if not psvc:
            steps.append({"step": len(steps) + 1, "agent": self._agent(packet[0]),
                          "do": "create the IP service between the routers over the link", "controller": packet[0],
                          "verb": "provision", "arguments": {"a": ha["router"], "z": hz["router"], "over": plink["name"]},
                          "why": f"no service joins {ha['router']} and {hz['router']}; name it and give it a free VLAN"})
        # hosts to prove it
        hosts = {}
        for k in packet:
            for h in _rows(self._read("read_hosts", k), ("hosts",)):
                for label, e in ends.items():
                    if str(h.get("router")) == e["handoff"]["router"] or str(h.get("site")) == e["site"]:
                        hosts.setdefault(label, {"name": _name(h), "ip": h.get("ip"), "controller": k})
        out["hosts"] = hosts
        if hosts.get("a") and hosts.get("z"):
            steps.append({"step": len(steps) + 1, "agent": "you (a read)", "do": "verify",
                          "tool": "net_ping", "controller": hosts["a"]["controller"],
                          "arguments": {"src": hosts["a"]["name"], "dst": hosts["z"]["ip"] or hosts["z"]["name"]},
                          "why": "a service configured is not a service that carries traffic: send a ping"})
        else:
            steps.append({"step": len(steps) + 1, "agent": "you", "do": "verify",
                          "why": "no host is published at both ends to ping from; read the link and service back instead"})
        out["steps"] = steps
        out["already"] = not [s for s in steps if s["do"] != "verify"]
        out["next"] = ("Nothing to change: verify with the ping." if out["already"] else
                       "Hand the first change step to its domain's config agent (it waits for approval), then call "
                       "ip_over_optical again: it reads the network anew and gives only what is left. Ping last.")
        return out
