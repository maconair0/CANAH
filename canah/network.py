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
  contract verbs  net_<verb>, one tool per verb of the common contract that any controller serves, with the
                  controller as an argument: the same tools for every controller; a read without a controller
                  asks every controller that serves it
  beyond them     network_tools finds a controller's own tools; controller_read / controller_write call one.
                  No agent per controller: the agentic MCP makes the controllers' verbs the same, so the
                  main agent works them all itself.

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
import re
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


CONNECTION_FILE = "agentic_mcp.json"     # under CANAH_HOME: the address and token set on the web page


def load_connection(home: Any) -> Dict[str, str]:
    """The agentic MCP's address and token as set on the web page, put in the environment so every client
    (the agent's, the topology's) uses them. The page's choice is newer than .env, so it wins."""
    from pathlib import Path
    f = Path(home) / CONNECTION_FILE
    try:
        got = json.loads(f.read_text())
    except (OSError, ValueError):
        return {}
    if got.get("url"):
        os.environ["AGENTIC_MCP_URL"] = got["url"]
    if got.get("token"):
        os.environ["AGENTIC_MCP_TOKEN"] = got["token"]
    return got


def save_connection(home: Any, url: Optional[str] = None, token: Optional[str] = None) -> Dict[str, Any]:
    """Keep the address and/or token (readable by this user only) and use them from now on. token="" clears it."""
    from pathlib import Path
    f = Path(home) / CONNECTION_FILE
    try:
        got = json.loads(f.read_text())
    except (OSError, ValueError):
        got = {}
    if url is not None:
        got["url"] = url.rstrip("/")
        os.environ["AGENTIC_MCP_URL"] = got["url"]
    if token is not None:
        if token:
            got["token"] = token
            os.environ["AGENTIC_MCP_TOKEN"] = token
        else:
            got.pop("token", None)
            os.environ.pop("AGENTIC_MCP_TOKEN", None)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(got))
    os.chmod(tmp, 0o600)
    os.replace(tmp, f)
    return {"url": got.get("url"), "token_set": bool(got.get("token"))}


class AgenticMCP:
    """The HTTP surface of the agentic MCP service. Without an explicit url or token it reads them from the
    environment at each call, so a token set on the web page reaches every client at once."""

    def __init__(self, url: str = "", token: str = "", timeout: float = 60.0):
        self._url, self._token = url.rstrip("/"), token
        self.timeout = timeout
        self.http = requests.Session()

    @property
    def url(self) -> str:
        return (self._url or os.getenv("AGENTIC_MCP_URL") or os.getenv("AGENTIC_MCP_ADDRESS")
                or "http://127.0.0.1:8400").rstrip("/")

    @url.setter
    def url(self, value: str) -> None:
        self._url = (value or "").rstrip("/")

    @property
    def token(self) -> str:
        return self._token or os.getenv("AGENTIC_MCP_TOKEN", "")

    @token.setter
    def token(self, value: str) -> None:
        self._token = value or ""

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
                   "here: read each controller's links and services with the net_ tools instead."

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


def _arg_summary(schema: Any) -> str:
    """A tool's arguments in a few words: names, a * for the required ones."""
    props = (schema or {}).get("properties") or {}
    if not props:
        return "no arguments"
    req = set((schema or {}).get("required") or [])
    return ", ".join(f"{k}{'*' if k in req else ''}" for k in props)


def _find(catalog: Dict[str, Dict[str, Dict[str, Any]]], controller: str, tool: str) -> Optional[Dict[str, Any]]:
    """A controller's tool by its name, given with or without the controller's prefix."""
    tools = catalog.get(controller) or {}
    for name in (tool, tool.split("__", 1)[-1], f"{controller}_{tool}"):
        if name in tools:
            return tools[name]
    return None


