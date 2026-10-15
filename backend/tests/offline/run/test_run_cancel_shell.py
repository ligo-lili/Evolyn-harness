"""Shell cancel：Run 被取消时终止整个进程树，无残留子进程。

覆盖：
- ShellCommandTool.execute 收到 asyncio.CancelledError 时终止进程树并重抛；
- 经 RunManager 取消一个正在执行 shell 工具的 Run 后，无残留进程。

使用真实本地子进程（Python 长睡进程），不调用模型 API。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.agent.runtime import AgentRuntime
from app.checkpoint import SQLiteCheckpointStore
from app.models.adapter import ModelAdapter
from app.models.config import ModelSettings, ProviderConfig
from app.models.registry import ModelAdapterRegistry
from app.models.types import (
    ApiStyle,
    Message,
    MessageRole,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ToolCall,
)
from app.run import RunManager, RunStatus, SQLiteRunStore
from app.tools.approval import AutoApproveGate
from app.tools.builtin.shell import ShellCommandTool
from app.tools.registry import ToolRegistry


class _FakeModelAdapter(ModelAdapter):
    def __init__(
        self,
        config: ProviderConfig,
        responses: Sequence[ModelResponse | Exception],
    ) -> None:
        super().__init__(config)
        self.responses = list(responses)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def close(self) -> None:
        pass


def model_response(
    *,
    content: str | None = None,
    tool_calls: tuple[ToolCall, ...] = (),
    usage: ModelUsage | None = None,
) -> ModelResponse:
    return ModelResponse(
        id="fake-response",
        provider="fake",
        model="fake-model",
        message=Message(
            role=MessageRole.ASSISTANT,
            content=content,
            tool_calls=tool_calls,
        ),
        usage=usage or ModelUsage(),
    )


def fake_registry(
    responses: Sequence[ModelResponse | Exception],
) -> tuple[ModelAdapterRegistry, _FakeModelAdapter]:
    config = ProviderConfig(
        provider="fake",
        model="fake-model",
        api_key=SecretStr("offline-test-key"),
        api_style=ApiStyle.CHAT_COMPLETIONS,
    )
    adapter = _FakeModelAdapter(config, responses)
    registry = ModelAdapterRegistry(ModelSettings(_env_file=None))
    registry.register("fake", lambda _: adapter, config=config)
    return registry, adapter


_WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="shell cancel 验证基于 Windows 进程树"
)

_CHILD_SCRIPT = """\
import pathlib
import sys
import time

pathlib.Path(sys.argv[1]).write_text("started", encoding="utf-8")
time.sleep(300)
"""


def _long_running_shell_setup(tmp_path: Path) -> tuple[str, Path, str]:
    """准备一个长命子进程，返回 (shell 命令, marker 路径, 命令行关键字)。

    子进程由脚本文件启动，脚本名携带唯一关键字；关键字同时出现在
    PowerShell 与子进程的命令行中，用于枚举残留进程（对应原 pgrep -f）。
    """

    token = f"evolyn-cancel-probe-{uuid.uuid4().hex[:12]}"
    script = tmp_path / f"{token}.py"
    script.write_text(_CHILD_SCRIPT, encoding="utf-8")
    marker = tmp_path / "shell-started"
    command = f'& "{sys.executable}" "{script}" "{marker}"'
    return command, marker, token


async def _process_matching(pattern: str) -> bool:
    """是否存在命令行包含 pattern 的进程（Windows 版 ``pgrep -f``）。

    ``Get-CimInstance`` 只能查询命令行；``$PID`` 排除枚举进程自身，
    避免它因为携带 pattern 而误报。
    """

    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.ProcessId -ne $PID -and "
        f"$_.CommandLine -like '*{pattern}*' }} | "
        "Select-Object -ExpandProperty ProcessId"
    )
    process = await asyncio.create_subprocess_exec(
        "powershell",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await asyncio.wait_for(
        process.communicate(), timeout=60
    )
    assert process.returncode == 0, stderr_bytes.decode("utf-8", errors="replace")
    return bool(stdout_bytes.strip())


# ---------------------------------------------------------------------------
# 单元：ShellCommandTool 被 cancel 时清理进程组
# ---------------------------------------------------------------------------


@_WINDOWS_ONLY
@pytest.mark.asyncio
async def test_shell_tool_cancel_terminates_process_group(tmp_path: Path) -> None:
    command, marker, token = _long_running_shell_setup(tmp_path)
    tool = ShellCommandTool(workspace_root=tmp_path)
    task = asyncio.create_task(
        tool.execute(
            {
                "command": command,
                "timeout_seconds": 300,
            }
        )
    )

    # 等子进程真正启动（写 marker 后进入长睡）。
    for _ in range(100):
        if marker.exists():
            break
        await asyncio.sleep(0.05)
    assert marker.exists(), "shell 子进程未启动"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # 进程树应已被终止，无残留子进程。
    await asyncio.sleep(0.2)
    assert await _process_matching(token) is False


# ---------------------------------------------------------------------------
# 集成：经 RunManager 取消正在执行 shell 工具的 Run
# ---------------------------------------------------------------------------


@_WINDOWS_ONLY
@pytest.mark.asyncio
async def test_runmanager_cancel_shell_run_leaves_no_process(
    tmp_path: Path,
) -> None:
    command, marker, token = _long_running_shell_setup(tmp_path)
    call = ToolCall(
        id="shell-1",
        name="run_shell_command",
        arguments={"command": command, "timeout_seconds": 300},
    )
    registry, _ = fake_registry(
        [
            model_response(tool_calls=(call,)),
            model_response(content="完成"),
        ]
    )
    tools = ToolRegistry()
    tools.register(ShellCommandTool(workspace_root=tmp_path))

    database = tmp_path / "evolyn.db"
    run_store = SQLiteRunStore(database)
    checkpoint_store = SQLiteCheckpointStore(database)
    await run_store.initialize()
    await checkpoint_store.initialize()
    runtime = AgentRuntime(
        registry,
        tools,
        provider="fake",
        checkpoint_store=checkpoint_store,
        approval_gate=AutoApproveGate(),
    )
    manager = RunManager(run_store, checkpoint_store, runtime)

    run_id, _ = await manager.start("跑一个长命令", conversation_id="conv-1")
    for _ in range(200):
        if marker.exists():
            break
        await asyncio.sleep(0.05)
    assert marker.exists(), "shell 子进程未启动"

    cancelled = await manager.cancel(run_id)
    assert cancelled.status is RunStatus.CANCELLED
    # checkpoint 保留执行边界（未决工具语义）。
    checkpoint = await checkpoint_store.get(run_id)
    assert checkpoint is not None
    assert checkpoint.status.value == "interrupted"

    await asyncio.sleep(0.2)
    assert await _process_matching(token) is False
