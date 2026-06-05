from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any, Callable

from .agent import run_agent_task
from .manifest import load_manifest, save_manifest
from .models import AgentRunResult, Manifest, Task
from .store import SessionStore
from .tools import unknown_tool_names
from .util import ManifestLock, truncate, utc_now
from .workspace import task_run_dir


class SchedulerError(RuntimeError):
    pass


ProgressCallback = Callable[[dict[str, Any]], None]


async def run_manifest(
    manifest_path: str | Path,
    *,
    limit: int | None = None,
    retry_failed: bool = False,
    task_ids: set[str] | None = None,
    progress_callback: ProgressCallback | None = None,
) -> list[AgentRunResult]:
    manifest = load_manifest(manifest_path)
    validate_manifest(manifest)
    state_db = _state_db_path(manifest)
    store = SessionStore(state_db)
    semaphore = asyncio.Semaphore(manifest.config.effective_concurrency)
    write_lock = asyncio.Lock()
    eligible = [task for task in manifest.tasks if task.is_eligible(retry_failed=retry_failed)]
    if task_ids is not None:
        known_ids = {task.id for task in manifest.tasks}
        missing = sorted(task_ids - known_ids)
        if missing:
            raise SchedulerError(f"task id(s) not found: {', '.join(missing)}")
        eligible = [task for task in eligible if task.id in task_ids]
    if limit is not None:
        eligible = eligible[: max(0, limit)]
    _emit(
        progress_callback,
        {
            "type": "batch_loaded",
            "total_tasks": len(manifest.tasks),
            "eligible_tasks": len(eligible),
            "concurrency": manifest.config.effective_concurrency,
            "started_at": utc_now(),
        },
    )
    for task in eligible:
        _emit(progress_callback, {"type": "task_queued", "task_id": task.id, "attempts": task.attempts})

    async def worker(task: Task) -> AgentRunResult:
        async with semaphore:
            return await _run_one_with_retries(manifest, task, store, write_lock, progress_callback)

    try:
        return await asyncio.gather(*(worker(task) for task in eligible))
    finally:
        store.close()


def status(manifest_path: str | Path) -> dict[str, int]:
    manifest = load_manifest(manifest_path)
    counts: dict[str, int] = {}
    for task in manifest.tasks:
        counts[task.status] = counts.get(task.status, 0) + 1
    return counts


def tasks_by_status(manifest_path: str | Path, statuses: set[str]) -> list[Task]:
    manifest = load_manifest(manifest_path)
    return [task for task in manifest.tasks if task.status in statuses]


def mark_tasks_for_retry(
    manifest_path: str | Path,
    task_ids: set[str] | None = None,
    *,
    reset_attempts: bool = False,
) -> int:
    manifest = load_manifest(manifest_path)
    target_ids = task_ids or {task.id for task in manifest.tasks if task.status == "failed"}
    changed = 0
    for task in manifest.tasks:
        if task.id not in target_ids:
            continue
        task.status = "retry"
        task.lease = ""
        task.error = ""
        task.updated = utc_now()
        if reset_attempts:
            task.attempts = 0
        changed += 1
    if changed:
        with ManifestLock(manifest.path):
            save_manifest(manifest)
    return changed


def rerun_tasks(manifest_path: str | Path, task_ids: set[str]) -> int:
    manifest = load_manifest(manifest_path)
    known_ids = {task.id for task in manifest.tasks}
    missing = sorted(task_ids - known_ids)
    if missing:
        raise SchedulerError(f"task id(s) not found: {', '.join(missing)}")
    changed = 0
    for task in manifest.tasks:
        if task.id not in task_ids:
            continue
        task.status = "todo"
        task.result = ""
        task.attempts = 0
        task.updated = utc_now()
        task.lease = ""
        task.error = ""
        changed += 1
    if changed:
        with ManifestLock(manifest.path):
            save_manifest(manifest)
    return changed


def recover_running(manifest_path: str | Path, target_status: str = "retry") -> int:
    manifest = load_manifest(manifest_path)
    changed = 0
    for task in manifest.tasks:
        if task.status == "running":
            task.status = target_status
            task.lease = ""
            task.updated = utc_now()
            task.error = "recovered stale running task"
            changed += 1
    if changed:
        with ManifestLock(manifest.path):
            save_manifest(manifest)
    return changed


