"""Cross-run 隔离的 runtime+session 集成测试（Windows runtime + Fake 后端）。

证明：Run A 绑定 Notes 并结束（end_session 清除 target/snapshot）后，
Run B 第一次 observe 使用全新 session，绝不会继承 Notes。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.computer.models import Bounds
from app.computer.windows import (
    ForegroundInfo,
    WindowInfo,
    WindowsComputerRuntime,
)


class FakeDesktop:
    """最小后端 stub：固定前台为 Code，窗口树为空。"""

    def __init__(self) -> None:
        self.foreground_calls = 0

    def foreground(self) -> ForegroundInfo:
        self.foreground_calls += 1
        return ForegroundInfo(name="Code", pid=222, hwnd=0x1234)

    def windows_for_process(self, pid: int) -> tuple[WindowInfo, ...]:
        if pid != 222:
            return ()
        return (
            WindowInfo(
                hwnd=0x1234,
                title="Code - main",
                bounds=Bounds(x=0, y=0, width=800, height=600),
            ),
        )

    def window_handle(self, hwnd: int):
        return None  # 无 UIA 树 → 空元素集

    def capture_primary_screen_png(self) -> bytes | None:
        return None


def _notes_runtime(tmp_path: Path) -> tuple[WindowsComputerRuntime, list[str]]:
    launched: list[str] = []

    def resolver(app: str) -> Path | None:
        return Path("Notes.exe") if app == "Notes" else None

    def launcher(executable: str) -> int:
        launched.append(executable)
        return 111

    runtime = WindowsComputerRuntime(
        backend=FakeDesktop(),
        screenshot_dir=tmp_path,
        app_resolver=resolver,
        app_launcher=launcher,
    )
    return runtime, launched


@pytest.mark.asyncio
async def test_run_a_end_then_run_b_never_inherits_notes(tmp_path) -> None:
    runtime, launched = _notes_runtime(tmp_path)

    # --- Run A：open_app Notes → target = Notes ---
    run_a = runtime.begin_session("run-a")
    result = await runtime.open_app("Notes")
    assert result.success is True
    assert launched == ["Notes.exe"]
    assert run_a.target_app is not None
    assert run_a.target_app.name == "Notes"
    assert run_a.target_app.pid == 111

    # --- Run A 结束：清除 Python 侧 target / snapshot ---
    assert await runtime.end_session("run-a") is True
    assert runtime._sessions.get_active() is None
    assert runtime._snapshot is None
    assert runtime._element_handles == {}
    assert runtime._window_handles == {}

    # --- Run B：全新 session，第一次 observe 绝不继承 Notes ---
    run_b = runtime.begin_session("run-b")
    assert run_b.session_id != run_a.session_id
    assert run_b.target_app is None

    observation = await runtime.observe(include_screenshot=False)
    # 前台是 Code：target 落在 Code，而不是 Run A 的 Notes。
    assert observation.target is not None
    assert observation.target.name == "Code"
    assert observation.target.pid == 222
    assert run_b.target_app is not None
    assert run_b.target_app.name == "Code"

    await runtime.close()


@pytest.mark.asyncio
async def test_end_session_is_idempotent_and_clears_python_state(tmp_path) -> None:
    runtime, _launched = _notes_runtime(tmp_path)
    runtime.begin_session("run-a")
    await runtime.open_app("Notes")
    await runtime.observe(include_screenshot=False)
    assert runtime._snapshot is not None

    assert await runtime.end_session("run-a") is True
    assert runtime._snapshot is None
    assert await runtime.end_session("run-a") is False  # 第二次幂等

    await runtime.close()
