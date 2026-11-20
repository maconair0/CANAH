"""
The agent over HTTP.

  POST   /session                 {"username", "password"} -> {"token", "user", "level"}   (admin / root)
  GET    /session                 who this token is (observer without one)
  DELETE /session                 sign out
  POST   /threads                 a new thread -> {"thread_id"}
  POST   /threads/{id}/messages   {"text"} -> the reply (text, pending, tool_calls)
  POST   /threads/{id}/stream     {"text"} -> server-sent events: token, tool_call, tool_result, reply
  GET    /threads/{id}/pending    the changes the thread waits on
  POST   /threads/{id}/decide     {"decisions": [{"type": "approve"} | {"type": "reject", "message"} |
                                   {"type": "edit", "name", "args"}], "stream": false}
  GET    /info                    what the agent is built from
  GET    /audit, /metrics         the audit log; request metrics (admin)
  GET    /memories, DELETE /memories/{user}?path=   long-term memories (admin)
  GET    /documents, POST /documents {"name", "content_base64"}   the corpus; add one (admin)
  GET    /health
  GET    /                        a small web page

The token goes in "Authorization: Bearer <token>". Without one the caller is
an observer: they may ask anything and change nothing. Threads belong to the
user who made them: another user's thread id is not found. One run per thread
at a time; a second request to a busy thread gets 409.

  python -m canah serve [--host 127.0.0.1] [--port 8350]
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse

from .agent import CanahAgent, Reply
from .identity import OBSERVER, Accounts, Identity

logger = logging.getLogger("canah.service")

SESSION_TTL_S = float(os.getenv("CANAH_SESSION_TTL_S", "28800"))
SESSION_IDLE_S = float(os.getenv("CANAH_SESSION_IDLE_S", "7200"))
PAGE = Path(__file__).resolve().parent / "static" / "index.html"


class Sessions:
    """Signed-in sessions, kept in SQLite (as a hash of each token), so a restart signs no one out."""

    def __init__(self, path: Optional[str] = None):
        import sqlite3
        self._db = sqlite3.connect(path or ":memory:", check_same_thread=False, isolation_level=None)
        self._db.execute("create table if not exists sessions (token text primary key, user text, level text, "
                         "at real, used real)")
        self._lock = threading.Lock()

    @staticmethod
    def _key(token: str) -> str:
        import hashlib
        return hashlib.sha256(token.encode()).hexdigest()

    def open(self, who: Identity) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._db.execute("insert into sessions values (?,?,?,?,?)", (self._key(token), who.user, who.level, now, now))
        return token

    def who(self, token: str) -> Optional[Identity]:
        now = time.time()
        with self._lock:
            row = self._db.execute("select user, level, at, used from sessions where token = ?",
                                   (self._key(token),)).fetchone()
            if row is None:
                return None
            if now - row[2] > SESSION_TTL_S or now - row[3] > SESSION_IDLE_S:
                self._db.execute("delete from sessions where token = ?", (self._key(token),))
                return None
            self._db.execute("update sessions set used = ? where token = ?", (now, self._key(token)))
            return Identity(row[0], row[1])

    def close(self, token: str) -> bool:
        with self._lock:
            return self._db.execute("delete from sessions where token = ?", (self._key(token),)).rowcount > 0


def _reply_json(r: Reply) -> Dict[str, Any]:
    return {"thread_id": r.thread_id, "text": r.text, "pending": r.pending, "tool_calls": r.tool_calls}


def create_app(agent: CanahAgent, accounts: Optional[Accounts] = None) -> FastAPI:
    app = FastAPI(title="CANAH", version="1")
    accounts = accounts or Accounts()
    sessions = Sessions(str(agent.settings.home_path / "sessions.sqlite"))
    busy: Dict[str, threading.Lock] = {}
    busy_guard = threading.Lock()

    def caller(authorization: str = Header(default="")) -> Dict[str, Any]:
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        if not token:
            return {"who": OBSERVER, "token": ""}
        who = sessions.who(token)
        if who is None:
            raise HTTPException(401, "this session has ended or never existed; sign in again")
        return {"who": who, "token": token}

    def thread_key(who: Identity, thread_id: str) -> str:
        # A user's threads are their own: the stored id carries the user's name.
        if not thread_id or len(thread_id) > 80 or not all(c.isalnum() or c in "-_" for c in thread_id):
            raise HTTPException(400, "a thread id is letters, digits, - and _ (at most 80)")
        return f"{who.user}.{thread_id}"

    def lock_for(key: str) -> threading.Lock:
        with busy_guard:
            return busy.setdefault(key, threading.Lock())

    def run(key: str, fn):
        lock = lock_for(key)
        if not lock.acquire(blocking=False):
            raise HTTPException(409, "this thread is already running a request")
        try:
            return fn()
        except PermissionError as e:
            raise HTTPException(403, str(e))
        except (RuntimeError, ValueError) as e:
            raise HTTPException(409 if "waiting" in str(e) else 400, str(e))
        finally:
            lock.release()

    def sse(key: str, events: Iterator[Dict[str, Any]], public_id: str) -> StreamingResponse:
        lock = lock_for(key)
        if not lock.acquire(blocking=False):
            raise HTTPException(409, "this thread is already running a request")
        first: List[Dict[str, Any]] = []
        try:
            first.append(next(events))            # errors before any output become HTTP errors
        except StopIteration:
            pass
        except PermissionError as e:
            lock.release()
            raise HTTPException(403, str(e))
        except (RuntimeError, ValueError) as e:
            lock.release()
            raise HTTPException(409 if "waiting" in str(e) else 400, str(e))

        def body():
            try:
                for ev in [*first, *events]:
                    if ev.get("type") == "reply":
                        ev = {**ev, "thread_id": public_id}
                    yield f"event: {ev['type']}\ndata: {json.dumps(ev, default=str)}\n\n"
            except Exception as e:  # noqa: BLE001 - said on the stream, not swallowed
                logger.exception("stream failed")
                yield f"event: error\ndata: {json.dumps({'error': f'{type(e).__name__}: {e}'})}\n\n"
            finally:
                lock.release()
        return StreamingResponse(body(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/health")
    def health():
        return {"ok": True, "agent": agent.settings.name}

    @app.get("/info")
    def info():
        keep = ("model", "tools", "main_agent_tools", "subagents", "skills", "memory", "sandbox", "interrupt_on",
                "middleware")
        return {k: agent.info.get(k) for k in keep}

    @app.post("/session")
    def sign_in(body: Dict[str, Any] = Body(...)):
        try:
            who = accounts.sign_in(str(body.get("username") or ""), str(body.get("password") or ""))
        except PermissionError as e:
            raise HTTPException(401, str(e))
        return {"token": sessions.open(who), "user": who.user, "level": who.level}

    @app.get("/session")
    def whoami(c=Depends(caller)):
        return {"user": c["who"].user, "level": c["who"].level, "signed_in": bool(c["token"])}

    @app.delete("/session")
    def sign_out(c=Depends(caller)):
        return {"signed_out": sessions.close(c["token"]) if c["token"] else False}

    @app.post("/threads")
    def new_thread(c=Depends(caller)):
        return {"thread_id": secrets.token_hex(6)}

    @app.post("/threads/{thread_id}/messages")
    def message(thread_id: str, body: Dict[str, Any] = Body(...), c=Depends(caller)):
        key = thread_key(c["who"], thread_id)
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "say something")
        r = run(key, lambda: agent.ask(text, key, c["who"]))
        return {**_reply_json(r), "thread_id": thread_id}

    @app.post("/threads/{thread_id}/stream")
    def stream(thread_id: str, body: Dict[str, Any] = Body(...), c=Depends(caller)):
        key = thread_key(c["who"], thread_id)
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "say something")
        return sse(key, agent.stream(text, key, c["who"]), thread_id)

    @app.get("/threads/{thread_id}/pending")
    def pending(thread_id: str, c=Depends(caller)):
        key = thread_key(c["who"], thread_id)
        out = agent.pending(key, c["who"])
        for p in out:
            p["needs"] = agent.needs(p["name"])
            p["you_may_approve"] = c["who"].can(p["needs"])
        return {"thread_id": thread_id, "pending": out}

    @app.post("/threads/{thread_id}/decide")
    def decide(thread_id: str, body: Dict[str, Any] = Body(...), c=Depends(caller)):
        key = thread_key(c["who"], thread_id)
        decisions = body.get("decisions")
        if not isinstance(decisions, list) or not decisions:
            raise HTTPException(400, "decisions: a list, one per pending change")
        if body.get("stream"):
            return sse(key, agent.stream(None, key, c["who"], decisions=decisions), thread_id)
        r = run(key, lambda: agent.decide(key, decisions, c["who"]))
        return {**_reply_json(r), "thread_id": thread_id}

    def admin(c) -> Identity:
        if not c["who"].can("admin"):
            raise HTTPException(403, "this needs admin")
        return c["who"]

    @app.get("/audit")
    def audit(limit: int = 100, thread: str = "", c=Depends(caller)):
        admin(c)
        return {"audit": agent.ops.audit_log(min(int(limit), 1000), thread) if agent.ops else []}

    @app.get("/metrics")
    def metrics(hours: float = 24, c=Depends(caller)):
        admin(c)
        return agent.ops.metrics(float(hours) * 3600) if agent.ops else {"requests": 0}

    @app.get("/memories")
    def memories(user: str = "", c=Depends(caller)):
        admin(c)
        from .memory import list_memories
        return {"memories": list_memories(agent.memory.store, user or None)}

    @app.delete("/memories/{user}")
    def forget(user: str, path: str, c=Depends(caller)):
        who = admin(c)
        from .memory import delete_memory
        gone = delete_memory(agent.memory.store, user, path)
        if agent.ops:
            agent.ops.audit("memory deleted", who.user, who.level, "", "memory", {"user": user, "path": path})
        return {"deleted": gone}

    @app.get("/documents")
    def documents(c=Depends(caller)):
        from .knowledge.rag.settings import CORPUS_DIR
        names = sorted(os.listdir(CORPUS_DIR)) if os.path.isdir(CORPUS_DIR) else []
        return {"folder": CORPUS_DIR, "documents": names}

    @app.post("/documents")
    def add_document(body: Dict[str, Any] = Body(...), c=Depends(caller)):
        """{"name": "rules.md", "content_base64": "..."}: saved into the corpus and indexed."""
        import base64
        import tempfile
        who = admin(c)
        name = os.path.basename(str(body.get("name") or ""))
        if not name or name.startswith("."):
            raise HTTPException(400, "a document needs a file name")
        try:
            data = base64.b64decode(str(body.get("content_base64") or ""), validate=True)
        except ValueError:
            raise HTTPException(400, "content_base64 is not base64")
        if len(data) > 50 * 1024 * 1024:
            raise HTTPException(413, "a document is at most 50 MB")
        from .knowledge import add_documents
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, name)
            with open(path, "wb") as fh:
                fh.write(data)
            got = add_documents([path], agent.settings.knowledge_collection)
        if agent.ops:
            agent.ops.audit("document added", who.user, who.level, "", "documents", {"name": name, "bytes": len(data)})
        return {"added": name, "index": {k: got.get(k) for k in ("added", "changed", "failed", "collection_size")}}

    @app.get("/", response_class=HTMLResponse)
    def page():
        return PAGE.read_text() if PAGE.is_file() else "<p>CANAH</p>"

    return app


def serve(host: str = "127.0.0.1", port: int = 8350, config: Optional[str] = None, sandbox: bool = True,
          tls_cert: str = "", tls_key: str = "") -> None:
    import uvicorn

    from .agent import build_agent
    from .config import load
    settings = load(config)
    if not sandbox:
        settings.sandbox = "none"
    if os.getenv("CANAH_TESTBED") == "1":
        # ./start.sh --testbed: the emulated domains' own context and skills, beside the harness's.
        tb = Path(__file__).resolve().parent.parent / "testbed"
        settings.memory_files = [*settings.memory_files, str(tb / "AGENTS.md")]
        settings.skills_dirs = [*settings.skills_dirs, str(tb / "skills")]
    agent = build_agent(settings)
    try:
        tls = {"ssl_certfile": tls_cert, "ssl_keyfile": tls_key} if tls_cert and tls_key else {}
        if not tls and host not in ("127.0.0.1", "localhost", "::1"):
            logger.warning("serving on %s without TLS: passwords and tokens cross the network in the clear "
                           "(use --tls-cert and --tls-key)", host)
        uvicorn.run(create_app(agent), host=host, port=port, log_level="info", **tls)
    finally:
        agent.close()
