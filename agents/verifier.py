"""Agent 3 — Verifier. Deterministic code, NO model calls, by design.

Runs 3 checks in order against build_dir/code and writes
VERIFICATION_REPORT.json:
  1. import smoke   — `python -c "from app.main import app"` in a provisioned venv
  2. alembic upgrade — `upgrade head` on a throwaway SQLite via the S-29 wrapper
                       (vacuously passes when the project has no alembic scaffold)
  3. pytest          — `-q --tb=short` with a py_compile prescan that --ignores
                       syntax-broken test files; waits for the repair barrier first

Verdict is a plain boolean: all three pass -> verified true. Any failure ->
verified false with the exact error output (capped 800 chars/stream). No
scores, no LLM judging.
"""
from __future__ import annotations

import json
import py_compile
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from agents.repairs import _backend_root, _wait_for_repair_barrier

_CAP = 800  # chars per stream in details — no 100MB logs in memory

# S-29 wrapper: strip cwd from sys.path FIRST so backend/alembic/ can't shadow
# pip's alembic, THEN add backend for `from app... import Base`, then upgrade
# against a throwaway SQLite so the real DB is never touched.
_ALEMBIC_WRAPPER = """\
import os, sys
sys.path = [p for p in sys.path if p not in ('', os.getcwd())]
from alembic import command
from alembic.config import Config
sys.path.insert(0, {backend!r})
db = {db!r}
if os.path.exists(db):
    os.remove(db)
cfg = Config({ini!r})
cfg.set_main_option('script_location', {script_loc!r})
cfg.set_main_option('sqlalchemy.url', 'sqlite:///' + db.replace('\\\\', '/'))
command.upgrade(cfg, 'head')
print('alembic upgrade head: ok')
"""


def _run(argv: list[str], cwd: Path, timeout: int) -> tuple[bool, str, float]:
    started = time.monotonic()
    try:
        p = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return False, f"timeout after {timeout}s: {e}", round(time.monotonic() - started, 1)
    detail = ((p.stdout or "")[-_CAP:] + "\n" + (p.stderr or "")[-_CAP:]).strip()
    return p.returncode == 0, f"[exit {p.returncode}] {detail}", round(time.monotonic() - started, 1)


def _provision_venv(build_dir: Path, backend: Path) -> tuple[str, str]:
    """(python_path, note). Venv at build_dir/.verify_venv with the project's
    requirements.txt + the verify toolchain. Re-provisions when requirements
    change (Builder retries can edit it).

    A killed process can leave a corrupt half-venv (python present but pip
    broken); the first `pip install` then fails and — before this — we silently
    fell back to the orchestrator interpreter, verifying against ITS deps
    instead of the project's pinned ones (seen 2026-07-04). So on the first
    failure we wipe the venv and rebuild ONCE from scratch; only if that also
    fails do we fall back so verification still runs."""
    venv = build_dir / ".verify_venv"
    py = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    reqs = backend / "requirements.txt"
    reqs_text = reqs.read_text(encoding="utf-8", errors="replace") if reqs.is_file() else ""
    stamp = venv / ".reqs_stamp"

    last_err = None
    for clean_rebuild in (False, True):
        try:
            if clean_rebuild and venv.exists():
                shutil.rmtree(venv, ignore_errors=True)
            if not py.exists():
                subprocess.run([sys.executable, "-m", "venv", str(venv)],
                               check=True, capture_output=True, timeout=120)
            need_install = clean_rebuild or not stamp.exists() \
                or stamp.read_text(encoding="utf-8") != reqs_text
            if need_install:
                argv = [str(py), "-m", "pip", "install", "--quiet",
                        "pytest", "pytest-asyncio", "pytest-timeout", "httpx", "alembic"]
                if reqs.is_file() and reqs_text.strip():
                    argv += ["-r", str(reqs)]
                else:
                    # No usable requirements.txt (E3-v2 shipped a 0-byte one, so
                    # NOTHING installed -> app can't import -> a false verify
                    # failure on 877-passing code). Install a base FastAPI stack so
                    # the app still imports and the tests can actually run.
                    argv += ["fastapi", "pydantic", "pydantic-settings", "sqlalchemy",
                             "python-jose[cryptography]", "pyjwt", "python-multipart",
                             "bcrypt", "email-validator"]
                # If a build pins an old Rust-based dep with no wheel for a very
                # new runtime Python (2026-07-04: pydantic 2.9 / pydantic-core
                # uses PyUnicode_DATA, removed in the Py3.14 C API — unbuildable
                # from source at ALL), this fails fast at pip's resolver and we
                # fall back to the orchestrator interpreter. NN-DEP-1 steers
                # builders to wheel-available pins to avoid it.
                subprocess.run(argv, check=True, capture_output=True, timeout=600)
                stamp.write_text(reqs_text, encoding="utf-8")
            return str(py), "provisioned venv" + (" (rebuilt)" if clean_rebuild else "")
        except (subprocess.SubprocessError, OSError) as e:
            last_err = e
    return sys.executable, f"venv provisioning failed ({last_err!r:.120}) — using orchestrator python"


