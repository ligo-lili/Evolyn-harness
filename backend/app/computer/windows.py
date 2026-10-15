"""WindowsComputerRuntime：UIAutomation + Pillow + SendInput 的真实 Computer Runtime。

分层：
- ``Win32DesktopBackend``：对 Win32 / UIAutomation / Pillow / SendInput 的
  最小语义封装（所有系统调用都在这一层，uiautomation 懒加载）；
- ``WindowsComputerRuntime``：编排层——观察预算（时间 / 深度 / 数量上限）、
  fresh-observation 校验、元素级优先的动作分派、session/target 生命周期。
  只依赖后端语义接口，测试可注入 Fake 后端。

V1 边界（如实标注）：
- 截图仅主屏全屏（Pillow ``ImageGrab``），不做多屏拼接 / 窗口级截图；
- ``open_app`` 不支持 UWP：优先 PATH / App Paths 解析 exe 后 ``Popen``，
  解析失败回退 ``os.startfile``（由 Shell 解析，可能落在商店应用）；
- 元素动作优先 UIA Pattern（Invoke / Value / SelectionItem / Toggle），
  无 Pattern 时回退前台坐标点击（``execution_mode=foreground_fallback``）；
- 构造时设置 DPI 感知，坐标统一为物理像素。
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import shutil
import subprocess
import sys
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from . import uia_mapping
from .models import (
    ActionName,
    ActionResult,
    ActiveApp,
    Bounds,
    CoordinateTarget,
    DeliveryStatus,
    Element,
    ElementStats,
    ElementTarget,
    Observation,
    VerificationStatus,
    Window,
)
from .session import ComputerSession, ComputerSessionManager

if sys.platform == "win32":  # pragma: no cover - 仅在 Windows 上执行
    from ctypes import wintypes

logger = logging.getLogger("evolyn.computer.windows")

_DEFAULT_SCREENSHOT_DIR = (
    Path(__file__).resolve().parents[2] / ".evolyn" / "computer" / "screenshots"
)

_MAX_WINDOWS = 10
_MAX_OBSERVED_ELEMENTS = 400
_MAX_OBSERVATION_DEPTH = 12
_OBSERVATION_TIME_BUDGET_SECONDS = 5.0
_MAX_REPETITIVE_ELEMENTS = 3

__all__ = [
    "ElementProbe",
    "ForegroundInfo",
    "Win32DesktopBackend",
    "WindowInfo",
    "WindowsComputerRuntime",
    "ensure_dpi_awareness",
    "resolve_app_executable",
]


# ---------------------------------------------------------------------------
# DPI 感知
# ---------------------------------------------------------------------------


def ensure_dpi_awareness() -> str | None:
    """设置进程 DPI 感知；返回生效的 API 名称，全部失败返回 None。

    UIA / SendInput 的坐标必须是物理像素，否则多显示器 + 缩放环境下
    观察坐标与点击坐标会整体偏移。已设置过时 Windows 返回错误码，视为成功。
    """

    if sys.platform != "win32":  # pragma: no cover - 产品仅面向 Windows
        return None
    user32 = _user32()
    try:
        set_context = user32.SetProcessDpiAwarenessContext
        set_context.argtypes = [ctypes.c_void_p]
        set_context.restype = ctypes.c_bool
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        set_context(ctypes.c_void_p(-4))
        return "SetProcessDpiAwarenessContext"
    except (AttributeError, OSError):
        pass
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        shcore.SetProcessDpiAwareness.restype = ctypes.c_int
        shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return "SetProcessDpiAwareness"
    except (AttributeError, OSError):
        pass
    try:
        user32.SetProcessDPIAware()
        return "SetProcessDPIAware"
    except (AttributeError, OSError):
        return None


# ---------------------------------------------------------------------------
# 后端语义数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ForegroundInfo:
    """当前前台窗口的信息（名称 / pid / hwnd）。"""

    name: str | None
    pid: int | None
    hwnd: int | None


@dataclass(frozen=True, slots=True)
class WindowInfo:
    """一个顶层窗口的 Win32 信息。"""

    hwnd: int
    title: str
    bounds: Bounds


@dataclass(frozen=True, slots=True)
class ElementProbe:
    """一个 UIA 元素的只读快照（后端探测结果，供 Runtime 组装 Element）。"""

    role: str
    name: str | None
    value: str | None
    enabled: bool
    focused: bool
    editable: bool
    bounds: Bounds | None
    actions: tuple[str, ...]


class _ActionTargetError(Exception):
    """动作目标解析失败（stale observation / element_not_found）。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