class _CatalogTool(BaseTool):
    """Shared by the tools below: the controllers' tools as the agentic MCP offers them."""
    service: Any
    catalog: Dict[str, Dict[str, Dict[str, Any]]] = {}     # controller -> tool name -> record

    def _find(self, controller: str, tool: str) -> Optional[Dict[str, Any]]:
        return _find(self.catalog, controller, tool)

    def _call(self, controller: str, rec: Dict[str, Any], arguments: Dict[str, Any], config: Any) -> str:
        # A write reaches here only after the operator approved it (interrupt_on stops the call before it
        # runs); the approval travels to the agentic MCP as approved_by, and its own gate checks it.
        by = f"{_user(config)} (approved in canah)" if rec.get("write") else ""
        try:
            got = self.service.call(controller, rec["name"], arguments or {}, approved_by=by)
        except Exception as e:  # noqa: BLE001 - the model is told, the run goes on
            return f"Error: {controller} {rec['name']} did not answer: {type(e).__name__}: {e}"
        text = str(got.get("text", ""))
        return _clip(("Error: " if got.get("is_error") and not text.lower().startswith("error") else "") + text)


class VerbTool(_CatalogTool):
    """One verb of the common contract, on whichever controller serves it: the same tool for every controller.
    A read with no controller named asks every controller that serves the verb, and answers per controller."""
    verb: str
    write: bool = False
    served: Dict[str, str] = {}                                # controller -> its tool for this verb
    args_schema: Any = None

    def _run(self, config: RunnableConfig, controller: str = "", arguments: Optional[Dict[str, Any]] = None,
             run_manager: Any = None, **kwargs: Any) -> str:
        arguments = dict(arguments or {}, **{k: v for k, v in kwargs.items() if k not in ("callbacks",)})
        if controller and controller not in self.served:
            return (f"Error: {controller} does not serve {self.verb} through the agentic MCP. It is served by: "
                    f"{', '.join(self.served) or 'no controller'}. network_tools(controller=...) lists what "
                    f"{controller} offers instead.")
        if not controller:
            if self.write:
                return f"Error: {self.verb} changes the network: name the controller ({', '.join(self.served)})."
            keys = list(self.served)
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(8, len(keys) or 1)) as pool:
                got = list(pool.map(lambda k: self._call(k, self.catalog[k][self.served[k]], arguments, config), keys))
            return _clip(json.dumps({k: _maybe_json(v) for k, v in zip(keys, got)}, indent=1, default=str))
        return self._call(controller, self.catalog[controller][self.served[controller]], arguments, config)


def _maybe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


class _SearchTool(_CatalogTool):
    args_schema: Any = {"type": "object", "properties": {
        "controller": {"type": "string", "description": "only this controller's tools (its key)"},
        "verb": {"type": "string", "description": "only the tools serving this contract verb"},
        "query": {"type": "string", "description": "words to look for in the tools' names and descriptions"},
        "limit": {"type": "integer", "description": "at most this many (default 15)"}}}

    def _run(self, controller: str = "", verb: str = "", query: str = "", limit: int = 15,
             run_manager: Any = None, **kwargs: Any) -> str:
        words = [w for w in re.findall(r"[a-z0-9]+", (query or "").lower()) if len(w) > 1]
        rows = []
        for key, tools in self.catalog.items():
            if controller and key != controller:
                continue
            for rec in tools.values():
                if verb and rec.get("verb") != verb:
                    continue
                hay = f"{rec['name']} {rec.get('verb') or ''} {rec.get('description') or ''}".lower()
                score = sum(hay.count(w) for w in words) if words else 1
                if words and not score:
                    continue
                rows.append((score, {"controller": key, "tool": rec["name"], "verb": rec.get("verb") or None,
                                     "write": bool(rec.get("write")), "arguments": rec.get("schema") or {},
                                     "description": (rec.get("description") or "")[:300]}))
        rows.sort(key=lambda r: -r[0])
        out = [r for _, r in rows[: max(1, min(int(limit or 15), 40))]]
        if not out:
            return "No tool matches. Try fewer words, another controller, or no filter to see what each one offers."
        return json.dumps(out, indent=1, default=str)


