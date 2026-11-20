"""
The agent's sandbox for `execute`: a locked-down local Docker container,
started with the agent so the tool is there from the first message.

`open_sandbox()` builds the image if needed, starts the container, checks it
runs a command, and gives the deepagents backend. If Docker is not usable the
agent starts without it and says so (`settings.sandbox = "none"` turns it off
on purpose, e.g. for tests or a system without Docker).
"""
from __future__ import annotations

from typing import Tuple

from .backend import SandboxBackend
from .docker import DockerRuntime

KINDS = ("docker", "none")


def check() -> Tuple[bool, str]:
    """Whether the Docker sandbox can be used here, and what it is or why not."""
    return DockerRuntime().available()


def open_sandbox() -> SandboxBackend:
    """The started, checked sandbox backend."""
    runtime = DockerRuntime()
    ok, why = runtime.available()
    if not ok:
        raise RuntimeError(f"the sandbox is not available: {why}")
    backend = SandboxBackend(runtime)
    runtime.start()
    got = backend.execute("echo ready && id -u && pwd", timeout=60)
    if got.exit_code != 0 or "ready" not in got.output:
        backend.close()
        raise RuntimeError(f"the sandbox did not run a test command: {got.output[:300]}")
    return backend


__all__ = ["KINDS", "SandboxBackend", "DockerRuntime", "check", "open_sandbox"]
