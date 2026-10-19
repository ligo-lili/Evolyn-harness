"""长期记忆的统一 Runtime 门面。

Runtime 不直接操作文件路径，统一通过 ``MemoryManager``：

- 加载 Core Memory、Memory Index 与 Memory Policy；
- 为在线 Recall、显式 Core 写入与 Post-Run Reflection 提供统一 API；
- 维护运行时元数据（access_count、last_accessed_at、updated_at）；
- 执行容量管理与 INDEX 重建；
- 提供 Hybrid（FTS5 + 向量 + RRF）普通记忆检索与 Harness 自动召回；
  Markdown 文件仍是唯一权威存储，SQLite 只是可重建的搜索投影，
  索引失败只降级检索，不影响记忆读写与 Host 启动。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows 暂时只保留进程内锁
    fcntl = None  # type: ignore[assignment]

from app.models.types import Message, MessageRole

from .core import DEFAULT_MAX_CORE_TOKENS, CoreMemoryEntry, CoreMemoryManager
from .embedding import EmbeddingAdapter
from .index import MemoryIndex
from .maintenance import MemoryMaintenance
from .models import MemoryRecord, MemoryStatus
from .prompts import (
    CORE_MEMORY_HEADER,
    MEMORY_POLICY_PROMPT,
)
from .recall import (
    MemoryRecallQueryInputs,
    MemoryRecallService,
    MemoryRecallSnapshot,
)
from .search_index import (
    DEFAULT_SEARCH_DATABASE_NAME,
    MemorySearchIndex,
    MemorySearchResult,
    MemorySearchSettings,
    MemorySimilarityHit,
    SearchMode,
)
from .store import DEFAULT_MEMORY_DIR, MemoryStore

logger = logging.getLogger("evolyn.memory.manager")

CORE_MEMORY_MESSAGE_NAME = "evolyn_core_memory"
MEMORY_INDEX_MESSAGE_NAME = "evolyn_memory_index"
MEMORY_POLICY_MESSAGE_NAME = "evolyn_memory_policy"


class MemoryManager:
    """Sparse 长期记忆的 Runtime 门面（Hybrid 自动召回 + 模型显式读取）。"""

    def __init__(
        self,
        memory_dir: str | Path = DEFAULT_MEMORY_DIR,
        *,
        max_active: int = 25,
        max_core_tokens: int = DEFAULT_MAX_CORE_TOKENS,
        embedding: EmbeddingAdapter | None = None,
        search_settings: MemorySearchSettings | None = None,
        min_vector_similarity: float | None = None,
        hybrid_search_enabled: bool = True,
    ) -> None:
        self.memory_dir = Path(memory_dir).expanduser().resolve()
        self.max_active = max_active
        self.store = MemoryStore(self.memory_dir, max_active=max_active)
        self.core = CoreMemoryManager(
            self.memory_dir,
            max_tokens=max_core_tokens,
        )
        self.index = MemoryIndex(self.memory_dir)
        self.maintenance = MemoryMaintenance(max_active=max_active)
        # 向量相似度阈值随 Embedding 模型分布校准（由 Embedding 配置传入），
        # 未指定时保持索引默认值。
        if search_settings is None and min_vector_similarity is not None:
            search_settings = MemorySearchSettings(
                min_vector_similarity=min_vector_similarity
            )
        self.search_settings = search_settings or MemorySearchSettings()
        self._hybrid_search_enabled = hybrid_search_enabled
        self._search_index = MemorySearchIndex(
            self.memory_dir / DEFAULT_SEARCH_DATABASE_NAME,
            embedding=embedding,
            settings=self.search_settings,
        )
        self._recall_service = MemoryRecallService(self)
        self._search_ok = False
        self._search_backfill_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._lock_path = self.memory_dir / ".memory.lock"

    @property
    def hybrid_recall_enabled(self) -> bool:
        """Hybrid 自动召回是否可用（索引初始化成功且未被关闭）。"""

        return self._hybrid_search_enabled and self._search_ok

    @property
    def embedding_backfill_status(self) -> str:
        """后台向量补全状态：idle / running / complete / failed（诊断用）。"""

        return self._search_index.backfill_status

    async def initialize(self) -> None:
        """创建 memory 目录结构；搜索投影失败只降级，不阻止启动。"""

        async with self._mutation_guard():
            await self.store.initialize()
            await self.core.initialize()
            # INDEX 是 active 文件的投影；启动时重建可修复中断或人工编辑造成的陈旧。
            await self._rebuild_index()
        if self._hybrid_search_enabled:
            try:
                await self._search_index.initialize()
                await self._search_index.reconcile(
                    await self.store.list_active(),
                    generate_embeddings=False,
                )
                self._search_ok = True
                # FTS 投影已经可用，远程 Embedding 补全移到后台，避免服务
                # 超时或重试阻塞 Host 启动。补全期间检索自然降级为 FTS。
                self._search_backfill_task = asyncio.create_task(
                    self._search_index.backfill_embeddings(),
                    name="memory-embedding-backfill",
                )
            except Exception as exc:
                # SQLite / FTS5 / 重建全部失败：回到 Legacy INDEX 注入模式。
                self._search_ok = False
                logger.warning(
                    "memory search index unavailable, fallback to index cues: %s",
                    exc,
                )

    async def close(self) -> None:
        """停止后台向量补全任务；由 Application 在关闭 Adapter 前调用。"""

        task = self._search_backfill_task
        self._search_backfill_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    # ------------------------------------------------------------------
    # Runtime 注入
    # ------------------------------------------------------------------

    async def context_messages(
        self,
        *,
        recall: MemoryRecallSnapshot | None = None,
    ) -> tuple[Message, ...]:
        """返回应注入请求上下文的消息。

        - ``recall`` 非 None 表示 Hybrid 模式：注入 Core + 本 Run 的 Recall
          Candidates + Policy，不再注入完整 INDEX.md；
        - ``recall`` 为 None 表示 Legacy 模式（搜索投影不可用）：注入
          Core + INDEX.md + Policy，行为与旧版本一致。
        """

        async with self._lock:
            messages: list[Message] = []
            core_text = await self.core.load()
            if core_text.strip():
                core_content = core_text.strip()
                if not core_content.startswith(CORE_MEMORY_HEADER):
                    core_content = f"{CORE_MEMORY_HEADER}\n\n{core_content}"
                messages.append(
                    Message(
                        role=MessageRole.SYSTEM,
                        name=CORE_MEMORY_MESSAGE_NAME,
                        content=core_content,
                    )
                )
            if recall is None:
                index_text = await self.index.load()
                if index_text is not None:
                    messages.append(
                        Message(
                            role=MessageRole.SYSTEM,
                            name=MEMORY_INDEX_MESSAGE_NAME,
                            content=index_text,
                        )
                    )
            else:
                recall_message = recall.render_message(
                    max_chars=self.search_settings.recall_message_max_chars
                )
                if recall_message is not None:
                    messages.append(recall_message)
            messages.append(
                Message(
                    role=MessageRole.SYSTEM,
                    name=MEMORY_POLICY_MESSAGE_NAME,
                    content=MEMORY_POLICY_PROMPT,
                )
            )
            return tuple(messages)

    async def reflection_context(self) -> tuple[str, str]:
        """返回 Post-Run Reflection 使用的 Core 正文与紧凑 Index。

        紧凑 Index 只保留 ``id | title | summary`` 行式 cue，去掉 INDEX.md
        面向主 Agent 的说明文字，减少 Reflector 的输入噪声。
        """

        async with self._lock:
            core_text = await self.core.load()
            records = await self.store.list_active()
            index_text = "\n".join(
                f"{record.id} | {record.title} | {record.summary}"
                for record in records
            )
            return core_text, index_text

    # ------------------------------------------------------------------
    # 语义 Memory API（由模型工具调用）
    # ------------------------------------------------------------------

    async def read(self, memory_id: str) -> MemoryRecord | None:
        """读取完整记忆；自动更新 access_count / last_accessed_at。"""

        async with self._mutation_guard():
            return await self.store.read(memory_id)

    async def list(self) -> tuple[MemoryRecord, ...]:
        """列出当前 active 记忆（id / title / summary）。"""

        async with self._lock:
            return await self.store.list_active()

    async def list_archived(self) -> tuple[MemoryRecord, ...]:
        """列出已归档记忆，仅供管理与观察界面读取。"""

        async with self._lock:
            return await self.store.list_archived()

    # ------------------------------------------------------------------
    # Hybrid 检索与自动召回（只读，不更新 access_count）
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        *,
        limit: int | None = None,
    ) -> MemorySearchResult:
        """Hybrid（FTS5 + 向量 + RRF）检索 active 记忆。

        结果用权威 Markdown 记录补全 title/summary/revision，并剔除已经
        不在 active 集合中的陈旧索引行；检索不增加 access_count。
        """

        if not self._search_ok:
            return MemorySearchResult(
                mode=SearchMode.UNAVAILABLE,
                candidates=(),
                query=query,
                degrade_reason="search index unavailable",
            )
        result = await self._search_index.search(query, limit=limit)
        enriched: list = []
        for candidate in result.candidates:
            record = await self.store.load(candidate.memory_id)
            if record is None or record.status is not MemoryStatus.ACTIVE:
                continue
            enriched.append(
                replace(
                    candidate,
                    title=record.title,
                    summary=record.summary,
                    revision=record.revision,
                )
            )
        return MemorySearchResult(
            mode=result.mode,
            candidates=tuple(enriched),
            query=result.query,
            degrade_reason=result.degrade_reason,
        )

    async def recall(
        self,
        inputs: MemoryRecallQueryInputs,
    ) -> MemoryRecallSnapshot:
        """执行一次自动召回（Harness 每 Run 只调用一次）。"""

        return await self._recall_service.recall(inputs)

    async def find_similar(
        self,
        text: str,
        *,
        limit: int = 3,
    ) -> tuple[MemorySimilarityHit, ...]:
        """按向量余弦找出与文本最相似的 active 记忆（不做阈值过滤）。

        用于 Reflection 创建前的近重复探测。Embedding 不可用、未就绪或
        失败时返回空元组，调用方退回确定性标题比对；探测不增加
        access_count，也不修改任何记忆。
        """

        if (
            not self._search_ok
            or not self._search_index.embedding_configured
        ):
            return ()
        hits = await self._search_index.nearest(text, limit=max(limit * 2, limit))
        if hits is None:
            return ()
        enriched: list[MemorySimilarityHit] = []
        for hit in hits:
            record = await self.store.load(hit.memory_id)
            if record is None or record.status is not MemoryStatus.ACTIVE:
                continue
            enriched.append(replace(hit, title=record.title))
            if len(enriched) >= limit:
                break
        return tuple(enriched)

    async def create(
        self,
        *,
        title: str,
        summary: str,
        content: str,
    ) -> MemoryRecord:
        """在硬容量范围内创建普通长期记忆并重建 INDEX。"""

        async with self._mutation_guard():
            if await self.store.count_active() >= self.max_active:
                raise ValueError("active memory capacity is full")
            record = await self.store.create(
                title=title,
                summary=summary,
                content=content,
            )
            await self._rebuild_index()
            await self._sync_search_index(record)
            return record

    async def create_if_capacity(
        self,
        *,
        title: str,
        summary: str,
        content: str,
    ) -> MemoryRecord | None:
        """在同一临界区检查容量并创建，避免并发突破 active 上限。"""

        async with self._mutation_guard():
            if await self.store.count_active() >= self.max_active:
                return None
            record = await self.store.create(
                title=title,
                summary=summary,
                content=content,
            )
            await self._rebuild_index()
            await self._sync_search_index(record)
            return record

    @staticmethod
    def validate_create(
        *,
        title: str,
        summary: str,
        content: str,
    ) -> None:
        """在容量维护前验证 CREATE 内容，避免先归档后才发现输入非法。"""

        now = datetime.now(UTC)
        MemoryRecord(
            id="M000",
            title=title,
            summary=summary,
            content=content,
            created_at=now,
            updated_at=now,
            last_accessed_at=now,
        )

    async def update(
        self,
        memory_id: str,
        *,
        title: str | None = None,
        summary: str | None = None,
        content: str,
        reason: str,
    ) -> MemoryRecord:
        """管理性更新已有记忆；未提供 Recall Cue 时保持原值。"""

        async with self._mutation_guard():
            record = await self.store.update(
                memory_id,
                title=title,
                summary=summary,
                content=content,
                reason=reason,
            )
            await self._rebuild_index()
            await self._sync_search_index(record)
            return record

    async def update_if_revision(
        self,
        memory_id: str,
        *,
        expected_revision: int,
        title: str,
        summary: str,
        content: str,
        reason: str,
    ) -> MemoryRecord:
        """仅在模型读取的 revision 仍为最新时替换完整记忆。"""

        async with self._mutation_guard():
            record = await self.store.update(
                memory_id,
                title=title,
                summary=summary,
                content=content,
                reason=reason,
                expected_revision=expected_revision,
            )
            await self._rebuild_index()
            await self._sync_search_index(record)
            return record

    async def append(
        self,
        memory_id: str,
        *,
        addition: str,
        reason: str,
    ) -> MemoryRecord:
        """Harness 自动合并路径：向最新正文追加内容并重建 INDEX。

        不校验模型授权链（调用方是系统探针而不是模型引用），但仍走
        mutation guard 串行化，读取的是提交时刻的最新快照。
        """

        async with self._mutation_guard():
            record = await self.store.append(
                memory_id,
                addition=addition,
                reason=reason,
            )
            await self._rebuild_index()
            await self._sync_search_index(record)
            return record

    async def append_if_revision(
        self,
        memory_id: str,
        *,
        expected_revision: int,
        addition: str,
        reason: str,
    ) -> MemoryRecord:
        """仅在模型读取的 revision 仍为最新时追加内容。"""

        async with self._mutation_guard():
            record = await self.store.append(
                memory_id,
                addition=addition,
                reason=reason,
                expected_revision=expected_revision,
            )
            await self._rebuild_index()
            await self._sync_search_index(record)
            return record

    async def archive(self, memory_id: str, *, reason: str) -> MemoryRecord:
        """归档记忆并重建 INDEX。"""

        async with self._mutation_guard():
            record = await self.store.archive(memory_id, reason=reason)
            await self._rebuild_index()
            await self._drop_search_index(record.id)
            return record

    async def archive_if_unchanged(
        self,
        memory_id: str,
        *,
        expected_record: MemoryRecord,
        reason: str,
    ) -> MemoryRecord:
        """候选快照仍为最新时归档，拒绝维护模型基于陈旧内容执行。"""

        async with self._mutation_guard():
            current = await self.store.load(memory_id)
            if current is None:
                raise KeyError(f"memory '{memory_id}' not found")
            if current != expected_record:
                raise ValueError(
                    f"memory '{memory_id}' changed since maintenance snapshot"
                )
            record = await self.store.archive(memory_id, reason=reason)
            await self._rebuild_index()
            await self._drop_search_index(record.id)
            return record

    async def restore(self, memory_id: str, *, reason: str) -> MemoryRecord:
        """把归档记忆恢复为 active，重新进入 INDEX 与 Hybrid 检索。

        管理路径（CLI 等）专用；恢复需要 active 有空位，不会突破硬上限。
        恢复前保存 archived 状态快照。
        """

        async with self._mutation_guard():
            record = await self.store.load(memory_id)
            if record is None:
                raise KeyError(f"memory '{memory_id}' not found")
            if record.status is MemoryStatus.ARCHIVED:
                if await self.store.count_active() >= self.max_active:
                    raise ValueError(
                        "active memory capacity is full; "
                        "archive a memory before restoring"
                    )
            restored = await self.store.restore(memory_id, reason=reason)
            await self._rebuild_index()
            await self._sync_search_index(restored)
            return restored

    async def load(self, memory_id: str) -> MemoryRecord | None:
        """读取记忆但不更新访问元数据（管理与观察路径）。"""

        async with self._lock:
            return await self.store.load(memory_id)

    async def list_revisions(self, memory_id: str) -> tuple[int, ...]:
        """列出某条记忆保存的 revision 快照编号（新 → 旧）。"""

        async with self._lock:
            return await self.store.list_revisions(memory_id)

    async def read_revision(
        self,
        memory_id: str,
        revision: int,
    ) -> MemoryRecord | None:
        """读取一份 revision 快照（不更新任何访问元数据）。"""

        async with self._lock:
            return await self.store.read_revision(memory_id, revision)

    async def restore_revision(
        self,
        memory_id: str,
        revision: int,
        *,
        reason: str | None = None,
    ) -> MemoryRecord:
        """把指定快照恢复为新的当前 revision（编号只前进，不回退）。"""

        async with self._mutation_guard():
            record = await self.store.restore_revision(
                memory_id,
                revision,
                reason=reason,
            )
            if record.status is MemoryStatus.ACTIVE:
                await self._rebuild_index()
                await self._sync_search_index(record)
            return record

    async def upsert_core(
        self,
        *,
        key: str,
        value: str,
        reason: str,
        source_statement: str,
        source_message_sha256: str | None = None,
        source_offset: int | None = None,
    ) -> tuple[CoreMemoryEntry, bool]:
        """按 key 更新 Core；模型不能覆盖整份 CORE.md。"""

        async with self._mutation_guard():
            return await self.core.upsert(
                key=key,
                value=value,
                reason=reason,
                source_statement=source_statement,
                source_message_sha256=source_message_sha256,
                source_offset=source_offset,
            )

    async def remove_core(self, key: str) -> CoreMemoryEntry:
        """移除单个 Core 条目；调用方负责验证当前用户明确证据。"""

        async with self._mutation_guard():
            return await self.core.remove(key)

    # ------------------------------------------------------------------
    # 容量管理
    # ------------------------------------------------------------------

    async def maintenance_required(self) -> bool:
        """active 数量是否超过上限。"""

        async with self._lock:
            return self.maintenance.exceeds_capacity(
                await self.store.count_active()
            )

    async def active_count(self) -> int:
        """返回当前 active Memory 数量。"""

        async with self._lock:
            return await self.store.count_active()

    async def has_capacity(self, *, required_slots: int = 1) -> bool:
        """判断是否能在不超过硬上限的情况下容纳指定新记录数。"""

        if required_slots < 0:
            raise ValueError("required_slots cannot be negative")
        async with self._lock:
            active_count = await self.store.count_active()
            return active_count + required_slots <= self.max_active

    async def retention_candidates(
        self,
        *,
        limit: int = 5,
    ) -> tuple[MemoryRecord, ...]:
        """返回最可能值得维护的候选（最终决策交给模型）。"""

        async with self._lock:
            active = await self.store.list_active()
            return self.maintenance.select_candidates(active, limit=limit)

    async def _rebuild_index(self) -> None:
        await self.index.rebuild(await self.store.list_active())

    async def _sync_search_index(self, record: MemoryRecord) -> None:
        """写入后增量同步搜索投影；失败只降级检索，不影响 Markdown。"""

        if not self._search_ok:
            return
        try:
            await self._search_index.upsert(record)
        except Exception as exc:
            logger.warning(
                "memory search index sync failed for %s: %s",
                record.id,
                exc,
            )

    async def _drop_search_index(self, memory_id: str) -> None:
        """归档后从搜索投影删除对应行。"""

        if not self._search_ok:
            return
        try:
            await self._search_index.remove(memory_id)
        except Exception as exc:
            logger.warning(
                "memory search index remove failed for %s: %s",
                memory_id,
                exc,
            )

    @asynccontextmanager
    async def _mutation_guard(self) -> AsyncIterator[None]:
        """串行化同一实例，并在 POSIX 上协调同目录的进程与 Manager。"""

        async with self._lock:
            await asyncio.to_thread(self.memory_dir.mkdir, parents=True, exist_ok=True)
            handle = await asyncio.to_thread(self._lock_path.open, "a+b")
            try:
                await self._acquire_file_lock(handle)
                yield
            finally:
                await self._release_file_lock(handle)

    @staticmethod
    async def _acquire_file_lock(handle: BinaryIO) -> None:
        if fcntl is not None:
            await asyncio.to_thread(fcntl.flock, handle.fileno(), fcntl.LOCK_EX)

    @staticmethod
    async def _release_file_lock(handle: BinaryIO) -> None:
        try:
            if fcntl is not None:
                await asyncio.to_thread(fcntl.flock, handle.fileno(), fcntl.LOCK_UN)
        finally:
            await asyncio.to_thread(handle.close)


__all__ = [
    "CORE_MEMORY_MESSAGE_NAME",
    "MEMORY_INDEX_MESSAGE_NAME",
    "MEMORY_POLICY_MESSAGE_NAME",
    "MemoryManager",
    "MemoryRecallQueryInputs",
    "MemoryRecallSnapshot",
    "MemorySearchResult",
    "MemorySearchSettings",
    "SearchMode",
]
