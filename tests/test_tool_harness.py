"""Tool harness tests — real local tools, mocked model."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.tools import Context
from providers.tool_harness import execute_tool, run_tool_loop, TOOL_NAMES


@pytest.fixture
def ctx(tmp_path):
    return Context(workspace_root=tmp_path)


# --- individual tools -------------------------------------------------------

def test_read_file(ctx, tmp_path):
    (tmp_path / "hello.txt").write_text("known content here", encoding="utf-8")
    out = execute_tool("read_file", {"path": "hello.txt"}, ctx)
    assert "known content here" in out


def test_write_file(ctx, tmp_path):
    execute_tool("write_file", {"path": "sub/out.txt", "content": "written!"}, ctx)
    assert (tmp_path / "sub" / "out.txt").read_text() == "written!"


def test_list_dir(ctx, tmp_path):
    for n in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / n).write_text("x", encoding="utf-8")
    out = execute_tool("list_dir", {"path": "."}, ctx)
    assert "a.txt" in out and "b.txt" in out and "c.txt" in out


def test_bash_run(ctx):
    out = execute_tool("bash_run", {"command": "echo hello"}, ctx)
    assert "hello" in out


def test_bash_run_timeout(ctx):
    out = execute_tool("bash_run", {"command": "sleep 30", "timeout": 2}, ctx)
    assert "timed out" in out.lower()


# --- the loop ---------------------------------------------------------------

def _tc(cid, name, args):
    return SimpleNamespace(id=cid, type="function",
                           function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def _resp(content=None, tool_calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content, tool_calls=tool_calls))])


class ScriptedModel:
    def __init__(self, turns):
        self.turns = list(turns)
        self.seen = []

    def __call__(self, messages, tools):
        self.seen.append(tools)
        return self.turns.pop(0)


def test_done_stops_loop(ctx):
    model = ScriptedModel([_resp(tool_calls=[_tc("c1", "done", {"summary": "finished"})])])
    result = run_tool_loop(model, [{"role": "user", "content": "go"}], ctx, max_iterations=30)
    assert result["stop_reason"] == "done"
    assert result["iterations"] == 1
    assert result["final_text"] == "finished"


def test_max_iterations_guard(ctx, caplog):
    # Model never calls done — always asks to list_dir.
    turns = [_resp(tool_calls=[_tc(f"c{i}", "list_dir", {})]) for i in range(10)]
    with caplog.at_level("WARNING"):
        result = run_tool_loop(ScriptedModel(turns), [{"role": "user", "content": "go"}], ctx, max_iterations=3)
    assert result["stop_reason"] == "max_iterations"
    assert result["iterations"] == 3
    assert any("max_iterations" in r.message for r in caplog.records)


def test_tool_loop_produces_output(ctx, tmp_path):
    import sys
    py = sys.executable.replace("\\", "/")
    model = ScriptedModel([
        _resp(tool_calls=[_tc("c1", "write_file", {"path": "test.py", "content": "print('hello')"})]),
        _resp(tool_calls=[_tc("c2", "bash_run", {"command": f'"{py}" test.py'})]),
        _resp(tool_calls=[_tc("c3", "done", {"summary": "ran it"})]),
    ])
    result = run_tool_loop(model, [{"role": "user", "content": "build"}], ctx, max_iterations=30)
    assert (tmp_path / "test.py").exists()
    tool_msgs = [m for m in result["messages"] if m.get("role") == "tool"]
    assert any("hello" in m["content"] for m in tool_msgs)  # bash output fed back
    assert result["iterations"] == 3
    assert result["stop_reason"] == "done"


def test_five_tools_registered():
    assert TOOL_NAMES == ["read_file", "write_file", "list_dir", "bash_run", "done"]


# --- guide.md M3: references/ gate for small-context models -----------------

from providers.tool_harness import (
    _touches_references, REFERENCES_REFUSAL, SMALL_CONTEXT_FLOOR,
)


def test_touches_references_helper():
    assert _touches_references("skills/senior-backend/references/api.md")
    assert _touches_references("skills\\senior-backend\\references\\api.md")  # Windows
    assert _touches_references("references/foo.md")
    assert not _touches_references("skills/senior-backend/SKILL.md")
    assert not _touches_references("references.md")  # file named `references`, not a segment


def test_references_gate_intercepts_small_context(ctx, tmp_path):
    # Set up a real references/ file so we can prove the gate returns the
    # refusal string INSTEAD of the file's content.
    refs = tmp_path / "skills" / "senior-backend" / "references"
    refs.mkdir(parents=True)
    (refs / "api.md").write_text("SECRET_ACTUAL_CONTENT", encoding="utf-8")

    model = ScriptedModel([
        _resp(tool_calls=[_tc("c1", "read_file",
              {"path": "skills/senior-backend/references/api.md"})]),
        _resp(tool_calls=[_tc("c2", "done", {"summary": "attempted"})]),
    ])
    result = run_tool_loop(
        model, [{"role": "user", "content": "read the reference"}], ctx,
        max_iterations=10, context_window=SMALL_CONTEXT_FLOOR - 1,
    )
    tool_msgs = [m for m in result["messages"] if m.get("role") == "tool"]
    # The read_file tool message carries the refusal, not the file content.
    read_result = tool_msgs[0]["content"]
    assert read_result == REFERENCES_REFUSAL
    assert "SECRET_ACTUAL_CONTENT" not in read_result


def test_references_allowed_large_context(ctx, tmp_path):
    refs = tmp_path / "skills" / "senior-backend" / "references"
    refs.mkdir(parents=True)
    (refs / "api.md").write_text("full-body content", encoding="utf-8")

    model = ScriptedModel([
        _resp(tool_calls=[_tc("c1", "read_file",
              {"path": "skills/senior-backend/references/api.md"})]),
        _resp(tool_calls=[_tc("c2", "done", {"summary": "read it"})]),
    ])
    result = run_tool_loop(
        model, [{"role": "user", "content": "read"}], ctx,
        max_iterations=10, context_window=32000,  # over the floor
    )
    read_result = [m for m in result["messages"] if m.get("role") == "tool"][0]["content"]
    assert "full-body content" in read_result
    assert read_result != REFERENCES_REFUSAL


def test_references_gate_off_when_window_unknown(ctx, tmp_path):
    # context_window=None → no gate, model reads normally.
    (tmp_path / "references").mkdir()
    (tmp_path / "references" / "x.md").write_text("readable", encoding="utf-8")
    model = ScriptedModel([
        _resp(tool_calls=[_tc("c1", "read_file", {"path": "references/x.md"})]),
        _resp(tool_calls=[_tc("c2", "done", {"summary": "ok"})]),
    ])
    result = run_tool_loop(model, [{"role": "user", "content": "go"}], ctx,
                           max_iterations=10)  # no context_window
    read_result = [m for m in result["messages"] if m.get("role") == "tool"][0]["content"]
    assert "readable" in read_result
