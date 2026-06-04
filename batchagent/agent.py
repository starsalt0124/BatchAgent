from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from .models import AgentRunResult, BatchConfig, Task
from .provider import create_provider
from .store import SessionStore
from .template import render_template
from .tools import ToolContext, ToolError, invoke_tool, tool_specs
from .util import write_json
from .validation import ArtifactValidationError, validate_artifact
from .workspace import prepare_workspace, task_run_dir


class AgentExecutionError(RuntimeError):
    pass


async def run_agent_task(
    manifest_path: Path,
    config: BatchConfig,
    task: Task,
    store: SessionStore,
    run_id: str | None = None,
) -> AgentRunResult:
    run_id = run_id or uuid.uuid4().hex[:12]
    run_dir = task_run_dir(config, manifest_path, task, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace = prepare_workspace(config, manifest_path, task, run_dir)
    store.start_run(run_id, task.id, task.attempts, run_dir)

    messages = _initial_messages(config, task, store, workspace)
    ctx = ToolContext(config=config, task=task, workspace=workspace, run_dir=run_dir)
    provider = create_provider(config)
    specs = tool_specs(config)
    seq = 0

    try:
        if config.artifact.require_submit and "submit_artifact" not in config.tools:
            raise AgentExecutionError("artifact.require_submit is true but submit_artifact is not loaded in tools")
        write_json(run_dir / "task.json", {"task": task.__dict__, "workspace": str(workspace)})
        for message in messages:
            seq += 1
            store.add_message(run_id, seq, message["role"], str(message.get("content") or ""), message)

        for _turn in range(config.max_turns):
            assistant = await asyncio.to_thread(provider.chat, messages, specs)
            seq += 1
            store.add_message(run_id, seq, "assistant", assistant.content, assistant.raw)
            messages.append(_assistant_message_for_wire(assistant.raw))

            if assistant.tool_calls:
                for tool_call in assistant.tool_calls:
                    result, error = await _invoke_tool_safely(ctx, tool_call.name, tool_call.arguments)
                    seq += 1
                    store.add_tool_event(run_id, seq, tool_call.name, tool_call.arguments, result, error)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps(result if not error else {"error": error}, ensure_ascii=False),
                        }
                    )
                    store.add_message(run_id, seq, "tool", messages[-1]["content"], messages[-1])

                if ctx.submission is not None:
                    validate_artifact(config, task, workspace, run_dir, ctx.submission)
                    store.add_artifact(
                        run_id,
                        task.id,
                        ctx.submission.summary,
                        ctx.submission.artifact_path,
                        ctx.submission.metadata,
                    )
                    store.finish_run(run_id, "done")
                    return AgentRunResult(
                        success=True,
                        task_id=task.id,
                        run_dir=run_dir,
                        artifact_record_path=run_dir / "artifact.json",
                        artifact=ctx.submission,
                    )
                continue

            if config.artifact.require_submit:
                raise AgentExecutionError("model returned final text without submit_artifact")
            store.finish_run(run_id, "done")
            return AgentRunResult(success=True, task_id=task.id, run_dir=run_dir)

        raise AgentExecutionError(f"max_turns exceeded: {config.max_turns}")
    except (AgentExecutionError, ArtifactValidationError, ToolError, Exception) as exc:
        error = str(exc)
        store.finish_run(run_id, "failed", error)
        return AgentRunResult(success=False, task_id=task.id, run_dir=run_dir, error=error)


def _initial_messages(config: BatchConfig, task: Task, store: SessionStore, workspace: Path) -> list[dict[str, Any]]:
    system = config.system_prompt.strip()
    protocol = _protocol_prompt(config)
    memory = _memory_prompt(config, task, store, workspace)
    user = render_template(config.user_prompt_template, task, config).strip()
    messages: list[dict[str, Any]] = []
    messages.append({"role": "system", "content": "\n\n".join(part for part in [system, protocol, memory] if part)})
    messages.append({"role": "user", "content": user})
    return messages


def _protocol_prompt(config: BatchConfig) -> str:
    command_note = (
        "run_command is loaded, but it is available only for allowlisted commands that pass safety review."
        if "run_command" in config.tools
        else "No shell command tool is available for this task."
    )
    submit_note = (
        "You must call submit_artifact exactly once when the task is complete. The task will not be marked done from text alone."
        if config.artifact.require_submit
        else "You may call submit_artifact to attach structured output."
    )
    tool_note = (
        "Loaded tools: " + ", ".join(config.tools)
        if config.tools
        else "No tools are loaded. Complete the task from the prompt context only."
    )
    return "\n".join(
        [
            "BatchAgent harness protocol:",
            "- You are assigned exactly one task. Do not claim work on any other task.",
            f"- {tool_note}",
            "- Use only the tools listed above; unloaded tools are intentionally unavailable.",
            "- File and command tools are restricted to the workspace and policy checks.",
            f"- {command_note}",
            f"- {submit_note}",
            "- submit_artifact metadata should include task-specific machine-readable fields, not only prose.",
        ]
    )


def _memory_prompt(config: BatchConfig, task: Task, store: SessionStore, workspace: Path) -> str:
    parts: list[str] = []
    for memory_file in config.memory_files:
        path = Path(memory_file)
        if not path.is_absolute():
            path = workspace / path
        if path.exists() and path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            parts.append(f"Memory file {memory_file}:\n{text[:20000]}")
    failures = store.recent_failures(task.id)
    if failures:
        parts.append("Previous failed attempts for this task:\n" + "\n".join(failures))
    return "\n\n".join(parts)


def _assistant_message_for_wire(raw: dict[str, Any]) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": raw.get("content")}
    if raw.get("tool_calls"):
        message["tool_calls"] = raw["tool_calls"]
    return message


async def _invoke_tool_safely(ctx: ToolContext, name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
    try:
        result = await asyncio.to_thread(invoke_tool, ctx, name, arguments)
        return result, ""
    except Exception as exc:
        return {}, str(exc)
