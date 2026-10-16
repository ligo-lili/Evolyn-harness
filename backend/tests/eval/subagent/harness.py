"""A/B 两臂运行器：同一任务、同一夹具仓库、同一模型。

- **A 臂（direct）**：主 Agent 直接探索（不注册委派工具），即基线；
- **B 臂（delegate）**：现行 V1 委派——注册 ``subagent_run``（deferred，
  与 ``app/application.py`` 的装配一致），主 Agent 自行决定是否委派。

两臂共用同一套装配参数（系统提示词、步数上限、工具集、关闭 Run Budget），
唯一差异是 B 臂多一个委派工具；这样比较的是「有无委派能力」而不是装配
差异。本模块只负责运行与采集，不做判定（判定见 ``reporting.py``）。

评测环境刻意收窄：仅保留只读探索工具（read_file / list_files /
get_current_time），移除写入、shell、网络工具，避免评测改变工作区或产生
额外费用。Run Budget 关闭：评测关注自然完成下的成本，而不是预算截断行为。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from app.agent.budget import RunBudgetConfig
from app.agent.events import AgentEvent, InMemoryEventHandler
from app.agent.result import AgentResult
from app.agent.runtime import AgentRuntime
from app.application import DEFAULT_SYSTEM_PROMPT
from app.context import ContextManager, ContextSettings
from app.models.registry import ModelAdapterRegistry
from app.subagent import (
    SUBAGENT_TOOL_NAME,
    SubAgentRunner,
    SubagentSettings,
    SubAgentTool,
)
from app.tools import ToolExecutor, build_builtin_tool_registry
from app.tools.registry import ToolRegistry

from .fixtures import write_fixture_repo
from .tasks import EvalTask

DIRECT_ARM = "direct"
DELEGATE_ARM = "delegate"
ARMS: tuple[str, ...] = (DIRECT_ARM, DELEGATE_ARM)

EXCLUDED_TOOLS: tuple[str, ...] = (
    "write_file",
    "run_shell_command",
    "http_request",
    "web_search",
)


@dataclass(frozen=True, slots=True)
class HarnessConfig:
    """两臂共用的装配参数。"""

    provider: str
    model: str
    max_steps: int = 40
    max_tool_rounds: int = 40
    subagent_settings: SubagentSettings | None = None


@dataclass(slots=True)
class TaskRunOutcome:
    """一次任务运行的原始产物（事件 + 结果），指标由其构建。"""

    task: EvalTask
    arm: str
    workspace: Path
    result: AgentResult | None
    events: tuple[AgentEvent, ...]
    duration_s: float
    error: str | None

    @property
    def ok(self) -> bool:
        return self.error is None and self.result is not None


def build_eval_tool_registry(workspace: Path) -> ToolRegistry:
    """只读探索工具集（与产品装配同源，去掉副作用/网络工具）。"""

    registry = build_builtin_tool_registry(workspace)
    for name in EXCLUDED_TOOLS:
        registry.unregister(name)
    return registry


async def run_task(
    task: EvalTask,
    *,
    arm: str,
    root: Path,
    config: HarnessConfig,
    registry: ModelAdapterRegistry,
) -> TaskRunOutcome:
    """在 ``root/workspace`` 的夹具仓库上运行一个任务的一个臂。

    ``registry`` 由调用方创建并跨任务复用（真实模型跑批时复用连接；
    离线测试注入 Fake Adapter）。运行异常（模型/装配错误）不向上抛，
    记录进 ``TaskRunOutcome.error``，由报告层作为失败计入。
    """

    if arm not in ARMS:
        raise ValueError(f"unknown arm: {arm!r} (expected one of {ARMS})")

    workspace = root / "workspace"
    write_fixture_repo(workspace)
    tool_registry = build_eval_tool_registry(workspace)
    tool_executor = ToolExecutor(tool_registry)

    run_finalizers: tuple = ()
    parallel_tool_names: frozenset[str] = frozenset()
    if arm == DELEGATE_ARM:
        runner = SubAgentRunner(
            registry,
            tool_registry,
            tool_executor=tool_executor,
            provider=config.provider,
            model=config.model,
            workspace_root=workspace,
            settings=config.subagent_settings or SubagentSettings(_env_file=None),
        )
        # 与 app/application.py 一致：deferred 注册，主 Agent 先 tool_search。
        tool_registry.register(SubAgentTool(runner), deferred=True)
        parallel_tool_names = frozenset({SUBAGENT_TOOL_NAME})
        run_finalizers = (runner.release_run,)

    runtime = AgentRuntime(
        registry,
        tool_registry,
        provider=config.provider,
        model=config.model,
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        tool_executor=tool_executor,
        context_manager=ContextManager(
            context_settings=ContextSettings(_env_file=None)
        ),
        max_steps=config.max_steps,
        max_tool_rounds=config.max_tool_rounds,
        run_budget_config=RunBudgetConfig(enabled=False),
        parallel_tool_names=parallel_tool_names,
        run_finalizers=run_finalizers,
    )

    handler = InMemoryEventHandler()
    start = perf_counter()
    result: AgentResult | None = None
    error: str | None = None
    try:
        result = await runtime.run(
            task.user_input,
            history=(),
            conversation_id=f"eval-{task.id}-{arm}",
            event_handler=handler,
        )
    except Exception as exc:  # noqa: BLE001 — 失败记进指标，不中断跑批
        error = f"{type(exc).__name__}: {exc}"
    duration_s = perf_counter() - start

    return TaskRunOutcome(
        task=task,
        arm=arm,
        workspace=workspace,
        result=result,
        events=tuple(handler.events),
        duration_s=duration_s,
        error=error,
    )


__all__ = [
    "ARMS",
    "DELEGATE_ARM",
    "DIRECT_ARM",
    "EXCLUDED_TOOLS",
    "HarnessConfig",
    "TaskRunOutcome",
    "build_eval_tool_registry",
    "run_task",
]
