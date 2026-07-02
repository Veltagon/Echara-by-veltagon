"""BuilderAgent — composes persona + skill reference + contract into a prompt
and dispatches it through a chosen provider."""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from providers.base import Provider, RunResult


ECHARA_ROOT = Path(__file__).resolve().parent
PERSONA_PATH = ECHARA_ROOT / "personas" / "builder.md"

# Skill source: alirezarezvani/claude-skills marketplace cache.
SKILL_SOURCE = (
    Path.home()
    / ".claude" / "plugins" / "marketplaces" / "claude-code-skills"
    / "engineering-team" / "skills" / "senior-backend"
)


@dataclass
class BuildResult:
    run: RunResult
    build_dir: Path
    self_verify_present: bool
    main_imports_fastapi: bool
    endpoints_found: list[str]
    files_created: int
    backend_files: int   # files written under backend/ — the real signal
    status: str          # "pass" | "incomplete" | "failed" | "unavailable"


class BuilderAgent:
    def __init__(self, provider: Provider, contract: dict, build_dir: Path):
        self.provider = provider
        self.contract = contract
        self.build_dir = build_dir

    def _stage_skill(self) -> Path:
        """Copy the skill into build_dir/skills/senior-backend so the agent can
        read it via relative paths inside its working directory."""
        dest = self.build_dir / "skills" / "senior-backend"
        if not SKILL_SOURCE.exists():
            print(f"[ECHARA] WARNING: skill source not found at {SKILL_SOURCE} — "
                  f"the agent will run WITHOUT its backend skill (expect weaker "
                  f"output; install alirezarezvani/claude-skills to fix).")
            return dest
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(SKILL_SOURCE, dest)
        return dest

    def _stage_persona(self) -> Path:
        """Place the persona inside the build dir so the agent reads it from
        the same root it operates in."""
        dest = self.build_dir / "BUILDER_PERSONA.md"
        dest.write_text(PERSONA_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        return dest

    def _stage_contract(self) -> Path:
        dest = self.build_dir / "CONTRACT.json"
        dest.write_text(json.dumps(self.contract, indent=2), encoding="utf-8")
        return dest

    def _build_prompt(
        self, persona_path: Path, skill_path: Path, contract_path: Path
    ) -> str:
        """Inline persona + contract; keep skill as a file ref (it's 15KB).

        Imperative phrasing matters: Codex `exec` is single-shot and treats
        meta-instructions ("read X then do Y") as orientation. Telling it the
        task IS to write files makes it call tools immediately."""
        rel_skill = skill_path.relative_to(self.build_dir).as_posix()
        persona = persona_path.read_text(encoding="utf-8")
        contract = contract_path.read_text(encoding="utf-8")
        return (
            "TASK: Build a FastAPI + SQLite Notes CRUD service in this "
            "directory, following the rules and contract below. Write every "
            "file using your file-write tool. Do not stop until the backend "
            "imports and SELF_VERIFY.md is written with every line PASS.\n\n"
            "=== RULES (from BUILDER_PERSONA.md) ===\n"
            f"{persona}\n"
            "=== CONTRACT (from CONTRACT.json) ===\n"
            f"{contract}\n\n"
            f"=== SKILL ===\nA backend-development skill is available at "
            f"`{rel_skill}/SKILL.md`. Read it for principles (input "
            "validation matrix, error code coverage, idempotency). Ignore "
            "Node.js / Express templates — this project is Python / FastAPI.\n\n"
            "=== DO NOW ===\n"
            "1. Create the file tree under `backend/app/` exactly as the "
            "RULES section's `Output layout` specifies. No extra files.\n"
            "2. After writing all files, run `python -c \"from app.main "
            "import app\"` from `backend/` and write `SELF_VERIFY.md` in this "
            "directory listing every check from the RULES with PASS or FAIL.\n"
            "3. Stop. Do not narrate; do not ask follow-up questions."
        )

    def run(self, timeout_sec: int = 1200) -> BuildResult:
        self.build_dir.mkdir(parents=True, exist_ok=True)
        persona = self._stage_persona()
        skill = self._stage_skill()
        contract = self._stage_contract()
        prompt = self._build_prompt(persona, skill, contract)
        (self.build_dir / "PROMPT.md").write_text(prompt, encoding="utf-8")

        log_dir = ECHARA_ROOT / "logs"
        run = self.provider.run(prompt, self.build_dir, log_dir, timeout_sec=timeout_sec)

        self_verify = (self.build_dir / "SELF_VERIFY.md").exists()
        imports_ok = self._check_main_imports_fastapi()
        endpoints = self._scan_endpoints()
        backend = self.build_dir / "backend"
        backend_files = (
            sum(1 for p in backend.rglob("*") if p.is_file()) if backend.exists() else 0
        )
        expected_endpoints = {
            f"{ep['method']} {ep['path']}"
            for ep in self.contract.get("api_endpoints", [])
        }

        # Classification order matters: unavailable > failed > incomplete > pass.
        if run.skipped_reason is not None:
            status = "unavailable"
        elif not run.ok:
            status = "failed"
        elif backend_files < 3:
            # exit-0 but barely touched disk → the model bailed early. Don't
            # call this a failure — it's a "needs continuation" signal that a
            # multi-turn runner (M4) can handle by re-prompting with the
            # current file list. For now: surface the distinction honestly.
            status = "incomplete"
        elif (
            imports_ok
            and self_verify
            and expected_endpoints.issubset(set(endpoints))
        ):
            status = "pass"
        else:
            status = "failed"

        return BuildResult(
            run=run,
            build_dir=self.build_dir,
            self_verify_present=self_verify,
            main_imports_fastapi=imports_ok,
            endpoints_found=endpoints,
            files_created=sum(1 for _ in self.build_dir.rglob("*") if _.is_file()),
            backend_files=backend_files,
            status=status,
        )

    def _check_main_imports_fastapi(self) -> bool:
        main = self.build_dir / "backend" / "app" / "main.py"
        if not main.exists():
            return False
        text = main.read_text(encoding="utf-8")
        # Tolerate the real import shapes: `from fastapi import FastAPI, ...`,
        # `from fastapi import (FastAPI)`, and `import fastapi` + `fastapi.FastAPI`.
        return ("from fastapi" in text or "import fastapi" in text) and "FastAPI" in text

    def _scan_endpoints(self) -> list[str]:
        """Import the produced FastAPI app and return contract endpoints whose
        (method, path) appears in app.routes. Definitive — no string-matching
        false negatives. Runs in a subprocess so a broken import only fails
        verification, never the parent."""
        import subprocess
        import sys

        backend = self.build_dir / "backend"
        if not (backend / "app" / "main.py").exists():
            return []
        probe = (
            "import json, sys\n"
            "from app.main import app\n"
            "routes = []\n"
            "for r in app.routes:\n"
            "    methods = sorted(getattr(r, 'methods', []) or [])\n"
            "    path = getattr(r, 'path', '')\n"
            "    for m in methods:\n"
            "        routes.append((m, path))\n"
            "print(json.dumps(routes))\n"
        )
        try:
            out = subprocess.check_output(
                [sys.executable, "-c", probe],
                cwd=str(backend),
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return []
        import json as _json
        try:
            data = _json.loads(out)
        except Exception:
            return []
        # Keep only well-formed (method, path) pairs — one malformed route entry
        # shouldn't discard the whole scan (already crash-safe; this is stricter).
        routes = {
            (item[0], item[1])
            for item in data
            if isinstance(item, (list, tuple)) and len(item) == 2
        }
        found = []
        for ep in self.contract.get("api_endpoints", []):
            if (ep["method"].upper(), ep["path"]) in routes:
                found.append(f"{ep['method']} {ep['path']}")
        return found