class _ControllerCall(_CatalogTool):
    write: bool = False
    args_schema: Any = {"type": "object", "properties": {
        "controller": {"type": "string", "description": "the controller's key"},
        "tool": {"type": "string", "description": "the tool's name, as network_tools gives it"},
        "arguments": {"type": "object", "description": "the tool's arguments, as its schema says"}},
        "required": ["controller", "tool"]}

    def _run(self, config: RunnableConfig, controller: str = "", tool: str = "",
             arguments: Optional[Dict[str, Any]] = None, run_manager: Any = None, **kwargs: Any) -> str:
        rec = self._find(controller, tool)
        if rec is None:
            return (f"Error: {controller or '(no controller)'} has no tool {tool!r}. network_tools(controller="
                    f"{controller!r}, query=...) finds it.")
        if rec.get("write") and not self.write:
            return (f"Error: {rec['name']} changes the network: call controller_write with the same arguments, "
                    f"so it waits for the operator's approval.")
        return self._call(controller, rec, arguments or {}, config)


def _verb_schema(verb: str, write: bool, served: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    keys = sorted(served)
    return {"type": "object", "properties": {
        "controller": {"type": "string", "enum": keys,
                       "description": ("the controller to change" if write else
                                       "the controller to read; leave it out to ask every controller that serves it")},
        "arguments": {"type": "object", "description": "the controller's own arguments for it, by controller: " +
                      "; ".join(f"{k}: {_arg_summary(served[k].get('schema'))}" for k in keys)}},
        "required": ["controller"] if write else []}


class AgenticMCPProvider:
    """The networks, through the agentic MCP. The agentic MCP holds the controllers and checks each against the
    common contract; CANAH's main agent works them all through it with one set of tools, the same for every
    controller:

      net_<verb>          one per contract verb any controller serves (net_read_devices, net_provision, ...),
                          with the controller as an argument; a read without one asks every controller
      network_tools       finds a controller's own tools beyond the contract (by controller, verb or words)
      controller_read     calls one of them that reads
      controller_write    calls one that changes the network: it waits for the operator's approval
      network_controllers, network_health, follow_relations: the view across them

    No agent per controller: the contract makes the controllers' verbs the same, so the main agent holds a
    couple of dozen tools rather than every controller's hundreds."""

    def __init__(self, url: str = "", token: str = "", controllers: Optional[List[str]] = None,
                 service: Optional[AgenticMCP] = None):
        self.service = service or AgenticMCP(url, token)
        self.only = controllers
        self._tools: List[BaseTool] = []
        self._writes: List[str] = []
        self.catalog: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.verb_tools: Dict[str, VerbTool] = {}
        self._loaded = False
        self.status: Dict[str, Any] = {}

    def _contract_verbs(self) -> Dict[str, Dict[str, Any]]:
        try:
            k = self.service.get("/contract", timeout=10).get("contract") or {}
            return {v: {**rule, "domain": d.get("name")} for d in k.get("domains") or [] for v, rule in (d.get("verbs") or {}).items()}
        except Exception:  # noqa: BLE001 - an agentic MCP without the contract route: the tools' own flags
            return {}

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
                             description="The SDN controllers the agentic MCP holds: each one's domain, how many tools "
                                         "it has, which parts of the common contract it covers and its gaps."),
            _Overview(service=self.service, path="/sessions", name="network_health",
                      description="Whether each controller itself answers: reachable or not, latency, errors, open "
                                  "breakers. About the controllers, not what they manage: read that with the net_ tools."),
        ]
        held: Dict[str, Any] = {}
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
            self.catalog[key] = {t["name"]: t for t in specs}
            held[key] = {"ok": True, "tools": len(specs), "label": c.get("label") or key, "domain": c.get("domain"),
                         "verbs": sorted({t["verb"] for t in specs if t.get("verb")})}
        rules = self._contract_verbs()
        served: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for key, tools in self.catalog.items():
            for rec in tools.values():
                if rec.get("verb"):
                    served.setdefault(rec["verb"], {}).setdefault(key, rec)
        for verb in sorted(served):
            by = served[verb]
            rule = rules.get(verb) or {}
            write = bool(rule.get("writes")) if rule else any(r.get("write") for r in by.values())
            what = (rule.get("help") or rule.get("why") or verb.replace("_", " ")).strip()
            name = f"net_{verb}"
            t = VerbTool(service=self.service, catalog=self.catalog, verb=verb, write=write,
                         served={k: r["name"] for k, r in by.items()}, name=name,
                         args_schema=_verb_schema(verb, write, by),
                         description=(f"Contract verb {verb.replace('_', ' ')}: {what}. On any controller that serves "
                                      f"it ({', '.join(sorted(by))}), through the agentic MCP."
                                      + (" It changes the network: it waits for the operator's approval." if write else
                                         " Without a controller it asks every one that serves it.")))
            self.verb_tools[name] = t
            self._tools.append(t)
            if write:
                self._writes.append(name)
        if self.catalog:
            self._tools += [
                _SearchTool(service=self.service, catalog=self.catalog, name="network_tools",
                            description="Find a controller's own tools beyond the contract verbs: by controller, by "
                                        "verb, or by words (e.g. 'meters', 'intents', 'spectrum'). Gives each one's "
                                        "name, whether it changes the network, and its arguments."),
                _ControllerCall(service=self.service, catalog=self.catalog, name="controller_read",
                                description="Call one of a controller's own tools that reads (as network_tools found "
                                            "it), through the agentic MCP."),
                _ControllerCall(service=self.service, catalog=self.catalog, write=True, name="controller_write",
                                description="Call one of a controller's own tools that changes the network (as "
                                            "network_tools found it), through the agentic MCP. It waits for the "
                                            "operator's approval."),
            ]
            self._writes.append("controller_write")
        owners = {k: list(v) for k, v in self.catalog.items()}
        if any("list_relations" in names for names in owners.values()):
            self._tools.append(_RelationsTool(
                service=self.service, owners=owners, name="follow_relations",
                description=("Follow what depends on what, across every controller that publishes its relations: "
                             "from one object (a link, a service, a port), what carries it, what it carries, and so "
                             "on, step by step. Use it first for impact questions (what does a fault in X affect) and "
                             "for how something reaches somewhere.")))
        if seats:
            self.status = {"ok": True, "url": self.service.url, "controllers": held}

    def write_target(self, name: str, args: Dict[str, Any]) -> Optional[tuple]:
        """(controller, tool, arguments) a write call will send, for the digital twin's write gate."""
        self._load()
        args = args or {}
        if name == "controller_write":
            key = str(args.get("controller") or "")
            rec = _find(self.catalog, key, str(args.get("tool") or ""))
            return (key, rec["name"] if rec else str(args.get("tool") or ""), dict(args.get("arguments") or {}))
        vt = self.verb_tools.get(name)
        if vt is not None and vt.write:
            key = str(args.get("controller") or "")
            return (key, vt.served.get(key, vt.verb), dict(args.get("arguments") or {}))
        return None

    def tools(self) -> List[BaseTool]:
        self._load()
        return list(self._tools)

    def writes(self) -> List[str]:
        self._load()
        return list(self._writes)

    def main_tools(self) -> List[str]:
        self._load()
        return [t.name for t in self._tools]

    def instructions(self) -> str:
        self._load()
        if not self.status.get("ok"):
            return (f"The networks are reached through the agentic MCP at {self.service.url}, which is not answering "
                    f"now ({self.status.get('error')}). Say so when a request needs the network.")
        rows = [f"{k} ({v.get('label')}, {v.get('domain') or 'network'})" for k, v in self.status.get("controllers", {}).items()
                if v.get("ok")]
        return (f"Networks are reached through the agentic MCP, which holds these SDN controllers: "
                f"{', '.join(rows) or 'none'}. You work them all yourself, with the same tools for every controller: "
                f"a net_<verb> tool per contract verb (net_read_devices, net_read_links, net_provision, ...), with the "
                f"controller as an argument; a read without a controller asks every controller that serves it, which "
                f"is how to answer across controllers. For anything beyond the contract verbs, find the controller's "
                f"own tool with network_tools, then call it with controller_read, or controller_write for a change. "
                f"Read before you answer and quote what the controllers returned. A change (a net_ write verb or "
                f"controller_write) waits for the operator's approval: call it with the exact arguments and the "
                f"pause asks for you.")


def provider() -> AgenticMCPProvider:
    """For `providers: [canah.network:provider]` in canah.yaml."""
    return AgenticMCPProvider()
