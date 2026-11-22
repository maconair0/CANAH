"""
Subagents the operators make from the chat: for one task and gone, or kept until they are removed.

  spawn_subagent(name, instructions, task, tools, keep, keep_hours)
      builds a focused agent with the instructions and the tools named (read tools only), runs the task, and gives
      its report back. Not kept (the default): it is gone when its work is done; nothing is saved. Kept (admin):
      saved under CANAH_HOME/subagents/<name>.json and available to `task` from the next question, until it is
      removed or, with keep_hours, until that time is up.
  remove_subagent(name)          a kept one, removed (admin)
  list_subagents()               the kept ones, and the built-in ones

A subagent made here only reads and reports. It cannot change a network: a change stays with the config agents,
the twin's check and the operator's approval. Its tools are the agent's read tools, never a write, a delegation or
these tools themselves.
"""
from __future__ import annotations

import fnmatch
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from langchain_core.tools import BaseTool, StructuredTool

NAME = re.compile(r"[a-z][a-z0-9-]{1,40}")
RELOAD_SOON: Callable[[], Any] = lambda: None      # the service sets it: rebuild the agent when it is idle
KNOWN: set = set()                                  # the kept subagents' names, for about_subagents
import contextvars  # noqa: E402
_QUESTION: "contextvars.ContextVar[str]" = contextvars.ContextVar("canah_operator_question", default="")
_SUB = re.compile(r"\bsub-?agents?\b", re.I)
_NO_KEEP = re.compile(r"\b(?:don'?t|do not|no need to|never)\s+keep\b", re.I)
_KEEP = re.compile(r"\bkeep (?:it|this|that|them|the \S+|[a-z0-9-]+)\b|\bkeep\b[^.]{0,40}\b(?:for|until|around)\b", re.I)
_HOURS = re.compile(r"\bkeep\b[^.]{0,60}?(\d+(?:\.\d+)?)\s*(h|hrs?|hours?|d|days?|m|mins?|minutes?)\b", re.I)


_REMOVE = re.compile(r"\b(remove|delete|drop|get rid of|retire|discard)\b|\buse is done\b|\bdone with\b", re.I)
_CHECK = "[subagent check]"


def about_subagents(q: str) -> bool:
    """The request is about making, using or removing a subagent, not about changing a network."""
    low = (q or "").lower()
    return bool(_SUB.search(low)) or any(re.search(rf"\b{re.escape(n)}\b", low) for n in KNOWN)


def keep_asked(q: str) -> tuple:
    """(keep, hours) as the operator put it: "keep it", "keep it for 2 hours", "don't keep it"."""
    if not q or _NO_KEEP.search(q):
        return False, 0.0
    if not _KEEP.search(q):
        return False, 0.0
    m = _HOURS.search(q)
    if not m:
        return True, 0.0
    n, unit = float(m.group(1)), m.group(2).lower()
    return True, n * 24 if unit.startswith("d") else n / 60 if unit.startswith("m") else n
_NEVER = {"task", "spawn_subagent", "remove_subagent", "list_subagents", "write_todos", "execute", "write_file",
          "edit_file", "delete", "twin_override", "compact_conversation"}
DEFAULT_TOOLS = ("net_read_*", "net_health", "net_ping", "net_traceroute", "follow_relations", "network_*",
                 "search_knowledge", "twin_list", "twin_schema", "twin_validate", "twin_predict", "twin_extra",
                 "incidents", "network_changes", "health_over_time", "ip_over_optical")


