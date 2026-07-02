"""Codex CLI provider.

Hardening beyond the generic base:
  - Process-global slot gate: one codex spawn at a time, 10s min spacing.
    V1's worst codex outage was 3 agents firing simultaneously, all hitting
    the rate limit together, all retrying together — a thundering herd that
    burned the hourly cap in seconds.
  - Rate-limit parsing on exit: scan stderr for "retry after Xs" messages.
    < 300s  → recorded on the result so the caller can sleep+retry once.
    >= 300s → mark codex exhausted in availability so every subsequent
              pick_provider() skips it until reset.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from providers import availability
from providers.base import Provider, RunResult


_SLOT = threading.Semaphore(1)
# None = never released yet → the first acquire needs no spacing wait. Do NOT
# init this to time.monotonic(): that would block the very first acquire for
# _MIN_SPACING_SEC. The None check makes that intent explicit, not accidental.
_LAST_RELEASE_TS: float | None = None
_LAST_RELEASE_LOCK = threading.Lock()
_MIN_SPACING_SEC = 10.0
_ACQUIRE_TIMEOUT_SEC = 10.0
_LONG_BACKOFF_THRESHOLD_SEC = 300.0  # >=5min → exhausted; not worth retrying


# Codex rate-limit message shapes — kept loose because the exact format has
# changed across versions. The captured group is the retry-after in seconds.
_RATE_LIMIT_PATTERNS: list[re.Pattern] = [
    re.compile(r"rate.?limit.*?(?:try again|retry).*?(\d+)\s*s", re.IGNORECASE | re.DOTALL),
    re.compile(r"try again in\s*(\d+)\s*s", re.IGNORECASE),
    re.compile(r"retry[- ]after[^\d]*(\d+)", re.IGNORECASE),
    re.compile(r"please wait\s*(\d+)\s*seconds", re.IGNORECASE),
    re.compile(r"too many requests.*?(\d+)\s*s", re.IGNORECASE),
]


def _acquire_slot(timeout_sec: float = _ACQUIRE_TIMEOUT_SEC) -> bool:
    deadline = time.monotonic() + timeout_sec
    if not _SLOT.acquire(timeout=timeout_sec):
        return False
    with _LAST_RELEASE_LOCK:
        last = _LAST_RELEASE_TS
    if last is not None:
        elapsed = time.monotonic() - last
        if elapsed < _MIN_SPACING_SEC:
            wait = _MIN_SPACING_SEC - elapsed
            if time.monotonic() + wait > deadline:
                _SLOT.release()
                return False
            time.sleep(wait)
    return True


def _release_slot() -> None:
    global _LAST_RELEASE_TS
    with _LAST_RELEASE_LOCK:
        _LAST_RELEASE_TS = time.monotonic()
    try:
        _SLOT.release()
    except ValueError:
        pass  # already released — defensive


def _parse_retry_after(stderr_text: str) -> float | None:
    for pat in _RATE_LIMIT_PATTERNS:
        m = pat.search(stderr_text)
        if m:
            try:
                return float(m.group(1))
            except (ValueError, IndexError):
                continue
    return None


class CodexProvider(Provider):
    name = "codex"
    # Was 60 ("codex emits reasoning every few seconds") — stale: with
    # `reasoning effort: xhigh` + `reasoning summaries: none` (user's codex
    # config) it can think SILENTLY for >60s and got idle-killed mid-build
    # (2026-07-02, 67KB of healthy stderr then a long quiet stretch). 300s
    # matches the claude calibration: true hangs still die in 5 minutes.
    idle_limit_sec = 300
    # codex on PATH is an npm .cmd shim → cmd.exe's 8,191-char argv ceiling.
    # Prompts embedding PLAN.md + contract exceed it, so deliver via stdin
    # (`codex exec -` reads the prompt from stdin; probe-verified).
    stdin_prompt = True

    def build_argv(self, prompt: str, cwd: Path) -> list[str]:
        return [
            "codex",
            "exec",
            "--cd", str(cwd.resolve()),
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-",
        ]

    def run(
        self,
        prompt: str,
        cwd: Path,
        log_dir: Path,
        timeout_sec: int = 900,
    ) -> RunResult:
        if not availability.is_available(self.name):
            return self.make_skip_result(log_dir, "exhausted", prompt=prompt)
        if not _acquire_slot():
            return self.make_skip_result(log_dir, "slot_busy", prompt=prompt)

        try:
            result = super().run(prompt, cwd, log_dir, timeout_sec)
        finally:
            _release_slot()

        # Rate-limit detection. The stderr file at this point has already had
        # noise stripped by the base, so any retry-after match is genuine.
        try:
            stderr_text = result.stderr_path.read_text(
                encoding="utf-8", errors="replace"
            )
        except FileNotFoundError:
            stderr_text = ""

        retry_after = _parse_retry_after(stderr_text)
        if retry_after is not None:
            result.rate_limit_retry_after_sec = retry_after
            if retry_after >= _LONG_BACKOFF_THRESHOLD_SEC:
                availability.mark_exhausted(self.name, time.time() + retry_after)

        return result
