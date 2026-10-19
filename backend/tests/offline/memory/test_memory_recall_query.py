"""Recall Query 确定性渲染的分段预算测试（纯函数，不触网、不建索引）。"""

from __future__ import annotations

from app.memory.recall import MemoryRecallQueryInputs


def test_render_keeps_objective_and_task_behind_long_user_message() -> None:
    inputs = MemoryRecallQueryInputs(
        user_message="长" * 3000,
        summary_objective="总结目标：梳理记忆模块的改进点",
        task_title="记忆模块升级",
        task_active_steps=("P0 检索正确性",),
    )

    query = inputs.render()

    assert len(query) <= 1_600
    # 用户消息按分段预算截断，不再吃光整体预算。
    assert query.count("长") == 700
    assert "总结目标：梳理记忆模块的改进点" in query
    assert "记忆模块升级 > P0 检索正确性" in query


def test_render_skips_short_and_duplicate_recent_messages() -> None:
    inputs = MemoryRecallQueryInputs(
        user_message="数据库迁移决定是什么",
        recent_user_messages=(
            "好",
            "数据库迁移决定是什么",
            "前端构建工具换成了 vite 吗",
        ),
    )

    query = inputs.render()

    assert "好" not in query
    assert query.count("数据库迁移决定是什么") == 1
    assert "前端构建工具换成了 vite 吗" in query


def test_render_caps_recent_budget_by_priority() -> None:
    recent = tuple(f"第 {index} 条历史消息 " + "内" * 200 for index in range(3))
    inputs = MemoryRecallQueryInputs(
        user_message="当前问题",
        recent_user_messages=recent,
    )

    query = inputs.render()
    recent_part = query.split("\n", 1)[1]

    # 最近的消息优先保留；超出 300 字符预算的更早消息被跳过。
    assert "第 2 条" in recent_part
    assert "第 1 条" in recent_part
    assert "第 0 条" not in recent_part
    assert recent_part.index("第 1 条") < recent_part.index("第 2 条")


def test_render_returns_empty_without_signals() -> None:
    assert MemoryRecallQueryInputs(user_message="   ").render() == ""