class WindowsComputerRuntime:
    """Windows 版 ComputerRuntime 编排层（实现 ComputerRuntime 契约）。"""

    def __init__(
        self,
        *,
        backend: Any | None = None,
        session_manager: ComputerSessionManager | None = None,
        screenshot_dir: str | Path | None = None,
        app_resolver: Callable[[str], str | Path | None] | None = None,
        app_launcher: Callable[[str], int | None] | None = None,
        app_opener: Callable[[str], None] | None = None,
        max_elements: int = _MAX_OBSERVED_ELEMENTS,
        max_depth: int = _MAX_OBSERVATION_DEPTH,
        time_budget_seconds: float = _OBSERVATION_TIME_BUDGET_SECONDS,
    ) -> None:
        self._backend = backend if backend is not None else Win32DesktopBackend()
        self._sessions = session_manager or ComputerSessionManager()
        self._screenshot_dir = (
            Path(screenshot_dir) if screenshot_dir is not None else None
        )
        self._app_resolver = app_resolver
        self._app_launcher = app_launcher
        self._app_opener = app_opener
        self._max_elements = max_elements
        self._max_depth = max_depth
        self._max_windows = _MAX_WINDOWS
        self._time_budget = time_budget_seconds
        self._snapshot: Observation | None = None
        self._element_handles: dict[tuple[str, str], Any] = {}
        self._window_handles: dict[tuple[str, str], int] = {}
        self._dpi_awareness = ensure_dpi_awareness()

    # ------------------------------------------------------------------
    # 生命周期 / 装配
    # ------------------------------------------------------------------

    @property
    def screenshot_dir(self) -> Path:
        """截图目录（server 的 media transport 直接读取它）。"""

        return self._screenshot_dir or _DEFAULT_SCREENSHOT_DIR

    def set_session_manager(self, manager: ComputerSessionManager) -> None:
        """由 composition root 注入应用级 SessionManager（替换内部实例）。"""

        self._sessions = manager

    def begin_session(self, run_id: str) -> ComputerSession:
        return self._sessions.begin(run_id)

    async def begin_session_rpc(self, run_id: str) -> ComputerSession:
        """ComputerLeaseHook 注入的 session_starter（Windows 侧无 helper 调用）。"""

        return self._sessions.begin(run_id)

    async def end_session(self, run_id: str) -> bool:
        ended = self._sessions.end(run_id)
        self._clear_snapshot()
        return ended

    async def start(self) -> None:
        """启动钩子：DPI 感知在构造时已设置，这里只记录一次日志。"""

        logger.info(
            "Windows computer runtime ready (dpi_awareness=%s)",
            self._dpi_awareness,
        )

    async def close(self) -> None:
        self._sessions.close()
        self._clear_snapshot()

    def permission_status(self) -> dict[str, bool | None]:
        """Windows V1 没有 TCC 式系统权限门槛：统一返回 unknown。"""

        return {"accessibility": None, "screen_recording": None}

    # ------------------------------------------------------------------
    # ComputerRuntime 契约
    # ------------------------------------------------------------------

    async def observe(self, include_screenshot: bool = True) -> Observation:
        return await asyncio.to_thread(self._observe_sync, include_screenshot)

    async def click(
        self,
        target: ElementTarget | CoordinateTarget,
    ) -> ActionResult:
        return await asyncio.to_thread(self._click_sync, target)

    async def type(
        self,
        text: str,
        element_ref: str | None = None,
    ) -> ActionResult:
        return await asyncio.to_thread(self._type_sync, text, element_ref)

    async def key(
        self,
        key: str,
        modifiers: tuple[str, ...] = (),
        element_ref: str | None = None,
    ) -> ActionResult:
        return await asyncio.to_thread(self._key_sync, key, modifiers, element_ref)

    async def scroll(
        self,
        delta_x: int = 0,
        delta_y: int = 0,
    ) -> ActionResult:
        return await asyncio.to_thread(self._scroll_sync, delta_x, delta_y)

    async def open_app(self, app: str) -> ActionResult:
        return await asyncio.to_thread(self._open_app_sync, app)

    async def focus_window(self, window_ref: str) -> ActionResult:
        return await asyncio.to_thread(self._focus_window_sync, window_ref)

    # ------------------------------------------------------------------
    # 观察
    # ------------------------------------------------------------------

    def _observe_sync(self, include_screenshot: bool) -> Observation:
        observation_id = uuid4().hex
        try:
            foreground = self._backend.foreground()
        except Exception as exc:
            logger.warning("observe: foreground lookup failed: %s", exc)
            foreground = ForegroundInfo(name=None, pid=None, hwnd=None)

        session = self._sessions.get_active()
        target: ActiveApp | None = (
            session.target_app if session is not None else None
        )
        if target is None and foreground.name:
            target = ActiveApp(name=foreground.name, pid=foreground.pid)
        target_is_frontmost = (
            target is not None
            and foreground.pid is not None
            and target.pid == foreground.pid
        )
        user_frontmost = (
            ActiveApp(name=foreground.name, pid=foreground.pid)
            if foreground.name
            else None
        )

        window_infos: tuple[WindowInfo, ...] = ()
        if target is not None and target.pid is not None:
            try:
                window_infos = tuple(
                    self._backend.windows_for_process(target.pid)
                )
            except Exception as exc:
                logger.warning("observe: window enumeration failed: %s", exc)
                window_infos = ()
        window_infos = window_infos[: self._max_windows]

        windows: list[Window] = []
        for info in window_infos:
            ref = _window_ref(info.hwnd)
            windows.append(Window(ref=ref, title=info.title, bounds=info.bounds))
            self._window_handles[(observation_id, ref)] = info.hwnd

        active_info: WindowInfo | None = None
        if foreground.hwnd is not None:
            active_info = next(
                (
                    info
                    for info in window_infos
                    if info.hwnd == foreground.hwnd
                ),
                None,
            )
        if active_info is None and window_infos:
            active_info = window_infos[0]
        active_window = (
            Window(
                ref=_window_ref(active_info.hwnd),
                title=active_info.title,
                bounds=active_info.bounds,
            )
            if active_info is not None
            else None
        )

        elements, element_stats, focused_ref, truncated = self._collect_elements(
            observation_id,
            active_info.hwnd if active_info is not None else None,
        )

        observation = Observation(
            id=observation_id,
            target=target,
            active_app=target,
            target_is_frontmost=target_is_frontmost,
            user_frontmost_app=user_frontmost,
            active_window=active_window,
            windows=tuple(windows),
            elements=elements,
            focused_element_ref=focused_ref,
            truncated=truncated,
            element_stats=element_stats,
        )
        if include_screenshot:
            observation = self._attach_screenshot(observation)
        self._remember_snapshot(observation)
        return observation

    def _collect_elements(
        self,
        observation_id: str,
        hwnd: int | None,
    ) -> tuple[tuple[Element, ...], ElementStats, str | None, bool]:
        """BFS 遍历 UIA 树；超时 / 超深 / 超量的截断如实写入 stats。"""

        if hwnd is None:
            return (), ElementStats(), None, False
        try:
            root = self._backend.window_handle(hwnd)
        except Exception as exc:
            logger.warning("observe: window handle lookup failed: %s", exc)
            root = None
        if root is None:
            return (), ElementStats(), None, False

        queue: deque[tuple[Any, int]] = deque([(root, 0)])
        elements: list[Element] = []
        visited = 0
        dropped = 0
        truncated = False
        focused_ref: str | None = None
        seen: Counter[tuple[str, str]] = Counter()
        deadline = monotonic() + self._time_budget
        while queue:
            if len(elements) >= self._max_elements or monotonic() > deadline:
                truncated = True
                break
            node, depth = queue.popleft()
            visited += 1
            probe: ElementProbe | None
            try:
                probe = self._backend.probe(node)
            except Exception as exc:
                logger.debug("observe: element probe failed: %s", exc)
                probe = None
            if probe is not None:
                dedup_key = (probe.role, probe.name or "")
                if probe.name and seen[dedup_key] >= _MAX_REPETITIVE_ELEMENTS:
                    dropped += 1
                else:
                    if probe.name:
                        seen[dedup_key] += 1
                    ref = f"e{len(elements) + 1}"
                    elements.append(
                        Element(
                            ref=ref,
                            role=probe.role,
                            title=probe.name,
                            value=probe.value,
                            enabled=probe.enabled,
                            focused=probe.focused,
                            editable=probe.editable,
                            bounds=probe.bounds,
                            actions=probe.actions,
                        )
                    )
                    self._element_handles[(observation_id, ref)] = node
                    if probe.focused and focused_ref is None:
                        focused_ref = ref
            try:
                children = tuple(self._backend.children(node))
            except Exception as exc:
                logger.debug("observe: children lookup failed: %s", exc)
                children = ()
            if depth + 1 > self._max_depth:
                if children:
                    truncated = True
                continue
            for child in children:
                queue.append((child, depth + 1))

        stats = ElementStats(
            observed=visited,
            returned=len(elements),
            editable_count=sum(1 for item in elements if item.editable),
            actionable_count=sum(1 for item in elements if item.actions),
            repetitive_elements_dropped=dropped,
        )
        return tuple(elements), stats, focused_ref, truncated

    def _attach_screenshot(self, observation: Observation) -> Observation:
        try:
            payload = self._backend.capture_primary_screen_png()
        except Exception as exc:
            logger.warning("observe: screenshot failed: %s", exc)
            return observation
        if not payload:
            return observation
        try:
            directory = self.screenshot_dir
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{observation.id}.png"
            path.write_bytes(payload)
        except OSError as exc:
            logger.warning("observe: screenshot save failed: %s", exc)
            return observation
        return observation.model_copy(update={"screenshot_ref": path.name})

    def _remember_snapshot(self, observation: Observation) -> None:
        self._snapshot = observation
        current = observation.id
        self._element_handles = {
            key: value
            for key, value in self._element_handles.items()
            if key[0] == current
        }
        self._window_handles = {
            key: value
            for key, value in self._window_handles.items()
            if key[0] == current
        }
        session = self._sessions.get_active()
        if session is not None:
            session.attach_snapshot(observation)
            if observation.target is not None and session.target_app is None:
                session.begin_target(observation.target)

    def _clear_snapshot(self) -> None:
        self._snapshot = None
        self._element_handles.clear()
        self._window_handles.clear()

    def _consume_snapshot(self) -> None:
        """动作产生 UI 副作用后使 Observation 失效（强制 re-observe）。"""

        self._clear_snapshot()
        session = self._sessions.get_active()
        if session is not None:
            session.invalidate_snapshot()

    # ------------------------------------------------------------------
    # 动作分派
    # ------------------------------------------------------------------

    def _click_sync(
        self, target: ElementTarget | CoordinateTarget
    ) -> ActionResult:
        snapshot = self._snapshot
        if snapshot is None or snapshot.id != target.observation_id:
            return self._failure(
                ActionName.CLICK,
                observation_id=target.observation_id,
                error=(
                    "stale_observation: observation_id does not match the "
                    "latest observation; re-observe before acting"
                ),
            )
        if isinstance(target, ElementTarget):
            element = next(
                (
                    item
                    for item in snapshot.elements
                    if item.ref == target.element_ref
                ),
                None,
            )
            handle = self._element_handles.get(
                (target.observation_id, target.element_ref)
            )
            if element is None or handle is None:
                return self._failure(
                    ActionName.CLICK,
                    observation_id=target.observation_id,
                    error=(
                        "element_not_found: element ref is not part of the "
                        f"latest observation: {target.element_ref}"
                    ),
                )
            result = self._dispatch_element_click(
                target.observation_id, element, handle
            )
        else:
            result = self._dispatch_coordinate_click(target)
        self._consume_snapshot()
        return result

    def _dispatch_element_click(
        self,
        observation_id: str,
        element: Element,
        handle: Any,
    ) -> ActionResult:
        try:
            if "press" in element.actions:
                self._backend.invoke(handle)
                return self._delivered(
                    ActionName.CLICK,
                    observation_id=observation_id,
                    method="invoke_pattern",
                )
            if "select" in element.actions:
                self._backend.select(handle)
                return self._delivered(
                    ActionName.CLICK,
                    observation_id=observation_id,
                    method="selection_item_pattern",
                )
            if "toggle" in element.actions:
                self._backend.toggle(handle)
                return self._delivered(
                    ActionName.CLICK,
                    observation_id=observation_id,
                    method="toggle_pattern",
                )
            point = self._element_click_point(element, handle)
            if point is None:
                return self._failure(
                    ActionName.CLICK,
                    observation_id=observation_id,
                    error=(
                        "no_action_available: element exposes neither a UIA "
                        "pattern nor a clickable point"
                    ),
                )
            self._backend.click_at(*point)
            return self._delivered(
                ActionName.CLICK,
                observation_id=observation_id,
                method="coordinate_fallback",
                execution_mode="foreground_fallback",
                metadata={"x": point[0], "y": point[1]},
            )
        except Exception as exc:
            return self._failure(
                ActionName.CLICK,
                observation_id=observation_id,
                error=f"action_failed: {type(exc).__name__}: {exc}",
            )

    def _element_click_point(
        self, element: Element, handle: Any
    ) -> tuple[int, int] | None:
        try:
            point = self._backend.clickable_point(handle)
        except Exception:
            point = None
        if point is not None:
            return point
        bounds = element.bounds
        if bounds is None:
            return None
        return (
            bounds.x + bounds.width // 2,
            bounds.y + bounds.height // 2,
        )

    def _dispatch_coordinate_click(
        self, target: CoordinateTarget
    ) -> ActionResult:
        try:
            self._backend.click_at(target.x, target.y)
        except Exception as exc:
            return self._failure(
                ActionName.CLICK,
                observation_id=target.observation_id,
                error=f"action_failed: {type(exc).__name__}: {exc}",
            )
        return self._delivered(
            ActionName.CLICK,
            observation_id=target.observation_id,
            method="coordinate",
            execution_mode="foreground_fallback",
            metadata={"x": target.x, "y": target.y},
        )

    def _type_sync(
        self, text: str, element_ref: str | None
    ) -> ActionResult:
        try:
            snapshot, element, handle = (
                self._lookup_element(element_ref)
                if element_ref is not None
                else self._focused_element()
            )
        except _ActionTargetError as exc:
            return self._failure(
                ActionName.TYPE,
                observation_id=self._snapshot.id if self._snapshot else None,
                error=str(exc),
            )
        if not element.editable:
            return self._failure(
                ActionName.TYPE,
                observation_id=snapshot.id,
                error=(
                    "element_not_editable: expected text_area/text_field/"
                    f"combo_box with set_value, got role={element.role!r}"
                ),
            )
        try:
            self._backend.set_value(handle, text)
        except Exception as exc:
            return self._failure(
                ActionName.TYPE,
                observation_id=snapshot.id,
                error=f"action_failed: {type(exc).__name__}: {exc}",
            )
        verified = self._verify_text_value(handle, text)
        result = ActionResult(
            success=verified,
            action=ActionName.TYPE,
            observation_id=snapshot.id,
            delivery_status=DeliveryStatus.DELIVERED,
            verification_status=(
                VerificationStatus.VERIFIED
                if verified
                else VerificationStatus.MISMATCH
            ),
            method="value_pattern",
            error=(
                None
                if verified
                else "input_effect_mismatch: value did not change after SetValue"
            ),
        )
        self._consume_snapshot()
        return result

    def _verify_text_value(self, handle: Any, text: str) -> bool:
        expected = uia_mapping.normalize_text(text.replace("\r\n", "\n"))
        for attempt in range(3):
            probe: ElementProbe | None
            try:
                probe = self._backend.probe(handle)
            except Exception:
                probe = None
            if probe is not None and probe.value is not None:
                actual = uia_mapping.normalize_text(
                    probe.value.replace("\r\n", "\n")
                )
                if actual == expected:
                    return True
            if attempt < 2:
                time.sleep(0.05)
        return False

    def _key_sync(
        self,
        key: str,
        modifiers: tuple[str, ...],
        element_ref: str | None,
    ) -> ActionResult:
        try:
            virtual_key = uia_mapping.parse_key(key)
            modifier_codes = uia_mapping.parse_modifiers(modifiers)
        except ValueError as exc:
            return self._failure(ActionName.KEY, error=f"invalid_key: {exc}")

        observation_id: str | None = None
        if element_ref is not None:
            try:
                snapshot, _element, handle = self._lookup_element(element_ref)
            except _ActionTargetError as exc:
                return self._failure(
                    ActionName.KEY,
                    observation_id=(
                        self._snapshot.id if self._snapshot else None
                    ),
                    error=str(exc),
                )
            observation_id = snapshot.id
            try:
                self._backend.focus(handle)
            except Exception as exc:
                return self._failure(
                    ActionName.KEY,
                    observation_id=observation_id,
                    error=(
                        f"action_failed: focus: {type(exc).__name__}: {exc}"
                    ),
                )
        try:
            self._backend.send_key_chord(virtual_key, modifier_codes)
        except Exception as exc:
            return self._failure(
                ActionName.KEY,
                observation_id=observation_id,
                error=f"action_failed: {type(exc).__name__}: {exc}",
            )
        if element_ref is not None:
            self._consume_snapshot()
        return self._delivered(
            ActionName.KEY,
            observation_id=observation_id,
            method="sendinput",
            verification=VerificationStatus.NOT_APPLICABLE,
            metadata={"key": key, "modifiers": tuple(modifiers)},
        )

    def _scroll_sync(self, delta_x: int, delta_y: int) -> ActionResult:
        try:
            self._backend.scroll_wheel(delta_x, delta_y)
        except Exception as exc:
            return self._failure(
                ActionName.SCROLL,
                error=f"action_failed: {type(exc).__name__}: {exc}",
            )
        return self._delivered(
            ActionName.SCROLL,
            method="sendinput_wheel",
            verification=VerificationStatus.NOT_APPLICABLE,
            metadata={"delta_x": delta_x, "delta_y": delta_y},
        )

    def _focus_window_sync(self, window_ref: str) -> ActionResult:
        snapshot = self._snapshot
        hwnd = (
            self._window_handles.get((snapshot.id, window_ref))
            if snapshot is not None
            else None
        )
        if snapshot is None or hwnd is None:
            return self._failure(
                ActionName.FOCUS_WINDOW,
                error=(
                    "stale_observation: window_ref is not part of the latest "
                    f"observation: {window_ref}"
                ),
            )
        try:
            focused = self._backend.set_foreground_window(hwnd)
        except Exception as exc:
            return self._failure(
                ActionName.FOCUS_WINDOW,
                observation_id=snapshot.id,
                error=f"action_failed: {type(exc).__name__}: {exc}",
            )
        if not focused:
            return self._failure(
                ActionName.FOCUS_WINDOW,
                observation_id=snapshot.id,
                error="focus_failed: SetForegroundWindow returned False",
            )
        self._consume_snapshot()
        return self._delivered(
            ActionName.FOCUS_WINDOW,
            observation_id=snapshot.id,
            method="set_foreground_window",
            verification=VerificationStatus.NOT_APPLICABLE,
            metadata={"window_ref": window_ref},
        )

    def _open_app_sync(self, app: str) -> ActionResult:
        resolver = self._app_resolver or resolve_app_executable
        resolved: str | Path | None
        try:
            resolved = resolver(app)
        except Exception as exc:
            logger.warning("open_app: resolution failed: %s", exc)
            resolved = None

        name = app
        pid: int | None = None
        if resolved is not None:
            executable = str(resolved)
            name = Path(executable).stem or app
            launcher = self._app_launcher or _launch_executable
            try:
                pid = launcher(executable)
            except Exception as exc:
                return self._failure(
                    ActionName.OPEN_APP,
                    error=f"open_app_failed: {type(exc).__name__}: {exc}",
                )
            method = "popen"
        else:
            opener = self._app_opener or _shell_open
            try:
                opener(app)
            except Exception as exc:
                return self._failure(
                    ActionName.OPEN_APP,
                    error=f"open_app_failed: {type(exc).__name__}: {exc}",
                )
            method = "shell_startfile"

        session = self._sessions.get_active()
        if session is not None:
            try:
                session.begin_target(ActiveApp(name=name, pid=pid))
            except Exception as exc:
                logger.warning("open_app: cannot bind session target: %s", exc)
        self._consume_snapshot()
        return self._delivered(
            ActionName.OPEN_APP,
            method=method,
            verification=VerificationStatus.NOT_APPLICABLE,
            metadata={"app": app, "pid": pid},
        )

    # ------------------------------------------------------------------
    # 目标解析 / 结果构造
    # ------------------------------------------------------------------

    def _lookup_element(
        self, element_ref: str
    ) -> tuple[Observation, Element, Any]:
        snapshot = self._snapshot
        if snapshot is None:
            raise _ActionTargetError(
                "stale_observation",
                "no fresh observation; call computer_observe first",
            )
        element = next(
            (item for item in snapshot.elements if item.ref == element_ref),
            None,
        )
        handle = self._element_handles.get((snapshot.id, element_ref))
        if element is None or handle is None:
            raise _ActionTargetError(
                "element_not_found",
                f"element ref is not part of the latest observation: {element_ref}",
            )
        return snapshot, element, handle

    def _focused_element(self) -> tuple[Observation, Element, Any]:
        snapshot = self._snapshot
        if snapshot is None:
            raise _ActionTargetError(
                "stale_observation",
                "no fresh observation; call computer_observe first",
            )
        if not snapshot.focused_element_ref:
            raise _ActionTargetError(
                "focused_element_not_editable",
                "no focused editable element in the latest observation; "
                "re-observe and pass element_ref",
            )
        return self._lookup_element(snapshot.focused_element_ref)

    def _delivered(
        self,
        action: ActionName,
        *,
        observation_id: str | None = None,
        method: str | None = None,
        execution_mode: str | None = None,
        verification: VerificationStatus = VerificationStatus.UNVERIFIED,
        metadata: dict[str, Any] | None = None,
    ) -> ActionResult:
        return ActionResult(
            success=True,
            action=action,
            observation_id=observation_id,
            delivery_status=DeliveryStatus.DELIVERED,
            verification_status=verification,
            method=method,
            execution_mode=execution_mode,
            metadata=metadata or {},
        )

    def _failure(
        self,
        action: ActionName,
        *,
        observation_id: str | None = None,
        error: str,
        metadata: dict[str, Any] | None = None,
    ) -> ActionResult:
        return ActionResult(
            success=False,
            action=action,
            observation_id=observation_id,
            error=error,
            metadata=metadata or {},
        )


