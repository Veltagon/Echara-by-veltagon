# V2 Reuse Reference — Logic & Function of Each Reusable Component

Not the code — the WHAT and HOW so you can rewrite each from scratch.

---

## 1. Deterministic Repairs (the proven subset)

### What `repair_all` does

A dispatcher that runs a list of repair functions against `code_dir/`. Each repair:
- Takes `code_dir: Path` → returns `list[str]` (actions taken, empty if no-op)
- Is idempotent (re-running on fixed code does nothing)
- Is gated by a feature flag (skip with `ECHARA_REPAIR_<NAME>=0`)
- AST-validates before writing (never writes unparseable code)

The dispatcher writes `.repairs_pending` at start, calls each repair, fsyncs touched files, renames to `.repairs_complete` at end (the barrier).

### The 9 repairs to reimplement

**D4 — `repair_alembic_migration_chain`**
- Problem: agent writes two migrations that both `op.create_table('notes')`. Second one crashes.
- Logic: AST-parse all `versions/*.py`. Chain them by `down_revision`. Walk the chain tracking `op.create_table(name)` calls. If migration N+1 creates a table N already created AND N+1 doesn't drop it first → inject `op.execute("DROP TABLE IF EXISTS <name>")` before the create.
- Key detail: only fires when chain has exactly 1 root (single `down_revision=None`). Multiple roots are a different repair's job.
- Gate: `ECHARA_REPAIR_ALEMBIC_CHAIN_DUPLICATE_CREATE`

**D6 — `repair_fastapi_validation_loc_shape`**
- Problem: agent writes `response.json()["detail"]["loc"]` but FastAPI returns `detail` as a LIST, not dict.
- Logic: regex scan `backend/tests/*.py` files that import `TestClient`. Pattern: `.json()["detail"]["<key>"]` → rewrite to `.json()["detail"][0]["<key>"]`. AST-validate result.
- Key detail: idempotency guard checks for `[0]` already present. Only fires on TestClient-importing files.
- Gate: `ECHARA_REPAIR_FASTAPI_VALIDATION_LOC_SHAPE`

**S-28a — `repair_alembic_scaffold`**
- Problem: agent creates `alembic.ini` but no `env.py` / `script.py.mako` / `versions/`.
- Logic: find alembic dirs (`backend/alembic/`, `alembic/`). If `env.py` missing → write it. Auto-detect async vs sync from `app/models/base.py` (look for `create_async_engine`). Fix `script_location` in `alembic.ini` to use `%(here)s/` prefix (cwd-independent).
- Key detail: ONE function decides async vs sync. Two templates (_ENV_PY_ASYNC, _ENV_PY_SYNC). Never two functions fighting.
- Gate: part of STRONG_SET default-on

**S-28b — `repair_pytest_asyncio`**
- Problem: `@pytest.fixture` on `async def` produces `async_generator` pytest can't unwrap.
- Logic: scan `pytest.ini` / `setup.cfg` / `pyproject.toml`. Ensure `asyncio_mode = auto` + asyncio marker registered. Rewrite section headers (`[tool:pytest]` in pytest.ini is ignored — must be `[pytest]`).
- Gate: part of STRONG_SET default-on

**`repair_ruff_autofix`**
- Problem: trivial lint failures block agent rounds.
- Logic: `subprocess.run(["ruff", "check", "--fix", "--quiet", "backend/app/"])`. That's it.
- Gate: part of STRONG_SET default-on

**`repair_test_migrations_capture_stderr`**
- Problem: `subprocess.run(check=True)` without `capture_output=True` loses alembic stderr on failure.
- Logic: AST NodeTransformer. Find `subprocess.run(check=True, ...)` Expr statements in `tests/test_migrations.py`. Add `capture_output=True, text=True`. Wrap in `try/except CalledProcessError as _e: raise AssertionError(f"...stdout: {_e.stdout}\nstderr: {_e.stderr}") from _e`. Whole-file `ast.unparse`.
- Key detail: idempotent — skips files already containing both `capture_output=True` and `CalledProcessError`.
- Gate: `ECHARA_REPAIR_TEST_MIGRATIONS_CAPTURE_STDERR`

