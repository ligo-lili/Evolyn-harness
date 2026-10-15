"""Windows UIA / 输入映射层（纯函数，不 import uiautomation）。

职责：
- UIA ``ControlTypeName`` → ``Element.role``；
- Pattern 可用性 → ``Element.actions``（press / set_value / select / ...）；
- editable / value 可读判定；
- ``computer_key`` 的按键名与修饰键 → Windows 虚拟键码（VK）。

真实 uiautomation Control 与测试 mock 都只需满足鸭子类型：本模块只消费
已经探测好的 ``PatternSupport``，不直接调用系统 API（探测在 windows.py 后端）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

__all__ = [
    "KEY_VIRTUAL_CODES",
    "PatternSupport",
    "element_actions",
    "element_role",
    "is_editable",
    "normalize_text",
    "parse_key",
    "parse_modifiers",
]

# ---------------------------------------------------------------------------
# ControlTypeName → Element.role（尽量复用 V0 词汇表：button / text_field /
# text_area / combo_box / list_item ...）
# ---------------------------------------------------------------------------

_CONTROL_TYPE_ROLES: dict[str, str] = {
    "ButtonControl": "button",
    "SplitButtonControl": "button",
    "CalendarControl": "calendar",
    "CheckBoxControl": "check_box",
    "ComboBoxControl": "combo_box",
    "CustomControl": "custom",
    "DataGridControl": "table",
    "DataItemControl": "list_item",
    "DocumentControl": "text_area",
    "EditControl": "text_field",
    "GroupControl": "group",
    "HeaderControl": "header",
    "HeaderItemControl": "header_item",
    "HyperlinkControl": "link",
    "ImageControl": "image",
    "ListControl": "list",
    "ListItemControl": "list_item",
    "MenuBarControl": "menu_bar",
    "MenuControl": "menu",
    "MenuItemControl": "menu_item",
    "PaneControl": "pane",
    "ProgressBarControl": "progress_bar",
    "RadioButtonControl": "radio_button",
    "ScrollBarControl": "scroll_bar",
    "SeparatorControl": "separator",
    "SliderControl": "slider",
    "SpinnerControl": "spinner",
    "StatusBarControl": "status_bar",
    "TabControl": "tab",
    "TabItemControl": "tab_item",
    "TableControl": "table",
    "TextControl": "text",
    "ThumbControl": "thumb",
    "TitleBarControl": "title_bar",
    "ToolBarControl": "tool_bar",
    "ToolTipControl": "tool_tip",
    "TreeControl": "tree",
    "TreeItemControl": "tree_item",
    "WindowControl": "window",
}


def element_role(control_type_name: str | None) -> str:
    """ControlTypeName → 稳定的 role 字符串；未知类型归为 custom。"""

    if not control_type_name:
        return "custom"
    return _CONTROL_TYPE_ROLES.get(control_type_name, "custom")


@dataclass(frozen=True, slots=True)
class PatternSupport:
    """一个 UIA 元素已探测到的 Pattern 可用性。"""

    invoke: bool = False
    value: bool = False
    value_read_only: bool = False
    selection_item: bool = False
    expand_collapse: bool = False
    toggle: bool = False
    scroll_item: bool = False


def element_actions(support: PatternSupport) -> tuple[str, ...]:
    """Pattern 可用性 → Element.actions（稳定的小写动作词汇表）。"""

    actions: list[str] = []
    if support.invoke:
        actions.append("press")
    if support.value and not support.value_read_only:
        actions.append("set_value")
    if support.selection_item:
        actions.append("select")
    if support.expand_collapse:
        actions.append("expand_collapse")
    if support.toggle:
        actions.append("toggle")
    if support.scroll_item:
        actions.append("scroll_into_view")
    return tuple(actions)


# 只有这些控件类型可能承载可编辑文本；是否真的可编辑还要看 ValuePattern。
_EDITABLE_CONTROL_TYPES = frozenset(
    {"EditControl", "DocumentControl", "ComboBoxControl"}
)


def is_editable(control_type_name: str | None, support: PatternSupport) -> bool:
    """可编辑 = 文本类控件 + ValuePattern 可写（只读文档 / 密码框不算）。"""

    if control_type_name not in _EDITABLE_CONTROL_TYPES:
        return False
    return support.value and not support.value_read_only


def normalize_text(value: str | None, *, limit: int = 2000) -> str | None:
    """规范 Element.value：空串归 None，超长截断（尾部显式标注）。"""

    if value is None:
        return None
    if not value:
        return None
    if len(value) <= limit:
        return value
    return value[:limit] + "…(truncated)"


# ---------------------------------------------------------------------------
# computer_key：按键名 / 修饰键 → Windows 虚拟键码
# ---------------------------------------------------------------------------

KEY_VIRTUAL_CODES: dict[str, int] = {
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "escape": 0x1B,
    "esc": 0x1B,
    "space": 0x20,
    "backspace": 0x08,
    "back": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "page_up": 0x21,
    "pageup": 0x21,
    "pgup": 0x21,
    "page_down": 0x22,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "capslock": 0x14,
    "printscreen": 0x2C,
    "pause": 0x13,
    "numlock": 0x90,
    "scrolllock": 0x91,
    "`": 0xC0,
    "-": 0xBD,
    "=": 0xBB,
    "[": 0xDB,
    "]": 0xDD,
    "\\": 0xDC,
    ";": 0xBA,
    "'": 0xDE,
    ",": 0xBC,
    ".": 0xBE,
    "/": 0xBF,
}
KEY_VIRTUAL_CODES.update({f"f{index}": 0x6F + index for index in range(1, 25)})

MODIFIER_VIRTUAL_CODES: dict[str, int] = {
    "ctrl": 0x11,
    "control": 0x11,
    "shift": 0x10,
    "alt": 0x12,
    "option": 0x12,
    "win": 0x5B,
    "command": 0x5B,
    "cmd": 0x5B,
    "meta": 0x5B,
}


def parse_key(key: str) -> int:
    """按键名 → 虚拟键码；未知按键抛 ValueError（fail closed）。"""

    normalized = key.strip().lower()
    if not normalized:
        raise ValueError("key cannot be empty")
    if len(normalized) == 1:
        character = normalized
        if "a" <= character <= "z":
            return 0x41 + (ord(character) - ord("a"))
        if "0" <= character <= "9":
            return 0x30 + (ord(character) - ord("0"))
    if normalized in KEY_VIRTUAL_CODES:
        return KEY_VIRTUAL_CODES[normalized]
    raise ValueError(f"unsupported key name: {key!r}")


def parse_modifiers(modifiers: Iterable[str]) -> tuple[int, ...]:
    """修饰键列表 → 虚拟键码元组；未知修饰键抛 ValueError。"""

    codes: list[int] = []
    for modifier in modifiers:
        normalized = modifier.strip().lower()
        code = MODIFIER_VIRTUAL_CODES.get(normalized)
        if code is None:
            raise ValueError(f"unsupported modifier: {modifier!r}")
        if code not in codes:
            codes.append(code)
    return tuple(codes)
