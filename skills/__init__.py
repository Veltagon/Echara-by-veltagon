"""ECHARA skill router — Claude-Code-style progressive skill disclosure.

Frontmatter index (name + description, ~100 tokens/skill) goes into an agent's
system prompt; full SKILL.md bodies are pulled on demand via the read_file tool,
tracked against a per-session token budget. See skills/router.py.
"""
from skills.loader import SkillLoader, count_tokens
from skills.router import (
    SkillRouter,
    SkillSession,
    BUDGET_EXCEEDED_MSG,
    DEFAULT_POOL_ROOT,
    TOKEN_BUDGET,
)

__all__ = [
    "SkillLoader", "count_tokens", "SkillRouter", "SkillSession",
    "BUDGET_EXCEEDED_MSG", "DEFAULT_POOL_ROOT", "TOKEN_BUDGET",
]
