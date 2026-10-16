"""Go/No-go 联合判定与报告渲染。

判定规则（阶段 0 决策，禁止单指标优化）：

1. **上下文**：B/A 的主上下文峰值均值下降 ≥ 阈值（默认 50%）；
2. **正确率**：B 的关键结论正确率相对 A 的下降 ≤ 阈值（默认 5 个百分点），
   且安全子集的关键错误「0 新增」（B ≤ A）；
3. **成本**：B/A 的总成本比值 ≤ 阈值（默认 1.3x）。配有单价时用 API 成本
   口径；未配单价时回退到 chargeable token 口径，并在报告中显式标注。

三项全部通过才输出 ``Go``；任何一项不通过输出 ``No-go`` 并列出原因。
正确率与安全关键错误以确定性字符串口径统计（见 ``tasks.py`` 的说明），
报告保留逐任务明细供人工抽样复核。
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean
from typing import Any

from .metrics import Prices, RunMetrics
from .tasks import SECURITY_CATEGORY


@dataclass(frozen=True, slots=True)
class GateThresholds:
    min_context_reduction: float = 0.5
    max_correctness_drop: float = 0.05
    max_cost_ratio: float = 1.3


@dataclass(frozen=True, slots=True)
class GateCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class GateVerdict:
    checks: tuple[GateCheck, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def verdict(self) -> str:
        return "Go" if self.passed else "No-go"


def _mean(values: list[float]) -> float | None:
    return fmean(values) if values else None


def _pooled_fact_rate(runs: tuple[RunMetrics, ...]) -> float:
    total = sum(run.answer.key_fact_total for run in runs)
    if total == 0:
        return 0.0
    matched = sum(run.answer.key_fact_matched for run in runs)
    return matched / total


def _run_mean(runs: tuple[RunMetrics, ...], attribute: str) -> float | None:
    values = [float(getattr(run, attribute)) for run in runs]
    return fmean(values) if values else None


@dataclass(frozen=True, slots=True)
class ArmAggregate:
    """单臂全部运行的汇总。"""

    arm: str
    runs: tuple[RunMetrics, ...]

    @property
    def run_count(self) -> int:
        return len(self.runs)

    @property
    def ok_count(self) -> int:
        return sum(1 for run in self.runs if run.ok)

    @property
    def strict_pass_count(self) -> int:
        return sum(1 for run in self.runs if run.answer.passed)

    @property
    def strict_pass_rate(self) -> float:
        if not self.runs:
            return 0.0
        return self.strict_pass_count / len(self.runs)

    @property
    def key_fact_rate(self) -> float:
        return _pooled_fact_rate(self.runs)

    @property
    def fact_matched(self) -> int:
        return sum(run.answer.key_fact_matched for run in self.runs)

    @property
    def fact_total(self) -> int:
        return sum(run.answer.key_fact_total for run in self.runs)

    @property
    def security_runs(self) -> tuple[RunMetrics, ...]:
        return tuple(run for run in self.runs if run.category == SECURITY_CATEGORY)

    @property
    def security_critical_errors(self) -> int:
        return sum(run.answer.critical_errors for run in self.security_runs)

    @property
    def security_key_fact_rate(self) -> float:
        return _pooled_fact_rate(self.security_runs)

    @property
    def peak_mean(self) -> float | None:
        return _mean(
            [
                float(run.main_peak_prompt_tokens)
                for run in self.runs
                if run.main_peak_prompt_tokens is not None
            ]
        )

    @property
    def peak_max(self) -> int | None:
        values = [
            run.main_peak_prompt_tokens
            for run in self.runs
            if run.main_peak_prompt_tokens is not None
        ]
        return max(values) if values else None

    @property
    def main_chargeable_total(self) -> int:
        return sum(run.main_chargeable_tokens for run in self.runs)

    @property
    def chargeable_total(self) -> int:
        return sum(run.all_chargeable_tokens for run in self.runs)

    @property
    def payload_chars_total(self) -> int:
        return sum(run.result_payload_chars for run in self.runs)

    @property
    def subagent_calls_total(self) -> int:
        return sum(run.subagent_calls for run in self.runs)

    @property
    def tool_search_total(self) -> int:
        return sum(run.main_tool_search_calls for run in self.runs)

    @property
    def subagent_attempts_total(self) -> int:
        return sum(run.main_subagent_attempts for run in self.runs)

    @property
    def duration_total(self) -> float:
        return sum(run.duration_s for run in self.runs)

    def cost_total(self, prices: Prices | None) -> float | None:
        if prices is None:
            return None
        return sum(
            run.cost_with(prices) or 0.0
            for run in self.runs
        )

    @property
    def failures(self) -> tuple[RunMetrics, ...]:
        return tuple(run for run in self.runs if not run.ok)

    def by_category(self) -> dict[str, tuple[RunMetrics, ...]]:
        grouped: dict[str, list[RunMetrics]] = {}
        for run in self.runs:
            grouped.setdefault(run.category, []).append(run)
        return {key: tuple(value) for key, value in grouped.items()}


@dataclass(frozen=True, slots=True)
class TaskPair:
    """同一任务在两臂上的运行（跨 run 折叠）。"""

    task_id: str
    name: str
    category: str
    direct: tuple[RunMetrics, ...]
    delegate: tuple[RunMetrics, ...]

    @property
    def direct_peak_mean(self) -> float | None:
        values = [
            float(run.main_peak_prompt_tokens)
            for run in self.direct
            if run.main_peak_prompt_tokens is not None
        ]
        return fmean(values) if values else None

    @property
    def delegate_peak_mean(self) -> float | None:
        values = [
            float(run.main_peak_prompt_tokens)
            for run in self.delegate
            if run.main_peak_prompt_tokens is not None
        ]
        return fmean(values) if values else None

    @property
    def direct_fact_rate(self) -> float:
        return _pooled_fact_rate(self.direct)

    @property
    def delegate_fact_rate(self) -> float:
        return _pooled_fact_rate(self.delegate)

    @property
    def direct_pass_rate(self) -> float:
        if not self.direct:
            return 0.0
        return sum(1 for run in self.direct if run.answer.passed) / len(self.direct)

    @property
    def delegate_pass_rate(self) -> float:
        if not self.delegate:
            return 0.0
        return sum(1 for run in self.delegate if run.answer.passed) / len(self.delegate)


@dataclass(frozen=True, slots=True)
class Comparison:
    direct: ArmAggregate
    delegate: ArmAggregate
    pairs: tuple[TaskPair, ...]
    thresholds: GateThresholds
    prices: Prices | None
    cost_basis: str
    context_mean_direct: float | None
    context_mean_delegate: float | None
    context_reduction: float | None
    cost_direct: float
    cost_delegate: float
    cost_ratio: float | None
    verdict: GateVerdict


def _build_pairs(
    direct_runs: tuple[RunMetrics, ...],
    delegate_runs: tuple[RunMetrics, ...],
) -> tuple[TaskPair, ...]:
    direct_by_task: dict[str, list[RunMetrics]] = {}
    for run in direct_runs:
        direct_by_task.setdefault(run.task_id, []).append(run)
    delegate_by_task: dict[str, list[RunMetrics]] = {}
    for run in delegate_runs:
        delegate_by_task.setdefault(run.task_id, []).append(run)

    ordered_ids: list[str] = []
    for run in (*direct_runs, *delegate_runs):
        if run.task_id not in ordered_ids:
            ordered_ids.append(run.task_id)

    pairs: list[TaskPair] = []
    for task_id in ordered_ids:
        sample = (direct_by_task.get(task_id) or delegate_by_task.get(task_id))[0]
        pairs.append(
            TaskPair(
                task_id=task_id,
                name=sample.task_name,
                category=sample.category,
                direct=tuple(direct_by_task.get(task_id, ())),
                delegate=tuple(delegate_by_task.get(task_id, ())),
            )
        )
    return tuple(pairs)


def compare_arms(
    direct_runs: tuple[RunMetrics, ...],
    delegate_runs: tuple[RunMetrics, ...],
    *,
    thresholds: GateThresholds | None = None,
    prices: Prices | None = None,
) -> Comparison:
    """构建两臂对比与联合判定（唯一的判定入口）。"""

    resolved = thresholds or GateThresholds()
    direct = ArmAggregate(arm="direct", runs=direct_runs)
    delegate = ArmAggregate(arm="delegate", runs=delegate_runs)
    pairs = _build_pairs(direct_runs, delegate_runs)

    # 上下文：逐任务先折叠，再跨任务平均（A/B 都有效的任务才纳入）。
    direct_peaks: list[float] = []
    delegate_peaks: list[float] = []
    for pair in pairs:
        a_peak = pair.direct_peak_mean
        b_peak = pair.delegate_peak_mean
        if a_peak is None or b_peak is None or a_peak <= 0:
            continue
        direct_peaks.append(a_peak)
        delegate_peaks.append(b_peak)
    context_mean_direct = _mean(direct_peaks)
    context_mean_delegate = _mean(delegate_peaks)
    context_reduction: float | None = None
    if (
        context_mean_direct is not None
        and context_mean_delegate is not None
        and context_mean_direct > 0
    ):
        context_reduction = 1 - (context_mean_delegate / context_mean_direct)

    # 成本：优先 API 成本口径，否则回退 token 口径。
    if prices is not None and direct.cost_total(prices) is not None:
        cost_basis = "api_cost"
        cost_direct = float(direct.cost_total(prices) or 0.0)
        cost_delegate = float(delegate.cost_total(prices) or 0.0)
    else:
        cost_basis = "chargeable_tokens"
        cost_direct = float(direct.chargeable_total)
        cost_delegate = float(delegate.chargeable_total)
    cost_ratio = cost_delegate / cost_direct if cost_direct > 0 else None

    # 判定。
    context_passed = (
        context_reduction is not None
        and context_reduction >= resolved.min_context_reduction
    )
    context_detail = (
        f"A 均值 {context_mean_direct:,.0f} → B 均值 {context_mean_delegate:,.0f}，"
        f"降幅 {context_reduction:.1%}（要求 ≥ {resolved.min_context_reduction:.0%}；"
        "口径：主 Run 最大 prompt token，逐任务先折叠再跨任务平均）"
        if context_reduction is not None
        else "无有效峰值数据（需要两臂都有 provider usage）"
    )

    rate_direct = direct.key_fact_rate
    rate_delegate = delegate.key_fact_rate
    drop = rate_direct - rate_delegate
    correctness_passed = drop <= resolved.max_correctness_drop + 1e-9
    max_drop_label = f"{resolved.max_correctness_drop * 100:.0f}pp"
    correctness_detail = (
        f"关键结论正确率 A {rate_direct:.1%} → B {rate_delegate:.1%}，"
        f"下降 {drop * 100:+.1f}pp（要求 ≤ {max_drop_label}；"
        f"严格通过率 A {direct.strict_pass_rate:.0%} / B "
        f"{delegate.strict_pass_rate:.0%}，仅作参考）"
    )

    critical_direct = direct.security_critical_errors
    critical_delegate = delegate.security_critical_errors
    security_passed = critical_delegate <= critical_direct
    missing_critical = tuple(
        f"{run.task_id}:{fact_id}"
        for run in delegate.security_runs
        for fact_id in run.answer.critical_missing_ids
    )
    security_detail = (
        f"安全子集关键错误 A {critical_direct} → B {critical_delegate}"
        f"（要求 B ≤ A）"
    )
    if missing_critical:
        security_detail += "；B 缺失的关键结论：" + "、".join(missing_critical)

    cost_passed = (
        cost_ratio is not None and cost_ratio <= resolved.max_cost_ratio + 1e-9
    )
    basis_label = (
        f"API 成本（{prices.currency}）"
        if cost_basis == "api_cost" and prices is not None
        else "chargeable token（未配置单价，Token 口径）"
    )
    cost_detail = (
        f"口径 {basis_label}：A {cost_direct:,.4f} → B {cost_delegate:,.4f}，"
        f"比值 {cost_ratio:.2f}（要求 ≤ {resolved.max_cost_ratio:.1f}）"
        if cost_ratio is not None
        else "无有效成本数据（两臂累计为 0）"
    )

    verdict = GateVerdict(
        checks=(
            GateCheck("上下文峰值降幅", context_passed, context_detail),
            GateCheck("正确率下降幅度", correctness_passed, correctness_detail),
            GateCheck("安全关键错误", security_passed, security_detail),
            GateCheck("总成本比值", cost_passed, cost_detail),
        )
    )

    return Comparison(
        direct=direct,
        delegate=delegate,
        pairs=pairs,
        thresholds=resolved,
        prices=prices,
        cost_basis=cost_basis,
        context_mean_direct=context_mean_direct,
        context_mean_delegate=context_mean_delegate,
        context_reduction=context_reduction,
        cost_direct=cost_direct,
        cost_delegate=cost_delegate,
        cost_ratio=cost_ratio,
        verdict=verdict,
    )


def _fmt_number(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.1f}" if isinstance(value, float) else f"{value:,}"


def _fmt_money(value: float | int | None) -> str:
    """金额口径：小额 API 成本需要 4 位小数才能看出差异。"""

    if value is None:
        return "n/a"
    return f"{value:,.4f}"


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def render_markdown(comparison: Comparison, meta: dict[str, Any]) -> str:
    """渲染人读报告（Markdown）。"""

    lines: list[str] = []
    lines.append("# 只读探索子 Agent 阶段 0 · A/B 评测报告")
    lines.append("")
    lines.append(f"- 生成时间：{meta.get('generated_at', '未知')}")
    lines.append(
        f"- 模型：{meta.get('provider', '?')} / {meta.get('model', '?')}"
    )
    lines.append(
        f"- 任务数：{meta.get('task_count', '?')}；每任务轮数："
        f"{meta.get('runs_per_task', '?')}"
    )
    lines.append(f"- 运行根目录：`{meta.get('run_root', '?')}`")
    lines.append(f"- 复现命令：`{meta.get('command', '?')}`")
    lines.append("")
    lines.append(f"## 判定：**{comparison.verdict.verdict}**")
    lines.append("")

    lines.append("| 判定项 | 通过 | 详情 |")
    lines.append("| --- | --- | --- |")
    for check in comparison.verdict.checks:
        marker = "✅" if check.passed else "❌"
        lines.append(f"| {check.name} | {marker} | {check.detail} |")
    lines.append("")
    lines.append(
        "> 联合判定：三项门槛（上下文、正确率含安全子集、成本）必须同时满足"
        "才是 Go；任何单项改善都不构成继续推进的理由。"
    )
    lines.append("")

    direct = comparison.direct
    delegate = comparison.delegate

    lines.append("## 五维总览")
    lines.append("")
    lines.append("| 指标 | A 主 Agent 直接探索 | B 委派子 Agent | 对比 |")
    lines.append("| --- | --- | --- | --- |")
    lines.append(
        f"| 主上下文峰值（均值） | {_fmt_number(comparison.context_mean_direct)} "
        f"| {_fmt_number(comparison.context_mean_delegate)} "
        f"| {_fmt_pct(comparison.context_reduction)} 降幅 |"
    )
    lines.append(
        f"| 主上下文峰值（单任务最大） | {_fmt_number(direct.peak_max)} "
        f"| {_fmt_number(delegate.peak_max)} | — |"
    )
    lines.append(
        f"| 主 Agent 累计 token | {_fmt_number(direct.main_chargeable_total)} "
        f"| {_fmt_number(delegate.main_chargeable_total)} | — |"
    )
    lines.append(
        f"| 全 Agent 累计 chargeable token | {_fmt_number(direct.chargeable_total)} "
        f"| {_fmt_number(delegate.chargeable_total)} | — |"
    )
    lines.append(
        f"| 回传结果大小（字符） | {_fmt_number(direct.payload_chars_total)} "
        f"| {_fmt_number(delegate.payload_chars_total)} | — |"
    )
    cost_label = (
        "API 成本"
        if comparison.cost_basis == "api_cost"
        else "成本（Token 口径）"
    )
    currency = comparison.prices.currency if comparison.prices is not None else ""
    suffix = f" {currency}" if currency else ""
    money = _fmt_money if comparison.cost_basis == "api_cost" else _fmt_number
    lines.append(
        f"| {cost_label} | {money(comparison.cost_direct)}{suffix} "
        f"| {money(comparison.cost_delegate)}{suffix} "
        f"| ×{comparison.cost_ratio:.2f} |"
        if comparison.cost_ratio is not None
        else f"| {cost_label} | {money(comparison.cost_direct)}{suffix} "
        f"| {money(comparison.cost_delegate)}{suffix} | n/a |"
    )
    lines.append(
        f"| 关键结论正确率 | {_fmt_pct(direct.key_fact_rate)} "
        f"| {_fmt_pct(delegate.key_fact_rate)} | — |"
    )
    lines.append(
        f"| 任务严格通过率 | {_fmt_pct(direct.strict_pass_rate)} "
        f"| {_fmt_pct(delegate.strict_pass_rate)} | — |"
    )
    lines.append(
        f"| 任务耗时合计（秒） | {_fmt_number(round(direct.duration_total, 1))} "
        f"| {_fmt_number(round(delegate.duration_total, 1))} | — |"
    )
    lines.append(
        f"| 子代理调用次数 | {_fmt_number(direct.subagent_calls_total)} "
        f"| {_fmt_number(delegate.subagent_calls_total)} | — |"
    )
    lines.append(
        f"| 主 Agent tool_search 次数 | {_fmt_number(direct.tool_search_total)} "
        f"| {_fmt_number(delegate.tool_search_total)} | — |"
    )
    lines.append(
        f"| 委派尝试次数（含失败） | {_fmt_number(direct.subagent_attempts_total)} "
        f"| {_fmt_number(delegate.subagent_attempts_total)} | — |"
    )
    lines.append("")

    lines.append("## 分类正确率（关键结论口径）")
    lines.append("")
    lines.append("| 分类 | A 正确率 | B 正确率 | A 严格通过 | B 严格通过 |")
    lines.append("| --- | --- | --- | --- | --- |")
    categories = sorted(
        set(direct.by_category()) | set(delegate.by_category())
    )
    for category in categories:
        a_runs = direct.by_category().get(category, ())
        b_runs = delegate.by_category().get(category, ())
        a_rate = _pooled_fact_rate(a_runs) if a_runs else None
        b_rate = _pooled_fact_rate(b_runs) if b_runs else None
        a_pass = (
            sum(1 for run in a_runs if run.answer.passed) / len(a_runs)
            if a_runs
            else None
        )
        b_pass = (
            sum(1 for run in b_runs if run.answer.passed) / len(b_runs)
            if b_runs
            else None
        )
        lines.append(
            f"| {category} | {_fmt_pct(a_rate)} | {_fmt_pct(b_rate)} "
            f"| {_fmt_pct(a_pass)} | {_fmt_pct(b_pass)} |"
        )
    lines.append("")

    lines.append("## 安全子集明细")
    lines.append("")
    lines.append(
        "| 任务 | A 关键错误 | B 关键错误 | A 正确率 | B 正确率 | B 缺失的关键结论 |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for pair in comparison.pairs:
        if pair.category != SECURITY_CATEGORY:
            continue
        a_errors = sum(run.answer.critical_errors for run in pair.direct)
        b_errors = sum(run.answer.critical_errors for run in pair.delegate)
        missing = "、".join(
            sorted(
                {
                    fact_id
                    for run in pair.delegate
                    for fact_id in run.answer.critical_missing_ids
                }
            )
        )
        lines.append(
            f"| {pair.task_id} | {a_errors} | {b_errors} "
            f"| {_fmt_pct(pair.direct_fact_rate)} "
            f"| {_fmt_pct(pair.delegate_fact_rate)} | {missing or '—'} |"
        )
    lines.append("")

    lines.append("## 逐任务对比")
    lines.append("")
    lines.append(
        "| 任务 | 分类 | 峰值 A | 峰值 B | 降幅 | 事实 A | 事实 B "
        "| 通过 A | 通过 B | 全Agent token A | 全Agent token B | 回传字符 B |"
    )
    lines.append("| --- " * 12 + "|")
    for pair in comparison.pairs:
        a_peak = pair.direct_peak_mean
        b_peak = pair.delegate_peak_mean
        reduction = (
            f"{1 - b_peak / a_peak:.1%}"
            if a_peak and b_peak and a_peak > 0
            else "n/a"
        )
        a_tokens = sum(run.all_chargeable_tokens for run in pair.direct)
        b_tokens = sum(run.all_chargeable_tokens for run in pair.delegate)
        payload = sum(run.result_payload_chars for run in pair.delegate)
        lines.append(
            f"| {pair.task_id} | {pair.category} "
            f"| {_fmt_number(a_peak)} | {_fmt_number(b_peak)} | {reduction} "
            f"| {_fmt_pct(pair.direct_fact_rate)} "
            f"| {_fmt_pct(pair.delegate_fact_rate)} "
            f"| {_fmt_pct(pair.direct_pass_rate)} "
            f"| {_fmt_pct(pair.delegate_pass_rate)} "
            f"| {_fmt_number(a_tokens)} | {_fmt_number(b_tokens)} "
            f"| {_fmt_number(payload)} |"
        )
    lines.append("")

    lines.append("## 校准数据（委派触发规则用）")
    lines.append("")
    lines.append(
        "| 任务 | 预估所需文件数 | A 读取文件数 | B 主读取 | B 子代理调用 "
        "| B tool_search | B 委派尝试 |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for pair in comparison.pairs:
        sample = (
            pair.direct[0]
            if pair.direct
            else (pair.delegate[0] if pair.delegate else None)
        )
        expected = sample.expected_files if sample is not None else None
        a_files = (
            fmean([run.files_read_main for run in pair.direct])
            if pair.direct
            else None
        )
        b_files = (
            fmean([run.files_read_main for run in pair.delegate])
            if pair.delegate
            else None
        )
        b_calls = sum(run.subagent_calls for run in pair.delegate)
        b_search = sum(run.main_tool_search_calls for run in pair.delegate)
        b_attempts = sum(run.main_subagent_attempts for run in pair.delegate)
        lines.append(
            f"| {pair.task_id} | {expected} | {_fmt_number(a_files)} "
            f"| {_fmt_number(b_files)} | {_fmt_number(b_calls)} "
            f"| {_fmt_number(b_search)} | {_fmt_number(b_attempts)} |"
        )
    lines.append("")

    failures = (*direct.failures, *delegate.failures)
    lines.append("## 失败与异常")
    lines.append("")
    if not failures:
        lines.append("无。")
    else:
        lines.append("| 任务 | 臂 | run | stop_reason | 错误 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for run in failures:
            lines.append(
                f"| {run.task_id} | {run.arm} | {run.run_index} "
                f"| {run.stop_reason or '—'} | {(run.error or '—')[:200]} |"
            )
    lines.append("")
    return "\n".join(lines)


def comparison_to_json(comparison: Comparison, meta: dict[str, Any]) -> dict[str, Any]:
    """机器可比较的完整报告（含逐次运行原始指标）。"""

    return {
        "meta": meta,
        "thresholds": {
            "min_context_reduction": comparison.thresholds.min_context_reduction,
            "max_correctness_drop": comparison.thresholds.max_correctness_drop,
            "max_cost_ratio": comparison.thresholds.max_cost_ratio,
        },
        "cost_basis": comparison.cost_basis,
        "prices": (
            None
            if comparison.prices is None
            else {
                "input_per_million": comparison.prices.input_per_million,
                "output_per_million": comparison.prices.output_per_million,
                "currency": comparison.prices.currency,
            }
        ),
        "verdict": {
            "result": comparison.verdict.verdict,
            "passed": comparison.verdict.passed,
            "checks": [
                {
                    "name": check.name,
                    "passed": check.passed,
                    "detail": check.detail,
                }
                for check in comparison.verdict.checks
            ],
        },
        "summary": {
            "context_mean_direct": comparison.context_mean_direct,
            "context_mean_delegate": comparison.context_mean_delegate,
            "context_reduction": comparison.context_reduction,
            "cost_direct": comparison.cost_direct,
            "cost_delegate": comparison.cost_delegate,
            "cost_ratio": comparison.cost_ratio,
            "direct": _arm_summary(comparison.direct),
            "delegate": _arm_summary(comparison.delegate),
        },
        "tasks": [
            {
                "task_id": pair.task_id,
                "category": pair.category,
                "direct": [run.as_dict() for run in pair.direct],
                "delegate": [run.as_dict() for run in pair.delegate],
            }
            for pair in comparison.pairs
        ],
    }


def _arm_summary(aggregate: ArmAggregate) -> dict[str, Any]:
    return {
        "run_count": aggregate.run_count,
        "ok_count": aggregate.ok_count,
        "strict_pass_rate": round(aggregate.strict_pass_rate, 4),
        "key_fact_rate": round(aggregate.key_fact_rate, 4),
        "security_critical_errors": aggregate.security_critical_errors,
        "security_key_fact_rate": round(aggregate.security_key_fact_rate, 4),
        "peak_mean": aggregate.peak_mean,
        "peak_max": aggregate.peak_max,
        "main_chargeable_total": aggregate.main_chargeable_total,
        "chargeable_total": aggregate.chargeable_total,
        "payload_chars_total": aggregate.payload_chars_total,
        "subagent_calls_total": aggregate.subagent_calls_total,
        "tool_search_total": aggregate.tool_search_total,
        "subagent_attempts_total": aggregate.subagent_attempts_total,
        "duration_total_s": round(aggregate.duration_total, 1),
    }


__all__ = [
    "ArmAggregate",
    "Comparison",
    "GateCheck",
    "GateThresholds",
    "GateVerdict",
    "TaskPair",
    "compare_arms",
    "comparison_to_json",
    "render_markdown",
]
