"""Agent 1 — Planner. Turns the user prompt into PLAN.md + CONTRACT_REGISTRY.json.

Model policy: cheapest available. Sonnet is not reachable as a raw API (no
Anthropic key funded), so the primary is Cerebras gemma-4-31b — free, live, and
tool-call capable — driven by the M2.5 harness. Output is validated hard
(parseable JSON, complete endpoints); invalid output gets one retry with the
exact validation errors, then falls back to the claude CLI.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from harness.loop import run_agent
from harness.tools import Context
from phases import AgentDispatchError

ECHARA_ROOT = Path(__file__).resolve().parent.parent

_CONTRACT_KEYS = ["api_endpoints", "shared_types", "db_tables", "env_vars", "dependencies"]
_ENDPOINT_KEYS = ["method", "path", "request_schema", "response_schema"]


class PlanFailed(AgentDispatchError):
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

Produce a plan for a PRODUCTION-GRADE Python FastAPI + SQLite (SQLAlchemy)
backend. Backend only — no frontend, no Docker, no CI. Create tables with
Base.metadata.create_all in the app lifespan; do NOT plan alembic migrations
unless the request demands them.

DEPTH REQUIREMENTS (production bar — a thin MVP plan is rejected):
- Feature surface: for EVERY resource, plan the full management surface —
  create, list, get-one, update, delete, PLUS every domain operation the
  request implies (e.g. archive AND unarchive; tags as their own resource with
  list/get/rename/delete; a GET /users/me profile endpoint). Every list
  endpoint MUST support and document in PLAN.md: pagination (page, size with a
  max cap), filtering by every relationship and flag it has (e.g. tag,
  archived), sorting (sort= field + order), and text search where the request
  implies it.
- Layered architecture, one responsibility per file:
  app/core/config.py (env-driven settings), app/core/security.py (hashing +
  JWT), app/core/pagination.py (shared PageParams + paginated response
  helper), app/core/deps.py (get_db / get_current_user dependencies),
  app/exceptions.py (domain exceptions + FastAPI handlers),
  app/models/<resource>.py, app/schemas/<resource>.py,
  app/services/<resource>.py (ALL database logic lives in services),
  app/routers/<resource>.py (thin: parse request -> call service -> respond).
- Test plan (this is where real projects spend their lines — do not skimp):
  * tests/conftest.py with db/client/user+auth-header factory fixtures.
  * ONE test file PER ROUTER covering, for EVERY endpoint: success, validation
    error (422), not-found (404), and ownership-violation cases.
  * tests/test_auth_matrix.py — EVERY protected endpoint tested with: no
    token, a malformed token, an expired token, and another user's token.
    One test per endpoint per case; parametrize if you like, but every
    endpoint x case pair must execute.
  * tests/test_pagination_and_search.py — per list endpoint: empty results,
    page beyond range, size above the cap, size=1, filter combinations, and
    search matching/non-matching cases.
  * tests/test_services_<resource>.py unit tests for service-layer logic and
    edge cases (duplicate tags, boundary dates, idempotent archive).
  * tests/test_flows.py — complete user-journey integration tests (e.g.
    register -> login -> create tags -> create bookmarks -> search -> paginate
    -> archive -> stats -> delete -> verify cascade), at least three distinct
    journeys, each asserting every intermediate response body.
  * tests/test_exception_handlers.py — every custom exception handler and
    error shape (404 body shape, 422 body shape, 401 body shape).
  Name each test file and list what it covers.
- Documentation is part of the code: every module gets a header docstring,
  every public class and function a Google-style docstring (Args/Returns/
  Raises). State this in PLAN.md so the builder implements it.
- The manifest for a multi-resource app should decompose into 50-60 files.
  Splitting by responsibility is required; padding or dead code is forbidden.

Write exactly two files with the write_file tool, then call done:

1. `PLAN.md` with exactly these sections:
   ## File manifest — one line per file: `path` — purpose. All code lives under
   `code/backend/`. Must include code/backend/requirements.txt and the full
   layered layout + test files described above, with __init__.py for every
   package dir (the build is rejected if tests are missing).
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
    else:
        text = plan.read_text(encoding="utf-8", errors="replace")
        if "manifest" not in text.lower():
            errors.append("PLAN.md has no file manifest section")
        # Depth floor: production plans decompose. Count manifest file paths.
        n_files = len(re.findall(r"(?m)^\W*code/backend/\S+", text))
        if 0 < n_files < 20:
            errors.append(
                f"manifest has only {n_files} files — too thin. Decompose per the "
                "DEPTH REQUIREMENTS: services layer, core/config+security, "
                "exceptions, per-router test files, service unit tests (35-45 files).")
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


# --- M5 per-module planning --------------------------------------------------

def _module_floor(module: dict) -> int:
    # Fixes breakage #6: the global 20-file floor rejected legit small module
    # plans. Per-module floor scales with the budget instead.
    return max(6, int(module.get("loc_budget", 800)) // 400)


def _validate_module_plan(build_dir: Path, module: dict) -> list[str]:
    """Parameterized validator: files under the module's path_root, count >=
    floor. Fixes #5 (foreign/empty paths pass) and #6 (global floor too high)."""
    name, root = module["name"], module["path_root"].rstrip("/")
    p = build_dir / f"PLAN_{name}.md"
    if not p.is_file():
        return [f"PLAN_{name}.md missing"]
    text = p.read_text(encoding="utf-8", errors="replace")
    errors = []
    if len(text) < 100 or "manifest" not in text.lower():
        errors.append(f"PLAN_{name}.md too short or has no manifest section")
    files = set(re.findall(r"(code/[\w/.\-]+\.\w+)", text))
    under = [f for f in files if f.startswith(root + "/")]
    if not under:
        errors.append(f"{name}: no manifest files under path_root {root!r}")
    elif len(under) < _module_floor(module):
        errors.append(f"{name}: {len(under)} files under {root} < floor "
                      f"{_module_floor(module)} — decompose further")
    return errors