# ---------------------------------------------------------------------------
# open_app 辅助
# ---------------------------------------------------------------------------


def resolve_app_executable(app: str) -> Path | None:
    """解析应用名 → 可执行文件：绝对路径 / PATH / App Paths 注册表。"""

    candidate = Path(app).expanduser()
    has_separator = any(
        separator and separator in app
        for separator in (os.sep, os.altsep)
    )
    if candidate.is_absolute() or has_separator:
        return candidate if candidate.is_file() else None

    names = [app] if app.lower().endswith(".exe") else [f"{app}.exe", app]
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)

    if sys.platform != "win32":  # pragma: no cover - 产品仅面向 Windows
        return None
    import winreg

    key_name = app if app.lower().endswith(".exe") else f"{app}.exe"
    sub_key = (
        "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" + key_name
    )
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, sub_key) as key:
                value, _kind = winreg.QueryValueEx(key, None)
        except OSError:
            continue
        if value and Path(value).is_file():
            return Path(value)
    return None


def _launch_executable(executable: str) -> int | None:
    """启动可执行文件并返回 pid（失败抛 OSError）。"""

    process = subprocess.Popen([executable])  # noqa: S603 - 已解析的绝对路径
    return process.pid


def _shell_open(app: str) -> None:
    """Shell 回退（os.startfile）；非 Windows 平台不支持。"""

    starter = getattr(os, "startfile", None)
    if starter is None:  # pragma: no cover - 产品仅面向 Windows
        raise OSError("open_app fallback requires Windows")
    starter(app)


