"""Provider routing tests — all mocked, no real API calls."""
from __future__ import annotations

import pytest

from providers import CliAdapter, ApiAdapter, ProviderRouter, AllProvidersExhausted
from providers import availability
from providers.router import DEFAULT_CONFIG, _looks_rate_limited


@pytest.fixture
def router():
    # Availability is process-global; wipe any state prior tests left behind.
    availability.reset()
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


# --- guide.md M3: cooldowns for API providers -------------------------------

def test_looks_rate_limited_signatures():
    assert _looks_rate_limited(RuntimeError("HTTP 429 Too Many Requests"))
    assert _looks_rate_limited(RuntimeError("openai.RateLimitError: quota"))
    class RateLimitError(Exception): ...
    assert _looks_rate_limited(RateLimitError("slow down"))
    assert not _looks_rate_limited(ConnectionError("dns"))


def test_cooldown_skips_exhausted_provider(router):
    """A provider already on cooldown must be SKIPPED without being called."""
    import time
    availability.mark_exhausted("anthropic", time.time() + 30)
    calls = []

    def work(adapter):
        calls.append(adapter.name)
        return f"ok:{adapter.name}"

    result = router.call_with_fallback(work, order=["anthropic", "chatgpt"])
    assert result == "ok:chatgpt"
    assert calls == ["chatgpt"]  # anthropic was skipped, never invoked


def test_rate_limit_error_marks_provider_on_cooldown(router):
    """A rate-limit-shaped failure must put the provider on the cooldown list."""
    assert availability.is_available("anthropic")  # sanity

    def work(adapter):
        if adapter.name == "anthropic":
            raise RuntimeError("HTTP 429 rate limit exceeded")
        return f"ok:{adapter.name}"

    router.call_with_fallback(work, order=["anthropic", "chatgpt"])
    assert not availability.is_available("anthropic")

    # A second call in the same window skips anthropic entirely — proves the
    # cooldown is honored end-to-end, not just recorded.
    seen = []
    def work2(adapter):
        seen.append(adapter.name); return "ok"
    router.call_with_fallback(work2, order=["anthropic", "chatgpt"])
    assert seen == ["chatgpt"]


def test_non_rate_limit_error_does_not_cooldown(router):
    """A ConnectionError shouldn't blackhole the provider — that's for 429s."""
    def work(adapter):
        if adapter.name == "anthropic":
            raise ConnectionError("dns fail")
        return "ok"

    router.call_with_fallback(work, order=["anthropic", "chatgpt"])
    assert availability.is_available("anthropic")  # still available after ConnectionError
