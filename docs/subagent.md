# Sub-Agent V1

Evolyn 的主 Agent 可以把一部分独立的调查工作委派给**子代理**执行。子代理
在隔离的上下文里运行、只持有被显式授予的能力，结束后把结构化结果聚合回
主循环。本页说明 V1 的实际行为与边界。

## 设计取向

子代理不是新的 Run，也不进入 Conversation：它是当前工具轮内的一次嵌套
`AgentLoop` 执行，结果以一条普通 ToolResult 回到主循环。这样做的原因：

- Conversation / Run / Checkpoint / Reconciliation 的语义保持不变，
  子代理不产生独立恢复边界（父 Run 中断时，该工具调用按既有语义视为
  「不确定」）；
- 主 Agent 的工作上下文不会被子代理的过程消息污染——只有最终结构化结果
  回流。

## 能力授予（capabilities）

主 Agent 在委派时**显式授予**能力，子代理工具面 = 授予（含降级）
∩ 主 Run 模式白名单 ∩ Active Skill allowed-tools，只能收窄，不能扩大。

| 能力 | 映射工具 | 作用域参数 | PLAN 降级 |
| --- | --- | --- | --- |
| `read_repo[:路径前缀]` | `read_file`、`list_files` | 可选取工作区相对前缀 | 保留 |
| `web_search` | `web_search` | 无 | 保留 |
| `web_fetch:域名[,域名…]` | `http_request` 受限包装 | 必填域名白名单，仅 GET/HEAD | 丢弃 |

- 带参数的能力通过受限包装工具实现：`web_fetch` 校验目标 URL 的 host 与
  method；`read_repo:src/` 校验解析后的工作区路径前缀。
- 能力目录 V1 仅只读三项；能力表结构（解析 → 校验 → scoped registry →
  降级 → 轨迹）可直接扩展（`write_repo`、`run_shell` 属于后续版本）。
- 授予记录写进轨迹：`SUBAGENT_STARTED` 事件同时携带请求值与生效值，
  降级/丢弃可见、可审计。
- 包装工具对模型声明 `permission=ALLOWED`（能力已被主 Agent 显式授予，
  无需再审批）；委托执行发生在共享的工具执行环境里，SSRF 防护、工作区
  边界与既有审批规则照常生效。
- 委派工具 `subagent_run` 本身在 PLAN 模式白名单内（V1 能力目录全为只读，
  委派不产生副作用）；PLAN 下 runner 仍会按同一白名单收窄子代理工具面，
  `web_fetch` 被丢弃并记录 `reason=plan_mode`。

## 隔离

子代理在嵌套 `AgentLoop` 中运行，以下依赖全部为空：

- 无 Checkpoint、无 Memory 自动召回、无 Skill 目录/激活、无 Task 上下文、
  无 Post-Run 反思；子代理无法读写 Task / Skill / Memory；
- 消息历史为空，只接收主 Agent 撰写的自包含任务说明（brief）；
- 子代理内部消息永不进入 Conversation、滚动摘要或主 Agent 上下文。

Memory 注入由主 Agent 决定档位（`memory` 参数）：

| 档位 | 内容 |
| --- | --- |
| `none` | 不注入 |
| `core`（默认） | Core Memory |
| `full` | Core Memory + 本 Run 召回候选（cue 级） |

内容来源是父 Run 已注入的 memory 消息快照（单一事实来源，零二次检索）；
Memory Policy 消息不转发。父上下文没有记忆时如实注入空并记录 `memory=none`。

## 预算与收口

子代理有独立子预算（默认 60k tokens / 8 步 / 8 轮 / 180s 墙钟），
三档阈值复用 RunBudget 机制：

| 阈值 | 行为 |
| --- | --- |
| 80% | 注入提示：「预算即将用尽，基于已有信息给出结论」 |
| 95%（收口） | 只保留 `submit_result` 交付工具，用最后一次调用提交已发现内容与未覆盖范围 |
| 100%（hard） | 强停；返回 `truncated=true` |

子代理的 token 与 model calls **全部计入父 Run 预算**（`RunBudget` 的
token 阈值与调用数硬上限），子代理不可能绕过父 Run 的成本边界。

`truncated=true` 由 harness 事实决定：触发过收口语义，或停止原因为
`max_steps` / `budget` / `timeout`。正常完成且未收口为 `false`。主 Agent
依据 `truncated` 与 `uncovered_scope` 决定继续委派或自行接手。

## 结构化返回（submit_result）

