"""Computer Runtime：目标绑定的屏幕观察、交互与 Windows 原生实现。

本轮提供：
- ``models``：Observation / ElementTarget / CoordinateTarget / ActionResult；
- ``runtime``：ComputerRuntime 异步接口（Protocol）；
- ``fake``：FakeComputerRuntime（供 Agent Tool 离线单测使用）；
- ``tools``：computer_observe / click / type / key / scroll / open_app /
  focus_window 七个 Agent 工具 + ``register_computer_tools``；
- ``windows``：WindowsComputerRuntime（UIAutomation / Pillow / SendInput）。

``windows`` 依赖 uiautomation / Pillow 为可选组件：只有真正构建 Runtime
时才导入（见 ``bootstrap.build_computer``），缺依赖不影响 Host 启动。
"""

from .bootstrap import (
    ComputerHostStatus,
    build_computer,
    computer_enabled,
    current_platform,
    missing_dependencies,
)
from .fake import FakeComputerRuntime, default_observation
from .lease import (
    ComputerBusyError,
    ComputerLeaseHook,
    ComputerLeaseManager,
    ComputerLeaseSnapshot,
)
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
    Target,
    VerificationStatus,
    Window,
)
from .runtime import ComputerRuntime
from .session import (
    ComputerSession,
    ComputerSessionError,
    ComputerSessionManager,
    ComputerSessionMismatchError,
    ComputerSessionNotActiveError,
)
from .tools import (
    ComputerClickTool,
    ComputerFocusWindowTool,
    ComputerKeyTool,
    ComputerObserveTool,
    ComputerOpenAppTool,
    ComputerScrollTool,
    ComputerTypeTool,
    register_computer_tools,
)

__all__ = [
    "ActionName",
    "ActionResult",
    "ActiveApp",
    "Bounds",
    "ComputerBusyError",
    "ComputerClickTool",
    "ComputerFocusWindowTool",
    "ComputerHostStatus",
    "ComputerKeyTool",
    "ComputerLeaseHook",
    "ComputerLeaseManager",
    "ComputerLeaseSnapshot",
    "ComputerObserveTool",
    "ComputerOpenAppTool",
    "ComputerRuntime",
    "ComputerScrollTool",
    "ComputerSession",
    "ComputerSessionError",
    "ComputerSessionManager",
    "ComputerSessionMismatchError",
    "ComputerSessionNotActiveError",
    "ComputerTypeTool",
    "CoordinateTarget",
    "DeliveryStatus",
    "Element",
    "ElementStats",
    "ElementTarget",
    "FakeComputerRuntime",
    "Observation",
    "Target",
    "VerificationStatus",
    "Window",
    "build_computer",
    "computer_enabled",
    "current_platform",
    "default_observation",
    "missing_dependencies",
    "register_computer_tools",
]
