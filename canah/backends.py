"""
Where the agent's files live: one CompositeBackend, routed by path.

  /             scratch: notes and drafts of this conversation (thread state)
  /memories/    long-term memory, in the store, per user (memory.py)
  /skills/<n>/  each skills folder, from disk, read-only
  /memory/<n>/  each AGENTS.md's folder, from disk, read-only

and, when a sandbox is configured (phase 3), commands run there.

The read-only parts are enforced with permissions (`permissions()`), which
deepagents applies to its filesystem tools for the main agent and every
subagent.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

from deepagents import FilesystemPermission
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend, StoreBackend
from deepagents.backends.protocol import BackendProtocol

from . import memory as mem
from .config import Settings


def _mounts(settings: Settings) -> Tuple[Dict[str, Path], Dict[str, Path]]:
    skills, memories = {}, {}
    for i, d in enumerate(settings.skills_dirs):
        p = Path(d).expanduser().resolve()
        if p.is_dir():
            skills[f"/skills/{i}-{p.name}/"] = p
    for i, f in enumerate(settings.memory_files):
        p = Path(f).expanduser().resolve()
        if p.is_file():
            memories[f"/memory/{i}-{p.parent.name}/"] = p.parent
    return skills, memories


def build(settings: Settings, sandbox: BackendProtocol | None = None) -> Tuple[BackendProtocol, List[str], List[str]]:
    """The backend, the skill source paths and the memory file paths, as the
    agent sees them."""
    skills, memories = _mounts(settings)
    routes: Dict[str, BackendProtocol] = {}
    for route, root in {**skills, **memories}.items():
        routes[route] = FilesystemBackend(root_dir=root, virtual_mode=True)
    if sandbox is None:
        # Large tool results kept on disk (large_results.py), readable by the file tools.
        results = settings.home_path / "results"
        results.mkdir(parents=True, exist_ok=True)
        routes["/results/"] = FilesystemBackend(root_dir=results, virtual_mode=True)
    if settings.long_term_memory:
        routes["/memories/"] = StoreBackend(namespace=lambda rt: mem.namespace(mem.user_of(rt)))
    default: BackendProtocol = sandbox if sandbox is not None else StateBackend()
    # deepagents keeps its own offloaded results under the artifacts root; in the sandbox only
    # /workspace can be written.
    backend = CompositeBackend(default=default, routes=routes,
                               artifacts_root="/workspace" if sandbox is not None else "/")
    memory_paths = []
    for f in settings.memory_files:
        p = Path(f).expanduser().resolve()
        for route, root in memories.items():
            if p.parent == root:
                memory_paths.append(route + p.name)
    return backend, list(skills), memory_paths


def permissions(settings: Settings, sandbox: BackendProtocol | None = None) -> List[FilesystemPermission]:
    """Read-only rules for the routed folders. Only for paths routed away from a sandbox: deepagents
    cannot enforce a file rule against `execute`, and refuses one on a path the sandbox would serve."""
    skills, memories = _mounts(settings)
    ro = [r + "**" for r in (*skills, *memories)] + (["/results/**"] if sandbox is None else [])
    rules = [FilesystemPermission(operations=["write"], paths=ro, mode="deny")] if ro else []
    if settings.approve_file_writes:
        rules.append(FilesystemPermission(operations=["write"], paths=["/**"], mode="interrupt"))
    return rules
