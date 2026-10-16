# 只读探索子 Agent 阶段 0 · A/B 评测报告

- 生成时间：2026-10-16T11:41:59+00:00
- 模型：deepseek / deepseek-v4-flash
- 任务数：20；每任务轮数：1
- 运行根目录：`C:\Users\cjy\AppData\Local\Temp\evolyn-subagent-eval-pk3n2h32`
- 复现命令：`C:\agents\Evolyn\backend\tests\eval\subagent\run_live.py --arm both-strong --runs 1 --provider deepseek --price-input 0.14 --price-output 0.28 --out tests\eval\subagent\reports\report_subagent_20261016_B2strong.md --allow-no-go`

## 判定：**No-go**

| 判定项 | 通过 | 详情 |
| --- | --- | --- |
| 上下文峰值降幅 | ❌ | A 均值 6,100 → B 均值 8,782，降幅 -44.0%（要求 ≥ 50%；口径：主 Run 最大 prompt token，逐任务先折叠再跨任务平均） |
| 正确率下降幅度 | ✅ | 关键结论正确率 A 98.3% → B 96.6%，下降 +1.7pp（要求 ≤ 5pp；严格通过率 A 95% / B 90%，仅作参考） |
| 安全关键错误 | ✅ | 安全子集关键错误 A 0 → B 0（要求 B ≤ A） |
| 总成本比值 | ❌ | 口径 API 成本（USD）：A 0.0743 → B 0.3970，比值 5.34（要求 ≤ 1.3） |

> 联合判定：三项门槛（上下文、正确率含安全子集、成本）必须同时满足才是 Go；任何单项改善都不构成继续推进的理由。

## 五维总览

| 指标 | A 主 Agent 直接探索 | B 委派子 Agent | 对比 |
| --- | --- | --- | --- |
| 主上下文峰值（均值） | 6,100.2 | 8,782.5 | -44.0% 降幅 |
| 主上下文峰值（单任务最大） | 10,414 | 13,842 | — |
| 主 Agent 累计 token | 234,045 | 272,921 | — |
| 全 Agent 累计 chargeable token | 234,045 | 1,127,405 | — |
| 回传结果大小（字符） | 0 | 172,728 | — |
| API 成本 | 0.0743 USD | 0.3970 USD | ×5.34 |
| 关键结论正确率 | 98.3% | 96.6% | — |
| 任务严格通过率 | 95.0% | 90.0% | — |
| 任务耗时合计（秒） | 342.2 | 1,592.5 | — |
| 子代理调用次数 | 0 | 49 | — |
| 主 Agent tool_search 次数 | 0 | 31 | — |
| 委派尝试次数（含失败） | 0 | 51 | — |

## 分类正确率（关键结论口径）

| 分类 | A 正确率 | B 正确率 | A 严格通过 | B 严格通过 |
| --- | --- | --- | --- | --- |
| call_chain | 90.9% | 90.9% | 66.7% | 66.7% |
| config_trace | 100.0% | 100.0% | 100.0% | 100.0% |
| error_handling | 100.0% | 80.0% | 100.0% | 50.0% |
| function_locate | 100.0% | 100.0% | 100.0% | 100.0% |
| module_dependency | 100.0% | 100.0% | 100.0% | 100.0% |
| security | 100.0% | 100.0% | 100.0% | 100.0% |
| test_coverage | 100.0% | 100.0% | 100.0% | 100.0% |

## 安全子集明细

| 任务 | A 关键错误 | B 关键错误 | A 正确率 | B 正确率 | B 缺失的关键结论 |
| --- | --- | --- | --- | --- | --- |
| T18-admin-authorization-gap | 0 | 0 | 100.0% | 100.0% | — |
| T19-session-validation-bypass | 0 | 0 | 100.0% | 100.0% | — |
| T20-data-isolation-gap | 0 | 0 | 100.0% | 100.0% | — |

## 逐任务对比

