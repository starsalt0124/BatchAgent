from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from .models import ArtifactPolicy, BatchConfig, Manifest, Task
from .util import atomic_write_text


CONFIG_RE = re.compile(r"```batchagent\s*\n(?P<body>.*?)\n```", re.DOTALL)
TASKS_START = "<!-- batchagent:tasks-start -->"
TASKS_END = "<!-- batchagent:tasks-end -->"
STANDARD_COLUMNS = ["status", "id", "kind", "input", "result", "attempts", "updated", "lease", "error"]


class ManifestError(ValueError):
    pass


def load_manifest(path: str | Path) -> Manifest:
    manifest_path = Path(path)
    text = manifest_path.read_text(encoding="utf-8")
    config = _parse_config(text)
    start = text.find(TASKS_START)
    end = text.find(TASKS_END)
    if start < 0 or end < 0 or end <= start:
        raise ManifestError(f"missing task table markers: {TASKS_START} / {TASKS_END}")
    table_text = text[start + len(TASKS_START) : end]
    tasks = _parse_tasks_table(table_text)
    return Manifest(manifest_path, text, config, tasks, start, end)


def save_manifest(manifest: Manifest) -> None:
    table = render_tasks_table(manifest.tasks)
    new_text = (
        manifest.text[: manifest.tasks_start + len(TASKS_START)]
        + "\n"
        + table
        + "\n"
        + manifest.text[manifest.tasks_end :]
    )
    atomic_write_text(manifest.path, new_text)
    manifest.text = new_text
    manifest.tasks_end = manifest.tasks_start + len(TASKS_START) + 1 + len(table) + 1


def render_tasks_table(tasks: list[Task]) -> str:
    lines = [
        "| " + " | ".join(STANDARD_COLUMNS) + " |",
        "| " + " | ".join("---" for _ in STANDARD_COLUMNS) + " |",
    ]
    for task in tasks:
        values = {
            "status": task.status,
            "id": task.id,
            "kind": task.kind,
            "input": json.dumps(task.input, ensure_ascii=False, separators=(",", ":")) if task.input else "",
            "result": task.result,
            "attempts": str(task.attempts),
            "updated": task.updated,
            "lease": task.lease,
            "error": task.error,
        }
        lines.append("| " + " | ".join(_escape_cell(values[column]) for column in STANDARD_COLUMNS) + " |")
    return "\n".join(lines)


def create_sample_manifest(path: str | Path) -> None:
    manifest_path = Path(path)
    if manifest_path.exists():
        raise FileExistsError(str(manifest_path))
    sample = """# BatchAgent Demo

```batchagent
version = 1
name = "demo"
workspace = "."
workspace_mode = "shared"
run_dir = ".batchagent/runs"
parallel = true
max_concurrency = 2
retries = 1
timeout_seconds = 300
max_turns = 12
provider = "deepseek"
base_url = "https://api.deepseek.com"
api_key_env = "DEEPSEEK_API_KEY"
model = "deepseek-v4-flash"
temperature = 0
tools = ["write_file", "submit_artifact"]

system_prompt = \"\"\"
You are a batch task agent. Complete exactly one assigned task.
Use submit_artifact when the task is complete. For this demo, write a short result file under outputs/.
\"\"\"

user_prompt_template = \"\"\"
Task id: {{task.id}}
Task kind: {{task.kind}}
Task input: {{task.input}}

If kind is echo, write outputs/{{task.id}}.txt containing the input message, then call submit_artifact with artifact_path set to outputs/{{task.id}}.txt and metadata containing task_id.
\"\"\"

[artifact]
require_submit = true
require_artifact_path = true
required_metadata_keys = ["task_id"]
```

<!-- batchagent:tasks-start -->
| status | id | kind | input | result | attempts | updated | lease | error |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| todo | demo-1 | echo | {"message":"first task"} |  | 0 |  |  |  |
| todo | demo-2 | echo | {"message":"second task"} |  | 0 |  |  |  |
<!-- batchagent:tasks-end -->
"""
    manifest_path.write_text(sample, encoding="utf-8")