子代理看不到聊天界面，也不能反问用户；它通过一个只存在于子循环的
`submit_result` 工具提交结构化结果：

```json
{
  "conclusions": "结论（必填）",
  "key_files": ["path/to/file.py:123"],
  "snippets": [{"file": "...", "lines": "10-24", "code": "..."}],
  "uncertainties": ["不确定点"],
  "uncovered": ["未覆盖范围（预算收口时填写）"]
}
```

- `submit_result` 是**终局工具**：成功的提交会立即结束子循环，不再多耗
  一次模型调用；同轮多次提交取最后一次。校验失败返回错误结果，模型可
  重试。
- 未按协议提交时：正常终结（final answer）以最终文本降级为结论；预算
  硬停 / 步数耗尽 / 模型错误等异常终结则用回退结论如实标注停止原因与
  错误细节（`submitted=false`），不伪造调查产出。
- 返回给主循环的 ToolResult 为有界 JSON：结构化四件套 +
  `truncated` / `uncovered_scope` / `submitted` / `status` / `grants` /
  `memory` / `usage` / `steps` / `tool_calls` / `subagent_id`。
- 子代理原始工具输出照常归档为 Evidence（归属父 Run），主 Agent 可按需
  用 `evidence_read` 回读细节。

## 并发

- 一轮模型响应中的多个 `subagent_run` 并发执行，上限 **3**（runner 级
  信号量，可配置）；结果按原始调用顺序装配。其余工具保持顺序执行。
- 每个父 Run 的子代理调用上限默认 **4** 次；Run 结束时统一清理计数。

## 观测与轨迹

- 父 Run 只发 `SUBAGENT_STARTED` / `SUBAGENT_COMPLETED` / `SUBAGENT_FAILED`
  摘要事件（携带 subagent_id、请求/生效能力、memory 档位、usage、
  stop_reason、truncated），进入父 Trace、CLI 与 WS 广播；
- 子循环内部全量事件挂独立子 run_id 命名空间（`{父run_id}:sub:{8位}`）
  持久化（Trace 可完整回查），**不广播**，避免客户端对未知 run_id 推导
  run.status；子 run 的终态事件（completed / failed / cancelled）由 runner
  补发，Trace 不会停在 RUNNING；
- 子循环沿用父 run_id 执行工具：审批规则、Run 级清理与 Evidence 归属都在
  父 Run 边界内（`executor.rebind(scoped_registry)` 只替换工具面，不替换
  权限、审批与证据链）；
- 并行工具的用量从结构化结果回流：子代理的 token 与 model_calls 由主循环
  累加进父 Run 的 usage 与预算账本；
- 被拒绝的委派（能力为空、参数非法、配额超限等）不产生 SUBAGENT_* 事件，
  不消耗调用配额，只以工具错误结果返回。

## 边界（V1 非目标）

- 无并行上限提升、无更深嵌套（子代理不能再生子代理）；
- 无独立 Checkpoint / 恢复；
- 能力目录不含写操作与 Shell（`write_repo` / `run_shell` 属后续版本）；
- 不接入 MCP 动态工具；
- 子代理与主 Agent 使用同一 provider/model。

## 配置

环境变量前缀 `SUBAGENT_`（`backend/.env`）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SUBAGENT_ENABLED` | `true` | 关闭后不注册 `subagent_run` |
| `SUBAGENT_MAX_STEPS` | `8` | 单次子代理步数上限 |
| `SUBAGENT_MAX_TOOL_ROUNDS` | `8` | 单次子代理工具轮上限 |
| `SUBAGENT_WALL_CLOCK_SECONDS` | `180` | 单次墙钟上限（外层工具超时 = 墙钟 + 30s 缓冲） |
| `SUBAGENT_TOKEN_BUDGET` | `60000` | 子预算（计入父 Run） |
| `SUBAGENT_MAX_CONCURRENCY` | `3` | 并发子代理上限 |
| `SUBAGENT_MAX_INVOCATIONS_PER_RUN` | `4` | 每 Run 调用次数上限 |
| `SUBAGENT_MAX_RESULT_CHARS` | `8000` | 回流结果上限 |
| `SUBAGENT_CONCLUSION_MAX_CHARS` | `4000` | 单条结论文本上限 |

## 验证

离线测试位于 `backend/tests/offline/subagent/`（能力解析/降级、受限包装
边界、隔离与结构化回流、提交协议与回退、预算三档时序、并发上限与用量
回流、取消传播、Trace / Evidence 持久化、Application 装配）。
