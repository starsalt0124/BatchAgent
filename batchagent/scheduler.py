from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import subprocess
import sys
import uuid
from dataclasses import asdict, fields
from datetime import date
from pathlib import Path
from typing import Any, Callable

from .agent import build_task_prompt
from .harness import HarnessRequest, canonical_harness_name, probe_harness, run_harness
from .manifest import load_manifest, save_manifest
from .models import AgentRunResult, ArtifactPolicy, BatchConfig, HarnessConfig, Manifest, RunVariable, Task
from .paths import bagent_home, runs_dir, state_db_path as global_state_db_path
from .settings import SettingsError, load_settings
from .skills import load_manifest_skills
from .store import SessionStore
from .tools import unknown_tool_names
from .util import ManifestLock, truncate, utc_now, write_json
from .validation import ArtifactValidationError, validate_artifact
from .workspace import prepare_workspace, task_run_dir


class SchedulerError(RuntimeError):
    pass


ProgressCallback = Callable[[dict[str, Any]], None]


async def run_manifest(
    manifest_path: str | Path,
    *,
    limit: int | None = None,
    pause_after: int | None = None,
    retry_failed: bool = False,
    task_ids: set[str] | None = None,
    run_id: str | None = None,
    work_id: str | None = None,
    resume: bool = False,
    harness: str | None = None,
    run_vars: dict[str, Any] | None = None,
    progress_callback: ProgressCallback | None = None,
    pause_event: asyncio.Event | None = None,
) -> list[AgentRunResult]:
    if pause_after is not None and pause_after < 0:
        raise SchedulerError("pause_after must be non-negative")
    if resume and pause_after is not None:
        raise SchedulerError("pause_after is only valid when creating a new Run")
    manifest = load_manifest(manifest_path)
    run_id = run_id or work_id or new_run_id()
    store = SessionStore(_state_db_path(manifest))
    write_lock = asyncio.Lock()
    run_claimed = False
    try:
        if resume:
            record = store.batch_run(run_id)
            if record is None:
                raise SchedulerError(f"run not found: {run_id}")
            expected_path = str(manifest.path.resolve())
            if record["manifest_path"] != expected_path:
                raise SchedulerError(f"run {run_id} belongs to another batch config: {record['manifest_path']}")
            manifest.config = _batch_config_from_snapshot(
                manifest.config,
                record.get("config_snapshot"),
            )
            manifest.config.run_vars = dict(record.get("run_vars") or {})
            selected_harness = canonical_harness_name(harness or str(record.get("harness") or "native"))
            if harness is None:
                # The Run record is the canonical harness identity. Preserve
                # the rest of its frozen per-harness options from the snapshot.
                manifest.config.harness.name = selected_harness
            else:
                # An explicit cross-harness resume should not accidentally
                # reuse another harness's command prefix or flags.
                manifest.config.harness = HarnessConfig(name=selected_harness)
            frozen_rows = store.run_tasks(run_id)
            frozen_tasks = [_task_from_snapshot(row) for row in frozen_rows]
            manifest.tasks = frozen_tasks
            validate_manifest(manifest, harness_name=selected_harness)
            if task_ids is not None:
                known_ids = {task.id for task in frozen_tasks}
                _raise_missing_task_ids(task_ids, known_ids)
        else:
            selected_harness = _resolve_harness_name(manifest, harness)
            validate_manifest(manifest, harness_name=selected_harness)
            manifest.config.run_vars = _resolve_run_vars(manifest, run_vars or {})
            if task_ids is not None:
                known_ids = {task.id for task in manifest.tasks}
                _raise_missing_task_ids(task_ids, known_ids)
                # An explicit task selection is a deliberate rerun request. Its
                # previous manifest cache status must not suppress a new Run.
                selected = [task for task in manifest.tasks if task.id in task_ids]
            else:
                # Run state belongs to the new Run, not to an earlier manifest
                # cache. Only an explicit definition-level skip is carried in.
                selected = [task for task in manifest.tasks if task.status != "skipped"]
            if limit is not None:
                selected = selected[: max(0, limit)]
            eligible = [_fresh_run_task(task) for task in selected]

        runtime_config = _runtime_harness_config(manifest, selected_harness)
        manifest.config.harness = runtime_config
        probe = await probe_harness(selected_harness, runtime_config)
        if not probe.available:
            raise SchedulerError(f"harness {selected_harness} is unavailable: {probe.error}")

        if resume:
            # Probe and validate first so a missing executable or malformed
            # frozen config cannot mutate an otherwise resumable Run. The
            # conditional UPDATE inside resume_batch_run remains the atomic
            # claim that rejects concurrent resume attempts.
            store.resume_batch_run(run_id)
            run_claimed = True
            frozen_rows = store.run_tasks(run_id)
            frozen_tasks = [_task_from_snapshot(row) for row in frozen_rows]
            manifest.tasks = frozen_tasks
            eligible_statuses = {"queued", "retry", "interrupted"}
            if retry_failed:
                eligible_statuses.add("failed")
            eligible = [task for task in frozen_tasks if task.status in eligible_statuses]
            if task_ids is not None:
                eligible = [task for task in eligible if task.id in task_ids]
            if limit is not None:
                eligible = eligible[: max(0, limit)]
        else:
            store.start_batch_run(
                run_id,
                manifest.path,
                manifest.config.name,
                manifest.tasks,
                harness=selected_harness,
                harness_version=probe.version,
                run_vars=manifest.config.run_vars,
                config_snapshot=asdict(manifest.config),
                selected_task_ids=[task.id for task in eligible],
                config_hash=hashlib.sha256(manifest.text.encode("utf-8")).hexdigest(),
            )
            run_claimed = True
        scheduled = eligible if pause_after is None else eligible[:pause_after]
        _clear_pause_request(run_id)
        _emit(
            progress_callback,
            {
                "type": "batch_loaded",
                "run_id": run_id,
                "work_id": run_id,
                "harness": selected_harness,
                "harness_version": probe.version,
                "resumed": resume,
                "total_tasks": len(store.run_tasks(run_id)),
                "eligible_tasks": len(eligible),
                "scheduled_tasks": len(scheduled),
                "pause_after": pause_after,
                "concurrency": manifest.config.effective_concurrency,
                "started_at": utc_now(),
            },
        )
        for task in eligible:
            _emit(
                progress_callback,
                {
                    "type": "task_queued",
                    "run_id": run_id,
                    "work_id": run_id,
                    "task_id": task.id,
                    "attempts": task.attempts,
                },
            )

        if manifest.config.task_selector_script or manifest.config.task_selector_command:
            results = await _run_manifest_with_task_selector(
                manifest,
                scheduled,
                store,
                write_lock,
                run_id,
                retry_failed=retry_failed,
                task_ids=task_ids,
                limit=limit,
                harness_name=selected_harness,
                harness_version=probe.version,
                progress_callback=progress_callback,
                pause_event=pause_event,
            )
        else:
            results = await _run_manifest_default(
                manifest,
                scheduled,
                store,
                write_lock,
                run_id,
                harness_name=selected_harness,
                harness_version=probe.version,
                progress_callback=progress_callback,
                pause_event=pause_event,
            )

        final_tasks = store.run_tasks(run_id)
        final_status = _derive_run_status(final_tasks)
        store.finish_batch_run(
            run_id,
            final_status,
            result=_summarize_run_tasks(final_tasks),
        )
        _emit(
            progress_callback,
            {
                "type": "run_finished",
                "run_id": run_id,
                "work_id": run_id,
                "status": final_status,
                "results": len(results),
                "timestamp": utc_now(),
            },
        )
        return results
    except asyncio.CancelledError:
        if run_claimed:
            try:
                store.interrupt_batch_run(run_id, "scheduler cancelled")
            except Exception:
                pass
        raise
    except Exception as exc:
        if run_claimed:
            try:
                store.finish_batch_run(
                    run_id,
                    "failed",
                    str(exc),
                    result=_summarize_run_tasks(store.run_tasks(run_id)),
                )
            except Exception:
                pass
        raise
    finally:
        store.close()


