"""
Subagents: focused agents the main one delegates to through its `task` tool.

Each spec is a deepagents `SubAgent`: name, description (what the main agent
reads to decide when to delegate), system_prompt, and the tools it may use.
Tools are named, and resolved against everything the agent has, so a YAML
file can declare subagents without code:

  subagents:
    - name: observer
      description: Reads the live state of devices and services. Use for "what is", "show", "why is X down".
      system_prompt: You read; you never change anything. Report values with their source.
      tools: [list_devices, "controllers__get_*"]     # names or glob patterns
      model: strong_model                              # optional: a role in models.py, or provider:model
      skills: [/skills/0-skills/]                      # optional

A subagent inherits the main agent's approvals: a write it calls pauses the
same way.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any, Dict, List, Sequence

import yaml
from langchain_core.tools import BaseTool

from . import models

ROLES = ("model", "strong_model", "fast_model")


def from_yaml(path: str) -> List[Dict[str, Any]]:
    if not path:
        return []
    data = yaml.safe_load(Path(path).expanduser().read_text()) or {}
    specs = data.get("subagents", data) if isinstance(data, dict) else data
    if not isinstance(specs, list):
        raise ValueError(f"{path}: expected a list of subagents")
    return specs


def resolve(specs: Sequence[Dict[str, Any]], tools: Sequence[BaseTool]) -> List[Dict[str, Any]]:
    """Specs with tool names (or patterns) turned into the tools themselves,
    and a model role turned into its model. An unknown name is an error, not a
    subagent quietly missing a tool."""
    by_name = {t.name: t for t in tools}
    out: List[Dict[str, Any]] = []
    seen = set()
    for spec in specs:
        spec = dict(spec)
        for key in ("name", "description"):
            if not str(spec.get(key) or "").strip():
                raise ValueError(f"a subagent needs a {key}: {spec}")
        if spec["name"] in seen:
            raise ValueError(f"two subagents are called {spec['name']!r}")
        seen.add(spec["name"])
        wanted = spec.get("tools")
        if wanted is not None:
            chosen: List[BaseTool] = []
            for w in wanted:
                if isinstance(w, BaseTool):
                    chosen.append(w)
                    continue
                hits = [t for n, t in by_name.items() if fnmatch.fnmatch(n, str(w))]
                if not hits and not any(c in str(w) for c in "*?["):
                    raise ValueError(f"subagent {spec['name']!r}: no tool called {w!r}")
                chosen += [h for h in hits if h not in chosen]
            spec["tools"] = chosen
        m = spec.get("model")
        if isinstance(m, str):
            spec["model"] = models.get(m) if m in ROLES else models.make(m)
        spec.setdefault("system_prompt", f"You are the {spec['name']} subagent. {spec['description']}")
        out.append(spec)
    return out
