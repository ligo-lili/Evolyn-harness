"""Windows Computer bootstrap：构建真实 WindowsComputerRuntime。

只支持 Windows。依赖缺失（uiautomation / Pillow 未安装）或显式 disable
都**不**应影响 Host 启动（Chat / Run / Automation 照常工作），
只是 Computer 不可用。

依赖探测只用 ``importlib.util.find_spec``（懒导入）：Host 启动不
import 重量级 UIA / 截图库，只有真正构建 Runtime 时才加载。
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from dataclasses import dataclass

from .runtime import ComputerRuntime

logger = logging.getLogger("evolyn.computer.bootstrap")

ENV_COMPUTER_ENABLED = "EVOLYN_COMPUTER_ENABLED"

# Windows Computer Runtime 的可选依赖（缺一即降级）。
_REQUIRED_MODULES = ("uiautomation", "PIL")


@dataclass(frozen=True, slots=True)
class ComputerHostStatus:
    """Computer Host 的轻量状态（不持久化，仅供 UI / 日志）。"""

    enabled: bool
    available: bool
    platform: str
    reason: str | None = None
    helper_path: str | None = None
    runtime: str | None = None


def current_platform() -> str:
    return "windows" if sys.platform == "win32" else sys.platform


def computer_enabled(enabled: bool | None = None) -> bool:
    """Computer 开关：显式参数 > EVOLYN_COMPUTER_ENABLED > 默认启用。

    默认启用（是否真正可用由 ``build_computer`` 判定，不会因为
    平台不同而把用户显式启用的意图误判为 disabled）。
    """

    if enabled is not None:
        return enabled
    raw = os.environ.get(ENV_COMPUTER_ENABLED, "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return True


def missing_dependencies() -> tuple[str, ...]:
    """返回缺失的可选依赖模块名（空元组 = 依赖齐全）。"""

    return tuple(
        name for name in _REQUIRED_MODULES if importlib.util.find_spec(name) is None
    )


def build_computer(
    *, enabled: bool | None = None
) -> tuple[ComputerRuntime | None, ComputerHostStatus]:
    """构建真实 WindowsComputerRuntime；不可用时返回 (None, unavailable status)。"""

    platform = current_platform()
    is_enabled = computer_enabled(enabled)

    if not is_enabled:
        status = ComputerHostStatus(
            enabled=False,
            available=False,
            platform=platform,
            reason="disabled",
        )
        logger.info("Computer disabled; %s", status)
        return None, status

    if platform != "windows":
        status = ComputerHostStatus(
            enabled=True,
            available=False,
            platform=platform,
            reason="unsupported_platform",
        )
        logger.info("Computer unavailable on platform %s", platform)
        return None, status

    missing = missing_dependencies()
    if missing:
        status = ComputerHostStatus(
            enabled=True,
            available=False,
            platform=platform,
            reason="dependencies_missing",
        )
        logger.info(
            "Computer dependencies missing (%s); Host continues without computer",
            ", ".join(missing),
        )
        return None, status

    from .windows import WindowsComputerRuntime

    runtime = WindowsComputerRuntime()
    status = ComputerHostStatus(
        enabled=True,
        available=True,
        platform=platform,
        reason=None,
        runtime="windows",
    )
    logger.info("Computer Runtime: Windows available")
    return runtime, status


__all__ = [
    "ComputerHostStatus",
    "build_computer",
    "computer_enabled",
    "current_platform",
    "missing_dependencies",
]
