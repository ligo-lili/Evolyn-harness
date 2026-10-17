# 只读探索子 Agent 阶段 0 · A/B 评测报告

- 生成时间：2026-10-17T08:47:37+00:00
- 模型：deepseek / deepseek-v4-flash
- 任务数：4；每任务轮数：1
- 运行根目录：`C:\Users\cjy\AppData\Local\Temp\evolyn-subagent-eval-5y1hrn_c`
- 复现命令：`C:\agents\Evolyn\backend\tests\eval\subagent\run_live.py --arm both --runs 1 --task L02-deprecated-unused --task L04-checkout-call-chain --task L05-import-time-connections --task T09-unused-config --provider deepseek --model deepseek-v4-flash --price-input 0.14 --price-output 0.28 --allow-no-go --out tests\eval\subagent\reports\trial_large_20261017.md`

## 判定：**No-go**

| 判定项 | 通过 | 详情 |
| --- | --- | --- |
| 上下文峰值降幅 | ❌ | A 均值 8,694 → B 均值 9,474，降幅 -9.0%（要求 ≥ 50%；口径：主 Run 最大 prompt token，逐任务先折叠再跨任务平均） |
| 正确率下降幅度 | ✅ | 关键结论正确率 A 64.3% → B 100.0%，下降 -35.7pp（要求 ≤ 5pp；严格通过率 A 50% / B 100%，仅作参考） |
| 安全关键错误 | ✅ | 安全子集关键错误 A 0 → B 0（要求 B ≤ A） |
| 总成本比值 | ✅ | 口径 API 成本（USD）：A 0.1099 → B 0.1104，比值 1.00（要求 ≤ 1.3） |

> 联合判定：三项门槛（上下文、正确率含安全子集、成本）必须同时满足才是 Go；任何单项改善都不构成继续推进的理由。

## 五维总览

| 指标 | A 主 Agent 直接探索 | B 委派子 Agent | 对比 |
| --- | --- | --- | --- |
| 主上下文峰值（均值） | 8,693.5 | 9,473.8 | -9.0% 降幅 |
| 主上下文峰值（单任务最大） | 10,991 | 11,631 | — |
| 主 Agent 累计 token | 421,445 | 51,289 | — |
| 全 Agent 累计 chargeable token | 421,445 | 232,604 | — |
| 回传结果大小（字符） | 0 | 13,177 | — |
| API 成本 | 0.1099 USD | 0.1104 USD | ×1.00 |
| 关键结论正确率 | 64.3% | 100.0% | — |
| 任务严格通过率 | 50.0% | 100.0% | — |
| 任务耗时合计（秒） | 228.1 | 227.0 | — |
| 子代理调用次数 | 0 | 3 | — |
| 主 Agent tool_search 次数 | 23 | 3 | — |
| 委派尝试次数（含失败） | 0 | 3 | — |

## 分类正确率（关键结论口径）

| 分类 | A 正确率 | B 正确率 | A 严格通过 | B 严格通过 |
| --- | --- | --- | --- | --- |
| call_chain | 100.0% | 100.0% | 100.0% | 100.0% |
| config_trace | 0.0% | 100.0% | 0.0% | 100.0% |
| negative_proof | 0.0% | 100.0% | 0.0% | 100.0% |
| repo_audit | 100.0% | 100.0% | 100.0% | 100.0% |

## 安全子集明细

| 任务 | A 关键错误 | B 关键错误 | A 正确率 | B 正确率 | B 缺失的关键结论 |
| --- | --- | --- | --- | --- | --- |

## 逐任务对比

| 任务 | 分类 | 峰值 A | 峰值 B | 降幅 | 事实 A | 事实 B | 通过 A | 通过 B | 全Agent token A | 全Agent token B | 回传字符 B |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T09-unused-config | config_trace | 10,452.0 | 9,788.0 | 6.4% | 0.0% | 100.0% | 0.0% | 100.0% | 209,518 | 49,747 | 4,342 |
| L02-deprecated-unused | negative_proof | 10,991.0 | 10,996.0 | -0.0% | 0.0% | 100.0% | 0.0% | 100.0% | 197,348 | 69,347 | 4,547 |
| L04-checkout-call-chain | call_chain | 7,808.0 | 11,631.0 | -49.0% | 100.0% | 100.0% | 100.0% | 100.0% | 8,627 | 107,419 | 4,288 |
| L05-import-time-connections | repo_audit | 5,523.0 | 5,480.0 | 0.8% | 100.0% | 100.0% | 100.0% | 100.0% | 5,952 | 6,091 | 0 |

## 校准数据（委派触发规则用）

| 任务 | 预估所需文件数 | A 读取文件数 | B 主读取 | B 子代理调用 | B tool_search | B 委派尝试 |
| --- | --- | --- | --- | --- | --- | --- |
| T09-unused-config | 2 | 75.0 | 4.0 | 1 | 1 | 1 |
| L02-deprecated-unused | 10 | 77.0 | 17.0 | 1 | 1 | 1 |
| L04-checkout-call-chain | 8 | 13.0 | 11.0 | 1 | 1 | 1 |
| L05-import-time-connections | 8 | 10.0 | 11.0 | 0 | 0 | 0 |

## 失败与异常

| 任务 | 臂 | run | stop_reason | 错误 |
| --- | --- | --- | --- | --- |
| T09-unused-config | direct | 1 | max_steps | maximum step limit (40) reached |
| L02-deprecated-unused | direct | 1 | max_steps | maximum step limit (40) reached |
