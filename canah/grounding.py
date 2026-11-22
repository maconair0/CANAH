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

import contextvars
import json
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
# The agent's own tools and subagents (set at build): naming them ("net_read_alarms", "optical-config") is not a
# value a tool should have returned. A self-description that listed them was sent back for it.
OWN_NAMES: set = set()
MAX_SENDBACKS = 2   # an answer is sent back at most this often in one turn, whichever checks do it


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
    # 2.06e-4 for 0.000206 (a BER, written the way BERs are): the same figure at the significant digits it gives
    sci = "e" in _norm(value) and v != 0
    digits = len(re.sub(r"\D", "", mantissa).lstrip("0")) or 1
    for g in numbers if numbers is not None else _numbers(given):
        for f in (1.0, 100.0, 0.01):
            if sci:
                if g * f != 0 and f"{g * f:.{digits - 1}e}" == f"{v:.{digits - 1}e}":
                    return True
            elif abs(round(g * f, places) - v) < 10 ** -(places + 6):
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
    # "Path: `F-ab`", "Router: **pe-a**": the value, not its Markdown (with its backticks it was never in any
    # tool's output, and a correct answer was sent back for it)
    found += [v for v in (x.strip("`*_\"'").rstrip(".").strip("`*_\"'") for x in _LABELLED.findall(text))
              if v and re.search(r"[-_./@]", v)]
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
        start = turn_start(msgs)
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
        known = _norm(" ".join(sorted(OWN_NAMES) + [str(m.content) for m in msgs if isinstance(m, (ToolMessage, HumanMessage))
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
            f"otherwise drop the value and say it was not obtained. " + QUIET))]}


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
# A check's message is between the check and the model. The operator reads only the answer, which must read as
# the first and only one ("as per the grounding check, the path was omitted" was written to them).
QUIET = ("Write the answer for the operator as if it were your first: do not mention this check, a revision, an "
         "earlier answer or what you left out because of it.")


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


def is_check(m: Any) -> bool:
    """One of the checks' own messages ("[look-first check] ..."), not the operator's."""
    return isinstance(m, HumanMessage) and "check]" in str(m.content)[:40]


def turn_start(msgs: List[Any]) -> int:
    """Where this turn begins: the operator's last message. Every check skips every check's messages, so one
    check's send-back does not look to another like a new question with nothing read yet."""
    return max((i for i, m in enumerate(msgs) if isinstance(m, HumanMessage) and not is_check(m)), default=0)


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
    out = note + " " + QUIET
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
        # The same answer written again after a check is the model keeping it: the check and the copy are dropped
        # (it was written out five times, word for word, one check after another).
        if (len(msgs) >= 3 and isinstance(msgs[-1], AIMessage) and not msgs[-1].tool_calls and is_check(msgs[-2])
                and isinstance(msgs[-3], AIMessage) and not msgs[-3].tool_calls and msgs[-1].id and msgs[-2].id
                and " ".join(str(msgs[-1].content).split()) == " ".join(str(msgs[-3].content).split())
                and str(msgs[-1].content).strip()):
            run = _this_run()
            if run is not None:
                run["kept"] = True
            return {"messages": [RemoveMessage(id=msgs[-2].id), RemoveMessage(id=msgs[-1].id)]}
        if (len(msgs) >= 3 and isinstance(msgs[-1], AIMessage) and not msgs[-1].tool_calls and is_keep(msgs[-1].content)
                and is_check(msgs[-2])
                and isinstance(msgs[-3], AIMessage) and msgs[-1].id and msgs[-2].id):
            run = _this_run()
            if run is not None:
                run["kept"] = True
            return {"messages": [RemoveMessage(id=msgs[-2].id), RemoveMessage(id=msgs[-1].id)]}
        # KEEP after looking something up is not an answer: what was read may contradict what was said before
        # (it did: an earlier answer said the harness had no roles or audit, the lookup said it had both, and the
        # model kept the earlier one). Once only: answer from what the tools returned.
        if (msgs and isinstance(msgs[-1], AIMessage) and not msgs[-1].tool_calls and is_keep(msgs[-1].content)):
            check = max((i for i, m in enumerate(msgs) if is_check(m)), default=-1)
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
        # The start counts only in a short reply: a full answer that opens with "I'll check the services ..." and
        # goes on with what it found is an answer (it was sent back, the model kept it, the page showed it twice).
        if not (asks or _ANNOUNCE.search(text[-400:]) or (len(text) < 600 and _ANNOUNCE.search(text[:200]))
                or _LACKS.search(text)):
            return None
        start = turn_start(msgs)
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




def _observer() -> bool:
    try:
        from langgraph.config import get_config
        return str(((get_config() or {}).get("configurable") or {}).get("level") or "") == "observer"
    except Exception:  # noqa: BLE001 - outside a run
        return False


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
        if _observer() and asks_change(current_question(msgs) or ""):
            return None                 # an observer's change: the answer is that an admin must ask, no lookup needed
        start = turn_start(msgs)
        turn = msgs[start:]
        if any(isinstance(m, ToolMessage) for m in turn):
            return None
        if any(isinstance(m, HumanMessage) and _LOOK in str(m.content) for m in turn):
            return None
        return {"jump_to": "model", "messages": [HumanMessage(content=anchored(
            f"{_LOOK} You answered without looking anything up. If the question is about this system (its live "
            f"state, its configuration, its documents), read it with your tools first and answer from that.", msgs))]}


