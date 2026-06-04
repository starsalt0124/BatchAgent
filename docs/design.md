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
  -> per-task lease
  -> agent loop
  -> tool calls
  -> submit_artifact
  -> artifact validator
  -> manifest writeback
  -> SQLite session/event store
  -> progress event stream
  -> Rich dashboard / plain logs
```

Core modules:

- `batchagent.manifest`: fenced TOML config and Markdown task table parsing/writeback.
- `batchagent.scheduler`: concurrency, leases, retries, stale recovery, status counts.
- `batchagent.agent`: OpenAI-compatible tool-calling loop.
- `batchagent.provider`: DeepSeek/OpenAI-compatible HTTP client.
- `batchagent.tools`: workspace-limited tools exposed to the model.
- `batchagent.security`: path, command, and environment safety checks.
- `batchagent.web_tools`: web search and web fetch helpers.
- `batchagent.validation`: deterministic artifact checks.
- `batchagent.store`: SQLite session, message, tool event, and artifact persistence.
- `batchagent.progress`: cross-platform Rich dashboard and plain progress fallback.

## Manifest Contract

The manifest has two machine-owned areas:

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

Only the marked table is rewritten during execution. Free-form notes outside the markers are preserved.

## Task States

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
- `delete_file(path)` only for files created by the current task run
- `web_search(query, limit, domains)`
- `web_fetch(url, prompt, max_chars)`
- `tool_search(query, limit)`
- `run_command(command, working_dir, timeout_seconds)` only when explicitly loaded and allowlisted
- `submit_artifact(summary, artifact_path, metadata)`

All paths are resolved inside the active workspace. Symlink escapes and `..` escapes are rejected.

`run_command` is disabled unless `allowed_command_prefixes` is configured. The model must supply command tokens, not a shell string.

## Safety Strategy

Batch execution has no per-turn human approval, so safety is deny-heavy:

- No tools are exposed unless the manifest lists them.
- Unknown tools fail `doctor` and `run`.
- If `artifact.require_submit = true`, `submit_artifact` must be loaded.
- `blocked_path_patterns` denies sensitive workspace paths such as `.git`, `.env`, and private keys.
- `workspace_mode = "readonly"` blocks write, edit, delete, and command writes.
- `delete_file` can only delete a file created by the same task run.
- `run_command` requires an exact token-prefix allowlist match.
- Delete commands, shell interpreter commands, reboot/format/registry commands, and configured `blocked_command_patterns` are rejected.
- Absolute command path arguments must stay inside the workspace or run directory.
- `command_clean_env = true` avoids leaking API keys and unrelated environment variables into command tools.

## Artifact Protocol

`submit_artifact` writes `artifact.json` in the run directory. The scheduler marks the task `done` only after:

- `submit_artifact` was called when required.
- `artifact_path` exists when required.
- required metadata keys exist.
- optional `validator_command` exits with code 0.

Validator commands receive:

- `BATCHAGENT_TASK_ID`
- `BATCHAGENT_RUN_DIR`
- `BATCHAGENT_WORKSPACE`
- `BATCHAGENT_ARTIFACT_PATH`

## Reliability Strategy

- The manifest is updated before each task starts, so interrupted tasks are visible as `running`.
- Each run has a unique lease id.
- SQLite records runs, messages, tool events, and artifacts.
- Scheduler emits progress events for loaded, queued, running, retry, done, and failed tasks.
- The Rich dashboard is a subscriber to those events; disabling UI does not change execution semantics.
- Provider and tool errors are either retried or returned to the model.
- Task timeout is enforced by the scheduler.
- `recover` moves stale `running` tasks back to `retry`, `failed`, or `todo`.
- Manifest writes are atomic and guarded by a lock file.

## Failure Recovery

- `failures` lists failed tasks and prints recovery commands.
- `inspect` reads SQLite state for one task, including run attempts, tool events, artifacts, and recent messages.
- `retry` marks failed or selected tasks as `retry` while preserving attempts unless `--reset-attempts` is used.
- `rerun` resets selected tasks to `todo`, clears result/error, and resets attempts.
- `run --only <task-id>` executes one selected task, and `run --focus <task-id>` keeps it visible in the dashboard.

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
