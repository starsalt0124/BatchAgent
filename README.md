# BatchAgent

BatchAgent is a Markdown-driven harness for running repeated agent tasks from a parseable task list. It is intended for workloads like patch analysis, dataset item review, issue triage, and other batch jobs where each item has the same structure and the harness must keep reliable progress.

## Why not only use an agent framework?

Existing frameworks cover important parts of the stack, but not the whole batch control plane:

- OpenAI Agents SDK: strong provider/tool/session/tracing primitives.
- LangGraph: durable workflow execution, checkpointing, human-in-the-loop, and replay.
- AutoGen/Semantic Kernel/CrewAI/PydanticAI: useful multi-agent abstractions, tool wrappers, and memory variants.

BatchAgent focuses on the missing outer harness: Markdown task manifests, durable Runs, concurrency control, per-task workspace isolation, artifact submission, validation, retries, and complete execution provenance. The built-in runtime is OpenAI-compatible, and local coding harnesses can execute the same Tasks without changing the manifest contract.

Terminology:

- Batch Config: the Markdown manifest and logical Task definitions. It can have many Runs.
- Run (`run_id`): one invocation or resumed execution of a Batch Config. It freezes the selected Tasks, runtime variables, and harness.
- Task (`task_id`): one logical manifest item inside a Run.
- Attempt (`attempt_id`): one execution of a Task. Retry creates a new Attempt without overwriting earlier output.

## Install

Python 3.11+ is required. Package installation installs the Rich and Textual dependencies.

`bagent` is the primary command-line executable. The legacy `batchagent` executable remains available as a compatibility alias; the Python package name remains `batchagent`.

Use `bagent --version` to verify the installed CLI. The TUI displays the same version in its always-visible Header.

For a source checkout, use an editable install so the console entry point always loads the current working tree:

```bash
uv pip install --python .venv/bin/python --editable .
```

```powershell
bagent --help
```

On Linux/macOS:

```bash
bagent --help
```

## DeepSeek Provider

The default provider is DeepSeek's OpenAI-compatible Chat Completions API:

- `base_url = "https://api.deepseek.com"`
- `model = "deepseek-v4-flash"`
- `api_key_env = "DEEPSEEK_API_KEY"`

Do not put API keys in manifests. Set the key in your shell:

```powershell
$env:DEEPSEEK_API_KEY = "..."
bagent models
```

```bash
export DEEPSEEK_API_KEY="..."
bagent models
```

## Manifest Format

A manifest is normal Markdown with one fenced TOML block and one marked task table.

