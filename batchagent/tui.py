from __future__ import annotations

import asyncio
import json
import shlex
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

from rich.panel import Panel
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, RichLog, Static

from .harness import available_harnesses, probe_harness
from .manifest import ManifestError, load_manifest
from .models import Manifest, RunVariable, Task
from .progress import ProgressState, _format_seconds
from .scheduler import (
    new_run_id,
    request_pause,
    resume_manifest,
    run_manifest,
    state_db_path,
    status,
    validate_manifest,
)
from .settings import DEFAULT_SETTINGS, SettingsError, load_settings, save_settings
from .store import SessionStore
from .util import console_safe, truncate


COMMAND_SPECS = [
    ("/show_batch", "/show_batch <manifest-path>", "Select a Batch Config and list its persisted Runs."),
    ("/run", "/run [manifest-path] [--only task-id] [--harness name]", "Create a new Run from the selected Batch Config."),
    ("/show_run", "/show_run <run-id>", "Open one Run and list its Tasks."),
    ("/resume", "/resume <run-id>", "Resume queued or interrupted Tasks in an incomplete Run."),
    ("/pause", "/pause [run-id]", "Safely pause an active Run after current Attempts finish."),
    ("/show_task", "/show_task <task-id>", "Show Attempts and durable output for a Task in the selected Run."),
    ("/history", "/history [run-id]", "Alias for the current Batch's Run list or one Run."),
    ("/retry", "/retry <task-id>", "Execute a new Attempt for a failed Task in the selected Run."),
    ("/rerun", "/rerun <task-id>", "Create a new Run containing the selected Task."),
    ("/harness", "/harness [use|reset|doctor] [name]", "Show or persist the local task harness."),
    ("/theme", "/theme [name]", "Show or persist the Textual color theme."),
    ("/refresh", "/refresh", "Reload manifests and current status."),
    ("/show_home", "/show_home", "Return to the manifest list page."),
    ("/help", "/help", "Show the home/help page."),
    ("/quit", "/quit", "Exit the TUI."),
    ("/exit", "/exit", "Exit the TUI."),
]
COMMAND_META = {name: (usage, description) for name, usage, description in COMMAND_SPECS}
COMMANDS = [name for name, _usage, _description in COMMAND_SPECS]


class CommandInput(Input):
    async def _on_key(self, event: events.Key) -> None:
        if event.key == "tab":
            event.stop()
            event.prevent_default()
            complete = getattr(self.app, "action_complete_command", None)
            if complete is not None:
                complete()
            return
        if event.key in {"up", "down"}:
            move = getattr(self.app, "action_move_candidate", None)
            if move is not None and move(-1 if event.key == "up" else 1):
                event.stop()
                event.prevent_default()
                return
        await super()._on_key(event)


class TaskDetailScreen(ModalScreen[None]):
    CSS = """
    TaskDetailScreen {
        align: center middle;
    }

    #task_modal {
        width: 92%;
        height: 88%;
        border: thick $primary;
        background: $surface;
    }

    #task_modal_title {
        height: 3;
        border-bottom: solid $surface;
        padding: 0 1;
    }

    #task_modal_body {
        height: 1fr;
        padding: 0 1;
    }
    """

    BINDINGS = [
        ("escape", "close", "Close"),
    ]

    def __init__(self, task_id: str, run_id: str = "", attempt_id: str = ""):
        super().__init__()
        self.task_id = task_id
        self.run_id = run_id
        self.attempt_id = attempt_id
        self._last_content = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="task_modal"):
            yield Static("", id="task_modal_title")
            yield RichLog(id="task_modal_body", wrap=True, highlight=False)

    def on_mount(self) -> None:
        self.refresh_content()
        self.set_interval(0.5, self.refresh_content)

    def refresh_content(self) -> None:
        title = self.query_one("#task_modal_title", Static)
        body = self.query_one("#task_modal_body", RichLog)
        context = f" in {self.run_id}" if self.run_id else ""
        title.update(f"Task Detail: {self.task_id}{context}\nEsc closes this window.")
        detail_text = getattr(self.app, "task_detail_text_for")(self.task_id, self.run_id, self.attempt_id)
        if detail_text == self._last_content:
            return
        self._last_content = detail_text
        scroll_y = body.scroll_y
        was_at_end = body.is_vertical_scroll_end
        body.clear()
        body.write(Panel(detail_text, title=self.task_id), scroll_end=was_at_end)
        if not was_at_end:
            body.scroll_to(y=scroll_y, animate=False, force=True)

    def action_close(self) -> None:
        self.dismiss()


