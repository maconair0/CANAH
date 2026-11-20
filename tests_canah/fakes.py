"""A scripted chat model: answers each turn with the next message of a script,
and records what it was shown, so a test can check prompts and tool results."""
from __future__ import annotations

import itertools
from typing import Any, Dict, List, Optional, Sequence

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

_ids = itertools.count(1)


def call(name: str, /, **args: Any) -> AIMessage:
    """A turn that calls one tool."""
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{next(_ids)}",
                                              "type": "tool_call"}])


def say(text: str) -> AIMessage:
    return AIMessage(content=text)


class ScriptedModel(BaseChatModel):
    script: List[AIMessage] = Field(default_factory=list)
    seen: List[List[BaseMessage]] = Field(default_factory=list)
    tools_bound: List[List[str]] = Field(default_factory=list)
    offered: List[List[str]] = Field(default_factory=list)   # tool names on each call, after middleware

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any):  # noqa: D401 - the agent binds; we record
        names = []
        for t in tools:
            n = getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None) or \
                (t.get("function", {}).get("name") if isinstance(t, dict) else None)
            names.append(n)
        self.tools_bound.append(names)
        self._last_bound = names
        return self

    def _generate(self, messages: List[BaseMessage], stop: Optional[List[str]] = None,
                  run_manager: Optional[CallbackManagerForLLMRun] = None, **kwargs: Any) -> ChatResult:
        self.seen.append(list(messages))
        self.offered.append(list(getattr(self, "_last_bound", [])))
        if not self.script:
            raise AssertionError("the scripted model ran out of turns")
        msg = self.script.pop(0)
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def system_text(self, turn: int = -1) -> str:
        msgs = self.seen[turn]
        return "\n".join(str(m.content) for m in msgs if m.type == "system")

    def tool_results(self, turn: int = -1) -> List[str]:
        return [str(m.content) for m in self.seen[turn] if m.type == "tool"]
