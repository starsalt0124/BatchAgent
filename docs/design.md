# BatchAgent Design

## Research Summary

Several existing agent libraries are useful building blocks:

| Library | Useful Capabilities | Gap For This Project |
| --- | --- | --- |
| OpenAI Agents SDK | Agents, tool calls, sessions, tracing, provider integration points | Does not define a Markdown batch manifest, task leases, artifact validation, or manifest writeback. |
| LangGraph | Durable workflow checkpoints, resume, fault tolerance, graph state | Excellent workflow engine, but still needs an outer batch manifest and artifact protocol. |
| Microsoft Agent Framework / Semantic Kernel | Model clients, sessions, context providers, middleware, MCP/tool integration | Broad app framework; batch progress format and submit validation remain app code. |
| AutoGen | Multi-agent chat, agent-as-tool patterns, team orchestration | More conversation-oriented than manifest-driven batch execution. |
| PydanticAI | Typed agents, typed tool validation, retries | Good agent runtime, not a batch scheduler. |
| CrewAI | Crews, tasks, memory | Task abstraction exists, but Markdown manifest leases and artifact verification are still custom. |

The resulting architecture is intentionally a harness around an OpenAI-compatible agent loop. If a later version adopts OpenAI Agents SDK or LangGraph internally, the manifest format and `submit_artifact` contract can remain stable.

## Architecture

```text
Markdown manifest
  -> parser
  -> scheduler
  -> Run (`run_id`)
  -> Run Task (`task_id`)
  -> Task Attempt (`attempt_id`)
  -> native agent loop or local harness subprocess
  -> tool calls
  -> submit_artifact
  -> artifact validator
  -> ~/.bagent SQLite state/event store
  -> progress event stream
  -> TUI run page / plain logs
```

Core modules:

- `batchagent.manifest`: fenced TOML config and Markdown task table parsing/writeback.
- `batchagent.scheduler`: concurrency, leases, retries, stale recovery, status counts.
- `batchagent.agent`: OpenAI-compatible tool-calling loop.
- `batchagent.harness`: native/OpenCode/Claude adapter registry and subprocess lifecycle.
- `batchagent.harness_mcp`: run-scoped `submit_artifact` and progress tools for external harnesses.
- `batchagent.provider`: DeepSeek/OpenAI-compatible HTTP client.
- `batchagent.tools`: workspace-limited tools exposed to the model.
- `batchagent.security`: path, command, and environment safety checks.
- `batchagent.web_tools`: web search and web fetch helpers.
- `batchagent.validation`: deterministic artifact checks.
- `batchagent.store`: SQLite Batch Config, Run, Task, Attempt, usage, message, tool event, and artifact persistence.
- `batchagent.progress`: progress state tracking and plain progress fallback.

## Manifest Contract

The manifest has two parseable areas:

1. A fenced TOML block:

```markdown
```batchagent
version = 1
name = "my-batch"
workspace = "."
workspace_mode = "copy"
parallel = true
max_concurrency = 4
tools = ["read_file", "write_file", "web_search", "web_fetch", "submit_artifact"]
...
```
```

2. A marked Markdown table:

```markdown
<!-- batchagent:tasks-start -->
| status | id | kind | input | result | attempts | updated | lease | error |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| todo | task-1 | patch | {"patch_file":"patches/1.patch"} |  | 0 |  |  |  |
<!-- batchagent:tasks-end -->
```

The marked table remains compatible with earlier manifests and explicit legacy status commands. Normal v2 execution stores runtime truth in SQLite instead of repeatedly rewriting the definition file. Free-form notes outside the markers are preserved.

Terminology:

- Batch Config: the manifest file and task table.
- Run: one invocation or resumed execution of a Batch Config. It has a stable `run_id`.
- Task: one logical item in a Run. It retains its manifest `task_id`.
- Attempt: one execution of a Task. It has a unique `attempt_id`; retry appends an Attempt.

The persisted hierarchy is:

```text
Batch Config -> Run (run_id) -> Task (task_id) -> Attempt (attempt_id)
```

Batch Configs may declare runtime variables:

```toml
run_variables = [
  { name = "market", label = "Market scope", required = true },
  { name = "as_of_date", label = "As-of date", default = "CURR_DATE", required = false }
]
```

Templates can read them as `{{vars.market}}` or `{{run_vars.market}}`. The TUI collects them before `/run`; non-interactive runs use `--var name=value`.

## Task States

The table below describes v1 manifest-cache values. A v2 new Run initializes its own Run Tasks from every definition except `skipped`; resume/retry eligibility comes from `run_tasks` in SQLite.

| State | Meaning | Eligible |
| --- | --- | --- |
| `todo` | Never attempted or manually reset. | Yes |
| `running` | Leased by an active scheduler run. | No |
| `retry` | Failed but still eligible. | Yes |
| `needs-review` | Human or verifier requested another pass. | Yes |
| `done` | Artifact passed validation. | No |
| `skipped` | Intentionally not run. | No |
| `failed` | Retry budget exhausted. | No unless `--retry-failed` |

## Workspace Modes

- `shared`: all tasks operate in the configured workspace.
- `readonly`: read/list allowed; writes are blocked.
- `copy` / `isolated-copy`: each task gets a copied workspace under its run directory.

For concurrent patch-analysis jobs, `copy` is the safer default because many tools mutate checkout state.

## Tool Protocol

Tools are not loaded by default. The model receives only the names listed in `tools = [...]`.

