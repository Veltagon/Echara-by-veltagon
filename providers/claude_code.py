"""Claude Code CLI provider.

Spawns `claude -p` in print (headless) mode with permissions skipped, routed
through the user's claude-test 2-pro config dir.
"""
from __future__ import annotations

import os
from pathlib import Path

from providers.base import Provider


CLAUDE_TEST_CONFIG_DIR = str(Path.home() / ".claude-test")


class ClaudeCodeProvider(Provider):
    name = "claude"
    # stream-json emits a JSONL event per tool call / message chunk, so the
    # mtime-based idle watcher gets real liveness signal (verified: events
    # flush to the log file incrementally, not in one burst at exit). The
    # remaining silent window is a single long-running tool call (e.g. a cold
    # `pip install`) between its tool_use and tool_result events — 300s covers
    # that while still catching true hangs in 5 minutes. The old `text` format
    # was silent until completion, so builds longer than the idle limit were
    # killed even when healthy (M4 Test 3 run 1).
    idle_limit_sec = 300
    # Prompt via stdin (probe-verified: `echo ... | claude -p` works). Wave
    # builds embed 40-file plans — argv would hit the ~32K CreateProcess
    # ceiling the codex .cmd shim already hit at 8K.
    stdin_prompt = True

    def env(self) -> dict[str, str]:
        return {"CLAUDE_CONFIG_DIR": CLAUDE_TEST_CONFIG_DIR}

    def build_argv(self, prompt: str, cwd: Path) -> list[str]:
        return [
            "claude",
            "--dangerously-skip-permissions",
            "--print",
            # stream-json requires --verbose in -p mode (CLI enforces it).
            "--output-format", "stream-json",
            "--verbose",
        ]
