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

from harness import registry
from harness.tools import Context

log = logging.getLogger("echara.tool_harness")

# The exact 5-tool set the spec mandates (a subset of the harness's 12).
TOOL_NAMES = ["read_file", "write_file", "list_dir", "bash_run", "done"]
TOOL_SCHEMAS = [registry.REGISTRY[n]["schema"] for n in TOOL_NAMES]


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


def run_tool_loop(complete, messages: list[dict], ctx: Context, max_iterations: int = 30) -> dict:
    """Drive an API model through the tool loop.

    `complete(messages, tools)` returns an OpenAI/LiteLLM-shaped response
    (resp.choices[0].message with .content and .tool_calls). Loops until the
    model calls `done`, stops emitting tool calls, or hits max_iterations.
    Returns {final_text, iterations, stop_reason, messages}.
    """
    messages = list(messages)
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
                result = execute_tool(name, args, ctx)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            if name == "done":
                return {"final_text": result, "iterations": i, "stop_reason": "done", "messages": messages}

    log.warning("tool loop hit max_iterations=%d without calling done", max_iterations)
    return {"final_text": "", "iterations": max_iterations, "stop_reason": "max_iterations", "messages": messages}
