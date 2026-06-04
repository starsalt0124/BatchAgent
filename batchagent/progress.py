from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable

from .models import Manifest, Task
from .util import console_safe, truncate


TERMINAL = {"done", "failed", "skipped"}


@dataclass
class TaskProgress:
    id: str
    status: str
    attempts: int = 0
    result: str = ""
    error: str = ""
    run_id: str = ""
    run_dir: str = ""
    started_monotonic: float | None = None
    finished_monotonic: float | None = None
    current_run_started_monotonic: float | None = None

    def elapsed_current(self, now: float) -> float:
        if self.current_run_started_monotonic is None:
            return 0.0
        end = self.finished_monotonic if self.status in TERMINAL and self.finished_monotonic else now
        return max(0.0, end - self.current_run_started_monotonic)

    def elapsed_total(self, now: float) -> float:
        if self.started_monotonic is None:
            return 0.0
        end = self.finished_monotonic if self.status in TERMINAL and self.finished_monotonic else now
        return max(0.0, end - self.started_monotonic)


@dataclass
class ProgressState:
    manifest: Manifest
    focus_task_id: str = ""
    started_monotonic: float = field(default_factory=time.monotonic)
    total_tasks: int = 0
    eligible_tasks: int = 0
    concurrency: int = 1
    tasks: dict[str, TaskProgress] = field(default_factory=dict)
    current_run_task_ids: set[str] = field(default_factory=set)

    @classmethod
    def from_manifest(cls, manifest: Manifest, focus_task_id: str = "") -> "ProgressState":
        state = cls(manifest=manifest, focus_task_id=focus_task_id)
        state.total_tasks = len(manifest.tasks)
        state.concurrency = manifest.config.effective_concurrency
        for task in manifest.tasks:
            state.tasks[task.id] = TaskProgress(
                id=task.id,
                status=task.status,
                attempts=task.attempts,
                result=task.result,
                error=task.error,
                run_id=task.lease,
            )
        return state

    def handle_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        now = time.monotonic()
        if event_type == "batch_loaded":
            self.total_tasks = int(event.get("total_tasks", self.total_tasks))
            self.eligible_tasks = int(event.get("eligible_tasks", self.eligible_tasks))
            self.concurrency = int(event.get("concurrency", self.concurrency))
            return
        task_id = str(event.get("task_id") or "")
        if not task_id:
            return
        task = self.tasks.setdefault(task_id, TaskProgress(id=task_id, status="unknown"))
        if event_type == "task_queued":
            self.current_run_task_ids.add(task_id)
            task.status = "queued"
            task.attempts = int(event.get("attempts", task.attempts))
            return
        if event_type == "task_started":
            self.current_run_task_ids.add(task_id)
            task.status = "running"
            task.run_id = str(event.get("run_id") or "")
            task.run_dir = str(event.get("run_dir") or "")
            task.attempts = int(event.get("attempt", task.attempts))
            if task.started_monotonic is None:
                task.started_monotonic = now
            task.current_run_started_monotonic = now
            task.finished_monotonic = None
            task.error = ""
            return
        if event_type in {"task_done", "task_failed", "task_retry"}:
            task.status = {"task_done": "done", "task_failed": "failed", "task_retry": "retry"}[event_type]
            task.attempts = int(event.get("attempt", task.attempts))
            task.run_dir = str(event.get("run_dir") or task.run_dir)
            task.result = str(event.get("result") or task.result)
            task.error = str(event.get("error") or "")
            task.finished_monotonic = now

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for task in self.tasks.values():
            counts[task.status] = counts.get(task.status, 0) + 1
        return counts

    def elapsed(self) -> float:
        return max(0.0, time.monotonic() - self.started_monotonic)

    def eta_seconds(self) -> float | None:
        now = time.monotonic()
        finished = [
            task.elapsed_total(now)
            for task_id, task in self.tasks.items()
            if task_id in self.current_run_task_ids and task.status in {"done", "failed"} and task.elapsed_total(now) > 0
        ]
        if not finished:
            return None
        current_done = sum(
            1 for task_id, task in self.tasks.items() if task_id in self.current_run_task_ids and task.status in {"done", "failed", "skipped"}
        )
        remaining = max(0, self.eligible_tasks - current_done)
        if remaining == 0:
            return 0.0
        avg = sum(finished) / len(finished)
        return avg * remaining / max(1, self.concurrency)


class PlainProgress:
    def __init__(self, state: ProgressState):
        self.state = state

    def callback(self, event: dict[str, Any]) -> None:
        self.state.handle_event(event)
        event_type = event.get("type")
        if event_type == "batch_loaded":
            print(
                f"loaded {event.get('total_tasks', 0)} task(s), "
                f"eligible {event.get('eligible_tasks', 0)}, concurrency {event.get('concurrency', 1)}"
            )
        elif event_type == "task_started":
            print(f"running {event.get('task_id')} attempt {event.get('attempt')} -> {event.get('run_dir')}")
        elif event_type == "task_done":
            print(f"done {event.get('task_id')} after attempt {event.get('attempt')}")
        elif event_type == "task_retry":
            print(f"retry {event.get('task_id')}: {event.get('error')}")
        elif event_type == "task_failed":
            print(f"failed {event.get('task_id')}: {event.get('error')}")