class Store:
    """The kept subagents, one JSON file each; one past its time is removed when they are next listed."""

    def __init__(self, home: Any):
        self.dir = Path(home) / "subagents"
        self._lock = threading.Lock()

    def _path(self, name: str) -> Path:
        if not NAME.fullmatch(name or ""):
            raise ValueError("a subagent's name is lower-case letters, digits and - (2 to 41), starting with a letter")
        return self.dir / f"{name}.json"

    def list(self) -> List[Dict[str, Any]]:
        out, now = [], time.time()
        with self._lock:
            for f in sorted(self.dir.glob("*.json")) if self.dir.is_dir() else []:
                try:
                    spec = json.loads(f.read_text())
                except (OSError, ValueError):
                    continue
                if spec.get("expires_at") and now > float(spec["expires_at"]):
                    f.unlink(missing_ok=True)          # its time is up
                    continue
                out.append(spec)
        KNOWN.clear()
        KNOWN.update(s["name"] for s in out)
        return out

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        return next((s for s in self.list() if s["name"] == name), None)

    def save(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        p = self._path(spec["name"])
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(spec, indent=1))
        return spec

    def delete(self, name: str) -> bool:
        p = self._path(name)
        with self._lock:
            if not p.exists():
                return False
            p.unlink()
            return True

    def specs(self) -> List[Dict[str, Any]]:
        """For build_agent: the kept ones as subagent specs (tools by name; subagents.resolve turns them into tools)."""
        return [{"name": s["name"], "description": s["description"], "system_prompt": s["system_prompt"],
                 "tools": s["tools"]} for s in self.list()]


def _ctx() -> Dict[str, Any]:
    try:
        from langgraph.config import get_config
        return (get_config() or {}).get("configurable") or {}
    except Exception:  # noqa: BLE001
        return {}


def choose(wanted: Sequence[str], tools: Sequence[BaseTool], writes: Sequence[str]) -> List[BaseTool]:
    """The tools a made subagent may have: the names or patterns asked for (or the usual reads), never a write."""
    banned = set(writes) | _NEVER
    pool = {t.name: t for t in tools if t.name not in banned}
    out: List[BaseTool] = []
    for w in (list(wanted) or list(DEFAULT_TOOLS)):
        for n, t in pool.items():
            if fnmatch.fnmatch(n, str(w).strip()) and t not in out:
                out.append(t)
    if not out and not wanted:             # none of the usual reads here: every read this agent has
        out = list(pool.values())
    return out


def prompt_for(name: str, instructions: str) -> str:
    return (f"You are {name}, a subagent an operator made for a focused task. {instructions.strip()}\n\n"
            "You read and report; you cannot change anything (you have no write tools). Use your tools to find out; "
            "report only what they returned, with the object names and values as read. If something needs changing, "
            "say what and why in your report: the main agent and the operator decide.")


