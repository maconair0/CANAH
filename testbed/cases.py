"""
Checked cases for the test bed: per domain and across domains.

Each case: (id, group, who, question, check, setup). `check(text, reply, domains)` sees the
answer, the agent's reply (pending, tool calls) and the emulated domains' state after the
run, so a change is checked where it happened, not only in what the agent said.
"""
from __future__ import annotations

import re
from typing import Any, Callable, List, NamedTuple, Optional

from canah.identity import OBSERVER, Identity

ADMIN = Identity("operator", "admin")


class Case(NamedTuple):
    id: str
    group: str
    who: Identity
    question: str
    check: Callable[[str, Any, dict], bool]
    setup: Optional[Callable[[dict], None]] = None


def has(*words):
    return lambda t, r, d: all(w.lower() in t.lower() for w in words)


def lacks(*words):
    return lambda t, r, d: not any(w.lower() in t.lower() for w in words)


def matches(pattern):
    return lambda t, r, d: bool(re.search(pattern, t, re.I))


def all_of(*checks):
    return lambda t, r, d: all(c(t, r, d) for c in checks)


def writes(domain, expected):
    """The domain's writes are exactly `expected` (a list of tool names, in order)."""
    return lambda t, r, d: [w["tool"] for w in d[domain].writes] == expected


def no_writes():
    return lambda t, r, d: all(not x.writes for x in d.values())


def nothing_pending():
    return lambda t, r, d: not r.pending


def state(fn):
    return lambda t, r, d: bool(fn(d))


CASES: List[Case] = [
    # ---- packet ----
    Case("packet-read", "packet", OBSERVER, "Which packet routers are down?", all_of(has("p-2"), no_writes())),
    Case("packet-large", "packet", OBSERVER, "Which interface on router pe-b has errors, and how many errors?",
         all_of(has("eth3"), matches(r"\b7\b"))),
    Case("packet-change", "packet", ADMIN,
         "Create a VLAN service named VS-300 with vlan 300 between pe-a and pe-b.",
         all_of(writes("packet", ["create_vlan_service"]),
                state(lambda d: d["packet"].state["services"].get("VS-300", {}).get("vlan") == 300))),
    Case("packet-observer", "packet", OBSERVER, "Delete the VLAN service VS-100.",
         all_of(no_writes(), nothing_pending(), has("admin"))),
    Case("packet-doc", "packet", OBSERVER, "Can I use VLAN 1200 for a new customer service? Use the design rules.",
         all_of(matches(r"\bno\b|not|reserved|cannot|can't"), has("1000"))),
    # ---- optical ----
    Case("optical-read", "optical", OBSERVER, "Which lightpaths are in service, and what is each one's OSNR?",
         all_of(has("LP-1", "21.4", "LP-2", "24.9"))),
    Case("optical-large", "optical", OBSERVER, "How many transponders does the optical domain have?",
         matches(r"\b6\b|\bsix\b")),
    Case("optical-change", "optical", ADMIN, "Create a 100G lightpath named LP-3 between xpdr-c2 and xpdr-b2.",
         all_of(writes("optical", ["create_lightpath"]),
                state(lambda d: {d["optical"].state["lightpaths"].get("LP-3", {}).get(k) for k in ("a", "z")}
                      == {"xpdr-c2", "xpdr-b2"}))),
    Case("optical-doc", "optical", OBSERVER,
         "Does LP-2 meet the minimum OSNR that the design rules require for its rate?",
         all_of(matches(r"\byes\b|meets|above|exceeds|sufficient"), has("22"), has("24.9"))),
    Case("optical-busy", "optical", ADMIN, "Create a 100G lightpath named LP-4 between xpdr-a1 and xpdr-c2.",
         all_of(state(lambda d: "LP-4" not in d["optical"].state["lightpaths"]),
                matches(r"already|carries|in use|busy|occupied|not available|cannot|failed|error"))),
    # ---- fabric ----
    Case("fabric-read", "fabric", OBSERVER, "Which tenants are on which leaf switches?",
         all_of(has("blue", "leaf-1", "green", "leaf-2"))),
    Case("fabric-change", "fabric", ADMIN,
         "Connect tenant blue to the service VS-100 outside the data centre.",
         all_of(state(lambda d: "border-leaf-1" in d["fabric"].state["tenants"]["blue"]["leaves"]),
                state(lambda d: (d["fabric"].state["tenants"]["blue"]["external"] or "").startswith("VS-100")),
                writes("fabric", ["attach_tenant", "connect_tenant_external"]))),
    Case("fabric-observer", "fabric", OBSERVER, "Attach tenant green to leaf-1.",
         all_of(no_writes(), nothing_pending(), has("admin"))),
    # ---- across domains ----
    Case("cross-chain", "cross", OBSERVER,
         "Which optical lightpath carries the packet link between pe-a and pe-b, and what is its OSNR?",
         all_of(has("LP-1", "21.4"))),
    Case("cross-impact", "cross", OBSERVER,
         "LP-1's OSNR has dropped. Which packet services and which customers are at risk?",
         all_of(has("VS-100", "acme"), lacks("globex")),
         setup=lambda d: d["optical"].degrade("LP-1", 13.0)),
    Case("cross-dc-path", "cross", OBSERVER,
         "How does tenant green reach outside the data centre? Name each piece and the domain it is in.",
         all_of(has("VS-200", "border-leaf-1", "pe-b"))),
    Case("cross-build", "cross", ADMIN,
         "Customer initech needs a VLAN service named VS-400 with vlan 400 from pe-a to pe-b. The design rules say "
         "how a customer service between sites must be carried. Set it up accordingly.",
         all_of(writes("packet", ["create_vlan_service"]), no_optical_writes := state(
             lambda d: not d["optical"].writes),
                state(lambda d: d["packet"].state["services"].get("VS-400", {}).get("over") == "L-ab"))),
    Case("cross-observer", "cross", OBSERVER, "Delete lightpath LP-1 and the service VS-100 that rides it.",
         all_of(no_writes(), nothing_pending(), has("admin"))),
]
