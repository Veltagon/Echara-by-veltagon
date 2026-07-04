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
import shutil
import subprocess
import sys
import time
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
                        "pytest", "pytest-asyncio", "httpx", "alembic"]
                if reqs.is_file():
                    argv += ["-r", str(reqs)]
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


def _check_import(py: str, backend: Path) -> dict:
    ok, detail, elapsed = _run([py, "-c", "from app.main import app"], backend, 60)
    return {"passed": ok, "detail": detail, "elapsed_sec": elapsed}


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


def _check_pytest(py: str, backend: Path, code_dir: Path) -> dict:
    tests = backend / "tests"
    if not tests.is_dir():
        return {"passed": False, "elapsed_sec": 0.0,
                "detail": "no tests/ directory — the plan requires tests and none were written"}
    ignores, notes = [], []
    for f in sorted(tests.glob("test_*.py")):
        try:
            py_compile.compile(str(f), doraise=True)
        except py_compile.PyCompileError as e:
            ignores += [f"--ignore={f}"]
            notes.append(f"py_compile skip {f.name}: {str(e)[:120]}")
    _wait_for_repair_barrier(code_dir)  # never pytest against half-fsynced files
    ok, detail, elapsed = _run(
        [py, "-m", "pytest", "tests/", "-q", "--tb=short", *ignores], backend, 300)
    if "no tests ran" in detail or "[exit 5]" in detail:
        ok = False
        detail = "no tests collected — " + detail
    if notes:
        detail = "; ".join(notes) + "\n" + detail
    return {"passed": ok, "detail": detail, "elapsed_sec": elapsed}


def verify(build_dir: Path) -> dict:
    """Run all 3 checks; write and return VERIFICATION_REPORT.json."""
    build_dir = Path(build_dir)
    code_dir = build_dir / "code"
    backend = _backend_root(code_dir)
    py, venv_note = _provision_venv(build_dir, backend)

    checks = {"import_smoke": _check_import(py, backend)}
    checks["alembic_upgrade"] = (
        _check_alembic(py, backend, build_dir) if checks["import_smoke"]["passed"]
        else {"passed": False, "detail": "skipped: import smoke failed", "elapsed_sec": 0.0})
    checks["pytest"] = (
        _check_pytest(py, backend, code_dir) if checks["import_smoke"]["passed"]
        else {"passed": False, "detail": "skipped: import smoke failed", "elapsed_sec": 0.0})

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
