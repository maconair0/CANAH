"""
The agentic MCP CANAH reaches its controllers through, set up and run the way a new deployment would be: from its
GitHub repository, with nothing borrowed from another system on this machine.

  python scripts/agentic_mcp.py prepare     get the code (clone, or fast-forward to the latest), its Python
                                            environment, the controller manifests and the onboarded controllers
  python scripts/agentic_mcp.py serve       run it (in the foreground; start.sh runs it in the background)
  python scripts/agentic_mcp.py status      where it is, what it holds

What to deploy is agentic_mcp.yaml next to start.sh (agentic_mcp.example.yaml shows every setting): the
repository and branch, the manifests to install (the kinds of controller), and the controllers to onboard, each
with its address and the NAMES of the variables that hold its credentials. The credentials themselves are in a
controllers.env file (default: the one next to start.sh, not committed), which the agentic MCP reads itself.

Its code, Python environment and data live under $CANAH_HOME/agentic-mcp/ (src/, venv/, data/): manifests,
onboarded controllers, the write queue and the audit log stay there across restarts and updates.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import venv
from pathlib import Path
from typing import Any, Dict, List

import yaml

HERE = Path(__file__).resolve().parent.parent
HOME = Path(os.getenv("CANAH_HOME") or HERE / "canah_data")
DEFAULTS = {"repo": "https://github.com/maconair0/agentic-mcp.git", "ref": "main", "update": True,
            "dir": str(HOME / "agentic-mcp" / "src"), "venv": str(HOME / "agentic-mcp" / "venv"),
            "data": str(HOME / "agentic-mcp" / "data"), "env_file": str(HERE / "controllers.env"),
            "manifests": [], "controllers": []}


def say(sym: str, text: str) -> None:
    colour = {"ok": "32m✓", "warn": "33m!", "bad": "31m✗"}[sym]
    print(f"  \033[{colour}\033[0m {text}", flush=True)


def config() -> Dict[str, Any]:
    path = Path(os.getenv("AGENTIC_MCP_CONFIG") or HERE / "agentic_mcp.yaml")
    got: Dict[str, Any] = {}
    if path.is_file():
        got = yaml.safe_load(path.read_text()) or {}
    cfg = {**DEFAULTS, **{k: v for k, v in got.items() if v not in (None, "")}}
    for k in ("dir", "venv", "data", "env_file"):
        cfg[k] = str(Path(os.path.expandvars(str(cfg[k]))).expanduser())
    cfg["file"] = str(path) if path.is_file() else ""
    return cfg


def run(cmd: List[str], cwd: str = None, env: Dict[str, str] = None, quiet: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=quiet, text=True)


def child_env(cfg: Dict[str, Any]) -> Dict[str, str]:
    env = dict(os.environ)
    env["AGENTIC_MCP_DATA"] = cfg["data"]
    env["AGENTIC_MCP_ENV_FILE"] = cfg["env_file"]
    # The contract it holds controllers to is data, not code: a contract applied on the console's SDN
    # controllers page is written here, survives an update of the code, and keeps the checkout clean.
    env["AGENTIC_MCP_CONTRACT"] = str(Path(cfg["data"]) / "contract.json")
    env.pop("PYTHONPATH", None)
    return env


def py(cfg: Dict[str, Any]) -> str:
    return str(Path(cfg["venv"]) / "bin" / "python")


def get_code(cfg: Dict[str, Any]) -> bool:
    src = Path(cfg["dir"])
    if not (src / ".git").is_dir():
        if src.exists() and any(src.iterdir()):
            say("bad", f"agentic MCP: {src} exists and is not a git checkout; move it or set dir in agentic_mcp.yaml")
            return False
        src.parent.mkdir(parents=True, exist_ok=True)
        got = run(["git", "clone", "--quiet", "--branch", str(cfg["ref"]), str(cfg["repo"]), str(src)])
        if got.returncode:
            say("bad", f"agentic MCP: could not clone {cfg['repo']} ({cfg['ref']}): {got.stderr.strip()[:300]}")
            return False
        say("ok", f"agentic MCP: cloned {cfg['repo']} ({cfg['ref']}) into {src}")
        return True
    if not cfg.get("update", True):
        return True
    # The latest of the branch asked for, by fast-forward only: a checkout with changes of its own is left as it is.
    if run(["git", "status", "--porcelain"], cwd=str(src)).stdout.strip():
        say("warn", f"agentic MCP: {src} has local changes; not updated")
        return True
    if run(["git", "fetch", "--quiet", "origin", str(cfg["ref"])], cwd=str(src)).returncode:
        say("warn", "agentic MCP: could not reach its repository; using the code already here")
        return True
    before = run(["git", "rev-parse", "--short", "HEAD"], cwd=str(src)).stdout.strip()
    run(["git", "checkout", "--quiet", str(cfg["ref"])], cwd=str(src))
    got = run(["git", "merge", "--quiet", "--ff-only", "FETCH_HEAD"], cwd=str(src))
    after = run(["git", "rev-parse", "--short", "HEAD"], cwd=str(src)).stdout.strip()
    if got.returncode:
        say("warn", f"agentic MCP: its {cfg['ref']} cannot be fast-forwarded; using {after}")
    elif before != after:
        say("ok", f"agentic MCP: updated {before} -> {after} ({cfg['ref']})")
    return True


def get_env(cfg: Dict[str, Any]) -> bool:
    p = Path(py(cfg))
    req = Path(cfg["dir"]) / "requirements.txt"
    stamp = Path(cfg["venv"]) / ".requirements"
    if not p.exists():
        venv.EnvBuilder(with_pip=True).create(cfg["venv"])
    want = req.read_text() if req.exists() else ""
    if stamp.exists() and stamp.read_text() == want:
        return True
    say("ok", "agentic MCP: installing its requirements (first start, or they changed)")
    got = run([str(p), "-m", "pip", "install", "-q", "-r", str(req)])
    if got.returncode:
        say("bad", f"agentic MCP: pip failed: {got.stderr.strip()[-400:]}")
        return False
    stamp.write_text(want)
    return True


def cli(cfg: Dict[str, Any], *args: str) -> subprocess.CompletedProcess:
    return run([py(cfg), "-m", "agentic_mcp", *args], cwd=cfg["dir"], env=child_env(cfg))


def onboarded(cfg: Dict[str, Any]) -> List[str]:
    data = Path(cfg["data"])
    return sorted(f.name[:-len(".manifest.json")] for f in data.glob("*.manifest.json")) if data.is_dir() else []


def install_manifests(cfg: Dict[str, Any]) -> None:
    files = []
    for m in cfg.get("manifests") or []:
        f = Path(os.path.expandvars(str(m))).expanduser()
        f = f if f.is_absolute() else Path(cfg["dir"]) / f
        if f.is_file():
            files.append(str(f))
        else:
            say("warn", f"agentic MCP: no manifest {m} (in {cfg['dir']} for a relative path)")
    if not files:
        return
    got = cli(cfg, "install", "--if-missing", *files)
    if got.returncode:
        say("warn", f"agentic MCP: installing manifests: {(got.stderr or got.stdout).strip()[-300:]}")


def onboard(cfg: Dict[str, Any]) -> None:
    have = set(onboarded(cfg))
    for c in cfg.get("controllers") or []:
        key = str(c.get("key") or "")
        if not key or key in have:
            continue
        args = ["onboard", key, "--url", str(c["url"])]
        for opt, k in (("--label", "label"), ("--kind", "kind"), ("--user-env", "user_env"),
                       ("--password-env", "password_env"), ("--spec", "spec")):
            if c.get(k):
                args += [opt, str(c[k])]
        got = cli(cfg, *args)
        if got.returncode == 0 and key in onboarded(cfg):
            say("ok", f"agentic MCP: onboarded {key} ({c['url']})")
        else:
            say("warn", f"agentic MCP: {key} not onboarded yet (tried again at the next start): "
                        f"{(got.stderr or got.stdout).strip().splitlines()[-1:] or ['no answer']}")


def prepare() -> int:
    cfg = config()
    if not cfg["file"]:
        say("warn", f"agentic MCP: no {HERE / 'agentic_mcp.yaml'} (copy agentic_mcp.example.yaml): it starts with no controller")
    if not (get_code(cfg) and get_env(cfg)):
        return 1
    Path(cfg["data"]).mkdir(parents=True, exist_ok=True)
    if not Path(cfg["env_file"]).is_file() and any(c.get("user_env") or c.get("password_env") for c in cfg["controllers"]):
        say("warn", f"agentic MCP: no {cfg['env_file']} for the controllers' credentials (copy controllers.env.example)")
    seed_contract(cfg)
    install_manifests(cfg)
    onboard(cfg)
    return 0


def seed_contract(cfg: Dict[str, Any]) -> None:
    """The first time: the contract agentic_mcp.yaml names (contract:), or else the one its code comes with."""
    import shutil
    dest = Path(cfg["data"]) / "contract.json"
    if dest.exists():
        return
    src = Path(os.path.expandvars(str(cfg.get("contract") or ""))).expanduser() if cfg.get("contract") else \
        Path(cfg["dir"]) / "agentic_mcp" / "contract.json"
    if src.is_file():
        shutil.copyfile(src, dest)
        say("ok", f"agentic MCP: contract from {src} (kept in {dest}; change it on the SDN controllers page)")


def serve(port: str) -> int:
    cfg = config()
    os.chdir(cfg["dir"])
    os.execve(py(cfg), [py(cfg), "-m", "agentic_mcp", "serve", "--port", str(port)], child_env(cfg))
    return 0


def status() -> int:
    cfg = config()
    head = run(["git", "log", "-1", "--format=%h %s"], cwd=cfg["dir"]).stdout.strip() if Path(cfg["dir"]).is_dir() else "not cloned"
    print(json.dumps({"config": cfg["file"] or "(none: defaults)", "repo": cfg["repo"], "ref": cfg["ref"],
                      "code": f"{cfg['dir']} ({head})", "data": cfg["data"], "credentials": cfg["env_file"],
                      "onboarded": onboarded(cfg)}, indent=1))
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "prepare":
        sys.exit(prepare())
    if cmd == "serve":
        sys.exit(serve(sys.argv[2] if len(sys.argv) > 2 else "8400"))
    sys.exit(status())
