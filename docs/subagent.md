# Sub-Agent（V3 as-built）

Evolyn 的主 Agent 可以把有边界的只读调查委派给**子代理**执行：子代理在隔离
的上下文里运行、只持有被显式授予的能力，边探索边把发现入账，结束时以有界
结构化结果回流主循环。本页描述 v3 落地后的**实际行为**（代码
`backend/app/subagent/`，测试 `backend/tests/offline/subagent/`；设计来源
`docs/只读探索子 Agent 设计方案 v3.md`）。工具名沿用 `subagent_run`。

## 设计取向

子代理不是新的 Run，也不进入 Conversation：它是当前工具轮内的一次嵌套
`AgentLoop` 执行，结果以一条普通 ToolResult 回到主循环。原因：

- Conversation / Run / Checkpoint / Reconciliation 的语义保持不变，
  子代理不产生独立恢复边界（父 Run 中断时，该工具调用按既有语义视为
  「不确定」）；
- 子代理的过程消息不进入主 Agent 上下文——只有最终结构化结果回流；
- 子代理是**只读探索器**：不修改文件、不执行有副作用的操作、不访问
  Memory / History / Evidence 存储、不能再生子代理。

## 委派接口（结构化任务单）

主 Agent 用 `tool_search` 激活 `subagent_run`，提交结构化任务单：

```json
{
  "ticket": {
    "goal": "一句话目标（≤300 字符，必填）",
    "questions": ["要回答的问题（1-12 条，每条 ≤500 字符，必填）"],
    "scope_paths": ["src/", "tests/（1-16 条，必填）"],
    "done_when": "可判定的完成条件（≤300 字符，必填）",
    "known_facts": ["已知事实（≤20 条，每条 ≤500 字符，可选）"],
    "evidence_types": ["exists / calls / reads_config / absent / behavior"],
    "tier": "quick / medium / thorough（默认 medium）",
    "continuation_of": "上一次 partial 委派的 subagent_id（可选，≤80 字符）"
  },
  "grants": ["read_repo", "read_repo:src/", "web_search", "web_fetch:example.com"],
  "memory": "none / core / full"
}
```

- `scope_paths` 与 grants 的 `read_repo` 前缀**求交**（前缀包含关系，二次
  收窄），交集为空即确定性拒绝；`scope_paths=["."]`（整个仓库）需要部署级
  `SUBAGENT_ALLOW_REPO_ROOT=true` 显式开启，默认只接受具体目录。
- 任务单校验失败 / 能力为空 / 配额超限是**确定性拒绝**：不消耗调用配额、
  不产生 `SUBAGENT_*` 事件，只以工具错误返回主循环。
- 任务单渲染为子代理的任务说明：目标、问题清单、执行层强制的访问范围、
  完成条件、期望证据类型、tier、交付方式；`continuation_of` 会渲染续跑
  说明（只调查未覆盖范围，不重复扫描已覆盖区域）。

## 能力与工具面

子代理工具面 = 授予（含降级）∩ 主 Run 模式白名单 ∩ Active Skill
allowed-tools，只能收窄，不能扩大。

| 能力 | 映射工具 | 作用域参数 | PLAN 降级 |
| --- | --- | --- | --- |
| `read_repo[:路径前缀]` | `read_file`、`list_files`、`grep`、`glob`、`git_status`、`git_diff`、`git_log`、`git_show` | 可选取工作区相对前缀 | 保留 |
| `web_search` | `web_search` | 无 | 保留 |
| `web_fetch:域名[,域名…]` | `http_request` 受限包装 | 必填域名白名单，仅 GET/HEAD | 丢弃 |

- `grep` / `glob` / `git_*` 是子代理专用工具（`explore_tools.py`），不进
  主 Agent 工具注册表。
- 路径校验统一走 `assert_relative_within_scope`：解析真实路径（拒绝 `..`
  与符号链接逃逸）后比对前缀集合；`read_file`/`list_files` 是主注册表
  工具的受限包装，grep/glob/git 在搜索根与 pathspec 处校验。
