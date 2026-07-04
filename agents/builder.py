"""Agent 2 — Builder. Executes PLAN.md into build_dir/code/ in WAVES.

A single CLI session naturally wraps up around ~800 LOC no matter what the
plan says — the observed ceiling across every eval. To produce 5k+ LOC with
real structure, the manifest's implementation order is chunked into waves of
~8 files; each wave is a fresh CLI dispatch scoped to exactly those files
(reading earlier waves' code from disk for interfaces), followed by one
integration pass that runs the full test suite and fixes failures. Small
manifests (<= 12 files) keep the proven single-pass path.

Model policy: claude CLI first, codex fallback. A lane that hard-fails once is
skipped for the REST OF THIS BUILD (a 429 session limit doesn't heal between
waves; burning 90s per wave re-proving it is waste).
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from skills import router as skills_router
from harness import skills as skills_stage
from phases import AgentDispatchError

ECHARA_ROOT = Path(__file__).resolve().parent.parent
WAVE_SIZE = 8
SINGLE_PASS_MAX = 12


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
- NN-DEP-1: The runtime is a very recent Python. Do NOT hard-pin old exact
  versions of Rust-backed packages (pydantic, cryptography, bcrypt) that may
  lack a wheel for it — pip would try a source build and fail. Prefer a
  compatible floor (e.g. `pydantic>=2.9`) or a version known to ship wheels
  for the current Python. When unsure, leave it unpinned.
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
- NN-STRUCT-1: Routers stay thin — parse request, call the service, shape the
  response. ALL database logic lives in app/services/. If a router grows a
  query, it is in the wrong file.
- NN-TEST-1: Implement the plan's FULL test matrix — for every endpoint:
  success, validation error, unauthenticated (where auth applies), not-found,
  and ownership-violation cases, plus the service unit tests the plan names.
  Never trim the test plan to finish faster. Prefer explicit test functions
  over heavy parametrize-compression — each named case should be readable on
  its own.
- NN-DOC-1: Every module gets a header docstring; every public class and
  function gets a Google-style docstring (Args/Returns/Raises). Code without
  docstrings is incomplete.
- Do not implement ANYTHING not in PLAN.md. No extra endpoints, no unrequested
  auth, no logging frameworks, no Docker. (If the plan REQUIRES auth, implement
  it exactly as specified.)
- Do not put everything in one file — follow the file manifest exactly.
- Self-verify what you wrote before stopping."""


def _implementation_order(plan_md: str) -> list[str]:
    """File paths from the '## Implementation order' numbered list (fallback:
    every code/backend path in the manifest, in order of appearance)."""
    section = re.split(r"(?mi)^##\s*Implementation order.*$", plan_md)
    text = section[1] if len(section) > 1 else plan_md
    files = re.findall(r"(?m)^\s*(?:\d+\.|\-)\s*`?(code/backend/\S+?)`?(?:\s+—.*|\s+-\s.*)?$", text)
    if len(files) < 5:
        files = re.findall(r"`?(code/backend/[\w/.\-]+)`?", plan_md)
    seen, out = set(), []
    for f in files:
        f = f.rstrip("`.,:;")
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def _waves(files: list[str], size: int = WAVE_SIZE) -> list[list[str]]:
    return [files[i:i + size] for i in range(0, len(files), size)]


def _base_context(plan_md: str, contract: str, skill_rel: str | None) -> str:
    skill_block = ""
    if skill_rel:
        skill_block = (f"\n=== SKILL ===\nA backend-development skill is at "
                       f"`{skill_rel}/SKILL.md`. Read it for principles. Ignore "
                       "Node.js specifics — this project is Python/FastAPI.\n")
    return (f"{_NN_RULES}\n\n=== PLAN.md ===\n{plan_md}\n"
            f"\n=== CONTRACT_REGISTRY.json ===\n{contract}\n{skill_block}")


