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

import json
import os
import py_compile
import re
import time
from pathlib import Path

from agents import interfaces
from agents import progress
from skills import router as skills_router
from harness import skills as skills_stage
from phases import AgentDispatchError

ECHARA_ROOT = Path(__file__).resolve().parent.parent
WAVE_SIZE = 8
SINGLE_PASS_MAX = 12
# Single-module builds (current pipeline) use this synthetic module name for the
# deterministic interface index; Phase C replaces it with real MODULES.json names.
_DEFAULT_MODULE = "app"


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


def _wave_context(build_dir: Path, module: str, deps: list[str], plan_md: str,
                  contract: str, skill_rel: str | None, journal_tail: str = "") -> str:
    """Flat wave context. The deterministic interface index (accurate signatures
    of everything built so far, read fresh from disk) REPLACES the old 'READ
    earlier waves and guess' instruction — the drift fix (#2). Regenerated each
    wave; costs zero tokens to produce."""
    parts = [_NN_RULES]
    iface = interfaces.read_interfaces(build_dir, [module, *deps])
    if iface.strip():
        parts.append(
            "=== INTERFACES ALREADY ON DISK (accurate signatures — import from "
            "these, do NOT re-read or redeclare them) ===\n" + iface)
    parts.append("=== PLAN.md ===\n" + plan_md)
    parts.append("=== CONTRACT_REGISTRY.json ===\n" + contract)
    if journal_tail.strip():
        parts.append("=== BUILD JOURNAL (recent decisions) ===\n" + journal_tail)
    if skill_rel:
        parts.append(f"=== SKILL ===\nA backend-development skill is at "
                     f"`{skill_rel}/SKILL.md`. Read it for principles. Ignore "
                     "Node.js specifics — the backend is Python/FastAPI.")
    return "\n\n".join(parts)