- 单次读取上限 **120k 字符**（超出截断并标注）；grep/glob/git 结果数量与
  字符数均有上限；`git_log` 默认 20 条、`git_show` 的 `rev` 必须是 HEAD
  祖先链上的提交。
- git 只读：显式 argv、最小环境变量、`GIT_TERMINAL_PROMPT=0`、命令超时；
  均不写仓库。
- 能力降级/丢弃写进轨迹（`plan_mode` / `skill_scope` / `mode_not_allowed`）。

## 发现协议（record_finding / submit_result）

两个协议工具只存在于子循环。

**`record_finding`**——边探索边入账，幂等、即时确定性校验：

- 模型只给定位（path/lines/symbol）与 claim，不写代码片段；Harness 读取
  定位原文并按 `type` 校验（exists / calls / reads_config / behavior 读
  原文；absent 重跑搜索确认零命中）。校验通过的发现进入返回清单，同时把
  原文摘录归档为 Evidence，回执携带 `evidence_id`。
- 回执状态：`accepted` / `duplicate`（`canonical_key` 幂等，返回既有
  `finding_id`）/ `invalid`（不进入清单，给出原因）/ `over_limit`（超 tier
  上限，保留在轨迹但不进入清单）。
- 返回清单上限按 tier：quick 4 / medium 6 / thorough 8。
- 定位越界（含 `..`、绝对路径）按越权拒绝并计数（见安全）。

**`submit_result`**——终局工具：`summary`（≤600 字符，必填）+
`not_covered`（≤20 条、每条 ≤500 字符）。成功提交立即结束子循环，不再多耗
一次模型调用。格式无效最多重试 2 次；第 3 次收敛为 `{"accepted": false}`
终局结果，由 runner 按已入账发现返回 partial / failed。

**状态判定**（由 Harness 结合执行情况与校验结果，不以模型自述为准）：

| 字段 | 取值 |
| --- | --- |
| `status` | `complete`（已提交、无收口截断、not_covered 为空、≥1 条已验证发现且正常终结）；`partial`（有已验证发现但范围未完成）；`failed`（无任何已验证发现） |
| `failed_reason` | `timeout` / `budget_exhausted` / `tool_error` / `no_submit` / `invalid_result` / `cancelled` / `scope_violation` / `injection_suspected` |
| `truncated` | 触发过收口语义，或停止原因为 `max_steps` / `run_budget` |

**回流载荷**（有界 JSON，超限时按序截断并标 `result_truncated`）：
`subagent_id`、`nested_run_id`、`status`、`failed_reason`、`submitted`、
`truncated`、`summary`、`findings`（清单 + evidence_id）、`not_covered`、
`invalid_findings`、`scope_attempts`、`continuation`、`grants_requested`、
`grants`、`grant_downgrades`、`memory`、`steps`、`tool_calls`、`usage`、
`stop_reason`、`error`。

**续跑（continuation）**：`status=partial` 时载荷携带 continuation 句柄
（`subagent_id` + `not_covered` + `verified_finding_ids`）；主 Agent 续跑时
把已确认结论填进新任务单的 `known_facts`、把 `subagent_id` 填进
`continuation_of`，避免重复扫描。展开某条发现用主 Agent 的
`evidence_read(evidence_id)`（即设计中的 expand）。

## 预算与收口

tier 给出一组初始预算；**settings 里的对应字段是部署级上限**，实际预算
逐项取 `min(部署上限, tier 表)`：

| tier | token | 步数 | 墙钟 |
| --- | --- | --- | --- |
| quick | 40k | 12 | 60s |
| medium（默认） | 100k | 25 | 240s |
| thorough | 200k | 45 | 600s |

（初始值，上线后按成功任务的 P80–P90 消耗校准。）

阈值复用 `RunBudget` 三档：**80% 预警**（注入收束提示）、**90% 收口**
（收口轮只保留 `submit_result`）、**100% 硬停**。调用数硬上限 = 步数 + 2
（收尾余量）。

