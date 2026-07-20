from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .harness_mcp import HarnessMcpError, load_progress_events, load_submission
from .models import ArtifactSubmission, BatchConfig, HarnessConfig, Task
from .util import atomic_write_text


ProgressCallback = Callable[[dict[str, Any]], None]
MAX_STREAM_TAIL = 20_000
MAX_EVENT_LINE = 1_048_576


class HarnessError(RuntimeError):
    pass


@dataclass(frozen=True)
class HarnessCapabilities:
    structured_events: bool
    mcp_tools: bool
    sessions: bool
    usage: bool


@dataclass(frozen=True)
class HarnessProbe:
    name: str
    available: bool
    executable: str = ""
    version: str = ""
    error: str = ""
    capabilities: HarnessCapabilities = HarnessCapabilities(False, False, False, False)


@dataclass
class HarnessRequest:
    """Everything needed to execute one task attempt.

    ``run_id`` identifies the containing batch run. ``attempt_id`` uniquely
    identifies this execution of ``task``; an external harness session id is a
    third, independent identifier discovered from the harness event stream.
    """

    run_id: str
    attempt_id: str
    manifest_path: Path
    config: BatchConfig
    task: Task
    workspace: Path
    run_dir: Path
    prompt: str = ""
    harness_config: HarnessConfig | None = None
    timeout_seconds: float | None = None
    environment: Mapping[str, str] | None = None
    progress_callback: ProgressCallback | None = None
    store: Any | None = None
    nonce: str = field(default_factory=lambda: secrets.token_urlsafe(24))

    @property
    def runtime_config(self) -> HarnessConfig:
        return self.harness_config or self.config.harness

    @property
    def spool_dir(self) -> Path:
        return self.run_dir / "harness-ipc"


@dataclass(frozen=True)
class HarnessInvocation:
    command: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    prompt: str
    mcp_config_path: Path | None = None


@dataclass
class HarnessResult:
    harness_name: str
    run_id: str
    attempt_id: str
    task_id: str
    success: bool
    exit_code: int | None = None
    pid: int | None = None
    session_id: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    submission: ArtifactSubmission | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    output: Any = None
    error: str = ""
    timed_out: bool = False
    nonce: str = ""


@dataclass
class _StreamState:
    session_id: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    stdout_tail: str = ""
    stderr_tail: str = ""
    output: Any = None


class HarnessAdapter:
    name = ""
    executable = ""
    capabilities = HarnessCapabilities(False, False, False, False)

    def command_prefix(self, config: HarnessConfig) -> list[str]:
        return list(config.command) if config.command else [self.executable]

    async def probe(self, config: HarnessConfig | None = None, *, timeout_seconds: float = 5.0) -> HarnessProbe:
        runtime = config or HarnessConfig(name=self.name)
        prefix = self.command_prefix(runtime)
        if not prefix or not prefix[0]:
            return HarnessProbe(self.name, False, error="harness command is empty", capabilities=self.capabilities)
        _validate_harness_command(self.name, prefix, runtime.extra_args)
        executable = _resolve_executable(prefix[0])
        if not executable:
            return HarnessProbe(
                self.name,
                False,
                executable=prefix[0],
                error=f"executable not found: {prefix[0]}",
                capabilities=self.capabilities,
            )
        command = [executable, *prefix[1:], "--version"]
        env = _minimal_environment(runtime.env_allowlist)
        process: asyncio.subprocess.Process | None = None
        probe_dir = tempfile.TemporaryDirectory(prefix="bagent-probe-")
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=probe_dir.name,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                **_process_group_kwargs(),
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=max(0.1, timeout_seconds))
        except asyncio.TimeoutError:
            if process is not None:
                await _terminate_process_tree(process)
            return HarnessProbe(
                self.name,
                False,
                executable=executable,
                error="version probe timed out",
                capabilities=self.capabilities,
            )
        except OSError as exc:
            return HarnessProbe(
                self.name,
                False,
                executable=executable,
                error=str(exc),
                capabilities=self.capabilities,
            )
        finally:
            probe_dir.cleanup()
        if process.returncode != 0:
            detail = _decode(stderr or stdout).strip()
            return HarnessProbe(
                self.name,
                False,
                executable=executable,
                error=detail[:500] or f"version probe exited {process.returncode}",
                capabilities=self.capabilities,
            )
        version = next((line.strip() for line in _decode(stdout or stderr).splitlines() if line.strip()), "")
        return HarnessProbe(
            self.name,
            True,
            executable=executable,
            version=version[:200],
            capabilities=self.capabilities,
        )

    async def build_invocation(self, request: HarnessRequest) -> HarnessInvocation:
        raise NotImplementedError

    async def run(self, request: HarnessRequest) -> HarnessResult:
        invocation = await self.build_invocation(request)
        return await _run_external_process(self, request, invocation)