async def _run_manifest_default(
    manifest: Manifest,
    eligible: list[Task],
    store: SessionStore,
    write_lock: asyncio.Lock,
    work_id: str,
    *,
    harness_name: str,
    harness_version: str,
    progress_callback: ProgressCallback | None = None,
    pause_event: asyncio.Event | None = None,
) -> list[AgentRunResult]:
    max_concurrency = manifest.config.effective_concurrency
    pending = list(eligible)
    running: dict[asyncio.Task[AgentRunResult], Task] = {}
    results: list[AgentRunResult] = []

    while pending or running:
        while pending and len(running) < max_concurrency and not _pause_requested(work_id, pause_event):
            task = pending.pop(0)
            future = asyncio.create_task(
                _run_one_with_retries(
                    manifest,
                    task,
                    store,
                    write_lock,
                    work_id,
                    harness_name,
                    harness_version,
                    progress_callback,
                )
            )
            running[future] = task

        if not running:
            if pending and _pause_requested(work_id, pause_event):
                _emit_batch_paused(progress_callback, work_id, pending=len(pending), running=0)
            break

        done, _pending = await asyncio.wait(running.keys(), return_when=asyncio.FIRST_COMPLETED)
        for future in done:
            task = running.pop(future)
            try:
                results.append(future.result())
            except Exception as exc:
                raise SchedulerError(f"task failed outside scheduler state machine: {task.id}: {exc}") from exc

    return results


