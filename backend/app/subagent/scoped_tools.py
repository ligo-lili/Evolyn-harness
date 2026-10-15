"""子代理能力对应的受限工具包装与装配。

包装只收紧不放开：作用域校验（路径前缀 / 主机白名单 / 方法白名单）全部在
本地确定性完成，真正的执行仍委托给主注册表里的原工具实例。
"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.models.types import ToolDefinition, ToolPermission
from app.tools.base import BaseTool
from app.tools.builtin._workspace import resolve_workspace_path, workspace_root_path
from app.tools.builtin.list_files import MAX_LISTED_FILES
from app.tools.registry import ToolRegistry

from .capabilities import CapabilityError, CapabilityGrant

_ALLOWED_FETCH_METHODS = ("GET", "HEAD")


def build_scoped_tools(
    grants: Sequence[CapabilityGrant],
    main_registry: ToolRegistry,
    workspace_root: str | Path | None,
) -> tuple[BaseTool, ...]:
    """按生效授予装配子代理工具。

    同一能力多次授予时作用域合并（read_repo 前缀取并集；任一授予不给前缀
    即为不限制；web_fetch 域名取并集），避免同一工具名在 scoped registry
    中重复注册。
    """

    read_prefixes: list[str | None] = []
    fetch_hosts: list[str] = []
    wants_search = False
    for grant in grants:
        if grant.name == "read_repo":
            read_prefixes.append(grant.argument)
        elif grant.name == "web_search":
            wants_search = True
        elif grant.name == "web_fetch":
            fetch_hosts.extend(
                grant.argument.split(",") if grant.argument else ()
            )
        else:  # pragma: no cover - 目录与解析器同步演进
            raise CapabilityError(f"能力 {grant.name!r} 没有装配实现")

    tools: list[BaseTool] = []
    if read_prefixes:
        prefixes = (
            None
            if any(item is None for item in read_prefixes)
            else tuple(dict.fromkeys(read_prefixes))
        )
        tools.append(
            ScopedReadFileTool(
                _require_tool(main_registry, "read_repo", "read_file"),
                workspace_root=workspace_root,
                prefixes=prefixes,
            )
        )
        tools.append(
            ScopedListFilesTool(
                _require_tool(main_registry, "read_repo", "list_files"),
                workspace_root=workspace_root,
                prefixes=prefixes,
            )
        )
    if wants_search:
        tools.append(_require_tool(main_registry, "web_search", "web_search"))
    if fetch_hosts:
        tools.append(
            ScopedWebFetchTool(
                _require_tool(main_registry, "web_fetch", "http_request"),
                hosts=tuple(dict.fromkeys(fetch_hosts)),
            )
        )
    return tuple(tools)


def _require_tool(registry: ToolRegistry, capability: str, name: str) -> BaseTool:
    try:
        return registry.get(name)
    except KeyError:
        raise CapabilityError(
            f"能力 {capability} 需要的工具 {name!r} 未在当前注册表中"
        ) from None


def _prefix_scope_text(prefixes: tuple[str, ...] | None) -> str:
    if prefixes is None:
        return "the whole workspace"
    return ", ".join(f"{prefix}/" for prefix in prefixes)


def _rel_within_prefixes(relative: str, prefixes: tuple[str, ...]) -> bool:
    return any(
        relative == prefix or relative.startswith(f"{prefix}/")
        for prefix in prefixes
    )


def _assert_relative_within_scope(
    workspace_root: Path,
    relative_path: str,
    prefixes: tuple[str, ...] | None,
    *,
    allow_root: bool = False,
) -> None:
    if prefixes is None:
        return
    target = resolve_workspace_path(
        workspace_root,
        relative_path,
        allow_root=allow_root,
    )
    relative = (
        "."
        if target == workspace_root
        else target.relative_to(workspace_root).as_posix()
    )
    if not _rel_within_prefixes(relative, prefixes):
        raise ValueError(
            f"path {relative_path!r} is outside the granted read_repo scope: "
            f"{_prefix_scope_text(prefixes)}"
        )


class ScopedReadFileTool(BaseTool):
    """把 read_file 收窄到授予的路径前缀。"""

    def __init__(
        self,
        delegate: BaseTool,
        *,
        workspace_root: str | Path | None,
        prefixes: tuple[str, ...] | None,
    ) -> None:
        self._delegate = delegate
        self._workspace_root = workspace_root_path(workspace_root)
        self._prefixes = prefixes

    @property
    def definition(self) -> ToolDefinition:
        base = self._delegate.definition
        return base.model_copy(
            update={
                "description": (
                    f"{base.description} Scoped by the sub-agent grant: "
                    f"access is limited to {_prefix_scope_text(self._prefixes)}."
                ),
            }
        )

    async def execute(self, arguments: dict[str, Any]) -> Any:
        relative_path = arguments.get("path")
        if not isinstance(relative_path, str) or not relative_path:
            raise ValueError("'path' must be a non-empty string")
        _assert_relative_within_scope(
            self._workspace_root,
            relative_path,
            self._prefixes,
        )
        return await self._delegate.execute(arguments)


class ScopedListFilesTool(BaseTool):
    """把 list_files 收窄到授予的路径前缀。"""

    def __init__(
        self,
        delegate: BaseTool,
        *,
        workspace_root: str | Path | None,
        prefixes: tuple[str, ...] | None,
    ) -> None:
        self._delegate = delegate
        self._workspace_root = workspace_root_path(workspace_root)
        self._prefixes = prefixes

    @property
    def definition(self) -> ToolDefinition:
        base = self._delegate.definition
        return base.model_copy(
            update={
                "description": (
                    f"{base.description} Scoped by the sub-agent grant: only "
                    f"{_prefix_scope_text(self._prefixes)} can be listed."
                ),
            }
        )

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        directory = arguments.get("directory", ".")
        if not isinstance(directory, str):
            raise ValueError("'directory' must be a string")
        if self._prefixes is None:
            return await self._delegate.execute(arguments)
        if directory.strip() in ("", "."):
            return await self._list_prefix_roots()
        _assert_relative_within_scope(
            self._workspace_root,
            directory,
            self._prefixes,
            allow_root=True,
        )
        return await self._delegate.execute(arguments)

    async def _list_prefix_roots(self) -> dict[str, Any]:
        """默认目录：列出全部授权前缀，而不是整个工作区。"""

        files: list[str] = []
        truncated = False
        listed_any = False
        first_error: Exception | None = None
        for prefix in self._prefixes or ():
            try:
                payload = await self._delegate.execute({"directory": prefix})
            except Exception as exc:
                if first_error is None:
                    first_error = exc
                continue
            listed_any = True
            raw_files = payload.get("files")
            if isinstance(raw_files, list):
                files.extend(
                    item for item in raw_files if isinstance(item, str)
                )
            truncated = truncated or bool(payload.get("truncated"))
        if not listed_any:
            if first_error is not None:
                raise first_error
            return {"directory": ".", "files": [], "count": 0, "truncated": False}
        merged = sorted(dict.fromkeys(files))
        truncated = truncated or len(merged) > MAX_LISTED_FILES
        return {
            "directory": ".",
            "files": merged[:MAX_LISTED_FILES],
            "count": min(len(merged), MAX_LISTED_FILES),
            "truncated": truncated,
        }


class ScopedWebFetchTool(BaseTool):
    """把 http_request 收窄到主机白名单 + GET/HEAD。"""

    def __init__(self, delegate: BaseTool, *, hosts: tuple[str, ...]) -> None:
        self._delegate = delegate
        self._hosts = hosts

    @property
    def definition(self) -> ToolDefinition:
        base = self._delegate.definition
        parameters = deepcopy(base.parameters)
        properties = parameters.get("properties")
        if isinstance(properties, dict):
            properties.pop("body", None)
            method = properties.get("method")
            if isinstance(method, dict):
                method["enum"] = list(_ALLOWED_FETCH_METHODS)
        return base.model_copy(
            update={
                "description": (
                    "Fetch a page from the explicitly granted hosts "
                    f"({', '.join(self._hosts)}). Only GET/HEAD are allowed "
                    "and the host allowlist is enforced locally."
                ),
                "parameters": parameters,
                # 能力已由主代理显式授予且作用域受限（域名 + 方法），子代理
                # 内不再走人工审批；委托执行仍保留 SSRF 防护。
                "permission": ToolPermission.ALLOWED,
            }
        )

    async def execute(self, arguments: dict[str, Any]) -> Any:
        url = arguments.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError("'url' must be a non-empty string")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("'url' must be an http(s) URL")
        host = parsed.hostname.casefold()
        if not _host_within_scope(host, self._hosts):
            raise ValueError(
                f"host {host!r} is outside the granted web_fetch scope: "
                f"{', '.join(self._hosts)}"
            )
        method = str(arguments.get("method", "GET")).upper()
        if method not in _ALLOWED_FETCH_METHODS:
            raise ValueError("'method' must be GET or HEAD under web_fetch")
        payload = {
            key: value for key, value in arguments.items() if key != "body"
        }
        payload["method"] = method
        return await self._delegate.execute(payload)


def _host_within_scope(host: str, hosts: tuple[str, ...]) -> bool:
    return any(
        host == granted or host.endswith(f".{granted}") for granted in hosts
    )


__all__ = [
    "ScopedListFilesTool",
    "ScopedReadFileTool",
    "ScopedWebFetchTool",
    "build_scoped_tools",
]
