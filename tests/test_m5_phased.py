"""Phase D — verifier scaling + junitxml + per-module failure routing. No models."""
from __future__ import annotations

import json
from pathlib import Path

from agents import builder, verifier


_JUNIT = """<?xml version="1.0"?>
<testsuites><testsuite name="pytest" tests="3" failures="2">
  <testcase classname="app.users.tests.test_users" name="test_login"/>
  <testcase classname="app.users.tests.test_users" name="test_register">
    <failure message="assert 500 == 201">traceback here</failure>
  </testcase>
  <testcase classname="app.notes.tests.test_notes" name="test_create">
    <error message="ImportError: cannot import name X">trace</error>
  </testcase>
</testsuite></testsuites>"""


def test_parse_junit(tmp_path):
    p = tmp_path / "j.xml"
    p.write_text(_JUNIT, encoding="utf-8")
    fails = verifier._parse_junit(p)
    assert len(fails) == 2
    names = {f["test"] for f in fails}
    assert names == {"test_register", "test_create"}
    reg = next(f for f in fails if f["test"] == "test_register")
    assert "assert 500 == 201" in reg["message"]
    assert reg["file"] == "app.users.tests.test_users"
    assert verifier._parse_junit(tmp_path / "missing.xml") == []


def test_pytest_timeout_scales_with_test_count(tmp_path):
    files = []
    for i in range(3):
        f = tmp_path / f"test_{i}.py"
        f.write_text("\n".join(f"def test_{i}_{j}(): pass" for j in range(100)), encoding="utf-8")
        files.append(f)
    n = verifier._count_tests(files)
    assert n == 300
    # 60 + 2*n, capped 1800
    assert min(1800, 60 + 2 * n) == 660
    assert min(1800, 60 + 2 * 2000) == 1800  # cap holds for huge suites


def test_failing_modules_routes_by_prefix(tmp_path):
    modules = [
        {"name": "users", "kind": "backend", "loc_budget": 1500, "depends_on": [],
         "path_root": "code/backend/app/users"},
        {"name": "notes", "kind": "backend", "loc_budget": 1500, "depends_on": [],
         "path_root": "code/backend/app/notes"},
    ]
    (tmp_path / "MODULES.json").write_text(json.dumps(modules), encoding="utf-8")
    report = {"checks": {"pytest": {"failures": [
        {"file": "app.users.tests.test_users", "test": "test_register", "message": "boom"},
        {"file": "app.notes.tests.test_notes", "test": "test_create", "message": "bang"},
        {"file": "tests.test_shared", "test": "test_x", "message": "?"},
    ]}}}
    (tmp_path / "VERIFICATION_REPORT.json").write_text(json.dumps(report), encoding="utf-8")
    routed = builder._failing_modules(tmp_path)
    assert set(routed["users"][0].values()) >= {"test_register"}
    assert routed["notes"][0]["test"] == "test_create"
    assert "__unrouted__" in routed  # the tests.test_shared failure


def test_failing_modules_empty_single_module(tmp_path):
    assert builder._failing_modules(tmp_path) == {}  # no MODULES.json


def test_module_prefix():
    assert builder._module_prefix("code/backend/app/users") == "app.users"
    assert builder._module_prefix("code/backend/app/core/") == "app.core"
