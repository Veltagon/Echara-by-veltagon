"""Deterministic frontend API-client codegen from CONTRACT_REGISTRY.json.

The backend↔frontend seam is the single most drift-prone interface in a
fullstack build, so a model never hand-writes it: this generates
`src/api/types.ts` (interfaces from shared_types) and `src/api/client.ts`
(typed fetch wrappers per endpoint) from the contract. Frontend NN-rules forbid
re-declaring these — the UI imports the generated client only. Pure string
transform, golden-file tested, zero tokens.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_SCALARS = {
    "str": "string", "string": "string", "int": "number", "integer": "number",
    "float": "number", "number": "number", "bool": "boolean", "boolean": "boolean",
    "datetime": "string", "date": "string", "uuid": "string", "any": "unknown",
    "dict": "Record<string, unknown>",
}


def ts_type(pytype) -> str:
    """Map a contract type string to TypeScript. Handles list[X]/List[X] and
    strips constraint noise like 'str (1..200)'."""
    s = str(pytype).strip()
    m = re.match(r"(?:list|List)\[(.+)\]$", s)
    if m:
        return ts_type(m.group(1)) + "[]"
    base = re.split(r"[\s(\[]", s, 1)[0].lower()
    if base in _SCALARS:
        return _SCALARS[base]
    # Unknown, non-scalar token → treat as a referenced interface name (keep case)
    ref = re.split(r"[\s(\[]", s, 1)[0]
    return ref if re.match(r"^[A-Za-z_]\w*$", ref) else "unknown"


def gen_types(shared_types: list[dict]) -> str:
    out = ["// AUTO-GENERATED from CONTRACT_REGISTRY.json — do not edit by hand."]
    for t in shared_types:
        name = t["name"]
        required = set(t.get("required", []))
        out.append(f"\nexport interface {name} {{")
        for fname, ftype in (t.get("fields") or {}).items():
            opt = "" if fname in required else "?"
            out.append(f"  {fname}{opt}: {ts_type(ftype)};")
        out.append("}")
    return "\n".join(out) + "\n"


def _fn_name(method: str, path: str) -> str:
    parts = [p for p in re.split(r"[/{}]", path) if p and p != "api"]
    camel = "".join(w[:1].upper() + w[1:] for w in parts)
    return method.lower() + camel


def _referenced_types(endpoints: list[dict]) -> list[str]:
    names = set()
    for ep in endpoints:
        for key in ("request_schema", "response_schema"):
            v = ep.get(key)
            if not v:
                continue
            base = re.sub(r"(?:list|List)\[(.+)\]$", r"\1", str(v))
            base = re.split(r"[\s(\[]", base, 1)[0]
            if re.match(r"^[A-Z]\w*$", base):  # an interface name, not a scalar
                names.add(base)
    return sorted(names)


_CLIENT_HEADER = """// AUTO-GENERATED from CONTRACT_REGISTRY.json — do not edit by hand.
{imports}
const API_URL = (import.meta as any).env?.VITE_API_URL ?? "http://localhost:8000";

export class ApiError extends Error {{
  constructor(public status: number, message: string) {{
    super(message);
    this.name = "ApiError";
  }}
}}

function authHeaders(): Record<string, string> {{
  const t = typeof localStorage !== "undefined" ? localStorage.getItem("token") : null;
  return t ? {{ Authorization: `Bearer ${{t}}` }} : {{}};
}}
"""


def gen_client(endpoints: list[dict]) -> str:
    refs = _referenced_types(endpoints)
    imports = f'import type {{ {", ".join(refs)} }} from "./types";\n' if refs else ""
    parts = [_CLIENT_HEADER.format(imports=imports)]
    for ep in endpoints:
        method = ep["method"].upper()
        path = ep["path"]
        req = ep.get("request_schema")
        resp = ep.get("response_schema")
        params = re.findall(r"\{(\w+)\}", path)
        args = [f"{p}: string | number" for p in params]
        if req:
            args.append(f"body: {ts_type(req)}")
        ret = ts_type(resp) if resp else "void"
        url = re.sub(r"\{(\w+)\}", r"${\1}", path)
        headers = (('"Content-Type": "application/json", ' if req else "") + "...authHeaders()")
        body_line = "\n    body: JSON.stringify(body)," if req else ""
        ret_line = "  return res.json();" if resp else "  return;"
        parts.append(
            f'export async function {_fn_name(method, path)}({", ".join(args)}): Promise<{ret}> {{\n'
            f'  const res = await fetch(`${{API_URL}}{url}`, {{\n'
            f'    method: "{method}",\n'
            f'    headers: {{ {headers} }},{body_line}\n'
            f'  }});\n'
            f'  if (!res.ok) throw new ApiError(res.status, await res.text());\n'
            f'{ret_line}\n'
            f'}}')
    return "\n\n".join(parts) + "\n"


def generate(contract: dict, frontend_root: Path) -> list[Path]:
    """Write src/api/{types,client}.ts under frontend_root. Returns written paths."""
    api_dir = Path(frontend_root) / "src" / "api"
    api_dir.mkdir(parents=True, exist_ok=True)
    types = api_dir / "types.ts"
    client = api_dir / "client.ts"
    types.write_text(gen_types(contract.get("shared_types") or []), encoding="utf-8")
    client.write_text(gen_client(contract.get("api_endpoints") or []), encoding="utf-8")
    return [types, client]


def generate_from_build(build_dir: Path) -> list[Path]:
    """Generate the client from build_dir/CONTRACT_REGISTRY.json into
    build_dir/code/frontend. No-op (returns []) if there is no contract."""
    cpath = Path(build_dir) / "CONTRACT_REGISTRY.json"
    if not cpath.is_file():
        return []
    try:
        contract = json.loads(cpath.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return generate(contract, Path(build_dir) / "code" / "frontend")
