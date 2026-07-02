"""OpenAI-compatible API provider for the harness loop.

The M2 CLI providers (claude_code, codex) wrap subprocesses that own their own
tool loop and return a RunResult. This is a different beast: it exposes a single
`complete(messages, tools)` over any OpenAI-shaped chat endpoint, and ECHARA's
harness/loop.py drives the tool loop. One class, one row of config per upstream
(base_url + env key + model) — the lift from opencode's BUNDLED_PROVIDERS table.

Must use the openai SDK (httpx), NOT raw urllib: Cerebras sits behind Cloudflare
which 403s the default Python-urllib User-Agent (error 1010).
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values
from openai import OpenAI

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_key(env_key: str) -> str:
    """Resolve an API key from the real environment first, then the project
    .env. Comma-separated lists yield the FIRST entry.
    ponytail: single-key by design for M2.5 — the SDK retries transient 429s on
    the one key. Multi-key failover belongs to M3 provider routing, not here."""
    raw = os.environ.get(env_key) or dotenv_values(_ENV_PATH).get(env_key) or ""
    key = raw.split(",")[0].strip()
    if not key:
        raise RuntimeError(f"no API key for {env_key} (checked env and {_ENV_PATH})")
    return key


class OpenAICompatProvider:
    def __init__(self, name: str, base_url: str, env_key: str, model: str):
        self.name = name
        self.base_url = base_url
        self.env_key = env_key
        self.model = model
        self._client: OpenAI | None = None

    def client(self) -> OpenAI:
        if self._client is None:
            # max_retries: the SDK owns transient resilience — exponential
            # backoff on 429/500/502/503/504/connection/timeout, and it honors
            # the Retry-After header. timeout bounds a hung request. A terminal
            # failure still raises; harness/loop.py catches it and ends cleanly.
            self._client = OpenAI(
                base_url=self.base_url,
                api_key=load_key(self.env_key),
                max_retries=4,
                timeout=120.0,
            )
        return self._client

    def complete(self, messages: list[dict], tools: list[dict]):
        return self.client().chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
