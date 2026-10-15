"""子代理的结构化结果交付工具（只存在于子循环）。

``submit_result`` 是子循环的终局工具：成功提交立即结束循环（不再多耗一次
模型调用），提交内容由 runner 取最后一次成功调用组装为有界 JSON 返回主
循环。校验失败返回工具错误，模型可修正后重试。
"""

from __future__ import annotations

from typing import Any

from app.models.types import ToolDefinition, ToolPermission
from app.tools.base import BaseTool

SUBMIT_RESULT_TOOL_NAME = "submit_result"

_MAX_KEY_FILES = 25
_MAX_SNIPPETS = 10
_MAX_SNIPPET_CODE_CHARS = 2_000
_MAX_LIST_ITEMS = 20
_MAX_ITEM_CHARS = 500
_MAX_PATH_CHARS = 300


class SubmitResultTool(BaseTool):
    """校验并归一化子代理的交付内容；成功提交即终止子循环。"""

    definition = ToolDefinition(
        name=SUBMIT_RESULT_TOOL_NAME,
        description=(
            "提交本次调查的结构化结果并立即结束。字段：conclusions（结论，"
            "必填）、key_files（关键文件与行号，如 app/agent/loop.py:120）、"
            "snippets（必要的短代码片段）、uncertainties（不确定点）、"
            "uncovered（未覆盖范围；预算收口时必须如实填写）。提交成功即结束，"
            "不要再调用其他工具；没有把握的内容写入 uncertainties/uncovered，"
            "不得虚构结论。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "conclusions": {
                    "type": "string",
                    "description": (
                        "结论：直接回答委派任务的核心发现（必填）。"
                    ),
                },
                "key_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "关键文件与行号（相对路径），如 "
                        "\"app/agent/loop.py:120\"；最多 25 条。"
                    ),
                },
                "snippets": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string"},
                            "lines": {
                                "type": "string",
                                "description": "行号范围，如 \"120-140\"（可选）",
                            },
                            "code": {"type": "string"},
                        },
                        "required": ["file", "code"],
                        "additionalProperties": False,
                    },
                    "description": (
                        "必要的短代码片段；最多 10 条，每条不超过 2000 字符。"
                    ),
                },
                "uncertainties": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "不确定点 / 未验证的推断；最多 20 条。",
                },
                "uncovered": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "未覆盖范围（预算收口时必填）：哪些部分没有查完、"
                        "哪些问题仍未有答案。"
                    ),
                },
            },
            "required": ["conclusions"],
            "additionalProperties": False,
        },
        strict=False,
        permission=ToolPermission.ALLOWED,
        closing_allowed=True,
        record_output=False,
    )

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        conclusions = arguments.get("conclusions")
        if not isinstance(conclusions, str) or not conclusions.strip():
            raise ValueError(
                "'conclusions' must be a non-empty string"
            )
        return {
            "conclusions": conclusions.strip(),
            "key_files": _string_list(
                arguments.get("key_files", []),
                field="key_files",
                item_max_chars=_MAX_PATH_CHARS,
                max_items=_MAX_KEY_FILES,
            ),
            "snippets": _snippets(arguments.get("snippets", [])),
            "uncertainties": _string_list(
                arguments.get("uncertainties", []),
                field="uncertainties",
                item_max_chars=_MAX_ITEM_CHARS,
                max_items=_MAX_LIST_ITEMS,
            ),
            "uncovered": _string_list(
                arguments.get("uncovered", []),
                field="uncovered",
                item_max_chars=_MAX_ITEM_CHARS,
                max_items=_MAX_LIST_ITEMS,
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


def _snippets(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("'snippets' must be a list of objects")
    if len(value) > _MAX_SNIPPETS:
        raise ValueError(f"'snippets' accepts at most {_MAX_SNIPPETS} items")
    normalized: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("'snippets' items must be objects")
        file_name = item.get("file")
        code = item.get("code")
        if not isinstance(file_name, str) or not file_name.strip():
            raise ValueError("'snippets[].file' must be a non-empty string")
        if not isinstance(code, str) or not code.strip():
            raise ValueError("'snippets[].code' must be a non-empty string")
        if len(code) > _MAX_SNIPPET_CODE_CHARS:
            raise ValueError(
                f"'snippets[].code' must be at most "
                f"{_MAX_SNIPPET_CODE_CHARS} characters"
            )
        lines = item.get("lines")
        if lines is not None and not isinstance(lines, str):
            raise ValueError("'snippets[].lines' must be a string when present")
        normalized.append(
            {
                "file": file_name.strip(),
                "lines": (lines or "").strip(),
                "code": code,
            }
        )
    return normalized


__all__ = ["SUBMIT_RESULT_TOOL_NAME", "SubmitResultTool"]
