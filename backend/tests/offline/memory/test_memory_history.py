"""普通记忆 revision 快照与归档恢复（P2-10 / P2-8a）的离线测试。

覆盖：写路径快照、10 份上限裁剪、快照读取/回退、归档恢复、中断恢复修复。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.memory import (
    FakeEmbeddingAdapter,
    MemoryManager,
    MemoryRecord,
    MemoryStatus,
)


@pytest.fixture
def memory_root(tmp_path: Path) -> Path:
    return tmp_path / "memory"


async def _manager(
    memory_root: Path,
    *,
    embedding: bool = False,
) -> MemoryManager:
    manager = MemoryManager(
        memory_root,
        embedding=FakeEmbeddingAdapter() if embedding else None,
    )
    await manager.initialize()
    return manager


@pytest.mark.asyncio
async def test_update_and_append_snapshot_previous_revisions(
    memory_root: Path,
) -> None:
    manager = await _manager(memory_root)
    record = await manager.create(title="标题", summary="cue", content="r1 内容")
    assert await manager.list_revisions(record.id) == ()

    await manager.update(record.id, content="r2 内容", reason="第一次修订")
    assert (memory_root / "history" / "M001.r1.md").is_file()
    assert await manager.list_revisions(record.id) == (1,)

    await manager.append(record.id, addition="r3 追加", reason="追加补充")
    assert await manager.list_revisions(record.id) == (2, 1)

    first = await manager.read_revision(record.id, 1)
    second = await manager.read_revision(record.id, 2)
    assert first is not None and first.content == "r1 内容"
    assert second is not None and second.content == "r2 内容"
    assert second.revision == 2


@pytest.mark.asyncio
async def test_archive_snapshots_last_active_state(memory_root: Path) -> None:
    manager = await _manager(memory_root)
    record = await manager.create(title="A", summary="a", content="内容")

    await manager.archive(record.id, reason="已经过时")

    assert await manager.list_revisions(record.id) == (1,)
    snapshot = await manager.read_revision(record.id, 1)
    assert snapshot is not None
    assert snapshot.status is MemoryStatus.ACTIVE
    archived = await manager.load(record.id)
    assert archived is not None
    assert archived.status is MemoryStatus.ARCHIVED


@pytest.mark.asyncio
async def test_history_prunes_to_ten_newest_snapshots(memory_root: Path) -> None:
    manager = await _manager(memory_root)
    record = await manager.create(title="A", summary="a", content="初始")

    for index in range(1, 13):
        await manager.update(record.id, content=f"修订 {index}", reason="持续修订")

    # 当前 r13；快照保留最新的 r3~r12 共 10 份。
    assert await manager.list_revisions(record.id) == tuple(range(12, 2, -1))
    history_files = list((memory_root / "history").glob("M001.r*.md"))
    assert len(history_files) == 10
    assert await manager.read_revision(record.id, 1) is None
    assert await manager.read_revision(record.id, 2) is None


@pytest.mark.asyncio
async def test_restore_revision_rewrites_content_and_bumps_revision(
    memory_root: Path,
) -> None:
    manager = await _manager(memory_root)
    record = await manager.create(title="标题 A", summary="cue A", content="第一版")
    await manager.update(
        record.id,
        title="标题 B",
        summary="cue B",
        content="第二版",
        reason="内容修订",
    )

    restored = await manager.restore_revision(record.id, 1, reason="用户要求回退")

    assert restored.revision == 3
    assert restored.title == "标题 A"
    assert restored.summary == "cue A"
    assert restored.content == "第一版"
    assert restored.last_update_reason == "用户要求回退"
    assert restored.access_count == record.access_count
    loaded = await manager.load(record.id)
    assert loaded is not None and loaded.content == "第一版"
    # 回退前保存了 r2 快照，历史仍然完整。
    assert await manager.list_revisions(record.id) == (2, 1)


@pytest.mark.asyncio
async def test_restore_revision_missing_or_broken_snapshot_is_safe(
    memory_root: Path,
) -> None:
    manager = await _manager(memory_root)
    record = await manager.create(title="A", summary="a", content="内容")
    memory_path = memory_root / "active" / f"{record.id}.md"
    before = memory_path.read_text(encoding="utf-8")

    with pytest.raises(KeyError, match="snapshot not found"):
        await manager.restore_revision(record.id, 2)
    assert memory_path.read_text(encoding="utf-8") == before

    await manager.update(record.id, content="新内容", reason="修订")
    broken = memory_root / "history" / "M001.r1.md"
    broken.write_text("这不是记忆文件", encoding="utf-8")
    assert await manager.read_revision(record.id, 1) is None
    assert await manager.read_revision(record.id, 99) is None

    with pytest.raises(ValueError, match="revision must be at least 1"):
        await manager.read_revision(record.id, 0)


@pytest.mark.asyncio
async def test_restore_reverts_archived_memory_to_active(
    memory_root: Path,
) -> None:
    manager = await _manager(memory_root, embedding=True)
    record = await manager.create(
        title="部署流程",
        summary="发布部署的既定流程",
        content="先跑测试，再构建镜像，最后滚动发布。",
    )
    await manager.archive(record.id, reason="暂时不需要")
    assert await manager.list() == ()

    restored = await manager.restore(record.id, reason="项目重新需要部署流程")

    assert restored.status is MemoryStatus.ACTIVE
    assert restored.revision == record.revision + 2
    assert restored.archive_reason is None
    assert restored.last_update_reason == "项目重新需要部署流程"
    assert not (memory_root / "archive" / f"{record.id}.md").exists()
    assert (memory_root / "active" / f"{record.id}.md").is_file()
    assert [item.id for item in await manager.list()] == [record.id]
    # 恢复前保存了 archived 状态快照；r1 是归档前的最后 active 状态。
    assert await manager.list_revisions(record.id) == (
        record.revision + 1,
        record.revision,
    )
    archived_snapshot = await manager.read_revision(
        record.id,
        record.revision + 1,
    )
    assert archived_snapshot is not None
    assert archived_snapshot.status is MemoryStatus.ARCHIVED
    # 恢复后重新进入 Hybrid 检索。
    result = await manager.search("发布流程")
    assert record.id in [candidate.memory_id for candidate in result.candidates]


@pytest.mark.asyncio
async def test_restore_active_memory_is_noop(memory_root: Path) -> None:
    manager = await _manager(memory_root)
    record = await manager.create(title="A", summary="a", content="内容")

    restored = await manager.restore(record.id, reason="无需恢复")

    assert restored.revision == record.revision
    assert await manager.list_revisions(record.id) == ()


@pytest.mark.asyncio
async def test_restore_missing_raises(memory_root: Path) -> None:
    manager = await _manager(memory_root)

    with pytest.raises(KeyError, match="not found"):
        await manager.restore("M999", reason="测试")


@pytest.mark.asyncio
async def test_restore_requires_free_slot(memory_root: Path) -> None:
    manager = MemoryManager(memory_root, max_active=1)
    await manager.initialize()
    archived = await manager.create(title="A", summary="a", content="内容")
    await manager.archive(archived.id, reason="暂时不用")
    await manager.create(title="B", summary="b", content="占满容量")

    with pytest.raises(ValueError, match="capacity is full"):
        await manager.restore(archived.id, reason="想恢复")

    assert [item.id for item in await manager.list()] == ["M002"]
    still_archived = await manager.load(archived.id)
    assert still_archived is not None
    assert still_archived.status is MemoryStatus.ARCHIVED


@pytest.mark.asyncio
async def test_initialize_repairs_interrupted_restore(memory_root: Path) -> None:
    manager = await _manager(memory_root)
    record = await manager.create(title="A", summary="a", content="内容")
    await manager.archive(record.id, reason="过期")
    interrupted = MemoryRecord(
        **{
            **record.model_dump(),
            "status": MemoryStatus.ACTIVE,
            "archive_reason": None,
            "revision": record.revision + 1,
        }
    )
    archived_path = memory_root / "archive" / f"{record.id}.md"
    archived_path.write_text(interrupted.render_markdown(), encoding="utf-8")

    restarted = await _manager(memory_root)

    assert not archived_path.exists()
    assert (memory_root / "active" / f"{record.id}.md").is_file()
    assert [item.id for item in await restarted.list()] == [record.id]


@pytest.mark.asyncio
async def test_load_does_not_increment_access_count(memory_root: Path) -> None:
    manager = await _manager(memory_root)
    record = await manager.create(title="A", summary="a", content="内容")

    loaded = await manager.load(record.id)

    assert loaded is not None
    assert loaded.access_count == 0
