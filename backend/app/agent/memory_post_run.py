"""Agent 主循环完成后的长期记忆整理与容量维护。"""

from __future__ import annotations

import logging
from collections.abc import Callable

from app.context.tokens import default_token_estimator
from app.memory import (
    MaintenanceAction,
    MemoryMaintenanceCandidate,
    MemoryMaintenanceInput,
    MemoryMaintenanceReflector,
    MemoryManager,
    MemoryRecord,
    MemoryReflectionConfig,
    MemoryReflectionInput,
    PostRunMemoryReflector,
    ReflectionAction,
    ReflectionDecision,
    decide_reflection_gate,
)
from app.models.types import ModelUsage
from app.task.context import TaskContextProvider

from .event_stream import EventEmitter
from .events import AgentEventType
from .post_run_processor import PostRunSubmitResult
from .result import AgentResult, AgentStopReason
from .runtime_helpers import (
    recalled_memory_revisions,
    reflection_tool_context,
)

logger = logging.getLogger("evolyn.agent.memory_post_run")


class PostRunMemoryCoordinator:
    """在 Agent critical path 结束后协调 Reflection 与容量维护。"""

    def __init__(
        self,
        *,
        manager: MemoryManager | None,
        reflector: PostRunMemoryReflector | None,
        maintenance_reflector: MemoryMaintenanceReflector | None,
        task_context_provider: TaskContextProvider | None,
        submit: Callable[..., PostRunSubmitResult] | None,
    ) -> None:
        self._manager = manager
        self._reflector = reflector
        self._maintenance_reflector = maintenance_reflector
        self._task_context_provider = task_context_provider
        self._submit = submit

    async def schedule(
        self,
        result: AgentResult,
        *,
        user_input: str,
        conversation_id: str | None,
        emitter: EventEmitter,
    ) -> None:
        """生成稳定快照并调度 Post-Run Memory；失败不改变主结果。"""

        reflector = self._reflector
        manager = self._manager

        if reflector is None:
            if (
                self._maintenance_reflector is not None
                and result.stop_reason is AgentStopReason.FINAL_ANSWER
            ):
                await self.ensure_capacity(required_slots=0, emitter=emitter)
            return
        if not reflector.enabled:
            await emitter.emit(
                AgentEventType.MEMORY_REFLECTION_SKIPPED,
                reflection_triggered=False,
                reflection_skip_reason="disabled",
            )
            if (
                self._maintenance_reflector is not None
                and result.stop_reason is AgentStopReason.FINAL_ANSWER
            ):
                await self.ensure_capacity(required_slots=0, emitter=emitter)
            return
        if result.stop_reason is not AgentStopReason.FINAL_ANSWER:
            await emitter.emit(
                AgentEventType.MEMORY_REFLECTION_SKIPPED,
                reflection_triggered=False,
                reflection_skip_reason=f"stop_reason={result.stop_reason.value}",
            )
            return

        recalled_revisions = recalled_memory_revisions(result.tool_calls)
        if reflector.config.gate_enabled:
            gate = decide_reflection_gate(
                user_input,
                recalled_memory_ids=tuple(recalled_revisions),
            )
            if not gate.should_reflect:
                await emitter.emit(
                    AgentEventType.MEMORY_REFLECTION_SKIPPED,
                    reflection_triggered=False,
                    reflection_skip_reason=f"gate:{gate.reason.value}",
                )
                if manager is not None:
                    await self.ensure_capacity(required_slots=0, emitter=emitter)
                return

        try:
            if manager is None:
                raise RuntimeError("memory manager unavailable")
            core_memory, memory_index = await manager.reflection_context()
            task_context = ""
            if self._task_context_provider is not None:
                task_message = await self._task_context_provider.message_for(
                    conversation_id
                )
                if task_message is not None:
                    task_context = task_message.content or ""
            reflection_input = MemoryReflectionInput(
                run_id=result.run_id,
                conversation_id=conversation_id,
                user_input=user_input[: reflector.config.max_user_chars],
                final_answer=(result.final_message.content or "")[
                    : reflector.config.max_answer_chars
                ],
                tool_context=reflection_tool_context(
                    result.tool_calls,
                    max_chars=reflector.config.max_tool_context_chars,
                ),
                recalled_memory_ids=tuple(recalled_revisions),
                core_memory=core_memory,
                memory_index=memory_index,
                task_context=task_context,
            )
        except Exception as exc:
            await emitter.emit(
                AgentEventType.MEMORY_REFLECTION_FAILED,
                reflection_triggered=True,
                reflection_error=f"{type(exc).__name__}: {exc}",
                reflection_mutation_applied=False,
                provider=reflector.provider_hint,
                model=reflector.model_hint,
                usage=ModelUsage(),
            )
            if manager is not None:
                await self.ensure_capacity(required_slots=0, emitter=emitter)
            return

        async def job() -> None:
            await self._run_reflection(
                reflector=reflector,
                manager=manager,
                reflection_input=reflection_input,
                recalled_revisions=recalled_revisions,
                emitter=emitter,
            )

        if self._submit is not None:
            submitted = self._submit(
                job,
                conversation_id=conversation_id,
                run_id=result.run_id,
            )
            if not submitted:
                # 后台处理器拒绝（closed / saturated）：Post-Run 任务被丢弃。
                # 必须留下可观察的 skipped 事件，绝不允许静默消失。
                reason = getattr(submitted, "reason", None) or "unavailable"
                await emitter.emit(
                    AgentEventType.MEMORY_REFLECTION_SKIPPED,
                    reflection_triggered=False,
                    reflection_skip_reason=f"post_run_dropped:{reason}",
                )
                return
        else:
            # 直接构造 Runtime 的调用方没有后台 Processor，保持同步 fallback。
            await job()

    async def _run_reflection(
        self,
        *,
        reflector: PostRunMemoryReflector,
        manager: MemoryManager,
        reflection_input: MemoryReflectionInput,
        recalled_revisions: dict[str, int],
        emitter: EventEmitter,
    ) -> None:
        """执行 Memory Reflection 的决策、应用与后续维护。"""

        await emitter.emit(
            AgentEventType.MEMORY_REFLECTION_STARTED,
            reflection_triggered=True,
            provider=reflector.provider_hint,
            model=reflector.model_hint,
        )
        proposal = None
        memory_id: str | None = None
        mutation_applied = False
        dedup_outcome: str | None = None
        dedup_memory_id: str | None = None
        dedup_similarity: float | None = None
        try:
            proposal = await reflector.decide(reflection_input)
            if proposal.error is not None:
                raise RuntimeError(proposal.error)
            if proposal.decision is None:
                raise RuntimeError("reflection model returned no decision")

            decision = proposal.decision
            _enforce_reflection_output_budget(decision, reflector.config)
            if decision.action is ReflectionAction.UPDATE:
                memory_id = decision.memory_id
                expected_revision = recalled_revisions.get(memory_id or "")
                if expected_revision is None:
                    raise ValueError(
                        "reflection update requires memory_read success in current run"
                    )
                record = await manager.update_if_revision(
                    memory_id or "",
                    expected_revision=expected_revision,
                    title=decision.title or "",
                    summary=decision.summary or "",
                    content=decision.content or "",
                    reason=decision.reason,
                )
                memory_id = record.id
                mutation_applied = True
            elif decision.action is ReflectionAction.APPEND:
                memory_id = decision.memory_id
                expected_revision = recalled_revisions.get(memory_id or "")
                if expected_revision is None:
                    raise ValueError(
                        "reflection append requires memory_read success in current run"
                    )
                addition = decision.addition or ""
                if len(addition) > reflector.config.max_append_chars:
                    raise ValueError(
                        "reflection append exceeds max_append_chars: "
                        f"{len(addition)} > {reflector.config.max_append_chars}"
                    )
                record = await manager.append_if_revision(
                    memory_id or "",
                    expected_revision=expected_revision,
                    addition=addition,
                    reason=decision.reason,
                )
                memory_id = record.id
                mutation_applied = True
            elif decision.action is ReflectionAction.CREATE:
                manager.validate_create(
                    title=decision.title or "",
                    summary=decision.summary or "",
                    content=decision.content or "",
                )
                (
                    merged_record,
                    dedup_outcome,
                    dedup_memory_id,
                    dedup_similarity,
                ) = await _apply_create_dedup(
                    manager=manager,
                    decision=decision,
                    config=reflector.config,
                )
                if merged_record is not None:
                    # 强重复：已自动追加到已有记忆，不再新建。
                    memory_id = merged_record.id
                    mutation_applied = True
                else:
                    capacity_ready = await self.ensure_capacity(
                        required_slots=1,
                        emitter=emitter,
                    )
                    if not capacity_ready:
                        raise RuntimeError(
                            "memory create skipped because capacity is unavailable"
                        )
                    record = await manager.create_if_capacity(
                        title=decision.title or "",
                        summary=decision.summary or "",
                        content=decision.content or "",
                    )
                    if record is None:
                        raise RuntimeError(
                            "memory create lost the available slot to a concurrent run"
                        )
                    memory_id = record.id
                    mutation_applied = True
        except Exception as exc:
            provider = (
                proposal.provider
                if proposal is not None
                else reflector.provider_hint
            )
            model = proposal.model if proposal is not None else reflector.model_hint
            usage = proposal.usage if proposal is not None else ModelUsage()
            duration_ms = proposal.duration_ms if proposal is not None else None
            await emitter.emit(
                AgentEventType.MEMORY_REFLECTION_FAILED,
                reflection_triggered=True,
                reflection_action=(
                    proposal.decision.action.value
                    if proposal is not None and proposal.decision is not None
                    else None
                ),
                reflection_duration_ms=duration_ms,
                reflection_attempts=(
                    proposal.attempts if proposal is not None else None
                ),
                reflection_finish_reason=(
                    proposal.finish_reason if proposal is not None else None
                ),
                reflection_error=f"{type(exc).__name__}: {exc}",
                reflection_memory_id=(
                    proposal.decision.memory_id
                    if proposal is not None and proposal.decision is not None
                    else None
                ),
                reflection_mutation_applied=False,
                reflection_dedup_outcome=dedup_outcome,
                reflection_dedup_memory_id=dedup_memory_id,
                reflection_dedup_similarity=dedup_similarity,
                reflection_input_json=(
                    proposal.input_json if proposal is not None else None
                ),
                reflection_raw_output=(
                    proposal.raw_output if proposal is not None else None
                ),
                provider=provider,
                model=model,
                usage=usage,
            )
            if (
                proposal is None
                or proposal.decision is None
                or proposal.decision.action is not ReflectionAction.CREATE
            ):
                await self.ensure_capacity(required_slots=0, emitter=emitter)
            return

        maintenance_required = await manager.maintenance_required()
        candidate_ids: tuple[str, ...] = ()
        if maintenance_required:
            candidates = await manager.retention_candidates()
            candidate_ids = tuple(item.id for item in candidates)
        await emitter.emit(
            AgentEventType.MEMORY_REFLECTION_COMPLETED,
            reflection_triggered=True,
            reflection_action=proposal.decision.action.value,
            reflection_duration_ms=proposal.duration_ms,
            reflection_attempts=proposal.attempts,
            reflection_finish_reason=proposal.finish_reason,
            reflection_memory_id=memory_id,
            reflection_mutation_applied=mutation_applied,
            reflection_maintenance_required=maintenance_required,
            reflection_retention_candidate_ids=candidate_ids,
            reflection_dedup_outcome=dedup_outcome,
            reflection_dedup_memory_id=dedup_memory_id,
            reflection_dedup_similarity=dedup_similarity,
            reflection_input_json=proposal.input_json,
            reflection_raw_output=proposal.raw_output,
            provider=proposal.provider,
            model=proposal.model,
            usage=proposal.usage,
        )
        if maintenance_required:
            await self.ensure_capacity(required_slots=0, emitter=emitter)

    async def ensure_capacity(
        self,
        *,
        required_slots: int,
        emitter: EventEmitter,
    ) -> bool:
        """隔离整个容量协调路径，任何异常都不能改变主 AgentResult。"""

        try:
            return await self._ensure_capacity_impl(
                required_slots=required_slots,
                emitter=emitter,
            )
        except Exception as exc:
            await emitter.emit(
                AgentEventType.MEMORY_MAINTENANCE_FAILED,
                maintenance_triggered=True,
                maintenance_error=f"{type(exc).__name__}: {exc}",
            )
            return False

    async def _ensure_capacity_impl(
        self,
        *,
        required_slots: int,
        emitter: EventEmitter,
    ) -> bool:
        """通过可恢复归档腾出容量；无法安全维护时返回 False。"""

        manager = self._manager
        if manager is None:
            return False
        active_count = await manager.active_count()
        if active_count + required_slots <= manager.max_active:
            return True

        maintainer = self._maintenance_reflector
        if maintainer is None or not maintainer.enabled:
            await emitter.emit(
                AgentEventType.MEMORY_MAINTENANCE_SKIPPED,
                maintenance_triggered=False,
                maintenance_skip_reason=(
                    "unavailable" if maintainer is None else "disabled"
                ),
                maintenance_active_count=active_count,
                maintenance_max_active=manager.max_active,
                maintenance_remaining_overflow=max(
                    0,
                    active_count + required_slots - manager.max_active,
                ),
            )
            return False

        for _ in range(maintainer.config.max_actions):
            active_count = await manager.active_count()
            remaining = active_count + required_slots - manager.max_active
            if remaining <= 0:
                return True
            records = await manager.retention_candidates(
                limit=maintainer.config.candidate_limit
            )
            record_snapshots = {record.id: record for record in records}
            candidates = tuple(
                MemoryMaintenanceCandidate(
                    id=record.id,
                    title=record.title,
                    summary=record.summary,
                    content=record.content,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                    last_accessed_at=record.last_accessed_at,
                    access_count=record.access_count,
                )
                for record in records
            )
            candidate_ids = tuple(candidate.id for candidate in candidates)
            if not candidates:
                await emitter.emit(
                    AgentEventType.MEMORY_MAINTENANCE_FAILED,
                    maintenance_triggered=True,
                    maintenance_error="no active maintenance candidates",
                    maintenance_active_count=active_count,
                    maintenance_max_active=manager.max_active,
                    maintenance_remaining_overflow=max(0, remaining),
                )
                return False
            await emitter.emit(
                AgentEventType.MEMORY_MAINTENANCE_STARTED,
                maintenance_triggered=True,
                maintenance_active_count=active_count,
                maintenance_max_active=manager.max_active,
                maintenance_candidate_ids=candidate_ids,
                maintenance_remaining_overflow=max(0, remaining),
                provider=maintainer.provider_hint,
                model=maintainer.model_hint,
            )
            proposal = await maintainer.decide(
                MemoryMaintenanceInput(
                    active_count=active_count,
                    max_active=manager.max_active,
                    required_slots=required_slots,
                    candidates=candidates,
                )
            )
            if proposal.error is not None or proposal.decision is None:
                await emitter.emit(
                    AgentEventType.MEMORY_MAINTENANCE_FAILED,
                    maintenance_triggered=True,
                    maintenance_duration_ms=proposal.duration_ms,
                    maintenance_error=(
                        proposal.error or "maintenance model returned no decision"
                    ),
                    maintenance_active_count=active_count,
                    maintenance_max_active=manager.max_active,
                    maintenance_candidate_ids=candidate_ids,
                    maintenance_remaining_overflow=max(0, remaining),
                    provider=proposal.provider,
                    model=proposal.model,
                    usage=proposal.usage,
                )
                return False

            decision = proposal.decision
            if decision.action is MaintenanceAction.DEFER:
                await emitter.emit(
                    AgentEventType.MEMORY_MAINTENANCE_COMPLETED,
                    maintenance_triggered=True,
                    maintenance_action=decision.action.value,
                    maintenance_duration_ms=proposal.duration_ms,
                    maintenance_reason=decision.reason,
                    maintenance_active_count=active_count,
                    maintenance_max_active=manager.max_active,
                    maintenance_candidate_ids=candidate_ids,
                    maintenance_remaining_overflow=max(0, remaining),
                    provider=proposal.provider,
                    model=proposal.model,
                    usage=proposal.usage,
                )
                return False

            memory_id = decision.memory_id or ""
            snapshots = {candidate.id: candidate for candidate in candidates}
            if snapshots.get(memory_id) is None:
                await emitter.emit(
                    AgentEventType.MEMORY_MAINTENANCE_FAILED,
                    maintenance_triggered=True,
                    maintenance_action=decision.action.value,
                    maintenance_duration_ms=proposal.duration_ms,
                    maintenance_error=(
                        "maintenance selected an ID outside the candidate set"
                    ),
                    maintenance_memory_id=memory_id,
                    maintenance_active_count=active_count,
                    maintenance_max_active=manager.max_active,
                    maintenance_candidate_ids=candidate_ids,
                    maintenance_remaining_overflow=max(0, remaining),
                    provider=proposal.provider,
                    model=proposal.model,
                    usage=proposal.usage,
                )
                return False
            try:
                archived = await manager.archive_if_unchanged(
                    memory_id,
                    expected_record=record_snapshots[memory_id],
                    reason=decision.reason,
                )
            except Exception as exc:
                await emitter.emit(
                    AgentEventType.MEMORY_MAINTENANCE_FAILED,
                    maintenance_triggered=True,
                    maintenance_action=decision.action.value,
                    maintenance_duration_ms=proposal.duration_ms,
                    maintenance_error=f"{type(exc).__name__}: {exc}",
                    maintenance_memory_id=memory_id,
                    maintenance_reason=decision.reason,
                    maintenance_active_count=active_count,
                    maintenance_max_active=manager.max_active,
                    maintenance_candidate_ids=candidate_ids,
                    maintenance_remaining_overflow=max(0, remaining),
                    provider=proposal.provider,
                    model=proposal.model,
                    usage=proposal.usage,
                )
                return False
            after_count = await manager.active_count()
            after_remaining = max(
                0,
                after_count + required_slots - manager.max_active,
            )
            await emitter.emit(
                AgentEventType.MEMORY_MAINTENANCE_COMPLETED,
                maintenance_triggered=True,
                maintenance_action=decision.action.value,
                maintenance_duration_ms=proposal.duration_ms,
                maintenance_memory_id=archived.id,
                maintenance_reason=decision.reason,
                maintenance_active_count=after_count,
                maintenance_max_active=manager.max_active,
                maintenance_candidate_ids=candidate_ids,
                maintenance_remaining_overflow=after_remaining,
                provider=proposal.provider,
                model=proposal.model,
                usage=proposal.usage,
            )

        active_count = await manager.active_count()
        remaining = max(0, active_count + required_slots - manager.max_active)
        if remaining == 0:
            return True
        await emitter.emit(
            AgentEventType.MEMORY_MAINTENANCE_SKIPPED,
            maintenance_triggered=True,
            maintenance_skip_reason="max_actions_reached",
            maintenance_active_count=active_count,
            maintenance_max_active=manager.max_active,
            maintenance_remaining_overflow=remaining,
        )
        return False


