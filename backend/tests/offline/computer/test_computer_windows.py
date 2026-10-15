"""WindowsComputerRuntime 单测（Fake 后端，不触碰真实 UIA / SendInput）。

覆盖：
- observe：映射 / BFS ref / 去重 / 预算截断 / 截图落盘 / 后端异常降级；
- click 分派顺序：Invoke → SelectionItem → Toggle → 坐标回退；
- fresh-observation 校验：stale_observation / element_not_found / 消费快照；
- type 回读校验：VERIFIED / MISMATCH；
- key / scroll / focus_window / open_app 的调用与结果语义；
- uia_mapping 纯函数：role / actions / editable / normalize / key 解析。
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

from app.computer import uia_mapping
from app.computer.models import Bounds, CoordinateTarget, ElementTarget
from app.computer.windows import (
    ElementProbe,
    ForegroundInfo,
    WindowInfo,
    WindowsComputerRuntime,
    ensure_dpi_awareness,
    resolve_app_executable,
)

# ---------------------------------------------------------------------------
# Fake 后端
# ---------------------------------------------------------------------------


class FakeNode:
    """带 children 的元素节点（probe 可被 set_value 改写）。"""

    def __init__(self, probe: ElementProbe, children: tuple = ()) -> None:
        self.probe = probe
        self.children = tuple(children)


def _probe(
    *,
    role: str = "button",
    name: str | None = None,
    value: str | None = None,
    enabled: bool = True,
    focused: bool = False,
    editable: bool = False,
    bounds: Bounds | None = None,
    actions: tuple[str, ...] = (),
) -> ElementProbe:
    return ElementProbe(
        role=role,
        name=name,
        value=value,
        enabled=enabled,
        focused=focused,
        editable=editable,
        bounds=bounds,
        actions=tuple(actions),
    )


class FakeBackend:
    """记录调用的 WindowsDesktopBackend 语义 stub。"""

    def __init__(self, root: FakeNode | None = None) -> None:
        self.root = root
        self.foreground_info = ForegroundInfo(name="FakeApp", pid=42, hwnd=0x100)
        self.foreground_error: Exception | None = None
        self.window_info = WindowInfo(
            hwnd=0x100,
            title="Fake Window",
            bounds=Bounds(x=0, y=0, width=800, height=600),
        )
        self.calls: list[tuple] = []
        self.screenshot: bytes | None = b"PNGDATA"
        self.click_point: tuple[int, int] | None = None
        self.apply_set_value = True
        self.invoke_error: Exception | None = None
        self.foreground_result = True

    def foreground(self) -> ForegroundInfo:
        if self.foreground_error is not None:
            raise self.foreground_error
        return self.foreground_info

    def windows_for_process(self, pid: int) -> tuple[WindowInfo, ...]:
        if pid != self.foreground_info.pid:
            return ()
        return (self.window_info,)

    def window_handle(self, hwnd: int):
        return self.root

    def children(self, node) -> tuple:
        return tuple(getattr(node, "children", ()))

    def probe(self, node) -> ElementProbe | None:
        return getattr(node, "probe", None)

    def capture_primary_screen_png(self) -> bytes | None:
        self.calls.append(("capture",))
        return self.screenshot

    def clickable_point(self, node) -> tuple[int, int] | None:
        return self.click_point

    def invoke(self, node) -> None:
        self.calls.append(("invoke", node))
        if self.invoke_error is not None:
            raise self.invoke_error

    def select(self, node) -> None:
        self.calls.append(("select", node))

    def toggle(self, node) -> None:
        self.calls.append(("toggle", node))

    def set_value(self, node, text: str) -> None:
        self.calls.append(("set_value", node, text))
        if self.apply_set_value:
            node.probe = dataclasses.replace(node.probe, value=text)

    def focus(self, node) -> None:
        self.calls.append(("focus", node))

    def click_at(self, x: int, y: int) -> None:
        self.calls.append(("click_at", x, y))

    def scroll_wheel(self, delta_x: int, delta_y: int) -> None:
        self.calls.append(("scroll_wheel", delta_x, delta_y))

    def send_key_chord(self, virtual_key: int, modifiers: tuple[int, ...]) -> None:
        self.calls.append(("send_key_chord", virtual_key, tuple(modifiers)))

    def set_foreground_window(self, hwnd: int) -> bool:
        self.calls.append(("set_foreground_window", hwnd))
        return self.foreground_result


def _runtime(backend: FakeBackend, tmp_path: Path, **kwargs) -> WindowsComputerRuntime:
    return WindowsComputerRuntime(
        backend=backend, screenshot_dir=tmp_path, **kwargs
    )


def _single_element_tree(probe: ElementProbe) -> tuple[FakeBackend, FakeNode]:
    node = FakeNode(probe)
    root = FakeNode(_probe(role="window", name="Fake Window"), (node,))
    backend = FakeBackend(root)
    backend.click_point = (10, 20)
    return backend, node


def _element_ref(observation, index: int = 1) -> str:
    return observation.elements[index].ref


# ---------------------------------------------------------------------------
# observe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observe_maps_tree_with_dedup_and_stats(tmp_path) -> None:
    edit = FakeNode(
        _probe(
            role="text_field",
            name="Search",
            focused=True,
            editable=True,
            actions=("set_value",),
        )
    )
    buttons = [
        FakeNode(_probe(role="button", name="OK", actions=("press",)))
        for _ in range(4)
    ]
    root = FakeNode(
        _probe(role="window", name="Fake Window"),
        (*buttons, edit),
    )
    backend = FakeBackend(root)
    runtime = _runtime(backend, tmp_path)

    observation = await runtime.observe(include_screenshot=False)

    assert observation.id
    assert observation.target is not None
    assert observation.target.name == "FakeApp"
    assert observation.target.pid == 42
    assert observation.target_is_frontmost is True
    assert observation.active_window is not None
    assert observation.active_window.ref == "w00000100"
    assert [window.ref for window in observation.windows] == ["w00000100"]

    refs = [element.ref for element in observation.elements]
    assert refs == ["e1", "e2", "e3", "e4", "e5"]
    assert [element.role for element in observation.elements] == [
        "window",
        "button",
        "button",
        "button",
        "text_field",
    ]
    assert observation.focused_element_ref == "e5"
    assert observation.truncated is False

    stats = observation.element_stats
    assert stats.observed == 6
    assert stats.returned == 5
    assert stats.editable_count == 1
    assert stats.actionable_count == 4
    assert stats.repetitive_elements_dropped == 1

    await runtime.close()


@pytest.mark.asyncio
async def test_observe_truncates_on_element_budget(tmp_path) -> None:
    root = FakeNode(
        _probe(role="window"),
        tuple(
            FakeNode(_probe(role="button", name=f"b{index}"))
            for index in range(10)
        ),
    )
    backend = FakeBackend(root)
    runtime = _runtime(backend, tmp_path, max_elements=3)

    observation = await runtime.observe(include_screenshot=False)

    assert observation.truncated is True
    assert len(observation.elements) == 3

    await runtime.close()


@pytest.mark.asyncio
async def test_observe_truncates_on_time_budget(tmp_path) -> None:
    backend = FakeBackend(FakeNode(_probe(role="window")))
    runtime = _runtime(backend, tmp_path, time_budget_seconds=-1.0)

    observation = await runtime.observe(include_screenshot=False)

    assert observation.truncated is True
    assert observation.element_stats.observed == 0

    await runtime.close()


@pytest.mark.asyncio
async def test_observe_truncates_on_depth_limit(tmp_path) -> None:
    leaf = FakeNode(_probe(role="text"))
    root = FakeNode(_probe(role="window"), (leaf,))
    backend = FakeBackend(root)
    runtime = _runtime(backend, tmp_path, max_depth=0)

    observation = await runtime.observe(include_screenshot=False)

    # 根能返回，但其 children 因深度上限被截断。
    assert observation.truncated is True
    assert [element.role for element in observation.elements] == ["window"]

    await runtime.close()


@pytest.mark.asyncio
async def test_observe_attaches_screenshot(tmp_path) -> None:
    backend = FakeBackend(FakeNode(_probe(role="window")))
    runtime = _runtime(backend, tmp_path)

    observation = await runtime.observe(include_screenshot=True)

    assert observation.screenshot_ref == f"{observation.id}.png"
    assert (tmp_path / observation.screenshot_ref).read_bytes() == b"PNGDATA"

    await runtime.close()


@pytest.mark.asyncio
async def test_observe_without_screenshot_skips_capture(tmp_path) -> None:
    backend = FakeBackend(FakeNode(_probe(role="window")))
    runtime = _runtime(backend, tmp_path)

    observation = await runtime.observe(include_screenshot=False)

    assert observation.screenshot_ref is None
    assert ("capture",) not in backend.calls

    await runtime.close()


@pytest.mark.asyncio
async def test_observe_survives_backend_failures(tmp_path) -> None:
    backend = FakeBackend(root=None)
    backend.foreground_error = RuntimeError("foreground boom")
    backend.screenshot = None
    runtime = _runtime(backend, tmp_path)

    observation = await runtime.observe(include_screenshot=True)

    assert observation.target is None
    assert observation.active_app is None
    assert observation.windows == ()
    assert observation.elements == ()
    assert observation.screenshot_ref is None

    await runtime.close()


# ---------------------------------------------------------------------------
# click 分派
# ---------------------------------------------------------------------------


async def _observed_element(
    tmp_path: Path, probe: ElementProbe, **runtime_kwargs
) -> tuple[FakeBackend, WindowsComputerRuntime, object]:
    backend, _node = _single_element_tree(probe)
    runtime = _runtime(backend, tmp_path, **runtime_kwargs)
    observation = await runtime.observe(include_screenshot=False)
    return backend, runtime, observation


@pytest.mark.asyncio
async def test_click_prefers_invoke_pattern(tmp_path) -> None:
    backend, runtime, observation = await _observed_element(
        tmp_path, _probe(role="button", name="OK", actions=("press",))
    )
    result = await runtime.click(
        ElementTarget(
            observation_id=observation.id, element_ref=_element_ref(observation)
        )
    )

    assert result.success is True
    assert result.method == "invoke_pattern"
    assert [call[0] for call in backend.calls] == ["invoke"]

    await runtime.close()


@pytest.mark.asyncio
async def test_click_uses_selection_item_pattern(tmp_path) -> None:
    backend, runtime, observation = await _observed_element(
        tmp_path,
        _probe(role="list_item", name="Row", actions=("select",)),
    )
    result = await runtime.click(
        ElementTarget(
            observation_id=observation.id, element_ref=_element_ref(observation)
        )
    )

    assert result.success is True
    assert result.method == "selection_item_pattern"
    assert [call[0] for call in backend.calls] == ["select"]

    await runtime.close()


@pytest.mark.asyncio
async def test_click_uses_toggle_pattern(tmp_path) -> None:
    backend, runtime, observation = await _observed_element(
        tmp_path,
        _probe(role="check_box", name="Agree", actions=("toggle",)),
    )
    result = await runtime.click(
        ElementTarget(
            observation_id=observation.id, element_ref=_element_ref(observation)
        )
    )

    assert result.success is True
    assert result.method == "toggle_pattern"
    assert [call[0] for call in backend.calls] == ["toggle"]

    await runtime.close()


@pytest.mark.asyncio
async def test_click_falls_back_to_clickable_point(tmp_path) -> None:
    backend, runtime, observation = await _observed_element(
        tmp_path,
        _probe(
            role="custom",
            name="Canvas",
            bounds=Bounds(x=100, y=100, width=50, height=50),
        ),
    )
    result = await runtime.click(
        ElementTarget(
            observation_id=observation.id, element_ref=_element_ref(observation)
        )
    )

    assert result.success is True
    assert result.method == "coordinate_fallback"
    assert result.execution_mode == "foreground_fallback"
    assert result.metadata == {"x": 10, "y": 20}
    assert ("click_at", 10, 20) in backend.calls

    await runtime.close()


@pytest.mark.asyncio
async def test_click_falls_back_to_bounds_center(tmp_path) -> None:
    backend, runtime, observation = await _observed_element(
        tmp_path,
        _probe(
            role="custom",
            bounds=Bounds(x=100, y=200, width=40, height=20),
        ),
    )
    backend.click_point = None
    result = await runtime.click(
        ElementTarget(
            observation_id=observation.id, element_ref=_element_ref(observation)
        )
    )

    assert result.success is True
    assert result.metadata == {"x": 120, "y": 210}

    await runtime.close()


@pytest.mark.asyncio
async def test_click_without_any_action_fails(tmp_path) -> None:
    backend, runtime, observation = await _observed_element(
        tmp_path, _probe(role="custom")
    )
    backend.click_point = None
    result = await runtime.click(
        ElementTarget(
            observation_id=observation.id, element_ref=_element_ref(observation)
        )
    )

    assert result.success is False
    assert (result.error or "").startswith("no_action_available")

    await runtime.close()


@pytest.mark.asyncio
async def test_click_reports_backend_failure(tmp_path) -> None:
    backend, runtime, observation = await _observed_element(
        tmp_path, _probe(role="button", name="OK", actions=("press",))
    )
    backend.invoke_error = RuntimeError("invoke boom")
    result = await runtime.click(
        ElementTarget(
            observation_id=observation.id, element_ref=_element_ref(observation)
        )
    )

    assert result.success is False
    assert (result.error or "").startswith("action_failed: RuntimeError")

    await runtime.close()


@pytest.mark.asyncio
async def test_click_coordinate_target(tmp_path) -> None:
    backend, runtime, observation = await _observed_element(
        tmp_path, _probe(role="window")
    )
    result = await runtime.click(
        CoordinateTarget(observation_id=observation.id, x=5, y=6)
    )

    assert result.success is True
    assert result.method == "coordinate"
    assert result.execution_mode == "foreground_fallback"
    assert ("click_at", 5, 6) in backend.calls

    await runtime.close()


# ---------------------------------------------------------------------------
# fresh-observation 校验
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_click_rejects_stale_observation_id(tmp_path) -> None:
    backend, runtime, observation = await _observed_element(
        tmp_path, _probe(role="button", name="OK", actions=("press",))
    )
    result = await runtime.click(
        ElementTarget(observation_id="obs-old", element_ref="e1")
    )

    assert result.success is False
    assert (result.error or "").startswith("stale_observation")

    await runtime.close()


@pytest.mark.asyncio
async def test_click_consumes_snapshot(tmp_path) -> None:
    backend, runtime, observation = await _observed_element(
        tmp_path, _probe(role="button", name="OK", actions=("press",))
    )
    target = ElementTarget(
        observation_id=observation.id, element_ref=_element_ref(observation)
    )
    first = await runtime.click(target)
    second = await runtime.click(target)

    assert first.success is True
    assert second.success is False
    assert (second.error or "").startswith("stale_observation")

    await runtime.close()


@pytest.mark.asyncio
async def test_click_unknown_element_ref_fails(tmp_path) -> None:
    backend, runtime, observation = await _observed_element(
        tmp_path, _probe(role="button", name="OK", actions=("press",))
    )
    result = await runtime.click(
        ElementTarget(observation_id=observation.id, element_ref="e999")
    )

    assert result.success is False
    assert (result.error or "").startswith("element_not_found")

    await runtime.close()


# ---------------------------------------------------------------------------
# type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_type_verifies_value_change(tmp_path) -> None:
    backend, runtime, observation = await _observed_element(
        tmp_path,
        _probe(role="text_field", name="Name", value="old", editable=True),
    )
    result = await runtime.type(
        "hello", element_ref=_element_ref(observation)
    )

    assert result.success is True
    assert result.verification_status == "verified"
    assert result.method == "value_pattern"
    assert [call[0] for call in backend.calls] == ["set_value"]

    await runtime.close()


@pytest.mark.asyncio
async def test_type_reports_mismatch_when_value_unchanged(tmp_path) -> None:
    backend, runtime, observation = await _observed_element(
        tmp_path, _probe(role="text_field", name="Name", value="old", editable=True)
    )
    backend.apply_set_value = False
    result = await runtime.type(
        "hello", element_ref=_element_ref(observation)
    )

    assert result.success is False
    assert result.verification_status == "mismatch"
    assert (result.error or "").startswith("input_effect_mismatch")

    await runtime.close()


@pytest.mark.asyncio
async def test_type_uses_focused_element_when_ref_omitted(tmp_path) -> None:
    backend, runtime, observation = await _observed_element(
        tmp_path,
        _probe(
            role="text_field",
            name="Name",
            value="",
            focused=True,
            editable=True,
        ),
    )
    result = await runtime.type("hello")

    assert result.success is True
    assert observation.focused_element_ref is not None

    await runtime.close()


@pytest.mark.asyncio
async def test_type_rejects_non_editable_element(tmp_path) -> None:
    backend, runtime, observation = await _observed_element(
        tmp_path, _probe(role="button", name="OK", actions=("press",))
    )
    result = await runtime.type("hello", element_ref=_element_ref(observation))

    assert result.success is False
    assert (result.error or "").startswith("element_not_editable")

    await runtime.close()


@pytest.mark.asyncio
async def test_type_without_observation_fails_closed(tmp_path) -> None:
    runtime = _runtime(FakeBackend(), tmp_path)

    result = await runtime.type("hello")

    assert result.success is False
    assert (result.error or "").startswith("stale_observation")

    await runtime.close()


# ---------------------------------------------------------------------------
# key / scroll
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_key_sends_chord_with_modifiers(tmp_path) -> None:
    backend, runtime, _observation = await _observed_element(
        tmp_path, _probe(role="window")
    )
    result = await runtime.key("a", modifiers=("ctrl", "shift"))

    assert result.success is True
    assert result.method == "sendinput"
    assert result.verification_status == "not_applicable"
    assert ("send_key_chord", 0x41, (0x11, 0x10)) in backend.calls

    await runtime.close()


@pytest.mark.asyncio
async def test_key_with_element_ref_focuses_and_consumes(tmp_path) -> None:
    backend, runtime, observation = await _observed_element(
        tmp_path, _probe(role="text_field", name="Name", editable=True)
    )
    ref = _element_ref(observation)
    result = await runtime.key("enter", element_ref=ref)

    assert result.success is True
    assert result.observation_id == observation.id
    assert [call[0] for call in backend.calls] == ["focus", "send_key_chord"]

    # 带 element_ref 的 key 产生焦点副作用 → 快照失效。
    again = await runtime.key("enter", element_ref=ref)
    assert again.success is False
    assert (again.error or "").startswith("stale_observation")

    await runtime.close()


@pytest.mark.asyncio
async def test_key_without_element_ref_keeps_snapshot(tmp_path) -> None:
    backend, runtime, observation = await _observed_element(
        tmp_path, _probe(role="button", name="OK", actions=("press",))
    )
    result = await runtime.key("tab")

    assert result.success is True
    # 无 element_ref 的 key 不失效快照：仍可用同一 observation 点击。
    follow_up = await runtime.click(
        ElementTarget(
            observation_id=observation.id, element_ref=_element_ref(observation)
        )
    )
    assert follow_up.success is True

    await runtime.close()


@pytest.mark.asyncio
async def test_key_rejects_unknown_key_and_modifier(tmp_path) -> None:
    backend, runtime, _observation = await _observed_element(
        tmp_path, _probe(role="window")
    )
    bad_key = await runtime.key("definitely-not-a-key")
    bad_modifier = await runtime.key("a", modifiers=("hyper",))

    assert bad_key.success is False
    assert (bad_key.error or "").startswith("invalid_key")
    assert bad_modifier.success is False
    assert (bad_modifier.error or "").startswith("invalid_key")
    assert not [call for call in backend.calls if call[0] == "send_key_chord"]

    await runtime.close()


@pytest.mark.asyncio
async def test_scroll_sends_wheel_and_keeps_snapshot(tmp_path) -> None:
    backend, runtime, observation = await _observed_element(
        tmp_path, _probe(role="button", name="OK", actions=("press",))
    )
    result = await runtime.scroll(delta_x=1, delta_y=-2)

    assert result.success is True
    assert result.method == "sendinput_wheel"
    assert result.verification_status == "not_applicable"
    assert ("scroll_wheel", 1, -2) in backend.calls

    # scroll 不产生确定性 UI 副作用 → 快照保留。
    follow_up = await runtime.click(
        ElementTarget(
            observation_id=observation.id, element_ref=_element_ref(observation)
        )
    )
    assert follow_up.success is True

    await runtime.close()


# ---------------------------------------------------------------------------
# focus_window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_focus_window_uses_latest_observation(tmp_path) -> None:
    backend, runtime, observation = await _observed_element(
        tmp_path, _probe(role="window")
    )
    ref = observation.windows[0].ref
    result = await runtime.focus_window(ref)

    assert result.success is True
    assert result.method == "set_foreground_window"
    assert ("set_foreground_window", 0x100) in backend.calls

    # 聚焦产生 UI 副作用 → 快照失效。
    again = await runtime.focus_window(ref)
    assert again.success is False
    assert (again.error or "").startswith("stale_observation")

    await runtime.close()


@pytest.mark.asyncio
async def test_focus_window_unknown_ref_fails(tmp_path) -> None:
    backend, runtime, _observation = await _observed_element(
        tmp_path, _probe(role="window")
    )
    result = await runtime.focus_window("w99999999")

    assert result.success is False
    assert (result.error or "").startswith("stale_observation")
    assert ("set_foreground_window", 0x100) not in backend.calls

    await runtime.close()


@pytest.mark.asyncio
async def test_focus_window_denied_reports_failure(tmp_path) -> None:
    backend, runtime, observation = await _observed_element(
        tmp_path, _probe(role="window")
    )
    backend.foreground_result = False
    result = await runtime.focus_window(observation.windows[0].ref)

    assert result.success is False
    assert (result.error or "").startswith("focus_failed")

    await runtime.close()


# ---------------------------------------------------------------------------
# open_app
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_app_uses_resolved_executable(tmp_path) -> None:
    launched: list[str] = []

    def resolver(app: str) -> Path | None:
        return Path("C:/apps/Notepad.exe") if app == "notepad" else None

    def launcher(executable: str) -> int:
        launched.append(executable)
        return 777

    runtime = _runtime(
        FakeBackend(), tmp_path, app_resolver=resolver, app_launcher=launcher
    )
    runtime.begin_session("run-a")

    result = await runtime.open_app("notepad")

    assert result.success is True
    assert result.method == "popen"
    assert result.metadata == {"app": "notepad", "pid": 777}
    assert launched == [str(Path("C:/apps/Notepad.exe"))]
    session = runtime._sessions.get_active()
    assert session is not None
    assert session.target_app is not None
    assert session.target_app.name == "Notepad"
    assert session.target_app.pid == 777

    await runtime.close()


@pytest.mark.asyncio
async def test_open_app_falls_back_to_shell(tmp_path) -> None:
    opened: list[str] = []

    runtime = _runtime(
        FakeBackend(),
        tmp_path,
        app_resolver=lambda app: None,
        app_opener=opened.append,
    )

    result = await runtime.open_app("ms-settings:")

    assert result.success is True
    assert result.method == "shell_startfile"
    assert result.metadata == {"app": "ms-settings:", "pid": None}
    assert opened == ["ms-settings:"]

    await runtime.close()


@pytest.mark.asyncio
async def test_open_app_reports_launch_failure(tmp_path) -> None:
    def launcher(executable: str) -> int:
        raise OSError("cannot start")

    runtime = _runtime(
        FakeBackend(),
        tmp_path,
        app_resolver=lambda app: Path("C:/apps/Tool.exe"),
        app_launcher=launcher,
    )

    result = await runtime.open_app("tool")

    assert result.success is False
    assert (result.error or "").startswith("open_app_failed")

    await runtime.close()


@pytest.mark.asyncio
async def test_open_app_consumes_snapshot(tmp_path) -> None:
    backend, runtime, observation = await _observed_element(
        tmp_path, _probe(role="window")
    )
    opened: list[str] = []
    runtime._app_resolver = lambda app: None
    runtime._app_opener = opened.append

    await runtime.open_app("tool")

    result = await runtime.click(
        CoordinateTarget(observation_id=observation.id, x=1, y=1)
    )
    assert result.success is False
    assert (result.error or "").startswith("stale_observation")
    assert opened == ["tool"]

    await runtime.close()


def test_resolve_app_executable_absolute_path(tmp_path) -> None:
    executable = tmp_path / "tool.exe"
    executable.write_bytes(b"MZ")

    assert resolve_app_executable(str(executable)) == executable
    assert resolve_app_executable(str(tmp_path / "missing.exe")) is None


def test_resolve_app_executable_uses_path(monkeypatch, tmp_path) -> None:
    executable = tmp_path / "tool.exe"
    executable.write_bytes(b"MZ")

    import shutil

    def fake_which(name: str) -> str | None:
        return str(executable) if name == "tool.exe" else None

    monkeypatch.setattr(shutil, "which", fake_which)

    assert resolve_app_executable("tool") == executable
    assert resolve_app_executable("nothing") is None


@pytest.mark.skipif(sys.platform != "win32", reason="App Paths 注册表仅 Windows")
def test_resolve_app_executable_unknown_name_returns_none() -> None:
    assert resolve_app_executable("evolyn-no-such-app-9dc3") is None


# ---------------------------------------------------------------------------
# 状态 / 生命周期
# ---------------------------------------------------------------------------


def test_permission_status_is_unknown_on_windows(tmp_path) -> None:
    runtime = _runtime(FakeBackend(), tmp_path)
    assert runtime.permission_status() == {
        "accessibility": None,
        "screen_recording": None,
    }


@pytest.mark.asyncio
async def test_start_and_close_are_safe(tmp_path) -> None:
    runtime = _runtime(FakeBackend(), tmp_path)
    await runtime.start()
    runtime.begin_session("run-a")
    await runtime.close()
    assert runtime._sessions.active_count() == 0


@pytest.mark.skipif(sys.platform != "win32", reason="DPI API 仅 Windows")
def test_ensure_dpi_awareness_reports_api() -> None:
    assert ensure_dpi_awareness() in (
        "SetProcessDpiAwarenessContext",
        "SetProcessDpiAwareness",
        "SetProcessDPIAware",
        None,
    )


# ---------------------------------------------------------------------------
# uia_mapping 纯函数
# ---------------------------------------------------------------------------


def test_element_role_mapping() -> None:
    assert uia_mapping.element_role("ButtonControl") == "button"
    assert uia_mapping.element_role("EditControl") == "text_field"
    assert uia_mapping.element_role("DocumentControl") == "text_area"
    assert uia_mapping.element_role("SomethingNewControl") == "custom"
    assert uia_mapping.element_role(None) == "custom"
    assert uia_mapping.element_role("") == "custom"


def test_element_actions_mapping() -> None:
    support = uia_mapping.PatternSupport(
        invoke=True,
        value=True,
        selection_item=True,
        expand_collapse=True,
        toggle=True,
        scroll_item=True,
    )
    assert uia_mapping.element_actions(support) == (
        "press",
        "set_value",
        "select",
        "expand_collapse",
        "toggle",
        "scroll_into_view",
    )
    read_only = uia_mapping.PatternSupport(value=True, value_read_only=True)
    assert uia_mapping.element_actions(read_only) == ()
    assert uia_mapping.element_actions(uia_mapping.PatternSupport()) == ()


def test_is_editable_requires_writable_value_pattern() -> None:
    writable = uia_mapping.PatternSupport(value=True)
    readonly = uia_mapping.PatternSupport(value=True, value_read_only=True)

    assert uia_mapping.is_editable("EditControl", writable) is True
    assert uia_mapping.is_editable("DocumentControl", writable) is True
    assert uia_mapping.is_editable("ComboBoxControl", writable) is True
    assert uia_mapping.is_editable("EditControl", readonly) is False
    assert uia_mapping.is_editable("ButtonControl", writable) is False
    assert uia_mapping.is_editable(None, writable) is False


def test_normalize_text() -> None:
    assert uia_mapping.normalize_text(None) is None
    assert uia_mapping.normalize_text("") is None
    assert uia_mapping.normalize_text("hello") == "hello"

    long_value = "x" * 2100
    normalized = uia_mapping.normalize_text(long_value)
    assert normalized is not None
    assert normalized.endswith("…(truncated)")
    assert len(normalized) == 2000 + len("…(truncated)")


def test_parse_key_accepts_letters_digits_and_named_keys() -> None:
    assert uia_mapping.parse_key("a") == 0x41
    assert uia_mapping.parse_key("Z") == 0x5A
    assert uia_mapping.parse_key("0") == 0x30
    assert uia_mapping.parse_key("enter") == 0x0D
    assert uia_mapping.parse_key("Page_Up") == 0x21
    assert uia_mapping.parse_key("f1") == 0x70
    assert uia_mapping.parse_key("f24") == 0x87


@pytest.mark.parametrize("bad", ["", "  ", "definitely-not-a-key", "ctrl", "+"])
def test_parse_key_fails_closed(bad: str) -> None:
    with pytest.raises(ValueError):
        uia_mapping.parse_key(bad)


def test_parse_modifiers() -> None:
    assert uia_mapping.parse_modifiers(("ctrl", "shift")) == (0x11, 0x10)
    assert uia_mapping.parse_modifiers(("command",)) == (0x5B,)
    assert uia_mapping.parse_modifiers(("ctrl", "control")) == (0x11,)
    assert uia_mapping.parse_modifiers(()) == ()


def test_parse_modifiers_fails_closed() -> None:
    with pytest.raises(ValueError):
        uia_mapping.parse_modifiers(("hyper",))