class NativeHarness(HarnessAdapter):
    name = "native"
    executable = sys.executable
    capabilities = HarnessCapabilities(True, False, False, False)

    async def probe(self, config: HarnessConfig | None = None, *, timeout_seconds: float = 5.0) -> HarnessProbe:
        from . import __version__

        return HarnessProbe(
            name=self.name,
            available=True,
            executable=sys.executable,
            version=__version__,
            capabilities=self.capabilities,
        )

    async def build_invocation(self, request: HarnessRequest) -> HarnessInvocation:
        raise HarnessError("the native harness does not use a subprocess invocation")

    async def run(self, request: HarnessRequest) -> HarnessResult:
        if request.store is None:
            raise HarnessError("the native harness requires a SessionStore in HarnessRequest.store")
        from .agent import run_agent_task

        result = await run_agent_task(
            request.manifest_path,
            request.config,
            request.task,
            request.store,
            run_id=request.attempt_id,
            work_id=request.run_id,
            progress_callback=request.progress_callback,
        )
        return HarnessResult(
            harness_name=self.name,
            run_id=request.run_id,
            attempt_id=request.attempt_id,
            task_id=request.task.id,
            success=result.success,
            exit_code=0 if result.success else 1,
            submission=result.artifact,
            usage=dict(result.usage),
            error=result.error,
            nonce=request.nonce,
        )


class OpenCodeHarness(HarnessAdapter):
    name = "opencode"
    executable = "opencode"
    capabilities = HarnessCapabilities(True, True, True, True)

    async def build_invocation(self, request: HarnessRequest) -> HarnessInvocation:
        runtime = request.runtime_config
        _require_completion_transport(request)
        prefix = self.command_prefix(runtime)
        _validate_harness_command(self.name, prefix, runtime.extra_args)
        command = [*prefix, "run", "--format", "json", "--dir", str(request.workspace)]
        if runtime.model:
            command.extend(["--model", runtime.model])
        if runtime.agent:
            command.extend(["--agent", runtime.agent])
        command.extend(runtime.extra_args)

        env = _request_environment(request)
        if runtime.inject_tools:
            mcp_environment = _mcp_environment(request)
            existing = env.get("OPENCODE_CONFIG_CONTENT", "").strip()
            if existing:
                try:
                    inline_config = json.loads(existing)
                except json.JSONDecodeError as exc:
                    raise HarnessError("OPENCODE_CONFIG_CONTENT must contain a JSON object before bagent can inject MCP") from exc
                if not isinstance(inline_config, dict):
                    raise HarnessError("OPENCODE_CONFIG_CONTENT must contain a JSON object")
            else:
                inline_config = {}
            mcp = inline_config.setdefault("mcp", {})
            if not isinstance(mcp, dict):
                raise HarnessError("OpenCode inline config field mcp must be an object")
            mcp["bagent"] = {
                "type": "local",
                "command": _mcp_command(),
                "environment": mcp_environment,
                "enabled": True,
            }
            env["OPENCODE_CONFIG_CONTENT"] = json.dumps(inline_config, ensure_ascii=False, separators=(",", ":"))
        return HarnessInvocation(
            command=tuple(command),
            cwd=request.workspace,
            env=env,
            prompt=_external_prompt(request),
        )


