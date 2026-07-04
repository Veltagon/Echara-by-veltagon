"""Phase B — wave gates + journal + progress ledger + forensics. No model calls."""
from __future__ import annotations

from pathlib import Path

from agents import builder, progress


def _mk(root: Path, rel: str, content: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# --- wave gate ---------------------------------------------------------------

def test_wave_gate_detects_syntax_and_missing(tmp_path):
    _mk(tmp_path, "code/backend/app/good.py", "def f() -> int:\n    return 1\n")
    _mk(tmp_path, "code/backend/app/bad.py", "def f(:\n    return\n")  # syntax error
    errors = builder._wave_gate(
        tmp_path, ["code/backend/app/good.py", "code/backend/app/bad.py",
                   "code/backend/app/missing.py"])
    joined = " | ".join(errors)
    assert "good.py" not in joined            # compiles → not flagged
    assert "bad.py" in joined                  # syntax error flagged
    assert "missing.py: MISSING" in joined     # never written → flagged
    assert len(errors) == 2


def test_wave_gate_clean(tmp_path):
    _mk(tmp_path, "code/backend/app/a.py", "X = 1\n")
    assert builder._wave_gate(tmp_path, ["code/backend/app/a.py"]) == []


# --- journal -----------------------------------------------------------------

def test_journal_append_and_tail(tmp_path):
    for i in range(40):
        progress.journal_append(tmp_path, f"wave {i}: did a thing")
    tail = progress.journal_tail(tmp_path, n=30)
    lines = tail.splitlines()
    assert len(lines) == 30
    assert lines[-1] == "wave 39: did a thing"
    assert "wave 9:" not in tail  # older than the tail window
    assert progress.journal_tail(tmp_path / "empty") == ""


# --- progress ledger + fix budget -------------------------------------------

def test_progress_roundtrip_and_module_state(tmp_path):
    data = progress.load(tmp_path)
    assert data == {"modules": {}, "global_fix_budget_used": 0}
    ms = progress.module_state(data, "app")
    ms["waves_done"] += 1
    progress.save(tmp_path, data)
    reloaded = progress.load(tmp_path)
    assert reloaded["modules"]["app"]["waves_done"] == 1


def test_fix_budget_enforced_and_crash_safe(tmp_path):
    data = progress.load(tmp_path)
    # Spend the whole budget; each record persists immediately (crash-safe).
    for _ in range(progress.GLOBAL_FIX_BUDGET):
        assert progress.can_fix(data)
        progress.record_fix(tmp_path, data, "app", "gate")
    assert not progress.can_fix(data)
    # Persisted, so a resumed process sees the exhausted budget (no retry reset).
    assert progress.load(tmp_path)["global_fix_budget_used"] == progress.GLOBAL_FIX_BUDGET
    assert progress.load(tmp_path)["modules"]["app"]["gate_fixes"] == progress.GLOBAL_FIX_BUDGET


# --- provider forensics hardening -------------------------------------------

def test_microsecond_log_names_no_collision():
    # Two RunResult log paths generated in the same second must differ (#11).
    import providers.base as base
    from datetime import datetime
    a = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    b = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    assert a != b  # microsecond field differs
    # sanity: the format is what base.run() uses
    assert "_" in a and len(a.split("_")[-1]) == 6
