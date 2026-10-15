"""Sub-Agent Runner：隔离、能力、预算、提交协议、并发与生命周期。"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.agent.events import AgentEventType, CompositeEventHandler
from app.agent.result import AgentStopReason
from app.evidence import EvidenceRecorder, SQLiteEvidenceStore
from app.memory import (
    CORE_MEMORY_MESSAGE_NAME,
    MEMORY_INDEX_MESSAGE_NAME,
    MEMORY_POLICY_MESSAGE_NAME,
)
from app.models.types import (
    AgentMode,
    Message,
    MessageRole,
    ToolCall,
)
from app.subagent import CapabilityError, SubagentSettings
from app.tools.hooks import ToolExecutionContext
from app.trace import RunStatus, SQLiteTraceEventHandler, SQLiteTraceStore
from tests.offline.subagent.harness import model_response, tool_call, usage


class FakeMemoryManager:
    """验证 Memory 门面（Core + INDEX + Policy 注入）。"""

    def __init__(self) -> None:
        self.messages = (
            Message(
                role=MessageRole.SYSTEM,
                name=CORE_MEMORY_MESSAGE_NAME,
                content="# Core Memory\n\n用户偏好中文",
            ),
            Message(
                role=MessageRole.SYSTEM,
                name=MEMORY_INDEX_MESSAGE_NAME,
                content="# Long-term Memory Index\n\n[M001] demo\nCue: demo",
            ),
            Message(
                role=MessageRole.SYSTEM,
                name=MEMORY_POLICY_MESSAGE_NAME,
                content="Long-term memory is intentionally sparse.",
            ),
        )

    async def context_messages(self) -> tuple[Message, ...]:
        return self.messages


def _delegate_call(
    *,
    grants: list[str] | None = None,
    memory: str = "core",
    task: str = "调查 notes.txt 并给出结论",
    call_id: str = "p1",
) -> ToolCall:
    return tool_call(
        "subagent_run",
        {
            "task": task,
            "grants": grants or ["read_repo"],
            "memory": memory,
        },
        call_id=call_id,
    )


def _submitted_script(
    *,
    grants: list[str] | None = None,
    memory: str = "core",
    conclusions: str = "已确认 notes.txt 的内容。",
    uncovered: list[str] | None = None,
    sub_read_tokens: int = 100,
    sub_submit_tokens: int = 100,
    parent_tokens: int = 10,
) -> list:
    return [
        model_response(
            usage=usage(parent_tokens),
            tool_calls=(_delegate_call(grants=grants, memory=memory),),
        ),
        model_response(
            usage=usage(sub_read_tokens),
            tool_calls=(tool_call("read_file", {"path": "notes.txt"}, call_id="s1"),),
        ),
        model_response(
            usage=usage(sub_submit_tokens),
            tool_calls=(
                tool_call(
                    "submit_result",
                    {
                        "conclusions": conclusions,
                        "key_files": ["notes.txt:1"],
                        "snippets": [
                            {
                                "file": "notes.txt",
                                "lines": "1",
                                "code": "print('sub-only')",
                            }
                        ],
                        "uncertainties": ["行号以本次读取为准"],
                        "uncovered": uncovered or [],
                    },
                    call_id="s2",
                ),
            ),
        ),
        model_response(usage=usage(parent_tokens), content="调查完成。"),
    ]


def _direct_submit_script(
    *,
    memory: str = "core",
    grants: list[str] | None = None,
    conclusions: str = "ok",
) -> list:
    return [
        model_response(
            usage=usage(10),
            tool_calls=(_delegate_call(grants=grants, memory=memory),),
        ),
        model_response(
            usage=usage(20),
            tool_calls=(
                tool_call("submit_result", {"conclusions": conclusions}, call_id="s1"),
            ),
        ),
        model_response(usage=usage(10), content="调查完成。"),
    ]


async def test_delegation_isolates_context_and_returns_structured_result(
    build_harness,
) -> None:
    harness = build_harness(
        _submitted_script(memory="none"),
        workspace_files={"notes.txt": "hello-file-content"},
    )

    result = await harness.runtime.run(
        "parent-secret 问题",
        conversation_id="conv-1",
        event_handler=harness.events,
    )

    assert result.ok is True
    assert result.content == "调查完成。"
    # submit 终止子循环：4 次模型调用即可完成（父 2 + 子 2）。
    assert len(harness.adapter.requests) == 4

    payload = json.loads(result.tool_calls[0].result.output)
    assert payload["submitted"] is True
    assert payload["status"] == "completed"
    assert payload["truncated"] is False
    assert payload["grants"] == ["read_repo"]
    assert payload["grants_requested"] == ["read_repo"]
    assert payload["memory"] == "none"
    assert payload["conclusions"] == "已确认 notes.txt 的内容。"
    assert payload["key_files"] == ["notes.txt:1"]
    assert payload["uncertainties"] == ["行号以本次读取为准"]
    assert payload["uncovered_scope"] == []
    assert payload["steps"] == 2
    assert payload["tool_calls"] == 2
    assert payload["usage"]["total_tokens"] == 200
    assert payload["nested_run_id"] == (
        f"{result.run_id}:sub:{payload['subagent_id']}"
    )

    # 用量回流：子代理 token / model_calls 累加进父 Run。
    assert result.usage.total_tokens == 220
    assert result.usage.model_calls == 4

    # 上下文隔离：子请求只看到任务说明，父请求看不到子循环原文。
    sub_requests = harness.sub_requests()
    assert len(sub_requests) == 2
    first_sub = sub_requests[0]
    assert all(
        "parent-secret" not in (message.content or "")
        for message in first_sub.messages
    )
    user_messages = [
        message
        for message in first_sub.messages
        if message.role is MessageRole.USER
    ]
    assert len(user_messages) == 1
    assert "调查 notes.txt" in (user_messages[0].content or "")
    for request in harness.parent_requests():
        assert all(
            "hello-file-content" not in (message.content or "")
            for message in request.messages
        )

    # 工具面：只有授予能力映射出的工具 + submit_result。
    assert {tool.name for tool in first_sub.tools} == {
        "read_file",
        "list_files",
        "submit_result",
    }

    # 事件面：父只收到摘要事件；子循环内部事件不广播。
    types = [event.type for event in harness.events.events]
    assert types.count(AgentEventType.MODEL_STARTED) == 2
    assert types.count(AgentEventType.TOOL_STARTED) == 1
    started = next(
        event
        for event in harness.events.events
        if event.type is AgentEventType.SUBAGENT_STARTED
    )
    completed = next(
        event
        for event in harness.events.events
        if event.type is AgentEventType.SUBAGENT_COMPLETED
    )
    assert started.subagent_id == payload["subagent_id"]
    assert started.subagent_grants_requested == ("read_repo",)
    assert started.subagent_grants == ("read_repo",)
    assert started.subagent_memory == "none"
    assert completed.subagent_truncated is False
    assert completed.usage is not None
    assert completed.usage.total_tokens == 200
    assert completed.stop_reason is AgentStopReason.FINAL_ANSWER


async def test_subagent_without_submit_falls_back_to_final_text(
    build_harness,
) -> None:
    harness = build_harness(
        [
            model_response(
                usage=usage(10),
                tool_calls=(_delegate_call(),),
            ),
            model_response(
                usage=usage(50),
                content="自由文本结论：notes.txt 存在。",
            ),
            model_response(usage=usage(10), content="调查完成。"),
        ],
        workspace_files={"notes.txt": "x"},
    )

    result = await harness.runtime.run("q", event_handler=harness.events)

    payload = json.loads(result.tool_calls[0].result.output)
    assert payload["submitted"] is False
    assert payload["status"] == "completed"
    assert payload["truncated"] is False
    assert "自由文本结论" in payload["conclusions"]
    assert payload["steps"] == 1
    assert payload["tool_calls"] == 0
    assert len(harness.sub_requests()) == 1
    completed = [
        event
        for event in harness.events.events
        if event.type is AgentEventType.SUBAGENT_COMPLETED
    ]
    assert len(completed) == 1
    assert completed[0].subagent_truncated is False


async def test_invalid_submit_is_rejected_and_model_can_retry(
    build_harness,
) -> None:
    harness = build_harness(
        [
            model_response(usage=usage(10), tool_calls=(_delegate_call(),)),
            model_response(
                usage=usage(50),
                tool_calls=(tool_call("submit_result", {}, call_id="s1"),),
            ),
            model_response(
                usage=usage(50),
                tool_calls=(
                    tool_call(
                        "submit_result",
                        {"conclusions": "修正后的结论"},
                        call_id="s2",
                    ),
                ),
            ),
            model_response(usage=usage(10), content="调查完成。"),
        ],
        workspace_files={"notes.txt": "x"},
    )

    result = await harness.runtime.run("q")

    # 第一次提交缺少 conclusions 被拒，子循环继续；第二次成功终止。
    assert len(harness.sub_requests()) == 2
    payload = json.loads(result.tool_calls[0].result.output)
    assert payload["submitted"] is True
    assert payload["conclusions"] == "修正后的结论"


async def test_budget_warning_then_closing_marks_truncated(
    build_harness,
) -> None:
    settings = SubagentSettings(
        _env_file=None,
        token_budget=10_000,
        max_steps=12,
        max_tool_rounds=12,
    )
    responses: list = [model_response(usage=usage(10), tool_calls=(_delegate_call(),))]
    read_tokens = [1500, 1500, 1500, 1500, 1500, 1500, 800]
    for index, tokens in enumerate(read_tokens):
        responses.append(
            model_response(
                usage=usage(tokens),
                tool_calls=(
                    tool_call(
                        "read_file",
                        {"path": f"notes-{index}.txt"},
                        call_id=f"r{index}",
                    ),
                ),
            )
        )
    responses.append(
        model_response(
            usage=usage(100),
            tool_calls=(
                tool_call(
                    "submit_result",
                    {
                        "conclusions": "预算内结论",
                        "uncovered": ["未覆盖：剩余 3 个模块"],
                    },
                    call_id="s1",
                ),
            ),
        )
    )
    responses.append(model_response(usage=usage(10), content="调查完成。"))

    harness = build_harness(
        responses,
        settings=settings,
        workspace_files={f"notes-{index}.txt": "x" for index in range(7)},
    )
    result = await harness.runtime.run("q", event_handler=harness.events)

    payload = json.loads(result.tool_calls[0].result.output)
    assert payload["submitted"] is True
    assert payload["status"] == "completed"
    assert payload["truncated"] is True
    assert payload["uncovered_scope"] == ["未覆盖：剩余 3 个模块"]

    sub_requests = harness.sub_requests()
    assert len(sub_requests) == 8

    def system_text(request) -> str:
        # 跳过子代理系统提示词本身（约定文案里也提到"预算即将用尽"），
        # 只看循环额外注入的预算提示消息。
        return "\n".join(
            message.content or ""
            for message in request.messages[1:]
            if message.role is MessageRole.SYSTEM
        )

    # 80% 预警：第 7 个请求（累计 9000 ≥ 8000）起带预警，但工具面不变。
    for request in sub_requests[:6]:
        assert "预算即将用尽" not in system_text(request)
    assert "预算即将用尽" in system_text(sub_requests[6])
    assert "read_file" in {tool.name for tool in sub_requests[6].tools}

    # 95% 收口：最后一个请求只保留交付工具，并注入收口提示。
    assert [tool.name for tool in sub_requests[7].tools] == ["submit_result"]
    assert "收口线" in system_text(sub_requests[7])

    types = [event.type for event in harness.events.events]
    assert AgentEventType.RUN_BUDGET_WARNING not in types
    completed = next(
        event
        for event in harness.events.events
        if event.type is AgentEventType.SUBAGENT_COMPLETED
    )
    assert completed.subagent_truncated is True


async def test_budget_hard_limit_stops_sub_run(build_harness) -> None:
    settings = SubagentSettings(
        _env_file=None,
        token_budget=10_000,
        max_steps=12,
        max_tool_rounds=12,
    )
    responses: list = [model_response(usage=usage(10), tool_calls=(_delegate_call(),))]
    for index in range(3):
        responses.append(
            model_response(
                usage=usage(4000),
                tool_calls=(
                    tool_call(
                        "read_file",
                        {"path": f"notes-{index}.txt"},
                        call_id=f"r{index}",
                    ),
                ),
            )
        )
    responses.append(model_response(usage=usage(10), content="调查完成。"))

    harness = build_harness(
        responses,
        settings=settings,
        workspace_files={f"notes-{index}.txt": "x" for index in range(3)},
    )
    result = await harness.runtime.run("q")

    payload = json.loads(result.tool_calls[0].result.output)
    assert payload["status"] == "budget"
    assert payload["submitted"] is False
    assert payload["truncated"] is True
    assert payload["stop_reason"] == "run_budget"
    assert payload["error"]
    # 未按协议提交时用回退结论说明停止原因与预算细节。
    assert "未按协议提交" in payload["conclusions"]
    assert "budget exceeded" in payload["conclusions"]
    # 硬停后不再发子请求；已发生的成本仍如实回流。
    assert len(harness.sub_requests()) == 3
    assert payload["usage"]["total_tokens"] == 12000


async def test_plan_mode_downgrades_web_fetch_and_records_reason(
    build_harness,
) -> None:
    harness = build_harness(
        [
            model_response(
                usage=usage(10),
                tool_calls=(
                    _delegate_call(
                        grants=["read_repo", "web_fetch:docs.python.org"],
                        memory="none",
                    ),
                ),
            ),
            model_response(
                usage=usage(20),
                tool_calls=(
                    tool_call(
                        "submit_result",
                        {"conclusions": "只读调查完成"},
                        call_id="s1",
                    ),
                ),
            ),
            model_response(usage=usage(10), content="规划完成。"),
        ],
        workspace_files={"notes.txt": "x"},
    )

    result = await harness.runtime.run(
        "plan this",
        mode=AgentMode.PLAN,
        event_handler=harness.events,
    )

    assert result.ok is True
    payload = json.loads(result.tool_calls[0].result.output)
    assert payload["grants_requested"] == [
        "read_repo",
        "web_fetch:docs.python.org",
    ]
    assert payload["grants"] == ["read_repo"]
    assert payload["grant_downgrades"] == [
        {
            "requested": "web_fetch:docs.python.org",
            "effective": None,
            "reason": "plan_mode",
        }
    ]

    started = next(
        event
        for event in harness.events.events
        if event.type is AgentEventType.SUBAGENT_STARTED
    )
    assert started.subagent_grants_requested == (
        "read_repo",
        "web_fetch:docs.python.org",
    )
    assert started.subagent_grants == ("read_repo",)

    sub_tool_names = {tool.name for tool in harness.sub_requests()[0].tools}
    assert "http_request" not in sub_tool_names
    assert "read_file" in sub_tool_names


MEMORY_CASES = [
    ("none", False, False),
    ("core", True, False),
    ("full", True, True),
]


@pytest.mark.parametrize(
    ("level", "expect_core", "expect_index"),
    MEMORY_CASES,
)
async def test_memory_levels_inject_only_expected_messages(
    build_harness,
    level: str,
    expect_core: bool,
    expect_index: bool,
) -> None:
    harness = build_harness(
        _direct_submit_script(memory=level),
        workspace_files={"notes.txt": "x"},
        memory_manager=FakeMemoryManager(),
    )

    result = await harness.runtime.run("q")

    payload = json.loads(result.tool_calls[0].result.output)
    assert payload["memory"] == level

    prompt = harness.sub_requests()[0].messages[0].content or ""
    assert ("用户偏好中文" in prompt) is expect_core
    assert ("[M001]" in prompt) is expect_index
    # Memory Policy 永不转发。
    assert "intentionally sparse" not in prompt
    if level == "none":
        assert "运行时记忆" not in prompt
    else:
        assert "运行时记忆" in prompt


async def test_memory_core_without_parent_memory_records_none(
    build_harness,
) -> None:
    harness = build_harness(
        _direct_submit_script(memory="core"),
        workspace_files={"notes.txt": "x"},
    )

    result = await harness.runtime.run("q")

    payload = json.loads(result.tool_calls[0].result.output)
    assert payload["memory"] == "none"
    prompt = harness.sub_requests()[0].messages[0].content or ""
    assert "运行时记忆" not in prompt


async def test_parallel_delegations_obey_concurrency_cap(build_harness) -> None:
    settings = SubagentSettings(
        _env_file=None,
        max_concurrency=3,
        max_invocations_per_run=5,
        wall_clock_seconds=30,
    )
    calls = tuple(
        _delegate_call(
            grants=["web_search"],
            memory="none",
            task=f"调查第 {index} 块",
            call_id=f"c{index}",
        )
        for index in range(5)
    )
    responses: list = [model_response(usage=usage(1), tool_calls=calls)]
    for index in range(5):
        responses.append(
            model_response(
                usage=usage(1),
                tool_calls=(
                    tool_call(
                        "web_search",
                        {"query": f"q{index}"},
                        call_id=f"q{index}",
                    ),
                ),
            )
        )
        responses.append(
            model_response(
                usage=usage(1),
                tool_calls=(
                    tool_call(
                        "submit_result",
                        {"conclusions": f"结论 {index}"},
                        call_id=f"s{index}",
                    ),
                ),
            )
        )
    responses.append(model_response(usage=usage(1), content="全部完成。"))

    harness = build_harness(responses, settings=settings, delay=0.05)
    result = await harness.runtime.run("并发委派")

    assert result.ok is True
    payloads = [
        json.loads(record.result.output) for record in result.tool_calls
    ]
    assert len(payloads) == 5
    assert all(payload["submitted"] is True for payload in payloads)
    # 信号量上限 3：并发模型请求峰值恰好为 3。
    assert harness.adapter.peak_active == 3


async def test_parallel_delegations_persist_trace_and_evidence(
    tmp_path,
    build_harness,
) -> None:
    trace_store = SQLiteTraceStore(tmp_path / "trace-parallel.db")
    await trace_store.initialize()
    evidence_store = SQLiteEvidenceStore(tmp_path / "evidence-parallel.db")
    await evidence_store.initialize()
    settings = SubagentSettings(
        _env_file=None,
        max_concurrency=3,
        max_invocations_per_run=4,
        wall_clock_seconds=30,
    )

    calls = tuple(
        _delegate_call(
            grants=["read_repo"],
            memory="none",
            task=f"核对第 {index} 块",
            call_id=f"c{index}",
        )
        for index in range(4)
    )
    responses: list = [model_response(usage=usage(1), tool_calls=calls)]
    for index in range(4):
        responses.append(
            model_response(
                usage=usage(1),
                tool_calls=(
                    tool_call(
                        "read_file",
                        {"path": f"notes-{index}.txt"},
                        call_id=f"r{index}",
                    ),
                ),
            )
        )
        responses.append(
            model_response(
                usage=usage(1),
                tool_calls=(
                    tool_call(
                        "submit_result",
                        {"conclusions": f"结论 {index}"},
                        call_id=f"s{index}",
                    ),
                ),
            )
        )
    responses.append(model_response(usage=usage(1), content="全部完成。"))

    harness = build_harness(
        responses,
        settings=settings,
        delay=0.02,
        workspace_files={f"notes-{index}.txt": "x" for index in range(4)},
        trace_store=trace_store,
        output_recorder=EvidenceRecorder(evidence_store),
    )
    result = await harness.runtime.run("并发委派", conversation_id="conv-parallel")

    payloads = [json.loads(record.result.output) for record in result.tool_calls]
    assert len(payloads) == 4
    assert all(payload["submitted"] is True for payload in payloads)
    # 并发的子 Run 各自拥有独立 Trace，终态为 COMPLETED。
    for payload in payloads:
        nested_events = await trace_store.load_events(payload["nested_run_id"])
        assert nested_events
        trace = await trace_store.get(payload["nested_run_id"])
        assert trace is not None
        assert trace.status is RunStatus.COMPLETED
    records = await evidence_store.list_recent(
        conversation_id="conv-parallel",
        limit=50,
    )
    assert sum(1 for record in records if record.tool_name == "read_file") >= 4
    assert sum(1 for record in records if record.tool_name == "subagent_run") == 4


async def test_invocation_quota_is_per_run_and_releasable(
    build_harness,
) -> None:
    harness = build_harness(
        [
            model_response(
                usage=usage(1),
                tool_calls=(
                    tool_call(
                        "submit_result",
                        {"conclusions": f"第 {index} 次"},
                        call_id=f"s{index}",
                    ),
                ),
            )
            for index in range(3)
        ],
        settings=SubagentSettings(_env_file=None, max_invocations_per_run=1),
        workspace_files={"notes.txt": "x"},
    )

    async def invoke(run_id: str):
        return await harness.runner.run(
            parent_run_id=run_id,
            conversation_id="conv",
            step=1,
            task="t",
            grants=["read_repo"],
            memory="none",
            mode=AgentMode.NORMAL,
        )

    first = await invoke("run-x")
    assert first.submitted is True
    with pytest.raises(CapabilityError, match="上限"):
        await invoke("run-x")
    # 被拒绝的委派不消耗模型调用。
    assert len(harness.adapter.requests) == 1

    await harness.runner.release_run("run-x")
    second = await invoke("run-x")
    assert second.submitted is True
    third = await invoke("run-y")
    assert third.submitted is True
    assert len(harness.adapter.requests) == 3


async def test_cancellation_propagates_and_run_finalizers_execute(
    build_harness,
) -> None:
    harness = build_harness(
        [
            model_response(
                usage=usage(1),
                tool_calls=(_delegate_call(),),
            ),
        ],
        workspace_files={"notes.txt": "x"},
    )
    # 第 2 个请求是子循环的首个模型调用；命中后永久阻塞。
    harness.adapter.block_on_request = 2

    task = asyncio.create_task(
        harness.runtime.run("q", conversation_id="conv-cancel")
    )
    for _ in range(500):
        if harness.adapter.blocked:
            break
        await asyncio.sleep(0.01)
    assert harness.adapter.blocked is True

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # 取消穿透到子循环模型调用，Run 收尾钩子（含配额清理）仍执行。
    assert harness.adapter.cancelled is True
    assert len(harness.finalizer_runs) == 1
    assert harness.runner._invocations == {}


async def test_invalid_grant_is_rejected_without_events_or_model_calls(
    build_harness,
) -> None:
    harness = build_harness(
        [
            model_response(
                usage=usage(1),
                tool_calls=(_delegate_call(grants=["run_shell"]),),
            ),
            model_response(usage=usage(1), content="改用其他方式。"),
        ]
    )

    result = await harness.runtime.run("q", event_handler=harness.events)

    record = result.tool_calls[0]
    assert record.result.success is False
    assert "run_shell" in (record.result.error or "")
    # 确定性拒绝发生在任何子模型调用与 SUBAGENT_* 事件之前。
    assert len(harness.adapter.requests) == 2
    types = [event.type for event in harness.events.events]
    assert AgentEventType.SUBAGENT_STARTED not in types
    assert AgentEventType.SUBAGENT_COMPLETED not in types
    assert AgentEventType.SUBAGENT_FAILED not in types


async def test_skill_scope_prunes_grants_and_records_reason(
    build_harness,
) -> None:
    """Active Skill 收窄掉的能力不进生效值，并以降级记录如实标注。"""

    harness = build_harness(
        [
            model_response(
                usage=usage(10),
                tool_calls=(
                    tool_call(
                        "submit_result",
                        {"conclusions": "只用到搜索。"},
                        call_id="s1",
                    ),
                ),
            )
        ]
    )

    outcome = await harness.runner.run(
        parent_run_id="run-skill",
        conversation_id="conv-skill",
        step=1,
        task="只允许搜索的委派",
        grants=["read_repo", "web_search"],
        memory="none",
        mode=AgentMode.NORMAL,
        skill_tool_scope=frozenset({"web_search"}),
    )

    assert outcome.submitted is True
    assert outcome.grants_requested == ("read_repo", "web_search")
    assert outcome.grants == ("web_search",)
    assert [(item.requested, item.reason) for item in outcome.downgrades] == [
        ("read_repo", "skill_scope")
    ]
    request = harness.adapter.requests[0]
    assert {tool.name for tool in request.tools} == {
        "web_search",
        "submit_result",
    }
    prompt = request.messages[0].content or ""
    assert "read_repo" not in prompt
    assert "web_search" in prompt


async def test_grants_fully_filtered_by_skill_scope_are_rejected(
    build_harness,
) -> None:
    harness = build_harness([])

    with pytest.raises(CapabilityError):
        await harness.runner.run(
            parent_run_id="run-skill",
            conversation_id="conv-skill",
            step=1,
            task="交集为空的委派",
            grants=["read_repo"],
            memory="none",
            mode=AgentMode.NORMAL,
            skill_tool_scope=frozenset({"web_search"}),
        )

    # 拒绝发生在任何子模型调用、SUBAGENT_* 事件与配额消耗之前。
    assert harness.adapter.requests == []
    assert harness.runner._invocations == {}


async def test_nested_trace_persists_sub_run_events(
    tmp_path,
    build_harness,
) -> None:
    store = SQLiteTraceStore(tmp_path / "trace.db")
    await store.initialize()

    harness = build_harness(
        _submitted_script(memory="none"),
        workspace_files={"notes.txt": "x"},
        trace_store=store,
    )
    # 父 Run 的 Trace 由测试显式挂载；子 Run 由 runner 自身挂载。
    handler = CompositeEventHandler(
        SQLiteTraceEventHandler(store),
        harness.events,
    )
    result = await harness.runtime.run(
        "q",
        conversation_id="conv-trace",
        event_handler=handler,
    )

    payload = json.loads(result.tool_calls[0].result.output)
    nested_run_id = payload["nested_run_id"]

    nested_events = await store.load_events(nested_run_id)
    nested_types = [event.type for event in nested_events]
    assert AgentEventType.AGENT_STARTED in nested_types
    assert AgentEventType.MODEL_COMPLETED in nested_types
    assert AgentEventType.TOOL_COMPLETED in nested_types
    assert nested_types[-1] in (
        AgentEventType.AGENT_COMPLETED,
        AgentEventType.AGENT_FAILED,
    )

    trace = await store.get(nested_run_id)
    assert trace is not None
    assert trace.status is RunStatus.COMPLETED

    # 父与子 Trace 相互独立：子的 TOOL_COMPLETED 不进入父事件流。
    parent_types = [
        event.type for event in await store.load_events(result.run_id)
    ]
    assert AgentEventType.SUBAGENT_STARTED in [
        event.type for event in harness.events.events
    ]
    assert parent_types.count(AgentEventType.TOOL_COMPLETED) == 1


async def test_subagent_tool_outputs_archived_under_parent_run(
    tmp_path,
    build_harness,
) -> None:
    store = SQLiteEvidenceStore(tmp_path / "evidence.db")
    await store.initialize()
    recorder = EvidenceRecorder(store)

    harness = build_harness(
        _submitted_script(),
        workspace_files={"notes.txt": "hello-file-content"},
        output_recorder=recorder,
    )

    result = await harness.runtime.run("q", conversation_id="conv-evidence")

    records = await store.list_recent(conversation_id="conv-evidence", limit=20)
    read_records = [
        record for record in records if record.tool_name == "read_file"
    ]
    assert read_records
    # 子循环工具证据归属父 Run（run_id 复用），可被 evidence_read 回读。
    assert all(record.run_id == result.run_id for record in read_records)
    assert any(record.tool_name == "subagent_run" for record in records)


async def test_subagent_tool_definition_and_context_requirements(
    build_harness,
) -> None:
    harness = build_harness([])
    tool = harness.tool_registry.get("subagent_run")
    definition = tool.definition

    assert definition.timeout_seconds == (
        harness.runner.settings.wall_clock_seconds + 30.0
    )
    assert definition.closing_allowed is False
    assert definition.parameters["required"] == ["task", "grants"]
    assert definition.parameters["properties"]["memory"]["enum"] == [
        "none",
        "core",
        "full",
    ]

    with pytest.raises(ValueError):
        await tool.execute({})
    with pytest.raises(ValueError):
        await tool.execute_with_context(
            {"task": "t", "grants": ["read_repo"]},
            ToolExecutionContext(
                tool_call=ToolCall(id="x", name="subagent_run"),
                run_id=None,
            ),
        )
    with pytest.raises(ValueError):
        await tool.execute_with_context(
            {"task": "t", "grants": []},
            ToolExecutionContext(
                tool_call=ToolCall(id="x", name="subagent_run"),
                run_id="run-1",
            ),
        )