async def _run_manifest_with_task_selector(
    manifest: Manifest,
    eligible: list[Task],
    store: SessionStore,
    write_lock: asyncio.Lock,
    work_id: str,
    *,
    retry_failed: bool,
    task_ids: set[str] | None,
    limit: int | None,
    harness_name: str,
    harness_version: str,
    progress_callback: ProgressCallback | None = None,
    pause_event: asyncio.Event | None = None,
) -> list[AgentRunResult]:
    max_concurrency = manifest.config.effective_concurrency
    pending_ids = [task.id for task in eligible]
    task_by_id = {task.id: task for task in eligible}
    running: dict[asyncio.Task[AgentRunResult], Task] = {}
    results: list[AgentRunResult] = []

    while pending_ids or running:
        while pending_ids and len(running) < max_concurrency and not _pause_requested(work_id, pause_event):
            slots = max_concurrency - len(running)
            selected_ids = _select_next_task_ids(
                manifest,
                pending_ids=pending_ids,
                running_tasks=list(running.values()),
                work_id=work_id,
                retry_failed=retry_failed,
                task_ids=task_ids,
                limit=limit,
                slots=slots,
            )
            selected_ids = [task_id for task_id in selected_ids if task_id in pending_ids]
            if not selected_ids:
                break
            for task_id in selected_ids[:slots]:
                task = task_by_id[task_id]
                pending_ids.remove(task_id)
                future = asyncio.create_task(
                    _run_one_with_retries(
                        manifest,
                        task,
                        store,
                        write_lock,
                        work_id,
                        harness_name,
                        harness_version,
                        progress_callback,
                    )
                )
                running[future] = task
                _emit(
                    progress_callback,
                    {
                        "type": "task_selector_selected",
                        "work_id": work_id,
                        "run_id": work_id,
                        "task_id": task.id,
                        "pending_tasks": len(pending_ids),
                        "running_tasks": len(running),
                    },
                )

        if not running:
            if pending_ids and _pause_requested(work_id, pause_event):
                _emit_batch_paused(progress_callback, work_id, pending=len(pending_ids), running=0)
                break
            raise SchedulerError(
                "task selector returned no runnable tasks while pending tasks remain: "
                + ", ".join(pending_ids[:20])
            )

        done, _pending = await asyncio.wait(running.keys(), return_when=asyncio.FIRST_COMPLETED)
        for future in done:
            task = running.pop(future)
            try:
                results.append(future.result())
            except Exception as exc:
                raise SchedulerError(f"task failed outside scheduler state machine: {task.id}: {exc}") from exc

    return results


def _select_next_task_ids(
    manifest: Manifest,
    *,
    pending_ids: list[str],
    running_tasks: list[Task],
    work_id: str,
    retry_failed: bool,
    task_ids: set[str] | None,
    limit: int | None,
    slots: int,
) -> list[str]:
    payload = _task_selector_payload(
        manifest,
        pending_ids=pending_ids,
        running_tasks=running_tasks,
        work_id=work_id,
        retry_failed=retry_failed,
        task_ids=task_ids,
        limit=limit,
        slots=slots,
    )
    command = _task_selector_command(manifest)
    workspace = _manifest_workspace(manifest)
    result = subprocess.run(
        command,
        cwd=str(workspace),
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=min(max(5, manifest.config.request_timeout_seconds), 300),
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise SchedulerError(
            "task selector failed (%s): %s\n%s"
            % (result.returncode, " ".join(command), detail)
        )
    try:
        parsed = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise SchedulerError(f"task selector returned invalid JSON: {result.stdout[:500]}") from exc
    if isinstance(parsed, list):
        selected = parsed
    elif isinstance(parsed, dict):
        selected = parsed.get("task_ids", [])
    else:
        raise SchedulerError("task selector must return a JSON object or list")
    if not isinstance(selected, list):
        raise SchedulerError("task selector field task_ids must be a list")
    values = [str(task_id) for task_id in selected]
    unknown = sorted(set(values) - set(pending_ids))
    if unknown:
        raise SchedulerError("task selector returned non-pending task id(s): " + ", ".join(unknown))
    return values[: max(0, slots)]


def _task_selector_payload(
    manifest: Manifest,
    *,
    pending_ids: list[str],
    running_tasks: list[Task],
    work_id: str,
    retry_failed: bool,
    task_ids: set[str] | None,
    limit: int | None,
    slots: int,
) -> dict[str, Any]:
    return {
        "event": "select",
        "manifest_path": str(manifest.path.resolve()),
        "workspace": str(_manifest_workspace(manifest)),
        "work_id": work_id,
        "run_id": work_id,
        "max_concurrency": manifest.config.effective_concurrency,
        "available_slots": slots,
        "retry_failed": retry_failed,
        "limit": limit,
        "only_task_ids": sorted(task_ids) if task_ids else None,
        "pending_task_ids": list(pending_ids),
        "running_task_ids": [task.id for task in running_tasks],
        "tasks": [
            {
                "id": task.id,
                "status": task.status,
                "kind": task.kind,
                "input": task.input,
                "attempts": task.attempts,
                "result": task.result,
                "updated": task.updated,
                "lease": task.lease,
                "error": task.error,
            }
            for task in manifest.tasks
        ],
        "run_vars": manifest.config.run_vars,
    }


def _task_selector_command(manifest: Manifest) -> list[str]:
    if manifest.config.task_selector_command:
        return list(manifest.config.task_selector_command)
    script = manifest.config.task_selector_script.strip()
    if not script:
        raise SchedulerError("task selector is enabled but no selector command/script is configured")
    if script.endswith(".py"):
        return [sys.executable, script]
    return [script]


def _manifest_workspace(manifest: Manifest) -> Path:
    workspace = Path(manifest.config.workspace).expanduser()
    if not workspace.is_absolute():
        workspace = manifest.path.parent / workspace
    return workspace.resolve()


def pause_request_path(target: str | Path) -> Path:
    return _pause_request_path(_resolve_pause_run_id(target))


def request_pause(target: str | Path) -> Path:
    run_id = _resolve_pause_run_id(target)
    path = _pause_request_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, {"active": True, "run_id": run_id, "requested_at": utc_now()})
    return path


