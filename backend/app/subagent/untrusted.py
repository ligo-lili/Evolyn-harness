"""外部内容的过滤层：不可信数据分隔符 + 密钥脱敏（v3 方案 7 / 3.4 节）。

子代理读取到的仓库原文、文件列表、搜索结果与网页都是**不可信数据**：其中
可能包含试图改变任务目标或权限的"指令"，也可能夹带密钥。包装器把这类输出
先做密钥脱敏（``[REDACTED:…]`` 占位符），再统一放进
``<untrusted-repo-content> ... </untrusted-repo-content>`` 分隔符内；子代理
系统提示词声明分隔符内的内容只是数据；主 Agent 提示词同样把带分隔符的
证据输出声明为不可信数据。

协议工具（record_finding / submit_result）与子代理回传载荷不经过包装：它们
是 Harness 生成的受信结构，包装会破坏机器的 JSON 解析。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.tools.base import BaseTool

from .redaction import redact_value

if TYPE_CHECKING:
    from app.models.types import ToolDefinition
    from app.tools.hooks import ToolExecutionContext

UNTRUSTED_OPEN = "<untrusted-repo-content>"
UNTRUSTED_CLOSE = "</untrusted-repo-content>"


def wrap_untrusted(value: Any) -> Any:
    """递归包装字符串（dict / list / tuple 保持结构）；已包装的不重复包装。"""

    if isinstance(value, str):
        if UNTRUSTED_OPEN in value:
            return value
        return f"{UNTRUSTED_OPEN}\n{value}\n{UNTRUSTED_CLOSE}"
    if isinstance(value, dict):
        return {key: wrap_untrusted(item) for key, item in value.items()}
    if isinstance(value, list):
        return [wrap_untrusted(item) for item in value]
    if isinstance(value, tuple):
        return tuple(wrap_untrusted(item) for item in value)
    return value


class UntrustedContentTool(BaseTool):
    """把工具输出脱敏后包装为不可信数据（外部内容工具统一使用）。"""

    def __init__(self, inner: BaseTool) -> None:
        self._inner = inner

    @property
    def definition(self) -> ToolDefinition:
        return self._inner.definition

    async def execute(self, arguments: dict[str, Any]) -> Any:
        return wrap_untrusted(
            redact_value(await self._inner.execute(arguments))
        )

    async def execute_with_context(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> Any:
        return wrap_untrusted(
            redact_value(
                await self._inner.execute_with_context(arguments, context)
            )
        )


__all__ = [
    "UNTRUSTED_CLOSE",
    "UNTRUSTED_OPEN",
    "UntrustedContentTool",
    "wrap_untrusted",
]
