"""Stub phase functions for milestone 1.

Each function writes hardcoded placeholder files into the build directory and
returns a one-line summary. No agents, no LLMs, no I/O beyond the local
filesystem.
"""
from __future__ import annotations

import json
from pathlib import Path


class VerifyFailed(Exception):
    """Raised by phase_verify when the produced code fails the smoke check."""


def phase_intake(build_dir: Path) -> str:
    build_dir.mkdir(parents=True, exist_ok=True)
    ledger = {
        "goal": "Notes CRUD API (milestone 1 stub goal).",
        "constraints": ["python>=3.11", "fastapi", "sqlite"],
        "non_goals": ["frontend", "auth", "deployment"],
    }
    (build_dir / "INSTRUCTION_LEDGER.json").write_text(
        json.dumps(ledger, indent=2), encoding="utf-8"
    )
    return "wrote INSTRUCTION_LEDGER.json"


def phase_plan(build_dir: Path) -> str:
    (build_dir / "PLAN.md").write_text(
        "# PLAN\n\n"
        "## Goal\nNotes CRUD API.\n\n"
        "## Endpoints\n"
        "- POST   /api/notes\n"
        "- GET    /api/notes\n"
        "- GET    /api/notes/{id}\n"
        "- DELETE /api/notes/{id}\n",
        encoding="utf-8",
    )
    contract = {
        "api_endpoints": [
            {"method": "POST",   "path": "/api/notes",      "request_schema": "NoteCreate", "response_schema": "NoteOut"},
            {"method": "GET",    "path": "/api/notes",      "response_schema": "list[NoteOut]"},
            {"method": "GET",    "path": "/api/notes/{id}", "response_schema": "NoteOut"},
            {"method": "DELETE", "path": "/api/notes/{id}", "response_schema": "None"},
        ],
        "shared_types": [
            {"name": "NoteCreate", "fields": {"title": "str", "body": "str"}},
            {"name": "NoteOut",    "fields": {"id": "int", "title": "str", "body": "str", "created_at": "datetime"}},
        ],
    }
    (build_dir / "CONTRACT_FROZEN.json").write_text(
        json.dumps(contract, indent=2), encoding="utf-8"
    )
    return "wrote PLAN.md, CONTRACT_FROZEN.json"


def phase_build(build_dir: Path) -> str:
    app = build_dir / "backend" / "app"
    (app / "models").mkdir(parents=True, exist_ok=True)
    (app / "routers").mkdir(parents=True, exist_ok=True)

    (app / "__init__.py").write_text("", encoding="utf-8")
    (app / "models" / "__init__.py").write_text("", encoding="utf-8")
    (app / "routers" / "__init__.py").write_text("", encoding="utf-8")

    (app / "main.py").write_text(
        "from fastapi import FastAPI\n"
        "from .routers import notes\n\n"
        'app = FastAPI(title="echara-notes")\n'
        'app.include_router(notes.router, prefix="/api")\n',
        encoding="utf-8",
    )
    (app / "models" / "note.py").write_text(
        "from datetime import datetime\n"
        "from pydantic import BaseModel\n\n"
        "class NoteCreate(BaseModel):\n"
        "    title: str\n"
        "    body: str\n\n"
        "class NoteOut(BaseModel):\n"
        "    id: int\n"
        "    title: str\n"
        "    body: str\n"
        "    created_at: datetime\n",
        encoding="utf-8",
    )
    (app / "routers" / "notes.py").write_text(
        "from fastapi import APIRouter\n"
        "from ..models.note import NoteOut\n\n"
        "router = APIRouter()\n\n"
        '@router.get("/notes")\n'
        "def list_notes() -> list[NoteOut]:\n"
        "    return []\n",
        encoding="utf-8",
    )
    return "wrote backend/app/{main.py, models/note.py, routers/notes.py}"


def phase_verify(build_dir: Path) -> str:
    main_py = build_dir / "backend" / "app" / "main.py"
    if not main_py.exists():
        raise VerifyFailed(f"missing {main_py}")
    if "from fastapi import FastAPI" not in main_py.read_text(encoding="utf-8"):
        raise VerifyFailed("main.py does not import FastAPI")
    (build_dir / "VERIFY.json").write_text(
        json.dumps(
            {
                "import_smoke": "passed",
                "checks": ["main.py exists", "from fastapi import FastAPI present"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return "verified main.py imports FastAPI"


def phase_deliver(build_dir: Path) -> str:
    (build_dir / "VERDICT.md").write_text(
        "# VERDICT\n\n"
        "verified_path: true\n"
        "score: stub-pass (milestone 1)\n"
        f"build_dir: {build_dir.as_posix()}\n",
        encoding="utf-8",
    )
    return "wrote VERDICT.md"
