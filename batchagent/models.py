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
class RunVariable:
    name: str
    label: str = ""
    default: str = ""
    required: bool = True


@dataclass
class HarnessConfig:
    """Configuration for the runtime that executes one task attempt.

    ``provider`` remains the model API used by the built-in/native harness.  A
    harness is deliberately a separate concept because external CLIs own their
    own model, authentication, sessions, and tools.
    """

    name: str = "native"
    command: list[str] = field(default_factory=list)
    extra_args: list[str] = field(default_factory=list)
    model: str = ""
    agent: str = ""
    inject_tools: bool = True
    env_allowlist: list[str] = field(default_factory=list)


@dataclass
class BatchConfig:
    version: int = 1
    name: str = "batchagent"
    workspace: str = "."
    workspace_mode: str = "shared"
    copy_exclude: list[str] = field(default_factory=lambda: [".git", ".batchagent", ".bagent", "__pycache__"])
    run_dir: str = "~/.bagent/runs"
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
    dialog_recovery_attempts: int = 5
    reasoning_effort: str = ""
    thinking: str = ""
    system_prompt: str = ""
    user_prompt_template: str = ""
    run_variables: list[RunVariable] = field(default_factory=list)
    run_vars: dict[str, Any] = field(default_factory=dict)
    memory_files: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    skill_roots: list[str] = field(default_factory=list)
    task_selector_script: str = ""
    task_selector_command: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    command_policy: str = "allowlist"
    allowed_command_prefixes: list[list[str]] = field(default_factory=list)
    blocked_command_patterns: list[str] = field(default_factory=list)
    blocked_path_patterns: list[str] = field(
        default_factory=lambda: [
            ".git",
            ".git/**",
            ".batchagent",
            ".batchagent/**",
            ".env",
            "**/.env",
            "**/*.pem",
            "**/*.key",
            "**/id_rsa",
            "**/id_ed25519",
        ]
    )
    command_clean_env: bool = True
    web_timeout_seconds: int = 15
    web_max_chars: int = 20000
    harness: HarnessConfig = field(default_factory=HarnessConfig)
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
    work_id: str = ""
    run_id: str = ""
    attempt_id: str = ""
    harness: str = "native"
    usage: dict[str, Any] = field(default_factory=dict)
    artifact_record_path: Path | None = None
    artifact: ArtifactSubmission | None = None
    error: str = ""
