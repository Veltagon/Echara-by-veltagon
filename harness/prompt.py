"""System-prompt assembly — environment block + skill index + persona.

A raw API model has no built-in awareness of its sandbox or its tools, so we
state them plainly. Kept well under the ~5-6k token budget the guide sets:
environment is a few lines, the skill index is frontmatter-only, and the
persona is the caller's choice. Ported from opencode's session/system.ts
(environment + skills sections).
"""
from __future__ import annotations

import platform
from datetime import date
from pathlib import Path

from harness import skills
from harness.tools import active_bash_shell


def _within(child: Path, parent: Path) -> bool:
    try:
        return Path(child).resolve().is_relative_to(Path(parent).resolve())
    except ValueError:
        return False


_BASE = """You are an autonomous build agent operating inside a sandboxed workspace.

You have tools for the filesystem and shell. Use them — do not ask the user for \
permission and do not narrate what you are about to do; just call the tool. \
Always write real files with write_file; never paste file contents into chat. \
Verify your own work by running it with bash_run before you finish. When the \
task is fully complete, call the `done` tool with a short summary."""


def build_system_prompt(
    workspace_root: Path,
    skills_root: Path | None = None,
    persona: str = "",
    full_access: bool = False,
) -> str:
    parts = [_BASE]
    access = (
        "You have full filesystem access; paths may be absolute or relative to "
        "workspace_root."
        if full_access
        else "File-tool paths are relative to workspace_root and cannot escape "
        "it. Use bash_run/powershell_run if you must reach outside it."
    )
    shell = active_bash_shell()
    shell_note = (
        f"shells: bash_run runs {shell}; powershell_run runs Windows PowerShell."
    )
    if shell != "bash":
        shell_note += (
            f" NOTE: bash_run is {shell} here — POSIX operators (&&, /dev/null, "
            "$VAR) will NOT work; use powershell_run for non-trivial shell work."
        )
    parts.append(
        "<environment>\n"
        f"workspace_root: {workspace_root}\n"
        f"platform: {platform.system()}\n"
        f"date: {date.today().isoformat()}\n"
        f"{shell_note}\n"
        f"{access}\n</environment>"
    )
    if skills_root:
        # path_base=workspace_root makes the index show workspace-relative
        # SKILL.md paths when skills are staged inside it (read_file-able).
        base = workspace_root if _within(skills_root, workspace_root) else None
        index = skills.render_index(skills.discover(skills_root), path_base=base)
        if index:
            parts.append("<skills>\n" + index + "\n</skills>")
    if persona.strip():
        parts.append("<persona>\n" + persona.strip() + "\n</persona>")
    return "\n\n".join(parts)
