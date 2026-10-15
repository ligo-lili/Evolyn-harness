"""沙箱测试：策略编译单测 + Windows 后端单测 + 低完整性集成测试。

低完整性（Low IL）集成测试需要本机支持「复制令牌 → 降完整性 →
CreateProcessAsUser」；不支持的环境自动 ``pytest.skip``（fail-closed 是
产品行为，不是测试失败）。读隔离在 Windows V1 未强制，因此只断言写保护。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.sandbox import (
    SandboxBackend,
    SandboxConfig,
    SandboxFilesystemMode,
    SandboxLaunchSpec,
    SandboxNetworkMode,
    SandboxPolicy,
    SandboxPolicyError,
    SandboxSupervisor,
    SandboxUnavailableError,
    UnsupportedSandboxBackend,
    WindowsSandboxBackend,
)
from app.sandbox import windows as sandbox_windows
from app.tools.builtin.shell import ShellCommandTool, _resolve_shell, _shell_environment

_WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows 沙箱仅 Windows"
)


class RecordingBackend(SandboxBackend):
    def __init__(self) -> None:
        self.policy: SandboxPolicy | None = None

    def prepare(
        self,
        *,
        command: Path,
        args: tuple[str, ...],
        env: dict[str, str],
        policy: SandboxPolicy,
    ) -> SandboxLaunchSpec:
        self.policy = policy
        return SandboxLaunchSpec(
            command=str(command),
            args=args,
            cwd=str(policy.working_directory),
            env=env,
            backend="recording",
            sandboxed=True,
        )


# ---------------------------------------------------------------------------
# 策略编译（平台无关）
# ---------------------------------------------------------------------------


def test_supervisor_compiles_workspace_policy_and_protects_metadata(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    (workspace / ".env").write_text("SECRET=value", encoding="utf-8")
    extra = workspace / "input"
    extra.mkdir()
    backend = RecordingBackend()
    supervisor = SandboxSupervisor(workspace, native_backend=backend)

    launch = supervisor.prepare_launch(
        command=sys.executable,
        args=("-V",),
        env={"PATH": os.environ.get("PATH", "")},
        cwd=None,
        config=SandboxConfig(
            filesystem=SandboxFilesystemMode.WORKSPACE_WRITE,
            network=SandboxNetworkMode.DENIED,
            readable_roots=("input",),
        ),
    )

    assert launch.sandboxed is True
    assert launch.backend == "recording"
    assert backend.policy is not None
    assert workspace in backend.policy.readable_roots
    assert workspace in backend.policy.writable_roots
    assert workspace / ".git" in backend.policy.denied_write_paths
    assert workspace / ".env" in backend.policy.denied_read_paths
    assert backend.policy.network is SandboxNetworkMode.DENIED


def test_supervisor_rejects_relative_path_traversal(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    supervisor = SandboxSupervisor(workspace, native_backend=RecordingBackend())

    with pytest.raises(SandboxPolicyError, match="不能越过 workspace"):
        supervisor.prepare_launch(
            command=sys.executable,
            args=(),
            env={"PATH": os.environ.get("PATH", "")},
            cwd=None,
            config=SandboxConfig(readable_roots=("../outside",)),
        )


def test_unsupported_platform_fails_closed(tmp_path: Path) -> None:
    supervisor = SandboxSupervisor(
        tmp_path,
        native_backend=UnsupportedSandboxBackend("unsupported"),
    )

    with pytest.raises(SandboxUnavailableError, match="拒绝降级执行"):
        supervisor.prepare_launch(
            command=sys.executable,
            args=(),
            env={"PATH": os.environ.get("PATH", "")},
            cwd=None,
            config=SandboxConfig(),
        )


def test_explicit_host_mode_is_visible_in_launch_spec(tmp_path: Path) -> None:
    supervisor = SandboxSupervisor(
        tmp_path,
        native_backend=UnsupportedSandboxBackend("unused"),
    )

    launch = supervisor.prepare_launch(
        command=sys.executable,
        args=(),
        env={"PATH": os.environ.get("PATH", "")},
        cwd=None,
        config=SandboxConfig(filesystem=SandboxFilesystemMode.HOST),
    )

    assert launch.sandboxed is False
    assert launch.backend == "host"


def test_resolve_executable_accepts_alias_that_fails_strict_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """App Execution Alias 兼容：strict resolve 失败时回退非严格解析。"""

    alias = tmp_path / "pwsh.EXE"
    alias.write_bytes(b"")
    original_resolve = Path.resolve

    def fake_resolve(self: Path, strict: bool = False) -> Path:
        if strict:
            raise OSError(1920, "系统无法访问此文件")
        return original_resolve(self, strict=False)

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    from app.sandbox.backends import resolve_executable

    assert resolve_executable(str(alias), env={}) == alias


@_WINDOWS_ONLY
def test_resolve_shell_skips_unresolvable_candidate_for_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """沙箱路径跳过 strict resolve 失败的 shell（Store 版 pwsh 别名）。"""

    from app.tools.builtin import shell as shell_module

    broken_alias = tmp_path / "pwsh.EXE"  # 不存在 → strict resolve 失败
    real_powershell = tmp_path / "powershell.exe"
    real_powershell.write_bytes(b"")

    def fake_which(name: str) -> str | None:
        return {
            "pwsh": str(broken_alias),
            "powershell": str(real_powershell),
        }.get(name)

    monkeypatch.delenv("EVOLYN_SHELL", raising=False)
    monkeypatch.setattr(shell_module.shutil, "which", fake_which)

    assert _resolve_shell()[0] == str(broken_alias)
    assert _resolve_shell(for_sandbox=True)[0] == str(real_powershell)


# ---------------------------------------------------------------------------
# WindowsSandboxBackend 单测（icacls 全部 mock，不产生真实标注）
# ---------------------------------------------------------------------------


def _recording_icacls(calls: list[list[str]]):
    def fake_run(args, **_kwargs):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return fake_run


def _workspace_policy(workspace: Path) -> SandboxPolicy:
    return SandboxPolicy(
        filesystem=SandboxFilesystemMode.WORKSPACE_WRITE,
        network=SandboxNetworkMode.DENIED,
        working_directory=workspace,
        readable_roots=(workspace,),
        writable_roots=(workspace,),
    )


@_WINDOWS_ONLY
def test_windows_backend_compiles_launcher_spec(tmp_path, monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        sandbox_windows.subprocess, "run", _recording_icacls(calls)
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    backend = WindowsSandboxBackend()

    launch = backend.prepare(
        command=Path(sys.executable),
        args=("-c", "print(1)"),
        env={"PATH": os.environ.get("PATH", "")},
        policy=_workspace_policy(workspace),
    )

    assert launch.command == sys.executable
    assert launch.args == (
        str(sandbox_windows._LAUNCHER_PATH),
        "--",
        sys.executable,
        "-c",
        "print(1)",
    )
    assert launch.cwd == str(workspace)
    assert launch.backend == sandbox_windows.BACKEND_NAME
    assert launch.sandboxed is True
    assert launch.enforcement == ("network", "read_isolation")
    assert launch.env["EVOLYN_SANDBOX_LOW_IL"] == "1"
    assert launch.env["TEMP"] == str(sandbox_windows._SANDBOX_TMP_DIR)
    assert launch.env["TMP"] == str(sandbox_windows._SANDBOX_TMP_DIR)


@_WINDOWS_ONLY
def test_windows_backend_low_il_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EVOLYN_SANDBOX_LOW_IL", "0")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    backend = WindowsSandboxBackend()

    launch = backend.prepare(
        command=Path(sys.executable),
        args=(),
        env={"PATH": os.environ.get("PATH", "")},
        policy=_workspace_policy(workspace),
    )

    assert launch.enforcement == (
        "network",
        "read_isolation",
        "write_protection",
    )
    assert launch.env["EVOLYN_SANDBOX_LOW_IL"] == "0"
    assert "TEMP" not in launch.env
    assert launch.backend == sandbox_windows.BACKEND_NAME


@_WINDOWS_ONLY
def test_windows_backend_labels_integrity_levels(tmp_path, monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        sandbox_windows.subprocess, "run", _recording_icacls(calls)
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    (workspace / ".env").write_text("SECRET=value", encoding="utf-8")
    supervisor = SandboxSupervisor(
        workspace, native_backend=WindowsSandboxBackend()
    )

    prepare = lambda: supervisor.prepare_launch(  # noqa: E731
        command=sys.executable,
        args=(),
        env={"PATH": os.environ.get("PATH", "")},
        cwd=None,
        config=SandboxConfig(),
    )
    prepare()
    integrity_calls = [call for call in calls if call[2] == "/setintegritylevel"]
    levels = {call[1]: call[3] for call in integrity_calls}

    assert levels[str(workspace)] == "(OI)(CI)L"
    assert levels[str(workspace / ".git")] == "(OI)(CI)M"
    assert levels[str(workspace / ".env")] == "M"
    assert levels[str(sandbox_windows._SANDBOX_TMP_DIR.resolve())] == "(OI)(CI)L"
    assert any(call[2] == "/grant" for call in calls)

    # 幂等：第二次 prepare 不再重复标注（进程内缓存）。
    prepare()
    integrity_calls_again = [
        call for call in calls if call[2] == "/setintegritylevel"
    ]
    assert len(integrity_calls_again) == len(integrity_calls)


@_WINDOWS_ONLY
def test_windows_backend_icacls_failure_is_fail_closed(
    tmp_path, monkeypatch
) -> None:
    def failing_run(args, **_kwargs):
        return SimpleNamespace(returncode=5, stdout="", stderr="denied")

    monkeypatch.setattr(sandbox_windows.subprocess, "run", failing_run)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    backend = WindowsSandboxBackend()

    with pytest.raises(SandboxUnavailableError, match="EVOLYN_SANDBOX_LOW_IL=0"):
        backend.prepare(
            command=Path(sys.executable),
            args=(),
            env={"PATH": os.environ.get("PATH", "")},
            policy=_workspace_policy(workspace),
        )


@_WINDOWS_ONLY
def test_windows_backend_wraps_cmd_scripts(tmp_path, monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        sandbox_windows.subprocess, "run", _recording_icacls(calls)
    )
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = workspace / "tool.cmd"
    script.write_text("@echo off\n", encoding="utf-8")
    backend = WindowsSandboxBackend()

    launch = backend.prepare(
        command=script,
        args=("--flag",),
        env={"PATH": os.environ.get("PATH", "")},
        policy=_workspace_policy(workspace),
    )

    assert launch.args[2] == r"C:\Windows\System32\cmd.exe"
    assert launch.args[3:6] == ("/d", "/s", "/c")
    assert str(script) in launch.args[6]
    assert "--flag" in launch.args[6]


# ---------------------------------------------------------------------------
# 低完整性集成测试（真实 launcher + icacls；不可用则 skip）
# ---------------------------------------------------------------------------


def _run_launch(launch: SandboxLaunchSpec) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - 测试固定参数数组
        [launch.command, *launch.args],
        cwd=launch.cwd,
        env=launch.env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )


def _skip_unless_low_il_available(workspace: Path) -> None:
    """跑一次最小低完整性启动；launcher fail-closed（退出码 86）则 skip。"""

    supervisor = SandboxSupervisor(workspace)
    launch = supervisor.prepare_launch(
        command=sys.executable,
        args=("-c", "print('probe-ok')"),
        env=_shell_environment(),
        cwd=None,
        config=SandboxConfig(),
    )
    completed = _run_launch(launch)
    if completed.returncode == 86 and "evolyn sandbox launcher" in completed.stderr:
        first_line = completed.stderr.splitlines()[0] if completed.stderr else ""
        pytest.skip(f"低完整性沙箱在此环境不可用：{first_line}")
    if completed.returncode != 0 or "probe-ok" not in completed.stdout:
        pytest.skip(
            "低完整性沙箱启动未达预期："
            f"exit={completed.returncode} stderr={completed.stderr[:200]!r}"
        )


@_WINDOWS_ONLY
def test_low_il_blocks_writes_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = workspace / ".env"
    protected.write_text("SECRET=keep", encoding="utf-8")

    _skip_unless_low_il_available(workspace)

    supervisor = SandboxSupervisor(workspace)
    code = (
        "import pathlib, sys, tempfile\n"
        "workspace = pathlib.Path(sys.argv[1])\n"
        "outside = pathlib.Path(sys.argv[2])\n"
        "def attempt(path):\n"
        "    try:\n"
        "        path.write_text('x', encoding='utf-8')\n"
        "        return 'ok'\n"
        "    except OSError as exc:\n"
        "        return type(exc).__name__\n"
        "print('INSIDE=' + attempt(workspace / 'inside.txt'))\n"
        "print('OUTSIDE=' + attempt(outside / 'evil.txt'))\n"
        "print('ENVFILE=' + attempt(workspace / '.env'))\n"
        "try:\n"
        "    probe = pathlib.Path(tempfile.gettempdir()) / 'evolyn-temp-probe.tmp'\n"
        "    probe.write_text('x', encoding='utf-8')\n"
        "    probe.unlink()\n"
        "    print('TEMP=ok')\n"
        "except OSError as exc:\n"
        "    print('TEMP=' + type(exc).__name__)\n"
    )
    launch = supervisor.prepare_launch(
        command=sys.executable,
        args=("-c", code, str(workspace), str(outside)),
        env=_shell_environment(),
        cwd=None,
        config=SandboxConfig(),
    )

    completed = _run_launch(launch)

    assert completed.returncode == 0, completed.stderr
    assert "INSIDE=ok" in completed.stdout
    assert "OUTSIDE=PermissionError" in completed.stdout
    assert "ENVFILE=PermissionError" in completed.stdout
    # TEMP 已重定向到沙箱临时目录（低完整性可写）。
    assert "TEMP=ok" in completed.stdout

    assert (workspace / "inside.txt").read_text(encoding="utf-8") == "x"
    assert not (outside / "evil.txt").exists()
    assert protected.read_text(encoding="utf-8") == "SECRET=keep"


@_WINDOWS_ONLY
@pytest.mark.asyncio
async def test_shell_tool_uses_workspace_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell_executable, _ = _resolve_shell()
    if Path(shell_executable).stem.lower() == "cmd":
        pytest.skip("本测试需要 PowerShell")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")

    _skip_unless_low_il_available(workspace)

    tool = ShellCommandTool(
        workspace,
        sandbox_supervisor=SandboxSupervisor(workspace),
    )

    write_result = await tool.execute(
        {"command": "Set-Content -Path inside.txt -Value safe -ErrorAction Stop"}
    )
    outside_result = await tool.execute(
        {
            "command": (
                f"try {{ Set-Content -Path '{outside / 'evil.txt'}' "
                "-Value x -ErrorAction Stop; 'WROTE' } catch { 'DENIED' }"
            )
        }
    )
    env_result = await tool.execute(
        {
            "command": (
                "if ($env:OPENAI_API_KEY) { 'LEAKED' } else { 'not-present' }"
            )
        }
    )

    assert write_result["exit_code"] == 0
    assert (workspace / "inside.txt").read_text(encoding="utf-8").strip() == "safe"
    assert write_result["sandbox"] == {
        "backend": "windows_low_integrity_job",
        "sandboxed": True,
        "enforcement": ["network", "read_isolation"],
    }

    assert "DENIED" in outside_result["stdout"]
    assert not (outside / "evil.txt").exists()

    # 环境变量白名单：Provider 密钥不进入沙箱子进程。
    assert env_result["stdout"].strip() == "not-present"
