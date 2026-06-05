from __future__ import annotations

import asyncio
import json
import shlex
import threading
from pathlib import Path
from typing import Any

from rich.panel import Panel
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, Input, RichLog, Static

from .manifest import ManifestError, load_manifest
from .models import Manifest, Task
from .progress import ProgressState, _format_seconds
from .scheduler import mark_tasks_for_retry, rerun_tasks, run_manifest, status, validate_manifest
from .util import console_safe, truncate


class BatchAgentTui(App[None]):
    CSS = """
    Screen {
        layout: vertical;
    }

    #body {
        height: 1fr;
    }

    #main {
        width: 1fr;
        height: 100%;
    }

    #side {
        width: 34;
        min-width: 24;
        border-right: solid $surface;
        padding: 0 1;
    }

    #title {
        height: 5;
        border-bottom: solid $surface;
        padding: 0 1;
    }

    #table {
        height: 2fr;
    }

    #detail {
        height: 1fr;
        border-top: solid $surface;
    }

    #command {
        dock: bottom;
    }
    """

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+q", "quit", "Quit"),
        ("f1", "help", "Help"),
        ("escape", "show_batch", "Batch"),
    ]

    def __init__(self, start_manifest: str | None = None):
        super().__init__()
        self.start_manifest = start_manifest
        self.manifest_paths: list[Path] = []
        self.selected_manifest_path: Path | None = None
        self.selected_manifest: Manifest | None = None
        self.page = "home"
        self.focus_task_id = ""
        self.progress_state: ProgressState | None = None
        self.run_task: asyncio.Task | None = None
        self.run_results: list[Any] = []
        self.ui_thread = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="side"):
                yield Static("", id="side_title")
                yield DataTable(id="manifest_table")
            with Vertical(id="main"):
                yield Static("", id="title")
                yield DataTable(id="table")
                yield RichLog(id="detail", wrap=True, highlight=False)
        yield Input(placeholder="/help", id="command")
        yield Footer()

    async def on_mount(self) -> None:
        self.ui_thread = threading.get_ident()
        self.query_one("#command", Input).focus()
        self.discover_manifests()
        if self.start_manifest:
            self.load_manifest(Path(self.start_manifest))
            self.page = "batch"
        self.render_page()

    def discover_manifests(self) -> None:
        candidates: list[Path] = []
        for path in Path.cwd().rglob("BATCHAGENT.md"):
            if ".batchagent" in set(path.parts) or "__pycache__" in set(path.parts):
                continue
            candidates.append(path)
        self.manifest_paths = sorted(candidates, key=lambda item: str(item).lower())

    def render_page(self) -> None:
        self.render_sidebar()
        if self.page == "home":
            self.render_home()
        elif self.page == "batch":
            self.render_batch()
        elif self.page == "run":
            self.render_run()
        elif self.page == "task":
            self.render_task()

    def render_sidebar(self) -> None:
        self.query_one("#side_title", Static).update("BatchAgent\n\n/show_batch <#>\n/run <#|path>\n/show_task <id>\n/quit")
        table = self.query_one("#manifest_table", DataTable)
        table.clear(columns=True)
        table.add_columns("#", "Manifest", "State")
        for index, path in enumerate(self.manifest_paths, start=1):
            state = ""
            try:
                counts = status(path)
                state = " ".join(f"{key}:{value}" for key, value in sorted(counts.items()))
            except Exception:
                state = "invalid"
            table.add_row(str(index), path.name if len(str(path)) > 32 else str(path), state, key=str(index))

    def render_home(self) -> None:
        self.query_one("#title", Static).update(
            Panel(
                "Home\n\nUse /show_batch <number> to inspect a manifest, /run <number> to execute, /quit to exit.",
                title="BatchAgent",
            )
        )
        table = self.query_one("#table", DataTable)
        table.clear(columns=True)
        table.add_columns("#", "Path", "Summary")
        for index, path in enumerate(self.manifest_paths, start=1):
            try:
                counts = status(path)
                summary = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
            except Exception as exc:
                summary = f"invalid: {exc}"
            table.add_row(str(index), str(path), summary, key=str(index))
        self.set_detail(
            [
                "Commands:",
                "  /show_batch <number|path>",
                "  /run <number|path> [--only task-id] [--retry-failed]",
                "  /show_task <task-id>",
                "  /refresh",
                "  /quit",
            ]
        )

    def render_batch(self) -> None:
        manifest = self.require_manifest()
        counts = status(manifest.path)
        self.query_one("#title", Static).update(
            Panel(
                "\n".join(
                    [
                        f"Manifest: {manifest.path}",
                        f"Name: {manifest.config.name}",
                        f"Model: {manifest.config.provider}/{manifest.config.model}",
                        f"Concurrency: {manifest.config.effective_concurrency}",
                        "Status: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())),
                    ]
                ),
                title="Batch",
            )
        )
        table = self.query_one("#table", DataTable)
        table.clear(columns=True)
        table.add_columns("Status", "Task", "Kind", "Attempts", "Output/Error")
        for task in manifest.tasks:
            table.add_row(
                task.status,
                task.id,
                task.kind,
                str(task.attempts),
                truncate(task.error or task.result or json.dumps(task.input, ensure_ascii=False), 140),
                key=task.id,
            )
        self.set_detail(
            [
                "Batch commands:",
                "  /run                       run eligible tasks in this manifest",
                "  /run --only <task-id>       run one task",
                "  /show_task <task-id>        inspect task details",
                "  /retry <task-id|all>        mark failed task(s) retry",
                "  /rerun <task-id>            reset task to todo",
                "  /show_home                  manifest list",
            ]
        )

    def render_run(self) -> None:
        state = self.require_progress()
        counts = state.counts()
        eta = state.eta_seconds()
        done = counts.get("done", 0)
        failed = counts.get("failed", 0)
        running = counts.get("running", 0)
        queued = counts.get("queued", 0)
        self.query_one("#title", Static).update(
            Panel(
                "\n".join(
                    [
                        f"Run: {state.manifest.path}",
                        f"loaded={state.total_tasks} eligible={state.eligible_tasks} running={running} queued={queued} done={done} failed={failed}",
                        f"elapsed={_format_seconds(state.elapsed())} eta={'unknown' if eta is None else _format_seconds(eta)}",
                    ]
                ),
                title="Running Batch",
            )
        )
        table = self.query_one("#table", DataTable)
        table.clear(columns=True)
        table.add_columns("Status", "Task", "Attempts", "Run Time", "Detail")
        now = __import__("time").monotonic()
        for task in state.ordered_tasks():
            detail = task.detail or task.error or task.artifact_path or task.result or task.run_dir
            table.add_row(
                task.status,
                task.id,
                str(task.attempts),
                _format_seconds(task.elapsed_current(now)),
                truncate(detail, 160),
                key=task.id,
            )
        self.render_focused_task_detail()

    def render_task(self) -> None:
        self.render_run() if self.progress_state else self.render_batch()
        self.render_focused_task_detail(full=True)

    def render_focused_task_detail(self, full: bool = False) -> None:
        detail = self.query_one("#detail", RichLog)
        detail.clear()
        task = self.current_task_progress()
        if task is not None:
            detail.write(Panel(self.task_progress_text(task, full=full), title=f"Task {task.id}"))
            return
        manifest = self.selected_manifest
        if manifest and self.focus_task_id:
            row = next((item for item in manifest.tasks if item.id == self.focus_task_id), None)
            if row:
                detail.write(Panel(self.task_row_text(row), title=f"Task {row.id}"))
                return
        detail.write("No focused task. Use /show_task <task-id>.")

    def set_detail(self, lines: list[str]) -> None:
        detail = self.query_one("#detail", RichLog)
        detail.clear()
        for line in lines:
            detail.write(line)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        command = event.value.strip()
        event.input.value = ""
        if not command:
            return
        await self.handle_command(command)

    async def handle_command(self, command: str) -> None:
        if not command.startswith("/"):
            if command.isdigit():
                self.load_manifest_by_token(command)
                self.page = "batch"
                self.render_page()
                return
            self.notify("Commands start with /. Use /help.", severity="warning")
            return
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return
        name = parts[0].lower()
        args = parts[1:]
        try:
            if name in {"/quit", "/exit"}:
                self.exit()
            elif name == "/help":
                self.page = "home"
                self.render_home()
            elif name == "/refresh":
                self.refresh_current()
            elif name == "/show_home":
                self.page = "home"
                self.render_page()
            elif name == "/show_batch":
                self.command_show_batch(args)
            elif name == "/show_task":
                self.command_show_task(args)
            elif name == "/run":
                await self.command_run(args)
            elif name == "/retry":
                self.command_retry(args)
            elif name == "/rerun":
                self.command_rerun(args)
            else:
                self.notify(f"Unknown command: {name}", severity="error")
        except Exception as exc:
            self.notify(str(exc), severity="error")

    def command_show_batch(self, args: list[str]) -> None:
        token = args[0] if args else None
        if token:
            self.load_manifest_by_token(token)
        elif self.selected_manifest is None:
            raise RuntimeError("No manifest selected.")
        self.page = "batch"
        self.render_page()

    def command_show_task(self, args: list[str]) -> None:
        if not args:
            raise RuntimeError("Usage: /show_task <task-id>")
        self.focus_task_id = args[0]
        self.page = "task"
        self.render_page()

    async def command_run(self, args: list[str]) -> None:
        if self.run_task and not self.run_task.done():
            raise RuntimeError("A batch is already running in this TUI.")
        token, only, retry_failed = self.parse_run_args(args)
        if token:
            self.load_manifest_by_token(token)
        manifest = self.require_manifest()
        validate_manifest(manifest)
        state = ProgressState.from_manifest(manifest, focus_task_id=only or self.focus_task_id)
        self.progress_state = state
        self.focus_task_id = state.focus_task_id
        self.page = "run"
        self.render_page()
        task_ids = {only} if only else None
        self.run_task = asyncio.create_task(
            run_manifest(
                manifest.path,
                retry_failed=retry_failed,
                task_ids=task_ids,
                progress_callback=self.progress_callback,
            )
        )
        self.run_task.add_done_callback(self.finish_run)

    def command_retry(self, args: list[str]) -> None:
        manifest = self.require_manifest()
        if not args or args[0] == "all":
            changed = mark_tasks_for_retry(manifest.path)
        else:
            changed = mark_tasks_for_retry(manifest.path, set(args))
        self.load_manifest(manifest.path)
        self.notify(f"marked {changed} task(s) retry")
        self.page = "batch"
        self.render_page()

    def command_rerun(self, args: list[str]) -> None:
        if not args:
            raise RuntimeError("Usage: /rerun <task-id>")
        manifest = self.require_manifest()
        changed = rerun_tasks(manifest.path, set(args))
        self.load_manifest(manifest.path)
        self.notify(f"reset {changed} task(s)")
        self.page = "batch"
        self.render_page()

    def parse_run_args(self, args: list[str]) -> tuple[str | None, str | None, bool]:
        token: str | None = None
        only: str | None = None
        retry_failed = False
        index = 0
        while index < len(args):
            arg = args[index]
            if arg == "--only":
                index += 1
                if index >= len(args):
                    raise RuntimeError("--only requires a task id")
                only = args[index]
            elif arg == "--retry-failed":
                retry_failed = True
            elif token is None:
                token = arg
            else:
                raise RuntimeError(f"unexpected argument: {arg}")
            index += 1
        return token, only, retry_failed

    def progress_callback(self, event: dict[str, Any]) -> None:
        if threading.get_ident() == self.ui_thread:
            self.handle_progress_event(event)
        else:
            self.call_from_thread(self.handle_progress_event, event)

    def handle_progress_event(self, event: dict[str, Any]) -> None:
        if self.progress_state is None:
            return
        self.progress_state.handle_event(event)
        task_id = str(event.get("task_id") or "")
        if task_id and not self.focus_task_id:
            self.focus_task_id = task_id
            self.progress_state.focus_task_id = task_id
        self.render_page()

    def finish_run(self, task: asyncio.Task) -> None:
        try:
            self.run_results = task.result()
            failed = [result for result in self.run_results if not result.success]
            if failed:
                self.notify(f"batch finished with {len(failed)} failed task(s)", severity="warning")
            else:
                self.notify(f"batch completed: {len(self.run_results)} task(s)")
        except Exception as exc:
            self.notify(f"batch failed: {exc}", severity="error")
        self.refresh_current()

    def refresh_current(self) -> None:
        if self.selected_manifest_path:
            self.load_manifest(self.selected_manifest_path)
        self.discover_manifests()
        self.render_page()

    def load_manifest_by_token(self, token: str) -> None:
        path = self.resolve_manifest_token(token)
        self.load_manifest(path)

    def resolve_manifest_token(self, token: str) -> Path:
        if token.isdigit():
            index = int(token)
            if 1 <= index <= len(self.manifest_paths):
                return self.manifest_paths[index - 1]
            raise RuntimeError(f"manifest index out of range: {token}")
        return Path(token)

    def load_manifest(self, path: Path) -> None:
        manifest = load_manifest(path)
        validate_manifest(manifest)
        self.selected_manifest = manifest
        self.selected_manifest_path = manifest.path
        if not self.focus_task_id and manifest.tasks:
            self.focus_task_id = manifest.tasks[0].id

    def require_manifest(self) -> Manifest:
        if self.selected_manifest is None:
            raise RuntimeError("No manifest selected. Use /show_batch <number|path>.")
        return self.selected_manifest

    def require_progress(self) -> ProgressState:
        if self.progress_state is None:
            manifest = self.require_manifest()
            self.progress_state = ProgressState.from_manifest(manifest, focus_task_id=self.focus_task_id)
        return self.progress_state

    def current_task_progress(self):
        if self.progress_state is None or not self.focus_task_id:
            return None
        return self.progress_state.tasks.get(self.focus_task_id)

    def task_progress_text(self, task, full: bool = False) -> str:
        base = [
            f"status: {task.status}",
            f"attempts: {task.attempts}",
            f"run_dir: {task.run_dir}",
            f"artifact: {task.artifact_path}",
            f"result: {task.result}",
            f"error: {task.error}",
            f"detail: {task.detail}",
        ]
        if full:
            base.extend(["", "events:", *task.events[-20:], "", "model output:", task.stream_text[-6000:]])
        return console_safe("\n".join(base))

    def task_row_text(self, task: Task) -> str:
        return console_safe(
            "\n".join(
                [
                    f"status: {task.status}",
                    f"kind: {task.kind}",
                    f"attempts: {task.attempts}",
                    f"result: {task.result}",
                    f"error: {task.error}",
                    f"input: {json.dumps(task.input, ensure_ascii=False, indent=2)}",
                ]
            )
        )


def run_tui(start_manifest: str | None = None) -> int:
    try:
        BatchAgentTui(start_manifest=start_manifest).run()
        return 0
    except (ManifestError, RuntimeError) as exc:
        print(f"error: {exc}")
        return 2
