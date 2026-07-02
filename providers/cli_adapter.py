"""Category A adapter — CLI tools (claude, codex) that own their file access.

Thin role-layer wrapper over the hardened M2 providers (ClaudeCodeProvider /
CodexProvider): those already handle subprocess spawning, idle watchdog, tree
kill, and rate-limit parsing, so we delegate rather than re-implement. Skills
reach a CLI model by writing `.echara/skills_index.md` into the project dir,
which the CLI tool reads natively.
"""
from __future__ import annotations

from pathlib import Path

from providers import PROVIDERS
from providers.base import ProviderBase

# config command name -> hardened M2 provider key
_CLI_IMPL = {"claude": "claude", "codex": "codex"}


class CliAdapter(ProviderBase):
    category = "cli"

    def __init__(self, name: str, command: str, project_dir: Path | str = "."):
        super().__init__(name)
        self.command = command
        self.project_dir = Path(project_dir)

    def setup_skills(self, skill_index: str, project_dir: Path | str | None = None) -> Path:
        """Write the frontmatter index where the CLI model will read it."""
        base = Path(project_dir) if project_dir is not None else self.project_dir
        dest = base / ".echara" / "skills_index.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(skill_index, encoding="utf-8")
        return dest

    def _impl(self):
        key = _CLI_IMPL.get(self.command)
        if key is None:
            raise ValueError(f"no CLI implementation for command {self.command!r}")
        return PROVIDERS[key]()

    def send_message(self, messages: list[dict], cwd: Path | None = None,
                     log_dir: Path | None = None):
        """Run the CLI on the last user message. Returns the M2 RunResult
        (the CLI writes files directly; there is no chat text to return)."""
        prompt = messages[-1]["content"] if messages else ""
        cwd = cwd or self.project_dir
        log_dir = log_dir or (Path(cwd) / "logs")
        return self._impl().run(prompt, Path(cwd), Path(log_dir))

    def send_with_tools(self, messages: list[dict], ctx=None, max_iterations: int = 30):
        # CLI tools have their own tool loop; this is just a run.
        return self.send_message(messages)