_NUM = r"(\d+(?:[.,]\d+)?)"
_RANGE = re.compile(_NUM + r"\s*(?:to|-|–|—|through|and)\s*" + _NUM)
_IN_RANGE = re.compile(_NUM + r"[^.\d\n]{0,40}?\b(?:is|falls|lies|sits|are|fall)\s+(not\s+)?(within|inside|in|outside)\s+"
                       r"(?:(?:of\s+)?(?:this|that|the|the same|its|our)\s+)?(?:allowed\s+|permitted\s+|valid\s+|customer\s+)?"
                       r"(?:range|band|interval|span)?\s*(?:of\s+|from\s+)?(?:" + _NUM + r"\s*(?:to|-|–|—|and)\s*" + _NUM + r")?", re.I)
_CMP = re.compile(_NUM + r"\s*(?:\w+\s+){0,2}?(?:is|are)\s+(not\s+)?(above|over|greater than|more than|higher than|larger than|"
                  r"below|under|less than|lower than|smaller than)\s+(?:the\s+)?(?:\w+\s+){0,2}?" + _NUM, re.I)
_NUM_GUARD = "[arithmetic check]"


def _f(x: str) -> float:
    return float(x.replace(",", "")) if x.count(",") and "." not in x and len(x.split(",")[-1]) == 3 else float(x.replace(",", "."))


_CLAIM = re.compile(r"\b(?:is|falls|lies|sits|are|fall|being)\s+(not\s+)?(within|inside|in|outside)\b", re.I)


def wrong_numbers(text: str) -> List[str]:
    """Claims in an answer that compare numbers, checked: "1200 falls within 100 to 999", "VLAN 1200 ... as it falls
    within the allowed range of VLAN IDs (100 to 999)", "within this range" (the range stated just before), "21.4
    is below 15". The model reads the rule right and gets the comparison wrong; arithmetic is the harness's."""
    out = []
    for c in _CLAIM.finditer(text):
        start = max(text.rfind(". ", 0, c.start()), text.rfind("\n", 0, c.start()), -1) + 1
        before = text[start:c.start()]
        after = text[c.end():c.end() + 80]
        # the range: right after the claim ("within the allowed range of VLAN IDs (100 to 999)"), else the last
        # one stated before it ("VLAN ids 100 to 999 ... and 1200 falls within this range")
        r = _RANGE.search(after)
        if r and not re.search(r"[.;]", after[:r.start()]):
            a, b = r.group(1), r.group(2)
        else:
            prior = [x for x in _RANGE.finditer(text[:c.start()]) if x.end() > c.start() - 400]
            if not prior or not re.search(r"\b(this|that|the|its|our|allowed|permitted|valid|customer)\b[^.]{0,30}"
                                          r"\b(range|band|interval)\b", after[:60], re.I):
                continue
            a, b = prior[-1].group(1), prior[-1].group(2)
        nums = [n for n in re.findall(_NUM, before) if n not in (a, b)]
        if not nums:
            continue
        x = nums[-1]
        try:
            xv, av, bv = _f(x), _f(a), _f(b)
        except ValueError:
            continue
        if av > bv:
            continue
        inside = av <= xv <= bv
        said = (c.group(2).lower() != "outside") != bool(c.group(1))
        if said != inside:
            out.append(f"{x} is {'' if inside else 'not '}within {a} to {b}")
    for m in _CMP.finditer(text):
        try:
            xv, yv = _f(m.group(1)), _f(m.group(4))
        except ValueError:
            continue
        up = m.group(3).lower() in ("above", "over", "greater than", "more than", "higher than", "larger than")
        holds = xv > yv if up else xv < yv
        claimed = not m.group(2)
        if claimed != holds:
            out.append(f"{m.group(1)} is {'' if holds else 'not '}{m.group(3)} {m.group(4)}")
    return list(dict.fromkeys(out))


class NumberCheck(AgentMiddleware):
    """An answer that compares numbers wrongly is sent back once with the right comparison."""

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: Any, runtime: Any) -> Optional[Dict[str, Any]]:
        msgs = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
        if not msgs or not isinstance(msgs[-1], AIMessage) or msgs[-1].tool_calls or is_keep(msgs[-1].content):
            return None
        turn = msgs[turn_start(msgs):]
        if any(isinstance(m, HumanMessage) and _NUM_GUARD in str(m.content) for m in turn):
            return None
        bad = wrong_numbers(str(msgs[-1].content))
        if not bad:
            return None
        return {"jump_to": "model", "messages": [HumanMessage(content=anchored(
            f"{_NUM_GUARD} Your answer compares numbers wrongly: {'; '.join(bad)}. Correct the comparison and "
            f"everything you concluded from it, then answer again.", msgs, keep=False))]}


