"""Step 5 token instrumentation: capture per-invocation input/output/cached
tokens (API fleet + claude CLI), aggregate, and surface in the delivery report.
No model calls — usage objects / stream-json lines are faked."""
from __future__ import annotations

import json
from types import SimpleNamespace

from harness.loop import _acc_usage
from providers.claude_code import _parse_usage
from agents import progress, report


def test_acc_usage_object_dict_and_none():
    acc = {"input": 0, "output": 0, "cached": 0}
    resp = SimpleNamespace(usage=SimpleNamespace(
        prompt_tokens=100, completion_tokens=50,
        prompt_tokens_details=SimpleNamespace(cached_tokens=30)))
    _acc_usage(acc, resp)
    assert acc == {"input": 100, "output": 50, "cached": 30}
    _acc_usage(acc, SimpleNamespace(usage={"prompt_tokens": 10, "completion_tokens": 5}))  # dict shape
    assert acc["input"] == 110 and acc["output"] == 55 and acc["cached"] == 30
    _acc_usage(acc, SimpleNamespace(usage=None))  # provider reported no usage
    assert acc["input"] == 110


def test_claude_parse_usage_from_stream_json(tmp_path):
    p = tmp_path / "out.log"
    p.write_text(
        '{"type":"assistant","message":{"usage":{"input_tokens":5}}}\n'
        '{"type":"result","subtype":"success","usage":{"input_tokens":1200,'
        '"output_tokens":300,"cache_read_input_tokens":900,'
        '"cache_creation_input_tokens":100}}\n', encoding="utf-8")
    assert _parse_usage(p) == {"input": 1200, "output": 300, "cached": 900, "cache_creation": 100}
    p2 = tmp_path / "o2.log"
    p2.write_text('{"type":"assistant"}\n', encoding="utf-8")  # no result line
    assert _parse_usage(p2) == {}


def test_token_summary_aggregates(tmp_path):
    (tmp_path / "BUILD_METRICS.json").write_text(json.dumps([
        {"lane": "claude", "usage": {"input": 1000, "output": 200, "cached": 800, "cache_creation": 50}},
        {"lane": "claude", "usage": {"input": 500, "output": 100, "cached": 400}},
        {"lane": "overflow:cerebras_gptoss", "usage": {"input": 2000, "output": 300, "cached": 0}},
    ]), encoding="utf-8")
    s = progress.token_summary(tmp_path)
    assert s["total"]["input"] == 3500 and s["total"]["cached"] == 1200
    assert s["by_lane"]["claude"]["n"] == 2 and s["by_lane"]["claude"]["avg_input"] == 750.0
    # the caching question, answered per lane: the API fleet lane cached nothing.
    assert s["by_lane"]["overflow:cerebras_gptoss"]["cached"] == 0


def test_delivery_report_has_token_section(tmp_path):
    (tmp_path / "BUILD_METRICS.json").write_text(json.dumps([
        {"lane": "claude", "model": "sonnet", "elapsed_sec": 10,
         "usage": {"input": 1000, "output": 200, "cached": 800}}]), encoding="utf-8")
    md = report.delivery_report(tmp_path)
    assert "## Tokens" in md and "avg input" in md and "claude" in md
