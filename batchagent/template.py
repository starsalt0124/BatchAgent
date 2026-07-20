from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from .models import BatchConfig, Task
from .util import safe_join


_TOKEN_RE = re.compile(r"{{\s*([^{}]+?)\s*}}")
_MAX_FILE_TOKEN_CHARS = 200_000


def _lookup(path: str, task: Task, config: BatchConfig, current_date: str, workspace: Path | None = None) -> Any:
    root: Any
    if path.startswith("file:"):
        return _read_file_token(path[len("file:") :].strip(), task, config, current_date, workspace)
    if path in {"CURR_DATE", "current_date"}:
        return current_date
    if path == "task":
        return {
            "id": task.id,
            "status": task.status,
            "kind": task.kind,
            "input": task.input,
            "attempts": task.attempts,
        }
    if path == "config":
        return config.raw
    if path in {"vars", "run_vars"}:
        return config.run_vars
    if path == "workspace":
        return config.workspace
    if path.startswith("task."):
        root = {
            "id": task.id,
            "status": task.status,
            "kind": task.kind,
            "input": task.input,
            "attempts": task.attempts,
            "result": task.result,
            "error": task.error,
        }
        parts = path.split(".")[1:]
    elif path.startswith("config."):
        root = config.raw
        parts = path.split(".")[1:]
    elif path.startswith("vars.") or path.startswith("run_vars."):
        root = config.run_vars
        parts = path.split(".")[1:]
    else:
        return ""

    for part in parts:
        if isinstance(root, dict):
            root = root.get(part, "")
        else:
            root = getattr(root, part, "")
    return root


def _read_file_token(expr: str, task: Task, config: BatchConfig, current_date: str, workspace: Path | None) -> str:
    if workspace is None:
        return ""
    target = _lookup(expr, task, config, current_date, workspace) if _looks_like_lookup(expr) else expr
    try:
        path = safe_join(workspace, str(target), must_exist=True)
    except Exception:
        return ""
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[:_MAX_FILE_TOKEN_CHARS]


def _looks_like_lookup(expr: str) -> bool:
    return expr in {"task", "config", "vars", "run_vars", "workspace", "CURR_DATE", "current_date"} or expr.startswith(
        ("task.", "config.", "vars.", "run_vars.")
    )


def render_template(
    template: str,
    task: Task,
    config: BatchConfig,
    current_date: str | None = None,
    workspace: Path | None = None,
) -> str:
    resolved_current_date = current_date or date.today().isoformat()

    def replace(match: re.Match[str]) -> str:
        value = _lookup(match.group(1), task, config, resolved_current_date, workspace)
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    rendered = _TOKEN_RE.sub(replace, template)
    return rendered.replace("CURR_DATE", resolved_current_date)
