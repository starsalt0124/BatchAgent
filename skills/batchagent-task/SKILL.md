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
tools = ["read_file", "write_file", "web_search", "web_fetch", "submit_artifact"]
blocked_path_patterns = [".git", ".git/**", ".batchagent", ".batchagent/**", ".env", "**/.env", "**/*.pem", "**/*.key"]
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

`tools` is explicit. If omitted or empty, no tools are loaded. If `require_submit = true`, `submit_artifact` must be included.

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

1. Validate the manifest:

```bash
python -m batchagent doctor BATCHAGENT.md
```

2. Run a small limit first:

```bash
python -m batchagent run BATCHAGENT.md --limit 1
```

3. Inspect status:

```bash
python -m batchagent status BATCHAGENT.md
```

4. Run the full batch:

```bash
python -m batchagent run BATCHAGENT.md
```

The run command uses a Rich live dashboard when available. Use `--plain` for line-oriented logs, `--no-progress` for quiet execution, and `--focus <task-id>` to keep one task visible in the dashboard.

5. If interrupted, recover stale leases:

```bash
python -m batchagent recover BATCHAGENT.md --to retry
```

6. Inspect or retry failures:

```bash
python -m batchagent failures BATCHAGENT.md
python -m batchagent inspect BATCHAGENT.md <task-id>
python -m batchagent retry BATCHAGENT.md <task-id>
python -m batchagent run BATCHAGENT.md --only <task-id>
python -m batchagent rerun BATCHAGENT.md <task-id>
```

## Completion Rule

Do not mark a task complete from natural language alone. The agent must call `submit_artifact`; the harness validates that submission and updates the manifest.

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
