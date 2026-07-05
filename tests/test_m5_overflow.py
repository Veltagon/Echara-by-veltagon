"""Overflow build lanes: when both CLI lanes (claude, codex) are exhausted this
round, the builder falls through to the cheap API lanes instead of parking/
failing. No real API calls — run_harness is faked and the CLI lanes are marked
exhausted on the real availability singleton the dispatcher consults."""
from __future__ import annotations

import re
import time
from pathlib import Path

from agents import builder
from providers import availability


def _fake_run_harness(calls):
    def fake(provider, task, workspace, **kw):
        calls.append(getattr(provider, "name", "?"))
        ws = Path(workspace)
        for rel in re.findall(r"code/[\w/.\-]+\.py", task):  # emulate writing the wave's files
            p = ws / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if not p.exists():
                p.write_text("def _stub():\n    return 1\n", encoding="utf-8")
        return {"provider": getattr(provider, "name", "?"), "stop_reason": "done", "tool_calls": 3}
    return fake


def test_overflow_builds_when_cli_lanes_exhausted(tmp_path, monkeypatch):
    build_dir = tmp_path / "b"
    (build_dir / "code").mkdir(parents=True)
    (build_dir / "PLAN.md").write_text(
        "1. `code/app/foo.py` — foo\n2. `code/app/bar.py` — bar\n", encoding="utf-8")
    monkeypatch.setattr(builder.skills_router, "DEFAULT_POOL_ROOT", tmp_path / "nopool")

    # both CLI lanes exhausted → dispatch's for-loop skips them → overflow fires
    availability.mark_exhausted("claude", time.time() + 9999)
    availability.mark_exhausted("codex", time.time() + 9999)

    calls: list[str] = []
    import run_harness_agent
    monkeypatch.setattr(run_harness_agent, "run_harness", _fake_run_harness(calls))

    info = builder.run_builder(build_dir)

    assert (build_dir / "code/app/foo.py").is_file()
    assert (build_dir / "code/app/bar.py").is_file()
    assert calls, "overflow run_harness was never invoked"
    assert all(c in builder._OVERFLOW_LANES for c in calls)
    assert "overflow:" in info["provider"]


def test_overflow_disabled_by_env_falls_through_to_failure(tmp_path, monkeypatch):
    build_dir = tmp_path / "b2"
    (build_dir / "code").mkdir(parents=True)
    (build_dir / "PLAN.md").write_text("1. `code/app/foo.py` — foo\n", encoding="utf-8")
    monkeypatch.setattr(builder.skills_router, "DEFAULT_POOL_ROOT", tmp_path / "nopool")
    monkeypatch.setenv("ECHARA_OVERFLOW", "0")  # disabled → no fallback, must fail
    availability.mark_exhausted("claude", time.time() + 9999)
    availability.mark_exhausted("codex", time.time() + 9999)

    import pytest
    with pytest.raises(builder.BuildDispatchFailed):
        builder.run_builder(build_dir)
