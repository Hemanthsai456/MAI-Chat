"""Approved-tool runner with streaming bounded output capture and process lifecycle controls.

Rules enforced here:
  * only tools registered in the approved registry can run;
  * arguments are strictly validated against tool specifications;
  * the process is started with an argument list and shell=False (no shell injection);
  * the child gets a minimal, safe environment allowlist (never leaking API keys);
  * timeouts and user cancellation terminate the entire process tree cross-platform;
  * stdout and stderr are captured incrementally with bounded in-flight memory;
  * exit code, duration, stdout, and stderr are captured and returned.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .registry import _REGISTRY, ApprovedTool, get_tool

PROJECT_DIR = Path(__file__).resolve().parent.parent
TOOLS_DIR = PROJECT_DIR / "tools"
MAX_CAPTURE_BYTES = 2_000_000

# Proxy, CA and OS-critical runtime variables.
# API keys and security secrets are deliberately EXCLUDED.
CHILD_ENV_ALLOWLIST = (
    "PATH", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP",
    "SYSTEMROOT", "SystemRoot", "WINDIR", "windir", "SYSTEMDRIVE",
    "LOCALAPPDATA", "APPDATA", "COMSPEC", "PATHEXT",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
    "BUILDSPRINT_ALLOW_PRIVATE_TARGETS",
)

# Backward-compatibility alias
APPROVED_TOOLS = _REGISTRY


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
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class _StreamCapture(threading.Thread):
    """Thread that reads a process stream incrementally up to a strict byte limit."""

    def __init__(self, stream, limit_bytes: int) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.limit_bytes = limit_bytes
        self.buffer = bytearray()
        self.truncated = False
        self.total_bytes_read = 0

    def run(self) -> None:
        try:
            chunk_size = 4096
            while True:
                chunk = self.stream.read(chunk_size)
                if not chunk:
                    break
                self.total_bytes_read += len(chunk)
                remaining = self.limit_bytes - len(self.buffer)
                if remaining > 0:
                    self.buffer.extend(chunk[:remaining])
                if len(self.buffer) >= self.limit_bytes and len(chunk) > remaining:
                    self.truncated = True
        except (ValueError, OSError):
            pass
        finally:
            try:
                self.stream.close()
            except Exception:
                pass

    def get_text(self) -> tuple[str, bool]:
        text = self.buffer.decode("utf-8", errors="replace")
        return text, self.truncated


def _kill_proc_tree(proc: subprocess.Popen) -> None:
    """Safely terminate a process and its child processes on Windows or POSIX."""
    if proc.poll() is not None:
        return

    pid = proc.pid
    if sys.platform == "win32":
        # Windows: Use taskkill to kill the whole process tree (/T) forcefully (/F)
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    else:
        # POSIX: Send SIGKILL to the process group
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def run_approved_tool(
    name: str,
    args: list[str],
    timeout_s: float,
    cancel_event: threading.Event | None = None,
    max_output_bytes: int = MAX_CAPTURE_BYTES,
) -> CliResult:
    """Run an approved tool with bounded execution and streaming output capture."""
    tool = get_tool(name)
    if tool is None:
        raise KeyError(f"tool '{name}' is not on the approved list")

    if not all(isinstance(arg, str) for arg in args):
        raise TypeError("CLI arguments must be strings")

    # Validate arguments against the tool's specification
    validated_args = tool.validate_args(args)

    timeout = min(max(float(timeout_s), 1.0), tool.max_timeout_s)
    # -I runs Python in isolated mode (ignores user site packages and PYTHONPATH)
    argv = [sys.executable, "-I", str(tool.script), *validated_args]

    # Build safe environment
    env: dict[str, str] = {}
    for key in CHILD_ENV_ALLOWLIST:
        if key in os.environ:
            env[key] = os.environ[key]
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    # Automatically set SSL_CERT_FILE if certifi is installed and not explicitly configured
    if "SSL_CERT_FILE" not in env:
        try:
            import certifi
            env["SSL_CERT_FILE"] = certifi.where()
        except ImportError:
            pass

    timed_out = cancelled = False
    started = time.monotonic()
    cancel_ev = cancel_event or threading.Event()

    workdir = tempfile.mkdtemp(prefix="bs-task-")
    proc = None
    try:
        # Pre-execution cancellation check
        if cancel_ev.is_set():
            return CliResult(
                tool=name,
                args=list(validated_args),
                exit_code=-1,
                stdout="",
                stderr="Task was cancelled prior to tool execution.",
                duration_s=0.0,
                timeout_s=timeout,
                timed_out=False,
                cancelled=True,
            )

        popen_kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": env,
            "cwd": workdir,
            "shell": False,
        }

        # On POSIX start a new session for process group isolation
        if sys.platform != "win32":
            popen_kwargs["start_new_session"] = True
        else:
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        proc = subprocess.Popen(argv, **popen_kwargs)

        # Launch streaming output reader threads to prevent pipe deadlocks
        stdout_capture = _StreamCapture(proc.stdout, max_output_bytes)
        stderr_capture = _StreamCapture(proc.stderr, max_output_bytes)
        stdout_capture.start()
        stderr_capture.start()

        # Monitor loop
        while proc.poll() is None:
            if cancel_ev.is_set():
                cancelled = True
                _kill_proc_tree(proc)
                break
            if time.monotonic() - started >= timeout:
                timed_out = True
                _kill_proc_tree(proc)
                break
            time.sleep(0.05)

        # Wait for process exit and reader thread termination
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            _kill_proc_tree(proc)
            proc.wait(timeout=2.0)

        stdout_capture.join(timeout=1.0)
        stderr_capture.join(timeout=1.0)

        stdout_str, stdout_trunc = stdout_capture.get_text()
        stderr_str, stderr_trunc = stderr_capture.get_text()

    finally:
        # Robust cleanup of temporary directory (retrying on Windows file lock delays)
        if proc and proc.poll() is None:
            _kill_proc_tree(proc)

        def _cleanup():
            for _ in range(5):
                try:
                    shutil.rmtree(workdir, ignore_errors=False)
                    break
                except Exception:
                    time.sleep(0.1)
            else:
                shutil.rmtree(workdir, ignore_errors=True)

        _cleanup()

    duration = round(time.monotonic() - started, 2)
    return CliResult(
        tool=name,
        args=list(validated_args),
        exit_code=proc.returncode if proc else None,
        stdout=stdout_str,
        stderr=stderr_str,
        duration_s=duration,
        timeout_s=timeout,
        timed_out=timed_out,
        cancelled=cancelled,
        stdout_truncated=stdout_trunc,
        stderr_truncated=stderr_trunc,
    )
