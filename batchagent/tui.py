from __future__ import annotations

import asyncio
import json
import shlex
import threading
from datetime import date
from pathlib import Path
from typing import Any

from rich.panel import Panel
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, RichLog, Static

from .manifest import ManifestError, load_manifest
from .models import Manifest, RunVariable, Task
from .progress import ProgressState, _format_seconds
from .scheduler import mark_tasks_for_retry, new_work_id, rerun_tasks, run_manifest, state_db_path, status, validate_manifest
from .store import SessionStore
from .util import console_safe, truncate


COMMAND_SPECS = [
    ("/show_batch", "/show_batch <manifest-path>", "Select and inspect a batch configuration file."),
    ("/run", "/run [manifest-path] [--only task-id] [--retry-failed]", "Start a batch work from the selected batch configuration."),
    ("/show_task", "/show_task <task-id>", "Show the selected task row and live task detail."),
    ("/history", "/history [task-id|all]", "Show persisted run history for the current batch."),
    ("/retry", "/retry <task-id|all>", "Mark failed task(s) as retry without deleting prior history."),
    ("/rerun", "/rerun <task-id>", "Reset task status to todo; prior run directories are kept."),
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

    def __init__(self, task_id: str):
        super().__init__()
        self.task_id = task_id
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
        title.update(f"Task Detail: {self.task_id}\nEsc closes this window.")
        detail_text = getattr(self.app, "task_detail_text_for")(self.task_id)
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
                    "Enter saves this value. Esc cancels this batch work.",
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
        self.current_work_id = ""
        self.ui_thread = 0
        self.history_task_id = ""
        self._completion_value = ""
        self._completion_candidates: list[str] = []
        self._completion_index = 0
        self._completion_input = ""

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
        counts = status(manifest.path)
        self.query_one("#title", Static).update(
            Panel(
                "\n".join(
                    [
                        self.selected_batch_line(),
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
                key=f"task:{task.id}",
            )
        self.set_detail(
            [
                "Batch page:",
                "  The center table lists tasks from the selected batch.",
                "  The Output/Error column shows the latest manifest result or error.",
                "  Type / in the bottom input for commands and examples.",
                "",
                "Every /run attempt creates a new .batchagent/runs/<task>-<run_id> directory; old results stay available in /history.",
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
                        self.selected_batch_line(),
                        f"Batch Work: {state.work_id or self.current_work_id or '(pending)'}",
                        f"Config: {state.manifest.path}",
                        f"loaded={state.total_tasks} selected={state.eligible_tasks} running={running} queued={queued} done={done} failed={failed}",
                        f"elapsed={_format_seconds(state.elapsed())} eta={'unknown' if eta is None else _format_seconds(eta)}",
                        "run dirs are unique per attempt; existing task results are not overwritten on disk",
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
                key=f"task:{task.id}",
            )
        self.render_focused_task_detail()

    def render_history(self) -> None:
        manifest = self.require_manifest()
        rows = self.history_rows(self.history_task_id or None)
        title_lines = [
            self.selected_batch_line(),
            f"Manifest: {manifest.path}",
            f"History: {self.history_task_id or 'all tasks'}",
            "Each run has an immutable run_id and a separate run directory.",
        ]
        self.query_one("#title", Static).update(Panel("\n".join(title_lines), title="Run History"))
        table = self.query_one("#table", DataTable)
        table.clear(columns=True)
        table.add_columns("Work ID", "Task", "Run ID", "Attempt", "Status", "Started", "Finished", "Run Dir", "Error")
        for row in rows:
            table.add_row(
                row.get("work_id", ""),
                row["task_id"],
                row["run_id"],
                str(row["attempt"]),
                row["status"],
                row["started_at"],
                row["finished_at"] or "",
                truncate(row["run_dir"], 80),
                truncate(row["error"], 80),
                key=f"task:{row['task_id']}",
            )
        self.set_detail(
            [
                f"history rows: {len(rows)}",
                "History page:",
                "  Rows are persisted SQLite run records for the selected batch.",
                "  The manifest stores the latest result, while this page keeps prior runs visible.",
                "  Type /history <task-id> or /show_task <task-id> in the bottom input.",
            ]
        )

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
        if key.startswith("task:"):
            try:
                self.open_task_detail(key.removeprefix("task:"))
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
            return store.task_runs(task_id) if task_id else store.all_runs()
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
            return "--retry-failed - Include failed tasks in this Batch Work selection."
        if candidate == "all":
            return "all - Apply to all relevant tasks or show all run history."
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
        if command in {"/show_task", "/retry", "/rerun", "/history"}:
            candidates = self.task_completion_tokens()
            if command in {"/retry", "/history"}:
                candidates = ["all", *candidates]
            return self.filter_candidates(candidates, token)
        if command == "/run":
            return self.run_completion_candidates(before, token)
        return []

    def run_completion_candidates(self, before: str, token: str) -> list[str]:
        tokens = before.strip().split()
        if tokens and tokens[-1] == "--only":
            return self.filter_candidates(self.task_completion_tokens(), token)
        if tokens and tokens[-1] == "--var":
            return []
        option_candidates = ["--only", "--retry-failed", "--var"]
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
            if item in {"--only", "--var"}:
                index += 2
                continue
            if item == "--retry-failed" or item.startswith("--var="):
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
        return [task.id for task in manifest.tasks]

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
            elif name == "/history":
                self.command_history(args)
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

    def command_show_task(self, args: list[str]) -> None:
        if len(args) != 1:
            raise RuntimeError("Usage: /show_task <task-id>")
        self.focus_task_id = args[0]
        self.open_task_detail(args[0])

    def open_task_detail(self, task_id: str) -> None:
        if self.task_for_token(task_id) is None and not (self.progress_state and task_id in self.progress_state.tasks):
            raise RuntimeError(f"task not found: {task_id}")
        self.focus_task_id = task_id
        if self.progress_state is not None:
            self.progress_state.focus_task_id = task_id
        self.push_screen(TaskDetailScreen(task_id))

    def command_history(self, args: list[str]) -> None:
        if args and args[0] not in {"all", "*"}:
            self.history_task_id = args[0]
            self.focus_task_id = args[0]
        else:
            self.history_task_id = ""
        self.page = "history"
        self.render_page()

    async def command_run(self, args: list[str]) -> None:
        if self.run_task and not self.run_task.done():
            raise RuntimeError("A batch is already running in this TUI.")
        token, only, retry_failed, inline_vars = self.parse_run_args(args)
        if token:
            self.load_manifest_by_token(token)
        manifest = self.require_manifest()
        validate_manifest(manifest)
        run_vars = await self.collect_run_vars(manifest, inline_vars)
        if run_vars is None:
            self.notify("batch work canceled", severity="warning")
            return
        work_id = new_work_id()
        state = ProgressState.from_manifest(manifest, focus_task_id=only or self.focus_task_id)
        state.work_id = work_id
        self.progress_state = state
        self.current_work_id = work_id
        self.focus_task_id = state.focus_task_id
        self.history_task_id = ""
        self.page = "run"
        self.render_page()
        task_ids = {only} if only else None
        self.run_task = asyncio.create_task(
            run_manifest(
                manifest.path,
                retry_failed=retry_failed,
                task_ids=task_ids,
                work_id=work_id,
                run_vars=run_vars,
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

    def parse_run_args(self, args: list[str]) -> tuple[str | None, str | None, bool, dict[str, str]]:
        token: str | None = None
        only: str | None = None
        retry_failed = False
        run_vars: dict[str, str] = {}
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
        return token, only, retry_failed, run_vars

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
        work_id = str(event.get("work_id") or "")
        if work_id:
            self.current_work_id = work_id
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
        direct = Path(token)
        if direct.exists():
            return direct
        raise RuntimeError(f"manifest path not found: {token}")

    def load_manifest(self, path: Path) -> None:
        manifest = load_manifest(path)
        validate_manifest(manifest)
        self.selected_manifest = manifest
        self.selected_manifest_path = manifest.path
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

    def task_detail_text_for(self, task_id: str) -> str:
        task = self.task_progress_for(task_id)
        if task is not None:
            return self.task_progress_text(task, full=True)
        manifest = self.require_manifest()
        row = next((item for item in manifest.tasks if item.id == task_id), None)
        if row is None:
            return console_safe(f"Task not found: {task_id}")
        return self.task_row_text(row, include_history=True)

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

    def persisted_task_output_lines(self, task_id: str) -> list[str]:
        manifest = self.require_manifest()
        db_path = state_db_path(manifest)
        if not db_path.exists():
            return ["persisted run output:", "(no state database yet)"]
        store = SessionStore(db_path)
        try:
            runs = store.task_runs(task_id)
            if not runs:
                return ["persisted run output:", "(no persisted runs for this task yet)"]
            run = runs[0]
            run_id = run["run_id"]
            lines = [
                "latest persisted run:",
                f"run_id: {run_id}",
                f"attempt: {run['attempt']}",
                f"status: {run['status']}",
                f"run_dir: {run['run_dir']}",
                f"started_at: {run['started_at']}",
                f"finished_at: {run['finished_at'] or ''}",
                f"error: {run['error']}",
            ]
            artifacts = store.run_artifacts(run_id)
            if artifacts:
                lines.extend(["", "artifacts:", json.dumps(artifacts, ensure_ascii=False, indent=2)])
            tool_events = store.run_tool_events(run_id, limit=20)
            if tool_events:
                lines.append("")
                lines.append("tool events:")
                for event in tool_events:
                    error = f" error={event['error']}" if event["error"] else ""
                    lines.append(f"  #{event['seq']} {event['tool_name']}{error}")
                    lines.append(f"    args: {truncate(json.dumps(event['arguments'], ensure_ascii=False), 500)}")
            messages = store.run_messages(run_id, limit=20)
            assistant_or_tool = [message for message in messages if message["role"] in {"assistant", "tool"}]
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


def run_tui(start_manifest: str | None = None) -> int:
    try:
        BatchAgentTui(start_manifest=start_manifest).run()
        return 0
    except (ManifestError, RuntimeError) as exc:
        print(f"error: {exc}")
        return 2
