"""Skill index + staging — the progressive-disclosure half of the ECHARA thesis.

Scan `skills_root/*/SKILL.md`, pull only the YAML frontmatter (name +
description, ~100 tokens each), and render a compact index for the system
prompt. The full body is NOT loaded here — the model pulls it on demand.

For the model to reach a skill's `references/` (read_file) and `scripts/`
(bash_run), the skill folder must live inside the workspace — so `stage()`
copies it in, exactly like M2's agent.py. Then the whole guide mechanism works
through the normal tools: index -> read SKILL.md -> read references -> run
scripts. Ported from opencode's skill/discovery.ts + skill/index.ts.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class Skill:
    name: str
    description: str
    md_path: Path  # path to this skill's SKILL.md


def _parse_frontmatter(text: str) -> dict:
    """Extract the leading `---\\n...\\n---` block as a dict. Returns {} if the
    file has no frontmatter or it isn't a mapping."""
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        data = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def discover(skills_root: Path | None) -> list[Skill]:
    """All skills under skills_root with a readable name in their frontmatter.
    Directory name wins over a missing/blank frontmatter `name`."""
    if not skills_root or not Path(skills_root).is_dir():
        return []
    found: list[Skill] = []
    for skill_md in sorted(Path(skills_root).glob("*/SKILL.md")):
        fm = _parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
        name = str(fm.get("name") or skill_md.parent.name).strip().strip('"')
        desc = str(fm.get("description") or "").strip()
        found.append(Skill(name=name, description=desc, md_path=skill_md))
    return found


def stage(source_root: Path, workspace: Path, subdir: str = "skills") -> Path:
    """Copy every `<name>/` skill folder from source_root into
    workspace/<subdir>/ so the model can read_file its references and bash_run
    its scripts. Returns the staged root. Idempotent per skill (skips existing)."""
    dest_root = workspace / subdir
    for skill_md in sorted(source_root.glob("*/SKILL.md")):
        dest = dest_root / skill_md.parent.name
        if not dest.exists():
            shutil.copytree(skill_md.parent, dest)
    return dest_root


def render_index(skills: list[Skill], path_base: Path | None = None) -> str:
    """Compact index for the system prompt. When path_base is given (the
    workspace root), each line shows the workspace-relative SKILL.md path so the
    model knows exactly what to read_file. Empty string when no skills."""
    if not skills:
        return ""
    how = (
        "read_file the path (then its references/, run its scripts/ with bash_run)"
        if path_base
        else "call load_skill(name) to read the full body"
    )
    lines = [f"Available skills — {how}:"]
    for s in skills:
        if path_base:
            try:
                rel = s.md_path.relative_to(path_base).as_posix()
            except ValueError:
                rel = s.md_path.as_posix()
            lines.append(f"- {s.name} [{rel}]: {s.description}")
        else:
            lines.append(f"- {s.name}: {s.description}")
    return "\n".join(lines)


def demo() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "src"
        sk = src / "demo-skill"
        (sk / "references").mkdir(parents=True)
        (sk / "SKILL.md").write_text(
            '---\nname: "demo-skill"\ndescription: does a demo thing\n---\n# body\n',
            encoding="utf-8",
        )
        (sk / "references" / "guide.md").write_text("ref body", encoding="utf-8")

        skills = discover(src)
        assert len(skills) == 1 and skills[0].name == "demo-skill", skills
        assert "does a demo thing" in render_index(skills)
        assert discover(src / "nope") == []

        ws = Path(d) / "ws"
        ws.mkdir()
        staged = stage(src, ws)
        assert (staged / "demo-skill" / "SKILL.md").is_file()
        assert (staged / "demo-skill" / "references" / "guide.md").is_file()
        idx = render_index(discover(staged), path_base=ws)
        assert "skills/demo-skill/SKILL.md" in idx, idx
    print("skills.demo OK")


if __name__ == "__main__":
    demo()
