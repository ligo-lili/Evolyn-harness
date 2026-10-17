"""主/子共用的只读探索工具：grep / glob。

构造时绑定工作区与可选前缀范围（``prefixes=None`` 表示整个工作区），每次
调用都在执行层做确定性路径校验；扫描不穿透符号链接与 Windows 目录
junction，输出有硬上限（结果条数 / 行数 / 字符数），保证回流的数据有界。
可注入 ``path_exclusion`` 排除钩子（命中返回排除原因）：子代理用它接入
敏感路径策略，主 Agent 默认不启用。
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.models.types import ToolDefinition
from app.tools.base import BaseTool

from ._scope import (
    assert_relative_within_scope,
    prefix_scope_text,
    resolve_scope_roots,
)
from ._workspace import workspace_root_path

MAX_GREP_RESULTS = 200
DEFAULT_GREP_RESULTS = 60
MAX_GLOB_FILES = 500
DEFAULT_GLOB_FILES = 100
MAX_SCAN_FILES = 5_000
MAX_SCAN_FILE_BYTES = 1_048_576
MAX_LINE_CHARS = 400

_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
    }
)

# Windows 重解析点里属「链接」语义的 tag：目录 junction 与符号链接。
# 其余重解析点（云占位 / 去重等）不改变路径归属，仍按普通文件处理。
_LINK_REPARSE_TAGS = frozenset(
    {
        0xA0000003,  # IO_REPARSE_TAG_MOUNT_POINT（目录 junction）
        0xA000000C,  # IO_REPARSE_TAG_SYMLINK
    }
)


def bound_count(value: Any, *, default: int, limit: int, field: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"'{field}' must be an integer")
    if value < 1:
        raise ValueError(f"'{field}' must be >= 1")
    return min(value, limit)


def _is_link_like(path: Path) -> bool:
    """路径本身是否为符号链接 / 目录 junction（“链接类”条目）。

    ``os.walk`` 默认不跟随符号链接，但 Windows 的目录 junction 会被当成
    普通目录进入；扫描前统一用它剪枝。``os.lstat`` 失败时按链接处理，
    宁可漏扫不越界。
    """

    try:
        info = path.lstat()
    except OSError:
        return True
    if stat.S_ISLNK(info.st_mode):
        return True
    return getattr(info, "st_reparse_tag", 0) in _LINK_REPARSE_TAGS


def _iter_files(root: Path) -> list[Path]:
    """列出范围内的常规文件，不穿透链接。

    扫描只沿真实目录进行：目录 junction / 目录符号链接不展开，文件符号
    链接一律跳过（含工作区内部的链接；与 ripgrep 的默认行为一致），避免
    grep / glob 通过链接读到授权范围之外的内容。单文件读取仍由
    ``assert_relative_within_scope`` 按真实路径校验。
    """

    if root.is_file():
        return [] if _is_link_like(root) else [root]
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in _SKIP_DIRS
            and not _is_link_like(current / name)
        )
        for name in sorted(filenames):
            candidate = current / name
            if _is_link_like(candidate):
                continue
            files.append(candidate)
    return files


def _glob_candidates(pattern: str) -> list[str]:
    normalized = pattern.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    candidates = [normalized]
    if "**/" in normalized:
        collapsed = normalized.replace("**/", "")
        if collapsed and collapsed not in candidates:
            candidates.append(collapsed)
    return candidates


def _glob_match(relative: str, name: str, pattern: str) -> bool:
    return any(
        fnmatch.fnmatch(relative, candidate)
        or fnmatch.fnmatch(name, candidate)
        for candidate in _glob_candidates(pattern)
    )


def _grep_payload(
    results: list[dict[str, Any]],
    scanned_files: int,
    truncated: bool,
) -> dict[str, Any]:
    return {
        "results": results,
        "count": len(results),
        "scanned_files": scanned_files,
        "truncated": truncated,
    }


class GrepTool(BaseTool):
    """在授权范围内按正则搜索文本内容。"""

    def __init__(
        self,
        *,
        workspace_root: str | Path | None,
        prefixes: tuple[str, ...] | None = None,
        path_exclusion: Callable[[str], str | None] | None = None,
    ) -> None:
        self._workspace_root = workspace_root_path(workspace_root)
        self._prefixes = prefixes
        self._path_exclusion = path_exclusion

    def _is_excluded(self, relative: str) -> bool:
        """注入式排除钩子：返回拒绝消息的路径不进入结果（敏感路径等）。"""

        return (
            self._path_exclusion is not None
            and self._path_exclusion(relative) is not None
        )

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="grep",
            description=(
                "Search file contents with a Python regular expression "
                f"inside the allowed scope ({prefix_scope_text(self._prefixes)}). "
                "Returns path, line number and trimmed line text, at most "
                f"{MAX_GREP_RESULTS} matches."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Python 正则表达式（如 r'def login'）。",
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "可选：限定到某个文件或目录（工作区相对路径）。"
                        ),
                    },
                    "glob": {
                        "type": "string",
                        "description": (
                            "可选：按文件名过滤（shell 通配，如 '*.py'）。"
                        ),
                    },
                    "case_sensitive": {"type": "boolean", "default": True},
                    "max_results": {
                        "type": "integer",
                        "default": DEFAULT_GREP_RESULTS,
                    },
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            strict=False,
        )

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("'pattern' must be a non-empty string")
        case_sensitive = arguments.get("case_sensitive", True)
        if not isinstance(case_sensitive, bool):
            raise ValueError("'case_sensitive' must be a boolean")
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            raise ValueError(f"invalid regular expression: {exc}") from None
        glob_filter = arguments.get("glob")
        if glob_filter is not None and (
            not isinstance(glob_filter, str) or not glob_filter
        ):
            raise ValueError("'glob' must be a non-empty string")
        max_results = bound_count(
            arguments.get("max_results"),
            default=DEFAULT_GREP_RESULTS,
            limit=MAX_GREP_RESULTS,
            field="max_results",
        )
        roots = self._search_roots(arguments.get("path"))
        return await asyncio.to_thread(
            self._search, regex, roots, glob_filter, max_results
        )

    def _search_roots(self, path: Any) -> tuple[Path, ...]:
        if path in (None, ""):
            if self._prefixes is None:
                return (self._workspace_root,)
            roots = resolve_scope_roots(self._workspace_root, self._prefixes)
            if not roots:
                raise ValueError(
                    "allowed scope contains no existing directories: "
                    f"{prefix_scope_text(self._prefixes)}"
                )
            return roots
        if not isinstance(path, str):
            raise ValueError("'path' must be a string")
        target = assert_relative_within_scope(
            self._workspace_root,
            path,
            self._prefixes,
            allow_root=True,
        )
        relative = (
            "."
            if target == self._workspace_root
            else target.relative_to(self._workspace_root).as_posix()
        )
        # 排除检查先于存在性检查：不存在的被排除路径同样拒绝（子代理
        # 计入越权），也避免通过错误信息区分被排除文件是否存在。
        if self._path_exclusion is not None:
            message = self._path_exclusion(relative)
            if message is not None:
                raise ValueError(message)
        if not target.exists():
            raise ValueError(f"path does not exist: {path!r}")
        return (target,)

    def _search(
        self,
        regex: re.Pattern[str],
        roots: tuple[Path, ...],
        glob_filter: str | None,
        max_results: int,
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        scanned = 0
        truncated = False
        for root in roots:
            for file in _iter_files(root):
                scanned += 1
                if scanned > MAX_SCAN_FILES:
                    truncated = True
                    break
                relative = file.relative_to(
                    self._workspace_root
                ).as_posix()
                if self._is_excluded(relative):
                    continue
                if glob_filter and not fnmatch.fnmatch(
                    file.name, glob_filter
                ):
                    continue
                try:
                    if file.stat().st_size > MAX_SCAN_FILE_BYTES:
                        continue
                    text = file.read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError:
                    continue
                if "\x00" in text:
                    continue
                for line_number, line in enumerate(
                    text.splitlines(), start=1
                ):
                    if not regex.search(line):
                        continue
                    results.append(
                        {
                            "path": relative,
                            "line": line_number,
                            "text": line.strip()[:MAX_LINE_CHARS],
                        }
                    )
                    if len(results) >= max_results:
                        return _grep_payload(results, scanned, True)
            if truncated:
                break
        return _grep_payload(results, scanned, truncated)


class GlobTool(BaseTool):
    """在授权范围内按通配符匹配文件路径。"""

    def __init__(
        self,
        *,
        workspace_root: str | Path | None,
        prefixes: tuple[str, ...] | None = None,
        path_exclusion: Callable[[str], str | None] | None = None,
    ) -> None:
        self._workspace_root = workspace_root_path(workspace_root)
        self._prefixes = prefixes
        self._path_exclusion = path_exclusion

    def _is_excluded(self, relative: str) -> bool:
        """注入式排除钩子：返回拒绝消息的路径不进入结果（敏感路径等）。"""

        return (
            self._path_exclusion is not None
            and self._path_exclusion(relative) is not None
        )

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="glob",
            description=(
                "Match file paths with a shell-style pattern inside the "
                f"allowed scope ({prefix_scope_text(self._prefixes)}), "
                "e.g. 'src/**/*.py' or '*.toml'. Returns sorted relative "
                f"paths, at most {MAX_GLOB_FILES} files."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "shell 通配模式（支持前缀 '**/'）。",
                    },
                    "max_files": {
                        "type": "integer",
                        "default": DEFAULT_GLOB_FILES,
                    },
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            strict=False,
        )

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("'pattern' must be a non-empty string")
        max_files = bound_count(
            arguments.get("max_files"),
            default=DEFAULT_GLOB_FILES,
            limit=MAX_GLOB_FILES,
            field="max_files",
        )
        roots = (
            (self._workspace_root,)
            if self._prefixes is None
            else resolve_scope_roots(self._workspace_root, self._prefixes)
        )
        if not roots:
            raise ValueError(
                "allowed scope contains no existing directories: "
                f"{prefix_scope_text(self._prefixes)}"
            )
        return await asyncio.to_thread(
            self._match, roots, pattern, max_files
        )

    def _match(
        self,
        roots: tuple[Path, ...],
        pattern: str,
        max_files: int,
    ) -> dict[str, Any]:
        files: list[str] = []
        scanned = 0
        truncated = False
        for root in roots:
            for file in _iter_files(root):
                scanned += 1
                if scanned > MAX_SCAN_FILES:
                    truncated = True
                    break
                relative = file.relative_to(
                    self._workspace_root
                ).as_posix()
                if self._is_excluded(relative):
                    continue
                if not _glob_match(relative, file.name, pattern):
                    continue
                files.append(relative)
                if len(files) >= max_files:
                    return {
                        "files": sorted(files),
                        "count": len(files),
                        "truncated": True,
                    }
            if truncated:
                break
        return {
            "files": sorted(files),
            "count": len(files),
            "truncated": truncated,
        }


__all__ = [
    "DEFAULT_GLOB_FILES",
    "DEFAULT_GREP_RESULTS",
    "GlobTool",
    "GrepTool",
    "MAX_GLOB_FILES",
    "MAX_GREP_RESULTS",
]
