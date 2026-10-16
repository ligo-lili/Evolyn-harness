"""子代理的结构化结果交付工具（只存在于子循环）。

``submit_result`` 是子循环的终局工具：成功提交立即结束循环（不再多耗一次
模型调用），提交内容由 runner 结合发现账本组装为有界 JSON 返回主循环。
格式无效时最多重试 2 次（重试不扩大权限、不重置预算）；重试耗尽后以“被拒
收”的终局结果结束循环，由 runner 按已入账的发现返回 partial / failed。
"""

from __future__ import annotations

from typing import Any

from app.models.types import ToolDefinition, ToolPermission
from app.tools.base import BaseTool

SUBMIT_RESULT_TOOL_NAME = "submit_result"

MAX_SUMMARY_CHARS = 600
_MAX_NOT_COVERED_ITEMS = 20
_MAX_NOT_COVERED_CHARS = 500
MAX_INVALID_ATTEMPTS = 2


class SubmitResultTool(BaseTool):
    """校验并归一化子代理的交付内容；成功提交即终止子循环。"""

    def __init__(self) -> None:
        self._invalid_attempts = 0

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=SUBMIT_RESULT_TOOL_NAME,
            description=(
                "提交本次调查的总结并立即结束。字段：summary（结论性总结，"
                f"最多 {MAX_SUMMARY_CHARS} 字符，必填）、not_covered（未覆盖"
                "范围 / 仍未回答的问题；预算收口时必须如实填写）。逐条发现"
                "已通过 record_finding 入账，这里只写总结，不要重复罗列细节；"
                "提交成功即结束，不要再调用其他工具；没有把握的内容写入 "
                "not_covered，不得虚构结论。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": (
                            "结论性总结：直接回答委派任务的核心发现"
                            f"（≤{MAX_SUMMARY_CHARS} 字符）。"
                        ),
                    },
                    "not_covered": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "未覆盖范围 / 未回答的问题；最多 "
                            f"{_MAX_NOT_COVERED_ITEMS} 条、每条 ≤"
                            f"{_MAX_NOT_COVERED_CHARS} 字符。"
                        ),
                    },
                },
                "required": ["summary"],
                "additionalProperties": False,
            },
            strict=False,
            permission=ToolPermission.ALLOWED,
            closing_allowed=True,
            record_output=False,
        )

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return _validate(arguments)
        except ValueError as exc:
            self._invalid_attempts += 1
            if self._invalid_attempts <= MAX_INVALID_ATTEMPTS:
                raise
            # 格式重试耗尽：收敛为“被拒收”的终局结果（缺少 summary 字段，
            # runner 会按已入账发现返回 partial / failed，不再消耗预算）。
            return {
                "accepted": False,
                "error": str(exc),
                "attempts": self._invalid_attempts,
            }


def _validate(arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("submit_result 参数必须是对象")
    summary = arguments.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("'summary' 必须是非空字符串（必填）")
    summary = summary.strip()
    if len(summary) > MAX_SUMMARY_CHARS:
        raise ValueError(f"'summary' 最多 {MAX_SUMMARY_CHARS} 字符")
    return {
        "summary": summary,
        "not_covered": _string_list(
            arguments.get("not_covered", []),
            field="not_covered",
            item_max_chars=_MAX_NOT_COVERED_CHARS,
            max_items=_MAX_NOT_COVERED_ITEMS,
        ),
    }


def _string_list(
    value: Any,
    *,
    field: str,
    item_max_chars: int,
    max_items: int,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"'{field}' must be a list of strings")
    if len(value) > max_items:
        raise ValueError(f"'{field}' accepts at most {max_items} items")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"'{field}' items must be non-empty strings")
        text = item.strip()
        if len(text) > item_max_chars:
            raise ValueError(
                f"'{field}' items must be at most {item_max_chars} characters"
            )
        normalized.append(text)
    return normalized


__all__ = [
    "MAX_INVALID_ATTEMPTS",
    "MAX_SUMMARY_CHARS",
    "SUBMIT_RESULT_TOOL_NAME",
    "SubmitResultTool",
]