```toml
version = 1
name = "patch-compat"
workspace = "D:/pcia_skill/repo/git"
workspace_mode = "copy" # shared | readonly | copy
run_dir = "~/.bagent/runs"
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
run_variables = [
  { name = "market", label = "Market scope", required = true },
  { name = "as_of_date", label = "As-of date", default = "CURR_DATE", required = false }
]

blocked_path_patterns = [
  ".git",
  ".git/**",
  ".batchagent",
  ".batchagent/**",
  ".bagent",
  ".bagent/**",
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
- `{{vars.some_name}}` or `{{run_vars.some_name}}`, supplied when a Run starts
- `{{workspace}}`
- `CURR_DATE` or `{{CURR_DATE}}`, replaced at task dispatch time with the current local date in `YYYY-MM-DD` format

When `run_variables` is configured, the TUI asks for those values before creating a Run. Non-interactive runs can pass them with `--var name=value`.

The manifest status columns remain readable as a v1 compatibility cache. Durable Run, Task, and Attempt status is stored in `~/.bagent/state.sqlite3` and is the source of truth shown by the TUI. A new Run selects every definition except rows explicitly marked `skipped`; use `--only` or `--limit` to narrow it.

Legacy manifest-cache statuses:

- `todo`: eligible.
- `running`: leased by the current scheduler.
- `retry`: eligible after a failed attempt.
- `needs-review`: eligible for another agent pass.
- `done`, `skipped`, `failed`: terminal by default.

## Commands

Run without arguments to enter the full-screen Textual TUI. BatchAgent will discover `BATCHAGENT.md` files and keep a command input at the bottom of the screen.

```powershell
bagent
```

TUI layout:

- Left sidebar: discovered batch manifests plus the current selected batch.
- Top panel: current page and selected batch context.
- Center table: Batch Config list, Run list, or the selected Run's Task list.
- Lower detail: focused Task/Attempt output or page-specific help.
- Bottom candidate area: command and argument suggestions while typing.
- Bottom input: command entry.

Core TUI commands:

```text
/show_batch <manifest-path>
/show_run <run-id>
/run [manifest-path] [--only task-id] [--harness name]
/resume <run-id>
/show_task <task-id>
/history [run-id]
/retry <task-id>
/rerun <task-id>
/harness [use|reset|doctor] [built-in|opencode|claudecode|codex]
/theme [name]
/refresh
/quit
```

Type `/` in the TUI command input to show commands with usage examples and descriptions. Use `Up` / `Down` to select a candidate and `Tab` to accept it. Selecting a Batch Config first shows its Run list. Selecting a Run then shows its Tasks. Opening a Task shows every Attempt and the selected Attempt's messages, tools, usage, result, and artifacts.

`/show_task <task-id>` opens an independent detail window in the selected Run. It shows live output while active and durable Attempt history after a restart. Press `Esc` to close the window. Theme changes made through `/theme` or Textual's theme picker are saved in `~/.bagent/settings.json`.

You can also start the same TUI with an explicit manifest:

```powershell
bagent tui tests\date_survey\BATCHAGENT.md
```

Create a demo manifest:

```powershell
bagent init BATCHAGENT.md
```

Validate the manifest:

```powershell
bagent doctor BATCHAGENT.md
```

Create a Run:

```powershell
bagent run BATCHAGENT.md --limit 2
```

Every `/run` creates a new `run_id`. Every Task execution gets a unique `attempt_id` and a directory under `~/.bagent/runs/<run-id>/<task-id>/<attempt-id>/` by default. `/resume` keeps the existing `run_id`; `/retry` adds an Attempt to the selected Task; `/rerun` creates a new Run. Earlier directories, artifacts, and SQLite records are never overwritten.

`run` opens the same Textual TUI as `bagent tui` and starts the Run automatically. The live page shows the Run id, Task status, current Attempt id, elapsed time, ETA, model/tool output, and artifact submission.

The TUI uses the terminal alternate screen, so the running UI owns the terminal while active instead of filling scrollback. During execution, each task's `Detail` column shows the model output/tool activity tail; after completion it shows the submitted artifact path or run artifact record. Use `--plain` or `--no-progress` for non-interactive logs and automation.

Useful run options:

```powershell
bagent run BATCHAGENT.md --focus task-id
bagent run BATCHAGENT.md --only task-id
bagent run BATCHAGENT.md --var market=A-share
bagent run BATCHAGENT.md --harness opencode
bagent run BATCHAGENT.md --plain
bagent run BATCHAGENT.md --no-progress
```

Run page keys:

- `Up` / `Down`: move task focus.
- `Enter`: open the focused task detail page.
- `Esc`: return to overview.

Show progress:

```powershell
bagent status BATCHAGENT.md
```

List and resume durable Runs:

```powershell
bagent runs BATCHAGENT.md
bagent resume BATCHAGENT.md run-0123456789ab
```

If the owning process crashed and left a Run marked `running`, recover it explicitly before resuming:

```powershell
bagent recover BATCHAGENT.md --run-id run-0123456789ab
bagent resume BATCHAGENT.md run-0123456789ab
```

Legacy v1 manifest-cache recovery:

```powershell
bagent recover BATCHAGENT.md --to retry
```

Inspect and recover failures:

```powershell
bagent failures BATCHAGENT.md
bagent inspect BATCHAGENT.md task-id --run-id run-0123456789ab
bagent retry BATCHAGENT.md task-id --run-id run-0123456789ab
bagent rerun BATCHAGENT.md task-id
```

`retry` keeps the Run and Task identity, then creates a new `attempt_id`. `rerun` creates a separate Run containing that Task. Neither operation deletes old output.

## Local Harnesses

The default `built-in` harness uses BatchAgent's OpenAI-compatible provider loop. `opencode`, `claudecode`, and `codex` execute the Task through an installed local CLI in a background subprocess while the TUI remains responsive. Persisted values `native` and `claude` remain supported aliases for compatibility.

```text
/harness
/harness doctor opencode
/harness use opencode
/harness reset
```

Entering `/harness` without arguments opens a keyboard-selectable table with `built-in`, `opencode`, `claudecode`, and `codex`. The table shows availability, detected version, executable, and a literal `CURRENT` marker for the selected harness. Press Enter on an available row to select it. The sidebar also shows the current harness on every TUI page.

The selection is saved in `~/.bagent/settings.json` and passed explicitly to each new Run started from the TUI. Resume uses the Run snapshot even if the selected harness, manifest prompt, or harness command changes later; `resume --harness ...` is an intentional override. For non-interactive CLI runs without `--harness`, a manifest can set its default with a string or table:

```toml
harness = "opencode"