class ClaudeCodeHarness(HarnessAdapter):
    name = "claude"
    executable = "claude"
    capabilities = HarnessCapabilities(True, True, True, True)

    async def build_invocation(self, request: HarnessRequest) -> HarnessInvocation:
        runtime = request.runtime_config
        _require_completion_transport(request)
        prefix = self.command_prefix(runtime)
        _validate_harness_command(self.name, prefix, runtime.extra_args)
        command = [*prefix, "-p", "--output-format", "stream-json", "--verbose"]
        mcp_config_path: Path | None = None
        if runtime.inject_tools:
            config_dir = request.run_dir / "harness"
            config_dir.mkdir(parents=True, exist_ok=True)
            mcp_config_path = config_dir / "claude-mcp.json"
            mcp_config = {
                "mcpServers": {
                    "bagent": {
                        "type": "stdio",
                        "command": _mcp_command()[0],
                        "args": _mcp_command()[1:],
                        "env": _mcp_environment(request),
                    }
                }
            }
            atomic_write_text(mcp_config_path, json.dumps(mcp_config, ensure_ascii=False, indent=2) + "\n")
            try:
                mcp_config_path.chmod(0o600)
            except OSError:
                pass
            command.extend(
                [
                    "--mcp-config",
                    str(mcp_config_path),
                    "--strict-mcp-config",
                    "--allowedTools",
                    "mcp__bagent__submit_artifact",
                    "mcp__bagent__report_progress",
                ]
            )
        if runtime.model:
            command.extend(["--model", runtime.model])
        if runtime.agent:
            command.extend(["--agent", runtime.agent])
        command.extend(runtime.extra_args)
        return HarnessInvocation(
            command=tuple(command),
            cwd=request.workspace,
            env=_request_environment(request),
            prompt=_external_prompt(request),
            mcp_config_path=mcp_config_path,
        )


class HarnessRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, HarnessAdapter] = {}
        self._aliases: dict[str, str] = {}

    def register(self, adapter: HarnessAdapter, *aliases: str) -> None:
        name = adapter.name.strip().lower()
        if not name:
            raise ValueError("harness adapter name is empty")
        self._adapters[name] = adapter
        self._aliases[name] = name
        for alias in aliases:
            self._aliases[alias.strip().lower()] = name

    def get(self, name: str) -> HarnessAdapter:
        normalized = name.strip().lower()
        canonical = self._aliases.get(normalized, normalized)
        try:
            return self._adapters[canonical]
        except KeyError as exc:
            raise HarnessError(f"unknown harness: {name}; available: {', '.join(self.names())}") from exc

    def names(self) -> list[str]:
        return sorted(self._adapters)

    async def probe(self, name: str, config: HarnessConfig | None = None) -> HarnessProbe:
        return await self.get(name).probe(config)


DEFAULT_HARNESS_REGISTRY = HarnessRegistry()
DEFAULT_HARNESS_REGISTRY.register(NativeHarness(), "builtin", "built-in")
DEFAULT_HARNESS_REGISTRY.register(OpenCodeHarness(), "open-code")
DEFAULT_HARNESS_REGISTRY.register(ClaudeCodeHarness(), "claude-code", "claude_code")


def get_harness(name: str) -> HarnessAdapter:
    return DEFAULT_HARNESS_REGISTRY.get(name)


def available_harnesses() -> list[str]:
    return DEFAULT_HARNESS_REGISTRY.names()


async def probe_harness(name: str, config: HarnessConfig | None = None) -> HarnessProbe:
    return await DEFAULT_HARNESS_REGISTRY.probe(name, config)


async def run_harness(request: HarnessRequest, name: str | None = None) -> HarnessResult:
    selected = name or request.runtime_config.name or "native"
    return await get_harness(selected).run(request)


