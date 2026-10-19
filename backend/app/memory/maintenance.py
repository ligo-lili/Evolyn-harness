"""长期记忆容量管理与 Retention 候选选择。

当 active 记忆数超过上限（默认 25）时触发 Memory Maintenance。算法只负责
找出最可能值得维护的 3~5 个候选，最终的 ARCHIVE / DEFER 判断交给模型。

候选排序使用 percentile rank 加权（分数越低越可能值得维护）：

- 0.4：距上次访问时长的百分位（越久未访问越低）；
- 0.3：访问次数的百分位（越少访问越低）；
- 0.3：距上次更新时长的百分位（越久未更新越低）。

从未被读取（``access_count == 0``）的记忆额外获得 +0.1 的保护分：零访问
往往只是"还没有被需要过"，不应仅凭该信号就被判为低价值。排序只影响候选
展示顺序，不做任何归档或保留决策。
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Sequence
from datetime import UTC, datetime

from .models import MemoryRecord

_ACCESS_WEIGHT = 0.4
_COUNT_WEIGHT = 0.3
_FRESHNESS_WEIGHT = 0.3
_NEVER_ACCESSED_BONUS = 0.1


class MemoryMaintenance:
    """容量检查与 Retention 候选选择。"""

    def __init__(self, *, max_active: int = 25) -> None:
        if max_active <= 0:
            raise ValueError("max_active must be greater than zero")
        self.max_active = max_active

    def exceeds_capacity(self, active_count: int) -> bool:
        """active 数量是否超过上限（触发维护）。"""

        return active_count > self.max_active

    def select_candidates(
        self,
        memories: Sequence[MemoryRecord],
        *,
        limit: int = 5,
    ) -> tuple[MemoryRecord, ...]:
        """按 retention score 选出最可能值得维护的候选。

        score 越低越可能被维护。三个信号先转成组内百分位再加权，避免
        无界的"小时数"主导排序；公式只负责排序，不做最终决策。
        """

        if limit <= 0:
            return ()
        now = datetime.now(UTC)
        scores = _retention_scores(memories, now)
        scored = sorted(
            memories,
            key=lambda record: (scores[record.id], record.id),
        )
        return tuple(scored[:limit])


def _retention_scores(
    memories: Sequence[MemoryRecord],
    now: datetime,
) -> dict[str, float]:
    """计算组内 percentile rank 加权分（未经百分位转换的信号无意义）。"""

    records = tuple(memories)
    if not records:
        return {}
    hours_since_accessed = [
        max(0.0, (now - record.last_accessed_at).total_seconds() / 3600)
        for record in records
    ]
    access_counts = [math.log1p(record.access_count) for record in records]
    hours_since_updated = [
        max(0.0, (now - record.updated_at).total_seconds() / 3600)
        for record in records
    ]
    # 取负数把"时长"信号翻转成"越大越值得保留"。
    recency_ranks = _percentile_ranks([-value for value in hours_since_accessed])
    count_ranks = _percentile_ranks(access_counts)
    freshness_ranks = _percentile_ranks([-value for value in hours_since_updated])
    scores: dict[str, float] = {}
    for index, record in enumerate(records):
        score = (
            _ACCESS_WEIGHT * recency_ranks[index]
            + _COUNT_WEIGHT * count_ranks[index]
            + _FRESHNESS_WEIGHT * freshness_ranks[index]
        )
        if record.access_count == 0:
            score += _NEVER_ACCESSED_BONUS
        scores[record.id] = min(1.0, score)
    return scores


def _percentile_ranks(values: Sequence[float]) -> list[float]:
    """把信号转成 [0,1] 百分位；平局取平均秩，全相等时为 0.5。"""

    if not values:
        return []
    ordered = sorted(values)
    count = len(ordered)
    ranks: list[float] = []
    for value in values:
        lower = bisect.bisect_left(ordered, value)
        upper = bisect.bisect_right(ordered, value)
        ranks.append((lower + upper) / (2.0 * count))
    return ranks


__all__ = ["MemoryMaintenance"]
