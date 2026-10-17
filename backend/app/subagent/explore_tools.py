"""子代理专用的只读 git 探索工具：git_status / git_diff / git_log / git_show。

构造时绑定工作区与任务单范围（前缀集合），每次调用都在执行层做确定性
路径校验；只读 HEAD 祖先链：显式 argv（不走 shell）、最小环境、禁用分页与
交互提示，不执行任何写操作；输出有硬上限，patch / status 中命中敏感路径
策略的文件片段被整体隐去。
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
from pathlib import Path
from typing import Any

from app.models.types import ToolDefinition
from app.tools.base import BaseTool
from app.tools.builtin._scope import (
    assert_relative_within_scope,
    prefix_scope_text,
)
from app.tools.builtin._workspace import workspace_root_path
from app.tools.builtin.explore import bound_count

from .redaction import is_sensitive_path, sensitive_rejection_text

MAX_GIT_OUTPUT_CHARS = 12_000
MAX_GIT_OUTPUT_LINES = 400
GIT_TIMEOUT_SECONDS = 20.0
MAX_GIT_LOG_COUNT = 50
DEFAULT_GIT_LOG_COUNT = 20

_REV_PATTERN = re.compile(r"^(?:HEAD(?:~\d+)?|[0-9a-fA-F]{4,40})$")


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
        output, truncated = _bound_git_output(
            _filter_git_output(command, stdout)
        )
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
        if is_sensitive_path(relative):
            raise ValueError(sensitive_rejection_text(path))
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
        max_count = bound_count(
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


_PATCH_SECTION_MARKER = (
    "[one file section was removed by the sensitive path policy]"
)


def _strip_ab_prefix(path: str) -> str:
    if path[:2] in ("a/", "b/"):
        return path[2:]
    return path


_DIFF_HEADER_PREFIXES = ("diff --git ", "diff --cc ", "diff --combined ")


def _diff_header_paths(line: str) -> tuple[str, ...] | None:
    """解析 diff 头行涉及的工作区路径；无法解析返回 None（按敏感处理）。

    ``diff --git`` 的两个路径带 ``a/`` / ``b/`` 前缀；combined diff（合并
    提交的 ``git show`` 与冲突态的 ``git diff``）的 ``diff --cc`` /
    ``diff --combined`` 头只带一个路径。两类头对空格都不加引号，只有特殊
    字符（非 ASCII 等）才引号包裹。
    """

    if line.startswith("diff --git "):
        rest = line[len("diff --git ") :]
        if rest.startswith('"'):
            try:
                tokens = shlex.split(rest)
            except ValueError:
                return None
            if len(tokens) != 2:
                return None
            return _strip_ab_prefix(tokens[0]), _strip_ab_prefix(tokens[1])
        index = rest.rfind(" b/")
        if index < 0 or not rest.startswith("a/"):
            return None
        return (
            _strip_ab_prefix(rest[:index]),
            _strip_ab_prefix(rest[index + 1 :]),
        )
    for prefix in ("diff --cc ", "diff --combined "):
        if not line.startswith(prefix):
            continue
        rest = line[len(prefix) :]
        if rest.startswith('"'):
            try:
                tokens = shlex.split(rest)
            except ValueError:
                return None
        else:
            tokens = rest.split()
        if not tokens:
            return None
        # 候选含原样整串：空格切分不改变「段名 / 后缀」判定的敏感性。
        candidates = (*tokens, rest)
        return tuple(
            _strip_ab_prefix(candidate) for candidate in candidates
        )
    return None


def _filter_patch(text: str) -> str:
    """丢弃 patch 中命中敏感路径策略的文件片段。

    适用于 ``git diff`` / ``git show`` 的常规（``diff --git``）与 combined
    diff（``diff --cc`` / ``diff --combined``；合并提交与冲突态）。
    """

    kept: list[str] = []
    section: list[str] | None = None
    section_kept = True

    def flush() -> None:
        if section is None:
            return
        if section_kept:
            kept.extend(section)
        elif not kept or kept[-1] != _PATCH_SECTION_MARKER:
            kept.append(_PATCH_SECTION_MARKER)

    for line in text.split("\n"):
        if line.startswith(_DIFF_HEADER_PREFIXES):
            flush()
            paths = _diff_header_paths(line)
            section_kept = paths is not None and not any(
                is_sensitive_path(path) for path in paths
            )
            section = [line]
            continue
        if section is None:
            kept.append(line)
        else:
            section.append(line)
    flush()
    return "\n".join(kept)


def _unquote_status_path(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        return text[1:-1]
    return text


def _filter_status(text: str) -> str:
    """隐去状态行里命中敏感路径策略的条目（含重命名两侧）。"""

    kept: list[str] = []
    for line in text.split("\n"):
        stripped = line.rstrip()
        if len(stripped) < 4 or stripped.startswith("## "):
            kept.append(line)
            continue
        rest = stripped[3:]
        if " -> " in rest:
            left, right = rest.split(" -> ", 1)
            paths = [
                _unquote_status_path(left),
                _unquote_status_path(right),
            ]
        else:
            paths = [_unquote_status_path(rest)]
        if any(is_sensitive_path(path) for path in paths):
            continue
        kept.append(line)
    return "\n".join(kept)


def _filter_git_output(command: str, text: str) -> str:
    if command == "status":
        return _filter_status(text)
    if command in ("diff", "show"):
        return _filter_patch(text)
    return text


__all__ = [
    "DEFAULT_GIT_LOG_COUNT",
    "GitDiffTool",
    "GitLogTool",
    "GitShowTool",
    "GitStatusTool",
    "MAX_GIT_LOG_COUNT",
    "MAX_GIT_OUTPUT_CHARS",
]