class RichRunDisplay:
    def __init__(self, state: ProgressState):
        self.state = state
        self._rich = _load_rich()

    @property
    def available(self) -> bool:
        return self._rich is not None

    async def run(self, awaitable: Awaitable[list[Any]]) -> list[Any]:
        if self._rich is None:
            return await awaitable
        Live = self._rich["Live"]
        console = self._rich["Console"]()
        with Live(self.render(), console=console, refresh_per_second=4, transient=False) as live:
            task = asyncio.create_task(awaitable)
            while not task.done():
                live.update(self.render())
                await asyncio.sleep(0.25)
            result = await task
            live.update(self.render(final=True))
            return result

    def render(self, final: bool = False) -> Any:
        if self._rich is None:
            return ""
        Group = self._rich["Group"]
        Panel = self._rich["Panel"]
        Table = self._rich["Table"]
        Text = self._rich["Text"]
        ProgressBar = self._rich["ProgressBar"]

        counts = self.state.counts()
        done = counts.get("done", 0)
        failed = counts.get("failed", 0)
        running = counts.get("running", 0)
        retry = counts.get("retry", 0)
        queued = counts.get("queued", 0)
        complete = done + failed + counts.get("skipped", 0)
        total = max(1, self.state.total_tasks)
        eta = self.state.eta_seconds()
        eta_text = "unknown" if eta is None else _format_seconds(eta)
        finish_text = "unknown" if eta is None else (datetime.now() + timedelta(seconds=eta)).strftime("%H:%M:%S")
        title = "BatchAgent Run" + (" (final)" if final else "")

        header = Table.grid(expand=True)
        header.add_column(ratio=1)
        header.add_row(
            Text(
                f"loaded {self.state.total_tasks} | eligible {self.state.eligible_tasks} | "
                f"running {running} | queued {queued} | done {done} | failed {failed} | retry {retry}"
            )
        )
        header.add_row(ProgressBar(total=total, completed=complete, width=None))
        header.add_row(Text(f"elapsed {_format_seconds(self.state.elapsed())} | ETA {eta_text} | estimated finish {finish_text}"))

        task_table = Table(expand=True)
        task_table.add_column("Status", width=9)
        task_table.add_column("Task", ratio=2, overflow="fold")
        task_table.add_column("Attempts", justify="right", width=8)
        task_table.add_column("Run Time", justify="right", width=10)
        task_table.add_column("Detail", ratio=3, overflow="fold")
        now = time.monotonic()
        for task in self._ordered_tasks():
            status_style = _status_style(task.status)
            detail = task.error or task.result or task.run_dir
            if task.id == self.state.focus_task_id:
                detail = "[focus] " + detail
            task_table.add_row(
                Text(task.status, style=status_style),
                console_safe(task.id),
                str(task.attempts),
                _format_seconds(task.elapsed_current(now)),
                console_safe(truncate(detail, 90)),
            )

        focused = self._focused_panel()
        panels = [Panel(header, title=title), Panel(task_table, title="Tasks")]
        if focused is not None:
            panels.append(focused)
        return Group(*panels)

    def _ordered_tasks(self) -> list[TaskProgress]:
        rank = {"running": 0, "retry": 1, "queued": 2, "failed": 3, "todo": 4, "done": 5, "skipped": 6}
        return sorted(self.state.tasks.values(), key=lambda task: (rank.get(task.status, 9), task.id))

    def _focused_panel(self) -> Any | None:
        if not self.state.focus_task_id or self._rich is None:
            return None
        Panel = self._rich["Panel"]
        task = self.state.tasks.get(self.state.focus_task_id)
        if task is None:
            return Panel(f"Task not found: {self.state.focus_task_id}", title="Focus")
        detail = "\n".join(
            [
                f"id: {task.id}",
                f"status: {task.status}",
                f"attempts: {task.attempts}",
                f"run_dir: {console_safe(task.run_dir)}",
                f"result: {console_safe(task.result)}",
                f"error: {console_safe(task.error)}",
            ]
        )
        return Panel(detail, title="Focus")


def _load_rich() -> dict[str, Any] | None:
    try:
        from rich.console import Console, Group
        from rich.live import Live
        from rich.panel import Panel
        from rich.progress_bar import ProgressBar
        from rich.table import Table
        from rich.text import Text
    except ImportError:
        return None
    return {
        "Console": Console,
        "Group": Group,
        "Live": Live,
        "Panel": Panel,
        "ProgressBar": ProgressBar,
        "Table": Table,
        "Text": Text,
    }


def _format_seconds(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{sec:02d}s"
    if minutes:
        return f"{minutes}m{sec:02d}s"
    return f"{sec}s"


def _status_style(status: str) -> str:
    return {
        "running": "bold cyan",
        "queued": "blue",
        "done": "green",
        "failed": "bold red",
        "retry": "yellow",
        "todo": "dim",
        "skipped": "magenta",
    }.get(status, "")
