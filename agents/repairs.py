"""Deterministic repair pass (the REPAIR phase). NOT an agent — no model calls.

Ports the 9 proven V1 repairs described in HELP.md section 1. Contract for
every repair function:
  - signature: repair(code_dir: Path) -> list[str]   (actions taken; [] = no-op)
  - idempotent: re-running on repaired code does nothing
  - gated: set ECHARA_REPAIR_<FLAG>=0 to skip
  - AST-validates any .py it writes (never writes unparseable code)

`repair_all` is the dispatcher: writes `.repairs_pending`, runs every repair
(one crashing repair never kills the phase), fsyncs touched files, renames the
marker to `.repairs_complete` (the barrier V1's B16 false-failed without —
Path.write_text closes but doesn't fsync; on NTFS a subprocess can read stale
bytes).
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path


# --- shared helpers ----------------------------------------------------------

def _enabled(flag: str) -> bool:
    return os.environ.get(f"ECHARA_REPAIR_{flag}", "1") != "0"


def _backend_root(code_dir: Path) -> Path:
    """Where `app/` lives: code/backend/ if present, else code/ itself."""
    b = code_dir / "backend"
    return b if (b / "app").is_dir() else code_dir


def _ast_ok(src: str) -> bool:
    try:
        ast.parse(src)
        return True
    except SyntaxError:
        return False


def _write_py(path: Path, src: str) -> bool:
    """Write a .py file only if it parses. Returns True on write."""
    if not _ast_ok(src):
        return False
    path.write_text(src, encoding="utf-8")
    return True


def _tests_dir(code_dir: Path) -> Path | None:
    for cand in (_backend_root(code_dir) / "tests", code_dir / "tests"):
        if cand.is_dir():
            return cand
    return None


def _alembic_versions(code_dir: Path) -> Path | None:
    for cand in (_backend_root(code_dir) / "alembic" / "versions",
                 code_dir / "alembic" / "versions"):
        if cand.is_dir():
            return cand
    return None


# --- D4: duplicate create_table across the migration chain -------------------

def _migration_meta(path: Path):
    """(revision, down_revision, [created tables]) or None if unparseable."""
    src = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    rev = down = _MISSING = object()
    rev, down = _MISSING, _MISSING
    creates: list[str] = []
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets, value = [node.target.id], node.value
        else:
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "create_table"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "op"
                    and node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                creates.append(node.args[0].value)
            continue
        if value is None or not isinstance(value, ast.Constant):
            continue
        if "revision" in targets and rev is _MISSING:
            rev = value.value
        if "down_revision" in targets:
            down = value.value
    if rev is _MISSING:
        return None
    return (rev, None if down is _MISSING else down, creates)


def repair_alembic_migration_chain(code_dir: Path) -> list[str]:
    """D4 — second migration re-creates a table the first already created."""
    if not _enabled("ALEMBIC_CHAIN_DUPLICATE_CREATE"):
        return []
    versions = _alembic_versions(code_dir)
    if versions is None:
        return []
    metas = {}
    for f in sorted(versions.glob("*.py")):
        m = _migration_meta(f)
        if m:
            metas[m[0]] = (f, m[1], m[2])
    roots = [r for r, (_, down, _) in metas.items() if down is None]
    if len(roots) != 1:  # multiple/zero roots are a different problem's job
        return []
    children: dict = {}
    for r, (_, down, _) in metas.items():
        if down is not None:
            children.setdefault(down, []).append(r)
    actions: list[str] = []
    created: set[str] = set()
    rev = roots[0]
    while rev is not None:
        f, _, creates = metas[rev]
        src = f.read_text(encoding="utf-8", errors="replace")
        changed = False
        for tbl in creates:
            if tbl in created and f'DROP TABLE IF EXISTS {tbl}' not in src:
                lines = src.splitlines()
                pat = re.compile(r"op\.create_table\(\s*['\"]" + re.escape(tbl) + r"['\"]")
                for i, line in enumerate(lines):
                    if pat.search(line):
                        indent = line[: len(line) - len(line.lstrip())]
                        lines.insert(i, f'{indent}op.execute("DROP TABLE IF EXISTS {tbl}")')
                        src = "\n".join(lines) + ("\n" if src.endswith("\n") else "")
                        changed = True
                        break
            created.add(tbl)
        if changed and _write_py(f, src):
            actions.append(f"D4: injected DROP TABLE IF EXISTS in {f.name}")
        kids = children.get(rev, [])
        rev = kids[0] if len(kids) == 1 else None  # linear chain only
    return actions


# --- D6: FastAPI validation `detail` is a list, not a dict -------------------

def repair_fastapi_validation_loc_shape(code_dir: Path) -> list[str]:
    if not _enabled("FASTAPI_VALIDATION_LOC_SHAPE"):
        return []
    tests = _tests_dir(code_dir)
    if tests is None:
        return []
    pat = re.compile(r'(\.json\(\)\s*\[\s*["\']detail["\']\s*\])\s*(\[\s*["\']\w+["\']\s*\])')
    actions = []
    for f in sorted(tests.glob("*.py")):
        src = f.read_text(encoding="utf-8", errors="replace")
        if "TestClient" not in src:
            continue
        fixed = pat.sub(r"\1[0]\2", src)
        if fixed != src and _write_py(f, fixed):
            actions.append(f"D6: fixed detail[0] shape in {f.name}")
    return actions


# --- S-28a: alembic scaffold (env.py missing next to alembic.ini) ------------

_ENV_PY_SYNC = '''\
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
try:
    from app.db import Base
except ImportError:
    from app.models.base import Base
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"),
                      target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}),
                                     prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
'''

# ponytail: async env.py template omitted — S-28a only fires when alembic.ini
# exists with no env.py, and every async project scaffolded by `alembic init
# -t async` already has one. Add _ENV_PY_ASYNC when a real build hits it.


def repair_alembic_scaffold(code_dir: Path) -> list[str]:
    if not _enabled("ALEMBIC_SCAFFOLD"):
        return []
    actions = []
    for root in (_backend_root(code_dir), code_dir):
        ini = root / "alembic.ini"
        if not ini.is_file():
            continue
        adir = root / "alembic"
        adir.mkdir(exist_ok=True)
        (adir / "versions").mkdir(exist_ok=True)
        env = adir / "env.py"
        if not env.is_file():
            if _write_py(env, _ENV_PY_SYNC):
                actions.append(f"S-28a: wrote {env.relative_to(code_dir)}")
        # script_location must be cwd-independent
        ini_src = ini.read_text(encoding="utf-8", errors="replace")
        fixed = re.sub(r"(?m)^script_location\s*=.*$",
                       "script_location = %(here)s/alembic", ini_src)
        if fixed != ini_src:
            ini.write_text(fixed, encoding="utf-8")
            actions.append("S-28a: fixed script_location to %(here)s/alembic")
        break
    return actions


# --- S-28b: pytest asyncio mode ----------------------------------------------

def repair_pytest_asyncio(code_dir: Path) -> list[str]:
    if not _enabled("PYTEST_ASYNCIO"):
        return []
    tests = _tests_dir(code_dir)
    if tests is None:
        return []
    uses_async = any("async def" in f.read_text(encoding="utf-8", errors="replace")
                     for f in tests.glob("*.py"))
    if not uses_async:
        return []
    root = _backend_root(code_dir)
    ini = root / "pytest.ini"
    actions = []
    if ini.is_file():
        src = ini.read_text(encoding="utf-8", errors="replace")
        fixed = src.replace("[tool:pytest]", "[pytest]")  # tool: header is ignored in pytest.ini
        if "asyncio_mode" not in fixed:
            fixed = fixed.rstrip() + "\nasyncio_mode = auto\n"
        if fixed != src:
            ini.write_text(fixed, encoding="utf-8")
            actions.append("S-28b: fixed pytest.ini asyncio config")
    else:
        ini.write_text("[pytest]\nasyncio_mode = auto\n", encoding="utf-8")
        actions.append("S-28b: wrote pytest.ini with asyncio_mode = auto")
    return actions


# --- ruff autofix -------------------------------------------------------------

def repair_ruff_autofix(code_dir: Path) -> list[str]:
    if not _enabled("RUFF_AUTOFIX") or shutil.which("ruff") is None:
        return []
    app_dir = _backend_root(code_dir) / "app"
    if not app_dir.is_dir():
        return []
    try:
        subprocess.run(["ruff", "check", "--fix", "--quiet", str(app_dir)],
                       capture_output=True, timeout=60)
        return ["ruff: check --fix ran"]
    except (subprocess.TimeoutExpired, OSError):
        return []


# --- test_migrations stderr capture -------------------------------------------

class _RunCallWrapper(ast.NodeTransformer):
    """Wrap bare `subprocess.run(..., check=True)` statements in try/except
    that re-raises with captured stdout/stderr."""

    def __init__(self):
        self.changed = False

    def visit_Expr(self, node: ast.Expr):
        call = node.value
        if not (isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute) and call.func.attr == "run"
                and isinstance(call.func.value, ast.Name) and call.func.value.id == "subprocess"
                and any(k.arg == "check" for k in call.keywords)):
            return node
        kw_names = {k.arg for k in call.keywords}
        if "capture_output" not in kw_names:
            call.keywords.append(ast.keyword("capture_output", ast.Constant(True)))
        if "text" not in kw_names:
            call.keywords.append(ast.keyword("text", ast.Constant(True)))
        handler_body = ast.parse(
            'raise AssertionError(f"migration command failed\\n'
            'stdout: {_e.stdout}\\nstderr: {_e.stderr}") from _e'
        ).body
        wrapped = ast.Try(
            body=[node],
            handlers=[ast.ExceptHandler(
                type=ast.parse("subprocess.CalledProcessError").body[0].value,
                name="_e", body=handler_body)],
            orelse=[], finalbody=[],
        )
        self.changed = True
        return ast.copy_location(wrapped, node)


def repair_test_migrations_capture_stderr(code_dir: Path) -> list[str]:
    if not _enabled("TEST_MIGRATIONS_CAPTURE_STDERR"):
        return []
    tests = _tests_dir(code_dir)
    if tests is None:
        return []
    f = tests / "test_migrations.py"
    if not f.is_file():
        return []
    src = f.read_text(encoding="utf-8", errors="replace")
    if "capture_output=True" in src and "CalledProcessError" in src:
        return []  # already repaired
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    w = _RunCallWrapper()
    tree = w.visit(tree)
    if not w.changed:
        return []
    ast.fix_missing_locations(tree)
    if _write_py(f, ast.unparse(tree)):
        return ["stderr-capture: wrapped subprocess.run in test_migrations.py"]
    return []


# --- missing `client` fixture --------------------------------------------------

_CLIENT_FIXTURE = '''\

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite:///" + tempfile.mkstemp(suffix=".db")[1])
from app.main import app  # noqa: E402


@pytest.fixture
def client():
    # TestClient as context manager runs the lifespan (tables get created).
    with TestClient(app) as c:
        yield c
'''


def repair_missing_client_fixture(code_dir: Path) -> list[str]:
    if not _enabled("MISSING_CLIENT_FIXTURE"):
        return []
    tests = _tests_dir(code_dir)
    if tests is None:
        return []
    wants_client = any(
        re.search(r"def test_\w+\([^)]*\bclient\b", f.read_text(encoding="utf-8", errors="replace"))
        for f in tests.glob("test_*.py")
    )
    if not wants_client:
        return []
    conftest = tests / "conftest.py"
    existing = conftest.read_text(encoding="utf-8", errors="replace") if conftest.is_file() else ""
    if re.search(r"def\s+client\s*\(", existing):
        return []
    merged = existing + _CLIENT_FIXTURE
    if _write_py(conftest, merged):
        return ["client-fixture: synthesized `client` in tests/conftest.py"]
    return []


# --- health endpoint path vs contract ------------------------------------------

def repair_health_endpoint_path(code_dir: Path) -> list[str]:
    if not _enabled("HEALTH_ENDPOINT_PATH"):
        return []
    contract = None
    for cand in (code_dir.parent / "CONTRACT_REGISTRY.json", code_dir / "CONTRACT_REGISTRY.json"):
        if cand.is_file():
            try:
                contract = json.loads(cand.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return []
            break
    if not contract:
        return []
    want = next((ep.get("path") for ep in contract.get("api_endpoints", [])
                 if "health" in str(ep.get("path", ""))), None)
    if not want:
        return []
    app_dir = _backend_root(code_dir) / "app"
    pat = re.compile(r'(@(?:app|router)\.get\(\s*["\'])(/[\w/\-]*health[\w/\-]*)(["\'])')
    actions = []
    for f in sorted(app_dir.rglob("*.py")) if app_dir.is_dir() else []:
        src = f.read_text(encoding="utf-8", errors="replace")
        fixed = pat.sub(lambda m: m.group(1) + want + m.group(3) if m.group(2) != want else m.group(0), src)
        if fixed != src and _write_py(f, fixed):
            actions.append(f"health-path: {f.name} aligned to contract {want}")
    return actions


# --- canonical filenames --------------------------------------------------------

_CANONICAL = {
    "backend/requirement": "backend/requirements.txt",
    "backend/requirements": "backend/requirements.txt",
    "requirement": "requirements.txt",
    "frontend/package": "frontend/package.json",
}


def repair_canonical_filenames(code_dir: Path) -> list[str]:
    if not _enabled("CANONICAL_FILENAMES"):
        return []
    actions = []
    for src_rel, dst_rel in _CANONICAL.items():
        src, dst = code_dir / src_rel, code_dir / dst_rel
        if src.is_file() and not dst.exists():
            src.rename(dst)
            actions.append(f"canonical: {src_rel} -> {dst_rel}")
    return actions


# --- passlib + modern bcrypt incompatibility -------------------------------------

def repair_passlib_bcrypt_compat(code_dir: Path) -> list[str]:
    """passlib 1.7.4 (unmaintained) is broken with bcrypt >= 4.1: its backend
    self-test crashes at load ('module bcrypt has no attribute __about__' /
    'password cannot be longer than 72 bytes'), so EVERY hash call dies and the
    error text misleads builders into truncating passwords instead of pinning.
    Seen twice on 2026-07-03 (auth eval runs 1+2; run 2 burned all 3 retries on
    it). If requirements pulls passlib, pin bcrypt==4.0.1 — the last release
    passlib works with."""
    if not _enabled("PASSLIB_BCRYPT_COMPAT"):
        return []
    reqs = _backend_root(code_dir) / "requirements.txt"
    if not reqs.is_file():
        return []
    lines = reqs.read_text(encoding="utf-8").splitlines()
    uses_passlib = any(re.match(r"\s*passlib", ln, re.IGNORECASE) for ln in lines)
    if not uses_passlib:
        return []
    out, pinned, changed = [], False, False
    for ln in lines:
        if re.match(r"\s*bcrypt\s*$", ln):  # bare, unpinned
            out.append("bcrypt==4.0.1")
            pinned, changed = True, True
        else:
            out.append(ln)
            if re.match(r"\s*bcrypt[=<>!]", ln):
                pinned = True  # already pinned somehow — leave it
    if not pinned:  # passlib[bcrypt] pulls bcrypt transitively — pin explicitly
        out.append("bcrypt==4.0.1")
        changed = True
    if not changed:
        return []
    reqs.write_text("\n".join(out) + "\n", encoding="utf-8")
    return ["passlib-compat: pinned bcrypt==4.0.1 in requirements.txt"]


# --- scratch sqlite files shipped in the deliverable ----------------------------

def repair_remove_scratch_db(code_dir: Path) -> list[str]:
    """Builders run the app/tests mid-build and leave app.db behind. Shipping
    it is a stale-schema time bomb: create_all only creates MISSING tables, so
    a DB written before the final model shape breaks the delivered app (seen
    live: 2026-07-02 projects+tasks eval, INSERT failed on a column the stale
    table lacked). The lifespan rebuilds the schema on boot — scratch DBs are
    never needed. Skips anything under tests/ (a deliberate fixture would live
    there) and alembic dirs."""
    if not _enabled("REMOVE_SCRATCH_DB"):
        return []
    root = _backend_root(code_dir)
    actions = []
    for pattern in ("*.db", "*.sqlite", "*.sqlite3"):
        for f in list(root.glob(pattern)) + list((root / "app").glob(pattern) if (root / "app").is_dir() else []):
            if f.is_file():
                f.unlink()
                actions.append(f"scratch-db: removed {f.relative_to(code_dir)}")
    return actions


# --- dispatcher + barriers ------------------------------------------------------

REPAIRS = [
    repair_canonical_filenames,     # filenames first — later repairs read them
    repair_passlib_bcrypt_compat,   # before verify provisions the venv
    repair_alembic_scaffold,
    repair_alembic_migration_chain,
    repair_fastapi_validation_loc_shape,
    repair_pytest_asyncio,
    repair_test_migrations_capture_stderr,
    repair_missing_client_fixture,
    repair_health_endpoint_path,
    repair_remove_scratch_db,
    repair_ruff_autofix,            # last — lint the final shape
]


def _fsync_file(path: Path, retries: int = 3) -> None:
    """fsync one file, retrying transient locks (antivirus / a sync client
    briefly holding a handle — M5 plan #15). Gives up quietly after `retries`."""
    for attempt in range(retries):
        try:
            with open(path, "rb+") as fh:
                os.fsync(fh.fileno())
            return
        except PermissionError:
            time.sleep(0.1 * (attempt + 1))
        except OSError:
            return  # not a lock (e.g. deleted) — nothing to retry


def _post_repair_barrier(code_dir: Path, started_ts: float) -> None:
    """fsync every file touched since the pass started, then flip the marker."""
    for f in code_dir.rglob("*"):
        try:
            touched = f.is_file() and f.stat().st_mtime >= started_ts - 1
        except OSError:
            continue
        if touched:
            _fsync_file(f)
    pending = code_dir / ".repairs_pending"
    if pending.exists():
        os.replace(pending, code_dir / ".repairs_complete")


def _wait_for_repair_barrier(code_dir: Path, timeout_s: float = 30.0) -> None:
    """Verifier calls this before pytest: wait out an in-flight repair pass,
    then defensively fsync the dirs a stale read would hurt most."""
    deadline = time.time() + timeout_s
    pending = code_dir / ".repairs_pending"
    while pending.exists() and time.time() < deadline:
        time.sleep(0.2)
    root = _backend_root(code_dir)
    for sub in ("alembic/versions", "tests", "app"):
        d = root / sub
        if not d.is_dir():
            continue
        for f in d.rglob("*.py"):
            _fsync_file(f)


def repair_all(code_dir: Path) -> list[str]:
    """Run every enabled repair. One repair crashing never kills the pass."""
    code_dir = Path(code_dir)
    started = time.time()
    (code_dir / ".repairs_pending").write_text("", encoding="utf-8")
    actions: list[str] = []
    try:
        for repair in REPAIRS:
            try:
                actions.extend(repair(code_dir))
            except Exception as e:  # noqa: BLE001 — isolate repair crashes
                actions.append(f"ERROR {repair.__name__}: {e!r}")
    finally:
        _post_repair_barrier(code_dir, started)
    return actions
