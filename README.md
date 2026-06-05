# BatchAgent

BatchAgent is a Markdown-driven harness for running repeated agent tasks from a parseable task list. It is intended for workloads like patch analysis, dataset item review, issue triage, and other batch jobs where each item has the same structure and the harness must keep reliable progress.

## Why not only use an agent framework?

Existing frameworks cover important parts of the stack, but not the whole batch control plane:

- OpenAI Agents SDK: strong provider/tool/session/tracing primitives.
- LangGraph: durable workflow execution, checkpointing, human-in-the-loop, and replay.
- AutoGen/Semantic Kernel/CrewAI/PydanticAI: useful multi-agent abstractions, tool wrappers, and memory variants.

BatchAgent focuses on the missing outer harness: Markdown task manifests, parseable statuses, concurrency control, per-task workspace isolation, artifact submission, validation, retries, and manifest writeback. The internal provider is OpenAI-compatible, so it can be swapped for a framework-backed agent runtime later without changing the manifest contract.

## Install

Python 3.11+ is required. No third-party Python packages are required.

```powershell
python -m batchagent --help
```

On Linux/macOS:

```bash
python -m batchagent --help
```

## DeepSeek Provider

The default provider is DeepSeek's OpenAI-compatible Chat Completions API:

- `base_url = "https://api.deepseek.com"`
- `model = "deepseek-v4-flash"`
- `api_key_env = "DEEPSEEK_API_KEY"`

Do not put API keys in manifests. Set the key in your shell:

```powershell
$env:DEEPSEEK_API_KEY = "..."
python -m batchagent models
```

```bash
export DEEPSEEK_API_KEY="..."
python -m batchagent models
```

## Manifest Format

A manifest is normal Markdown with one fenced TOML block and one marked task table.

```toml
version = 1
name = "patch-compat"
workspace = "D:/pcia_skill/repo/git"
workspace_mode = "copy" # shared | readonly | copy
run_dir = ".batchagent/runs"
parallel = true
max_concurrency = 4
retries = 1
timeout_seconds = 1800
max_turns = 30

provider = "deepseek"
base_url = "https://api.deepseek.com"
api_key_env = "DEEPSEEK_API_KEY"
model = "deepseek-v4-flash"
temperature = 0
tools = [
  "read_file",
  "write_file",
  "web_search",
  "web_fetch",
  "submit_artifact"
]

blocked_path_patterns = [
  ".git",
  ".git/**",
  ".batchagent",
  ".batchagent/**",
  ".env",
  "**/.env",
  "**/*.pem",
  "**/*.key"
]

system_prompt = """
You are a patch compatibility analysis agent.
"""

user_prompt_template = """
Analyze one patch.
Task id: {{task.id}}
Input: {{task.input}}
"""

allowed_command_prefixes = [
  ["python", "D:/pcia_skill/patch-compatibility-analyzer/scripts/orchestrator.py"],
  ["git", "show"]
]
blocked_command_patterns = [
  "\\bRemove-Item\\b",
  "\\brm\\b",
  "\\bgit\\s+(?:reset|clean|checkout)\\b"
]
command_clean_env = true

[artifact]
require_submit = true
require_artifact_path = true
required_metadata_keys = ["task_id", "status"]
validator_command = ["python", "scripts/validate_artifact.py", "{artifact_path}"]
validator_timeout_seconds = 120
```

Task rows live between markers:

```markdown
<!-- batchagent:tasks-start -->
| status | id | kind | input | result | attempts | updated | lease | error |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| todo | ce70cbc294f2 | pca | {"patch_file":"patches/0004.patch","base_ref":"abc123"} |  | 0 |  |  |  |
<!-- batchagent:tasks-end -->
```

Prompt templates support both structured placeholders and a small set of built-ins:

- `{{task.id}}`, `{{task.kind}}`, `{{task.input.some_key}}`
- `{{config.some_key}}`
- `{{workspace}}`
- `CURR_DATE` or `{{CURR_DATE}}`, replaced at task dispatch time with the current local date in `YYYY-MM-DD` format

Statuses:

- `todo`: eligible.
- `running`: leased by the current scheduler.
- `retry`: eligible after a failed attempt.
- `needs-review`: eligible for another agent pass.
- `done`, `skipped`, `failed`: terminal by default.

## Commands

