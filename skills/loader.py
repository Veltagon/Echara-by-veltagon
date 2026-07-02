"""Skill file access + token counting.

Reads SKILL.md files from the pool: the frontmatter (name + description) for the
index, and the full body on demand. Frontmatter parsing is reused from
harness.skills — one parser for the whole project.
"""
from __future__ import annotations

from pathlib import Path

import tiktoken

from harness.skills import _parse_frontmatter  # reuse: single frontmatter parser

# cl100k_base is the tokenizer for GPT-4/4o and a close proxy for others — good
# enough for budgeting; we don't need per-model exactness here.
_ENC = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_ENC.encode(text or ""))


class SkillLoader:
    """Resolves flat skill IDs (`senior-backend`) to `<pool>/<id>/SKILL.md`."""

    def __init__(self, pool_root: Path | str):
        self.pool_root = Path(pool_root)

    def skill_md(self, skill_id: str) -> Path:
        return self.pool_root / skill_id / "SKILL.md"

    def exists(self, skill_id: str) -> bool:
        return self.skill_md(skill_id).is_file()

    def frontmatter(self, skill_id: str) -> tuple[str, str]:
        """(name, description) from the skill's YAML frontmatter."""
        p = self.skill_md(skill_id)
        if not p.is_file():
            raise FileNotFoundError(f"no SKILL.md for skill {skill_id!r} under {self.pool_root}")
        fm = _parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        name = str(fm.get("name") or skill_id).strip().strip('"')
        desc = str(fm.get("description") or "").strip()
        return name, desc

    def frontmatter_line(self, skill_id: str) -> str:
        name, desc = self.frontmatter(skill_id)
        return f"- {name}: {desc}"

    def load_body(self, skill_id: str) -> str:
        """The complete SKILL.md text (frontmatter + body)."""
        p = self.skill_md(skill_id)
        if not p.is_file():
            raise FileNotFoundError(f"no SKILL.md for skill {skill_id!r} under {self.pool_root}")
        return p.read_text(encoding="utf-8", errors="replace")
