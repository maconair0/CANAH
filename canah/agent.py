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

import json
import os
import re
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from deepagents import create_deep_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from . import approval, backends, identity, mcp, models, subagents as sub
from . import window
from .notify import Notifier
from .grounding import DelegationGuard, FollowThrough, GroundingGuard, KeepAnswer, LookFirst, TimeBudget
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
    usage: Dict[str, Any] = field(default_factory=dict)  # context window filled, KV cache (canah.window)

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
    twins: Any = None               # canah.twins.Registry: the digital twins and the one in use
    twin_checks: Any = None         # the checks they made, and operators' overrides
    providers: List[Any] = field(default_factory=list)   # the ToolProviders, for warm()
    notifier: Any = None            # canah.notify.Notifier: incidents and waiting approvals, to the channels
    model: Any = None               # the main agent's model

    def warm(self) -> Dict[str, float]:
        """Load what the first question would otherwise wait for: each provider's own models and indexes
        (the re-ranker, the embedder, BM25) and the main model, into memory. Seconds each, by name."""
        import time
        took: Dict[str, float] = {}
        for p in self.providers:
            if hasattr(p, "warm"):
                t = time.time()
                try:
                    p.warm()
                except Exception as e:  # noqa: BLE001 - warming is an optimisation, never a failure
                    logger.warning("warming %s failed: %s", type(p).__name__, e)
                took[type(p).__name__] = round(time.time() - t, 2)
        if self.model is not None:
            t = time.time()
            try:
                # A local model only (a hosted one is always loaded, and would cost tokens). One real turn on a
                # thread no user owns: Ollama loads the model and keeps the agent's own system prompt and tool
                # list (~7,000 tokens) in its prompt cache, so the first question does not spend ~2.5 s reading
                # them. Straight on the graph, so no metrics or audit; the thread is deleted after.
                if type(self.model).__name__ == "ChatOllama":
                    cfg = self.config(f"_warm.{uuid.uuid4().hex[:8]}")
                    try:
                        self.graph.invoke({"messages": [{"role": "user", "content": "Reply with the single word OK."}]},
                                          cfg)
                    finally:
                        try:
                            self.memory.checkpointer.delete_thread(cfg["configurable"]["thread_id"])
                        except Exception:  # noqa: BLE001
                            pass
            except Exception as e:  # noqa: BLE001
                logger.warning("warming the model failed: %s", e)
            took["model"] = round(time.time() - t, 2)
        return took

    def interrupt_config(self, tool: str) -> Optional[Dict[str, Any]]:
        """How a tool call pauses (allowed decisions, description), or None if it does not."""
        return self.interrupts.get(tool)

    def config(self, thread_id: str, who: Identity = OBSERVER, deadline: bool = False) -> Dict[str, Any]:
        c = {"configurable": {"thread_id": thread_id, "user_id": who.user, "level": who.level,
                              "scope": list(getattr(who, "controllers", ()) or ())},
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
        win = (self.info.get("context") or {}).get("window")
        reply.usage = window.usage(run, win)
        try:
            ev = self.graph.get_state(cfg).values.get("_summarization_event") or {}
            if ev.get("cutoff_message_id") and ev.get("cutoff_message_id") != run.get("summary_at_start"):
                # deepagents summarised the older part of the conversation during this turn
                reply.usage["compacted"] = True
        except Exception:  # noqa: BLE001
            pass
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
                             cache_prompt=run.get("cache_prompt"), cache_hit=run.get("cache_hit"),
                             context_used=run.get("context_tokens"), context_window=win,
                             ended=ended or run.get("ended") or ("waiting" if reply.pending else "answered"))
            for p in reply.pending:
                if self.ops.pending_since(c.get("thread_id"), p["name"]) is None:
                    self.ops.audit("proposed", c.get("user_id", ""), c.get("level", ""), c.get("thread_id", ""),
                                   p["name"], p.get("args"), message=p.get("description", ""))
                    self._notify(c, p)
        return reply

    def _notify(self, c: Dict[str, Any], p: Dict[str, Any]) -> None:
        """Tell someone a change is waiting, on every channel that takes approvals (canah/notify.py)."""
        if self.notifier is None:
            return
        args = json.dumps(p.get("args") or {}, default=str)[:400]
        self.notifier.send("approval_waiting", "major", f"A change waits for approval: {p['name']}",
                           f"Asked by {c.get('user_id')}; needs {p.get('needs') or 'admin'}.\n{p['name']} {args}",
                           link="/#ops", data={"thread": c.get("thread_id"), "requested_by": c.get("user_id"),
                                                "tool": p["name"], "args": p.get("args"), "needs": p.get("needs")})

    def _reply(self, out: Dict[str, Any], cfg: Dict[str, Any], start: int) -> Reply:
        from .grounding import is_keep
        msgs = out.get("messages", [])[start:]
        text = ""
        for m in reversed(msgs):
            if isinstance(m, AIMessage) and str(m.content or "").strip() and not is_keep(m.content):
                text = m.content if isinstance(m.content, str) else str(m.content)
                text = re.sub(r"\n+\s*KEEP\s*$", "", text)       # a KEEP tacked on the end of an answer
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
            values = self.graph.get_state(cfg).values
        except Exception:  # noqa: BLE001
            return 0
        run = cfg["configurable"].get("run")
        if isinstance(run, dict):
            # where the last compaction cut the conversation, to tell at the end whether this turn compacted it
            run["summary_at_start"] = (values.get("_summarization_event") or {}).get("cutoff_message_id")
        return len(values.get("messages", []))

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
               "thread_id": thread_id, "usage": reply.usage}

    def _events(self, payload: Any, cfg: Dict[str, Any]) -> Iterator[Tuple[str, Dict[str, Any]]]:
        from .grounding import KEEP
        seen_calls = set()
        # The start of each reply is held back while it could still be the look-first check's one word
        # KEEP, which never reaches the page; any other reply is let through from its first differing token.
        cur, held, passing = None, "", False
        for ns, mode, chunk in self.graph.stream(payload, cfg, stream_mode=["messages", "updates"], subgraphs=True):
            if mode == "messages":
                msg, meta = chunk
                if not ns and isinstance(msg, AIMessageChunk) and msg.content and not msg.tool_call_chunks:
                    text = msg.content if isinstance(msg.content, str) else ""
                    if not text:
                        continue
                    if msg.id != cur:
                        cur, held, passing = msg.id, "", False
                    if not passing:
                        held += text
                        if KEEP.startswith(held.strip().upper()):
                            continue
                        passing, text, held = True, held, ""
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
                                     "subagent": bool(ns), "ms": (m.additional_kwargs or {}).get("ms")}

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
    # The agentic MCP's address and token as set on the web page, when they were.
    from .network import load_connection
    load_connection(settings.home_path)
    # The multi-agent team (supervisor, qot, a config agent per domain): the network provider reads this.
    os.environ["CANAH_TEAM"] = "1" if settings.team else "0"
    # The model chosen on the web page, and the providers' keys kept there.
    models.load_page_choice(settings.home_path)
    sandbox_note = ""
    if sandbox is None and settings.sandbox == "docker":
        from .sandbox import open_sandbox
        try:
            sandbox = open_sandbox()
        except Exception as e:  # noqa: BLE001 - the agent still starts, and says it has no sandbox
            sandbox_note = str(e)
            logger.warning("starting without a sandbox: %s", e)
    from .tools import load_providers
    # Digital twins: one set of tools for whichever twin is in use, when any is configured.
    from .twins.provider import Checks, TwinsProvider
    from .twins.registry import Registry
    twins = Registry(settings.home_path, settings.twins, settings.twin_active)
    twin_checks = Checks()
    twin_provider = [TwinsProvider(twins, settings.home_path, twin_checks)] if twins.entries() else []
    from .diagrams import DiagramProvider
    from .operations import OperationsProvider
    diagram_provider = [DiagramProvider(settings.home_path)] if settings.diagrams else []
    base_providers = list(providers) + load_providers(settings.providers)

    def describe_now() -> Dict[str, Any]:
        ctls = []
        for pv in base_providers:
            st = getattr(pv, "status", None) or {}
            ctls += [k for k, v in (st.get("controllers") or {}).items() if v.get("ok")]
        act = twins.active() if twins.entries() else None
        down = []
        for pv in base_providers:
            st = getattr(pv, "status", None) or {}
            down += [k for k, v in (st.get("controllers") or {}).items() if not v.get("ok")]
        try:
            sess = next((pv.service.get("/sessions", timeout=5) for pv in base_providers if hasattr(pv, "service")), {}) or {}
            down += [x["controller"] for x in sess.get("sessions", []) if x.get("state") == "down" and x["controller"] not in down]
        except Exception:  # noqa: BLE001
            pass
        return {"controllers": ctls, "controllers_down": down, "twin": getattr(act, "name", None),
                "model": (models.describe() if model is None else {}).get("model")}
    ops_provider = [OperationsProvider(settings.home_path, settings, describe_now)]
    all_providers = base_providers + twin_provider + diagram_provider + ops_provider
    got = merge(all_providers)
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

    team_specs = _qot_spec(tools, set(writes)) if settings.team and any(getattr(pv, "team", False) for pv in all_providers) else []
    if team_specs and twin_provider:
        # The twin's own instructions are qot's to follow.
        team_specs[0]["system_prompt"] += "\n\n" + twin_provider[0].instructions()
    specs = sub.resolve(team_specs + list(got["subagents"]) + list(extra_subagents) + sub.from_yaml(settings.subagents_file),
                        tools)
    # Who may do what: on the main agent and in every subagent (identity.py).
    # deepagents adds a "general-purpose" subagent holding the main agent's
    # tools; declaring it here replaces that one, so it carries the roles too.
    roles = identity.RoleMiddleware(approvals, settings.root_tools)
    from .large_results import LargeResults
    large = LargeResults(limit=settings.large_result_chars, sandbox=sandbox,
                         folder=settings.home_path / "results" if sandbox is None else None)
    extra_mw = [*_load_middleware(settings.middleware), *list(middleware)]
    if twin_provider and settings.twin_gate:
        from .twins.provider import make_gate
        approvals_set = set(approvals)
        resolvers = [pv.write_target for pv in all_providers if hasattr(pv, "write_target")]

        def resolve(name, args):
            for r in resolvers:
                got = r(name, args)
                if got is not None:
                    return got
            return None
        extra_mw.append(make_gate(twins, twin_checks, lambda: approvals_set, resolve))
    if not any(s["name"] == "general-purpose" for s in specs):
        specs.append({"name": "general-purpose",
                      "description": ("A general-purpose agent for a self-contained piece of work that needs several "
                                      "steps: research, computing in the sandbox, drafting. It has only the main "
                                      "agent's own tools: it cannot read or change a controller; delegate those to "
                                      "the main agent, which works the controllers through the agentic MCP."),
                      "system_prompt": ("You do one self-contained piece of work for the main agent and report the "
                                        "result briefly. Read before you conclude; a change is paused for approval.")})
    shared = [t for t in tools if t.name in set(got.get("shared") or [])]
    budget = TimeBudget()
    from .ops import Ops
    from .safety import GuardMemory, RecordWrites, RepeatGuard, RunTracker, ScreenOutput
    ops = Ops(settings.home_path / "ops.sqlite") if settings.audit else None
    tracker, screen, record = RunTracker(settings.max_tokens_per_request), ScreenOutput(), RecordWrites(approvals, ops)
    repeat = RepeatGuard()
    guard_mem = GuardMemory(ops)
    for spec in specs:
        # Every subagent is held to the same checks as the main agent: a value a subagent invents
        # would otherwise reach the main agent looking like a tool result.
        spec["middleware"] = [budget, tracker, repeat, roles, screen, record, large, GroundingGuard(), FollowThrough(),
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
    if team_specs:
        prompt += ("\n\nWith your team, rehearsing in the digital twin is qot's: you do not hold twin_schema, twin_validate, "
                   "twin_predict, twin_check_write or twin_extra. Hand every what-if, and every change before it is made, to "
                   "qot with `task`; where the twin instructions above say to call those tools, qot does.")

    hidden = set(got.get("subagent_only") or [])
    if team_specs:
        # With the team, rehearsing in the twin is qot's: the supervisor keeps which twin is in use (twin_list) and
        # the operator's override (twin_override), and hands every what-if to qot.
        hidden |= {"twin_schema", "twin_validate", "twin_predict", "twin_check_write", "twin_extra"}
    main_tools = [t for t in tools if t.name not in hidden]
    main_model = model or models.get("model")
    # The window in the model's profile, so compaction happens at compact_at of the real window.
    ctx_window = window.give_profile(main_model, settings.context_window)
    for sp in specs:
        if isinstance(sp.get("model"), BaseChatModel):
            window.give_profile(sp["model"], settings.context_window)
    compact_mw = []
    with window.compaction(settings.compact_at, settings.compact_keep):
        if settings.compact_tool and isinstance(main_model, BaseChatModel):
            from deepagents.middleware.summarization import create_summarization_tool_middleware
            compact_mw = [create_summarization_tool_middleware(main_model, backend, system_prompt=(
                "When the operator asks to compact the conversation (or says /compact), call compact_conversation."))]
        graph = create_deep_agent(
            model=main_model,
            tools=main_tools,
            system_prompt=prompt,
            middleware=[budget, tracker, repeat, roles, screen, record, guard_mem, large, GroundingGuard(), FollowThrough(),
                        KeepAnswer(), *([DelegationGuard()] if team_specs else []), *_fallback(),
                        *([LookFirst()] if settings.look_first else []), *got.get("middleware", []), *extra_mw,
                        *compact_mw],
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
    # What each subagent is: the librarian, a general one, or a provider's own (agent_roles). The controllers
    # are not subagents: the agentic MCP holds them and the main agent works them through it.
    roles: Dict[str, Dict[str, Any]] = {}
    for prov in all_providers:
        if hasattr(prov, "agent_roles"):
            try:
                roles.update(prov.agent_roles() or {})
            except Exception:  # noqa: BLE001
                pass
    known = {"librarian": "knowledge", "general-purpose": "general", "qot": "qot"}
    detail = [{"name": sp["name"], "description": str(sp.get("description") or "")[:400],
               "tools": len(sp["tools"]) if sp.get("tools") is not None else None,
               "kind": (roles.get(sp["name"]) or {}).get("kind") or known.get(sp["name"], "custom"),
               **{k: v for k, v in (roles.get(sp["name"]) or {}).items() if k != "kind"}} for sp in specs]
    info = {"model": models.describe() if model is None else {"model": type(model).__name__},
            "subagent_detail": detail,
            "tools": len(tools), "main_agent_tools": len(main_tools), "writes": writes, "subagents": [s["name"] for s in specs],
            "skills": skill_paths, "memory": memory_paths, "mcp": mcp_info, "state": memory.where,
            "sandbox": (sandbox.runtime.describe() if sandbox is not None and hasattr(sandbox, "runtime") else
                        ("a sandbox" if sandbox is not None else ("none: " + sandbox_note if sandbox_note else "none")))}
    logger.info("agent %s: %d tools (%d need approval), subagents %s", settings.name, len(tools), len(writes),
                info["subagents"] or "none")
    info["interrupt_on"] = sorted(interrupts)
    info["middleware"] = [type(m).__name__ for m in extra_mw]
    info["context"] = {"window": ctx_window, "compact_at": settings.compact_at, "compact_keep": settings.compact_keep,
                       "compact_tool": bool(compact_mw)}
    return CanahAgent(graph=graph, settings=settings, memory=memory, writes=approvals, info=info, sandbox=sandbox,
                      interrupts=interrupts, ops=ops, twins=twins, twin_checks=twin_checks,
                      providers=all_providers, model=main_model,
                      notifier=Notifier(settings.notify_channels, settings.notify_url, settings.console_url))


QOT_TOOLS = ("net_read_*", "net_health", "net_compute_path", "net_check_feasibility", "network_controllers", "network_health",
             "follow_relations", "network_tools", "controller_read", "twin_list", "twin_schema", "twin_validate",
             "twin_predict", "twin_check_write", "twin_extra", "incidents", "network_changes", "health_over_time",
             "canah_status", "search_knowledge", "draw_diagram", "get_diagram")


def _qot_spec(tools: Sequence[Any], writes: set) -> List[Dict[str, Any]]:
    """The quality-of-transmission agent: watches quality and health across every domain, works the digital twin,
    proposes configurations. Reads only: it holds no tool that changes a network."""
    import fnmatch
    chosen = [t for t in tools if t.name not in writes and any(fnmatch.fnmatch(t.name, p) for p in QOT_TOOLS)]
    if not chosen:
        return []
    return [{"name": "qot",
             "description": ("The quality-of-transmission agent. Watches the networks' quality and health (margins, OSNR, "
                             "pre-FEC BER, availability, delay, loss, incidents, trends), rehearses what-ifs and changes in "
                             "the digital twin, and proposes the exact configuration to apply with the twin's decision, "
                             "probability and trust. Give it any what-if, any 'is it safe', any degradation, and every "
                             "change before a config agent makes it. It changes nothing itself."),
             "system_prompt": ("You are CANAH's quality-of-transmission agent. You read every domain and you work the digital "
                               "twin; you change nothing. For a question about quality or health: read it (net_read_ tools, "
                               "incidents, health_over_time, network_changes) and report the figures as read. For a what-if "
                               "or a change: build the twin's scenario from what you read (twin_schema for its format), "
                               "twin_validate, twin_predict; for a write to a controller, twin_check_write with exactly the "
                               "arguments it would send. End with:\n"
                               "PROPOSAL: the exact configuration (domain, controller, verb or tool, arguments), or none\n"
                               "TWIN: the decision, probability with its interval, trust tier, and what it does not model\n"
                               "Never say a change is made: the supervisor hands your proposal to the domain's config agent."),
             "tools": chosen}]


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