Run without arguments to enter interactive mode. BatchAgent will discover `BATCHAGENT.md` files, show the task list, and ask before starting execution.

```powershell
python -m batchagent
```

Create a demo manifest:

```powershell
python -m batchagent init BATCHAGENT.md
```

Validate the manifest:

```powershell
python -m batchagent doctor BATCHAGENT.md
```

Run tasks:

```powershell
python -m batchagent run BATCHAGENT.md --limit 2
```

`run` uses a Rich live dashboard by default when Rich is installed. The dashboard shows loaded, eligible, running, completed, failed, elapsed time, ETA, and the concrete running task ids.

The dashboard uses the terminal alternate screen, so the running UI owns the terminal while active instead of filling scrollback. During execution, each task's `Detail` column shows the model output/tool activity tail; after completion it shows the submitted artifact path or run artifact record.

Useful run options:

```powershell
python -m batchagent run BATCHAGENT.md --focus task-id
python -m batchagent run BATCHAGENT.md --only task-id --retry-failed
python -m batchagent run BATCHAGENT.md --plain
python -m batchagent run BATCHAGENT.md --no-progress
```

Dashboard keys:

- `Up` / `Down`: move task focus.
- `Enter`: open the focused task detail page.
- `Esc`: return to overview.

Show progress:

```powershell
python -m batchagent status BATCHAGENT.md
```

Recover interrupted leases:

```powershell
python -m batchagent recover BATCHAGENT.md --to retry
```

Inspect and recover failures:

```powershell
python -m batchagent failures BATCHAGENT.md
python -m batchagent inspect BATCHAGENT.md task-id
python -m batchagent retry BATCHAGENT.md task-id
python -m batchagent run BATCHAGENT.md --only task-id
python -m batchagent rerun BATCHAGENT.md task-id
python -m batchagent run BATCHAGENT.md --only task-id
```

`retry` keeps the attempt counter by default and marks a failed task as `retry`. `rerun` resets status to `todo`, clears result/error, and resets attempts to `0`.

## Tools Exposed to Agents

Tools are explicitly loaded by the manifest `tools` list. If `tools` is omitted or empty, no tools are exposed to the model. If `[artifact].require_submit = true`, `submit_artifact` must be listed.

- `read_file`: read UTF-8 files inside the workspace.
- `list_files`: list files inside the workspace.
- `glob`: find files by glob pattern inside the workspace.
- `grep_search`: search text files inside the workspace.
- `write_file`: write UTF-8 files unless `workspace_mode = "readonly"`.
- `edit_file`: replace exact text in a workspace file.
- `file_edit`: replace a 1-based line range in a workspace file.
- `delete_file`: delete only files created by the same task run.
- `web_search`: search the public web through DuckDuckGo HTML.
- `web_fetch`: fetch a web page and return readable text.
- `tool_search`: search known BatchAgent tool schemas.
- `run_command`: only available when loaded and `allowed_command_prefixes` is configured.
- `submit_artifact`: required completion signal by default.

Every task run is saved in SQLite under `run_dir/state.sqlite3`, and each run has a folder with `task.json` and `artifact.json` when submitted.

## Safety Defaults

- File tools resolve paths inside the configured workspace and reject path escapes.
- `blocked_path_patterns` denies sensitive workspace paths such as `.git`, `.env`, private keys, and manifest state.
- `workspace_mode = "readonly"` disables all write tools.
- `delete_file` only deletes files created by that same task run.
- `run_command` accepts token arrays, never shell strings.
- `run_command` requires an exact prefix match in `allowed_command_prefixes`.
- Delete commands, shell interpreter commands, registry/system commands, reboot/format commands, and configured `blocked_command_patterns` are rejected before execution.
- Command arguments that are absolute paths must stay inside the workspace or the task run directory.
- `command_clean_env = true` prevents command tools from inheriting provider API keys and unrelated environment variables.

## Failure Handling

- A task is marked `running` before agent execution.
- Each provider message, tool event, and artifact is persisted.
- Tool errors are returned to the model so it can recover.
- If the model stops without `submit_artifact`, the task fails.
- Artifact metadata and paths are validated before the manifest is marked `done`.
- Failed tasks retry up to `retries`; otherwise they become `failed`.
- Interrupted `running` tasks can be moved back with `recover`.
