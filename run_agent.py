"""CLI: dispatch the BuilderAgent through a chosen provider.

Usage:
    python run_agent.py --provider claude
    python run_agent.py --provider codex --timeout 1500
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from agent import BuilderAgent
from providers import PROVIDERS
from providers import availability


# The frozen contract for the M2 smoke task. Notes CRUD over /api/notes.
NOTES_CRUD_CONTRACT = {
    "title": "Notes CRUD",
    "description": "Minimal FastAPI service for storing short text notes in SQLite.",
    "api_endpoints": [
        {"method": "POST",   "path": "/api/notes",      "request_schema": "NoteCreate", "response_schema": "NoteOut",       "status": 201},
        {"method": "GET",    "path": "/api/notes",      "response_schema": "list[NoteOut]"},
        {"method": "GET",    "path": "/api/notes/{id}", "response_schema": "NoteOut"},
        {"method": "DELETE", "path": "/api/notes/{id}", "status": 204},
    ],
    "shared_types": [
        {"name": "NoteCreate", "fields": {"title": "str (1..200)", "body": "str (<=10000)"}, "required": ["title", "body"]},
        {"name": "NoteOut",    "fields": {"id": "int", "title": "str", "body": "str", "created_at": "datetime"}, "required": ["id", "title", "body", "created_at"]},
    ],
    "storage": "SQLite via SQLAlchemy",
    "dependencies": ["fastapi", "uvicorn", "sqlalchemy", "pydantic"],
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=sorted(PROVIDERS), required=True)
    parser.add_argument("--timeout", type=int, default=1200,
                        help="Per-run wall-clock timeout in seconds (default 1200).")
    parser.add_argument("--build-dir", type=str, default=None,
                        help="Override build dir (default: builds/agent_<provider>_<ts>).")
    args = parser.parse_args()

    ts = time.strftime("%Y%m%d_%H%M%S")
    build_dir = Path(args.build_dir or f"builds/agent_{args.provider}_{ts}")

    provider = PROVIDERS[args.provider]()
    avail = availability.status(args.provider)
    if not avail.available:
        print(
            f"[ECHARA] {args.provider} marked exhausted; "
            f"resets in {int(avail.seconds_until_reset)}s — refusing to dispatch."
        )
        return 2

    agent = BuilderAgent(provider, NOTES_CRUD_CONTRACT, build_dir)

    print(f"[ECHARA] dispatching {args.provider} -> {build_dir}")
    result = agent.run(timeout_sec=args.timeout)

    report = {
        "provider": result.run.provider,
        "status": result.status,
        "exit_code": result.run.exit_code,
        "timed_out": result.run.timed_out,
        "kill_reason": result.run.kill_reason,
        "skipped_reason": result.run.skipped_reason,
        "rate_limit_retry_after_sec": result.run.rate_limit_retry_after_sec,
        "elapsed_sec": result.run.elapsed_sec,
        "build_dir": str(result.build_dir),
        "files_created": result.files_created,
        "backend_files": result.backend_files,
        "main_imports_fastapi": result.main_imports_fastapi,
        "self_verify_present": result.self_verify_present,
        "endpoints_found": result.endpoints_found,
        "endpoints_expected": [
            f"{ep['method']} {ep['path']}"
            for ep in NOTES_CRUD_CONTRACT["api_endpoints"]
        ],
        "stdout_log": str(result.run.stdout_path),
        "stderr_log": str(result.run.stderr_path),
        "prompt_log": str(result.run.prompt_path) if result.run.prompt_path else None,
    }
    report_path = build_dir / "M2_REPORT.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))

    print(f"[ECHARA] {args.provider}: {result.status.upper()}")
    # Exit codes: 0 pass, 1 failed, 2 unavailable, 3 incomplete (needs continuation).
    return {"pass": 0, "failed": 1, "unavailable": 2, "incomplete": 3}[result.status]


if __name__ == "__main__":
    sys.exit(main())
