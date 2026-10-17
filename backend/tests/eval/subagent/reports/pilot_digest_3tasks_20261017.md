# 只读探索子 Agent 阶段 0 · A/B 评测报告

- 生成时间：2026-10-17T13:19:48+00:00
- 模型：deepseek / deepseek-v4-flash
- 任务数：3；每任务轮数：1
- 运行根目录：`C:\Users\cjy\AppData\Local\Temp\evolyn-subagent-eval-6nkhnw84`
- 复现命令：`C:\agents\Evolyn\backend\tests\eval\subagent\run_live.py --arm both --suite digest --runs 1 --provider deepseek --model deepseek-v4-flash --price-input 0.14 --price-output 0.28 --allow-no-go --out tests/eval/subagent/reports/pilot_digest_3tasks_20261017.md`

## 判定：**No-go**

| 判定项 | 通过 | 详情 |
| --- | --- | --- |
| 上下文峰值降幅 | ❌ | A 均值 12,471 → B 均值 14,145，降幅 -13.4%（要求 ≥ 50%；口径：主 Run 最大 prompt token，逐任务先折叠再跨任务平均） |
| 正确率下降幅度 | ✅ | 关键结论正确率 A 25.0% → B 45.0%，下降 -20.0pp（要求 ≤ 5pp；严格通过率 A 0% / B 0%，仅作参考） |
| 安全关键错误 | ✅ | 安全子集关键错误 A 0 → B 0（要求 B ≤ A） |
| 总成本比值 | ❌ | 口径 API 成本（USD）：A 0.1364 → B 0.2289，比值 1.68（要求 ≤ 1.3） |

> 联合判定：三项门槛（上下文、正确率含安全子集、成本）必须同时满足才是 Go；任何单项改善都不构成继续推进的理由。

## 五维总览

| 指标 | A 主 Agent 直接探索 | B 委派子 Agent | 对比 |
| --- | --- | --- | --- |
| 主上下文峰值（均值） | 12,470.7 | 14,145.0 | -13.4% 降幅 |
| 主上下文峰值（单任务最大） | 14,636 | 16,759 | — |
| 主 Agent 累计 token | 718,866 | 688,475 | — |
| 全 Agent 累计 chargeable token | 718,866 | 945,171 | — |
| 回传结果大小（字符） | 0 | 2,227 | — |
| API 成本 | 0.1364 USD | 0.2289 USD | ×1.68 |
| 关键结论正确率 | 25.0% | 45.0% | — |
| 任务严格通过率 | 0.0% | 0.0% | — |
| 任务耗时合计（秒） | 262.4 | 413.2 | — |
| 子代理调用次数 | 0 | 3 | — |
| 主 Agent tool_search 次数 | 0 | 2 | — |
| 委派尝试次数（含失败） | 0 | 19 | — |

## 分类正确率（关键结论口径）

| 分类 | A 正确率 | B 正确率 | A 严格通过 | B 严格通过 |
| --- | --- | --- | --- | --- |
| digest_synthesis | 25.0% | 45.0% | 0.0% | 0.0% |

## 安全子集明细

| 任务 | A 关键错误 | B 关键错误 | A 正确率 | B 正确率 | B 缺失的关键结论 |
| --- | --- | --- | --- | --- | --- |

## 逐任务对比

| 任务 | 分类 | 峰值 A | 峰值 B | 降幅 | 事实 A | 事实 B | 通过 A | 通过 B | 全Agent token A | 全Agent token B | 回传字符 B |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D01-specs-impl-mismatch | digest_synthesis | 12,891.0 | 16,759.0 | -30.0% | 50.0% | 12.5% | 0.0% | 0.0% | 252,616 | 467,240 | 2,227 |
| D02-specs-internal-conflicts | digest_synthesis | 14,636.0 | 15,068.0 | -3.0% | 0.0% | 25.0% | 0.0% | 0.0% | 258,903 | 267,857 | 0 |
| D03-audit-event-conformance | digest_synthesis | 9,885.0 | 10,608.0 | -7.3% | 12.5% | 87.5% | 0.0% | 0.0% | 207,347 | 210,074 | 0 |

## 校准数据（委派触发规则用）

| 任务 | 预估所需文件数 | A 读取文件数 | B 主读取 | B 子代理调用 | B tool_search | B 委派尝试 |
| --- | --- | --- | --- | --- | --- | --- |
| D01-specs-impl-mismatch | 22 | 48.0 | 47.0 | 3 | 2 | 19 |
| D02-specs-internal-conflicts | 35 | 55.0 | 66.0 | 0 | 0 | 0 |
| D03-audit-event-conformance | 12 | 14.0 | 12.0 | 0 | 0 | 0 |

## 失败与异常

无。
