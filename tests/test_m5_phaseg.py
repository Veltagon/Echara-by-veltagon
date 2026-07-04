"""Phase G — metrics + delivery report. No model calls."""
from __future__ import annotations

import json
from pathlib import Path

from agents import progress, report


def test_metric_append_roundtrip(tmp_path):
    progress.metric_append(tmp_path, {"label": "core w1/2", "lane": "claude",
                                      "model": "sonnet", "elapsed_sec": 120.0, "outcome": "ok"})
    progress.metric_append(tmp_path, {"label": "core w2/2", "lane": "codex",
                                      "model": "default", "elapsed_sec": 90.0, "outcome": "fail"})
    ms = progress.metrics(tmp_path)
    assert len(ms) == 2
    assert ms[0]["lane"] == "claude" and ms[1]["outcome"] == "fail"
    assert progress.metrics(tmp_path / "empty") == []


def test_delivery_report_multi_module(tmp_path):
    build = tmp_path
    (build / "MODULES.json").write_text(json.dumps([
        {"name": "core", "kind": "backend", "loc_budget": 800, "depends_on": [],
         "path_root": "code/backend/app/core"},
        {"name": "notes", "kind": "backend", "loc_budget": 1500, "depends_on": ["core"],
         "path_root": "code/backend/app/notes"},
    ]), encoding="utf-8")
    (build / "code" / "backend" / "app" / "core").mkdir(parents=True)
    (build / "code" / "backend" / "app" / "core" / "db.py").write_text(
        "def get_db():\n    return 1\n" * 5, encoding="utf-8")
    (build / "VERIFICATION_REPORT.json").write_text(json.dumps({
        "verified": True, "checks": {
            "import_smoke": {"passed": True},
            "pytest": {"passed": True, "n_tests": 120, "failures": []},
        }}), encoding="utf-8")
    (build / "BUILD_PROGRESS.json").write_text(json.dumps({
        "modules": {"core": {"waves_done": 1, "integrated": True, "seams_ok": True,
                             "gate_fixes": 0, "integration_fixes": 1, "seam_fixes": 0}},
        "global_fix_budget_used": 1}), encoding="utf-8")
    progress.metric_append(build, {"label": "core w1/1", "lane": "claude",
                                   "model": "sonnet", "elapsed_sec": 3600.0, "outcome": "ok"})
    progress.journal_append(build, "core w1/1: db.py, models.py [gate ok]")

    md = report.delivery_report(build)
    assert "# DELIVERY REPORT" in md
    assert "| core | backend | 800 |" in md          # LOC table row
    assert "**total**" in md
    assert "verified: **True**" in md
    assert "pytest: PASS (120 tests)" in md
    assert "core: waves=1 integrated=True seams_ok=True" in md
    assert "CLI sessions: 1" in md and "sonnet" in md
    assert "core w1/1: db.py" in md                   # journal digest


def test_delivery_report_single_module(tmp_path):
    (tmp_path / "code" / "backend" / "app").mkdir(parents=True)
    (tmp_path / "code" / "backend" / "app" / "main.py").write_text("x = 1\n" * 10, encoding="utf-8")
    (tmp_path / "VERIFICATION_REPORT.json").write_text(
        json.dumps({"verified": True, "checks": {}}), encoding="utf-8")
    md = report.delivery_report(tmp_path)
    assert "total: **10** LOC" in md
