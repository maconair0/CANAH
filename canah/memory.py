"""
Memory at two levels.

  per conversation   a checkpointer: each thread's messages and state, so a
                     conversation can continue (and a paused approval can be
                     resumed) after a restart. Keyed by `thread_id`.
  across them        a store: what the agent keeps under /memories/ (see
                     backends.py), shared by every thread of the same user.

Both are SQLite files under CANAH_HOME by default; `in_memory=True` gives
throw-away ones (tests, one-off scripts).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore


@dataclass
class Memory:
    checkpointer: BaseCheckpointSaver
    store: BaseStore
    where: str

    def close(self) -> None:
        for obj in (self.checkpointer, self.store):
            conn = getattr(obj, "conn", None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass


def open_memory(home: Optional[Path] = None, in_memory: bool = False) -> Memory:
    if in_memory or home is None:
        return Memory(InMemorySaver(), InMemoryStore(), "memory")
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.store.sqlite import SqliteStore
    home.mkdir(parents=True, exist_ok=True)
    threads = sqlite3.connect(str(home / "threads.sqlite"), check_same_thread=False)
    saver = SqliteSaver(threads)
    saver.setup()
    longterm = sqlite3.connect(str(home / "store.sqlite"), check_same_thread=False, isolation_level=None)
    store = SqliteStore(longterm)
    store.setup()
    return Memory(saver, store, str(home))


def namespace(user: str = "default") -> tuple:
    """Where one user's long-term memories live in the store."""
    return ("canah", "memories", user)


def uploads_namespace(user: str = "default") -> tuple:
    """Where one user's chat attachments live in the store (read as /uploads/<thread>/<file>)."""
    return ("canah", "uploads", user)


def user_of(runtime: Any) -> str:
    """The user a run is for: `configurable.user_id` of the running request, else "default".

    Read from the run's config: deepagents' Runtime does not carry it, and reading it there put every
    user's memories under "default" (one shared memory for everyone)."""
    try:
        from langgraph.config import get_config
        cfg = get_config() or {}
    except Exception:  # noqa: BLE001 - outside a run
        cfg = getattr(runtime, "config", None) or {}
    return str((cfg.get("configurable") or {}).get("user_id") or "default")


# ----- reviewing what the agent keeps (/memories/) -----

def list_memories(store: BaseStore, user: Optional[str] = None) -> list:
    """Every long-term memory (or one user's): user, path, size, when it changed, the start of it."""
    prefix = namespace(user) if user else ("canah", "memories")
    out = []
    for item in store.search(prefix, limit=10_000):
        content = item.value.get("content", "")
        text = content if isinstance(content, str) else "\n".join(content)
        out.append({"user": item.namespace[-1], "path": item.key, "chars": len(text),
                    "modified": str(item.updated_at), "preview": text[:200]})
    return out


def delete_memory(store: BaseStore, user: str, path: str) -> bool:
    if store.get(namespace(user), path) is None:
        return False
    store.delete(namespace(user), path)
    return True


def prune(store: BaseStore, older_than_days: float) -> int:
    """Delete memories not changed for `older_than_days`; how many went."""
    import datetime
    if not older_than_days:
        return 0
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=older_than_days)
    gone = 0
    for item in store.search(("canah", "memories"), limit=10_000):
        if item.updated_at < cutoff:
            store.delete(item.namespace, item.key)
            gone += 1
    return gone
