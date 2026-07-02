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
    # `claude -p text` only flushes the final text; tool calls during the
    # session produce no stdout. 60s would false-positive on legitimate
    # multi-tool rounds. 180s is the V1-calibrated safe ceiling.
    idle_limit_sec = 180

    def env(self) -> dict[str, str]:
        return {"CLAUDE_CONFIG_DIR": CLAUDE_TEST_CONFIG_DIR}

    def build_argv(self, prompt: str, cwd: Path) -> list[str]:
        return [
            "claude",
            "--dangerously-skip-permissions",
            "--print",
            "--output-format", "text",
            prompt,
        ]
