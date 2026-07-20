from __future__ import annotations

import fnmatch
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .models import ArtifactSubmission, BatchConfig, Task
from .security import SecurityError, resolve_workspace_path, review_command, safe_command_env
from .util import atomic_write_text, truncate
from .web_tools import web_fetch, web_search


class ToolError(RuntimeError):
    pass


@dataclass
class ToolContext:
    config: BatchConfig
    task: Task
    workspace: Path
    run_dir: Path
    submission: ArtifactSubmission | None = None
    created_paths: set[Path] = field(default_factory=set)


ToolFn = Callable[[ToolContext, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ToolDefinition:
    spec: dict[str, Any]
    invoke: ToolFn


def tool_specs(config: BatchConfig) -> list[dict[str, Any]]:
    return [ALL_TOOLS[name].spec for name in config.tools if name in ALL_TOOLS]


def available_tool_names() -> list[str]:
    return sorted(ALL_TOOLS)


def unknown_tool_names(config: BatchConfig) -> list[str]:
    return sorted({name for name in config.tools if name not in ALL_TOOLS})


def invoke_tool(ctx: ToolContext, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name not in ctx.config.tools:
        raise ToolError(f"tool is not loaded for this manifest: {name}")
    definition = ALL_TOOLS.get(name)
    if definition is None:
        raise ToolError(f"unknown tool: {name}")
    try:
        return definition.invoke(ctx, arguments)
    except SecurityError as exc:
        raise ToolError(str(exc)) from exc


def _read_file(ctx: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    path = resolve_workspace_path(ctx.workspace, str(arguments["path"]), ctx.config, action="read", must_exist=True)
    if not path.is_file():
        raise ToolError(f"not a file: {arguments['path']}")
    offset = max(0, int(arguments.get("offset", 0)))
    limit = max(1, min(int(arguments.get("limit", 12000)), 100000))
    text = path.read_text(encoding="utf-8", errors="replace")
    return {
        "path": str(path.relative_to(ctx.workspace)),
        "offset": offset,
        "limit": limit,
        "content": text[offset : offset + limit],
        "truncated": offset + limit < len(text),
        "size": len(text),
    }


def _list_files(ctx: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    root = resolve_workspace_path(ctx.workspace, str(arguments.get("path") or "."), ctx.config, action="read", must_exist=True)
    if not root.is_dir():
        raise ToolError(f"not a directory: {arguments.get('path') or '.'}")
    pattern = str(arguments.get("pattern") or "*")
    recursive = bool(arguments.get("recursive", False))
    limit = max(1, min(int(arguments.get("limit", 200)), 2000))
    iterator = root.rglob("*") if recursive else root.iterdir()
    files: list[str] = []
    for item in sorted(iterator, key=lambda p: str(p).lower()):
        if item.is_file() and fnmatch.fnmatch(item.name, pattern):
            _guard_path(ctx, item, "read")
            files.append(str(item.relative_to(ctx.workspace)))
            if len(files) >= limit:
                break
    return {"files": files, "truncated": len(files) >= limit}


def _glob(ctx: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    base = resolve_workspace_path(ctx.workspace, str(arguments.get("base_path") or "."), ctx.config, action="read", must_exist=True)
    if not base.is_dir():
        raise ToolError(f"base_path is not a directory: {arguments.get('base_path') or '.'}")
    pattern = str(arguments["pattern"])
    limit = max(1, min(int(arguments.get("limit", 200)), 2000))
    matches = [item for item in base.glob(pattern) if item.is_file()]
    matches.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    files: list[str] = []
    for item in matches[:limit]:
        _guard_path(ctx, item, "read")
        files.append(str(item.relative_to(ctx.workspace)))
    return {"files": files, "truncated": len(matches) > limit}


def _grep_search(ctx: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    query = str(arguments["query"])
    if not query:
        raise ToolError("query is required")
    root = resolve_workspace_path(ctx.workspace, str(arguments.get("path") or "."), ctx.config, action="read", must_exist=True)
    pattern = str(arguments.get("pattern") or "*")
    case_sensitive = bool(arguments.get("case_sensitive", False))
    limit = max(1, min(int(arguments.get("limit", 100)), 1000))
    needle = query if case_sensitive else query.lower()
    matches: list[dict[str, Any]] = []
    for item in root.rglob("*"):
        if not item.is_file() or not fnmatch.fnmatch(item.name, pattern):
            continue
        _guard_path(ctx, item, "read")
        if item.stat().st_size > 2_000_000:
            continue
        try:
            text = item.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            haystack = line if case_sensitive else line.lower()
            if needle in haystack:
                matches.append({"path": str(item.relative_to(ctx.workspace)), "line": lineno, "text": truncate(line, 500)})
                if len(matches) >= limit:
                    return {"matches": matches, "truncated": True}
    return {"matches": matches, "truncated": False}


def _write_file(ctx: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    _ensure_writable(ctx)
    path = resolve_workspace_path(ctx.workspace, str(arguments["path"]), ctx.config, action="write")
    existed = path.exists()
    content = str(arguments["content"])
    append = bool(arguments.get("append", False))
    path.parent.mkdir(parents=True, exist_ok=True)
    if append:
        with path.open("a", encoding="utf-8", newline="") as f:
            f.write(content)
    else:
        atomic_write_text(path, content)
    if not existed:
        ctx.created_paths.add(path.resolve())
    return {"path": str(path.relative_to(ctx.workspace)), "bytes": len(content.encode("utf-8")), "append": append}


def _edit_file(ctx: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    _ensure_writable(ctx)
    path = resolve_workspace_path(ctx.workspace, str(arguments["path"]), ctx.config, action="write", must_exist=True)
    if not path.is_file():
        raise ToolError(f"not a file: {arguments['path']}")
    find = str(arguments["find"])
    replace = str(arguments["replace"])
    text = path.read_text(encoding="utf-8", errors="replace")
    if find not in text:
        raise ToolError("find text not found")
    atomic_write_text(path, text.replace(find, replace, 1))
    return {"path": str(path.relative_to(ctx.workspace)), "replacements": 1}


def _file_edit(ctx: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    _ensure_writable(ctx)
    path = resolve_workspace_path(ctx.workspace, str(arguments["path"]), ctx.config, action="write", must_exist=True)
    if not path.is_file():
        raise ToolError(f"not a file: {arguments['path']}")
    start = int(arguments["start_line"])
    end = int(arguments["end_line"])
    if start < 1 or end < start:
        raise ToolError("start_line and end_line must be positive and end_line >= start_line")
    text = path.read_text(encoding="utf-8", errors="replace")
    newline = "\r\n" if "\r\n" in text else "\n"
    had_trailing_newline = text.endswith(("\n", "\r\n"))
    lines = text.replace("\r\n", "\n").split("\n")
    if had_trailing_newline and lines[-1] == "":
        lines.pop()
    if start > len(lines) or end > len(lines):
        raise ToolError(f"line range {start}-{end} is outside file with {len(lines)} lines")
    replacement_lines = str(arguments["replacement"]).replace("\r\n", "\n").split("\n")
    next_lines = [*lines[: start - 1], *replacement_lines, *lines[end:]]
    atomic_write_text(path, newline.join(next_lines) + (newline if had_trailing_newline else ""))
    return {"path": str(path.relative_to(ctx.workspace)), "start_line": start, "end_line": end}


def _delete_file(ctx: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    _ensure_writable(ctx)
    path = resolve_workspace_path(ctx.workspace, str(arguments["path"]), ctx.config, action="write", must_exist=True)
    resolved = path.resolve()
    if resolved not in ctx.created_paths:
        raise ToolError("delete_file may only delete files created by this Task Attempt")
    if not path.is_file():
        raise ToolError("delete_file only deletes files, not directories")
    path.unlink()
    ctx.created_paths.remove(resolved)
    return {"path": str(path.relative_to(ctx.workspace)), "deleted": True}


def _run_command(ctx: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    command_raw = arguments.get("command")
    if not isinstance(command_raw, list) or not all(isinstance(item, str) for item in command_raw):
        raise ToolError("command must be an array of strings")
    command = [str(item) for item in command_raw]
    review_command(ctx.config, ctx.workspace, ctx.run_dir, command)
    cwd = resolve_workspace_path(ctx.workspace, str(arguments.get("working_dir") or "."), ctx.config, action="read", must_exist=True)
    if not cwd.is_dir():
        raise ToolError(f"working_dir is not a directory: {arguments.get('working_dir') or '.'}")
    timeout = max(1, min(int(arguments.get("timeout_seconds", 120)), ctx.config.timeout_seconds))
    env = safe_command_env(ctx.config)
    env.update(
        {
            "BATCHAGENT_TASK_ID": ctx.task.id,
            "BATCHAGENT_RUN_DIR": str(ctx.run_dir),
            "BATCHAGENT_WORKSPACE": str(ctx.workspace),
        }
    )
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return {
        "returncode": completed.returncode,
        "stdout": truncate(completed.stdout, 20000),
        "stderr": truncate(completed.stderr, 20000),
    }


def _web_fetch(ctx: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    return web_fetch(
        ctx.config,
        str(arguments["url"]),
        prompt=str(arguments.get("prompt") or ""),
        max_chars=int(arguments["max_chars"]) if "max_chars" in arguments else None,
    )


def _web_search(ctx: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    domains_raw = arguments.get("domains") or []
    domains = [str(item) for item in domains_raw] if isinstance(domains_raw, list) else []
    return web_search(ctx.config, str(arguments["query"]), int(arguments.get("limit", 5)), domains)


def _tool_search(_ctx: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    query = str(arguments["query"]).lower()
    terms = [term for term in query.split() if term]
    limit = max(1, min(int(arguments.get("limit", 8)), 20))
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for name, definition in ALL_TOOLS.items():
        function = definition.spec["function"]
        haystack = f"{name} {function.get('description', '')}".lower()
        score = sum(1 for term in terms if term in haystack)
        if score:
            scored.append((score, name, definition.spec))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return {
        "tools": [
            {
                "name": name,
                "description": spec["function"].get("description", ""),
                "parameters": spec["function"].get("parameters", {}),
                "score": score,
            }
            for score, name, spec in scored[:limit]
        ]
    }


def _submit_artifact(ctx: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    summary = str(arguments.get("summary") or "").strip()
    if not summary:
        raise ToolError("summary is required")
    metadata = arguments.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ToolError("metadata must be an object")
    artifact_path = str(arguments.get("artifact_path") or "").strip()
    ctx.submission = ArtifactSubmission(summary=summary, artifact_path=artifact_path, metadata=metadata)
    record = {
        "task_id": ctx.task.id,
        "summary": summary,
        "artifact_path": artifact_path,
        "metadata": metadata,
    }
    ctx.run_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(ctx.run_dir / "artifact.json", json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    return {"accepted": True, "message": "artifact submitted"}


def _ensure_writable(ctx: ToolContext) -> None:
    if ctx.config.workspace_mode == "readonly":
        raise ToolError("write tools are disabled in readonly workspace_mode")


def _guard_path(ctx: ToolContext, path: Path, action: str) -> None:
    resolve_workspace_path(ctx.workspace, str(path.relative_to(ctx.workspace)), ctx.config, action=action, must_exist=True)


def _spec(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


ALL_TOOLS: dict[str, ToolDefinition] = {
    "read_file": ToolDefinition(
        _spec(
            "read_file",
            "Read a UTF-8 text file inside the assigned workspace.",
            {
                "path": {"type": "string", "description": "Workspace-relative file path."},
                "offset": {"type": "integer", "description": "Character offset to start reading from.", "default": 0},
                "limit": {"type": "integer", "description": "Maximum characters to return.", "default": 12000},
            },
            ["path"],
        ),
        _read_file,
    ),
    "list_files": ToolDefinition(
        _spec(
            "list_files",
            "List files under a workspace-relative directory.",
            {
                "path": {"type": "string", "description": "Workspace-relative directory path.", "default": "."},
                "pattern": {"type": "string", "description": "Glob-like filename filter, e.g. *.py.", "default": "*"},
                "recursive": {"type": "boolean", "description": "Whether to recurse into subdirectories.", "default": False},
                "limit": {"type": "integer", "description": "Maximum files to return.", "default": 200},
            },
            [],
        ),
        _list_files,
    ),
    "glob": ToolDefinition(
        _spec(
            "glob",
            "Find files by glob pattern inside the workspace, sorted by modification time.",
            {
                "pattern": {"type": "string", "description": "Glob pattern, e.g. **/*.md."},
                "base_path": {"type": "string", "description": "Workspace-relative base directory.", "default": "."},
                "limit": {"type": "integer", "description": "Maximum files to return.", "default": 200},
            },
            ["pattern"],
        ),
        _glob,
    ),
    "grep_search": ToolDefinition(
        _spec(
            "grep_search",
            "Search text files under the workspace.",
            {
                "query": {"type": "string"},
                "path": {"type": "string", "default": "."},
                "pattern": {"type": "string", "default": "*"},
                "case_sensitive": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "default": 100},
            },
            ["query"],
        ),
        _grep_search,
    ),
    "write_file": ToolDefinition(
        _spec(
            "write_file",
            "Write a UTF-8 text file inside the workspace. Disabled in readonly workspace mode.",
            {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "append": {"type": "boolean", "default": False},
            },
            ["path", "content"],
        ),
        _write_file,
    ),
    "edit_file": ToolDefinition(
        _spec(
            "edit_file",
            "Replace the first exact text occurrence inside a workspace file.",
            {
                "path": {"type": "string"},
                "find": {"type": "string"},
                "replace": {"type": "string"},
            },
            ["path", "find", "replace"],
        ),
        _edit_file,
    ),
    "file_edit": ToolDefinition(
        _spec(
            "file_edit",
            "Replace a 1-based inclusive line range in a workspace file.",
            {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
                "replacement": {"type": "string"},
            },
            ["path", "start_line", "end_line", "replacement"],
        ),
        _file_edit,
    ),
    "delete_file": ToolDefinition(
        _spec(
            "delete_file",
            "Delete a file only if this Task Attempt created it with write_file. Directories and existing files are blocked.",
            {"path": {"type": "string"}},
            ["path"],
        ),
        _delete_file,
    ),
    "run_command": ToolDefinition(
        _spec(
            "run_command",
            "Run a command token array in the workspace after allowlist and safety review.",
            {
                "command": {"type": "array", "items": {"type": "string"}},
                "working_dir": {"type": "string", "default": "."},
                "timeout_seconds": {"type": "integer", "default": 120},
            },
            ["command"],
        ),
        _run_command,
    ),
    "web_fetch": ToolDefinition(
        _spec(
            "web_fetch",
            "Fetch a URL and return readable page text plus metadata. Supports only http/https.",
            {
                "url": {"type": "string"},
                "prompt": {"type": "string", "description": "Optional extraction instruction.", "default": ""},
                "max_chars": {"type": "integer", "description": "Maximum content characters.", "default": 20000},
            },
            ["url"],
        ),
        _web_fetch,
    ),
    "web_search": ToolDefinition(
        _spec(
            "web_search",
            "Search the public web and return result titles, URLs, snippets, and sources.",
            {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
                "domains": {"type": "array", "items": {"type": "string"}, "default": []},
            },
            ["query"],
        ),
        _web_search,
    ),
    "tool_search": ToolDefinition(
        _spec(
            "tool_search",
            "Search BatchAgent tool names, descriptions, and schemas by keyword.",
            {"query": {"type": "string"}, "limit": {"type": "integer", "default": 8}},
            ["query"],
        ),
        _tool_search,
    ),
    "submit_artifact": ToolDefinition(
        _spec(
            "submit_artifact",
            "Submit the final result for this task. Required when artifact.require_submit is true.",
            {
                "summary": {"type": "string"},
                "artifact_path": {"type": "string", "description": "Workspace-relative result file or directory path.", "default": ""},
                "metadata": {"type": "object", "description": "Structured result metadata."},
            },
            ["summary", "metadata"],
        ),
        _submit_artifact,
    ),
}
