"""Post-Run Memory Reflection 的离线测试。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.agent.events import AgentEventType, InMemoryEventHandler
from app.agent.result import AgentStopReason
from app.agent.runtime import AgentRuntime
from app.memory import (
    FakeEmbeddingAdapter,
    MemoryManager,
    MemoryReflectionConfig,
    MemoryReflectionInput,
    PostRunMemoryReflector,
    ReflectionAction,
    ReflectionDecision,
    register_memory_tools,
)
from app.models.adapter import ModelAdapter
from app.models.config import ModelSettings, ProviderConfig
from app.models.registry import ModelAdapterRegistry
from app.models.types import (
    ApiStyle,
    Message,
    MessageRole,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ToolCall,
)
from app.tools.registry import ToolRegistry


class FakeAdapter(ModelAdapter):
    """按顺序返回固定结果的离线模型适配器。"""

    def __init__(
        self,
        config: ProviderConfig,
        responses: Sequence[ModelResponse | Exception],
        *,
        delay_seconds: float = 0.0,
        before_return: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(config)
        self.responses = list(responses)
        self.delay_seconds = delay_seconds
        self.before_return = before_return
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.before_return is not None:
            await self.before_return()
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def close(self) -> None:
        pass


def _config(provider: str, model: str) -> ProviderConfig:
    return ProviderConfig(
        provider=provider,
        model=model,
        api_key=SecretStr("offline-test-key"),
        api_style=ApiStyle.CHAT_COMPLETIONS,
    )


def _response(
    content: str,
    *,
    provider: str = "reflect",
    model: str = "reflection-model",
    usage: ModelUsage | None = None,
) -> ModelResponse:
    return ModelResponse(
        id="offline-response",
        provider=provider,
        model=model,
        message=Message(role=MessageRole.ASSISTANT, content=content),
        usage=usage or ModelUsage(),
    )


def _registry(**adapters: FakeAdapter) -> ModelAdapterRegistry:
    registry = ModelAdapterRegistry(ModelSettings(_env_file=None))
    for provider, adapter in adapters.items():
        registry.register(
            provider,
            lambda _, current=adapter: current,
            config=adapter.config,
        )
    return registry


def _decision_response(payload: dict[str, object]) -> ModelResponse:
    return _response(json.dumps(payload, ensure_ascii=False))


def _input(
    *,
    recalled_memory_ids: tuple[str, ...] = (),
) -> MemoryReflectionInput:
    return MemoryReflectionInput(
        run_id="run-1",
        conversation_id="conversation-1",
        user_input="我们决定继续使用 Markdown 长期记忆。",
        final_answer="架构调整已经完成。",
        recalled_memory_ids=recalled_memory_ids,
    )


def _reflection_config(**overrides: object) -> MemoryReflectionConfig:
    values: dict[str, object] = {
        "provider": "reflect",
        "model": "reflection-model",
    }
    values.update(overrides)
    return MemoryReflectionConfig(
        _env_file=None,
        **values,
    )


async def _manager(path: Path, *, max_active: int = 25) -> MemoryManager:
    manager = MemoryManager(path, max_active=max_active)
    await manager.initialize()
    return manager


async def _wait_for_embeddings(manager: MemoryManager) -> None:
    for _ in range(200):
        if manager.embedding_backfill_status == "complete":
            return
        await asyncio.sleep(0.01)
    raise AssertionError("embedding backfill did not complete")


@pytest.mark.asyncio
async def test_reflection_noop_does_not_mutate_store(tmp_path: Path) -> None:
    manager = await _manager(tmp_path / "memory")
    adapter = FakeAdapter(
        _config("reflect", "reflection-model"),
        [_response('{"action":"none","reason":"只有临时任务信息"}')],
    )
    reflector = PostRunMemoryReflector(
        _registry(reflect=adapter),
        config=_reflection_config(),
    )

    proposal = await reflector.decide(_input())

    assert proposal.decision is not None
    assert proposal.decision.action is ReflectionAction.NONE
    assert proposal.error is None
    assert await manager.store.count_active() == 0


@pytest.mark.asyncio
async def test_reflection_prompt_treats_confirmed_same_topic_delta_as_update(
) -> None:
    adapter = FakeAdapter(
        _config("reflect", "reflection-model"),
        [
            _response(
                '{"action":"update","memory_id":"M001",'
                '"title":"普通记忆容量","summary":"容量维护与并发规则",'
                '"content":"上限 25 条；归档前校验 revision。",'
                '"reason":"用户明确确认了同主题的新增规则"}'
            )
        ],
    )
    reflector = PostRunMemoryReflector(
        _registry(reflect=adapter),
        config=_reflection_config(capture_raw_io=True),
    )
    reflection_input = MemoryReflectionInput(
        run_id="run-update",
        conversation_id="conversation-1",
        user_input=(
            "我们刚完成的新规则是：归档前必须校验候选 revision，并且更新 "
            "Recall Cue。"
        ),
        final_answer="当前完整规则已经说明。",
        recalled_memory_ids=("M001",),
    )

    proposal = await reflector.decide(reflection_input)

    assert proposal.decision is not None
    assert proposal.decision.action is ReflectionAction.UPDATE
    assert proposal.input_json is not None
    assert "刚完成的新规则" in proposal.input_json
    assert proposal.raw_output is not None
    prompt = adapter.requests[0].messages[0].content or ""
    assert "prefer UPDATE or APPEND over" in prompt
    assert "NONE, even when the new rule" in prompt
    assert "not require a code edit or file mutation" in prompt


@pytest.mark.asyncio
async def test_reflection_prompt_never_uses_ordinary_memory_as_core_fallback(
) -> None:
    adapter = FakeAdapter(
        _config("reflect", "reflection-model"),
        [_response('{"action":"none","reason":"这是 Core 级全局偏好"}')],
    )
    reflector = PostRunMemoryReflector(
        _registry(reflect=adapter),
        config=_reflection_config(),
    )
    reflection_input = MemoryReflectionInput(
        run_id="run-core-preference",
        conversation_id="conversation-1",
        user_input="以后所有回答都先给结论，再解释原因。",
        final_answer="好的，我记住了。",
    )

    proposal = await reflector.decide(reflection_input)

    assert proposal.decision is not None
    assert proposal.decision.action is ReflectionAction.NONE
    prompt = adapter.requests[0].messages[0].content or ""
    assert "Core-worthy and must return NONE" in prompt
    assert "main Agent did" in prompt
    assert "not call core_memory_update" in prompt
    assert "Never CREATE or UPDATE ordinary memory as a fallback" in prompt


@pytest.mark.asyncio
async def test_reflection_raw_io_is_disabled_by_default() -> None:
    adapter = FakeAdapter(
        _config("reflect", "reflection-model"),
        [_response('{"action":"none","reason":"没有耐久变化"}')],
    )
    reflector = PostRunMemoryReflector(
        _registry(reflect=adapter),
        config=_reflection_config(),
    )

    proposal = await reflector.decide(_input())

    assert proposal.input_json is None
    assert proposal.raw_output is None


@pytest.mark.asyncio
async def test_reflection_create_uses_manager_and_rebuilds_index(
    tmp_path: Path,
) -> None:
    manager = await _manager(tmp_path / "memory")
    decision = (
        '{"action":"create","title":"Memory 架构决定",'
        '"summary":"Evolyn 使用 Markdown Memory 与运行后反思",'
        '"content":"普通长期记忆由 Post-Run Reflector 沉淀。",'
        '"reason":"这是跨会话仍有价值的架构决定"}'
    )
    adapter = FakeAdapter(
        _config("reflect", "reflection-model"),
        [_response(decision)],
    )
    main = FakeAdapter(
        _config("main", "main-model"),
        [_response("主任务完成", provider="main", model="main-model")],
    )
    registry = _registry(main=main, reflect=adapter)
    reflector = PostRunMemoryReflector(
        registry,
        config=_reflection_config(),
    )
    runtime = AgentRuntime(
        registry,
        ToolRegistry(),
        provider="main",
        memory_manager=manager,
        memory_reflector=reflector,
    )

    result = await runtime.run("完成记忆架构调整")

    assert result.ok is True
    record = await manager.store.load("M001")
    assert record is not None
    assert "Post-Run Reflector" in record.content
    assert "Evolyn 使用 Markdown Memory" in (
        await manager.index.load() or ""
    )


@pytest.mark.asyncio
async def test_reflection_update_decision_does_not_write_store(
    tmp_path: Path,
) -> None:
    manager = await _manager(tmp_path / "memory")
    record = await manager.create(
        title="Memory 架构决定",
        summary="Markdown Memory 架构",
        content="旧决定",
    )
    (manager.memory_dir / "INDEX.md").write_text("stale", encoding="utf-8")
    decision = (
        '{"action":"update","memory_id":"m001",'
        '"title":"Memory 架构决定",'
        '"summary":"普通记忆由运行后反思统一沉淀",'
        '"content":"新决定：普通记忆改由 Post-Run Reflector 写入。",'
        '"reason":"本轮改变了已有架构决定"}'
    )
    adapter = FakeAdapter(
        _config("reflect", "reflection-model"),
        [_response(decision)],
    )
    reflector = PostRunMemoryReflector(
        _registry(reflect=adapter),
        config=_reflection_config(),
    )

    proposal = await reflector.decide(
        _input(recalled_memory_ids=(record.id,))
    )

    assert proposal.decision is not None
    assert proposal.decision.action is ReflectionAction.UPDATE
    assert proposal.decision.memory_id == record.id
    updated = await manager.store.load(record.id)
    assert updated is not None
    assert updated.content == "旧决定"
    assert await manager.index.load() == "stale"


@pytest.mark.asyncio
async def test_reflection_update_requires_current_run_memory_read(
    tmp_path: Path,
) -> None:
    manager = await _manager(tmp_path / "memory")
    record = await manager.create(
        title="已有决定",
        summary="只能从 Index 看到的 cue",
        content="不能被猜测覆盖的完整正文",
    )
    before = (manager.memory_dir / "active" / f"{record.id}.md").read_text(
        encoding="utf-8"
    )
    adapter = FakeAdapter(
        _config("reflect", "reflection-model"),
        [
            _response(
                '{"action":"update","memory_id":"M001",'
                '"title":"已有决定",'
                '"summary":"模型根据 cue 猜测的错误更新",'
                '"content":"模型根据 cue 猜测的替代正文",'
                '"reason":"错误地尝试更新"}'
            )
        ],
    )
    main = FakeAdapter(
        _config("main", "main-model"),
        [_response("任务完成", provider="main", model="main-model")],
    )
    registry = _registry(main=main, reflect=adapter)
    reflector = PostRunMemoryReflector(
        registry,
        config=_reflection_config(),
    )
    events = InMemoryEventHandler()
    runtime = AgentRuntime(
        registry,
        ToolRegistry(),
        provider="main",
        memory_manager=manager,
        memory_reflector=reflector,
    )

    result = await runtime.run("没有读取旧记忆", event_handler=events)

    assert result.ok is True
    failed = next(
        event
        for event in events.events
        if event.type is AgentEventType.MEMORY_REFLECTION_FAILED
    )
    assert failed.reflection_memory_id == record.id
    assert failed.reflection_error is not None
    assert "memory_read success" in failed.reflection_error
    assert (
        manager.memory_dir / "active" / f"{record.id}.md"
    ).read_text(encoding="utf-8") == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("responses", "delay_seconds", "timeout_seconds", "error_text"),
    [
        ([RuntimeError("provider unavailable")], 0.0, 1.0, "provider unavailable"),
        ([_response("not-json")], 0.0, 1.0, "ValidationError"),
        ([_response('{"action":"none","reason":"noop"}')], 0.05, 0.01, "TimeoutError"),
    ],
)
async def test_reflection_failures_are_isolated(
    tmp_path: Path,
    responses: Sequence[ModelResponse | Exception],
    delay_seconds: float,
    timeout_seconds: float,
    error_text: str,
) -> None:
    manager = await _manager(tmp_path / "memory")
    adapter = FakeAdapter(
        _config("reflect", "reflection-model"),
        responses,
        delay_seconds=delay_seconds,
    )
    reflector = PostRunMemoryReflector(
        _registry(reflect=adapter),
        config=_reflection_config(
            timeout_seconds=timeout_seconds,
            max_attempts=1,
        ),
    )

    proposal = await reflector.decide(_input())

    assert proposal.error is not None and error_text in proposal.error
    assert await manager.store.count_active() == 0


@pytest.mark.asyncio
async def test_reflection_retries_truncated_json_within_one_timeout() -> None:
    """结构化输出被截断时只重试一次，并聚合两次调用用量。"""

    first_usage = ModelUsage(input_tokens=10, output_tokens=20, total_tokens=30)
    second_usage = ModelUsage(input_tokens=11, output_tokens=7, total_tokens=18)
    adapter = FakeAdapter(
        _config("reflect", "reflection-model"),
        [
            _response(
                '{"action":"create","title":"未结束',
                usage=first_usage,
            ),
            _response(
                '{"action":"none","reason":"没有耐久变化"}',
                usage=second_usage,
            ),
        ],
    )
    reflector = PostRunMemoryReflector(
        _registry(reflect=adapter),
        config=_reflection_config(capture_raw_io=True),
    )

    proposal = await reflector.decide(_input())

    assert proposal.error is None
    assert proposal.decision is not None
    assert proposal.decision.action is ReflectionAction.NONE
    assert proposal.attempts == 2
    assert len(adapter.requests) == 2
    assert proposal.usage.input_tokens == 21
    assert proposal.usage.output_tokens == 27
    assert proposal.usage.total_tokens == 48
    retry_content = adapter.requests[1].messages[1].content or ""
    assert "previous response was empty, invalid, or truncated" in retry_content


@pytest.mark.asyncio
async def test_runtime_final_answer_triggers_independent_reflection_model(
    tmp_path: Path,
) -> None:
    manager = await _manager(tmp_path / "memory")
    main = FakeAdapter(
        _config("main", "main-model"),
        [_response("主任务完成", provider="main", model="main-model")],
    )
    reflection_usage = ModelUsage(input_tokens=9, output_tokens=4, total_tokens=13)
    reflect = FakeAdapter(
        _config("reflect", "adapter-default"),
        [
            _response(
                '{"action":"none","reason":"没有耐久的新信息"}',
                usage=reflection_usage,
            )
        ],
    )
    registry = _registry(main=main, reflect=reflect)
    reflector = PostRunMemoryReflector(
        registry,
        config=_reflection_config(
            model="cheap-memory-model",
            max_output_tokens=321,
            temperature=0.2,
        ),
        default_provider="main",
        default_model="main-model",
    )
    events = InMemoryEventHandler()
    runtime = AgentRuntime(
        registry,
        ToolRegistry(),
        provider="main",
        model="main-model",
        memory_manager=manager,
        memory_reflector=reflector,
    )

    result = await runtime.run("完成当前任务", event_handler=events)

    assert result.ok is True
    assert len(main.requests) == 1
    assert len(reflect.requests) == 1
    request = reflect.requests[0]
    assert request.model == "cheap-memory-model"
    assert request.max_output_tokens == 321
    assert request.temperature == 0.2
    assert request.tools == ()
    assert [event.type for event in events.events][-3:] == [
        AgentEventType.AGENT_COMPLETED,
        AgentEventType.MEMORY_REFLECTION_STARTED,
        AgentEventType.MEMORY_REFLECTION_COMPLETED,
    ]
    completed = events.events[-1]
    assert completed.provider == "reflect"
    assert completed.model == "cheap-memory-model"
    assert completed.reflection_action == "none"
    assert completed.usage == reflection_usage


@pytest.mark.asyncio
async def test_runtime_gate_skips_obvious_smalltalk_without_calling_reflector(
    tmp_path: Path,
) -> None:
    manager = await _manager(tmp_path / "memory")
    main = FakeAdapter(
        _config("main", "main-model"),
        [_response("你好", provider="main", model="main-model")],
    )
    reflect = FakeAdapter(
        _config("reflect", "reflection-model"),
        [_response('{"action":"none","reason":"unused"}')],
    )
    registry = _registry(main=main, reflect=reflect)
    events = InMemoryEventHandler()
    runtime = AgentRuntime(
        registry,
        ToolRegistry(),
        provider="main",
        memory_manager=manager,
        memory_reflector=PostRunMemoryReflector(
            registry,
            config=_reflection_config(),
        ),
    )

    result = await runtime.run("你好！", event_handler=events)

    assert result.ok is True
    assert reflect.requests == []
    skipped = events.events[-1]
    assert skipped.type is AgentEventType.MEMORY_REFLECTION_SKIPPED
    assert skipped.reflection_skip_reason == "gate:smalltalk"


@pytest.mark.asyncio
async def test_runtime_successful_memory_read_authorizes_reflection_update(
    tmp_path: Path,
) -> None:
    manager = await _manager(tmp_path / "memory")
    record = await manager.create(
        title="项目方向",
        summary="Evolyn 的长期记忆方向",
        content="旧方向",
    )
    (manager.memory_dir / "INDEX.md").write_text("stale", encoding="utf-8")
    main = FakeAdapter(
        _config("main", "main-model"),
        [
            ModelResponse(
                id="read-response",
                provider="main",
                model="main-model",
                message=Message(
                    role=MessageRole.ASSISTANT,
                    tool_calls=(
                        ToolCall(
                            id="read-memory-1",
                            name="memory_read",
                            arguments={"memory_id": record.id},
                        ),
                    ),
                ),
            ),
            _response("已完成架构调整", provider="main", model="main-model"),
        ],
    )
    reflect = FakeAdapter(
        _config("reflect", "reflection-model"),
        [
            _response(
                '{"action":"update","memory_id":"M001",'
                '"title":"项目长期记忆方向",'
                '"summary":"普通记忆由运行后反思模型沉淀",'
                '"content":"新方向：普通记忆由运行后反思模型沉淀。",'
                '"reason":"本轮更新了已有架构决定"}'
            )
        ],
    )
    registry = _registry(main=main, reflect=reflect)
    tools = ToolRegistry()
    register_memory_tools(tools, manager)
    runtime = AgentRuntime(
        registry,
        tools,
        provider="main",
        memory_manager=manager,
        memory_reflector=PostRunMemoryReflector(
            registry,
            config=_reflection_config(),
        ),
    )

    result = await runtime.run("读取旧决定并完成调整")

    assert result.ok is True
    updated = await manager.store.load(record.id)
    assert updated is not None
    assert updated.title == "项目长期记忆方向"
    assert updated.summary == "普通记忆由运行后反思模型沉淀"
    assert "运行后反思模型" in updated.content
    assert "普通记忆由运行后反思模型沉淀" in (
        await manager.index.load() or ""
    )


def test_reflection_append_decision_validates_fields() -> None:
    decision = ReflectionDecision.model_validate(
        {
            "action": "append",
            "memory_id": "m001",
            "addition": "补充：灰度发布前必须跑数据库迁移。",
            "reason": "补充部署规则",
        }
    )

    assert decision.action is ReflectionAction.APPEND
    assert decision.memory_id == "M001"
    assert decision.addition == "补充：灰度发布前必须跑数据库迁移。"

    with pytest.raises(ValidationError):
        ReflectionDecision.model_validate(
            {"action": "append", "memory_id": "M001", "reason": "缺少增量"}
        )
    with pytest.raises(ValidationError):
        ReflectionDecision.model_validate(
            {
                "action": "append",
                "memory_id": "M001",
                "addition": "补充",
                "content": "不允许完整正文",
                "reason": "字段冲突",
            }
        )


@pytest.mark.asyncio
async def test_manager_append_keeps_body_and_bumps_revision(
    tmp_path: Path,
) -> None:
    manager = await _manager(tmp_path / "memory")
    record = await manager.create(
        title="部署流程",
        summary="发布部署的既定流程",
        content="先跑测试，再构建镜像。",
    )

    updated = await manager.append(
        record.id,
        addition="补充：灰度发布前必须跑数据库迁移。",
        reason="追加部署补充事实",
    )

    assert updated.revision == record.revision + 1
    assert updated.content == (
        "先跑测试，再构建镜像。\n\n补充：灰度发布前必须跑数据库迁移。"
    )
    assert updated.title == record.title
    assert updated.summary == record.summary

    with pytest.raises(ValueError, match="revision conflict"):
        await manager.append_if_revision(
            record.id,
            expected_revision=record.revision,
            addition="基于旧 revision 的追加",
            reason="revision 冲突测试",
        )


@pytest.mark.asyncio
async def test_runtime_successful_memory_read_authorizes_reflection_append(
    tmp_path: Path,
) -> None:
    manager = await _manager(tmp_path / "memory")
    record = await manager.create(
        title="项目方向",
        summary="Evolyn 的长期记忆方向",
        content="旧方向：只保留少量长期记忆。",
    )
    main = FakeAdapter(
        _config("main", "main-model"),
        [
            ModelResponse(
                id="read-response",
                provider="main",
                model="main-model",
                message=Message(
                    role=MessageRole.ASSISTANT,
                    tool_calls=(
                        ToolCall(
                            id="read-memory-append",
                            name="memory_read",
                            arguments={"memory_id": record.id},
                        ),
                    ),
                ),
            ),
            _response("已完成补充", provider="main", model="main-model"),
        ],
    )
    reflect = FakeAdapter(
        _config("reflect", "reflection-model"),
        [
            _response(
                '{"action":"append","memory_id":"M001",'
                '"addition":"补充：核心偏好写入 Core，普通决定写入普通记忆。",'
                '"reason":"本轮补充了长期记忆边界"}'
            )
        ],
    )
    registry = _registry(main=main, reflect=reflect)
    tools = ToolRegistry()
    register_memory_tools(tools, manager)
    runtime = AgentRuntime(
        registry,
        tools,
        provider="main",
        memory_manager=manager,
        memory_reflector=PostRunMemoryReflector(
            registry,
            config=_reflection_config(),
        ),
    )

    result = await runtime.run("读取旧决定并补充一条边界规则")

    assert result.ok is True
    updated = await manager.store.load(record.id)
    assert updated is not None
    assert updated.revision == record.revision + 1
    assert updated.title == record.title
    assert updated.summary == record.summary
    assert updated.content == (
        "旧方向：只保留少量长期记忆。\n\n"
        "补充：核心偏好写入 Core，普通决定写入普通记忆。"
    )
    assert updated.last_update_reason == "本轮补充了长期记忆边界"
    assert "项目方向" in (await manager.index.load() or "")


@pytest.mark.asyncio
async def test_runtime_rejects_oversized_reflection_output(
    tmp_path: Path,
) -> None:
    manager = await _manager(tmp_path / "memory")
    main = FakeAdapter(
        _config("main", "main-model"),
        [_response("最终答案", provider="main", model="main-model")],
    )
    oversized = "长" * 3_000
    reflect = FakeAdapter(
        _config("reflect", "reflection-model"),
        [
            _response(
                '{"action":"create","title":"超长记忆","summary":"预算护栏测试",'
                f'"content":"{oversized}",'
                '"reason":"输出预算测试"}'
            )
        ],
    )
    registry = _registry(main=main, reflect=reflect)
    events = InMemoryEventHandler()
    runtime = AgentRuntime(
        registry,
        ToolRegistry(),
        provider="main",
        memory_manager=manager,
        memory_reflector=PostRunMemoryReflector(
            registry,
            config=_reflection_config(max_output_tokens=100),
        ),
    )

    result = await runtime.run("正常任务", event_handler=events)

    assert result.ok is True
    assert await manager.list() == ()
    failed = next(
        event
        for event in events.events
        if event.type is AgentEventType.MEMORY_REFLECTION_FAILED
    )
    assert failed.reflection_error is not None
    assert "exceeds configured budget" in failed.reflection_error


# ----------------------------------------------------------------------
# CREATE 近重复探测：强重复自动合并，中相似记录 suspect
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_create_merges_near_duplicate_via_embedding(
    tmp_path: Path,
) -> None:
    manager = MemoryManager(tmp_path / "memory", embedding=FakeEmbeddingAdapter())
    await manager.initialize()
    original = await manager.create(
        title="数据库迁移决定",
        summary="生产库从 MySQL 迁移到 PostgreSQL 的最终决定",
        content="2024-12 定稿：生产环境数据库从 MySQL 整体迁移到 PostgreSQL 16。",
    )
    await _wait_for_embeddings(manager)

    main = FakeAdapter(
        _config("main", "main-model"),
        [_response("最终答案", provider="main", model="main-model")],
    )
    reflect = FakeAdapter(
        _config("reflect", "reflection-model"),
        [
            _decision_response(
                {
                    "action": "create",
                    "title": "迁移决定补充",
                    "summary": "生产库迁移决定的最新补充",
                    "content": (
                        "2024-12 定稿：生产环境数据库从 MySQL 整体迁移到 "
                        "PostgreSQL 16，迁移脚本放在 deploy/pg-migration。"
                    ),
                    "reason": "用户确认了迁移脚本位置",
                }
            )
        ],
    )
    registry = _registry(main=main, reflect=reflect)
    events = InMemoryEventHandler()
    runtime = AgentRuntime(
        registry,
        ToolRegistry(),
        provider="main",
        memory_manager=manager,
        memory_reflector=PostRunMemoryReflector(
            registry,
            # FakeEmbedding 下这对近重复约 0.78，高于 0.72 的默认合并阈值。
            config=_reflection_config(),
        ),
    )

    result = await runtime.run("确认数据库迁移决定", event_handler=events)

    assert result.ok is True
    active = await manager.list()
    assert [record.id for record in active] == [original.id]
    merged = await manager.store.load(original.id)
    assert merged is not None
    assert merged.revision == 2
    assert "迁移脚本放在 deploy/pg-migration" in merged.content
    completed = next(
        event
        for event in events.events
        if event.type is AgentEventType.MEMORY_REFLECTION_COMPLETED
    )
    assert completed.reflection_dedup_outcome == "merged"
    assert completed.reflection_dedup_memory_id == original.id
    assert completed.reflection_dedup_similarity is not None


@pytest.mark.asyncio
async def test_runtime_create_title_match_merges_without_new_memory(
    tmp_path: Path,
) -> None:
    manager = MemoryManager(tmp_path / "memory", embedding=FakeEmbeddingAdapter())
    await manager.initialize()
    original = await manager.create(
        title="数据库迁移决定",
        summary="生产库从 MySQL 迁移到 PostgreSQL",
        content="2024-12 定稿：生产环境数据库迁移到 PostgreSQL 16。",
    )
    await _wait_for_embeddings(manager)

    main = FakeAdapter(
        _config("main", "main-model"),
        [_response("最终答案", provider="main", model="main-model")],
    )
    reflect = FakeAdapter(
        _config("reflect", "reflection-model"),
        [
            _decision_response(
                {
                    "action": "create",
                    "title": " 数据库迁移决定 ",
                    "summary": "迁移脚本位置补充",
                    "content": "补充：迁移脚本放在 deploy/pg-migration。",
                    "reason": "标题与既有记忆一致",
                }
            )
        ],
    )
    registry = _registry(main=main, reflect=reflect)
    events = InMemoryEventHandler()
    runtime = AgentRuntime(
        registry,
        ToolRegistry(),
        provider="main",
        memory_manager=manager,
        memory_reflector=PostRunMemoryReflector(
            registry,
            config=_reflection_config(),
        ),
    )

    result = await runtime.run("补充迁移脚本文档", event_handler=events)

    assert result.ok is True
    active = await manager.list()
    assert [record.id for record in active] == [original.id]
    merged = await manager.store.load(original.id)
    assert merged is not None
    assert "迁移脚本放在 deploy/pg-migration" in merged.content
    completed = next(
        event
        for event in events.events
        if event.type is AgentEventType.MEMORY_REFLECTION_COMPLETED
    )
    assert completed.reflection_dedup_outcome == "title_match"
    assert completed.reflection_dedup_memory_id == original.id
    assert completed.reflection_dedup_similarity is None


@pytest.mark.asyncio
async def test_runtime_create_records_suspect_duplicate_but_still_creates(
    tmp_path: Path,
) -> None:
    manager = MemoryManager(tmp_path / "memory", embedding=FakeEmbeddingAdapter())
    await manager.initialize()
    original = await manager.create(
        title="部署流程",
        summary="发布部署的既定流程",
        content="先跑测试，再构建镜像，最后滚动发布。",
    )
    await _wait_for_embeddings(manager)

    main = FakeAdapter(
        _config("main", "main-model"),
        [_response("最终答案", provider="main", model="main-model")],
    )
    reflect = FakeAdapter(
        _config("reflect", "reflection-model"),
        [
            _decision_response(
                {
                    "action": "create",
                    "title": "部署与发布补充",
                    "summary": "发布流程的补充规则",
                    "content": "发布流程需要灰度观察，部署后再全量。",
                    "reason": "同主题的新增规则",
                }
            )
        ],
    )
    registry = _registry(main=main, reflect=reflect)
    events = InMemoryEventHandler()
    runtime = AgentRuntime(
        registry,
        ToolRegistry(),
        provider="main",
        memory_manager=manager,
        memory_reflector=PostRunMemoryReflector(
            registry,
            config=_reflection_config(
                dedup_threshold=0.99,
                dedup_suspect_threshold=0.0,
            ),
        ),
    )

    result = await runtime.run("补充发布流程规则", event_handler=events)

    assert result.ok is True
    active = await manager.list()
    assert len(active) == 2
    assert original.id in [record.id for record in active]
    completed = next(
        event
        for event in events.events
        if event.type is AgentEventType.MEMORY_REFLECTION_COMPLETED
    )
    assert completed.reflection_dedup_outcome == "suspect"
    assert completed.reflection_dedup_memory_id == original.id
    assert completed.reflection_dedup_similarity is not None


@pytest.mark.asyncio
async def test_runtime_create_dedup_disabled_creates_duplicate(
    tmp_path: Path,
) -> None:
    manager = MemoryManager(tmp_path / "memory", embedding=FakeEmbeddingAdapter())
    await manager.initialize()
    original = await manager.create(
        title="数据库迁移决定",
        summary="生产库从 MySQL 迁移到 PostgreSQL",
        content="2024-12 定稿：生产环境数据库迁移到 PostgreSQL 16。",
    )
    await _wait_for_embeddings(manager)

    main = FakeAdapter(
        _config("main", "main-model"),
        [_response("最终答案", provider="main", model="main-model")],
    )
    reflect = FakeAdapter(
        _config("reflect", "reflection-model"),
        [
            _decision_response(
                {
                    "action": "create",
                    "title": "数据库迁移决定",
                    "summary": "生产库从 MySQL 迁移到 PostgreSQL",
                    "content": "2024-12 定稿：生产环境数据库迁移到 PostgreSQL 16。",
                    "reason": "dedup 关闭时保持原行为",
                }
            )
        ],
    )
    registry = _registry(main=main, reflect=reflect)
    events = InMemoryEventHandler()
    runtime = AgentRuntime(
        registry,
        ToolRegistry(),
        provider="main",
        memory_manager=manager,
        memory_reflector=PostRunMemoryReflector(
            registry,
            config=_reflection_config(dedup_enabled=False),
        ),
    )

    result = await runtime.run("再次确认迁移决定", event_handler=events)

    assert result.ok is True
    active = await manager.list()
    assert len(active) == 2
    assert original.id in [record.id for record in active]
    completed = next(
        event
        for event in events.events
        if event.type is AgentEventType.MEMORY_REFLECTION_COMPLETED
    )
    assert completed.reflection_dedup_outcome is None


@pytest.mark.asyncio
async def test_runtime_create_merge_failure_falls_back_to_create(
    tmp_path: Path,
) -> None:
    manager = await _manager(tmp_path / "memory")
    original = await manager.create(
        title="超长记忆",
        summary="正文接近上限",
        content="甲" * 11_000,
    )

    main = FakeAdapter(
        _config("main", "main-model"),
        [_response("最终答案", provider="main", model="main-model")],
    )
    reflect = FakeAdapter(
        _config("reflect", "reflection-model"),
        [
            _decision_response(
                {
                    "action": "create",
                    "title": "超长记忆",
                    "summary": "追加内容会超过正文上限",
                    "content": "乙" * 1_200,
                    "reason": "合并注定超限，应降级为新建",
                }
            )
        ],
    )
    registry = _registry(main=main, reflect=reflect)
    events = InMemoryEventHandler()
    runtime = AgentRuntime(
        registry,
        ToolRegistry(),
        provider="main",
        memory_manager=manager,
        memory_reflector=PostRunMemoryReflector(
            registry,
            # 1_200 个 CJK 字符约 3_000 token，放宽输出预算以让它进入合并路径。
            config=_reflection_config(max_output_tokens=6_000),
        ),
    )

    result = await runtime.run("补充超长记忆", event_handler=events)

    assert result.ok is True
    active = await manager.list()
    assert len(active) == 2
    assert original.id in [record.id for record in active]
    completed = next(
        event
        for event in events.events
        if event.type is AgentEventType.MEMORY_REFLECTION_COMPLETED
    )
    assert completed.reflection_dedup_outcome == "merge_failed"
    assert completed.reflection_dedup_memory_id == original.id


@pytest.mark.asyncio
async def test_runtime_rejects_reflection_update_after_concurrent_change(
    tmp_path: Path,
) -> None:
    manager = await _manager(tmp_path / "memory")
    record = await manager.create(
        title="项目方向",
        summary="旧方向",
        content="初始正文",
    )

    async def concurrent_update() -> None:
        await manager.update(
            record.id,
            title="并发更新标题",
            summary="并发更新 cue",
            content="另一个 Run 已经更新",
            reason="并发测试",
        )

    main = FakeAdapter(
        _config("main", "main-model"),
        [
            ModelResponse(
                id="read-response",
                provider="main",
                model="main-model",
                message=Message(
                    role=MessageRole.ASSISTANT,
                    tool_calls=(
                        ToolCall(
                            id="read-memory-1",
                            name="memory_read",
                            arguments={"memory_id": record.id},
                        ),
                    ),
                ),
            ),
            _response("主任务完成", provider="main", model="main-model"),
        ],
    )
    reflect = FakeAdapter(
        _config("reflect", "reflection-model"),
        [
            _response(
                '{"action":"update","memory_id":"M001",'
                '"title":"过期标题","summary":"过期 cue",'
                '"content":"基于旧 revision 的正文",'
                '"reason":"模拟过期更新"}'
            )
        ],
        before_return=concurrent_update,
    )
    registry = _registry(main=main, reflect=reflect)
    tools = ToolRegistry()
    register_memory_tools(tools, manager)
    events = InMemoryEventHandler()
    runtime = AgentRuntime(
        registry,
        tools,
        provider="main",
        memory_manager=manager,
        memory_reflector=PostRunMemoryReflector(
            registry,
            config=_reflection_config(),
        ),
    )

    result = await runtime.run("读取并更新已有方向", event_handler=events)

    assert result.ok is True
    current = await manager.store.load(record.id)
    assert current is not None
    assert current.content == "另一个 Run 已经更新"
    failed = next(
        event
        for event in events.events
        if event.type is AgentEventType.MEMORY_REFLECTION_FAILED
    )
    assert failed.reflection_error is not None
    assert "revision conflict" in failed.reflection_error


@pytest.mark.asyncio
async def test_reflection_provider_only_uses_that_adapters_default_model(
) -> None:
    reflect = FakeAdapter(
        _config("reflect", "provider-default-model"),
        [_response('{"action":"none","reason":"没有新记忆"}')],
    )
    registry = _registry(reflect=reflect)
    reflector = PostRunMemoryReflector(
        registry,
        config=MemoryReflectionConfig(
            _env_file=None,
            provider="reflect",
            model=None,
        ),
        default_provider="main",
        default_model="main-model-must-not-leak",
    )

    proposal = await reflector.decide(_input())

    assert proposal.error is None
    assert proposal.model == "provider-default-model"
    assert reflect.requests[0].model == "provider-default-model"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [RuntimeError("provider failed"), ValueError("bad")],
)
async def test_runtime_reflection_failure_does_not_change_success_result(
    tmp_path: Path,
    failure: Exception,
) -> None:
    manager = await _manager(tmp_path / "memory")
    main = FakeAdapter(
        _config("main", "main-model"),
        [_response("最终答案", provider="main", model="main-model")],
    )
    reflection_response: ModelResponse | Exception
    if isinstance(failure, ValueError):
        reflection_response = _response("invalid-json")
    else:
        reflection_response = failure
    reflect = FakeAdapter(
        _config("reflect", "reflection-model"),
        [reflection_response],
    )
    registry = _registry(main=main, reflect=reflect)
    events = InMemoryEventHandler()
    runtime = AgentRuntime(
        registry,
        ToolRegistry(),
        provider="main",
        memory_manager=manager,
        memory_reflector=PostRunMemoryReflector(
            registry,
            config=_reflection_config(),
        ),
    )

    result = await runtime.run("正常任务", event_handler=events)

    assert result.ok is True
    assert result.content == "最终答案"
    # reflection 失败不改变 Run 结果；agent_completed 先于 post-run 失败事件。
    event_types = [event.type for event in events.events]
    assert event_types.index(AgentEventType.AGENT_COMPLETED) < event_types.index(
        AgentEventType.MEMORY_REFLECTION_FAILED
    )
    failed = next(
        event
        for event in events.events
        if event.type is AgentEventType.MEMORY_REFLECTION_FAILED
    )
    assert failed.reflection_error is not None


@pytest.mark.asyncio
async def test_runtime_model_error_skips_reflection(tmp_path: Path) -> None:
    manager = await _manager(tmp_path / "memory")
    main = FakeAdapter(
        _config("main", "main-model"),
        [RuntimeError("main failed")],
    )
    reflect = FakeAdapter(
        _config("reflect", "reflection-model"),
        [_response('{"action":"none","reason":"unused"}')],
    )
    registry = _registry(main=main, reflect=reflect)
    events = InMemoryEventHandler()
    runtime = AgentRuntime(
        registry,
        ToolRegistry(),
        provider="main",
        memory_manager=manager,
        memory_reflector=PostRunMemoryReflector(
            registry,
            config=_reflection_config(),
        ),
    )

    result = await runtime.run("会失败", event_handler=events)

    assert result.stop_reason is AgentStopReason.MODEL_ERROR
    assert reflect.requests == []
    assert events.events[-2].type is AgentEventType.AGENT_FAILED
    assert events.events[-1].type is AgentEventType.MEMORY_REFLECTION_SKIPPED


@pytest.mark.asyncio
async def test_runtime_max_steps_skips_reflection(tmp_path: Path) -> None:
    manager = await _manager(tmp_path / "memory")
    main = FakeAdapter(
        _config("main", "main-model"),
        [
            ModelResponse(
                id="tool-response",
                provider="main",
                model="main-model",
                message=Message(
                    role=MessageRole.ASSISTANT,
                    tool_calls=(ToolCall(id="missing-1", name="missing"),),
                ),
            )
        ],
    )
    reflect = FakeAdapter(
        _config("reflect", "reflection-model"),
        [_response('{"action":"none","reason":"unused"}')],
    )
    registry = _registry(main=main, reflect=reflect)
    events = InMemoryEventHandler()
    runtime = AgentRuntime(
        registry,
        ToolRegistry(),
        provider="main",
        max_steps=1,
        memory_manager=manager,
        memory_reflector=PostRunMemoryReflector(
            registry,
            config=_reflection_config(),
        ),
    )

    result = await runtime.run("需要更多步骤", event_handler=events)

    assert result.stop_reason is AgentStopReason.MAX_STEPS
    assert reflect.requests == []
    assert events.events[-2].type is AgentEventType.AGENT_FAILED
    assert events.events[-1].type is AgentEventType.MEMORY_REFLECTION_SKIPPED


@pytest.mark.asyncio
async def test_classification_regression_samples(tmp_path: Path) -> None:
    """Core / Ordinary 分类回归样本：提示词对两类典型样本给出正确边界。"""

    core_samples = (
        "用户明确说：以后所有回复都用简体中文。",
        "我住在北京，长期偏好夜间工作。",
    )
    reflector = PostRunMemoryReflector(
        _registry(
            reflect=FakeAdapter(
                _config("reflect", "reflection-model"),
                [
                    _response(
                        '{"action":"none","reason":"core-worthy sample"}'
                    ),
                    _response(
                        '{"action":"none","reason":"core-worthy sample"}'
                    ),
                ],
            )
        ),
        config=_reflection_config(),
    )

    for sample in core_samples:
        proposal = await reflector.decide(
            MemoryReflectionInput(
                run_id="run-1",
                conversation_id="conversation-1",
                user_input=sample,
                final_answer="已记录。",
                recalled_memory_ids=(),
            )
        )
        assert proposal.error is None
        assert proposal.decision is not None

    # 提示词包含两类样本的分类边界（Core 归 Core 工具，普通记忆拒绝兜底）。
    from app.memory.reflection import _REFLECTION_PROMPT

    assert "Core-worthy and must return NONE" in _REFLECTION_PROMPT
    assert "Never CREATE or UPDATE ordinary memory as a fallback" in _REFLECTION_PROMPT
    assert "Ordinary memory is for important" in _REFLECTION_PROMPT
    # Ordinary 样本属于 Post-Run Reflection 的职责范围（非 Task / Skill）。
    assert "current task progress" in _REFLECTION_PROMPT
    assert "reusable procedures" in _REFLECTION_PROMPT