# ---------------------------------------------------------------------------
# 真实 Win32 / UIA 后端
# ---------------------------------------------------------------------------


def _user32():  # pragma: no cover - 仅在 Windows 上执行
    return ctypes.WinDLL("user32", use_last_error=True)


def _kernel32():  # pragma: no cover - 仅在 Windows 上执行
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _window_ref(hwnd: int) -> str:
    return f"w{hwnd:08x}"


def _window_pid(hwnd: int) -> int | None:  # pragma: no cover - 仅在 Windows 上执行
    pid = wintypes.DWORD()
    _user32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value) or None


def _window_bounds(hwnd: int) -> Bounds | None:  # pragma: no cover
    rect = wintypes.RECT()
    try:
        dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        result = dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd),
            wintypes.DWORD(9),  # DWMWA_EXTENDED_FRAME_BOUNDS
            ctypes.byref(rect),
            ctypes.sizeof(rect),
        )
    except OSError:
        result = 1
    if result != 0:
        if not _user32().GetWindowRect(
            wintypes.HWND(hwnd), ctypes.byref(rect)
        ):
            return None
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width <= 0 or height <= 0:
        return None
    return Bounds(
        x=int(rect.left), y=int(rect.top), width=width, height=height
    )


def _safe(call: Callable[[], Any], default: Any = None) -> Any:
    try:
        return call()
    except Exception:
        return default