_STATE_GUARD = "[state check]"
_UPW = {"up", "in-service", "inservice", "active", "available", "true", "enabled", "ok", "online"}
_DOWNW = {"down", "out-of-service", "outofservice", "failed", "inactive", "unavailable", "false", "disabled", "offline"}
_CLAIM_STATE = re.compile(r"\b([A-Za-z][\w.:-]*[\w])\s+(?:is|was|are|remains|stays)\s+(?:currently\s+|now\s+|also\s+)?"
                          r"(down|up|out[- ]of[- ]service|in[- ]service|unavailable|available|failed|offline|online)\b", re.I)


def read_states(msgs: List[Any]) -> Dict[str, str]:
    """{object name: up|down} from every object the tools returned this turn (devices, links, services)."""
    out: Dict[str, str] = {}

    def walk(v):
        if isinstance(v, dict):
            name = v.get("name") or v.get("id") or v.get("node-id")
            st = next((v[k] for k in ("status", "state", "oper-status", "available") if k in v), None)
            if isinstance(name, str) and st is not None:
                w = str(st).lower().replace(" ", "-")
                if w in _UPW:
                    out[name.lower()] = "up"
                elif w in _DOWNW:
                    out[name.lower()] = "down"
            for x in v.values():
                if isinstance(x, (dict, list)):
                    walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    for m in msgs:
        if isinstance(m, ToolMessage):
            text = str(m.content)
            start = text.find("{") if text.find("{") != -1 else text.find("[")
            body = text[start:] if start >= 0 else ""
            body = body.split("\n\n[")[0]
            try:
                walk(json.loads(body))
            except (ValueError, TypeError):
                continue
    return out


class StateCheck(AgentMiddleware):
    """An answer that says an object is down (or up) when what the tools read this turn says otherwise is sent back
    once, with what was read: one answer said pe-b was down when the read had pe-b up and p-2 down."""

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: Any, runtime: Any) -> Optional[Dict[str, Any]]:
        msgs = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
        if not msgs or not isinstance(msgs[-1], AIMessage) or msgs[-1].tool_calls or is_keep(msgs[-1].content):
            return None
        turn = msgs[turn_start(msgs):]
        if any(isinstance(m, HumanMessage) and _STATE_GUARD in str(m.content) for m in turn):
            return None
        seen = read_states(turn)
        if not seen:
            return None
        wrong = []
        for m in _CLAIM_STATE.finditer(str(msgs[-1].content)):
            name, said = m.group(1).lower(), m.group(2).lower().replace(" ", "-")
            said = "up" if said in _UPW or said == "in-service" else "down"
            if name in seen and seen[name] != said and (name, said) not in [(w[0], w[1]) for w in wrong]:
                wrong.append((name, said, seen[name]))
        if not wrong:
            return None
        return {"jump_to": "model", "messages": [HumanMessage(content=anchored(
            f"{_STATE_GUARD} Your answer contradicts what the tools read this turn: "
            + "; ".join(f"you say {n} is {s}, the read says {r}" for n, s, r in wrong)
            + ". Check which object is which (similar names), correct it and everything concluded from it.",
            msgs, keep=False))]}


_TASK_DEADLINE: contextvars.ContextVar = contextvars.ContextVar("canah_task_deadline", default=0.0)


class TimeBudget(AgentMiddleware):
    """A request has a time budget (settings.max_seconds), across the main agent and every subagent:
    a subagent counts its own steps, so a run that keeps delegating can outlast any step limit.
    Checked before each model call; once the deadline has passed, the run ends with a reply saying so.

    One delegated task also has its own share (`task_seconds`, default a third of the budget): a subagent that
    goes round in circles ends with what it had, and the supervisor still has time to act on it or say so."""

    def __init__(self, task_seconds: float = 0):
        super().__init__()
        self.task_seconds = float(task_seconds or 0)

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: Any, runtime: Any) -> Optional[Dict[str, Any]]:
        import time
        try:
            from langgraph.config import get_config
            deadline = float(((get_config() or {}).get("configurable") or {}).get("deadline") or 0)
        except Exception:  # noqa: BLE001 - outside a run: no budget to hold to
            deadline = 0.0
        sub = float(_TASK_DEADLINE.get() or 0)
        if sub and time.time() >= sub and (not deadline or time.time() < deadline):
            msgs = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
            last = next((str(m.content) for m in reversed(msgs) if isinstance(m, AIMessage) and str(m.content).strip()), "")
            return {"jump_to": "end", "messages": [AIMessage(content=(
                "This task ran out of its share of time before finishing, so it stopped. Nothing more will run for it."
                + (f" What it had last: {last[:800]}" if last else "")))]}
        if not deadline or time.time() < deadline:
            return None
        return {"jump_to": "end", "messages": [AIMessage(content=(
            "I ran out of time for this request before finishing it, so I stopped. Nothing more will run for it. "
            "Ask again more narrowly, or say which part to do first."))]}

    def _share(self) -> float:
        import time
        try:
            from langgraph.config import get_config
            deadline = float(((get_config() or {}).get("configurable") or {}).get("deadline") or 0)
        except Exception:  # noqa: BLE001
            deadline = 0.0
        share = self.task_seconds or (max(60.0, (deadline - time.time()) / 3) if deadline else 0.0)
        if not share:
            return 0.0
        end = time.time() + share
        return min(end, deadline) if deadline else end

    def wrap_tool_call(self, request, handler):
        if str(request.tool_call.get("name")) != "task" or _TASK_DEADLINE.get():
            return handler(request)
        tok = _TASK_DEADLINE.set(self._share())
        try:
            return handler(request)
        finally:
            _TASK_DEADLINE.reset(tok)

    async def awrap_tool_call(self, request, handler):
        if str(request.tool_call.get("name")) != "task" or _TASK_DEADLINE.get():
            return await handler(request)
        tok = _TASK_DEADLINE.set(self._share())
        try:
            return await handler(request)
        finally:
            _TASK_DEADLINE.reset(tok)


