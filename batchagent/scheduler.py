from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from .agent import run_agent_task
from .manifest import load_manifest, save_manifest
from .models import AgentRunResult, Manifest, Task
from .store import SessionStore
from .util import ManifestLock, truncate, utc_now


class SchedulerError(RuntimeError):
    pass


async def run_manifest(
    manifest_path: str | Path,
    *,
    limit: int | None = None,
    retry_failed: bool = False,
) -> list[AgentRunResult]:
    manifest = load_manifest(manifest_path)
    _validate_manifest(manifest)
    state_db = _state_db_path(manifest)
    store = SessionStore(state_db)
    semaphore = asyncio.Semaphore(manifest.config.effective_concurrency)
    write_lock = asyncio.Lock()
    eligible = [task for task in manifest.tasks if task.is_eligible(retry_failed=retry_failed)]
    if limit is not None:
        eligible = eligible[: max(0, limit)]

    async def worker(task: Task) -> AgentRunResult:
        async with semaphore:
            return await _run_one_with_retries(manifest, task, store, write_lock)

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
) -> AgentRunResult:
    attempt_deadline = task.attempts + max(1, manifest.config.retries + 1)
    last_result: AgentRunResult | None = None
    while task.attempts < attempt_deadline:
        run_id = uuid.uuid4().hex[:12]
        await _mark_task(write_lock, manifest, task, "running", run_id, "")
        task.attempts += 1
        await _save_manifest(write_lock, manifest)
        try:
            result = await asyncio.wait_for(
                run_agent_task(manifest.path, manifest.config, task, store, run_id=run_id),
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
            return result
        task.error = truncate(result.error, 500)
        if task.attempts < attempt_deadline:
            await _mark_task(write_lock, manifest, task, "retry", "", task.error)
            continue
        await _mark_task(write_lock, manifest, task, "failed", "", task.error)
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


def _validate_manifest(manifest: Manifest) -> None:
    ids = [task.id for task in manifest.tasks]
    duplicates = sorted({task_id for task_id in ids if ids.count(task_id) > 1})
    if duplicates:
        raise SchedulerError(f"duplicate task ids: {', '.join(duplicates)}")
    if not manifest.config.user_prompt_template.strip():
        raise SchedulerError("user_prompt_template is required")
