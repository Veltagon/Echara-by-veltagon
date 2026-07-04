"""Phase E — quota survival + model tiering. No model calls."""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from providers import availability
from providers.claude_code import ClaudeCodeProvider, _parse_reset, _LIMIT_RE, _RESET_RE


def test_parse_claude_reset_next_occurrence():
    now = datetime(2026, 7, 4, 18, 0, 0)  # 6:00pm
    # later today
    assert round((_parse_reset("resets 10:30pm (Asia/Calcutta)", now) - now.timestamp()) / 60) == 270
    # already past -> tomorrow
    assert round((_parse_reset("resets 2:00am", now) - now.timestamp()) / 3600) == 8
    # 12-hour edge cases
    assert _parse_reset("resets 12:00am", now) is not None  # midnight
    assert _parse_reset("resets 12:00pm", now) is not None  # noon
    # junk
    assert _parse_reset("no reset here") is None
    assert _parse_reset("resets 99:99pm") is None


def test_limit_message_detection():
    capped = "You've hit your session limit · resets 10:30pm (Asia/Calcutta)"
    assert _LIMIT_RE.search(capped) and _RESET_RE.search(capped)
    assert not _LIMIT_RE.search("build completed successfully")


def test_model_tiering_argv():
    argv_sonnet = ClaudeCodeProvider(model="sonnet").build_argv("p", Path("."))
    assert argv_sonnet[-2:] == ["--model", "sonnet"]
    argv_default = ClaudeCodeProvider().build_argv("p", Path("."))
    assert "--model" not in argv_default  # None -> config default (Opus)


def test_cooldown_expires_not_permadeath():
    # A blip marks a short cooldown that auto-clears — the opposite of the old
    # permadeath where one failure killed a lane for the whole build (#9).
    availability.reset()
    availability.mark_exhausted("claude", time.time() + 0.5)
    assert not availability.is_available("claude")
    time.sleep(0.7)
    assert availability.is_available("claude")  # recovered
    availability.reset()


def test_quota_reset_far_in_future_is_honored():
    availability.reset()
    availability.mark_exhausted("claude", time.time() + 3600)  # 1h cap
    s = availability.status("claude")
    assert not s.available and 3500 < s.seconds_until_reset <= 3600
    availability.reset()
