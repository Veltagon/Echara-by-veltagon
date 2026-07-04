"""Architect — one Opus session that decomposes a prompt into modules.

Produces the four second-brain artifacts (ARCHITECTURE.md, MODULES.json,
SEAMS.json, CONVENTIONS.md) that every downstream planner/wave reads. This is
the highest-leverage decision in the whole build — a bad boundary set poisons
everything — so it runs on the best model (claude, Opus via config) for its one
session, and its output is HARD-validated (module count, ≤3k budgets, acyclic
deps, seam completeness) with one error-fed retry before the human gate.
"""
from __future__ import annotations

import graphlib
import json
from pathlib import Path

from phases import AgentDispatchError

ECHARA_ROOT = Path(__file__).resolve().parent.parent
MIN_MODULES, MAX_MODULES = 4, 16
MAX_MODULE_LOC = 3000


class ArchitectFailed(AgentDispatchError):
    pass


_SYSTEM = """You are the ECHARA Architect. You decompose a software request into
a set of small, independently-buildable modules with machine-checkable seams.
You output structure, never code and never prose padding.

Non-negotiable rules:
- Between 4 and 16 modules. Each module has a loc_budget of AT MOST 3000 lines —
  split anything larger. A module is one coherent responsibility.
- Dependencies form a DAG (no cycles). A module may only depend on modules
  declared before it can be built.
- Every module gets a distinct path_root (the directory prefix it OWNS); no two
  modules share a path_root.
- SEAMS.json declares every symbol one module exports for others to import —
  this is the ONLY cross-module knowledge the builders get, so it must be
  complete and accurate.
- CONVENTIONS.md is ~1k tokens of hard constraints every builder re-reads
  (error pattern, dependency-injection style, naming, service/router split,
  auth approach, pagination shape). Constraints, not narrative."""


def _task(prompt: str, errors: list[str] | None = None) -> str:
    err = ""
    if errors:
        err = ("\n\nYOUR PREVIOUS OUTPUT WAS REJECTED. Fix exactly these and "
               "rewrite ALL FOUR files:\n- " + "\n- ".join(errors))
    return f"""USER REQUEST: {prompt}

Design a PRODUCTION-GRADE Python FastAPI + SQLite backend (plus a React+TS
frontend ONLY if the request asks for a UI). Write EXACTLY these four files into
the current directory with your file tool, then stop. Do not write any code.

1. ARCHITECTURE.md — module list with one-paragraph rationale each, and the
   dependency order.

2. MODULES.json — a JSON array, 4-16 entries, each:
   {{"name": "<short-id>", "kind": "backend"|"frontend",
     "loc_budget": <int <= 3000>, "depends_on": ["<module>", ...],
     "path_root": "code/backend/app/<feature>"}}
   Order them so every depends_on target appears earlier. Acyclic. Distinct
   path_roots. A shared "core" module (config, db session, security, deps,
   pagination, exceptions) with no dependencies is usually module 1.

3. SEAMS.json — a JSON object mapping each module name to the list of symbols
   it exports for OTHER modules to import:
   {{"core": [{{"name": "get_db", "signature": "def get_db() -> Session"}},
              {{"name": "Base", "signature": "class Base"}}], ...}}
   Include every symbol another module needs. A module that exports nothing gets
   an empty list.

4. CONVENTIONS.md — the hard constraints every builder must follow (error
   handling pattern, DI via FastAPI Depends, one SQLAlchemy Base in core,
   services hold all DB logic, thin routers, bcrypt-direct auth, pagination
   response shape, absolute imports, __init__.py in every package).

FRONTEND: if the request asks for a UI, ALSO include frontend module(s) in
MODULES.json (kind "frontend", path_root under "code/frontend"), and write a
FIFTH file CONTRACT_REGISTRY.json describing the API the frontend consumes:
{{"api_endpoints": [{{"method","path","request_schema","response_schema",
"auth_required"}}], "shared_types": [{{"name","fields","required"}}],
"db_tables": [], "env_vars": [], "dependencies": []}}. The frontend client is
generated from this file — it must be complete. If the request is backend-only,
do NOT create a frontend module.{err}"""