class RunVariablesScreen(ModalScreen[dict[str, str] | None]):
    CSS = """
    RunVariablesScreen {
        align: center middle;
    }

    #vars_modal {
        width: 78%;
        height: 54%;
        border: thick $primary;
        background: $surface;
    }

    #vars_title {
        height: 5;
        border-bottom: solid $surface;
        padding: 0 1;
    }

    #vars_help {
        height: 1fr;
        padding: 0 1;
    }

    #vars_input {
        height: 3;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, variables: list[RunVariable], initial: dict[str, Any] | None = None):
        super().__init__()
        self.variables = variables
        self.values = {key: str(value) for key, value in (initial or {}).items()}
        self.index = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="vars_modal"):
            yield Static("", id="vars_title")
            yield RichLog(id="vars_help", wrap=True, highlight=False)
            yield Input(id="vars_input")

    def on_mount(self) -> None:
        self.render_variable()
        self.query_one("#vars_input", Input).focus()

    def render_variable(self) -> None:
        variable = self.variables[self.index]
        title = self.query_one("#vars_title", Static)
        detail = self.query_one("#vars_help", RichLog)
        input_widget = self.query_one("#vars_input", Input)
        default_value = self.resolve_variable_default(self.values.get(variable.name, variable.default))
        title.update(
            "\n".join(
                [
                    "Runtime Variables",
                    f"{self.index + 1}/{len(self.variables)} {variable.name}",
                    "Enter saves this value. Esc cancels this Run.",
                ]
            )
        )
        detail.clear()
        detail.write(variable.label or variable.name)
        detail.write(f"required: {variable.required}")
        if variable.default:
            detail.write(f"default: {variable.default}")
        input_widget.placeholder = variable.name
        input_widget.value = default_value
        input_widget.cursor_position = len(input_widget.value)

    def resolve_variable_default(self, value: str) -> str:
        return str(value).replace("CURR_DATE", date.today().isoformat())

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        variable = self.variables[self.index]
        value = event.value
        if variable.required and not value.strip():
            self.notify(f"{variable.name} is required", severity="error")
            return
        self.values[variable.name] = value
        self.index += 1
        if self.index >= len(self.variables):
            self.dismiss(self.values)
            return
        self.render_variable()

    def action_cancel(self) -> None:
        self.dismiss(None)


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
        width: 40;
        min-width: 30;
        border-right: solid $surface;
        padding: 0 1;
    }

    #side_title {
        height: 3;
    }

    #selection {
        height: 7;
        border-bottom: solid $surface;
        padding: 0 1;
    }

    #title {
        height: 6;
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

    #command_palette {
        display: none;
        max-height: 9;
        border-top: solid $surface;
        padding: 0 1;
    }

    #command {
        height: 3;
    }
    """

    BINDINGS = [
        ("ctrl+c", "copy", "Copy"),
        ("ctrl+q", "quit", "Quit"),
        ("f1", "help", "Help"),
        ("ctrl+t", "change_theme", "Theme"),
        ("escape", "show_batch", "Batch"),
    ]

    def __init__(self, start_manifest: str | None = None, auto_run_args: list[str] | None = None):
        try:
            self.settings = load_settings()
            self.settings_error = ""
        except SettingsError as exc:
            self.settings = dict(DEFAULT_SETTINGS)
            self.settings_error = str(exc)
        self._settings_ready = False
        super().__init__()
        configured_theme = str(self.settings.get("theme") or DEFAULT_SETTINGS["theme"])
        if configured_theme in self.available_themes:
            self.theme = configured_theme
        self._settings_ready = True
        self.start_manifest = start_manifest
        self.auto_run_args = auto_run_args
        self.manifest_paths: list[Path] = []
        self.selected_manifest_path: Path | None = None
        self.selected_manifest: Manifest | None = None
        self.page = "home"
        self.focus_task_id = ""
        self.progress_state: ProgressState | None = None
        self.run_task: asyncio.Task | None = None
        self.pause_event: asyncio.Event | None = None
        self.pause_requested = False
        self.exit_after_pause = False
        self.run_results: list[Any] = []
        self.current_work_id = ""
        self.selected_run_id = ""
        self.active_run_id = ""
        self.active_manifest_path: Path | None = None
        self.harness_name = str(self.settings.get("harness") or "native")
        self.ui_thread = 0
        self.history_task_id = ""
        self._completion_value = ""
        self._completion_candidates: list[str] = []
        self._completion_index = 0
        self._completion_input = ""
        self._detail_content_key = ""
        self._run_table_keys: list[str] = []
        self._run_render_dirty = False
        self._auto_run_started = False

    def _watch_theme(self, theme_name: str) -> None:
        super()._watch_theme(theme_name)
        if getattr(self, "_settings_ready", False):
            try:
                self._save_preferences({"theme": theme_name})
            except SettingsError:
                pass

    def _save_preferences(self, changes: dict[str, Any]) -> None:
        values = dict(self.settings)
        values.update(changes)
        save_settings(values)
        self.settings = values

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="side"):
                yield Static("", id="side_title")
                yield Static("", id="selection")
                yield DataTable(id="manifest_table")
            with Vertical(id="main"):
                yield Static("", id="title")
                yield DataTable(id="table")
                yield RichLog(id="detail", wrap=True, highlight=False)
        yield Static("", id="command_palette")
        yield CommandInput(placeholder="Type / for commands. Tab completes.", id="command")
        yield Footer()

    async def on_mount(self) -> None:
        self.ui_thread = threading.get_ident()
        self.query_one("#command", Input).focus()
        self.query_one("#manifest_table", DataTable).cursor_type = "row"
        self.query_one("#table", DataTable).cursor_type = "row"
        self.discover_manifests()
        if self.start_manifest:
            self.load_manifest(Path(self.start_manifest))
            self.page = "batch"
        self.render_page()
        if self.settings_error:
            self.notify(f"settings fallback: {self.settings_error}", severity="warning")
        self.set_interval(0.5, self.flush_run_render)
        if self.auto_run_args is not None and not self._auto_run_started:
            self._auto_run_started = True
            await self.command_run(list(self.auto_run_args))

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
        elif self.page == "history":
            self.render_history()

    def action_copy(self) -> None:
        focused = self.focused
        copy_action = getattr(focused, "action_copy", None)
        if copy_action is not None:
            copy_action()
            return
        self.notify("No focused text input to copy from.", severity="warning")

    def render_sidebar(self) -> None:
        self.query_one("#side_title", Static).update("Batches\nDiscovered manifests")
        self.query_one("#selection", Static).update(self.selection_text())
        table = self.query_one("#manifest_table", DataTable)
        table.clear(columns=True)
        table.add_columns("Sel", "#", "Batch", "State")
        for index, path in enumerate(self.manifest_paths, start=1):
            state = ""
            label = self.manifest_label(path)
            try:
                counts = status(path)
                state = " ".join(f"{key}:{value}" for key, value in sorted(counts.items()))
            except Exception:
                state = "invalid"
            selected = "*" if self.selected_manifest_path and path.resolve() == self.selected_manifest_path.resolve() else ""
            table.add_row(selected, str(index), label, state, key=f"manifest:{self.display_manifest_path(path)}")

    def selection_text(self) -> str:
        if self.selected_manifest is None:
            return "Current Batch\n\nNone selected\nUse /show_batch or /run."
        return console_safe(
            "\n".join(
                [
                    "Current Batch",
                    self.selected_manifest.config.name,
                    truncate(self.display_manifest_path(self.selected_manifest.path), 64),
                    f"selected run: {self.selected_run_id or '(none)'}",
                    f"active run: {self.active_run_id or '(none)'}",
                    f"page: {self.page}",
                ]
            )
        )

    def manifest_label(self, path: Path) -> str:
        try:
            manifest = load_manifest(path)
            return truncate(manifest.config.name or path.parent.name or path.name, 28)
        except Exception:
            return truncate(path.parent.name or path.name, 28)

    def selected_batch_line(self) -> str:
        if self.selected_manifest is None:
            return "Selected batch: none"
        return f"Selected batch: {self.selected_manifest.config.name} ({self.display_manifest_path(self.selected_manifest.path)})"

    def render_home(self) -> None:
        self.query_one("#title", Static).update(
            Panel(
                "\n".join(
                    [
                        "Home",
                        self.selected_batch_line(),
                        "Type / for commands; Tab completes the current token.",
                    ]
                ),
                title="BatchAgent",
            )
        )
        table = self.query_one("#table", DataTable)
        table.clear(columns=True)
        table.add_columns("Path", "Summary")
        for index, path in enumerate(self.manifest_paths, start=1):
            try:
                counts = status(path)
                summary = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
            except Exception as exc:
                summary = f"invalid: {exc}"
            table.add_row(self.display_manifest_path(path), summary, key=f"manifest:{self.display_manifest_path(path)}")
        self.set_detail(
            [
                "Screen areas:",
                "  Left sidebar: discovered batch manifests and the current selected batch.",
                "  Top panel: current page and selected batch context.",
                "  Center table: the primary list for the current page.",
                "  Lower detail: focused task, history, or page-specific detail.",
                "  Bottom candidate area: command suggestions while typing.",
                "  Bottom input: enter commands; type / to show all commands.",
                "",
                "Tab completes the current command, batch token, option, or task id.",
            ]
        )

    def render_batch(self) -> None:
        manifest = self.require_manifest()
        rows = self.batch_run_rows()
        self.query_one("#title", Static).update(
            Panel(
                "\n".join(
                    [
                        self.selected_batch_line(),
                        f"Manifest: {manifest.path}",
                        f"Name: {manifest.config.name}",
                        f"Runs: {len(rows)}",
                        f"Default harness: {self.harness_name}",
                        "Select a Run to inspect its Tasks, or use /run to create one.",
                    ]
                ),
                title="Batch Config → Runs",
            )
        )
        table = self.query_one("#table", DataTable)
        table.clear(columns=True)
        table.add_columns("Run ID", "Status", "Harness", "Tasks", "Elapsed", "Tokens", "Started", "Result / Error")
        for row in rows:
            result = row.get("error") or json.dumps(row.get("result") or {}, ensure_ascii=False)
            table.add_row(
                str(row["run_id"]),
                str(row["status"]),
                str(row.get("harness") or "native"),
                str(len(row.get("selected_task_ids") or [])),
                _format_seconds(float(row.get("elapsed_seconds") or 0)),
                str(row.get("total_tokens") if row.get("total_tokens") is not None else "-"),
                str(row.get("started_at") or ""),
                truncate(result, 120),
                key=f"run:{row['run_id']}",
            )
        self.set_detail(
            [
                "Hierarchy: Batch Config → Run → Task → Attempt",
                "Each /run creates an immutable run_id and freezes the selected Tasks, variables, and harness.",
                "Use /resume <run-id> for unfinished work. Use /retry <task-id> inside a selected Run.",
                "",
                "No Runs yet." if not rows else "Press Enter on a row or use /show_run <run-id>.",
            ]
        )

    def render_run(self) -> None:
        if not (self.run_task and not self.run_task.done() and self.selected_run_id == self.active_run_id):
            self.render_persisted_run()
            return
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
                        self.selected_batch_line(),
                        f"Run: {state.work_id or self.active_run_id or '(pending)'}",
                        f"Config: {state.manifest.path}",
                        f"loaded={state.total_tasks} selected={state.eligible_tasks} running={running} queued={queued} done={done} failed={failed}",
                        f"elapsed={_format_seconds(state.elapsed())} eta={'unknown' if eta is None else _format_seconds(eta)}",
                        state.pause_detail if state.paused else ("pause requested: waiting for running task(s) to finish" if self.pause_requested else ""),
                        "run dirs are unique per attempt; existing task results are not overwritten on disk",
                    ]
                ),
                title="Run → Tasks (live)",
            )
        )
        self.render_run_table(state)
        self.render_focused_task_detail()

    def render_persisted_run(self) -> None:
        manifest = self.require_manifest()
        run_id = self.selected_run_id
        if not run_id:
            self.page = "batch"
            self.render_batch()
            return
        store = self.open_store_if_present()
        if store is None:
            raise RuntimeError("No persisted state database yet.")
        try:
            run = store.batch_run(run_id)
            tasks = store.run_tasks(run_id)
        finally:
            store.close()
        if run is None or run["manifest_path"] != str(manifest.path.resolve()):
            raise RuntimeError(f"run not found for selected batch: {run_id}")
        self.query_one("#title", Static).update(
            Panel(
                "\n".join(
                    [
                        self.selected_batch_line(),
                        f"Run: {run_id}",
                        f"Status: {run['status']}  Harness: {run['harness']} {run.get('harness_version') or ''}",
                        f"Started: {run['started_at']}  Finished: {run.get('finished_at') or '-'}",
                        f"Elapsed: {_format_seconds(float(run.get('elapsed_seconds') or 0))}  Tokens: {run.get('total_tokens') if run.get('total_tokens') is not None else '-'}",
                        f"Tasks: {len(tasks)}  Variables: {json.dumps(run.get('run_vars') or {}, ensure_ascii=False)}",
                    ]
                ),
                title="Run → Tasks",
            )
        )
        table = self.query_one("#table", DataTable)
        table.clear(columns=True)
        table.add_columns("Status", "Task", "Kind", "Attempts", "Elapsed", "Tokens", "Result / Error")
        for row in tasks:
            result = row.get("error") or json.dumps(row.get("result") or {}, ensure_ascii=False)
            table.add_row(
                str(row["status"]),
                str(row["task_id"]),
                str(row.get("kind") or ""),
                str(row.get("attempt_count") or 0),
                _format_seconds(float(row.get("elapsed_seconds") or 0)),
                str(row.get("total_tokens") if row.get("total_tokens") is not None else "-"),
                truncate(result, 140),
                key=f"run-task:{run_id}:{row['task_id']}",
            )
        self.set_detail(
            [
                f"Run {run_id} has {len(tasks)} Task(s).",
                "Open a Task to see every immutable attempt_id, timing, usage, messages, tools, and artifacts.",
                "Resume preserves this run_id. Retry creates a new attempt_id for one failed Task.",
            ]
        )

    def batch_run_rows(self) -> list[dict[str, Any]]:
        manifest = self.require_manifest()
        store = self.open_store_if_present()
        if store is None:
            return []
        try:
            return store.batch_runs(manifest.path)
        finally:
            store.close()

    def open_store_if_present(self) -> SessionStore | None:
        manifest = self.require_manifest()
        db_path = state_db_path(manifest)
        if not db_path.exists():
            return None
        return SessionStore(db_path)

    def persisted_run_snapshot(self, run_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        manifest = self.require_manifest()
        store = self.open_store_if_present()
        if store is None:
            raise RuntimeError("No persisted state database yet.")
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

    def render_run_table(self, state: ProgressState) -> None:
        table = self.query_one("#table", DataTable)
        now = time.monotonic()
        rows: list[tuple[str, tuple[str, str, str, str, str, str]]] = []
        for task in state.ordered_tasks():
            detail = task.detail or task.error or task.artifact_path or task.result or task.run_dir
            rows.append(
                (
                    f"task:{task.id}",
                    (
                        task.status,
                        task.id,
                        str(task.attempts),
                        task.attempt_id or "-",
                        _format_seconds(task.elapsed_current(now)),
                        truncate(detail, 160),
                    ),
                )
            )
        row_keys = [key for key, _values in rows]
        if self._run_table_keys != row_keys:
            self.rebuild_run_table(table, rows)
            return
        column_keys = ("status", "task", "attempts", "attempt_id", "runtime", "detail")
        for row_key, values in rows:
            for column_key, value in zip(column_keys, values):
                try:
                    if table.get_cell(row_key, column_key) != value:
                        table.update_cell(row_key, column_key, value, update_width=False)
                except Exception:
                    self.rebuild_run_table(table, rows)
                    return

    def rebuild_run_table(self, table: DataTable, rows: list[tuple[str, tuple[str, str, str, str, str, str]]]) -> None:
        scroll_y = table.scroll_y
        table.clear(columns=True)
        table.add_columns(
            ("Status", "status"),
            ("Task", "task"),
            ("Attempts", "attempts"),
            ("Attempt ID", "attempt_id"),
            ("Run Time", "runtime"),
            ("Detail", "detail"),
        )
        for row_key, values in rows:
            table.add_row(*values, key=row_key)
        self._run_table_keys = [key for key, _values in rows]
        try:
            table.scroll_to(y=scroll_y, animate=False, force=True)
        except Exception:
            pass

    def render_history(self) -> None:
        if self.selected_run_id:
            self.render_persisted_run()
        else:
            self.render_batch()

    def render_focused_task_detail(self, full: bool = False) -> None:
        task = self.current_task_progress()
        if task is not None:
            text = self.task_progress_text(task, full=full)
            self.write_detail(Panel(text, title=f"Task {task.id}"), f"task-progress:{task.id}:{text}")
            return
        manifest = self.selected_manifest
        if manifest and self.focus_task_id:
            row = next((item for item in manifest.tasks if item.id == self.focus_task_id), None)
            if row:
                text = self.task_row_text(row)
                self.write_detail(Panel(text, title=f"Task {row.id}"), f"task-row:{row.id}:{text}")
                return
        self.write_detail("No focused task. Use /show_task <task-id>.", "no-focused-task")

    def set_detail(self, lines: list[str]) -> None:
        text = "\n".join(lines)
        self.write_detail(text, f"lines:{text}")

    def write_detail(self, content: Any, content_key: str) -> None:
        if content_key == self._detail_content_key:
            return
        detail = self.query_one("#detail", RichLog)
        scroll_y = detail.scroll_y
        was_at_end = detail.is_vertical_scroll_end
        detail.clear()
        detail.write(content, scroll_end=was_at_end)
        if not was_at_end:
            detail.scroll_to(y=scroll_y, animate=False, force=True)
        self._detail_content_key = content_key

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = self.row_key_value(event)
        if key.startswith("manifest:"):
            try:
                self.load_manifest_by_token(key.removeprefix("manifest:"))
                self.page = "batch"
                self.render_page()
            except Exception as exc:
                self.notify(str(exc), severity="error")
            return
        if key.startswith("run:"):
            try:
                self.selected_run_id = key.removeprefix("run:")
                self.page = "run"
                self.render_page()
            except Exception as exc:
                self.notify(str(exc), severity="error")
            return
        if key.startswith("run-task:"):
            try:
                _prefix, run_id, task_id = key.split(":", 2)
                self.open_task_detail(task_id, run_id=run_id)
            except Exception as exc:
                self.notify(str(exc), severity="error")
            return
        if key.startswith("task:"):
            try:
                self.open_task_detail(key.removeprefix("task:"), run_id=self.active_run_id or self.selected_run_id)
            except Exception as exc:
                self.notify(str(exc), severity="error")

    def row_key_value(self, event: DataTable.RowSelected) -> str:
        row_key = event.row_key
        return str(getattr(row_key, "value", row_key))

    def history_rows(self, task_id: str | None = None) -> list[dict[str, Any]]:
        manifest = self.require_manifest()
        db_path = state_db_path(manifest)
        if not db_path.exists():
            return []
        store = SessionStore(db_path)
        try:
            if self.selected_run_id:
                return store.task_attempts(self.selected_run_id, task_id)
            return store.batch_runs(manifest.path)
        finally:
            store.close()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        command = event.value.strip()
        event.input.value = ""
        self.reset_completion()
        self.render_command_palette("")
        if not command:
            return
        await self.handle_command(command)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "command":
            return
        if event.value != self._completion_value:
            self.reset_completion()
        self.render_command_palette(event.value)

    def action_complete_command(self) -> None:
        command_input = self.query_one("#command", Input)
        command_input.focus()
        current = command_input.value
        candidates = self.refresh_completion_candidates(current)
        if not candidates:
            self.notify("no completion", severity="warning")
            self.reset_completion()
            return
        base = self._completion_input or current
        next_value = self.apply_completion(base, candidates[self._completion_index])
        self._completion_value = next_value
        command_input.value = next_value
        command_input.cursor_position = len(next_value)
        self.render_command_palette(next_value)

    def action_move_candidate(self, offset: int) -> bool:
        command_input = self.query_one("#command", Input)
        candidates = self.refresh_completion_candidates(command_input.value)
        if not candidates:
            return False
        self._completion_index = (self._completion_index + offset) % len(candidates)
        self.render_command_palette(command_input.value)
        return True

    def refresh_completion_candidates(self, value: str, *, reset_index: bool = False) -> list[str]:
        candidates = self.completion_candidates(value)
        if value != self._completion_input or candidates != self._completion_candidates or reset_index:
            self._completion_input = value
            self._completion_candidates = candidates
            self._completion_index = 0
        if self._completion_candidates:
            self._completion_index %= len(self._completion_candidates)
        else:
            self._completion_index = 0
        return self._completion_candidates

    def render_command_palette(self, value: str) -> None:
        palette = self.query_one("#command_palette", Static)
        lines = self.command_palette_lines(value)
        if not lines:
            palette.update("")
            palette.display = False
            return
        palette.display = True
        palette.update(Panel(console_safe("\n".join(lines)), title="Command Candidates"))

    def command_palette_lines(self, value: str) -> list[str]:
        if not value.startswith("/"):
            return []
        candidates = self.refresh_completion_candidates(value)
        if not candidates:
            return ["No matching command, batch, option, or task."]
        lines: list[str] = []
        for index, candidate in enumerate(candidates[:8]):
            marker = ">" if index == self._completion_index else " "
            lines.append(f"{marker} {self.describe_candidate(value, candidate)}")
        if len(candidates) > 8:
            lines.append(f"  ... {len(candidates) - 8} more")
        return lines

    def describe_candidate(self, value: str, candidate: str) -> str:
        if candidate in COMMAND_META:
            usage, description = COMMAND_META[candidate]
            return f"{usage} - {description}"
        if candidate == "--only":
            return "--only <task-id> - Run exactly one task from the selected/current batch."
        if candidate == "--retry-failed":
            return "--retry-failed - Include failed Tasks when resuming; compatibility-only for a new Run."
        if candidate == "all":
            return "all - Apply to all relevant tasks or show all run history."
        if candidate in available_harnesses():
            return f"{candidate} - local task harness"
        if candidate in self.available_themes:
            return f"{candidate} - Textual color theme"
        if candidate in self.run_completion_tokens():
            return f"{candidate} - persisted Run"
        task = self.task_for_token(candidate)
        if task is not None:
            return f"{candidate} - task: status={task.status}, kind={task.kind or '-'}, attempts={task.attempts}"
        manifest = self.manifest_for_token(candidate)
        if manifest is not None:
            return f"{candidate} - batch: {manifest.config.name} ({self.display_manifest_path(manifest.path)})"
        return candidate

    def completion_candidates(self, text: str) -> list[str]:
        before, token = self.split_completion_token(text)
        command = self.command_name_for_completion(before, token)
        if command is None:
            return self.filter_candidates(COMMANDS, token)
        if command in {"/show_batch"}:
            return self.filter_candidates(self.manifest_completion_tokens(), token)
        if command in {"/show_run", "/resume", "/history"}:
            return self.filter_candidates(self.run_completion_tokens(), token)
        if command in {"/show_task", "/retry", "/rerun"}:
            candidates = self.task_completion_tokens()
            return self.filter_candidates(candidates, token)
        if command == "/run":
            return self.run_completion_candidates(before, token)
        if command == "/harness":
            tokens = before.strip().split()
            if tokens and tokens[-1] in {"use", "doctor"}:
                return self.filter_candidates(available_harnesses(), token)
            return self.filter_candidates(["use", "reset", "doctor", *available_harnesses()], token)
        if command == "/theme":
            return self.filter_candidates(sorted(self.available_themes), token)
        return []

    def run_completion_candidates(self, before: str, token: str) -> list[str]:
        tokens = before.strip().split()
        if tokens and tokens[-1] in {"--only", "--focus"}:
            return self.filter_candidates(self.task_completion_tokens(), token)
        if tokens and tokens[-1] == "--harness":
            return self.filter_candidates(available_harnesses(), token)
        if tokens and tokens[-1] in {"--var", "--limit"}:
            return []
        option_candidates = ["--only", "--retry-failed", "--limit", "--focus", "--var", "--harness"]
        consumed_manifest = self.run_command_has_manifest(tokens)
        if token.startswith("-"):
            return self.filter_candidates(option_candidates, token)
        candidates = option_candidates if consumed_manifest or self.selected_manifest else []
        if not consumed_manifest:
            candidates = [*self.manifest_completion_tokens(), *candidates]
        return self.filter_candidates(candidates, token)

    def run_command_has_manifest(self, tokens: list[str]) -> bool:
        index = 1
        while index < len(tokens):
            item = tokens[index]
            if item in {"--only", "--var", "--limit", "--focus", "--harness", "--resume"}:
                index += 2
                continue
            if item == "--retry-failed" or item.startswith("--var=") or item.startswith("--limit=") or item.startswith("--focus="):
                index += 1
                continue
            if not item.startswith("-"):
                return True
            index += 1
        return False

    def command_name_for_completion(self, before: str, token: str) -> str | None:
        tokens = before.strip().split()
        if not tokens:
            if token.startswith("/") and " " not in before:
                return None
            return None
        command = tokens[0].lower()
        if command not in COMMANDS:
            return None
        return command

    def split_completion_token(self, text: str) -> tuple[str, str]:
        if not text:
            return "", ""
        if text[-1].isspace():
            return text, ""
        stripped = text.rstrip()
        index = max(stripped.rfind(" "), stripped.rfind("\t"))
        if index == -1:
            return "", stripped
        return stripped[: index + 1], stripped[index + 1 :]

    def apply_completion(self, base: str, candidate: str) -> str:
        before, _token = self.split_completion_token(base)
        return before + self.quote_completion_token(candidate)

    def quote_completion_token(self, token: str) -> str:
        if not token or any(char.isspace() for char in token) or "\\" in token:
            return shlex.quote(token)
        return token

    def filter_candidates(self, candidates: list[str], prefix: str) -> list[str]:
        seen: set[str] = set()
        matches: list[str] = []
        prefix_lower = prefix.lower()
        for candidate in candidates:
            if candidate in seen:
                continue
            if candidate.lower().startswith(prefix_lower):
                seen.add(candidate)
                matches.append(candidate)
        return matches

    def manifest_completion_tokens(self) -> list[str]:
        return [self.display_manifest_path(path) for path in self.manifest_paths]

    def manifest_for_token(self, token: str) -> Manifest | None:
        matches: list[Manifest] = []
        for path in self.manifest_paths:
            try:
                manifest = load_manifest(path)
            except Exception:
                continue
            aliases = {self.display_manifest_path(path), str(path)}
            if token in aliases:
                matches.append(manifest)
        return matches[0] if len(matches) == 1 else None

    def task_completion_tokens(self) -> list[str]:
        manifest = self.selected_manifest
        if manifest is None:
            return []
        if self.selected_run_id:
            store = self.open_store_if_present()
            if store is not None:
                try:
                    rows = store.run_tasks(self.selected_run_id)
                    if rows:
                        return [str(row["task_id"]) for row in rows]
                finally:
                    store.close()
        return [task.id for task in manifest.tasks]

    def run_completion_tokens(self) -> list[str]:
        if self.selected_manifest is None:
            return []
        return [str(row["run_id"]) for row in self.batch_run_rows()]

    def task_for_token(self, token: str) -> Task | None:
        manifest = self.selected_manifest
        if manifest is None:
            return None
        return next((task for task in manifest.tasks if task.id == token), None)

    def display_manifest_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
        except ValueError:
            return path.as_posix()

    def reset_completion(self) -> None:
        self._completion_value = ""
        self._completion_candidates = []
        self._completion_index = 0
        self._completion_input = ""

    async def handle_command(self, command: str) -> None:
        if not command.startswith("/"):
            self.notify("Commands start with /. Use /help.", severity="warning")
            return
        try:
            parts = self.split_command(command)
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return
        name = parts[0].lower()
        args = parts[1:]
        try:
            if name in {"/quit", "/exit"}:
                await self.safe_quit()
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
            elif name == "/show_run":
                self.command_show_run(args)
            elif name == "/show_task":
                self.command_show_task(args)
            elif name == "/history":
                self.command_history(args)
            elif name == "/run":
                await self.command_run(args)
            elif name == "/resume":
                await self.command_resume(args)
            elif name == "/pause":
                self.command_pause(args)
            elif name == "/retry":
                await self.command_retry(args)
            elif name == "/rerun":
                await self.command_rerun(args)
            elif name == "/harness":
                await self.command_harness(args)
            elif name == "/theme":
                self.command_theme(args)
            else:
                self.notify(f"Unknown command: {name}", severity="error")
        except Exception as exc:
            self.notify(str(exc), severity="error")

    def split_command(self, command: str) -> list[str]:
        lexer = shlex.shlex(command, posix=True)
        lexer.whitespace_split = True
        lexer.escape = ""
        return list(lexer)

    def command_show_batch(self, args: list[str]) -> None:
        if len(args) > 1:
            raise RuntimeError("Usage: /show_batch <manifest-path>")
        token = args[0] if args else None
        if token:
            self.load_manifest_by_token(token)
        elif self.selected_manifest is None:
            raise RuntimeError("No manifest selected.")
        self.page = "batch"
        self.render_page()

    def action_show_batch(self) -> None:
        if self.selected_manifest is None:
            self.page = "home"
        elif self.page == "run":
            self.page = "batch"
        else:
            self.page = "home"
        self.render_page()

    def command_show_run(self, args: list[str]) -> None:
        if len(args) != 1:
            raise RuntimeError("Usage: /show_run <run-id>")
        run_id = args[0]
        if run_id not in self.run_completion_tokens():
            raise RuntimeError(f"run not found for selected batch: {run_id}")
        self.selected_run_id = run_id
        self.page = "run"
        self.render_page()

    def command_show_task(self, args: list[str]) -> None:
        if len(args) != 1:
            raise RuntimeError("Usage: /show_task <task-id>")
        self.focus_task_id = args[0]
        self.open_task_detail(args[0], run_id=self.selected_run_id or self.active_run_id)

    def open_task_detail(self, task_id: str, *, run_id: str = "", attempt_id: str = "") -> None:
        if run_id:
            store = self.open_store_if_present()
            try:
                persisted = store.run_task(run_id, task_id) if store is not None else None
            finally:
                if store is not None:
                    store.close()
            if persisted is None and not (self.progress_state and task_id in self.progress_state.tasks):
                raise RuntimeError(f"task not found in run {run_id}: {task_id}")
        elif self.task_for_token(task_id) is None and not (self.progress_state and task_id in self.progress_state.tasks):
            raise RuntimeError(f"task not found: {task_id}")
        self.focus_task_id = task_id
        if self.progress_state is not None:
            self.progress_state.focus_task_id = task_id
        self.push_screen(TaskDetailScreen(task_id, run_id, attempt_id))

    def command_history(self, args: list[str]) -> None:
        if len(args) > 1:
            raise RuntimeError("Usage: /history [run-id]")
        if args:
            self.command_show_run(args)
            return
        self.selected_run_id = ""
        self.page = "batch"
        self.render_page()

    async def command_run(self, args: list[str]) -> None:
        if self.run_task and not self.run_task.done():
            raise RuntimeError("A batch is already running in this TUI.")
        token, only, retry_failed, inline_vars, limit, focus, harness, resume_id = self.parse_run_args(args)
        if token:
            self.load_manifest_by_token(token)
        manifest = self.require_manifest()
        if resume_id:
            run_record, frozen_run_tasks = self.persisted_run_snapshot(resume_id)
            if str(run_record["status"]) not in {"paused", "failed", "interrupted"}:
                raise RuntimeError(f"run is not resumable: {resume_id} (status={run_record['status']})")
            run_vars: dict[str, str] = {}
            run_id = resume_id
            progress_manifest = _manifest_with_frozen_run_tasks(manifest, frozen_run_tasks)
        else:
            validate_manifest(manifest, harness_name=harness or self.harness_name)
            collected = await self.collect_run_vars(manifest, inline_vars)
            if collected is None:
                self.notify("run canceled", severity="warning")
                return
            run_vars = collected
            run_id = new_run_id()
            progress_manifest = manifest
        focus_task = focus or (next(iter(only)) if only and len(only) == 1 else self.focus_task_id)
        state = ProgressState.from_manifest(progress_manifest, focus_task_id=focus_task)
        state.work_id = run_id
        self.progress_state = state
        self.current_work_id = run_id
        self.active_run_id = run_id
        self.selected_run_id = run_id
        self.active_manifest_path = manifest.path.resolve()
        self.pause_event = asyncio.Event()
        self.pause_requested = False
        self.exit_after_pause = False
        self.focus_task_id = state.focus_task_id
        self.history_task_id = ""
        self._run_table_keys = []
        self.page = "run"
        self.render_page()
        if resume_id:
            coroutine = resume_manifest(
                manifest.path,
                run_id,
                task_ids=only,
                retry_failed=retry_failed,
                harness=harness,
                progress_callback=self.progress_callback,
                pause_event=self.pause_event,
            )
        else:
            coroutine = run_manifest(
                manifest.path,
                limit=limit,
                retry_failed=retry_failed,
                task_ids=only,
                run_id=run_id,
                run_vars=run_vars,
                harness=harness,
                progress_callback=self.progress_callback,
                pause_event=self.pause_event,
            )
        self.run_task = asyncio.create_task(coroutine)
        self.run_task.add_done_callback(self.finish_run)

    async def command_resume(self, args: list[str]) -> None:
        if len(args) != 1:
            raise RuntimeError("Usage: /resume <run-id>")
        await self.command_run(["--resume", args[0]])

    async def action_quit(self) -> None:
        await self.safe_quit()

    async def safe_quit(self) -> None:
        if self.run_task and not self.run_task.done():
            self.exit_after_pause = True
            self.request_current_pause()
            self.notify("pause requested; exiting after running task(s) finish")
            return
        self.exit()

    def command_pause(self, args: list[str]) -> None:
        if args:
            raise RuntimeError("Usage: /pause")
        if not self.run_task or self.run_task.done():
            raise RuntimeError("No batch is currently running in this TUI.")
        self.request_current_pause()

    def request_current_pause(self) -> None:
        if self.active_run_id:
            request_pause(self.active_run_id)
        if self.pause_event is not None:
            self.pause_event.set()
        self.pause_requested = True
        self._run_render_dirty = True
        self.notify("pause requested; no new task will be started")

    async def command_retry(self, args: list[str]) -> None:
        if len(args) != 1:
            raise RuntimeError("Usage: /retry <task-id>")
        if not self.selected_run_id:
            raise RuntimeError("Select a Run before retrying a Task.")
        store = self.open_store_if_present()
        if store is None:
            raise RuntimeError("No persisted state database yet.")
        try:
            run = store.batch_run(self.selected_run_id)
            if run is None:
                raise RuntimeError(f"run not found: {self.selected_run_id}")
            expected_path = str(self.require_manifest().path.resolve())
            if str(run["manifest_path"]) != expected_path:
                raise RuntimeError(
                    f"run {self.selected_run_id} belongs to another batch config: {run['manifest_path']}"
                )
            if str(run["status"]) not in {"paused", "failed", "interrupted"}:
                raise RuntimeError(
                    f"run is not retryable: {self.selected_run_id} (status={run['status']})"
                )
            store.mark_run_task_retry(self.selected_run_id, args[0])
        finally:
            store.close()
        await self.command_run(["--resume", self.selected_run_id, "--only", args[0], "--retry-failed"])

    async def command_rerun(self, args: list[str]) -> None:
        if len(args) != 1:
            raise RuntimeError("Usage: /rerun <task-id>")
        await self.command_run(["--only", args[0]])

    async def command_harness(self, args: list[str]) -> None:
        if not args:
            self.set_detail(
                [
                    f"Current harness: {self.harness_name}",
                    "Available: " + ", ".join(available_harnesses()),
                    "Use /harness doctor <name> before /harness use <name>.",
                    "The selected harness is frozen into each new Run.",
                ]
            )
            return
        action = args[0].lower()
        if action in available_harnesses() and len(args) == 1:
            action, args = "use", ["use", action]
        if action == "reset":
            if len(args) != 1:
                raise RuntimeError("Usage: /harness reset")
            self.harness_name = "native"
            self._save_preferences({"harness": "native"})
            self.notify("harness reset to native")
            self.render_page()
            return
        if action not in {"use", "doctor"} or len(args) != 2:
            raise RuntimeError("Usage: /harness [use|doctor] <native|opencode|claude> | /harness reset")
        name = args[1].lower()
        if name not in available_harnesses():
            raise RuntimeError(f"unknown harness: {name}")
        probe = await probe_harness(name)
        if action == "doctor":
            severity = "information" if probe.available else "warning"
            self.notify(
                f"{name}: {'available' if probe.available else 'unavailable'}: {probe.version or probe.error}",
                severity=severity,
            )
            return
        if not probe.available:
            raise RuntimeError(f"harness {name} is unavailable: {probe.error}")
        self.harness_name = name
        self._save_preferences({"harness": name})
        self.notify(f"harness set to {name}; applies to the next Run")
        self.render_page()

    def command_theme(self, args: list[str]) -> None:
        if not args:
            self.set_detail(
                [
                    f"Current theme: {self.theme}",
                    "Available: " + ", ".join(sorted(self.available_themes)),
                    "Use /theme <name>. Changes are saved in ~/.bagent/settings.json.",
                ]
            )
            return
        if len(args) != 1 or args[0] not in self.available_themes:
            raise RuntimeError("Usage: /theme <" + "|".join(sorted(self.available_themes)) + ">")
        self.theme = args[0]
        self._save_preferences({"theme": self.theme})
        self.notify(f"theme saved: {self.theme}")

    async def collect_run_vars(self, manifest: Manifest, initial: dict[str, str]) -> dict[str, str] | None:
        if not manifest.config.run_variables:
            return initial
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, str] | None] = loop.create_future()

        def callback(result: dict[str, str] | None) -> None:
            if not future.done():
                future.set_result(result)

        self.push_screen(RunVariablesScreen(manifest.config.run_variables, initial), callback=callback)
        return await future

    def parse_run_args(
        self, args: list[str]
    ) -> tuple[str | None, set[str] | None, bool, dict[str, str], int | None, str, str | None, str]:
        token: str | None = None
        only: set[str] = set()
        retry_failed = False
        run_vars: dict[str, str] = {}
        limit: int | None = None
        focus = ""
        harness: str | None = None
        resume_id = ""
        index = 0
        while index < len(args):
            arg = args[index]
            if arg == "--only":
                index += 1
                if index >= len(args):
                    raise RuntimeError("--only requires a task id")
                only.add(args[index])
            elif arg == "--retry-failed":
                retry_failed = True
            elif arg == "--limit":
                index += 1
                if index >= len(args):
                    raise RuntimeError("--limit requires a number")
                limit = self.parse_limit_arg(args[index])
            elif arg.startswith("--limit="):
                limit = self.parse_limit_arg(arg.removeprefix("--limit="))
            elif arg == "--focus":
                index += 1
                if index >= len(args):
                    raise RuntimeError("--focus requires a task id")
                focus = args[index]
            elif arg.startswith("--focus="):
                focus = arg.removeprefix("--focus=")
            elif arg == "--harness":
                index += 1
                if index >= len(args):
                    raise RuntimeError("--harness requires a name")
                harness = args[index].lower()
                if harness not in available_harnesses():
                    raise RuntimeError(f"unknown harness: {harness}")
            elif arg.startswith("--harness="):
                harness = arg.removeprefix("--harness=").lower()
                if harness not in available_harnesses():
                    raise RuntimeError(f"unknown harness: {harness}")
            elif arg == "--resume":
                index += 1
                if index >= len(args):
                    raise RuntimeError("--resume requires a run id")
                resume_id = args[index]
            elif arg.startswith("--resume="):
                resume_id = arg.removeprefix("--resume=")
            elif arg == "--var":
                index += 1
                if index >= len(args):
                    raise RuntimeError("--var requires name=value")
                name, value = self.parse_var_arg(args[index])
                run_vars[name] = value
            elif arg.startswith("--var="):
                name, value = self.parse_var_arg(arg.removeprefix("--var="))
                run_vars[name] = value
            elif token is None:
                token = arg
            else:
                raise RuntimeError(f"unexpected argument: {arg}")
            index += 1
        return token, only or None, retry_failed, run_vars, limit, focus, harness, resume_id

    def parse_limit_arg(self, value: str) -> int:
        try:
            limit = int(value)
        except ValueError as exc:
            raise RuntimeError(f"--limit must be an integer: {value}") from exc
        if limit < 0:
            raise RuntimeError("--limit must be non-negative")
        return limit

    def parse_var_arg(self, value: str) -> tuple[str, str]:
        if "=" not in value:
            raise RuntimeError(f"--var must be name=value: {value}")
        name, item = value.split("=", 1)
        name = name.strip()
        if not name:
            raise RuntimeError(f"--var name is empty: {value}")
        return name, item

    def progress_callback(self, event: dict[str, Any]) -> None:
        if threading.get_ident() == self.ui_thread:
            self.handle_progress_event(event)
        else:
            self.call_from_thread(self.handle_progress_event, event)

    def handle_progress_event(self, event: dict[str, Any]) -> None:
        if self.progress_state is None:
            return
        self.progress_state.handle_event(event)
        run_id = str(event.get("run_id") or event.get("work_id") or "")
        if run_id:
            self.current_work_id = run_id
            self.active_run_id = run_id
            self.selected_run_id = run_id
        task_id = str(event.get("task_id") or "")
        if task_id and not self.focus_task_id:
            self.focus_task_id = task_id
            self.progress_state.focus_task_id = task_id
        self._run_render_dirty = True

    def flush_run_render(self) -> None:
        if self.page != "run":
            return
        if self.run_task and not self.run_task.done():
            self.render_run()
            self._run_render_dirty = False
            return
        if self._run_render_dirty:
            self.render_run()
            self._run_render_dirty = False

    def finish_run(self, task: asyncio.Task) -> None:
        run_id = self.active_run_id or self.selected_run_id
        execution_error: Exception | None = None
        try:
            self.run_results = task.result()
        except Exception as exc:
            self.run_results = []
            execution_error = exc

        notified = False
        if run_id:
            try:
                run, tasks = self.persisted_run_snapshot(run_id)
            except RuntimeError:
                pass
            else:
                message, severity = _run_status_feedback(run, tasks)
                if execution_error is not None and str(execution_error) not in message:
                    message = f"{message} Error: {execution_error}"
                self.notify(message, severity=severity)
                notified = True
        if not notified:
            if execution_error is not None:
                self.notify(f"Run failed: {execution_error}", severity="error")
            else:
                failed = [result for result in self.run_results if not result.success]
                if failed:
                    self.notify(f"Run finished with {len(failed)} failed Task(s).", severity="warning")
                else:
                    self.notify(f"Run completed: {len(self.run_results)} Task(s).")
        self.pause_event = None
        self.active_run_id = ""
        self.active_manifest_path = None
        self.refresh_current()
        if self.exit_after_pause:
            self.exit()

    def refresh_current(self) -> None:
        if self.selected_manifest_path:
            self.load_manifest(self.selected_manifest_path)
        self.discover_manifests()
        self.render_page()

    def load_manifest_by_token(self, token: str) -> None:
        path = self.resolve_manifest_token(token)
        self.load_manifest(path)

    def resolve_manifest_token(self, token: str) -> Path:
        direct = Path(token)
        if direct.exists():
            return direct
        raise RuntimeError(f"manifest path not found: {token}")

    def load_manifest(self, path: Path) -> None:
        previous_path = self.selected_manifest_path.resolve() if self.selected_manifest_path else None
        manifest = load_manifest(path)
        validate_manifest(manifest)
        self.selected_manifest = manifest
        self.selected_manifest_path = manifest.path
        if previous_path is not None and previous_path != manifest.path.resolve():
            self.selected_run_id = ""
            self.progress_state = None
        task_ids = {task.id for task in manifest.tasks}
        if manifest.tasks and self.focus_task_id not in task_ids:
            self.focus_task_id = manifest.tasks[0].id
        if self.history_task_id and self.history_task_id not in task_ids:
            self.history_task_id = ""

    def require_manifest(self) -> Manifest:
        if self.selected_manifest is None:
            raise RuntimeError("No manifest selected. Use /show_batch <manifest-path> or click a batch row.")
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

    def task_progress_for(self, task_id: str):
        if self.progress_state is None:
            return None
        return self.progress_state.tasks.get(task_id)

    def task_detail_text_for(self, task_id: str, run_id: str = "", attempt_id: str = "") -> str:
        task = self.task_progress_for(task_id)
        selected_run = run_id or self.selected_run_id or self.active_run_id
        if task is not None and selected_run == self.active_run_id and self.run_task and not self.run_task.done():
            live = self.task_progress_text(task, full=True)
            persisted = self.persisted_task_output_lines(task_id, selected_run, attempt_id)
            return console_safe(live + "\n\n" + "\n".join(persisted))
        if selected_run:
            return console_safe("\n".join(self.persisted_task_output_lines(task_id, selected_run, attempt_id)))
        manifest = self.require_manifest()
        row = next((item for item in manifest.tasks if item.id == task_id), None)
        if row is None:
            return console_safe(f"Task not found: {task_id}")
        return self.task_row_text(row, include_history=True)

    def task_progress_text(self, task, full: bool = False) -> str:
        base = [
            f"status: {task.status}",
            f"attempts: {task.attempts}",
            f"run_id: {task.run_id or self.active_run_id}",
            f"attempt_id: {task.attempt_id}",
            f"total_tokens: {task.total_tokens if task.total_tokens is not None else ''}",
            f"run_dir: {task.run_dir}",
            f"artifact: {task.artifact_path}",
            f"result: {task.result}",
            f"error: {task.error}",
            f"detail: {task.detail}",
        ]
        if full:
            events = task.events[-40:] or ["(no tool events yet)"]
            stream = task.stream_text[-12000:] if task.stream_text else "(no model output captured yet)"
            base.extend(["", "events:", *events, "", "model output:", stream])
        return console_safe("\n".join(base))

    def task_row_text(self, task: Task, *, include_history: bool = False) -> str:
        lines = [
            f"status: {task.status}",
            f"kind: {task.kind}",
            f"attempts: {task.attempts}",
            f"result: {task.result}",
            f"error: {task.error}",
            f"input: {json.dumps(task.input, ensure_ascii=False, indent=2)}",
        ]
        if include_history:
            lines.extend(["", *self.persisted_task_output_lines(task.id)])
        return console_safe("\n".join(lines))

    def persisted_task_output_lines(
        self,
        task_id: str,
        run_id: str = "",
        attempt_id: str = "",
    ) -> list[str]:
        manifest = self.require_manifest()
        db_path = state_db_path(manifest)
        if not db_path.exists():
            return ["persisted output:", "(no state database yet)"]
        store = SessionStore(db_path)
        try:
            selected_run = run_id
            if not selected_run:
                runs = store.batch_runs(manifest.path)
                selected_run = next(
                    (
                        str(row["run_id"])
                        for row in runs
                        if store.run_task(str(row["run_id"]), task_id) is not None
                    ),
                    "",
                )
            if not selected_run:
                return ["persisted output:", "(no persisted Run for this Task yet)"]
            run = store.batch_run(selected_run)
            run_task = store.run_task(selected_run, task_id)
            if run is None or run_task is None:
                return ["persisted output:", f"(Task {task_id} is not part of Run {selected_run})"]
            attempts = store.task_attempts(selected_run, task_id)
            lines = [
                "Run / Task:",
                f"run_id: {selected_run}",
                f"run_status: {run['status']}",
                f"task_id: {task_id}",
                f"task_status: {run_task['status']}",
                f"attempt_count: {run_task['attempt_count']}",
                f"elapsed: {_format_seconds(float(run_task.get('elapsed_seconds') or 0))}",
                f"total_tokens: {run_task.get('total_tokens') if run_task.get('total_tokens') is not None else ''}",
                f"result: {json.dumps(run_task.get('result') or {}, ensure_ascii=False)}",
                f"error: {run_task.get('error') or ''}",
                "",
                "Attempts (newest first):",
            ]
            if not attempts:
                lines.append("(no Attempts yet)")
                return lines
            for row in attempts:
                lines.append(
                    "  {attempt_id}  #{attempt_no}  {status}  {harness}  {elapsed}  tokens={tokens}".format(
                        attempt_id=row["attempt_id"],
                        attempt_no=row["attempt_no"],
                        status=row["status"],
                        harness=row["harness"],
                        elapsed=_format_seconds(float(row.get("elapsed_seconds") or 0)),
                        tokens=row.get("total_tokens") if row.get("total_tokens") is not None else "-",
                    )
                )
            chosen = next((row for row in attempts if row["attempt_id"] == attempt_id), None) if attempt_id else attempts[0]
            if chosen is None:
                lines.extend(["", f"Attempt not found: {attempt_id}"])
                return lines
            selected_attempt = str(chosen["attempt_id"])
            lines.extend(
                [
                    "",
                    "Selected Attempt:",
                    f"attempt_id: {selected_attempt}",
                    f"attempt_no: {chosen['attempt_no']}",
                    f"status: {chosen['status']}",
                    f"harness: {chosen['harness']} {chosen.get('harness_version') or ''}",
                    f"external_session_id: {chosen.get('external_session_id') or ''}",
                    f"run_dir: {chosen['run_dir']}",
                    f"started_at: {chosen['started_at']}",
                    f"finished_at: {chosen.get('finished_at') or ''}",
                    f"elapsed: {_format_seconds(float(chosen.get('elapsed_seconds') or 0))}",
                    f"usage: {json.dumps(chosen.get('usage') or {}, ensure_ascii=False)}",
                    f"result: {json.dumps(chosen.get('result') or {}, ensure_ascii=False)}",
                    f"error: {chosen.get('error') or ''}",
                ]
            )
            artifacts = store.run_artifacts(selected_attempt)
            if artifacts:
                lines.extend(["", "artifacts:", json.dumps(artifacts, ensure_ascii=False, indent=2)])
            tool_events = store.run_tool_events(selected_attempt, limit=20)
            if tool_events:
                lines.append("")
                lines.append("tool events:")
                for event in tool_events:
                    error = f" error={event['error']}" if event["error"] else ""
                    lines.append(f"  #{event['seq']} {event['tool_name']}{error}")
                    lines.append(f"    args: {truncate(json.dumps(event['arguments'], ensure_ascii=False), 500)}")
            model_calls = store.model_calls(selected_attempt)
            if model_calls:
                lines.extend(["", "model calls:", json.dumps(model_calls, ensure_ascii=False, indent=2)])
            messages = store.run_messages(selected_attempt, limit=20)
            assistant_or_tool = [
                message
                for message in messages
                if message["role"] in {"assistant", "tool", "harness", "harness-stderr"}
            ]
            lines.extend(["", "agent output:"])
            if not assistant_or_tool:
                lines.append("(no assistant/tool messages persisted)")
            for message in assistant_or_tool:
                content = message["content"] or "(empty)"
                lines.append(f"[{message['seq']} {message['role']} {message['created_at']}]")
                lines.append(content[-4000:] if len(content) > 4000 else content)
            return lines
        finally:
            store.close()


