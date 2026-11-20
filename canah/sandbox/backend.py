"""
The deepagents backend over a sandbox: `execute`, and files through it.

A runtime only has to run an argv with a timeout and give back
(stdout, stderr, exit code). Everything else is built on that:

  execute(command)      bash -c <command> in /workspace
  upload_files          base64 through a small shell script (no extra port, no shared volume);
                        the agent's files live under /workspace, the one writable place
  download_files        the same, the other way
  ls/read/grep/glob/edit  deepagents' BaseSandbox, on top of execute

The runtime is the Docker container in docker.py (`docker exec`); another
runtime with a `run` would slot in the same way.
"""
from __future__ import annotations

import base64
import posixpath
import shlex
from typing import List, Protocol, Tuple

from deepagents.backends.protocol import ExecuteResponse, FileDownloadResponse, FileUploadResponse
from deepagents.backends.sandbox import BaseSandbox

WORKDIR = "/workspace"
MAX_OUTPUT_CHARS = 30000
CHUNK = 60000          # bytes of base64 per command line, well under ARG_MAX


class Runtime(Protocol):
    name: str

    def run(self, argv: List[str], timeout: float, cwd: str = WORKDIR) -> Tuple[str, str, int]: ...

    def close(self) -> None: ...


def _in_workspace(path: str) -> str:
    """The real path in the sandbox. Absolute paths are used as they are (deepagents' own read/ls/grep
    pass them through unchanged, so a remapping here would make writes and reads disagree); a relative
    one is inside /workspace."""
    path = str(path)
    return posixpath.normpath(path if path.startswith("/") else posixpath.join(WORKDIR, path))


class SandboxBackend(BaseSandbox):
    def __init__(self, runtime: Runtime, default_timeout: float = 120.0):
        self.runtime = runtime
        self.default_timeout = default_timeout

    @property
    def id(self) -> str:
        return f"canah-sandbox:{self.runtime.name}"

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        try:
            out, err, code = self.runtime.run(["bash", "-c", command], float(timeout or self.default_timeout))
        except Exception as e:  # noqa: BLE001 - told to the model, not raised through the agent
            return ExecuteResponse(output=f"Error: the sandbox did not run it: {type(e).__name__}: {e}",
                                   exit_code=None)
        text = out + (("\n" if out and err else "") + err if err else "")
        truncated = len(text) > MAX_OUTPUT_CHARS
        if truncated:
            text = text[:MAX_OUTPUT_CHARS] + f"\n... [{len(text) - MAX_OUTPUT_CHARS} more characters cut]"
        return ExecuteResponse(output=text, exit_code=code, truncated=truncated)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        out = []
        for path, data in files:
            target = _in_workspace(path)
            try:
                b64 = base64.b64encode(bytes(data)).decode()
                tmp = target + ".canah-upload"
                steps = [f"mkdir -p {shlex.quote(posixpath.dirname(target))} && : > {shlex.quote(tmp)}"]
                for i in range(0, len(b64), CHUNK):
                    steps.append(f"printf %s {shlex.quote(b64[i:i + CHUNK])} >> {shlex.quote(tmp)}")
                steps.append(f"base64 -d {shlex.quote(tmp)} > {shlex.quote(target)} && rm -f {shlex.quote(tmp)}")
                for step in steps:
                    _, err, code = self.runtime.run(["sh", "-c", step], self.default_timeout)
                    if code != 0:
                        raise OSError(err.strip() or f"exit {code}")
                out.append(FileUploadResponse(path=path, error=None))
            except Exception as e:  # noqa: BLE001 - partial success, as the protocol asks
                out.append(FileUploadResponse(path=path, error=_upload_error(e)))
        return out

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        out = []
        for path in paths:
            target = _in_workspace(path)
            try:
                got, err, code = self.runtime.run(["sh", "-c", f"base64 -w0 {shlex.quote(target)}"],
                                                  self.default_timeout)
                if code != 0:
                    out.append(FileDownloadResponse(path=path, content=None,
                                                    error="file_not_found" if "No such file" in err
                                                    else "permission_denied" if "denied" in err else "invalid_path"))
                    continue
                out.append(FileDownloadResponse(path=path, content=base64.b64decode(got.strip() or b""), error=None))
            except Exception:  # noqa: BLE001
                out.append(FileDownloadResponse(path=path, content=None, error="invalid_path"))
        return out

    def close(self) -> None:
        self.runtime.close()


def _upload_error(e: Exception) -> str:
    text = str(e).lower()
    if "denied" in text:
        return "permission_denied"
    if "is a directory" in text:
        return "is_directory"
    return "invalid_path"
