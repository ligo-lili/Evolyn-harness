# 只读探索子 Agent 阶段 0 · A/B 评测报告

- 生成时间：2026-10-17T09:31:15+00:00
- 模型：deepseek / deepseek-v4-flash
- 任务数：4；每任务轮数：1
- 运行根目录：`C:\Users\cjy\AppData\Local\Temp\evolyn-subagent-eval-493a_r8k`
- 复现命令：`C:\agents\Evolyn\backend\tests\eval\subagent\run_live.py --arm both --task T09-unused-config --task L02-deprecated-unused --task L04-checkout-call-chain --task L05-import-time-connections --runs 1 --provider deepseek --model deepseek-v4-flash --price-input 0.14 --price-output 0.28 --allow-no-go --out tests/eval/subagent/reports/trial2_large_20261017.md`

## 判定：**No-go**

| 判定项 | 通过 | 详情 |
| --- | --- | --- |
| 上下文峰值降幅 | ❌ | A 均值 13,451 → B 均值 10,618，降幅 21.1%（要求 ≥ 50%；口径：主 Run 最大 prompt token，逐任务先折叠再跨任务平均） |
| 正确率下降幅度 | ✅ | 关键结论正确率 A 64.3% → B 100.0%，下降 -35.7pp（要求 ≤ 5pp；严格通过率 A 50% / B 100%，仅作参考） |
| 安全关键错误 | ✅ | 安全子集关键错误 A 0 → B 0（要求 B ≤ A） |
| 总成本比值 | ❌ | 口径 API 成本（USD）：A 0.1308 → B 0.1722，比值 1.32（要求 ≤ 1.3） |

> 联合判定：三项门槛（上下文、正确率含安全子集、成本）必须同时满足才是 Go；任何单项改善都不构成继续推进的理由。

## 五维总览

| 指标 | A 主 Agent 直接探索 | B 委派子 Agent | 对比 |
| --- | --- | --- | --- |
| 主上下文峰值（均值） | 13,451.0 | 10,617.5 | 21.1% 降幅 |
| 主上下文峰值（单任务最大） | 23,057 | 12,428 | — |
| 主 Agent 累计 token | 592,972 | 85,055 | — |
| 全 Agent 累计 chargeable token | 592,972 | 530,219 | — |
| 回传结果大小（字符） | 0 | 14,596 | — |
| API 成本 | 0.1308 USD | 0.1722 USD | ×1.32 |
| 关键结论正确率 | 64.3% | 100.0% | — |
| 任务严格通过率 | 50.0% | 100.0% | — |
| 任务耗时合计（秒） | 267.5 | 343.2 | — |
| 子代理调用次数 | 0 | 4 | — |
| 主 Agent tool_search 次数 | 32 | 4 | — |
| 委派尝试次数（含失败） | 0 | 6 | — |

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
| T09-unused-config | config_trace | 12,668.0 | 12,428.0 | 1.9% | 0.0% | 100.0% | 0.0% | 100.0% | 239,077 | 315,829 | 5,139 |
| L02-deprecated-unused | negative_proof | 23,057.0 | 11,711.0 | 49.2% | 0.0% | 100.0% | 0.0% | 100.0% | 264,208 | 78,829 | 5,084 |
| L04-checkout-call-chain | call_chain | 10,526.0 | 10,922.0 | -3.8% | 100.0% | 100.0% | 100.0% | 100.0% | 81,208 | 127,753 | 4,373 |
| L05-import-time-connections | repo_audit | 7,553.0 | 7,409.0 | 1.9% | 100.0% | 100.0% | 100.0% | 100.0% | 8,479 | 7,808 | 0 |

## 校准数据（委派触发规则用）

| 任务 | 预估所需文件数 | A 读取文件数 | B 主读取 | B 子代理调用 | B tool_search | B 委派尝试 |
| --- | --- | --- | --- | --- | --- | --- |
| T09-unused-config | 2 | 33.0 | 11.0 | 2 | 2 | 3 |
| L02-deprecated-unused | 10 | 69.0 | 10.0 | 1 | 1 | 2 |
| L04-checkout-call-chain | 8 | 13.0 | 6.0 | 1 | 1 | 1 |
| L05-import-time-connections | 8 | 11.0 | 10.0 | 0 | 0 | 0 |

## 失败与异常

| 任务 | 臂 | run | stop_reason | 错误 |
| --- | --- | --- | --- | --- |
| T09-unused-config | direct | 1 | max_steps | maximum step limit (40) reached |
| L02-deprecated-unused | direct | 1 | max_steps | maximum step limit (40) reached |
