"""Agent 1 — Planner. Turns the user prompt into PLAN.md + CONTRACT_REGISTRY.json.

Model policy: cheapest available. Sonnet is not reachable as a raw API (no
Anthropic key funded), so the primary is Cerebras gemma-4-31b — free, live, and
tool-call capable — driven by the M2.5 harness. Output is validated hard
(parseable JSON, complete endpoints); invalid output gets one retry with the
exact validation errors, then falls back to the claude CLI.
"""
from __future__ import annotations

import json
from pathlib import Path

from harness.loop import run_agent
from harness.tools import Context

ECHARA_ROOT = Path(__file__).resolve().parent.parent

_CONTRACT_KEYS = ["api_endpoints", "shared_types", "db_tables", "env_vars", "dependencies"]
_ENDPOINT_KEYS = ["method", "path", "request_schema", "response_schema"]


class PlanFailed(Exception):
    pass


_SYSTEM = """You are the ECHARA Planner. You produce implementation plans, nothing else.

Non-negotiable rules:
- No market analysis, no vision statements, no architecture astronautics.
- Every item in the file manifest is a concrete file path with a one-line purpose.
- Every endpoint in CONTRACT_REGISTRY.json specifies method, path, request_schema,
  response_schema (use null where a schema does not apply, e.g. GET request bodies).
- PLAN.md is plain markdown. CONTRACT_REGISTRY.json is valid JSON. Both must be
  parseable — a build system consumes them, not a human.
- The plan must be concrete enough that a builder executes it file-by-file
  without asking a single question."""


def _task(prompt: str, errors: list[str] | None = None) -> str:
    err_block = ""
    if errors:
        err_block = ("\n\nYOUR PREVIOUS OUTPUT WAS REJECTED. Fix exactly these "
                     "problems and rewrite BOTH files:\n- " + "\n- ".join(errors))
    return f"""USER REQUEST: {prompt}

Produce a plan for a Python FastAPI + SQLite (SQLAlchemy) backend. Backend only —
no frontend, no Docker, no CI. Create tables with Base.metadata.create_all in the
app lifespan; do NOT plan alembic migrations unless the request demands them.

Write exactly two files with the write_file tool, then call done:

1. `PLAN.md` with exactly these sections:
   ## File manifest — one line per file: `path` — purpose. All code lives under
   `code/backend/`. It MUST include: code/backend/app/main.py, code/backend/app/db.py,
   model + router files per resource, __init__.py for every package dir,
   code/backend/requirements.txt, and code/backend/tests/ with real pytest tests
   (the build is rejected if tests are missing).
   ## Dependency order — which files must exist before which, as a list.
   ## Implementation order — numbered steps, one file per step.

2. `CONTRACT_REGISTRY.json` — valid JSON, exactly this shape:
{{
  "api_endpoints": [
    {{"method": "POST", "path": "/api/<resource>", "request_schema": "<Name>Create",
      "response_schema": "<Name>Out", "auth_required": false}}
  ],
  "shared_types": [
    {{"name": "<Name>Create", "fields": {{"<field>": "<type>"}}, "required": ["<field>"]}}
  ],
  "db_tables": [{{"name": "<table>", "columns": {{"id": "int pk", "<field>": "<type>"}}}}],
  "env_vars": [],
  "dependencies": ["fastapi", "uvicorn", "sqlalchemy", "pydantic"]
}}
Cover the full CRUD lifecycle (create, list, get one, update or delete as the
request implies). Every schema named in api_endpoints must exist in shared_types.{err_block}"""


def validate_plan(build_dir: Path) -> list[str]:
    """Hard validation. Empty list = valid."""
    errors: list[str] = []
    plan = build_dir / "PLAN.md"
    if not plan.is_file() or len(plan.read_text(encoding="utf-8", errors="replace")) < 200:
        errors.append("PLAN.md missing or trivially short")
    elif "manifest" not in plan.read_text(encoding="utf-8", errors="replace").lower():
        errors.append("PLAN.md has no file manifest section")
    cpath = build_dir / "CONTRACT_REGISTRY.json"
    if not cpath.is_file():
        return errors + ["CONTRACT_REGISTRY.json missing"]
    try:
        contract = json.loads(cpath.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return errors + [f"CONTRACT_REGISTRY.json is not valid JSON: {e}"]
    for key in _CONTRACT_KEYS:
        if key not in contract:
            errors.append(f"contract missing key {key!r}")
    eps = contract.get("api_endpoints") or []
    if not eps:
        errors.append("api_endpoints is empty")
    for i, ep in enumerate(eps):
        for k in _ENDPOINT_KEYS:
            if k not in ep:
                errors.append(f"api_endpoints[{i}] missing {k!r}")
    return errors


def _run_harness_attempt(prompt: str, build_dir: Path, errors: list[str] | None, log) -> None:
    from providers import HARNESS_PROVIDERS
    provider = HARNESS_PROVIDERS["cerebras_gemma"]
    ctx = Context(workspace_root=build_dir.resolve())
    run_agent(provider, _SYSTEM, _task(prompt, errors), ctx, max_rounds=8, log=log)


def _run_cli_fallback(prompt: str, build_dir: Path, errors: list[str], log) -> None:
    from providers import PROVIDERS
    log("planner: falling back to claude CLI")
    cli_prompt = (_SYSTEM + "\n\n" + _task(prompt, errors)
                  + "\n\nWrite the two files into the current directory using your "
                    "file tools. Do not narrate.")
    PROVIDERS["claude"]().run(cli_prompt, build_dir, ECHARA_ROOT / "logs", timeout_sec=600)


def run_planner(prompt: str, build_dir: Path, log=lambda s: None) -> dict:
    """Produce PLAN.md + CONTRACT_REGISTRY.json in build_dir. Raises PlanFailed
    when even the fallback can't produce a valid plan."""
    build_dir = Path(build_dir)
    attempts, model = 0, "cerebras/gemma-4-31b"
    errors: list[str] | None = None
    for _ in range(2):  # primary + one error-fed retry
        attempts += 1
        try:
            _run_harness_attempt(prompt, build_dir, errors, log)
        except Exception as e:  # noqa: BLE001 — provider down → go to fallback
            log(f"planner: harness attempt failed: {e!r}")
            break
        errors = validate_plan(build_dir)
        if not errors:
            return {"model": model, "attempts": attempts}
        log(f"planner: plan rejected: {errors}")
    attempts += 1
    _run_cli_fallback(prompt, build_dir, errors or ["harness provider unavailable"], log)
    errors = validate_plan(build_dir)
    if errors:
        raise PlanFailed(f"no valid plan after {attempts} attempts: {errors}")
    return {"model": "claude-cli (fallback)", "attempts": attempts}