def _normalize_title_key(title: str) -> str:
    """标题归一化：忽略大小写、空白与标点，用于确定性重复比对。"""

    return "".join(
        character for character in title.casefold() if character.isalnum()
    )


async def _apply_create_dedup(
    *,
    manager: MemoryManager,
    decision: ReflectionDecision,
    config: MemoryReflectionConfig,
) -> tuple[MemoryRecord | None, str | None, str | None, float | None]:
    """CREATE 前的近重复探测：命中强重复时自动合并到已有记忆。

    返回 ``(merged_record, outcome, duplicate_id, similarity)``：

    - ``merged_record`` 非 None 表示已把提议内容追加到已有记忆，调用方
      跳过 CREATE；
    - ``outcome`` 供 Trace 观测：``merged`` / ``title_match`` /
      ``merge_failed`` / ``suspect`` / ``none`` / None（未探测）；
    - 探测或合并失败一律降级为"未命中"，绝不阻塞正常 CREATE。
    """

    if not config.dedup_enabled:
        return None, None, None, None
    title = (decision.title or "").strip()
    duplicate_id: str | None = None
    similarity: float | None = None
    outcome = "none"
    title_key = _normalize_title_key(title)
    if title_key:
        try:
            active = await manager.list()
        except Exception:
            active = ()
        for record in active:
            if _normalize_title_key(record.title) == title_key:
                duplicate_id = record.id
                outcome = "title_match"
                break
    if duplicate_id is None:
        probe = "\n".join(
            part
            for part in (
                title,
                (decision.summary or "").strip(),
                (decision.content or "").strip()[:600],
            )
            if part
        )
        try:
            hits = await manager.find_similar(probe, limit=1)
        except Exception:
            hits = ()
        if hits:
            top = hits[0]
            if top.similarity >= config.dedup_threshold:
                duplicate_id = top.memory_id
                similarity = top.similarity
                outcome = "merged"
            elif top.similarity >= config.dedup_suspect_threshold:
                return None, "suspect", top.memory_id, top.similarity
    if duplicate_id is None:
        return None, outcome, None, None
    try:
        merged = await manager.append(
            duplicate_id,
            addition=decision.content or "",
            reason=f"auto-merge near-duplicate create: {decision.reason}",
        )
    except Exception as exc:
        logger.warning(
            "memory create dedup merge failed for %s: %s",
            duplicate_id,
            exc,
        )
        return None, "merge_failed", duplicate_id, similarity
    return merged, outcome, duplicate_id, similarity


def _enforce_reflection_output_budget(
    decision: ReflectionDecision,
    config: MemoryReflectionConfig,
) -> None:
    """拒绝"生成内容几乎必然超出模型输出预算"的决策。

    模型无法输出超过 max_output_tokens 的完整文本：当决策携带的标题、
    摘要、正文（或 append 增量）估算 token 超过预算的 75% 时，实际响应
    必然被截断或校验失败。在这里显式拒绝，把静默的 JSON 截断变成带
    估算值的可解释失败事件。
    """

    if decision.action is ReflectionAction.NONE:
        return
    output_text = "\n".join(
        part
        for part in (
            decision.title,
            decision.summary,
            decision.content,
            decision.addition,
        )
        if part
    )
    budget = max(1, int(config.max_output_tokens * 0.75))
    estimated = default_token_estimator().estimate_text(output_text)
    if estimated > budget:
        raise ValueError(
            "reflection output exceeds configured budget: "
            f"estimated {estimated} tokens > {budget} "
            f"(max_output_tokens={config.max_output_tokens}); "
            "prefer append or a shorter update"
        )


__all__ = ["PostRunMemoryCoordinator"]
