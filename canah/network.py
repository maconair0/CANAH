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
import time
import logging
import os
from typing import Any, Dict, List, Optional, Type

import requests
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool
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

    def _run(self, config: RunnableConfig, run_manager: Any = None, **kwargs: Any) -> str:
        from .identity import scope_of
        try:
            rows = self.service.controllers()
        except Exception as e:  # noqa: BLE001
            return f"Error: the agentic MCP did not answer: {type(e).__name__}: {e}"
        scope = scope_of(config)
        out = []
        for c in rows:
            if scope and c.get("key") not in scope:
                continue
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
        # "tenant green", "lightpath LP-1", "the service VS-100": the object is the name, not its kind.
        names = {x for e in good for x in (e["from"], e["to"])}
        if not any(same(start, n) for n in names):
            kinds = r"^(tenant|lightpath|service|link|router|node|device|switch|port|fibre|fiber|customer|vlan)[-_ ]"
            cands = str(start).replace(",", " ").split() + [re.sub(kinds, "", str(start), flags=re.I)]
            for cand in reversed(cands):
                if any(same(cand, n) for n in names):
                    start = cand
                    break
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
        # A summary first: a long chain is easy to misread (one answer put a customer on the link instead of the
        # service that serves it). What was reached, by domain, and which services serve which customers.
        reached: Dict[str, List[str]] = {}
        for e in good:
            if (e["from"], e["relation"], e["to"]) in seen:
                for x in (e["from"], e["to"]):
                    dom, _, obj = x.partition(":")
                    if obj and not same(x, start) and obj not in reached.setdefault(dom, []):
                        reached[dom].append(obj)
        serves = [(e["from"].partition(":")[2] or e["from"], e["to"].partition(":")[2] or e["to"]) for e in good
                  if (e["from"], e["relation"], e["to"]) in seen and re.search(r"serves|customer", e["relation"], re.I)]
        head = [f"Reached from {start}: " + "; ".join(f"{d}: {', '.join(v)}" for d, v in reached.items() if d != "customer")]
        if serves:
            head.append("Services and the customers they serve: " + "; ".join(f"{svc} serves {cust}" for svc, cust in serves))
        return "\n".join([*head, f"The chain, step by step (as the controllers publish it):", *lines] +
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


_DOWN: Dict[str, float] = {}          # controller -> when it last timed out
DOWN_FOR_S = float(os.getenv("CANAH_DOWN_FOR_S", "20"))


class _CatalogTool(BaseTool):
    """Shared by the tools below: the controllers' tools as the agentic MCP offers them."""
    service: Any
    catalog: Dict[str, Dict[str, Dict[str, Any]]] = {}     # controller -> tool name -> record
    changes_by: Dict[str, str] = {}                        # controller -> the config agent that changes it (team)
    locate: Any = None                                     # name -> controllers whose reads mention it

    def _find(self, controller: str, tool: str) -> Optional[Dict[str, Any]]:
        return _find(self.catalog, controller, tool)

    def _out_of_scope(self, controller: str, config: Any) -> Optional[str]:
        from .identity import scope_of
        scope = scope_of(config)
        if scope and controller not in scope:
            return (f"Error: {controller} is outside what this operator works with ({', '.join(scope)}). Answer "
                    f"from those controllers only, and say so.")
        return None

    def _call(self, controller: str, rec: Dict[str, Any], arguments: Dict[str, Any], config: Any) -> str:
        refused = self._out_of_scope(controller, config)
        if refused:
            return refused
        # A controller that just timed out is not asked again for a little while: each read of it would wait and
        # fail the same way (an investigation read a down controller nine times). Writes are always tried.
        since = _DOWN.get(controller)
        if since and not rec.get("write") and time.time() - since < DOWN_FOR_S:
            return (f"Error: {controller} is not answering (it timed out {int(time.time() - since)} s ago), so it "
                    f"cannot be read now. Do not read it again in this request: work from what the other controllers "
                    f"and the evidence say, and report that {controller} could not be read.")
        # A write reaches here only after the operator approved it (interrupt_on stops the call before it
        # runs); the approval travels to the agentic MCP as approved_by, and its own gate checks it.
        by = f"{_user(config)} (approved in canah)" if rec.get("write") else ""
        try:
            got = self.service.call(controller, rec["name"], arguments or {}, approved_by=by)
        except Exception as e:  # noqa: BLE001 - the model is told, the run goes on
            return f"Error: {controller} {rec['name']} did not answer: {type(e).__name__}: {e}"
        text = str(got.get("text", ""))
        if re.search(r"does not answer|not answering|ConnectTimeout|ConnectError|timed out|unreachable", text[:300], re.I) \
                and (got.get("is_error") or text.lower().startswith("error")):
            _DOWN[controller] = time.time()
        else:
            _DOWN.pop(controller, None)
        out = _clip(("Error: " if got.get("is_error") and not text.lower().startswith("error") else "") + text)
        if rec.get("write") and out.startswith("Error") and self.locate is not None:
            out += self._where_it_is(out, controller)
        return out

    def _where_it_is(self, error: str, controller: str) -> str:
        """A write refused for an object the controller does not have: where that object is, and who changes it
        there. (A tenant change sent to the packet domain failed on "no router border-leaf-1"; the leaf is the
        data centre's.)"""
        m = (re.search(r"\b(?:no|unknown|not found:?)\s+(?:router|switch|device|node|link|service|tenant|port|"
                       r"transponder|lightpath|interface|leaf)\s+['\"]?([\w:./-]+)", error, re.I)
             or re.search(r"['\"]?([\w:./-]+)['\"]? is not an? \w+", error))
        if not m:
            return ""
        name = m.group(1).rstrip(".,")
        try:
            found = [k for k in self.locate(name) if k != controller]
        except Exception:  # noqa: BLE001
            return ""
        if not found:
            return ""
        who = "; ".join(f"{k}" + (f" (changed by {self.changes_by[k]})" if self.changes_by.get(k) else "") for k in found)
        return (f"\n[{name} is not on {controller}: it is on {who}. If the change is to {name} itself, it belongs "
                f"there: say so in your report. If {name} was only a value in this change (the link to ride, say), "
                f"use {controller}'s own object instead: the error, or a read, names them (the one {name} carries, "
                f"for a lightpath).]")


def _terms(match: Any) -> List[str]:
    if isinstance(match, (list, tuple)):
        return [str(x).strip().lower() for x in match if str(x).strip()]
    return [x.strip().lower() for x in str(match or "").split(",") if x.strip()]


def _names_it(d: Dict[str, Any], terms: List[str]) -> bool:
    """The object itself carries one of the terms in its own fields (its name, id, router...), not only deeper."""
    return any(not isinstance(v, (dict, list)) and any(t in str(v).lower() for t in terms) for v in d.values())


def _mentions(x: Any, terms: List[str]) -> bool:
    """A term in the values (never in the keys: "lightpaths" is not a lightpath)."""
    if isinstance(x, dict):
        return any(_mentions(v, terms) for v in x.values())
    if isinstance(x, list):
        return any(_mentions(v, terms) for v in x)
    return x is not None and any(t in str(x).lower() for t in terms)


def _keep(value: Any, terms: List[str], tally: List[int]) -> Any:
    """Every list in a controller's answer cut to the items that mention one of the terms. An object that names a
    term itself is kept whole: {"router": "pe-b", "interfaces": [...]} asked for pe-b keeps all its interfaces.
    tally: [items kept, items looked at], over the outermost lists."""
    if isinstance(value, list):
        if value and all(isinstance(x, (dict, list, str)) for x in value):
            kept = [x if isinstance(x, dict) and _names_it(x, terms) else _keep(x, terms, [0, 0]) for x in value
                    if _mentions(x, terms)]
            tally[0] += len(kept)
            tally[1] += len(value)
            return kept
        return value
    if isinstance(value, dict):
        if _names_it(value, terms):
            tally[0] += 1
            tally[1] += 1
            return value
        return {k: _keep(v, terms, tally) for k, v in value.items()}
    return value


def filter_answer(text: str, match: Any) -> str:
    """A read's answer cut to what `match` names (comma-separated words: names, ids, a site, a type), done by the
    harness, so it works the same on every controller: a long device list becomes the devices asked about. The
    first line says how many items were kept of how many, so a cut list is never read as the whole."""
    terms = _terms(match)
    if not terms or text.startswith("Error"):
        return text
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        lines = text.splitlines()
        hit = [x for x in lines if any(t in x.lower() for t in terms)]
        if not hit:
            return f"Nothing in the answer mentions {', '.join(terms)}."
        return f"[match {', '.join(terms)}: {len(hit)} of {len(lines)} lines kept]\n" + "\n".join(hit)
    tally = [0, 0]
    cut = _keep(data, terms, tally)
    if not tally[0]:
        return f"Nothing in the answer mentions {', '.join(terms)}."
    return (f"[match {', '.join(terms)}: {tally[0]} of {tally[1]} items kept; the others do not mention it]\n"
            + json.dumps(cut, indent=1, default=str))


class VerbTool(_CatalogTool):
    """One verb of the common contract, on whichever controller serves it: the same tool for every controller.
    A read with no controller named asks every controller that serves the verb, and answers per controller."""
    verb: str
    write: bool = False
    served: Dict[str, str] = {}                                # controller -> its tool for this verb
    args_schema: Any = None

    def _run(self, config: RunnableConfig, controller: str = "", arguments: Optional[Dict[str, Any]] = None,
             match: Any = None, run_manager: Any = None, **kwargs: Any) -> str:
        arguments = dict(arguments or {}, **{k: v for k, v in kwargs.items() if k not in ("callbacks",)})
        if match and not self.write and controller:
            got = filter_answer(self._read(config, controller, arguments, None), match)
            if got.startswith("Nothing in the answer mentions") and len(self.served) > 1:
                # The object may be another controller's (a packet service asked of the optical controller).
                from .identity import scope_of
                scope = scope_of(config)
                elsewhere = {}
                for k in self.served:
                    if k == controller or (scope and k not in scope):
                        continue
                    found = filter_answer(self._call(k, self.catalog[k][self.served[k]], {}, config), match)
                    if not found.startswith(("Nothing in the answer mentions", "Error")):
                        elsewhere[k] = _maybe_json(found)
                if elsewhere:
                    return (f"Nothing on {controller} mentions {match}. Found on "
                            f"{', '.join(elsewhere)}:\n" + _clip(json.dumps(elsewhere, indent=1, default=str)))
            if got.startswith("Nothing in the answer mentions"):
                others = sorted(v for v in ("read_devices", "read_links", "read_services", "read_alarms") if v != self.verb)
                got += (f" match looks in the values (names, ids, states, types), not in field names; to see "
                        f"everything, call it without match. It may also be another kind of object: try "
                        f"{', '.join('net_' + v for v in others)} (a lightpath or a tunnel is a service), or "
                        f"follow_relations from it.")
            return got
        if match and not self.write:
            return self._read(config, controller, arguments, match)
        return self._read(config, controller, arguments, None)

    def _read(self, config: Any, controller: str, arguments: Dict[str, Any], match: Any) -> str:
        if controller and controller not in self.served:
            return (f"Error: {controller} does not serve {self.verb} through the agentic MCP. It is served by: "
                    f"{', '.join(self.served) or 'no controller'}. network_tools(controller=...) lists what "
                    f"{controller} offers instead.")
        if not controller:
            if self.write:
                return f"Error: {self.verb} changes the network: name the controller ({', '.join(self.served)})."
            from .identity import scope_of
            scope = scope_of(config)
            keys = [k for k in self.served if not scope or k in scope]
            if not keys:
                return f"No controller this operator works with ({', '.join(scope)}) serves {self.verb}."
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(8, len(keys) or 1)) as pool:
                got = list(pool.map(lambda k: self._call(k, self.catalog[k][self.served[k]], arguments, config), keys))
            if match:
                got = [filter_answer(v, match) for v in got]
            per = int(os.getenv("CANAH_FANOUT_CHARS", "3000"))

            def short(k: str, v: str) -> Any:
                # One controller's long answer (a YANG library for "health") must not drown the others.
                if len(v) <= per:
                    return _maybe_json(v)
                return v[:per] + f" ... [cut: {len(v)} characters; call {self.name} with controller={k!r} for all of it]"
            return _clip(json.dumps({k: short(k, v) for k, v in zip(keys, got)}, indent=1, default=str))
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

    def _run(self, config: RunnableConfig, controller: str = "", verb: str = "", query: str = "", limit: int = 15,
             run_manager: Any = None, **kwargs: Any) -> str:
        from .identity import scope_of
        scope = scope_of(config)
        words = [w for w in re.findall(r"[a-z0-9]+", (query or "").lower()) if len(w) > 1]
        rows = []
        for key, tools in self.catalog.items():
            if (controller and key != controller) or (scope and key not in scope):
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


