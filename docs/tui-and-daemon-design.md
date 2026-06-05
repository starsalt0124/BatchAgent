# BatchAgent TUI And Daemon Design

## Current Implementation

The current implementation is a single-process runner with a full-screen Rich TUI:

- `python -m batchagent` starts interactive mode, discovers manifests, previews tasks, and asks before execution.
- `python -m batchagent run <manifest>` keeps direct non-interactive execution.
- The scheduler emits structured progress events.
- The TUI consumes those events and renders an alternate-screen dashboard.
- While a task is running, the detail field shows model deltas and tool activity.
- When a task completes, the detail field switches to the artifact path or artifact record.
- The TUI supports task focus with `Up`/`Down`, a task detail page with `Enter`, and return to overview with `Esc`.
- `--plain` and `--no-progress` remain available for logs and automation.

This keeps execution deterministic: disabling the TUI does not change scheduling, tool calls, retries, or manifest writeback.

## Page Model

Recommended navigation model:

1. Batch list page: multiple manifests/batch runs, their state, start/stop/recover actions.
2. Batch run page: current dashboard with task table, progress, ETA, and focused task summary.
3. Task run page: model output tail, tool call timeline, artifact submission, errors, and links to persisted SQLite records.

The current code implements pages 2 and 3 for one running manifest. Interactive startup is the seed for page 1.

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