_APP_SKIP = frozenset({"__pycache__", ".verify_venv", ".venv", "node_modules", "alembic"})


def _discover_app_import(backend: Path) -> str:
    """The import that loads the ASGI app. The Architect is free to place it at
    app/main.py, bootstrap/main.py, api/main.py, ... — don't hardcode it (E3-v2
    put it at bootstrap/main.py, so `from app.main import app` false-failed a
    working app). Find the file that instantiates FastAPI() and derive its dotted
    import; prefer a file named main.py."""
    best = None
    for src in sorted(backend.rglob("*.py")):
        if _APP_SKIP & set(src.relative_to(backend).parts):
            continue
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # A module-level ASGI app var at column 0: 'app = ...', 'app: FastAPI = ...',
        # or 'application = ...'. Covers both `app = FastAPI()` and the common
        # factory pattern `app: FastAPI = create_app()` (E3-v2's bootstrap/main.py).
        m = re.search(r"(?m)^(app|application)\b[^=\n]*=(?!=)", text)
        if not m:
            continue
        dotted = src.relative_to(backend).with_suffix("").as_posix().replace("/", ".")
        stmt = f"from {dotted} import {m.group(1)}"
        if src.name == "main.py":
            return stmt
        best = best or stmt
    return best or "from app.main import app"


def _check_import(py: str, backend: Path) -> dict:
    stmt = _discover_app_import(backend)
    ok, detail, elapsed = _run([py, "-c", stmt], backend, 60)
    return {"passed": ok, "detail": (detail if ok else f"[{stmt}] {detail}"),
            "elapsed_sec": elapsed}


def _check_alembic(py: str, backend: Path, build_dir: Path) -> dict:
    ini = backend / "alembic.ini"
    if not ini.is_file():
        return {"passed": True, "detail": "no alembic scaffold — skipped (create_all path)",
                "elapsed_sec": 0.0}
    script = _ALEMBIC_WRAPPER.format(
        backend=str(backend), db=str(build_dir / ".verify_alembic.db"),
        ini=str(ini), script_loc=str(backend / "alembic"),
    )
    ok, detail, elapsed = _run([py, "-c", script], backend, 120)
    return {"passed": ok, "detail": detail, "elapsed_sec": elapsed}


def _test_files(backend: Path) -> list[Path]:
    return [f for f in backend.rglob("test_*.py") if "__pycache__" not in f.parts]


def _count_tests(files: list[Path]) -> int:
    n = 0
    for f in files:
        try:
            n += f.read_text(encoding="utf-8", errors="replace").count("def test_")
        except OSError:
            pass
    return n


def _parse_junit(xml_path: Path) -> list[dict]:
    """Structured per-test failures from junitxml (fixes #8: at 600 tests the
    800-char stdout tail is summary counters, not tracebacks — routing needs
    real per-test data). classname is the dotted module path -> maps to a file."""
    if not xml_path.is_file():
        return []
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError:
        return []
    fails = []
    for tc in root.iter("testcase"):
        fe = tc.find("failure")
        if fe is None:
            fe = tc.find("error")
        if fe is not None:
            msg = (fe.get("message", "") or (fe.text or "")).strip()
            fails.append({"file": tc.get("classname", ""), "test": tc.get("name", ""),
                          "message": msg[:400]})
    return fails