_DELEGATE = "[delegation check]"
_WHATIF = re.compile(r"\b(what if|what happens|what would|rehears\w*|simulat\w*|digital twin|the twin|is it safe|safe to|"
                     r"feasib\w*|would \w+(?: \w+){0,6} (keep|survive|hold|still|stay|lose|break|drop))\b|"
                     r"\bif\b[^.?]{0,80}\b(were|was|is|gets?|got) (cut|down|lost|removed|added|failed|broken|degraded)\b", re.I)


_RAN_GUARD = "[twin run check]"
_EXAMPLE = re.compile(r"\b(example|sample) (scenario|network)\b|\bthe twin'?s (own )?example\b|\bexample\b.{0,40}\btwin\b|"
                      r"\btwin\b.{0,40}\bexample\b", re.I)
_LIVE = ("net_read_", "follow_relations", "network_controllers", "network_health", "controller_read")


def _num(v: Any) -> str:
    """A number as written: 5500, 395.5 (not 5500.0)."""
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return str(v)


class QotRuns(AgentMiddleware):
    """On qot. A task about the twin's own example scenario is run in the twin, not looked for in the live network
    (qot searched every controller for "turin" before, and once never ran the twin at all). And a report with a
    TWIN verdict but no prediction behind it is sent back once: the figures in it would be made up (it once said
    "85%, tier B" with no run)."""

    RUNS = ("twin_predict", "twin_check_write")

    def _task(self, msgs: List[Any]) -> str:
        """The task as handed over, and the operator's own question with it: a supervisor once dropped "example"
        from the task, and qot went looking for the example's terminal in the live network."""
        task = next((str(m.content) for m in msgs if isinstance(m, HumanMessage) and not is_check(m)), "")
        try:
            from .safety import _ASKED
            asked = _ASKED.get() or ""
        except Exception:  # noqa: BLE001
            asked = ""
        return f"{task}\n{asked}"

    def _tried(self, msgs: List[Any]) -> bool:
        return any(isinstance(m, AIMessage) and any(c.get("name") in ("twin_validate", "twin_predict", "twin_check_write")
                                                    for c in (m.tool_calls or [])) for m in msgs)

    def _ran(self, msgs: List[Any]) -> bool:
        ids = {c.get("id") for m in msgs if isinstance(m, AIMessage) for c in (m.tool_calls or [])
               if c.get("name") in self.RUNS}
        return any(isinstance(m, ToolMessage) and m.tool_call_id in ids and '"ok": true' in str(m.content) for m in msgs)

    def wrap_tool_call(self, request, handler):
        name = str(request.tool_call.get("name") or "")
        state = getattr(request, "state", None) or {}
        msgs = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
        args = request.tool_call.get("args") or {}
        if name == "twin_extra" and str(args.get("name")) == "route":
            a = args.get("arguments") or {}
            given = self._task(msgs)
            length = a.get("length_km")
            if length not in (None, "", 0) and not re.search(rf"(?<![\d.]){re.escape(_num(length))}(\.0+)?\s*km",
                                                            given, re.I):
                return ToolMessage(tool_call_id=request.tool_call["id"], name=name, status="error", content=(
                    f"Not run: length_km={length} is not a length the operator gave, so it would be made up. Leave "
                    f"length_km out: give the places as the operator did, or their coordinates as 'lat,lon' if the "
                    f"question has them, and the route is computed from them (great-circle distance and a detour, "
                    f"stated). If it then says what it needs, ask the operator for exactly that."))
        if name == "twin_extra" and str(args.get("name")) == "route":
            got = handler(request)
            text = str(getattr(got, "content", got))
            if '"ok": true' in text:
                try:
                    from .safety import _run
                    run = _run()
                    if run is not None:
                        run["hypothetical"] = True       # no change follows in this request (safety.ScopeGuard)
                except Exception:  # noqa: BLE001
                    pass
                if isinstance(got, ToolMessage):
                    got = got.model_copy(update={"content": text + (
                        "\n\n[HYPOTHETICAL: this route is built for the question, from distances, not read from any "
                        "network. Nothing in it exists. Say so in your report (TWIN: ... on a hypothetical route), and "
                        "propose no change from it.]")})
            return got
        live = name.startswith(_LIVE) or (name == "twin_extra" and str(args.get("name")) in ("mirror", "inspect", "subnetwork",
                                                                                        "snapshots"))
        if live and _EXAMPLE.search(self._task(msgs)) and not self._ran(msgs):
            return ToolMessage(tool_call_id=request.tool_call["id"], name=name, status="error", content=(
                "Not run: the task is about the twin's own example scenario, and its objects are not in the live "
                "network. Take the example from twin_schema(domain), change only what the task changes, then "
                "twin_validate and twin_predict, and report what the prediction returns."))
        return handler(request)

    async def awrap_tool_call(self, request, handler):
        import asyncio
        if request.tool_call.get("name") == "twin_extra" and str((request.tool_call.get("args") or {}).get("name")) == "route":
            got = await handler(request)
            return self.wrap_tool_call(request, lambda r: got)
        out = self.wrap_tool_call(request, lambda r: None)
        return out if out is not None else await handler(request)

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: Any, runtime: Any) -> Optional[Dict[str, Any]]:
        msgs = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
        if not msgs or not isinstance(msgs[-1], AIMessage) or msgs[-1].tool_calls or is_keep(msgs[-1].content):
            return None
        text = str(msgs[-1].content)
        if not re.search(r"^\s*\W*TWIN\W*:", text, re.M | re.I) or self._ran(msgs):
            return None
        if re.search(r"TWIN\W*:\W*(not run|not_modelled|not modelled|no twin|cannot)", text, re.I) and (
                self._tried(msgs) or not _EXAMPLE.search(self._task(msgs))):
            return None                 # "cannot" stands once the twin was tried, or for a network it cannot have
        if any(isinstance(m, HumanMessage) and _RAN_GUARD in str(m.content) for m in msgs):
            return None
        return {"jump_to": "model", "messages": [HumanMessage(content=(
            f"{_RAN_GUARD} Your report gives the twin's verdict, but no twin_predict (or twin_check_write) ran in this "
            f"task, so its figures are not the twin's. Run it now: build the scenario (twin_schema's example, changed "
            f"as the task says), twin_validate, twin_predict, and report what it returns. If it cannot be run, write "
            f"TWIN: not run, and why."))]}


