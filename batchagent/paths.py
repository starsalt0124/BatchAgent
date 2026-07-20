from __future__ import annotations

import os
from pathlib import Path


BAGENT_HOME_ENV = "BAGENT_HOME"


def bagent_home(*, create: bool = False) -> Path:
    """Return the state home, optionally creating it with private permissions."""
    configured = os.environ.get(BAGENT_HOME_ENV, "").strip()
    path = Path(configured).expanduser() if configured else Path.home() / ".bagent"
    if create:
        ensure_private_dir(path)
    return path


def settings_path() -> Path:
    return bagent_home() / "settings.json"


def state_db_path() -> Path:
    return bagent_home() / "state.sqlite3"


def runs_dir(*, create: bool = False) -> Path:
    path = bagent_home(create=create) / "runs"
    if create:
        ensure_private_dir(path)
    return path


def skills_dir(*, create: bool = False) -> Path:
    path = bagent_home(create=create) / "skills"
    if create:
        ensure_private_dir(path)
    return path


def legacy_skills_dir() -> Path:
    """Return the pre-bagent skill location for read compatibility."""
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config_home.expanduser() / "batchagent" / "skills"


def skill_search_dirs() -> list[Path]:
    """Return current then legacy skill roots, without duplicate locations."""
    roots: list[Path] = []
    seen: set[Path] = set()
    for path in (skills_dir(), legacy_skills_dir()):
        resolved = path.resolve(strict=False)
        if resolved in seen:
            continue
        seen.add(resolved)
        roots.append(path)
    return roots


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _best_effort_chmod(path, 0o700)
    return path


def ensure_private_file(path: Path) -> Path:
    _best_effort_chmod(path, 0o600)
    return path


def _best_effort_chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except (NotImplementedError, OSError):
        # Windows ACLs and some network filesystems do not implement POSIX modes.
        pass
