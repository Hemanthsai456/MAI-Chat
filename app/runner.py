"""Approved-tool registry and the single place that starts subprocesses.

Rules enforced here:
  * only tools listed in APPROVED_TOOLS can run;
  * the process is started with an argument list and shell=False, so no
    user or model text is ever interpreted by a shell;
  * the child gets a small environment allowlist (no API keys);
  * timeouts and user cancellation kill the whole process group;
  * stdout, stderr and the exit code are captured and returned to the caller.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
TOOLS_DIR = PROJECT_DIR / "tools"
MAX_CAPTURE_BYTES = 2_000_000
# Proxy and CA variables are passed through so the crawler can reach the web in
# corporate networks. Secrets such as API keys are deliberately NOT on this list.
CHILD_ENV_ALLOWLIST = (
    "PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
)


@dataclass(frozen=True)
class ApprovedTool:
    name: str
    script: Path
    description: str
    max_timeout_s: float


APPROVED_TOOLS: dict[str, ApprovedTool] = {
    tool.name: tool
    for tool in (
        ApprovedTool(
            name="site-crawler",
            script=TOOLS_DIR / "site_crawler.py",
            description="Bounded, same-host website crawler that prints one JSON document.",
            max_timeout_s=120.0,
        ),
    )
}


@dataclass
class CliResult:
    tool: str
    args: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float
    timeout_s: float
    timed_out: bool
    cancelled: bool


def _decode(data: bytes, limit: int) -> str:
    return data[:limit].decode("utf-8", errors="replace")


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # start_new_session=True makes pid the group id
    except ProcessLookupError:
        pass
    except OSError:
        proc.kill()


def run_approved_tool(
    name: str,
    args: list[str],
    timeout_s: float,
    cancel_event,
    max_output_bytes: int = MAX_CAPTURE_BYTES,
) -> CliResult:
    tool = APPROVED_TOOLS.get(name)
    if tool is None:
        raise KeyError(f"tool '{name}' is not on the approved list")
    if not all(isinstance(arg, str) for arg in args):
        raise TypeError("CLI arguments must be strings")

    timeout = min(max(float(timeout_s), 1.0), tool.max_timeout_s)
    argv = [sys.executable, "-I", str(tool.script), *args]  # -I: isolated interpreter mode
    env = {key: os.environ[key] for key in CHILD_ENV_ALLOWLIST if key in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    timed_out = cancelled = False
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="bs-task-") as workdir:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=workdir,
            shell=False,
            start_new_session=True,
        )
        while True:
            try:
                out, err = proc.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if cancel_event.is_set():
                    cancelled = True
                elif time.monotonic() - started >= timeout:
                    timed_out = True
                else:
                    continue
                _kill_group(proc)
                out, err = proc.communicate()
                break

    return CliResult(
        tool=name,
        args=list(args),
        exit_code=proc.returncode,
        stdout=_decode(out, max_output_bytes),
        stderr=_decode(err, max_output_bytes),
        duration_s=round(time.monotonic() - started, 2),
        timeout_s=timeout,
        timed_out=timed_out,
        cancelled=cancelled,
    )
