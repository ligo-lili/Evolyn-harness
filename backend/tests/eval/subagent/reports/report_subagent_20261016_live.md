# 只读探索子 Agent 阶段 0 · A/B 评测报告

- 生成时间：2026-10-16T10:44:47+00:00
- 模型：deepseek / deepseek-v4-flash
- 任务数：20；每任务轮数：3
- 运行根目录：`C:\Users\cjy\AppData\Local\Temp\evolyn-subagent-eval-4takrbax`
- 复现命令：`C:\agents\Evolyn\backend\tests\eval\subagent\run_live.py --arm both --runs 3 --provider deepseek --price-input 0.14 --price-output 0.28 --out tests\eval\subagent\reports\report_subagent_20261016_live.md --allow-no-go`

## 判定：**No-go**

| 判定项 | 通过 | 详情 |
| --- | --- | --- |
| 上下文峰值降幅 | ❌ | A 均值 5,915 → B 均值 6,700，降幅 -13.3%（要求 ≥ 50%；口径：主 Run 最大 prompt token，逐任务先折叠再跨任务平均） |
| 正确率下降幅度 | ✅ | 关键结论正确率 A 96.6% → B 97.7%，下降 -1.1pp（要求 ≤ 5pp；严格通过率 A 90% / B 93%，仅作参考） |
| 安全关键错误 | ❌ | 安全子集关键错误 A 0 → B 1（要求 B ≤ A）；B 缺失的关键结论：T20-data-isolation-gap:no-check |
| 总成本比值 | ✅ | 口径 API 成本（USD）：A 0.2197 → B 0.2685，比值 1.22（要求 ≤ 1.3） |

> 联合判定：三项门槛（上下文、正确率含安全子集、成本）必须同时满足才是 Go；任何单项改善都不构成继续推进的理由。

## 五维总览

| 指标 | A 主 Agent 直接探索 | B 委派子 Agent | 对比 |
| --- | --- | --- | --- |
| 主上下文峰值（均值） | 5,914.6 | 6,700.1 | -13.3% 降幅 |
| 主上下文峰值（单任务最大） | 11,773 | 12,887 | — |
| 主 Agent 累计 token | 625,911 | 754,430 | — |
| 全 Agent 累计 chargeable token | 625,911 | 778,899 | — |
| 回传结果大小（字符） | 0 | 669 | — |
| API 成本 | 0.2197 USD | 0.2685 USD | ×1.22 |
| 关键结论正确率 | 96.6% | 97.7% | — |
| 任务严格通过率 | 90.0% | 93.3% | — |
| 任务耗时合计（秒） | 1,021.0 | 1,136.7 | — |
| 子代理调用次数 | 0 | 1 | — |
| 主 Agent tool_search 次数 | 0 | 8 | — |
| 委派尝试次数（含失败） | 0 | 1 | — |

## 分类正确率（关键结论口径）

| 分类 | A 正确率 | B 正确率 | A 严格通过 | B 严格通过 |
| --- | --- | --- | --- | --- |
| call_chain | 97.0% | 100.0% | 88.9% | 100.0% |
| config_trace | 90.5% | 100.0% | 77.8% | 100.0% |
| error_handling | 86.7% | 80.0% | 66.7% | 50.0% |
| function_locate | 100.0% | 100.0% | 100.0% | 100.0% |
| module_dependency | 96.7% | 100.0% | 88.9% | 100.0% |
| security | 100.0% | 96.7% | 100.0% | 88.9% |
| test_coverage | 100.0% | 100.0% | 100.0% | 100.0% |

## 安全子集明细

| 任务 | A 关键错误 | B 关键错误 | A 正确率 | B 正确率 | B 缺失的关键结论 |
| --- | --- | --- | --- | --- | --- |
| T18-admin-authorization-gap | 0 | 0 | 100.0% | 100.0% | — |
| T19-session-validation-bypass | 0 | 0 | 100.0% | 100.0% | — |
| T20-data-isolation-gap | 0 | 1 | 100.0% | 88.9% | no-check |

## 逐任务对比

