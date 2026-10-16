"""联合判定与报告渲染自检。"""

from __future__ import annotations

import json

from tests.eval.subagent.metrics import AnswerScore, Prices, RunMetrics
from tests.eval.subagent.reporting import (
    GateThresholds,
    compare_arms,
    comparison_to_json,
    render_markdown,
)


def _run(
    task_id: str,
    arm: str,
    *,
    category: str = "function_locate",
    peak: int = 10_000,
    matched: int = 4,
    total: int = 4,
    critical: int = 0,
    chargeable: int = 1000,
    input_tokens: int = 0,
    output_tokens: int = 0,
    payload: int = 0,
) -> RunMetrics:
    missing = tuple(f"f{index}" for index in range(matched, total))
    critical_missing = tuple(f"c{index}" for index in range(critical))
    return RunMetrics(
        task_id=task_id,
        task_name=task_id,
        category=category,
        arm=arm,
        run_index=1,
        ok=True,
        stop_reason="final_answer",
        error=None,
        duration_s=1.0,
        steps=3,
        tool_calls=2,
        main_peak_prompt_tokens=peak,
        main_last_prompt_tokens=peak,
        main_mean_prompt_tokens=float(peak),
        prompt_sample_count=3,
        main_input_tokens=input_tokens,
        main_output_tokens=output_tokens,
        main_model_calls=3,
        main_chargeable_tokens=chargeable,
        sub_input_tokens=0,
        sub_output_tokens=0,
        sub_model_calls=0,
        sub_chargeable_tokens=0,
        all_chargeable_tokens=chargeable,
        subagent_calls=0,
        result_payload_chars=payload,
        expand_chars=0,
        files_read_main=2,
        main_tool_search_calls=1,
        main_subagent_attempts=1,
        expected_files=2,
        answer=AnswerScore(
            matched_fact_ids=tuple(f"f{index}" for index in range(matched)),
            missing_fact_ids=missing,
            critical_missing_ids=critical_missing,
            key_fact_total=total,
            key_fact_matched=matched,
        ),
    )


def _arm(
    arm: str,
    *,
    peak: int,
    matched: int = 4,
    critical: int = 0,
    chargeable: int = 1000,
) -> tuple[RunMetrics, ...]:
    return (
        _run(
            "T01-locate-validate-session",
            arm,
            peak=peak,
            matched=matched,
            total=4,
            chargeable=chargeable,
        ),
        _run(
            "T18-admin-authorization-gap",
            arm,
            category="security",
            peak=peak,
            matched=matched,
            total=4,
            critical=critical,
            chargeable=chargeable,
        ),
    )


def _check(comparison, name: str):
    return next(check for check in comparison.verdict.checks if check.name == name)


def test_joint_gate_go() -> None:
    comparison = compare_arms(
        _arm("direct", peak=10_000, chargeable=2000),
        _arm("delegate", peak=4000, chargeable=2400),
    )
    assert comparison.verdict.passed
    assert comparison.verdict.verdict == "Go"
    assert comparison.context_reduction == 0.6
    assert _check(comparison, "总成本比值").passed
    # 未配单价：成本回退 Token 口径并标注。
    assert comparison.cost_basis == "chargeable_tokens"
    assert "chargeable token" in _check(comparison, "总成本比值").detail


def test_context_gate_fails() -> None:
    comparison = compare_arms(
        _arm("direct", peak=10_000),
        _arm("delegate", peak=8000),
    )
    assert not comparison.verdict.passed
    assert comparison.verdict.verdict == "No-go"
    context_check = _check(comparison, "上下文峰值降幅")
    assert not context_check.passed
    # 其余门槛仍应通过：单指标失败不影响其他门槛的独立判定。
    assert _check(comparison, "正确率下降幅度").passed
    assert _check(comparison, "总成本比值").passed


def test_correctness_gate_fails_beyond_five_points() -> None:
    direct = (
        _run("T01-locate-validate-session", "direct", peak=10_000),
        _run(
            "T18-admin-authorization-gap",
            "direct",
            category="security",
            peak=10_000,
        ),
    )
    delegate = (
        _run("T01-locate-validate-session", "delegate", peak=4000),
        _run(
            "T18-admin-authorization-gap",
            "delegate",
            category="security",
            peak=4000,
            matched=3,
        ),
    )
    comparison = compare_arms(direct, delegate)
    correctness = _check(comparison, "正确率下降幅度")
    assert not correctness.passed
    assert "12.5pp" in correctness.detail
    assert not comparison.verdict.passed


def test_security_gate_fails_with_new_critical_error() -> None:
    comparison = compare_arms(
        _arm("direct", peak=10_000, critical=0),
        _arm("delegate", peak=4000, matched=2, critical=2),
    )
    security = _check(comparison, "安全关键错误")
    assert not security.passed
    assert "c0" in security.detail
    assert not comparison.verdict.passed


def test_cost_gate_fails_above_ratio() -> None:
    comparison = compare_arms(
        _arm("direct", peak=10_000, chargeable=2000),
        _arm("delegate", peak=4000, chargeable=3000),
    )
    cost = _check(comparison, "总成本比值")
    assert not cost.passed
    assert comparison.cost_ratio == 1.5
    assert not comparison.verdict.passed


def test_cost_basis_prefers_explicit_prices() -> None:
    direct = (
        _run(
            "T01-locate-validate-session",
            "direct",
            peak=10_000,
            input_tokens=1_000_000,
        ),
    )
    delegate = (
        _run(
            "T01-locate-validate-session",
            "delegate",
            peak=4000,
            input_tokens=2_000_000,
        ),
    )
    comparison = compare_arms(
        direct,
        delegate,
        prices=Prices(input_per_million=1.0, output_per_million=2.0),
    )
    assert comparison.cost_basis == "api_cost"
    assert comparison.cost_direct == 1.0
    assert comparison.cost_delegate == 2.0
    assert comparison.cost_ratio == 2.0
    assert not _check(comparison, "总成本比值").passed
    # 金额口径保留 4 位小数，避免小额成本在报告中显示为 0。
    detail = _check(comparison, "总成本比值").detail
    assert "1.0000" in detail and "2.0000" in detail


def test_thresholds_are_configurable() -> None:
    comparison = compare_arms(
        _arm("direct", peak=10_000, chargeable=2000),
        _arm("delegate", peak=6000, chargeable=3000),
        thresholds=GateThresholds(
            min_context_reduction=0.3,
            max_cost_ratio=2.0,
        ),
    )
    assert comparison.verdict.passed


def test_render_and_json_are_stable() -> None:
    comparison = compare_arms(
        _arm("direct", peak=10_000, chargeable=2000),
        _arm("delegate", peak=4000, chargeable=2400),
    )
    meta = {"provider": "fake", "model": "fake-model"}
    report = render_markdown(comparison, meta)
    assert "判定：**Go**" in report
    assert "安全子集明细" in report
    assert "校准数据" in report

    payload = comparison_to_json(comparison, meta)
    text = json.dumps(payload, ensure_ascii=False)
    assert '"result": "Go"' in text
    # 原始逐任务数据可序列化并保留。
    assert payload["tasks"][0]["direct"][0]["answer"]["key_fact_rate"] == 1.0
