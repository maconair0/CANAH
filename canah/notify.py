"""
Telling people: an incident opened or resolved, a change waiting for approval.

Channels come from the settings (`notify_channels` in canah.yaml); a secret is always the NAME of an
environment variable, never the value:

  - {kind: webhook, url_env: OPS_WEBHOOK_URL}                    JSON POST {event, severity, title, text, ...}
  - {kind: slack,   url_env: SLACK_WEBHOOK_URL, min_severity: major}
  - {kind: teams,   url_env: TEAMS_WEBHOOK_URL}
  - {kind: email,   to: [noc@example.org], min_severity: critical} SMTP from CANAH_SMTP_HOST, CANAH_SMTP_PORT,
                                                                  CANAH_SMTP_USER, CANAH_SMTP_PASSWORD, CANAH_SMTP_FROM
  events: [incident_opened, incident_resolved, approval_waiting]  (default: all three)

`notify_url` (the older single webhook setting) is a webhook channel for every event.
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import threading
from email.message import EmailMessage
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("canah.notify")

SEVERITIES = ("info", "warning", "minor", "major", "critical")
EVENTS = ("incident_opened", "incident_resolved", "approval_waiting")


def rank(severity: str) -> int:
    s = str(severity or "").lower()
    return SEVERITIES.index(s) if s in SEVERITIES else 2


class Notifier:
    def __init__(self, channels: List[Dict[str, Any]], notify_url: str = "", console_url: str = ""):
        self.channels = [dict(c) for c in channels or []]
        if notify_url:
            self.channels.append({"kind": "webhook", "url": notify_url, "name": "notify_url"})
        self.console_url = console_url.rstrip("/")
        self.sent: List[Dict[str, Any]] = []          # the last few, for the page
        self._lock = threading.Lock()

    def describe(self) -> List[Dict[str, Any]]:
        """The channels, as the page shows them: no secret, only whether it is set."""
        out = []
        for c in self.channels:
            kind = c.get("kind", "webhook")
            ready = bool(c.get("url") or (c.get("url_env") and os.getenv(c["url_env"]))) if kind != "email" else \
                bool(os.getenv("CANAH_SMTP_HOST") and c.get("to"))
            out.append({"kind": kind, "name": c.get("name") or c.get("url_env") or kind, "min_severity": c.get("min_severity", "info"),
                        "events": c.get("events") or list(EVENTS), "ready": ready,
                        "to": c.get("to") if kind == "email" else None})
        return out

    def send(self, event: str, severity: str, title: str, text: str, link: str = "", data: Optional[Dict[str, Any]] = None,
             wait: bool = False) -> List[Dict[str, Any]]:
        """Send to every channel that takes this event at this severity. In the background unless wait=True."""
        results: List[Dict[str, Any]] = []

        def go():
            for c in self.channels:
                if event not in (c.get("events") or EVENTS) or rank(severity) < rank(c.get("min_severity", "info")):
                    continue
                try:
                    self._one(c, event, severity, title, text, link, data or {})
                    results.append({"channel": c.get("name") or c.get("kind"), "ok": True})
                except Exception as e:  # noqa: BLE001 - a channel that fails is logged, the rest still go
                    logger.warning("notification by %s failed: %s", c.get("kind"), e)
                    results.append({"channel": c.get("name") or c.get("kind"), "ok": False, "error": f"{type(e).__name__}: {e}"[:200]})
            with self._lock:
                import time
                self.sent.append({"at": time.time(), "event": event, "severity": severity, "title": title, "results": results})
                del self.sent[:-50]
        if wait:
            go()
        else:
            threading.Thread(target=go, name="canah-notify", daemon=True).start()
        return results

    def _one(self, c: Dict[str, Any], event: str, severity: str, title: str, text: str, link: str, data: Dict[str, Any]):
        kind = c.get("kind", "webhook")
        url = c.get("url") or (os.getenv(c["url_env"], "") if c.get("url_env") else "")
        link = link if link.startswith("http") else (self.console_url + link if self.console_url and link else link)
        head = f"[{severity.upper()}] {title}"
        if kind == "email":
            host = os.getenv("CANAH_SMTP_HOST", "")
            if not host or not c.get("to"):
                raise RuntimeError("CANAH_SMTP_HOST and the channel's `to` are needed")
            msg = EmailMessage()
            msg["Subject"], msg["From"] = f"CANAH: {head}", os.getenv("CANAH_SMTP_FROM", "canah@localhost")
            msg["To"] = ", ".join(c["to"] if isinstance(c["to"], list) else [c["to"]])
            msg.set_content(f"{text}\n\n{link}".strip())
            port = int(os.getenv("CANAH_SMTP_PORT", "587"))
            with smtplib.SMTP(host, port, timeout=20) as s:
                if port != 25:
                    s.starttls()
                if os.getenv("CANAH_SMTP_USER"):
                    s.login(os.getenv("CANAH_SMTP_USER", ""), os.getenv("CANAH_SMTP_PASSWORD", ""))
                s.send_message(msg)
            return
        if not url:
            raise RuntimeError(f"no URL ({c.get('url_env') or 'url'} is not set)")
        if kind == "slack":
            body = {"text": f"*{head}*\n{text}" + (f"\n<{link}|Open in CANAH>" if link else "")}
        elif kind == "teams":
            body = {"@type": "MessageCard", "@context": "https://schema.org/extensions", "summary": head,
                    "themeColor": {"critical": "D13438", "major": "F7630C", "minor": "FFB900"}.get(severity, "0078D4"),
                    "title": head, "text": text + (f"\n\n[Open in CANAH]({link})" if link else "")}
        else:
            body = {"source": "canah", "event": event, "severity": severity, "title": title, "text": text, "link": link, **data}
        r = requests.post(url, json=body, timeout=15)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
