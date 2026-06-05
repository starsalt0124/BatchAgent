# Date-Aware Survey Batch

```batchagent
version = 1
name = "date-aware-sector-survey"
workspace = "E:/BatchAgent/tests/date_survey"
workspace_mode = "shared"
run_dir = ".batchagent/runs"
parallel = true
max_concurrency = 2
retries = 1
timeout_seconds = 1200
max_turns = 16

provider = "deepseek"
base_url = "https://api.deepseek.com"
api_key_env = "DEEPSEEK_API_KEY"
model = "deepseek-v4-flash"
temperature = 0
tools = ["web_search", "web_fetch", "write_file", "submit_artifact"]

blocked_path_patterns = [
  ".git",
  ".git/**",
  ".batchagent",
  ".batchagent/**",
  ".env",
  "**/.env",
  "**/*.pem",
  "**/*.key",
  "**/id_rsa",
  "**/id_ed25519"
]
command_clean_env = true
web_timeout_seconds = 15
web_max_chars = 30000

system_prompt = """
你是一个日期敏感的行业调研 Agent。当前日期是 CURR_DATE。

时间规则：
1. 所有“近期、当前、最新、未来 1-3 个月、未来 6-12 个月”都必须以 CURR_DATE 为参考点。
2. 不要把 CURR_DATE 之后的事件写成已经发生；若来源发布时间早于 CURR_DATE 很久，要明确其时效性限制。
3. 报告必须区分已验证事实、来源观点和你的推断。
4. 每个任务只调研 input.topic 指定主题，报告写入 input.output_path。
5. 完成后必须调用 submit_artifact，artifact_path 使用 input.output_path，metadata 至少包含 task_id、topic、status、current_date、sources_count。
"""

user_prompt_template = """
当前日期：CURR_DATE
任务 ID：{{task.id}}
任务类型：{{task.kind}}
任务输入：{{task.input}}

请围绕 input.topic 做一份日期敏感调研。建议使用 input.search_queries 检索，并打开关键来源核验。

报告结构：
- 主题定义
- 截至 CURR_DATE 的近期走势
- 关键驱动
- 未来 1-3 个月预期
- 未来 6-12 个月预期
- 主要风险和反证信号
- 需要持续跟踪的指标
- 来源列表，带 URL 和日期/时效性说明
"""

[artifact]
require_submit = true
require_artifact_path = true
required_metadata_keys = ["task_id", "topic", "status", "current_date", "sources_count"]
```

<!-- batchagent:tasks-start -->
| status | id | kind | input | result | attempts | updated | lease | error |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| todo | survey-ai-infra | date-aware-research | {"topic":"AI基础设施与数据中心","output_path":"reports/survey-ai-infra.md","search_queries":["AI infrastructure data center outlook current trends","AI算力 数据中心 当前 走势 预期","hyperscaler capex AI data center 2026"]} |  | 0 |  |  |  |
| todo | survey-semiconductor-cycle | date-aware-research | {"topic":"半导体周期与先进制程","output_path":"reports/survey-semiconductor-cycle.md","search_queries":["semiconductor cycle outlook current trends","半导体 周期 先进制程 当前 预期","foundry memory semiconductor outlook 2026"]} |  | 0 |  |  |  |
| todo | survey-biotech-innovation | date-aware-research | {"topic":"创新药与生物科技","output_path":"reports/survey-biotech-innovation.md","search_queries":["biotech innovative drugs outlook current trends","创新药 生物科技 当前 走势 预期","China biotech licensing deals outlook 2026"]} |  | 0 |  |  |  |
| todo | survey-energy-transition | date-aware-research | {"topic":"能源转型与电力设备","output_path":"reports/survey-energy-transition.md","search_queries":["energy transition power equipment outlook current trends","能源转型 电力设备 当前 走势 预期","grid storage renewable power equipment outlook 2026"]} |  | 0 |  |  |  |
<!-- batchagent:tasks-end -->