_CALL_WRITE_SCHEMA = {"type": "object", "properties": {
    "controller": {"type": "string", "description": "the controller's key"},
    "tool": {"type": "string", "description": "the tool's name, as network_tools gives it"},
    "arguments": {"type": "object", "description": "the tool's arguments, as its schema says"}},
    "required": ["controller", "tool"]}


class _ControllerCall(_CatalogTool):
    write: bool = False
    args_schema: Any = {"type": "object", "properties": {
        "controller": {"type": "string", "description": "the controller's key"},
        "tool": {"type": "string", "description": "the tool's name, as network_tools gives it"},
        "arguments": {"type": "object", "description": "the tool's arguments, as its schema says"},
        "match": {"type": "string", "description": "a read only: keep only the items that mention these words, "
                                                   "comma-separated (names, ids)"}},
        "required": ["controller", "tool"]}

    def _run(self, config: RunnableConfig, controller: str = "", tool: str = "",
             arguments: Optional[Dict[str, Any]] = None, match: Any = None, run_manager: Any = None,
             **kwargs: Any) -> str:
        rec = self._find(controller, tool)
        if rec is None:
            if self.changes_by.get(controller) and (tool.startswith("net_") or tool in ("controller_write", "write", "provision")):
                return (f"Error: {tool!r} is not a {controller} tool, and changes are not yours: hand the change to "
                        f"{self.changes_by[controller]} with `task`.")
            return (f"Error: {controller or '(no controller)'} has no tool {tool!r}. network_tools(controller="
                    f"{controller!r}, query=...) finds it.")
        if rec.get("write") and not self.write:
            if self.changes_by.get(controller):
                return (f"Error: {rec['name']} changes the network, and you do not make changes: hand it to "
                        f"{self.changes_by[controller]} with `task`, saying exactly: call {rec['name']} on {controller} "
                        f"with {json.dumps(arguments or {})}. Do not try other tool names.")
            return (f"Error: {rec['name']} changes the network: call controller_write with the same arguments, "
                    f"so it waits for the operator's approval.")
        got = self._call(controller, rec, arguments or {}, config)
        return filter_answer(got, match) if match and not rec.get("write") else got


