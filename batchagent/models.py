from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


TERMINAL_STATUSES = {"done", "skipped", "failed"}
ELIGIBLE_STATUSES = {"todo", "retry", "needs-review"}


@dataclass
class ArtifactPolicy:
    require_submit: bool = True
    require_artifact_path: bool = False
    required_metadata_keys: list[str] = field(default_factory=list)
    validator_command: list[str] = field(default_factory=list)
    validator_timeout_seconds: int = 120


@dataclass
class BatchConfig:
    version: int = 1
    name: str = "batchagent"
    workspace: str = "."
    workspace_mode: str = "shared"
    copy_exclude: list[str] = field(default_factory=lambda: [".git", ".batchagent", "__pycache__"])
    run_dir: str = ".batchagent/runs"
    parallel: bool = False
    max_concurrency: int = 1
    retries: int = 0
    timeout_seconds: int = 1800
    max_turns: int = 30
    provider: str = "deepseek"
    model: str = "deepseek-v4-flash"
    base_url: str = "https://api.deepseek.com"
    api_key_env: str = "DEEPSEEK_API_KEY"
    temperature: float = 0.0
    max_tokens: int | None = None
    request_timeout_seconds: int = 120
    provider_retries: int = 2
    reasoning_effort: str = ""
    thinking: str = ""
    system_prompt: str = ""
    user_prompt_template: str = ""
    memory_files: list[str] = field(default_factory=list)
    allowed_command_prefixes: list[list[str]] = field(default_factory=list)
    artifact: ArtifactPolicy = field(default_factory=ArtifactPolicy)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def effective_concurrency(self) -> int:
        if not self.parallel:
            return 1
        return max(1, self.max_concurrency)


@dataclass
class Task:
    status: str
    id: str
    kind: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    result: str = ""
    attempts: int = 0
    updated: str = ""
    error: str = ""
    lease: str = ""

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def is_eligible(self, retry_failed: bool = False) -> bool:
        if self.status in ELIGIBLE_STATUSES:
            return True
        return retry_failed and self.status == "failed"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatMessage:
    role: str
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Manifest:
    path: Path
    text: str
    config: BatchConfig
    tasks: list[Task]
    tasks_start: int
    tasks_end: int


@dataclass
class ArtifactSubmission:
    summary: str
    artifact_path: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentRunResult:
    success: bool
    task_id: str
    run_dir: Path
    artifact_record_path: Path | None = None
    artifact: ArtifactSubmission | None = None
    error: str = ""
