"""Unit tests for the M2 hardening — no API spend.

Run: python tests/tests_hardening.py
Each test prints PASS/FAIL and the script exits 0 only if every test passes.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root importable
from providers import availability
from providers.base import _filter_noise, NOISE_PATTERNS, Provider
from providers.codex import _acquire_slot, _parse_retry_after, _release_slot


SCRATCH = Path(__file__).resolve().parent.parent / "logs" / "_test_scratch"


_results: list[tuple[str, bool, str]] = []


def _record(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    print(f"  [{('PASS' if ok else 'FAIL')}] {name}{(' — ' + detail) if detail else ''}")


# ---------------------------------------------------------------------------
# 1. Noise filter
# ---------------------------------------------------------------------------

def test_noise_filter() -> None:
    print("\n>>> noise filter")
    sample = (
        "OpenAI Codex v0.138.0\n"
        "2026-06-30T14:34:18Z ERROR rmcp::transport::worker: worker quit with fatal: "
        'Transport channel closed, when AuthRequired(AuthRequiredError { '
        'www_authenticate_header: "Bearer realm=\\"OAuth\\", '
        'resource_metadata=\\"https://mcp.linear.app/.well-known/oauth-protected-resource/mcp\\""})\n'
        "Error: rate limit exceeded, try again in 47s\n"
    )
    filtered = _filter_noise(sample)
    _record("strips linear mcp noise", "mcp.linear.app" not in filtered)
    _record("keeps real rate-limit line", "rate limit exceeded" in filtered)
    _record("keeps preamble", "OpenAI Codex" in filtered)


# ---------------------------------------------------------------------------
# 2. Rate-limit parser
# ---------------------------------------------------------------------------

def test_rate_limit_parser() -> None:
    print("\n>>> rate-limit parser")
    cases = [
        ("Error: rate limit exceeded, try again in 47s", 47.0),
        ("please wait 3600 seconds before retrying", 3600.0),
        ("HTTP 429 Too Many Requests: retry-after: 120", 120.0),
        ("nothing rate limited here", None),
    ]
    for text, expected in cases:
        got = _parse_retry_after(text)
        _record(f"parse '{text[:35]}...' -> {expected}", got == expected, f"got={got}")

    # Regression: 400KB of echoed generated code containing "rate limiting"
    # docs mid-stream must NOT match (benched a healthy codex for 4.7 days).
    poison = (
        "diff output...\n**Add rate limiting**\n```typescript\n"
        "import rateLimit from 'express-rate-limit';\n"
        "const apiLimiter = rateLimit({ windowMs: 15 * 60 * 1000, max: 100 });\n"
        "```\n" + "x = 1\n" * 3000 +
        "retry logic discussed above uses exponential backoff of 404171 steps\n"
        + "y = 2\n" * 500 + "tokens used\n67,504\n"
    )
    _record("echoed code with 'rate limiting' mid-stream -> None",
            _parse_retry_after(poison) is None,
            f"got={_parse_retry_after(poison)}")
    # A REAL terminal rate-limit message (at the end) still parses.
    real = "z = 3\n" * 5000 + "\nERROR: rate limit exceeded, try again in 120s\n"
    _record("terminal rate-limit after long transcript -> 120",
            _parse_retry_after(real) == 120.0)
    # Implausible retry-after (>24h) is rejected.
    _record("implausible 404171s -> None",
            _parse_retry_after("rate limit: retry after 404171 s") is None)


# ---------------------------------------------------------------------------
# 3. Availability registry
# ---------------------------------------------------------------------------

def test_availability() -> None:
    print("\n>>> availability registry")
    availability.reset()
    _record("default available", availability.is_available("codex"))
    availability.mark_exhausted("codex", time.time() + 60)
    _record("marked exhausted shows unavailable", not availability.is_available("codex"))
    s = availability.status("codex")
    _record(
        "resets_at within 1s of expected",
        s.resets_at is not None and abs(s.resets_at - (time.time() + 60)) < 1.5,
        f"seconds_until={s.seconds_until_reset:.1f}",
    )
    # Past timestamp should auto-expire on next check
    availability.mark_exhausted("codex", time.time() - 1)
    _record("expired ts auto-clears", availability.is_available("codex"))
    availability.reset()


# ---------------------------------------------------------------------------
# 4. Slot gate — concurrency
# ---------------------------------------------------------------------------

def test_slot_gate() -> None:
    print("\n>>> codex slot gate")
    import providers.codex as cdx

    def reset_state() -> None:
        cdx._SLOT = threading.Semaphore(1)
        cdx._LAST_RELEASE_TS = None  # fresh: never released → first acquire free

    # Sub-test A: single acquire+release works.
    reset_state()
    assert _acquire_slot(timeout_sec=2)
    _release_slot()
    _record("acquire+release ok", True)

    # Sub-test B: concurrency. Reset spacing so the FIRST acquire isn't
    # rejected by the 10s spacing rule (which would mask the real test).
    reset_state()
    got_slot_a = _acquire_slot(timeout_sec=2)
    second_result: dict[str, bool] = {"got": False}

    def runner() -> None:
        # Use 2s timeout so the thread joins quickly with rejection.
        second_result["got"] = _acquire_slot(timeout_sec=2)

    t = threading.Thread(target=runner)
    started = time.monotonic()
    t.start()
    t.join(timeout=5)
    waited = time.monotonic() - started
    _record(
        "second acquire blocked while first holds",
        got_slot_a and not second_result["got"] and waited >= 1.5,
        f"first_got={got_slot_a}, second_got={second_result['got']}, waited={waited:.2f}s",
    )
    if got_slot_a:
        _release_slot()

    # Sub-test C: spacing rejects rapid re-acquire within a short timeout.
    # We just released above, so _LAST_RELEASE_TS is fresh. timeout=2 < spacing=10
    got = _acquire_slot(timeout_sec=2)
    _record(
        "spacing rejects rapid re-acquire within timeout",
        not got,
        f"got={got}",
    )

    # Sub-test D: longer timeout waits through the spacing window.
    # Need to reset state so we're not re-locking semaphore from C's success path.
    reset_state()
    # Simulate "just released" by setting last release to now.
    cdx._LAST_RELEASE_TS = time.monotonic()
    got = _acquire_slot(timeout_sec=12)
    _record("eventually acquires after spacing window", got)
    if got:
        _release_slot()


# ---------------------------------------------------------------------------
# 5. Idle watcher (fake provider that just sleeps)
# ---------------------------------------------------------------------------

class _SleepProvider(Provider):
    name = "fakesleep"
    idle_limit_sec = 3  # tight for the test

    def build_argv(self, prompt: str, cwd: Path) -> list[str]:
        # Pure stdlib sleep with NO stdout AND NO stderr writes — guaranteed
        # idle on every stream the watcher tracks.
        return [sys.executable, "-c", "import time; time.sleep(30)"]


class _StderrChattyProvider(Provider):
    """Writes to stderr every 0.5s. The idle watcher MUST NOT kill it just
    because stdout is silent — codex behaves exactly like this."""
    name = "fakechatty"
    idle_limit_sec = 3

    def build_argv(self, prompt: str, cwd: Path) -> list[str]:
        return [
            sys.executable,
            "-c",
            "import sys, time\n"
            "for _ in range(12):\n"
            "    print('tick', file=sys.stderr, flush=True)\n"
            "    time.sleep(0.5)\n",
        ]


def test_idle_watcher() -> None:
    print("\n>>> idle watcher (real subprocess)")
    SCRATCH.mkdir(parents=True, exist_ok=True)

    # Case A: silent on BOTH stdout and stderr → MUST be killed.
    provider = _SleepProvider()
    started = time.monotonic()
    result = provider.run("noop", SCRATCH / "idleA", SCRATCH, timeout_sec=60)
    elapsed = time.monotonic() - started
    _record(
        "silent stdout+stderr -> kill_reason='idle'",
        result.kill_reason == "idle",
        f"kill_reason={result.kill_reason}, elapsed={elapsed:.1f}s",
    )
    _record("silent run killed before hard timeout", elapsed < 30, f"{elapsed:.1f}s")
    _record("silent run -> RunResult.ok is False", not result.ok)
    _record("prompt log file exists", result.prompt_path is not None and result.prompt_path.exists())

    # Case B: chatty STDERR (stdout silent) → MUST NOT be killed. This is the
    # codex behavior that false-positived the first regression run.
    chatty = _StderrChattyProvider()
    started = time.monotonic()
    result = chatty.run("noop", SCRATCH / "idleB", SCRATCH, timeout_sec=30)
    elapsed = time.monotonic() - started
    _record(
        "chatty stderr -> NOT killed by idle watcher",
        result.kill_reason is None,
        f"kill_reason={result.kill_reason}, elapsed={elapsed:.1f}s, exit={result.exit_code}",
    )
    _record(
        "chatty stderr run exited cleanly",
        result.exit_code == 0,
        f"exit={result.exit_code}",
    )


# ---------------------------------------------------------------------------
# 6. stdin prompt delivery (Windows argv-length fix)
# ---------------------------------------------------------------------------

class _StdinEchoProvider(Provider):
    """Echoes stdin back to stdout — proves the prompt arrives via the pipe."""
    name = "fakestdin"
    idle_limit_sec = 30
    stdin_prompt = True

    def build_argv(self, prompt: str, cwd: Path) -> list[str]:
        return [sys.executable, "-c", "import sys; print(sys.stdin.read())"]


def test_stdin_prompt_delivery() -> None:
    print("\n>>> stdin prompt delivery")
    SCRATCH.mkdir(parents=True, exist_ok=True)
    # 20KB prompt — far past cmd.exe's 8,191-char argv ceiling that broke the
    # codex .cmd shim ("The command line is too long.", 2026-07-02).
    prompt = "NN-RULE " * 2500 + "END-MARKER"
    provider = _StdinEchoProvider()
    result = provider.run(prompt, SCRATCH / "stdin", SCRATCH, timeout_sec=30)
    _record("stdin run exits 0", result.exit_code == 0, f"exit={result.exit_code}")
    out = result.stdout_path.read_text(encoding="utf-8", errors="replace")
    _record("full 20KB prompt arrived via stdin",
            "END-MARKER" in out and out.count("NN-RULE") == 2500)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("ECHARA M2 hardening — unit tests")
    test_noise_filter()
    test_rate_limit_parser()
    test_availability()
    test_slot_gate()
    test_idle_watcher()
    test_stdin_prompt_delivery()
    failed = [n for n, ok, _ in _results if not ok]
    print()
    print(f"  {len(_results) - len(failed)} passed, {len(failed)} failed")
    if failed:
        for n in failed:
            print(f"    - FAIL: {n}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
