"""Build journal + progress ledger — file-based, write-through.

Deliberately NOT in ProjectState: state saves only at phase boundaries, so a
mid-BUILD crash across 80 sessions would lose per-wave progress and re-run
completed waves. These files are written after every session, so resume is
exact. The journal is Anthropic's structured note-taking (decisions carried to
fresh contexts); the progress file also enforces fix budgets — persisted BEFORE
a fix dispatches so a crash can't reset the count and multiply retries.
"""
from __future__ import annotations

import json
from pathlib import Path

_PROGRESS = "BUILD_PROGRESS.json"
_JOURNAL = "BUILD_JOURNAL.md"

# Ceiling on fix dispatches across the whole build — the backstop against the
# retry-multiplication worst case (M5 plan risk #4). Exhausted → the normal
# VerifyFailed / MAX_RETRIES path takes over.
GLOBAL_FIX_BUDGET = 25


def load(build_dir: Path) -> dict:
    p = Path(build_dir) / _PROGRESS
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"modules": {}, "global_fix_budget_used": 0}


def save(build_dir: Path, data: dict) -> None:
    (Path(build_dir) / _PROGRESS).write_text(json.dumps(data, indent=2), encoding="utf-8")


def module_state(data: dict, module: str) -> dict:
    return data["modules"].setdefault(
        module, {"waves_done": 0, "gate_fixes": 0, "integration_fixes": 0,
                 "seam_fixes": 0, "seams_ok": False, "integrated": False})


def can_fix(data: dict) -> bool:
    """True if the global fix budget still has room."""
    return data.get("global_fix_budget_used", 0) < GLOBAL_FIX_BUDGET


def record_fix(build_dir: Path, data: dict, module: str, kind: str) -> None:
    """Charge one fix against the global budget + the module counter, and
    persist BEFORE the fix dispatches (crash-safe budget)."""
    data["global_fix_budget_used"] = data.get("global_fix_budget_used", 0) + 1
    module_state(data, module)[f"{kind}_fixes"] = \
        module_state(data, module).get(f"{kind}_fixes", 0) + 1
    save(build_dir, data)


def journal_append(build_dir: Path, line: str) -> None:
    with (Path(build_dir) / _JOURNAL).open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")


def journal_tail(build_dir: Path, n: int = 30) -> str:
    p = Path(build_dir) / _JOURNAL
    if not p.is_file():
        return ""
    return "\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