_CHANGE_GUARD = "[change check]"
_TWIN_GUARD = "[twin report check]"
_CONFIG_GUARD = "[config check]"


class MakeTheChange(AgentMiddleware):
    """On a config agent: a task is a change to make, so a report with no write tried is sent back once (one
    described the right tool, connect_tenant_external, and stopped). A write that was held, refused or failed
    counts as tried: its report says why."""

    def __init__(self, writes, controller_writes=(), tool_of=None):
        super().__init__()
        self.writes = set(writes)
        self.controller_writes = set(controller_writes)          # every controller's own write tool names
        self.tool_of = tool_of or (lambda name, args: name)

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: Any, runtime: Any) -> Optional[Dict[str, Any]]:
        msgs = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
        if not msgs or not isinstance(msgs[-1], AIMessage) or msgs[-1].tool_calls or is_keep(msgs[-1].content):
            return None
        if _observer() or any(isinstance(m, HumanMessage) and _CONFIG_GUARD in str(m.content) for m in msgs):
            return None
        sent = [c for m in msgs if isinstance(m, AIMessage) for c in (m.tool_calls or []) if c.get("name") in self.writes]
        if sent:
            # Every write tool the task named must have been called: a report once said "I connected it with
            # connect_tenant_external" with only attach_tenant sent.
            task = next((str(m.content) for m in msgs if isinstance(m, HumanMessage) and not is_check(m)), "")
            named = {t for t in self.controller_writes if re.search(rf"\b{re.escape(t)}\b", task)}
            called = set()
            for c in sent:
                try:
                    called.add(self.tool_of(c.get("name"), c.get("args") or {}))
                except Exception:  # noqa: BLE001
                    called.add(c.get("name"))
            missing = sorted(named - called)
            if not missing:
                # A write the controller refused until another step was done ("blue is not on border-leaf-1"), then
                # that step done, and the refused one never tried again: the change is half made.
                results = {m.tool_call_id: str(m.content) for m in msgs if isinstance(m, ToolMessage)}
                failed_then = []
                for i, c in enumerate(sent):
                    if results.get(c.get("id"), "").startswith(("Error", "Not sent")):
                        later = sent[i + 1:]
                        ok_after = any(not results.get(x.get("id"), "").startswith(("Error", "Not sent")) for x in later)
                        again = any(x.get("name") == c.get("name") and x.get("args") == c.get("args") for x in later)
                        if ok_after and not again:
                            failed_then.append((c, results.get(c.get("id"), "")[:200]))
                if not failed_then:
                    tried_args = [(x.get("name"), json.dumps(x.get("args"), sort_keys=True, default=str)) for x in sent]
                    refused = [c for c in sent if results.get(c.get("id"), "").startswith("Error")]
                    if refused and len(refused) == len(sent) and len(set(tried_args)) == len(tried_args) == 1:
                        c = refused[-1]
                        why = results.get(c.get("id"), "")[:400]
                        return {"jump_to": "model", "messages": [HumanMessage(content=(
                            f"{_CONFIG_GUARD} The controller refused your write: {why} If it says a value is wrong "
                            f"(and what it accepts), correct that value and send the write once more. If it says "
                            f"something must be done first (be on a border leaf, say), do that step in your domain, "
                            f"then send this write again. Otherwise report the refusal as the controller gave it."))]}
                    return None
                c, why = failed_then[0]
                return {"jump_to": "model", "messages": [HumanMessage(content=(
                    f"{_CONFIG_GUARD} {c.get('name')} {json.dumps(c.get('args'), default=str)[:300]} was refused "
                    f"({why}), and since then another step succeeded. If that step is what it needed, call it again now, "
                    f"with the same arguments, so the change is complete; if not, say why it still cannot be made."))]}
            return {"jump_to": "model", "messages": [HumanMessage(content=(
                f"{_CONFIG_GUARD} The change you were given names {', '.join(missing)}, and you have not called "
                f"{'it' if len(missing) == 1 else 'them'}. Call {'it' if len(missing) == 1 else 'each'} now with the "
                f"exact arguments (a net_ write verb, or controller_write with the tool's name), and report only "
                f"what was sent."))]}
        return {"jump_to": "model", "messages": [HumanMessage(content=(
            f"{_CONFIG_GUARD} You were given a change to make and you have not called a tool that makes it. Call it "
            f"now with the exact arguments (net_ write verb or controller_write; network_tools finds which). Its "
            f"approval pause is the confirmation. If the controller says another step must come first, do that "
            f"step first. Only if no tool can make this change, say exactly why."))]}