**`repair_missing_client_fixture`**
- Problem: tests request a `client` fixture but conftest only defines `session`.
- Logic: scan `backend/tests/*.py` for `def test_*(client)` parameter. If conftest doesn't define `client` → synthesize it: sync schema-create on temp SQLite file, async sessions via `dependency_overrides`, TestClient, cleanup.
- Gate: part of STRONG_SET default-on

**`repair_health_endpoint_path`**
- Problem: contract says `/health` but code has `/api/health` (or vice versa).
- Logic: read CONTRACT_FROZEN.json health route path. Grep `main.py` + router files for health endpoint decorators. If path disagrees → rewrite the decorator path.
- Gate: part of STRONG_SET default-on

**`repair_canonical_filenames`**
- Problem: agent writes `frontend/package` (no `.json` extension).
- Logic: known mapping: `frontend/package` → `frontend/package.json`, `backend/requirement` → `backend/requirements.txt`, etc. Check existence + rename.
- Gate: part of STRONG_SET default-on

### The fsync barrier (not a repair, but lives with them)

- `_post_repair_barrier(code_dir, started_ts)` — after all repairs run, walk files with `mtime >= started_ts`, `os.fsync` each, atomic rename `.repairs_pending` → `.repairs_complete`
- `_wait_for_repair_barrier(code_dir, smoke_cwd, timeout_s=30)` — in runtime_smoke, before pytest dispatch: poll for `.repairs_pending` to clear, then defensively fsync `alembic/versions/`, `tests/`, `app/`

Why: `Path.write_text()` closes but doesn't fsync. On Windows NTFS, subprocess sees stale bytes. B16 false-failed on this.

---

## 2. Smoke Runner (`runtime_smoke.py`)

### What it does

Runs 3 workflows in order against produced code. Returns a tri-state verdict: `ok`, `failed`, `unknown`.

### The 3 workflows

**import_smoke** — `subprocess.run([python, "-c", "from app.main import app"], cwd=backend_dir)`. Passes → app is importable. Fails → SyntaxError, ImportError, or missing dependency.

