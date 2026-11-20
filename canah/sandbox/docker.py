"""
The sandbox as a local Docker container.

One container per agent process, started on first use and removed on close:

  --network none              nothing in or out
  --read-only, tmpfs /tmp     only /workspace (a named volume) and /tmp can be written
  --user 1000                 not root
  --cap-drop ALL, no-new-privileges, pids/memory/CPU limits
  --runtime runsc             gVisor, when Docker has it (CANAH_SANDBOX_RUNTIME, "auto" by default)

Commands go in with `docker exec`, so the container needs no network at all.
The workspace volume outlives the container (CANAH_SANDBOX_KEEP_WORKSPACE=1,
the default), so files the agent wrote are there next time.

  CANAH_SANDBOX_IMAGE      canah-sandbox:1 (built from canah/sandbox/Dockerfile if missing)
  CANAH_DOCKER_CONTEXT     the Docker context; "default" (the local engine) unless set
  CANAH_SANDBOX_MEMORY     1g        CANAH_SANDBOX_CPUS  2        CANAH_SANDBOX_PIDS  256
  CANAH_SANDBOX_NETWORK    none      (or a Docker network name, to let it reach one)
"""
from __future__ import annotations

import logging
import os
import secrets
import subprocess
import threading
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger("canah.sandbox.docker")

HERE = Path(__file__).resolve().parent


class DockerRuntime:
    name = "docker"

    def __init__(self, image: str = "", context: str = "", workspace: str = "", network: str = "",
                 memory: str = "", cpus: str = "", pids: str = "", runtime: str = ""):
        self.image = image or os.getenv("CANAH_SANDBOX_IMAGE", "canah-sandbox:1")
        self.context = context or os.getenv("CANAH_DOCKER_CONTEXT", "default")
        self.workspace = workspace or os.getenv("CANAH_SANDBOX_WORKSPACE", "canah-workspace")
        self.network = network or os.getenv("CANAH_SANDBOX_NETWORK", "none")
        self.memory = memory or os.getenv("CANAH_SANDBOX_MEMORY", "1g")
        self.cpus = cpus or os.getenv("CANAH_SANDBOX_CPUS", "2")
        self.pids = pids or os.getenv("CANAH_SANDBOX_PIDS", "256")
        self.want_runtime = runtime or os.getenv("CANAH_SANDBOX_RUNTIME", "auto")
        self.container = ""
        self.isolation = ""
        self._lock = threading.Lock()

    def _docker(self, *args: str, timeout: float = 120, check: bool = True) -> subprocess.CompletedProcess:
        cmd = ["docker", "--context", self.context, *args]
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if check and done.returncode != 0:
            raise RuntimeError(f"docker {args[0]}: {(done.stderr or done.stdout).strip()[:400]}")
        return done

    def available(self) -> Tuple[bool, str]:
        try:
            done = self._docker("version", "--format", "{{.Server.Version}}", timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"docker is not usable: {e}"
        if done.returncode != 0:
            return False, f"the Docker engine ({self.context}) is not answering: {done.stderr.strip()[:200]}"
        return True, f"Docker {done.stdout.strip()} ({self.context})"

    def _runtime_flag(self) -> List[str]:
        if self.want_runtime in ("runc", "default", ""):
            self.isolation = "runc (a container)"
            return []
        info = self._docker("info", "--format", "{{json .Runtimes}}", timeout=20, check=False).stdout
        if "runsc" in info:
            self.isolation = "gVisor (runsc)"
            return ["--runtime", "runsc"]
        if self.want_runtime == "runsc":
            raise RuntimeError("gVisor (runsc) was asked for, and Docker does not have it")
        self.isolation = "runc (a container; install gVisor for a stronger boundary)"
        return []

    def _ensure_image(self) -> None:
        if self._docker("image", "inspect", self.image, check=False, timeout=30).returncode == 0:
            return
        logger.info("building the sandbox image %s", self.image)
        self._docker("build", "-q", "-t", self.image, str(HERE), timeout=900)

    def reap(self) -> list:
        """Remove sandboxes left behind by agents on this host that are no longer running (a killed process
        never closes its container). Containers of agents still running are left alone."""
        import socket
        host = socket.gethostname()
        got = self._docker("ps", "-a", "--filter", "label=canah.sandbox=1", "--format",
                           '{{.Names}}\t{{.Label "canah.owner"}}', check=False, timeout=30).stdout
        gone = []
        for line in got.splitlines():
            name, _, owner = line.partition("\t")
            h, _, pid = owner.rpartition(":")
            if not owner or h != host or not pid.isdigit() or int(pid) == os.getpid():
                continue
            try:
                os.kill(int(pid), 0)
                continue                                  # its agent is still running
            except ProcessLookupError:
                pass
            except PermissionError:
                continue
            self._docker("rm", "-f", name, check=False, timeout=60)
            gone.append(name)
        if gone:
            logger.info("removed %d sandbox(es) left by agents no longer running: %s", len(gone), ", ".join(gone))
        return gone

    def start(self) -> None:
        with self._lock:
            if self.container:
                return
            ok, why = self.available()
            if not ok:
                raise RuntimeError(why)
            self.reap()
            self._ensure_image()
            name = f"canah-sbx-{secrets.token_hex(4)}"
            import socket
            args = ["run", "-d", "--rm", "--name", name, "--label", "canah.sandbox=1",
                    "--label", f"canah.owner={socket.gethostname()}:{os.getpid()}",
                    "--network", self.network, "--read-only",
                    "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m",
                    "-v", f"{self.workspace}:/workspace",
                    "--user", "1000:1000", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "--pids-limit", self.pids, "--memory", self.memory, "--cpus", self.cpus,
                    *self._runtime_flag(), self.image, "sleep", "infinity"]
            self._docker(*args, timeout=120)
            self.container = name
            # A fresh named volume is root-owned; hand it to the sandbox user once.
            self._docker("exec", "-u", "0", name, "chown", "1000:1000", "/workspace", check=False, timeout=30)
            logger.info("sandbox %s started (%s, network %s)", name, self.isolation, self.network)

    def run(self, argv: List[str], timeout: float, cwd: str = "/workspace") -> Tuple[str, str, int]:
        self.start()
        try:
            done = subprocess.run(["docker", "--context", self.context, "exec", "-w", cwd, self.container,
                                   "timeout", "--kill-after=5", str(int(max(1, timeout))), *argv],
                                  capture_output=True, timeout=timeout + 30)
        except subprocess.TimeoutExpired:
            return "", f"timed out after {timeout:g}s", -1
        out, err = done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace")
        code = done.returncode
        if code == 124:
            err += f"\n(killed at its timeout, {timeout:g}s)"
        return out, err, code

    def close(self) -> None:
        with self._lock:
            if self.container:
                self._docker("rm", "-f", self.container, check=False, timeout=60)
                logger.info("sandbox %s removed", self.container)
                self.container = ""
            if os.getenv("CANAH_SANDBOX_KEEP_WORKSPACE", "1") == "0":
                self._docker("volume", "rm", "-f", self.workspace, check=False, timeout=60)

    def describe(self) -> str:
        return f"Docker container {self.container or '(not started)'}, {self.isolation or 'isolation chosen at start'}, " \
               f"network {self.network}, {self.memory} memory, {self.cpus} CPUs"
