"""单次运行的指标采集（阶段 0 五维度）。

口径（与阶段 0 决策一致，报告层不得更换口径）：

1. **主上下文占用**：``main_peak_prompt_tokens`` = 主 Run 全部模型请求（含
   上下文压缩请求）中最大的 prompt 输入 token。同时给出末次与均值，供
   观察收敛过程；Go/No-go 门槛只用峰值。
2. **主 Agent 累计消耗**：主 Run 的 MODEL_COMPLETED usage + MODEL_STARTED
   携带的 summary_usage（压缩调用）。
3. **全部 Agent 累计消耗**：主 Agent 累计 + 全部 Sub-Agent（SUBAGENT_COMPLETED /
   SUBAGENT_FAILED 事件携带的 usage）累计。
4. **回传结果大小**：主 Run 内 ``subagent_run`` 工具结果的字符总量；另记
   ``evidence_read`` 的展开字符量（阶段 3 起才可能非零）。该维度用于识别
   「只是把内容挪进子代理」的假优化。
5. **API 成本**：仅当显式提供单价时换算；未提供时报告层回退到 Token 口径，
   并在报告中明确标注所用口径。

Sub-Agent 内部逐请求的 prompt 采样在 V1 不可见（子循环事件不广播到父
Run），因此峰值口径只统计主 Run；子代理的消耗通过 SUBAGENT_* 事件总账
进入维度 3。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from statistics import fmean

from app.agent.budget import chargeable_tokens
from app.agent.events import AgentEvent, AgentEventType
from app.agent.result import AgentResult
from app.models.types import ModelUsage, add_model_usage
from app.subagent import SUBAGENT_TOOL_NAME
from app.tools.catalog import TOOL_SEARCH_NAME
from app.trace.usage import summarize_run_usage

from .tasks import EvalTask, KeyFact

EVIDENCE_READ_TOOL_NAME = "evidence_read"

_SUB_USAGE_EVENTS = frozenset(
    {
        AgentEventType.SUBAGENT_COMPLETED,
        AgentEventType.SUBAGENT_FAILED,
    }
)


@dataclass(frozen=True, slots=True)
class Prices:
    """显式单价（每百万 token），用于成本换算。"""

    input_per_million: float
    output_per_million: float
    currency: str = "USD"


@dataclass(frozen=True, slots=True)
class AnswerScore:
    """最终答复对标准答案（关键结论）的确定性评分。"""

    matched_fact_ids: tuple[str, ...]
    missing_fact_ids: tuple[str, ...]
    critical_missing_ids: tuple[str, ...]
    key_fact_total: int
    key_fact_matched: int

    @property
    def key_fact_rate(self) -> float:
        if self.key_fact_total == 0:
            return 0.0
        return self.key_fact_matched / self.key_fact_total

    @property
    def passed(self) -> bool:
        return self.key_fact_total > 0 and self.key_fact_matched == self.key_fact_total

    @property
    def critical_errors(self) -> int:
        return len(self.critical_missing_ids)


def _fact_matches(fact: KeyFact, text: str) -> bool:
    if fact.match == "all":
        return all(re.search(pattern, text, re.IGNORECASE) for pattern in fact.patterns)
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in fact.patterns)


def score_answer(task: EvalTask, text: str | None) -> AnswerScore:
    """把最终答复与任务关键结论逐条比对（确定性口径）。

    - 答复为空（运行失败 / 未产出最终消息）时全部结论记缺失；
    - ``critical=True`` 的结论缺失计入安全关键错误（安全口径在报告层
      单独汇总，与其他正确率指标分开）。
    """

    haystack = (text or "").replace("\r\n", "\n").strip()
    matched: list[str] = []
    missing: list[str] = []
    for fact in task.key_facts:
        if haystack and _fact_matches(fact, haystack):
            matched.append(fact.id)
        else:
            missing.append(fact.id)
    critical_missing = tuple(
        fact.id for fact in task.key_facts if fact.critical and fact.id in missing
    )
    return AnswerScore(
        matched_fact_ids=tuple(matched),
        missing_fact_ids=tuple(missing),
        critical_missing_ids=critical_missing,
        key_fact_total=len(task.key_facts),
        key_fact_matched=len(matched),
    )


@dataclass(frozen=True, slots=True)
class RunMetrics:
    """单任务单臂一次运行的完整指标。"""

    task_id: str
    task_name: str
    category: str
    arm: str
    run_index: int
    ok: bool
    stop_reason: str | None
    error: str | None
    duration_s: float
    steps: int
    tool_calls: int
    # 维度 1：主上下文占用（prompt token 口径）。
    main_peak_prompt_tokens: int | None
    main_last_prompt_tokens: int | None
    main_mean_prompt_tokens: float | None
    prompt_sample_count: int
    # 维度 2：主 Agent 累计。
    main_input_tokens: int
    main_output_tokens: int
    main_model_calls: int
    main_chargeable_tokens: int
    # 维度 3：全部 Agent 累计。
    sub_input_tokens: int
    sub_output_tokens: int
    sub_model_calls: int
    sub_chargeable_tokens: int
    all_chargeable_tokens: int
    # 维度 4：回传与展开。
    subagent_calls: int
    result_payload_chars: int
    expand_chars: int
    # 探索行为校准数据。
    files_read_main: int
    main_tool_search_calls: int
    main_subagent_attempts: int
    # 任务元数据（用于报告与阈值校准）。
    expected_files: int
    # 正确率。
    answer: AnswerScore

    def cost_with(self, prices: Prices | None) -> float | None:
        """按显式单价估算本 Run 的 API 成本；无单价时返回 None。"""

        if prices is None:
            return None
        input_tokens = self.main_input_tokens + self.sub_input_tokens
        output_tokens = self.main_output_tokens + self.sub_output_tokens
        return (
            input_tokens * prices.input_per_million
            + output_tokens * prices.output_per_million
        ) / 1_000_000.0

    def as_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "task_name": self.task_name,
            "category": self.category,
            "arm": self.arm,
            "run_index": self.run_index,
            "ok": self.ok,
            "stop_reason": self.stop_reason,
            "error": self.error,
            "duration_s": round(self.duration_s, 3),
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "main_peak_prompt_tokens": self.main_peak_prompt_tokens,
            "main_last_prompt_tokens": self.main_last_prompt_tokens,
            "main_mean_prompt_tokens": (
                round(self.main_mean_prompt_tokens, 1)
                if self.main_mean_prompt_tokens is not None
                else None
            ),
            "prompt_sample_count": self.prompt_sample_count,
            "main_input_tokens": self.main_input_tokens,
            "main_output_tokens": self.main_output_tokens,
            "main_model_calls": self.main_model_calls,
            "main_chargeable_tokens": self.main_chargeable_tokens,
            "sub_input_tokens": self.sub_input_tokens,
            "sub_output_tokens": self.sub_output_tokens,
            "sub_model_calls": self.sub_model_calls,
            "sub_chargeable_tokens": self.sub_chargeable_tokens,
            "all_chargeable_tokens": self.all_chargeable_tokens,
            "subagent_calls": self.subagent_calls,
            "result_payload_chars": self.result_payload_chars,
            "expand_chars": self.expand_chars,
            "files_read_main": self.files_read_main,
            "main_tool_search_calls": self.main_tool_search_calls,
            "main_subagent_attempts": self.main_subagent_attempts,
            "expected_files": self.expected_files,
            "answer": {
                "matched": list(self.answer.matched_fact_ids),
                "missing": list(self.answer.missing_fact_ids),
                "critical_missing": list(self.answer.critical_missing_ids),
                "key_fact_total": self.answer.key_fact_total,
                "key_fact_matched": self.answer.key_fact_matched,
                "key_fact_rate": round(self.answer.key_fact_rate, 4),
                "passed": self.answer.passed,
            },
        }


def _prompt_samples(events: tuple[AgentEvent, ...]) -> list[int]:
    """主 Run 的 prompt 输入采样（含压缩请求）。"""

    samples: list[int] = []
    for event in events:
        if event.type is AgentEventType.MODEL_COMPLETED and event.usage is not None:
            if event.usage.input_tokens and event.usage.input_tokens > 0:
                samples.append(event.usage.input_tokens)
        elif (
            event.type is AgentEventType.MODEL_STARTED
            and event.summary_usage is not None
            and event.summary_usage.input_tokens > 0
        ):
            samples.append(event.summary_usage.input_tokens)
    return samples


def _sub_usage(
    events: tuple[AgentEvent, ...],
) -> tuple[ModelUsage, int, int]:
    """汇总全部 Sub-Agent 的 usage、chargeable token 与模型调用数。"""

    usage = ModelUsage()
    chargeable = 0
    model_calls = 0
    for event in events:
        if event.type not in _SUB_USAGE_EVENTS or event.usage is None:
            continue
        usage = add_model_usage(usage, event.usage)
        chargeable += chargeable_tokens(event.usage)
        model_calls += event.usage.model_calls
    return usage, chargeable, model_calls


def _tool_payload_chars(events: tuple[AgentEvent, ...], tool_name: str) -> int:
    total = 0
    for event in events:
        if event.type is not AgentEventType.TOOL_COMPLETED:
            continue
        result = event.tool_result
        if result is None or result.tool_name != tool_name:
            continue
        total += len(result.output or "")
    return total


def _arguments_dict(arguments: object) -> dict[str, object]:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except ValueError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _tool_completed_count(
    events: tuple[AgentEvent, ...],
    tool_name: str,
) -> int:
    """主 Run 内某工具的调用次数（含失败尝试）。"""

    return sum(
        1
        for event in events
        if event.type is AgentEventType.TOOL_COMPLETED
        and event.tool_call is not None
        and event.tool_call.name == tool_name
    )


def _read_file_paths(events: tuple[AgentEvent, ...]) -> frozenset[str]:
    paths: set[str] = set()
    for event in events:
        if event.type is not AgentEventType.TOOL_COMPLETED:
            continue
        tool_call = event.tool_call
        if tool_call is None or tool_call.name != "read_file":
            continue
        path = _arguments_dict(tool_call.arguments).get("path")
        if isinstance(path, str) and path.strip():
            paths.add(path.strip())
    return frozenset(paths)


def build_run_metrics(
    task: EvalTask,
    arm: str,
    *,
    result: AgentResult | None,
    events: tuple[AgentEvent, ...],
    duration_s: float,
    error: str | None,
    run_index: int = 1,
) -> RunMetrics:
    """从一次运行的 AgentResult + 父 Run 事件构建五维指标。"""

    usage_summary = summarize_run_usage(events)
    main_usage = add_model_usage(
        usage_summary.main_agent,
        usage_summary.context_summary,
    )
    sub_usage, sub_chargeable, sub_calls = _sub_usage(events)

    samples = _prompt_samples(events)
    peak = max(samples) if samples else None
    last = samples[-1] if samples else None
    mean = fmean(samples) if samples else None

    subagent_starts = sum(
        1 for event in events if event.type is AgentEventType.SUBAGENT_STARTED
    )

    content: str | None = None
    stop_reason: str | None = None
    steps = 0
    tool_calls = 0
    run_error = error
    if result is not None:
        content = result.content
        stop_reason = result.stop_reason.value
        steps = result.steps
        tool_calls = len(result.tool_calls)
        if run_error is None and result.error is not None:
            run_error = result.error.message

    return RunMetrics(
        task_id=task.id,
        task_name=task.name,
        category=task.category,
        arm=arm,
        run_index=run_index,
        ok=run_error is None and result is not None,
        stop_reason=stop_reason,
        error=run_error,
        duration_s=duration_s,
        steps=steps,
        tool_calls=tool_calls,
        main_peak_prompt_tokens=peak,
        main_last_prompt_tokens=last,
        main_mean_prompt_tokens=mean,
        prompt_sample_count=len(samples),
        main_input_tokens=main_usage.input_tokens,
        main_output_tokens=main_usage.output_tokens,
        main_model_calls=main_usage.model_calls,
        main_chargeable_tokens=usage_summary.main_agent_chargeable_tokens,
        sub_input_tokens=sub_usage.input_tokens,
        sub_output_tokens=sub_usage.output_tokens,
        sub_model_calls=sub_calls,
        sub_chargeable_tokens=sub_chargeable,
        all_chargeable_tokens=(
            usage_summary.main_agent_chargeable_tokens + sub_chargeable
        ),
        subagent_calls=subagent_starts,
        result_payload_chars=_tool_payload_chars(events, SUBAGENT_TOOL_NAME),
        expand_chars=_tool_payload_chars(events, EVIDENCE_READ_TOOL_NAME),
        files_read_main=len(_read_file_paths(events)),
        main_tool_search_calls=_tool_completed_count(events, TOOL_SEARCH_NAME),
        main_subagent_attempts=_tool_completed_count(events, SUBAGENT_TOOL_NAME),
        expected_files=task.expected_files,
        answer=score_answer(task, content),
    )


def cost_of(usage: ModelUsage, prices: Prices) -> float:
    """按显式单价换算一次 usage 的成本。"""

    return (
        usage.input_tokens * prices.input_per_million
        + usage.output_tokens * prices.output_per_million
    ) / 1_000_000.0


__all__ = [
    "EVIDENCE_READ_TOOL_NAME",
    "AnswerScore",
    "Prices",
    "RunMetrics",
    "build_run_metrics",
    "cost_of",
    "score_answer",
]
