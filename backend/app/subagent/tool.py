"""主循环可调用的委派工具：``subagent_run``。"""

from __future__ import annotations

from typing import Any

from app.agent.event_stream import EventEmitter
from app.models.types import AgentMode, ToolDefinition, ToolPermission
from app.tools.base import BaseTool
from app.tools.hooks import ToolExecutionContext

from .runner import SubAgentRunner
from .ticket import DEFAULT_TIER, EVIDENCE_TYPES, TIERS, parse_ticket

SUBAGENT_TOOL_NAME = "subagent_run"
_MEMORY_LEVELS = ("none", "core", "full")

_TICKET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {
            "type": "string",
            "description": "一句话目标（≤300 字符）。",
        },
        "questions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "需要回答的具体问题（1-12 条，每条 ≤500 字符）。",
        },
        "scope_paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "允许访问的工作区相对路径（1-16 条），执行层白名单：越界"
                "调用会被拒绝。不能是整个仓库根（需要整体扫描时列出具体"
                "目录）。"
            ),
        },
        "known_facts": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "主代理已知的事实（可选，≤20 条）：子代理据此避免重复调查，"
                "但仍需自行核实。"
            ),
        },
        "done_when": {
            "type": "string",
            "description": (
                "可判定的完成条件（≤300 字符），例如：每个问题都有结论与"
                "文件定位。"
            ),
        },
        "evidence_types": {
            "type": "array",
            "items": {"type": "string", "enum": list(EVIDENCE_TYPES)},
            "description": (
                "期望的证据类型（可选，缺省不限）：exists（存在/定义）、"
                "calls（调用关系）、reads_config（配置读取）、absent"
                "（不存在/未使用）、behavior（行为）。"
            ),
        },
        "tier": {
            "type": "string",
            "enum": list(TIERS),
            "default": DEFAULT_TIER,
            "description": "预算档位：quick / medium（默认）/ thorough。",
        },
        "continuation_of": {
            "type": "string",
            "description": (
                "可选：上一次 partial 委派的 subagent_id（续跑时填写），"
                "只补充探索未覆盖范围。"
            ),
        },
    },
    "required": ["goal", "questions", "scope_paths", "done_when"],
    "additionalProperties": False,
}


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
                "把一次独立的只读调查委派给子代理：子代理在隔离上下文中运行"
                "（看不到本对话），只持有 grants 显式授予的只读能力；调查中"
                "逐条记录可验证发现，最后返回有界结果：已验证的发现清单"
                "（含 evidence_id，可用 evidence_read 展开原文）、summary、"
                "not_covered、status（complete/partial/failed）与失败原因。"
                "ticket 必须自包含：goal（一句话目标）、questions（要回答的"
                "问题）、scope_paths（允许访问的路径范围，同时是执行层白名单"
                "——与 read_repo 前缀取交，越界调用会被拒绝）、done_when"
                "（可判定的完成条件）；建议同时给出 known_facts（已知事实）与"
                "evidence_types（期望的证据类型）。适合：预计读取超过约 8 个"
                "文件、跨模块调用链、可并行检索的资料调查；不适合：少量文件的"
                "直接查看、写入、执行命令或任何有副作用的工作（能力目录只有"
                "只读能力）。status 不是 complete、或 not_covered 非空时，"
                "据其决定是否继续委派（缩小范围）或自行接手；续跑时把"
                "continuation 里的 not_covered 与已验证发现填进新 ticket 的"
                "known_facts、把 subagent_id 填进 continuation_of，只补查"
                "未覆盖范围；关键结论应基于证据核实原文，不要直接采信结论"
                "文字。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "ticket": _TICKET_SCHEMA,
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
                "required": ["ticket", "grants"],
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

        ticket = parse_ticket(
            arguments.get("ticket"),
            allow_repo_root=self._runner.settings.allow_repo_root,
        )

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
            ticket=ticket,
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