async def _run_external_process(
    adapter: HarnessAdapter,
    request: HarnessRequest,
    invocation: HarnessInvocation,
) -> HarnessResult:
    if not request.run_id.strip() or not request.attempt_id.strip() or not request.task.id.strip():
        raise HarnessError("run_id, attempt_id, and task.id are required")
    request.run_dir.mkdir(parents=True, exist_ok=True)
    request.workspace.resolve(strict=True)
    state = _StreamState()
    process: asyncio.subprocess.Process | None = None
    timed_out = False
    process_error = ""
    seen_progress: set[str] = set()
    progress_stop = asyncio.Event()
    stdout_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    progress_task: asyncio.Task[None] | None = None

    try:
        process = await asyncio.create_subprocess_exec(
            *invocation.command,
            cwd=str(invocation.cwd),
            env=invocation.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=MAX_EVENT_LINE,
            **_process_group_kwargs(),
        )
        _emit(
            request,
            {
                "type": "harness_started",
                "harness": adapter.name,
                "pid": process.pid,
                "executable": Path(invocation.command[0]).name,
            },
        )
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_task = asyncio.create_task(_consume_stdout(process.stdout, request, adapter.name, state))
        stderr_task = asyncio.create_task(_consume_stderr(process.stderr, request, adapter.name, state))
        if request.runtime_config.inject_tools:
            progress_task = asyncio.create_task(_poll_progress_spool(request, seen_progress, progress_stop))

        if process.stdin is not None:
            try:
                process.stdin.write(invocation.prompt.encode("utf-8"))
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

        timeout = request.timeout_seconds if request.timeout_seconds is not None else request.config.timeout_seconds
        try:
            await asyncio.wait_for(process.wait(), timeout=max(0.1, float(timeout)))
        except asyncio.TimeoutError:
            timed_out = True
            process_error = f"harness timed out after {timeout} seconds"
            await _terminate_process_tree(process)
    except asyncio.CancelledError:
        if process is not None:
            await _terminate_process_tree(process)
        raise
    except (OSError, ValueError) as exc:
        process_error = str(exc)
        if process is not None:
            await _terminate_process_tree(process)
    finally:
        if process is not None:
            if process.returncode is None:
                await _terminate_process_tree(process)
            elif os.name != "nt":
                await _cleanup_posix_process_group(process.pid)
        readers = [task for task in (stdout_task, stderr_task) if task is not None]
        if readers:
            await asyncio.gather(*readers, return_exceptions=True)
        progress_stop.set()
        if progress_task is not None:
            await asyncio.gather(progress_task, return_exceptions=True)
        if request.runtime_config.inject_tools:
            try:
                await _emit_new_progress_events(request, seen_progress)
            except HarnessMcpError as exc:
                process_error = process_error or str(exc)

    submission: ArtifactSubmission | None = None
    if request.runtime_config.inject_tools:
        try:
            submission = load_submission(
                request.spool_dir,
                nonce=request.nonce,
                run_id=request.run_id,
                attempt_id=request.attempt_id,
                task_id=request.task.id,
            )
        except HarnessMcpError as exc:
            process_error = process_error or str(exc)

    exit_code = process.returncode if process is not None else None
    if not process_error and exit_code not in {0}:
        detail = state.stderr_tail.strip() or state.stdout_tail.strip()
        process_error = f"harness exited with code {exit_code}" + (f": {detail[-1000:]}" if detail else "")
    if not process_error and request.config.artifact.require_submit and submission is None:
        process_error = "harness exited without calling submit_artifact"
    success = not process_error and exit_code == 0
    result = HarnessResult(
        harness_name=adapter.name,
        run_id=request.run_id,
        attempt_id=request.attempt_id,
        task_id=request.task.id,
        success=success,
        exit_code=exit_code,
        pid=process.pid if process is not None else None,
        session_id=state.session_id,
        usage=state.usage,
        submission=submission,
        stdout_tail=state.stdout_tail,
        stderr_tail=state.stderr_tail,
        output=state.output,
        error=process_error,
        timed_out=timed_out,
        nonce=request.nonce,
    )
    _emit(
        request,
        {
            "type": "harness_finished",
            "harness": adapter.name,
            "exit_code": exit_code,
            "success": success,
            "session_id": state.session_id,
            "usage": state.usage,
            "error": process_error,
            "timed_out": timed_out,
        },
    )
    if submission is not None:
        _emit(
            request,
            {
                "type": "artifact_submitted",
                "summary": submission.summary,
                "artifact_path": submission.artifact_path,
                "metadata": submission.metadata,
            },
        )
    return result


