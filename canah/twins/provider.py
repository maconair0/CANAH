"""
The agent's twin tools, the same whichever twin is in use (the registry's active one, looked up at each call,
so switching twins on the web page needs no restart), and the gate that holds controller writes to the
twin's check.

  twin_list         the twin in use: its domains, what each answers, its KPIs, what it does not model
  twin_schema       a domain's worked example scenario
  twin_validate     a scenario checked before it runs
  twin_predict      the prediction, in the contract's one shape
  twin_check_write  a controller write tried in the twin before it is sent (twins that can)
  twin_override     (a write: an operator approves it) go ahead with a write the twin did not support
  twin_extra        one of the twin's own read-only helpers (twin_list names them: a snapshot, a slice...)

  make_gate()       the gate, a middleware on every agent: a controller write runs only when the active twin's check
                    lets it (propose, auto_apply_eligible) or an operator overrode that check; with no twin,
                    or a twin that does not check writes, writes go through approval alone
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field, field_validator

from . import contract as C
from .registry import Registry
from .activity import FEED, controller_view, scenario_view

logger = logging.getLogger("canah.twins")


def _obj(v):
    if isinstance(v, str):
        try:
            return json.loads(v) if v.strip() else {}
        except ValueError as e:
            raise ValueError(f"not valid JSON: {e}") from None
    return v


class ScenarioArg(BaseModel):
    scenario: Dict[str, Any] = Field(description="the scenario, a JSON object in the format twin_schema shows")

    @field_validator("scenario", mode="before")
    @classmethod
    def _t(cls, v):
        return _obj(v)


class ExtraArg(BaseModel):
    name: str = Field(description="the extra's name, as twin_list lists it")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="its arguments")

    @field_validator("arguments", mode="before")
    @classmethod
    def _t(cls, v):
        return _obj(v) or {}


class CheckArg(BaseModel):
    controller: str = Field(description="the controller's key (network_controllers names them)")
    tool: str = Field(description="the controller's write tool, without '<controller>__', or a contract verb "
                                  "(provision, teardown, adjust, configure_device)")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="exactly the arguments the write will be sent with")
    answers: Dict[str, Any] = Field(default_factory=dict, description="the operator's answers to the twin's questions")

    @field_validator("arguments", "answers", mode="before")
    @classmethod
    def _t(cls, v):
        return _obj(v)


def _norm(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): _norm(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).strip()
    try:
        return float(t)
    except ValueError:
        return t.lower()


def _key(controller: str, tool: str, args: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps([controller, tool, _norm(args)], sort_keys=True).encode()).hexdigest()[:20]


class Checks:
    """The checks the twins made in this process, and the operators' overrides of them."""

    def __init__(self, ttl_s: float = 900.0):
        self.ttl_s = ttl_s
        self.by_id: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._running: Dict[str, threading.Event] = {}     # a check under way (a precheck), by write key

    def start_running(self, k: str) -> bool:
        with self._lock:
            if k in self._running:
                return False
            self._running[k] = threading.Event()
            return True

    def end_running(self, k: str) -> None:
        with self._lock:
            ev = self._running.pop(k, None)
        if ev is not None:
            ev.set()

    def is_running(self, k: str) -> bool:
        with self._lock:
            return k in self._running

    def wait_running(self, k: str, timeout_s: float) -> None:
        """Wait for a check of this write already under way, rather than start the same one again."""
        with self._lock:
            ev = self._running.get(k)
        if ev is not None:
            ev.wait(timeout_s)

    def keep(self, check: C.Check, twin: str) -> Dict[str, Any]:
        rec = {**check.model_dump(), "twin": twin, "at": time.time(), "key": _key(check.controller, check.tool, check.arguments)}
        with self._lock:
            old = self.by_id.get(check.check_id)
            if old and old.get("override"):               # an operator's word is not undone by checking again
                rec["override"] = old["override"]
            self.by_id[check.check_id] = rec
        return rec

    def override(self, check_id: str, reason: str, by: str) -> Dict[str, Any]:
        with self._lock:
            rec = self.by_id.get(check_id)
            if rec is None:
                raise KeyError(f"no twin check {check_id!r}")
            rec["override"] = {"by": by, "reason": reason, "at": time.time()}
            return rec

    def fresh(self, controller: str, tool: str, args: Dict[str, Any], max_age_s: float = 600.0) -> Optional[Dict[str, Any]]:
        """The latest check of exactly this write, if it was made in the last `max_age_s` (before its approval, say)."""
        k, now = _key(controller, tool, args), time.time()
        with self._lock:
            got = [r for r in self.by_id.values() if r["key"] == k and r["controller"] == controller and r["tool"] == tool
                   and now - r["at"] <= max_age_s]
        return max(got, key=lambda r: r["at"]) if got else None

    def overridden(self, controller: str, tool: str, args: Dict[str, Any], change: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """An override that covers this write: the same call, or the same change written differently."""
        k = _key(controller, tool, args)
        now = time.time()
        with self._lock:
            for rec in sorted(self.by_id.values(), key=lambda r: -r["at"]):
                if not rec.get("override") or now - rec["at"] > self.ttl_s:
                    continue
                if rec["controller"] != controller or rec["tool"] != tool:
                    continue
                if rec["key"] == k or (change is not None and rec.get("change_ran") == change):
                    return rec
        return None


def _user() -> str:
    try:
        from langgraph.config import get_config
        c = (get_config() or {}).get("configurable") or {}
        return str(c.get("user_id") or "")
    except Exception:  # noqa: BLE001
        return ""


def compact(r: C.Result) -> Dict[str, Any]:
    """The result as the agent reads it: everything it may report, rounded so it can be quoted."""
    def st(s: Optional[C.Stats]):
        return None if s is None else {k: (round(v, 6) if isinstance(v, float) else v) for k, v in s.model_dump().items() if v is not None}
    if not r.ok:
        return {"ok": False, "problems": r.problems}
    return {"ok": True, "twin": r.twin, "domain": r.domain, "run_id": r.run_id,
            "decision": r.decision.model_dump() if r.decision else None,
            "probability": r.probability.model_dump() if r.probability else None,
            "baseline_probability": r.baseline_probability.model_dump() if r.baseline_probability else None,
            "trust": r.trust.model_dump() if r.trust else None,
            "figures": [{"entity": f.entity, "kpi": f.kpi, "changed": st(f.changed), "baseline": st(f.baseline),
                         "effect": st(f.effect), "calibrated_90": [round(x, 6) for x in f.calibrated_90] if f.calibrated_90 else None}
                        for f in r.figures],
            "requirements": r.requirements,
            "change_ran (exactly what was simulated; say only this)": r.change_ran,
            "not_modelled": r.not_modelled, "fidelity": r.fidelity, "cached": r.cached}


class TwinsProvider:
    def __init__(self, registry: Registry, home: Path, checks: Optional[Checks] = None):
        self.registry = registry
        self.home = Path(home)
        self.checks = checks or Checks()
        self._asked: Dict[str, List[float]] = {}
        self._tools = self._make()

    def _twin(self):
        t = self.registry.active()
        if t is None:
            raise RuntimeError("no digital twin is in use (choose one on the Digital twins page)")
        return t

    def _make(self) -> List[BaseTool]:
        def twin_list() -> str:
            """The digital twin in use: its name, the network domains it covers and, for each, what it answers,
            its KPIs, its fidelity and what it does not model. The other twins configured are listed too."""
            try:
                t = self._twin()
                d = t.describe()
            except Exception as e:  # noqa: BLE001
                return f"Error: {e}"
            return json.dumps({"in_use": d.name, "version": d.version, "about": d.about, "capabilities": d.capabilities,
                               "domains": [{"domain": x.domain, "title": x.title, "answers": x.answers, "kpis": x.kpis,
                                            "fidelity": x.fidelity, "not_modelled": x.not_modelled} for x in d.domains],
                               "extras (call with twin_extra)": [{"name": x.name, "description": x.description,
                                                                  "arguments": x.parameters} for x in d.extras],
                               "other_twins": [e["name"] for e in self.registry.entries() if e["name"] != d.name]})

        def twin_schema(domain: str) -> str:
            """A worked example scenario for one domain of the twin in use: copy it, put the network and the
            what-if in, then twin_validate. The KPIs a requirement may name are listed with it."""
            op = FEED.begin("schema", self._name(), domain, detail=f"the {domain} example")
            try:
                d = self._twin().describe()
            except Exception as e:  # noqa: BLE001
                FEED.end(op, False, error=str(e))
                return f"Error: {e}"
            FEED.end(op, True)
            info = next((x for x in d.domains if x.domain == domain), None)
            if info is None:
                return f"Error: the twin {d.name!r} has no domain {domain!r}; it has {[x.domain for x in d.domains]}"
            now = time.time()
            seen = [x for x in self._asked.get(domain, []) if now - x < 120]
            self._asked[domain] = seen + [now]
            out = {"domain": domain, "twin": d.name, "example": info.example, "kpis": info.kpis, "answers": info.answers,
                   "snapshot_id": ("leave it empty for a network the operator described or you built from live reads; set it "
                                   "only to a snapshot you took just now: a stored one over an hour old is rejected")}
            if seen:
                out["note"] = "You already have this example. Build the scenario now and call twin_validate; do not ask again."
            return json.dumps(out)

        def twin_validate(scenario: Dict[str, Any]) -> str:
            """Check a scenario with the twin in use before running it: its problems, if any, to fix."""
            op = FEED.begin("validate", self._name(), view=scenario_view(scenario), detail=str((scenario or {}).get("question") or ""))
            try:
                v = self._twin().validate(scenario).model_dump()
            except Exception as e:  # noqa: BLE001
                FEED.end(op, False, error=str(e))
                return f"Error: {e}"
            FEED.end(op, bool(v.get("ok")), v, error="; ".join(v.get("problems") or [])[:300])
            return json.dumps(v)

        def twin_predict(scenario: Dict[str, Any]) -> str:
            """Run a scenario in the twin in use: with and without its change, as many times as it needs. Returns
            the decision (propose, reject, gather_more_data; met or not_met when no change was asked about) with its
            reasons, the probability that the requirements hold and its interval, the trust tier, the figures behind
            them, exactly what change ran and what the twin does not model. Report these as given; the decision is
            the twin's, not yours."""
            op = FEED.begin("predict", self._name(), view=scenario_view(scenario), detail=str((scenario or {}).get("question") or ""))
            try:
                r = self._twin().predict(scenario)
            except Exception as e:  # noqa: BLE001
                FEED.end(op, False, error=str(e))
                return f"Error: the twin failed: {e}"
            FEED.end(op, bool(r.ok), compact(r), error="; ".join(r.problems or [])[:300])
            if r.ok and r.run_id:
                runs = self.home / "twin_runs"
                runs.mkdir(parents=True, exist_ok=True)
                (runs / f"{r.run_id}.json").write_text(r.model_dump_json())
                # what it ran on, for the Digital twins page's plot: the scenario, when, whether it was hypothetical
                try:
                    from ..safety import _DEPTH, _run
                    hypo = bool((_run() or {}).get("hypothetical"))
                    by = _DEPTH.get() or "supervisor"
                except Exception:  # noqa: BLE001
                    hypo, by = False, ""
                (runs / f"{r.run_id}.scenario.json").write_text(json.dumps(
                    {"run_id": r.run_id, "at": time.time(), "twin": r.twin, "domain": r.domain, "by": by,
                     "hypothetical": hypo, "user": _user(), "scenario": scenario}, default=str))
            return json.dumps(compact(r), default=str)

        def twin_check_write(controller: str, tool: str, arguments: Dict[str, Any] = None, answers: Dict[str, Any] = None) -> str:
            """Try a controller write in the twin in use BEFORE sending it, with exactly the arguments you will send.
            Do what `next` says: propose, send it (the operator approves it); reject, do not; ask_operator or
            gather_more_data, ask the operator; not_modelled, send it (it goes to approval alone)."""
            op = FEED.begin("check_write", self._name(), view=_live_view(controller, arguments or {}),
                            detail=f"{tool} on {controller}")
            try:
                t = self._twin()
                if not t.can_check_writes():
                    FEED.end(op, True, {"action": "no_twin"})
                    return json.dumps({"action": "no_twin", "next": f"The twin {t.name!r} does not check controller writes: "
                                       "the write goes through the operator's approval alone."})
                c = t.check_write(controller, tool, arguments or {}, answers or {})
            except Exception as e:  # noqa: BLE001
                FEED.end(op, False, error=str(e))
                return f"Error: {e}"
            FEED.end(op, True, {"action": c.action, "reasons": c.reasons, "questions": c.questions, **({"prediction": compact(c.result)} if c.result is not None and c.result.ok else {})})
            rec = self.checks.keep(c, t.name)
            out = c.model_dump(exclude={"result"})
            if c.action == "not_modelled":
                # The same rule as the write gate: a change the twin cannot represent is not held by it.
                out["next"] = ("The twin cannot check this kind of change. Send the write as planned: it goes to the "
                               "operator's approval alone, and the result says it was not checked.")
            if c.result is not None and c.result.ok:
                out["prediction"] = compact(c.result)
            out["twin"] = rec["twin"]
            return json.dumps(out, default=str)

        def twin_override(check_id: str, reason: str) -> str:
            """Go ahead with a write the twin did not support, because the OPERATOR said so, in their words (`reason`).
            It waits for the operator's approval like any change; then send the write you checked."""
            if not reason.strip():
                return "Error: an override needs the operator's reason"
            try:
                rec = self.checks.override(check_id, reason, _user())
            except KeyError as e:
                return (f"Error: {e}. An override is only for a check the write gate held: its id is in the 'Not sent' "
                        f"message of the held write, and the operator must have said to go ahead anyway. To make a "
                        f"change, send it (or hand it to the domain's config agent): the gate checks it in the twin "
                        f"by itself.")
            call = f"{rec['controller']}__{rec['tool']}"
            return json.dumps({"ok": True, "check_id": check_id, "override": rec["override"], "send_as": call,
                               "arguments": rec["arguments"],
                               "next": f"Send the write now, once: {rec['tool']} on {rec['controller']} with exactly "
                                       f"{json.dumps(rec['arguments'])}. If you do not hold the write, hand it with `task` to "
                                       f"the config agent for {rec['controller']}'s domain, in these words: \"Make this change "
                                       f"now: {rec['tool']} on {rec['controller']} with {json.dumps(rec['arguments'])}; the "
                                       f"twin's check {check_id} was overridden by the operator.\" Do not override again: "
                                       f"the gate lets this exact write through now."})

        def twin_extra(name: str, arguments: Dict[str, Any] = None) -> str:
            """Call one of the twin's own read-only helpers by name (twin_list lists them with their arguments):
            for example a network's snapshot, or the slice of it to build a scenario from."""
            op = FEED.begin("extra", self._name(), detail=name)
            try:
                got = self._twin().extra(name, _obj(arguments) or {})
            except Exception as e:  # noqa: BLE001
                FEED.end(op, False, error=str(e))
                return f"Error: {e}"
            sc = got.get("scenario") if isinstance(got, dict) and isinstance(got.get("scenario"), dict) else \
                {"domain": "optical", **got} if isinstance(got, dict) and got.get("topology") else None
            FEED.end(op, True, view=scenario_view(sc) if sc else None)
            return json.dumps(got, default=str)

        plain = [StructuredTool.from_function(f) for f in (twin_list, twin_schema, twin_override)]
        plain.append(StructuredTool.from_function(twin_extra, args_schema=ExtraArg))
        takes = [StructuredTool.from_function(f, args_schema=ScenarioArg) for f in (twin_validate, twin_predict)]
        takes.append(StructuredTool.from_function(twin_check_write, args_schema=CheckArg))
        return plain + takes

    def _name(self) -> str:
        t = self.registry.active()
        return t.name if t is not None else ""

    def tools(self) -> List[BaseTool]:
        return list(self._tools)

    def writes(self) -> List[str]:
        return ["twin_override"]

    def shared_tools(self) -> List[str]:
        return ["twin_check_write", "twin_override", "twin_list"]

    def instructions(self) -> str:
        return ("Digital twins. A twin predicts what a change or a failure does to a network before the network sees it. "
                "The one in use is on the Digital twins page; twin_list says what it covers.\n"
                "- What-if questions: take the network from the question when the operator describes it, or from what you "
                "read; start from twin_schema(domain)'s example, then twin_validate and twin_predict. Use the twin's domain "
                "that matches the network.\n"
                "- Before any write to a controller, call twin_check_write with exactly the arguments you will send, and do "
                "what its `next` says. Override (twin_override) only on the operator's word.\n"
                "- Report the twin's decision, its probability and interval, the trust tier, the figures, what change ran and "
                "what it does not model, as given. Say which twin answered. The decision is the twin's, not yours.")


class Precheck:
    """A change proposed is tried in the twin at once, while it waits for approval: the operator sees the twin's
    verdict on the approval card, and the gate uses that same check when the write is sent (no second run)."""

    # One try at a time, in the background: the replies and the operations never wait for it, and a burst of
    # proposals does not turn into a burst of simulations competing with the agent for the machine.
    _ONE = threading.Semaphore(1)

    def __init__(self, registry: Registry, checks: Checks, resolve: Callable[[str, Dict[str, Any]], Optional[tuple]],
                 writes: Callable[[], Set[str]]):
        self.registry, self.checks, self.resolve, self.writes = registry, checks, resolve, writes
        self._since: Dict[str, float] = {}
        self._failed: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def target(self, name: str, args: Dict[str, Any]) -> Optional[tuple]:
        if name not in self.writes():
            return None
        got = self.resolve(name, args or {}) if self.resolve else None
        if got is None and "__" in name:
            controller, tool = name.split("__", 1)
            got = (controller, tool, args or {})
        return got

    def start(self, name: str, args: Dict[str, Any], ctx: Optional[Dict[str, str]] = None) -> bool:
        """Returns at once. Only the in-memory steps run here (which controller, already tried or under way); asking
        the twin anything, its description included, happens in the background thread."""
        target = self.target(name, args)
        if target is None:
            return False
        controller, tool, a = target
        k = _key(controller, tool, a)
        if self.checks.fresh(controller, tool, a) or not self.checks.start_running(k):
            return False
        with self._lock:
            self._since[k] = time.time()
            self._failed.pop(k, None)

        def run():
            op = None
            try:
                with self._ONE:
                    t = self.registry.active()
                    if t is None or not t.can_check_writes():
                        return
                    op = FEED.begin("precheck", t.name, view=_live_view(controller, a), detail=f"{tool} on {controller}", ctx=ctx)
                    c = t.check_write(controller, tool, a, {})
                    self.checks.keep(c, t.name)
                    FEED.end(op, True, {"action": c.action, "reasons": c.reasons, "questions": c.questions, **({"prediction": compact(c.result)} if c.result is not None and c.result.ok else {})})
            except Exception as e:  # noqa: BLE001 - said on the card; the gate checks again when it is sent
                with self._lock:
                    self._failed[k] = {"error": str(e)[:300], "at": time.time()}
                if op:
                    FEED.end(op, False, error=str(e))
            finally:
                self.checks.end_running(k)
        threading.Thread(target=run, name="canah-twin-precheck", daemon=True).start()
        return True

    def state(self, name: str, args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """For the approval card: running, the verdict, or why the twin could not try it."""
        t = self.registry.active()
        target = self.target(name, args)
        if t is None or target is None:
            return None
        controller, tool, a = target
        desc = getattr(t, "_desc", None)       # what is known already: a card's refresh never asks the twin itself
        if desc is not None and "check_write" not in (desc.capabilities or []):
            return {"state": "unchecked", "twin": t.name, "why": f"the twin {t.name} does not check controller writes"}
        k = _key(controller, tool, a)
        if self.checks.is_running(k):
            return {"state": "running", "twin": t.name, "since": self._since.get(k)}
        with self._lock:
            failed = self._failed.get(k)
        rec = self.checks.fresh(controller, tool, a)
        if rec is None:
            return {"state": "error", "twin": t.name, "why": failed["error"]} if failed else None
        res = rec.get("result") or {}
        return {"state": "done", "twin": rec.get("twin"), "check_id": rec.get("check_id"), "action": rec.get("action"),
                "reasons": (rec.get("reasons") or [])[:4], "questions": (rec.get("questions") or [])[:3],
                "decision": ((res.get("decision") or {}).get("action")), "p": ((res.get("probability") or {}).get("p")),
                "run_id": res.get("run_id"), "override": rec.get("override")}


def _live_view(controller: str, arguments: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        return controller_view(FEED.topology() if FEED.topology else {}, controller, arguments)
    except Exception:  # noqa: BLE001 - a step without a drawing is still a step
        return None


def make_gate(registry: Registry, checks: Checks, writes: Callable[[], Set[str]],
              resolve: Optional[Callable[[str, Dict[str, Any]], Optional[tuple]]] = None):
    """`resolve(tool name, args)`: (controller, controller's tool, its arguments) for a write tool that is not
    named <controller>__<tool> (the network provider's net_<verb> writes and controller_write)."""
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.messages import ToolMessage

    class _Gate(AgentMiddleware):
        def _hold(self, request):
            name = str(request.tool_call.get("name") or "")
            if name not in writes():
                return None, None
            target = resolve(name, request.tool_call.get("args") or {}) if resolve else None
            if target is None and "__" not in name:
                return None, None
            t = registry.active()
            if t is None or not t.can_check_writes():
                return None, None
            if target is not None:
                controller, tool, args = target
            else:
                controller, tool = name.split("__", 1)
                args = request.tool_call.get("args") or {}
            # A try of this very write still under way (approved while the twin was at it): wait for it, which is
            # sooner than starting the same simulation again.
            checks.wait_running(_key(controller, tool, args), 300.0)
            earlier = checks.fresh(controller, tool, args)
            op = FEED.begin("gate", t.name, view=_live_view(controller, args),
                            detail=f"{tool} on {controller}" + (" (the check made when it was proposed)" if earlier else ""))
            try:
                if earlier is not None:   # tried when it was proposed, the operator saw it: the same check, not a second run
                    c = C.Check.model_validate({k: earlier[k] for k in C.Check.model_fields if k in earlier})
                else:
                    c = t.check_write(controller, tool, args, {})
                FEED.end(op, True, {"action": c.action, "reasons": c.reasons, "questions": c.questions, **({"prediction": compact(c.result)} if c.result is not None and c.result.ok else {})})
            except Exception as e:  # noqa: BLE001 - a twin that fails holds the write and says why
                FEED.end(op, False, error=str(e))
                return ToolMessage(tool_call_id=request.tool_call["id"], name=name, status="error",
                                   content=f"Not sent: the digital twin {t.name!r} could not check this write: {e}"), None
            rec = checks.keep(c, t.name)
            # A twin that cannot represent the change has no grounds to hold it: like no twin, it goes to the
            # operator's approval alone, and the result says the twin could not check it.
            if c.action in C.GO or c.action in ("no_twin", "not_modelled"):
                return None, rec
            ov = checks.overridden(controller, tool, args, c.change_ran)
            if ov:
                return None, ov
            body = {k: v for k, v in c.model_dump(exclude={"result"}).items() if v not in (None, [], {}, "")}
            return ToolMessage(tool_call_id=request.tool_call["id"], name=name, status="error", content=(
                f"Not sent: the digital twin {t.name!r} holds this write ({c.action}).\n" + json.dumps(body, indent=1, default=str))), None

        def _after(self, rec, result):
            if rec is None or not isinstance(result, ToolMessage):
                return result
            if rec.get("action") == "no_twin":
                text = result.content if isinstance(result.content, str) else str(result.content)
                return result.model_copy(update={"content": text + f"\n\n[Not checked: the digital twin {rec['twin']!r} could not "
                                                 "check this write (no_twin), so it went to approval alone. Do not say the twin "
                                                 "checked or verified it.]"})
            if rec.get("action") == "not_modelled":
                text = result.content if isinstance(result.content, str) else str(result.content)
                if text.lstrip().lower().startswith("error"):
                    # Said so the model does not blame the twin for the controller's own refusal.
                    return result.model_copy(update={"content": text + "\n\n[That refusal is the controller's own answer. "
                                                     "The digital twin did not check this change and did not refuse it.]"})
                return result.model_copy(update={"content": text + f"\n\n[Not checked: the digital twin {rec['twin']!r} cannot "
                                                 f"model this kind of change (check {rec['check_id']}); it went to approval alone.]"})
            note = (f"\n\n[Checked in the digital twin {rec['twin']!r} first: check {rec['check_id']}, {rec['action']}"
                    + (f", overridden by {rec['override']['by']}: {rec['override']['reason']}" if rec.get("override") else "") + ".]")
            text = result.content if isinstance(result.content, str) else str(result.content)
            return result.model_copy(update={"content": text + note})

        def wrap_tool_call(self, request, handler):
            held, rec = self._hold(request)
            return held if held is not None else self._after(rec, handler(request))

        async def awrap_tool_call(self, request, handler):
            import asyncio
            held, rec = await asyncio.to_thread(self._hold, request)
            return held if held is not None else self._after(rec, await handler(request))

    return _Gate()