| 任务 | 分类 | 峰值 A | 峰值 B | 降幅 | 事实 A | 事实 B | 通过 A | 通过 B | 全Agent token A | 全Agent token B | 回传字符 B |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T01-locate-validate-session | function_locate | 3,289.0 | 7,850.0 | -138.7% | 100.0% | 100.0% | 100.0% | 100.0% | 3,302 | 40,899 | 8,134 |
| T02-locate-login-handler | function_locate | 4,516.0 | 6,413.0 | -42.0% | 100.0% | 100.0% | 100.0% | 100.0% | 4,474 | 20,027 | 6,148 |
| T03-locate-permission | function_locate | 2,014.0 | 6,971.0 | -246.1% | 100.0% | 100.0% | 100.0% | 100.0% | 1,696 | 33,610 | 669 |
| T04-locate-error-mapping | function_locate | 1,965.0 | 7,135.0 | -263.1% | 100.0% | 100.0% | 100.0% | 100.0% | 1,637 | 31,686 | 6,773 |
| T05-login-call-chain | call_chain | 6,255.0 | 10,618.0 | -69.8% | 100.0% | 75.0% | 100.0% | 0.0% | 7,121 | 66,110 | 14,129 |
| T06-admin-call-chain | call_chain | 5,850.0 | 6,846.0 | -17.0% | 66.7% | 100.0% | 0.0% | 100.0% | 7,986 | 40,035 | 6,794 |
| T07-audit-call-chain | call_chain | 8,080.0 | 11,618.0 | -43.8% | 100.0% | 100.0% | 100.0% | 100.0% | 27,972 | 92,977 | 15,826 |
| T08-auth-env-vars | config_trace | 8,340.0 | 8,383.0 | -0.5% | 100.0% | 100.0% | 100.0% | 100.0% | 8,897 | 53,453 | 9,699 |
| T09-unused-config | config_trace | 8,844.0 | 13,842.0 | -56.5% | 100.0% | 100.0% | 100.0% | 100.0% | 46,440 | 174,383 | 12,864 |
| T10-token-secret | config_trace | 3,468.0 | 10,211.0 | -194.4% | 100.0% | 100.0% | 100.0% | 100.0% | 3,696 | 58,550 | 12,140 |
| T11-error-handling-mechanism | error_handling | 7,714.0 | 7,599.0 | 1.5% | 100.0% | 100.0% | 100.0% | 100.0% | 8,390 | 40,874 | 7,175 |
| T12-login-failure-path | error_handling | 6,925.0 | 8,998.0 | -29.9% | 100.0% | 66.7% | 100.0% | 0.0% | 7,947 | 43,078 | 11,230 |
| T13-session-test-coverage | test_coverage | 4,233.0 | 6,048.0 | -42.9% | 100.0% | 100.0% | 100.0% | 100.0% | 6,791 | 17,911 | 7,527 |
| T14-missing-test-files | test_coverage | 6,432.0 | 12,936.0 | -101.1% | 100.0% | 100.0% | 100.0% | 100.0% | 6,975 | 77,306 | 1,338 |
| T15-api-dependencies | module_dependency | 3,339.0 | 5,602.0 | -67.8% | 100.0% | 100.0% | 100.0% | 100.0% | 3,701 | 27,356 | 6,917 |
| T16-dependency-direction | module_dependency | 10,414.0 | 8,041.0 | 22.8% | 100.0% | 100.0% | 100.0% | 100.0% | 29,607 | 96,164 | 7,970 |
| T17-audit-callers | module_dependency | 10,322.0 | 6,693.0 | 35.2% | 100.0% | 100.0% | 100.0% | 100.0% | 31,220 | 43,157 | 6,407 |
| T18-admin-authorization-gap | security | 6,362.0 | 10,749.0 | -69.0% | 100.0% | 100.0% | 100.0% | 100.0% | 8,892 | 47,304 | 10,438 |
| T19-session-validation-bypass | security | 6,271.0 | 9,484.0 | -51.2% | 100.0% | 100.0% | 100.0% | 100.0% | 8,375 | 43,882 | 6,532 |
| T20-data-isolation-gap | security | 7,371.0 | 9,613.0 | -30.4% | 100.0% | 100.0% | 100.0% | 100.0% | 8,926 | 78,643 | 14,018 |

## 校准数据（委派触发规则用）

| 任务 | 预估所需文件数 | A 读取文件数 | B 主读取 | B 子代理调用 | B tool_search | B 委派尝试 |
| --- | --- | --- | --- | --- | --- | --- |
| T01-locate-validate-session | 2 | 5.0 | 0.0 | 2 | 2 | 2 |
| T02-locate-login-handler | 3 | 5.0 | 0.0 | 2 | 2 | 2 |
| T03-locate-permission | 2 | 2.0 | 9.0 | 1 | 1 | 1 |
| T04-locate-error-mapping | 3 | 2.0 | 0.0 | 2 | 2 | 2 |
| T05-login-call-chain | 6 | 10.0 | 0.0 | 3 | 1 | 3 |
| T06-admin-call-chain | 5 | 10.0 | 0.0 | 2 | 1 | 2 |
| T07-audit-call-chain | 5 | 29.0 | 3.0 | 4 | 2 | 5 |
| T08-auth-env-vars | 2 | 18.0 | 0.0 | 3 | 1 | 3 |
| T09-unused-config | 2 | 29.0 | 28.0 | 4 | 3 | 5 |
| T10-token-secret | 3 | 6.0 | 0.0 | 3 | 2 | 3 |
| T11-error-handling-mechanism | 3 | 14.0 | 0.0 | 2 | 2 | 2 |
| T12-login-failure-path | 3 | 12.0 | 0.0 | 3 | 1 | 3 |
| T13-session-test-coverage | 3 | 7.0 | 0.0 | 1 | 1 | 1 |
| T14-missing-test-files | 8 | 12.0 | 16.0 | 2 | 1 | 2 |
| T15-api-dependencies | 6 | 5.0 | 0.0 | 1 | 1 | 1 |
| T16-dependency-direction | 5 | 25.0 | 0.0 | 4 | 2 | 4 |
| T17-audit-callers | 4 | 26.0 | 0.0 | 2 | 1 | 2 |
| T18-admin-authorization-gap | 4 | 10.0 | 3.0 | 3 | 1 | 3 |
| T19-session-validation-bypass | 4 | 11.0 | 4.0 | 2 | 2 | 2 |
| T20-data-isolation-gap | 4 | 12.0 | 0.0 | 3 | 2 | 3 |

## 失败与异常

无。
