"""
An answer states only values it was given.

A small model, told to "report exactly what the command printed", reported a
number and a user name the command never printed: the shell had left
`$(...)` unexpanded inside single quotes, and the model filled in what it
expected. The same slip on a reading from the system (a measured value, an id)
would be worse.

So before a final answer goes out, the values in it that are specific enough
to be checked are looked for in this turn's tool results and in the
operator's own message:

  names joined with - or _ that carry a digit (node-17, INC-214122-2bee), and anything set in
  `backticks`, a value set out as output ("Running as: canah-user" alone on its line, when it looks
  like an identifier) and a single word in quotes ('agent'): held to everything said or returned in
  the conversation, and to the agent's own calls
  counts ("9 transponders", "there are 12 nodes"): the number must appear in this turn's tool
  results (a count computed in the sandbox appears in execute's output)
  decimals with 3 or more significant digits (-2.37, 12.17, 196.05), whole numbers
  with 4 or more digits (18446744073709551616, 2048), as written
  hex strings of 12 or more characters (hashes, ids)

One that appears in neither sends the answer back once, naming the values,
to run the tool again (and fix the command) or to drop the claim. Short
whole numbers ("6 nodes", "100G") are not checked: they are counts and rates the
model often derives correctly, and checking them would send back good answers.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, RemoveMessage

_NUMBER = re.compile(r"(?<![\w.,])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?(?![\w]|\.\d)")
_HEX = re.compile(r"\b[0-9a-fA-F]{12,}\b")
# Names joined with - or _ that carry a digit: objects, services, incidents, containers (node-17, INC-214122-2bee).
_IDENT = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)+\b")
# Anything the answer sets in backticks, as if quoted from output.
# Only a single token: a name, a path, a value. A backticked command with spaces is code the answer
# proposes ("Corrected command: `echo \"...\"`"), not output it claims.
_TICK = re.compile(r"`([^`\s]{2,80})`")
# A value presented as output: "label: value" alone on its line, where the value looks like an identifier
# (canah-user, /workspace/x.json, admin@host), and a single word in quotes ('agent', "node-a").
_LABELLED = re.compile(r"^[ \t*\-•]*[A-Za-z][\w .()/-]{0,40}?:[ \t]*([^\s,;]+)[ \t]*$", re.M)
_QUOTED = re.compile(r"(?<![\w'])'([A-Za-z0-9_.@/-]{2,60})'(?![\w'])|\"([A-Za-z0-9_.@/-]{2,60})\"")
_GUARD = "[grounding check]"


def _significant(token: str) -> bool:
    mantissa = token.split("e")[0].split("E")[0]
    digits = re.sub(r"[^\d]", "", mantissa).lstrip("0")
    return len(digits) >= (3 if "." in mantissa else 4)


def _norm(token: str) -> str:
    return token.replace(",", "").lower()


_FLOAT = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def _derived(value: str, given: str, numbers: Optional[List[float]] = None) -> bool:
    """A value that is a tool's number written another way: rounded to the precision the answer gives
    (4.83 for 4.83265), or as a percentage (95.8 for 0.958, 0.25 for 25). Nothing else: a sum, a ratio
    or a unit change still has to come from a tool (or the sandbox)."""
    try:
        v = float(_norm(value))
    except ValueError:
        return False
    mantissa = _norm(value).split("e")[0]
    places = len(mantissa.split(".")[1]) if "." in mantissa else 0
    for g in numbers if numbers is not None else _numbers(given):
        for f in (1.0, 100.0, 0.01):
            if abs(round(g * f, places) - v) < 10 ** -(places + 6):
                return True
    return False


def _numbers(text: str) -> List[float]:
    out = []
    for t in _FLOAT.findall(text or "")[:20000]:
        try:
            out.append(float(t))
        except ValueError:
            pass
    return out


def specifics(text: str) -> List[str]:
    """The checkable values in a text: long numbers and long hex strings."""
    found = [t for t in _NUMBER.findall(text or "") if _significant(t)]
    found += [h for h in _HEX.findall(text or "") if not h.isdigit()]
    return list(dict.fromkeys(found))


_UNITS = {"of", "to", "and", "or", "in", "on", "at", "by", "per", "percent", "gb", "mb", "kb", "ms", "s", "second",
          "seconds", "minute", "minutes", "hour", "hours", "day", "days", "year", "years", "db", "dbm", "step", "steps",
          "time", "times", "g", "gbps", "mbps", "characters", "chars", "bytes", "tokens", "lines", "percent"}
_COUNT = re.compile(r"(?<![\w.,-])(\d{1,4})\s+([a-z][a-z-]+)(?:\s+([a-z][a-z-]+))?", re.I)


def counts(text: str) -> List[str]:
    """Counts an answer states ("9 transponders", "9 transponder nodes", "there are 12 nodes"): the number."""
    out = []
    for n, w1, w2 in _COUNT.findall(text or ""):
        if n in ("0", "1") or w1.lower() in _UNITS:
            continue
        plural = w1 if w1.lower().endswith("s") else (w2 if w2 and w2.lower().endswith("s") else "")
        if plural and plural.lower() not in _UNITS and len(plural) > 3:
            out.append(n)
    return list(dict.fromkeys(out))


def names(text: str) -> List[str]:
    """The checkable names in a text: joined identifiers with a digit, and backticked strings."""
    text = text or ""
    found = [n for n in _IDENT.findall(text) if any(c.isdigit() for c in n)]
    found += [t.strip() for t in _TICK.findall(text) if t.strip()]
    found += [v.rstrip(".") for v in _LABELLED.findall(text) if re.search(r"[-_./@]", v.rstrip("."))]
    found += [a or b for a, b in _QUOTED.findall(text)]
    found = list(dict.fromkeys(f for f in found if f))
    # A name inside a longer one found here ("nodes-17" in "nodes-17.json") adds nothing.
    return [f for f in found if not any(f != g and f in g for g in found)]


def _calls(msgs) -> str:
    """What the agent itself called, with its arguments: true facts about what it did."""
    out = []
    for m in msgs:
        if isinstance(m, AIMessage):
            for c in m.tool_calls or []:
                out.append(f"{c.get('name')} {c.get('args')}")
                for v in (c.get("args") or {}).values():
                    out.append(str(v))
    return " ".join(out)


class GroundingGuard(AgentMiddleware):
    @hook_config(can_jump_to=["model"])
    def after_model(self, state: Any, runtime: Any) -> Optional[Dict[str, Any]]:
        msgs = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
        if not msgs or not isinstance(msgs[-1], AIMessage) or msgs[-1].tool_calls:
            return None
        start = max((i for i, m in enumerate(msgs) if isinstance(m, HumanMessage) and _GUARD not in str(m.content)),
                    default=0)
        turn = msgs[start:]
        if any(isinstance(m, HumanMessage) and _GUARD in str(m.content) for m in turn):
            return None                                   # sent back once already this turn
        tools_said = [str(m.content) for m in turn if isinstance(m, ToolMessage)]
        if not tools_said:
            return None                                   # nothing was looked up: nothing to hold it to
        answer = str(msgs[-1].content)
        # Values are held to this turn's tools; names to everything said or returned in the conversation,
        # and to what the agent itself called (an object named three turns ago is still known).
        given = _norm(" ".join(tools_said + [str(turn[0].content)] + [_calls(turn)]))
        known = _norm(" ".join([str(m.content) for m in msgs if isinstance(m, (ToolMessage, HumanMessage))
                                and _GUARD not in str(m.content)] + [_calls(msgs)]))
        nums = _numbers(given)
        missing = [v for v in specifics(answer) if _norm(v) not in given and not _derived(v, given, nums)]
        # A count must appear as a number in what the tools returned (a count the sandbox computed is in
        # execute's output); a count read off a clipped list does not.
        numbers = set(re.findall(r"(?<![\w.])\d+(?![\w.])", given))
        missing += [n for n in counts(answer) if n not in numbers and n not in missing]
        missing += [n for n in names(answer) if _norm(n) not in known and n not in missing]
        if not missing:
            return None
        return {"jump_to": "model", "messages": [HumanMessage(content=(
            f"{_GUARD} Your answer states {', '.join(missing[:8])}, which no tool returned. "
            f"Report only what the tools returned (rounding a figure, or giving it as a percentage, is fine). "
            f"If a command did not print what you "
            f"expected (for example `$(...)` inside single quotes is not expanded), fix it and run it again; "
            f"otherwise drop the value and say it was not obtained."))]}


_ANNOUNCE = re.compile(r"(?:^|[.!?]\s+|\n)\s*(?:let me(?! know\b)|i(?:'ll| will) (?:now |first |next )?(?:check|look|run|query|fetch|get|"
                       r"examine|inspect|count|search|read|try|call|ask|process|verify|create|make|apply|connect|execute|send|perform|implement|submit|carry out|shut|delete|remove|configure|"
                       r"attach|delegate|proceed)|next,? i(?:'ll| will)|now i(?:'ll| will)|"
                       r"(?:i now|now i) have (?:all )?(?:the )?(?:necessary |required )?information|here(?:'s| is) the plan|"
                       r"i (?:don't|do not) have (?:the |access to the )?(?:current |live |latest )?(?:\w+ ){0,3}"
                       r"(?:measurement|value|data|reading|state|status))\b",
                       re.I)
_NUDGE = "[follow-through check]"
_LOOK = "[look-first check]"
KEEP = "KEEP"


def is_keep(content: Any) -> bool:
    return str(content or "").strip().strip(".!*`\"'").upper() == KEEP
_LACKS = re.compile(r"\bi (?:don't|do not) have (?:the |access to the )?(?:current |live |latest )?(?:\w+ ){0,3}"
                    r"(?:measurement|value|data|reading|state|status)\b", re.I)
_ASKS = re.compile(r"\b(?:please confirm|confirm (?:if|that|whether) you|"
                   r"(?:would|do) you (?:still )?(?:like|want)(?: me)? to (?:proceed|go ahead|continue|delete|create|"
                   r"remove|make|apply|change|restart|do)|"
                   r"(?:shall|should) i (?:proceed|go ahead|continue|delete|create|remove|do|make|apply)|"
                   r"if so,? i(?:'ll| will) (?:need|proceed|go ahead)|i(?:'ll| will) need your (?:approval|confirmation)|"
                   r"before proceeding[^.]{0,60}confirm)", re.I)


def current_question(msgs: List[Any]) -> str:
    """The operator's message this turn: the last human message that is not one of the checks' own."""
    for m in reversed(msgs):
        if isinstance(m, HumanMessage) and "check]" not in str(m.content)[:40]:
            return str(m.content if isinstance(m.content, str) else m.content)
    return ""


def anchored(note: str, msgs: List[Any], keep: bool = True) -> str:
    """A check's message, tied to the question this turn so the model does not drift to an earlier one, and,
    when the answer may stand as it is, with the one word that keeps it (KEEP) instead of a rewrite."""
    q = " ".join(current_question(msgs).split())
    if len(q) > 400:
        q = q[:400] + " ..."
    out = note
    if q:
        out += f"\n\nThe operator's question this turn: \u00ab{q}\u00bb. Answer that question, not an earlier one."
    if keep:
        out += f" If your answer above already answers it, reply with the single word {KEEP} and it stands as it is."
    return out


def _this_run() -> Optional[Dict[str, Any]]:
    try:
        from langgraph.config import get_config
        run = ((get_config() or {}).get("configurable") or {}).get("run")
    except Exception:  # noqa: BLE001
        return None
    return run if isinstance(run, dict) else None


def answer_kept() -> bool:
    """The model already kept its answer once in this request: no check sends it back again."""
    run = _this_run()
    return bool(run and run.get("kept"))


class KeepAnswer(AgentMiddleware):
    """When a check sent an answer back and the model replies KEEP, the check and the KEEP are dropped and the
    answer stands: one word instead of the whole answer written again (or, worse, a different one)."""

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: Any, runtime: Any) -> Optional[Dict[str, Any]]:
        msgs = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
        if (len(msgs) >= 3 and isinstance(msgs[-1], AIMessage) and not msgs[-1].tool_calls and is_keep(msgs[-1].content)
                and isinstance(msgs[-2], HumanMessage) and any(t in str(msgs[-2].content) for t in (_LOOK, _NUDGE))
                and isinstance(msgs[-3], AIMessage) and msgs[-1].id and msgs[-2].id):
            run = _this_run()
            if run is not None:
                run["kept"] = True
            return {"messages": [RemoveMessage(id=msgs[-2].id), RemoveMessage(id=msgs[-1].id)]}
        # KEEP after looking something up is not an answer: what was read may contradict what was said before
        # (it did: an earlier answer said the harness had no roles or audit, the lookup said it had both, and the
        # model kept the earlier one). Once only: answer from what the tools returned.
        if (msgs and isinstance(msgs[-1], AIMessage) and not msgs[-1].tool_calls and is_keep(msgs[-1].content)):
            check = max((i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)
                         and any(t in str(m.content) for t in (_LOOK, _NUDGE))), default=-1)
            looked = check >= 0 and any(isinstance(m, ToolMessage) for m in msgs[check:])
            run = _this_run()
            if looked and not (run or {}).get("rewrote"):
                if run is not None:
                    run["rewrote"] = True
                return {"jump_to": "model", "messages": [HumanMessage(content=anchored(
                    f"{_LOOK} You looked it up, so KEEP no longer applies: write the answer now from what the tools "
                    f"returned. Where it contradicts something said earlier in this conversation, what the tools "
                    f"returned is right; say so.", msgs, keep=False))]}
        return None


class FollowThrough(AgentMiddleware):
    """A final answer that only announces the next step ("Let me first check ...") is not an answer:
    the run would end with the step never taken. Sent back once: do it now, or answer."""

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: Any, runtime: Any) -> Optional[Dict[str, Any]]:
        msgs = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
        if not msgs or not isinstance(msgs[-1], AIMessage) or msgs[-1].tool_calls:
            return None
        text = str(msgs[-1].content or "")
        if answer_kept() or is_keep(text):
            return None
        asks = bool(_ASKS.search(text)) and self._may_change()
        # A stall ("I now have all the information...") sits at the start; an announced step at the end.
        if not (asks or _ANNOUNCE.search(text[-400:]) or _ANNOUNCE.search(text[:200]) or _LACKS.search(text)):
            return None
        start = max((i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)
                     and _NUDGE not in str(m.content) and _GUARD not in str(m.content)), default=0)
        if any(isinstance(m, HumanMessage) and _NUDGE in str(m.content) for m in msgs[start:]):
            return None
        if asks:
            return {"jump_to": "model", "messages": [HumanMessage(content=anchored(
                f"{_NUDGE} You asked for confirmation in text. If a change was asked for, make it now with the tool: "
                f"the system pauses it for the operator, who sees it and can approve or reject it there. That pause is "
                f"the confirmation. Say any risk you found in one line first.", msgs, keep=False))]}
        return {"jump_to": "model", "messages": [HumanMessage(content=anchored(
            f"{_NUDGE} You said what you would do next, or that you lack something your tools can get, but did not "
            f"do it. If it is needed to answer the question, do it now with your tools.", msgs))]}

    @staticmethod
    def _may_change() -> bool:
        try:
            from langgraph.config import get_config
            level = ((get_config() or {}).get("configurable") or {}).get("level")
        except Exception:  # noqa: BLE001
            return False
        return level in ("admin", "root")




class LookFirst(AgentMiddleware):
    """An answer given in a turn where nothing was looked up is sent back once: look it up if it is
    about the system (its live state, its documents), or give the same answer again if it needs no
    lookup. A model that knows the general shape of a network otherwise describes it instead of
    reading it."""

    def __init__(self, min_chars: int = 120):
        super().__init__()
        self.min_chars = min_chars

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: Any, runtime: Any) -> Optional[Dict[str, Any]]:
        msgs = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
        if not msgs or not isinstance(msgs[-1], AIMessage) or msgs[-1].tool_calls:
            return None
        if is_keep(msgs[-1].content) or answer_kept():
            return None                 # KeepAnswer drops the check and the KEEP; a kept answer is not sent back
        if len(str(msgs[-1].content or "")) < self.min_chars:
            return None
        start = max((i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)
                     and not any(g in str(m.content) for g in (_LOOK, _GUARD, _NUDGE))), default=0)
        turn = msgs[start:]
        if any(isinstance(m, ToolMessage) for m in turn):
            return None
        if any(isinstance(m, HumanMessage) and _LOOK in str(m.content) for m in turn):
            return None
        return {"jump_to": "model", "messages": [HumanMessage(content=anchored(
            f"{_LOOK} You answered without looking anything up. If the question is about this system (its live "
            f"state, its configuration, its documents), read it with your tools first and answer from that.", msgs))]}


class TimeBudget(AgentMiddleware):
    """A request has a time budget (settings.max_seconds), across the main agent and every subagent:
    a subagent counts its own steps, so a run that keeps delegating can outlast any step limit.
    Checked before each model call; once the deadline has passed, the run ends with a reply saying so."""

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: Any, runtime: Any) -> Optional[Dict[str, Any]]:
        import time
        try:
            from langgraph.config import get_config
            deadline = float(((get_config() or {}).get("configurable") or {}).get("deadline") or 0)
        except Exception:  # noqa: BLE001 - outside a run: no budget to hold to
            return None
        if not deadline or time.time() < deadline:
            return None
        return {"jump_to": "end", "messages": [AIMessage(content=(
            "I ran out of time for this request before finishing it, so I stopped. Nothing more will run for it. "
            "Ask again more narrowly, or say which part to do first."))]}
