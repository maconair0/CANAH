"""
Which digital twins CANAH knows and which one is in use.

A twin is declared in canah.yaml (`twins:`) or added from the web page; the page's changes and the choice
of active twin are kept in CANAH_HOME/twins.json. An entry is one of:

  {"name": "netwins", "kind": "python", "spec": "netwins.contract:twin", "path": "~/netwins"}
      an object made in this process: `spec` is "module:factory" (or "module:object"); `path` is put on
      sys.path first, so a twin need not be installed

  {"name": "lab-twin", "kind": "http", "url": "http://10.0.0.9:8420", "token_env": "LAB_TWIN_TOKEN"}
      a service that speaks the contract over HTTP; `token_env` names the environment variable holding its
      bearer token (the token itself is never written to a file here)

Every call goes through `contract.checked`: an answer outside the contract is the twin's error.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import contract as C

_NAME = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")


class PythonTwin:
    def __init__(self, entry: Dict[str, Any]):
        self.entry = entry
        self._obj = None
        self._lock = threading.Lock()

    def _get(self):
        with self._lock:
            if self._obj is None:
                path = self.entry.get("path")
                if path:
                    p = str(Path(os.path.expanduser(path)).resolve())
                    if p not in sys.path:
                        sys.path.insert(0, p)
                mod, _, attr = str(self.entry["spec"]).partition(":")
                obj = getattr(importlib.import_module(mod), attr)
                self._obj = obj() if callable(obj) and not hasattr(obj, "describe") else obj
            return self._obj

    def describe(self) -> Dict[str, Any]:
        return self._get().describe()

    def validate(self, scenario: Dict[str, Any]) -> Dict[str, Any]:
        return self._get().validate(scenario)

    def predict(self, scenario: Dict[str, Any]) -> Dict[str, Any]:
        return self._get().predict(scenario)

    def extra(self, name: str, arguments: Dict[str, Any]) -> Any:
        obj = self._get()
        if not hasattr(obj, "extra"):
            raise NotImplementedError("this twin has no extras")
        return obj.extra(name, arguments)

    def check_write(self, controller: str, tool: str, arguments: Dict[str, Any], answers: Dict[str, Any]) -> Dict[str, Any]:
        obj = self._get()
        if not hasattr(obj, "check_write"):
            raise NotImplementedError("this twin does not check controller writes")
        return obj.check_write(controller, tool, arguments, answers)


class HttpTwin:
    def __init__(self, entry: Dict[str, Any], timeout: float = 900.0):
        import requests
        self.entry = entry
        self.url = str(entry["url"]).rstrip("/")
        self.timeout = timeout
        self.http = requests.Session()

    def _headers(self) -> Dict[str, str]:
        env = self.entry.get("token_env")
        token = os.getenv(env, "") if env else ""
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _call(self, method: str, path: str, body: Any = None, timeout: Optional[float] = None) -> Any:
        r = self.http.request(method, self.url + path, json=body, headers=self._headers(), timeout=timeout or self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"{self.url}{path}: HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    def describe(self) -> Dict[str, Any]:
        return self._call("GET", "/twin", timeout=20)

    def validate(self, scenario: Dict[str, Any]) -> Dict[str, Any]:
        return self._call("POST", "/twin/validate", {"scenario": scenario}, timeout=60)

    def predict(self, scenario: Dict[str, Any]) -> Dict[str, Any]:
        return self._call("POST", "/twin/predict", {"scenario": scenario})

    def extra(self, name: str, arguments: Dict[str, Any]) -> Any:
        return self._call("POST", "/twin/extra", {"name": name, "arguments": arguments})

    def check_write(self, controller: str, tool: str, arguments: Dict[str, Any], answers: Dict[str, Any]) -> Dict[str, Any]:
        return self._call("POST", "/twin/check_write", {"controller": controller, "tool": tool,
                                                         "arguments": arguments, "answers": answers})


class Twin:
    """One twin, its answers held to the contract."""

    def __init__(self, entry: Dict[str, Any]):
        self.entry = entry
        self.name = entry["name"]
        self.impl = PythonTwin(entry) if entry.get("kind", "python") == "python" else HttpTwin(entry)
        self._desc: Optional[C.Description] = None
        self._desc_at = 0.0

    def describe(self, fresh: bool = False) -> C.Description:
        if fresh or self._desc is None or time.time() - self._desc_at > 60:
            self._desc = C.checked(C.Description, self.impl.describe(), self.name)
            self._desc_at = time.time()
        return self._desc

    def validate(self, scenario: Dict[str, Any]) -> C.Validation:
        return C.checked(C.Validation, self.impl.validate(scenario), self.name)

    def predict(self, scenario: Dict[str, Any]) -> C.Result:
        return C.checked(C.Result, self.impl.predict(scenario), self.name)

    def check_write(self, controller: str, tool: str, arguments: Dict[str, Any], answers: Dict[str, Any]) -> C.Check:
        return C.checked(C.Check, self.impl.check_write(controller, tool, arguments, answers), self.name)

    def extra(self, name: str, arguments: Dict[str, Any]) -> Any:
        d = self.describe()
        if name not in [e.name for e in d.extras]:
            raise KeyError(f"the twin {self.name!r} has no extra {name!r}; it has {[e.name for e in d.extras]}")
        return self.impl.extra(name, arguments)

    def can_check_writes(self) -> bool:
        try:
            return "check_write" in self.describe().capabilities
        except Exception:  # noqa: BLE001 - a twin that cannot be reached checks nothing
            return False


class Registry:
    def __init__(self, home: Path, declared: Optional[List[Dict[str, Any]]] = None, active: str = ""):
        self.path = Path(home) / "twins.json"
        self.declared = [dict(e) for e in (declared or [])]
        self.default_active = active
        self._lock = threading.Lock()
        self._twins: Dict[str, Twin] = {}

    # -- state
    def _saved(self) -> Dict[str, Any]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.path)

    def entries(self) -> List[Dict[str, Any]]:
        saved = self._saved()
        out = {e["name"]: {**e, "declared": True} for e in self.declared}
        for e in saved.get("added", []):
            out.setdefault(e["name"], {**e, "declared": False})
        return list(out.values())

    def active_name(self) -> str:
        names = [e["name"] for e in self.entries()]
        want = self._saved().get("active") or self.default_active
        if want == "none":
            return ""
        return want if want in names else (names[0] if names else "")

    # -- use
    def get(self, name: str) -> Twin:
        entry = next((e for e in self.entries() if e["name"] == name), None)
        if entry is None:
            raise KeyError(f"no twin {name!r}")
        with self._lock:
            t = self._twins.get(name)
            if t is None or t.entry != entry:
                t = self._twins[name] = Twin(entry)
            return t

    def active(self) -> Optional[Twin]:
        name = self.active_name()
        return self.get(name) if name else None

    # -- changes (from the web page; admin and root check in the service)
    def set_active(self, name: str) -> None:
        if name != "none" and name not in [e["name"] for e in self.entries()]:
            raise KeyError(f"no twin {name!r}")
        data = self._saved()
        data["active"] = name
        self._save(data)

    def add(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        e = {k: str(v).strip() for k, v in entry.items() if k in ("name", "kind", "url", "token_env", "spec", "path", "about") and v}
        if not _NAME.match(e.get("name", "")):
            raise ValueError("a twin's name is 2-32 lower-case letters, digits, - or _, starting with a letter")
        if e["name"] in [x["name"] for x in self.entries()]:
            raise ValueError(f"there is already a twin {e['name']!r}")
        kind = e.setdefault("kind", "http")
        if kind == "http":
            if not re.match(r"^https?://[^\s]+$", e.get("url", "")):
                raise ValueError("an HTTP twin needs its url (http:// or https://)")
            if e.get("token_env") and not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", e["token_env"]):
                raise ValueError("token_env is the NAME of an environment variable holding the token, never the token")
        elif kind == "python":
            if not re.fullmatch(r"[A-Za-z_][\w.]*:[A-Za-z_]\w*", e.get("spec", "")):
                raise ValueError("a Python twin needs spec as module:factory")
        else:
            raise ValueError("kind is http or python")
        data = self._saved()
        data.setdefault("added", []).append(e)
        self._save(data)
        return e

    def remove(self, name: str) -> None:
        data = self._saved()
        if name in [e["name"] for e in self.declared]:
            raise ValueError(f"{name} is declared in canah.yaml: remove it there")
        before = len(data.get("added", []))
        data["added"] = [e for e in data.get("added", []) if e["name"] != name]
        if len(data["added"]) == before:
            raise KeyError(f"no twin {name!r}")
        if data.get("active") == name:
            data.pop("active")
        self._save(data)
        self._twins.pop(name, None)

    def status(self) -> List[Dict[str, Any]]:
        out = []
        active = self.active_name()
        for e in self.entries():
            row = {k: e.get(k) for k in ("name", "kind", "url", "spec", "path", "token_env", "about", "declared")}
            row["active"] = e["name"] == active
            try:
                d = self.get(e["name"]).describe()
                row.update(ok=True, version=d.version, about=d.about or row.get("about"), contract=d.contract,
                           capabilities=d.capabilities, controller_domains=d.controller_domains,
                           extras=[{"name": x.name, "description": x.description} for x in d.extras],
                           domains=[{"domain": x.domain, "title": x.title, "answers": x.answers, "kpis": x.kpis,
                                     "fidelity": x.fidelity, "not_modelled": x.not_modelled} for x in d.domains])
            except Exception as ex:  # noqa: BLE001 - shown on the page
                row.update(ok=False, error=f"{type(ex).__name__}: {str(ex)[:300]}")
            out.append(row)
        return out