def _verb_schema(verb: str, write: bool, served: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    keys = sorted(served)
    return {"type": "object", "properties": {
        "controller": {"type": "string", "enum": keys,
                       "description": ("the controller to change" if write else
                                       "the controller to read; leave it out to ask every controller that serves it")},
        "arguments": {"type": "object", "description": "the controller's own arguments for it, by controller: " +
                      "; ".join(f"{k}: {_arg_summary(served[k].get('schema'))}" for k in keys)},
        **({} if write else {"match": {"type": "string", "description": (
            "only the items that mention these words, comma-separated (names, ids, a site, a type): "
            "for example 'xpdr-b2, xpdr-c2'. Use it to find the objects a question or change names in a long list.")}})},
        "required": ["controller"] if write else []}


_REL_GUARD = "[relations check]"
_PATHQ = re.compile(r"\b(how does .{1,60} (reach|get to|connect)|which .{0,40}(carr(y|ies)|rides?)|what (carries|rides)|"
                    r"carried by|rides? (on|over)|path|end[- ]to[- ]end|depend\w*|at risk|affect\w*|impact\w*|"
                    r"reach(es)? outside|goes? through)\b", re.I)


class RelationsFirst:
    """Built in AgenticMCPProvider.middleware(): a question about what carries what (a path, an impact, what is at
    risk) answered without follow_relations is sent back once. The model otherwise strings pieces together
    from separate reads and invents the joins (an optical hop for a tenant that never leaves packet)."""

    @staticmethod
    def make():
        from langchain.agents.middleware import AgentMiddleware, hook_config
        from langchain_core.messages import AIMessage, HumanMessage

        class _RelationsFirst(AgentMiddleware):
            @hook_config(can_jump_to=["model"])
            def after_model(self, state, runtime):
                from .grounding import anchored, current_question, is_keep, turn_start
                msgs = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
                if not msgs or not isinstance(msgs[-1], AIMessage) or msgs[-1].tool_calls or is_keep(msgs[-1].content):
                    return None
                q = current_question(msgs) or ""
                if not _PATHQ.search(q) or len(q) > 500:
                    return None
                turn = msgs[turn_start(msgs):]
                if any(isinstance(m, HumanMessage) and _REL_GUARD in str(m.content) for m in turn):
                    return None
                if any(isinstance(m, AIMessage) and any(c.get("name") in ("follow_relations", "task") for c in
                                                        (m.tool_calls or [])) for m in turn):
                    return None
                return {"jump_to": "model", "messages": [HumanMessage(content=anchored(
                    f"{_REL_GUARD} This asks what carries what. Call follow_relations from the object the question "
                    f"names (a tenant, a service, a lightpath) and answer from the chain it returns: name each piece "
                    f"and its domain as it returns them, and no piece it does not.", msgs))]}
        return _RelationsFirst()


_PROBE_GUARD = "[probe check]"
_REACHQ = re.compile(r"\b(can .{1,60} (reach|ping|talk to|get to|connect to)|ping\w*|reachab\w*|reach each other|"
                     r"connectivity|traceroute|packet loss|(is|are) .{1,40} (reachable|connected))\b", re.I)


class ProbeFirst:
    """Built in AgenticMCPProvider.middleware() when a controller serves ping: a question about whether traffic gets
    through, answered without sending any, is sent back once. Configured is not reachable: one run read the optical
    inventory for two minutes instead of pinging."""

    @staticmethod
    def make():
        from langchain.agents.middleware import AgentMiddleware, hook_config
        from langchain_core.messages import AIMessage, HumanMessage

        class _ProbeFirst(AgentMiddleware):
            @hook_config(can_jump_to=["model"])
            def after_model(self, state, runtime):
                from .grounding import anchored, current_question, is_keep, turn_start
                msgs = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
                if not msgs or not isinstance(msgs[-1], AIMessage) or msgs[-1].tool_calls or is_keep(msgs[-1].content):
                    return None
                q = current_question(msgs) or ""
                if not _REACHQ.search(q) or len(q) > 500:
                    return None
                turn = msgs[turn_start(msgs):]
                if any(isinstance(m, HumanMessage) and _PROBE_GUARD in str(m.content) for m in turn):
                    return None
                if any(isinstance(m, AIMessage) and any(c.get("name") in ("net_ping", "net_traceroute", "task")
                                                        for c in (m.tool_calls or [])) for m in turn):
                    return None
                return {"jump_to": "model", "messages": [HumanMessage(content=anchored(
                    f"{_PROBE_GUARD} This asks whether traffic gets through, and nothing was sent. Read the hosts "
                    f"(net_read_hosts), then net_ping from the host at one end to the other's address; if it is not "
                    f"answered, net_traceroute to see the hop where it stops, and follow_relations from there for the "
                    f"cause. Answer from what the probes returned.", msgs, keep=False))]}
        return _ProbeFirst()


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
                 service: Optional[AgenticMCP] = None, team: Optional[bool] = None):
        self.service = service or AgenticMCP(url, token)
        self.only = controllers
        # The multi-agent team: the supervisor reads and delegates, a config agent per network domain changes it.
        self.team = (os.getenv("CANAH_TEAM", "1") == "1") if team is None else team
        self.config_agents: List[Dict[str, Any]] = []
        self.roles: Dict[str, Dict[str, Any]] = {}
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
                                      f"it, through the agentic MCP; on each it is that controller's own tool: "
                                      + "; ".join(f"{k}: {r['name']}({_arg_summary(r.get('schema'))})"
                                                  + (f" - {str(r.get('description') or '')[:90].rstrip('.')}" if write else "")
                                                  for k, r in sorted(by.items())) + "."
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
                _ControllerCall(service=self.service, catalog=self.catalog, write=True, args_schema=_CALL_WRITE_SCHEMA, name="controller_write",
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
        if any(str((v or {}).get("domain") or "").lower() in ("optical", "wdm", "photonic") for v in held.values()):
            from .ip_optical import Planner
            planner = Planner(self)

            def ip_over_optical(a: str, z: str, rate_gbps: int = 100) -> str:
                """IP connectivity between two sites (or routers) over the optical network: the chain that carries it
                (routers, the ports that hand off to transponders, the lightpath, the IP link, the IP service, a host
                at each end), what is missing, and the steps to make it in order, each for the agent that owns it,
                then the ping that proves it. Reads only. Use it before setting up or troubleshooting IP between
                sites; follow its steps as given."""
                try:
                    return json.dumps(planner.plan(a, z, rate_gbps), default=str, indent=1)
                except Exception as e:  # noqa: BLE001
                    return f"Error: the plan could not be read: {type(e).__name__}: {e}"
            self._tools.append(StructuredTool.from_function(ip_over_optical))
        if self.team:
            self._build_team(held, served, rules)
            by = {k: a["name"] for a in self.config_agents for k in self.roles[a["name"]]["controllers"]}
            for t in self._tools + [t for a in self.config_agents for t in a["tools"]]:
                if isinstance(t, _CatalogTool):
                    t.changes_by = by
                    t.locate = self.locate

    def _build_team(self, held: Dict[str, Any], served: Dict[str, Dict[str, Dict[str, Any]]], rules: Dict[str, Any]) -> None:
        """One config agent per network domain (packet, optical, data centre, ...), from the domain each controller
        declares: the same tools as the supervisor's, cut to that domain's controllers, writes included."""
        by_domain: Dict[str, List[str]] = {}
        for k, v in held.items():
            if v.get("ok"):
                by_domain.setdefault(str(v.get("domain") or "network").lower(), []).append(k)
        for domain, keys in sorted(by_domain.items()):
            name = re.sub(r"[^a-z0-9]+", "-", domain).strip("-") + "-config"
            cat = {k: self.catalog[k] for k in keys}
            tools: List[BaseTool] = []
            for verb, by in sorted(served.items()):
                mine = {k: r for k, r in by.items() if k in keys}
                if not mine:
                    continue
                full = self.verb_tools[f"net_{verb}"]
                desc = (f"Contract verb {verb.replace('_', ' ')}; on each controller it is its own tool: "
                        + "; ".join(f"{k}: {r['name']}({_arg_summary(r.get('schema'))})"
                                    + (f" - {str(r.get('description') or '')[:120].rstrip('.')}" if full.write else "")
                                    for k, r in sorted(mine.items())) + "."
                        + (" It changes the network: it waits for the operator's approval." if full.write else ""))
                tools.append(VerbTool(service=self.service, catalog=cat, verb=verb, write=full.write,
                                      served={k: r["name"] for k, r in mine.items()}, name=full.name,
                                      args_schema=_verb_schema(verb, full.write, mine), description=desc))
            tools += [
                _SearchTool(service=self.service, catalog=cat, name="network_tools",
                            description=f"Find a {domain} controller's own tools beyond the contract verbs."),
                _ControllerCall(service=self.service, catalog=cat, name="controller_read",
                                description=f"Call one of a {domain} controller's own tools that reads."),
                _ControllerCall(service=self.service, catalog=cat, write=True, args_schema=_CALL_WRITE_SCHEMA, name="controller_write",
                                description=f"Call one of a {domain} controller's own tools that changes the network. "
                                            f"It waits for the operator's approval."),
            ]
            labels = ", ".join(f"{held[k].get('label') or k} ({k})" for k in keys)
            self.roles[name] = {"kind": "config", "domain": domain, "controllers": keys, "label": f"{domain} configuration"}
            self.config_agents.append({
                "name": name,
                "description": (f"The {domain} configuration agent: makes changes on the {domain} controllers ({labels}) "
                                f"through the agentic MCP with the contract's verbs (provision, teardown, adjust, reroute, "
                                f"configure a device) or a controller's own write tool, and reads them back. Delegate to it "
                                f"every change in the {domain} domain: say exactly what (controller, tool, each argument's "
                                f"value)."),
                "system_prompt": (f"You are CANAH's {domain} configuration agent. You change the {domain} network ({labels}) "
                                  f"through the agentic MCP and nothing else. First read only the objects the change names, "
                                  f"with match (for example net_read_devices(controller=..., match='xpdr-b2, xpdr-c2')): "
                                  f"an object of another domain that the change names (a packet service a tenant is to "
                                  f"reach, say) you will not find in yours: use its name as given. "
                                  f"a whole device list is long and you do not need it. Then make exactly the change you "
                                  f"were given with the tool that does it (network_tools(verb=...) when unsure which), "
                                  f"with the real arguments in the controller's own names, and every value the change names among them, the "
                                  f"optional ones too (a link or path to ride, a rate, a port): the call pauses for the operator's approval, which is the "
                                  f"confirmation (never ask in text). After it runs, read the object back and report "
                                  f"what you did and what you read. A change you were not given, or one in another "
                                  f"domain, you do not make: say so. Your write is checked in the digital twin before it "
                                  f"is sent: if it is held (reject, gather_more_data, ask_operator), do not retry it or "
                                  f"work around it; report the twin's reasons and questions as given, and stop. If the controller "
                                  f"answers that another step in your domain must come first (attach to a border leaf "
                                  f"first, say), do that step, then the change, each with its own approval. A change "
                                  f"may take several calls (attach a tenant, then connect it): make every one until the "
                                  f"change you were given is complete, then read it back."),
                "tools": tools})

    def middleware(self) -> List[Any]:
        self._load()
        names = {t.name for t in self._tools}
        return ([RelationsFirst.make()] if "follow_relations" in names else []) + \
               ([ProbeFirst.make()] if "net_ping" in names else [])

    _NAME_KEYS = ("name", "service", "service_id", "lightpath", "tenant", "id", "link", "device", "node", "node_id",
                  "router", "switch", "port", "interface")
    _DOWN_WORDS = {"down", "failed", "error", "out-of-service", "degraded", "disabled", "inactive"}

    def _records(self, controller: str) -> List[Dict[str, Any]]:
        """Every object a controller lists through the contract's reads (devices, links, services), flattened."""
        out: List[Dict[str, Any]] = []

        def walk(v):
            if isinstance(v, dict):
                if any(k in v for k in ("name", "id", "node-id", "interface", "tenant")):
                    out.append(v)
                for x in v.values():
                    if isinstance(x, (dict, list)):
                        walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)
        for verb in ("read_services", "read_devices", "read_links"):
            vt = self.verb_tools.get(f"net_{verb}")
            if vt is None or controller not in vt.served:
                continue
            try:
                got = self.service.call(controller, vt.served[controller], {}, approved_by="")
                walk(json.loads(got.get("text") or "null"))
            except Exception:  # noqa: BLE001 - a read that fails says nothing either way
                continue
        return out

    def verify(self, name: str, args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """After a write went through: does the network now show what was asked? Read back through the contract's
        reads, on any controller: the object the write names is there (or gone, for a removal), not down, and
        carries the values the write gave it. {"ok": True/False/None, "note", "controller", "tool", "missing"};
        None when there is nothing to check against."""
        self._load()
        target = self.write_target(name, args or {})
        if target is None:
            return None
        controller, tool, a = target
        rec = (self.catalog.get(controller) or {}).get(tool) or {}
        verb = str(rec.get("verb") or "")
        removing = verb == "teardown" or bool(re.search(r"delete|remove|teardown|destroy|detach|disconnect", tool, re.I))
        scalars = {k: v for k, v in (a or {}).items() if isinstance(v, (str, int, float)) and not isinstance(v, bool)
                   and str(v).strip()}
        names = [k for k in self._NAME_KEYS if isinstance(scalars.get(k), str)] + \
            [k for k, v in scalars.items() if isinstance(v, str) and k not in self._NAME_KEYS]
        if not names:
            return None

        def find(rows, subject):
            for r in rows:
                if any(isinstance(v, (str, int)) and str(v).lower() == subject.lower() for v in r.values()):
                    return r
            return None
        rows = self._records(controller)
        # The subject is the named value that is an object on this controller (connect_tenant_external(tenant,
        # service): the tenant is the fabric's, the service another domain's); for a creation, the first name.
        key = next((k for k in names if find(rows, str(scalars[k])) is not None), None)
        if key is None or (verb == "provision" and "name" in scalars and key != "name"):
            key = "name" if "name" in scalars else names[0]
        subject = str(scalars[key])
        if not rows:
            return {"ok": None, "controller": controller, "tool": tool, "missing": [],
                    "note": f"[Not verified: {controller} listed nothing to read {subject} back from.]"}
        obj = find(rows, subject)
        if obj is None and not removing:
            time.sleep(1.0)                     # a controller may take a moment to list what it just made
            obj = find(self._records(controller), subject)
        if removing:
            ok = obj is None
            return {"ok": ok, "controller": controller, "tool": tool, "missing": [] if ok else [subject],
                    "note": (f"[Verified by the harness: {subject} is no longer listed on {controller}.]" if ok else
                             f"[Not as intended: {subject} is still listed on {controller} after {tool}. Say so; "
                             f"propose the correction, which waits for approval.]")}
        if obj is None:
            return {"ok": False, "controller": controller, "tool": tool, "missing": [subject],
                    "note": (f"[Not as intended: after {tool}, {controller} lists no {subject}, though it answered the write "
                             f"as done. Report that now: what was sent, what {controller} answered, and that reading it "
                             f"back shows no {subject}. Do not report it as done, and do not search further for the cause "
                             f"unless the operator asks.]")}
        values: List[Any] = []

        def collect(v):
            if isinstance(v, dict):
                for x in v.values():
                    collect(x)
            elif isinstance(v, list):
                for x in v:
                    collect(x)
            else:
                values.append(v)
        collect(obj)

        def shows(v) -> bool:
            if isinstance(v, (int, float)) or re.fullmatch(r"-?\d+(\.\d+)?", str(v)):
                return any(isinstance(x, (int, float, str)) and re.fullmatch(r"-?\d+(\.\d+)?", str(x))
                           and float(x) == float(v) for x in values)
            pat = re.compile(rf"(?<![\w-]){re.escape(str(v).lower())}(?![\w-])")
            return any(isinstance(x, str) and pat.search(x.lower()) for x in values)
        missing = [f"{k}={v}" for k, v in scalars.items() if k != key and not shows(v)]
        down = [f"{k}={v}" for k, v in obj.items() if k in ("status", "state", "oper-status", "oper_status")
                and str(v).lower() in self._DOWN_WORDS]
        if not missing and not down:
            shown = ", ".join(f"{k} {v}" for k, v in list(obj.items())[:6] if isinstance(v, (str, int, float)))
            return {"ok": True, "controller": controller, "tool": tool, "missing": [],
                    "note": f"[Verified by the harness: {controller} now lists {subject} ({shown[:240]}).]"}
        return {"ok": False, "controller": controller, "tool": tool, "missing": missing + down,
                "note": (f"[Not as intended: {controller} lists {subject}, but "
                         + "; ".join(([f"it does not show {', '.join(missing)}"] if missing else [])
                                     + ([f"it is {', '.join(down)}"] if down else []))
                         + ". Report that now, with the correction or a rollback proposed (it waits for approval). "
                           "Do not report it as done as asked.]")}

    def locate(self, name: str) -> List[str]:
        """The controllers whose devices, links or services mention this name (reads, cached for 30 s)."""
        self._load()
        now = time.time()
        cache = getattr(self, "_seen_cache", None)
        if cache is None or now - cache[0] > 30:
            seen: Dict[str, str] = {}
            for verb in ("read_devices", "read_links", "read_services"):
                vt = self.verb_tools.get(f"net_{verb}")
                if vt is None:
                    continue
                for k, tool in vt.served.items():
                    try:
                        got = self.service.call(k, tool, {}, approved_by="")
                        seen[k] = seen.get(k, "") + str(got.get("text", "")).lower()
                    except Exception:  # noqa: BLE001
                        continue
            cache = (now, seen)
            if seen:
                self._seen_cache = cache
        n = name.lower()
        return [k for k, text in cache[1].items() if re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", text)]

    def agent_roles(self) -> Dict[str, Dict[str, Any]]:
        self._load()
        return dict(self.roles)

    def subagents(self) -> List[Dict[str, Any]]:
        self._load()
        return list(self.config_agents)

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
        """With the team, the supervisor keeps every read and no write: changes are its config agents'."""
        self._load()
        if self.team and self.config_agents:
            return [t.name for t in self._tools if t.name not in self._writes]
        return [t.name for t in self._tools]

    def instructions(self) -> str:
        self._load()
        if not self.status.get("ok"):
            return (f"The networks are reached through the agentic MCP at {self.service.url}, which is not answering "
                    f"now ({self.status.get('error')}). Say so when a request needs the network.")
        rows = [f"{k} ({v.get('label')}, {v.get('domain') or 'network'})" for k, v in self.status.get("controllers", {}).items()
                if v.get("ok")]
        if self.team and self.config_agents:
            def changes(name: str) -> str:
                keys = self.roles[name]["controllers"]
                tools = sorted({t for k in keys for t, r in self.catalog.get(k, {}).items() if r.get("write")})
                return f" (its changes: {', '.join(tools[:14])}{' …' if len(tools) > 14 else ''})" if tools else ""
            agents = "; ".join(f"{a['name']} for {', '.join(self.roles[a['name']]['controllers'])}{changes(a['name'])}"
                               for a in self.config_agents)
            return (f"Networks are reached through the agentic MCP, which holds these SDN controllers: {', '.join(rows) or 'none'}. "
                    f"You are the supervisor of a team. You read the networks yourself (the net_read_ tools; a read without a "
                    f"controller asks every one, and match='name, name' keeps only the objects you are after) and you do not change them. Your agents, each called with `task`:\n"
                    f"- qot: the quality-of-transmission agent. It watches quality and health (margins, OSNR, availability, "
                    f"delay, loss, incidents, trends), rehearses what-ifs and changes in the digital twin, and proposes the "
                    f"exact configuration to apply, with the twin's decision. Give it any what-if, any 'is this safe', any "
                    f"degradation and quality question; not a plain change (the write gate rehearses that).\n"
                    f"- the config agents, one per network domain: {agents}. A change in a domain is made only by its config "
                    f"agent: give it the exact change as the controller's tool takes it (controller, tool, each argument's "
                    f"value, e.g. over='L-ab'), not a description. Its call waits for the operator's approval.\n"
                    f"To know whether traffic gets through, send some: net_ping and net_traceroute from the hosts the network "
                    f"has (net_read_hosts), where a controller offers them; a service configured is not proof. IP between sites over "
                    f"optical: ip_over_optical(a, z) gives the chain, what is missing and the steps in order (the lightpath "
                    f"to the optical config agent first, then the IP service to the packet one, then the ping). "
                    f"For a change: first find whose it is. The domain is the one whose controller has the tool that does it: "
                    f"network_tools(query=<the change's words>) with no controller searches them all (attaching a tenant is "
                    f"the data centre's, a lightpath the optical one's, a VLAN service the packet one's). A change that "
                    f"needs steps in two domains goes to each in turn, in the order the design rules give. When the "
                    f"operator names the change exactly, hand it on at once: the config agent reads what it needs, and "
                    f"the controller and the twin's gate check resources (spectrum, ports, rates), so you do not. "
                    f"Hand it to the domain's config agent with the exact change. Its write is "
                    f"rehearsed in the digital twin automatically before it is sent (the write gate) and then waits for "
                    f"the operator's approval, so do not ask qot to rehearse it as well. Ask qot only for what-ifs, 'is it "
                    f"safe', quality questions, or when the gate held a write and the operator wants to know more. A "
                    f"change across domains goes to each domain's config agent in turn.")
        return (f"Networks are reached through the agentic MCP, which holds these SDN controllers: "
                f"{', '.join(rows) or 'none'}. You work them all yourself, with the same tools for every controller: "
                f"a net_<verb> tool per contract verb (net_read_devices, net_read_links, net_provision, ...), with the "
                f"controller as an argument; a read without a controller asks every controller that serves it, which "
                f"is how to answer across controllers; match='name, name' keeps only the objects you are after in a long list. For anything beyond the contract verbs, find the controller's "
                f"own tool with network_tools, then call it with controller_read, or controller_write for a change. "
                f"Read before you answer and quote what the controllers returned. A change (a net_ write verb or "
                f"controller_write) waits for the operator's approval: call it with the exact arguments and the "
                f"pause asks for you.")


def provider() -> AgenticMCPProvider:
    """For `providers: [canah.network:provider]` in canah.yaml."""
    return AgenticMCPProvider()
