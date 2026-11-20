"""
The agent in a terminal.

  python -m canah info                     what the agent is built from
  python -m canah serve [--host H] [--port 8350] [--tls-cert F --tls-key F]   the HTTP API and web page
  python -m canah user list | user add NAME [--level admin|root] | user remove NAME
  python -m canah audit [N] | metrics [HOURS] | memories [USER] | memories forget USER PATH | docs add FILE...
  python -m canah chat [--thread T] [--as observer|admin|root] [--no-sandbox]
  python -m canah ask "question" [--thread T] [--as ...] [--yes]

At the start it asks who you are: sign in as admin or root (password), or
continue as observer, who can ask anything and change nothing. `--as` picks
without the menu; a password is still asked for, or read from CANAH_PASSWORD
when there is no terminal.

Then it starts the agent's sandbox, a locked-down local Docker container, so
the agent can run commands from the first message (`--no-sandbox`, or
`sandbox: none` in canah.yaml, starts without it).

A change the agent proposes is shown with its arguments; answer
y (approve), n (reject, with a reason) or e (edit the arguments as JSON).
`--yes` approves everything: for scripted, trusted use only.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import getpass
import os
import time
import uuid

from . import approval
from .agent import CanahAgent, Reply, build_agent
from .config import load
from .identity import OBSERVER, Accounts, Identity


def sign_in(accounts: Accounts, wanted: str = "") -> Identity:
    """Ask who the operator is: admin or root with a password, or observer."""
    levels = accounts.levels()
    by_level = {lvl: user for user, lvl in levels.items()}
    choice = wanted
    if not choice:
        print("Sign in:")
        print(f"  1  admin   ({by_level.get('admin', 'admin')}: may approve changes)")
        print(f"  2  root    ({by_level.get('root', 'root')}: may approve everything)")
        print("  3  continue as observer (read only, no password)")
        while choice not in ("admin", "root", "observer"):
            got = input("choose 1, 2 or 3 [3]: ").strip().lower() or "3"
            choice = {"1": "admin", "2": "root", "3": "observer"}.get(got, got)
    if choice == "observer":
        return OBSERVER
    user = by_level.get(choice, choice)
    for attempt in range(3):
        password = os.getenv("CANAH_PASSWORD") if not sys.stdin.isatty() else getpass.getpass(f"{user} password: ")
        try:
            who = accounts.sign_in(user, password or "")
            print(f"signed in as {who.label}")
            return who
        except PermissionError as e:
            print(f"  {e}")
            if not sys.stdin.isatty() or "locked" in str(e):
                break
    print("continuing as observer")
    return OBSERVER


def _decide(agent: CanahAgent, reply: Reply, yes: bool, who: Identity) -> Reply:
    while reply.waiting:
        decisions = []
        for p in reply.pending:
            print(f"\n  ⚠ wants to run {p['name']}({json.dumps(p['args'], default=str)})   needs {p.get('needs')}")
            if not who.can(p.get("needs") or "admin"):
                print(f"    {who.label} cannot approve this; it is rejected. Sign in as {p.get('needs')} to approve.")
                decisions.append(approval.decision("reject", message=f"{who.label} may not approve this change."))
                continue
            if yes:
                print("    approved (--yes)")
                decisions.append(approval.decision("approve"))
                continue
            while True:
                got = input("    approve? [y]es / [n]o / [e]dit: ").strip().lower()
                if got in ("y", "yes"):
                    decisions.append(approval.decision("approve"))
                elif got in ("n", "no"):
                    why = input("    why: ").strip()
                    decisions.append(approval.decision("reject", message=why or "The operator said no."))
                elif got in ("e", "edit"):
                    try:
                        args = json.loads(input("    arguments as JSON: "))
                    except ValueError as e:
                        print(f"    not JSON: {e}")
                        continue
                    decisions.append(approval.decision("edit", name=p["name"], args=args))
                else:
                    continue
                break
        reply = agent.decide(reply.thread_id, decisions, who=who)
    return reply


def admin_command(a) -> int:
    """user add NAME | user list | user remove NAME; audit [N]; metrics [HOURS]; memories [USER];
    memories forget USER PATH; docs add FILE..."""
    from .config import load
    settings = load(a.config)
    home = settings.home_path
    if a.command == "user":
        acc = Accounts(str(home / "users.json"))
        op = (a.args or ["list"])[0]
        if op == "list":
            for user, level in sorted(acc.levels().items()):
                print(f"{user:20} {level}")
        elif op == "add" and len(a.args) == 2:
            pw = os.getenv("CANAH_NEW_PASSWORD") or getpass.getpass(f"password for {a.args[1]}: ")
            acc.add(a.args[1], a.level, pw)
            print(f"{a.args[1]} added as {a.level}")
        elif op == "remove" and len(a.args) == 2:
            print("removed" if acc.remove(a.args[1]) else "no such user (env accounts are set in the environment)")
        else:
            print("user list | user add NAME [--level admin|root] | user remove NAME")
            return 2
        return 0
    if a.command in ("audit", "metrics"):
        from .ops import Ops
        ops = Ops(home / "ops.sqlite")
        if a.command == "audit":
            for r in reversed(ops.audit_log(int(a.args[0]) if a.args else 30)):
                print(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r['at']))}  {r['event']:15} "
                      f"{r['user'] or '-':12} {r['tool'] or '':32} {r['decision'] or ''} {r['args'][:80]}")
        else:
            print(json.dumps(ops.metrics(float(a.args[0]) * 3600 if a.args else 86400), indent=1))
        return 0
    if a.command == "memories":
        from .memory import delete_memory, list_memories, open_memory
        m = open_memory(home)
        try:
            if a.args[:1] == ["forget"] and len(a.args) == 3:
                print("deleted" if delete_memory(m.store, a.args[1], a.args[2]) else "not found")
            else:
                for r in list_memories(m.store, a.args[0] if a.args else None):
                    print(f"{r['user']:12} {r['path']:30} {r['chars']:6} chars  {r['modified'][:19]}  {r['preview'][:60]!r}")
        finally:
            m.close()
        return 0
    if a.command == "docs":
        if a.args[:1] == ["add"] and len(a.args) > 1:
            from .knowledge import add_documents
            print(json.dumps(add_documents(a.args[1:], settings.knowledge_collection), indent=1, default=str))
            return 0
        print("docs add FILE...")
        return 2
    return 2


def _show(reply: Reply) -> None:
    for c in reply.tool_calls:
        print(f"  · {c['name']}({json.dumps(c['args'], default=str)[:160]})")
        if c["name"] == "execute" and c.get("result") is not None:
            # What the sandbox really printed, so the operator sees it beside the answer.
            out = str(c["result"]).rstrip()
            for line in (out[:1500] + ("\n    ..." if len(out) > 1500 else "")).splitlines() or ["(no output)"]:
                print(f"    │ {line}")
    print(reply.text or "(no answer)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="canah")
    ap.add_argument("command", choices=["info", "chat", "ask", "serve", "user", "audit", "metrics", "memories",
                                        "docs"])
    ap.add_argument("args", nargs="*", default=[])
    ap.add_argument("--level", default="admin", choices=["admin", "root"])
    ap.add_argument("--tls-cert", default="")
    ap.add_argument("--tls-key", default="")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8350)
    ap.add_argument("--text", dest="text_opt", default="")
    ap.add_argument("--config", default=None)
    ap.add_argument("--thread", default="")
    ap.add_argument("--as", dest="as_", choices=["observer", "admin", "root"], default="")
    ap.add_argument("--no-sandbox", action="store_true", help="start without the Docker sandbox")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    a.text = a.text_opt or (" ".join(a.args) if a.command == "ask" else "")
    if a.command == "serve":
        # Sign-in happens per request on the web page and the API, not here.
        from .service import serve
        serve(a.host, a.port, a.config, sandbox=not a.no_sandbox, tls_cert=a.tls_cert, tls_key=a.tls_key)
        return 0
    if a.command in ("user", "audit", "metrics", "memories", "docs"):
        return admin_command(a)
    who = OBSERVER if a.command == "info" else sign_in(Accounts(), a.as_)
    settings = load(a.config)
    if a.no_sandbox or a.command == "info":
        settings.sandbox = "none"
    if settings.sandbox == "docker":
        print("starting the sandbox ...", flush=True)
    agent = build_agent(settings)
    if settings.sandbox == "docker":
        print(f"sandbox: {agent.info['sandbox']}")
    try:
        if a.command == "info":
            print(json.dumps(agent.info, indent=1, default=str))
            return 0
        thread = a.thread or uuid.uuid4().hex[:12]
        if a.command == "ask":
            _show(_decide(agent, agent.ask(a.text or sys.stdin.read(), thread, who), a.yes, who))
            return 0
        print(f"canah · {who.label} · thread {thread} · {agent.info['model']['model']}"
              f" · ctrl-d to leave")
        while True:
            try:
                text = input("\n> ").strip()
            except EOFError:
                print()
                return 0
            if text:
                _show(_decide(agent, agent.ask(text, thread, who), a.yes, who))
    finally:
        agent.close()


if __name__ == "__main__":
    sys.exit(main())
