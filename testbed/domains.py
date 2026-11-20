"""
Three emulated network domains for testing CANAH, each a controller with its own state and tools.

  packet    routers, links between them, VLAN services; interface counters (a large read)
  optical   ROADMs and transponders, lightpaths with their OSNR; the full node list (a large read)
  fabric    a data-centre leaf-spine fabric with tenants attached to leaves

Where they meet (declared on both sides, the way real domains declare their hand-offs):

  packet pe-a eth3   <->  optical xpdr-a1 client1      (site-a)
  packet pe-b eth3   <->  optical xpdr-b1 client1      (site-b)
  packet pe-b eth4   <->  fabric border-leaf-1 port48  (site-b data centre)

A packet link between pe-a and pe-b rides an optical lightpath between xpdr-a1 and
xpdr-b1. A tenant in the fabric reaches site-a through border-leaf-1, pe-b, that
link and pe-a.

Each tool: name, description, JSON schema, write flag, function. `reset()` puts a
domain back to its starting state. A fault can be injected (`degrade`).
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List


@dataclass
class Tool:
    name: str
    description: str
    fn: Callable[..., Any]
    schema: Dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    write: bool = False


def _obj(**props):
    req = [k for k, v in props.items() if not v.pop("optional", False)]
    return {"type": "object", "properties": props, "required": req}


S = {"type": "string"}
I = {"type": "integer"}


class Domain:
    key = ""
    label = ""
    domain = ""
    START: Dict[str, Any] = {}

    def __init__(self):
        self.reset()

    def reset(self):
        self.state = copy.deepcopy(self.START)
        self.writes: List[Dict[str, Any]] = []

    def tools(self) -> List[Tool]:
        raise NotImplementedError

    def _did(self, tool, **args):
        self.writes.append({"tool": tool, **args})


class Packet(Domain):
    key, label, domain = "packet", "PacketCtl", "packet"
    START = {
        "routers": {
            "pe-a": {"site": "site-a", "status": "up", "role": "provider-edge"},
            "pe-b": {"site": "site-b", "status": "up", "role": "provider-edge"},
            "p-1": {"site": "site-a", "status": "up", "role": "core"},
            "p-2": {"site": "site-b", "status": "down", "role": "core"},
        },
        "links": [
            {"name": "L-ab", "a": "pe-a:eth3", "z": "pe-b:eth3", "status": "up",
             "carried_by": "optical lightpath LP-1 (xpdr-a1 to xpdr-b1)"},
            {"name": "L-a1", "a": "pe-a:eth1", "z": "p-1:eth1", "status": "up", "carried_by": "fibre"},
            {"name": "L-b2", "a": "pe-b:eth1", "z": "p-2:eth1", "status": "down", "carried_by": "fibre"},
        ],
        "handoffs": [
            {"port": "pe-a:eth3", "to": "optical xpdr-a1 client1"},
            {"port": "pe-b:eth3", "to": "optical xpdr-b1 client1"},
            {"port": "pe-b:eth4", "to": "fabric border-leaf-1 port48"},
        ],
        "services": {"VS-100": {"vlan": 100, "a": "pe-a", "z": "pe-b", "over": "L-ab", "customer": "acme",
                                "status": "up"},
                     "VS-200": {"vlan": 200, "a": "pe-b", "z": "pe-b:eth4", "over": "local", "customer": "globex",
                                "status": "up"}},
    }

    def tools(self):
        st = self.state

        def list_routers():
            return json.dumps(st["routers"])

        def list_links():
            return json.dumps(st["links"])

        def list_handoffs():
            return json.dumps(st["handoffs"])

        def list_services():
            return json.dumps(st["services"])

        def interface_counters(router: str):
            if router not in st["routers"]:
                return f"Error: no router {router}"
            rows = [{"interface": f"eth{i}", "in_octets": 1000003 * i + 17, "out_octets": 900007 * i + 3,
                     "errors": (7 if i == 3 and router == "pe-b" else 0), "state": "up"} for i in range(1, 401)]
            return json.dumps({"router": router, "interfaces": rows})

        def create_vlan_service(name: str, vlan: int, a: str, z: str, over: str = ""):
            if name in st["services"]:
                return f"Error: service {name} exists"
            if any(s["vlan"] == int(vlan) for s in st["services"].values()):
                return f"Error: vlan {vlan} is in use"
            for end in (a, z):
                if end.split(":")[0] not in st["routers"]:
                    return f"Error: no router {end}"
            st["services"][name] = {"vlan": int(vlan), "a": a, "z": z, "over": over or "best path", "customer": "",
                                    "status": "up"}
            self._did("create_vlan_service", name=name, vlan=int(vlan), a=a, z=z)
            return f"service {name} created (vlan {vlan}, {a} to {z})"

        def delete_service(name: str):
            if name not in st["services"]:
                return f"Error: no service {name}"
            del st["services"][name]
            self._did("delete_service", name=name)
            return f"service {name} deleted"

        def restart_router(name: str):
            if name not in st["routers"]:
                return f"Error: no router {name}"
            st["routers"][name]["status"] = "up"
            for l in st["links"]:
                if l["z"].startswith(name + ":") or l["a"].startswith(name + ":"):
                    if l["carried_by"] == "fibre":
                        l["status"] = "up"
            self._did("restart_router", name=name)
            return f"{name} restarted; it is up"

        def list_relations():
            rel = []
            for l in st["links"]:
                if "lightpath" in l["carried_by"]:
                    lp = l["carried_by"].split("lightpath ")[1].split()[0]
                    rel.append({"from": f"optical:{lp}", "relation": "carries", "to": f"packet:{l['name']}"})
            for name, svc in st["services"].items():
                if svc.get("over") and svc["over"] not in ("local", "best path"):
                    rel.append({"from": f"packet:{svc['over']}", "relation": "carries", "to": f"packet:{name}"})
                if str(svc.get("z", "")).count(":"):
                    rel.append({"from": f"packet:{name}", "relation": "uses port", "to": f"packet:{svc['z']}"})
                if svc.get("customer"):
                    rel.append({"from": f"packet:{name}", "relation": "serves customer", "to": f"customer:{svc['customer']}"})
            for h in st["handoffs"]:
                dom, obj = h["to"].split(" ", 1)
                rel.append({"from": f"packet:{h['port']}", "relation": "hands off to", "to": f"{dom}:{obj.split()[0]}"})
            return json.dumps(rel)

        return [
            Tool("list_relations", "What depends on what in this domain, and where it meets others: rows "
                 "{from, relation, to}.", list_relations),
            Tool("list_routers", "List the routers with site, role and status.", list_routers),
            Tool("list_links", "List the links between routers, their status and what carries each.", list_links),
            Tool("list_handoffs", "List the ports that hand off to other domains, and to what.", list_handoffs),
            Tool("list_services", "List the VLAN services: vlan, ends, the link they use, customer, status.",
                 list_services),
            Tool("interface_counters", "Every interface's counters on one router (in/out octets, errors, state).",
                 interface_counters, _obj(router=dict(S))),
            Tool("create_vlan_service", "Create a VLAN service between two routers (or router:port), optionally over "
                 "a named link.", create_vlan_service,
                 _obj(name=dict(S), vlan=dict(I), a=dict(S), z=dict(S), over=dict(S, optional=True)), write=True),
            Tool("delete_service", "Delete a VLAN service by name.", delete_service, _obj(name=dict(S)), write=True),
            Tool("restart_router", "Restart a router.", restart_router, _obj(name=dict(S)), write=True),
        ]


class Optical(Domain):
    key, label, domain = "optical", "OptiCtl", "optical"
    START = {
        "nodes": {**{f"roadm-{s}": {"site": f"site-{s}", "node-type": "ROADM"} for s in "abc"},
                  **{f"xpdr-{s}{i}": {"site": f"site-{s}", "node-type": "XPONDER"} for s in "abc" for i in (1, 2)},
                  **{f"amp-{n}": {"site": "span", "node-type": "AMPLIFIER"} for n in range(1, 5)}},
        "lightpaths": {
            "LP-1": {"a": "xpdr-a1", "z": "xpdr-b1", "rate_gbps": 100, "osnr_db": 21.4, "state": "in-service",
                     "client": {"xpdr-a1": "packet pe-a:eth3", "xpdr-b1": "packet pe-b:eth3"}},
            "LP-2": {"a": "xpdr-a2", "z": "xpdr-c1", "rate_gbps": 400, "osnr_db": 24.9, "state": "in-service",
                     "client": {}},
        },
    }

    def tools(self):
        st = self.state

        def list_nodes():
            rows = []
            for name, n in st["nodes"].items():
                rows.append({"node-id": name, **n,
                             "ports": [{"port-id": f"{name}-p{i}", "port-type": "line" if i < 3 else "client",
                                        "state": "in-service"} for i in range(1, 25)]})
            return json.dumps({"nodes": rows})

        def list_lightpaths():
            return json.dumps(st["lightpaths"])

        def lightpath_osnr(name: str):
            lp = st["lightpaths"].get(name)
            return f"Error: no lightpath {name}" if lp is None else json.dumps({"name": name, "osnr_db": lp["osnr_db"]})

        def create_lightpath(name: str, a: str, z: str, rate_gbps: int):
            for end in (a, z):
                n = st["nodes"].get(end)
                if not n or n["node-type"] != "XPONDER":
                    return f"Error: {end} is not a transponder"
                if any(end in (lp["a"], lp["z"]) for lp in st["lightpaths"].values()):
                    return f"Error: {end} already carries a lightpath"
            if name in st["lightpaths"]:
                return f"Error: lightpath {name} exists"
            st["lightpaths"][name] = {"a": a, "z": z, "rate_gbps": int(rate_gbps), "osnr_db": 22.0,
                                      "state": "in-service", "client": {}}
            self._did("create_lightpath", name=name, a=a, z=z, rate_gbps=int(rate_gbps))
            return f"lightpath {name} created ({a} to {z}, {rate_gbps}G), in service"

        def delete_lightpath(name: str):
            if name not in st["lightpaths"]:
                return f"Error: no lightpath {name}"
            del st["lightpaths"][name]
            self._did("delete_lightpath", name=name)
            return f"lightpath {name} deleted"

        def list_relations():
            rel = []
            for name, lp in st["lightpaths"].items():
                for end in (lp["a"], lp["z"]):
                    rel.append({"from": f"optical:{name}", "relation": "ends at", "to": f"optical:{end}"})
                for xp, client in lp.get("client", {}).items():
                    dom, port = client.split(" ", 1)
                    rel.append({"from": f"optical:{xp}", "relation": "hands off to", "to": f"{dom}:{port}"})
            return json.dumps(rel)

        return [
            Tool("list_relations", "What depends on what in this domain, and where it meets others: rows "
                 "{from, relation, to}.", list_relations),
            Tool("list_nodes", "Every node (ROADMs, transponders, amplifiers) with its site, node-type and ports.",
                 list_nodes),
            Tool("list_lightpaths", "The lightpaths: ends, rate, OSNR, state, and the client each end hands off to.",
                 list_lightpaths),
            Tool("lightpath_osnr", "One lightpath's measured OSNR in dB.", lightpath_osnr, _obj(name=dict(S))),
            Tool("create_lightpath", "Create a lightpath between two transponders at a rate in Gb/s.", create_lightpath,
                 _obj(name=dict(S), a=dict(S), z=dict(S), rate_gbps=dict(I)), write=True),
            Tool("delete_lightpath", "Delete a lightpath by name.", delete_lightpath, _obj(name=dict(S)), write=True),
        ]

    def degrade(self, name: str, osnr_db: float):
        self.state["lightpaths"][name]["osnr_db"] = osnr_db


class Fabric(Domain):
    key, label, domain = "fabric", "FabricCtl", "data-centre"
    START = {
        "switches": {"spine-1": {"role": "spine"}, "spine-2": {"role": "spine"},
                     "leaf-1": {"role": "leaf"}, "leaf-2": {"role": "leaf"},
                     "border-leaf-1": {"role": "border-leaf", "uplink": "packet pe-b:eth4 (port48)"}},
        "tenants": {"blue": {"vni": 5001, "leaves": ["leaf-1"], "external": None},
                    "green": {"vni": 5002, "leaves": ["leaf-2"], "external": "VS-200 via border-leaf-1"}},
    }

    def tools(self):
        st = self.state

        def list_switches():
            return json.dumps(st["switches"])

        def list_tenants():
            return json.dumps(st["tenants"])

        def attach_tenant(tenant: str, leaf: str):
            if tenant not in st["tenants"]:
                return f"Error: no tenant {tenant}"
            if leaf not in st["switches"]:
                return f"Error: no switch {leaf}"
            if leaf in st["tenants"][tenant]["leaves"]:
                return f"Error: {tenant} is already on {leaf}"
            st["tenants"][tenant]["leaves"].append(leaf)
            self._did("attach_tenant", tenant=tenant, leaf=leaf)
            return f"tenant {tenant} attached to {leaf}"

        def connect_tenant_external(tenant: str, service: str):
            if tenant not in st["tenants"]:
                return f"Error: no tenant {tenant}"
            if "border-leaf-1" not in st["tenants"][tenant]["leaves"]:
                return f"Error: {tenant} is not on border-leaf-1, the only way out"
            st["tenants"][tenant]["external"] = f"{service} via border-leaf-1"
            self._did("connect_tenant_external", tenant=tenant, service=service)
            return f"tenant {tenant} reaches {service} through border-leaf-1"

        def list_relations():
            rel = [{"from": "fabric:border-leaf-1", "relation": "uplinks to", "to": "packet:pe-b:eth4"}]
            for name, t in st["tenants"].items():
                for leaf in t["leaves"]:
                    rel.append({"from": f"fabric:{name}", "relation": "is on", "to": f"fabric:{leaf}"})
                if t.get("external"):
                    svc = t["external"].split()[0]
                    rel.append({"from": f"fabric:{name}", "relation": "leaves the data centre through",
                                "to": "fabric:border-leaf-1"})
                    rel.append({"from": f"fabric:{name}", "relation": "uses", "to": f"packet:{svc}"})
            return json.dumps(rel)

        return [
            Tool("list_relations", "What depends on what in this domain, and where it meets others: rows "
                 "{from, relation, to}.", list_relations),
            Tool("list_switches", "The fabric's switches and their roles (and the border leaf's uplink).",
                 list_switches),
            Tool("list_tenants", "The tenants: VNI, the leaves they are on, and their external connection.",
                 list_tenants),
            Tool("attach_tenant", "Attach a tenant's network to a leaf switch.", attach_tenant,
                 _obj(tenant=dict(S), leaf=dict(S)), write=True),
            Tool("connect_tenant_external", "Connect a tenant to a service outside the fabric, through the border "
                 "leaf (the tenant must be on the border leaf).", connect_tenant_external,
                 _obj(tenant=dict(S), service=dict(S)), write=True),
        ]


def all_domains() -> Dict[str, Domain]:
    return {d.key: d for d in (Packet(), Optical(), Fabric())}
