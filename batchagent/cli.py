from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .manifest import ManifestError, create_sample_manifest, load_manifest
from .provider import create_provider
from .progress import PlainProgress, ProgressState, RichRunDisplay
from .scheduler import (
    mark_tasks_for_retry,
    recover_running,
    rerun_tasks,
    run_manifest,
    state_db_path,
    status,
    tasks_by_status,
    validate_manifest,
)
from .store import SessionStore
from .util import console_safe, truncate


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        from .tui import run_tui

        return run_tui()

    parser = argparse.ArgumentParser(prog="batchagent", description="Markdown-driven batch agent harness.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    tui_parser = subparsers.add_parser("tui", help="Start the full-screen interactive TUI.")
    tui_parser.add_argument("manifest", nargs="?", default=None)

    init_parser = subparsers.add_parser("init", help="Create a sample manifest.")
    init_parser.add_argument("manifest", nargs="?", default="BATCHAGENT.md")

    doctor_parser = subparsers.add_parser("doctor", help="Validate a manifest without running tasks.")
    doctor_parser.add_argument("manifest")

    status_parser = subparsers.add_parser("status", help="Print task status counts.")
    status_parser.add_argument("manifest")

    run_parser = subparsers.add_parser("run", help="Start a batch work from a manifest.")
    run_parser.add_argument("manifest")
    run_parser.add_argument("--limit", type=int, default=None, help="Maximum task rows to select for this Batch Work.")
    run_parser.add_argument("--retry-failed", action="store_true", help="Include failed tasks in this Batch Work selection.")
    run_parser.add_argument("--only", action="append", default=None, help="Select only this task id for the Batch Work. Can be repeated.")
    run_parser.add_argument("--plain", action="store_true", help="Disable Rich live dashboard and print plain progress events.")
    run_parser.add_argument("--no-progress", action="store_true", help="Disable progress output during execution.")
    run_parser.add_argument("--focus", default="", help="Highlight one task in the live dashboard.")
    run_parser.add_argument("--var", action="append", default=None, help="Runtime template variable as name=value. Can be repeated.")

    recover_parser = subparsers.add_parser("recover", help="Move stale running tasks to retry or failed.")
    recover_parser.add_argument("manifest")
    recover_parser.add_argument("--to", choices=["retry", "failed", "todo"], default="retry")

    failures_parser = subparsers.add_parser("failures", help="List failed tasks and suggested recovery commands.")
    failures_parser.add_argument("manifest")

    retry_parser = subparsers.add_parser("retry", help="Mark failed or selected tasks as retry.")
    retry_parser.add_argument("manifest")
    retry_parser.add_argument("task_ids", nargs="*", help="Task ids to retry. Defaults to all failed tasks.")
    retry_parser.add_argument("--reset-attempts", action="store_true", help="Reset attempt counters before retrying.")

    rerun_parser = subparsers.add_parser("rerun", help="Reset selected tasks to todo and clear prior result/error.")
    rerun_parser.add_argument("manifest")
    rerun_parser.add_argument("task_ids", nargs="+")

    inspect_parser = subparsers.add_parser("inspect", help="Inspect one task's manifest row and persisted run history.")
    inspect_parser.add_argument("manifest")
    inspect_parser.add_argument("task_id")
    inspect_parser.add_argument("--run-id", default="", help="Specific run id to inspect. Defaults to latest run for the task.")
    inspect_parser.add_argument("--messages", type=int, default=12, help="Recent messages to show.")
    inspect_parser.add_argument("--tools", type=int, default=20, help="Recent tool events to show.")

    models_parser = subparsers.add_parser("models", help="List models through the configured provider.")
    models_parser.add_argument("manifest", nargs="?", default=None)
    models_parser.add_argument("--base-url", default=None)
    models_parser.add_argument("--api-key-env", default=None)

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
            results = asyncio.run(_run_with_progress(args))
            if getattr(args, "_used_rich_progress", False):
                _print_rich_completion(args.manifest, results)
            else:
                _print_run_results(args.manifest, results)
            return 0 if all(result.success for result in results) else 1
        if args.command == "recover":
            changed = recover_running(args.manifest, args.to)
            print(f"recovered {changed} running task(s) to {args.to}")
            return 0
        if args.command == "failures":
            return _failures(args.manifest)
        if args.command == "retry":
            task_ids = set(args.task_ids) if args.task_ids else None
            changed = mark_tasks_for_retry(args.manifest, task_ids, reset_attempts=args.reset_attempts)
            print(f"marked {changed} task(s) as retry")
            print(f"next: python -m batchagent run {args.manifest}")
            return 0
        if args.command == "rerun":
            changed = rerun_tasks(args.manifest, set(args.task_ids))
            print(f"reset {changed} task(s) to todo")
            print(f"next: python -m batchagent run {args.manifest} --only {args.task_ids[0]}" if len(args.task_ids) == 1 else f"next: python -m batchagent run {args.manifest}")
            return 0
        if args.command == "inspect":
            return _inspect(args)
        if args.command == "models":
            return _models(args)
    except (ManifestError, RuntimeError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


async def _run_with_progress(args) -> list:
    manifest = load_manifest(args.manifest)
    validate_manifest(manifest)
    task_ids = set(args.only) if args.only else None
    run_vars = _parse_run_vars(args.var or [])
    if args.no_progress:
        args._used_rich_progress = False
        return await run_manifest(args.manifest, limit=args.limit, retry_failed=args.retry_failed, task_ids=task_ids, run_vars=run_vars)

    state = ProgressState.from_manifest(manifest, focus_task_id=args.focus)
    if args.plain:
        args._used_rich_progress = False
        plain = PlainProgress(state)
        return await run_manifest(
            args.manifest,
            limit=args.limit,
            retry_failed=args.retry_failed,
            task_ids=task_ids,
            run_vars=run_vars,
            progress_callback=plain.callback,
        )

    display = RichRunDisplay(state)
    if not display.available:
        args._used_rich_progress = False
        plain = PlainProgress(state)
        return await run_manifest(
            args.manifest,
            limit=args.limit,
            retry_failed=args.retry_failed,
            task_ids=task_ids,
            run_vars=run_vars,
            progress_callback=plain.callback,
        )
    args._used_rich_progress = True
    return await display.run(
        run_manifest(
            args.manifest,
            limit=args.limit,
            retry_failed=args.retry_failed,
            task_ids=task_ids,
            run_vars=run_vars,
            progress_callback=state.handle_event,
        )
    )


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
        print(f"  Inspect: python -m batchagent inspect {manifest_path} {failed[0].task_id}")
        print(f"  Retry:   python -m batchagent retry {manifest_path} " + " ".join(result.task_id for result in failed))
        print(f"  Run:     python -m batchagent run {manifest_path} --retry-failed")
        return
    print(f"Batch work {work_id or '(unknown)'} completed: {len(results)} task(s) done.")


def _print_run_results(manifest_path: str, results) -> None:
    for result in results:
        state = "done" if result.success else "failed"
        print(f"{state}\t{result.work_id}\t{result.task_id}\t{result.run_dir}")
        if result.error:
            print(f"  error: {result.error}")
    failed = [result for result in results if not result.success]
    if failed:
        print("")
        print("Recovery options:")
        print(f"  Continue failed tasks: python -m batchagent retry {manifest_path} " + " ".join(result.task_id for result in failed))
        print(f"  Then run:              python -m batchagent run {manifest_path} --retry-failed")
        if len(failed) == 1:
            print(f"  Inspect:               python -m batchagent inspect {manifest_path} {failed[0].task_id}")
            print(f"  Full rerun:            python -m batchagent rerun {manifest_path} {failed[0].task_id}")


def _print_manifest_summary(manifest) -> None:
    counts: dict[str, int] = {}
    for task in manifest.tasks:
        counts[task.status] = counts.get(task.status, 0) + 1
    print(f"name: {manifest.config.name}")
    print(f"provider: {manifest.config.provider}")
    print(f"model: {manifest.config.model}")
    print(f"tools: {', '.join(manifest.config.tools) if manifest.config.tools else '(none)'}")
    print(f"workspace: {Path(manifest.config.workspace)} ({manifest.config.workspace_mode})")
    print(f"parallel: {manifest.config.parallel}, max_concurrency: {manifest.config.effective_concurrency}")
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


def _failures(manifest_path: str) -> int:
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
            table.add_row(console_safe(task.id), str(task.attempts), console_safe(truncate(task.error, 100)))
        Console().print(table)
    except ImportError:
        for task in failures:
            print(f"{task.id}\tattempts={task.attempts}\t{task.error}")
    print("")
    print("Commands:")
    print(f"  Retry all: python -m batchagent retry {manifest_path}")
    print(f"  Run retry: python -m batchagent run {manifest_path}")
    print(f"  Inspect:   python -m batchagent inspect {manifest_path} <task_id>")
    print(f"  Rerun:     python -m batchagent rerun {manifest_path} <task_id>")
    return 0


def _inspect(args) -> int:
    manifest = load_manifest(args.manifest)
    task = next((item for item in manifest.tasks if item.id == args.task_id), None)
    if task is None:
        print(f"error: task not found: {args.task_id}", file=sys.stderr)
        return 2
    db_path = state_db_path(manifest)
    store = SessionStore(db_path)
    try:
        runs = store.task_runs(args.task_id)
        selected_run = args.run_id or (runs[0]["run_id"] if runs else "")
        _render_inspect(task, runs, selected_run, store, args.messages, args.tools)
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
