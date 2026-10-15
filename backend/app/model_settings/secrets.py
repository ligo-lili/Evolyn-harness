"""模型API Key的安全存储边界（Windows 凭据管理器）。"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from typing import Protocol

logger = logging.getLogger("evolyn.model_settings.secrets")

_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168


class ModelSecretStore(Protocol):
    """测试可替换的最小密钥存储接口。"""

    def get(self, provider: str) -> str | None: ...

    def set(self, provider: str, value: str) -> None: ...


class _CREDENTIAL_ATTRIBUTE(ctypes.Structure):
    _fields_ = [
        ("Keyword", wintypes.LPWSTR),
        ("Flags", wintypes.DWORD),
        ("ValueSize", wintypes.DWORD),
        ("Value", ctypes.POINTER(ctypes.c_byte)),
    ]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.POINTER(_CREDENTIAL_ATTRIBUTE)),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _load_advapi32() -> ctypes.WinDLL | None:
    """仅 Windows 上加载凭据 API，非 Windows 返回 None（get 退化为 None）。"""

    if sys.platform != "win32":
        return None
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
    ]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
    advapi32.CredWriteW.restype = wintypes.BOOL
    advapi32.CredDeleteW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    advapi32.CredDeleteW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]
    advapi32.CredFree.restype = None
    return advapi32


class WindowsCredentialSecretStore:
    """通过 Windows 凭据管理器保存密钥，不把密钥写入项目或 JSON。"""

    target_prefix = "Evolyn/ModelApiKey"

    def __init__(self) -> None:
        self._advapi32 = _load_advapi32()

    def target_name(self, provider: str) -> str:
        return f"{self.target_prefix}/{provider}"

    def get(self, provider: str) -> str | None:
        if self._advapi32 is None:
            return None
        credential = ctypes.POINTER(_CREDENTIALW)()
        if not self._advapi32.CredReadW(
            self.target_name(provider),
            _CRED_TYPE_GENERIC,
            0,
            ctypes.byref(credential),
        ):
            error = ctypes.get_last_error()
            if error != _ERROR_NOT_FOUND:
                logger.warning(
                    "Windows 凭据管理器读取失败：%s（error %s）", provider, error
                )
            return None
        try:
            blob_size = credential.contents.CredentialBlobSize
            if not blob_size:
                return None
            blob = ctypes.string_at(credential.contents.CredentialBlob, blob_size)
            return blob.decode("utf-16-le") or None
        finally:
            self._advapi32.CredFree(credential)

    def set(self, provider: str, value: str) -> None:
        normalized = value.strip()
        if not normalized:
            raise ValueError("api key cannot be empty")
        if self._advapi32 is None:
            raise RuntimeError(
                "model API keys require Windows Credential Manager"
            )
        blob = normalized.encode("utf-16-le")
        buffer = ctypes.create_string_buffer(blob, len(blob))
        credential = _CREDENTIALW()
        credential.Type = _CRED_TYPE_GENERIC
        credential.TargetName = self.target_name(provider)
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(
            buffer, ctypes.POINTER(ctypes.c_byte)
        )
        credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
        if not self._advapi32.CredWriteW(ctypes.byref(credential), 0):
            error = ctypes.get_last_error()
            raise RuntimeError(
                f"Windows 凭据管理器写入失败（error {error}）"
            )

    def delete(self, provider: str) -> None:
        """删除凭据；不存在时视为成功（幂等）。"""

        if self._advapi32 is None:
            return
        if not self._advapi32.CredDeleteW(
            self.target_name(provider), _CRED_TYPE_GENERIC, 0
        ):
            error = ctypes.get_last_error()
            if error != _ERROR_NOT_FOUND:
                raise RuntimeError(
                    f"Windows 凭据管理器删除失败（error {error}）"
                )


__all__ = ["ModelSecretStore", "WindowsCredentialSecretStore"]