def _wave_prompt(n: int, total: int, files: list[str], ctx: str) -> str:
    listing = "\n".join(f"- {f}" for f in files)
    return (
        f"TASK: Wave {n} of {total} of a phased build. Files from earlier waves "
        "already exist on disk — READ them for interfaces and imports; do NOT "
        "rewrite them. Implement ONLY the following manifest files now, "
        "completely, with production-quality code (no stubs, no TODOs):\n"
        f"{listing}\n\n"
        "Every listed file must exist with full real code when you stop. Do not "
        "touch files outside this list. Do not narrate; do not ask questions.\n\n"
        f"{ctx}")


def _integration_prompt(ctx: str) -> str:
    return (
        "TASK: Final integration pass. Every manifest file exists on disk. From "
        "code/backend run `python -c \"from app.main import app\"` and "
        "`python -m pytest tests/ -q`. Fix EVERY failure — imports, wiring, "
        "fixtures, test bugs — until both are clean. Do not add features; do "
        "not delete tests to make them pass. Do not narrate.\n\n"
        f"{ctx}")


def _fix_prompt(last_error: str, ctx: str) -> str:
    return (
        "TASK: The build exists but verification failed. Fix exactly these "
        "errors, re-run the failing checks from code/backend, and stop when "
        "they pass. Do not rewrite working files; do not delete tests.\n"
        "=== VERIFICATION ERRORS ===\n"
        f"{last_error}\n\n"
        f"{ctx}")


def run_builder(build_dir: Path, last_error: str = "", log=lambda s: None) -> dict:
    """Dispatch the build (waves for big manifests, single-pass for small,
    one focused fix-pass on retry). Raises BuildDispatchFailed only when every
    lane is dead — bad code is the Verifier's problem."""
    from providers import PROVIDERS
    from providers import availability

    build_dir = Path(build_dir)
    plan_md = (build_dir / "PLAN.md").read_text(encoding="utf-8", errors="replace")
    contract = (build_dir / "CONTRACT_REGISTRY.json").read_text(encoding="utf-8", errors="replace")

    skill_rel = None
    pool = skills_router.DEFAULT_POOL_ROOT
    if pool.is_dir():
        staged = skills_stage.stage(pool, build_dir)
        if (staged / "senior-backend" / "SKILL.md").is_file():
            skill_rel = "skills/senior-backend"

    ctx = _base_context(plan_md, contract, skill_rel)
    if last_error:
        prompts = [("fix", _fix_prompt(last_error, ctx))]
    else:
        files = _implementation_order(plan_md)
        if len(files) > SINGLE_PASS_MAX:
            chunks = _waves(files)
            prompts = []
            for i, chunk in enumerate(chunks):
                # Resume cheaply: a wave whose files all exist already ran.
                if all((build_dir / f).is_file() for f in chunk):
                    log(f"builder: wave {i + 1}/{len(chunks)} already on disk — skipped")
                    continue
                prompts.append((f"wave {i + 1}/{len(chunks)}",
                                _wave_prompt(i + 1, len(chunks), chunk, ctx)))
            prompts.append(("integration", _integration_prompt(ctx)))
        else:
            prompts = [("single", _wave_prompt(1, 1, files, ctx))]
    (build_dir / "BUILDER_PROMPT.md").write_text(prompts[-1][1], encoding="utf-8")

    dead: set[str] = set()  # lanes that hard-failed this build — don't retry them per wave
    used, started = [], time.monotonic()
    for label, prompt in prompts:
        dispatched = False
        failures = []
        for name in ("claude", "codex"):
            if name in dead:
                continue
            if not availability.is_available(name):
                failures.append(f"{name}: on cooldown")
                continue
            log(f"builder: {label} -> {name}")
            result = PROVIDERS[name]().run(prompt, build_dir, ECHARA_ROOT / "logs",
                                           timeout_sec=1500)
            if result.ok:
                used.append(name)
                dispatched = True
                break
            dead.add(name)
            failures.append(f"{name}: exit={result.exit_code} kill={result.kill_reason} "
                            f"skip={result.skipped_reason}")
            log(f"builder: {failures[-1]} — lane dead for this build")
        if not dispatched:
            raise BuildDispatchFailed(f"{label}: " + "; ".join(failures))
    return {"provider": "+".join(sorted(set(used))), "waves": len(prompts),
            "elapsed_sec": round(time.monotonic() - started, 2)}
