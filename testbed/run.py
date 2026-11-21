"""
Run the checked cases against CANAH, on the emulated domains.

  python -m testbed.run [--rounds 2] [--only packet,cross] [--no-sandbox] [--out testbed/results]

Starts the emulator, indexes the test bed's documents, builds the agent (the networks
through the emulator's agentic MCP, the documents, the test bed's AGENTS.md and skills,
the sandbox), and runs each case in a fresh thread with the domains reset. For a case
run as admin, every change the agent proposes is approved (as root when it needs root),
up to 4 times; the state checks then catch a wrong or repeated change.

Writes <out>/<timestamp>.json: every run, a score per group, the model and the harness
commit.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--only", default="")
    ap.add_argument("--no-sandbox", action="store_true")
    ap.add_argument("--out", default=str(HERE / "results"))
    ap.add_argument("--trace", default="", help="a directory: write each case's messages there")
    a = ap.parse_args(argv)

    home = Path(tempfile.mkdtemp(prefix="canah-testbed-"))
    os.environ["CANAH_HOME"] = str(home)
    os.environ["CANAH_KNOWLEDGE_DIR"] = str(home / "knowledge")
    os.environ.setdefault("CANAH_SANDBOX_WORKSPACE", f"canah-testbed-{os.getpid()}")
    sys.path.insert(0, str(ROOT))

    from canah.knowledge.rag import refresh
    refresh.refresh(str(HERE / "docs"), "corpus", False)

    from canah import approval, build_agent, load_settings, models
    from canah.identity import Identity
    from canah.knowledge import KnowledgeProvider
    from canah.network import AgenticMCP, AgenticMCPProvider
    from testbed.cases import CASES
    from testbed.emulator import Running

    emu = Running()
    domains = emu.domains
    s = load_settings(path=str(ROOT / "canah.yaml"))
    s.providers = []
    s.home = str(home)
    s.sandbox = "none" if a.no_sandbox else "docker"
    s.memory_files = s.memory_files + [str(HERE / "AGENTS.md")]
    s.skills_dirs = s.skills_dirs + [str(HERE / "skills")]
    agent = build_agent(s, [AgenticMCPProvider(service=AgenticMCP(emu.url, token="")), KnowledgeProvider()])
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True,
                            text=True).stdout.strip()
    print(f"model {models.describe()['model']} · harness {commit} · {agent.info['tools']} tools · "
          f"subagents {agent.info['subagents']} · sandbox {agent.info['sandbox'][:40]}", flush=True)

    only = set(filter(None, a.only.split(",")))
    cases = [c for c in CASES if not only or c.group in only or c.id in only]
    runs = []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for rnd in range(a.rounds):
        for c in cases:
            for d in domains.values():
                d.reset()
            # Each case starts clean on the agent's side too: no long-term memories from an earlier case,
            # no files left in the sandbox (kept results, notes). Otherwise one case answers from another.
            for item in list(agent.memory.store.search(("canah", "memories"), limit=10_000)):
                agent.memory.store.delete(item.namespace, item.key)
            if agent.sandbox is not None:
                agent.sandbox.execute("rm -rf /workspace/* /workspace/.[!.]* 2>/dev/null; true", timeout=30)
            if c.setup:
                c.setup(domains)
            thread = f"tb-{stamp}-{rnd}-{c.id}"
            t0 = time.time()
            approved, err = [], ""
            try:
                r = agent.ask(c.question, thread_id=thread, who=c.who)
                for _ in range(4):
                    if not r.pending or c.who.level == "observer":
                        break
                    approved += [p["name"] for p in r.pending]
                    who = Identity(c.who.user, "root") if any(p.get("needs") == "root" for p in r.pending) else c.who
                    r = agent.decide(thread, [approval.decision("approve") for _ in r.pending], who=who)
                ok = bool(c.check(r.text or "", r, domains))
                text = r.text or ""
                calls = [x["name"] for x in r.tool_calls]
            except Exception as e:  # noqa: BLE001 - a crash is a failed case, recorded
                ok, text, calls, err = False, "", [], f"{type(e).__name__}: {e}"
            dt = time.time() - t0
            msgs = agent.graph.get_state(agent.config(thread, c.who)).values.get("messages", [])
            humans = " ".join(str(m.content) for m in msgs if m.type == "human")
            row = {"round": rnd, "case": c.id, "group": c.group, "who": c.who.level, "ok": ok, "seconds": round(dt, 1),
                   "approved": approved, "calls": calls,
                   "writes": {k: [w["tool"] for w in d.writes] for k, d in domains.items() if d.writes},
                   "refused_by_gate": list(emu.app.state.refused),
                   "sent_back": {k: humans.count(f"[{k} check]") for k in ("grounding", "citation", "follow-through")},
                   "answer": text[:600], "error": err}
            runs.append(row)
            if a.trace:
                Path(a.trace).mkdir(parents=True, exist_ok=True)
                lines = []
                for m in msgs:
                    tc = getattr(m, "tool_calls", None) or []
                    lines.append(f"--- {m.type} {getattr(m, 'name', '') or ''}\n{str(m.content)[:3000]}"
                                 + "".join(f"\n  CALL {x['name']} {json.dumps(x.get('args'), default=str)[:1500]}" for x in tc))
                (Path(a.trace) / f"r{rnd}-{c.id}.txt").write_text("\n".join(lines))
            emu.app.state.refused.clear()
            print(f"{'PASS' if ok else 'FAIL'} r{rnd} {c.group:8} {c.id:16} {dt:6.1f}s  approved={approved} "
                  f"writes={row['writes']}" + (f" ERR {err}" if err else ""), flush=True)
            if not ok:
                print("     answer:", text[:300].replace("\n", " | "), flush=True)
    agent.close()
    emu.stop()

    score = {}
    for g in sorted({r["group"] for r in runs}):
        rs = [r for r in runs if r["group"] == g]
        score[g] = {"passed": sum(r["ok"] for r in rs), "runs": len(rs)}
    total = {"passed": sum(r["ok"] for r in runs), "runs": len(runs)}
    print("\nscore:", json.dumps(score), "total", f"{total['passed']}/{total['runs']}")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{stamp}.json").write_text(json.dumps({"model": models.describe(), "harness": commit,
                                                   "score": score, "total": total, "runs": runs}, indent=1))
    print("results:", out / f"{stamp}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
