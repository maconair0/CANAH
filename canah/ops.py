"""
What happened, kept: the audit log and per-request metrics, in CANAH_HOME/ops.sqlite.

  audit     every decision on a change (who, at what level, which thread, the tool and its
            arguments, approve / edit / reject, the message), every approved write's result,
            every change put up for approval, and every write to long-term memory
  requests  one row per request: user, thread, seconds, model calls, tokens in and out, tool
            calls, the checks that sent an answer back, how it ended

Read with `python -m canah audit` / `python -m canah metrics`, or GET /audit and /metrics
(admin). LangSmith tracing needs no code: set LANGSMITH_TRACING=true and LANGSMITH_API_KEY.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_SCHEMA = """
create table if not exists audit (
  id integer primary key, at real, event text, user text, level text, thread text,
  tool text, args text, decision text, message text, result text);
create table if not exists requests (
  id integer primary key, at real, user text, level text, thread text, seconds real,
  model_calls integer, tokens_in integer, tokens_out integer, tool_calls integer,
  sent_back text, ended text);
create index if not exists audit_at on audit(at);
create index if not exists requests_at on requests(at);
"""


class Ops:
    def __init__(self, path: Optional[Path] = None):
        self.path = str(path) if path else ":memory:"
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.executescript(_SCHEMA)
        have = {r[1] for r in self._db.execute("pragma table_info(requests)")}
        for col, kind in (("cache_prompt", "integer"), ("cache_hit", "integer"), ("context_used", "integer"),
                          ("context_window", "integer")):
            if col not in have:      # a database from before these figures: add them
                self._db.execute(f"alter table requests add column {col} {kind}")
        self._lock = threading.Lock()

    def audit(self, event: str, user: str = "", level: str = "", thread: str = "", tool: str = "",
              args: Any = None, decision: str = "", message: str = "", result: str = "") -> None:
        with self._lock:
            self._db.execute("insert into audit (at, event, user, level, thread, tool, args, decision, message, result)"
                             " values (?,?,?,?,?,?,?,?,?,?)",
                             (time.time(), event, user, level, thread, tool, json.dumps(args, default=str),
                              decision, message[:2000], str(result)[:4000]))

    def request(self, **row: Any) -> None:
        cols = ("user", "level", "thread", "seconds", "model_calls", "tokens_in", "tokens_out", "tool_calls",
                "sent_back", "ended", "cache_prompt", "cache_hit", "context_used", "context_window")
        vals = [row.get(c) for c in cols]
        vals[cols.index("sent_back")] = json.dumps(row.get("sent_back") or {})
        with self._lock:
            self._db.execute(f"insert into requests (at, {', '.join(cols)}) values (?{', ?' * len(cols)})",
                             (time.time(), *vals))

    def _rows(self, sql: str, args=()) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._db.execute(sql, args)
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]

    def audit_log(self, limit: int = 100, thread: str = "") -> List[Dict[str, Any]]:
        if thread:
            return self._rows("select * from audit where thread = ? order by id desc limit ?", (thread, limit))
        return self._rows("select * from audit order by id desc limit ?", (limit,))

    def pending_since(self, thread: str, tool: str) -> Optional[float]:
        """When the change now waiting on this thread was put up (the last 'proposed' after any decision)."""
        rows = self._rows("select at, event from audit where thread = ? and tool = ? order by id desc limit 1",
                          (thread, tool))
        return rows[0]["at"] if rows and rows[0]["event"] == "proposed" else None

    def metrics(self, since_s: float = 86400.0) -> Dict[str, Any]:
        rows = self._rows("select * from requests where at > ?", (time.time() - since_s,))
        if not rows:
            return {"requests": 0}
        secs = sorted(r["seconds"] or 0 for r in rows)
        sent: Dict[str, int] = {}
        for r in rows:
            for k, v in json.loads(r["sent_back"] or "{}").items():
                sent[k] = sent.get(k, 0) + int(v)
        return {"requests": len(rows), "median_seconds": round(secs[len(secs) // 2], 1),
                "p95_seconds": round(secs[int(len(secs) * 0.95) - 1 if len(secs) > 1 else 0], 1),
                "tokens_in": sum(r["tokens_in"] or 0 for r in rows), "tokens_out": sum(r["tokens_out"] or 0 for r in rows),
                "model_calls": sum(r["model_calls"] or 0 for r in rows), "tool_calls": sum(r["tool_calls"] or 0 for r in rows),
                "sent_back": sent, "ended": _count(r["ended"] for r in rows),
                **_cache_and_context(rows)}

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _cache_and_context(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The share of prompt tokens read from the KV cache, and how full requests left the context window."""
    out: Dict[str, Any] = {}
    p = sum(r.get("cache_prompt") or 0 for r in rows)
    if p:
        out["kv_cache_percent"] = round(100 * sum(r.get("cache_hit") or 0 for r in rows) / p, 1)
    pct = sorted(100 * r["context_used"] / r["context_window"] for r in rows
                 if r.get("context_used") and r.get("context_window"))
    if pct:
        out["context_percent_median"] = round(pct[len(pct) // 2], 1)
        out["context_percent_max"] = round(pct[-1], 1)
    return out


def _count(values) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return out
