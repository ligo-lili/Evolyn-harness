# 只读探索子 Agent · 阶段 0 A/B 评测

对应《只读探索子 Agent 设计方案 v3》的阶段 0：先评测后主线。这里回答三个
问题——委派是否真的降低主 Agent 上下文占用、正确率（尤其安全相关）是否
可接受、总成本是否在门槛内。**三项必须同时满足才算 Go**（按套件分别判定，
见下）。

```text
subagent/
├── tasks.py       29 个真实代码探索任务 + 标准答案（core 20 + large 9）
├── fixtures.py    夹具仓库 shop-auth-sandbox（确定性生成，107 个文件、约 10 万字符）
├── harness.py     A（直接探索）/ B（现行 V1 委派）两臂装配与运行
├── metrics.py     五维指标采集
├── reporting.py   联合判定（Go/No-go）与报告渲染
├── run_live.py    真实模型跑批入口（有 API 成本，需显式执行）
├── tests/         离线自检（不调用真实模型）
└── reports/       跑批产出的报告（.md + .json）
```

## 双套件（预登记）

- **core（20 个）**：v3 §9 的基础覆盖面（函数定位、跨文件调用链、配置追踪、
  异常处理、测试覆盖、跨模块依赖、安全子集），作为对照面；
- **large（9 个）**：大范围盘点 / 负向证明 / 跨模块链，任务形态看起来就
  需要大面积检索，是委派机制的设计工作区间。

两套件**分别跑批、分别归档**（`--suite core` / `--suite large`），评分口径
与门槛数字不随跑批结果调整；结论按套件分别陈述（避免「全套件均值」把两种
工作区间混在一起）。

## 两臂定义

- **A 臂（direct）**：主 Agent 直接探索，不注册委派工具，基线。
- **B 臂（delegate）**：现行 V1 委派——`subagent_run` 以 deferred 注册
  （与 `app/application.py` 一致），主 Agent 自行决定是否委派。

两臂共用同一装配：系统提示词、步数上限、只读工具集（read_file /
list_files / get_current_time）、Evidence 链路（工具输出与子代理发现原文
落 `root/eval.db`，`evidence_search` / `evidence_read` 按需加载）、主 Run
的 Run Budget 关闭（两臂一致，评测关注自然完成下的成本）。B 臂的子代理
沿用生产默认子预算（tier 三档 quick/medium/thorough + 父 Run 累计上限），
与生产一致、不另行放宽。唯一差异是 B 臂多一个委派工具，因此对比的是
「有无委派能力」。B 臂的子代理 Trace 同样写入 `eval.db`，用于复盘调查过程。

## 指标口径（报告不得换口径）

| 维度 | 字段 | 口径 |
| --- | --- | --- |
| 主上下文占用 | `main_peak_prompt_tokens` | 主 Run 全部模型请求（含压缩）中最大 prompt 输入 token，**门槛对象** |
| 主 Agent 累计 | `main_chargeable_tokens` | 主 Run 的 MODEL_COMPLETED + summary usage（chargeable 口径） |
| 全部 Agent 累计 | `all_chargeable_tokens` | 主 + 全部子代理（SUBAGENT_* 事件 usage） |
| 回传结果大小 | `result_payload_chars` | 主 Run 内 `subagent_run` 结果字符总量（识别「把内容挪进子代理」） |
| 探索行为 | `main_tool_search_calls` / `main_subagent_attempts` | 主 Run 内 `tool_search` 调用次数、委派尝试次数（含失败尝试，B 臂专用诊断） |
| API 成本 | 报告层换算 | 显式提供单价时用 API 成本；否则回退 chargeable token 并标注 |

正确率：任务标准答案拆成「关键结论」（KeyFact），确定性字符串匹配。
缺失标注 `critical=True` 的结论记一次**安全关键错误**（仅安全子集有）。
字符串口径对「结论缺失」可靠、对「表述含混」会误报，报告保留逐任务明细
供人工抽样复核。

## 判定规则（`reporting.py` 唯一入口）

1. 上下文：`1 - B峰值均值/A峰值均值 ≥ 50%`（逐任务先折叠再跨任务平均）；
2. 正确率：`A正确率 - B正确率 ≤ 5pp`，且安全子集关键错误 `B ≤ A`（0 新增）；
3. 成本：`B总成本/A总成本 ≤ 1.3`（API 成本口径，或回退 token 口径）。

三项全部通过输出 `Go`；任何单项不通过输出 `No-go`。禁止单指标优化。

## 用法

```bash
cd backend

# 离线自检（不产生 API 成本）
.venv/bin/python -m pytest tests/eval/subagent/tests

# 查看跑批参数
.venv/bin/python -m tests.eval.subagent.run_live --help

# 小范围试跑（先确认预算）
.venv/bin/python -m tests.eval.subagent.run_live --arm both \
    --task T01-locate-validate-session --task T18-admin-authorization-gap

# 双套件分别跑批（各 2 条命令；波动建议 --runs 3）
.venv/bin/python -m tests.eval.subagent.run_live --arm both --suite core --runs 3 \
    --provider deepseek --model deepseek-v4-flash \
    --price-input 0.27 --price-output 1.10
.venv/bin/python -m tests.eval.subagent.run_live --arm both --suite large --runs 3 \
    --provider deepseek --model deepseek-v4-flash \
    --price-input 0.27 --price-output 1.10
```

退出码：`0`=Go（或 `--allow-no-go`），`1`=No-go，`2`=无匹配任务。
报告默认写入 `tests/eval/subagent/reports/`（同名 `.md` + `.json`）。

## 网络环境提示

httpx 会跟随系统代理（Windows 上通过注册表读取）。若本机开着本地代理
（如 Watt Toolkit / Steam++，监听 `127.0.0.1` 端口），到 `api.deepseek.com`
的 TLS 可能被中间人导致 `CERTIFICATE_VERIFY_FAILED`（表现为
`ModelAdapterError: ... Connection error`，每次运行秒失败、0 token）。
跑批前先验证直连：

```powershell
$env:NO_PROXY="api.deepseek.com"; $env:no_proxy="api.deepseek.com"
```

若直连可用，给跑批进程加上上述 `NO_PROXY` 即可（不影响系统代理设置）。

## 校准产出

报告附带校准表：每个任务的「预估所需文件数 / A 实际读取数 / B 主读取数 /
B 子代理调用次数 / B tool_search 次数 / B 委派尝试次数」，用于回答两个问题
并校准三个待定参数：

- **行为诊断**：B 臂没降上下文时，区分「模型没找过委派工具」（tool_search
  与委派尝试均为 0）还是「试了但被拒/失败」（尝试 ≥ 1 而调用为 0）；
- 待校准参数：

1. 委派触发阈值（如「预计读取超过 N 个文件才委派」的 N，v3 参考值 8）；
2. tier 三档预算（quick/medium/thorough）的初始值；
3. 重叠委派阈值。

跑批完成后请把报告（含 JSON）归档到本目录 `reports/`，作为阶段 1 之前
的门槛证据。
