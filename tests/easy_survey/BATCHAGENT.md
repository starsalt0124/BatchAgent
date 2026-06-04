# Easy Survey Sector Research Batch

```batchagent
version = 1
name = "easy-survey-sector-research"
workspace = "E:/BatchAgent/tests/easy_survey"
workspace_mode = "shared"
run_dir = ".batchagent/runs"
parallel = true
max_concurrency = 4
retries = 1
timeout_seconds = 1200
max_turns = 18

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
blocked_command_patterns = [
  "\\b(?:rm|del|erase|rmdir|rd)\\b",
  "\\bRemove-Item\\b",
  "\\bgit\\s+(?:reset|clean|checkout|switch|merge|rebase|push|commit|tag)\\b"
]
command_clean_env = true
web_timeout_seconds = 15
web_max_chars = 30000

system_prompt = """
你是一个面向二级市场板块的调研 Agent。你只处理分配给你的一个板块。

要求：
1. 使用 web_search 搜索近期公开资料，再用 web_fetch 打开关键来源。优先使用交易所/监管机构、公司公告、券商研报摘要、主流财经媒体、行业协会、官方统计与可靠数据源。
2. 输出必须包含：板块定义、近期走势、主要驱动、风险因素、未来 1-3 个月预期、未来 6-12 个月预期、需要跟踪的指标、来源列表。
3. 走势和预期要区分“确定事实”和“推断判断”，不要把预测写成事实。
4. 每份报告写入任务 input.output_path 指定的 Markdown 文件。
5. 完成后必须调用 submit_artifact，artifact_path 使用同一个 output_path，metadata 至少包含 task_id、sector、status、sources_count。
"""

user_prompt_template = """
任务 ID: {{task.id}}
任务类型: {{task.kind}}
任务输入: {{task.input}}

请调研 input.sector 指定的板块走势和预期。建议围绕 input.search_queries 中的关键词检索。
报告写到 input.output_path。
"""

[artifact]
require_submit = true
require_artifact_path = true
required_metadata_keys = ["task_id", "sector", "status", "sources_count"]
```

<!-- batchagent:tasks-start -->
| status | id | kind | input | result | attempts | updated | lease | error |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| todo | sector-ai-compute | sector-research | {"sector":"AI算力与数据中心","output_path":"reports/sector-ai-compute.md","search_queries":["AI算力 数据中心 板块 走势 预期","AI server data center demand outlook China 2026","GPU 云计算 资本开支 板块"]} |  | 0 |  |  |  |
| todo | sector-semiconductor | sector-research | {"sector":"半导体与国产替代","output_path":"reports/sector-semiconductor.md","search_queries":["半导体 国产替代 板块 走势 预期","China semiconductor industry outlook 2026","晶圆代工 设备 材料 景气度"]} |  | 0 |  |  |  |
| todo | sector-humanoid-robot | sector-research | {"sector":"人形机器人与工业自动化","output_path":"reports/sector-humanoid-robot.md","search_queries":["人形机器人 板块 走势 预期","humanoid robot industry outlook China 2026","工业自动化 机器人 需求 景气度"]} |  | 0 |  |  |  |
| todo | sector-ev-smart-driving | sector-research | {"sector":"新能源车与智能驾驶","output_path":"reports/sector-ev-smart-driving.md","search_queries":["新能源车 智能驾驶 板块 走势 预期","EV smart driving China outlook 2026","汽车电子 端到端 自动驾驶 政策"]} |  | 0 |  |  |  |
| todo | sector-battery-storage | sector-research | {"sector":"锂电与储能","output_path":"reports/sector-battery-storage.md","search_queries":["锂电 储能 板块 走势 预期","battery energy storage outlook China 2026","碳酸锂 电池 储能 价格 需求"]} |  | 0 |  |  |  |
| todo | sector-pv-wind | sector-research | {"sector":"光伏与风电","output_path":"reports/sector-pv-wind.md","search_queries":["光伏 风电 板块 走势 预期","solar wind power China outlook 2026","组件价格 装机 消纳 政策"]} |  | 0 |  |  |  |
| todo | sector-innovative-drug | sector-research | {"sector":"创新药与医疗服务","output_path":"reports/sector-innovative-drug.md","search_queries":["创新药 医疗服务 板块 走势 预期","China innovative drugs biotech outlook 2026","医保谈判 出海 临床 数据 医药"]} |  | 0 |  |  |  |
| todo | sector-defense | sector-research | {"sector":"国防军工","output_path":"reports/sector-defense.md","search_queries":["国防军工 板块 走势 预期","defense industry China outlook 2026","军工 订单 装备 信息化 景气度"]} |  | 0 |  |  |  |
| todo | sector-low-altitude | sector-research | {"sector":"低空经济","output_path":"reports/sector-low-altitude.md","search_queries":["低空经济 板块 走势 预期","low altitude economy China outlook 2026","eVTOL 无人机 空域 政策"]} |  | 0 |  |  |  |
| todo | sector-gold-nonferrous | sector-research | {"sector":"黄金与有色金属","output_path":"reports/sector-gold-nonferrous.md","search_queries":["黄金 有色金属 板块 走势 预期","gold copper nonferrous metals outlook 2026","美联储 利率 铜 铝 金价"]} |  | 0 |  |  |  |
<!-- batchagent:tasks-end -->
