"""The agent loop — WE own it (the provider is just a raw chat endpoint).

Send the model the tool schemas, execute whatever tool_calls come back against
the workspace, feed each result in as a `tool` message, repeat. Stop when the
model emits no tool_calls (it's done talking) or calls the `done` tool, or when
we hit the round cap. Ported from opencode's session/prompt.ts runLoop +
processor.ts dispatch, collapsed to a non-streaming chat.completions loop.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from harness import registry
from harness.tools import Context


@dataclass
class LoopResult:
    final_text: str
    rounds: int
    tool_calls: int
    stop_reason: str  # "done" | "stop" | "max_rounds" | "error"
    transcript: list[dict] = field(default_factory=list)


def _assistant_dict(msg) -> dict:
    """Rebuild the assistant turn as a plain dict the API will accept next
    round. Avoids msg.model_dump(), which can carry provider-specific fields
    (refusal, reasoning, function_call) that some endpoints reject."""
    out: dict = {"role": "assistant", "content": msg.content or ""}
    if getattr(msg, "tool_calls", None):
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in msg.tool_calls
        ]
    return out


def run_agent(
    provider,
    system_prompt: str,
    task: str,
    ctx: Context,
    max_rounds: int = 25,
    log=lambda s: None,
) -> LoopResult:
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]
    total_calls = 0
    final_text = ""

    for rnd in range(1, max_rounds + 1):
        try:
            resp = provider.complete(messages, registry.tool_schemas())
        except Exception as e:  # noqa: BLE001 — the SDK already retried transient
            # errors; a raise here is terminal. End the run cleanly instead of
            # crashing the process, so the caller still gets a report.
            log(f"round {rnd}: API error, aborting: {e}")
            return LoopResult(f"API error: {e}", rnd, total_calls, "error", messages)
        msg = resp.choices[0].message
        # Some models route assistant text into `reasoning` with content=None.
        text = (getattr(msg, "reasoning", None) or msg.content or "").strip()
        messages.append(_assistant_dict(msg))

        calls = getattr(msg, "tool_calls", None) or []
        if not calls:
            log(f"round {rnd}: stop (no tool calls)")
            return LoopResult(text, rnd, total_calls, "stop", messages)

        finished_summary = None
        for tc in calls:
            total_calls += 1
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError as e:
                result = {"output": f"ERROR: bad JSON arguments: {e}", "metadata": {}}
            else:
                result = registry.dispatch(name, args, ctx)
                if result["metadata"].get("done"):
                    finished_summary = result["output"]
            log(f"round {rnd}: {name}({_brief(tc.function.arguments)}) -> {_brief(result['output'])}")
            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": result["output"]}
            )

        if finished_summary is not None:
            return LoopResult(finished_summary, rnd, total_calls, "done", messages)

    return LoopResult(final_text, max_rounds, total_calls, "max_rounds", messages)


def _brief(s: str | None, n: int = 80) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"
