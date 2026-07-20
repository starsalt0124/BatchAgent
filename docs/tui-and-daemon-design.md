# BatchAgent TUI And Daemon Design

## Current Implementation

The current implementation is a single-process runner with a full-screen Textual TUI:

- `bagent` starts a persistent full-screen TUI.
- `bagent tui <manifest>` starts the same TUI with a selected manifest.
- `bagent run <manifest>` opens the TUI with the manifest selected and starts `/run` automatically.
- The bottom command input accepts `/show_batch`, `/show_run`, `/run`, `/resume`, `/show_task`, `/history`, `/retry`, `/rerun`, `/harness`, `/theme`, `/refresh`, and `/quit`.
- Typing `/` opens command candidates with usage examples and descriptions.
- `Up` / `Down` selects a command candidate and `Tab` accepts it.
- Completion covers commands, manifest paths, Run ids, Task ids, harness names, themes, and options in the current context.
- The left sidebar is batch context only: discovered manifests and the current selected batch.
- Clicking a Batch Config first opens its Run list. Clicking a Run opens its Task list. Clicking a Task opens the same Attempt-aware detail modal as `/show_task <task-id>`.
- The top panel repeats the selected batch so every page has explicit batch context.
- `/show_task <task-id>` opens an independent detail modal. It lists every `attempt_id` in the selected Run and shows the chosen Attempt's timing, usage, messages, tool events, artifacts, result, and errors.
- `Ctrl+C` is left for terminal/input copy behavior; exiting the TUI is `Ctrl+Q`, `/quit`, or `/exit`.
- If a Batch Config declares `run_variables`, `/run` opens a runtime-variable modal before creating the Run.
- The scheduler emits structured progress events.
- The TUI consumes those events and renders manifest, batch, run, and task pages.
- While a task is running, the detail field shows model deltas and tool activity.
- When a task completes, the detail field switches to the artifact path or artifact record.
- The hierarchy is Batch Config -> Run (`run_id`) -> Task (`task_id`) -> Attempt (`attempt_id`). Resume retains the Run id, retry appends an Attempt, and rerun creates another Run. Runtime data lives under `~/.bagent` by default.
- `/harness` persists the default local runtime, and `/theme` plus Textual's theme picker persist color selection in `~/.bagent/settings.json`.
- `--plain` and `--no-progress` remain available for logs and automation.

This keeps execution deterministic: disabling the TUI does not change scheduling, tool calls, retries, artifact validation, or SQLite state transitions.

## Page Model

Recommended navigation model:

1. Batch Config list: discovered manifests and latest state.
2. Run list: persisted Runs for the selected Batch Config, including status, harness, duration, tokens, and result.
3. Run Task page: Task status, Attempt count, duration, tokens, result, live progress, and resume/retry actions.
4. Task detail: immutable Attempt ids, timing, model/tool timeline, artifact submission, external session id, and errors.

The current code implements these pages for local manifests in one process. The next daemon version should lift the same page model over multiple concurrent server-owned runs.

## Server/Client Singleton Direction

A full singleton server/client design is useful once BatchAgent needs multiple concurrent batch runs and late-attaching terminals.

Recommended shape:

- First `bagent` process becomes a local daemon/server and owns the state store.
- Later `bagent` invocations become clients that connect to the server.
- The server manages multiple batch run processes, run locks, cancellation, recovery, and persisted event streams.
- Clients render TUI pages, request new batch starts, attach to existing runs, inspect task details, and send retry/rerun/cancel commands.
- Transport can start with local TCP on `127.0.0.1` or a Unix/Windows named pipe, with an auth token stored in `~/.bagent/server.json`.
- State should remain in SQLite so clients can reconnect even if the UI exits.

This should be implemented after the event stream stabilizes. The current event schema is intentionally server-compatible: events already describe batch loading, task start/retry/done/fail, model deltas, tool calls, and artifact submission.

## Safety Notes

The server must never make tool approval interactive per model turn in unattended batch mode. It should keep the current manifest-level tool allowlist, command allowlist, command blacklist, workspace path policy, clean environment policy, and artifact validation.