def _manifest_with_frozen_run_tasks(manifest: Manifest, rows: list[dict[str, Any]]) -> Manifest:
    tasks: list[Task] = []
    for row in rows:
        definition = dict(row.get("definition") or {})
        result = row.get("result") or {}
        if isinstance(result, dict):
            result_text = str(result.get("artifact_path") or result.get("output") or "")
            if not result_text and result:
                result_text = json.dumps(result, ensure_ascii=False)
        else:
            result_text = str(result)
        tasks.append(
            Task(
                status=str(row.get("status") or "queued"),
                id=str(row["task_id"]),
                kind=str(definition.get("kind") or row.get("kind") or ""),
                input=dict(definition.get("input") or row.get("input") or {}),
                result=result_text,
                attempts=int(row.get("attempt_count") or 0),
                error=str(row.get("error") or ""),
                lease=str(row.get("latest_attempt_id") or ""),
            )
        )
    return Manifest(
        path=manifest.path,
        text=manifest.text,
        config=manifest.config,
        tasks=tasks,
        tasks_start=manifest.tasks_start,
        tasks_end=manifest.tasks_end,
    )


def _run_status_feedback(run: dict[str, Any], tasks: list[dict[str, Any]]) -> tuple[str, str]:
    run_id = str(run["run_id"])
    status = str(run["status"])
    counts: dict[str, int] = {}
    for task in tasks:
        task_status = str(task["status"])
        counts[task_status] = counts.get(task_status, 0) + 1
    unfinished = sum(
        counts.get(item, 0)
        for item in {"queued", "retry", "interrupted", "running", "todo", "needs-review"}
    )
    failed = counts.get("failed", 0)
    done = counts.get("done", 0)
    if status == "completed":
        return f"Run {run_id} completed: {done} Task(s) done.", "information"
    if status in {"paused", "interrupted", "queued"}:
        queued = counts.get("queued", 0)
        return (
            f"Run {run_id} {status}: {unfinished} unfinished Task(s), {queued} queued. "
            f"Resume with /resume {run_id}.",
            "warning",
        )
    if status == "failed":
        if unfinished:
            next_action = f" Resume unfinished work with /resume {run_id}."
        elif failed:
            next_action = f" Select a failed Task, then use /retry <task-id> in Run {run_id}."
        else:
            next_action = ""
        return f"Run {run_id} failed: {failed} failed, {unfinished} unfinished Task(s).{next_action}", "error"
    if status == "running":
        return f"Run {run_id} is still active.", "information"
    return f"Run {run_id} finished with status {status}.", "warning"


def run_tui(start_manifest: str | None = None, auto_run_args: list[str] | None = None) -> int:
    try:
        BatchAgentTui(start_manifest=start_manifest, auto_run_args=auto_run_args).run()
        return 0
    except (ManifestError, RuntimeError) as exc:
        print(f"error: {exc}")
        return 2
