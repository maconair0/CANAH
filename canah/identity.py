"""
Who is asking: observer, admin or root.

  observer   no password. Reads only: the model is never offered a tool that
             changes anything, and no change can be approved.
  admin      signs in with a password. May approve changes.
  root       signs in with a password. May approve everything, including the
             tools marked root-only (settings.root_tools).

Three checks enforce it, not one:
  1. RoleMiddleware takes the tools above the caller's level out of the model's
     choice, on the main agent and in every subagent.
  2. CanahAgent.decide() refuses a decision from below the level the tool needs.
  3. RoleMiddleware refuses, at the moment of execution, a call above the level
     of whoever's run it is.

Accounts come from the environment, as plaintext or "sha256:<hex>":

  CANAH_ADMIN_USER / CANAH_ADMIN_PASSWORD      default user "admin"
  CANAH_ROOT_USER  / CANAH_ROOT_PASSWORD       default user "root"

Without them, documented defaults apply and a warning says so: they keep an
accidental change behind a deliberate act, they do not keep anyone out.
"""
from __future__ import annotations

import fnmatch
import hashlib
import hmac
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

logger = logging.getLogger("canah.identity")

LEVELS = ("observer", "admin", "root")
MAX_FAILURES = int(os.getenv("CANAH_MAX_SIGNIN_FAILURES", "5"))
LOCKOUT_S = int(os.getenv("CANAH_SIGNIN_LOCKOUT_S", "300"))
_LAB_DEFAULTS = {"admin": "admin123", "root": "toor123"}


def rank(level: str) -> int:
    try:
        return LEVELS.index(level)
    except ValueError:
        raise ValueError(f"a level is one of {LEVELS}, not {level!r}")


@dataclass(frozen=True)
class Identity:
    user: str
    level: str = "observer"

    def can(self, needed: str) -> bool:
        return rank(self.level) >= rank(needed)

    @property
    def label(self) -> str:
        return self.user if self.user == self.level else f"{self.user} ({self.level})"


OBSERVER = Identity("observer", "observer")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _digest(level: str) -> tuple:
    got = os.getenv(f"CANAH_{level.upper()}_PASSWORD", "").strip()
    if got:
        return (got[7:] if got.startswith("sha256:") else _sha256(got)), False
    return _sha256(_LAB_DEFAULTS[level]), True


def hash_password(password: str, iterations: int = 200_000) -> str:
    salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iterations).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def _matches(password: str, stored: str) -> bool:
    if stored.startswith("pbkdf2_sha256$"):
        _, it, salt, digest = stored.split("$", 3)
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(it)).hex()
        return hmac.compare_digest(got, digest)
    return hmac.compare_digest(_sha256(password), stored)


