# BatchAgent Batch Task Skill

Use this skill when defining or running a Markdown batch of similar agent tasks that must be parsed, leased, executed, validated, and marked complete without manual task assignment.

## Required Manifest Sections

Create one Markdown file containing:

1. A fenced `batchagent` TOML block.
2. A task table between `<!-- batchagent:tasks-start -->` and `<!-- batchagent:tasks-end -->`.

The table must include these columns:

```markdown
| status | id | kind | input | result | attempts | updated | lease | error |
```

`input` must be a JSON object. Put only per-task variables there. Keep shared instructions and paths in TOML and prompt templates.

## TOML Fields

Minimum:

```toml
version = 1
name = "batch-name"
workspace = "."
workspace_mode = "copy"
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
tools = ["read_file", "write_file", "web_search", "web_fetch", "submit_artifact"]
blocked_path_patterns = [".git", ".git/**", ".batchagent", ".batchagent/**", ".bagent", ".bagent/**", ".env", "**/.env", "**/*.pem", "**/*.key"]
command_clean_env = true

system_prompt = """
Shared role and policy.
"""

user_prompt_template = """
Task id: {{task.id}}
Task kind: {{task.kind}}
Task input: {{task.input}}
Current date: CURR_DATE
"""

[artifact]
require_submit = true
require_artifact_path = true
required_metadata_keys = ["task_id", "status"]
```

`tools` is explicit for the native harness. If omitted or empty, no native tools are loaded. With `require_submit = true`, native execution must include `submit_artifact`; OpenCode/Claude receive it through the injected MCP server.

Prompt templates support `CURR_DATE` and `{{CURR_DATE}}`; both are replaced when a task is dispatched with the current local date in `YYYY-MM-DD` format. Use it for date-sensitive research and status reports.

Add `allowed_command_prefixes` only for commands the agent is allowed to run:

```toml
allowed_command_prefixes = [
  ["python", "D:/tools/orchestrator.py"],
  ["git", "show"]
]
blocked_command_patterns = ["\\bRemove-Item\\b", "\\brm\\b", "\\bgit\\s+(?:reset|clean|checkout)\\b"]
```

In unattended batch mode, avoid `run_command` unless there is a concrete need. Prefer workspace-scoped tools such as `read_file`, `write_file`, `grep_search`, `web_search`, and `web_fetch`.

## Execution Workflow

Use the primary `bagent` executable in new workflows. The legacy `batchagent` executable remains a compatibility alias.

1. Validate the manifest:

```bash
bagent doctor BATCHAGENT.md
```

2. Run a small limit first:

```bash
bagent run BATCHAGENT.md --limit 1
```

3. Inspect status:

```bash
bagent status BATCHAGENT.md
```

4. Run the full batch:

```bash
bagent run BATCHAGENT.md
```

The run command opens the Textual TUI by default. Use `--plain` for line-oriented logs, `--no-progress` for quiet execution, and `--focus <task-id>` to keep one Task visible.

5. List Runs and resume an interrupted Run with the same `run_id`:

```bash
bagent runs BATCHAGENT.md
bagent resume BATCHAGENT.md <run-id>
```

6. Inspect or retry failures:

```bash
bagent failures BATCHAGENT.md
bagent inspect BATCHAGENT.md <task-id> --run-id <run-id>
bagent retry BATCHAGENT.md <task-id> --run-id <run-id>
bagent rerun BATCHAGENT.md <task-id>
```

## Completion Rule

Do not mark a Task complete from natural language alone. The agent must call `submit_artifact`; the harness validates that submission and stores the Attempt result in `~/.bagent/state.sqlite3`.

## Recommended Task Row Shape

For patch analysis:

```markdown
| todo | ce70cbc294f2 | pca | {"patch_file":"patches/0004.patch","base_ref":"abc123","profile":"git"} |  | 0 |  |  |  |
```

The prompt template should expand those fields and instruct the agent to submit metadata such as:

```json
{
  "task_id": "ce70cbc294f2",
  "status": "analyzed",
  "findings_count": 3,
  "high_priority_findings_count": 1,
  "cleaned_worktree": true
}
```