# Advanced form:
# [harness]
# name = "claude"
# model = "sonnet"
# inject_tools = true
# env_allowlist = ["ANTHROPIC_API_KEY"]
```

For OpenCode, Claude Code, and Codex, BatchAgent injects a run-scoped MCP server with `submit_artifact` and `report_progress`. Codex runs with its `workspace-write` sandbox and receives MCP overrides only for that process, so the user's global Codex config is not modified. BatchAgent never silently enables OpenCode `--auto`, Claude permission-bypass flags, or Codex sandbox/approval bypass flags; inline shell/interpreter command prefixes are also rejected. Probes run with a minimal environment in a neutral temporary directory. The external CLI keeps its own authentication and session id; those are never confused with `run_id` or `attempt_id`.

## Tools Exposed to Agents

Native-harness tools are explicitly loaded by the manifest `tools` list. If `tools` is omitted or empty, no BatchAgent-native tools are exposed. With the native harness, `[artifact].require_submit = true` requires `submit_artifact`; external harnesses receive their completion tool through the injected MCP server.

- `read_file`: read UTF-8 files inside the workspace.
- `list_files`: list files inside the workspace.
- `glob`: find files by glob pattern inside the workspace.
- `grep_search`: search text files inside the workspace.
- `write_file`: write UTF-8 files unless `workspace_mode = "readonly"`.
- `edit_file`: replace exact text in a workspace file.
- `file_edit`: replace a 1-based line range in a workspace file.
- `delete_file`: delete only files created by the same Attempt.
- `web_search`: search the public web through DuckDuckGo HTML.
- `web_fetch`: fetch a web page and return readable text.
- `tool_search`: search known BatchAgent tool schemas.
- `run_command`: available when loaded. By default it requires `allowed_command_prefixes`; set `command_policy = "blacklist"` to allow ordinary commands while rejecting high-risk patterns.
- `submit_artifact`: required completion signal by default.

All Batch Config registrations, Runs, Tasks, Attempts, timing, token usage, external session ids, messages, tool events, results, and artifacts are indexed in `~/.bagent/state.sqlite3`. External stdout/stderr are also streamed to `stdout.jsonl` and `stderr.log` in the Attempt directory, so output survives a harness crash. `BAGENT_HOME` overrides the entire state root—including Run directories—for automation and tests. The home directory and settings/database files use private permissions where the operating system supports them. When an older manifest-local `run_dir/state.sqlite3` is found, its history is imported read-only and idempotently into the global database.

## Safety Defaults

- File tools resolve paths inside the configured workspace and reject path escapes.
- `blocked_path_patterns` denies sensitive workspace paths such as `.git`, `.env`, private keys, and manifest state.
- `workspace_mode = "readonly"` disables all write tools.
- `delete_file` only deletes files created by that same Attempt.
- `run_command` accepts token arrays, never shell strings.
- `run_command` defaults to exact prefix matching in `allowed_command_prefixes`; with `command_policy = "blacklist"`, commands are allowed unless rejected by high-risk or configured blocked patterns.
- Delete commands, shell interpreter commands, registry/system commands, reboot/format commands, and configured `blocked_command_patterns` are rejected before execution.
- Command arguments that are absolute paths must stay inside the workspace or the Attempt directory.
- `command_clean_env = true` prevents command tools from inheriting provider API keys and unrelated environment variables.
- External harness subprocesses receive a minimal environment plus explicit `[harness].env_allowlist` names. Their native file/shell tools follow that harness's permission system, so review its project configuration before unattended execution.

## Failure Handling

- A task is marked `running` before agent execution.
- Each provider message, tool event, and artifact is persisted.
- Tool errors are returned to the model so it can recover.
- If the model stops without `submit_artifact`, the task fails.
- Artifact metadata and paths are validated before the Attempt is marked `done`.
- Failed tasks retry up to `retries`; otherwise they become `failed`.
- Interrupted Attempts become retryable when the same Run is resumed.
