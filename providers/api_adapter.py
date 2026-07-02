"""Category B adapter — API models via LiteLLM.

LiteLLM normalizes OpenAI-compatible chat + function-calling across Anthropic,
OpenAI, and OpenRouter, so one adapter serves all three. API models have no
native file access, so send_with_tools drives providers.tool_harness.

`complete_fn` is injectable: tests pass a mock so no real API call is ever made
(litellm is imported lazily, only when no mock is supplied).
"""
from __future__ import annotations

from harness.tools import Context
from providers.base import ProviderBase
from providers import tool_harness


class ApiAdapter(ProviderBase):
    category = "api"

    def __init__(self, name: str, model: str, api_key: str | None = None,
                 complete_fn=None, context_window: int | None = None):
        super().__init__(name)
        self.model = model
        self.api_key = api_key
        self.context_window = context_window  # enables the small-context refs/ gate
        self._complete_fn = complete_fn  # test/DI hook

    def complete(self, messages: list[dict], tools: list[dict] | None = None):
        """One raw chat-completion call (OpenAI/LiteLLM response shape)."""
        if self._complete_fn is not None:
            return self._complete_fn(messages, tools)
        import litellm  # lazy: only needed for real calls
        return litellm.completion(
            model=self.model, messages=messages, tools=tools, api_key=self.api_key
        )

    def build_system_prompt(self, skill_index: str, base: str = "") -> str:
        """System prompt carrying the frontmatter index (not skill bodies —
        those load on demand via the read_file tool)."""
        parts = [base.strip()] if base.strip() else []
        if skill_index.strip():
            parts.append("<skills>\n" + skill_index.strip() + "\n</skills>")
        return "\n\n".join(parts)

    def send_message(self, messages: list[dict]) -> str:
        resp = self.complete(messages, None)
        return resp.choices[0].message.content or ""

    def send_with_tools(self, messages: list[dict], ctx: Context, max_iterations: int = 30) -> dict:
        return tool_harness.run_tool_loop(
            self.complete, messages, ctx, max_iterations,
            context_window=self.context_window,
        )

    @property
    def tools(self) -> list[dict]:
        return tool_harness.TOOL_SCHEMAS
