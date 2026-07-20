from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .models import Manifest
from .util import truncate


TERMINAL = {"done", "failed", "skipped"}


@dataclass
class TaskProgress:
    id: str
    status: str
    attempts: int = 0
    result: str = ""
    error: str = ""
    run_id: str = ""
    attempt_id: str = ""
    run_dir: str = ""
    artifact_path: str = ""
    detail: str = ""
    stream_text: str = ""
    events: list[str] = field(default_factory=list)
    total_tokens: int | None = None
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
    work_id: str = ""
    harness: str = ""
    started_monotonic: float = field(default_factory=time.monotonic)
    total_tasks: int = 0
    eligible_tasks: int = 0
    concurrency: int = 1
    tasks: dict[str, TaskProgress] = field(default_factory=dict)
    current_run_task_ids: set[str] = field(default_factory=set)
    paused: bool = False
    pause_detail: str = ""

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
            self.work_id = str(event.get("run_id") or event.get("work_id") or self.work_id)
            self.harness = str(event.get("harness") or self.harness)
            self.total_tasks = int(event.get("total_tasks", self.total_tasks))
            self.eligible_tasks = int(event.get("eligible_tasks", self.eligible_tasks))
            self.concurrency = int(event.get("concurrency", self.concurrency))
            self.paused = False
            self.pause_detail = ""
            return
        if event_type == "batch_paused":
            self.paused = True
            pending = int(event.get("pending_tasks") or 0)
            running = int(event.get("running_tasks") or 0)
            self.pause_detail = f"paused with {pending} pending task(s), {running} running task(s)"
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
            task.run_id = str(event.get("run_id") or self.work_id)
            task.attempt_id = str(event.get("attempt_id") or "")
            task.run_dir = str(event.get("run_dir") or "")
            task.attempts = int(event.get("attempt", task.attempts))
            if task.started_monotonic is None:
                task.started_monotonic = now
            task.current_run_started_monotonic = now
            task.finished_monotonic = None
            task.error = ""
            task.detail = "model starting"
            return
        if event_type in {"harness_progress", "harness_stderr"}:
            text = str(event.get("message") or event.get("content") or "")
            if text:
                task.detail = truncate(text, 160)
                task.events.append(task.detail)
                task.events = task.events[-30:]
            return
        if event_type == "harness_usage":
            usage = event.get("usage") or {}
            if isinstance(usage, dict):
                total = usage.get("total_tokens")
                tokens = usage.get("tokens") if isinstance(usage.get("tokens"), dict) else {}
                if total is None:
                    total = tokens.get("total") or tokens.get("total_tokens")
                if total is None:
                    prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
                    completion = usage.get("completion_tokens", usage.get("output_tokens"))
                    if prompt is None:
                        prompt = tokens.get("prompt_tokens", tokens.get("input_tokens"))
                    if completion is None:
                        completion = tokens.get("completion_tokens", tokens.get("output_tokens"))
                    if prompt is not None or completion is not None:
                        try:
                            total = int(prompt or 0) + int(completion or 0)
                        except (TypeError, ValueError):
                            total = None
                try:
                    task.total_tokens = int(total) if total is not None else task.total_tokens
                except (TypeError, ValueError):
                    pass
            return
        if event_type == "model_delta":
            delta = str(event.get("delta") or "")
            if delta:
                task.stream_text = _tail_text(task.stream_text + delta, 5000)
                task.detail = _last_nonempty_line(task.stream_text) or "model streaming"
            return
        if event_type == "assistant_message":
            content = str(event.get("content") or "")
            if content:
                task.stream_text = _tail_text(content, 5000)
                task.detail = _last_nonempty_line(content) or "assistant message"
            return
        if event_type == "tool_started":
            tool = str(event.get("tool") or "")
            text = f"calling {tool}"
            task.detail = text
            task.events.append(text)
            task.events = task.events[-30:]
            return
        if event_type == "tool_finished":
            tool = str(event.get("tool") or "")
            error = str(event.get("error") or "")
            text = f"{tool} failed: {error}" if error else f"{tool} finished"
            task.detail = text
            task.events.append(text)
            task.events = task.events[-30:]
            return
        if event_type == "artifact_submitted":
            task.artifact_path = str(event.get("artifact_path") or "")
            task.detail = f"submitted {task.artifact_path}" if task.artifact_path else "artifact submitted"
            return
        if event_type in {"task_done", "task_failed", "task_retry"}:
            task.status = {"task_done": "done", "task_failed": "failed", "task_retry": "retry"}[event_type]
            task.attempts = int(event.get("attempt", task.attempts))
            task.run_dir = str(event.get("run_dir") or task.run_dir)
            task.result = str(event.get("result") or task.result)
            task.error = str(event.get("error") or "")
            if task.status == "done":
                task.detail = task.artifact_path or task.result or task.run_dir
            elif task.error:
                task.detail = task.error
            task.finished_monotonic = now

    def move_focus(self, offset: int) -> None:
        ordered = [task.id for task in self.ordered_tasks()]
        if not ordered:
            return
        if self.focus_task_id not in ordered:
            self.focus_task_id = ordered[0]
            return
        index = ordered.index(self.focus_task_id)
        self.focus_task_id = ordered[(index + offset) % len(ordered)]

    def ordered_tasks(self) -> list[TaskProgress]:
        rank = {"running": 0, "retry": 1, "queued": 2, "failed": 3, "todo": 4, "done": 5, "skipped": 6}
        return sorted(self.tasks.values(), key=lambda task: (rank.get(task.status, 9), task.id))

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
                f"run {event.get('run_id') or event.get('work_id') or '(unknown)'} loaded {event.get('total_tasks', 0)} task(s), "
                f"selected {event.get('eligible_tasks', 0)}, concurrency {event.get('concurrency', 1)}"
            )
        elif event_type == "task_started":
            print(f"running {event.get('task_id')} attempt {event.get('attempt')} -> {event.get('run_dir')}")
        elif event_type == "task_done":
            print(f"done {event.get('task_id')} after attempt {event.get('attempt')}")
        elif event_type == "task_retry":
            print(f"retry {event.get('task_id')}: {event.get('error')}")
        elif event_type == "task_failed":
            print(f"failed {event.get('task_id')}: {event.get('error')}")
        elif event_type == "batch_paused":
            print(
                f"paused {event.get('run_id') or event.get('work_id') or '(unknown)'}: "
                f"{event.get('pending_tasks', 0)} pending task(s), {event.get('running_tasks', 0)} running"
            )


def _format_seconds(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{sec:02d}s"
    if minutes:
        return f"{minutes}m{sec:02d}s"
    return f"{sec}s"


def _tail_text(value: str, limit: int) -> str:
    return value[-limit:] if len(value) > limit else value


def _last_nonempty_line(value: str) -> str:
    for line in reversed(value.splitlines()):
        if line.strip():
            return truncate(line.strip(), 120)
    return ""
