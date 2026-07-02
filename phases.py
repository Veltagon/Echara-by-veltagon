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
import shutil
from pathlib import Path

from state import ProjectState


class VerifyFailed(Exception):
    """Verification failed. str(exc) is the exact error fed back to Builder."""


class AgentDispatchError(Exception):
    """An agent could not run at all (every provider lane down / invalid plan).
    Distinct from bad output — that's the Verifier's job. The orchestrator
    stamps a failed verdict and leaves state resumable."""


def real_agents() -> dict:
    """Late imports so the dry-run path never loads provider/LLM machinery."""
    from agents import builder, planner, repairs, verifier

    return {
        "planner": planner.run_planner,
        "builder": builder.run_builder,
        "repair": repairs.repair_all,
        "verifier": verifier.verify,
        "failure_summary": verifier.failure_summary,
    }


def phase_intake(state: ProjectState, build_dir: Path, agents: dict) -> str:
    if not state.user_prompt.strip():
        raise ValueError("INTAKE: no user prompt — pass --prompt")
    (build_dir / "PROMPT.txt").write_text(state.user_prompt, encoding="utf-8")
    return f"saved prompt ({len(state.user_prompt)} chars)"


def phase_plan(state: ProjectState, build_dir: Path, agents: dict) -> str:
    info = agents["planner"](state.user_prompt, build_dir)
    return f"plan written by {info.get('model', '?')} in {info.get('attempts', '?')} attempt(s)"


def phase_build(state: ProjectState, build_dir: Path, agents: dict) -> str:
    (build_dir / "code").mkdir(exist_ok=True)
    info = agents["builder"](build_dir, last_error=state.last_error)
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
    verdict = {
        "deployment_verified": True,
        "build_id": state.build_id,
        "retries_used": state.retry_count,
        "checks": "import_smoke, alembic_upgrade, pytest — all passed",
    }
    (build_dir / "BUILD_VERDICT.json").write_text(json.dumps(verdict, indent=2),
                                                  encoding="utf-8")
    out = build_dir / "output"
    if (build_dir / "code").is_dir():
        shutil.copytree(build_dir / "code", out, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", ".repairs_*"))
    return f"stamped BUILD_VERDICT.json, code copied to {out.name}/"


PHASE_FNS = {
    "INTAKE": phase_intake,
    "PLAN": phase_plan,
    "BUILD": phase_build,
    "REPAIR": phase_repair,
    "VERIFY": phase_verify,
    "DELIVER": phase_deliver,
}
