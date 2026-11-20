"""
Networks, through the agentic MCP: one service, one contract, every controller.

The agentic MCP (`connector/`, or the standalone agentic-mcp package) holds a
seat per controller and offers each one's tools, every tool marked as a read
or a write. This provider turns that into the agent's federation:

  main agent      controller-neutral tools: network_controllers (who is there,
                  which parts of the contract each covers), network_health, and
                  follow_relations when any controller publishes `list_relations`
                  (rows {from, relation, to}): the dependency chain across domains,
                  followed by code rather than by the model
  a subagent per  that controller's whole tool set, named <key>__<tool>; the
  controller      main agent delegates to it with `task` (subagent_type = the controller's key)

A write pauses for an operator like every write here (approval.py), and only
the approved call goes to the agentic MCP with `approved_by` set to the
approving user, which is what the agentic MCP's own write gate lets through.
A call that was never approved carries no name, and the agentic MCP refuses it
before it reaches the controller: two gates, not one.

  AGENTIC_MCP_URL     default http://127.0.0.1:8400
  AGENTIC_MCP_TOKEN   bearer token, when the service requires one
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Type

import requests
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("canah.network")

# A safety cap only: a large result is kept whole and previewed by large_results.py, so the model
# knows when it is not seeing everything. Clipping here would hide that.
MAX_RESULT_CHARS = int(os.getenv("CANAH_NETWORK_RESULT_CHARS", "2000000"))


class AgenticMCP:
    """The HTTP surface of the agentic MCP service."""

    def __init__(self, url: str = "", token: str = "", timeout: float = 60.0):
        self.url = (url or os.getenv("AGENTIC_MCP_URL") or os.getenv("AGENTIC_MCP_ADDRESS")
                    or "http://127.0.0.1:8400").rstrip("/")
        self.token = token if token else os.getenv("AGENTIC_MCP_TOKEN", "")
        self.timeout = timeout
        self.http = requests.Session()

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def get(self, path: str, timeout: Optional[float] = None) -> Any:
        r = self.http.get(self.url + path, headers=self._headers(), timeout=timeout or self.timeout)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: Dict[str, Any], timeout: Optional[float] = None) -> Any:
        r = self.http.post(self.url + path, json=body, headers=self._headers(), timeout=timeout or self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"agentic MCP {path}: HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    def controllers(self) -> List[Dict[str, Any]]:
        return self.get("/controllers").get("controllers", [])

    def tools(self, key: str) -> List[Dict[str, Any]]:
        return self.get(f"/controllers/{key}/tools").get("tools", [])

    def call(self, key: str, tool: str, arguments: Dict[str, Any], approved_by: str = "") -> Dict[str, Any]:
        return self.post(f"/controllers/{key}/call",
                         {"tool": tool, "arguments": arguments, "approved_by": approved_by})


def _clip(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + f"\n... [{len(text) - MAX_RESULT_CHARS} more characters cut]"


def _user(config: Optional[RunnableConfig]) -> str:
    return str(((config or {}).get("configurable") or {}).get("user_id") or "operator")


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class ControllerTool(BaseTool):
    """One tool of one controller, called through the agentic MCP."""

    service: Any
    controller: str
    tool: str
    write: bool = False
    args_schema: Any = _Open

    def _run(self, config: RunnableConfig, run_manager: Any = None, **kwargs: Any) -> str:
        # A write reaches here only after the operator approved it (interrupt_on
        # stops the call before it runs); its approval travels as approved_by.
        by = f"{_user(config)} (approved in canah)" if self.write else ""
        try:
            got = self.service.call(self.controller, self.tool, kwargs, approved_by=by)
        except Exception as e:  # noqa: BLE001 - the model is told, the run goes on
            return f"Error: {self.controller} {self.tool} did not answer: {type(e).__name__}: {e}"
        text = str(got.get("text", ""))
        return _clip(("Error: " if got.get("is_error") and not text.lower().startswith("error") else "") + text)


class _Overview(BaseTool):
    service: Any
    path: str = ""
    args_schema: Type[BaseModel] = _Open

    def _run(self, run_manager: Any = None, **kwargs: Any) -> str:
        try:
            return _clip(json.dumps(self.service.get(self.path, timeout=30), default=str, indent=1))
        except Exception as e:  # noqa: BLE001
            return f"Error: the agentic MCP did not answer {self.path}: {type(e).__name__}: {e}"


class _ControllersTool(BaseTool):
    service: Any
    args_schema: Type[BaseModel] = _Open

    def _run(self, run_manager: Any = None, **kwargs: Any) -> str:
        try:
            rows = self.service.controllers()
        except Exception as e:  # noqa: BLE001
            return f"Error: the agentic MCP did not answer: {type(e).__name__}: {e}"
        out = []
        for c in rows:
            conf = c.get("conformance") or {}
            out.append({"controller": c.get("key"), "label": c.get("label"), "domain": c.get("domain"),
                        "family": c.get("family"), "tools": c.get("tools"), "contract": conf.get("verdict"),
                        "gaps": conf.get("gaps"), "error": c.get("error")})
        return json.dumps(out, indent=1, default=str)


def _description(c: Dict[str, Any]) -> str:
    conf = c.get("conformance") or {}
    covered = []
    for d in conf.get("domains") or []:
        if d.get("status") == "covered":
            covered.append(d.get("domain"))
    return (f"{c.get('label') or c['key']} ({c.get('domain') or 'network'} controller). Delegate here anything "
            f"that reads or changes what {c.get('label') or c['key']} holds"
            + (f": {', '.join(covered)}" if covered else "") + ".")


class _RelationsTool(BaseTool):
    """Follow what depends on what, across every controller that publishes its relations."""
    service: Any
    owners: Dict[str, List[str]] = {}
    args_schema: Any = {"type": "object", "properties": {
        "start": {"type": "string", "description": "the object to start from (a name, e.g. a link or a service)"},
        "depth": {"type": "integer", "description": "how many steps to follow (default 4)"}}, "required": ["start"]}

    def _run(self, start: str = "", depth: int = 4, run_manager: Any = None, **kwargs: Any) -> str:
        edges = []
        for key, tools in self.owners.items():
            if "list_relations" not in tools:
                continue
            try:
                got = self.service.call(key, "list_relations", {})
                rows = json.loads(got.get("text") or "[]")
            except Exception as e:  # noqa: BLE001
                edges.append({"error": f"{key}: {type(e).__name__}: {e}"})
                continue
            for r in rows if isinstance(rows, list) else []:
                if isinstance(r, dict) and r.get("from") and r.get("to"):
                    edges.append({"from": str(r["from"]), "relation": str(r.get("relation") or "relates to"),
                                  "to": str(r["to"]), "controller": key})
        good = [e for e in edges if "from" in e]
        if not good:
            return "No controller publishes its relations (a list_relations tool), so the chain cannot be followed " \
                   "here: ask each controller's subagent instead."

        def same(a: str, b: str) -> bool:
            a, b = a.lower(), b.lower()
            return a == b or a.split(":")[-1] == b.split(":")[-1]
        seen, frontier, lines = set(), [start], []
        for step in range(max(1, min(int(depth or 4), 8))):
            nxt = []
            for node in frontier:
                for e in good:
                    key = (e["from"], e["relation"], e["to"])
                    if key in seen:
                        continue
                    if same(e["from"], node):
                        seen.add(key)
                        lines.append(f"{'  ' * step}{e['from']} {e['relation']} {e['to']}   ({e['controller']})")
                        nxt.append(e["to"])
                    elif same(e["to"], node):
                        seen.add(key)
                        lines.append(f"{'  ' * step}{e['to']} <- {e['relation']} by {e['from']}   ({e['controller']})")
                        nxt.append(e["from"])
            frontier = nxt
            if not frontier:
                break
        errors = [e["error"] for e in edges if "error" in e]
        if not lines:
            return f"Nothing in the published relations involves {start}." + (f" ({'; '.join(errors)})" if errors else "")
        return "\n".join([f"What {start} connects to, step by step (as the controllers publish it):", *lines] +
                         ([f"(not read: {'; '.join(errors)})"] if errors else []))


class AgenticMCPProvider:
    """The networks the agentic MCP holds, as tools and subagents."""

    def __init__(self, url: str = "", token: str = "", controllers: Optional[List[str]] = None,
                 service: Optional[AgenticMCP] = None):
        self.service = service or AgenticMCP(url, token)
        self.only = controllers
        self._tools: List[BaseTool] = []
        self._writes: List[str] = []
        self._subagents: List[Dict[str, Any]] = []
        self._loaded = False
        self.status: Dict[str, Any] = {}

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            seats = self.service.controllers()
        except Exception as e:  # noqa: BLE001 - the agent still starts, and says the network is out of reach
            logger.warning("agentic MCP at %s unavailable: %s", self.service.url, e)
            self.status = {"ok": False, "error": f"{type(e).__name__}: {e}", "url": self.service.url}
            seats = []
        self._tools = [
            _ControllersTool(service=self.service, name="network_controllers",
                             description="The controllers the agentic MCP holds: each one's domain, how many tools "
                                         "it has, which parts of the common contract it covers and its gaps."),
            _Overview(service=self.service, path="/sessions", name="network_health",
                      description="Whether each controller itself answers: reachable or not, latency, errors, open "
                                         "breakers. About the controllers, not the devices or services they manage: "
                                         "ask a controller's subagent for those."),
        ]
        held = {}
        self._owners: Dict[str, List[str]] = {}
        for c in seats:
            key = c.get("key")
            if not key or (self.only and key not in self.only) or c.get("error"):
                continue
            try:
                specs = self.service.tools(key)
            except Exception as e:  # noqa: BLE001
                logger.warning("agentic MCP: no tools for %s: %s", key, e)
                held[key] = {"ok": False, "error": str(e)}
                continue
            mine: List[BaseTool] = []
            self._owners[key] = [t["name"] for t in specs]
            for t in specs:
                name = f"{key}__{t['name']}"
                tool = ControllerTool(service=self.service, controller=key, tool=t["name"], write=bool(t.get("write")),
                                      name=name, description=(t.get("description") or t["name"]).strip()[:1000],
                                      args_schema=t.get("schema") or {"type": "object", "properties": {}})
                mine.append(tool)
                if tool.write:
                    self._writes.append(name)
            self._tools += mine
            self._subagents.append({
                "name": key, "description": _description(c),
                "system_prompt": (f"You operate {c.get('label') or key} through its tools ({key}__*). Read before "
                                  f"you answer; quote what the controller returned. Ask for the smallest view that "
                                  f"answers: a tool, or a parameter, that lists exactly what was asked comes before "
                                  f"fetching everything the controller holds. When you count, name what you counted, so the count "
                                  f"can be checked. A tool that changes the network is paused for an operator's "
                                  f"approval: call it with the right arguments and the pause asks for you. Report "
                                  f"what you did and what you found, briefly."),
                "tools": mine})
            held[key] = {"ok": True, "tools": len(mine), "writes": sum(1 for t in mine if t.write)}
        if any("list_relations" in names for names in self._owners.values()):
            self._tools.append(_RelationsTool(
                service=self.service, owners=self._owners, name="follow_relations",
                description=("Follow what depends on what, across every controller that publishes its relations: "
                             "from one object (a link, a service, a port), what carries it, what it carries, and so "
                             "on, step by step. Use it first for impact questions (what does a fault in X affect) and "
                             "for how something reaches somewhere.")))
        if seats:
            self.status = {"ok": True, "url": self.service.url, "controllers": held}

    def tools(self) -> List[BaseTool]:
        self._load()
        # The main agent keeps the overview; the controllers' own tools are its subagents'.
        return list(self._tools)

    def writes(self) -> List[str]:
        self._load()
        return list(self._writes)

    def subagents(self) -> List[Dict[str, Any]]:
        self._load()
        return list(self._subagents)

    def main_tools(self) -> List[str]:
        return ["network_controllers", "network_health", "follow_relations"]

    def instructions(self) -> str:
        self._load()
        if not self.status.get("ok"):
            return (f"The networks are reached through the agentic MCP at {self.service.url}, which is not answering "
                    f"now ({self.status.get('error')}). Say so when a request needs the network.")
        names = ", ".join(self.status.get("controllers", {})) or "none"
        return (f"Networks are reached through the agentic MCP, which holds these controllers: {names}. Use "
                f"network_controllers / network_health for a view across them, and delegate "
                f"anything about one controller to its subagent with `task` (subagent_type = the controller's key). "
                f"To change something on a controller, delegate the exact change to its subagent (what, where, with "
                f"which values); its call is what pauses for the operator's approval. "
                f"For a request that spans controllers, ask each one's subagent and combine the answers.")


def provider() -> AgenticMCPProvider:
    """For `providers: [canah.network:provider]` in canah.yaml."""
    return AgenticMCPProvider()
