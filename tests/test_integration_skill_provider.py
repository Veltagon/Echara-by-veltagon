"""Integration: skill router x provider adapters. No real API calls."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from skills import SkillRouter, DEFAULT_POOL_ROOT
from providers import ApiAdapter, CliAdapter

pytestmark = pytest.mark.skipif(
    not DEFAULT_POOL_ROOT.is_dir(), reason="skill pool not cloned"
)


def _echo_system_prompt(messages, tools=None):
    """Mock LLM that returns whatever system prompt it was given."""
    sysmsg = next((m["content"] for m in messages if m["role"] == "system"), "")
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=sysmsg, tool_calls=None))])


def test_skill_loaded_into_api_session():
    router = SkillRouter()
    index = router.build_index("builder")
    adapter = ApiAdapter("anthropic", "claude-sonnet-4-20250514", complete_fn=_echo_system_prompt)
    system_prompt = adapter.build_system_prompt(index)

    out = adapter.send_message([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "hello"},
    ])
    # Frontmatter index is present ...
    assert "senior-backend" in out
    # ... but the full skill body (e.g. its "## Quick Start" section) is not.
    assert "## Quick Start" not in out


def test_cli_skill_index_written(tmp_path):
    router = SkillRouter()
    index = router.build_index("builder")
    adapter = CliAdapter("claude_code", "claude", project_dir=tmp_path)
    written = adapter.setup_skills(index)

    assert written == tmp_path / ".echara" / "skills_index.md"
    assert written.is_file()
    content = written.read_text(encoding="utf-8")
    # Only assigned skills' frontmatter, no body.
    for skill_id in router.skills_for("builder"):
        name, _ = router.loader.frontmatter(skill_id)
        assert name in content
    assert "## Quick Start" not in content
