"""Unit tests for the M2.5 harness — no API spend.

Run: python tests_m25_harness.py
Exits 0 only if every check passes. Mirrors tests_hardening.py's plain-stdlib
style. The loop is exercised end-to-end with a SCRIPTED fake provider, so the
real tools (write_file, bash_run) actually run against a temp workspace.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from harness import safety, skills, tools, registry
from harness.loop import run_agent
from harness.tools import Context

_results: list[tuple[str, bool, str]] = []


def _record(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    print(f"  [{('PASS' if ok else 'FAIL')}] {name}{(' — ' + detail) if detail else ''}")


# --- module self-checks (each demo() asserts internally) --------------------

def test_module_demos() -> None:
    print("\n>>> module self-checks")
    for mod in (safety, tools, skills):
        try:
            mod.demo()
            _record(f"{mod.__name__}.demo", True)
        except Exception as e:  # noqa: BLE001
            _record(f"{mod.__name__}.demo", False, repr(e))


# --- path clamp -------------------------------------------------------------

def test_clamp_path() -> None:
    print("\n>>> path clamp")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _record("inside path accepted", safety.clamp_path(root, "sub/f.txt").is_relative_to(root.resolve()))
        for bad in ["../out.txt", "../../x", "/etc/passwd", "C:\\Windows\\x"]:
            try:
                safety.clamp_path(root, bad)
                _record(f"escape blocked: {bad}", False, "no exception")
            except safety.PathEscape:
                _record(f"escape blocked: {bad}", True)


def test_full_access_toggle() -> None:
    print("\n>>> full-access toggle")
    with tempfile.TemporaryDirectory() as ws_d, tempfile.TemporaryDirectory() as out_d:
        ws = Context(workspace_root=Path(ws_d))
        target = Path(out_d) / "outside.txt"  # absolute, outside the workspace
        # Default: clamp blocks the write entirely.
        blocked = tools.write_file({"path": str(target), "content": "x"}, ws)
        _record("clamp blocks write outside workspace",
                blocked["metadata"].get("error") is True and not target.exists())
        # Full access: same write lands on disk outside the workspace.
        full = Context(workspace_root=Path(ws_d), allow_outside_workspace=True)
        ok = tools.write_file({"path": str(target), "content": "escaped"}, full)
        _record("full-access write reaches outside workspace",
                not ok["metadata"].get("error") and target.read_text() == "escaped")


# --- registry shape ---------------------------------------------------------

def test_read_file_edges() -> None:
    print("\n>>> read_file edge cases")
    with tempfile.TemporaryDirectory() as d:
        ctx = Context(workspace_root=Path(d))
        tools.write_file({"path": "f.txt", "content": "l1\nl2\nl3\nl4\nl5\n"}, ctx)
        _record("non-int offset -> error (no crash)",
                tools.read_file({"path": "f.txt", "offset": "abc"}, ctx)["metadata"].get("error"))
        _record("offset=0 -> error", tools.read_file({"path": "f.txt", "offset": 0}, ctx)["metadata"].get("error"))
        _record("negative limit -> error", tools.read_file({"path": "f.txt", "limit": -1}, ctx)["metadata"].get("error"))
        r = tools.read_file({"path": "f.txt", "offset": 2, "limit": 2}, ctx)
        _record("valid window returns lines 2-3", r["output"] == "l2\nl3", repr(r["output"]))


def test_write_file_edges() -> None:
    print("\n>>> write_file edge cases")
    with tempfile.TemporaryDirectory() as d:
        ctx = Context(workspace_root=Path(d))
        r = tools.write_file({"path": "n.txt"}, ctx)  # content omitted / null
        _record("missing content -> no crash, empty file",
                not r["metadata"].get("error") and (Path(d) / "n.txt").read_text() == "")
        r2 = tools.write_file({"path": "u.txt", "content": "café ☕"}, ctx)
        _record("bytes metadata is real UTF-8 length (9)", r2["metadata"]["bytes"] == 9,
                f"got {r2['metadata']['bytes']}")


def test_glob_cap() -> None:
    print("\n>>> glob output cap")
    with tempfile.TemporaryDirectory() as d:
        ctx = Context(workspace_root=Path(d))
        for i in range(300):
            (Path(d) / f"f{i}.txt").write_text("x", encoding="utf-8")
        r = tools.glob({"pattern": "*.txt"}, ctx)
        _record("glob capped at 200 shown", r["metadata"]["shown"] == 200 and r["metadata"]["count"] == 300,
                f"shown={r['metadata']['shown']} count={r['metadata']['count']}")
        _record("truncation noted in output", "truncated at 200 of 300" in r["output"])


def test_loop_api_error() -> None:
    print("\n>>> loop: API error ends cleanly")
    class _Boom:
        def complete(self, messages, tools_):
            raise RuntimeError("429 rate limit")
    with tempfile.TemporaryDirectory() as d:
        res = run_agent(_Boom(), "s", "t", Context(workspace_root=Path(d)), max_rounds=5)
        _record("API error -> stop_reason 'error' (not raised)", res.stop_reason == "error", res.stop_reason)
        _record("error surfaced in final_text", "429 rate limit" in res.final_text, res.final_text)


def test_registry() -> None:
    print("\n>>> registry")
    schemas = registry.tool_schemas()
    names = {s["function"]["name"] for s in schemas}
    _record("12 tools registered", len(schemas) == 12, str(sorted(names)))
    _record("core + new tools present",
            {"read_file", "write_file", "bash_run", "powershell_run",
             "web_search", "webfetch", "done"} <= names)
    bad = registry.dispatch("nope", {}, Context(Path(".")))
    _record("unknown tool -> error result", bad["metadata"].get("error") is True)


# --- the loop, driven by a scripted fake provider ---------------------------

def _tc(cid: str, name: str, args: dict):
    return SimpleNamespace(
        id=cid, type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


def _resp(content=None, tool_calls=None, reasoning=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls, reasoning=reasoning)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


class _Scripted:
    """Returns pre-baked responses in order; records the tool list it saw."""
    def __init__(self, turns):
        self.turns = list(turns)
        self.seen_tools = None

    def complete(self, messages, tools_):
        self.seen_tools = tools_
        return self.turns.pop(0)


def test_loop_done() -> None:
    print("\n>>> loop: write -> run -> done")
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        ctx = Context(workspace_root=ws)
        turns = [
            _resp(tool_calls=[_tc("c1", "write_file",
                  {"path": "hello.py", "content": "print('hello echara')\n"})]),
            _resp(tool_calls=[_tc("c2", "bash_run",
                  {"command": f'"{sys.executable}" hello.py'})]),
            _resp(tool_calls=[_tc("c3", "done", {"summary": "made hello echara"})]),
        ]
        prov = _Scripted(turns)
        res = run_agent(prov, "sys", "task", ctx, max_rounds=10)

        _record("stop_reason == done", res.stop_reason == "done", res.stop_reason)
        _record("3 rounds, 3 tool calls", res.rounds == 3 and res.tool_calls == 3,
                f"rounds={res.rounds} calls={res.tool_calls}")
        _record("hello.py written", (ws / "hello.py").exists())
        _record("final_text from done summary", res.final_text == "made hello echara", res.final_text)
        tool_msgs = [m for m in res.transcript if m.get("role") == "tool"]
        _record("bash output fed back to model",
                any("hello echara" in m["content"] for m in tool_msgs))
        _record("provider received tool schemas", prov.seen_tools == registry.tool_schemas())


def test_loop_stop_and_cap() -> None:
    print("\n>>> loop: plain stop + round cap")
    with tempfile.TemporaryDirectory() as d:
        ctx = Context(workspace_root=Path(d))
        # No tool calls -> immediate stop, final text comes from `reasoning`.
        prov = _Scripted([_resp(reasoning="all done here")])
        res = run_agent(prov, "s", "t", ctx, max_rounds=5)
        _record("no tool_calls -> stop", res.stop_reason == "stop" and res.rounds == 1)
        _record("reasoning used as final text", res.final_text == "all done here", res.final_text)

        # Always returns a tool call -> must hit the cap, never loop forever.
        loop_turns = [_resp(tool_calls=[_tc(f"x{i}", "list_dir", {})]) for i in range(20)]
        res2 = run_agent(_Scripted(loop_turns), "s", "t", ctx, max_rounds=4)
        _record("round cap enforced", res2.stop_reason == "max_rounds" and res2.rounds == 4,
                f"{res2.stop_reason}/{res2.rounds}")


def test_bash_shell() -> None:
    print("\n>>> bash_run shell honesty")
    with tempfile.TemporaryDirectory() as d:
        ctx = Context(workspace_root=Path(d))
        shell = tools.active_bash_shell()
        _record(f"active shell reported: {shell}", shell in ("bash", "cmd.exe", "/bin/sh"))
        _record("bash_run basic echo works", "hi" in tools.bash_run({"command": "echo hi"}, ctx)["output"])
        if shell == "bash":
            # POSIX-only arithmetic expansion; cmd.exe would echo it literally.
            r = tools.bash_run({"command": "echo $((20 + 22))"}, ctx)
            _record("bash_run runs real POSIX ($((...)) -> 42)", "42" in r["output"], r["output"].strip())


def test_integration_full_assembly() -> None:
    """The 'put together' proof: run the WHOLE harness via run_harness() with a
    scripted provider — staging, prompt build, loop, tools, report — and assert
    the components are actually wired (a staged skill reference is readable
    THROUGH the loop, not just by a direct tool call)."""
    print("\n>>> integration: run_harness full assembly")
    from run_harness_agent import run_harness

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        src = d / "skillsrc" / "demo-skill"
        (src / "references").mkdir(parents=True)
        (src / "SKILL.md").write_text(
            '---\nname: "demo-skill"\ndescription: demo skill\n---\n# body\n', encoding="utf-8")
        (src / "references" / "note.md").write_text("SECRET_MARKER_42", encoding="utf-8")
        ws = d / "ws"

        turns = [
            _resp(tool_calls=[_tc("c1", "read_file", {"path": "skills/demo-skill/references/note.md"})]),
            _resp(tool_calls=[_tc("c2", "write_file", {"path": "out.txt", "content": "built"})]),
            _resp(tool_calls=[_tc("c3", "bash_run", {"command": "echo integrated"})]),
            _resp(tool_calls=[_tc("c4", "done", {"summary": "assembled"})]),
        ]
        report = run_harness(_Scripted(turns), "do it", ws, skills_dir=d / "skillsrc", max_rounds=10)

        _record("skill staged into workspace", (ws / "skills" / "demo-skill" / "SKILL.md").is_file())
        sp = (ws / "SYSTEM_PROMPT.md").read_text(encoding="utf-8")
        _record("system prompt has skill index path", "skills/demo-skill/SKILL.md" in sp, )
        _record("system prompt names the active shell", "bash_run runs" in sp)
        _record("report + transcript persisted",
                (ws / "HARNESS_REPORT.json").is_file() and (ws / "TRANSCRIPT.json").is_file())
        _record("model's file write landed", (ws / "out.txt").read_text() == "built")
        _record("run_harness returns stop_reason done", report["stop_reason"] == "done", report["stop_reason"])

        transcript = json.loads((ws / "TRANSCRIPT.json").read_text(encoding="utf-8"))
        tool_msgs = [m for m in transcript if m.get("role") == "tool"]
        _record("STAGED reference readable through the loop (cross-component)",
                any("SECRET_MARKER_42" in m["content"] for m in tool_msgs))
        _record("bash ran through the assembly",
                any("integrated" in m["content"] for m in tool_msgs))


def main() -> int:
    print("ECHARA M2.5 harness — unit tests")
    test_module_demos()
    test_clamp_path()
    test_full_access_toggle()
    test_read_file_edges()
    test_write_file_edges()
    test_glob_cap()
    test_loop_api_error()
    test_registry()
    test_loop_done()
    test_loop_stop_and_cap()
    test_bash_shell()
    test_integration_full_assembly()
    failed = [n for n, ok, _ in _results if not ok]
    print(f"\n  {len(_results) - len(failed)} passed, {len(failed)} failed")
    for n in failed:
        print(f"    - FAIL: {n}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