def _resolve_pause_run_id(target: str | Path) -> str:
    candidate = Path(target)
    if candidate.exists() and candidate.is_file():
        store_path = global_state_db_path()
        if not store_path.exists():
            raise SchedulerError(f"no persisted runs for batch config: {candidate}")
        store = SessionStore(store_path)
        try:
            runs = store.batch_runs(candidate, limit=20)
            row = next((item for item in runs if item["status"] == "running"), None)
        finally:
            store.close()
        if row is None:
            raise SchedulerError(f"no active run for batch config: {candidate}")
        return str(row["run_id"])
    run_id = str(target).strip()
    if not run_id:
        raise SchedulerError("run id is required")
    return run_id


def _pause_request_path(run_id: str) -> Path:
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]
    return bagent_home(create=True) / "control" / f"pause-{digest}.json"


def _clear_pause_request(run_id: str) -> None:
    path = _pause_request_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, {"active": False, "run_id": run_id, "cleared_at": utc_now()})


def _pause_requested(run_id: str, pause_event: asyncio.Event | None = None) -> bool:
    if pause_event is not None and pause_event.is_set():
        return True
    path = _pause_request_path(run_id)
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(data.get("active"))


def _emit_batch_paused(
    progress_callback: ProgressCallback | None,
    work_id: str,
    *,
    pending: int,
    running: int,
) -> None:
    _emit(
        progress_callback,
        {
            "type": "batch_paused",
            "work_id": work_id,
            "run_id": work_id,
            "pending_tasks": pending,
            "running_tasks": running,
            "timestamp": utc_now(),
        },
    )