async def _consume_stdout(
    stream: asyncio.StreamReader,
    request: HarnessRequest,
    harness_name: str,
    state: _StreamState,
) -> None:
    while True:
        try:
            raw = await stream.readline()
        except (ValueError, asyncio.LimitOverrunError) as exc:
            _emit(request, {"type": "harness_output_error", "harness": harness_name, "error": str(exc)})
            break
        if not raw:
            break
        line = _decode(raw).rstrip("\r\n")
        _append_stream_log(request.run_dir / "stdout.jsonl", line + "\n")
        state.stdout_tail = _tail(state.stdout_tail + line + "\n")
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            _emit(request, {"type": "harness_output", "harness": harness_name, "content": line[:MAX_STREAM_TAIL]})
            continue
        if not isinstance(payload, dict):
            _emit(request, {"type": "harness_event", "harness": harness_name, "event_type": "json", "raw": payload})
            continue
        _handle_json_event(request, harness_name, state, payload)


async def _consume_stderr(
    stream: asyncio.StreamReader,
    request: HarnessRequest,
    harness_name: str,
    state: _StreamState,
) -> None:
    while True:
        try:
            raw = await stream.readline()
        except (ValueError, asyncio.LimitOverrunError) as exc:
            _emit(request, {"type": "harness_output_error", "harness": harness_name, "error": str(exc)})
            break
        if not raw:
            break
        line = _decode(raw).rstrip("\r\n")
        _append_stream_log(request.run_dir / "stderr.log", line + "\n")
        state.stderr_tail = _tail(state.stderr_tail + line + "\n")
        if line:
            _emit(request, {"type": "harness_stderr", "harness": harness_name, "content": line[:MAX_STREAM_TAIL]})


def _handle_json_event(
    request: HarnessRequest,
    harness_name: str,
    state: _StreamState,
    payload: dict[str, Any],
) -> None:
    event_type = str(payload.get("type") or payload.get("event") or "json")
    _emit(request, {"type": "harness_event", "harness": harness_name, "event_type": event_type, "raw": payload})

    if event_type.lower() == "result" and "result" in payload:
        state.output = payload.get("result")

    session_id = _find_string(payload, {"session_id", "sessionid"})
    if session_id and session_id != state.session_id:
        state.session_id = session_id
        _emit(request, {"type": "harness_session", "harness": harness_name, "session_id": session_id})

    usage = _extract_usage(payload)
    if usage:
        _merge_usage(state.usage, usage)
        _emit(request, {"type": "harness_usage", "harness": harness_name, "usage": dict(state.usage)})

    for tool in _extract_tool_events(payload):
        _emit(request, tool)

    text = _extract_text(payload)
    if text:
        _emit(request, {"type": "model_delta", "harness": harness_name, "delta": text})


def _extract_usage(payload: dict[str, Any]) -> dict[str, Any]:
    usage = _find_mapping(payload, "usage")
    result: dict[str, Any] = dict(usage) if usage else {}
    tokens = _find_mapping(payload, "tokens")
    if tokens and "tokens" not in result:
        result["tokens"] = dict(tokens)
    for key in ("total_cost_usd", "cost", "duration_ms", "duration_api_ms", "num_turns"):
        value = _find_scalar(payload, key)
        if value is not None:
            result[key] = value
    return result


