"""Provider routing tests — all mocked, no real API calls."""
from __future__ import annotations

import pytest

from providers import CliAdapter, ApiAdapter, ProviderRouter, AllProvidersExhausted
from providers.router import DEFAULT_CONFIG


@pytest.fixture
def router():
    return ProviderRouter(DEFAULT_CONFIG)


def test_config_loading(router):
    assert set(router.providers_cfg) == {"anthropic", "chatgpt", "openrouter", "claude_code", "codex"}
    assert len(router.role_assignment) == 3
    assert len(router.fallback_order) >= 2


def test_adapter_instantiation(router):
    assert isinstance(router.make_adapter("claude_code"), CliAdapter)
    assert isinstance(router.make_adapter("codex"), CliAdapter)
    assert isinstance(router.make_adapter("anthropic"), ApiAdapter)
    assert isinstance(router.make_adapter("chatgpt"), ApiAdapter)
    # A provider with a bogus type must raise ValueError.
    router.providers_cfg["weird"] = {"type": "quantum"}
    with pytest.raises(ValueError):
        router.make_adapter("weird")


def test_fallback_on_failure(router, caplog):
    calls = {"n": 0}

    def work(adapter):
        calls["n"] += 1
        if adapter.name == "anthropic":  # primary fails
            raise ConnectionError("primary down")
        return f"ok:{adapter.name}"  # fallback succeeds

    with caplog.at_level("WARNING"):
        result = router.call_with_fallback(work, order=["anthropic", "chatgpt"])
    assert result == "ok:chatgpt"
    assert calls["n"] == 2  # tried primary, then fell back
    assert any("anthropic" in r.message for r in caplog.records)


def test_all_providers_exhausted(router):
    def work(adapter):
        raise ConnectionError(f"{adapter.name} unreachable")

    with pytest.raises(AllProvidersExhausted) as exc:
        router.call_with_fallback(work, order=["anthropic", "chatgpt", "openrouter"])
    errs = exc.value.errors
    assert set(errs) == {"anthropic", "chatgpt", "openrouter"}
    assert all(isinstance(e, ConnectionError) for e in errs.values())


def test_role_routing(router):
    router.role_assignment = {"planner": "anthropic", "builder": "claude_code"}
    planner = router.get_provider("planner")
    assert isinstance(planner, ApiAdapter) and planner.name == "anthropic"
    builder = router.get_provider("builder")
    assert isinstance(builder, CliAdapter) and builder.name == "claude_code"
