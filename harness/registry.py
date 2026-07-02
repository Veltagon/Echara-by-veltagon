"""Tool registry — one place that maps a tool name to its OpenAI function
schema (sent to the model in `tools=`) and its Python implementation (called on
a tool_call). Ported from opencode's tool/registry.ts, flattened to a dict.
"""
from __future__ import annotations

from harness import tools
from harness.tools import Context

_STR = {"type": "string"}
_INT = {"type": "integer"}


def _spec(name, desc, props, required):
    return {
        "fn": getattr(tools, name),
        "schema": {
            "type": "function",
            "function": {
                "name": name,
                "description": desc,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                },
            },
        },
    }


REGISTRY: dict[str, dict] = {
    e["schema"]["function"]["name"]: e
    for e in [
        _spec("read_file", "Read a text file. Optional 1-based line window via offset/limit.",
              {"path": _STR, "offset": _INT, "limit": _INT}, ["path"]),
        _spec("write_file", "Create or overwrite a file (parent dirs auto-created).",
              {"path": _STR, "content": _STR}, ["path", "content"]),
        _spec("edit_file", "Replace an exact substring `old` (must occur exactly once) with `new`.",
              {"path": _STR, "old": _STR, "new": _STR}, ["path", "old", "new"]),
        _spec("list_dir", "List one directory level. Defaults to the workspace root.",
              {"path": _STR}, []),
        _spec("glob", "Recursive filename match, e.g. pattern='**/*.py'.",
              {"pattern": _STR, "path": _STR}, ["pattern"]),
        _spec("grep", "Regex content search across files. Optional `glob` filters filenames.",
              {"pattern": _STR, "path": _STR, "glob": _STR}, ["pattern"]),
        _spec("bash_run", "Run a shell command in the workspace (real bash if on PATH, else the platform shell). Returns exit code + output. See <environment> for which shell is active.",
              {"command": _STR, "timeout": _INT}, ["command"]),
        _spec("powershell_run", "Run a command in Windows PowerShell (registry, COM, native modules, Windows CLIs, .ps1 scripts).",
              {"command": _STR, "timeout": _INT}, ["command"]),
        _spec("web_search", "Search the web (DuckDuckGo). Returns top results as title/url/snippet.",
              {"query": _STR, "max_results": _INT}, ["query"]),
        _spec("webfetch", "Fetch a URL and return its text content (HTML stripped to readable text).",
              {"url": _STR}, ["url"]),
        _spec("load_skill", "Load the full SKILL.md body for a skill named in the index.",
              {"name": _STR}, ["name"]),
        _spec("done", "Call when the task is fully complete. `summary` is the final report.",
              {"summary": _STR}, []),
    ]
}


def tool_schemas() -> list[dict]:
    """The `tools=` array for chat.completions.create."""
    return [e["schema"] for e in REGISTRY.values()]


def dispatch(name: str, args: dict, ctx: Context) -> dict:
    """Execute a tool by name. Unknown names return an error result (fed back
    to the model) rather than raising — a hallucinated tool shouldn't crash the
    loop."""
    entry = REGISTRY.get(name)
    if entry is None:
        return {"output": f"ERROR: unknown tool {name!r}", "metadata": {"error": True}}
    return entry["fn"](args, ctx)
