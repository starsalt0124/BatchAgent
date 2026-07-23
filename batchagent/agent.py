from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .models import AgentRunResult, BatchConfig, Task
from .provider import ProviderError, create_provider
from .skills import render_skills_prompt
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
    work_id: str = "",
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> AgentRunResult:
    run_id = run_id or uuid.uuid4().hex[:12]
    run_dir = task_run_dir(config, manifest_path, task, run_id, work_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace = prepare_workspace(config, manifest_path, task, run_dir)
    store.start_run(run_id, task.id, task.attempts, run_dir, work_id=work_id)

    messages = _initial_messages(config, task, store, workspace, manifest_path)
    ctx = ToolContext(config=config, task=task, workspace=workspace, run_dir=run_dir)
    provider = create_provider(config)
    specs = tool_specs(config)
    seq = 0
    recovery_attempts = 0
    model_call_seq = 0
    usage_totals: dict[str, int] = {}

    try:
        if config.artifact.require_submit and "submit_artifact" not in config.tools:
            raise AgentExecutionError("artifact.require_submit is true but submit_artifact is not loaded in tools")
        write_json(
            run_dir / "task.json",
            {
                "run_id": work_id,
                "attempt_id": run_id,
                "task": task.__dict__,
                "workspace": str(workspace),
                "harness": "native",
                "run_vars": config.run_vars,
            },
        )
        for message in messages:
            seq += 1
            store.add_message(run_id, seq, message["role"], str(message.get("content") or ""), message)

        for _turn in range(config.max_turns):
            try:
                call_started = time.perf_counter()
                assistant = await asyncio.to_thread(provider.chat, messages, specs, _delta_callback(progress_callback, task.id, run_id, work_id))
            except Exception as exc:
                if _is_recoverable_provider_error(exc) and recovery_attempts < config.dialog_recovery_attempts:
                    recovery_attempts += 1
                    _emit(
                        progress_callback,
                        {
                            "type": "dialog_recovery",
                            "task_id": task.id,
                            "run_id": run_id,
                            "work_id": work_id,
                            "reason": str(exc),
                            "attempt": recovery_attempts,
                            "max_attempts": config.dialog_recovery_attempts,
                        },
                    )
                    await asyncio.sleep(min(2**recovery_attempts, 10))
                    continue
                raise
            model_call_seq += 1
            usage = _provider_usage(assistant.raw)
            store.add_model_call(
                run_id,
                model_call_seq,
                config.provider,
                str(assistant.raw.get("_bagent_response", {}).get("model") or config.model),
                usage,
                latency_ms=max(0, int((time.perf_counter() - call_started) * 1000)),
            )
            _merge_usage(usage_totals, usage)
            seq += 1
            store.add_message(run_id, seq, "assistant", assistant.content, assistant.raw)
            _emit(
                progress_callback,
                {
                    "type": "assistant_message",
                    "task_id": task.id,
                    "run_id": run_id,
                    "work_id": work_id,
                    "content": assistant.content or "",
                    "timestamp": "",
                },
            )
            wire_message = _assistant_message_for_wire(assistant.raw)
            if wire_message is not None:
                messages.append(wire_message)

            if assistant.tool_calls:
                for tool_call in assistant.tool_calls:
                    _emit(
                        progress_callback,
                        {
                            "type": "tool_started",
                            "task_id": task.id,
                            "run_id": run_id,
                            "work_id": work_id,
                            "tool": tool_call.name,
                            "arguments": tool_call.arguments,
                        },
                    )
                    result, error = await _invoke_tool_safely(ctx, tool_call.name, tool_call.arguments)
                    _emit(
                        progress_callback,
                        {
                            "type": "tool_finished",
                            "task_id": task.id,
                            "run_id": run_id,
                            "work_id": work_id,
                            "tool": tool_call.name,
                            "arguments": tool_call.arguments,
                            "error": error,
                            "result": result,
                        },
                    )
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
                    _emit(
                        progress_callback,
                        {
                            "type": "artifact_submitted",
                            "task_id": task.id,
                            "run_id": run_id,
                            "work_id": work_id,
                            "summary": ctx.submission.summary,
                            "artifact_path": ctx.submission.artifact_path,
                            "metadata": ctx.submission.metadata,
                        },
                    )
                    validate_artifact(config, task, workspace, run_dir, ctx.submission)
                    store.add_artifact(
                        run_id,
                        task.id,
                        ctx.submission.summary,
                        ctx.submission.artifact_path,
                        ctx.submission.metadata,
                    )
                    result_record = {
                        "artifact_path": ctx.submission.artifact_path,
                        "summary": ctx.submission.summary,
                        "metadata": ctx.submission.metadata,
                    }
                    store.finish_attempt(run_id, "done", result=result_record, usage=usage_totals)
                    return AgentRunResult(
                        success=True,
                        task_id=task.id,
                        run_dir=run_dir,
                        work_id=work_id,
                        run_id=work_id,
                        attempt_id=run_id,
                        harness="native",
                        usage=dict(usage_totals),
                        artifact_record_path=run_dir / "artifact.json",
                        artifact=ctx.submission,
                    )
                continue

            if config.artifact.require_submit:
                if recovery_attempts < config.dialog_recovery_attempts:
                    recovery_attempts += 1
                    recovery_message = _submit_artifact_recovery_prompt()
                    messages.append({"role": "user", "content": recovery_message})
                    seq += 1
                    store.add_message(run_id, seq, "user", recovery_message, messages[-1])
                    _emit(
                        progress_callback,
                        {
                            "type": "dialog_recovery",
                            "task_id": task.id,
                            "run_id": run_id,
                            "work_id": work_id,
                            "reason": "model returned final text without submit_artifact",
                            "attempt": recovery_attempts,
                            "max_attempts": config.dialog_recovery_attempts,
                        },
                    )
                    continue
                raise AgentExecutionError("model returned final text without submit_artifact")
            store.finish_attempt(run_id, "done", result={"output": assistant.content or ""}, usage=usage_totals)
            return AgentRunResult(
                success=True,
                task_id=task.id,
                run_dir=run_dir,
                work_id=work_id,
                run_id=work_id,
                attempt_id=run_id,
                harness="native",
                usage=dict(usage_totals),
            )

        raise AgentExecutionError(f"max_turns exceeded: {config.max_turns}")
    except (AgentExecutionError, ArtifactValidationError, ToolError, Exception) as exc:
        error = str(exc)
        store.finish_attempt(run_id, "failed", error, usage=usage_totals)
        return AgentRunResult(
            success=False,
            task_id=task.id,
            run_dir=run_dir,
            work_id=work_id,
            run_id=work_id,
            attempt_id=run_id,
            harness="native",
            usage=dict(usage_totals),
            error=error,
        )


def _initial_messages(config: BatchConfig, task: Task, store: SessionStore, workspace: Path, manifest_path: Path) -> list[dict[str, Any]]:
    system = render_template(config.system_prompt, task, config, workspace=workspace).strip()
    protocol = _protocol_prompt(config) if config.inject_batchagent_protocol else ""
    skills = render_skills_prompt(config, manifest_path)
    memory = _memory_prompt(config, task, store, workspace, manifest_path)
    user = render_template(config.user_prompt_template, task, config, workspace=workspace).strip()
    messages: list[dict[str, Any]] = []
    messages.append({"role": "system", "content": "\n\n".join(part for part in [system, protocol, skills, memory] if part)})
    messages.append({"role": "user", "content": user})
    return messages


def build_task_prompt(
    config: BatchConfig,
    task: Task,
    store: SessionStore,
    workspace: Path,
    manifest_path: Path,
) -> str:
    """Render the native task context as one prompt for an external harness."""

    messages = _initial_messages(config, task, store, workspace, manifest_path)
    system = str(messages[0].get("content") or "") if messages else ""
    user = str(messages[1].get("content") or "") if len(messages) > 1 else ""
    if not system:
        return user
    sections = []
    if system:
        sections.append("System instructions:\n" + system)
    if user:
        sections.append("Assigned task:\n" + user)
    return "\n\n".join(sections)


def _protocol_prompt(config: BatchConfig) -> str:
    if "run_command" in config.tools:
        if config.command_policy == "blacklist":
            command_note = "run_command is loaded in blacklist mode; ordinary inspection commands are allowed, but high-risk commands and blocked patterns are rejected."
        else:
            command_note = "run_command is loaded, but it is available only for allowlisted commands that pass safety review."
    else:
        command_note = "No shell command tool is available for this task."
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


def _memory_prompt(
    config: BatchConfig,
    task: Task,
    store: SessionStore,
    workspace: Path,
    manifest_path: Path,
) -> str:
    parts: list[str] = []
    for memory_file in config.memory_files:
        path = Path(memory_file)
        if not path.is_absolute():
            path = workspace / path
        if path.exists() and path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            parts.append(f"Memory file {memory_file}:\n{text[:20000]}")
    failures = store.recent_failures(task.id, manifest_path=manifest_path)
    if failures:
        parts.append("Previous failed attempts for this task:\n" + "\n".join(failures))
    return "\n\n".join(parts)


def _provider_usage(raw: dict[str, Any]) -> dict[str, Any]:
    response = raw.get("_bagent_response")
    if not isinstance(response, dict):
        return {}
    usage = response.get("usage")
    return dict(usage) if isinstance(usage, dict) else {}


def _merge_usage(totals: dict[str, int], usage: dict[str, Any]) -> None:
    prompt_details = usage.get("prompt_tokens_details")
    completion_details = usage.get("completion_tokens_details")
    values = {
        "prompt_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
        "completion_tokens": usage.get("completion_tokens", usage.get("output_tokens")),
        "total_tokens": usage.get("total_tokens"),
        "cached_tokens": usage.get("cached_tokens")
        or (prompt_details.get("cached_tokens") if isinstance(prompt_details, dict) else None),
        "reasoning_tokens": usage.get("reasoning_tokens")
        or (completion_details.get("reasoning_tokens") if isinstance(completion_details, dict) else None),
    }
    if values["total_tokens"] is None and (
        values["prompt_tokens"] is not None or values["completion_tokens"] is not None
    ):
        values["total_tokens"] = int(values["prompt_tokens"] or 0) + int(values["completion_tokens"] or 0)
    for key, value in values.items():
        if value is None or isinstance(value, bool):
            continue
        try:
            totals[key] = totals.get(key, 0) + int(value)
        except (TypeError, ValueError):
            continue


def _assistant_message_for_wire(raw: dict[str, Any]) -> dict[str, Any] | None:
    content = raw.get("content")
    tool_calls = raw.get("tool_calls") or []
    has_content = isinstance(content, str) and content != ""
    if not has_content and not tool_calls:
        return None

    message: dict[str, Any] = {"role": "assistant"}
    if has_content:
        message["content"] = content
    else:
        message["content"] = None
    if raw.get("reasoning_content"):
        message["reasoning_content"] = raw.get("reasoning_content")
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _submit_artifact_recovery_prompt() -> str:
    return (
        "If you have completed the task, call submit_artifact now with the required "
        "artifact path and metadata. If the task is not complete, continue the required "
        "work and then call submit_artifact. Do not finish with plain text only."
    )


def _is_recoverable_provider_error(exc: Exception) -> bool:
    message = str(exc)
    if isinstance(exc, ProviderError):
        if re.search(r"\b(?:400|401|403|404)\b", message) and "reasoning_content" not in message:
            return False
        return True
    return isinstance(exc, (OSError, ConnectionError, TimeoutError))


async def _invoke_tool_safely(ctx: ToolContext, name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
    try:
        result = await asyncio.to_thread(invoke_tool, ctx, name, arguments)
        return result, ""
    except Exception as exc:
        return {}, str(exc)


def _delta_callback(
    progress_callback: Callable[[dict[str, Any]], None] | None,
    task_id: str,
    run_id: str,
    work_id: str,
) -> Callable[[str], None] | None:
    if progress_callback is None:
        return None

    def callback(delta: str) -> None:
        _emit(progress_callback, {"type": "model_delta", "task_id": task_id, "run_id": run_id, "work_id": work_id, "delta": delta})

    return callback


def _emit(callback: Callable[[dict[str, Any]], None] | None, event: dict[str, Any]) -> None:
    if callback is None:
        return
    try:
        callback(event)
    except Exception:
        pass