def _wave_prompt(n: int, total: int, files: list[str], ctx: str) -> str:
    listing = "\n".join(f"- {f}" for f in files)
    return (
        f"TASK: Wave {n} of {total} of a phased build. Everything built so far is "
        "listed with accurate signatures in the INTERFACES section below — import "
        "from those, do NOT re-read or rewrite existing files. Implement ONLY the "
        "following manifest files now, completely, with production-quality code "
        "(no stubs, no TODOs):\n"
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


def _wave_gate(build_dir: Path, wave_files: list[str]) -> list[str]:
    """Syntax tripwire after a wave: every listed file must exist, and each .py
    must compile. Catches the wave-3 syntax bug that would otherwise poison
    waves 4-25 before end-loaded VERIFY sees it (#4). NOT a full import — a
    sibling may legitimately not exist yet; that's the integration gate's job."""
    errors: list[str] = []
    for f in wave_files:
        p = build_dir / f
        if not p.is_file():
            errors.append(f"{f}: MISSING (wave did not create it)")
        elif p.suffix == ".py":
            try:
                py_compile.compile(str(p), doraise=True)
            except py_compile.PyCompileError as e:
                errors.append(f"{f}: {str(e).splitlines()[-1][:200]}")
    return errors


def _gate_fix_prompt(errors: list[str], ctx: str) -> str:
    listing = "\n".join(f"- {e}" for e in errors)
    return (
        "TASK: The wave you just wrote has syntax errors or missing files. Fix "
        "ONLY these, completely, then stop. Do not touch other files; do not "
        "add features; do not narrate:\n"
        f"{listing}\n\n"
        f"{ctx}")


def _fix_prompt(last_error: str, ctx: str) -> str:
    return (
        "TASK: The build exists but verification failed. Fix exactly these "
        "errors, re-run the failing checks from code/backend, and stop when "
        "they pass. Do not rewrite working files; do not delete tests.\n"
        "=== VERIFICATION ERRORS ===\n"
        f"{last_error}\n\n"
        f"{ctx}")


def _module_integration_prompt(mname: str, test_rel: str, ctx: str) -> str:
    return (
        f"TASK: Integration pass for module '{mname}'. Its files and tests exist. "
        f"From code/backend run `python -m pytest {test_rel} -q` and fix EVERY "
        "failure in THIS module — imports, wiring, fixtures, test bugs — until "
        "clean. Do not touch other modules; do not add features; do not delete "
        "tests. Do not narrate.\n\n"
        f"{ctx}")


def _module_prefix(path_root: str) -> str:
    """path_root 'code/backend/app/users' -> import prefix 'app.users' (junit
    classname prefix for routing failures back to their module)."""
    return path_root.replace("code/backend/", "").strip("/").replace("/", ".")


def _failing_modules(build_dir: Path) -> dict[str, list[dict]]:
    """Route VERIFICATION_REPORT.json's structured pytest failures to owning
    modules by longest junit-classname prefix. {} when single-module or no data.
    '__unrouted__' collects failures no module claims (e.g. top-level conftest)."""
    if not (build_dir / "MODULES.json").is_file():
        return {}
    report = _read_json(build_dir / "VERIFICATION_REPORT.json", {})
    failures = report.get("checks", {}).get("pytest", {}).get("failures", [])
    from agents import architect
    prefixes = {m["name"]: _module_prefix(m["path_root"])
                for m in architect.load_modules(build_dir)}
    routed: dict[str, list[dict]] = {}
    for f in failures:
        cls = f.get("file", "")
        owner = next((n for n, p in sorted(prefixes.items(), key=lambda kv: -len(kv[1]))
                      if p and cls.startswith(p)), "__unrouted__")
        routed.setdefault(owner, []).append(f)
    return routed


def _module_context(build_dir: Path, module: dict, seams: dict, conventions: str,
                    module_plan: str, skill_rel: str | None) -> str:
    """Flat, per-MODULE context (M5): CONVENTIONS + only the seams of THIS
    module's dependencies + accurate on-disk interfaces of self+deps + this
    module's own plan + journal tail. The global plan is never embedded — this
    is what keeps context constant from module 1 to module 16."""
    deps = module.get("depends_on", [])
    parts = [_NN_RULES]
    if conventions.strip():
        parts.append("=== CONVENTIONS (obey exactly) ===\n" + conventions)
    dep_seams = {d: seams.get(d, []) for d in deps}
    if any(dep_seams.values()):
        parts.append("=== SEAMS YOU MAY IMPORT (from dependency modules — do NOT "
                     "redeclare these) ===\n" + json.dumps(dep_seams, indent=1))
    iface = interfaces.read_interfaces(build_dir, [module.get("name", ""), *deps])
    if iface.strip():
        parts.append("=== INTERFACES ALREADY ON DISK (accurate signatures — import "
                     "from these) ===\n" + iface)
    if module_plan.strip():
        parts.append(f"=== THIS MODULE ({module.get('name')}) PLAN ===\n" + module_plan)
    journal = progress.journal_tail(build_dir)
    if journal.strip():
        parts.append("=== BUILD JOURNAL (recent decisions) ===\n" + journal)
    if skill_rel:
        parts.append(f"=== SKILL ===\nBackend skill at `{skill_rel}/SKILL.md` — "
                     "principles only; ignore Node.js specifics.")
    return "\n\n".join(parts)


def _slug(label: str) -> str:
    return re.sub(r"[^\w]+", "_", label).strip("_")


def _read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""


def _read_json(p: Path, default):
    if not p.is_file():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def run_builder(build_dir: Path, last_error: str = "", log=lambda s: None) -> dict:
    """Dispatch the build. Single-module (PLAN.md) or multi-module (MODULES.json
    + PLAN_<module>.md, built in topological order) — each module's waves get a
    flat, per-module context. Interfaces regenerate from disk after every wave.
    Raises BuildDispatchFailed only when every lane is dead."""
    build_dir = Path(build_dir)

    skill_rel = None
    pool = skills_router.DEFAULT_POOL_ROOT
    if pool.is_dir():
        staged = skills_stage.stage(pool, build_dir)
        if (staged / "senior-backend" / "SKILL.md").is_file():
            skill_rel = "skills/senior-backend"

    used: list[str] = []
    started = time.monotonic()
    consec = {"claude": 0, "codex": 0}  # consecutive failures per lane this build
    wait_budget = [8 * 3600.0]          # seconds we may still park (M5 cap)
    _WAVE_MODEL = os.environ.get("ECHARA_WAVE_MODEL", "sonnet")  # tiering default
    _WAIT = os.environ.get("ECHARA_WAIT_ON_EXHAUST") == "1"

    def dispatch(label: str, prompt: str, model: str | None = None) -> None:
        """Run one pass on the first live lane. A lane is dead only after
        DEAD_AFTER consecutive failures (#9 — a single idle blip is a 60s
        cooldown, not permadeath). When all lanes are exhausted with a known
        reset and ECHARA_WAIT_ON_EXHAUST=1, park until the earliest reset (#10)."""
        from providers import PROVIDERS, availability
        (build_dir / f"BUILDER_PROMPT_{_slug(label)}.md").write_text(prompt, encoding="utf-8")
        DEAD_AFTER = 3
        while True:
            failures = []
            for name in ("claude", "codex"):  # codex = spot-fix fallback lane
                if consec[name] >= DEAD_AFTER:
                    failures.append(f"{name}: dead ({DEAD_AFTER} consecutive)")
                    continue
                avail = availability.status(name)
                if not avail.available:
                    failures.append(f"{name}: cooldown {int(avail.seconds_until_reset)}s")
                    continue
                tag = f" ({model})" if name == "claude" and model else ""
                log(f"builder: {label} -> {name}{tag}")
                prov = PROVIDERS[name](model=model) if name == "claude" else PROVIDERS[name]()
                result = prov.run(prompt, build_dir, ECHARA_ROOT / "logs", timeout_sec=1500)
                if result.ok:
                    consec[name] = 0
                    used.append(name)
                    return
                consec[name] += 1
                # Provider already marked a real quota reset; a plain blip (idle
                # kill / exit 1) gets a short cooldown so the NEXT pass retries it.
                if not result.rate_limit_retry_after_sec:
                    availability.mark_exhausted(name, time.time() + 60)
                failures.append(f"{name}: exit={result.exit_code} kill={result.kill_reason}")
                log(f"builder: {failures[-1]} (consec={consec[name]})")
            if _WAIT:
                resets = [availability.status(n).resets_at for n in ("claude", "codex")
                          if consec[n] < DEAD_AFTER and availability.status(n).resets_at]
                if resets:
                    wait = min(resets) - time.time()
                    if 0 < wait <= wait_budget[0]:
                        log(f"builder: all lanes exhausted — parking {int(wait)}s until reset")
                        time.sleep(wait + 5)
                        wait_budget[0] -= wait
                        continue
            raise BuildDispatchFailed(f"{label}: " + "; ".join(failures))

    def build_module(mname: str, module_dir: Path, plan_md: str, ctx_fn, prog: dict) -> int:
        """Wave over one module's files with per-wave gate + interface regen."""
        files = _implementation_order(plan_md)
        if not files:  # empty/foreign-path plan would dispatch a no-op wave (#5)
            raise BuildDispatchFailed(f"module {mname!r}: plan produced no file list")
        chunks = _waves(files) if len(files) > SINGLE_PASS_MAX else [files]
        passes = 0
        for i, chunk in enumerate(chunks):
            if all((build_dir / f).is_file() for f in chunk):
                log(f"builder: {mname} wave {i + 1}/{len(chunks)} on disk — skipped")
                continue
            label = f"{mname} w{i + 1}/{len(chunks)}"
            dispatch(label, _wave_prompt(i + 1, len(chunks), chunk, ctx_fn()), model=_WAVE_MODEL)
            interfaces.write_module_interface(build_dir, mname, module_dir)
            errors = _wave_gate(build_dir, chunk)
            if errors:
                log(f"builder: {label} gate — {len(errors)} issue(s)")
                if progress.can_fix(prog):
                    progress.record_fix(build_dir, prog, mname, "gate")  # persist BEFORE dispatch
                    dispatch(f"{label} gate-fix", _gate_fix_prompt(errors, ctx_fn()), model="opus")
                    interfaces.write_module_interface(build_dir, mname, module_dir)
                else:
                    log("builder: global fix budget exhausted — VERIFY will catch it")
            progress.journal_append(
                build_dir, f"{label}: {', '.join(Path(f).name for f in chunk)}"
                + (f" [gate:{len(errors)}]" if errors else ""))
            progress.module_state(prog, mname)["waves_done"] += 1
            progress.save(build_dir, prog)
            passes += 1
        return passes

    prog = progress.load(build_dir)
    is_multi = (build_dir / "MODULES.json").is_file()

    if last_error and is_multi:
        # Route the failure per-module (from junitxml) and fix each owner in its
        # own scoped context — lifetime budget ≤2 integration-fixes/module.
        from agents import architect
        seams = _read_json(build_dir / "SEAMS.json", {})
        conv = _read_text(build_dir / "CONVENTIONS.md")
        modules = {m["name"]: m for m in architect.load_modules(build_dir)}
        routed = _failing_modules(build_dir)
        real = {k: v for k, v in routed.items() if k in modules}
        n_passes = 0
        if real:
            for mname, fails in real.items():
                pm = progress.module_state(prog, mname)
                if pm.get("integration_fixes", 0) >= 2 or not progress.can_fix(prog):
                    log(f"builder: {mname} integration-fix budget spent — skip")
                    continue
                progress.record_fix(build_dir, prog, mname, "integration")
                err = "\n".join(f"{x['file']}::{x['test']}: {x['message']}" for x in fails)
                ctx = _module_context(build_dir, modules[mname], seams, conv, "", skill_rel)
                dispatch(f"{mname} fix", _fix_prompt(err, ctx), model="opus")
                interfaces.write_module_interface(build_dir, mname, build_dir / modules[mname]["path_root"])
                n_passes += 1
            progress.save(build_dir, prog)
        else:
            # Unrouted (e.g. import-smoke or shared conftest) — one whole-build fix.
            allmods = {"name": "__all__", "depends_on": list(modules)}
            dispatch("fix", _fix_prompt(last_error, _module_context(build_dir, allmods, seams, conv, "", skill_rel)), model="opus")
            n_passes = 1
    elif last_error:
        plan_md = _read_text(build_dir / "PLAN.md")
        contract = _read_text(build_dir / "CONTRACT_REGISTRY.json") or "{}"
        fix_ctx = _wave_context(build_dir, _DEFAULT_MODULE, [], plan_md, contract,
                                skill_rel, progress.journal_tail(build_dir))
        dispatch("fix", _fix_prompt(last_error, fix_ctx), model="opus")
        n_passes = 1
    elif is_multi:
        from agents import architect
        seams = _read_json(build_dir / "SEAMS.json", {})
        conventions = _read_text(build_dir / "CONVENTIONS.md")
        modules = {m["name"]: m for m in architect.load_modules(build_dir)}
        n_passes = 0
        for mname in architect.module_order(build_dir):  # deps first
            m = modules[mname]
            mroot = build_dir / m["path_root"]
            plan_path = build_dir / f"PLAN_{mname}.md"
            if not plan_path.is_file():
                raise BuildDispatchFailed(f"module {mname}: PLAN_{mname}.md missing")
            plan_md = plan_path.read_text(encoding="utf-8", errors="replace")

            def ctx_fn(m=m, plan_md=plan_md):
                return _module_context(build_dir, m, seams, conventions, plan_md, skill_rel)

            n_passes += build_module(mname, mroot, plan_md, ctx_fn, prog)
            pm = progress.module_state(prog, mname)

            # Per-module integration (scoped to this module's tests) — replaces
            # the single 30k-codebase integration session (#3). Budget ≤2/module.
            if (mroot / "tests").is_dir() and not pm.get("integrated") and progress.can_fix(prog):
                progress.record_fix(build_dir, prog, mname, "integration")
                test_rel = _module_prefix(m["path_root"]).replace(".", "/") + "/tests"
                dispatch(f"{mname} integrate",
                         _module_integration_prompt(mname, test_rel, ctx_fn()), model="opus")
                interfaces.write_module_interface(build_dir, mname, mroot)
                pm["integrated"] = True
                n_passes += 1

            # Deterministic seam check + ≤1 seam-fix/module.
            mism = interfaces.check_seams(build_dir, {mname: seams.get(mname, [])})
            if mism and pm.get("seam_fixes", 0) < 1 and progress.can_fix(prog):
                progress.record_fix(build_dir, prog, mname, "seam")
                dispatch(f"{mname} seam-fix",
                         _fix_prompt("Declared exports missing:\n" + "\n".join(mism), ctx_fn()), model="opus")
                interfaces.write_module_interface(build_dir, mname, mroot)
                n_passes += 1
            pm["seams_ok"] = not interfaces.check_seams(build_dir, {mname: seams.get(mname, [])})
            progress.save(build_dir, prog)
        # No final global integration — VERIFY is the cross-module gate; failures
        # route back per-module on retry.
    else:
        plan_md = _read_text(build_dir / "PLAN.md")
        contract = _read_text(build_dir / "CONTRACT_REGISTRY.json") or "{}"

        def ctx_fn():
            return _wave_context(build_dir, _DEFAULT_MODULE, [], plan_md, contract,
                                 skill_rel, progress.journal_tail(build_dir))

        n_passes = build_module(_DEFAULT_MODULE, build_dir / "code", plan_md, ctx_fn, prog)
        dispatch("integration", _integration_prompt(ctx_fn()), model="opus")
        n_passes += 1

    return {"provider": "+".join(sorted(set(used))) or "none", "waves": n_passes,
            "elapsed_sec": round(time.monotonic() - started, 2)}
