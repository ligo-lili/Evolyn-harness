"""Harness 自动召回：确定性 Recall Query + 每 Run 一次的候选快照。

职责边界：
- Recall Query 完全由确定性输入拼装（当前用户消息、近期用户消息、会话
  摘要目标、活动 Task 标题与进行中步骤），不额外调用模型；
- 每个 Run 只执行一次检索并缓存快照，所有 Step 复用同一份 Recall
  Context（缓存由 RuntimeContextSession 维护）；
- 召回内容只作为"可能相关的历史候选"注入临时请求上下文：不写入原始
  聊天历史、不进入滚动摘要、不增加 access_count、不授权 Reflection
  Update；正式读取仍然只能通过 memory_read。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from app.models.types import Message, MessageRole

from .prompts import MEMORY_RECALL_HEADER
from .search_index import MemorySearchResult, SearchMode

if TYPE_CHECKING:
    from .manager import MemoryManager

MEMORY_RECALL_MESSAGE_NAME = "evolyn_memory_recall"

# Recall Query 各信号的分段预算：当前用户消息优先，其余信号在各自预算内
# 补充，避免一条超长用户消息挤掉 Objective / Task / 近期消息。
_RECALL_USER_MESSAGE_MAX_CHARS = 700
_RECALL_RECENT_TOTAL_MAX_CHARS = 300
_RECALL_RECENT_ITEM_MAX_CHARS = 120
_RECALL_RECENT_MIN_CHARS = 4
_RECALL_OBJECTIVE_MAX_CHARS = 300
_RECALL_TASK_MAX_CHARS = 300


@dataclass(frozen=True, slots=True)
class MemoryRecallQueryInputs:
    """构造 Recall Query 的确定性输入（Run 开始时收集一次）。"""

    user_message: str
    recent_user_messages: tuple[str, ...] = ()
    summary_objective: str | None = None
    task_title: str | None = None
    task_active_steps: tuple[str, ...] = ()

    def with_task(
        self,
        task_title: str | None,
        task_active_steps: tuple[str, ...],
    ) -> MemoryRecallQueryInputs:
        """补上活动 Task 字段（Session 在首次构建时读取一次）。"""

        return replace(self, task_title=task_title, task_active_steps=task_active_steps)

    def render(self, *, max_chars: int = 1_600) -> str:
        """确定性渲染为有界 Query 文本（向量 + FTS 共用）。

        分段配额：当前用户消息是最高优先级信号；近期用户消息、摘要目标
        与活动 Task 各自受独立预算约束，不会互相挤占。过短或与当前消息
        重复的近期消息不提供检索信号，直接跳过。
        """

        sections: list[str] = []
        user_message = self.user_message.strip()[:_RECALL_USER_MESSAGE_MAX_CHARS]
        if user_message:
            sections.append(user_message)
        recent: list[str] = []
        recent_used = 0
        for message in reversed(self.recent_user_messages):
            text = message.strip()
            if len(text) < _RECALL_RECENT_MIN_CHARS or text in user_message:
                continue
            text = text[:_RECALL_RECENT_ITEM_MAX_CHARS]
            if recent_used + len(text) > _RECALL_RECENT_TOTAL_MAX_CHARS:
                continue
            recent.append(text)
            recent_used += len(text)
        if recent:
            sections.append(" | ".join(reversed(recent)))
        objective = (self.summary_objective or "").strip()[
            :_RECALL_OBJECTIVE_MAX_CHARS
        ]
        if objective:
            sections.append(objective)
        if self.task_title:
            task_line = self.task_title.strip()
            if self.task_active_steps:
                task_line += " > " + "; ".join(
                    step.strip() for step in self.task_active_steps if step.strip()
                )
            task_line = task_line[:_RECALL_TASK_MAX_CHARS]
            if task_line:
                sections.append(task_line)
        return "\n".join(section for section in sections if section)[:max_chars]


def recent_user_message_texts(
    history: Sequence[Message],
    *,
    limit: int = 3,
    max_chars: int = 300,
) -> tuple[str, ...]:
    """从持久历史中取最近几条用户消息（不含当前 Run 的新消息）。"""

    texts: list[str] = []
    for message in reversed(history):
        if message.role is not MessageRole.USER:
            continue
        content = (message.content or "").strip()
        if content:
            texts.append(content[:max_chars])
        if len(texts) >= limit:
            break
    return tuple(reversed(texts))


@dataclass(frozen=True, slots=True)
class MemoryRecallCandidate:
    """注入模型上下文的一个召回候选（只有 cue 级信息与最相关片段）。"""

    memory_id: str
    title: str
    summary: str
    revision: int
    snippet: str
    rrf_score: float
    matched_by_vector: bool
    matched_by_fts: bool


@dataclass(frozen=True, slots=True)
class MemoryRecallSnapshot:
    """一次 Run 的自动召回结果（Run 内缓存，所有 Step 复用）。"""

    query: str
    mode: SearchMode
    candidates: tuple[MemoryRecallCandidate, ...]
    degrade_reason: str | None = None

    def render_message(self, *, max_chars: int = 2_400) -> Message | None:
        """渲染为临时注入的系统消息；无候选时返回 None（不注入噪声）。"""

        if not self.candidates:
            return None
        lines = [MEMORY_RECALL_HEADER.rstrip()]
        used = len(lines[0])
        for candidate in self.candidates:
            entry_lines = [
                f"[{candidate.memory_id}] {candidate.title} "
                f"(revision {candidate.revision})",
                f"Summary: {candidate.summary}",
            ]
            if candidate.snippet:
                entry_lines.append(f"Snippet: {candidate.snippet}")
            entry = "\n".join(entry_lines)
            if used + len(entry) > max_chars:
                break
            lines.append("")
            lines.append(entry)
            used += len(entry) + 1
        if len(lines) == 1:
            return None
        return Message(
            role=MessageRole.SYSTEM,
            name=MEMORY_RECALL_MESSAGE_NAME,
            content="\n".join(lines).rstrip() + "\n",
        )


class MemoryRecallService:
    """把 Recall Query 转成一次 Hybrid 检索与注入快照。"""

    def __init__(self, manager: MemoryManager) -> None:
        self._manager = manager

    async def recall(
        self,
        inputs: MemoryRecallQueryInputs,
    ) -> MemoryRecallSnapshot:
        """执行一次自动召回；任何失败都转成不可用快照，绝不让 Run 失败。"""

        query = inputs.render(
            max_chars=self._manager.search_settings.query_max_chars
        )
        if not query:
            return MemoryRecallSnapshot(
                query="",
                mode=SearchMode.UNAVAILABLE,
                candidates=(),
                degrade_reason="empty recall query",
            )
        try:
            result: MemorySearchResult = await self._manager.search(query)
        except Exception as exc:  # 防御：检索实现异常也不能中断 Run。
            return MemoryRecallSnapshot(
                query=query,
                mode=SearchMode.UNAVAILABLE,
                candidates=(),
                degrade_reason=f"recall failed: {type(exc).__name__}: {exc}",
            )
        candidates = tuple(
            MemoryRecallCandidate(
                memory_id=item.memory_id,
                title=item.title,
                summary=item.summary,
                revision=item.revision,
                snippet=item.snippet,
                rrf_score=item.rrf_score,
                matched_by_vector=item.matched_by_vector,
                matched_by_fts=item.matched_by_fts,
            )
            for item in result.candidates
        )
        return MemoryRecallSnapshot(
            query=query,
            mode=result.mode,
            candidates=candidates,
            degrade_reason=result.degrade_reason,
        )


__all__ = [
    "MEMORY_RECALL_MESSAGE_NAME",
    "MemoryRecallCandidate",
    "MemoryRecallQueryInputs",
    "MemoryRecallService",
    "MemoryRecallSnapshot",
    "recent_user_message_texts",
]