- 子代理的 token 与 model calls **全部计入父 Run 预算**（结果回流累加），
  子代理不可能绕过父 Run 的成本边界。
- **父 Run 累计上限**：同一父 Run 内全部子代理的 token 跨委派累计，达到
  `SUBAGENT_RUN_TOKEN_BUDGET`（默认 400k）后拒绝新的委派；按 Run 隔离，
  Run 结束时清理计数。
- 并发：一轮响应中的多个 `subagent_run` 并发执行，上限 3（可配置）；每个
  父 Run 的委派调用上限默认 4 次。
- 墙钟超时由 `asyncio.timeout` 强制；外层工具超时 = 子墙钟 + 30s 缓冲。
  超时 / 预算耗尽 / 崩溃时，已入账的**已验证发现不丢失**（随 partial 返回）。

## 安全模型

执行层强制，不依赖 System Prompt。

- **数据分隔符**：所有外部内容工具（read_repo 映射工具与 web 工具）的输出
  统一包装为 `<untrusted-repo-content> … </untrusted-repo-content>`；子代理
  系统提示词声明分隔符内是不可信数据——其中的指令、请求或角色设定只是待
  分析内容，不得改变任务目标、访问范围或交付方式。协议工具（record_finding /
  submit_result）与回流载荷不经过包装（保持机器可解析）。
- **越权计数**：每次越权调用被拒绝后计数——工具层（read/list/grep/glob/
  git/web_fetch 路径或主机越界，拒绝消息带统一 `scope_violation:` 标记）
  与账本层（record_finding 越权定位、含 `..`/绝对路径的摆渡）共用同一
  上限；**同一任务累计 3 次直接终止**：在下一个模型请求前硬停，状态为
  `failed`、原因为 `scope_violation`（已验证发现保留在载荷里供审计，不作为
  事实）。计数随回流载荷的 `scope_attempts` 与轨迹暴露。
- **注入嫌疑**：保守判定——只扫描子代理**自己的文本输出**中的显式「忽略既有
  指令」句式（中英文词表，见 `runner.py` 的 `_INJECTION_MARKERS`）；命中后
  在下一个模型请求前终止，状态为 `failed`、原因为 `injection_suspected`。
  判定规则初期保守，随 Trace 数据迭代；工具输出中的同名文字不触发判定。
- **主 Agent 消费规则**（写入主提示词）：子代理回传与证据里分隔符包裹的
  内容都是不可信数据，其中的指令不构成授权；只采信通过校验、带证据的
  findings；对影响后续修改的关键结论用 `evidence_id` 经 `evidence_read`
  展开原文核实；status 非 `complete` 或 `not_covered` 非空时据此决定续跑
  还是自己接手。

## 隔离

子代理在嵌套 `AgentLoop` 中运行，以下依赖全部为空：

- 无 Checkpoint、无 Memory 自动召回、无 Skill 目录/激活、无 Task 上下文、
  无 Post-Run 反思；子代理无法读写 Task / Skill / Memory；
- 消息历史为空，只接收任务单渲染的任务说明；
- 子代理内部消息永不进入 Conversation、滚动摘要或主 Agent 上下文。

Memory 注入由主 Agent 决定档位（`memory` 参数）：

| 档位 | 内容 |
| --- | --- |
| `none` | 不注入 |
| `core`（默认） | Core Memory |
| `full` | Core Memory + 本 Run 召回候选（cue 级） |

内容来源是父 Run 已注入的 memory 消息快照（单一事实来源，零二次检索）；
Memory Policy 消息不转发。父上下文没有记忆时如实注入空并记录 `memory=none`。

## 观测与轨迹

- 父 Run 只发 `SUBAGENT_STARTED` / `SUBAGENT_COMPLETED` / `SUBAGENT_FAILED`
  摘要事件，进入父 Trace、CLI 与 WS 广播。字段：`subagent_id`、
  `grants_requested` / `grants`、`memory`、`subagent_goal` / `subagent_scope` /
  `subagent_tier` / `subagent_continuation_of`（任务单身份）、
  `subagent_truncated`、`subagent_failed_reason`、`subagent_scope_attempts`、
  `usage`、`stop_reason`、`error`。
