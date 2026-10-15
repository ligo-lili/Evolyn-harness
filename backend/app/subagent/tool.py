"""主循环可调用的委派工具：``subagent_run``。"""

from __future__ import annotations

from typing import Any

from app.agent.event_stream import EventEmitter
from app.models.types import AgentMode, ToolDefinition, ToolPermission
from app.tools.base import BaseTool
from app.tools.hooks import ToolExecutionContext

from .runner import SubAgentRunner

SUBAGENT_TOOL_NAME = "subagent_run"
_MEMORY_LEVELS = ("none", "core", "full")


class SubAgentTool(BaseTool):
    """把一次独立调查委派给子代理（隔离上下文 + 显式授予能力）。"""

    def __init__(self, runner: SubAgentRunner) -> None:
        self._runner = runner

    @property
    def definition(self) -> ToolDefinition:
        settings = self._runner.settings
        return ToolDefinition(
            name=SUBAGENT_TOOL_NAME,
            description=(
                "把一次独立的调查任务委派给子代理：子代理在隔离上下文中运行"
                "（看不到本对话），只持有 grants 显式授予的只读能力，完成后"
                "返回结构化结果（结论、关键文件与行号、短代码片段、不确定点、"
                "未覆盖范围）。task 必须自包含：背景、要回答的问题、期望的"
                "证据形态。结果 truncated=true 时，依据 uncovered_scope 决定"
                "是重新委派（缩小范围）还是自行接手。适合：多文件 / 多来源的"
                "资料调查、批量阅读与核实；不适合：写入、执行命令或任何有"
                "副作用的工作（V1 能力目录只有只读能力）。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": (
                            "自包含的任务说明：背景、要回答的具体问题、期望"
                            "的证据形态（子代理看不到本对话）。"
                        ),
                    },
                    "grants": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "显式授予的只读能力清单：read_repo[:工作区路径"
                            "前缀]、web_search、web_fetch:域名[,域名]。"
                            "能力只收窄不放开，PLAN 模式下 web_fetch 会被"
                            "自动丢弃。"
                        ),
                    },
                    "memory": {
                        "type": "string",
                        "enum": list(_MEMORY_LEVELS),
                        "default": "core",
                        "description": (
                            "注入给子代理的运行时记忆档位：none / core（默认）"
                            " / full。"
                        ),
                    },
                },
                "required": ["task", "grants"],
                "additionalProperties": False,
            },
            strict=False,
            permission=ToolPermission.ALLOWED,
            # 外层执行器兜底超时：子循环自身按墙钟收口，外层再留 30s 缓冲。
            timeout_seconds=settings.wall_clock_seconds + 30.0,
        )

    async def execute(self, arguments: dict[str, Any]) -> Any:
        # subagent_run 需要运行上下文（run_id / emitter / memory 快照）。
        raise ValueError("subagent_run requires run context")

    async def execute_with_context(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        run_id = (context.run_id or "").strip()
        if not run_id:
            raise ValueError("subagent_run 必须在 Agent Run 上下文中执行")

        task = arguments.get("task")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("'task' 必须是非空字符串（自包含的任务说明）")

        grants = arguments.get("grants")
        if not isinstance(grants, list) or not grants:
            raise ValueError(
                "'grants' 必须是非空字符串数组（显式授予的能力清单）"
            )

        memory = arguments.get("memory", "core")
        if memory not in _MEMORY_LEVELS:
            raise ValueError("'memory' 必须是 none / core / full 之一")

        raw_emitter = context.metadata.get("agent_emitter")
        raw_messages = context.metadata.get("memory_messages") or ()
        raw_scope = context.metadata.get("skill_tool_scope")
        skill_tool_scope = raw_scope if isinstance(raw_scope, frozenset) else None

        outcome = await self._runner.run(
            parent_run_id=run_id,
            conversation_id=context.conversation_id,
            step=context.step,
            task=task.strip(),
            grants=list(grants),
            memory=memory,
            mode=context.mode or AgentMode.NORMAL,
            memory_messages=tuple(raw_messages),
            skill_tool_scope=skill_tool_scope,
            emitter=raw_emitter if isinstance(raw_emitter, EventEmitter) else None,
        )
        return outcome.as_payload(
            max_result_chars=self._runner.settings.max_result_chars
        )


__all__ = ["SUBAGENT_TOOL_NAME", "SubAgentTool"]
