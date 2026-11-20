"""
MCP servers, from an mcp.json in the format Claude Desktop, Cursor and most
clients use:

  {"mcpServers": {
     "controllers": {"url": "http://localhost:8400/mcp", "transport": "streamable_http",
                     "headers": {"Authorization": "Bearer ${AGENTIC_MCP_TOKEN}"}},
     "files": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/srv"]}}}

`${VAR}` in a string is filled from the environment, so no secret sits in the
file. A server with `"enabled": false` is skipped.

MCP tools are read-only by default. A tool whose MCP annotations say it is not
read-only, or whose name looks like a change (create, delete, set, ...), is
kept only when `mcp_write_tools` names it, and then it always asks a human
(approval.py). Each tool is prefixed with its server's name.
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from langchain_core.tools import BaseTool

logger = logging.getLogger("canah.mcp")

_WRITE_WORDS = re.compile(r"(^|_)(create|add|delete|remove|set|update|patch|put|post|write|apply|provision|"
                          r"configure|deploy|restart|reboot|kill|drop|insert|modify|commit|push|send|enable|disable)"
                          r"(_|$)", re.I)


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return re.sub(r"\$\{(\w+)\}", lambda m: os.getenv(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def connections(path: str) -> Dict[str, Dict[str, Any]]:
    """The servers in an mcp.json, as langchain-mcp-adapters connections."""
    if not path:
        return {}
    raw = json.loads(Path(path).expanduser().read_text())
    servers = raw.get("mcpServers", raw)
    out: Dict[str, Dict[str, Any]] = {}
    for name, spec in servers.items():
        spec = _expand(dict(spec))
        if spec.pop("enabled", True) is False:
            continue
        if "command" in spec:
            spec.setdefault("transport", "stdio")
        elif "url" in spec:
            t = spec.get("transport") or spec.pop("type", None) or (
                "sse" if spec["url"].rstrip("/").endswith("/sse") else "streamable_http")
            spec["transport"] = {"http": "streamable_http", "streamable-http": "streamable_http"}.get(t, t)
        else:
            raise ValueError(f"{path}: server {name!r} has neither a command nor a url")
        out[name] = spec
    return out


def is_write(tool: BaseTool) -> bool:
    ann = (getattr(tool, "metadata", None) or {}).get("annotations") or {}
    if isinstance(ann, dict) and "readOnlyHint" in ann:
        return not ann["readOnlyHint"]
    if hasattr(ann, "readOnlyHint") and ann.readOnlyHint is not None:
        return not ann.readOnlyHint
    return bool(_WRITE_WORDS.search(tool.name.split("__")[-1]))


def _allowed(name: str, patterns: List[str]) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def split(tools: List[BaseTool], write_patterns: List[str]) -> Tuple[List[BaseTool], List[str], List[str]]:
    """(tools kept, names of kept writes, names dropped as unallowed writes)."""
    kept, writes, dropped = [], [], []
    for t in tools:
        if is_write(t):
            if _allowed(t.name, write_patterns):
                kept.append(t)
                writes.append(t.name)
            else:
                dropped.append(t.name)
        else:
            kept.append(t)
    return kept, writes, dropped


async def aload(path: str, write_patterns: List[str]) -> Dict[str, Any]:
    conns = connections(path)
    if not conns:
        return {"tools": [], "writes": [], "dropped": [], "servers": {}}
    from langchain_mcp_adapters.client import MultiServerMCPClient
    tools: List[BaseTool] = []
    servers: Dict[str, Any] = {}
    for name, conn in conns.items():
        client = MultiServerMCPClient({name: conn}, tool_name_prefix=True)
        try:
            got = await client.get_tools()
            servers[name] = {"ok": True, "tools": len(got)}
            tools += got
        except Exception as e:  # noqa: BLE001 - one server down does not stop the agent
            logger.warning("MCP server %s unavailable: %s", name, e)
            servers[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    kept, writes, dropped = split(tools, write_patterns)
    if dropped:
        logger.info("MCP write tools left out (not in mcp_write_tools): %s", ", ".join(dropped))
    return {"tools": kept, "writes": writes, "dropped": dropped, "servers": servers}


def load(path: str, write_patterns: List[str]) -> Dict[str, Any]:
    """`aload`, from synchronous code."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(aload(path, write_patterns))
    raise RuntimeError("canah.mcp.load called inside an event loop: await canah.mcp.aload instead")
