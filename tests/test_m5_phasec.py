"""Phase C — hierarchical planning + approval gate. No model calls."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import orchestrator
from agents import architect, planner
from state import DONE, PHASES, ProjectState
from tests.test_m4_pipeline import StubAgents  # reuse the stub harness


def _write_arch(build: Path, modules, seams=None):
    build.mkdir(parents=True, exist_ok=True)
    (build / "ARCHITECTURE.md").write_text("# Arch\n" + "x" * 300, encoding="utf-8")
    (build / "CONVENTIONS.md").write_text("Use bcrypt directly. " * 20, encoding="utf-8")
    (build / "MODULES.json").write_text(json.dumps(modules), encoding="utf-8")
    (build / "SEAMS.json").write_text(json.dumps(seams or {}), encoding="utf-8")


_GOOD = [
    {"name": "core", "kind": "backend", "loc_budget": 800, "depends_on": [],
     "path_root": "code/backend/app/core"},
    {"name": "users", "kind": "backend", "loc_budget": 1500, "depends_on": ["core"],
     "path_root": "code/backend/app/users"},
    {"name": "notes", "kind": "backend", "loc_budget": 1500, "depends_on": ["core", "users"],
     "path_root": "code/backend/app/notes"},
    {"name": "wiring", "kind": "backend", "loc_budget": 400, "depends_on": ["users", "notes"],
     "path_root": "code/backend/app/main"},
]


def test_validate_architecture_accepts_good(tmp_path):
    _write_arch(tmp_path, _GOOD, {"core": [{"name": "get_db"}]})
    assert architect.validate_architecture(tmp_path) == []


def test_validate_architecture_catches_cycle(tmp_path):
    cyc = [dict(m) for m in _GOOD]
    cyc[0]["depends_on"] = ["wiring"]  # core <- wiring <- notes <- core : cycle
    _write_arch(tmp_path, cyc)
    errs = architect.validate_architecture(tmp_path)
    assert any("cycle" in e for e in errs)


def test_validate_architecture_catches_bad_budget_and_unknown_dep(tmp_path):
    bad = [dict(m) for m in _GOOD]
    bad[1]["loc_budget"] = 9000            # > 3000
    bad[2]["depends_on"] = ["ghost"]       # unknown module
    _write_arch(tmp_path, bad)
    errs = " | ".join(architect.validate_architecture(tmp_path))
    assert "loc_budget" in errs and "unknown module" in errs


def test_module_order_topological(tmp_path):
    _write_arch(tmp_path, _GOOD)
    order = architect.module_order(tmp_path)
    assert order.index("core") < order.index("users") < order.index("notes")
    assert order.index("notes") < order.index("wiring")


def test_module_plan_validator_floor_and_pathroot(tmp_path):
    mod = _GOOD[1]  # users, budget 1500 -> floor max(6, 3) = 6
    # too few files -> rejected
    (tmp_path / "PLAN_users.md").write_text(
        "## File manifest\ncode/backend/app/users/models.py — m\n", encoding="utf-8")
    assert planner._validate_module_plan(tmp_path, mod)
    # foreign path root -> rejected (no files under path_root)
    (tmp_path / "PLAN_users.md").write_text(
        "## File manifest\n" + "".join(
            f"code/backend/app/OTHER/f{i}.py — x\n" for i in range(8)), encoding="utf-8")
    assert any("path_root" in e for e in planner._validate_module_plan(tmp_path, mod))
    # enough files under the right root -> clean
    (tmp_path / "PLAN_users.md").write_text(
        "## File manifest\n" + "".join(
            f"code/backend/app/users/f{i}.py — x\n" for i in range(8)), encoding="utf-8")
    assert planner._validate_module_plan(tmp_path, mod) == []


# --- approval-flow walk (stub architect + planner) --------------------------

class ArchStubAgents(StubAgents):
    """StubAgents + a stub architect that writes a valid MODULES.json set."""
    def as_dict(self):
        d = super().as_dict()
        d["architect"] = self.architect
        return d

    def architect(self, prompt, build_dir, log=None):
        _write_arch(Path(build_dir), _GOOD, {"core": [{"name": "get_db"}]})
        return {"model": "stub", "modules": len(_GOOD)}


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_approval_gate_blocks_then_resumes(workdir):
    # First run: architect drafts, gate raises ApprovalPending -> exit 0, parked.
    stubs = ArchStubAgents()
    assert orchestrator.run("build an app", stubs.as_dict()) == 0
    state = ProjectState.load()
    assert state.current_phase == "PLAN"          # parked at PLAN, not advanced
    assert not state.approved
    assert (Path(state.build_dir) / "MODULES.json").is_file()
    assert stubs.calls["builder"] == 0            # nothing built before approval

    # Resume WITHOUT approve -> still blocked (architect skipped, files exist).
    assert orchestrator.run(None, ArchStubAgents().as_dict()) == 0
    assert ProjectState.load().current_phase == "PLAN"

    # Resume WITH approve -> planner runs, build proceeds to DONE.
    resumed = ArchStubAgents()
    assert orchestrator.run(None, resumed.as_dict(), approve=True) == 0
    final = ProjectState.load()
    assert final.current_phase == DONE
    assert final.approved
    assert resumed.calls["planner"] == 1
