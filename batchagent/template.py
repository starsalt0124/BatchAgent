from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from .models import BatchConfig, Task


_TOKEN_RE = re.compile(r"{{\s*([^{}]+?)\s*}}")


def _lookup(path: str, task: Task, config: BatchConfig, current_date: str) -> Any:
    root: Any
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
    else:
        return ""

    for part in parts:
        if isinstance(root, dict):
            root = root.get(part, "")
        else:
            root = getattr(root, part, "")
    return root


def render_template(template: str, task: Task, config: BatchConfig, current_date: str | None = None) -> str:
    resolved_current_date = current_date or date.today().isoformat()

    def replace(match: re.Match[str]) -> str:
        value = _lookup(match.group(1), task, config, resolved_current_date)
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    rendered = _TOKEN_RE.sub(replace, template)
    return rendered.replace("CURR_DATE", resolved_current_date)