def _probe_bounds(raw: Any) -> Bounds | None:
    if raw is None:
        return None
    try:
        left = int(raw.left)
        top = int(raw.top)
        right = int(raw.right)
        bottom = int(raw.bottom)
    except (AttributeError, TypeError, ValueError):
        return None
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        return None
    return Bounds(x=left, y=top, width=width, height=height)


class Win32DesktopBackend:
    """真实系统后端：Win32 窗口 + UIAutomation 树 + SendInput + Pillow。"""

    def __init__(self) -> None:
        self._uia: Any | None = None
        self._uia_unavailable = False
        self._user32 = None
        self._kernel32 = None

    # ------------------------------------------------------------------
    # 进程 / 窗口
    # ------------------------------------------------------------------

    def foreground(self) -> ForegroundInfo:  # pragma: no cover - Windows
        user32 = self._user32_api()
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            return ForegroundInfo(name=None, pid=None, hwnd=None)
        pid = _window_pid(hwnd)
        name = self.process_name(pid) if pid is not None else None
        return ForegroundInfo(name=name, pid=pid, hwnd=hwnd)

    def process_name(self, pid: int) -> str | None:  # pragma: no cover - Windows
        kernel32 = self._kernel32_api()
        process = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED
        if not process:
            return None
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buffer))
            if not kernel32.QueryFullProcessImageNameW(
                process, 0, buffer, ctypes.byref(size)
            ):
                return None
            name = Path(buffer.value).stem
            return name or None
        finally:
            kernel32.CloseHandle(process)

    def windows_for_process(
        self, pid: int
    ) -> tuple[WindowInfo, ...]:  # pragma: no cover - Windows
        user32 = self._user32_api()
        results: list[WindowInfo] = []
        callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )

        def callback(hwnd, _lparam):  # noqa: ANN001 - ctypes 回调
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                if user32.GetWindow(hwnd, 4):  # GW_OWNER：跳过 owned popup
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                if _window_pid(int(hwnd)) != pid:
                    return True
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                bounds = _window_bounds(int(hwnd))
                if bounds is None:
                    return True
                results.append(
                    WindowInfo(
                        hwnd=int(hwnd),
                        title=buffer.value.strip(),
                        bounds=bounds,
                    )
                )
            except Exception:
                # 单个窗口探测失败不应打断整次枚举。
                return True
            return True

        user32.EnumWindows(callback_type(callback), 0)
        return tuple(results)

    def set_foreground_window(
        self, hwnd: int
    ) -> bool:  # pragma: no cover - Windows
        user32 = self._user32_api()
        if user32.IsIconic(wintypes.HWND(hwnd)):
            user32.ShowWindow(wintypes.HWND(hwnd), 9)  # SW_RESTORE
        return bool(user32.SetForegroundWindow(wintypes.HWND(hwnd)))

    # ------------------------------------------------------------------
    # UIA 树
    # ------------------------------------------------------------------

    def _uia_module(self) -> Any | None:
        if self._uia is None and not self._uia_unavailable:
            try:
                import uiautomation

                uiautomation.SetGlobalSearchTimeout(1.5)
                self._uia = uiautomation
            except Exception as exc:
                self._uia_unavailable = True
                logger.warning("uiautomation unavailable: %s", exc)
        return self._uia

    def window_handle(self, hwnd: int) -> Any | None:
        uia = self._uia_module()
        if uia is None:
            return None
        return _safe(lambda: uia.ControlFromHandle(hwnd))

    def children(self, node: Any) -> tuple[Any, ...]:
        result = _safe(lambda: node.GetChildren())
        return tuple(result or ())

    def probe(self, node: Any) -> ElementProbe | None:
        uia = self._uia_module()
        if uia is None:
            return None
        control_type = _safe(lambda: node.ControlTypeName) or ""
        support = self._pattern_support(node, uia)
        return ElementProbe(
            role=uia_mapping.element_role(control_type),
            name=_safe(lambda: node.Name) or None,
            value=self._read_value(node, uia, support),
            enabled=bool(_safe(lambda: node.IsEnabled, True)),
            focused=bool(_safe(lambda: node.HasKeyboardFocus, False)),
            editable=uia_mapping.is_editable(control_type, support),
            bounds=_probe_bounds(_safe(lambda: node.BoundingRectangle)),
            actions=uia_mapping.element_actions(support),
        )

    def _pattern_support(
        self, node: Any, uia: Any
    ) -> uia_mapping.PatternSupport:
        ids = uia.PatternId

        def has(pattern_id: int) -> bool:
            return _safe(lambda: node.GetPattern(pattern_id)) is not None

        value_support = has(ids.ValuePattern)
        read_only = False
        if value_support:
            def read_read_only() -> bool:
                pattern = node.GetPattern(ids.ValuePattern)
                return bool(pattern.IsReadOnly) if pattern is not None else True

            read_only = bool(_safe(read_read_only, True))
        return uia_mapping.PatternSupport(
            invoke=has(ids.InvokePattern),
            value=value_support,
            value_read_only=read_only,
            selection_item=has(ids.SelectionItemPattern),
            expand_collapse=has(ids.ExpandCollapsePattern),
            toggle=has(ids.TogglePattern),
            scroll_item=has(ids.ScrollItemPattern),
        )

    def _read_value(
        self,
        node: Any,
        uia: Any,
        support: uia_mapping.PatternSupport,
    ) -> str | None:
        if not support.value:
            return None
        if bool(_safe(lambda: node.IsPassword, False)):
            return None

        def read() -> str | None:
            pattern = node.GetPattern(uia.PatternId.ValuePattern)
            if pattern is None:
                return None
            return pattern.Value

        return uia_mapping.normalize_text(_safe(read))

    # ------------------------------------------------------------------
    # 动作原语
    # ------------------------------------------------------------------

    def invoke(self, node: Any) -> None:
        uia = self._require_uia()
        pattern = node.GetPattern(uia.PatternId.InvokePattern)
        if pattern is None:
            raise RuntimeError("InvokePattern unavailable")
        pattern.Invoke()

    def select(self, node: Any) -> None:
        uia = self._require_uia()
        pattern = node.GetPattern(uia.PatternId.SelectionItemPattern)
        if pattern is None:
            raise RuntimeError("SelectionItemPattern unavailable")
        pattern.Select()

    def toggle(self, node: Any) -> None:
        uia = self._require_uia()
        pattern = node.GetPattern(uia.PatternId.TogglePattern)
        if pattern is None:
            raise RuntimeError("TogglePattern unavailable")
        pattern.Toggle()

    def set_value(self, node: Any, text: str) -> None:
        uia = self._require_uia()
        pattern = node.GetPattern(uia.PatternId.ValuePattern)
        if pattern is None:
            raise RuntimeError("ValuePattern unavailable")
        pattern.SetValue(text)

    def focus(self, node: Any) -> None:
        node.SetFocus()

    def clickable_point(self, node: Any) -> tuple[int, int] | None:
        result = _safe(lambda: node.GetClickablePoint())
        if not result or len(result) < 3 or not result[2]:
            return None
        return int(result[0]), int(result[1])

    def _require_uia(self) -> Any:
        uia = self._uia_module()
        if uia is None:
            raise RuntimeError("uiautomation unavailable")
        return uia

    def click_at(self, x: int, y: int) -> None:  # pragma: no cover - Windows
        screen_width = self._user32_api().GetSystemMetrics(0)
        screen_height = self._user32_api().GetSystemMetrics(1)
        normalized_x = int(x * 65535 / max(screen_width - 1, 1))
        normalized_y = int(y * 65535 / max(screen_height - 1, 1))
        self._send_input_mouse(
            _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_LEFTDOWN,
            normalized_x,
            normalized_y,
            0,
        )
        self._send_input_mouse(
            _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_LEFTUP,
            normalized_x,
            normalized_y,
            0,
        )

    def scroll_wheel(
        self, delta_x: int, delta_y: int
    ) -> None:  # pragma: no cover - Windows
        if delta_y:
            # Windows：mouseData 正值 = 滚轮向前（内容上移）；约定 delta_y>0 = 向下。
            self._send_input_mouse(
                _MOUSEEVENTF_WHEEL, 0, 0, -int(delta_y) * _WHEEL_DELTA
            )
        if delta_x:
            self._send_input_mouse(
                _MOUSEEVENTF_HWHEEL, 0, 0, int(delta_x) * _WHEEL_DELTA
            )

    def send_key_chord(
        self, virtual_key: int, modifiers: tuple[int, ...]
    ) -> None:  # pragma: no cover - Windows
        events: list[_INPUT] = []
        for modifier in modifiers:
            events.append(_key_input(modifier, key_up=False))
        events.append(_key_input(virtual_key, key_up=False))
        events.append(_key_input(virtual_key, key_up=True))
        for modifier in reversed(modifiers):
            events.append(_key_input(modifier, key_up=True))
        self._send_inputs(events)

    def _send_input_mouse(
        self, flags: int, dx: int, dy: int, mouse_data: int
    ) -> None:  # pragma: no cover - Windows
        event = _INPUT()
        event.type = _INPUT_MOUSE
        event.mi.dx = dx
        event.mi.dy = dy
        event.mi.mouseData = mouse_data & 0xFFFFFFFF
        event.mi.dwFlags = flags
        self._send_inputs([event])

    def _send_inputs(self, events: list[_INPUT]) -> None:  # pragma: no cover
        if not events:
            return
        array = (_INPUT * len(events))(*events)
        sent = self._user32_api().SendInput(
            len(events), ctypes.byref(array), ctypes.sizeof(_INPUT)
        )
        if sent != len(events):
            raise RuntimeError(
                f"SendInput delivered {sent}/{len(events)} events"
            )

    # ------------------------------------------------------------------
    # 截图
    # ------------------------------------------------------------------

    def capture_primary_screen_png(self) -> bytes | None:
        try:
            from PIL import ImageGrab
        except Exception as exc:
            logger.warning("Pillow unavailable: %s", exc)
            return None
        try:
            import io

            image = ImageGrab.grab()  # 默认仅主屏
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()
        except Exception as exc:
            logger.warning("primary screen grab failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # 懒加载句柄
    # ------------------------------------------------------------------

    def _user32_api(self):  # pragma: no cover - Windows
        if self._user32 is None:
            self._user32 = _user32()
        return self._user32

    def _kernel32_api(self):  # pragma: no cover - Windows
        if self._kernel32 is None:
            self._kernel32 = _kernel32()
        return self._kernel32


# ---------------------------------------------------------------------------
# SendInput 结构（模块级，供 backend 使用）
# ---------------------------------------------------------------------------

_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_WHEEL = 0x0800
_MOUSEEVENTF_HWHEEL = 0x1000
_MOUSEEVENTF_ABSOLUTE = 0x8000
_WHEEL_DELTA = 120


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


def _key_input(virtual_key: int, *, key_up: bool) -> _INPUT:
    event = _INPUT()
    event.type = _INPUT_KEYBOARD
    event.ki.wVk = virtual_key
    event.ki.dwFlags = _KEYEVENTF_KEYUP if key_up else 0
    return event
