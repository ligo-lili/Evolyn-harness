"""指标采集自检：峰值口径、子代理归账、回传大小、答案评分。"""

from __future__ import annotations

from app.agent.events import AgentEvent, AgentEventType
from app.agent.result import AgentResult, AgentStopReason
from app.models.types import (
    Message,
    MessageRole,
    ModelUsage,
    ToolCall,
    ToolResult,
)
from tests.eval.subagent.metrics import (
    Prices,
    build_run_metrics,
    cost_of,
    score_answer,
)
from tests.eval.subagent.tasks import TASKS

_T01 = next(task for task in TASKS if task.id.startswith("T01"))
_T18 = next(task for task in TASKS if task.id.startswith("T18"))


def _event(event_type: AgentEventType, **kwargs: object) -> AgentEvent:
    return AgentEvent(type=event_type, run_id="run-1", **kwargs)


def _completed(input_tokens: int, output_tokens: int) -> AgentEvent:
    return _event(
        AgentEventType.MODEL_COMPLETED,
        usage=ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            model_calls=1,
        ),
    )


def _tool_completed(
    tool_name: str,
    output: str,
    *,
    call_id: str,
    arguments: dict[str, object] | None = None,
) -> AgentEvent:
    return _event(
        AgentEventType.TOOL_COMPLETED,
        tool_call=ToolCall(id=call_id, name=tool_name, arguments=arguments or {}),
        tool_result=ToolResult(
            tool_call_id=call_id,
            tool_name=tool_name,
            success=True,
            output=output,
            duration_ms=1.0,
        ),
    )


def _result(content: str, *, steps: int = 3) -> AgentResult:
    return AgentResult(
        run_id="run-1",
        final_message=Message(role=MessageRole.ASSISTANT, content=content),
        messages=(),
        steps=steps,
        stop_reason=AgentStopReason.FINAL_ANSWER,
    )


def _events() -> tuple[AgentEvent, ...]:
    return (
        _completed(1000, 100),
        _event(
            AgentEventType.MODEL_STARTED,
            summary_usage=ModelUsage(
                input_tokens=1500,
                output_tokens=50,
                total_tokens=1550,
                model_calls=1,
            ),
        ),
        _completed(2000, 200),
        _tool_completed(
            "read_file",
            "x",
            call_id="c1",
            arguments={"path": "src/auth/session.py"},
        ),
        _tool_completed(
            "read_file",
            "y",
            call_id="c2",
            arguments={"path": "src/auth/session.py"},
        ),
        _tool_completed(
            "read_file",
            "z",
            call_id="c3",
            arguments={"path": "tests/test_session.py"},
        ),
        _event(AgentEventType.SUBAGENT_STARTED),
        _tool_completed("tool_search", "{}", call_id="c0"),
        _tool_completed("subagent_run", "abcdef", call_id="c4"),
        _tool_completed("evidence_read", "123", call_id="c5"),
        _event(
            AgentEventType.SUBAGENT_COMPLETED,
            usage=ModelUsage(
                input_tokens=500,
                output_tokens=100,
                total_tokens=600,
                model_calls=2,
            ),
        ),
        _completed(1200, 120),
    )


def test_peak_prompt_uses_main_requests_only() -> None:
    metrics = build_run_metrics(
        _T01,
        "direct",
        result=_result(
            "validate_session 定义在 src/auth/session.py；"
            "会话 expires_at 过期时会被删除。"
        ),
        events=_events(),
        duration_s=1.5,
        error=None,
    )
    # 采样 = 主 Run 的三次完成 + 一次压缩请求（子代理 500 不参与）。
    assert metrics.main_peak_prompt_tokens == 2000
    assert metrics.main_last_prompt_tokens == 1200
    assert metrics.main_mean_prompt_tokens == 1425.0
    assert metrics.prompt_sample_count == 4


def test_usage_accounting_splits_main_and_sub() -> None:
    metrics = build_run_metrics(
        _T01,
        "delegate",
        result=_result("..."),
        events=_events(),
        duration_s=1.5,
        error=None,
    )
    assert metrics.main_input_tokens == 5700
    assert metrics.main_output_tokens == 470
    assert metrics.main_model_calls == 4
    # chargeable = uncached input + output，逐次累加。
    assert metrics.main_chargeable_tokens == 6170
    assert metrics.sub_input_tokens == 500
    assert metrics.sub_output_tokens == 100
    assert metrics.sub_model_calls == 2
    assert metrics.sub_chargeable_tokens == 600
    assert metrics.all_chargeable_tokens == 6770


def test_payload_and_read_calibration() -> None:
    metrics = build_run_metrics(
        _T01,
        "delegate",
        result=_result("..."),
        events=_events(),
        duration_s=1.5,
        error=None,
    )
    assert metrics.subagent_calls == 1
    assert metrics.result_payload_chars == 6
    assert metrics.expand_chars == 3
    assert metrics.files_read_main == 2  # 去重后的读取文件数
    assert metrics.main_tool_search_calls == 1
    assert metrics.main_subagent_attempts == 1


def test_answer_scoring_matches_key_facts() -> None:
    good = score_answer(
        _T01,
        "validate_session 在 src/auth/session.py 中定义；"
        "当 expires_at 过期时删除该会话。",
    )
    assert good.passed
    assert good.key_fact_matched == good.key_fact_total

    partial = score_answer(_T01, "validate_session 会检查过期并删除。")
    assert not partial.passed
    assert "location" in partial.missing_fact_ids

    empty = score_answer(_T01, None)
    assert empty.key_fact_matched == 0
    assert not empty.passed


def test_security_critical_facts_count_errors() -> None:
    # 只给出对比信息，缺少两个 critical 结论。
    score = score_answer(
        _T18,
        "handle_admin_audit 里有 require_permission，审计接口是安全的。",
    )
    assert score.critical_errors >= 2
    assert not score.passed


def test_failed_run_scores_all_facts_missing() -> None:
    metrics = build_run_metrics(
        _T01,
        "direct",
        result=None,
        events=(),
        duration_s=0.1,
        error="RuntimeError: boom",
    )
    assert not metrics.ok
    assert metrics.main_peak_prompt_tokens is None
    assert metrics.answer.key_fact_matched == 0
    assert metrics.error == "RuntimeError: boom"


def test_cost_of_uses_explicit_prices() -> None:
    usage = ModelUsage(input_tokens=1_000_000, output_tokens=100_000)
    cost = cost_of(usage, Prices(input_per_million=2.0, output_per_million=5.0))
    assert cost == 2.0 + 0.5

    metrics = build_run_metrics(
        _T01,
        "direct",
        result=_result("..."),
        events=(_completed(1_000_000, 100_000),),
        duration_s=1.0,
        error=None,
    )
    assert metrics.cost_with(Prices(2.0, 5.0)) == 2.5
    assert metrics.cost_with(None) is None
