"""
An agentic-MCP-compatible service over the emulated domains: the same HTTP contract
CANAH's canah/network.py speaks to a real agentic MCP.

  GET  /health
  GET  /controllers                  [{key, label, domain, tools, conformance}]
  GET  /controllers/{key}/tools      [{name, description, schema, write}]
  POST /controllers/{key}/call       {tool, arguments, approved_by} -> {text, is_error}
  GET  /sessions                     each controller's session
  POST /reset                        every domain back to its start (test use)

As the real agentic MCP's write gate does: a write without `approved_by` is
refused before it reaches the domain. Optional bearer token (TESTBED_TOKEN).
"""
from __future__ import annotations

import os
import socket
import threading
import time
from typing import Any, Dict, Optional

from fastapi import Body, FastAPI, Header, HTTPException

from .domains import Domain, all_domains


def create_app(domains: Optional[Dict[str, Domain]] = None, token: str = "") -> FastAPI:
    domains = domains or all_domains()
    app = FastAPI(title="testbed agentic MCP")
    app.state.domains = domains
    app.state.refused = []

    def check(authorization: str):
        if token and authorization != f"Bearer {token}":
            raise HTTPException(401, "this needs the agentic MCP's token")

    @app.get("/health")
    def health():
        return {"ok": True, "controllers": list(domains)}

    @app.get("/controllers")
    def controllers():
        out = []
        for d in domains.values():
            out.append({"key": d.key, "label": d.label, "domain": d.domain, "tools": len(d.tools()),
                        "conformance": {"verdict": "accepted", "domains": [{"domain": "inventory",
                                                                           "status": "covered"}]}})
        return {"controllers": out}

    @app.get("/controllers/{key}/tools")
    def tools(key: str):
        d = domains.get(key) or (_ for _ in ()).throw(HTTPException(404, f"no controller {key}"))
        return {"tools": [{"name": t.name, "description": t.description, "schema": t.schema, "write": t.write,
                           "verb": ""} for t in d.tools()]}

    @app.post("/controllers/{key}/call")
    def call(key: str, body: Dict[str, Any] = Body(...), authorization: str = Header(default="")):
        check(authorization)
        d = domains.get(key)
        if d is None:
            raise HTTPException(404, f"no controller {key}")
        t = {x.name: x for x in d.tools()}.get(str(body.get("tool")))
        if t is None:
            return {"text": f"Error: {key} has no tool {body.get('tool')}", "is_error": True}
        if t.write and not str(body.get("approved_by") or "").strip():
            app.state.refused.append({"controller": key, "tool": t.name})
            return {"text": f"Error: {t.name} changes {key} and carries no approval; refused", "is_error": True}
        try:
            got = t.fn(**(body.get("arguments") or {}))
        except TypeError as e:
            return {"text": f"Error: {t.name}: {e}", "is_error": True}
        text = str(got)
        return {"text": text, "is_error": text.startswith("Error")}

    @app.get("/sessions")
    def sessions():
        return {k: {"reachable": True, "latency_ms": 3} for k in domains}

    @app.post("/reset")
    def reset():
        for d in domains.values():
            d.reset()
        app.state.refused.clear()
        return {"ok": True}

    return app


class Running:
    """The emulator served on a free local port, in a background thread."""

    def __init__(self, domains: Optional[Dict[str, Domain]] = None, token: str = ""):
        import uvicorn
        self.domains = domains or all_domains()
        self.app = create_app(self.domains, token)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(5)


def main(argv=None) -> int:
    """Serve the emulated domains on their own: python -m testbed.emulator [--host H] [--port 8401]."""
    import argparse
    import uvicorn
    ap = argparse.ArgumentParser(prog="testbed.emulator")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8401)
    a = ap.parse_args(argv)
    uvicorn.run(create_app(token=os.getenv("TESTBED_TOKEN", "")), host=a.host, port=a.port, log_level="warning")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
