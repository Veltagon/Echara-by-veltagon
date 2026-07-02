"""Skill router tests — no API spend, uses the real cloned skill pool."""
from __future__ import annotations

import pytest

from skills import (
    SkillRouter,
    SkillLoader,
    count_tokens,
    BUDGET_EXCEEDED_MSG,
    DEFAULT_POOL_ROOT,
    TOKEN_BUDGET,
)

pytestmark = pytest.mark.skipif(
    not DEFAULT_POOL_ROOT.is_dir(),
    reason="skill pool not cloned (skills-pool/engineering-team/skills missing)",
)


@pytest.fixture
def router():
    return SkillRouter()


def test_frontmatter_extraction():
    loader = SkillLoader(DEFAULT_POOL_ROOT)
    name, desc = loader.frontmatter("senior-backend")
    assert name == "senior-backend"
    assert desc and "backend" in desc.lower()
    # Frontmatter (name + description) is a compact index entry.
    assert count_tokens(f"{name}: {desc}") < 200


def test_role_skill_assignment(router):
    assert router.skills_for("planner") == ["senior-architect", "senior-backend"]
    assert router.skills_for("builder") == ["senior-backend", "senior-data-engineer", "senior-devops"]
    for role in router.roles():
        assert len(router.skills_for(role)) <= 4


def test_token_budget_enforcement(router):
    session = router.session("builder")
    # Load full bodies of many real skills until the budget refuses one.
    pool_skills = [p.name for p in sorted(DEFAULT_POOL_ROOT.iterdir()) if (p / "SKILL.md").is_file()]
    refused = False
    for skill_id in pool_skills:
        result = session.load_full_skill(skill_id)
        assert session.tokens_used <= TOKEN_BUDGET  # never exceeds the cap
        if result == BUDGET_EXCEEDED_MSG:
            refused = True
            break
    assert refused, "budget was never hit — pool too small? loosen or add skills"


def test_skill_index_generation(router):
    index = router.build_index("builder")
    for skill_id in router.skills_for("builder"):
        name, desc = router.loader.frontmatter(skill_id)
        assert name in index
        assert desc[:40] in index  # description present
    # Index is frontmatter only — no skill body. senior-backend's body has a
    # "## Quick Start" section; the index must not contain it.
    assert "## Quick Start" not in index
    assert count_tokens(index) < 500


def test_full_body_load_on_demand(router):
    session = router.session("builder")
    before = session.tokens_used
    body = session.load_full_skill("senior-backend")
    assert body != BUDGET_EXCEEDED_MSG
    assert body.startswith("---")  # full SKILL.md, frontmatter included
    assert "## Quick Start" in body  # body content, not just frontmatter
    assert session.tokens_used > before  # budget tracker updated
