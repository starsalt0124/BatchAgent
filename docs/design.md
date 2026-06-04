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
```

Core modules:

- `batchagent.manifest`: fenced TOML config and Markdown task table parsing/writeback.
- `batchagent.scheduler`: concurrency, leases, retries, stale recovery, status counts.
- `batchagent.agent`: OpenAI-compatible tool-calling loop.
- `batchagent.provider`: DeepSeek/OpenAI-compatible HTTP client.
- `batchagent.tools`: workspace-limited tools exposed to the model.
- `batchagent.validation`: deterministic artifact checks.
- `batchagent.store`: SQLite session, message, tool event, and artifact persistence.

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

The model receives:

- `read_file(path, offset, limit)`
- `list_files(path, pattern, recursive, limit)`
- `write_file(path, content, append)`
- `run_command(command, working_dir, timeout_seconds)` only when allowlisted
- `submit_artifact(summary, artifact_path, metadata)`

All paths are resolved inside the active workspace. Symlink escapes and `..` escapes are rejected.

`run_command` is disabled unless `allowed_command_prefixes` is configured. The model must supply command tokens, not a shell string.

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
- Provider and tool errors are either retried or returned to the model.
- Task timeout is enforced by the scheduler.
- `recover` moves stale `running` tasks back to `retry`, `failed`, or `todo`.
- Manifest writes are atomic and guarded by a lock file.

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

