"""runtime_helpers.looks_like_textual_tool_call 的变形覆盖测试。"""

from __future__ import annotations

import pytest

from app.agent.runtime_helpers import looks_like_textual_tool_call


@pytest.mark.parametrize(
    "content",
    [
        # 规范形态
        "<tool_calls>count(1)</tool_calls>",
        '<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="count">2',
        # 机制试跑实测变形：关键字前带空格、tool_ 前缀丢失
        '<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="submit_result">\n'
        '<｜｜DSML｜｜ parameter name="summary" string="true">已核实不符项',
        '<｜｜DSML｜｜ calls><｜｜DSML｜｜invoke name="submit_result">',
        # 空格式变形与零宽 / 块状分隔符
        "< tool_calls>count(1)</tool_calls>",
        "<｜｜DSML｜｜\u200btool_calls>",
        "<｜｜DSML｜｜\u2581invoke name=\"count\">",
    ],
)
def test_detects_textual_tool_call_variants(content: str) -> None:
    assert looks_like_textual_tool_call(content) is True


@pytest.mark.parametrize(
    "content",
    [
        None,
        "",
        "   \n\t",
        "普通回答：不要输出工具协议标记，这句话只是文字说明。",
        "讨论中提到 tool_calls 这个词，但没有协议形态。",
    ],
)
def test_ignores_plain_content(content: str | None) -> None:
    assert looks_like_textual_tool_call(content) is False
