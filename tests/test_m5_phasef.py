"""Phase F — fullstack frontend leg (deterministic codegen). No model calls."""
from __future__ import annotations

from pathlib import Path

from agents import contract_codegen as cg
from agents import verifier


_CONTRACT = {
    "api_endpoints": [
        {"method": "POST", "path": "/api/notes", "request_schema": "NoteCreate",
         "response_schema": "NoteOut", "auth_required": True},
        {"method": "GET", "path": "/api/notes", "request_schema": None,
         "response_schema": "list[NoteOut]", "auth_required": True},
        {"method": "DELETE", "path": "/api/notes/{id}", "request_schema": None,
         "response_schema": None, "auth_required": True},
    ],
    "shared_types": [
        {"name": "NoteCreate", "fields": {"title": "str (1..200)", "body": "str"},
         "required": ["title", "body"]},
        {"name": "NoteOut", "fields": {"id": "int", "title": "str", "created_at": "datetime"},
         "required": ["id", "title", "created_at"]},
    ],
}


def test_ts_type_mapping():
    assert cg.ts_type("str (1..200)") == "string"
    assert cg.ts_type("int") == "number"
    assert cg.ts_type("bool") == "boolean"
    assert cg.ts_type("datetime") == "string"
    assert cg.ts_type("list[NoteOut]") == "NoteOut[]"
    assert cg.ts_type("NoteOut") == "NoteOut"


def test_gen_types():
    ts = cg.gen_types(_CONTRACT["shared_types"])
    assert "export interface NoteCreate {" in ts
    assert "title: string;" in ts          # required
    assert "created_at: string;" in ts     # datetime -> string
    assert "id: number;" in ts


def test_gen_client_functions_and_imports():
    js = cg.gen_client(_CONTRACT["api_endpoints"])
    assert 'import type { NoteCreate, NoteOut } from "./types";' in js
    # 'api' segment is dropped; path params fold into the name (collision-free)
    assert "export async function postNotes(body: NoteCreate): Promise<NoteOut>" in js
    assert "export async function getNotes(): Promise<NoteOut[]>" in js
    assert "deleteNotesId(id: string | number): Promise<void>" in js  # param arg, void return
    assert "VITE_API_URL" in js and "authHeaders()" in js
    assert "class ApiError" in js


def test_generate_writes_files(tmp_path):
    written = cg.generate(_CONTRACT, tmp_path / "code" / "frontend")
    api = tmp_path / "code" / "frontend" / "src" / "api"
    assert (api / "types.ts").is_file() and (api / "client.ts").is_file()
    assert len(written) == 2


def test_generate_from_build_noop_without_contract(tmp_path):
    assert cg.generate_from_build(tmp_path) == []  # no CONTRACT_REGISTRY.json


def test_frontend_check_skips_when_absent(tmp_path):
    # No code/frontend -> the frontend leg is skipped entirely (None), so a
    # pure-backend build's verdict is unaffected.
    assert verifier._check_frontend(tmp_path) is None


def test_codegen_robust_to_bare_list_and_namespaced(tmp_path):
    # The architect emits a BARE endpoint list with module-namespaced schema
    # names and no shared_types (E1, 2026-07-05) — this used to crash BUILD.
    contract = [
        {"method": "POST", "path": "/auth/register",
         "request_schema": "auth.RegisterRequest", "response_schema": "auth.UserPublic"},
        {"method": "GET", "path": "/bookmarks",
         "request_schema": None, "response_schema": "bookmarks.BookmarkPage"},
    ]
    written = cg.generate(contract, tmp_path / "code" / "frontend")  # must not raise
    types = written[0].read_text(encoding="utf-8")
    client = written[1].read_text(encoding="utf-8")
    # namespaces stripped, placeholders emitted so it type-checks
    assert "export interface UserPublic { [key: string]: unknown; }" in types
    assert "auth." not in client and "bookmarks." not in types
    assert "postAuthRegister(body: RegisterRequest): Promise<UserPublic>" in client


def test_codegen_never_raises_on_garbage(tmp_path):
    assert cg.generate("not a contract", tmp_path / "f1") == \
        [tmp_path / "f1" / "src" / "api" / "types.ts",
         tmp_path / "f1" / "src" / "api" / "client.ts"]
    assert cg.generate({}, tmp_path / "f2")  # empty dict -> empty but valid files
