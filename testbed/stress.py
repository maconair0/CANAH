"""
A stress test of CANAH as an operations harness, on the emulated domains (no real controller needed).

  python -m testbed.stress [--port 8363] [--watch 8] [--chats 8,16] [--out testbed/results]

It starts the emulator, a webhook catcher and a CANAH service of its own (its own CANAH_HOME; the watcher every
`--watch` seconds; notifications to the catcher), then:

  operations  injects faults across the domains (a fibre cut that takes a lightpath, a packet link and a service
              with it; a critical alarm; a controller that stops answering), measures how long until each
              incident opens, which runbook it got, whether CANAH investigated it, what was notified; then clears
              them and measures how long until they resolve; and checks the history and health over time
  api         concurrent reads of the console's endpoints: latency p50 / p95, errors
  chat        N conversations at once (read-only questions): each one's time, errors, empty answers
  twin        what-ifs asked through the agent: the digital twin used, and its decision reported

Writes <out>/stress-<time>.json and prints a summary.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List

import requests

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def pct(xs: List[float], p: float) -> float:
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(len(xs) * p))], 3) if xs else None


class Catcher:
    """A webhook that keeps what it is sent."""

    def __init__(self):
        got = self.got = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                got.append({"at": time.time(), **json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")})
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/hook"


def start_canah(port: int, emu_url: str, hook: str, watch_s: int) -> tuple:
    home = Path(tempfile.mkdtemp(prefix="canah-stress-"))
    (home / "canah.yaml").write_text(json.dumps({
        "name": "canah", "providers": ["canah.network:provider"], "sandbox": "none",
        "root_tools": ["*delete*", "*remove*", "*reboot*", "*restart*"],
        "twins": [{"name": "netwins", "kind": "python", "spec": "netwins.contract:twin", "path": "~/netwins"}],
        "twin_active": "netwins", "watch_interval_s": watch_s, "resolve_after": 2, "history_every": 1,
        "respond": True, "respond_per_hour": 30, "notify_channels": [{"kind": "webhook", "url": hook}],
        "memory_files": [str(HERE / "AGENTS.md")], "skills_dirs": [str(HERE / "skills")]}))
    env = {**os.environ, "CANAH_HOME": str(home), "CANAH_CONFIG": str(home / "canah.yaml"), "AGENTIC_MCP_URL": emu_url,
           "AGENTIC_MCP_TOKEN": "", "CANAH_ADMIN_PASSWORD": "stress-admin", "CANAH_ROOT_PASSWORD": "stress-root"}
    log = open(home / "canah.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "canah", "serve", "--port", str(port), "--no-sandbox"], cwd=str(ROOT),
                            env=env, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    for _ in range(240):
        try:
            if requests.get(url + "/health", timeout=2).ok:
                break
        except requests.RequestException:
            pass
        time.sleep(0.5)
    return proc, url, home


def wait(cond, timeout: float, every: float = 1.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        got = cond()
        if got:
            return got, time.time() - t0
        time.sleep(every)
    return None, None


def operations(url: str, emu: str, catcher: Catcher, watch_s: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {}

    def active():
        return requests.get(url + "/incidents?status=active", timeout=30).json()["incidents"]
    base, t = wait(lambda: active() or None, watch_s * 4)
    out["baseline"] = sorted(f"{i['controller']}:{i['kind']}" for i in base or [])
    before = {i["fp"] for i in active()}
    faults = [("optical", "link_down", "F-ab", "", "major"), ("fabric", "alarm", "leaf-2", "fan failure", "critical"),
              ("packet", "controller_down", "", "", "major")]
    t_inject = time.time()
    for d, k, tg, text, sev in faults:
        r = requests.post(emu + "/fault", json={"domain": d, "kind": k, "target": tg, "text": text, "severity": sev}, timeout=10)
        r.raise_for_status()
    expect = {"optical:link_down", "optical:service_down", "fabric:alarm", "packet:controller_unreachable"}
    opened, took = wait(lambda: (lambda now: now if expect <= {f"{i['controller']}:{i['kind']}" for i in now} else None)(
        [i for i in active() if i["fp"] not in before]), watch_s * 6)
    out["faults"] = [f"{d}:{k}:{tg}" for d, k, tg, _, _ in faults]
    out["opened_all_expected"] = opened is not None
    out["seconds_to_open"] = round(took, 1) if took else None
    new = [i for i in active() if i["fp"] not in before]
    out["opened"] = [{"id": i["id"], "kind": i["kind"], "controller": i["controller"], "severity": i["severity"],
                      "runbook": i["runbook"], "mode": i["mode"]} for i in new]
    # the investigations the runbooks started (the model is the slow part)
    inv = [i for i in new if i["mode"] in ("investigate", "propose")]
    done, t_inv = wait(lambda: (lambda rows: rows if all(r["report"] for r in rows) else None)(
        [requests.get(f"{url}/incidents/{i['id']}", timeout=30).json()["incident"] for i in inv]), 900, 5)
    rows = [requests.get(f"{url}/incidents/{i['id']}", timeout=30).json() for i in inv]
    out["investigations"] = {"started": len(inv), "reported": sum(bool(r["incident"]["report"]) for r in rows),
                             "seconds_until_all_reported": round(t_inv, 1) if t_inv else None,
                             "fixes_waiting_for_approval": sum(len(r["pending"]) for r in rows),
                             "tool_calls": sum(len([m for m in r["messages"] if m["role"] == "tool"]) for r in rows),
                             "reports": {f"#{r['incident']['id']} {r['incident']['kind']}": (r["incident"]["report"] or "")[:500] for r in rows}}
    # clear: everything should resolve by itself
    requests.post(emu + "/fault/clear", json={}, timeout=10)
    gone, t_res = wait(lambda: not [i for i in active() if i["fp"] not in before and i["fp"] in {n["fp"] for n in new}] or None,
                       watch_s * 6)
    out["resolved_after_clear"] = gone is not None
    out["seconds_to_resolve"] = round(t_res, 1) if t_res else None
    ev = {}
    for n in catcher.got:
        ev[n.get("event")] = ev.get(n.get("event"), 0) + 1
    out["notifications"] = ev
    hist = requests.get(url + "/history?hours=1", timeout=30).json()
    out["history_changes"] = sum(len(c["changes"]) for c in hist["changes"])
    series = requests.get(url + "/health/series?hours=1", timeout=30).json()["series"]
    out["health_readings"] = {k: len(v) for k, v in series.items()}
    out["seconds_total"] = round(time.time() - t_inject, 1)
    return out


def api_load(url: str, n: int = 400, workers: int = 24) -> Dict[str, Any]:
    paths = ["/topology", "/incidents", "/history?hours=1", "/health/series?hours=1", "/info", "/network", "/twins", "/"]
    lat, errs = {p: [] for p in paths}, 0

    def one(i):
        p = paths[i % len(paths)]
        t = time.time()
        try:
            ok = requests.get(url + p, timeout=60).ok
        except requests.RequestException:
            ok = False
        return p, time.time() - t, ok
    t0 = time.time()
    with ThreadPoolExecutor(workers) as pool:
        for p, d, ok in pool.map(one, range(n)):
            lat[p].append(d)
            errs += not ok
    allv = [x for v in lat.values() for x in v]
    return {"requests": n, "concurrency": workers, "errors": errs, "seconds": round(time.time() - t0, 1),
            "per_second": round(n / (time.time() - t0), 1), "p50_s": pct(allv, 0.5), "p95_s": pct(allv, 0.95),
            "by_path_p95_s": {p: pct(v, 0.95) for p, v in lat.items()}}


QUESTIONS = ["Which packet routers are down?", "Which lightpaths are in service, and what is each one's OSNR?",
             "Which tenants are on which leaf switches?", "What changed in the networks in the last hour?",
             "How has each controller been over the last hour?", "Which incidents are open?",
             "How many transponders does the optical domain have?", "What services does the packet domain carry?"]


def chats(url: str, n: int) -> Dict[str, Any]:
    def one(i):
        tid = requests.post(url + "/threads", json={}, timeout=30).json()["thread_id"]
        t = time.time()
        try:
            r = requests.post(f"{url}/threads/{tid}/messages", json={"text": QUESTIONS[i % len(QUESTIONS)]}, timeout=900)
            j = r.json() if r.ok else {}
            return {"ok": r.ok, "s": time.time() - t, "empty": not (j.get("text") or "").strip(),
                    "tools": len(j.get("tool_calls") or []), "status": r.status_code, "text": (j.get("text") or "")[:160]}
        except requests.RequestException as e:
            return {"ok": False, "s": time.time() - t, "empty": True, "tools": 0, "status": str(e)[:80], "text": ""}
    t0 = time.time()
    with ThreadPoolExecutor(n) as pool:
        rows = list(pool.map(one, range(n)))
    s = [r["s"] for r in rows]
    return {"conversations": n, "wall_s": round(time.time() - t0, 1), "errors": sum(not r["ok"] for r in rows),
            "empty": sum(r["empty"] for r in rows), "p50_s": pct(s, 0.5), "p95_s": pct(s, 0.95), "max_s": round(max(s), 1),
            "tool_calls_mean": round(sum(r["tools"] for r in rows) / n, 1),
            "sample": [{"q": QUESTIONS[i % len(QUESTIONS)], "s": round(r["s"], 1), "a": r["text"]} for i, r in enumerate(rows[:4])]}


def twin(url: str) -> Dict[str, Any]:
    out = []
    for q in ["If fibre F-ab is cut, does lightpath LP-1 keep at least 3 dB of margin after it restores? Ask the digital twin.",
              "Using the digital twin's non-terrestrial (ntn) example scenario, what does 25 mm/h of rain on the terminal do "
              "to the backhaul's availability? Ask the twin."]:
        tid = requests.post(url + "/threads", json={}, timeout=30).json()["thread_id"]
        t = time.time()
        r = requests.post(f"{url}/threads/{tid}/messages", json={"text": q}, timeout=900).json()
        tools = [c["name"] for c in r.get("tool_calls") or []]
        txt = r.get("text") or ""
        out.append({"q": q[:80], "s": round(time.time() - t, 1), "predicted": "twin_predict" in tools, "tools": tools,
                    "decision_reported": any(w in txt.lower() for w in ("reject", "propose", "gather", "met", "not_met")),
                    "answer": txt[:400]})
    return {"questions": out}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="testbed.stress")
    ap.add_argument("--port", type=int, default=8363)
    ap.add_argument("--watch", type=int, default=8)
    ap.add_argument("--chats", default="8,16")
    ap.add_argument("--out", default=str(HERE / "results"))
    ap.add_argument("--only", default="ops,api,chat,twin", help="which layers: ops, api, chat, twin")
    a = ap.parse_args(argv)
    from testbed.emulator import Running
    emu = Running()
    catcher = Catcher()
    proc, url, home = start_canah(a.port, emu.url, catcher.url, a.watch)
    report: Dict[str, Any] = {"at": time.time(), "canah": url, "home": str(home)}
    try:
        only = set(a.only.split(","))
        if "ops" in only:
            print("operations ...", flush=True)
            report["operations"] = operations(url, emu.url, catcher, a.watch)
            print(json.dumps(report["operations"], indent=1)[:4000], flush=True)
        if "api" in only:
            print("api load ...", flush=True)
            report["api"] = api_load(url)
            print(json.dumps(report["api"], indent=1), flush=True)
        report["chat"] = []
        for n in ([int(x) for x in a.chats.split(",") if x] if "chat" in only else []):
            print(f"{n} conversations at once ...", flush=True)
            report["chat"].append(chats(url, n))
            print(json.dumps(report["chat"][-1], indent=1)[:1500], flush=True)
        if "twin" in only:
            print("twin ...", flush=True)
            report["twin"] = twin(url)
            print(json.dumps(report["twin"], indent=1)[:2000], flush=True)
    finally:
        proc.terminate()
        try:
            proc.wait(20)
        except subprocess.TimeoutExpired:
            proc.kill()
        emu.stop()
    Path(a.out).mkdir(parents=True, exist_ok=True)
    f = Path(a.out) / f"stress-{time.strftime('%Y%m%d-%H%M%S')}.json"
    f.write_text(json.dumps(report, indent=1, default=str))
    print("written:", f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
