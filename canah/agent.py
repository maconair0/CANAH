"""
The agent: `build_agent()` puts the pieces into `create_deep_agent`.

  model        models.py (or one passed in)
  tools        from the ToolProviders, plus MCP servers (mcp.py)
  subagents    a provider's, plus subagents.yaml (subagents.py)
  skills       SKILL.md folders, mounted read-only (backends.py)
  memory       AGENTS.md files loaded at start; /memories/ kept across threads
  approvals    every write tool in interrupt_on (approval.py)
  state        a checkpointer per thread, a store across them (memory.py)

and `CanahAgent` is the small surface a service, a CLI or a test uses:
`ask()`, `pending()`, `decide()`.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from deepagents import create_deep_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from . import approval, backends, identity, mcp, models, subagents as sub
from .grounding import FollowThrough, GroundingGuard, LookFirst, TimeBudget
from .identity import OBSERVER, Identity
from . import memory as mem
from .config import Settings, load
from .tools import merge

logger = logging.getLogger("canah.agent")


@dataclass
class Reply:
    text: str                                   # the agent's last message
    thread_id: str
    pending: List[Dict[str, Any]] = field(default_factory=list)   # writes waiting for a human
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)  # what it called on the way

    @property
    def waiting(self) -> bool:
        return bool(self.pending)


@dataclass
class CanahAgent:
    graph: Any
    settings: Settings
    memory: mem.Memory
    writes: List[str]
    info: Dict[str, Any]
    sandbox: Any = None
    interrupts: Dict[str, Any] = field(default_factory=dict)
    ops: Any = None

    def interrupt_config(self, tool: str) -> Optional[Dict[str, Any]]:
        """How a tool call pauses (allowed decisions, description), or None if it does not."""
        return self.interrupts.get(tool)

    def config(self, thread_id: str, who: Identity = OBSERVER, deadline: bool = False) -> Dict[str, Any]:
        c = {"configurable": {"thread_id": thread_id, "user_id": who.user, "level": who.level},
             "recursion_limit": self.settings.recursion_limit}
        if deadline:
            import time
            if self.settings.max_seconds:
                c["configurable"]["deadline"] = time.time() + float(self.settings.max_seconds)
            # This request's counters (safety.RunTracker); subagents add to the same ones.
            c["configurable"]["run"] = {"t0": time.time()}
        return c

    def _finish(self, cfg: Dict[str, Any], reply: Reply, ended: str = "") -> Reply:
        """Record the request (metrics), and each change now waiting (audit, notification)."""
        import time
        c = cfg["configurable"]
        run = c.get("run") or {}
        if self.ops is not None:
            try:
                msgs = self.graph.get_state(cfg).values.get("messages", [])
                humans = " ".join(str(m.content) for m in msgs[-60:] if getattr(m, "type", "") == "human")
                sent = {k: humans.count(f"[{k} check]") for k in ("grounding", "citation", "follow-through",
                                                                   "look-first")}
            except Exception:  # noqa: BLE001
                sent = {}
            self.ops.request(user=c.get("user_id"), level=c.get("level"), thread=c.get("thread_id"),
                             seconds=round(time.time() - run.get("t0", time.time()), 2),
                             model_calls=run.get("model_calls", 0), tokens_in=run.get("tokens_in", 0),
                             tokens_out=run.get("tokens_out", 0), tool_calls=run.get("tool_calls", 0),
                             sent_back={k: v for k, v in sent.items() if v},
                             ended=ended or run.get("ended") or ("waiting" if reply.pending else "answered"))
            for p in reply.pending:
                if self.ops.pending_since(c.get("thread_id"), p["name"]) is None:
                    self.ops.audit("proposed", c.get("user_id", ""), c.get("level", ""), c.get("thread_id", ""),
                                   p["name"], p.get("args"), message=p.get("description", ""))
                    self._notify(c, p)
        return reply

    def _notify(self, c: Dict[str, Any], p: Dict[str, Any]) -> None:
        """Tell someone a change is waiting: POST to settings.notify_url (a webhook), if set."""
        url = getattr(self.settings, "notify_url", "")
        if not url:
            return
        try:
            import requests
            requests.post(url, timeout=5, json={"event": "change waiting", "thread": c.get("thread_id"),
                                                "requested_by": c.get("user_id"), "tool": p["name"],
                                                "args": p.get("args"), "needs": p.get("needs")})
        except Exception as e:  # noqa: BLE001 - a missed notification is logged, not fatal
            logger.warning("notification to %s failed: %s", url, e)

    def _reply(self, out: Dict[str, Any], cfg: Dict[str, Any], start: int) -> Reply:
        msgs = out.get("messages", [])[start:]
        text = ""
        for m in reversed(msgs):
            if isinstance(m, AIMessage) and str(m.content or "").strip():
                text = m.content if isinstance(m.content, str) else str(m.content)
                break
        results = {m.tool_call_id: str(m.content) for m in msgs if isinstance(m, ToolMessage)}
        calls = []
        for m in msgs:
            if isinstance(m, AIMessage):
                calls += [{"name": c["name"], "args": c.get("args", {}), "result": results.get(c.get("id"))}
                          for c in (m.tool_calls or [])]
        pending = approval.pending(self.graph, cfg)
        for p in pending:
            p["needs"] = self.needs(p["name"])
        return Reply(text=text, thread_id=cfg["configurable"]["thread_id"], pending=pending, tool_calls=calls)

    def _seen(self, cfg: Dict[str, Any]) -> int:
        try:
            return len(self.graph.get_state(cfg).values.get("messages", []))
        except Exception:  # noqa: BLE001
            return 0

    def _invoke(self, payload: Any, cfg: Dict[str, Any], start: int) -> Reply:
        """Run, and when the run hits its step limit, answer saying so instead of raising."""
        from langgraph.errors import GraphRecursionError
        try:
            out = self.graph.invoke(payload, cfg)
        except GraphRecursionError:
            logger.warning("thread %s stopped at its step limit (%d)", cfg["configurable"]["thread_id"],
                           self.settings.recursion_limit)
            state = self.graph.get_state(cfg)
            reply = self._reply(state.values, cfg, start)
            reply.text = (f"I stopped after {self.settings.recursion_limit} steps without finishing this request. "
                          f"Nothing more will run for it; ask again more narrowly, or say which part to do first."
                          + (f" What I had last: {reply.text}" if reply.text else ""))
            return self._finish(cfg, reply, "step limit")
        return self._finish(cfg, self._reply(out, cfg, start))

    def needs(self, tool: str) -> str:
        """The level a tool needs: observer, admin or root."""
        return identity.required(tool, self.writes, self.settings.root_tools)

    def ask(self, text: str, thread_id: Optional[str] = None, who: Identity = OBSERVER) -> Reply:
        """One message from `who`; returns when the agent answers or pauses for an approval.
        An observer's run is never offered a tool that changes anything."""
        thread_id = thread_id or uuid.uuid4().hex[:12]
        cfg = self.config(thread_id, who, deadline=True)
        if approval.pending(self.graph, cfg):
            raise RuntimeError(f"thread {thread_id} is waiting for a decision on a change; decide() first")
        start = self._seen(cfg)
        return self._invoke({"messages": [{"role": "user", "content": text}]}, cfg, start)

    def pending(self, thread_id: str, who: Identity = OBSERVER) -> List[Dict[str, Any]]:
        return approval.pending(self.graph, self.config(thread_id, who))

    def decide(self, thread_id: str, decisions: List[Dict[str, Any]], who: Identity = OBSERVER) -> Reply:
        """Continue a paused thread: one decision per pending write (approval.decision).
        Approving or editing needs the level each tool needs; anyone may reject."""
        cfg = self.config(thread_id, who, deadline=True)
        self._check_decisions(approval.pending(self.graph, cfg), decisions, who, thread_id)
        start = self._seen(cfg)
        return self._invoke(approval.resume(decisions), cfg, start)

    def stream(self, text: Optional[str] = None, thread_id: Optional[str] = None, who: Identity = OBSERVER,
               decisions: Optional[List[Dict[str, Any]]] = None) -> Iterator[Dict[str, Any]]:
        """`ask` (with `text`) or `decide` (with `decisions`), as events while it runs:

          {"type": "token", "text": ...}                 the main agent's reply, as it is written
          {"type": "tool_call", "name", "args"}          a call made (by the main agent or a subagent)
          {"type": "tool_result", "name", "content"}     its result, first 2,000 characters
          {"type": "reply", "text", "pending", "tool_calls", "thread_id"}   last, always
        """
        thread_id = thread_id or uuid.uuid4().hex[:12]
        cfg = self.config(thread_id, who, deadline=True)
        waiting = approval.pending(self.graph, cfg)
        if decisions is None:
            if waiting:
                raise RuntimeError(f"thread {thread_id} is waiting for a decision on a change; decide() first")
            payload: Any = {"messages": [{"role": "user", "content": str(text or "")}]}
        else:
            self._check_decisions(waiting, decisions, who, thread_id)
            payload = approval.resume(decisions)
        start = self._seen(cfg)
        for mode, chunk in self._events(payload, cfg):
            yield chunk
        state = self.graph.get_state(cfg)
        reply = self._finish(cfg, self._reply(state.values, cfg, start))
        yield {"type": "reply", "text": reply.text, "pending": reply.pending, "tool_calls": reply.tool_calls,
               "thread_id": thread_id}

    def _events(self, payload: Any, cfg: Dict[str, Any]) -> Iterator[Tuple[str, Dict[str, Any]]]:
        seen_calls = set()
        for ns, mode, chunk in self.graph.stream(payload, cfg, stream_mode=["messages", "updates"], subgraphs=True):
            if mode == "messages":
                msg, meta = chunk
                if not ns and isinstance(msg, AIMessageChunk) and msg.content and not msg.tool_call_chunks:
                    text = msg.content if isinstance(msg.content, str) else ""
                    if text:
                        yield mode, {"type": "token", "text": text}
                continue
            for update in (chunk or {}).values() if isinstance(chunk, dict) else ():
                for m in (update or {}).get("messages", []) if isinstance(update, dict) else ():
                    if isinstance(m, AIMessage):
                        for c in m.tool_calls or []:
                            if c.get("id") not in seen_calls:
                                seen_calls.add(c.get("id"))
                                yield mode, {"type": "tool_call", "name": c["name"], "args": c.get("args", {}),
                                             "subagent": bool(ns)}
                    elif isinstance(m, ToolMessage):
                        yield mode, {"type": "tool_result", "name": m.name, "content": str(m.content)[:2000],
                                     "subagent": bool(ns)}

    def _check_decisions(self, waiting, decisions, who, thread_id) -> None:
        if not waiting:
            raise RuntimeError(f"thread {thread_id} is not waiting for a decision")
        if len(decisions) != len(waiting):
            raise ValueError(f"{len(waiting)} change(s) are waiting; {len(decisions)} decision(s) given")
        for p, d in zip(waiting, decisions):
            allowed = (self.interrupts.get(p["name"]) or {}).get("allowed_decisions") or approval.DECISIONS
            if d.get("type") not in allowed:
                raise ValueError(f"{p['name']} allows {allowed}, not {d.get('type')!r}")
            need = self.needs(p["name"])
            if d.get("type") in ("approve", "edit") and not who.can(need):
                raise PermissionError(f"{p['name']} needs {need} to approve; {who.label} may only reject it")
            ttl = getattr(self.settings, "approval_ttl_s", 0)
            since = self.ops.pending_since(thread_id, p["name"]) if self.ops is not None else None
            if d.get("type") in ("approve", "edit") and ttl and since is not None:
                import time
                if time.time() - since > ttl:
                    raise PermissionError(f"{p['name']} has waited {int(time.time() - since)} s, longer than the "
                                          f"{ttl} s an approval stays open: reject it and ask again")
        if self.ops is not None:
            for p, d in zip(waiting, decisions):
                self.ops.audit("decided", who.user, who.level, thread_id, p["name"],
                               d.get("edited_action", {}).get("args") if d.get("type") == "edit" else p.get("args"),
                               decision=d.get("type", ""), message=str(d.get("message") or ""))

    def close(self) -> None:
        self.memory.close()
        if self.ops is not None:
            self.ops.close()
        if self.sandbox is not None and hasattr(self.sandbox, "close"):
            self.sandbox.close()