class Maker:
    """The three tools, for the main agent. `model`: the main agent's; `middleware()`: fresh checks for one run;
    `builtin`: the names a made subagent may not take; `reload_soon()`: rebuild the agent when it is idle."""

    def __init__(self, store: Store, tools: Sequence[BaseTool], writes: Sequence[str], model: Any,
                 middleware: Callable[[], List[Any]], builtin: Sequence[str], reload_soon: Callable[[], Any]):
        self.store, self.tools, self.writes, self.model = store, list(tools), list(writes), model
        self.middleware, self.builtin, self.reload_soon = middleware, set(builtin), reload_soon

    def question_middleware(self) -> Any:
        """For the main agent: the operator's question reaches these tools (keep or not is theirs to say)."""
        from langchain.agents.middleware import AgentMiddleware
        names = {"spawn_subagent", "remove_subagent", "list_subagents"}
        maker = self

        class _Question(AgentMiddleware):
            def _set(self, request):
                if str(request.tool_call.get("name")) not in names:
                    return None
                from .grounding import current_question
                st = getattr(request, "state", None) or {}
                msgs = st.get("messages", []) if isinstance(st, dict) else getattr(st, "messages", [])
                return _QUESTION.set(current_question(list(msgs or [])))

            def wrap_tool_call(self, request, handler):
                tok = self._set(request)
                try:
                    return handler(request)
                finally:
                    if tok is not None:
                        _QUESTION.reset(tok)

            async def awrap_tool_call(self, request, handler):
                tok = self._set(request)
                try:
                    return await handler(request)
                finally:
                    if tok is not None:
                        _QUESTION.reset(tok)

            # "The span-checker's use is done: remove it." The operator named a kept subagent and asked to remove
            # it: done first, as the call the agent would make, before the model writes a word. (Left to the model,
            # it said "removed" with nothing removed; sent back, it said so again; and once it handed the removal to
            # another agent, which said it needed root.) The answer then starts from the removal made.
            def before_model(self, state, runtime):
                import uuid as _uuid
                from langchain_core.messages import AIMessage, ToolMessage
                from .grounding import current_question, turn_start
                msgs = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
                start = turn_start(msgs)
                if not msgs or any(isinstance(m, AIMessage) for m in msgs[start:]):
                    return None                       # only before the first step of the turn
                q = current_question(msgs)
                maker.store.list()                     # what is kept now (and KNOWN with it)
                if not (_REMOVE.search(q) and about_subagents(q)):
                    return None
                named = [n for n in sorted(KNOWN) if re.search(rf"\b{re.escape(n)}\b", q.lower())]
                if len(named) != 1 or _ctx().get("level") not in ("admin", "root"):
                    return None                       # not named, or not theirs to remove: the model answers
                tool = next(t for t in maker.make() if t.name == "remove_subagent")
                tok = _QUESTION.set(q)
                try:
                    out = tool.invoke({"name": named[0]})
                finally:
                    _QUESTION.reset(tok)
                cid = f"call_{_uuid.uuid4().hex[:12]}"
                return {"messages": [AIMessage(content="", tool_calls=[{"name": "remove_subagent", "args": {"name": named[0]},
                                                                        "id": cid, "type": "tool_call"}]),
                                     ToolMessage(content=out, tool_call_id=cid, name="remove_subagent")]}

            from langchain.agents.middleware import hook_config

            @hook_config(can_jump_to=["model"])
            def after_model(self, state, runtime):
                from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
                from .grounding import QUIET, current_question, turn_start
                msgs = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
                if not msgs or not isinstance(msgs[-1], AIMessage) or msgs[-1].tool_calls:
                    return None
                q = current_question(msgs)
                if not (_REMOVE.search(q) and about_subagents(q)):
                    return None
                turn = msgs[turn_start(msgs):]
                if any(isinstance(m, ToolMessage) and m.name == "remove_subagent" for m in turn):
                    return None
                if any(isinstance(m, HumanMessage) and _CHECK in str(m.content) for m in turn):
                    return None
                return {"jump_to": "model", "messages": [HumanMessage(content=(
                    f"{_CHECK} The operator asked to remove a subagent and nothing was removed: no remove_subagent "
                    f"call was made this turn. Call remove_subagent with its name now (list_subagents gives the kept "
                    f"ones), then say what it returned. " + QUIET))]}
        _Question.__name__ = "SubagentQuestion"
        return _Question()

    def make(self) -> List[BaseTool]:
        def spawn_subagent(name: str, instructions: str, task: str, tools: Any = None,
                           description: str = "", keep: Any = False, keep_hours: Any = 0) -> str:
            """Make a subagent for a focused piece of work and run it now, when the operator asks for one ("use a
            subagent to ...", "create an agent that ..."). name: short, lower-case-with-dashes. instructions: what it
            is and how it works. task: what to do now. tools: names or patterns of the read tools it gets (default:
            the network reads, the twin and the documents); it never gets a write. keep: false (the default) and it
            is gone when this task is done; true only when the operator asks to keep it (admin), then it is available
            to `task` from the next question, until removed or for keep_hours if given. Returns its report."""
            name = re.sub(r"[^a-z0-9-]+", "-", (name or "").strip().lower()).strip("-")
            # What a model sends is not always the schema's shape: "a, b" for a list, "true" for a flag.
            if isinstance(tools, str):
                tools = [x for x in re.split(r"[,\s]+", tools) if x]
            tools = [str(x) for x in tools] if isinstance(tools, (list, tuple)) else []
            keep = keep if isinstance(keep, bool) else str(keep).strip().lower() in ("true", "yes", "1")
            try:
                keep_hours = float(keep_hours or 0)
            except (TypeError, ValueError):
                keep_hours = 0.0
            # The operator's own words decide, whatever the model passed: "keep it for 1 hour", "don't keep it".
            asked = _QUESTION.get()
            if asked:
                said_keep, said_hours = keep_asked(asked)
                if _NO_KEEP.search(asked):
                    keep = False
                elif said_keep:
                    keep, keep_hours = True, (said_hours or keep_hours)
            if not NAME.fullmatch(name):
                return "Error: name it with lower-case letters, digits and - (for example fibre-auditor)."
            if name in self.builtin:
                return f"Error: {name} is a built-in subagent; choose another name."
            if not (instructions or "").strip() or not (task or "").strip():
                return "Error: give it instructions (what it is) and a task (what to do now)."
            cfg = _ctx()
            if keep and cfg.get("level") not in ("admin", "root"):
                return "Error: keeping a subagent needs admin; run it once without keep, or ask an admin."
            chosen = choose(tools, self.tools, self.writes)
            note = ""
            if not chosen and tools:            # names it made up ("read_fibres"): the usual reads instead
                chosen = choose([], self.tools, self.writes)
                note = f" (none of {', '.join(tools)} is a read tool here: it got the usual reads)"
            if not chosen:
                return "Error: this agent has no read tools to give a subagent."
            from langchain.agents import create_agent
            from langchain_core.messages import AIMessage, HumanMessage
            from .safety import _DEPTH
            prompt = prompt_for(name, instructions)
            graph = create_agent(self.model, tools=chosen, system_prompt=prompt, middleware=self.middleware(),
                                 name=name)
            tok = _DEPTH.set(name)
            t0 = time.time()
            try:
                got = graph.invoke({"messages": [HumanMessage(content=task)]},
                                   {"recursion_limit": 60, "configurable": {"ls_agent_type": "subagent"}})
            except Exception as e:  # noqa: BLE001 - said to the main agent
                return f"Error: the subagent {name} failed: {type(e).__name__}: {e}"
            finally:
                _DEPTH.reset(tok)
            msgs = got.get("messages", []) if isinstance(got, dict) else []
            report = next((str(m.content) for m in reversed(msgs) if isinstance(m, AIMessage) and str(m.content).strip()), "")
            used = sorted({c["name"] for m in msgs if isinstance(m, AIMessage) for c in (m.tool_calls or [])})
            kept = ""
            if keep:
                spec = {"name": name, "description": (description or instructions).strip()[:600],
                        "system_prompt": prompt, "tools": [t.name for t in chosen], "created_by": cfg.get("user_id", ""),
                        "created_at": time.time(), "expires_at": time.time() + float(keep_hours) * 3600 if keep_hours else None,
                        "uses": 1}
                self.store.save(spec)
                self.reload_soon()
                kept = (f"\n\n[Kept: {name} is available to `task` from the next question"
                        + (f", for {keep_hours:g} h" if keep_hours else ", until it is removed") + ".]")
            else:
                kept = f"\n\n[{name} was made for this task only and is gone now: nothing was kept.]"
            return (f"Report from {name} ({round(time.time() - t0, 1)} s; tools used: {', '.join(used) or 'none'}{note}):\n\n"
                    f"{report or '(it gave no report)'}{kept}")

        def remove_subagent(name: str) -> str:
            """Remove a subagent an operator kept, when the operator asks (its use is done). Admin. A built-in one
            cannot be removed."""
            name = (name or "").strip().lower()
            if name in self.builtin:
                return f"Error: {name} is built in; it cannot be removed here."
            if _ctx().get("level") not in ("admin", "root"):
                return "Error: removing a subagent needs admin."
            try:
                gone = self.store.delete(name)
            except ValueError as e:
                return f"Error: {e}"
            if not gone:
                have = [s["name"] for s in self.store.list()]
                return f"Error: no kept subagent {name!r}; kept: {', '.join(have) or 'none'}."
            self.reload_soon()
            return f"Removed {name}: it is no longer available (from the next question)."

        def list_subagents() -> str:
            """The subagents: the ones operators kept (with who made them, their tools and until when) and the
            built-in ones."""
            kept = [{"name": s["name"], "description": s["description"], "tools": s["tools"], "created_by": s.get("created_by"),
                     "until": time.strftime("%Y-%m-%d %H:%M", time.localtime(s["expires_at"])) if s.get("expires_at") else "removed"}
                    for s in self.store.list()]
            return json.dumps({"kept": kept, "built_in": sorted(self.builtin)})

        return [StructuredTool.from_function(f) for f in (spawn_subagent, remove_subagent, list_subagents)]


def instructions() -> str:
    return ("Subagents on request: when the operator asks you to create or use a subagent for a task, call "
            "spawn_subagent with a short name, its instructions, the task and the read tools it needs; it runs now and "
            "you report what it found. It is gone when done unless the operator asks to keep it (keep=true, admin; "
            "keep_hours if they give a time). A kept one is used later with `task`; remove_subagent when the operator "
            "says its use is done. A made subagent only reads: changes stay with the config agents and approval.")
