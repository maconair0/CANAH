"""
What the agent knows about itself and about what is happening: its own harness, the incidents the watcher
opened, and the networks' history (a ToolProvider).

  canah_status        who is asking and what they may do, the controllers in reach, the digital twin, the model,
                      open incidents and what the harness already does (roles, approvals, audit, watcher, ...)
  incidents           the incidents open now (or recent), with their runbook and investigation
  network_changes     what changed in the networks: devices, links and services added, removed, up or down
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool

HARNESS = """What this harness already does (so never say these are missing, and never try to build them yourself):
- roles: observer (reads only), admin (approves changes), root (approves everything, manages accounts, the model,
  the agentic MCP's token and contract); an account may be limited to some controllers (its scope)
- approvals: every tool that changes a network pauses for a person with the right role; the pause IS the request
  for approval, and the web page's Approvals page queues them
- audit: every proposal, decision and execution is logged with who, when and the arguments (Approvals & audit)
- a watcher reads every controller through the contract each minute: a controller unreachable, a device, link or
  service down, an alarm become incidents, notified to the configured channels (webhook, Slack, Teams, email)
- runbooks respond to incidents: investigate, or rehearse a fix in the digital twin and propose it for approval
- history: what each network held, kept when it changes (network_changes)
- a digital twin rehearses changes first; a write is checked there before it is sent
- access: the web console, an HTTP API and a CLI (python -m canah ask|chat|audit|metrics|user)

Not built yet (say so when it matters; see docs/ROADMAP.md):
- traffic counters over time (health_over_time keeps counts of what is up and down, not traffic)
- applying a fix with no person, even under a policy: every fix waits for approval
- several agentic MCPs (sites) behind one CANAH
- calibration of the twin on this lab's measurements for every domain (uncalibrated twins answer at trust tier C)"""


class OperationsProvider:
    def __init__(self, home: Any, settings: Any, describe: Callable[[], Dict[str, Any]]):
        self.home = Path(home)
        self.settings = settings
        self.describe = describe
        self._tools = [StructuredTool.from_function(f) for f in (self.canah_status, self.incidents, self.network_changes,
                                                                    self.health_over_time)]

    def _store(self):
        from .incidents import Incidents
        return Incidents(self.home / "incidents.sqlite")

    def canah_status(self, config: RunnableConfig) -> str:
        """This harness and this moment: who is asking and what they may approve, the controllers in reach, the
        digital twin and the model in use, open incidents, recent network changes, and what the harness already
        does (roles, approvals, audit, watcher, runbooks, history). Use it when asked what you can do, about
        yourself, or how things stand."""
        from .identity import from_config
        who = from_config(config)
        try:
            inc = self._store().counts(who.controllers)
        except Exception:  # noqa: BLE001
            inc = {"active": None}
        try:
            from .history import History
            changes = len(History(self.home).changes(since_s=86400))
        except Exception:  # noqa: BLE001
            changes = None
        d = self.describe()
        attention = []
        chans = list(getattr(self.settings, "notify_channels", None) or []) + ([1] if getattr(self.settings, "notify_url", "") else [])
        if not chans:
            attention.append("no notification channel is set: incidents and waiting approvals reach no one outside the page "
                             "(notify_channels in canah.yaml)")
        if d.get("controllers_down"):
            attention.append(f"controllers not answering: {', '.join(d['controllers_down'])}")
        if inc.get("active"):
            attention.append(f"{inc['active']} incident(s) open")
        if not getattr(self.settings, "watch", True):
            attention.append("the watcher is switched off")
        return json.dumps({
            "you": {"user": who.user, "level": who.level,
                    "may": {"observer": "read only", "admin": "read, and approve changes",
                            "root": "everything, including root-only tools, accounts, the model and the contract"}[who.level],
                    "scope": list(who.controllers) or "every controller"},
            "controllers": [k for k in d.get("controllers", []) if who.sees(k)],
            "digital_twin": d.get("twin"), "model": d.get("model"),
            "incidents_active": inc, "network_changes_24h": changes,
            "watcher": {"on": bool(getattr(self.settings, "watch", True)), "every_s": getattr(self.settings, "watch_interval_s", 60)},
            "needs_attention": attention or ["nothing"],
            "harness": HARNESS,
            "when_asked_what_is_missing": "be balanced: what the harness has, what it does not have yet (the list above), "
                                          "and what needs attention now. Never say it is complete."}, indent=1, default=str)

    def incidents(self, config: RunnableConfig, status: str = "active", limit: int = 20) -> str:
        """The incidents the watcher opened: status "active" (open or acknowledged), "resolved" or "" for all; each
        with its controller, severity, what was seen, since when, its runbook and the investigation's report."""
        from .identity import from_config
        who = from_config(config)
        rows = self._store().list(status if status in ("active", "open", "acked", "resolved", "") else "active",
                                  max(1, min(int(limit or 20), 100)), who.controllers)
        now = time.time()
        return json.dumps([{"id": r["id"], "severity": r["severity"], "status": r["status"], "controller": r["controller"],
                            "kind": r["kind"], "title": r["title"], "open_for_s": int((r["resolved_at"] or now) - r["first_seen"]),
                            "seen": r["count"], "runbook": r["runbook"], "report": (r["report"] or "")[:1200] or None}
                           for r in rows], indent=1) if rows else "No incidents."

    def network_changes(self, config: RunnableConfig, controller: str = "", since_hours: float = 24.0) -> str:
        """What changed in the networks over the last hours: devices, links and services added or removed, and
        status changes (up, down), per controller and time, newest first. From the history the watcher keeps."""
        from .history import History
        from .identity import from_config
        who = from_config(config)
        if controller and not who.sees(controller):
            return f"Error: {controller} is outside what this operator works with."
        rows = [r for r in History(self.home).changes(controller, float(since_hours) * 3600) if who.sees(r["controller"])]
        if not rows:
            return f"No change recorded in the last {since_hours:g} h" + (f" on {controller}" if controller else "") + \
                   " (the history starts when the watcher first read each network)."
        return json.dumps([{"at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["at"])), "controller": r["controller"],
                            "changes": r["changes"][:40]} for r in rows[:30]], indent=1)

    def health_over_time(self, config: RunnableConfig, controller: str = "", hours: float = 24.0) -> str:
        """Each controller's health over the last hours, from the watcher's readings: devices, links and services
        (how many, how many down) and incidents open, now, at worst and at best, and whether it was reachable.
        For trends and "how has it been"."""
        from .health import Series
        from .identity import from_config
        who = from_config(config)
        got = {k: v for k, v in Series(self.home).get(float(hours), controller).items() if who.sees(k)}
        if not got:
            return f"No readings in the last {hours:g} h (the watcher records one each minute while the service runs)."
        out = {}
        for k, v in got.items():
            last = v[-1]
            out[k] = {"readings": len(v), "reachable_share": round(sum(x["reachable"] for x in v) / len(v), 3),
                      **{f: {"now": last[f], "max": max(x[f] for x in v), "min": min(x[f] for x in v)}
                         for f in ("devices_down", "links_down", "services_down", "incidents")},
                      "devices": last["devices"], "links": last["links"], "services": last["services"],
                      "since": time.strftime("%Y-%m-%d %H:%M", time.localtime(v[0]["at"]))}
        return json.dumps(out, indent=1)

    def tools(self) -> List[BaseTool]:
        return list(self._tools)

    def writes(self) -> List[str]:
        return []

    def instructions(self) -> str:
        return ("A question about CANAH itself (what you can do, your build, what you have or lack, your role or the "
                "operator's) is about the harness, not about the networks: answer it from canah_status, covering what the "
                "harness has, what it does not have yet, and what needs attention now. Use incidents for what is wrong in "
                "the networks now, network_changes for what changed and when, and health_over_time for trends.")
