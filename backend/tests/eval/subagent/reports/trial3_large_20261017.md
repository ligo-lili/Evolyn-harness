# 只读探索子 Agent 阶段 0 · A/B 评测报告

- 生成时间：2026-10-17T10:44:03+00:00
- 模型：deepseek / deepseek-v4-flash
- 任务数：4；每任务轮数：1
- 运行根目录：`C:\Users\cjy\AppData\Local\Temp\evolyn-subagent-eval-doi4is8c`
- 复现命令：`C:\agents\Evolyn\backend\tests\eval\subagent\run_live.py --arm both --task T09-unused-config --task L02-deprecated-unused --task L04-checkout-call-chain --task L05-import-time-connections --runs 1 --provider deepseek --model deepseek-v4-flash --price-input 0.14 --price-output 0.28 --allow-no-go --out tests/eval/subagent/reports/trial3_large_20261017.md`

## 判定：**No-go**

| 判定项 | 通过 | 详情 |
| --- | --- | --- |
| 上下文峰值降幅 | ❌ | A 均值 6,921 → B 均值 6,616，降幅 4.4%（要求 ≥ 50%；口径：主 Run 最大 prompt token，逐任务先折叠再跨任务平均） |
| 正确率下降幅度 | ✅ | 关键结论正确率 A 100.0% → B 100.0%，下降 +0.0pp（要求 ≤ 5pp；严格通过率 A 100% / B 100%，仅作参考） |
| 安全关键错误 | ✅ | 安全子集关键错误 A 0 → B 0（要求 B ≤ A） |
| 总成本比值 | ✅ | 口径 API 成本（USD）：A 0.0163 → B 0.0140，比值 0.86（要求 ≤ 1.3） |

> 联合判定：三项门槛（上下文、正确率含安全子集、成本）必须同时满足才是 Go；任何单项改善都不构成继续推进的理由。

## 五维总览

| 指标 | A 主 Agent 直接探索 | B 委派子 Agent | 对比 |
| --- | --- | --- | --- |
| 主上下文峰值（均值） | 6,920.8 | 6,615.8 | 4.4% 降幅 |
| 主上下文峰值（单任务最大） | 8,681 | 8,617 | — |
| 主 Agent 累计 token | 36,518 | 25,896 | — |
| 全 Agent 累计 chargeable token | 36,518 | 25,896 | — |
| 回传结果大小（字符） | 0 | 0 | — |
| API 成本 | 0.0163 USD | 0.0140 USD | ×0.86 |
| 关键结论正确率 | 100.0% | 100.0% | — |
| 任务严格通过率 | 100.0% | 100.0% | — |
| 任务耗时合计（秒） | 56.2 | 43.3 | — |
| 子代理调用次数 | 0 | 0 | — |
| 主 Agent tool_search 次数 | 0 | 0 | — |
| 委派尝试次数（含失败） | 0 | 0 | — |

## 分类正确率（关键结论口径）

| 分类 | A 正确率 | B 正确率 | A 严格通过 | B 严格通过 |
| --- | --- | --- | --- | --- |
| call_chain | 100.0% | 100.0% | 100.0% | 100.0% |
| config_trace | 100.0% | 100.0% | 100.0% | 100.0% |
| negative_proof | 100.0% | 100.0% | 100.0% | 100.0% |
| repo_audit | 100.0% | 100.0% | 100.0% | 100.0% |

## 安全子集明细

| 任务 | A 关键错误 | B 关键错误 | A 正确率 | B 正确率 | B 缺失的关键结论 |
| --- | --- | --- | --- | --- | --- |

## 逐任务对比

| 任务 | 分类 | 峰值 A | 峰值 B | 降幅 | 事实 A | 事实 B | 通过 A | 通过 B | 全Agent token A | 全Agent token B | 回传字符 B |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T09-unused-config | config_trace | 5,829.0 | 5,329.0 | 8.6% | 100.0% | 100.0% | 100.0% | 100.0% | 7,289 | 6,241 | 0 |
| L02-deprecated-unused | negative_proof | 8,324.0 | 8,617.0 | -3.5% | 100.0% | 100.0% | 100.0% | 100.0% | 7,992 | 8,132 | 0 |
| L04-checkout-call-chain | call_chain | 8,681.0 | 8,051.0 | 7.3% | 100.0% | 100.0% | 100.0% | 100.0% | 16,362 | 7,617 | 0 |
| L05-import-time-connections | repo_audit | 4,849.0 | 4,466.0 | 7.9% | 100.0% | 100.0% | 100.0% | 100.0% | 4,875 | 3,906 | 0 |

## 校准数据（委派触发规则用）

| 任务 | 预估所需文件数 | A 读取文件数 | B 主读取 | B 子代理调用 | B tool_search | B 委派尝试 |
| --- | --- | --- | --- | --- | --- | --- |
| T09-unused-config | 2 | 4.0 | 2.0 | 0 | 0 | 0 |
| L02-deprecated-unused | 10 | 7.0 | 6.0 | 0 | 0 | 0 |
| L04-checkout-call-chain | 8 | 10.0 | 9.0 | 0 | 0 | 0 |
| L05-import-time-connections | 8 | 4.0 | 3.0 | 0 | 0 | 0 |

## 失败与异常

无。
