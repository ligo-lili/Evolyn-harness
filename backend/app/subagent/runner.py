"""子代理执行器：能力装配、嵌套循环、子预算与结果回流。

子代理不是新的 Run：它在当前工具轮内运行一个隔离的嵌套 ``AgentLoop``，
消息历史为空、能力面被显式收窄、预算由子预算收口；只有结构化结果回流到
主循环，子循环内部消息不进入 Conversation / 滚动摘要。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.agent.budget import RunBudget, RunBudgetMessages
from app.agent.event_stream import EventEmitter
from app.agent.events import (
    AgentEvent,
    AgentEventHandler,
    AgentEventType,
    CompositeEventHandler,
)
from app.agent.loop import AgentLoop
from app.agent.result import AgentError, AgentResult, AgentStopReason
from app.context import ContextManager, ContextSettings, ConversationReducer
from app.memory import CORE_MEMORY_MESSAGE_NAME, MEMORY_POLICY_MESSAGE_NAME
from app.models.registry import ModelAdapterRegistry
from app.models.types import (
    AgentMode,
    Message,
    ModelProvider,
    ModelUsage,
    add_model_usage,
)
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry
from app.trace import SQLiteTraceEventHandler, SQLiteTraceStore

from .capabilities import (
    CAPABILITY_CATALOG,
    CapabilityError,
    CapabilityGrant,
    GrantDowngrade,
    apply_plan_downgrade,
    parse_grants,
)
from .scoped_tools import build_scoped_tools
from .settings import SubagentSettings
from .submit import SUBMIT_RESULT_TOOL_NAME, SubmitResultTool

_MEMORY_LEVELS = ("none", "core", "full")

# 子代理预算提示：80% 收束、95% 收口（只留 submit_result）、100% 硬停。
SUBAGENT_BUDGET_MESSAGES = RunBudgetMessages(
    warning=(
        "预算即将用尽：请基于已有信息给出结论，不要再开始新的调查方向，"
        "必要时直接用 submit_result 提交当前发现。"
    ),
    finalization=(
        "子代理预算已到收口线。不要再调用其他工具：立即调用 submit_result "
        "提交已发现的内容，并把未覆盖范围写入 uncovered；不得虚构结论。"
    ),
    closing=(
        "子代理预算已到收口线，进入 Closing：仅保留交付工具。请立即调用 "
        "submit_result 提交已发现的内容与未覆盖范围。"
    ),
)

_STATUS_BY_STOP_REASON: dict[AgentStopReason, str] = {
    AgentStopReason.FINAL_ANSWER: "completed",
    AgentStopReason.MAX_STEPS: "max_steps",
    AgentStopReason.RUN_BUDGET: "budget",
    AgentStopReason.MODEL_ERROR: "failed",
    AgentStopReason.CONTEXT_ERROR: "context_error",
    AgentStopReason.REPEATED_TOOL_CALL: "repeated_tool_call",
    AgentStopReason.CANCELLED: "cancelled",
    AgentStopReason.INTERRUPTED: "interrupted",
}


@dataclass(frozen=True, slots=True)
class SubAgentOutcome:
    """一次子代理执行的有界结果（组装为回流主循环的 ToolResult 载荷）。"""

    subagent_id: str
    nested_run_id: str
    status: str
    submitted: bool
    truncated: bool
    conclusions: str
    key_files: tuple[str, ...]
    snippets: tuple[dict[str, str], ...]
    uncertainties: tuple[str, ...]
    uncovered: tuple[str, ...]
    grants: tuple[str, ...]
    grants_requested: tuple[str, ...]
    downgrades: tuple[GrantDowngrade, ...]
    memory: str
    steps: int
    tool_calls: int
    usage: ModelUsage
    stop_reason: str | None
    error: str | None

    def as_payload(self, *, max_result_chars: int) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "subagent_id": self.subagent_id,
            "status": self.status,
            "submitted": self.submitted,
            "truncated": self.truncated,
            "uncovered_scope": list(self.uncovered),
            "conclusions": self.conclusions,
            "key_files": list(self.key_files),
            "snippets": [dict(item) for item in self.snippets],
            "uncertainties": list(self.uncertainties),
            "grants_requested": list(self.grants_requested),
            "grants": list(self.grants),
            "memory": self.memory,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "usage": self.usage.model_dump(mode="json"),
            "nested_run_id": self.nested_run_id,
        }
        if self.downgrades:
            payload["grant_downgrades"] = [
                {
                    "requested": item.requested,
                    "effective": item.effective,
                    "reason": item.reason,
                }
                for item in self.downgrades
            ]
        if self.stop_reason:
            payload["stop_reason"] = self.stop_reason
        if self.error:
            payload["error"] = self.error
        return _bound_payload(payload, max_result_chars)


class _SubRunObserver(AgentEventHandler):
    """旁挂观察者：只读子循环事件，判定截断事实并旁路统计用量。

    观察者不参与控制流：``finalizing_seen`` 表示子预算收口语义触发过；
    用量累计用于墙钟超时 / 终止后仍能如实回流已发生的成本。
    """

    def __init__(self) -> None:
        self.warning_seen = False
        self.finalizing_seen = False
        self.usage = ModelUsage()
        self.tool_calls = 0
        self.last_step: int | None = None

    async def emit(self, event: AgentEvent) -> None:
        if event.type is AgentEventType.RUN_BUDGET_WARNING:
            self.warning_seen = True
        elif event.type is AgentEventType.RUN_BUDGET_FINALIZING:
            self.finalizing_seen = True
        if event.type is AgentEventType.MODEL_COMPLETED and event.usage is not None:
            self.usage = add_model_usage(self.usage, event.usage)
        elif (
            event.type is AgentEventType.MODEL_STARTED
            and event.summary_usage is not None
        ):
            self.usage = add_model_usage(self.usage, event.summary_usage)
        if event.type is AgentEventType.TOOL_COMPLETED:
            self.tool_calls += 1
        if event.step is not None:
            self.last_step = event.step


class SubAgentRunner:
    """子树委派的装配器：能力 → scoped registry → 嵌套循环 → 有界结果。"""

    def __init__(
        self,
        model_registry: ModelAdapterRegistry,
        tool_registry: ToolRegistry,
        *,
        tool_executor: ToolExecutor | None = None,
        provider: ModelProvider | str | None = None,
        model: str | None = None,
        context_settings: ContextSettings | None = None,
        conversation_reducer: ConversationReducer | None = None,
        trace_store: SQLiteTraceStore | None = None,
        workspace_root: str | Path | None = None,
        settings: SubagentSettings | None = None,
    ) -> None:
        self._model_registry = model_registry
        self._tool_registry = tool_registry
        self._tool_executor = tool_executor
        self._provider = provider
        self._model = model
        self._context_settings = context_settings or ContextSettings()
        self._conversation_reducer = conversation_reducer
        self._trace_store = trace_store
        self._workspace_root = workspace_root
        self.settings = settings or SubagentSettings()
        # 并发与每 Run 配额是进程内旁路状态：Run 结束时由 run_finalizers 清理。
        self._semaphore = asyncio.Semaphore(self.settings.max_concurrency)
        self._invocations: dict[str, int] = {}

    async def release_run(self, run_id: str) -> None:
        """清理一次 Run 的子代理调用配额（挂到 AgentRuntime.run_finalizers）。"""

        self._invocations.pop(run_id, None)

    async def run(
        self,
        *,
        parent_run_id: str,
        conversation_id: str | None,
        step: int | None,
        task: str,
        grants: Sequence[str],
        memory: str,
        mode: AgentMode,
        memory_messages: Sequence[Message] = (),
        skill_tool_scope: frozenset[str] | None = None,
        emitter: EventEmitter | None = None,
    ) -> SubAgentOutcome:
        """执行一次子代理委派；确定性拒绝直接抛出 CapabilityError。"""

        settings = self.settings
        if not settings.enabled:
            raise CapabilityError("子代理功能已关闭（SUBAGENT_ENABLED=false）")

        grants_requested = parse_grants(grants)
        downgrades: tuple[GrantDowngrade, ...] = ()
        effective: tuple[CapabilityGrant, ...] = grants_requested
        if mode is AgentMode.PLAN:
            effective, downgrades = apply_plan_downgrade(grants_requested)

        tools = build_scoped_tools(
            effective,
            self._tool_registry,
            self._workspace_root,
        )
        tools = self._filter_tools(
            tools,
            mode=mode,
            skill_tool_scope=skill_tool_scope,
        )
        if not tools:
            raise CapabilityError(
                "授予的能力与当前模式 / Active Skill allowed-tools 的交集为空，"
                "无法启动子代理"
            )

        # 配额预留在全部确定性校验之后：被拒绝的委派不消耗次数、不发事件。
        self._reserve_invocation(parent_run_id)

        subagent_id = uuid4().hex[:8]
        nested_run_id = f"{parent_run_id}:sub:{subagent_id}"
        requested_specs = tuple(grant.spec for grant in grants_requested)
        effective_specs = tuple(grant.spec for grant in effective)
        memory_level, memory_block = _memory_injection(memory, memory_messages)
        system_prompt = _build_system_prompt(
            grants=effective,
            tool_names=tuple(tool.definition.name for tool in tools),
            memory_block=memory_block,
        )

        scoped_registry = ToolRegistry()
        for tool in tools:
            scoped_registry.register(tool)
        # submit_result 只存在于子循环，且从第一次请求起就可见。
        scoped_registry.register(SubmitResultTool())
        sub_executor = (
            self._tool_executor.rebind(scoped_registry)
            if self._tool_executor is not None
            else ToolExecutor(scoped_registry)
        )

        observer = _SubRunObserver()
        handlers: list[AgentEventHandler] = [observer]
        if self._trace_store is not None:
            # 子循环内部全量事件只持久化（独立 run_id 命名空间），不广播。
            handlers.insert(0, SQLiteTraceEventHandler(self._trace_store))
        sub_emitter = EventEmitter(
            handler=CompositeEventHandler(*handlers),
            run_id=nested_run_id,
            conversation_id=conversation_id,
        )

        loop = AgentLoop(
            model_registry=self._model_registry,
            tool_registry=scoped_registry,
            tool_executor=sub_executor,
            provider=self._provider,
            model=self._model,
            system_prompt=system_prompt,
            max_steps=settings.max_steps,
            max_tool_rounds=settings.max_tool_rounds,
            max_output_tokens=None,
            context_manager=ContextManager(
                context_settings=self._context_settings,
                conversation_reducer=self._conversation_reducer,
            ),
            task_context_provider=None,
            checkpoint_store=None,
            memory_manager=None,
            skill_store=None,
            skill_context_provider=None,
            run_budget=RunBudget(settings.run_budget_config()),
            terminal_tool_names=frozenset({SUBMIT_RESULT_TOOL_NAME}),
            run_budget_messages=SUBAGENT_BUDGET_MESSAGES,
        )

        if emitter is not None:
            await emitter.emit(
                AgentEventType.SUBAGENT_STARTED,
                step=step,
                subagent_id=subagent_id,
                subagent_grants_requested=requested_specs,
                subagent_grants=effective_specs,
                subagent_memory=memory_level,
            )

        result: AgentResult | None = None
        error_text: str | None = None
        timed_out = False
        try:
            async with self._semaphore:
                async with asyncio.timeout(settings.wall_clock_seconds):
                    # 嵌套循环沿用父 run_id：审批规则、run 级清理与 Evidence
                    # 归属都留在父 Run 边界内；子事件走独立 emitter 命名空间。
                    result = await loop.run(
                        parent_run_id,
                        task,
                        history=(),
                        conversation_id=conversation_id,
                        emitter=sub_emitter,
                        summary_state=None,
                        recovery_checkpoint=None,
                        mode=AgentMode.NORMAL,
                    )
        except TimeoutError:
            timed_out = True
        except asyncio.CancelledError:
            with suppress(Exception):
                await sub_emitter.emit(
                    AgentEventType.AGENT_CANCELLED,
                    step=observer.last_step,
                    stop_reason=AgentStopReason.CANCELLED,
                )
            raise
        except Exception as exc:  # noqa: BLE001 - 结构化返回，不向父循环抛裸异常
            error_text = f"{type(exc).__name__}: {exc}"

        await self._emit_sub_run_terminal(
            sub_emitter,
            observer=observer,
            result=result,
            timed_out=timed_out,
            error_text=error_text,
            wall_clock_seconds=settings.wall_clock_seconds,
        )

        outcome = self._build_outcome(
            settings=settings,
            observer=observer,
            result=result,
            timed_out=timed_out,
            error_text=error_text,
            subagent_id=subagent_id,
            nested_run_id=nested_run_id,
            grants_requested=requested_specs,
            effective=effective_specs,
            downgrades=downgrades,
            memory_level=memory_level,
        )
        await self._emit_parent_summary(
            emitter,
            step=step,
            subagent_id=subagent_id,
            outcome=outcome,
            requested_specs=requested_specs,
            effective_specs=effective_specs,
            memory_level=memory_level,
            result=result,
        )
        return outcome

    def _filter_tools(
        self,
        tools: Sequence[Any],
        *,
        mode: AgentMode,
        skill_tool_scope: frozenset[str] | None,
    ) -> tuple[Any, ...]:
        """模式白名单与 Active Skill allowed-tools 的硬交集（只收窄）。"""

        filtered = []
        for tool in tools:
            name = tool.definition.name
            if not self._tool_registry.is_allowed_for_mode(name, mode):
                continue
            if skill_tool_scope is not None and name not in skill_tool_scope:
                continue
            filtered.append(tool)
        return tuple(filtered)

    def _reserve_invocation(self, run_id: str) -> None:
        count = self._invocations.get(run_id, 0)
        if count >= self.settings.max_invocations_per_run:
            raise CapabilityError(
                "本 Run 的 subagent_run 调用次数已达上限"
                f"（{self.settings.max_invocations_per_run}）"
            )
        self._invocations[run_id] = count + 1

    @staticmethod
    async def _emit_sub_run_terminal(
        emitter: EventEmitter,
        *,
        observer: _SubRunObserver,
        result: AgentResult | None,
        timed_out: bool,
        error_text: str | None,
        wall_clock_seconds: float,
    ) -> None:
        """让子 run 的 Trace 具备终态事件（镜像 AgentRuntime.run 的收尾）。"""

        with suppress(Exception):
            if result is not None:
                event_type = (
                    AgentEventType.AGENT_COMPLETED
                    if result.ok
                    else AgentEventType.AGENT_FAILED
                )
                await emitter.emit(
                    event_type,
                    step=result.steps or None,
                    message=result.final_message,
                    usage=result.usage,
                    stop_reason=result.stop_reason,
                    error=result.error,
                    result=result,
                )
                return
            if timed_out:
                await emitter.emit(
                    AgentEventType.AGENT_FAILED,
                    step=observer.last_step,
                    error=AgentError(
                        type="SubagentTimeoutError",
                        message=(
                            "子代理在 "
                            f"{wall_clock_seconds:g} 秒墙钟内未完成，已中止"
                        ),
                    ),
                )
                return
            await emitter.emit(
                AgentEventType.AGENT_FAILED,
                step=observer.last_step,
                error=AgentError(
                    type="SubagentExecutionError",
                    message=error_text or "子代理执行失败",
                ),
            )

    def _build_outcome(
        self,
        *,
        settings: SubagentSettings,
        observer: _SubRunObserver,
        result: AgentResult | None,
        timed_out: bool,
        error_text: str | None,
        subagent_id: str,
        nested_run_id: str,
        grants_requested: tuple[str, ...],
        effective: tuple[str, ...],
        downgrades: tuple[GrantDowngrade, ...],
        memory_level: str,
    ) -> SubAgentOutcome:
        common: dict[str, Any] = {
            "subagent_id": subagent_id,
            "nested_run_id": nested_run_id,
            "grants": effective,
            "grants_requested": grants_requested,
            "downgrades": downgrades,
            "memory": memory_level,
        }
        if timed_out:
            return SubAgentOutcome(
                **common,
                status="timeout",
                submitted=False,
                truncated=True,
                conclusions=(
                    f"子代理在 {settings.wall_clock_seconds:g} 秒墙钟内未完成，"
                    "已被中止；以下无结构化结论。"
                ),
                key_files=(),
                snippets=(),
                uncertainties=(),
                uncovered=(),
                steps=observer.last_step or 0,
                tool_calls=observer.tool_calls,
                usage=observer.usage,
                stop_reason=None,
                error="timeout",
            )
        if result is None:
            return SubAgentOutcome(
                **common,
                status="failed",
                submitted=False,
                truncated=False,
                conclusions=f"子代理执行失败：{error_text or '未知错误'}",
                key_files=(),
                snippets=(),
                uncertainties=(),
                uncovered=(),
                steps=observer.last_step or 0,
                tool_calls=observer.tool_calls,
                usage=observer.usage,
                stop_reason=None,
                error=error_text or "unknown error",
            )

        submission = _last_submission(result)
        if submission is not None:
            conclusions = submission["conclusions"]
            key_files = tuple(submission["key_files"])
            snippets = tuple(
                {
                    "file": str(item.get("file", "")),
                    "lines": str(item.get("lines", "")),
                    "code": str(item.get("code", "")),
                }
                for item in submission["snippets"]
            )
            uncertainties = tuple(submission["uncertainties"])
            uncovered = tuple(submission["uncovered"])
        else:
            conclusions = (result.content or "").strip()
            # 非正常终结（预算硬停 / 步数耗尽 / 模型错误）时 content 是循环的
            # 通用停止文案；用带停止原因与错误细节的回退结论替换，避免主代理
            # 把"Agent stopped"误读为子代理的调查产出。
            if (
                result.stop_reason is not AgentStopReason.FINAL_ANSWER
                or not conclusions
            ):
                conclusions = _fallback_conclusion(result)
            key_files = ()
            snippets = ()
            uncertainties = ()
            uncovered = ()
        truncated = observer.finalizing_seen or result.stop_reason in (
            AgentStopReason.MAX_STEPS,
            AgentStopReason.RUN_BUDGET,
        )
        return SubAgentOutcome(
            **common,
            status=_STATUS_BY_STOP_REASON.get(
                result.stop_reason, result.stop_reason.value
            ),
            submitted=submission is not None,
            truncated=truncated,
            conclusions=_bound_text(conclusions, settings.conclusion_max_chars),
            key_files=key_files,
            snippets=snippets,
            uncertainties=uncertainties,
            uncovered=uncovered,
            steps=result.steps,
            tool_calls=len(result.tool_calls),
            usage=result.usage,
            stop_reason=result.stop_reason.value,
            error=result.error.message if result.error is not None else None,
        )

    @staticmethod
    async def _emit_parent_summary(
        emitter: EventEmitter | None,
        *,
        step: int | None,
        subagent_id: str,
        outcome: SubAgentOutcome,
        requested_specs: tuple[str, ...],
        effective_specs: tuple[str, ...],
        memory_level: str,
        result: AgentResult | None,
    ) -> None:
        """父 Run 只发摘要事件；子循环内部事件不进入父广播。"""

        if emitter is None:
            return
        failed = outcome.status in ("failed", "timeout")
        payload: dict[str, Any] = {
            "step": step,
            "subagent_id": subagent_id,
            "subagent_grants_requested": requested_specs,
            "subagent_grants": effective_specs,
            "subagent_memory": memory_level,
            "subagent_truncated": outcome.truncated,
            "usage": outcome.usage,
        }
        if result is not None:
            payload["stop_reason"] = result.stop_reason
        if failed:
            payload["error"] = AgentError(
                type=(
                    "SubagentTimeoutError"
                    if outcome.status == "timeout"
                    else "SubagentExecutionError"
                ),
                message=outcome.error or outcome.conclusions,
            )
        await emitter.emit(
            AgentEventType.SUBAGENT_FAILED
            if failed
            else AgentEventType.SUBAGENT_COMPLETED,
            **payload,
        )


def _memory_injection(
    requested: str,
    messages: Sequence[Message],
) -> tuple[str, str]:
    """从父 Run 已注入的 Memory 消息里选择档位并渲染为只读块。

    单一事实来源、零二次检索；Memory Policy 消息永不转发。父上下文没有
    可用记忆时如实返回 ``none``。
    """

    level = requested if requested in _MEMORY_LEVELS else "core"
    if level == "none" or not messages:
        return ("none", "")
    selected: list[Message] = []
    for message in messages:
        if message.name == MEMORY_POLICY_MESSAGE_NAME:
            continue
        if level == "core" and message.name != CORE_MEMORY_MESSAGE_NAME:
            continue
        selected.append(message)
    contents = [
        (message.content or "").strip()
        for message in selected
        if (message.content or "").strip()
    ]
    if not contents:
        return ("none", "")
    return (level, "\n\n".join(contents))


def _build_system_prompt(
    *,
    grants: Sequence[CapabilityGrant],
    tool_names: Sequence[str],
    memory_block: str,
) -> str:
    lines = [
        "你是 Evolyn 的子代理：主代理把一次有边界的调查任务委派给你，"
        "你只对这次任务负责。",
        "",
        "工作约定：",
        "1. 你看不到主代理与用户的对话；信息唯一来源是任务说明、被授予的"
        "能力和下面注入的记忆。",
        "2. 不要向用户提问，也不要编造。无法推进时，在结论中如实说明阻塞"
        "与不确定点。",
        "3. 完成调查后必须调用 submit_result 提交结构化结果（结论、关键文件"
        "与行号、必要的短代码片段、不确定点、未覆盖范围）；提交成功即结束，"
        "不要再调用其他工具。",
        "4. 引用文件时使用工作区相对路径与行号；引用网页时给出 URL。",
        "5. 出现“预算即将用尽”提示时，立即基于已有信息收束；预算收口时把"
        "未覆盖范围写入 uncovered，不得虚构结论。",
        "",
        "你的能力（由主代理显式授予，只能使用映射出的工具）：",
    ]
    available = set(tool_names)
    for grant in grants:
        spec = CAPABILITY_CATALOG[grant.name]
        mapped = [name for name in spec.tools if name in available]
        if grant.argument:
            scope = grant.argument
        elif grant.name == "read_repo":
            scope = "整个工作区"
        else:
            scope = "默认"
        lines.append(
            f"- {grant.spec}：{spec.summary}；可用工具：{', '.join(mapped)}；"
            f"作用域：{scope}"
        )
    prompt = "\n".join(lines)
    if memory_block:
        prompt += (
            "\n\n# 主代理注入的运行时记忆（只读背景，不可修改）\n\n"
            f"{memory_block}"
        )
    return prompt


def _last_submission(result: AgentResult) -> dict[str, Any] | None:
    """取最后一次成功的 submit_result 提交（同轮多次提交取最后一次）。"""

    for record in reversed(result.tool_calls):
        if (
            record.tool_call.name != SUBMIT_RESULT_TOOL_NAME
            or not record.result.success
            or not record.result.output
        ):
            continue
        try:
            payload = json.loads(record.result.output)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and isinstance(
            payload.get("conclusions"), str
        ):
            return payload
    return None


def _fallback_conclusion(result: AgentResult) -> str:
    if result.error is not None:
        return (
            "子代理未按协议提交结构化结果"
            f"（停止原因：{result.stop_reason.value}）：{result.error.message}"
        )
    return f"子代理未按协议提交结构化结果（停止原因：{result.stop_reason.value}）。"


def _bound_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}…"


def _bound_payload(payload: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """把结果载荷压到有界长度；优先保留结论与未覆盖范围。"""

    if _payload_len(payload) <= max_chars:
        return payload
    payload["result_truncated"] = True
    for field in ("snippets", "uncertainties", "key_files"):
        if _payload_len(payload) <= max_chars:
            break
        if payload.get(field):
            payload[field] = []
    if _payload_len(payload) > max_chars:
        conclusions = payload.get("conclusions")
        if isinstance(conclusions, str):
            overflow = _payload_len(payload) - max_chars
            keep = max(120, len(conclusions) - overflow - 1)
            payload["conclusions"] = f"{conclusions[:keep]}…"
    return payload


def _payload_len(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False))


__all__ = [
    "SUBAGENT_BUDGET_MESSAGES",
    "SubAgentOutcome",
    "SubAgentRunner",
]
