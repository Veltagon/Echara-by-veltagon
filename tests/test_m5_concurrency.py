"""Concurrent multi-lane module router: independent modules of a topological
layer build in PARALLEL (each on its own lane); dependents wait for their layer.
No real API calls — CLI lanes are marked exhausted so dispatch routes to the API
fleet, and run_harness is faked to (a) write the wave's files and (b) record how
many builds run simultaneously."""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from agents import builder
from providers import availability

# 3-module DAG: core (layer 1) <- {a, b} (layer 2, independent of each other).
_MODULES = [
    {"name": "core", "kind": "backend", "loc_budget": 400, "depends_on": [],
     "path_root": "code/backend/app/core"},
    {"name": "a", "kind": "backend", "loc_budget": 400, "depends_on": ["core"],
     "path_root": "code/backend/app/a"},
    {"name": "b", "kind": "backend", "loc_budget": 400, "depends_on": ["core"],
     "path_root": "code/backend/app/b"},
]


def _setup(build_dir: Path) -> None:
    (build_dir / "code").mkdir(parents=True)
    (build_dir / "MODULES.json").write_text(json.dumps(_MODULES), encoding="utf-8")
    (build_dir / "SEAMS.json").write_text("{}", encoding="utf-8")
    (build_dir / "CONVENTIONS.md").write_text("conventions.", encoding="utf-8")
    (build_dir / "ARCHITECTURE.md").write_text("arch.", encoding="utf-8")
    for m in _MODULES:
        root = m["path_root"]
        (build_dir / f"PLAN_{m['name']}.md").write_text(
            f"1. `{root}/__init__.py` — init\n2. `{root}/svc.py` — service\n", encoding="utf-8")


def _fake_harness(active, maxc, violations, alock):
    def fake(provider, task, workspace, **kw):
        ws = Path(workspace)
        paths = re.findall(r"code/[\w/.\-]+\.py", task)
        # dependency invariant: no a/b file is written before core exists on disk
        if any("/app/a/" in p or "/app/b/" in p for p in paths):
            if not (ws / "code/backend/app/core/__init__.py").is_file():
                violations.append("built a/b before core")
        with alock:
            active[0] += 1
            maxc[0] = max(maxc[0], active[0])
        time.sleep(0.15)  # widen the window so genuine overlap is observable
        for rel in paths:
            p = ws / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if not p.exists():
                p.write_text("x = 1\n", encoding="utf-8")
        with alock:
            active[0] -= 1
        return {"stop_reason": "done", "tool_calls": 3}
    return fake


def _run(build_dir, monkeypatch, concurrency):
    monkeypatch.setattr(builder.skills_router, "DEFAULT_POOL_ROOT", build_dir / "nopool")
    monkeypatch.setenv("ECHARA_CONCURRENCY", str(concurrency))
    availability.mark_exhausted("claude", time.time() + 9999)  # force API-fleet routing
    availability.mark_exhausted("codex", time.time() + 9999)
    active, maxc, violations, alock = [0], [0], [], threading.Lock()
    import run_harness_agent
    monkeypatch.setattr(run_harness_agent, "run_harness",
                        _fake_harness(active, maxc, violations, alock))
    info = builder.run_builder(build_dir)
    return info, maxc[0], violations


def test_independent_modules_build_in_parallel(tmp_path, monkeypatch):
    build_dir = tmp_path / "b"
    _setup(build_dir)
    info, maxc, violations = _run(build_dir, monkeypatch, concurrency=2)

    for m in _MODULES:  # every module's files got built
        assert (build_dir / m["path_root"] / "svc.py").is_file()
    assert not violations, violations               # core always built before a/b
    assert maxc >= 2, f"expected parallel builds, saw max {maxc} concurrent"


def test_sequential_when_concurrency_one(tmp_path, monkeypatch):
    build_dir = tmp_path / "b1"
    _setup(build_dir)
    info, maxc, violations = _run(build_dir, monkeypatch, concurrency=1)

    for m in _MODULES:
        assert (build_dir / m["path_root"] / "svc.py").is_file()
    assert not violations, violations
    assert maxc == 1, f"concurrency=1 must be sequential, saw max {maxc}"
