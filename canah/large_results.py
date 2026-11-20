"""
A tool result too large to read whole is kept whole, and the model is told so.

A clipped result read as a whole one gives wrong counts and lists: the model
counts what it can see. So: a result over `limit` characters
is saved in full, and the model gets the beginning, how much it is not seeing,
where the rest is, and that anything counted or listed from the preview is
incomplete.

  with the sandbox   /workspace/.results/<id>.txt inside it: read_file and grep
                     reach it, and execute can process it (jq, python3)
  without            /results/<id>.txt, a folder under CANAH_HOME mounted for
                     the file tools (backends.py)

Applies to the main agent and every subagent; the file tools' own reads are
left alone (they page already).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable, Optional

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

logger = logging.getLogger("canah.large_results")

SKIP = {"read_file", "ls", "glob", "grep", "write_file", "edit_file", "delete", "task", "write_todos"}


def shape(text: str, max_fields: int = 12, max_values: int = 12) -> str:
    """For a JSON result: the fields that take few distinct values, with how often each occurs
    ("kind: A 14, B 9, C 7"). The labels a model would otherwise guess."""
    import json
    from collections import Counter, defaultdict
    try:
        data = json.loads(text)
    except ValueError:
        return ""
    seen: dict = defaultdict(Counter)

    def walk(o, depth=0):
        if depth > 40:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, (str, bool)) and len(str(v)) <= 60:
                    seen[str(k)][str(v)] += 1
                else:
                    walk(v, depth + 1)
        elif isinstance(o, list):
            for v in o:
                walk(v, depth + 1)
    walk(data)
    def numeric(v: str) -> bool:
        try:
            float(v)
            return True
        except ValueError:
            return False
    # Labels, not measurements: a field whose values are all numbers says nothing about kinds of thing.
    fields = [(k, c) for k, c in seen.items() if 2 <= len(c) <= max_values and sum(c.values()) >= 3
              and not all(numeric(v) for v in c) and max(c.values()) > 1]   # all-once values are ids
    fields.sort(key=lambda kc: -sum(kc[1].values()))
    lines = [f"{k.split(':')[-1]}: " + ", ".join(f"{v} {n}" for v, n in c.most_common(max_values))
             for k, c in fields[:max_fields]]
    return ("Fields with few distinct values in the full result (value count):\n  " + "\n  ".join(lines)) if lines else ""


class LargeResults(AgentMiddleware):
    def __init__(self, limit: int = 12000, preview: int = 2500, sandbox: Any = None, folder: Optional[Path] = None):
        super().__init__()
        self.limit, self.preview = limit, preview
        self.sandbox, self.folder = sandbox, folder

    def _keep(self, call_id: str, text: str) -> Optional[str]:
        name = re.sub(r"[^\w.-]", "_", call_id)[:80] + ".txt"
        try:
            if self.sandbox is not None:
                got = self.sandbox.upload_files([(f"/workspace/.results/{name}", text.encode())])[0]
                if got.error is None:
                    return f"/workspace/.results/{name}"
            if self.folder is not None:
                self.folder.mkdir(parents=True, exist_ok=True)
                (self.folder / name).write_text(text)
                return f"/results/{name}"
        except Exception as e:  # noqa: BLE001 - a result not kept is said, not hidden
            logger.warning("large result not kept: %s", e)
        return None

    def _shrink(self, request, result):
        if not isinstance(result, ToolMessage) or request.tool_call.get("name") in SKIP:
            return result
        text = result.content if isinstance(result.content, str) else str(result.content)
        if len(text) <= self.limit:
            return result
        path = self._keep(str(request.tool_call.get("id") or "result"), text)
        where = (f"The full result is in {path}: read it with read_file or grep"
                 + (", or process it in the sandbox with execute (jq, python3)" if path and path.startswith("/workspace")
                    else "") + "." if path else "The full result could not be kept; ask the tool for less.")
        note = (f"\n\n[Only the first {self.preview} of {len(text)} characters are shown. {where} "
                f"Anything you count or list from this preview is incomplete; work from the full result. Look at "
                f"its structure first (its keys, and the distinct values of the field you mean to filter on) "
                f"rather than guessing names or labels. A name that is not in the preview may still be in the "
                f"full result: search it before saying something is missing.]")
        summary = shape(text)
        if summary:
            note += "\n" + summary
        return result.model_copy(update={"content": text[:self.preview] + note})

    def wrap_tool_call(self, request, handler: Callable):
        return self._shrink(request, handler(request))

    async def awrap_tool_call(self, request, handler: Callable):
        return self._shrink(request, await handler(request))
