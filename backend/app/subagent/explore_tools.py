"""子代理专用的只读探索工具：grep / glob / git 系列。

构造时绑定工作区与任务单范围（前缀集合），每次调用都在执行层做确定性
路径校验；输出有硬上限（结果条数 / 行数 / 字符数），保证回流的数据有界。
git 工具只读 HEAD 祖先链：显式 argv（不走 shell）、最小环境、禁用分页与
交互提示，不执行任何写操作。
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
from pathlib import Path
from typing import Any

from app.models.types import ToolDefinition
from app.tools.base import BaseTool
from app.tools.builtin._workspace import workspace_root_path

from .path_scope import (
    assert_relative_within_scope,
    prefix_scope_text,
    resolve_scope_roots,
)

MAX_GREP_RESULTS = 200
DEFAULT_GREP_RESULTS = 60
MAX_GLOB_FILES = 500
DEFAULT_GLOB_FILES = 100
MAX_SCAN_FILES = 5_000
MAX_SCAN_FILE_BYTES = 1_048_576
MAX_LINE_CHARS = 400
MAX_GIT_OUTPUT_CHARS = 12_000
MAX_GIT_OUTPUT_LINES = 400
GIT_TIMEOUT_SECONDS = 20.0
MAX_GIT_LOG_COUNT = 50
DEFAULT_GIT_LOG_COUNT = 20

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

_REV_PATTERN = re.compile(r"^(?:HEAD(?:~\d+)?|[0-9a-fA-F]{4,40})$")


def _bound_count(value: Any, *, default: int, limit: int, field: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"'{field}' must be an integer")
    if value < 1:
        raise ValueError(f"'{field}' must be >= 1")
    return min(value, limit)


def _iter_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name for name in dirnames if name not in _SKIP_DIRS
        )
        for name in sorted(filenames):
            files.append(Path(dirpath) / name)
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
        prefixes: tuple[str, ...] | None,
    ) -> None:
        self._workspace_root = workspace_root_path(workspace_root)
        self._prefixes = prefixes

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="grep",
            description=(
                "Search file contents with a Python regular expression "
                f"inside the granted scope ({prefix_scope_text(self._prefixes)}). "
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
        max_results = _bound_count(
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
                    "granted scope contains no existing directories: "
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
                relative = file.relative_to(
                    self._workspace_root
                ).as_posix()
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
        prefixes: tuple[str, ...] | None,
    ) -> None:
        self._workspace_root = workspace_root_path(workspace_root)
        self._prefixes = prefixes

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="glob",
            description=(
                "Match file paths with a shell-style pattern inside the "
                f"granted scope ({prefix_scope_text(self._prefixes)}), "
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
        max_files = _bound_count(
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
                "granted scope contains no existing directories: "
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


class _GitTool(BaseTool):
    """git 只读基类：显式 argv、最小环境、输出有界。"""

    def __init__(
        self,
        *,
        workspace_root: str | Path | None,
        prefixes: tuple[str, ...] | None,
    ) -> None:
        self._workspace_root = workspace_root_path(workspace_root)
        self._prefixes = prefixes

    async def _git(self, args: list[str]) -> tuple[int, str, str]:
        env = {
            key: os.environ[key]
            for key in (
                "PATH",
                "SYSTEMROOT",
                "COMSPEC",
                "PATHEXT",
                "WINDIR",
                "TEMP",
                "TMP",
                "HOME",
                "USERPROFILE",
            )
            if key in os.environ
        }
        env.update(
            {
                "GIT_PAGER": "cat",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_OPTIONAL_LOCKS": "0",
            }
        )
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                "-c",
                "core.fsmonitor=false",
                "--no-pager",
                *args,
                cwd=str(self._workspace_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except OSError:  # pragma: no cover - 取决于运行环境
            raise ValueError("git is not available in this environment") from None
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=GIT_TIMEOUT_SECONDS
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise ValueError("git command timed out") from None
        return (
            process.returncode or 0,
            stdout.decode("utf-8", "replace"),
            stderr.decode("utf-8", "replace"),
        )

    async def _git_checked(
        self, args: list[str], *, command: str
    ) -> dict[str, Any]:
        code, stdout, stderr = await self._git(args)
        if code != 0:
            detail = (stderr.strip() or stdout.strip() or "unknown error")
            raise ValueError(
                f"git {command} failed: {detail.splitlines()[0][:300]}"
            )
        output, truncated = _bound_git_output(stdout)
        return {
            "command": " ".join(["git", *args]),
            "output": output,
            "truncated": truncated,
        }

    def _pathspecs(self, path: Any) -> list[str]:
        """把可选 path 参数解析为 git pathspec；缺省回落到范围前缀。"""

        if path in (None, ""):
            return list(self._prefixes or [])
        if not isinstance(path, str):
            raise ValueError("'path' must be a string")
        assert_relative_within_scope(
            self._workspace_root,
            path,
            self._prefixes,
            allow_root=True,
        )
        return [path]


class GitStatusTool(_GitTool):
    """读取工作区状态（只读 HEAD 祖先链之外的信息不越界）。"""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="git_status",
            description=(
                "Show working tree status (porcelain) from the local git "
                f"repository, limited to {prefix_scope_text(self._prefixes)}."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "可选：限定到某个路径。",
                    }
                },
                "additionalProperties": False,
            },
            strict=False,
        )

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        pathspecs = self._pathspecs(arguments.get("path"))
        args = [
            "status",
            "--porcelain=v1",
            "--branch",
            "--untracked-files=all",
        ]
        if pathspecs:
            args.extend(["--", *pathspecs])
        return await self._git_checked(args, command="status")


class GitDiffTool(_GitTool):
    """读取工作区差异（可选已暂存差异）。"""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="git_diff",
            description=(
                "Show diff from the local git repository (unstaged by "
                "default, staged=true for --cached), limited to "
                f"{prefix_scope_text(self._prefixes)}."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "可选：限定到某个路径。",
                    },
                    "staged": {
                        "type": "boolean",
                        "default": False,
                        "description": "true 时显示已暂存差异（--cached）。",
                    },
                },
                "additionalProperties": False,
            },
            strict=False,
        )

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        staged = arguments.get("staged", False)
        if not isinstance(staged, bool):
            raise ValueError("'staged' must be a boolean")
        pathspecs = self._pathspecs(arguments.get("path"))
        args = ["diff", "--no-color"]
        if staged:
            args.append("--cached")
        if pathspecs:
            args.extend(["--", *pathspecs])
        return await self._git_checked(args, command="diff")


class GitLogTool(_GitTool):
    """读取提交历史（HEAD 祖先链）。"""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="git_log",
            description=(
                "Show recent commits reachable from HEAD (one line per "
                "commit), optionally limited to a path inside "
                f"{prefix_scope_text(self._prefixes)}."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "可选：只看触及该路径的提交。",
                    },
                    "max_count": {
                        "type": "integer",
                        "default": DEFAULT_GIT_LOG_COUNT,
                    },
                },
                "additionalProperties": False,
            },
            strict=False,
        )

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        max_count = _bound_count(
            arguments.get("max_count"),
            default=DEFAULT_GIT_LOG_COUNT,
            limit=MAX_GIT_LOG_COUNT,
            field="max_count",
        )
        pathspecs = self._pathspecs(arguments.get("path"))
        args = [
            "log",
            "--no-color",
            "--date=short",
            "--format=%h %ad %an %s",
            "-n",
            str(max_count),
        ]
        if pathspecs:
            args.extend(["--", *pathspecs])
        return await self._git_checked(args, command="log")


class GitShowTool(_GitTool):
    """读取指定提交的内容（rev 必须是 HEAD 祖先链上的提交）。"""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="git_show",
            description=(
                "Show a commit reachable from HEAD (git show), optionally "
                "limited to a path inside "
                f"{prefix_scope_text(self._prefixes)}. rev 形如 HEAD、"
                "HEAD~2 或十六进制提交号。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "rev": {
                        "type": "string",
                        "default": "HEAD",
                        "description": "提交标识（HEAD / HEAD~N / 十六进制）。",
                    },
                    "path": {
                        "type": "string",
                        "description": "可选：只看该路径下的变更。",
                    },
                },
                "additionalProperties": False,
            },
            strict=False,
        )

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        rev = arguments.get("rev", "HEAD")
        if rev in (None, ""):
            rev = "HEAD"
        if not isinstance(rev, str) or not _REV_PATTERN.match(rev):
            raise ValueError(
                "'rev' must be HEAD, HEAD~N or a hex commit id"
            )
        if rev != "HEAD":
            code, _, _ = await self._git(
                ["merge-base", "--is-ancestor", rev, "HEAD"]
            )
            if code != 0:
                raise ValueError(
                    f"rev is not an ancestor of HEAD: {rev!r}"
                )
        pathspecs = self._pathspecs(arguments.get("path"))
        args = ["show", "--no-color", rev]
        if pathspecs:
            args.extend(["--", *pathspecs])
        return await self._git_checked(args, command="show")


def _bound_git_output(text: str) -> tuple[str, bool]:
    lines = text.splitlines()
    truncated = False
    if len(lines) > MAX_GIT_OUTPUT_LINES:
        lines = lines[:MAX_GIT_OUTPUT_LINES]
        truncated = True
    output = "\n".join(lines)
    if len(output) > MAX_GIT_OUTPUT_CHARS:
        output = output[:MAX_GIT_OUTPUT_CHARS]
        truncated = True
    return output, truncated


__all__ = [
    "DEFAULT_GIT_LOG_COUNT",
    "DEFAULT_GLOB_FILES",
    "DEFAULT_GREP_RESULTS",
    "GitDiffTool",
    "GitLogTool",
    "GitShowTool",
    "GitStatusTool",
    "GlobTool",
    "GrepTool",
    "MAX_GIT_LOG_COUNT",
    "MAX_GIT_OUTPUT_CHARS",
    "MAX_GLOB_FILES",
    "MAX_GREP_RESULTS",
]
