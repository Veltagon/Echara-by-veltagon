"""Focused checks for the trickiest deterministic repairs (AST surgery)."""
from __future__ import annotations

import ast
import json
from pathlib import Path

from agents import repairs


def _mk(root: Path, rel: str, content: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_d4_duplicate_create_table(tmp_path):
    _mk(tmp_path, "backend/app/__init__.py", "")
    _mk(tmp_path, "backend/alembic/versions/001_init.py",
        "revision = '001'\ndown_revision = None\n"
        "from alembic import op\n"
        "def upgrade():\n    op.create_table('notes')\n")
    m2 = _mk(tmp_path, "backend/alembic/versions/002_dup.py",
             "revision = '002'\ndown_revision = '001'\n"
             "from alembic import op\n"
             "def upgrade():\n    op.create_table('notes')\n")
    actions = repairs.repair_alembic_migration_chain(tmp_path)
    assert actions and "002_dup" in actions[0]
    src = m2.read_text(encoding="utf-8")
    assert 'op.execute("DROP TABLE IF EXISTS notes")' in src
    assert src.index("DROP TABLE") < src.index("op.create_table")
    ast.parse(src)  # still valid python
    assert repairs.repair_alembic_migration_chain(tmp_path) == []  # idempotent


def test_d6_detail_list_shape(tmp_path):
    f = _mk(tmp_path, "backend/tests/test_api.py",
            "from fastapi.testclient import TestClient\n"
            'def test_v(resp):\n    assert resp.json()["detail"]["loc"] == ["body"]\n')
    _mk(tmp_path, "backend/app/__init__.py", "")
    assert repairs.repair_fastapi_validation_loc_shape(tmp_path)
    assert '.json()["detail"][0]["loc"]' in f.read_text(encoding="utf-8")
    assert repairs.repair_fastapi_validation_loc_shape(tmp_path) == []  # idempotent


def test_s28b_pytest_asyncio(tmp_path):
    _mk(tmp_path, "backend/app/__init__.py", "")
    _mk(tmp_path, "backend/tests/test_async.py", "async def test_x():\n    assert True\n")
    assert repairs.repair_pytest_asyncio(tmp_path)
    ini = (tmp_path / "backend" / "pytest.ini").read_text(encoding="utf-8")
    assert "[pytest]" in ini and "asyncio_mode = auto" in ini
    assert repairs.repair_pytest_asyncio(tmp_path) == []  # idempotent

    # [tool:pytest] header (silently ignored by pytest.ini) gets rewritten
    (tmp_path / "backend" / "pytest.ini").write_text("[tool:pytest]\naddopts = -q\n",
                                                     encoding="utf-8")
    assert repairs.repair_pytest_asyncio(tmp_path)
    ini = (tmp_path / "backend" / "pytest.ini").read_text(encoding="utf-8")
    assert "[pytest]" in ini and "[tool:pytest]" not in ini and "asyncio_mode" in ini


def test_stderr_capture_wrap(tmp_path):
    _mk(tmp_path, "backend/app/__init__.py", "")
    f = _mk(tmp_path, "backend/tests/test_migrations.py",
            "import subprocess\n"
            "def test_upgrade():\n"
            "    subprocess.run(['alembic', 'upgrade', 'head'], check=True)\n")
    assert repairs.repair_test_migrations_capture_stderr(tmp_path)
    src = f.read_text(encoding="utf-8")
    assert "capture_output=True" in src and "CalledProcessError" in src
    ast.parse(src)
    assert repairs.repair_test_migrations_capture_stderr(tmp_path) == []  # idempotent


def test_missing_client_fixture(tmp_path):
    _mk(tmp_path, "backend/app/__init__.py", "")
    _mk(tmp_path, "backend/tests/test_notes.py",
        "def test_create(client):\n    assert client is not None\n")
    assert repairs.repair_missing_client_fixture(tmp_path)
    conftest = (tmp_path / "backend" / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert "def client" in conftest and "TestClient" in conftest
    assert repairs.repair_missing_client_fixture(tmp_path) == []  # idempotent


def test_health_endpoint_path(tmp_path):
    build = tmp_path / "build"
    code = build / "code"
    _mk(build, "CONTRACT_REGISTRY.json", json.dumps({
        "api_endpoints": [{"method": "GET", "path": "/health",
                           "request_schema": None, "response_schema": None}]}))
    main = _mk(code, "backend/app/main.py",
               '@app.get("/api/health")\ndef health():\n    return {"status": "ok"}\n')
    assert repairs.repair_health_endpoint_path(code)
    assert '@app.get("/health")' in main.read_text(encoding="utf-8")
    assert repairs.repair_health_endpoint_path(code) == []  # idempotent


def test_canonical_filenames(tmp_path):
    _mk(tmp_path, "backend/requirement", "fastapi\n")
    assert repairs.repair_canonical_filenames(tmp_path)
    assert (tmp_path / "backend" / "requirements.txt").is_file()
    assert not (tmp_path / "backend" / "requirement").exists()


def test_passlib_bcrypt_pin(tmp_path):
    # bare bcrypt alongside passlib -> pinned in place
    r = _mk(tmp_path, "backend/requirements.txt", "fastapi\npasslib[bcrypt]\nbcrypt\n")
    _mk(tmp_path, "backend/app/__init__.py", "")
    assert repairs.repair_passlib_bcrypt_compat(tmp_path)
    assert "bcrypt==4.0.1" in r.read_text(encoding="utf-8")
    assert repairs.repair_passlib_bcrypt_compat(tmp_path) == []  # idempotent

    # passlib[bcrypt] with NO explicit bcrypt line -> pin appended
    r.write_text("fastapi\npasslib[bcrypt]\n", encoding="utf-8")
    assert repairs.repair_passlib_bcrypt_compat(tmp_path)
    assert "bcrypt==4.0.1" in r.read_text(encoding="utf-8")

    # no passlib -> untouched (direct-bcrypt projects keep any version)
    r.write_text("fastapi\nbcrypt\n", encoding="utf-8")
    assert repairs.repair_passlib_bcrypt_compat(tmp_path) == []
    assert "bcrypt\n" in r.read_text(encoding="utf-8")


def test_remove_scratch_db(tmp_path):
    _mk(tmp_path, "backend/app/__init__.py", "")
    (tmp_path / "backend" / "app.db").write_bytes(b"stale")
    (tmp_path / "backend" / "app" / "dev.sqlite3").write_bytes(b"stale")
    keep = _mk(tmp_path, "backend/tests/fixture.db", "deliberate fixture")
    actions = repairs.repair_remove_scratch_db(tmp_path)
    assert len(actions) == 2, actions
    assert not (tmp_path / "backend" / "app.db").exists()
    assert not (tmp_path / "backend" / "app" / "dev.sqlite3").exists()
    assert keep.exists()  # tests/ fixtures untouched
    assert repairs.repair_remove_scratch_db(tmp_path) == []  # idempotent


def test_repair_all_barrier_and_isolation(tmp_path, monkeypatch):
    _mk(tmp_path, "backend/app/__init__.py", "")
    # one repair crashing must not kill the pass
    monkeypatch.setitem(repairs.__dict__, "REPAIRS",
                        [lambda d: (_ for _ in ()).throw(RuntimeError("boom")),
                         repairs.repair_canonical_filenames])
    actions = repairs.repair_all(tmp_path)
    assert any("ERROR" in a for a in actions)
    assert (tmp_path / ".repairs_complete").exists()
    assert not (tmp_path / ".repairs_pending").exists()


def test_flag_gating(tmp_path, monkeypatch):
    _mk(tmp_path, "backend/requirement", "fastapi\n")
    monkeypatch.setenv("ECHARA_REPAIR_CANONICAL_FILENAMES", "0")
    assert repairs.repair_canonical_filenames(tmp_path) == []
    assert (tmp_path / "backend" / "requirement").exists()  # untouched when gated off
