from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .models import BatchConfig
from .paths import skill_search_dirs, skills_dir


SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
SKILL_PROMPT_CHAR_LIMIT = 60000


class SkillError(RuntimeError):
    pass


@dataclass(frozen=True)
class LoadedSkill:
    name: str
    path: Path
    text: str


def installed_skills_dir() -> Path:
    return skills_dir()


def list_installed_skills(root: Path | None = None) -> list[dict[str, str]]:
    rows_by_name: dict[str, dict[str, str]] = {}
    seen: set[str] = set()
    roots = [root] if root is not None else skill_search_dirs()
    for skill_root in roots:
        if not skill_root.exists():
            continue
        for item in sorted(skill_root.iterdir(), key=lambda value: value.name.lower()):
            skill_md = _skill_md_for_path(item)
            if skill_md is None or item.name in seen:
                continue
            seen.add(item.name)
            rows_by_name[item.name] = {"name": item.name, "path": str(item.resolve()), "skill_md": str(skill_md.resolve())}
    return [rows_by_name[name] for name in sorted(rows_by_name, key=str.lower)]


def install_skill(source: str | Path, *, name: str = "", force: bool = False, root: Path | None = None) -> Path:
    source_path = Path(source).expanduser().resolve()
    skill_md = _skill_md_for_path(source_path)
    if skill_md is None:
        raise SkillError(f"source is not a skill directory or SKILL.md file: {source}")

    skill_name = name.strip() or source_path.name
    if source_path.is_file() and source_path.name == "SKILL.md":
        skill_name = name.strip() or source_path.parent.name
    _validate_skill_name(skill_name)

    if root is None:
        target_root = skills_dir(create=True)
    else:
        target_root = root
        target_root.mkdir(parents=True, exist_ok=True)
    target = target_root / skill_name
    if target.exists():
        if not force:
            raise SkillError(f"skill already installed: {skill_name}; pass --force to replace it")
        shutil.rmtree(target)

    target.parent.mkdir(parents=True, exist_ok=True)
    if source_path.is_dir():
        ignore = shutil.ignore_patterns("__pycache__", ".git", ".bagent", ".batchagent", "output", "pca-results", "_worktrees")
        shutil.copytree(source_path, target, ignore=ignore)
    else:
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target / "SKILL.md")
    return target.resolve()


def load_manifest_skills(config: BatchConfig, manifest_path: Path) -> list[LoadedSkill]:
    loaded: list[LoadedSkill] = []
    for spec in config.skills:
        skill_dir = resolve_skill(spec, config, manifest_path)
        skill_md = _skill_md_for_path(skill_dir)
        if skill_md is None:
            raise SkillError(f"cannot resolve skill: {spec}")
        text = skill_md.read_text(encoding="utf-8", errors="replace")
        loaded.append(LoadedSkill(name=skill_dir.name, path=skill_dir.resolve(), text=text))
    return loaded


def render_skills_prompt(config: BatchConfig, manifest_path: Path) -> str:
    if not config.skills:
        return ""
    loaded = load_manifest_skills(config, manifest_path)
    parts = [
        "BatchAgent skills:",
        "- The following Skill instructions are loaded for this task.",
        "- Treat each loaded SKILL.md as authoritative task guidance when it applies.",
        "- Relative files mentioned by a Skill are available under that Skill path.",
    ]
    for skill in loaded:
        text = skill.text
        truncated = len(text) > SKILL_PROMPT_CHAR_LIMIT
        if truncated:
            text = text[:SKILL_PROMPT_CHAR_LIMIT]
        suffix = "\n[truncated]" if truncated else ""
        parts.append(f"Skill {skill.name} ({skill.path}):\n{text}{suffix}")
    return "\n\n".join(parts)


def resolve_skill(spec: str, config: BatchConfig, manifest_path: Path) -> Path:
    value = spec.strip()
    if not value:
        raise SkillError("skill spec must not be empty")

    direct = Path(value).expanduser()
    direct_candidates: list[Path] = []
    if direct.is_absolute():
        direct_candidates.append(direct)
    elif "/" in value or "\\" in value or value in {".", ".."}:
        direct_candidates.append((manifest_path.parent / direct).resolve())
    for candidate in direct_candidates:
        if _skill_md_for_path(candidate) is not None:
            return candidate

    for root in _skill_roots(config, manifest_path):
        candidate = root / value
        if _skill_md_for_path(candidate) is not None:
            return candidate
    raise SkillError(f"skill not found: {spec}")


def _skill_roots(config: BatchConfig, manifest_path: Path) -> list[Path]:
    roots: list[Path] = [*skill_search_dirs(), manifest_path.parent / "skills"]
    for item in config.skill_roots:
        path = Path(item).expanduser()
        if not path.is_absolute():
            path = manifest_path.parent / path
        roots.append(path)
    return [path.resolve() for path in roots]


def _skill_md_for_path(path: Path) -> Path | None:
    if path.is_dir():
        skill_md = path / "SKILL.md"
        return skill_md if skill_md.is_file() else None
    if path.is_file() and path.name == "SKILL.md":
        return path
    return None


def _validate_skill_name(name: str) -> None:
    if not SKILL_NAME_RE.fullmatch(name):
        raise SkillError("skill name may contain only letters, numbers, dot, underscore, and dash")