def _extract_text(payload: dict[str, Any]) -> str:
    event_type = str(payload.get("type") or "").lower()
    if event_type in {"assistant", "message", "text", "result"}:
        for key in ("message", "part", "content", "text", "result"):
            text = _content_text(payload.get(key))
            if text:
                return text
    if event_type in {"stream_event", "content_block_delta", "message_delta", "message.part.updated"}:
        for key in ("event", "delta", "part", "content"):
            text = _content_text(payload.get(key))
            if text:
                return text
    part = payload.get("part")
    if isinstance(part, dict) and str(part.get("type") or "").lower() in {"text", "reasoning"}:
        return str(part.get("text") or "")
    return ""


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        texts = [_content_text(item) for item in value]
        return "".join(text for text in texts if text)
    if not isinstance(value, dict):
        return ""
    kind = str(value.get("type") or "").lower()
    if kind in {"tool_use", "tool_result", "image"}:
        return ""
    for key in ("text", "content", "result"):
        text = _content_text(value.get(key))
        if text:
            return text
    delta = value.get("delta")
    if isinstance(delta, dict):
        return _content_text(delta)
    return ""


def _extract_tool_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    content = payload.get("content")
    if isinstance(content, list):
        blocks.extend(item for item in content if isinstance(item, dict))
    message = payload.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), list):
        blocks.extend(item for item in message["content"] if isinstance(item, dict))
    part = payload.get("part")
    if isinstance(part, dict):
        blocks.append(part)
    for block in blocks:
        kind = str(block.get("type") or "").lower()
        name = str(block.get("name") or block.get("tool") or "")
        if kind in {"tool_use", "tool", "tool-call"} and name:
            state = block.get("state") or {}
            status = str(state.get("status") or block.get("status") or "").lower() if isinstance(state, dict) else ""
            if status in {"completed", "done", "error", "failed"}:
                events.append(
                    {
                        "type": "tool_finished",
                        "tool": name,
                        "arguments": block.get("input") or block.get("arguments") or {},
                        "error": str(state.get("error") or "") if isinstance(state, dict) else "",
                        "result": state.get("output") if isinstance(state, dict) else None,
                    }
                )
            else:
                events.append(
                    {
                        "type": "tool_started",
                        "tool": name,
                        "arguments": block.get("input") or block.get("arguments") or {},
                    }
                )
    return events


async def _poll_progress_spool(
    request: HarnessRequest,
    seen: set[str],
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        try:
            await _emit_new_progress_events(request, seen)
        except HarnessMcpError as exc:
            _emit(request, {"type": "harness_progress_error", "error": str(exc)})
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.1)
        except asyncio.TimeoutError:
            pass


async def _emit_new_progress_events(request: HarnessRequest, seen: set[str]) -> None:
    records = load_progress_events(
        request.spool_dir,
        nonce=request.nonce,
        run_id=request.run_id,
        attempt_id=request.attempt_id,
        task_id=request.task.id,
        seen=seen,
    )
    for _name, record in records:
        _emit(
            request,
            {
                "type": "harness_progress",
                "message": str(record.get("message") or ""),
                "percent": record.get("percent"),
                "metadata": record.get("metadata") or {},
            },
        )


def _external_prompt(request: HarnessRequest) -> str:
    if request.runtime_config.inject_tools:
        submit = (
            "Before finishing, call the bagent MCP submit_artifact tool exactly once; final text alone does not complete the attempt."
            if request.config.artifact.require_submit
            else "You may call the bagent MCP submit_artifact tool to attach a structured result."
        )
        progress = "- You may call the bagent MCP report_progress tool for meaningful milestones."
    else:
        submit = "No bagent MCP completion tool is injected; provide a clear final response."
        progress = ""
    protocol = "\n".join(
        [
            "Bagent external harness protocol:",
            f"- Batch run id: {request.run_id}",
            f"- Task attempt id: {request.attempt_id}",
            f"- Task id: {request.task.id}",
            "- Work only on this assigned task.",
            f"- {submit}",
            progress,
        ]
    )
    return "\n\n".join(part for part in (request.prompt.strip(), protocol) if part) + "\n"


def _require_completion_transport(request: HarnessRequest) -> None:
    if request.config.artifact.require_submit and not request.runtime_config.inject_tools:
        raise HarnessError("artifact.require_submit is true but external harness MCP tool injection is disabled")


