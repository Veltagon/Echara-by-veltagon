"""DELIVERY_REPORT.md — the human gate-2 artifact. LLM-free; reads disk only.

Assembles LOC by module, the verification summary, seam status, journal digest,
and per-session/quota stats into one markdown report at DELIVER.
"""
from __future__ import annotations

import json
from pathlib import Path

from agents import progress

_CODE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx"}


def _loc(root: Path) -> int:
    total = 0
    if not root.is_dir():
        return 0
    for f in root.rglob("*"):
        if f.suffix in _CODE_SUFFIXES and "__pycache__" not in f.parts and "node_modules" not in f.parts:
            try:
                total += len(f.read_text(encoding="utf-8", errors="replace").splitlines())
            except OSError:
                pass
    return total


def _read_json(p: Path, default):
    if not p.is_file():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def delivery_report(build_dir: Path) -> str:
    build_dir = Path(build_dir)
    lines = ["# DELIVERY REPORT", ""]

    # LOC by module (multi) or total (single).
    modules = _read_json(build_dir / "MODULES.json", None)
    lines.append("## LOC")
    if isinstance(modules, list) and modules:
        grand = 0
        lines.append("| module | kind | budget | actual LOC |")
        lines.append("|---|---|---|---|")
        for m in modules:
            actual = _loc(build_dir / m["path_root"])
            grand += actual
            lines.append(f"| {m['name']} | {m.get('kind','?')} | {m.get('loc_budget','?')} | {actual} |")
        lines.append(f"| **total** | | | **{grand}** |")
    else:
        lines.append(f"total: **{_loc(build_dir / 'code')}** LOC")
    lines.append("")

    # Verification summary.
    report = _read_json(build_dir / "VERIFICATION_REPORT.json", {})
    lines.append("## Verification")
    lines.append(f"verified: **{report.get('verified')}**")
    for name, c in (report.get("checks") or {}).items():
        mark = "PASS" if c.get("passed") else "FAIL"
        extra = f" ({c['n_tests']} tests)" if c.get("n_tests") else ""
        lines.append(f"- {name}: {mark}{extra}")
    fails = (report.get("checks", {}).get("pytest", {}) or {}).get("failures") or []
    if fails:
        lines.append(f"- failing tests ({len(fails)}): "
                     + ", ".join(f"{f['file']}::{f['test']}" for f in fails[:10])
                     + (" …" if len(fails) > 10 else ""))
    lines.append("")

    # Seam status per module.
    prog = _read_json(build_dir / "BUILD_PROGRESS.json", {})
    if prog.get("modules"):
        lines.append("## Module status")
        for name, s in prog["modules"].items():
            lines.append(f"- {name}: waves={s.get('waves_done',0)} "
                         f"integrated={s.get('integrated',False)} seams_ok={s.get('seams_ok',False)} "
                         f"fixes(gate/integ/seam)={s.get('gate_fixes',0)}/"
                         f"{s.get('integration_fixes',0)}/{s.get('seam_fixes',0)}")
        lines.append(f"global fix budget used: {prog.get('global_fix_budget_used',0)}/"
                     f"{progress.GLOBAL_FIX_BUDGET}")
        lines.append("")

    # Session / quota stats.
    ms = progress.metrics(build_dir)
    if ms:
        by_lane: dict = {}
        total_sec = 0.0
        for m in ms:
            by_lane[m.get("lane", "?")] = by_lane.get(m.get("lane", "?"), 0) + 1
            total_sec += float(m.get("elapsed_sec") or 0)
        lines.append("## Sessions")
        lines.append(f"- CLI sessions: {len(ms)} ({', '.join(f'{k}={v}' for k, v in by_lane.items())})")
        lines.append(f"- wall-clock in sessions: {total_sec / 3600:.1f} h")
        models = sorted({m.get("model", "?") for m in ms})
        lines.append(f"- model tiers used: {', '.join(models)}")
        lines.append("")

    # Token model (§1 instrumentation): per-lane input/output/cached. Answers the
    # §0.2 caching question and whether per-invocation input stays flat with S.
    tks = progress.token_summary(build_dir)
    if tks["total"]["input"] or tks["total"]["output"]:
        t = tks["total"]
        lines.append("## Tokens (per-invocation)")
        lines.append(f"- total: input={t['input']:,} output={t['output']:,} "
                     f"cached={t['cached']:,} cache_creation={t['cache_creation']:,}")
        lines.append("| lane | invocations | avg input | total input | output | cached |")
        lines.append("|---|---|---|---|---|---|")
        for lane, l in sorted(tks["by_lane"].items()):
            lines.append(f"| {lane} | {l['n']} | {l['avg_input']} | {l['input']:,} | "
                         f"{l['output']:,} | {l['cached']:,} |")
        lines.append("")

    # Journal digest (tail).
    tail = progress.journal_tail(build_dir, n=40)
    if tail:
        lines.append("## Build journal (recent)")
        lines.append("```")
        lines.append(tail)
        lines.append("```")
    return "\n".join(lines) + "\n"