**alembic_upgrade** — runs alembic via a `-c` wrapper script that:
1. Strips cwd from sys.path (prevents `backend/alembic/` shadowing pip's alembic)
2. Imports alembic from site-packages FIRST
3. Then adds backend_dir to sys.path for `from app.models.base import Base`
4. Overrides `sqlalchemy.url` to a throwaway SQLite file (auto-detects async vs sync driver)
5. Deletes the throwaway db file before each run (fresh state)
6. Runs `alembic.command.upgrade(cfg, 'head')`

**pytest** — `subprocess.run([python, "-m", "pytest", "tests/", "-q", "--tb=short"], cwd=backend_dir)`. Pre-scans test files with `py_compile`; silently `--ignore`s files with SyntaxError so one bad test doesn't kill the entire suite.

### Key design decisions

- Uses `_subprocess_run()` helper that captures stdout+stderr, enforces timeout, returns `(ok, detail, elapsed)`
- Detail string is capped at 800 chars per stream (no 100MB stream-json in memory)
- `SmokeReport.status` property returns `"unknown"` when nothing executed (no docker AND no native run). `unknown` is NEVER a pass.
- The barrier call (`_wait_for_repair_barrier`) runs RIGHT BEFORE pytest dispatch, after the py_compile prescan

### What V2 should keep

The function signature + the tri-state semantics + the alembic wrapper trick (the S-29 fix). The fsync barrier. The py_compile prescan. Drop the docker-compose path if you're not targeting Docker on day 1.

---

## 3. Import Gate (`preflight_gates.py` import oracle)

### What it does

At BUILD phase exit, before advancing to VERIFY: run `from app.main import app` in a provisioned venv. If it fails → don't advance. Re-dispatch the owning agent with the exact error. Retry up to N cycles. Fail-open after exhaustion (so hard tasks still terminate).

### The logic (from `enterprise_orchestrator.py:3783-3841`)

```
1. reconcile_cross_module_symbols(code_dir)   # fix __init__.py re-exports
2. ensure_app_entrypoint()                     # ensure main.py exists
3. run_import_oracle(code_dir, interpreter)    # subprocess: python -c "from app.main import app"
4. if oracle.ok → advance
5. if cycle >= max_cycles → advance (fail-open)
6. else → map failures to owning agents → dispatch focused wave → stay in BUILD
```

### Key details

- Uses the `.preflight_venv` interpreter (provisioned with the project's requirements.txt) so imports resolve against the project's actual deps, not the orchestrator's Python
- `run_import_oracle` returns `(ok: bool, failures: list[str], detail: str)`
- `_import_gate_owner_tasks` maps each ImportError to the agent whose `can_write` scope covers the failing module file
- Fail-open after N cycles is critical — without it, an impossible import blocks the build forever
- Also runs at QG entry (`:3846-3880`) as a belt-and-suspenders catch for the watchdog force-advance path that bypasses the IMPLEMENT-exit gate

### What V2 should keep

The concept: importability is a BLOCKING precondition for leaving BUILD. The venv provisioning (test against real deps, not the orchestrator's Python). The fail-open after N cycles. The owner-attribution for re-dispatch.

---

## 4. NN-Rules (`agent_spec/*.yaml`)

### What they are

Non-negotiable rules in each agent's YAML spec. Each rule is a structured object:

```yaml
- id: "NN-5: NEVER import alembic.command in app code"
  rule: |
    from alembic import command in main.py shadows the pip package
    when cwd=backend/ (backend/alembic/ dir wins the import).
    ImportError on first app import. Build dies.
  why: "Build 5 + Build 8 died from this. Two builds wasted."
  example_wrong: "from alembic import command  # in main.py"
  example_right: "# Use subprocess for migrations, never import alembic in app"
```

### The pattern (what makes them work)

Each NN-rule has 4 parts:
1. **ID** — short, memorable (`NN-BE-5`, `NN-IMPORT-3`)
2. **Rule text** — the exact thing to do/avoid
3. **Why** — which specific build crashed without it (not generic "it's bad practice")
4. **Examples** — wrong code + right code, verbatim

### The proven rules to carry forward

**Backend_Dev rules:**
- NN-1: CONTRACT_FROZEN is source of truth (read it before writing any endpoint)
- NN-2: Lifespan creates tables before first request (`Base.metadata.create_all`)
- NN-3: CORS never wildcard + credentials (browsers reject it)
- NN-5: requirements.txt covers every non-stdlib import
- NN-6: Self-verify lifecycle for every resource (POST→GET→PATCH→DELETE)
- NN-IMPORT-1: Verify import exists before writing it
- NN-IMPORT-2: Never collide `sqlalchemy.Enum` with `enum.Enum`
- NN-IMPORT-3: No circular imports
- NN-IMPORT-4: Every package dir needs `__init__.py`
- NN-IMPORT-5: Absolute imports only
- NN-BE-1: Register every new router in main.py
- NN-BE-2: One Base, one metadata

**Database_Engineer rules:**
- NN-1: Migration chain has exactly ONE root (`down_revision=None`)
- NN-2: Migration strings properly terminated
- NN-3: Every FK column gets an index
- NN-5: Every upgrade has a working downgrade
- NN-6: Test files don't `cd backend` (cwd doubling)

### What V2 should keep

The STRUCTURE (id + rule + why + examples). The specific rules above. Add new rules only from actual build failures — never speculatively.

---

## 5. Contract Registry Data Structure

### The actual schema (from B13's `CONTRACT_REGISTRY.json`)

```json
{
  "api_endpoints": [
    {
      "method": "POST",
      "path": "/api/notes",
      "request_schema": "NoteCreate",
      "response_schema": "NoteOut",
      "auth_required": false,
      "declared_by": "architect",
      "declared_at_round": 0,
      "backend_file": "",
      "used_by": []
    }
  ],
  "db_tables": [],
  "frontend_components": [],
  "frontend_routes": [],
  "shared_types": [
    {
      "name": "NoteCreate",
      "fields": {"title": "str"},
      "required": ["title"],
      "description": ""
    },
    {
      "name": "NoteOut",
      "fields": {"id": "int", "title": "str", "created_at": "datetime"},
      "required": ["id", "title", "created_at"],
      "description": ""
    }
  ],
  "env_vars": [],
  "dependencies": []
}
```

### What each field means

**`api_endpoints[]`** — every REST endpoint the app must implement:
- `method` + `path`: the route (e.g. `GET /api/notes/{id}`)
- `request_schema` / `response_schema`: names of shared_types the endpoint uses
- `auth_required`: whether the route needs auth middleware
- `declared_by`: which agent/phase declared it (traceability)
- `backend_file`: filled post-BUILD with the actual file that implements it
- `used_by`: which frontend components call this endpoint

**`shared_types[]`** — Pydantic schemas shared between backend + frontend:
- `name`: the schema class name (`NoteCreate`, `NoteOut`)
- `fields`: field name → type string
- `required`: list of field names that are NOT optional
- `description`: optional

**`db_tables[]`** — (empty in notes CRUD; populated for complex apps)

**`env_vars[]`** — environment variables the app needs

**`dependencies[]`** — pip/npm packages

### How it's used

1. **CONTRACT phase** emits this JSON via structured output from the Architect
2. **`contract_artifact.py:load_frozen()`** reads it at IMPLEMENT dispatch time
3. **`contract_artifact.py:slice_for_role(frozen, agent_name, budget_chars=2500)`** filters to only the endpoints/types relevant to this agent
4. Both the full JSON (Layer 2, 6K cap) and the per-role slice (A2, 2.5K cap) are injected into each agent's prompt
5. **Preflight gate `ac_realbody_check`** verifies that test files exercise the contract's endpoints (AST body walk, not docstring grep)
6. **D2.4 canonical path-param normalization** rewrites `{note_id}` → `{id}` in the contract at freeze time so agents see ONE canonical path-param name

### What V2 should keep

The schema shape. The `slice_for_role` function (agents shouldn't see endpoints they don't own). The two-path injection (belt+suspenders). The path-param canonicalization.

### What V2 should change

- Emit via StructuredOutput (not free-form markdown that gets regex-parsed into JSON)
- `auth_required` should come from `INSTRUCTION_LEDGER.json` policy detection, not be hardcoded `true` for every endpoint (B13's contract has `auth_required: true` on a `auth:none` project)
- `db_tables` should be populated — it's always empty because the Architect never fills it. Either make the Architect fill it or derive it from `shared_types` + `api_endpoints` automatically

---

## Quick lookup: where to find each in V1 source

| Component | V1 file | V1 lines | What to extract |
|---|---|---|---|
| repair_all dispatcher | `produced_code_repair.py` | 4719-4730 | The `_r()` registration pattern + barrier calls |
| D4 alembic chain | same | 2310-2520 | AST walker + chain builder + inject logic |
| D6 fastapi loc | same | 2527-2613 | Regex + idempotency guard + AST validate |
| stderr capture | same | 2616-2740 | AST NodeTransformer + try/except wrap |
| alembic scaffold | same | 549-679 | Auto-detect async/sync + template + ini fix |
| pytest asyncio | same | 691-780 | Config section + marker + decorator rewrite |
| ruff autofix | same | ~3200 | One subprocess call |
| client fixture | same | ~3800 | conftest synthesis |
| health path | same | ~1560 | CONTRACT_FROZEN read + decorator rewrite |
| canonical filenames | same | ~2100 | Known mapping + rename |
| fsync barrier | same | 4528-4587 | mtime walk + fsync + atomic rename |
| smoke wait barrier | `runtime_smoke.py` | 194-253 | Poll + defensive fsync |
| smoke runner | same | 256-461 | 3 workflows + tri-state + prescan |
| import gate | `enterprise_orchestrator.py` | 3783-3841 | oracle + owner map + fail-open |
| import oracle | `preflight_gates.py` | ~2200 | subprocess -c import + venv |
| contract registry | `contract_artifact.py` | 1-130 | freeze + load + slice_for_role |
| NN-rules | `agent_spec/Backend_Dev.yaml` | throughout | id + rule + why + examples |
| NN-rules | `agent_spec/Database_Engineer.yaml` | throughout | same pattern |
