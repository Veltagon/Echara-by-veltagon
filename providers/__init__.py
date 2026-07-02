from providers.base import Provider, ProviderBase, RunResult
from providers.claude_code import ClaudeCodeProvider
from providers.codex import CodexProvider
from providers.openai_compat import OpenAICompatProvider

# M2 CLI providers — subprocess agents that own their own tool loop.
PROVIDERS: dict[str, type[Provider]] = {
    "claude": ClaudeCodeProvider,
    "codex": CodexProvider,
}

# M2.5 raw-API providers — driven by ECHARA's own harness/loop.py.
# Additive: each row is base_url + .env key + model. Add upstreams here.
# (gemma-4-31b is the only live Cerebras model that emits real tool_calls;
#  gpt-oss-120b / zai-glm-4.7 route text into `reasoning` with content=None.)
HARNESS_PROVIDERS: dict[str, OpenAICompatProvider] = {
    "cerebras_gemma": OpenAICompatProvider(
        "cerebras_gemma", "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", "gemma-4-31b"
    ),
}

# M3 routing layer. Imported AFTER PROVIDERS is defined — cli_adapter does
# `from providers import PROVIDERS`, so this ordering avoids a circular import.
from providers.cli_adapter import CliAdapter
from providers.api_adapter import ApiAdapter
from providers.router import ProviderRouter, AllProvidersExhausted

__all__ = [
    "Provider", "ProviderBase", "RunResult", "ClaudeCodeProvider", "CodexProvider",
    "OpenAICompatProvider", "PROVIDERS", "HARNESS_PROVIDERS",
    "CliAdapter", "ApiAdapter", "ProviderRouter", "AllProvidersExhausted",
]