| 任务 | 分类 | 峰值 A | 峰值 B | 降幅 | 事实 A | 事实 B | 通过 A | 通过 B | 全Agent token A | 全Agent token B | 回传字符 B |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T01-locate-validate-session | function_locate | 3,469.3 | 3,933.7 | -13.4% | 100.0% | 100.0% | 100.0% | 100.0% | 10,644 | 12,699 | 0 |
| T02-locate-login-handler | function_locate | 3,278.3 | 3,016.3 | 8.0% | 100.0% | 100.0% | 100.0% | 100.0% | 9,667 | 9,161 | 0 |
| T03-locate-permission | function_locate | 2,416.0 | 2,313.3 | 4.2% | 100.0% | 100.0% | 100.0% | 100.0% | 6,652 | 6,284 | 0 |
| T04-locate-error-mapping | function_locate | 2,433.0 | 3,545.3 | -45.7% | 100.0% | 100.0% | 100.0% | 100.0% | 6,929 | 9,971 | 0 |
| T05-login-call-chain | call_chain | 6,651.3 | 7,297.3 | -9.7% | 100.0% | 100.0% | 100.0% | 100.0% | 23,996 | 24,185 | 0 |
| T06-admin-call-chain | call_chain | 8,072.7 | 8,811.3 | -9.2% | 88.9% | 100.0% | 66.7% | 100.0% | 30,017 | 48,137 | 0 |
| T07-audit-call-chain | call_chain | 9,100.3 | 9,133.0 | -0.4% | 100.0% | 100.0% | 100.0% | 100.0% | 84,059 | 115,905 | 0 |
| T08-auth-env-vars | config_trace | 5,534.0 | 7,849.0 | -41.8% | 100.0% | 100.0% | 100.0% | 100.0% | 17,280 | 24,883 | 0 |
| T09-unused-config | config_trace | 9,177.0 | 9,532.0 | -3.9% | 66.7% | 100.0% | 66.7% | 100.0% | 98,058 | 132,290 | 669 |
| T10-token-secret | config_trace | 3,283.0 | 4,692.7 | -42.9% | 83.3% | 100.0% | 66.7% | 100.0% | 11,616 | 16,297 | 0 |
| T11-error-handling-mechanism | error_handling | 5,396.0 | 8,099.3 | -50.1% | 100.0% | 100.0% | 100.0% | 100.0% | 18,540 | 27,460 | 0 |
| T12-login-failure-path | error_handling | 6,230.3 | 7,954.0 | -27.7% | 77.8% | 66.7% | 33.3% | 0.0% | 21,625 | 27,762 | 0 |
| T13-session-test-coverage | test_coverage | 4,006.0 | 4,189.7 | -4.6% | 100.0% | 100.0% | 100.0% | 100.0% | 15,533 | 18,819 | 0 |
| T14-missing-test-files | test_coverage | 6,136.0 | 5,943.0 | 3.1% | 100.0% | 100.0% | 100.0% | 100.0% | 19,709 | 18,807 | 0 |
| T15-api-dependencies | module_dependency | 3,828.3 | 3,644.7 | 4.8% | 100.0% | 100.0% | 100.0% | 100.0% | 12,235 | 13,099 | 0 |
| T16-dependency-direction | module_dependency | 9,966.3 | 10,779.0 | -8.2% | 91.7% | 100.0% | 66.7% | 100.0% | 83,865 | 79,806 | 0 |
| T17-audit-callers | module_dependency | 9,827.7 | 10,297.3 | -4.8% | 100.0% | 100.0% | 100.0% | 100.0% | 78,064 | 107,106 | 0 |
| T18-admin-authorization-gap | security | 6,695.0 | 7,963.7 | -18.9% | 100.0% | 100.0% | 100.0% | 100.0% | 28,728 | 30,249 | 0 |
| T19-session-validation-bypass | security | 6,537.0 | 6,024.3 | 7.8% | 100.0% | 100.0% | 100.0% | 100.0% | 23,657 | 21,678 | 0 |
| T20-data-isolation-gap | security | 6,254.7 | 8,982.0 | -43.6% | 100.0% | 88.9% | 100.0% | 66.7% | 25,037 | 34,301 | 0 |

## 校准数据（委派触发规则用）

| 任务 | 预估所需文件数 | A 读取文件数 | B 主读取 | B 子代理调用 | B tool_search | B 委派尝试 |
| --- | --- | --- | --- | --- | --- | --- |
| T01-locate-validate-session | 2 | 5.0 | 4.3 | 0 | 2 | 0 |
| T02-locate-login-handler | 3 | 3.3 | 3.0 | 0 | 0 | 0 |
| T03-locate-permission | 2 | 3.0 | 2.0 | 0 | 0 | 0 |
| T04-locate-error-mapping | 3 | 3.0 | 4.3 | 0 | 1 | 0 |
| T05-login-call-chain | 6 | 10.7 | 10.7 | 0 | 0 | 0 |
| T06-admin-call-chain | 5 | 17.0 | 20.3 | 0 | 0 | 0 |
| T07-audit-call-chain | 5 | 29.3 | 26.3 | 0 | 0 | 0 |
| T08-auth-env-vars | 2 | 11.0 | 16.7 | 0 | 0 | 0 |
| T09-unused-config | 2 | 26.7 | 26.7 | 1 | 1 | 1 |
| T10-token-secret | 3 | 4.7 | 7.0 | 0 | 0 | 0 |
| T11-error-handling-mechanism | 3 | 8.7 | 15.3 | 0 | 0 | 0 |
| T12-login-failure-path | 3 | 10.7 | 14.3 | 0 | 0 | 0 |
| T13-session-test-coverage | 3 | 6.0 | 6.7 | 0 | 0 | 0 |
| T14-missing-test-files | 8 | 10.7 | 10.3 | 0 | 0 | 0 |
| T15-api-dependencies | 6 | 6.3 | 6.0 | 0 | 0 | 0 |
| T16-dependency-direction | 5 | 25.3 | 26.3 | 0 | 1 | 0 |
| T17-audit-callers | 4 | 25.7 | 27.0 | 0 | 2 | 0 |
| T18-admin-authorization-gap | 4 | 11.3 | 13.3 | 0 | 0 | 0 |
| T19-session-validation-bypass | 4 | 9.7 | 8.3 | 0 | 0 | 0 |
| T20-data-isolation-gap | 4 | 11.0 | 17.0 | 0 | 1 | 0 |

## 失败与异常

无。