def _mcp_environment(request: HarnessRequest) -> dict[str, str]:
    request.spool_dir.mkdir(parents=True, exist_ok=True)
    package_root = str(Path(__file__).resolve().parent.parent)
    return {
        "BAGENT_MCP_SPOOL_DIR": str(request.spool_dir.resolve()),
        "BAGENT_MCP_NONCE": request.nonce,
        "BAGENT_RUN_ID": request.run_id,
        "BAGENT_ATTEMPT_ID": request.attempt_id,
        "BAGENT_TASK_ID": request.task.id,
        "BAGENT_RUN_DIR": str(request.run_dir.resolve()),
        # Keep `python -m batchagent.harness_mcp` working for both installed
        # wheels and source-tree execution from a different task workspace.
        "PYTHONPATH": package_root,
    }


def _mcp_command() -> list[str]:
    return [sys.executable, "-m", "batchagent.harness_mcp"]


def _request_environment(request: HarnessRequest) -> dict[str, str]:
    if request.environment is not None:
        return {str(key): str(value) for key, value in request.environment.items()}
    return _minimal_environment(request.runtime_config.env_allowlist)


def _minimal_environment(allowlist: list[str]) -> dict[str, str]:
    names = {
        "PATH",
        "Path",
        "HOME",
        "USERPROFILE",
        "SYSTEMROOT",
        "SystemRoot",
        "WINDIR",
        "TEMP",
        "TMP",
        "TMPDIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "APPDATA",
        "LOCALAPPDATA",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
    }
    names.update(allowlist)
    return {name: value for name in names if (value := os.environ.get(name)) is not None}


def _validate_harness_command(name: str, prefix: list[str], extra_args: list[str]) -> None:
    if not prefix or not str(prefix[0]).strip():
        raise HarnessError("harness command is empty")
    arguments = [str(item) for item in [*prefix[1:], *extra_args]]
    _reject_unsafe_launcher(prefix)
    _reject_unsafe_extra_args(name, arguments)


def _reject_unsafe_launcher(prefix: list[str]) -> None:
    """Reject common inline-evaluation launchers in unattended manifests.

    A direct executable or an interpreter plus a script path remains supported
    for local harness development. Inline code/eval flags are deliberately not
    supported because merely probing a Batch Config would execute them.
    """

    configured = str(prefix[0])
    resolved = _resolve_executable(configured)
    executable_path = Path(resolved or configured).expanduser().resolve(strict=False)
    executable = executable_path.name.lower()
    if executable.endswith(".exe"):
        executable = executable[:-4]
    args = [str(item).strip().lower() for item in prefix[1:]]
    if executable in {"env", "xargs", "sudo", "doas"}:
        raise HarnessError(f"unsafe harness command launcher is not allowed: {executable}")
    if executable in {"sh", "bash", "zsh", "dash", "ksh", "fish"} and any(
        arg in {"-c", "--command"} for arg in args
    ):
        raise HarnessError("inline shell harness commands are not allowed")
    if executable in {"cmd", "cmd64"} and any(arg in {"/c", "/k"} for arg in args):
        raise HarnessError("inline cmd harness commands are not allowed")
    if executable in {"powershell", "pwsh"} and any(
        arg in {"-c", "-command", "-encodedcommand", "-enc"} for arg in args
    ):
        raise HarnessError("inline PowerShell harness commands are not allowed")
    if (executable.startswith("python") or executable.startswith("pypy")) and any(
        arg in {"-c", "-m", "-"} for arg in args
    ):
        raise HarnessError("inline Python/module harness commands are not allowed")
    if executable in {"node", "nodejs", "ruby", "perl", "php"} and any(
        arg in {"-e", "--eval", "-r"} or arg.startswith("--eval=") for arg in args
    ):
        raise HarnessError(f"inline {executable} harness commands are not allowed")


