from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from .models import BatchConfig
from .util import safe_join


class SecurityError(RuntimeError):
    pass


DEFAULT_BLOCKED_COMMAND_PATTERNS = [
    r"\b(?:rm|del|erase|rmdir|rd)\b",
    r"\bRemove-Item\b",
    r"\b(?:format|mkfs(?:\.[a-z0-9]+)?|diskpart)\b",
    r"\bdd\b.*\bof=/dev/",
    r":\s*\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;",
    r"\b(?:shutdown|reboot|halt|poweroff)\b",
    r"\breg(?:\.exe)?\s+delete\b",
    r"\b(?:Invoke-Expression|iex|Start-Process|Start-Job)\b",
    r"\|\s*(?:sh|bash|powershell|pwsh|cmd|iex|Invoke-Expression)\b",
    r"\b(?:powershell|pwsh|cmd|bash|sh)\b.*\b(?:-Command|-c|/c)\b",
    r"\b(?:git)\s+(?:reset|clean|checkout|switch|merge|rebase|push|commit|tag)\b",
]


WRITE_VERBS = {
    "copy-item",
    "cp",
    "mkdir",
    "move-item",
    "mv",
    "new-item",
    "ni",
    "out-file",
    "rename-item",
    "set-content",
}


def resolve_workspace_path(
    workspace: Path,
    user_path: str,
    config: BatchConfig,
    *,
    action: str,
    must_exist: bool = False,
) -> Path:
    path = safe_join(workspace, user_path, must_exist=must_exist)
    _reject_blocked_path(workspace, path, config, action)
    return path


def review_command(config: BatchConfig, workspace: Path, run_dir: Path, command: list[str]) -> None:
    if not command:
        raise SecurityError("command must not be empty")
    if not _is_allowed_command(config, command):
        raise SecurityError(f"command is not allowlisted: {command}")

    command_text = " ".join(command)
    for pattern in [*DEFAULT_BLOCKED_COMMAND_PATTERNS, *config.blocked_command_patterns]:
        if re.search(pattern, command_text, re.IGNORECASE):
            raise SecurityError(f"command rejected by safety policy: {pattern}")

    executable = Path(command[0]).name.lower()
    if executable in WRITE_VERBS:
        raise SecurityError(f"command rejected because it can modify files: {executable}")

    for token in command[1:]:
        _reject_external_absolute_path_token(token, workspace, run_dir)


def safe_command_env(config: BatchConfig) -> dict[str, str]:
    if not config.command_clean_env:
        return os.environ.copy()
    keys = [
        "PATH",
        "Path",
        "SYSTEMROOT",
        "SystemRoot",
        "WINDIR",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "COMSPEC",
    ]
    return {key: value for key in keys if (value := os.environ.get(key))}


def _is_allowed_command(config: BatchConfig, command: list[str]) -> bool:
    for prefix in config.allowed_command_prefixes:
        if prefix and command[: len(prefix)] == prefix:
            return True
    return False


def _reject_blocked_path(workspace: Path, path: Path, config: BatchConfig, action: str) -> None:
    rel = path.relative_to(workspace.resolve())
    rel_posix = rel.as_posix()
    for pattern in config.blocked_path_patterns:
        normalized = pattern.replace("\\", "/")
        if fnmatch.fnmatch(rel_posix, normalized):
            raise SecurityError(f"blocked {action} path by policy: {rel_posix}")


def _reject_external_absolute_path_token(token: str, workspace: Path, run_dir: Path) -> None:
    cleaned = token.strip("\"'")
    if _looks_like_url(cleaned):
        return
    path = Path(cleaned)
    if not path.is_absolute():
        return
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        raise SecurityError(f"absolute command argument is invalid: {token}") from None
    allowed_roots = [workspace.resolve(), run_dir.resolve()]
    if not any(resolved == root or resolved.is_relative_to(root) for root in allowed_roots):
        raise SecurityError(f"absolute command argument escapes workspace/run_dir: {token}")


def _looks_like_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")

