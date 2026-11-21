"""
Lessons: what went wrong before, kept so it is not repeated (the Reflexion idea, kept narrow and visible).

A conversation's corrections are lost when it ends, so the model could make the same mistake tomorrow. The harness
already sees the corrections that matter for a network, and keeps each as a short lesson:

  refused-then-done   a controller refused a write, and it went through after another step or other values
  not-as-intended     a change was made, and reading it back showed something else than was asked
  twin-rejected       the digital twin rejected a change
  operator-rejected   the operator rejected a change, with their reason

The ones that fit a request (by its words, the controllers and tools in it) are put in front of the agent at the
next one, marked as the harness's and possibly out of date: what a controller answers now wins.

They are memories of a reserved user, "~lessons", so the Documents & memory page lists them, an admin can correct
or forget one, and nothing else reads them. Each expires (`lessons_ttl_days`, default 30); seen again, its count
goes up and its expiry moves.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from typing import Any, Dict, Iterable, List, Optional

from .memory import namespace

USER = "~lessons"
_WORD = re.compile(r"[a-z0-9][a-z0-9_.:-]{2,}")
_COMMON = set("the and for with from that this was were are not but into over after before then than when what "
              "which its has had have been being does did done can could should would will one two all any each "
              "operator controller change changes write writes tool tools".split())


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _words(text: str) -> set:
    """Words and the parts of joined ones: connect_tenant_external also says connect, tenant, external."""
    out = set()
    for w in _WORD.findall(str(text).lower()):
        out.add(w)
        out |= {x for x in re.split(r"[_.:-]+", w) if len(x) >= 3}
    return {w for w in out if w not in _COMMON}


class Lessons:
    def __init__(self, store: Any, ttl_days: float = 30):
        self.store = store
        self.ttl_days = float(ttl_days or 30)

    def add(self, kind: str, text: str, controller: str = "", tool: str = "") -> str:
        """Keep a lesson (or count it again if it is already kept). Its path, under /lessons/."""
        text = " ".join(str(text).split())[:600]
        norm = re.sub(r"\d+", "#", text.lower())
        key = hashlib.sha1(f"{kind}|{controller}|{tool}|{norm}".encode()).hexdigest()[:10]
        path = f"/lessons/{kind}-{re.sub(r'[^a-z0-9-]+', '-', (controller + '-' + tool).lower()).strip('-') or 'any'}-{key}.md"
        now = _now()
        old = self.store.get(namespace(USER), path)
        value = dict(old.value) if old is not None else {"created_at": now.isoformat(), "count": 0}
        value.update({"content": text, "kind": kind, "controller": controller, "tool": tool,
                      "count": int(value.get("count") or 0) + 1, "modified_at": now.isoformat(),
                      "expires_at": (now + dt.timedelta(days=self.ttl_days)).isoformat()})
        self.store.put(namespace(USER), path, value)
        return path

    def all(self) -> List[Dict[str, Any]]:
        now = _now()
        out = []
        for item in self.store.search(namespace(USER), limit=10_000):
            v = item.value
            try:
                if v.get("expires_at") and dt.datetime.fromisoformat(v["expires_at"]) < now:
                    continue
            except ValueError:
                pass
            content = v.get("content", "")
            out.append({"path": item.key, "text": content if isinstance(content, str) else "\n".join(content),
                        "kind": v.get("kind", ""), "controller": v.get("controller", ""), "tool": v.get("tool", ""),
                        "count": v.get("count", 1)})
        return out

    def relevant(self, text: str, names: Iterable[str] = (), limit: int = 3) -> List[Dict[str, Any]]:
        """The lessons that fit a request: by the controllers and tools it names, and the words they share."""
        want = _words(text) | {n.lower() for n in names if n}
        scored = []
        for les in self.all():
            score = len(want & _words(les["text"]))
            if les["controller"] and les["controller"].lower() in want:
                score += 3
            if les["tool"] and les["tool"].lower() in want:
                score += 3
            if score >= 2:
                scored.append((score + min(int(les["count"] or 1), 5) * 0.2, les))
        scored.sort(key=lambda x: -x[0])
        return [les for _, les in scored[:limit]]

    def prune(self) -> int:
        now, gone = _now(), 0
        for item in self.store.search(namespace(USER), limit=10_000):
            try:
                if item.value.get("expires_at") and dt.datetime.fromisoformat(item.value["expires_at"]) < now:
                    self.store.delete(namespace(USER), item.key)
                    gone += 1
            except ValueError:
                continue
        return gone


def note(lessons: List[Dict[str, Any]]) -> str:
    if not lessons:
        return ""
    return ("Lessons from earlier requests (kept by the harness from what went wrong; they may be out of date, and what a "
            "controller or the operator says now wins):\n" + "\n".join(f"- {x['text']}" for x in lessons))


class LessonsMiddleware:
    """Puts the lessons that fit the request in front of the model (supervisor and config agents)."""

    @staticmethod
    def make(get_lessons, names_of=lambda: ()):
        from langchain.agents.middleware import AgentMiddleware
        from langchain_core.messages import SystemMessage

        class _Lessons(AgentMiddleware):
            def _with(self, request):
                les = get_lessons()
                if les is None:
                    return request
                from .grounding import current_question
                msgs = list(getattr(request, "messages", []) or [])
                q = current_question(msgs) or ""
                if not q:
                    return request
                try:
                    found = les.relevant(q, names_of())
                except Exception:  # noqa: BLE001 - lessons are a help, never a failure
                    return request
                if not found:
                    return request
                sm = request.system_message
                extra = "\n\n" + note(found)
                if sm is None:
                    return request.override(system_message=SystemMessage(content=extra.strip()))
                if isinstance(sm.content, list):
                    return request.override(system_message=SystemMessage(content=[*sm.content, {"type": "text", "text": extra}]))
                return request.override(system_message=SystemMessage(content=str(sm.content) + extra))

            def wrap_model_call(self, request, handler):
                return handler(self._with(request))

            async def awrap_model_call(self, request, handler):
                return await handler(self._with(request))
        return _Lessons()
