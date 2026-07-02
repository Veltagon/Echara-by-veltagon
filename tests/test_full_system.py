"""Full-system smoke: router + skill router wired for every role. No API calls."""
from __future__ import annotations

import pytest

from skills import SkillRouter, count_tokens, DEFAULT_POOL_ROOT
from providers import ProviderRouter, ApiAdapter, CliAdapter
from providers.router import DEFAULT_CONFIG

pytestmark = pytest.mark.skipif(
    not DEFAULT_POOL_ROOT.is_dir(), reason="skill pool not cloned"
)


def test_end_to_end_mock():
    prov_router = ProviderRouter(DEFAULT_CONFIG)
    skill_router = SkillRouter()

    for role in ("planner", "builder", "verifier"):
        # Provider comes back as the right adapter type.
        provider = prov_router.get_provider(role)
        assert isinstance(provider, (ApiAdapter, CliAdapter))

        # Skill index builds and stays within budget.
        index = skill_router.build_index(role)
        assert count_tokens(index) < 500

        # API adapters expose all 5 harness tools; CLI adapters use native tools.
        if isinstance(provider, ApiAdapter):
            names = [t["function"]["name"] for t in provider.tools]
            assert names == ["read_file", "write_file", "list_dir", "bash_run", "done"]
        else:
            assert provider.category == "cli"
