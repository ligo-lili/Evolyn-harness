"""任务集自检：结构性约束与筛选行为。"""

from __future__ import annotations

import re

from tests.eval.subagent.tasks import (
    CATEGORIES,
    SECURITY_CATEGORY,
    TASKS,
    select_tasks,
    validate_task_set,
)


def test_task_set_is_valid() -> None:
    validate_task_set()


def test_task_count_and_unique_ids() -> None:
    assert len(TASKS) == 20
    ids = [task.id for task in TASKS]
    assert len(set(ids)) == len(ids)


def test_all_categories_covered() -> None:
    covered = {task.category for task in TASKS}
    assert covered == set(CATEGORIES)


def test_security_subset_has_critical_facts() -> None:
    security_tasks = [task for task in TASKS if task.is_security]
    assert len(security_tasks) == 3
    for task in security_tasks:
        assert task.critical_fact_count >= 2, task.id
    for task in TASKS:
        if not task.is_security:
            assert task.critical_fact_count == 0, task.id


def test_every_pattern_compiles() -> None:
    for task in TASKS:
        for fact in task.key_facts:
            for pattern in fact.patterns:
                re.compile(pattern)  # 抛异常即失败


def test_task_metadata_is_reasonable() -> None:
    for task in TASKS:
        assert task.expected_files >= 1, task.id
        assert task.scope_hint, task.id
        assert task.criteria.strip(), task.id
        assert task.user_input.strip(), task.id


def test_select_by_task_id() -> None:
    selected = select_tasks(task_ids=("T01-locate-validate-session",))
    assert [task.id for task in selected] == ["T01-locate-validate-session"]


def test_select_by_category() -> None:
    selected = select_tasks(categories=(SECURITY_CATEGORY,))
    assert len(selected) == 3
    assert all(task.is_security for task in selected)


def test_select_without_filter_returns_all() -> None:
    assert select_tasks() == TASKS