def status(manifest_path: str | Path) -> dict[str, int]:
    manifest = load_manifest(manifest_path)
    db_path = _state_db_path(manifest)
    if db_path.exists():
        store = SessionStore(db_path, read_only=True)
        try:
            runs = store.batch_runs(manifest.path, limit=1)
            if runs:
                counts: dict[str, int] = {}
                for row in store.run_tasks(str(runs[0]["run_id"])):
                    state = str(row["status"])
                    counts[state] = counts.get(state, 0) + 1
                return counts
        finally:
            store.close()
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
    work_id: str,
    harness_name: str,
    harness_version: str,
    progress_callback: ProgressCallback | None = None,
) -> AgentRunResult:
    del write_lock  # SQLite is the run source of truth; the manifest is definition-only during execution.
    attempt_deadline = task.attempts + max(1, manifest.config.retries + 1)
    last_result: AgentRunResult | None = None
    while task.attempts < attempt_deadline:
        attempt_id = new_attempt_id()
        task.status = "running"
        task.lease = attempt_id
        task.error = ""
        task.updated = utc_now()
        task.attempts += 1
        run_dir = task_run_dir(manifest.config, manifest.path, task, attempt_id, work_id)
        _emit(
            progress_callback,
            {
                "type": "task_started",
                "work_id": work_id,
                "run_id": work_id,
                "task_id": task.id,
                "attempt_id": attempt_id,
                "attempt": task.attempts,
                "status": task.status,
                "harness": harness_name,
                "run_dir": str(run_dir),
                "timestamp": utc_now(),
            },
        )
        try:
            result = await asyncio.wait_for(
                _execute_harness_attempt(
                    manifest,
                    task,
                    store,
                    run_id=work_id,
                    attempt_id=attempt_id,
                    harness_name=harness_name,
                    harness_version=harness_version,
                    progress_callback=progress_callback,
                ),
                timeout=manifest.config.timeout_seconds + 2,
            )
        except asyncio.TimeoutError:
            error = f"task timed out after {manifest.config.timeout_seconds} seconds"
            attempt = store.attempt(attempt_id)
            if attempt is not None and attempt["status"] == "running":
                store.finish_attempt(attempt_id, "failed", error)
            result = AgentRunResult(
                success=False,
                task_id=task.id,
                run_dir=run_dir,
                work_id=work_id,
                run_id=work_id,
                attempt_id=attempt_id,
                harness=harness_name,
                error=error,
            )
        last_result = result
        if result.success:
            task.result = str(result.artifact_record_path or result.run_dir)
            task.error = ""
            task.status = "done"
            task.lease = ""
            task.updated = utc_now()
            _emit(
                progress_callback,
                {
                    "type": "task_done",
                    "work_id": work_id,
                    "run_id": work_id,
                    "task_id": task.id,
                    "attempt_id": attempt_id,
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
            task.status = "retry"
            task.lease = ""
            task.updated = utc_now()
            try:
                store.mark_run_task_retry(work_id, task.id)
            except ValueError:
                store.update_run_task(work_id, task.id, "retry", error=task.error)
            _emit(
                progress_callback,
                {
                    "type": "task_retry",
                    "work_id": work_id,
                    "run_id": work_id,
                    "task_id": task.id,
                    "attempt_id": attempt_id,
                    "attempt": task.attempts,
                    "status": task.status,
                    "run_dir": str(result.run_dir),
                    "error": task.error,
                    "timestamp": utc_now(),
                },
            )
            continue
        task.status = "failed"
        task.lease = ""
        task.updated = utc_now()
        store.update_run_task(work_id, task.id, "failed", error=task.error)
        _emit(
            progress_callback,
            {
                "type": "task_failed",
                "work_id": work_id,
                "run_id": work_id,
                "task_id": task.id,
                "attempt_id": attempt_id,
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


async def _execute_harness_attempt(
    manifest: Manifest,
    task: Task,
    store: SessionStore,
    *,
    run_id: str,
    attempt_id: str,
    harness_name: str,
    harness_version: str,
    progress_callback: ProgressCallback | None,
) -> AgentRunResult:
    run_dir = task_run_dir(manifest.config, manifest.path, task, attempt_id, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace = prepare_workspace(manifest.config, manifest.path, task, run_dir)
    prompt = build_task_prompt(manifest.config, task, store, workspace, manifest.path)
    request = HarnessRequest(
        run_id=run_id,
        attempt_id=attempt_id,
        manifest_path=manifest.path,
        config=manifest.config,
        task=task,
        workspace=workspace,
        run_dir=run_dir,
        prompt=prompt,
        harness_config=manifest.config.harness,
        timeout_seconds=manifest.config.timeout_seconds,
        progress_callback=_attempt_progress_callback(
            progress_callback,
            run_id,
            attempt_id,
            task.id,
            store=store if harness_name != "native" else None,
        ),
        store=store,
    )
    if harness_name != "native":
        store.start_attempt(
            attempt_id,
            run_id,
            task.id,
            task.attempts,
            run_dir,
            harness=harness_name,
            harness_version=harness_version,
        )
        write_json(
            run_dir / "task.json",
            {
                "run_id": run_id,
                "attempt_id": attempt_id,
                "task": task.__dict__,
                "workspace": str(workspace),
                "harness": harness_name,
                "run_vars": manifest.config.run_vars,
            },
        )

    try:
        harness_result = await run_harness(request, harness_name)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error = str(exc)
        attempt = store.attempt(attempt_id)
        if attempt is not None and attempt["status"] == "running":
            store.finish_attempt(attempt_id, "failed", error)
        return AgentRunResult(
            success=False,
            task_id=task.id,
            run_dir=run_dir,
            work_id=run_id,
            run_id=run_id,
            attempt_id=attempt_id,
            harness=harness_name,
            error=error,
        )
    if harness_name == "native":
        artifact_record = run_dir / "artifact.json"
        return AgentRunResult(
            success=harness_result.success,
            task_id=task.id,
            run_dir=run_dir,
            work_id=run_id,
            run_id=run_id,
            attempt_id=attempt_id,
            harness=harness_name,
            usage=harness_result.usage,
            artifact_record_path=artifact_record if artifact_record.exists() else None,
            artifact=harness_result.submission,
            error=harness_result.error,
        )

    if harness_result.stdout_tail:
        store.add_message(
            attempt_id,
            1_000_000,
            "harness",
            harness_result.stdout_tail,
            {"harness": harness_name, "stream": "stdout", "session_id": harness_result.session_id},
        )
    if harness_result.stderr_tail:
        store.add_message(
            attempt_id,
            1_000_001,
            "harness-stderr",
            harness_result.stderr_tail,
            {"harness": harness_name, "stream": "stderr", "session_id": harness_result.session_id},
        )

    artifact = harness_result.submission
    error = harness_result.error
    success = harness_result.success
    artifact_record: Path | None = None
    result_record: dict[str, Any] = {
        "harness": harness_name,
        "external_session_id": harness_result.session_id,
        "output": harness_result.output if harness_result.output is not None else harness_result.stdout_tail,
    }
    stdout_log = run_dir / "stdout.jsonl"
    stderr_log = run_dir / "stderr.log"
    if stdout_log.is_file():
        result_record["stdout_log"] = str(stdout_log)
    if stderr_log.is_file():
        result_record["stderr_log"] = str(stderr_log)
    if harness_result.stderr_tail:
        result_record["stderr_tail"] = harness_result.stderr_tail
    if success:
        try:
            validate_artifact(manifest.config, task, workspace, run_dir, artifact)
            if artifact is not None:
                record = {
                    "run_id": run_id,
                    "attempt_id": attempt_id,
                    "task_id": task.id,
                    "summary": artifact.summary,
                    "artifact_path": artifact.artifact_path,
                    "metadata": artifact.metadata,
                }
                artifact_record = run_dir / "artifact.json"
                write_json(artifact_record, record)
                store.add_artifact(
                    attempt_id,
                    task.id,
                    artifact.summary,
                    artifact.artifact_path,
                    artifact.metadata,
                )
                result_record.update(record)
        except ArtifactValidationError as exc:
            success = False
            error = str(exc)

    store.finish_attempt(
        attempt_id,
        "done" if success else "failed",
        error,
        result=result_record,
        usage=harness_result.usage,
        external_session_id=harness_result.session_id,
        exit_code=harness_result.exit_code,
        pid=harness_result.pid,
    )
    return AgentRunResult(
        success=success,
        task_id=task.id,
        run_dir=run_dir,
        work_id=run_id,
        run_id=run_id,
        attempt_id=attempt_id,
        harness=harness_name,
        usage=harness_result.usage,
        artifact_record_path=artifact_record,
        artifact=artifact,
        error=error,
    )


def _state_db_path(manifest: Manifest) -> Path:
    destination = global_state_db_path()
    configured = manifest.config.run_dir.strip().replace("\\", "/").rstrip("/")
    expanded_default = str((Path.home() / ".bagent" / "runs").resolve(strict=False)).replace("\\", "/")
    if configured in {"", "~/.bagent/runs", expanded_default}:
        legacy_root = runs_dir()
    else:
        legacy_root = Path(manifest.config.run_dir).expanduser()
        if not legacy_root.is_absolute():
            legacy_root = manifest.path.parent / legacy_root
    legacy_path = legacy_root.resolve(strict=False) / "state.sqlite3"
    if legacy_path.is_file() and legacy_path.resolve(strict=False) != destination.resolve(strict=False):
        store = SessionStore(destination)
        try:
            store.import_legacy_database(
                legacy_path,
                manifest.path,
                manifest.config.name,
            )
        finally:
            store.close()
    return destination


def state_db_path(manifest: Manifest) -> Path:
    return _state_db_path(manifest)


def new_work_id() -> str:
    """Compatibility alias for callers using the pre-v2 name."""
    return new_run_id()


def new_run_id() -> str:
    return f"run-{uuid.uuid4().hex[:12]}"


def new_attempt_id() -> str:
    return f"attempt-{uuid.uuid4().hex[:12]}"


async def resume_manifest(
    manifest_path: str | Path,
    run_id: str,
    *,
    task_ids: set[str] | None = None,
    retry_failed: bool = False,
    harness: str | None = None,
    progress_callback: ProgressCallback | None = None,
    pause_event: asyncio.Event | None = None,
) -> list[AgentRunResult]:
    return await run_manifest(
        manifest_path,
        run_id=run_id,
        resume=True,
        task_ids=task_ids,
        retry_failed=retry_failed,
        harness=harness,
        progress_callback=progress_callback,
        pause_event=pause_event,
    )


async def retry_run_task(
    manifest_path: str | Path,
    run_id: str,
    task_id: str,
    *,
    progress_callback: ProgressCallback | None = None,
    pause_event: asyncio.Event | None = None,
) -> list[AgentRunResult]:
    manifest = load_manifest(manifest_path)
    store = SessionStore(_state_db_path(manifest))
    try:
        record = store.batch_run(run_id)
        if record is None:
            raise SchedulerError(f"run not found: {run_id}")
        expected_path = str(manifest.path.resolve())
        if record["manifest_path"] != expected_path:
            raise SchedulerError(f"run {run_id} belongs to another batch config: {record['manifest_path']}")
        if record["status"] not in {"paused", "failed", "interrupted"}:
            raise SchedulerError(f"run {run_id} is not retryable from status {record['status']}")
        store.mark_run_task_retry(run_id, task_id)
    finally:
        store.close()
    return await resume_manifest(
        manifest_path,
        run_id,
        task_ids={task_id},
        retry_failed=True,
        progress_callback=progress_callback,
        pause_event=pause_event,
    )


def _fresh_run_task(task: Task) -> Task:
    value = copy.deepcopy(task)
    value.status = "todo"
    value.result = ""
    value.attempts = 0
    value.updated = ""
    value.error = ""
    value.lease = ""
    return value


def _task_from_snapshot(row: dict[str, Any]) -> Task:
    definition = dict(row.get("definition") or {})
    result = row.get("result") or {}
    if isinstance(result, dict):
        result_text = str(result.get("artifact_path") or result.get("result") or "")
    else:
        result_text = str(result)
    return Task(
        status=str(row.get("status") or "queued"),
        id=str(row["task_id"]),
        kind=str(definition.get("kind") or row.get("kind") or ""),
        input=dict(definition.get("input") or row.get("input") or {}),
        result=result_text,
        attempts=int(row.get("attempt_count") or 0),
        error=str(row.get("error") or ""),
        lease=str(row.get("latest_attempt_id") or ""),
    )


def _raise_missing_task_ids(requested: set[str], known: set[str]) -> None:
    missing = sorted(requested - known)
    if missing:
        raise SchedulerError(f"task id(s) not found: {', '.join(missing)}")


def _resolve_harness_name(manifest: Manifest, explicit: str | None) -> str:
    if explicit:
        return canonical_harness_name(explicit)
    if "harness" in manifest.config.raw:
        return canonical_harness_name(manifest.config.harness.name or "native")
    try:
        configured = str(load_settings().get("harness") or "native")
    except SettingsError:
        configured = "native"
    return canonical_harness_name(configured.strip().lower() or "native")


def _runtime_harness_config(manifest: Manifest, selected: str) -> HarnessConfig:
    current = manifest.config.harness
    if canonical_harness_name(current.name or "native") == selected:
        current.name = selected
        return current
    return HarnessConfig(name=selected)


def _batch_config_from_snapshot(current: BatchConfig, snapshot: Any) -> BatchConfig:
    """Restore the execution config frozen when the Run was created.

    The manifest file remains the address of a Batch Config, but edits to its
    prompt, workspace, retry policy, or harness options must not silently
    change the meaning of an existing Run. Older rows without a complete
    snapshot retain the currently parsed values for forward compatibility.
    """

    if not isinstance(snapshot, dict):
        return current
    restored = copy.deepcopy(current)
    nested = {"artifact", "harness", "run_variables"}
    for descriptor in fields(BatchConfig):
        name = descriptor.name
        if name in nested or name not in snapshot:
            continue
        setattr(restored, name, copy.deepcopy(snapshot[name]))

    artifact = snapshot.get("artifact")
    if isinstance(artifact, dict):
        restored.artifact = ArtifactPolicy(
            require_submit=bool(artifact.get("require_submit", True)),
            require_artifact_path=bool(artifact.get("require_artifact_path", False)),
            required_metadata_keys=[str(item) for item in artifact.get("required_metadata_keys") or []],
            validator_command=[str(item) for item in artifact.get("validator_command") or []],
            validator_timeout_seconds=int(artifact.get("validator_timeout_seconds", 120)),
        )
    if isinstance(snapshot.get("harness"), dict):
        restored.harness = _harness_config_from_snapshot(snapshot)
    variables = snapshot.get("run_variables")
    if isinstance(variables, list):
        restored.run_variables = [
            RunVariable(
                name=str(item.get("name") or ""),
                label=str(item.get("label") or ""),
                default=str(item.get("default") or ""),
                required=bool(item.get("required", True)),
            )
            for item in variables
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
    return restored


def _harness_config_from_snapshot(snapshot: Any) -> HarnessConfig:
    if not isinstance(snapshot, dict):
        return HarnessConfig()
    value = snapshot.get("harness")
    if not isinstance(value, dict):
        return HarnessConfig()
    return HarnessConfig(
        name=str(value.get("name") or "native"),
        command=[str(item) for item in value.get("command") or []],
        extra_args=[str(item) for item in value.get("extra_args") or []],
        model=str(value.get("model") or ""),
        agent=str(value.get("agent") or ""),
        inject_tools=bool(value.get("inject_tools", True)),
        env_allowlist=[str(item) for item in value.get("env_allowlist") or []],
    )


def _derive_run_status(tasks: list[dict[str, Any]]) -> str:
    statuses = {str(row.get("status") or "") for row in tasks}
    if "failed" in statuses:
        return "failed"
    if statuses & {"queued", "retry", "running", "interrupted", "todo", "needs-review"}:
        return "paused"
    if not statuses or statuses <= {"done", "skipped"}:
        return "completed"
    return "paused"


def _summarize_run_tasks(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for row in tasks:
        status = str(row.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
        task_id = str(row.get("task_id") or "")
        result = row.get("result")
        if task_id and result not in (None, {}, ""):
            results[task_id] = result
        error = str(row.get("error") or "")
        if task_id and error:
            errors[task_id] = error
    pending_statuses = {"queued", "retry", "running", "interrupted", "todo", "needs-review"}
    return {
        "tasks": len(tasks),
        "done": counts.get("done", 0),
        "failed": counts.get("failed", 0),
        "skipped": counts.get("skipped", 0),
        "pending": sum(counts.get(status, 0) for status in pending_statuses),
        "status_counts": counts,
        "task_results": results,
        "task_errors": errors,
    }


def _attempt_progress_callback(
    callback: ProgressCallback | None,
    run_id: str,
    attempt_id: str,
    task_id: str,
    *,
    store: SessionStore | None = None,
) -> ProgressCallback | None:
    if callback is None and store is None:
        return None

    tool_seq = 0
    message_seq = 0

    def enriched(event: dict[str, Any]) -> None:
        nonlocal message_seq, tool_seq
        payload = dict(event)
        payload["run_id"] = run_id
        payload["work_id"] = run_id
        payload["attempt_id"] = attempt_id
        payload.setdefault("task_id", task_id)
        event_type = str(payload.get("type") or "")
        if store is not None:
            if event_type == "harness_started":
                store.update_attempt_process(attempt_id, pid=_optional_int(payload.get("pid")))
            elif event_type == "harness_session":
                store.update_attempt_process(
                    attempt_id,
                    external_session_id=str(payload.get("session_id") or ""),
                )
            elif event_type == "harness_usage" and isinstance(payload.get("usage"), dict):
                store.update_attempt_usage(attempt_id, payload["usage"])
            elif event_type == "harness_finished":
                store.update_attempt_process(
                    attempt_id,
                    external_session_id=str(payload.get("session_id") or ""),
                    exit_code=_optional_int(payload.get("exit_code")),
                )
                if isinstance(payload.get("usage"), dict):
                    store.update_attempt_usage(attempt_id, payload["usage"])

            if event_type in {"model_delta", "harness_output", "harness_stderr", "harness_output_error"}:
                content = str(payload.get("delta") or payload.get("content") or payload.get("error") or "")
                if content:
                    message_seq += 1
                    role = {
                        "model_delta": "assistant",
                        "harness_output": "harness",
                        "harness_stderr": "harness-stderr",
                        "harness_output_error": "harness-error",
                    }[event_type]
                    store.add_message(attempt_id, message_seq, role, content, payload)

            if event_type in {"tool_started", "tool_finished", "harness_progress"}:
                tool_seq += 1
                store.add_tool_event(
                    attempt_id,
                    tool_seq,
                    str(payload.get("tool") or event_type),
                    dict(payload.get("arguments") or payload.get("metadata") or {}),
                    payload.get("result")
                    if isinstance(payload.get("result"), dict)
                    else {"message": payload.get("message")},
                    str(payload.get("error") or ""),
                )
        _emit(callback, payload)

    return enriched


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _resolve_run_vars(manifest: Manifest, provided: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    missing: list[str] = []
    for variable in manifest.config.run_variables:
        value = provided.get(variable.name, variable.default)
        if isinstance(value, str):
            value = value.replace("CURR_DATE", date.today().isoformat())
        if variable.required and str(value).strip() == "":
            missing.append(variable.name)
        values[variable.name] = value
    for key, value in provided.items():
        values.setdefault(key, value)
    if missing:
        raise SchedulerError(f"missing required run variable(s): {', '.join(missing)}")
    return values


def _emit(callback: ProgressCallback | None, event: dict[str, Any]) -> None:
    if callback is None:
        return
    try:
        callback(event)
    except Exception:
        pass


def validate_manifest(manifest: Manifest, *, harness_name: str | None = None) -> None:
    ids = [task.id for task in manifest.tasks]
    duplicates = sorted({task_id for task_id in ids if ids.count(task_id) > 1})
    if duplicates:
        raise SchedulerError(f"duplicate task ids: {', '.join(duplicates)}")
    if not manifest.config.user_prompt_template.strip():
        raise SchedulerError("user_prompt_template is required")
    unknown = unknown_tool_names(manifest.config)
    if unknown:
        raise SchedulerError(f"unknown tools in manifest config: {', '.join(unknown)}")
    selected_harness = canonical_harness_name(harness_name) if harness_name else _resolve_harness_name(manifest, None)
    if (
        manifest.config.artifact.require_submit
        and selected_harness == "native"
        and "submit_artifact" not in manifest.config.tools
    ):
        raise SchedulerError("native harness requires submit_artifact in tools when artifact.require_submit is true")
    if manifest.config.command_policy not in {"allowlist", "blacklist"}:
        raise SchedulerError("command_policy must be allowlist or blacklist")
    if (
        "run_command" in manifest.config.tools
        and manifest.config.command_policy == "allowlist"
        and not manifest.config.allowed_command_prefixes
    ):
        raise SchedulerError("run_command is loaded but allowed_command_prefixes is empty")
    if manifest.config.task_selector_script and manifest.config.task_selector_command:
        raise SchedulerError("configure only one of task_selector_script or task_selector_command")
    load_manifest_skills(manifest.config, manifest.path)
