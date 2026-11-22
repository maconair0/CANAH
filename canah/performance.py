"""
How the networks perform, not only whether they are up: what the controllers measure, over time, against limits.

A controller that serves the contract's `read_metrics` verb answers with rows, one per object it measures:

  {"object": "LP-1", "kind": "lightpath", "state": "in-service", "pre_fec_ber": 1.9e-05, "osnr_db": 21.3, ...}
  {"object": "L-ab", "kind": "link", "state": "up", "drop_pct": 0.0, "utilisation_pct": 39.3, ...}

(a list, or under "metrics" / "performance" / "counters"; every numeric field is a measurement). A row may carry
its own "limits": {"pre_fec_ber": {"warn": 3.8e-3, "bad": 1.5e-2}}, since a vendor's FEC knows its threshold better
than a default does; otherwise the catalogue below applies. Nothing is measured here and nothing is made up: a
controller without the verb is shown as publishing no measurements.

  rows_of(parsed)                  the rows, whatever the answer's shape
  judge(metric, value, limits)     "ok", "warn" or "bad", against the row's limits or the catalogue's
  Store(home).add(at, key, rows)   kept 7 days; .latest(), .series(key, object, metric, hours, points)
  findings(key, label, rows)       a value past its limit, as a watcher finding (minor if warn, major if bad)
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

KEEP_DAYS = 7

# name: label, unit, which way is worse ("high": above the limit is worse), warn, bad, log scale for a plot
CATALOGUE: Dict[str, Dict[str, Any]] = {
    "pre_fec_ber":      {"label": "pre-FEC BER", "unit": "", "worse": "high", "warn": 2e-3, "bad": 1e-2, "log": True},
    "post_fec_ber":     {"label": "post-FEC BER", "unit": "", "worse": "high", "warn": 1e-15, "bad": 1e-12, "log": True},
    "osnr_db":          {"label": "OSNR", "unit": "dB", "worse": "low", "warn": 16.0, "bad": 13.0},
    "q_margin_db":      {"label": "Q margin", "unit": "dB", "worse": "low", "warn": 2.0, "bad": 0.5},
    "rx_power_dbm":     {"label": "Rx power", "unit": "dBm", "worse": "low", "warn": -18.0, "bad": -24.0},
    "tx_power_dbm":     {"label": "Tx power", "unit": "dBm"},
    "cd_ps_nm":         {"label": "CD", "unit": "ps/nm"},
    "pmd_ps":           {"label": "PMD", "unit": "ps", "worse": "high", "warn": 10.0, "bad": 25.0},
    "span_loss_db":     {"label": "span loss", "unit": "dB"},
    "drop_pct":         {"label": "drops", "unit": "%", "worse": "high", "warn": 0.1, "bad": 1.0},
    "loss_pct":         {"label": "loss", "unit": "%", "worse": "high", "warn": 0.1, "bad": 1.0},
    "drops_per_s":      {"label": "drops", "unit": "/s", "worse": "high", "warn": 10, "bad": 1000},
    "crc_errors_per_s": {"label": "CRC errors", "unit": "/s", "worse": "high", "warn": 1, "bad": 100},
    "errors_per_s":     {"label": "errors", "unit": "/s", "worse": "high", "warn": 1, "bad": 100},
    "utilisation_pct":  {"label": "utilisation", "unit": "%", "worse": "high", "warn": 80.0, "bad": 95.0},
    "latency_ms":       {"label": "latency", "unit": "ms", "worse": "high", "warn": 50.0, "bad": 150.0},
    "jitter_ms":        {"label": "jitter", "unit": "ms", "worse": "high", "warn": 10.0, "bad": 30.0},
    "cpu_pct":          {"label": "CPU", "unit": "%", "worse": "high", "warn": 85.0, "bad": 95.0},
    "memory_pct":       {"label": "memory", "unit": "%", "worse": "high", "warn": 85.0, "bad": 95.0},
    "temperature_c":    {"label": "temperature", "unit": "°C", "worse": "high", "warn": 70.0, "bad": 85.0},
}
_ID_KEYS = ("object", "name", "id", "resource", "interface", "port", "link", "lightpath", "service")
_SKIP = {"at", "time", "timestamp", "ts"}


def rows_of(data: Any) -> List[Dict[str, Any]]:
    """[{object, kind, state, metrics: {name: number}, limits}] from a read_metrics answer of any shape."""
    if isinstance(data, dict):
        for k in ("metrics", "performance", "counters", "items", "rows"):
            if isinstance(data.get(k), list):
                data = data[k]
                break
    if not isinstance(data, list):
        return []
    out = []
    for r in data:
        if not isinstance(r, dict):
            continue
        obj = next((str(r[k]) for k in _ID_KEYS if r.get(k) not in (None, "")), "")
        if not obj:
            continue
        m = {k: float(v) for k, v in r.items()
             if k not in _SKIP and k not in _ID_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool)}
        if isinstance(r.get("metrics"), dict):
            m.update({k: float(v) for k, v in r["metrics"].items() if isinstance(v, (int, float)) and not isinstance(v, bool)})
        out.append({"object": obj, "kind": str(r.get("kind") or r.get("type") or ""), "state": str(r.get("state") or r.get("status") or ""),
                    "metrics": m, "limits": r.get("limits") if isinstance(r.get("limits"), dict) else {}})
    return out


def judge(metric: str, value: Optional[float], limits: Optional[Dict[str, Any]] = None) -> str:
    if value is None:
        return ""
    spec = {**CATALOGUE.get(metric, {}), **((limits or {}).get(metric) or {})}
    if "warn" not in spec and "bad" not in spec:
        return ""
    worse = spec.get("worse", "high")
    past = (lambda x: value > x) if worse == "high" else (lambda x: value < x)
    if spec.get("bad") is not None and past(float(spec["bad"])):
        return "bad"
    if spec.get("warn") is not None and past(float(spec["warn"])):
        return "warn"
    return "ok"


def describe(metric: str) -> Dict[str, Any]:
    c = CATALOGUE.get(metric, {})
    return {"metric": metric, "label": c.get("label", metric.replace("_", " ")), "unit": c.get("unit", ""),
            "worse": c.get("worse", ""), "warn": c.get("warn"), "bad": c.get("bad"), "log": bool(c.get("log"))}


def fmt(metric: str, v: float) -> str:
    if CATALOGUE.get(metric, {}).get("log"):
        return "0" if v == 0 else f"{v:.2e}"
    return f"{v:g}" if abs(v) >= 100 or v == int(v) else f"{v:.3g}" if abs(v) < 1 else f"{v:.2f}".rstrip("0").rstrip(".")


def findings(key: str, label: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for r in rows:
        for m, v in r["metrics"].items():
            j = judge(m, v, r["limits"])
            if j in ("warn", "bad"):
                d = describe(m)
                spec = {**CATALOGUE.get(m, {}), **(r["limits"].get(m) or {})}
                lim = spec["bad" if j == "bad" else "warn"]
                side = "over" if spec.get("worse", "high") == "high" else "under"
                u = f" {d['unit']}" if d["unit"] and d["unit"] != "%" else d["unit"]
                out.append({"fp": f"{key}:perf:{r['object']}:{m}", "controller": key, "severity": "major" if j == "bad" else "minor",
                            "kind": "performance",
                            "title": f"{label}: {d['label']} on {r['object']} at {fmt(m, v)}{u}, "
                                     f"{side} the {'limit' if j == 'bad' else 'warning level'} of {fmt(m, float(lim))}{u}",
                            "details": f"{r['kind'] or 'object'} {r['object']}: {', '.join(f'{k}={fmt(k, x)}' for k, x in r['metrics'].items())}"})
    return out


class Store:
    """Every measurement at every watcher read, kept KEEP_DAYS."""

    def __init__(self, home: Any):
        self._db = sqlite3.connect(str(Path(home) / "performance.sqlite"), check_same_thread=False, isolation_level=None)
        self._db.execute("create table if not exists perf (at real, controller text, object text, kind text, metric text, value real)")
        self._db.execute("create index if not exists perf_key on perf(controller, object, metric, at)")
        self._lock = threading.Lock()
        self._latest: Dict[str, Dict[str, Any]] = {}
        self._added = 0

    def add(self, at: float, key: str, rows: List[Dict[str, Any]], published: bool = True) -> None:
        with self._lock:
            self._latest[key] = {"at": at, "rows": rows, "published": published}
            self._db.execute("begin")
            for r in rows:
                for m, v in r["metrics"].items():
                    self._db.execute("insert into perf values (?, ?, ?, ?, ?, ?)", (at, key, r["object"], r["kind"], m, v))
            self._db.execute("commit")
            self._added += 1
            if self._added % 200 == 0:
                self._db.execute("delete from perf where at < ?", (time.time() - KEEP_DAYS * 86400,))

    def latest(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return dict(self._latest)

    def series(self, key: str, obj: str = "", metric: str = "", hours: float = 6.0, points: int = 60) -> Dict[str, List[List[float]]]:
        """{"object|metric": [[at, value], ...]} oldest first, at most `points` each (every n-th, and the last)."""
        q, args = "select object, metric, at, value from perf where controller = ? and at >= ?", [key, time.time() - hours * 3600]
        if obj:
            q += " and object = ?"
            args.append(obj)
        if metric:
            q += " and metric = ?"
            args.append(metric)
        with self._lock:
            got = self._db.execute(q + " order by at", args).fetchall()
        out: Dict[str, List[List[float]]] = {}
        for o, m, at, v in got:
            out.setdefault(f"{o}|{m}", []).append([round(at, 1), v])
        for k, v in out.items():
            if len(v) > points:
                step = len(v) / points
                out[k] = [v[int(i * step)] for i in range(points - 1)] + [v[-1]]
        return out