- `read_file(path, offset, limit)`
- `list_files(path, pattern, recursive, limit)`
- `glob(pattern, base_path, limit)`
- `grep_search(query, path, pattern, case_sensitive, limit)`
- `write_file(path, content, append)`
- `edit_file(path, find, replace)`
- `file_edit(path, start_line, end_line, replacement)`
- `delete_file(path)` only for files created by the current Attempt
- `web_search(query, limit, domains)`
- `web_fetch(url, prompt, max_chars)`
- `tool_search(query, limit)`
- `run_command(command, working_dir, timeout_seconds)` only when explicitly loaded and allowlisted
- `submit_artifact(summary, artifact_path, metadata)`

All paths are resolved inside the active workspace. Symlink escapes and `..` escapes are rejected.

`run_command` is disabled unless it is listed in `tools`. In the default `command_policy = "allowlist"` mode, `allowed_command_prefixes` is required. With `command_policy = "blacklist"`, ordinary tokenized commands are allowed unless blocked by high-risk or configured patterns. The model must supply command tokens, not a shell string.

## Safety Strategy

Batch execution has no per-turn human approval, so safety is deny-heavy:

- No tools are exposed unless the manifest lists them.
- Unknown tools fail `doctor` and `run`.
- With the native harness, `artifact.require_submit = true` requires `submit_artifact` to be loaded.
- `blocked_path_patterns` denies sensitive workspace paths such as `.git`, `.env`, and private keys.
- `workspace_mode = "readonly"` blocks write, edit, delete, and command writes.
- `delete_file` can only delete a file created by the same Attempt.
- `run_command` defaults to an exact token-prefix allowlist match; blacklist mode skips the allowlist and relies on blocked patterns plus path checks.
- Delete commands, shell interpreter commands, reboot/format/registry commands, and configured `blocked_command_patterns` are rejected.
- Absolute command path arguments must stay inside the workspace or run directory.
- `command_clean_env = true` avoids leaking API keys and unrelated environment variables into command tools.
- External harnesses receive a minimal environment plus explicit allowlisted names. Probes use a neutral temporary working directory; inline interpreter evaluation, OpenCode `--auto`, and Claude permission-bypass flags are rejected.

## Local Harness Protocol

`native` runs the built-in provider loop. `opencode` and `claude` use argument-array subprocesses with concurrent stdout/stderr consumption, process-group timeout/cancel cleanup, and tolerant JSONL parsing. Each Attempt receives a private MCP spool containing:

- `submit_artifact(summary, artifact_path, metadata)`
- `report_progress(message, percent, metadata)`

The MCP record includes a random nonce plus `run_id`, `task_id`, and `attempt_id`. The parent scheduler validates the identity, artifact path, metadata, and optional validator before it writes the canonical record. External harness session ids remain separate metadata.

## Artifact Protocol

`submit_artifact` writes `artifact.json` in the Attempt directory. The scheduler marks the Attempt and Run Task `done` only after:

- `submit_artifact` was called when required.
- `artifact_path` exists when required.
- required metadata keys exist.
- optional `validator_command` exits with code 0.

Validator commands receive:

- `BATCHAGENT_TASK_ID`
- `BATCHAGENT_RUN_DIR`
- `BATCHAGENT_WORKSPACE`
- `BATCHAGENT_ARTIFACT_PATH`

## Persistence Schema

The single `~/.bagent/state.sqlite3` database contains `batch_configs`, `batch_runs`, `run_tasks`, `task_attempts`, `model_calls`, `messages`, `tool_events`, and `artifacts`. Timing, provider/external usage, results, errors, harness versions, process ids, and external session ids survive TUI restarts. External stream logs live beside the Attempt as `stdout.jsonl` and `stderr.log`. The default Attempt directory is `~/.bagent/runs/<run-id>/<task-id>/<attempt-id>/`, and `BAGENT_HOME` relocates the entire layout. Older manifest-local state databases are imported through a read-only, idempotent migration with collision-safe identifier mapping.

## Reliability Strategy

- A Run row and frozen Task snapshot are written before scheduling, so an empty or immediately paused Run is still visible.
- Every retry creates a unique Attempt row and directory; IDs and prior results are never replaced.
- SQLite records timing, usage, messages, tool events, results, and artifacts.
- Scheduler emits progress events for Run loading plus queued, running, retry, done, and failed Tasks/Attempts.
- The TUI run page is a subscriber to those events; disabling UI does not change execution semantics.
- Provider and tool errors are either retried or returned to the model.
- Task timeout is enforced by the scheduler.
- `resume` marks orphaned running Attempts interrupted and schedules only eligible Tasks in the same Run.
- The manifest remains a portable definition; SQLite is the runtime source of truth.

## Failure Recovery

- `failures` lists failed tasks and prints recovery commands.
- `runs` lists persisted Runs for one Batch Config.
- `inspect` reads one Run Task and its Attempts, usage, tool events, artifacts, and recent messages.
- `resume` continues queued/interrupted Tasks under the same `run_id`.
- `recover --run-id` explicitly marks a stale active Run and its orphan Attempts interrupted before resume.
- `retry --run-id` appends an Attempt to one failed Task.
- `rerun` creates a new Run and preserves the earlier Run and Attempts.
- `run --only <task-id>` executes one selected task, and `run --focus <task-id>` keeps it visible in the TUI run page.

## Patch Compatibility Example Shape

Each patch row can store only the variable fields:

```json
{
  "patch_id": "ce70cbc294f2",
  "patch_file": "patches/0004-setup-stop-using-the_repository.patch",
  "base_ref": "parent-commit",
  "profile": "git"
}
```

The shared PCA instructions, repo paths, command allowlist, and artifact requirements belong in the TOML config and prompt template. This avoids duplicating large prompts per row.
