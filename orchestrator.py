"""ECHARA v2 orchestrator — milestone 4.

Walks INTAKE → PLAN → BUILD → REPAIR → VERIFY → DELIVER with real agents.
State persists to PROJECT_STATE.json after every transition and on SIGINT, so a
kill mid-build resumes cleanly. VERIFY failure rewinds to BUILD with the exact
error (max 3 retries), then stamps a failed verdict. No refine loop, no scores.

Usage:
    python orchestrator.py --prompt "Build a small CRUD API for managing notes"
    python orchestrator.py            # resume an interrupted build
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

from phases import PHASE_FNS, VerifyFailed, real_agents
from state import DONE, MAX_RETRIES, STATE_FILE, ProjectState


def _install_sigint_handler(state: ProjectState) -> None:
    def handler(_sig, _frame):
        print(f"\n[ECHARA] SIGINT — saving state (next phase: {state.current_phase})")
        state.save()
        sys.exit(130)

    signal.signal(signal.SIGINT, handler)


def _bootstrap_state(prompt: str | None) -> ProjectState:
    state = ProjectState.load()
    if state is None or state.current_phase == DONE:
        if state is not None:
            print(f"[ECHARA] previous build {state.build_id} finished — starting fresh")
        state = ProjectState.new()
        state.user_prompt = prompt or ""
        print(f"[ECHARA] new build: {state.build_id}")
    else:
        print(f"[ECHARA] resuming {state.build_id} at phase {state.current_phase} "
              f"(retry {state.retry_count}/{MAX_RETRIES})")
        if prompt:
            state.user_prompt = prompt
    return state


def _stamp_failed(state: ProjectState, build_dir: Path, error: str) -> None:
    verdict = {
        "deployment_verified": False,
        "build_id": state.build_id,
        "retries_used": state.retry_count,
        "error": error,
    }
    (build_dir / "BUILD_VERDICT.json").write_text(json.dumps(verdict, indent=2),
                                                  encoding="utf-8")


def run(prompt: str | None = None, agents: dict | None = None) -> int:
    state = _bootstrap_state(prompt)
    state.save()
    _install_sigint_handler(state)

    build_dir = Path(state.build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)
    agents = agents or real_agents()

    while state.current_phase != DONE:
        phase = state.current_phase
        if phase not in PHASE_FNS:
            print(f"[ECHARA] corrupted state: unknown phase {phase!r} "
                  f"(expected one of {list(PHASE_FNS)} or {DONE}). "
                  f"Delete {STATE_FILE} to restart cleanly.")
            return 1
        print(f"[ECHARA] >>> {phase}")
        try:
            summary = PHASE_FNS[phase](state, build_dir, agents)
        except VerifyFailed as e:
            if state.retry_build(str(e)):
                print(f"[ECHARA] VERIFY failed — retry {state.retry_count}/{MAX_RETRIES}, "
                      f"feeding error back to BUILD")
                state.save()
                continue
            state.verdict = f"failed: verification failed after {MAX_RETRIES} retries"
            _stamp_failed(state, build_dir, str(e))
            state.save()
            print(f"[ECHARA] FAILED after {MAX_RETRIES} retries:\n{e}")
            return 1
        print(f"[ECHARA] <<< {phase}: {summary}")
        state.mark_done(phase)
        state.save()

    state.verdict = state.verdict or "delivered"
    state.save()
    print(f"[ECHARA] done — build_dir = {build_dir.as_posix()}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", default=None,
                        help="User request for a new build (omit to resume).")
    args = parser.parse_args()
    sys.exit(run(args.prompt))
