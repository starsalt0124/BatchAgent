# BatchAgent TUI And Daemon Design

## Current Implementation

The current implementation is a single-process runner with a full-screen Textual TUI:

- `python -m batchagent` starts a persistent full-screen TUI.
- `python -m batchagent tui <manifest>` starts the same TUI with a selected manifest.
- `python -m batchagent run <manifest>` keeps direct non-interactive execution.
- The bottom command input accepts `/show_batch`, `/run`, `/show_task`, `/history`, `/retry`, `/rerun`, `/refresh`, and `/quit`.
- Typing `/` opens command candidates with usage examples and descriptions.
- `Up` / `Down` selects a command candidate and `Tab` accepts it.
- Completion covers the current command, discovered manifest, option, or task id from the selected manifest.
- The left sidebar is batch context only: discovered manifests and the current selected batch.
- The top panel repeats the selected batch so every page has explicit batch context.
- `/show_task <task-id>` opens an independent task detail modal. It shows live progress when available and falls back to persisted SQLite messages, tool events, artifacts, and errors for prior runs.
- The scheduler emits structured progress events.
- The TUI consumes those events and renders manifest, batch, run, and task pages.
- While a task is running, the detail field shows model deltas and tool activity.
- When a task completes, the detail field switches to the artifact path or artifact record.
- Each task attempt creates a unique `.batchagent/runs/<task>-<run_id>` directory. Re-running a batch updates the latest manifest row but keeps previous run directories, artifacts, and SQLite history.
- `--plain` and `--no-progress` remain available for logs and automation.

This keeps execution deterministic: disabling the TUI does not change scheduling, tool calls, retries, or manifest writeback.

## Page Model

Recommended navigation model:

1. Batch list page: multiple manifests/batch runs, their state, start/stop/recover actions.
2. Batch run page: current dashboard with task table, progress, ETA, and focused task summary.
3. Task run page: model output tail, tool call timeline, artifact submission, errors, and links to persisted SQLite records.
4. History page: persisted run records for all tasks or one selected task, including run id, attempt, status, timestamps, run directory, and error.

The current code implements these pages for local manifests in one process. The next daemon version should lift the same page model over multiple concurrent server-owned runs.

## Server/Client Singleton Direction

A full singleton server/client design is useful once BatchAgent needs multiple concurrent batch runs and late-attaching terminals.

Recommended shape:

- First `batchagent` process becomes a local daemon/server and owns the state store.
- Later `batchagent` invocations become clients that connect to the server.
- The server manages multiple batch run processes, run locks, cancellation, recovery, and persisted event streams.
- Clients render TUI pages, request new batch starts, attach to existing runs, inspect task details, and send retry/rerun/cancel commands.
- Transport can start with local TCP on `127.0.0.1` or a Unix/Windows named pipe, with an auth token stored in `.batchagent/server.json`.
- State should remain in SQLite so clients can reconnect even if the UI exits.

This should be implemented after the event stream stabilizes. The current event schema is intentionally server-compatible: events already describe batch loading, task start/retry/done/fail, model deltas, tool calls, and artifact submission.

## Safety Notes

The server must never make tool approval interactive per model turn in unattended batch mode. It should keep the current manifest-level tool allowlist, command allowlist, command blacklist, workspace path policy, clean environment policy, and artifact validation.
