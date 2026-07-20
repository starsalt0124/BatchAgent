from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .harness import available_harnesses, probe_harness
from .manifest import ManifestError, create_sample_manifest, load_manifest
from .provider import create_provider
from .progress import PlainProgress, ProgressState
from .scheduler import (
    mark_tasks_for_retry,
    new_run_id,
    request_pause,
    recover_running,
    resume_manifest,
    retry_run_task,
    run_manifest,
    state_db_path,
    status,
    tasks_by_status,
    validate_manifest,
)
from .settings import load_settings, update_settings
from .skills import SkillError, install_skill, installed_skills_dir, list_installed_skills
from .store import SessionStore
from .util import console_safe, truncate


CLI_NAME = "bagent"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        from .tui import run_tui

        return run_tui()

    parser = argparse.ArgumentParser(prog=CLI_NAME, description="Markdown-driven batch agent harness.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    tui_parser = subparsers.add_parser("tui", help="Start the full-screen interactive TUI.")
    tui_parser.add_argument("manifest", nargs="?", default=None)

    init_parser = subparsers.add_parser("init", help="Create a sample manifest.")
    init_parser.add_argument("manifest", nargs="?", default="BATCHAGENT.md")

    doctor_parser = subparsers.add_parser("doctor", help="Validate a manifest without running tasks.")
    doctor_parser.add_argument("manifest")

    status_parser = subparsers.add_parser("status", help="Print Task status counts from the latest Run.")
    status_parser.add_argument("manifest")

    run_parser = subparsers.add_parser("run", help="Create a new Run from a Batch Config, in the TUI by default.")
    run_parser.add_argument("manifest")
    run_parser.add_argument("--limit", type=int, default=None, help="Maximum Task rows to select for this Run.")
    run_parser.add_argument("--retry-failed", action="store_true", help="Compatibility flag; new Runs already ignore prior cache failures.")
    run_parser.add_argument("--only", action="append", default=None, help="Select only this Task id for the Run. Can be repeated.")
    run_parser.add_argument("--plain", action="store_true", help="Use non-interactive plain progress output instead of the TUI.")
    run_parser.add_argument("--no-progress", action="store_true", help="Disable progress output during execution.")
    run_parser.add_argument("--focus", default="", help="Focus one task in the TUI run page.")
    run_parser.add_argument("--var", action="append", default=None, help="Runtime template variable as name=value. Can be repeated.")
    run_parser.add_argument("--harness", choices=available_harnesses(), default=None, help="Task harness for this new run.")

    resume_parser = subparsers.add_parser("resume", help="Resume an incomplete persisted run using the same run id.")
    resume_parser.add_argument("manifest")
    resume_parser.add_argument("run_id")
    resume_parser.add_argument("--only", action="append", default=None, help="Resume only this task id. Can be repeated.")
    resume_parser.add_argument("--retry-failed", action="store_true", help="Include failed tasks when resuming.")
    resume_parser.add_argument("--plain", action="store_true", help="Use non-interactive plain progress output.")
    resume_parser.add_argument("--no-progress", action="store_true", help="Disable progress output.")
    resume_parser.add_argument("--harness", choices=available_harnesses(), default=None, help="Override the frozen harness explicitly.")

    runs_parser = subparsers.add_parser("runs", help="List persisted runs for a batch config.")
    runs_parser.add_argument("manifest")
    runs_parser.add_argument("--json", action="store_true")

    recover_parser = subparsers.add_parser(
        "recover",
        help="Explicitly interrupt a stale persisted Run, or update a v1 manifest cache.",
    )
    recover_parser.add_argument("manifest")
    recover_parser.add_argument("--run-id", default="", help="Persisted running Run to mark interrupted.")
    recover_parser.add_argument(
        "--to",
        choices=["retry", "failed", "todo"],
        default="retry",
        help="Compatibility target for stale rows in the v1 manifest cache when --run-id is omitted.",
    )

    pause_parser = subparsers.add_parser("pause", help="Request a safe pause for an active Run.")
    pause_parser.add_argument("manifest", help="Run id, or a Batch Config path with one active Run.")

    failures_parser = subparsers.add_parser("failures", help="List failed tasks and suggested recovery commands.")
    failures_parser.add_argument("manifest")

    retry_parser = subparsers.add_parser("retry", help="Append an Attempt to a failed Run Task (or use v1 cache compatibility).")
    retry_parser.add_argument("manifest")
    retry_parser.add_argument("task_ids", nargs="*", help="Task ids to retry. Defaults to all failed tasks.")
    retry_parser.add_argument("--reset-attempts", action="store_true", help="Reset attempt counters before retrying.")
    retry_parser.add_argument("--run-id", default="", help="Retry inside this persisted run instead of changing the manifest cache.")

    rerun_parser = subparsers.add_parser("rerun", help="Create a new Run containing selected task ids.")
    rerun_parser.add_argument("manifest")
    rerun_parser.add_argument("task_ids", nargs="+")

    inspect_parser = subparsers.add_parser("inspect", help="Inspect one Run Task and its persisted Attempts.")
    inspect_parser.add_argument("manifest")
    inspect_parser.add_argument("task_id")
    inspect_parser.add_argument("--run-id", default="", help="Containing Run id. Defaults to the latest Run containing this task.")
    inspect_parser.add_argument("--attempt-id", default="", help="Specific task Attempt id. Defaults to the latest Attempt.")
    inspect_parser.add_argument("--messages", type=int, default=12, help="Recent messages to show.")
    inspect_parser.add_argument("--tools", type=int, default=20, help="Recent tool events to show.")

    models_parser = subparsers.add_parser("models", help="List models through the configured provider.")
    models_parser.add_argument("manifest", nargs="?", default=None)
    models_parser.add_argument("--base-url", default=None)
    models_parser.add_argument("--api-key-env", default=None)

    harness_parser = subparsers.add_parser("harness", help="Show, select, reset, or probe local task harnesses.")
    harness_parser.add_argument("action", nargs="?", choices=["show", "use", "reset", "doctor"], default="show")
    harness_parser.add_argument("name", nargs="?", choices=available_harnesses())

    skills_parser = subparsers.add_parser("skills", help="Install and list reusable agent skills.")
    skills_subparsers = skills_parser.add_subparsers(dest="skills_command", required=True)
    skills_list_parser = skills_subparsers.add_parser("list", help="List installed skills.")
    skills_list_parser.add_argument("--json", action="store_true", help="Print JSON instead of a plain table.")
    skills_install_parser = skills_subparsers.add_parser("install", help="Install a skill directory or SKILL.md file.")
    skills_install_parser.add_argument("source")
    skills_install_parser.add_argument("--name", default="", help="Installed skill name. Defaults to the source directory name.")
    skills_install_parser.add_argument("--force", action="store_true", help="Replace an existing installed skill with the same name.")

    args = parser.parse_args(argv)
    try:
        if args.command == "tui":
            from .tui import run_tui

            return run_tui(args.manifest)
        if args.command == "init":
            create_sample_manifest(args.manifest)
            print(f"created {args.manifest}")
            return 0
        if args.command == "doctor":
            manifest = load_manifest(args.manifest)
            validate_manifest(manifest)
            _print_manifest_summary(manifest)
            return 0
        if args.command == "status":
            print(json.dumps(status(args.manifest), ensure_ascii=False, indent=2))
            return 0
        if args.command == "run":
            if not args.plain and not args.no_progress:
                from .tui import run_tui

                return run_tui(args.manifest, auto_run_args=_auto_run_args(args))
            args.run_id = new_run_id()
            results = asyncio.run(_run_with_progress(args))
            final_status = _print_run_results(args.manifest, results, run_id=args.run_id)
            return _run_exit_code(final_status, results)
        if args.command == "resume":
            if not args.plain and not args.no_progress:
                from .tui import run_tui

                auto_args = ["--resume", args.run_id]
                for task_id in args.only or []:
                    auto_args.extend(["--only", task_id])
                if args.retry_failed:
                    auto_args.append("--retry-failed")
                if args.harness:
                    auto_args.extend(["--harness", args.harness])
                return run_tui(args.manifest, auto_run_args=auto_args)
            results = asyncio.run(_resume_with_progress(args))
            final_status = _print_run_results(args.manifest, results, run_id=args.run_id)
            return _run_exit_code(final_status, results)
        if args.command == "runs":
            return _runs(args)
        if args.command == "recover":
            if args.run_id:
                return _recover_persisted_run(args.manifest, args.run_id)
            changed = recover_running(args.manifest, args.to)
            print(f"recovered {changed} running task(s) to {args.to}")
            return 0
        if args.command == "pause":
            path = request_pause(args.manifest)
            print(f"pause requested: {path}")
            print("running tasks will finish; no new tasks will be started for this work")
            return 0
        if args.command == "failures":
            return _failures(args.manifest)
        if args.command == "retry":
            if args.run_id:
                if len(args.task_ids) != 1:
                    raise RuntimeError("retry --run-id requires exactly one task id")
                _require_run_for_manifest(args.manifest, args.run_id)
                results = asyncio.run(retry_run_task(args.manifest, args.run_id, args.task_ids[0]))
                final_status = _print_run_results(args.manifest, results, run_id=args.run_id)
                return _run_exit_code(final_status, results)
            task_ids = set(args.task_ids) if args.task_ids else None
            changed = mark_tasks_for_retry(args.manifest, task_ids, reset_attempts=args.reset_attempts)
            print(f"marked {changed} task(s) as retry")
            print(f"next: {CLI_NAME} run {args.manifest}")
            return 0
        if args.command == "rerun":
            results = asyncio.run(run_manifest(args.manifest, task_ids=set(args.task_ids)))
            _print_run_results(args.manifest, results)
            return 0 if all(result.success for result in results) else 1
        if args.command == "inspect":
            return _inspect(args)
        if args.command == "models":
            return _models(args)
        if args.command == "harness":
            return asyncio.run(_harness_command(args))
        if args.command == "skills":
            return _skills(args)
    except (ManifestError, RuntimeError, FileNotFoundError, ValueError, SkillError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


async def _run_with_progress(args) -> list:
    manifest = load_manifest(args.manifest)
    validate_manifest(manifest)
    task_ids = set(args.only) if args.only else None
    run_vars = _parse_run_vars(args.var or [])
    if args.no_progress:
        return await run_manifest(
            args.manifest,
            limit=args.limit,
            retry_failed=args.retry_failed,
            task_ids=task_ids,
            run_id=args.run_id,
            run_vars=run_vars,
            harness=args.harness,
        )

    state = ProgressState.from_manifest(manifest, focus_task_id=args.focus)
    plain = PlainProgress(state)
    return await run_manifest(
        args.manifest,
        limit=args.limit,
        retry_failed=args.retry_failed,
        task_ids=task_ids,
        run_id=args.run_id,
        run_vars=run_vars,
        harness=args.harness,
        progress_callback=plain.callback,
    )


async def _resume_with_progress(args) -> list:
    task_ids = set(args.only) if args.only else None
    if args.no_progress:
        return await resume_manifest(
            args.manifest,
            args.run_id,
            task_ids=task_ids,
            retry_failed=args.retry_failed,
            harness=args.harness,
        )
    manifest = load_manifest(args.manifest)
    state = ProgressState.from_manifest(manifest)
    plain = PlainProgress(state)
    return await resume_manifest(
        args.manifest,
        args.run_id,
        task_ids=task_ids,
        retry_failed=args.retry_failed,
        harness=args.harness,
        progress_callback=plain.callback,
    )


def _auto_run_args(args) -> list[str]:
    values: list[str] = []
    if args.limit is not None:
        values.extend(["--limit", str(args.limit)])
    if args.retry_failed:
        values.append("--retry-failed")
    for task_id in args.only or []:
        values.extend(["--only", task_id])
    if args.focus and not args.only:
        values.extend(["--focus", args.focus])
    for variable in args.var or []:
        values.extend(["--var", variable])
    if getattr(args, "harness", None):
        values.extend(["--harness", args.harness])
    return values


def _parse_run_vars(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise RuntimeError(f"--var must be name=value: {value}")
        name, item = value.split("=", 1)
        name = name.strip()
        if not name:
            raise RuntimeError(f"--var name is empty: {value}")
        parsed[name] = item
    return parsed


def _print_rich_completion(manifest_path: str, results) -> None:
    failed = [result for result in results if not result.success]
    work_id = next((result.work_id for result in results if getattr(result, "work_id", "")), "")
    if failed:
        print(f"Batch work {work_id or '(unknown)'} finished with {len(failed)} failed task(s).")
        print("Recovery options:")
        print(f"  Inspect: {CLI_NAME} inspect {manifest_path} {failed[0].task_id}")
        print(f"  Retry:   {CLI_NAME} retry {manifest_path} " + " ".join(result.task_id for result in failed))
        print(f"  Run:     {CLI_NAME} run {manifest_path} --retry-failed")
        return
    print(f"Batch work {work_id or '(unknown)'} completed: {len(results)} task(s) done.")


def _print_run_results(manifest_path: str, results, *, run_id: str = "") -> str:
    for result in results:
        state = "done" if result.success else "failed"
        print(
            f"{state}\trun={result.run_id or result.work_id}\ttask={result.task_id}"
            f"\tattempt={result.attempt_id or '-'}\tharness={result.harness}\t{result.run_dir}"
        )
        if result.error:
            print(f"  error: {result.error}")
    selected_run_id = run_id or next(
        (str(result.run_id or result.work_id) for result in results if result.run_id or result.work_id),
        "",
    )
    if not selected_run_id:
        failed = [result for result in results if not result.success]
        return "failed" if failed else "completed"

    run, tasks = _persisted_run_snapshot(manifest_path, selected_run_id)
    final_status = str(run["status"])
    counts: dict[str, int] = {}
    for task in tasks:
        task_status = str(task["status"])
        counts[task_status] = counts.get(task_status, 0) + 1
    count_text = ", ".join(f"{key}={counts[key]}" for key in sorted(counts)) or "tasks=0"
    print("")
    print(f"Run {selected_run_id}: {final_status} ({count_text})")

    unfinished = [
        task
        for task in tasks
        if str(task["status"]) in {"queued", "retry", "interrupted", "running", "todo", "needs-review"}
    ]
    failed_tasks = [task for task in tasks if str(task["status"]) == "failed"]
    if final_status == "completed":
        print("All selected Tasks are complete.")
    elif final_status in {"paused", "interrupted", "queued"}:
        print(f"Run is incomplete with {len(unfinished)} resumable Task(s).")
        print(f"  Resume:  {CLI_NAME} resume {manifest_path} {selected_run_id}")
    elif final_status == "failed":
        print(f"Run has {len(failed_tasks)} failed Task(s).")
        if unfinished:
            print(f"  Resume unfinished:  {CLI_NAME} resume {manifest_path} {selected_run_id}")
        if failed_tasks:
            task_id = str(failed_tasks[0]["task_id"])
            print(f"  Retry failed Task:  {CLI_NAME} retry {manifest_path} {task_id} --run-id {selected_run_id}")
            print(f"  Inspect:            {CLI_NAME} inspect {manifest_path} {task_id} --run-id {selected_run_id}")
    elif final_status == "running":
        print("Run is still active. Use the Runs view to monitor it or request a safe pause.")
    return final_status


def _run_exit_code(final_status: str, results) -> int:
    if final_status in {"completed", "paused"}:
        return 0
    if final_status:
        return 1
    return 0 if all(result.success for result in results) else 1


def _persisted_run_snapshot(manifest_path: str | Path, run_id: str) -> tuple[dict, list[dict]]:
    manifest = load_manifest(manifest_path)
    db_path = state_db_path(manifest)
    if not db_path.exists():
        raise RuntimeError("no persisted runs")
    store = SessionStore(db_path)
    try:
        run = store.batch_run(run_id)
        if run is None:
            raise RuntimeError(f"run not found: {run_id}")
        expected_path = str(manifest.path.resolve())
        if str(run["manifest_path"]) != expected_path:
            raise RuntimeError(f"run {run_id} belongs to another batch config: {run['manifest_path']}")
        return run, store.run_tasks(run_id)
    finally:
        store.close()


def _require_run_for_manifest(manifest_path: str | Path, run_id: str) -> dict:
    run, _tasks = _persisted_run_snapshot(manifest_path, run_id)
    return run


def _recover_persisted_run(manifest_path: str | Path, run_id: str) -> int:
    run, _tasks = _persisted_run_snapshot(manifest_path, run_id)
    if str(run["status"]) != "running":
        raise RuntimeError(f"run is not active: {run_id} (status={run['status']})")
    manifest = load_manifest(manifest_path)
    store = SessionStore(state_db_path(manifest))
    try:
        store.interrupt_batch_run(run_id)
        tasks = store.run_tasks(run_id)
    finally:
        store.close()
    retryable = sum(1 for task in tasks if str(task["status"]) in {"retry", "queued", "interrupted"})
    print(f"recovered stale Run {run_id}: running -> interrupted")
    print(f"retryable Tasks: {retryable}")
    print(f"next: {CLI_NAME} resume {manifest_path} {run_id}")
    return 0


def _print_manifest_summary(manifest) -> None:
    counts: dict[str, int] = {}
    for task in manifest.tasks:
        counts[task.status] = counts.get(task.status, 0) + 1
    print(f"name: {manifest.config.name}")
    print(f"provider: {manifest.config.provider}")
    print(f"model: {manifest.config.model}")
    print(f"tools: {', '.join(manifest.config.tools) if manifest.config.tools else '(none)'}")
    if "run_command" in manifest.config.tools:
        print(f"command policy: {manifest.config.command_policy}")
    print(f"workspace: {Path(manifest.config.workspace)} ({manifest.config.workspace_mode})")
    print(f"parallel: {manifest.config.parallel}, max_concurrency: {manifest.config.effective_concurrency}")
    if manifest.config.task_selector_command or manifest.config.task_selector_script:
        selector = manifest.config.task_selector_command or [manifest.config.task_selector_script]
        print(f"task selector: {' '.join(selector)}")
    print(f"tasks: {len(manifest.tasks)}")
    print(json.dumps(counts, ensure_ascii=False, indent=2))


def _models(args) -> int:
    if args.manifest:
        config = load_manifest(args.manifest).config
    else:
        from .models import BatchConfig

        config = BatchConfig()
    if args.base_url:
        config.base_url = args.base_url
    if args.api_key_env:
        config.api_key_env = args.api_key_env
    provider = create_provider(config)
    print(json.dumps(provider.list_models(), ensure_ascii=False, indent=2))
    return 0


def _runs(args) -> int:
    manifest = load_manifest(args.manifest)
    db_path = state_db_path(manifest)
    if not db_path.exists():
        rows: list[dict] = []
    else:
        store = SessionStore(db_path)
        try:
            rows = store.batch_runs(manifest.path)
        finally:
            store.close()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("(no persisted runs)")
        return 0
    print("run_id\tstatus\tharness\ttasks\tduration\ttokens\tstarted_at")
    for row in rows:
        print(
            "\t".join(
                [
                    str(row["run_id"]),
                    str(row["status"]),
                    str(row["harness"]),
                    str(len(row.get("selected_task_ids") or [])),
                    f"{float(row.get('elapsed_seconds') or 0):.1f}s",
                    str(row.get("total_tokens") if row.get("total_tokens") is not None else "-"),
                    str(row["started_at"]),
                ]
            )
        )
    return 0


async def _harness_command(args) -> int:
    settings = load_settings()
    current = str(settings.get("harness") or "native")
    if args.action == "show":
        print(f"current: {current}")
        print("available: " + ", ".join(available_harnesses()))
        return 0
    if args.action == "reset":
        update_settings({"harness": "native"})
        print("harness reset to native")
        return 0
    if not args.name:
        raise RuntimeError(f"harness {args.action} requires a harness name")
    probe = await probe_harness(args.name)
    if args.action == "doctor":
        state = "available" if probe.available else "unavailable"
        detail = probe.version or probe.error or "(no detail)"
        print(f"{probe.name}: {state}: {detail}")
        return 0 if probe.available else 1
    if not probe.available:
        raise RuntimeError(f"harness {args.name} is unavailable: {probe.error}")
    update_settings({"harness": args.name})
    print(f"harness set to {args.name} ({probe.version or probe.executable})")
    return 0


def _skills(args) -> int:
    if args.skills_command == "list":
        rows = list_installed_skills()
        if args.json:
            print(json.dumps({"root": str(installed_skills_dir()), "skills": rows}, ensure_ascii=False, indent=2))
            return 0
        print(f"root: {installed_skills_dir()}")
        if not rows:
            print("(none)")
            return 0
        for row in rows:
            print(f"{row['name']}\t{row['path']}")
        return 0
    if args.skills_command == "install":
        target = install_skill(args.source, name=args.name, force=args.force)
        print(f"installed {target.name}: {target}")
        return 0
    return 2


def _failures(manifest_path: str) -> int:
    manifest = load_manifest(manifest_path)
    run_id = ""
    failures: list = []
    db_path = state_db_path(manifest)
    if db_path.exists():
        store = SessionStore(db_path)
        try:
            runs = store.batch_runs(manifest.path, limit=1)
            if runs:
                run_id = str(runs[0]["run_id"])
                failures = [row for row in store.run_tasks(run_id) if row["status"] == "failed"]
        finally:
            store.close()
    if not run_id:
        failures = tasks_by_status(manifest_path, {"failed"})
    if not failures:
        print("no failed tasks")
        return 0
    try:
        from rich.console import Console
        from rich.table import Table

        table = Table(title="Failed Tasks")
        table.add_column("Task")
        table.add_column("Attempts", justify="right")
        table.add_column("Error")
        for task in failures:
            if isinstance(task, dict):
                table.add_row(
                    console_safe(task["task_id"]),
                    str(task["attempt_count"]),
                    console_safe(truncate(task.get("error") or "", 100)),
                )
            else:
                table.add_row(console_safe(task.id), str(task.attempts), console_safe(truncate(task.error, 100)))
        Console().print(table)
    except ImportError:
        for task in failures:
            if isinstance(task, dict):
                print(f"{task['task_id']}\tattempts={task['attempt_count']}\t{task.get('error') or ''}")
            else:
                print(f"{task.id}\tattempts={task.attempts}\t{task.error}")
    print("")
    print("Commands:")
    if run_id:
        print(f"  Retry:     {CLI_NAME} retry {manifest_path} <task_id> --run-id {run_id}")
        print(f"  Resume:    {CLI_NAME} resume {manifest_path} {run_id} --retry-failed")
    else:
        print(f"  Legacy retry cache: {CLI_NAME} retry {manifest_path}")
    print(f"  Inspect:   {CLI_NAME} inspect {manifest_path} <task_id>")
    print(f"  Rerun:     {CLI_NAME} rerun {manifest_path} <task_id>")
    return 0


def _inspect(args) -> int:
    manifest = load_manifest(args.manifest)
    db_path = state_db_path(manifest)
    if not db_path.exists():
        print("error: no persisted runs", file=sys.stderr)
        return 2
    store = SessionStore(db_path)
    try:
        batch_runs = store.batch_runs(manifest.path)
        if args.run_id:
            run = store.batch_run(args.run_id)
            if run is None:
                print(f"error: run not found: {args.run_id}", file=sys.stderr)
                return 2
            expected_path = str(manifest.path.resolve())
            if str(run["manifest_path"]) != expected_path:
                print(
                    f"error: run {args.run_id} belongs to another batch config: {run['manifest_path']}",
                    file=sys.stderr,
                )
                return 2
            selected_run_id = args.run_id
        else:
            selected_run_id = next(
                (
                    str(row["run_id"])
                    for row in batch_runs
                    if store.run_task(str(row["run_id"]), args.task_id) is not None
                ),
                "",
            )
        if not selected_run_id:
            print(f"error: no persisted Run contains task: {args.task_id}", file=sys.stderr)
            return 2
        run = store.batch_run(selected_run_id)
        run_task = store.run_task(selected_run_id, args.task_id)
        if run_task is None:
            print(f"error: task not found in Run {selected_run_id}: {args.task_id}", file=sys.stderr)
            return 2
        attempts = store.task_attempts(selected_run_id, args.task_id)
        selected_attempt = (
            next((row for row in attempts if row["attempt_id"] == args.attempt_id), None)
            if args.attempt_id
            else (attempts[0] if attempts else None)
        )
        if args.attempt_id and selected_attempt is None:
            print(f"error: attempt not found in Run {selected_run_id}: {args.attempt_id}", file=sys.stderr)
            return 2
        payload = {
            "run": run,
            "task": run_task,
            "attempts": attempts,
            "selected_attempt": selected_attempt,
            "artifacts": store.run_artifacts(selected_attempt["attempt_id"]) if selected_attempt else [],
            "tool_events": store.run_tool_events(selected_attempt["attempt_id"], limit=args.tools) if selected_attempt else [],
            "messages": store.run_messages(selected_attempt["attempt_id"], limit=args.messages) if selected_attempt else [],
            "model_calls": store.model_calls(selected_attempt["attempt_id"]) if selected_attempt else [],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    finally:
        store.close()
    return 0


def _render_inspect(task, runs, run_id: str, store: SessionStore, messages_limit: int, tools_limit: int) -> None:
    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table
    except ImportError:
        _render_inspect_plain(task, runs, run_id, store, messages_limit, tools_limit)
        return

    console = Console()
    console.print(
        Panel(
            "\n".join(
                [
                    f"id: {task.id}",
                    f"status: {task.status}",
                    f"kind: {task.kind}",
                    f"attempts: {task.attempts}",
                    f"result: {console_safe(task.result)}",
                    f"error: {console_safe(task.error)}",
                    f"input: {console_safe(json.dumps(task.input, ensure_ascii=False))}",
                ]
            ),
            title="Task",
        )
    )

    table = Table(title="Runs")
    table.add_column("Work ID")
    table.add_column("Run ID")
    table.add_column("Attempt", justify="right")
    table.add_column("Status")
    table.add_column("Started")
    table.add_column("Finished")
    table.add_column("Error")
    for run in runs:
        table.add_row(
            run.get("work_id", ""),
            run["run_id"],
            str(run["attempt"]),
            run["status"],
            run["started_at"],
            run["finished_at"] or "",
            console_safe(truncate(run["error"], 80)),
        )
    console.print(table)
    if not run_id:
        return

    tool_events = store.run_tool_events(run_id, tools_limit)
    tool_table = Table(title=f"Tool Events: {run_id}")
    tool_table.add_column("Seq", justify="right")
    tool_table.add_column("Tool")
    tool_table.add_column("Error")
    tool_table.add_column("Arguments")
    for event in tool_events:
        tool_table.add_row(
            str(event["seq"]),
            console_safe(event["tool_name"]),
            console_safe(truncate(event["error"], 80)),
            console_safe(truncate(json.dumps(event["arguments"], ensure_ascii=False), 120)),
        )
    console.print(tool_table)

    artifacts = store.run_artifacts(run_id)
    if artifacts:
        console.print(Panel(console_safe(json.dumps(artifacts[0], ensure_ascii=False, indent=2)), title="Latest Artifact"))

    messages = store.run_messages(run_id, messages_limit)
    for message in messages:
        content = console_safe(truncate(message["content"], 2000))
        console.print(Panel(content, title=f"{message['seq']} {message['role']} {message['created_at']}"))


def _render_inspect_plain(task, runs, run_id: str, store: SessionStore, messages_limit: int, tools_limit: int) -> None:
    print(json.dumps(task.__dict__, ensure_ascii=False, indent=2))
    print(json.dumps(runs, ensure_ascii=False, indent=2))
    if run_id:
        print(json.dumps(store.run_tool_events(run_id, tools_limit), ensure_ascii=False, indent=2))
        print(json.dumps(store.run_artifacts(run_id), ensure_ascii=False, indent=2))
        print(json.dumps(store.run_messages(run_id, messages_limit), ensure_ascii=False, indent=2))
