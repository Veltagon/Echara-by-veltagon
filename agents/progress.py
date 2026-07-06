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
import os
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
# 25 was sized for small builds; a 16-module ERP (E3-v2, 2026-07-06) exhausted it
# before the frontend even started. Scale it and make it tunable per build — a
# large build legitimately needs ~2-3 fixes/module. Default fits ~16-20 modules.
GLOBAL_FIX_BUDGET = int(os.environ.get("ECHARA_FIX_BUDGET", "50"))


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


def token_summary(build_dir: Path) -> dict:
    """Aggregate per-invocation token usage from BUILD_METRICS: {total, by_lane}.
    The per-lane 'cached' figure is the empirical answer to the §0.2 question —
    does the API fleet (Cerebras / HF / NVIDIA) actually cache the frozen prefix?
    'avg_input' per lane tests §1's flat-curve claim (should not grow with S)."""
    tot = {"input": 0, "output": 0, "cached": 0, "cache_creation": 0}
    by_lane: dict[str, dict] = {}
    for e in metrics(build_dir):
        u = e.get("usage") or {}
        for k in tot:
            tot[k] += u.get(k, 0) or 0
        ln = by_lane.setdefault(e.get("lane", "?"),
                                {"input": 0, "output": 0, "cached": 0, "n": 0})
        ln["input"] += u.get("input", 0) or 0
        ln["output"] += u.get("output", 0) or 0
        ln["cached"] += u.get("cached", 0) or 0
        ln["n"] += 1
    for ln in by_lane.values():
        ln["avg_input"] = round(ln["input"] / ln["n"], 1) if ln["n"] else 0
    return {"total": tot, "by_lane": by_lane}
