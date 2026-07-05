"""Minimal §5 lesson ledger — append-only LESSONS.jsonl + prompt injection.
No model calls."""
from __future__ import annotations

import json

from agents import lessons
from agents import builder


def test_record_appends_and_dedups(tmp_path):
    lessons.record(tmp_path, "orders", "cannot import reserve_stock", "export it", tags=["seam"])
    lessons.record(tmp_path, "orders", "cannot import reserve_stock", "export it", tags=["seam"])  # dup
    lessons.record(tmp_path, "orders", "different symptom", "different fix", tags=["import"])
    recs = [json.loads(l) for l in (tmp_path / "LESSONS.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(recs) == 2                       # duplicate collapsed
    assert recs[0]["id"] == "L-0001" and recs[1]["id"] == "L-0002"


def test_for_prompt_selects_by_module_and_tag(tmp_path):
    lessons.record(tmp_path, "inventory", "seam miss X", "export X", tags=["seam"])
    lessons.record(tmp_path, "customers", "unrelated local bug", "fix locally", tags=[])
    # module match:
    block = lessons.for_prompt(tmp_path, "inventory")
    assert "LESSONS (guardrails" in block and "export X" in block
    # tag match from another module (shared 'seam' tag), even if module differs:
    block2 = lessons.for_prompt(tmp_path, "orders", extra_tags=["seam"])
    assert "export X" in block2
    # no relevance -> empty
    assert lessons.for_prompt(tmp_path, "billing") == ""


def test_for_prompt_empty_without_file(tmp_path):
    assert lessons.for_prompt(tmp_path, "any") == ""


def test_char_cap_bounds_injection(tmp_path):
    for i in range(40):
        lessons.record(tmp_path, "core", f"symptom number {i} " + "x" * 80,
                       "some fix " + "y" * 80, tags=["seam"])
    block = lessons.for_prompt(tmp_path, "core")
    assert len(block) <= lessons.MAX_CHARS + 200          # cap respected (+header)
    assert block.count("\n- ") <= lessons.MAX_INJECT      # at most MAX_INJECT lines


def test_module_context_injects_lessons(tmp_path):
    lessons.record(tmp_path, "orders", "declared seam missing: create_order",
                   "export create_order", tags=["seam"])
    ctx = builder._module_context(
        tmp_path, {"name": "orders", "depends_on": [], "kind": "backend"},
        seams={}, conventions="Be terse.", module_plan="build orders", skill_rel=None)
    assert "LESSONS (guardrails" in ctx and "export create_order" in ctx


def test_tags_from_extracts_framework_tags():
    t = lessons.tags_from("sqlalchemy.exc: DetachedInstanceError in pytest", ["breach"])
    assert "sqlalchemy" in t and "pytest" in t and "breach" in t