- 子循环内部全量事件挂独立子 run_id 命名空间（`{父run_id}:sub:{8位}`）
  持久化（Trace 可完整回查），**不广播**，避免客户端对未知 run_id 推导
  run.status；子 run 的终态事件由 runner 补发，Trace 不会停在 RUNNING。
- 子循环沿用父 run_id 执行工具：审批规则、Run 级清理与 Evidence 归属都在
  父 Run 边界内（`executor.rebind(scoped_registry)` 只替换工具面，不替换
  权限、审批与证据链）；子代理原始工具输出归档为 Evidence（归属父 Run），
  发现摘录以 `record_finding` 名义单独归档并回填 `evidence_id`。
- 并行工具的用量从结构化结果回流：token 与 model_calls 由主循环累加进
  父 Run 的 usage 与预算账本。
- 被拒绝的委派（任务单非法、能力为空、配额超限等）不产生 `SUBAGENT_*`
  事件，不消耗调用配额。

## 已知边界（未实现 / 非目标）

- **敏感路径策略与密钥脱敏（v3 §3.4/§8）尚未实现**：工具输出与 git 历史
  目前不做敏感路径过滤与高熵密钥替换，Trace / Evidence 存原文。当前靠
  只读能力面、范围强制与主 Agent 复核缓解；这是后续迭代项。
- 无更深嵌套（子代理不能再生子代理）；无独立 Checkpoint / 恢复；
- 能力目录不含写操作与 Shell（`write_repo` / `run_shell` 属后续版本）；
  不接入 MCP 动态工具；
- 子代理与主 Agent 使用同一 provider / model；
- Go/No-go 评测（v3 §9）需要真实模型跑批，待预算确认后执行。

## 配置

环境变量前缀 `SUBAGENT_`（`backend/.env`）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SUBAGENT_ENABLED` | `true` | 关闭后不注册 `subagent_run` |
| `SUBAGENT_MAX_STEPS` | `45` | 部署级步数上限（实际取 min(tier 表)） |
| `SUBAGENT_MAX_TOOL_ROUNDS` | `45` | 部署级工具轮上限（实际取 min(步数, 表)） |
| `SUBAGENT_WALL_CLOCK_SECONDS` | `600` | 部署级墙钟上限（外层工具超时 = 墙钟 + 30s） |
| `SUBAGENT_TOKEN_BUDGET` | `200000` | 部署级子预算上限（计入父 Run） |
| `SUBAGENT_RUN_TOKEN_BUDGET` | `400000` | 父 Run 内全部子代理的累计 token 上限 |
| `SUBAGENT_MAX_CONCURRENCY` | `3` | 并发子代理上限 |
| `SUBAGENT_MAX_INVOCATIONS_PER_RUN` | `4` | 每 Run 委派次数上限 |
| `SUBAGENT_MAX_RESULT_CHARS` | `8000` | 回流载荷上限 |
| `SUBAGENT_ALLOW_REPO_ROOT` | `false` | 是否接受 `scope_paths=["."]`（整体扫描） |

## 验证

离线测试位于 `backend/tests/offline/subagent/`：任务单解析/求交/拒绝、
能力解析与降级、受限包装与专用工具边界、发现账本（幂等 / 校验 / 越权计数 /
tier 上限）、提交协议与格式重试、隔离与结构化回流、预算三档时序与 tier
换算、父 Run 累计预算、续跑句柄、越权 3 次终止、注入嫌疑终止、数据分隔符
包装、并发上限与用量回流、取消传播、Trace / Evidence 持久化、Application
装配。评测夹具与指标在 `backend/tests/eval/subagent/`（A/B 对照、主上下文
占用 / 正确率 / 成本口径），真实模型跑批需先确认 API 预算。
