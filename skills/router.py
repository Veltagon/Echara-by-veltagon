"""Skill router — role→skill config, frontmatter index, per-session token budget.

The index (name+description per assigned skill) is injected into an agent's
system prompt at session start. Full bodies are loaded on demand through a
SkillSession, which tracks cumulative tokens and refuses any load that would
push a session past TOKEN_BUDGET — keeping context lean, as the guide requires.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from skills.loader import SkillLoader, count_tokens

ECHARA_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POOL_ROOT = ECHARA_ROOT / "skills-pool" / "engineering-team" / "skills"
DEFAULT_ASSIGNMENTS = ECHARA_ROOT / "skill_assignments.yaml"

TOKEN_BUDGET = 5000
MAX_SKILLS_PER_ROLE = 4
BUDGET_EXCEEDED_MSG = "Skill budget exceeded. Use loaded skills only."


class SkillRouter:
    def __init__(
        self,
        assignments_path: Path | str = DEFAULT_ASSIGNMENTS,
        pool_root: Path | str = DEFAULT_POOL_ROOT,
    ):
        self.pool_root = Path(pool_root)
        self.loader = SkillLoader(self.pool_root)
        self.assignments: dict[str, list[str]] = yaml.safe_load(
            Path(assignments_path).read_text(encoding="utf-8")
        )

    def roles(self) -> list[str]:
        return list(self.assignments)

    def skills_for(self, role: str) -> list[str]:
        if role not in self.assignments:
            raise KeyError(f"unknown role {role!r}; known: {self.roles()}")
        skills = self.assignments[role]
        if len(skills) > MAX_SKILLS_PER_ROLE:
            raise ValueError(
                f"role {role!r} has {len(skills)} skills; max is {MAX_SKILLS_PER_ROLE}"
            )
        return skills

    def build_index(self, role: str) -> str:
        """Frontmatter-only index for a role's system prompt. No skill bodies."""
        lines = [f"Available skills for the {role} (call read_file on the path to load a full skill body):"]
        for skill_id in self.skills_for(role):
            lines.append(self.loader.frontmatter_line(skill_id))
        return "\n".join(lines)

    def session(self, role: str) -> "SkillSession":
        return SkillSession(self, role)


class SkillSession:
    """One agent's live skill context. The frontmatter index counts against the
    budget from the start; each full-body load adds to it and is refused if it
    would exceed TOKEN_BUDGET."""

    def __init__(self, router: SkillRouter, role: str, budget: int = TOKEN_BUDGET):
        self.router = router
        self.role = role
        self.budget = budget
        self.index = router.build_index(role)
        self.tokens_used = count_tokens(self.index)
        self.loaded: dict[str, str] = {}

    def load_full_skill(self, skill_id: str) -> str:
        """Return the full SKILL.md body and charge its tokens to the budget.
        If the load would exceed the budget, refuse and return the standard
        budget-exceeded message (tokens unchanged). Idempotent per skill."""
        if skill_id in self.loaded:
            return self.loaded[skill_id]
        body = self.router.loader.load_body(skill_id)
        cost = count_tokens(body)
        if self.tokens_used + cost > self.budget:
            return BUDGET_EXCEEDED_MSG
        self.tokens_used += cost
        self.loaded[skill_id] = body
        return body
