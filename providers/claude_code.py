"""Claude Code CLI provider.

Spawns `claude -p` in print (headless) mode with permissions skipped, routed
through the user's claude-test 2-pro config dir. Supports model tiering
(--model sonnet|opus) and parses the session-limit message so the orchestrator
can park until the quota window resets.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from providers import availability
from providers.base import Provider, RunResult


CLAUDE_TEST_CONFIG_DIR = str(Path.home() / ".claude-test")

# "You've hit your session limit · resets 10:30pm (Asia/Calcutta)" — a CLOCK
# time in the account's tz, which matches the machine tz. Parse to a reset ts.
_RESET_RE = re.compile(r"resets?\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)", re.IGNORECASE)
_LIMIT_RE = re.compile(r"(session|usage|weekly)\s+limit|hit your .*limit", re.IGNORECASE)


def _parse_reset(text: str, now: datetime | None = None) -> float | None:
    """Reset time from a claude limit message -> unix ts (next occurrence)."""
    m = _RESET_RE.search(text)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    ap = m.group(3).lower()
    if ap == "pm" and hour != 12:
        hour += 12
    if ap == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    now = now or datetime.now()
    reset = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if reset <= now:
        reset += timedelta(days=1)  # already past today -> tomorrow
    return reset.timestamp()


class ClaudeCodeProvider(Provider):
    name = "claude"
    # stream-json emits a JSONL event per tool call / message chunk, so the
    # mtime-based idle watcher gets real liveness. 300s covers a single long
    # tool call (cold pip install) while still catching true hangs.
    idle_limit_sec = 300
    # Prompt via stdin (argv would hit the ~32K CreateProcess ceiling).
    stdin_prompt = True

    def __init__(self, model: str | None = None):
        # model: "sonnet" for waves (cheap), "opus"/None for architect/fixes.
        self.model = model

    def env(self) -> dict[str, str]:
        return {"CLAUDE_CONFIG_DIR": CLAUDE_TEST_CONFIG_DIR}

    def build_argv(self, prompt: str, cwd: Path) -> list[str]:
        argv = [
            "claude",
            "--dangerously-skip-permissions",
            "--print",
            # stream-json requires --verbose in -p mode (CLI enforces it).
            "--output-format", "stream-json",
            "--verbose",
        ]
        if self.model:
            argv += ["--model", self.model]
        return argv

    def run(self, prompt: str, cwd: Path, log_dir: Path, timeout_sec: int = 900) -> RunResult:
        result = super().run(prompt, cwd, log_dir, timeout_sec)
        # Detect a quota cap so the orchestrator can park until it resets (#10).
        try:
            out = result.stdout_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        except OSError:
            out = ""
        if _LIMIT_RE.search(out):
            reset = _parse_reset(out)
            if reset:
                availability.mark_exhausted(self.name, reset)
                result.rate_limit_retry_after_sec = max(0.0, reset - time.time())
        return result
