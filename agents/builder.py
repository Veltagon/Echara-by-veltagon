"""Agent 2 — Builder. Executes PLAN.md file-by-file into build_dir/code/.

Model policy: best available — the claude CLI (M2's hardened ClaudeCodeProvider,
proven on exactly this task class), codex CLI as fallback. The CLI's own tool
loop writes the files; nothing is dumped into chat. The NN-rules below are the
V1-proven non-negotiables from HELP.md section 4, each one paid for by a real
dead build.
"""
from __future__ import annotations

from pathlib import Path

from skills import router as skills_router
from harness import skills as skills_stage
from phases import AgentDispatchError

ECHARA_ROOT = Path(__file__).resolve().parent.parent


class BuildDispatchFailed(AgentDispatchError):
    pass


_NN_RULES = """NON-NEGOTIABLE RULES (each one killed a real build when broken):
- NN-1: CONTRACT_REGISTRY.json is the source of truth. Read it before writing any
  endpoint. Implement every endpoint exactly as specified — method, path, schemas.
- NN-2: The FastAPI lifespan creates tables before the first request:
  `Base.metadata.create_all(bind=engine)` inside the lifespan. No alembic in app code.
- NN-3: CORS is never wildcard origins together with allow_credentials=True.
- NN-5: requirements.txt covers EVERY non-stdlib import you write. Missing dep =
  failed import smoke = failed build.
- NN-IMPORT-1: Verify an import target exists before writing the import.
- NN-IMPORT-3: No circular imports. db.py must not import from routers; models
  must not import from main.
- NN-IMPORT-4: Every package directory gets an __init__.py. No exceptions.
- NN-IMPORT-5: Absolute imports only (`from app.models.note import ...`), never
  relative dots across packages.
- NN-BE-1: Every router you create is registered in main.py with include_router.
- NN-BE-2: Exactly one SQLAlchemy Base, one metadata, defined once in app/db.py.
- NN-DB-1: Never a cwd-relative database URL. `sqlite:///./app.db` opens a
  different database per launch directory (stale-schema crashes in production).
  Anchor it: DATABASE_URL = os.environ.get("DATABASE_URL",
  f"sqlite:///{Path(__file__).resolve().parent.parent / 'app.db'}")
- NN-DB-2: Never commit/leave a .db file in the source tree; the lifespan
  creates the schema on boot.
- NN-AUTH-1: For password hashing use the `bcrypt` library DIRECTLY
  (bcrypt.hashpw/checkpw, truncate input to 72 bytes). Do NOT use passlib —
  passlib 1.7.4 is unmaintained and crashes with modern bcrypt at backend
  load. If you must use passlib, pin bcrypt==4.0.1 in requirements.txt.
- Do not implement ANYTHING not in PLAN.md. No extra endpoints, no unrequested
  auth, no logging frameworks, no Docker. (If the plan REQUIRES auth, implement
  it exactly as specified.)
- Do not put everything in one file — follow the file manifest exactly.
- Self-verify: after writing each file, read it back; after writing all files run
  `python -c "from app.main import app"` from code/backend/ and run
  `python -m pytest tests/ -q` from code/backend/. Fix failures before stopping."""


def _build_prompt(plan_md: str, contract_json: str, skill_rel: str | None,
                  last_error: str) -> str:
    retry_block = ""
    if last_error:
        retry_block = (
            "\n=== PREVIOUS VERIFICATION FAILED — FIX THESE EXACT ERRORS FIRST ===\n"
            f"{last_error}\n"
            "Fix the code so these specific checks pass. Do not rewrite working files.\n")
    skill_block = ""
    if skill_rel:
        skill_block = (f"\n=== SKILL ===\nA backend-development skill is at "
                       f"`{skill_rel}/SKILL.md`. Read it for principles (validation, "
                       "error codes, idempotency). Ignore Node.js specifics — this "
                       "project is Python/FastAPI.\n")
    return (
        "TASK: Implement the plan below file-by-file, in the dependency order it "
        "specifies, writing every file under `code/` with your file tools. The app "
        "lives at code/backend/. Do not stop until the import smoke and pytest both "
        "pass and every manifest file exists. Do not narrate; do not ask questions.\n"
        f"{retry_block}\n"
        f"{_NN_RULES}\n"
        "\n=== PLAN.md ===\n"
        f"{plan_md}\n"
        "\n=== CONTRACT_REGISTRY.json ===\n"
        f"{contract_json}\n"
        f"{skill_block}")


def run_builder(build_dir: Path, last_error: str = "", log=lambda s: None) -> dict:
    """Dispatch the build. Returns {provider, elapsed_sec}. Raises
    BuildDispatchFailed only when every CLI lane fails to run at all —
    bad code is the Verifier's job to catch, not ours."""
    from providers import PROVIDERS
    from providers import availability

    build_dir = Path(build_dir)
    plan_md = (build_dir / "PLAN.md").read_text(encoding="utf-8", errors="replace")
    contract = (build_dir / "CONTRACT_REGISTRY.json").read_text(encoding="utf-8", errors="replace")

    skill_rel = None
    pool = skills_router.DEFAULT_POOL_ROOT
    if pool.is_dir():  # stage senior-backend next to the code (M2-proven pattern)
        staged = skills_stage.stage(pool, build_dir)
        if (staged / "senior-backend" / "SKILL.md").is_file():
            skill_rel = "skills/senior-backend"

    prompt = _build_prompt(plan_md, contract, skill_rel, last_error)
    (build_dir / "BUILDER_PROMPT.md").write_text(prompt, encoding="utf-8")

    failures = []
    for name in ("claude", "codex"):
        if not availability.is_available(name):
            failures.append(f"{name}: on cooldown")
            continue
        log(f"builder: dispatching {name} CLI")
        result = PROVIDERS[name]().run(prompt, build_dir, ECHARA_ROOT / "logs",
                                       timeout_sec=1500)
        if result.ok:
            return {"provider": name, "elapsed_sec": result.elapsed_sec}
        failures.append(f"{name}: exit={result.exit_code} kill={result.kill_reason} "
                        f"skip={result.skipped_reason}")
        log(f"builder: {failures[-1]}")
    raise BuildDispatchFailed("; ".join(failures))
