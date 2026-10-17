"""Sub-Agent 离线测试的共享假件与装配。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from app.agent.events import InMemoryEventHandler
from app.agent.runtime import AgentRuntime
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
    ToolDefinition,
    ToolPermission,
)
from app.subagent import (
    SUBAGENT_TOOL_NAME,
    SubAgentRunner,
    SubagentSettings,
    SubAgentTool,
    Ticket,
    parse_ticket,
)
from app.tools.base import BaseTool
from app.tools.builtin.list_files import ListFilesTool
from app.tools.builtin.read_file import ReadFileTool
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry

SUBAGENT_PROMPT_MARKER = "你是 Evolyn 的子代理"
REVIEW_PROMPT_MARKER = "你是独立复核员"


class FakeModelAdapter(ModelAdapter):
    """按脚本顺序返回响应；同一实例同时服务父循环与子循环。"""

    def __init__(
        self,
        config: ProviderConfig,
        responses: list[ModelResponse | Exception],
        *,
        delay: float = 0.0,
    ) -> None:
        super().__init__(config)
        self.responses = responses
        self.requests: list[ModelRequest] = []
        self.delay = delay
        self.active = 0
        self.peak_active = 0
        # 1 基请求序号；命中后永久阻塞，用于取消传播测试。
        self.block_on_request: int | None = None
        self.blocked = False
        self.cancelled = False

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        index = len(self.requests)
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        try:
            if self.block_on_request == index:
                self.blocked = True
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
            if self.delay:
                await asyncio.sleep(self.delay)
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        finally:
            self.active -= 1

    async def close(self) -> None:
        pass


class FakeSearchTool(BaseTool):
    """离线 web_search：不触网、可计数。"""

    definition = ToolDefinition(
        name="web_search",
        description="Offline fake web search",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        strict=False,
    )

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        query = str(arguments.get("query", ""))
        return {
            "query": query,
            "results": [
                {
                    "title": "Fake Result",
                    "url": "https://docs.python.org/3/library/asyncio.html",
                    "snippet": "asyncio docs",
                }
            ],
        }


class FakeHttpTool(BaseTool):
    """离线 http_request：记录收到的参数，供 web_fetch 包装断言。"""

    definition = ToolDefinition(
        name="http_request",
        description="Offline fake HTTP request (requires approval normally)",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "HEAD"],
                    "default": "GET",
                },
                "headers": {"type": "object"},
                "body": {"type": "string"},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        strict=False,
        permission=ToolPermission.HUMAN_APPROVAL,
    )

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(deepcopy(arguments))
        return {
            "url": arguments.get("url"),
            "status": 200,
            "body": "fake-body",
        }


def model_response(
    *,
    content: str | None = None,
    tool_calls: tuple[ToolCall, ...] = (),
    usage: ModelUsage | None = None,
) -> ModelResponse:
    return ModelResponse(
        id="fake-response",
        provider="fake",
        model="fake-model",
        message=Message(
            role=MessageRole.ASSISTANT,
            content=content,
            tool_calls=tool_calls,
        ),
        usage=usage or ModelUsage(),
    )


def tool_call(name: str, arguments: dict[str, Any], *, call_id: str) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def usage(tokens: int, *, model_calls: int = 1) -> ModelUsage:
    return ModelUsage(
        input_tokens=tokens,
        output_tokens=0,
        total_tokens=tokens,
        model_calls=model_calls,
    )


def ticket_args(**overrides: object) -> dict[str, Any]:
    """默认任务单参数：scope 为仓库根（与夹具的 allow_repo_root=True 配套）。"""

    payload: dict[str, Any] = {
        "goal": "调查 notes.txt 并给出结论",
        "questions": ["notes.txt 的内容是什么"],
        "scope_paths": ["."],
        "done_when": "给出结论与文件定位",
    }
    payload.update(overrides)
    return payload


def make_ticket(**overrides: object) -> Ticket:
    """构造已解析的任务单（允许仓库根，便于夹具）。"""

    return parse_ticket(ticket_args(**overrides), allow_repo_root=True)


@dataclass(slots=True)
class Harness:
    workspace: Path
    registry: ModelAdapterRegistry
    adapter: FakeModelAdapter
    tool_registry: ToolRegistry
    tool_executor: ToolExecutor
    runner: SubAgentRunner
    runtime: AgentRuntime
    events: InMemoryEventHandler
    search_tool: FakeSearchTool
    http_tool: FakeHttpTool
    finalizer_runs: list[str]

    def sub_requests(self) -> list[ModelRequest]:
        """子循环请求：系统提示词为子代理专用。"""

        result: list[ModelRequest] = []
        for request in self.adapter.requests:
            for message in request.messages[:2]:
                if (
                    message.role is MessageRole.SYSTEM
                    and message.content
                    and SUBAGENT_PROMPT_MARKER in message.content
                ):
                    result.append(request)
                    break
        return result

    def review_requests(self) -> list[ModelRequest]:
        """behavior 独立复核请求：系统提示词为复核专用。"""

        return [
            request
            for request in self.adapter.requests
            if any(
                message.role is MessageRole.SYSTEM
                and message.content
                and REVIEW_PROMPT_MARKER in message.content
                for message in request.messages[:2]
            )
        ]

    def parent_requests(self) -> list[ModelRequest]:
        excluded = {
            id(request) for request in self.sub_requests()
        } | {id(request) for request in self.review_requests()}
        return [
            request
            for request in self.adapter.requests
            if id(request) not in excluded
        ]


def build_harness(
    tmp_path: Path,
    responses: list[ModelResponse | Exception],
    *,
    workspace_files: dict[str, str] | None = None,
    settings: SubagentSettings | None = None,
    extra_tools: tuple[BaseTool, ...] = (),
    memory_manager: Any = None,
    run_budget_config: Any = None,
    max_steps: int = 8,
    max_tool_rounds: int = 8,
    delay: float = 0.0,
    trace_store: Any = None,
    output_recorder: Any = None,
    run_finalizers: tuple[Any, ...] = (),
    parallel_tool_names: frozenset[str] = frozenset({SUBAGENT_TOOL_NAME}),
) -> Harness:
    """装配 父 Runtime + SubAgentRunner（共享同一个工具执行器）。"""

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    for name, content in (workspace_files or {}).items():
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    config = ProviderConfig(
        provider="fake",
        model="fake-model",
        api_key=SecretStr("offline-test-key"),
        api_style=ApiStyle.CHAT_COMPLETIONS,
    )
    adapter = FakeModelAdapter(config, list(responses), delay=delay)
    registry = ModelAdapterRegistry(ModelSettings(_env_file=None))
    registry.register("fake", lambda _: adapter, config=config)

    tool_registry = ToolRegistry()
    tool_registry.register(ReadFileTool(workspace))
    tool_registry.register(ListFilesTool(workspace))
    search_tool = FakeSearchTool()
    http_tool = FakeHttpTool()
    tool_registry.register(search_tool)
    tool_registry.register(http_tool)
    for tool in extra_tools:
        tool_registry.register(tool)

    tool_executor = ToolExecutor(
        tool_registry,
        output_recorder=output_recorder,
    )
    # 离线夹具大多直接在仓库根放文件：夹具默认允许 scope_paths=["."]。
    # 产品默认（拒绝仓库根）由 test_ticket.py 单独覆盖。
    if settings is None:
        settings = SubagentSettings(_env_file=None, allow_repo_root=True)
    runner = SubAgentRunner(
        registry,
        tool_registry,
        tool_executor=tool_executor,
        provider="fake",
        model="fake-model",
        trace_store=trace_store,
        workspace_root=workspace,
        settings=settings or SubagentSettings(_env_file=None),
    )
    tool_registry.register(SubAgentTool(runner))

    finalizer_runs: list[str] = []

    async def spy_finalizer(run_id: str) -> None:
        finalizer_runs.append(run_id)

    runtime = AgentRuntime(
        registry,
        tool_registry,
        provider="fake",
        model="fake-model",
        tool_executor=tool_executor,
        max_steps=max_steps,
        max_tool_rounds=max_tool_rounds,
        memory_manager=memory_manager,
        run_budget_config=run_budget_config,
        parallel_tool_names=parallel_tool_names,
        run_finalizers=(runner.release_run, spy_finalizer, *run_finalizers),
    )

    return Harness(
        workspace=workspace,
        registry=registry,
        adapter=adapter,
        tool_registry=tool_registry,
        tool_executor=tool_executor,
        runner=runner,
        runtime=runtime,
        events=InMemoryEventHandler(),
        search_tool=search_tool,
        http_tool=http_tool,
        finalizer_runs=finalizer_runs,
    )
