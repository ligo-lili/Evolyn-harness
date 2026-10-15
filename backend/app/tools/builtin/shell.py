"""Shell 命令执行工具（需人工审核）。

危险操作，权限档位为 HUMAN_APPROVAL：模型可以申请调用，
但执行前必须经过 ApprovalGate 的人工确认。

Windows 下 shell 选择 ``pwsh`` → ``powershell`` → ``cmd``（可用
``EVOLYN_SHELL`` 覆盖）；未注入沙箱 Supervisor 时仍走白名单环境变量。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from time import perf_counter
from typing import Any

from app.models.types import ToolDefinition, ToolPermission
from app.sandbox import (
    SandboxConfig,
    SandboxLaunchSpec,
    SandboxNetworkMode,
    SandboxSupervisor,
)

from ..base import BaseTool
from ._workspace import resolve_workspace_path, workspace_root_path

MAX_SHELL_TIMEOUT_SECONDS = 120.0

_ENV_SHELL = "EVOLYN_SHELL"
_WINDOWS_SHELL_CANDIDATES = ("pwsh", "powershell", "cmd")
_WINDOWS_ENVIRONMENT_KEYS = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "USERPROFILE",
    "TEMP",
    "TMP",
    "LOCALAPPDATA",
    "APPDATA",
    "NUMBER_OF_PROCESSORS",
)
_POSIX_ENVIRONMENT_KEYS = ("HOME", "LANG", "LC_ALL", "PATH", "TMPDIR")


class ShellCommandTool(BaseTool):
    def __init__(
        self,
        workspace_root: str | Path | None = None,
        *,
        sandbox_supervisor: SandboxSupervisor | None = None,
    ) -> None:
        self._workspace_root = workspace_root_path(workspace_root)
        self._sandbox_supervisor = sandbox_supervisor

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="run_shell_command",
            description=(
                "Run a shell command inside the local workspace and return "
                "its exit code, stdout, and stderr. Requires human approval."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute.",
                    },
                    "working_directory": {
                        "type": "string",
                        "description": (
                            "Optional subdirectory (relative to the workspace) "
                            "in which to run the command. Defaults to the "
                            "workspace root."
                        ),
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": (
                            "Maximum seconds to wait before terminating. "
                            f"Capped at {MAX_SHELL_TIMEOUT_SECONDS:g}."
                        ),
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            strict=True,
            permission=ToolPermission.HUMAN_APPROVAL,
        )

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("'command' must be a non-empty string")

        cwd = self._workspace_root
        raw_directory = arguments.get("working_directory")
        if raw_directory is not None:
            if not isinstance(raw_directory, str):
                raise ValueError("'working_directory' must be a string")
            cwd = resolve_workspace_path(
                self._workspace_root,
                raw_directory,
                allow_root=True,
            )

        timeout = arguments.get("timeout_seconds", 30.0)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("'timeout_seconds' must be a positive number")
        timeout = min(float(timeout), MAX_SHELL_TIMEOUT_SECONDS)

        shell_command, shell_arguments = _resolve_shell(
            for_sandbox=self._sandbox_supervisor is not None
        )
        environment = _shell_environment()

        started_at = perf_counter()
        timed_out = False
        launch: SandboxLaunchSpec | None = None
        if self._sandbox_supervisor is None:
            process = await asyncio.create_subprocess_exec(
                shell_command,
                *shell_arguments,
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                env=environment,
                start_new_session=True,
            )
        else:
            launch = self._sandbox_supervisor.prepare_launch(
                command=shell_command,
                args=(*shell_arguments, command),
                env=environment,
                cwd=str(cwd),
                config=SandboxConfig(network=SandboxNetworkMode.DENIED),
            )
            process = await asyncio.create_subprocess_exec(
                launch.command,
                *launch.args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=launch.cwd,
                env=launch.env,
                start_new_session=True,
            )
        try:
            async with asyncio.timeout(timeout):
                stdout_bytes, stderr_bytes = await process.communicate()
        except TimeoutError:
            timed_out = True
            _terminate_process(process)
            stdout_bytes, stderr_bytes = await process.communicate()
        except asyncio.CancelledError:
            # Run 被 cancel：立即终止整个进程树，避免残留子进程，再重抛取消。
            _terminate_process(process)
            with suppress(Exception):
                await process.communicate()
            raise

        duration_ms = (perf_counter() - started_at) * 1000
        result: dict[str, Any] = {
            "command": command,
            "working_directory": cwd.as_posix(),
            "shell": shell_command,
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "stdout": stdout_bytes.decode("utf-8", errors="replace"),
            "stderr": stderr_bytes.decode("utf-8", errors="replace"),
            "duration_ms": round(duration_ms, 3),
        }
        if launch is not None:
            result["sandbox"] = {
                "backend": launch.backend,
                "sandboxed": launch.sandboxed,
                "enforcement": list(launch.enforcement),
            }
        return result


def _resolve_shell(*, for_sandbox: bool = False) -> tuple[str, tuple[str, ...]]:
    """按 ``EVOLYN_SHELL`` → pwsh → powershell → cmd 选择 shell。

    ``for_sandbox=True`` 时跳过无法 strict-resolve 的候选：Store 版
    ``pwsh`` 的 App Execution Alias（``WindowsApps\\pwsh.EXE``）不能可靠地
    作为低完整性 Job 的启动目标，此时回退到 System32 下的 ``powershell``。
    返回 ``(executable, 前缀参数)``；执行时在参数末尾追加命令文本。
    """

    if sys.platform != "win32":  # pragma: no cover - 产品仅面向 Windows
        return "sh", ("-c",)
    override = os.environ.get(_ENV_SHELL, "").strip()
    if override:
        return override, _shell_arguments(override)
    for candidate in _WINDOWS_SHELL_CANDIDATES:
        found = shutil.which(candidate)
        if not found:
            continue
        if for_sandbox and not _strictly_resolvable(found):
            continue
        return found, _shell_arguments(found)
    comspec = os.environ.get("COMSPEC", "").strip()
    if comspec:
        return comspec, _shell_arguments(comspec)
    raise RuntimeError("找不到可用的 Windows shell（pwsh / powershell / cmd）")


def _strictly_resolvable(executable: str) -> bool:
    """App Execution Alias 等重解析点 strict resolve 会失败，视为不可靠启动目标。"""

    try:
        Path(executable).resolve(strict=True)
    except OSError:
        return False
    return True


def _shell_arguments(executable: str) -> tuple[str, ...]:
    """按可执行文件名给出去除命令文本的前缀参数。"""

    name = Path(executable).stem.lower()
    if name == "cmd":
        return ("/d", "/s", "/c")
    return ("-NoLogo", "-NoProfile", "-NonInteractive", "-Command")


def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """终止整个进程树，避免遗留子进程。

    沙箱路径杀掉 launcher 进程即可：Job Object（KILL_ON_JOB_CLOSE）会连杀
    整棵低完整性进程树；未沙箱路径用 ``taskkill /T`` 终止进程树。
    """

    try:
        if sys.platform == "win32":
            subprocess.run(  # noqa: S603 - 固定参数数组，不经过 shell
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                timeout=10,
            )
            process.kill()
        elif hasattr(os, "killpg"):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:  # pragma: no cover - 非 POSIX 非 Windows
            process.kill()
    except (ProcessLookupError, PermissionError):
        pass


def _shell_environment() -> dict[str, str]:
    """Shell 只继承执行所需环境，不向命令暴露 Provider 密钥。"""

    keys = (
        _WINDOWS_ENVIRONMENT_KEYS
        if sys.platform == "win32"
        else _POSIX_ENVIRONMENT_KEYS
    )
    return {key: os.environ[key] for key in keys if os.environ.get(key)}