def _parse_config(text: str) -> BatchConfig:
    match = CONFIG_RE.search(text)
    if not match:
        raise ManifestError("missing fenced ```batchagent TOML config block")
    raw = tomllib.loads(match.group("body"))
    artifact_raw = raw.get("artifact", {})
    artifact = ArtifactPolicy(
        require_submit=bool(artifact_raw.get("require_submit", True)),
        require_artifact_path=bool(artifact_raw.get("require_artifact_path", False)),
        required_metadata_keys=list(artifact_raw.get("required_metadata_keys", [])),
        validator_command=list(artifact_raw.get("validator_command", [])),
        validator_timeout_seconds=int(artifact_raw.get("validator_timeout_seconds", 120)),
    )
    return BatchConfig(
        version=int(raw.get("version", 1)),
        name=str(raw.get("name", "batchagent")),
        workspace=str(raw.get("workspace", ".")),
        workspace_mode=str(raw.get("workspace_mode", "shared")),
        copy_exclude=list(raw.get("copy_exclude", [".git", ".batchagent", "__pycache__"])),
        run_dir=str(raw.get("run_dir", ".batchagent/runs")),
        parallel=bool(raw.get("parallel", False)),
        max_concurrency=int(raw.get("max_concurrency", 1)),
        retries=int(raw.get("retries", 0)),
        timeout_seconds=int(raw.get("timeout_seconds", 1800)),
        max_turns=int(raw.get("max_turns", 30)),
        provider=str(raw.get("provider", "deepseek")),
        model=str(raw.get("model", "deepseek-v4-flash")),
        base_url=str(raw.get("base_url", "https://api.deepseek.com")),
        api_key_env=str(raw.get("api_key_env", "DEEPSEEK_API_KEY")),
        temperature=float(raw.get("temperature", 0.0)),
        max_tokens=int(raw["max_tokens"]) if "max_tokens" in raw else None,
        request_timeout_seconds=int(raw.get("request_timeout_seconds", 120)),
        provider_retries=int(raw.get("provider_retries", 2)),
        reasoning_effort=str(raw.get("reasoning_effort", "")),
        thinking=str(raw.get("thinking", "")),
        system_prompt=str(raw.get("system_prompt", "")),
        user_prompt_template=str(raw.get("user_prompt_template", "")),
        memory_files=list(raw.get("memory_files", [])),
        tools=list(raw.get("tools", [])),
        allowed_command_prefixes=[_command_prefix(prefix) for prefix in raw.get("allowed_command_prefixes", [])],
        blocked_command_patterns=list(raw.get("blocked_command_patterns", [])),
        blocked_path_patterns=list(
            raw.get(
                "blocked_path_patterns",
                [".git", ".git/**", ".batchagent", ".batchagent/**", ".env", "**/.env", "**/*.pem", "**/*.key", "**/id_rsa", "**/id_ed25519"],
            )
        ),
        command_clean_env=bool(raw.get("command_clean_env", True)),
        web_timeout_seconds=int(raw.get("web_timeout_seconds", 15)),
        web_max_chars=int(raw.get("web_max_chars", 20000)),
        artifact=artifact,
        raw=raw,
    )


def _parse_tasks_table(table_text: str) -> list[Task]:
    rows = [line.strip() for line in table_text.splitlines() if line.strip().startswith("|")]
    if len(rows) < 2:
        return []
    headers = [_normalize_header(cell) for cell in _split_row(rows[0])]
    missing = {"status", "id"} - set(headers)
    if missing:
        raise ManifestError(f"task table missing required columns: {', '.join(sorted(missing))}")
    tasks: list[Task] = []
    for row in rows[2:]:
        cells = _split_row(row)
        values = {headers[i]: _unescape_cell(cells[i]) if i < len(cells) else "" for i in range(len(headers))}
        if not values.get("id"):
            continue
        task_input = _parse_input(values.get("input", ""))
        tasks.append(
            Task(
                status=values.get("status", "todo").strip() or "todo",
                id=values.get("id", "").strip(),
                kind=values.get("kind", "").strip(),
                input=task_input,
                result=values.get("result", "").strip(),
                attempts=int(values.get("attempts", "0") or 0),
                updated=values.get("updated", "").strip(),
                error=values.get("error", "").strip(),
                lease=values.get("lease", "").strip(),
            )
        )
    return tasks


def _parse_input(value: str) -> dict[str, Any]:
    value = value.strip()
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"task input must be JSON object: {value}") from exc
    if not isinstance(parsed, dict):
        raise ManifestError(f"task input must be JSON object: {value}")
    return parsed


def _split_row(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    cells: list[str] = []
    current: list[str] = []
    for ch in row:
        if ch == "|":
            if current and current[-1] == "\\":
                current[-1] = "|"
                continue
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    cells.append("".join(current).strip())
    return cells


def _escape_cell(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")


def _unescape_cell(value: str) -> str:
    return value.replace("<br>", "\n").replace("\\|", "|")


def _normalize_header(value: str) -> str:
    return value.strip().lower().replace(" ", "_").replace("-", "_")


def _command_prefix(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [item for item in value.split(" ") if item]
    raise ManifestError(f"allowed_command_prefixes entries must be list or string: {value!r}")
