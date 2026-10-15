"""Evolyn Windows 沙箱启动器（stdlib-only）。

由 ``app/sandbox/windows.py`` 生成的 LaunchSpec 调用，一般不需要手工执行：

    <python> <launcher.py 绝对路径> -- <target> [args...]

职责：
- 复制当前进程令牌 → 降为低完整性（Low IL，S-1-16-4096）→ CreateProcessAsUserW
  （普通用户权限即可；失败即 fail closed）；
- Job Object（KILL_ON_JOB_CLOSE）托管整棵子进程树，父进程退出/被杀即整树终止；
- 匿名管道 + 守护线程在调用方与子进程之间双向中继 stdin/stdout/stderr
  （二进制安全，不做任何文本变换）；
- 子进程退出后按真实 exitcode 退出。

逃生口（失败信息中也会提示）：
- 环境变量 ``EVOLYN_SANDBOX_LOW_IL=0``：跳过低完整性令牌，仅保留 Job Object；
- MCP Server 配置 ``filesystem: "host"``：完全关闭沙箱（由上层处理）。

本文件必须保持 stdlib-only：它以绝对路径被解释器直接执行，
不能依赖 ``app`` 包或任何第三方库。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from typing import NoReturn

if sys.platform != "win32":  # pragma: no cover - 仅防御误用
    print("evolyn sandbox launcher: only supported on Windows", file=sys.stderr)
    raise SystemExit(2)

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

_HANDLE = wintypes.HANDLE
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# --- 常量 ---
_MAXIMUM_ALLOWED = 0x02000000
_SECURITY_IMPERSONATION = 2
_TOKEN_PRIMARY = 1
_TOKEN_INTEGRITY_LEVEL = 25
_SE_GROUP_INTEGRITY = 0x20
_STARTF_USESTDHANDLES = 0x00000100
_CREATE_SUSPENDED = 0x00000004
_CREATE_NO_WINDOW = 0x08000000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_HANDLE_FLAG_INHERIT = 0x00000001
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_INFINITE = 0xFFFFFFFF
_STD_INPUT_HANDLE = -10
_STD_OUTPUT_HANDLE = -11
_STD_ERROR_HANDLE = -12
_RELAY_BUFFER_SIZE = 65536
_EXIT_SANDBOX_FAILURE = 86


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", _HANDLE),
        ("hStdOutput", _HANDLE),
        ("hStdError", _HANDLE),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", _HANDLE),
        ("hThread", _HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", _SID_AND_ATTRIBUTES)]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


def _declare_functions() -> None:
    _kernel32.GetCurrentProcess.restype = _HANDLE
    _kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    _kernel32.GetStdHandle.restype = _HANDLE
    _kernel32.CloseHandle.argtypes = [_HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [_HANDLE]
    _kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(_HANDLE),
        ctypes.POINTER(_HANDLE),
        ctypes.POINTER(_SECURITY_ATTRIBUTES),
        wintypes.DWORD,
    ]
    _kernel32.SetHandleInformation.argtypes = [
        _HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = _HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        _HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _kernel32.AssignProcessToJobObject.argtypes = [_HANDLE, _HANDLE]
    _kernel32.WaitForSingleObject.argtypes = [_HANDLE, wintypes.DWORD]
    _kernel32.GetExitCodeProcess.argtypes = [
        _HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.ResumeThread.argtypes = [_HANDLE]
    _kernel32.TerminateProcess.argtypes = [_HANDLE, wintypes.UINT]
    _kernel32.ReadFile.argtypes = [
        _HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    _kernel32.WriteFile.argtypes = [
        _HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    _advapi32.OpenProcessToken.argtypes = [
        _HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(_HANDLE),
    ]
    _advapi32.DuplicateTokenEx.argtypes = [
        _HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(_HANDLE),
    ]
    _advapi32.SetTokenInformation.argtypes = [
        _HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _advapi32.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _advapi32.CreateProcessAsUserW.argtypes = [
        _HANDLE,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(_STARTUPINFOW),
        ctypes.POINTER(_PROCESS_INFORMATION),
    ]
    _advapi32.CreateProcessAsUserW.restype = wintypes.BOOL
    _kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(_STARTUPINFOW),
        ctypes.POINTER(_PROCESS_INFORMATION),
    ]
    _kernel32.CreateProcessW.restype = wintypes.BOOL


def _fail(stage: str, detail: str) -> NoReturn:
    message = (
        f"evolyn sandbox launcher: {stage}: {detail}\n"
        "hint: set EVOLYN_SANDBOX_LOW_IL=0 to skip the low-integrity token, "
        'or configure the MCP server with filesystem: "host" to disable the sandbox.\n'
    )
    os.write(2, message.encode("utf-8", "replace"))
    raise SystemExit(_EXIT_SANDBOX_FAILURE)


def _low_integrity_enabled() -> bool:
    raw = os.environ.get("EVOLYN_SANDBOX_LOW_IL", "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _create_low_integrity_token() -> int:
    """把当前进程令牌复制并降为 Low IL（S-1-16-4096）。"""

    process_token = _HANDLE()
    if not _advapi32.OpenProcessToken(
        _kernel32.GetCurrentProcess(),
        _MAXIMUM_ALLOWED,
        ctypes.byref(process_token),
    ):
        _fail(
            "OpenProcessToken",
            f"cannot open own token (error {ctypes.get_last_error()})",
        )

    duplicated = _HANDLE()
    if not _advapi32.DuplicateTokenEx(
        process_token,
        _MAXIMUM_ALLOWED,
        None,
        _SECURITY_IMPERSONATION,
        _TOKEN_PRIMARY,
        ctypes.byref(duplicated),
    ):
        error = ctypes.get_last_error()
        _kernel32.CloseHandle(process_token)
        _fail("DuplicateTokenEx", f"cannot duplicate token (error {error})")

    sid = ctypes.c_void_p()
    if not _advapi32.ConvertStringSidToSidW("S-1-16-4096", ctypes.byref(sid)):
        error = ctypes.get_last_error()
        _kernel32.CloseHandle(process_token)
        _kernel32.CloseHandle(duplicated)
        _fail("ConvertStringSidToSidW", f"cannot build low-integrity SID ({error})")

    label = _TOKEN_MANDATORY_LABEL()
    label.Label.Sid = sid
    label.Label.Attributes = _SE_GROUP_INTEGRITY
    if not _advapi32.SetTokenInformation(
        duplicated,
        _TOKEN_INTEGRITY_LEVEL,
        ctypes.byref(label),
        ctypes.sizeof(label),
    ):
        error = ctypes.get_last_error()
        _kernel32.LocalFree(sid)
        _kernel32.CloseHandle(process_token)
        _kernel32.CloseHandle(duplicated)
        _fail(
            "SetTokenInformation",
            f"cannot lower token integrity to S-1-16-4096 (error {error})",
        )

    _kernel32.LocalFree(sid)
    _kernel32.CloseHandle(process_token)
    return duplicated.value


def _create_kill_on_close_job() -> int:
    job = _kernel32.CreateJobObjectW(None, None)
    if not job:
        _fail("CreateJobObject", f"cannot create job (error {ctypes.get_last_error()})")
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not _kernel32.SetInformationJobObject(
        job,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        error = ctypes.get_last_error()
        _kernel32.CloseHandle(job)
        _fail("SetInformationJobObject", f"cannot set job limits (error {error})")
    return job


def _create_pipe() -> tuple[int, int]:
    """返回 (read_end, write_end)，两端均允许继承（由调用方清理不需要的一端）。"""

    read_end = _HANDLE()
    write_end = _HANDLE()
    attributes = _SECURITY_ATTRIBUTES()
    attributes.nLength = ctypes.sizeof(attributes)
    attributes.bInheritHandle = True
    if not _kernel32.CreatePipe(
        ctypes.byref(read_end),
        ctypes.byref(write_end),
        ctypes.byref(attributes),
        0,
    ):
        _fail("CreatePipe", f"cannot create pipe (error {ctypes.get_last_error()})")
    return read_end.value, write_end.value


def _clear_inherit(handle: int) -> None:
    _kernel32.SetHandleInformation(handle, _HANDLE_FLAG_INHERIT, 0)


def _relay(source: int, target: int, *, close_target: bool) -> None:
    """把 source 句柄的字节流原样搬到 target；线程安全、二进制安全。"""

    buffer = ctypes.create_string_buffer(_RELAY_BUFFER_SIZE)
    while True:
        read_count = wintypes.DWORD()
        if not _kernel32.ReadFile(
            source, buffer, _RELAY_BUFFER_SIZE, ctypes.byref(read_count), None
        ):
            break
        if read_count.value == 0:
            break
        offset = 0
        while offset < read_count.value:
            chunk = bytes(buffer.raw[offset : read_count.value])
            written = wintypes.DWORD()
            if not _kernel32.WriteFile(
                target,
                ctypes.c_char_p(chunk),
                len(chunk),
                ctypes.byref(written),
                None,
            ) or written.value == 0:
                return
            offset += written.value
    if close_target:
        _kernel32.CloseHandle(target)


def _parse_target(argv: list[str]) -> tuple[str, tuple[str, ...]]:
    try:
        index = argv.index("--")
    except ValueError:
        _fail("arguments", "expected '-- <target> [args...]'")
    target = argv[index + 1] if index + 1 < len(argv) else ""
    if not target:
        _fail("arguments", "missing target executable after '--'")
    return target, tuple(argv[index + 2 :])


def main(argv: list[str] | None = None) -> int:
    import threading

    _declare_functions()
    argv = list(sys.argv[1:] if argv is None else argv)
    target, target_args = _parse_target(argv)

    low_integrity = _low_integrity_enabled()
    token = _create_low_integrity_token() if low_integrity else None
    job = _create_kill_on_close_job()

    try:
        child_stdin_read, launcher_stdin_write = _create_pipe()
        launcher_stdout_read, child_stdout_write = _create_pipe()
        launcher_stderr_read, child_stderr_write = _create_pipe()
    except SystemExit:
        if token:
            _kernel32.CloseHandle(token)
        _kernel32.CloseHandle(job)
        raise

    # 调用方保留的一端不允许被孙进程继承，否则中继线程永远等不到 EOF。
    _clear_inherit(launcher_stdin_write)
    _clear_inherit(launcher_stdout_read)
    _clear_inherit(launcher_stderr_read)

    startup = _STARTUPINFOW()
    startup.cb = ctypes.sizeof(_STARTUPINFOW)
    startup.dwFlags = _STARTF_USESTDHANDLES
    startup.hStdInput = child_stdin_read
    startup.hStdOutput = child_stdout_write
    startup.hStdError = child_stderr_write

    command_line = subprocess.list2cmdline([target, *target_args])
    command_buffer = ctypes.create_unicode_buffer(command_line)
    process_info = _PROCESS_INFORMATION()
    creation_flags = (
        _CREATE_SUSPENDED | _CREATE_NO_WINDOW | _CREATE_UNICODE_ENVIRONMENT
    )

    if low_integrity:
        created = _advapi32.CreateProcessAsUserW(
            token,
            target,
            command_buffer,
            None,
            None,
            True,
            creation_flags,
            None,
            None,
            ctypes.byref(startup),
            ctypes.byref(process_info),
        )
    else:
        created = _kernel32.CreateProcessW(
            target,
            command_buffer,
            None,
            None,
            True,
            creation_flags,
            None,
            None,
            ctypes.byref(startup),
            ctypes.byref(process_info),
        )

    if not created:
        error = ctypes.get_last_error()
        for handle in (
            child_stdin_read,
            launcher_stdin_write,
            launcher_stdout_read,
            child_stdout_write,
            launcher_stderr_read,
            child_stderr_write,
            job,
        ):
            _kernel32.CloseHandle(handle)
        if token:
            _kernel32.CloseHandle(token)
        _fail(
            "CreateProcess",
            f"cannot start sandboxed child (error {error})",
        )

    if token:
        _kernel32.CloseHandle(token)
    # 子进程已持有自己的管道端，父端立即释放。
    _kernel32.CloseHandle(child_stdin_read)
    _kernel32.CloseHandle(child_stdout_write)
    _kernel32.CloseHandle(child_stderr_write)

    if not _kernel32.AssignProcessToJobObject(job, process_info.hProcess):
        error = ctypes.get_last_error()
        # 挂 Job 失败:连招 fail-closed,先终止已挂起、尚未恢复的子进程,避免泄漏。
        _kernel32.TerminateProcess(process_info.hProcess, _EXIT_SANDBOX_FAILURE)
        _kernel32.CloseHandle(process_info.hProcess)
        _kernel32.CloseHandle(process_info.hThread)
        for handle in (
            launcher_stdin_write,
            launcher_stdout_read,
            launcher_stderr_read,
            job,
        ):
            _kernel32.CloseHandle(handle)
        _fail(
            "AssignProcessToJobObject",
            f"cannot attach child to job (error {error})",
        )
    _kernel32.ResumeThread(process_info.hThread)

    launcher_stdin = _kernel32.GetStdHandle(_STD_INPUT_HANDLE)
    launcher_stdout = _kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)
    launcher_stderr = _kernel32.GetStdHandle(_STD_ERROR_HANDLE)

    if launcher_stdin not in (None, _INVALID_HANDLE_VALUE):
        threading.Thread(
            target=_relay,
            args=(launcher_stdin, launcher_stdin_write),
            kwargs={"close_target": True},
            daemon=True,
        ).start()
    else:
        _kernel32.CloseHandle(launcher_stdin_write)
    if launcher_stdout not in (None, _INVALID_HANDLE_VALUE):
        threading.Thread(
            target=_relay,
            args=(launcher_stdout_read, launcher_stdout),
            kwargs={"close_target": True},
            daemon=True,
        ).start()
    if launcher_stderr not in (None, _INVALID_HANDLE_VALUE):
        threading.Thread(
            target=_relay,
            args=(launcher_stderr_read, launcher_stderr),
            kwargs={"close_target": True},
            daemon=True,
        ).start()

    _kernel32.WaitForSingleObject(process_info.hProcess, _INFINITE)
    exit_code = wintypes.DWORD()
    _kernel32.GetExitCodeProcess(process_info.hProcess, ctypes.byref(exit_code))

    # 关闭 Job：KILL_ON_JOB_CLOSE 连杀整棵子进程树（含遗留孙进程）。
    _kernel32.CloseHandle(job)
    _kernel32.CloseHandle(process_info.hProcess)
    _kernel32.CloseHandle(process_info.hThread)
    return exit_code.value


if __name__ == "__main__":
    raise SystemExit(main())