def _reject_unsafe_extra_args(name: str, args: list[str]) -> None:
    lowered = [arg.strip().lower() for arg in args]
    if name == "opencode" and any(arg == "--auto" or arg.startswith("--auto=") for arg in lowered):
        raise HarnessError("bagent does not allow OpenCode --auto in unattended task execution")
    joined = " ".join(lowered)
    if name == "claude" and (
        any(arg == "--dangerously-skip-permissions" or arg.startswith("--dangerously-skip-permissions=") for arg in lowered)
        or any(
            arg == "--dangerously-bypass-approvals-and-sandbox"
            or arg.startswith("--dangerously-bypass-approvals-and-sandbox=")
            for arg in lowered
        )
        or "--permission-mode bypasspermissions" in joined
        or any(arg.startswith("--permission-mode=bypasspermissions") for arg in lowered)
    ):
        raise HarnessError("bagent does not allow Claude permission bypass flags in unattended task execution")


def _merge_usage(target: dict[str, Any], incoming: Mapping[str, Any]) -> None:
    """Merge partial/cumulative usage events without dropping nested tokens."""

    for key, value in incoming.items():
        existing = target.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            _merge_usage(existing, value)
        elif isinstance(value, Mapping):
            nested: dict[str, Any] = {}
            _merge_usage(nested, value)
            target[key] = nested
        else:
            target[key] = value


def _append_stream_log(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    created = not path.exists()
    with path.open("a", encoding="utf-8", errors="replace") as handle:
        handle.write(text)
        handle.flush()
    if created:
        try:
            path.chmod(0o600)
        except OSError:
            pass


def _resolve_executable(value: str) -> str:
    path = Path(value).expanduser()
    if path.is_absolute() or path.parent != Path("."):
        return str(path.resolve()) if path.is_file() else ""
    return shutil.which(value) or ""


def _process_group_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def _terminate_process_tree(process: asyncio.subprocess.Process, *, grace_seconds: float = 1.0) -> None:
    if os.name == "nt":
        if process.returncode is not None:
            return
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        except (ProcessLookupError, OSError, ValueError):
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=grace_seconds)
            return
        except asyncio.TimeoutError:
            pass
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        except OSError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            await process.wait()
        except ProcessLookupError:
            pass
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        if process.returncode is None:
            try:
                await process.wait()
            except ProcessLookupError:
                pass
        return

    deadline = asyncio.get_running_loop().time() + grace_seconds
    while asyncio.get_running_loop().time() < deadline:
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=0.05)
            except asyncio.TimeoutError:
                pass
        if not _posix_process_group_exists(process.pid):
            return
        await asyncio.sleep(0.02)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.returncode is None:
        try:
            await process.wait()
        except ProcessLookupError:
            pass


async def _cleanup_posix_process_group(process_group_id: int) -> None:
    """Clean task-scoped descendants left behind after a normal CLI exit."""

    if not _posix_process_group_exists(process_group_id):
        return
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(10):
        if not _posix_process_group_exists(process_group_id):
            return
        await asyncio.sleep(0.02)
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _posix_process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _find_string(value: Any, keys: set[str]) -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in keys and isinstance(item, (str, int)) and str(item):
                return str(item)
        for item in value.values():
            found = _find_string(item, keys)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_string(item, keys)
            if found:
                return found
    return ""


def _find_mapping(value: Any, key_name: str) -> dict[str, Any] | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() == key_name and isinstance(item, dict):
                return item
        for item in value.values():
            found = _find_mapping(item, key_name)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_mapping(item, key_name)
            if found is not None:
                return found
    return None


def _find_scalar(value: Any, key_name: str) -> Any | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() == key_name and isinstance(item, (str, int, float, bool)):
                return item
        for item in value.values():
            found = _find_scalar(item, key_name)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_scalar(item, key_name)
            if found is not None:
                return found
    return None


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


def _tail(value: str, limit: int = MAX_STREAM_TAIL) -> str:
    return value[-limit:] if len(value) > limit else value


def _emit(request: HarnessRequest, event: dict[str, Any]) -> None:
    callback = request.progress_callback
    if callback is None:
        return
    envelope = {
        "run_id": request.run_id,
        "attempt_id": request.attempt_id,
        "task_id": request.task.id,
        **event,
    }
    try:
        callback(envelope)
    except Exception:
        pass