def build_agent(settings: Optional[Settings] = None, providers: Sequence[Any] = (), *,
                model: Optional[BaseChatModel] = None, memory: Optional[mem.Memory] = None,
                extra_tools: Sequence[Any] = (), extra_writes: Sequence[str] = (),
                extra_subagents: Sequence[Dict[str, Any]] = (), sandbox: Any = None,
                middleware: Sequence[Any] = (), interrupt_on: Optional[Dict[str, Any]] = None) -> CanahAgent:
    """The agent, from settings and the systems' tool providers.

    middleware     LangChain AgentMiddleware for the main agent and every subagent, after the
                   built-in ones (roles, grounding) and the providers' own
    interrupt_on   tool name or glob -> True or an interrupt config: pause these too. Writes
                   always pause; their config may be reshaped here, never switched off.
    Both add to what the settings name (settings.middleware, settings.interrupt_on)."""
    settings = settings or load()
    sandbox_note = ""
    if sandbox is None and settings.sandbox == "docker":
        from .sandbox import open_sandbox
        try:
            sandbox = open_sandbox()
        except Exception as e:  # noqa: BLE001 - the agent still starts, and says it has no sandbox
            sandbox_note = str(e)
            logger.warning("starting without a sandbox: %s", e)
    from .tools import load_providers
    got = merge(list(providers) + load_providers(settings.providers))
    tools = list(got["tools"]) + list(extra_tools)
    writes = list(got["writes"]) + list(extra_writes) + list(settings.approve_tools)
    if sandbox is not None and settings.approve_execute:
        writes.append("execute")

    mcp_info: Dict[str, Any] = {}
    if settings.mcp_config:
        loaded = mcp.load(settings.mcp_config, settings.mcp_write_tools)
        tools += loaded["tools"]
        writes += loaded["writes"]
        mcp_info = {"servers": loaded["servers"], "dropped_writes": loaded["dropped"]}

    names = [t.name for t in tools]
    pauses = {**(settings.interrupt_on or {}), **(interrupt_on or {})}
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ValueError(f"tool name(s) offered twice: {sorted(dupes)}")
    builtin = {"execute", "write_file", "edit_file", "delete"}
    unknown = set(writes) - set(names) - builtin
    if unknown:
        logger.info("approval asked for tool(s) the agent does not have: %s", sorted(unknown))
    writes = sorted(set(writes) & (set(names) | builtin))
    interrupts = approval.interrupt_on(writes, pauses, list(names) + sorted(builtin))
    # A tool paused on request is approved like a change: it needs admin (identity.required).
    approvals = sorted(set(writes) | {n for n in interrupts})

    specs = sub.resolve(list(got["subagents"]) + list(extra_subagents) + sub.from_yaml(settings.subagents_file),
                        tools)
    # Who may do what: on the main agent and in every subagent (identity.py).
    # deepagents adds a "general-purpose" subagent holding the main agent's
    # tools; declaring it here replaces that one, so it carries the roles too.
    roles = identity.RoleMiddleware(approvals, settings.root_tools)
    from .large_results import LargeResults
    large = LargeResults(limit=settings.large_result_chars, sandbox=sandbox,
                         folder=settings.home_path / "results" if sandbox is None else None)
    extra_mw = [*_load_middleware(settings.middleware), *list(middleware)]
    if not any(s["name"] == "general-purpose" for s in specs):
        specs.append({"name": "general-purpose",
                      "description": ("A general-purpose agent for a self-contained piece of work that needs several "
                                      "steps: research, computing in the sandbox, drafting. It has only the main "
                                      "agent's own tools: it cannot read or change a controller; delegate those to "
                                      "the controller's subagent."),
                      "system_prompt": ("You do one self-contained piece of work for the main agent and report the "
                                        "result briefly. Read before you conclude; a change is paused for approval.")})
    shared = [t for t in tools if t.name in set(got.get("shared") or [])]
    budget = TimeBudget()
    from .ops import Ops
    from .safety import GuardMemory, RecordWrites, RunTracker, ScreenOutput
    ops = Ops(settings.home_path / "ops.sqlite") if settings.audit else None
    tracker, screen, record = RunTracker(settings.max_tokens_per_request), ScreenOutput(), RecordWrites(approvals, ops)
    guard_mem = GuardMemory(ops)
    for spec in specs:
        # Every subagent is held to the same checks as the main agent: a value a subagent invents
        # would otherwise reach the main agent looking like a tool result.
        spec["middleware"] = [budget, tracker, roles, screen, record, large, GroundingGuard(), FollowThrough(),
                              *list(spec.get("middleware") or []),
                              *extra_mw]
        if spec.get("tools") is not None and shared:
            have = {t.name for t in spec["tools"]}
            spec["tools"] = [*spec["tools"], *[t for t in shared if t.name not in have]]
    backend, skill_paths, memory_paths = backends.build(settings, sandbox)
    memory = memory or mem.open_memory(settings.home_path)
    if settings.memory_ttl_days:
        gone = mem.prune(memory.store, settings.memory_ttl_days)
        if gone:
            logger.info("%d long-term memories older than %s days deleted", gone, settings.memory_ttl_days)
    prompt = settings.prompt()
    if sandbox is not None:
        prompt += ("\n\nYou have a sandbox (" + getattr(sandbox.runtime, "describe", lambda: sandbox.id)() + "). "
                   "`execute` runs a shell command there, as an unprivileged user with no network. Your files live "
                   "under /workspace/ (write and read them there; it is kept between conversations); /memories/ and "
                   "/skills/ are as before. Use it to compute, parse and check data you got from tools, never to "
                   "reach the network or the controllers: those go through their tools. Report what a command "
                   "printed as it printed it; if the output is not what you expected (an empty line, `$(...)` left "
                   "unexpanded inside single quotes, an error), fix the command and run it again rather than "
                   "saying what it should have printed.")
    else:
        prompt += ("\n\nThere is no sandbox in this session" + (f" ({sandbox_note})" if sandbox_note else "")
                   + ": you cannot run commands, only call your tools.")
    if got["instructions"]:
        prompt += "\n\n" + got["instructions"]

    hidden = set(got.get("subagent_only") or [])
    main_tools = [t for t in tools if t.name not in hidden]
    graph = create_deep_agent(
        model=model or models.get("model"),
        tools=main_tools,
        system_prompt=prompt,
        middleware=[budget, tracker, roles, screen, record, guard_mem, large, GroundingGuard(), FollowThrough(),
                    *_fallback(),
                    *([LookFirst()] if settings.look_first else []), *got.get("middleware", []), *extra_mw],
        subagents=specs or None,
        skills=skill_paths or None,
        memory=memory_paths or None,
        permissions=backends.permissions(settings, sandbox) or None,
        backend=backend,
        interrupt_on=interrupts or None,
        checkpointer=memory.checkpointer,
        store=memory.store,
        name=settings.name,
        debug=settings.debug,
    )
    info = {"model": models.describe() if model is None else {"model": type(model).__name__},
            "tools": len(tools), "main_agent_tools": len(main_tools), "writes": writes, "subagents": [s["name"] for s in specs],
            "skills": skill_paths, "memory": memory_paths, "mcp": mcp_info, "state": memory.where,
            "sandbox": (sandbox.runtime.describe() if sandbox is not None and hasattr(sandbox, "runtime") else
                        ("a sandbox" if sandbox is not None else ("none: " + sandbox_note if sandbox_note else "none")))}
    logger.info("agent %s: %d tools (%d need approval), subagents %s", settings.name, len(tools), len(writes),
                info["subagents"] or "none")
    info["interrupt_on"] = sorted(interrupts)
    info["middleware"] = [type(m).__name__ for m in extra_mw]
    return CanahAgent(graph=graph, settings=settings, memory=memory, writes=approvals, info=info, sandbox=sandbox,
                      interrupts=interrupts, ops=ops)


def _fallback() -> List[Any]:
    """models.fallback_model, when one is set: the main agent switches to it if its model fails."""
    fb = getattr(models, "fallback_model", None)
    if fb is None:
        return []
    from langchain.agents.middleware import ModelFallbackMiddleware
    return [ModelFallbackMiddleware(fb)]


def _load_middleware(specs: Sequence[str]) -> List[Any]:
    """Middleware named as "package.module:Class" or "package.module:factory"."""
    import importlib
    out = []
    for spec in specs or []:
        mod, _, attr = str(spec).partition(":")
        if not mod or not attr:
            raise ValueError(f"middleware {spec!r}: write it as package.module:Class")
        obj = getattr(importlib.import_module(mod), attr)
        out.append(obj() if callable(obj) else obj)
    return out
