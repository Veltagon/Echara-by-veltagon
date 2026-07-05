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
import threading
from pathlib import Path

_PROGRESS = "BUILD_PROGRESS.json"
_JOURNAL = "BUILD_JOURNAL.md"
_METRICS = "BUILD_METRICS.json"

# Re-entrant so record_fix (which calls module_state + save) can hold it across
# nested calls. Serializes every shared write-through mutation + file write, so
# the concurrent module builder (ECHARA_CONCURRENCY>1) can't corrupt these files
# or double-count the global fix budget. Uncontended (no-op) at concurrency 1.
_LOCK = threading.RLock()

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
    with _LOCK:
        (Path(build_dir) / _PROGRESS).write_text(json.dumps(data, indent=2), encoding="utf-8")


def module_state(data: dict, module: str) -> dict:
    with _LOCK:  # setdefault must not race a concurrent save() iterating the dict
        return data["modules"].setdefault(
            module, {"waves_done": 0, "gate_fixes": 0, "integration_fixes": 0,
                     "seam_fixes": 0, "seams_ok": False, "integrated": False})


def can_fix(data: dict) -> bool:
    """True if the global fix budget still has room."""
    with _LOCK:
        return data.get("global_fix_budget_used", 0) < GLOBAL_FIX_BUDGET


def record_fix(build_dir: Path, data: dict, module: str, kind: str) -> None:
    """Charge one fix against the global budget + the module counter, and
    persist BEFORE the fix dispatches (crash-safe budget). Atomic under _LOCK so
    concurrent workers can't double-spend the shared budget."""
    with _LOCK:
        data["global_fix_budget_used"] = data.get("global_fix_budget_used", 0) + 1
        module_state(data, module)[f"{kind}_fixes"] = \
            module_state(data, module).get(f"{kind}_fixes", 0) + 1
        save(build_dir, data)


def journal_append(build_dir: Path, line: str) -> None:
    with _LOCK, (Path(build_dir) / _JOURNAL).open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")


def journal_tail(build_dir: Path, n: int = 30) -> str:
    p = Path(build_dir) / _JOURNAL
    if not p.is_file():
        return ""
    return "\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])


def metric_append(build_dir: Path, entry: dict) -> None:
    """Append one per-session record (lane/model/duration/outcome) for tuning
    and the delivery report."""
    p = Path(build_dir) / _METRICS
    with _LOCK:  # read-modify-write of a shared file — must be atomic across lanes
        data = []
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = []
        data.append(entry)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def metrics(build_dir: Path) -> list[dict]:
    p = Path(build_dir) / _METRICS
    if not p.is_file():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
