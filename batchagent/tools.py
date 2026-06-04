from __future__ import annotations

import fnmatch
import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import ArtifactSubmission, BatchConfig, Task
from .util import atomic_write_text, safe_join, truncate


class ToolError(RuntimeError):
    pass


@dataclass
class ToolContext:
    config: BatchConfig
    task: Task
    workspace: Path
    run_dir: Path
    submission: ArtifactSubmission | None = None


def tool_specs(config: BatchConfig) -> list[dict[str, Any]]:
    specs = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a UTF-8 text file inside the assigned workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Workspace-relative file path."},
                        "offset": {"type": "integer", "description": "Character offset to start reading from.", "default": 0},
                        "limit": {"type": "integer", "description": "Maximum characters to return.", "default": 12000},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List files under a workspace-relative directory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Workspace-relative directory path.", "default": "."},
                        "pattern": {"type": "string", "description": "Glob-like filename filter, e.g. *.py.", "default": "*"},
                        "recursive": {"type": "boolean", "description": "Whether to recurse into subdirectories.", "default": False},
                        "limit": {"type": "integer", "description": "Maximum files to return.", "default": 200},
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write a UTF-8 text file inside the assigned workspace. Disabled in readonly workspace mode.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Workspace-relative file path."},
                        "content": {"type": "string", "description": "Complete file content to write."},
                        "append": {"type": "boolean", "description": "Append instead of replacing the file.", "default": False},
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "submit_artifact",
                "description": "Submit the final result for this task. This is required before a task can be marked done.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string", "description": "Concise completion summary."},
                        "artifact_path": {"type": "string", "description": "Workspace-relative result file or directory path.", "default": ""},
                        "metadata": {"type": "object", "description": "Structured result metadata."},
                    },
                    "required": ["summary", "metadata"],
                    "additionalProperties": False,
                },
            },
        },
    ]
    if config.allowed_command_prefixes:
        specs.insert(
            3,
            {
                "type": "function",
                "function": {
                    "name": "run_command",
                    "description": "Run an allowlisted command in the workspace.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "array",
                                "description": "Command tokens, e.g. ['python', '-m', 'pytest'].",
                                "items": {"type": "string"},
                            },
                            "working_dir": {
                                "type": "string",
                                "description": "Workspace-relative working directory.",
                                "default": ".",
                            },
                            "timeout_seconds": {
                                "type": "integer",
                                "description": "Command timeout in seconds.",
                                "default": 120,
                            },
                        },
                        "required": ["command"],
                        "additionalProperties": False,
                    },
                },
            },
        )
    return specs


def invoke_tool(ctx: ToolContext, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "read_file":
        return _read_file(ctx, arguments)
    if name == "list_files":
        return _list_files(ctx, arguments)
    if name == "write_file":
        return _write_file(ctx, arguments)
    if name == "run_command":
        return _run_command(ctx, arguments)
    if name == "submit_artifact":
        return _submit_artifact(ctx, arguments)
    raise ToolError(f"unknown tool: {name}")


def _read_file(ctx: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    path = safe_join(ctx.workspace, str(arguments["path"]), must_exist=True)
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
    root = safe_join(ctx.workspace, str(arguments.get("path") or "."), must_exist=True)
    if not root.is_dir():
        raise ToolError(f"not a directory: {arguments.get('path') or '.'}")
    pattern = str(arguments.get("pattern") or "*")
    recursive = bool(arguments.get("recursive", False))
    limit = max(1, min(int(arguments.get("limit", 200)), 2000))
    iterator = root.rglob("*") if recursive else root.iterdir()
    files: list[str] = []
    for item in iterator:
        if item.is_file() and fnmatch.fnmatch(item.name, pattern):
            files.append(str(item.relative_to(ctx.workspace)))
            if len(files) >= limit:
                break
    return {"files": files, "truncated": len(files) >= limit}


def _write_file(ctx: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    if ctx.config.workspace_mode == "readonly":
        raise ToolError("write_file is disabled in readonly workspace_mode")
    path = safe_join(ctx.workspace, str(arguments["path"]))
    content = str(arguments["content"])
    append = bool(arguments.get("append", False))
    path.parent.mkdir(parents=True, exist_ok=True)
    if append:
        with path.open("a", encoding="utf-8", newline="") as f:
            f.write(content)
    else:
        atomic_write_text(path, content)
    return {"path": str(path.relative_to(ctx.workspace)), "bytes": len(content.encode("utf-8")), "append": append}


def _run_command(ctx: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    command_raw = arguments.get("command")
    if not isinstance(command_raw, list) or not all(isinstance(item, str) for item in command_raw):
        raise ToolError("command must be an array of strings")
    command = [str(item) for item in command_raw]
    if not _is_allowed_command(ctx.config, command):
        raise ToolError(f"command is not allowlisted: {command}")
    cwd = safe_join(ctx.workspace, str(arguments.get("working_dir") or "."), must_exist=True)
    if not cwd.is_dir():
        raise ToolError(f"working_dir is not a directory: {arguments.get('working_dir') or '.'}")
    timeout = max(1, min(int(arguments.get("timeout_seconds", 120)), ctx.config.timeout_seconds))
    env = os.environ.copy()
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


def _is_allowed_command(config: BatchConfig, command: list[str]) -> bool:
    for prefix in config.allowed_command_prefixes:
        tokens = _prefix_tokens(prefix)
        if tokens and command[: len(tokens)] == tokens:
            return True
    return False


def _prefix_tokens(prefix: list[str] | str) -> list[str]:
    if isinstance(prefix, list):
        return [str(item) for item in prefix]
    return shlex.split(str(prefix), posix=os.name != "nt")

