"""Tool-calling loop for API models (Category B).

CLI tools have native file access; API models get equivalent power here: the 5
spec tools (read_file, write_file, list_dir, bash_run, done), an OpenAI-shaped
schema list, and a bounded loop. Tool *implementations* and their schemas are
reused from the M2.5 harness (harness.tools / harness.registry) — this module is
just the 5-tool wiring + loop, not a reimplementation.
"""
from __future__ import annotations

import json
import logging
from pathlib import PurePosixPath

from harness import registry
from harness.tools import Context

log = logging.getLogger("echara.tool_harness")

# The exact 5-tool set the spec mandates (a subset of the harness's 12).
TOOL_NAMES = ["read_file", "write_file", "list_dir", "bash_run", "done"]
TOOL_SCHEMAS = [registry.REGISTRY[n]["schema"] for n in TOOL_NAMES]

# guide.md M3: small-context models can't hold a full skill's references/, so
# for models with a context window below this floor we intercept read_file calls
# targeting a references/ segment and return the guide's exact refusal string.
SMALL_CONTEXT_FLOOR = 16_000
REFERENCES_REFUSAL = "reference not available, use core SKILL.md instructions only"


def _touches_references(path: str) -> bool:
    """True if the model-supplied path has a `references` segment (either
    slash flavor). Cheap, no filesystem I/O."""
    normalized = path.replace("\\", "/")
    return "references" in PurePosixPath(normalized).parts


def execute_tool(name: str, args: dict, ctx: Context) -> str:
    """Run one tool by name against the workspace; return its output text.
    Unknown tools return an error string (never raise) so a bad call can't
    crash the loop."""
    if name not in TOOL_NAMES:
        return f"ERROR: unknown tool {name!r}"
    return registry.dispatch(name, args, ctx)["output"]


def _assistant_turn(msg) -> dict:
    out: dict = {"role": "assistant", "content": getattr(msg, "content", None) or ""}
    if getattr(msg, "tool_calls", None):
        out["tool_calls"] = [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in msg.tool_calls
        ]
    return out


def run_tool_loop(
    complete, messages: list[dict], ctx: Context,
    max_iterations: int = 30, context_window: int | None = None,
) -> dict:
    """Drive an API model through the tool loop.

    `complete(messages, tools)` returns an OpenAI/LiteLLM-shaped response
    (resp.choices[0].message with .content and .tool_calls). Loops until the
    model calls `done`, stops emitting tool calls, or hits max_iterations.
    Returns {final_text, iterations, stop_reason, messages}.

    `context_window` (tokens) enables the guide's small-context gate: when
    known and < SMALL_CONTEXT_FLOOR, read_file calls into a skill's references/
    subdir short-circuit to REFERENCES_REFUSAL. Unknown window → no gate.
    """
    messages = list(messages)
    small_ctx = context_window is not None and context_window < SMALL_CONTEXT_FLOOR
    for i in range(1, max_iterations + 1):
        resp = complete(messages, TOOL_SCHEMAS)
        msg = resp.choices[0].message
        text = (getattr(msg, "content", None) or "").strip()
        messages.append(_assistant_turn(msg))

        calls = getattr(msg, "tool_calls", None) or []
        if not calls:
            return {"final_text": text, "iterations": i, "stop_reason": "stop", "messages": messages}

        for tc in calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError as e:
                result = f"ERROR: bad JSON arguments: {e}"
            else:
                if small_ctx and name == "read_file" and _touches_references(args.get("path", "")):
                    result = REFERENCES_REFUSAL  # gate — do NOT dispatch
                else:
                    result = execute_tool(name, args, ctx)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            if name == "done":
                return {"final_text": result, "iterations": i, "stop_reason": "done", "messages": messages}

    log.warning("tool loop hit max_iterations=%d without calling done", max_iterations)
    return {"final_text": "", "iterations": max_iterations, "stop_reason": "max_iterations", "messages": messages}
