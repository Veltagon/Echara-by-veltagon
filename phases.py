"""M4 phase implementations: INTAKE → PLAN → BUILD → REPAIR → VERIFY → DELIVER.

Each phase is `fn(state, build_dir, agents) -> str` (one-line summary). `agents`
is an injectable dict — the orchestrator wires the real planner/builder/
verifier/repair via `real_agents()`; the dry-run tests inject stubs, so the
whole pipeline is walkable with zero model calls.

VERIFY raises VerifyFailed with the exact failing output; the orchestrator owns
the retry policy (max 3, then a failed verdict — no refine loop, no scores).
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from state import ProjectState


def _rmtree_long(path: Path) -> None:
    """shutil.rmtree that survives Windows MAX_PATH via the extended-length path
    prefix (a deeply self-nested code/ tree overruns 260 chars — the same limit
    that makes copytree die with WinError 1921)."""
    target = os.path.abspath(str(path))
    shutil.rmtree("\\\\?\\" + target if os.name == "nt" else target,
                  ignore_errors=True)


def _prune_nested_code(code_root: Path) -> int:
    """Remove any directory named 'code' nested inside the build's code/ tree.

    A model builder session can run a stray recursive copy (e.g. `cp -r code
    code/backend/`), nesting code/ into itself until the path exceeds Windows
    MAX_PATH and copytree dies with WinError 1921 (E1, 2026-07-05). Real
    path_roots are code/backend/app/* and code/frontend/src/* — a 'code' dir
    under code/ is always that junk, safe to delete. rglob is top-down, so the
    shallowest junk dir is found on a short path before any over-length scandir
    happens; _rmtree_long then clears the deep tree in one shot."""
    removed = 0
    while code_root.is_dir():
        junk = next((d for d in code_root.rglob("code") if d.is_dir()), None)
        if junk is None:
            break
        _rmtree_long(junk)
        removed += 1
        if junk.is_dir():  # rmtree could not remove it — stop rather than spin
            break
    return removed


class VerifyFailed(Exception):
    """Verification failed. str(exc) is the exact error fed back to Builder."""


class AgentDispatchError(Exception):
    """An agent could not run at all (every provider lane down / invalid plan).
    Distinct from bad output — that's the Verifier's job. The orchestrator
    stamps a failed verdict and leaves state resumable."""


class ApprovalPending(Exception):
    """Architecture is drafted and validated but not yet human-approved (M5
    gate 1). The orchestrator catches this, saves state, tells the user which
    files to review + to re-run with --approve, and exits 0 (not a failure)."""


def real_agents() -> dict:
    """Late imports so the dry-run path never loads provider/LLM machinery."""
    from agents import architect, builder, planner, repairs, verifier

    return {
        "architect": architect.run_architect,
        "planner": planner.run_planner,
        "builder": builder.run_builder,
        "repair": repairs.repair_all,
        "verifier": verifier.verify,
        "failure_summary": verifier.failure_summary,
    }


_FRONTEND_HINTS = ("frontend", "react", "dashboard", "admin panel", "web ui",
                   "web app", "user interface", " ui ", "single-page")


def phase_intake(state: ProjectState, build_dir: Path, agents: dict) -> str:
    if not state.user_prompt.strip():
        raise ValueError("INTAKE: no user prompt — pass --prompt")
    # Fail fast if the request implies a UI but the toolchain is absent (better
    # here than after an hour of backend waves).
    if any(k in state.user_prompt.lower() for k in _FRONTEND_HINTS):
        import shutil
        if not (shutil.which("node") and shutil.which("npm")):
            raise ValueError("INTAKE: the prompt implies a frontend but node/npm "
                             "are not on PATH — install Node.js and retry")
    (build_dir / "PROMPT.txt").write_text(state.user_prompt, encoding="utf-8")
    return f"saved prompt ({len(state.user_prompt)} chars)"


def phase_plan(state: ProjectState, build_dir: Path, agents: dict) -> str:
    # M5 hierarchical planning: an Architect decomposes the request into modules,
    # a HUMAN approves the architecture (gate 1), then per-module planners run.
    # When no architect agent is wired (dry-run stubs), fall straight through to
    # the flat single-plan path — the pipeline tests are unaffected.
    architect = agents.get("architect")
    if architect is not None:
        if not (build_dir / "ARCHITECTURE.md").is_file():
            arch = architect(state.user_prompt, build_dir)
            log_line = f"architect drafted {arch.get('modules', '?')} modules"
        else:
            log_line = "architecture already drafted"
        if (build_dir / "MODULES.json").is_file() and not state.approved:
            raise ApprovalPending(log_line + " — review and approve to continue")
    info = agents["planner"](state.user_prompt, build_dir)
    return f"plan written by {info.get('model', '?')} in {info.get('attempts', '?')} attempt(s)"


def phase_build(state: ProjectState, build_dir: Path, agents: dict) -> str:
    (build_dir / "code").mkdir(exist_ok=True)
    info = agents["builder"](build_dir, last_error=state.last_error)
    _prune_nested_code(build_dir / "code")  # kill a builder's stray recursive copy
    return f"built via {info.get('provider', '?')} in {info.get('elapsed_sec', '?')}s"


def phase_repair(state: ProjectState, build_dir: Path, agents: dict) -> str:
    actions = agents["repair"](build_dir / "code")
    log_path = build_dir / "REPAIR_LOG.json"
    history = json.loads(log_path.read_text(encoding="utf-8")) if log_path.is_file() else []
    history.append({"attempt": state.retry_count, "actions": actions})
    log_path.write_text(json.dumps(history, indent=2), encoding="utf-8")
    return f"{len(actions)} repair action(s)"


def phase_verify(state: ProjectState, build_dir: Path, agents: dict) -> str:
    report = agents["verifier"](build_dir)
    if not report["verified"]:
        raise VerifyFailed(agents["failure_summary"](report))
    state.last_error = ""
    passed = ", ".join(report["checks"])
    return f"verified: {passed} all passed"


def phase_deliver(state: ProjectState, build_dir: Path, agents: dict) -> str:
    _prune_nested_code(build_dir / "code")  # before LOC report + copytree walk it
    verdict = {
        "deployment_verified": True,
        "build_id": state.build_id,
        "retries_used": state.retry_count,
        "checks": "import_smoke, alembic_upgrade, pytest — all passed",
    }
    (build_dir / "BUILD_VERDICT.json").write_text(json.dumps(verdict, indent=2),
                                                  encoding="utf-8")
    # Human gate 2: the delivery report (LLM-free, disk-derived).
    from agents import report
    (build_dir / "DELIVERY_REPORT.md").write_text(
        report.delivery_report(build_dir), encoding="utf-8")
    out = build_dir / "output"
    if out.exists():
        _rmtree_long(out)  # idempotent: a re-delivery must not inherit a stale/partial copy
    if (build_dir / "code").is_dir():
        shutil.copytree(build_dir / "code", out, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(
                            "__pycache__", ".repairs_*", "node_modules",
                            ".pytest_cache", "dist"))
    return f"stamped BUILD_VERDICT.json + DELIVERY_REPORT.md, code copied to {out.name}/"


PHASE_FNS = {
    "INTAKE": phase_intake,
    "PLAN": phase_plan,
    "BUILD": phase_build,
    "REPAIR": phase_repair,
    "VERIFY": phase_verify,
    "DELIVER": phase_deliver,
}
