"""Provider abstraction — every backend (CLI or API) implements this contract.

Hardening this layer (instead of each provider) handles the cross-provider
failure modes V1 burned the most rounds on:
  - subprocess hangs that produce no output but never exit
  - Windows process trees leaking child workers that hold file locks
  - stderr drowned in MCP transport noise
  - lost forensics when a run produces bad code and we have no prompt log
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


# Stderr lines matching any of these are stripped before the log is written.
# Add patterns here only when (a) they appear in every run and (b) they
# carry no diagnostic value — never filter rate-limit or auth errors.
NOISE_PATTERNS: list[re.Pattern] = [
    re.compile(r"rmcp::transport::worker.*AuthRequired.*mcp\.linear\.app"),
]


@dataclass
class RunResult:
    provider: str
    exit_code: int
    elapsed_sec: float
    stdout_path: Path
    stderr_path: Path
    prompt_path: Path | None = None
    timed_out: bool = False
    kill_reason: str | None = None  # "timeout" | "idle" | None
    rate_limit_retry_after_sec: float | None = None
    skipped_reason: str | None = None  # set when run never spawned (slot/exhaust)

    @property
    def ok(self) -> bool:
        return (
            self.exit_code == 0
            and self.kill_reason is None
            and self.skipped_reason is None
        )


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the subprocess AND every child it spawned.

    On Windows, proc.kill() only stops the top pid; codex spawns node/MCP
    children that survive and hold file locks. taskkill /F /T walks the tree.
    """
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        try:
            os.killpg(os.getpgid(proc.pid), 15)  # SIGTERM
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass


@dataclass
class _WatcherState:
    kill_reason: str | None = None
    stop: threading.Event = field(default_factory=threading.Event)


def _idle_watcher(
    proc: subprocess.Popen,
    activity_paths: list[Path],
    idle_limit_sec: int,
    state: _WatcherState,
) -> None:
    """Kill the subprocess if NO log file's mtime advances for idle_limit_sec.

    Codex writes its real activity (reasoning, tool calls, diffs) to stderr,
    not stdout. Watching only stdout false-positived on every codex run.
    Track every file the subprocess might be writing; treat any one growing
    as "alive". Check cadence: 10s.
    """
    while not state.stop.is_set():
        if proc.poll() is not None:
            return
        latest = 0.0
        for p in activity_paths:
            try:
                m = p.stat().st_mtime
            except FileNotFoundError:
                continue
            if m > latest:
                latest = m
        if latest == 0.0:
            latest = time.time()  # nothing written yet — give it a tick
        if time.time() - latest > idle_limit_sec:
            state.kill_reason = "idle"
            _kill_tree(proc)
            return
        state.stop.wait(timeout=10)


def _filter_noise(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines()
        if not any(p.search(line) for p in NOISE_PATTERNS)
    )


def _strip_noise_in_place(path: Path) -> None:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return
    filtered = _filter_noise(raw)
    if filtered != raw:
        path.write_text(filtered, encoding="utf-8")


class Provider(ABC):
    name: str = "abstract"
    # Per-provider idle threshold. Override in subclass when the CLI is
    # legitimately quiet between bytes (Claude -p text only emits final text).
    idle_limit_sec: int = 60

    @abstractmethod
    def build_argv(self, prompt: str, cwd: Path) -> list[str]:
        ...

    def env(self) -> dict[str, str] | None:
        return None

    def run(
        self,
        prompt: str,
        cwd: Path,
        log_dir: Path,
        timeout_sec: int = 900,
    ) -> RunResult:
        cwd.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        stdout_path = log_dir / f"{self.name}_{ts}.stdout.log"
        stderr_path = log_dir / f"{self.name}_{ts}.stderr.log"
        prompt_path = log_dir / f"{self.name}_{ts}.prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")

        argv = self.build_argv(prompt, cwd)
        resolved = shutil.which(argv[0])
        if resolved is None:
            raise FileNotFoundError(
                f"{self.name}: CLI binary {argv[0]!r} not on PATH — install it or "
                f"correct the name in {type(self).__name__}.build_argv()."
            )
        argv[0] = resolved

        merged_env = os.environ.copy()
        overrides = self.env()
        if overrides:
            merged_env.update(overrides)

        started = time.monotonic()
        watcher_state = _WatcherState()
        watcher_thread: threading.Thread | None = None
        timed_out = False

        try:
            with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
                proc = subprocess.Popen(
                    argv,
                    cwd=str(cwd),
                    stdout=out,
                    stderr=err,
                    stdin=subprocess.DEVNULL,
                    env=merged_env,
                )
                watcher_thread = threading.Thread(
                    target=_idle_watcher,
                    args=(
                        proc,
                        [stdout_path, stderr_path],
                        self.idle_limit_sec,
                        watcher_state,
                    ),
                    daemon=True,
                )
                watcher_thread.start()

                try:
                    exit_code = proc.wait(timeout=timeout_sec)
                except subprocess.TimeoutExpired:
                    watcher_state.kill_reason = "timeout"
                    _kill_tree(proc)
                    proc.wait()
                    exit_code = -1
                    timed_out = True
        finally:
            watcher_state.stop.set()
            if watcher_thread is not None:
                watcher_thread.join(timeout=2)

        _strip_noise_in_place(stderr_path)

        return RunResult(
            provider=self.name,
            exit_code=exit_code,
            elapsed_sec=round(time.monotonic() - started, 2),
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            prompt_path=prompt_path,
            timed_out=timed_out,
            kill_reason=watcher_state.kill_reason,
        )

    def make_skip_result(
        self, log_dir: Path, reason: str, prompt: str | None = None
    ) -> RunResult:
        """Helper for subclasses that abort before spawning (slot busy,
        exhausted, etc.). Writes the prompt log so post-mortems still have
        the inputs."""
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        prompt_path = log_dir / f"{self.name}_{ts}.prompt.txt"
        if prompt is not None:
            prompt_path.write_text(prompt, encoding="utf-8")
        return RunResult(
            provider=self.name,
            exit_code=-1,
            elapsed_sec=0.0,
            stdout_path=log_dir / f"{self.name}_{ts}.stdout.log",
            stderr_path=log_dir / f"{self.name}_{ts}.stderr.log",
            prompt_path=prompt_path if prompt is not None else None,
            skipped_reason=reason,
        )


# ---------------------------------------------------------------------------
# M2.5/M3 routing layer. `Provider` above is the M2 CLI-subprocess abstraction;
# `ProviderBase` below is the unified role-adapter interface the router speaks
# to (CliAdapter wraps a Provider; ApiAdapter wraps a LiteLLM model). Kept in
# the same module because the spec names providers/base.py as its home.
# ---------------------------------------------------------------------------

class ProviderBase(ABC):
    """A role-assignable provider: either a CLI tool (native file access) or an
    API model (needs the tool harness). category is "cli" or "api"."""

    category: str = "abstract"

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def send_message(self, messages: list[dict]) -> str:
        """One request/response turn; returns the assistant's text."""

    @abstractmethod
    def send_with_tools(self, messages: list[dict], ctx, max_iterations: int = 30) -> dict:
        """Run the agent with filesystem/shell tools until it finishes."""