def _check_pytest(py: str, backend: Path, code_dir: Path, build_dir: Path) -> dict:
    files = _test_files(backend)  # recursive — multi-module tests live per-module
    if not files:
        return {"passed": False, "elapsed_sec": 0.0, "failures": [],
                "detail": "no test files — the plan requires tests and none were written"}
    ignores, notes = [], []
    for f in files:
        try:
            py_compile.compile(str(f), doraise=True)
        except py_compile.PyCompileError as e:
            ignores += [f"--ignore={f}"]
            notes.append(f"py_compile skip {f.name}: {str(e)[:120]}")
    _wait_for_repair_barrier(code_dir)  # never pytest against half-fsynced files
    n_tests = _count_tests(files)
    timeout = min(1800, 60 + 2 * n_tests)  # #7: real bcrypt × auth matrix is slow
    junit = build_dir / "verify_junit.xml"
    junit.unlink(missing_ok=True)
    ok, detail, elapsed = _run(
        [py, "-m", "pytest", "-q", "--tb=short", "--timeout=45", "--timeout-method=thread",
         f"--junitxml={junit}", *ignores],
        backend, timeout)
    failures = _parse_junit(junit)
    if "no tests ran" in detail or "[exit 5]" in detail:
        ok = False
        detail = "no tests collected — " + detail
    if notes:
        detail = "; ".join(notes) + "\n" + detail
    return {"passed": ok, "detail": detail, "elapsed_sec": elapsed,
            "n_tests": n_tests, "failures": failures}


def _check_frontend(build_dir: Path) -> dict | None:
    """None when there is no frontend. Otherwise: npm install (cached) ->
    tsc --noEmit -> vite build, all must pass (M5 frontend DoD)."""
    fe = build_dir / "code" / "frontend"
    if not fe.is_dir() or not (fe / "package.json").is_file():
        return None
    npm, npx = shutil.which("npm"), shutil.which("npx")
    if not npm or not npx:
        return {"passed": False, "detail": "node/npm not on PATH", "elapsed_sec": 0.0}
    elapsed = 0.0
    if not (fe / "node_modules").is_dir():  # cached across retries
        ok, detail, e = _run([npm, "install", "--no-audit", "--no-fund"], fe, 900)
        elapsed += e
        if not ok:
            return {"passed": False, "detail": "npm install failed\n" + detail, "elapsed_sec": elapsed}
    ok, d1, e1 = _run([npx, "tsc", "--noEmit"], fe, 300)
    elapsed += e1
    if not ok:
        return {"passed": False, "detail": "tsc --noEmit\n" + d1, "elapsed_sec": elapsed}
    ok, d2, e2 = _run([npx, "vite", "build"], fe, 300)
    elapsed += e2
    return {"passed": ok, "detail": ("vite build\n" + d2) if not ok else "tsc + vite build ok",
            "elapsed_sec": elapsed}


def verify(build_dir: Path) -> dict:
    """Run the backend checks (+ frontend when present); write and return
    VERIFICATION_REPORT.json."""
    build_dir = Path(build_dir)
    code_dir = build_dir / "code"
    backend = _backend_root(code_dir)
    py, venv_note = _provision_venv(build_dir, backend)

    checks = {"import_smoke": _check_import(py, backend)}
    checks["alembic_upgrade"] = (
        _check_alembic(py, backend, build_dir) if checks["import_smoke"]["passed"]
        else {"passed": False, "detail": "skipped: import smoke failed", "elapsed_sec": 0.0})
    checks["pytest"] = (
        _check_pytest(py, backend, code_dir, build_dir) if checks["import_smoke"]["passed"]
        else {"passed": False, "detail": "skipped: import smoke failed", "elapsed_sec": 0.0})

    frontend = _check_frontend(build_dir)  # None when there is no frontend
    if frontend is not None:
        checks["frontend"] = frontend

    report = {
        "verified": all(c["passed"] for c in checks.values()),
        "checks": checks,
        "interpreter": py,
        "venv_note": venv_note,
    }
    (build_dir / "VERIFICATION_REPORT.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    return report


def failure_summary(report: dict) -> str:
    """The exact error text fed back to the Builder on retry."""
    parts = [f"{name}: {c['detail']}" for name, c in report["checks"].items()
             if not c["passed"]]
    return "\n\n".join(parts)[:4000]
