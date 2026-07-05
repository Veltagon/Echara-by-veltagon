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
# Code-gen quality verified 3/3 (run-and-verify) for every model below on
# 2026-07-05 (see scratchpad/provider_probe). Tool-calling capability — the gate
# for using one as a BUILD lane — is verified separately through the harness.
HARNESS_PROVIDERS: dict[str, OpenAICompatProvider] = {
    # Cerebras (fast inference, ~1-3s; Cloudflare-fronted, needs the openai SDK).
    "cerebras_gemma": OpenAICompatProvider(
        "cerebras_gemma", "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", "gemma-4-31b"
    ),
    "cerebras_gptoss": OpenAICompatProvider(
        "cerebras_gptoss", "https://api.cerebras.ai/v1", "CEREBRAS_API_KEYS", "gpt-oss-120b"
    ),
    # HuggingFace router (OpenAI-compatible; 119 live models).
    "hf_qwen_coder": OpenAICompatProvider(
        "hf_qwen_coder", "https://router.huggingface.co/v1", "HUGGINGFACE_TOKEN",
        "Qwen/Qwen2.5-Coder-32B-Instruct"
    ),
    "hf_deepseek": OpenAICompatProvider(
        "hf_deepseek", "https://router.huggingface.co/v1", "HUGGINGFACE_TOKEN",
        "deepseek-ai/DeepSeek-V3-0324"
    ),
    # NVIDIA NIM (build.nvidia.com integrate endpoint; 3-key rotation pool).
    # (mistralai/mistral-nemotron codes fine but NIM doesn't serve it with native
    #  function-calling — it emits the tool call as TEXT, so it can't drive the
    #  harness build loop; dropped as a build lane. deepseek-v4-flash tool-calls OK.)
    "nvidia_deepseek": OpenAICompatProvider(
        "nvidia_deepseek", "https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEYS",
        "deepseek-ai/deepseek-v4-flash"
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