def validate_architecture(build_dir: Path) -> list[str]:
    """Hard validation. Empty list = valid + acyclic."""
    errors: list[str] = []
    for name, floor in (("ARCHITECTURE.md", 200), ("CONVENTIONS.md", 100)):
        p = build_dir / name
        if not p.is_file() or len(p.read_text(encoding="utf-8", errors="replace")) < floor:
            errors.append(f"{name} missing or too short")

    mpath = build_dir / "MODULES.json"
    if not mpath.is_file():
        return errors + ["MODULES.json missing"]
    try:
        modules = json.loads(mpath.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return errors + [f"MODULES.json invalid JSON: {e}"]
    if not isinstance(modules, list):
        return errors + ["MODULES.json must be a JSON array"]
    if not (MIN_MODULES <= len(modules) <= MAX_MODULES):
        errors.append(f"need {MIN_MODULES}-{MAX_MODULES} modules, got {len(modules)}")

    names, roots, graph = set(), set(), {}
    for i, m in enumerate(modules):
        if not isinstance(m, dict):
            errors.append(f"module[{i}] is not an object")
            continue
        name = m.get("name")
        if not name or name in names:
            errors.append(f"module[{i}] name missing or duplicated: {name!r}")
            continue
        names.add(name)
        if m.get("kind") not in ("backend", "frontend"):
            errors.append(f"{name}: kind must be backend|frontend")
        budget = m.get("loc_budget")
        if not isinstance(budget, int) or not (1 <= budget <= MAX_MODULE_LOC):
            errors.append(f"{name}: loc_budget must be int 1..{MAX_MODULE_LOC}")
        root = m.get("path_root")
        if not root:
            errors.append(f"{name}: path_root missing")
        elif root in roots:
            errors.append(f"{name}: path_root {root!r} shared with another module")
        else:
            roots.add(root)
        graph[name] = list(m.get("depends_on", []))

    for name, deps in graph.items():
        for d in deps:
            if d == name:
                errors.append(f"{name}: depends on itself")
            elif d not in names:
                errors.append(f"{name}: depends on unknown module {d!r}")
    try:
        graphlib.TopologicalSorter(graph).prepare()
    except graphlib.CycleError as e:
        errors.append(f"dependency cycle: {e.args[1] if len(e.args) > 1 else e}")

    spath = build_dir / "SEAMS.json"
    if not spath.is_file():
        errors.append("SEAMS.json missing")
    else:
        try:
            seams = json.loads(spath.read_text(encoding="utf-8"))
            if not isinstance(seams, dict):
                errors.append("SEAMS.json must be a JSON object")
            else:
                for k in seams:
                    if k not in names:
                        errors.append(f"SEAMS.json references unknown module {k!r}")
        except json.JSONDecodeError as e:
            errors.append(f"SEAMS.json invalid JSON: {e}")
    return errors


def module_order(build_dir: Path) -> list[str]:
    """Topological build order of module names (deps first)."""
    modules = json.loads((build_dir / "MODULES.json").read_text(encoding="utf-8"))
    graph = {m["name"]: list(m.get("depends_on", [])) for m in modules}
    return list(graphlib.TopologicalSorter(graph).static_order())


def load_modules(build_dir: Path) -> list[dict]:
    return json.loads((build_dir / "MODULES.json").read_text(encoding="utf-8"))


def run_architect(prompt: str, build_dir: Path, log=lambda s: None) -> dict:
    """Produce + validate the four architecture artifacts. Raises ArchitectFailed
    if invalid after one retry."""
    from providers import PROVIDERS, availability

    build_dir = Path(build_dir)
    errors: list[str] | None = None
    for attempt in (1, 2):
        prompt_text = _SYSTEM + "\n\n" + _task(prompt, errors) + (
            "\n\nWrite the four files into the current directory now. Do not narrate.")
        dispatched = False
        for name in ("claude", "codex"):
            if not availability.is_available(name):
                continue
            log(f"architect: attempt {attempt} -> {name}")
            result = PROVIDERS[name]().run(prompt_text, build_dir, ECHARA_ROOT / "logs",
                                           timeout_sec=900)
            if result.ok:
                dispatched = True
                break
        if not dispatched:
            raise ArchitectFailed("no live lane for the architect")
        errors = validate_architecture(build_dir)
        if not errors:
            n = len(load_modules(build_dir))
            return {"model": "claude (opus)", "modules": n, "attempts": attempt}
        log(f"architect: rejected — {errors}")
    raise ArchitectFailed(f"invalid architecture after 2 attempts: {errors}")