def _module_task(prompt: str, module: dict, conventions: str, dep_seams: dict,
                 errors: list[str] | None) -> str:
    name, root, budget = module["name"], module["path_root"].rstrip("/"), module["loc_budget"]
    seam_txt = json.dumps(dep_seams, indent=1) if any(dep_seams.values()) \
        else "(this module has no dependencies to import from)"
    err = ("\n\nPREVIOUS OUTPUT REJECTED — fix exactly:\n- " + "\n- ".join(errors)) if errors else ""
    return f"""OVERALL SYSTEM REQUEST: {prompt}

You are planning ONE module of a larger system: **{name}** (kind: {module['kind']}).
ALL of this module's files live under: {root}/
LOC budget: {budget}. It depends on: {module.get('depends_on', []) or 'nothing'}.

Symbols you MAY import from your dependencies (do NOT redeclare them):
{seam_txt}

CONVENTIONS every module must obey:
{conventions or '(none provided)'}

Write ONE file `PLAN_{name}.md` with your file tool, then call done. Sections:
## File manifest — one line per file: `path` — purpose. EVERY path starts with
`{root}/`. Follow the CONVENTIONS layout (models/schemas/services/routers as
applicable), an __init__.py for each package dir, and a tests/ subdir with real
pytest tests for this module (success, validation, not-found, auth+ownership
where relevant). At least {_module_floor(module)} files.
## Implementation order — numbered, one file per step, in dependency order.{err}"""


def _run_module_planner(prompt: str, build_dir: Path, module: dict, conventions: str,
                        dep_seams: dict, errors: list[str] | None, log) -> None:
    task = _module_task(prompt, module, conventions, dep_seams, errors)
    try:
        from providers import HARNESS_PROVIDERS
        provider = HARNESS_PROVIDERS["cerebras_gemma"]
        run_agent(provider, _SYSTEM, task, Context(workspace_root=build_dir.resolve()),
                  max_rounds=6, log=log)
        return
    except Exception as e:  # noqa: BLE001 — harness down → claude fallback
        log(f"planner[{module['name']}]: harness failed ({e!r}) — claude fallback")
    from providers import PROVIDERS
    PROVIDERS["claude"]().run(
        _SYSTEM + "\n\n" + task + "\n\nWrite the file now; do not narrate.",
        build_dir, ECHARA_ROOT / "logs", timeout_sec=600)


def _run_module_planners(prompt: str, build_dir: Path, log) -> dict:
    from agents import architect
    modules = architect.load_modules(build_dir)
    conventions = (build_dir / "CONVENTIONS.md").read_text(encoding="utf-8", errors="replace") \
        if (build_dir / "CONVENTIONS.md").is_file() else ""
    seams = json.loads((build_dir / "SEAMS.json").read_text(encoding="utf-8")) \
        if (build_dir / "SEAMS.json").is_file() else {}
    planned = 0
    for m in modules:
        if not _validate_module_plan(build_dir, m):
            continue  # already planned + valid (cheap resume)
        dep_seams = {d: seams.get(d, []) for d in m.get("depends_on", [])}
        errors: list[str] | None = None
        for _ in range(2):  # primary + one error-fed retry
            _run_module_planner(prompt, build_dir, m, conventions, dep_seams, errors, log)
            errors = _validate_module_plan(build_dir, m)
            if not errors:
                break
            log(f"planner: module {m['name']} rejected: {errors}")
        if errors:
            raise PlanFailed(f"module {m['name']}: {errors}")
        planned += 1
    return {"model": "gemma per-module", "attempts": planned, "modules": len(modules)}


def run_planner(prompt: str, build_dir: Path, log=lambda s: None) -> dict:
    """Multi-module (MODULES.json present) → per-module plans; else the flat
    single-plan path. Raises PlanFailed when a valid plan can't be produced."""
    build_dir = Path(build_dir)
    if (build_dir / "MODULES.json").is_file():
        return _run_module_planners(prompt, build_dir, log)

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