_CHANGE = re.compile(r"^\W*(?:please\s+|can you\s+|could you\s+|now\s+)*(create|delete|remove|connect|attach|detach|add|"
                     r"set|provision|tear down|teardown|reroute|re-route|configure|enable|disable|shut|bring|change|"
                     r"move|increase|decrease|raise|lower|update|apply|restore|disconnect)\b", re.I)


def asks_change(q: str) -> bool:
    """Does any sentence of the request tell the agent to change something ("Create ...", "... Set it up
    accordingly.")? The instruction is often last, after the context. Making or removing a subagent is not a
    change to a network ("create a subagent ...", "the span-checker's use is done: remove it")."""
    try:
        from .user_subagents import about_subagents
        if about_subagents(q or ""):
            return False
    except Exception:  # noqa: BLE001
        pass
    return any(_CHANGE.search(x) for x in re.split(r"(?<=[.!?;:])\s+|\n+", q or "") if x.strip())


class DelegationGuard(AgentMiddleware):
    """With the team, a what-if is qot's: an answer to one, given without asking qot this turn, is sent back once.
    (Without it, the supervisor read the twin's name and wrote what "the twin predicts" with no prediction run.)
    And a plain change is its domain's config agent's: handed to qot instead, it is turned back with where it goes
    (qot rehearsed a plain 'create LP-3' for ten minutes; the write gate rehearses it anyway)."""

    def __init__(self, qot: str = "qot", config_agents: Optional[List[str]] = None, locate=None,
                 agent_of: Optional[Dict[str, str]] = None):
        super().__init__()
        self.qot = qot
        self.config_agents = list(config_agents or [])
        self.locate = locate                    # name -> the controllers whose reads mention it
        self.agent_of = dict(agent_of or {})    # controller -> its config agent

    def _owner_of(self, q: str) -> Optional[str]:
        """The one config agent whose controllers hold the objects the request names, if there is exactly one."""
        if self.locate is None or not self.agent_of:
            return None
        names = set(re.findall(r"\b[A-Za-z][\w.-]*\d[\w.:-]*\b|\b[a-z]+-[a-z0-9-]+\b", q or ""))
        owners = set()
        for n in list(names)[:12]:
            try:
                for ctl in self.locate(n):
                    if ctl in self.agent_of:
                        owners.add(self.agent_of[ctl])
            except Exception:  # noqa: BLE001
                continue
        return owners.pop() if len(owners) == 1 else None

    _SUBJECT = re.compile(r"^\W*(?:please\s+)?(?:connect|attach|detach|disconnect|move|reroute|re-route|restore|enable|"
                          r"disable|shut|bring|change|update|raise|lower|increase|decrease|set)\s+(?:the\s+)?"
                          r"(?:tenant|lightpath|service|router|switch|link|port|transponder|interface|customer|vlan)?\s*"
                          r"([\w:./-]+)", re.I)

    def _subject_elsewhere(self, request) -> Optional[Any]:
        """The change's subject (Connect tenant blue ...) lives on one controller; its first step goes to that
        controller's config agent. (Asked to connect a fabric tenant, the supervisor built a packet service.)"""
        tc = request.tool_call
        target = str((tc.get("args") or {}).get("subagent_type") or "")
        if tc.get("name") != "task" or not target.endswith("-config") or self.locate is None or not self.agent_of:
            return None
        state = getattr(request, "state", None) or {}
        msgs = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
        m = self._SUBJECT.search(current_question(msgs) or "")
        if not m:
            return None
        subject = m.group(1).strip(".,")
        try:
            homes = sorted(set(self.locate(subject)))
        except Exception:  # noqa: BLE001
            return None
        agents = sorted({self.agent_of[h] for h in homes if h in self.agent_of})
        handed = any(isinstance(x, AIMessage) and any(c.get("name") == "task" and (c.get("args") or {}).get(
            "subagent_type") in agents for c in (x.tool_calls or [])) for x in msgs[turn_start(msgs):])
        if len(agents) != 1 or target in agents or handed:
            return None
        from langchain_core.messages import ToolMessage
        return ToolMessage(tool_call_id=tc["id"], name="task", status="error", content=(
            f"Not sent: what this change acts on, {subject}, is on {', '.join(homes)}, which {agents[0]} changes, not "
            f"{target}. Start with {agents[0]}: hand it the change in the operator's words. Hand {target} only a "
            f"step that {agents[0]}'s report says belongs to its domain."))

    def _misrouted(self, request) -> Optional[Any]:
        tc = request.tool_call
        args = tc.get("args") or {}
        if tc.get("name") == "task" and str(args.get("subagent_type", "")).endswith("-config") and _observer():
            from langchain_core.messages import ToolMessage
            return ToolMessage(tool_call_id=tc["id"], name="task", status="error", content=(
                "Not sent: the operator is signed in as an observer, so no change can be made or approved in this "
                "session. Reply with the exact change that would be needed, and that someone signed in as admin has "
                "to ask for it. Nothing else."))
        if tc.get("name") != "task" or args.get("subagent_type") != self.qot or not self.config_agents:
            return None
        state = getattr(request, "state", None) or {}
        msgs = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
        q = current_question(msgs) or ""
        if not asks_change(q) or _WHATIF.search(q) or len(q) > 400:
            return None
        from langchain_core.messages import ToolMessage
        return ToolMessage(tool_call_id=tc["id"], name="task", status="error", content=(
            "Not sent to qot: the operator asked for a change, not a what-if. Hand it straight to the config agent of "
            f"its domain ({', '.join(self.config_agents)}) with the exact change. Its write is rehearsed in the "
            "digital twin automatically before it is sent, and then waits for the operator's approval."))

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: Any, runtime: Any) -> Optional[Dict[str, Any]]:
        return self._whatif(state) or self._undone_change(state) or self._decision_dropped(state)

    def _decision_dropped(self, state: Any) -> Optional[Dict[str, Any]]:
        """qot's report says what the twin decided; an answer that drops it, or says the twin could not answer when
        it did, is sent back once with the report's TWIN line. (One relayed "the twin cannot simulate this" over a
        report with a prediction in it.)"""
        msgs = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
        if not msgs or not isinstance(msgs[-1], AIMessage) or msgs[-1].tool_calls or is_keep(msgs[-1].content):
            return None
        turn = msgs[turn_start(msgs):]
        if any(isinstance(m, HumanMessage) and _TWIN_GUARD in str(m.content) for m in turn):
            return None
        ids = {c.get("id") for m in turn if isinstance(m, AIMessage) for c in (m.tool_calls or [])
               if c.get("name") == "task" and (c.get("args") or {}).get("subagent_type") == self.qot}
        reports = [str(m.content) for m in turn if isinstance(m, ToolMessage) and m.tool_call_id in ids]
        if not reports:
            return None
        line = next((ln for ln in reversed(reports[-1].splitlines()) if ln.strip().upper().startswith("TWIN:")), "")
        decision = next((d for d in ("gather_more_data", "not_modelled", "not_met", "propose", "reject", "met")
                         if re.search(rf"\b{d}\b|\b{d.replace('_', ' ')}\b", line, re.I)), "")
        ran = bool(re.search(r"\b(predicts?|probability|availability|margin|figures?)\b", reports[-1], re.I))
        answer = str(msgs[-1].content)
        dropped = decision and not re.search(rf"\b{decision}\b|\b{decision.replace('_', ' ')}\b|{decision[:5]}", answer, re.I)
        denied = ran and re.search(r"\b(cannot|can.t|could not|unable to|not able to) (simulate|run|answer|predict|determine|"
                                   r"assess|say|tell|confirm|evaluate)", answer, re.I)
        if not (dropped or denied):
            return None
        return {"jump_to": "model", "messages": [HumanMessage(content=anchored(
            f"{_TWIN_GUARD} qot's report has the twin's answer, and yours does not carry it"
            + (f" (the decision: {decision})" if decision else "") + f". Its TWIN line: {line[:600] or reports[-1][-600:]} "
            f"Answer from the report: the figures, the decision, the trust tier and what it does not model, as given.",
            msgs, keep=False))]}

    def _undone_change(self, state: Any) -> Optional[Dict[str, Any]]:
        """An admin asked for a change and the turn ends with a plan or a question, with nothing handed to a config
        agent: sent back once. The approval pause is the confirmation; asking 'shall I?' in text is not."""
        msgs = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
        if not self.config_agents or not msgs or not isinstance(msgs[-1], AIMessage) or msgs[-1].tool_calls \
                or is_keep(msgs[-1].content):
            return None
        try:
            from langgraph.config import get_config
            level = str(((get_config() or {}).get("configurable") or {}).get("level") or "")
        except Exception:  # noqa: BLE001
            level = ""
        if level not in ("admin", "root"):
            return None
        q = current_question(msgs) or ""
        if not asks_change(q) or _WHATIF.search(q) or len(q) > 400:
            return None
        start = turn_start(msgs)
        turn = msgs[start:]
        claims = bool(re.search(r"\b(submitted|handed|delegated|sent (it|the change)|passed (it|the change)|asked "
                                r"\w+-config|is (now )?(waiting|pending) for (your )?approval|(using|with|via|to) the "
                                r"[\w-]+-config( agent)?|I('ll| will) (now )?(create|make|set up|hand|delegate))\b",
                                str(msgs[-1].content), re.I))
        sent_back = sum(isinstance(m, HumanMessage) and _CHANGE_GUARD in str(m.content) for m in turn)
        if sent_back >= (2 if claims else 1):
            return None
        handed = any(isinstance(m, AIMessage) and any(c.get("name") == "task" and str((c.get("args") or {}).get(
            "subagent_type", "")).endswith("-config") for c in (m.tool_calls or [])) for m in turn)
        if handed:
            return None
        target = self._owner_of(q)
        if target:
            return {"jump_to": "model", "messages": [HumanMessage(content=anchored(
                f"{_CHANGE_GUARD} The operator, an admin, asked for this change, and it was not handed on: saying you "
                f"will is not doing it. What it names is {target}'s. Make this call now, as your next step: "
                f"task(subagent_type='{target}', description=<the operator's request, word for word>). Its write waits "
                f"for the operator's approval; do not ask in text.", msgs, keep=False))]}
        return {"jump_to": "model", "messages": [HumanMessage(content=anchored(
            f"{_CHANGE_GUARD} The operator, an admin, asked you to make this change, and nothing was handed to a "
            f"config agent. Do it now: find whose it is (network_tools(query=<the change's words>) searches every "
            f"controller; the design rules, with search_knowledge, say the order of steps across domains), then hand "
            f"each step to its domain's config agent ({', '.join(self.config_agents)}) with `task`. The approval pause "
            f"is the operator's confirmation: do not ask for it in text. Only if it cannot be done at all, say exactly "
            f"why.", msgs, keep=False))]}

    def wrap_tool_call(self, request, handler):
        return self._misrouted(request) or self._subject_elsewhere(request) or handler(request)

    async def awrap_tool_call(self, request, handler):
        import asyncio
        refused = self._misrouted(request) or await asyncio.to_thread(self._subject_elsewhere, request)
        return refused or await handler(request)

    def _whatif(self, state: Any) -> Optional[Dict[str, Any]]:
        msgs = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
        if not msgs or not isinstance(msgs[-1], AIMessage) or msgs[-1].tool_calls or is_keep(msgs[-1].content):
            return None
        q = current_question(msgs)
        if not _WHATIF.search(q or ""):
            return None
        start = turn_start(msgs)
        turn = msgs[start:]
        if any(isinstance(m, HumanMessage) and _DELEGATE in str(m.content) for m in turn):
            return None
        asked = any(isinstance(m, AIMessage) and any(c.get("name") == "task" and (c.get("args") or {}).get("subagent_type") == self.qot
                                                     for c in (m.tool_calls or [])) for m in turn)
        if asked:
            return None
        return {"jump_to": "model", "messages": [HumanMessage(content=anchored(
            f"{_DELEGATE} This is a what-if: it is answered by rehearsing it in the digital twin, which is qot's work, "
            f"not yours. Call task(subagent_type='{self.qot}', description=<the what-if, with what you read>) and "
            f"answer from its report: its PROPOSAL and TWIN lines. Never say what the twin predicts without that.", msgs,
            keep=False))]}


def limit_sendbacks(middleware: List[Any], limit: int = MAX_SENDBACKS) -> List[Any]:
    """Every check sends an answer back at most once, but there are many of them: one after another they had the
    model write the same answer five times. Past `limit` send-backs in a turn, whichever checks made them, the
    answer stands (the checks' other work, such as dropping a KEEP, still happens)."""
    for m in middleware:
        if not isinstance(m, AgentMiddleware) or type(m).after_model is AgentMiddleware.after_model:
            continue
        if getattr(m, "_canah_limited", False):
            continue
        orig = m.after_model

        def wrapped(state, runtime, _orig=orig):
            out = _orig(state, runtime)
            if isinstance(out, dict) and out.get("jump_to") == "model" and any(is_check(x) for x in out.get("messages") or []):
                msgs = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
                sent = sum(1 for x in msgs[turn_start(msgs):] if is_check(x))
                if sent >= limit:
                    return None
            return out
        object.__setattr__(m, "after_model", wrapped)
        object.__setattr__(m, "_canah_limited", True)
    return middleware