async def _run_one_with_retries(
    manifest: Manifest,
    task: Task,
    store: SessionStore,
    write_lock: asyncio.Lock,
    progress_callback: ProgressCallback | None = None,
) -> AgentRunResult:
    attempt_deadline = task.attempts + max(1, manifest.config.retries + 1)
    last_result: AgentRunResult | None = None
    while task.attempts < attempt_deadline:
        run_id = uuid.uuid4().hex[:12]
        await _mark_task(write_lock, manifest, task, "running", run_id, "")
        task.attempts += 1
        await _save_manifest(write_lock, manifest)
        _emit(
            progress_callback,
            {
                "type": "task_started",
                "task_id": task.id,
                "run_id": run_id,
                "attempt": task.attempts,
                "status": task.status,
                "run_dir": str(task_run_dir(manifest.config, manifest.path, task, run_id)),
                "timestamp": utc_now(),
            },
        )
        try:
            result = await asyncio.wait_for(
                run_agent_task(manifest.path, manifest.config, task, store, run_id=run_id, progress_callback=progress_callback),
                timeout=manifest.config.timeout_seconds,
            )
        except asyncio.TimeoutError:
            store.finish_run(run_id, "failed", f"task timed out after {manifest.config.timeout_seconds} seconds")
            run_dir = Path(manifest.config.run_dir)
            if not run_dir.is_absolute():
                run_dir = manifest.path.parent / run_dir
            result = AgentRunResult(
                success=False,
                task_id=task.id,
                run_dir=run_dir,
                error=f"task timed out after {manifest.config.timeout_seconds} seconds",
            )
        last_result = result
        if result.success:
            task.result = str(result.artifact_record_path or result.run_dir)
            task.error = ""
            await _mark_task(write_lock, manifest, task, "done", "", "")
            _emit(
                progress_callback,
                {
                    "type": "task_done",
                    "task_id": task.id,
                    "attempt": task.attempts,
                    "status": task.status,
                    "run_dir": str(result.run_dir),
                    "result": task.result,
                    "timestamp": utc_now(),
                },
            )
            return result
        task.error = truncate(result.error, 500)
        if task.attempts < attempt_deadline:
            await _mark_task(write_lock, manifest, task, "retry", "", task.error)
            _emit(
                progress_callback,
                {
                    "type": "task_retry",
                    "task_id": task.id,
                    "attempt": task.attempts,
                    "status": task.status,
                    "run_dir": str(result.run_dir),
                    "error": task.error,
                    "timestamp": utc_now(),
                },
            )
            continue
        await _mark_task(write_lock, manifest, task, "failed", "", task.error)
        _emit(
            progress_callback,
            {
                "type": "task_failed",
                "task_id": task.id,
                "attempt": task.attempts,
                "status": task.status,
                "run_dir": str(result.run_dir),
                "error": task.error,
                "timestamp": utc_now(),
            },
        )
        return result

    if last_result is not None:
        return last_result
    raise SchedulerError(f"task could not be scheduled: {task.id}")


async def _mark_task(
    write_lock: asyncio.Lock,
    manifest: Manifest,
    task: Task,
    status_value: str,
    lease: str,
    error: str,
) -> None:
    task.status = status_value
    task.lease = lease
    task.error = error
    task.updated = utc_now()
    await _save_manifest(write_lock, manifest)


async def _save_manifest(write_lock: asyncio.Lock, manifest: Manifest) -> None:
    async with write_lock:
        with ManifestLock(manifest.path):
            save_manifest(manifest)


def _state_db_path(manifest: Manifest) -> Path:
    run_dir = Path(manifest.config.run_dir)
    if not run_dir.is_absolute():
        run_dir = manifest.path.parent / run_dir
    return run_dir / "state.sqlite3"


def state_db_path(manifest: Manifest) -> Path:
    return _state_db_path(manifest)


def _emit(callback: ProgressCallback | None, event: dict[str, Any]) -> None:
    if callback is None:
        return
    try:
        callback(event)
    except Exception:
        pass


def validate_manifest(manifest: Manifest) -> None:
    ids = [task.id for task in manifest.tasks]
    duplicates = sorted({task_id for task_id in ids if ids.count(task_id) > 1})
    if duplicates:
        raise SchedulerError(f"duplicate task ids: {', '.join(duplicates)}")
    if not manifest.config.user_prompt_template.strip():
        raise SchedulerError("user_prompt_template is required")
    unknown = unknown_tool_names(manifest.config)
    if unknown:
        raise SchedulerError(f"unknown tools in manifest config: {', '.join(unknown)}")
    if manifest.config.artifact.require_submit and "submit_artifact" not in manifest.config.tools:
        raise SchedulerError("artifact.require_submit is true but submit_artifact is not listed in tools")
    if "run_command" in manifest.config.tools and not manifest.config.allowed_command_prefixes:
        raise SchedulerError("run_command is loaded but allowed_command_prefixes is empty")
