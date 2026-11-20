"""
The agent over HTTP.

  POST   /session                 {"username", "password"} -> {"token", "user", "level"}   (admin / root)
  GET    /session                 who this token is (observer without one)
  DELETE /session                 sign out
  POST   /threads                 a new thread -> {"thread_id"}
  POST   /threads/{id}/messages   {"text"} -> the reply (text, pending, tool_calls)
  POST   /threads/{id}/stream     {"text"} -> server-sent events: token, tool_call, tool_result, reply
  GET    /threads/{id}/pending    the changes the thread waits on
  GET    /threads/{id}/history    the conversation so far: what was asked and answered (the checks' own
                                  prompts to the model left out)
  POST   /threads/{id}/decide     {"decisions": [{"type": "approve"} | {"type": "reject", "message"} |
                                   {"type": "edit", "name", "args"}], "stream": false}
  GET    /info                    what the agent is built from
  GET    /audit, /metrics         the audit log; request metrics (admin)
  GET    /memories, DELETE /memories/{user}?path=   long-term memories (admin)
  GET    /documents, POST /documents {"name", "content_base64"}   the corpus; add one (admin)
  POST   /threads/{id}/files      {"name", "content_base64"}: a file attached to the conversation, kept per user;
                                  the agent reads it as /uploads/<thread>/<name> (text extracted from PDFs and
                                  documents) and, with a sandbox, as /workspace/uploads/<thread>/<name>
  GET    /activity                what is running now: requests, the subagents at work, the last steps
  GET    /knowledge/mesh[?rebuild=1]  the documents as a graph: one node per document, edges by meaning
  GET    /network                 the controllers the agentic MCP holds: domain, contract coverage, health
  GET    /network/kinds           the controller kinds the agentic MCP has manifests for
  POST   /network/controllers     onboard one (admin): {key, url, label, kind, spec, user_env, password_env}
  DELETE /network/controllers/{key}   remove one (root)
  POST   /network/manifests, /network/guides   a manifest, or a user guide drafted into one (admin)
  POST   /admin/reload            rebuild the agent from the settings, so new controllers get subagents (admin)
  GET    /twins                   the digital twins configured, which one is in use, what each covers
  PUT    /twins/active            {"name"}: use this twin (admin); "none" for no twin
  POST   /twins                   {"name", "kind": "http", "url", "token_env"} or {"name", "kind": "python",
                                  "spec", "path"}: add a twin (admin)
  DELETE /twins/{name}            remove an added twin (root)
  POST   /twins/{name}/test       describe it now: is it there, does it answer in the contract
  GET    /topology[?refresh=1]    each controller's devices and links (read through the contract), relations
  GET    /health
  GET    /                        the web console (static/: index.html, app.css, app.js, logos, fonts)

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
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .agent import CanahAgent, Reply
from .identity import OBSERVER, Accounts, Identity

logger = logging.getLogger("canah.service")

SESSION_TTL_S = float(os.getenv("CANAH_SESSION_TTL_S", "28800"))
SESSION_IDLE_S = float(os.getenv("CANAH_SESSION_IDLE_S", "7200"))
STATIC = Path(__file__).resolve().parent / "static"
PAGE = STATIC / "index.html"


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


def _as_text(name: str, data: bytes):
    """(text, how) for an attachment: as it is, read by the knowledge loaders, or (None, why) for bytes."""
    low = name.lower()
    if b"\x00" not in data[:4096]:
        try:
            return data.decode("utf-8"), "text"
        except UnicodeDecodeError:
            pass
    import tempfile
    try:
        from .knowledge.rag.loaders import is_supported, load_file
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, os.path.basename(name))
            with open(path, "wb") as fh:
                fh.write(data)
            if is_supported(path):
                text, _meta = load_file(path)
                if text and text.strip():
                    return text, "extracted text"
    except Exception:  # noqa: BLE001 - kept as bytes instead
        logger.warning("could not read %s as text", name, exc_info=True)
    return None, "bytes (base64): an image or a binary the model cannot read as text"


class Activity:
    """What is running now, for the console: each request's subagents at work and its last steps.
    A subagent is at work while its `task` runs, or while one of its controller tools (<key>__<tool>) runs."""

    def __init__(self, keep: int = 60):
        self._lock = threading.Lock()
        self.running: Dict[str, Dict[str, Any]] = {}
        self.recent: List[Dict[str, Any]] = []
        self.used: Dict[str, Dict[str, Any]] = {}
        self.keep = keep

    def watch(self, key: str, events: Iterator[Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
        user = key.split(".", 1)[0]
        with self._lock:
            self.running[key] = {"user": user, "since": time.time(), "agents": {"main"}, "step": ""}
        try:
            for ev in events:
                self._see(key, ev)
                yield ev
        finally:
            with self._lock:
                self.running.pop(key, None)

    def _see(self, key: str, ev: Dict[str, Any]) -> None:
        if ev.get("type") not in ("tool_call", "tool_result"):
            return
        name = str(ev.get("name") or "")
        who = "main"
        if name == "task" and isinstance(ev.get("args"), dict):
            who = str(ev["args"].get("subagent_type") or "general-purpose")
        elif "__" in name:
            who = name.split("__", 1)[0]
        now = time.time()
        with self._lock:
            run = self.running.get(key)
            if run is not None:
                if ev["type"] == "tool_call":
                    run["agents"].add(who)
                    run["step"] = name
                elif name == "task":
                    run["agents"].discard(who) if who != "main" else None
            u = self.used.setdefault(who, {"calls": 0, "last": 0.0})
            if ev["type"] == "tool_call":
                u["calls"] += 1
                u["last"] = now
                self.recent.append({"at": now, "agent": who, "tool": name, "user": key.split(".", 1)[0]})
                del self.recent[:-self.keep]

    def snapshot(self, show_users: bool = False) -> Dict[str, Any]:
        with self._lock:
            runs = [{"user": r["user"] if show_users else "", "since": r["since"], "agents": sorted(r["agents"]),
                     "step": r["step"]} for r in self.running.values()]
            active = sorted({a for r in self.running.values() for a in r["agents"]})
            recent = [{**e, "user": e["user"] if show_users else ""} for e in self.recent[-25:]]
            return {"at": time.time(), "busy": bool(runs), "running": runs, "active": active,
                    "used": dict(self.used), "recent": recent}


def _status(e: Exception) -> tuple:
    """A failed request's HTTP status: waiting on a decision is a conflict, bad input is the caller's, and a
    model or tool that fell over is upstream's, not a bad request."""
    if "waiting" in str(e):
        return 409, str(e)
    if isinstance(e, ValueError):
        return 400, str(e)
    return 502, f"the model or a tool failed: {e}"