class Accounts:
    """Accounts and sign-in with a lockout after repeated failures.

    Users in `<CANAH_HOME>/users.json` ({name: {level, hash}}, PBKDF2 hashes), managed with
    `python -m canah user add|list|remove`, plus the admin and root accounts from the environment."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join(os.path.expanduser(os.getenv("CANAH_HOME", "canah_data")), "users.json")
        self.users: Dict[str, Dict[str, Any]] = {}
        for level in ("admin", "root"):
            user = os.getenv(f"CANAH_{level.upper()}_USER", level)
            digest, default = _digest(level)
            self.users[user] = {"level": level, "digest": digest, "default": default}
            if default:
                logger.warning("%s signs in with the documented default password; set CANAH_%s_PASSWORD",
                               user, level.upper())
        for name, a in self._file().items():
            self.users[name] = {"level": a["level"], "digest": a["hash"], "default": False}
        self._fails: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def _file(self) -> Dict[str, Dict[str, str]]:
        import json
        try:
            with open(self.path) as fh:
                got = json.load(fh)
            return {k: v for k, v in got.items() if isinstance(v, dict) and v.get("level") in LEVELS}
        except (OSError, ValueError):
            return {}

    def _save(self, users: Dict[str, Dict[str, str]]) -> None:
        import json
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(users, fh, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def add(self, user: str, level: str, password: str) -> None:
        if level not in ("admin", "root"):
            raise ValueError("an account is admin or root (an observer needs none)")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{2,40}", user or ""):
            raise ValueError("a user name is 2-40 letters, digits, . _ -")
        if len(password or "") < 8:
            raise ValueError("a password is at least 8 characters")
        users = self._file()
        users[user] = {"level": level, "hash": hash_password(password)}
        self._save(users)
        self.users[user] = {"level": level, "digest": users[user]["hash"], "default": False}

    def remove(self, user: str) -> bool:
        users = self._file()
        gone = users.pop(user, None) is not None
        if gone:
            self._save(users)
            self.users.pop(user, None)
        return gone

    def levels(self) -> Dict[str, str]:
        return {u: a["level"] for u, a in self.users.items()}

    def sign_in(self, user: str, password: str) -> Identity:
        """The identity for a user and password; PermissionError otherwise."""
        user = str(user or "").strip()
        now = time.time()
        with self._lock:
            recent = [t for t in self._fails.get(user, []) if now - t < LOCKOUT_S]
            self._fails[user] = recent
            if len(recent) >= MAX_FAILURES:
                raise PermissionError(f"{user} is locked for {int(LOCKOUT_S - (now - recent[0]))} s after "
                                      f"{MAX_FAILURES} wrong passwords")
        account = self.users.get(user)
        if account is None or not _matches(str(password), account["digest"]):
            with self._lock:
                self._fails.setdefault(user, []).append(now)
            raise PermissionError("wrong user or password")
        with self._lock:
            self._fails.pop(user, None)
        return Identity(user, account["level"])


def required(tool: str, writes: Iterable[str], root_tools: Iterable[str]) -> str:
    """The level a tool needs: root for a root-only one, admin for any write, else observer."""
    if any(fnmatch.fnmatch(tool, p) for p in root_tools):
        return "root"
    return "admin" if tool in set(writes) else "observer"


def from_config(config: Optional[Dict[str, Any]]) -> Identity:
    c = (config or {}).get("configurable") or {}
    level = c.get("level") if c.get("level") in LEVELS else "observer"
    return Identity(str(c.get("user_id") or level), level)


class RoleMiddleware(AgentMiddleware):
    """Offers the model only the tools the caller's level may use, and refuses
    at execution a call above it (checks 1 and 3)."""

    def __init__(self, writes: Iterable[str], root_tools: Iterable[str] = ()):
        super().__init__()
        self.writes = set(writes)
        self.root_tools = list(root_tools)

    def _who(self) -> Identity:
        try:
            from langgraph.config import get_config
            return from_config(get_config())
        except Exception:  # noqa: BLE001 - outside a run: the narrowest view
            return OBSERVER

    def _allowed(self, name: str, who: Identity) -> bool:
        return who.can(required(name, self.writes, self.root_tools))

    def _narrow(self, request):
        who = self._who()
        keep = [t for t in request.tools if self._allowed(getattr(t, "name", None) or
                                                          (t.get("name") if isinstance(t, dict) else ""), who)]
        # Told whenever the system has changes this person cannot approve, not only
        # when a tool was taken away here: the writes often live in subagents.
        if len(keep) == len(request.tools) and (who.can("admin") or not self.writes):
            return request
        if not who.can("admin"):
            note = (f"The person you are talking to is signed in as {who.label} and cannot change the system or "
                    f"approve a change, so you have no tool that changes anything in this conversation. Reading is not "
                    f"a change: look up anything they ask about with your tools and subagents, as for anyone. If they ask "
                    f"for a change, you cannot make it or propose it in this session, and offering to ('shall I proceed?') is "
                    f"wrong. Reply with: the change that would be needed, exactly, and that someone signed in as admin "
                    f"(or root) has to ask for it. Nothing else.")
        else:
            note = (f"The person you are talking to is signed in as {who.label}. Some changes need root and are not "
                    f"available here; if one of those is asked for, say it needs someone signed in as root.")
        from langchain_core.messages import SystemMessage
        sm = request.system_message
        content = "" if sm is None else (sm.content if isinstance(sm.content, str) else str(sm.content))
        return request.override(tools=keep, system_message=SystemMessage(content=(content + "\n\n" + note).strip()))

    def wrap_model_call(self, request, handler: Callable):
        return handler(self._narrow(request))

    async def awrap_model_call(self, request, handler: Callable):
        return await handler(self._narrow(request))

    def _refusal(self, request) -> Optional[ToolMessage]:
        call = request.tool_call
        who = self._who()
        need = required(call["name"], self.writes, self.root_tools)
        if who.can(need):
            return None
        return ToolMessage(content=f"Refused: {call['name']} needs {need}; this run is {who.label}.",
                           tool_call_id=call["id"], name=call["name"], status="error")

    def wrap_tool_call(self, request, handler: Callable):
        return self._refusal(request) or handler(request)

    async def awrap_tool_call(self, request, handler: Callable):
        return self._refusal(request) or await handler(request)
