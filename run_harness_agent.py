"""Drive a raw API model through ECHARA's tool loop.

Sibling to run_agent.py (which dispatches CLI agents). This one targets
OpenAI-compatible API providers that have no tool loop of their own — the
harness gives them filesystem + shell + skill access.

`run_harness()` is the reusable entry point (any caller passes a provider object
+ task and gets a report dict back). The M1 orchestrator's BUILD phase can call
it directly once M3 defines provider routing — that's the seam, not built here.

Usage:
    python run_harness_agent.py --provider cerebras_gemma
    python run_harness_agent.py --provider cerebras_gemma --task "build X" --skills-dir skills
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from harness import prompt as prompt_mod
from harness import skills as skills_mod
from harness.loop import run_agent
from harness.tools import Context
from providers import HARNESS_PROVIDERS

ECHARA_ROOT = Path(__file__).resolve().parent

# Canned cheap E2E (from M2.5 plan): exercises write_file + bash_run + done.
CANNED_TASK = (
    "Create `hello.py` in your workspace that prints exactly `hello echara`. "
    "Then run it with bash_run and confirm the stdout contains that phrase. "
    "Finally call done with a one-line summary."
)


def run_harness(
    provider,
    task: str,
    workspace: Path,
    skills_dir: Path | None = None,
    persona: str = "",
    max_rounds: int = 25,
    full_access: bool = False,
    log=lambda s: None,
) -> dict:
    """Assemble and run one harness agent end to end: stage skills into the
    workspace, build the system prompt, run the tool loop, persist forensics.
    Returns the report dict (includes stop_reason). `provider` is any object
    with a `complete(messages, tools)` method — a real OpenAICompatProvider or a
    fake in tests. This is the single seam a caller (CLI or orchestrator) uses."""
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    # Stage skills INTO the workspace so the model can read_file references and
    # bash_run scripts — the full progressive-disclosure mechanism.
    skills_root = None
    if skills_dir:
        skills_root = skills_mod.stage(Path(skills_dir).resolve(), workspace.resolve())

    system_prompt = prompt_mod.build_system_prompt(
        workspace.resolve(), skills_root, persona, full_access=full_access
    )
    # Forensics: what did the agent actually see? (mirrors M2's PROMPT.md)
    (workspace / "SYSTEM_PROMPT.md").write_text(system_prompt, encoding="utf-8")

    ctx = Context(
        workspace_root=workspace.resolve(),
        skills_root=skills_root,
        allow_outside_workspace=full_access,
    )

    started = time.monotonic()
    result = run_agent(provider, system_prompt, task, ctx, max_rounds=max_rounds, log=log)
    elapsed = round(time.monotonic() - started, 2)

    report = {
        "provider": getattr(provider, "name", type(provider).__name__),
        "model": getattr(provider, "model", None),
        "workspace": str(workspace),
        "stop_reason": result.stop_reason,
        "rounds": result.rounds,
        "tool_calls": result.tool_calls,
        "elapsed_sec": elapsed,
        "usage": result.usage,
        "final_text": result.final_text,
    }
    (workspace / "HARNESS_REPORT.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (workspace / "TRANSCRIPT.json").write_text(
        json.dumps(result.transcript, indent=2, default=str), encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=sorted(HARNESS_PROVIDERS), required=True)
    parser.add_argument("--task", default=CANNED_TASK)
    parser.add_argument("--workspace", default=None,
                        help="Workspace dir (default: builds/harness_<provider>_<ts>).")
    parser.add_argument("--skills-dir", default=None,
                        help="Directory of <skill>/SKILL.md folders to index.")
    parser.add_argument("--persona", default=None, help="Path to a persona .md file.")
    parser.add_argument("--max-rounds", type=int, default=25)
    parser.add_argument("--full-access", action="store_true",
                        help="Drop the workspace clamp on file tools (M2-equivalent posture). "
                             "Shell tools are unclamped either way.")
    args = parser.parse_args()

    ts = time.strftime("%Y%m%d_%H%M%S")
    workspace = Path(args.workspace or f"builds/harness_{args.provider}_{ts}")
    persona = Path(args.persona).read_text(encoding="utf-8") if args.persona else ""

    print(f"[ECHARA] harness: {args.provider} -> {workspace}")
    report = run_harness(
        HARNESS_PROVIDERS[args.provider],
        args.task,
        workspace,
        skills_dir=Path(args.skills_dir) if args.skills_dir else None,
        persona=persona,
        max_rounds=args.max_rounds,
        full_access=args.full_access,
        log=lambda s: print("  " + s),
    )
    print(json.dumps(report, indent=2))
    # 0 = model reported done/stopped; 1 = error/round-cap without finishing.
    return 0 if report["stop_reason"] in ("done", "stop") else 1


if __name__ == "__main__":
    sys.exit(main())
