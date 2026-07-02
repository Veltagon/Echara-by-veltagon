"""Provider router — role→provider assignment, adapter instantiation, fallback.

Reads provider_config.yaml, builds the right adapter per provider (CLI vs API),
routes a role to its assigned provider, and runs work through the fallback_order
with bounded retries. Never retries indefinitely: each provider is tried once,
and if all fail, AllProvidersExhausted carries every collected error.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from providers.api_adapter import ApiAdapter
from providers.base import ProviderBase
from providers.cli_adapter import CliAdapter

ECHARA_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ECHARA_ROOT / "provider_config.yaml"

_ENV_REF = re.compile(r"^\$\{([^}]+)\}$")


class AllProvidersExhausted(Exception):
    """Raised when every provider in fallback_order failed. `.errors` maps
    provider name -> the exception it raised."""

    def __init__(self, errors: dict[str, BaseException]):
        self.errors = errors
        detail = "; ".join(f"{name}: {err!r}" for name, err in errors.items())
        super().__init__(f"all providers exhausted -> {detail}")


def _resolve_env(value):
    """Turn "${VAR}" into the env var's value (None if unset). Plain strings
    pass through unchanged."""
    if isinstance(value, str):
        m = _ENV_REF.match(value)
        if m:
            return os.environ.get(m.group(1))
    return value


class ProviderRouter:
    def __init__(self, config_path: Path | str = DEFAULT_CONFIG):
        cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
        self.providers_cfg: dict = cfg["providers"]
        self.role_assignment: dict = cfg.get("role_assignment", {})
        self.fallback_order: list = cfg.get("fallback_order", [])

    def make_adapter(self, name: str) -> ProviderBase:
        if name not in self.providers_cfg:
            raise ValueError(f"unknown provider {name!r}")
        c = self.providers_cfg[name]
        ptype = c.get("type")
        if ptype == "cli":
            return CliAdapter(name, c["command"])
        if ptype is None and "model" in c:  # API providers carry a model, no type
            return ApiAdapter(name, c["model"], _resolve_env(c.get("api_key")))
        raise ValueError(f"provider {name!r} has unknown type {ptype!r}")

    def get_provider(self, role: str) -> ProviderBase:
        if role not in self.role_assignment:
            raise KeyError(f"no provider assigned to role {role!r}")
        return self.make_adapter(self.role_assignment[role])

    def call_with_fallback(self, work, order: list[str] | None = None):
        """Run `work(adapter)` against each provider in fallback_order until one
        succeeds. Each provider is tried once; failures are logged and collected.
        All fail -> AllProvidersExhausted."""
        import logging
        log = logging.getLogger("echara.provider_router")
        order = order or self.fallback_order
        errors: dict[str, BaseException] = {}
        for name in order:
            try:
                adapter = self.make_adapter(name)
                return work(adapter)
            except Exception as e:  # noqa: BLE001 — any failure → next provider
                log.warning("provider %s failed: %r — falling back", name, e)
                errors[name] = e
        raise AllProvidersExhausted(errors)