def _reply_json(r: Reply) -> Dict[str, Any]:
    return {"thread_id": r.thread_id, "text": r.text, "pending": r.pending, "tool_calls": r.tool_calls}


def create_app(agent: CanahAgent, accounts: Optional[Accounts] = None, topology: Any = None,
               rebuild: Optional[Any] = None) -> FastAPI:
    """`topology`: what /network and /topology read (a canah.topology.Topology); by default one over the
    agentic MCP the settings name. `rebuild`: makes a new agent from the settings, for /admin/reload (a
    controller onboarded since the start gets its subagent); without it, reload is refused."""
    ref = {"agent": agent}

    def A() -> CanahAgent:
        return ref["agent"]

    app = FastAPI(title="CANAH", version="1")
    accounts = accounts or Accounts()
    sessions = Sessions(str(A().settings.home_path / "sessions.sqlite"))
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
            raise HTTPException(*_status(e))
        finally:
            lock.release()

    activity = Activity()

    def sse(key: str, events: Iterator[Dict[str, Any]], public_id: str) -> StreamingResponse:
        events = activity.watch(key, events)
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
            raise HTTPException(*_status(e))

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
        return {"ok": True, "agent": A().settings.name}

    @app.get("/info")
    def info():
        keep = ("model", "tools", "main_agent_tools", "subagents", "subagent_detail", "skills", "memory", "sandbox",
                "interrupt_on", "middleware")
        return {k: A().info.get(k) for k in keep}

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
        r = run(key, lambda: A().ask(text, key, c["who"]))
        return {**_reply_json(r), "thread_id": thread_id}

    @app.post("/threads/{thread_id}/stream")
    def stream(thread_id: str, body: Dict[str, Any] = Body(...), c=Depends(caller)):
        key = thread_key(c["who"], thread_id)
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "say something")
        return sse(key, A().stream(text, key, c["who"]), thread_id)

    @app.get("/threads/{thread_id}/history")
    def history(thread_id: str, c=Depends(caller)):
        key = thread_key(c["who"], thread_id)
        try:
            msgs = A().graph.get_state(A().config(key, c["who"])).values.get("messages", [])
        except Exception:  # noqa: BLE001 - an unknown thread has no history
            msgs = []
        # In order, as the console draws them: what was asked, each tool the agent called (with its
        # arguments and what came back), and what it answered.
        results = {getattr(m, "tool_call_id", ""): m for m in msgs if m.type == "tool"}
        out = []
        for m in msgs:
            text = m.content if isinstance(m.content, str) else " ".join(
                str(x.get("text", "")) for x in (m.content or []) if isinstance(x, dict))
            if m.type == "human":
                if text.startswith("[") and "check]" in text[:40]:
                    continue                      # a check's prompt to the model, not the user's words
                out.append({"role": "user", "text": text})
            elif m.type == "ai":
                if text.strip():
                    out.append({"role": "agent", "text": text})
                for t in (getattr(m, "tool_calls", None) or []):
                    r = results.get(t.get("id"))
                    out.append({"role": "tool", "name": t.get("name"), "args": t.get("args") or {},
                                "result": (str(r.content)[:4000] if r is not None else None)})
        return {"thread_id": thread_id, "messages": out[-200:]}

    @app.get("/threads/{thread_id}/pending")
    def pending(thread_id: str, c=Depends(caller)):
        key = thread_key(c["who"], thread_id)
        out = A().pending(key, c["who"])
        for p in out:
            p["needs"] = A().needs(p["name"])
            p["you_may_approve"] = c["who"].can(p["needs"])
        return {"thread_id": thread_id, "pending": out}

    @app.post("/threads/{thread_id}/decide")
    def decide(thread_id: str, body: Dict[str, Any] = Body(...), c=Depends(caller)):
        key = thread_key(c["who"], thread_id)
        decisions = body.get("decisions")
        if not isinstance(decisions, list) or not decisions:
            raise HTTPException(400, "decisions: a list, one per pending change")
        # An edit as documented ({"type": "edit", "name", "args"}) is what the agent reads as an edited action.
        from . import approval
        norm = []
        for d in decisions:
            if not isinstance(d, dict) or d.get("type") not in approval.DECISIONS:
                raise HTTPException(400, f"a decision is one of {approval.DECISIONS}")
            if d["type"] == "edit" and "edited_action" not in d:
                if not d.get("name") or not isinstance(d.get("args"), dict):
                    raise HTTPException(400, "an edit gives the change's name and its new args (an object)")
                d = approval.decision("edit", name=d["name"], args=d["args"])
            norm.append(d)
        decisions = norm
        if body.get("stream"):
            return sse(key, A().stream(None, key, c["who"], decisions=decisions), thread_id)
        r = run(key, lambda: A().decide(key, decisions, c["who"]))
        return {**_reply_json(r), "thread_id": thread_id}

    def admin(c) -> Identity:
        if not c["who"].can("admin"):
            raise HTTPException(403, "this needs admin")
        return c["who"]

    @app.get("/audit")
    def audit(limit: int = 100, thread: str = "", c=Depends(caller)):
        admin(c)
        return {"audit": A().ops.audit_log(min(int(limit), 1000), thread) if A().ops else []}

    @app.get("/metrics")
    def metrics(hours: float = 24, c=Depends(caller)):
        admin(c)
        return A().ops.metrics(float(hours) * 3600) if A().ops else {"requests": 0}

    @app.get("/memories")
    def memories(user: str = "", c=Depends(caller)):
        admin(c)
        from .memory import list_memories
        return {"memories": list_memories(A().memory.store, user or None)}

    @app.delete("/memories/{user}")
    def forget(user: str, path: str, c=Depends(caller)):
        who = admin(c)
        from .memory import delete_memory
        gone = delete_memory(A().memory.store, user, path)
        if A().ops:
            A().ops.audit("memory deleted", who.user, who.level, "", "memory", {"user": user, "path": path})
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
            got = add_documents([path], A().settings.knowledge_collection)
        if A().ops:
            A().ops.audit("document added", who.user, who.level, "", "documents", {"name": name, "bytes": len(data)})
        return {"added": name, "index": {k: got.get(k) for k in ("added", "changed", "failed", "collection_size")}}

    from .topology import Topology
    topology = topology or Topology()

    @app.get("/network")
    def network(c=Depends(caller)):
        """The controllers and whether each answers: what the console's status strip and controller page show."""
        try:
            rows = topology.service.controllers()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "url": topology.service.url, "error": f"{type(e).__name__}: {e}", "controllers": []}
        try:
            sessions_ = topology.service.get("/sessions", timeout=8)
        except Exception:  # noqa: BLE001 - health is extra
            sessions_ = {}
        return {"ok": True, "url": topology.service.url, "controllers": rows, "sessions": sessions_}

    @app.get("/topology")
    def topo(refresh: int = 0, c=Depends(caller)):
        return topology.read(bool(refresh))


    # ----- attachments -----

    @app.post("/threads/{thread_id}/files")
    def attach(thread_id: str, body: Dict[str, Any] = Body(...), c=Depends(caller)):
        """A file for the agent to read in this conversation. Text is kept as it is; a PDF, a Word or
        PowerPoint file is read to text (the knowledge loaders); anything else is kept as bytes."""
        import base64
        key = thread_key(c["who"], thread_id)
        name = os.path.basename(str(body.get("name") or "")).replace("/", "_")
        if not name or name.startswith("."):
            raise HTTPException(400, "a file needs a name")
        try:
            data = base64.b64decode(str(body.get("content_base64") or ""), validate=True)
        except ValueError:
            raise HTTPException(400, "content_base64 is not base64")
        if len(data) > 25 * 1024 * 1024:
            raise HTTPException(413, "an attachment is at most 25 MB")
        text, how = _as_text(name, data)
        from .memory import uploads_namespace
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        folder = f"/{thread_id}/"
        store = A().memory.store
        stored = name if text is not None else name + ".b64"
        content, enc = (text, "utf-8") if text is not None else (base64.b64encode(data).decode("ascii"), "base64")
        store.put(uploads_namespace(c["who"].user), folder + stored,
                  {"content": content, "encoding": enc, "created_at": now, "modified_at": now})
        in_sandbox = ""
        if A().sandbox is not None:
            try:
                got = A().sandbox.upload_files([(f"/workspace/uploads/{thread_id}/{name}", data)])
                if got and not getattr(got[0], "error", None):
                    in_sandbox = f"/workspace/uploads/{thread_id}/{name}"
            except Exception:  # noqa: BLE001 - the store copy is enough to read it
                logger.warning("could not copy %s into the sandbox", name, exc_info=True)
        if A().ops:
            A().ops.audit("file attached", c["who"].user, c["who"].level, key, "attach", {"name": name, "bytes": len(data)})
        return {"name": name, "bytes": len(data), "path": f"/uploads{folder}{stored}", "sandbox_path": in_sandbox,
                "text_chars": len(text) if text is not None else 0, "read_as": how,
                "preview": (text or "")[:6000]}

    # ----- what is running -----

    @app.get("/activity")
    def running(c=Depends(caller)):
        return activity.snapshot(show_users=c["who"].can("admin"))

    # ----- the documents as a graph -----

    @app.get("/knowledge/mesh")
    def knowledge_mesh(rebuild: int = 0, c=Depends(caller)):
        try:
            from .knowledge.rag import mesh as _mesh
            from .knowledge.rag.store import RagStore
            store = RagStore(collection=A().settings.knowledge_collection)
            got = _mesh.get(store, rebuild=bool(rebuild))
            return {"available": True, **got}
        except Exception as e:  # noqa: BLE001 - said on the page
            return {"available": False, "reason": f"{type(e).__name__}: {e}"}

    # ----- onboarding controllers, through the agentic MCP -----

    def mcp_send(method: str, path: str, *, json_body: Any = None, data: bytes = b"", params: Any = None,
                 content_type: str = "", timeout: float = 180.0) -> Any:
        svc = topology.service
        headers = dict(svc._headers())
        if content_type:
            headers["Content-Type"] = content_type
        try:
            r = svc.http.request(method, svc.url + path, json=json_body, data=data or None, params=params,
                                 headers=headers, timeout=timeout)
        except Exception as e:  # noqa: BLE001 - the agentic MCP is down or unreachable: say so, not 500
            raise HTTPException(502, f"the agentic MCP at {svc.url} did not answer: {type(e).__name__}: {str(e)[:200]}")
        if r.status_code >= 400:
            raise HTTPException(r.status_code if r.status_code in (400, 401, 403, 404, 409, 422) else 502,
                                f"agentic MCP {path}: {r.text[:400]}")
        try:
            return r.json()
        except ValueError:
            return {"text": r.text}

    def changed(who: Identity, what: str, args: Dict[str, Any]) -> None:
        if A().ops:
            A().ops.audit(what, who.user, who.level, "", "agentic-mcp", args)
        topology.read(refresh=True)

    @app.get("/network/kinds")
    def kinds(c=Depends(caller)):
        try:
            return mcp_send("GET", "/manifests", timeout=20)
        except HTTPException as e:
            return {"kinds": [], "error": e.detail}

    @app.post("/network/controllers")
    def onboard(body: Dict[str, Any] = Body(...), c=Depends(caller)):
        who = admin(c)
        k = str(body.get("key") or "").strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", k):
            raise HTTPException(400, "a key is 2-32 lower-case letters, digits or _, starting with a letter")
        if not str(body.get("url") or "").startswith(("http://", "https://")):
            raise HTTPException(400, "the controller's url starts with http:// or https://")
        sent = {f: str(body.get(f) or "").strip() for f in ("key", "url", "label", "kind", "spec", "user_env", "password_env")}
        for f in ("user_env", "password_env"):
            if sent[f] and not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", sent[f]):
                raise HTTPException(400, f"{f} is the NAME of an environment variable on the agentic MCP's host "
                                         f"(like ONOS_USER), never the secret itself")
        got = mcp_send("POST", "/controllers", json_body={k2: v for k2, v in sent.items() if v})
        changed(who, "controller onboarded", sent)
        return {"onboarded": got, "reload_needed": True}

    @app.delete("/network/controllers/{key}")
    def remove_controller(key: str, c=Depends(caller)):
        if not c["who"].can("root"):
            raise HTTPException(403, "removing a controller needs root")
        got = mcp_send("DELETE", f"/controllers/{key}", timeout=30)
        changed(c["who"], "controller removed", {"key": key})
        return {"removed": got, "reload_needed": True}

    @app.post("/network/manifests")
    def add_manifest(body: Dict[str, Any] = Body(...), c=Depends(caller)):
        import base64
        who = admin(c)
        data = base64.b64decode(str(body.get("content_base64") or ""))
        name = os.path.basename(str(body.get("name") or "manifest.yaml"))
        got = mcp_send("POST", "/manifests", data=data, params={"name": name}, content_type="application/yaml")
        changed(who, "manifest installed", {"name": name})
        return got

    @app.post("/network/guides")
    def add_guide(body: Dict[str, Any] = Body(...), c=Depends(caller)):
        import base64
        who = admin(c)
        kind = str(body.get("kind") or "").strip()
        if not kind:
            raise HTTPException(400, "a guide is for a kind of controller: say which")
        data = base64.b64decode(str(body.get("content_base64") or ""))
        name = os.path.basename(str(body.get("name") or "guide"))
        got = mcp_send("POST", "/guides", data=data, params={"kind": kind, "title": body.get("title") or "", "name": name},
                       content_type="application/octet-stream", timeout=300)
        changed(who, "guide installed", {"kind": kind, "name": name})
        return got

    # ----- digital twins -----

    def twins_or_409():
        reg = A().twins
        if reg is None:
            raise HTTPException(409, "this agent was built without twins")
        return reg

    @app.get("/twins")
    def twins(c=Depends(caller)):
        reg = twins_or_409()
        from .twins.contract import CONTRACT
        return {"contract": CONTRACT, "active": reg.active_name(), "twins": reg.status()}

    @app.put("/twins/active")
    def use_twin(body: Dict[str, Any] = Body(...), c=Depends(caller)):
        who = admin(c)
        reg = twins_or_409()
        try:
            reg.set_active(str(body.get("name") or ""))
        except KeyError as e:
            raise HTTPException(404, str(e))
        if A().ops:
            A().ops.audit("twin chosen", who.user, who.level, "", "twins", {"name": body.get("name")})
        return {"active": reg.active_name()}

    @app.post("/twins")
    def add_twin(body: Dict[str, Any] = Body(...), c=Depends(caller)):
        who = admin(c)
        reg = twins_or_409()
        try:
            e = reg.add(body)
        except ValueError as e2:
            raise HTTPException(400, str(e2))
        if A().ops:
            A().ops.audit("twin added", who.user, who.level, "", "twins", {k: v for k, v in e.items() if k != "token_env"})
        status = next((x for x in reg.status() if x["name"] == e["name"]), {})
        return {"added": e, "status": status}

    @app.delete("/twins/{name}")
    def remove_twin(name: str, c=Depends(caller)):
        if not c["who"].can("root"):
            raise HTTPException(403, "removing a twin needs root")
        reg = twins_or_409()
        try:
            reg.remove(name)
        except (KeyError, ValueError) as e:
            raise HTTPException(400, str(e))
        if A().ops:
            A().ops.audit("twin removed", c["who"].user, c["who"].level, "", "twins", {"name": name})
        return {"removed": name, "active": reg.active_name()}

    @app.post("/twins/{name}/test")
    def test_twin(name: str, c=Depends(caller)):
        reg = twins_or_409()
        try:
            d = reg.get(name).describe(fresh=True)
        except KeyError as e:
            raise HTTPException(404, str(e))
        except Exception as e:  # noqa: BLE001 - the answer is the test's result
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:400]}"}
        return {"ok": True, "name": d.name, "version": d.version, "contract": d.contract,
                "domains": [x.domain for x in d.domains], "capabilities": d.capabilities}

    @app.post("/admin/reload")
    def reload(c=Depends(caller)):
        """A new agent from the settings: the controllers the agentic MCP holds now, each with its subagent.
        Refused while a request runs, so nothing is cut off."""
        who = admin(c)
        if rebuild is None:
            raise HTTPException(409, "this service cannot rebuild its agent (it was started without a builder)")
        with busy_guard:
            held = [k for k, lk in busy.items() if lk.locked()]
        if held:
            raise HTTPException(409, f"{len(held)} request(s) are running; reload when they are done")
        old = ref["agent"]
        new = rebuild()
        ref["agent"] = new
        try:
            old.close()
        except Exception:  # noqa: BLE001
            logger.warning("closing the previous agent failed", exc_info=True)
        if new.ops:
            new.ops.audit("agent reloaded", who.user, who.level, "", "reload", {"subagents": new.info.get("subagents")})
        return {"reloaded": True, "subagents": new.info.get("subagents"), "tools": new.info.get("tools")}

    if STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

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
        uvicorn.run(create_app(agent, rebuild=lambda: build_agent(settings)), host=host, port=port,
                    log_level="info", **tls)
    finally:
        agent.close()
