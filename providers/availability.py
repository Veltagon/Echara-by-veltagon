"""Provider availability registry.

Thread-safe global state tracking which providers are temporarily exhausted
(rate-limited with a known reset time). The runner consults this before
dispatching so we don't burn rounds against a known-dead provider — V1's
single biggest waste.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

_LOCK = threading.Lock()
_EXHAUSTED: dict[str, float] = {}  # provider_name -> resets_at_unix


@dataclass
class Availability:
    available: bool
    resets_at: float | None = None  # unix ts; only set when not available

    @property
    def seconds_until_reset(self) -> float:
        if self.resets_at is None:
            return 0.0
        return max(0.0, self.resets_at - time.time())


def status(name: str) -> Availability:
    with _LOCK:
        ts = _EXHAUSTED.get(name)
        if ts is not None and time.time() >= ts:
            _EXHAUSTED.pop(name, None)
            ts = None
    if ts is None:
        return Availability(available=True)
    return Availability(available=False, resets_at=ts)


def is_available(name: str) -> bool:
    return status(name).available


def mark_exhausted(name: str, resets_at_unix: float) -> None:
    with _LOCK:
        _EXHAUSTED[name] = resets_at_unix


def reset(name: str | None = None) -> None:
    with _LOCK:
        if name is None:
            _EXHAUSTED.clear()
        else:
            _EXHAUSTED.pop(name, None)
