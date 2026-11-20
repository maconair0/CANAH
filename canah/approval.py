"""
Nothing changes the system without a human.

Every tool that writes (a provider's `writes()`, the settings'
`approve_tools`, MCP tools allowed to write) is listed in `interrupt_on`.
When the model calls one, the run stops before the call with an interrupt
carrying the tool's name and arguments. It continues only with a decision:

  approve   the call runs as proposed
  edit      it runs with the arguments the operator changed
  reject    it does not run; the model is told why

`pending()` reads what a paused thread is waiting for, `decide()` resumes it.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from langgraph.types import Command

DECISIONS = ["approve", "edit", "reject"]


def interrupt_on(write_tools: Iterable[str], extra: Optional[Dict[str, Any]] = None,
                 all_tools: Iterable[str] = ()) -> Dict[str, Any]:
    """The interrupt map: every write pauses; `extra` adds tools (names or glob patterns over
    `all_tools`) with True or their own config ({"allowed_decisions": [...], "description": ...}),
    and may reshape a write's config, never switch it off."""
    import fnmatch
    writes = set(write_tools)
    out: Dict[str, Any] = {name: {"allowed_decisions": list(DECISIONS),
                                  "description": f"{name} changes the system and waits for an operator's approval."}
                           for name in sorted(writes)}
    names = sorted(set(all_tools) | writes)
    for pattern, cfg in (extra or {}).items():
        hits = [n for n in names if fnmatch.fnmatch(n, pattern)] or [pattern]
        for name in hits:
            if cfg is False:
                if name in writes:
                    raise ValueError(f"interrupt_on: {name} changes the system, so it always pauses for approval")
                out.pop(name, None)
                continue
            base = out.get(name) or {"allowed_decisions": list(DECISIONS),
                                     "description": f"{name} waits for an operator's approval."}
            if isinstance(cfg, dict):
                bad = set(cfg.get("allowed_decisions") or []) - set(DECISIONS)
                if bad:
                    raise ValueError(f"interrupt_on {name}: unknown decision(s) {sorted(bad)}; use {DECISIONS}")
                if cfg.get("allowed_decisions") is not None and "reject" not in cfg["allowed_decisions"]:
                    raise ValueError(f"interrupt_on {name}: a change can always be rejected")
                base = {**base, **cfg}
            out[name] = base
    return out


def pending(agent: Any, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The tool calls a thread is paused on, each {name, args, description}."""
    state = agent.get_state(config)
    out: List[Dict[str, Any]] = []
    for intr in getattr(state, "interrupts", ()) or ():
        value = getattr(intr, "value", intr)
        for req in (value or {}).get("action_requests", []) if isinstance(value, dict) else []:
            out.append({"name": req.get("name"), "args": req.get("args") or req.get("arguments") or {},
                        "description": req.get("description", "")})
    return out


def decision(kind: str, *, args: Optional[Dict[str, Any]] = None, name: str = "",
             message: str = "") -> Dict[str, Any]:
    if kind == "approve":
        return {"type": "approve"}
    if kind == "edit":
        return {"type": "edit", "edited_action": {"name": name, "args": dict(args or {})}}
    if kind == "reject":
        return {"type": "reject", "message": message or "The operator rejected this change."}
    raise ValueError(f"a decision is one of {DECISIONS}, not {kind!r}")


def resume(decisions: List[Dict[str, Any]]) -> Command:
    """What to pass to `agent.invoke` to continue a paused thread: one decision
    per pending call, in order."""
    return Command(resume={"decisions": decisions})
