"""Windows 沙箱执行后端：低完整性令牌写保护 + Job Object 进程树托管。

V1 边界（如实标注，不假装完整隔离）：

- **已强制**：写入保护 —— 低完整性（Low IL）子进程无法创建/修改/删除
  未标注为 Low 的对象（含工作区外的用户文件、`workspace/.git`、`.env`）；
  进程树托管 —— Job Object（KILL_ON_JOB_CLOSE）随调用方退出整树终止。
- **未强制**：网络访问（低完整性不限制 socket）；读隔离（Low IL 不限制读取）；
  ``EVOLYN_SANDBOX_LOW_IL=0`` 时写入保护同样失效。
  未强制项通过 ``SandboxLaunchSpec.enforcement`` 透传给上层与日志。

低完整性令牌由 ``launcher.py`` 在子进程中落实（失败 fail-closed）。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from .backends import SandboxBackend
from .errors import SandboxUnavailableError
from .models import (
    SandboxFilesystemMode,
    SandboxLaunchSpec,
    SandboxPolicy,
)

logger = logging.getLogger("evolyn.sandbox.windows")

BACKEND_NAME = "windows_low_integrity_job"

# 低完整性天然无法强制的维度（V1 如实标注）。
_UNENFORCED_ALWAYS = ("network", "read_isolation")
# 关闭低完整性令牌后，写入保护也失效。
_UNENFORCED_WITHOUT_LOW_IL = "write_protection"

_LAUNCHER_PATH = Path(__file__).resolve().parent / "launcher.py"
_SANDBOX_TMP_DIR = (
    Path(__file__).resolve().parents[2] / ".evolyn" / "tmp" / "sandbox"
)
_ICACLS_TIMEOUT_SECONDS = 30.0

_ENV_LOW_IL = "EVOLYN_SANDBOX_LOW_IL"


def low_integrity_enabled() -> bool:
    """低完整性令牌开关（默认开启；``EVOLYN_SANDBOX_LOW_IL=0`` 降级为仅 Job）。"""

    raw = os.environ.get(_ENV_LOW_IL, "").strip().lower()
    return raw not in ("0", "false", "no", "off")


class WindowsSandboxBackend(SandboxBackend):
    """把统一策略编译为「launcher + Low IL + Job Object」启动规格。"""

    def __init__(self, *, python_executable: str | Path | None = None) -> None:
        self._python_executable = Path(python_executable or sys.executable)
        # 进程内幂等缓存：同一目录只标注一次，避免每次启动都跑 icacls。
        self._labeled_paths: set[Path] = set()

    # ------------------------------------------------------------------
    # SandboxBackend
    # ------------------------------------------------------------------

    def prepare(
        self,
        *,
        command: Path,
        args: tuple[str, ...],
        env: dict[str, str],
        policy: SandboxPolicy,
    ) -> SandboxLaunchSpec:
        if sys.platform != "win32":
            raise SandboxUnavailableError(
                "Windows 沙箱后端只能在 Windows 上运行"
            )
        if not _LAUNCHER_PATH.is_file():
            raise SandboxUnavailableError(
                f"沙箱 launcher 缺失：{_LAUNCHER_PATH}"
            )
        use_low_il = low_integrity_enabled()
        self._prepare_filesystem(policy, low_integrity=use_low_il)

        target_command, target_args = _wrap_script_target(command, args)
        launch_env = dict(env)
        launch_env[_ENV_LOW_IL] = "1" if use_low_il else "0"
        sandbox_tmp = self._ensure_sandbox_tmp() if use_low_il else None
        if sandbox_tmp is not None:
            # 低完整性进程不能写普通 TEMP；重定向到已标注的沙箱临时目录。
            launch_env["TEMP"] = str(sandbox_tmp)
            launch_env["TMP"] = str(sandbox_tmp)

        enforcement = list(_UNENFORCED_ALWAYS)
        if not use_low_il:
            enforcement.append(_UNENFORCED_WITHOUT_LOW_IL)
        logger.warning(
            "sandbox backend %s does not enforce: %s",
            BACKEND_NAME,
            ", ".join(enforcement),
        )
        return SandboxLaunchSpec(
            command=str(self._python_executable),
            args=(
                str(_LAUNCHER_PATH),
                "--",
                str(target_command),
                *target_args,
            ),
            cwd=str(policy.working_directory),
            env=launch_env,
            backend=BACKEND_NAME,
            sandboxed=True,
            enforcement=tuple(enforcement),
        )

    # ------------------------------------------------------------------
    # 文件系统标注
    # ------------------------------------------------------------------

    def _prepare_filesystem(
        self, policy: SandboxPolicy, *, low_integrity: bool
    ) -> None:
        """把可写根标注为 Low、受保护路径恢复为 Medium（幂等）。"""

        if not low_integrity:
            # 不启用低完整性时标注没有意义；read_only / none 天然禁写。
            return
        if policy.filesystem in {
            SandboxFilesystemMode.NONE,
            SandboxFilesystemMode.READ_ONLY,
        }:
            return
        for root in policy.writable_roots:
            self._label_low(root)
        for denied in policy.denied_write_paths:
            self._label_medium(denied)

    def _label_low(self, path: Path) -> None:
        resolved = path.resolve()
        if resolved in self._labeled_paths:
            return
        self._ensure_full_control(resolved)
        if resolved.is_dir():
            self._run_icacls(
                str(resolved), "/setintegritylevel", "(OI)(CI)L"
            )
        else:
            self._run_icacls(str(resolved), "/setintegritylevel", "L")
        self._labeled_paths.add(resolved)

    def _label_medium(self, path: Path) -> None:
        resolved = path.resolve()
        if resolved in self._labeled_paths:
            return
        self._ensure_full_control(resolved)
        if resolved.is_dir():
            self._run_icacls(
                str(resolved), "/setintegritylevel", "(OI)(CI)M"
            )
        else:
            self._run_icacls(str(resolved), "/setintegritylevel", "M")
        self._labeled_paths.add(resolved)

    def _ensure_full_control(self, path: Path) -> None:
        """保证当前用户对该对象有完整控制权（写 SACL 前置）。

        在用户目录下通常已具备；仓库位于非用户目录（例如 ``C:\\agents``）
        时可能只有 Modify，此时以 owner 身份自我授予 F 再标注。
        授予失败不在此处报错：由后续 setintegritylevel 的返回码给出结论。
        """

        user = _current_user_expression()
        if user is None:
            return
        subprocess.run(  # noqa: S603 - 固定参数数组，不经过 shell
            ["icacls", str(path), "/grant", f"{user}:(OI)(CI)F"],
            capture_output=True,
            check=False,
            timeout=_ICACLS_TIMEOUT_SECONDS,
        )

    def _run_icacls(self, *arguments: str) -> None:
        try:
            result = subprocess.run(  # noqa: S603 - 固定参数数组，不经过 shell
                ["icacls", *arguments],
                capture_output=True,
                check=False,
                text=True,
                encoding="oem",
                errors="replace",
                timeout=_ICACLS_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxUnavailableError(
                f"icacls 执行失败：{arguments[0]}（{type(exc).__name__}: {exc}）"
            ) from exc
        if result.returncode != 0:
            raise SandboxUnavailableError(
                "沙箱无法标注路径完整性级别："
                f"{arguments[0]}（icacls 退出码 {result.returncode}）。"
                "请确认当前用户对该路径有写权限；"
                "Escape hatch：EVOLYN_SANDBOX_LOW_IL=0 仅保留 Job Object。"
            )

    def _ensure_sandbox_tmp(self) -> Path:
        directory = _SANDBOX_TMP_DIR
        directory.mkdir(parents=True, exist_ok=True)
        self._label_low(directory)
        return directory


def _current_user_expression() -> str | None:
    """返回 icacls 可用的当前用户表达式（失败返回 None 表示跳过授权）。"""

    username = os.environ.get("USERNAME", "").strip()
    if not username:
        return None
    domain = os.environ.get("USERDOMAIN", "").strip()
    if domain:
        return f"{domain}\\{username}"
    return username


def _wrap_script_target(
    command: Path, args: tuple[str, ...]
) -> tuple[Path, tuple[str, ...]]:
    """``.cmd`` / ``.bat`` 无法被 CreateProcess 直接启动，包装为 cmd.exe 调用。"""

    if command.suffix.lower() in (".cmd", ".bat"):
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        inner = subprocess.list2cmdline([str(command), *args])
        return Path(comspec), ("/d", "/s", "/c", inner)
    return command, args


__all__ = ["BACKEND_NAME", "WindowsSandboxBackend", "low_integrity_enabled"]
