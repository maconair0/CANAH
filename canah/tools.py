"""
How a system hands its tools to the agent.

A `ToolProvider` gives:
  tools()      LangChain tools (BaseTool, or plain functions with type hints
               and a docstring)
  writes()     the names of those that change the system: each pauses for a
               human (approval.py)
  subagents()  optional: subagent specs that use some of those tools
  instructions()  optional: text added to the agent's system prompt
  middleware() optional: LangChain middleware for the main agent (a check on
               its answers, say)
  shared_tools() optional: names of its tools every subagent should also have
               (a document search a controller's subagent can use for a rule)
  main_tools() optional: the names the main agent holds itself; the rest are
               only for the provider's subagents (a large system keeps its
               hundreds of tools out of the main agent's choice this way)

The core knows nothing else about the system. `FunctionProvider` wraps plain
functions, for small systems and tests; an adapter for a large system
implements the protocol itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Protocol, Sequence, runtime_checkable

from langchain_core.tools import BaseTool, StructuredTool


@runtime_checkable
class ToolProvider(Protocol):
    def tools(self) -> Sequence[BaseTool]: ...

    def writes(self) -> Iterable[str]: ...


def as_tool(fn: Callable[..., Any], name: str = "", description: str = "") -> BaseTool:
    """A plain function as a tool: its type hints become the argument schema,
    its docstring the description the model sees."""
    if isinstance(fn, BaseTool):
        return fn
    return StructuredTool.from_function(fn, name=name or fn.__name__,
                                        description=description or (fn.__doc__ or fn.__name__).strip())


@dataclass
class FunctionProvider:
    """Tools from plain functions. `write=True` marks one that changes things."""
    _tools: List[BaseTool] = field(default_factory=list)
    _writes: List[str] = field(default_factory=list)
    _subagents: List[Dict[str, Any]] = field(default_factory=list)
    _instructions: str = ""

    def add(self, fn: Callable[..., Any], *, write: bool = False, name: str = "",
            description: str = "") -> BaseTool:
        tool = as_tool(fn, name, description)
        self._tools.append(tool)
        if write:
            self._writes.append(tool.name)
        return tool

    def tools(self) -> Sequence[BaseTool]:
        return list(self._tools)

    def writes(self) -> Iterable[str]:
        return list(self._writes)

    def subagents(self) -> List[Dict[str, Any]]:
        return list(self._subagents)

    def instructions(self) -> str:
        return self._instructions


def merge(providers: Sequence[Any]) -> Dict[str, Any]:
    """Every provider's tools, writes, subagents and instructions, with
    duplicate tool names refused (two systems both offering `list_devices`
    would leave the model guessing which it called)."""
    tools: List[BaseTool] = []
    seen: Dict[str, str] = {}
    writes: List[str] = []
    subagents: List[Dict[str, Any]] = []
    notes: List[str] = []
    hidden: List[str] = []
    middleware: List[Any] = []
    shared: List[str] = []
    for p in providers:
        label = type(p).__name__
        for t in p.tools():
            if t.name in seen:
                raise ValueError(f"tool {t.name!r} is offered by both {seen[t.name]} and {label}")
            seen[t.name] = label
            tools.append(t)
        writes += list(p.writes())
        if hasattr(p, "main_tools") and p.main_tools() is not None:
            keep = set(p.main_tools())
            hidden += [t.name for t in p.tools() if t.name not in keep]
        if hasattr(p, "subagents"):
            subagents += list(p.subagents() or [])
        if hasattr(p, "middleware"):
            middleware += list(p.middleware() or [])
        if hasattr(p, "shared_tools"):
            shared += list(p.shared_tools() or [])
        if hasattr(p, "instructions") and p.instructions():
            notes.append(p.instructions().strip())
    return {"tools": tools, "writes": writes, "subagents": subagents, "instructions": "\n\n".join(notes),
            "subagent_only": hidden, "middleware": middleware, "shared": shared}


def load_providers(specs: Sequence[str]) -> List[Any]:
    """Providers named as "package.module:factory" (the factory is called with
    no arguments and returns a ToolProvider)."""
    import importlib
    out = []
    for spec in specs:
        mod, _, attr = str(spec).partition(":")
        if not mod or not attr:
            raise ValueError(f"provider {spec!r}: write it as package.module:factory")
        obj = getattr(importlib.import_module(mod), attr)
        got = obj() if callable(obj) and not isinstance(obj, ToolProvider) else obj
        if not isinstance(got, ToolProvider):
            raise TypeError(f"provider {spec!r} gave {type(got).__name__}, not a ToolProvider (tools() and writes())")
        out.append(got)
    return out
