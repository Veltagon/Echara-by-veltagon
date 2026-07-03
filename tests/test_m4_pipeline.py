"""Test 1 — phase-walk dry run. Zero model calls: planner/builder/verifier are
stubs; REPAIR runs the real deterministic repair_all (it's code, not an agent).

Covers: full walk with state saves, resume from every phase, the retry loop
(VERIFY fail → BUILD → REPAIR → VERIFY), and fail-after-3-retries.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import orchestrator
from agents.repairs import repair_all
from state import DONE, PHASES, ProjectState


class StubAgents:
    """Callable stubs with call counters and a scriptable verifier."""

    def __init__(self, verify_results: list[bool] | None = None):
        self.calls: dict[str, int] = {"planner": 0, "builder": 0, "verifier": 0}
        self.builder_errors_seen: list[str] = []
        self.verify_results = list(verify_results or [True])

    def as_dict(self) -> dict:
        return {
            "planner": self.planner,
            "builder": self.builder,
            "repair": repair_all,  # real deterministic code — no model
            "verifier": self.verifier,
            "failure_summary": lambda report: report["checks"]["pytest"]["detail"],
        }

    def planner(self, prompt: str, build_dir: Path, log=None) -> dict:
        self.calls["planner"] += 1
        (build_dir / "PLAN.md").write_text("# PLAN\n## File manifest\n- x", encoding="utf-8")
        (build_dir / "CONTRACT_REGISTRY.json").write_text(json.dumps({
            "api_endpoints": [{"method": "GET", "path": "/api/x",
                               "request_schema": None, "response_schema": "XOut"}],
            "shared_types": [], "db_tables": [], "env_vars": [], "dependencies": [],
        }), encoding="utf-8")
        return {"model": "stub", "attempts": 1}

    def builder(self, build_dir: Path, last_error: str = "", log=None) -> dict:
        self.calls["builder"] += 1
        self.builder_errors_seen.append(last_error)
        app = build_dir / "code" / "backend" / "app"
        app.mkdir(parents=True, exist_ok=True)
        (app / "__init__.py").write_text("", encoding="utf-8")
        (app / "main.py").write_text("app = object()\n", encoding="utf-8")
        return {"provider": "stub", "elapsed_sec": 0.0}

    def verifier(self, build_dir: Path) -> dict:
        self.calls["verifier"] += 1
        passed = self.verify_results.pop(0) if self.verify_results else True
        detail = "ok" if passed else "1 failed: assert resp.status_code == 201"
        return {"verified": passed,
                "checks": {"import_smoke": {"passed": True, "detail": "ok"},
                           "alembic_upgrade": {"passed": True, "detail": "ok"},
                           "pytest": {"passed": passed, "detail": detail}}}


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # isolates PROJECT_STATE.json + builds/
    return tmp_path


def test_full_walk_all_transitions(workdir):
    stubs = StubAgents()
    assert orchestrator.run("build a thing", stubs.as_dict()) == 0
    state = ProjectState.load()
    # every phase transitioned exactly once, in order, and state persisted
    assert state.completed_phases == PHASES
    assert state.current_phase == DONE
    assert state.verdict == "delivered"
    build_dir = Path(state.build_dir)
    verdict = json.loads((build_dir / "BUILD_VERDICT.json").read_text(encoding="utf-8"))
    assert verdict["deployment_verified"] is True
    assert (build_dir / "output" / "backend" / "app" / "main.py").is_file()
    assert (build_dir / "code" / ".repairs_complete").exists()  # barrier flipped
    assert stubs.calls == {"planner": 1, "builder": 1, "verifier": 1}


def test_resume_after_crash(workdir):
    # First run dies inside REPAIR (after BUILD was marked done + saved).
    stubs = StubAgents()
    agents = stubs.as_dict()
    agents["repair"] = lambda code_dir: (_ for _ in ()).throw(RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        orchestrator.run("build a thing", agents)
    state = ProjectState.load()
    assert state.current_phase == "REPAIR"  # saved exactly where it died

    # Second run resumes at REPAIR: planner/builder must NOT run again.
    resumed = StubAgents()
    assert orchestrator.run(None, resumed.as_dict()) == 0
    assert resumed.calls["planner"] == 0
    assert resumed.calls["builder"] == 0
    assert resumed.calls["verifier"] == 1
    assert ProjectState.load().current_phase == DONE


@pytest.mark.parametrize("start_phase", ["PLAN", "BUILD", "REPAIR", "VERIFY", "DELIVER"])
def test_resume_from_any_phase(workdir, start_phase):
    # Hand-craft a state parked at start_phase with the artifacts earlier
    # phases would have produced, then confirm the walk completes from there.
    state = ProjectState.new()
    state.user_prompt = "build a thing"
    build_dir = Path(state.build_dir)
    pre = StubAgents()
    pre.planner("build a thing", build_dir.absolute() if build_dir.mkdir(parents=True, exist_ok=True) else build_dir)
    pre.builder(build_dir)
    idx = PHASES.index(start_phase)
    state.completed_phases = PHASES[:idx]
    state.current_phase = start_phase
    state.save()

    stubs = StubAgents()
    assert orchestrator.run(None, stubs.as_dict()) == 0
    # only the phases from start_phase onward ran
    assert stubs.calls["planner"] == (1 if idx <= PHASES.index("PLAN") else 0)
    assert stubs.calls["builder"] == (1 if idx <= PHASES.index("BUILD") else 0)
    assert stubs.calls["verifier"] == (1 if idx <= PHASES.index("VERIFY") else 0)
    assert ProjectState.load().current_phase == DONE


def test_retry_loop_verify_fail_then_pass(workdir):
    # VERIFY fails twice, passes on the 3rd — loop is VERIFY→BUILD→REPAIR→VERIFY.
    stubs = StubAgents(verify_results=[False, False, True])
    assert orchestrator.run("build a thing", stubs.as_dict()) == 0
    state = ProjectState.load()
    assert state.retry_count == 2
    assert stubs.calls["builder"] == 3          # initial + 2 retries
    assert stubs.calls["verifier"] == 3
    assert stubs.builder_errors_seen[0] == ""   # first build: no feedback
    assert "assert resp.status_code" in stubs.builder_errors_seen[1]  # exact error fed back
    assert "assert resp.status_code" in stubs.builder_errors_seen[2]
    assert state.last_error == ""               # cleared on the passing verify
    assert json.loads((Path(state.build_dir) / "BUILD_VERDICT.json")
                      .read_text(encoding="utf-8"))["deployment_verified"] is True


def test_wave_parser_and_chunking():
    from agents.builder import _implementation_order, _waves
    plan = (
        "## File manifest\n"
        "code/backend/app/main.py — entry\n"
        "## Implementation order\n"
        "1. code/backend/requirements.txt\n"
        "2. `code/backend/app/db.py`\n"
        "3. code/backend/app/models/user.py — user model\n"
        "4. code/backend/app/models/user.py\n"          # dup — must dedupe
        "5. code/backend/tests/test_users.py - tests\n"
    )
    files = _implementation_order(plan)
    assert files == ["code/backend/requirements.txt", "code/backend/app/db.py",
                     "code/backend/app/models/user.py", "code/backend/tests/test_users.py"]
    chunks = _waves(list(range(19)), size=8)
    assert [len(c) for c in chunks] == [8, 8, 3]


def test_prompt_always_starts_fresh(workdir):
    # A FAILED (unfinished) state must not hijack a new --prompt run: the old
    # behavior re-verified dead code ("13-second build", auth eval 2026-07-03).
    stubs = StubAgents(verify_results=[False, False, False, False])
    assert orchestrator.run("build a thing", stubs.as_dict()) == 1
    failed_id = ProjectState.load().build_id

    fresh = StubAgents()
    assert orchestrator.run("build a thing", fresh.as_dict()) == 0
    state = ProjectState.load()
    assert state.build_id != failed_id          # genuinely new build
    assert fresh.calls["planner"] == 1          # planned from scratch
    assert state.retry_count == 0

    # resume with nothing to resume -> clear exit, not a silent new build
    with pytest.raises(SystemExit):
        orchestrator.run(None, StubAgents().as_dict())


def test_dispatch_failure_stamps_clean_verdict(workdir):
    # All builder lanes down -> clean failed verdict, no traceback, resumable.
    from phases import AgentDispatchError
    stubs = StubAgents()
    agents = stubs.as_dict()

    def dead_builder(build_dir, last_error="", log=None):
        raise AgentDispatchError("claude: 429 session limit; codex: dead")
    agents["builder"] = dead_builder

    assert orchestrator.run("build a thing", agents) == 1
    state = ProjectState.load()
    assert state.current_phase == "BUILD"  # parked, resumable
    verdict = json.loads((Path(state.build_dir) / "BUILD_VERDICT.json")
                         .read_text(encoding="utf-8"))
    assert verdict["deployment_verified"] is False
    assert "429" in verdict["error"]

    # Lanes come back -> plain resume completes without re-planning.
    resumed = StubAgents()
    assert orchestrator.run(None, resumed.as_dict()) == 0
    assert resumed.calls["planner"] == 0 and resumed.calls["builder"] == 1


def test_fail_after_three_retries(workdir):
    stubs = StubAgents(verify_results=[False, False, False, False])
    assert orchestrator.run("build a thing", stubs.as_dict()) == 1
    state = ProjectState.load()
    assert state.retry_count == 3
    assert stubs.calls["builder"] == 4          # initial + 3 retries, then stop
    assert state.verdict.startswith("failed")
    verdict = json.loads((Path(state.build_dir) / "BUILD_VERDICT.json")
                         .read_text(encoding="utf-8"))
    assert verdict["deployment_verified"] is False
    assert "assert resp.status_code" in verdict["error"]  # exact trace preserved
