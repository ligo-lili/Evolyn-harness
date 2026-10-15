"""Computer bootstrap 测试：开关 / 依赖缺失降级 / 平台判定 / 构建真实 runtime。"""

from __future__ import annotations

import sys

import pytest

from app.computer import bootstrap
from app.computer.bootstrap import (
    build_computer,
    computer_enabled,
    current_platform,
    missing_dependencies,
)


def test_disabled_does_not_build_runtime() -> None:
    runtime, status = build_computer(enabled=False)
    assert runtime is None
    assert status.enabled is False
    assert status.available is False
    assert status.reason == "disabled"


def test_disabled_via_env(monkeypatch) -> None:
    monkeypatch.setenv("EVOLYN_COMPUTER_ENABLED", "0")
    runtime, status = build_computer()
    assert runtime is None
    assert status.reason == "disabled"


def test_unsupported_platform_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    runtime, status = build_computer()
    assert runtime is None
    assert status.enabled is True
    assert status.available is False
    assert status.reason == "unsupported_platform"
    assert status.platform == "linux"


def test_dependencies_missing_degrades(monkeypatch) -> None:
    monkeypatch.setattr(
        bootstrap, "missing_dependencies", lambda: ("uiautomation",)
    )
    runtime, status = build_computer()
    assert runtime is None
    assert status.enabled is True
    assert status.available is False
    assert status.reason == "dependencies_missing"


def test_missing_dependencies_reports_both_modules(monkeypatch) -> None:
    monkeypatch.setattr(
        bootstrap.importlib.util, "find_spec", lambda name: None
    )
    assert missing_dependencies() == ("uiautomation", "PIL")


def test_build_runtime_when_available(monkeypatch) -> None:
    if sys.platform != "win32":
        pytest.skip("Windows-only runtime")
    # 本测试聚焦 wiring：依赖探测替换为空，真实构造 WindowsComputerRuntime。
    monkeypatch.setattr(bootstrap, "missing_dependencies", lambda: ())
    runtime, status = build_computer()
    assert runtime is not None
    assert status.enabled is True
    assert status.available is True
    assert status.reason is None
    assert status.runtime == "windows"
    assert status.platform == "windows"
    assert status.helper_path is None

    from app.computer.windows import WindowsComputerRuntime

    assert isinstance(runtime, WindowsComputerRuntime)


def test_computer_enabled_switch_and_env(monkeypatch) -> None:
    monkeypatch.delenv("EVOLYN_COMPUTER_ENABLED", raising=False)
    assert computer_enabled() is True
    assert computer_enabled(enabled=False) is False
    assert computer_enabled(enabled=True) is True

    monkeypatch.setenv("EVOLYN_COMPUTER_ENABLED", "false")
    assert computer_enabled() is False
    monkeypatch.setenv("EVOLYN_COMPUTER_ENABLED", "true")
    assert computer_enabled() is True


def test_current_platform_reports(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert current_platform() == "windows"
    monkeypatch.setattr(sys, "platform", "linux")
    assert current_platform() == "linux"
