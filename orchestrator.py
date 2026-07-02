"""ECHARA v2 orchestrator — milestone 1.

Walks INTAKE -> PLAN -> BUILD -> VERIFY -> DELIVER. Each phase writes hardcoded
placeholder files. State persists to PROJECT_STATE.json after every phase and
on SIGINT, so a kill mid-build resumes cleanly on the next run.

Usage:
    python orchestrator.py
"""
from __future__ import annotations

import signal
import sys
from pathlib import Path

from phases import (
    VerifyFailed,
    phase_build,
    phase_deliver,
    phase_intake,
    phase_plan,
    phase_verify,
)
from state import DONE, STATE_FILE, ProjectState

PHASE_FNS = {
    "INTAKE":  phase_intake,
    "PLAN":    phase_plan,
    "BUILD":   phase_build,
    "VERIFY":  phase_verify,
    "DELIVER": phase_deliver,
}


def _install_sigint_handler(state: ProjectState) -> None:
    def handler(_sig, _frame):
        print(f"\n[ECHARA] SIGINT — saving state (next phase: {state.current_phase})")
        state.save()
        sys.exit(130)

    signal.signal(signal.SIGINT, handler)


def _bootstrap_state() -> ProjectState:
    state = ProjectState.load()
    if state is None:
        state = ProjectState.new()
        print(f"[ECHARA] new build: {state.build_id}")
    elif state.current_phase == DONE:
        print(f"[ECHARA] previous build {state.build_id} already finished — starting fresh")
        state = ProjectState.new()
        print(f"[ECHARA] new build: {state.build_id}")
    else:
        print(f"[ECHARA] resuming {state.build_id} at phase {state.current_phase}")
    return state


def run() -> int:
    state = _bootstrap_state()
    state.save()
    _install_sigint_handler(state)

    build_dir = Path(state.build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)

    while state.current_phase != DONE:
        phase = state.current_phase
        if phase not in PHASE_FNS:
            print(f"[ECHARA] corrupted state: unknown phase {phase!r} "
                  f"(expected one of {list(PHASE_FNS)} or {DONE}). "
                  f"Delete {STATE_FILE} to restart cleanly.")
            return 1
        print(f"[ECHARA] >>> {phase}")
        try:
            summary = PHASE_FNS[phase](build_dir)
        except VerifyFailed as e:
            state.verdict = f"VERIFY failed: {e}"
            state.save()
            print(f"[ECHARA] VERIFY failed: {e}")
            return 1
        print(f"[ECHARA] <<< {phase}: {summary}")
        state.mark_done(phase)
        state.save()

    state.verdict = state.verdict or "delivered"
    state.save()
    print(f"[ECHARA] done — build_dir = {build_dir.as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
