"""子代理只读探索工具：grep / glob / git 系列的作用域与输出边界。"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.subagent import (
    GitDiffTool,
    GitLogTool,
    GitShowTool,
    GitStatusTool,
    GlobTool,
    GrepTool,
)
from app.subagent.scoped_tools import MAX_SCOPED_READ_CHARS

GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(GIT is None, reason="git is not available")


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return workspace


async def test_grep_limits_search_to_prefixes(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text(
        "def login():\n    return 1\n", encoding="utf-8"
    )
    (workspace / "docs").mkdir()
    (workspace / "docs" / "readme.md").write_text(
        "login docs\n", encoding="utf-8"
    )
    (workspace / "secret.txt").write_text("login secret\n", encoding="utf-8")

    tool = GrepTool(workspace_root=workspace, prefixes=("src",))
    payload = await tool.execute({"pattern": "login"})

    assert payload["count"] == 1
    assert payload["results"][0]["path"] == "src/app.py"
    assert payload["results"][0]["line"] == 1
    assert payload["truncated"] is False

    # 越界路径被拒绝（含 '..' 绕行）。
    for bad in ("docs", "secret.txt", "../outside", "src/../docs"):
        with pytest.raises(ValueError):
            await tool.execute({"pattern": "login", "path": bad})


async def test_grep_glob_filter_case_and_limits(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "src").mkdir()
    (workspace / "src" / "a.py").write_text(
        "TODO: one\nTODO: two\n", encoding="utf-8"
    )
    (workspace / "src" / "b.txt").write_text("TODO: three\n", encoding="utf-8")

    tool = GrepTool(workspace_root=workspace, prefixes=("src",))
    payload = await tool.execute({"pattern": "todo", "case_sensitive": False})
    assert payload["count"] == 3

    payload = await tool.execute({"pattern": "TODO", "glob": "*.py"})
    assert [item["path"] for item in payload["results"]] == [
        "src/a.py",
        "src/a.py",
    ]

    payload = await tool.execute({"pattern": "TODO", "max_results": 2})
    assert payload["count"] == 2
    assert payload["truncated"] is True

    with pytest.raises(ValueError):
        await tool.execute({"pattern": "([unclosed"})
    with pytest.raises(ValueError):
        await tool.execute({"pattern": "x", "max_results": 0})


async def test_glob_matches_within_scope(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "src" / "sub").mkdir(parents=True)
    (workspace / "src" / "a.py").write_text("a", encoding="utf-8")
    (workspace / "src" / "sub" / "b.py").write_text("b", encoding="utf-8")
    (workspace / "docs").mkdir()
    (workspace / "docs" / "c.md").write_text("c", encoding="utf-8")

    tool = GlobTool(workspace_root=workspace, prefixes=("src",))
    payload = await tool.execute({"pattern": "**/*.py"})
    assert payload["files"] == ["src/a.py", "src/sub/b.py"]

    payload = await tool.execute({"pattern": "*.md"})
    assert payload["files"] == []  # docs 不在范围内

    payload = await tool.execute({"pattern": "**/*.py", "max_files": 1})
    assert payload["count"] == 1
    assert payload["truncated"] is True


async def test_scoped_read_is_capped(tmp_path: Path) -> None:
    from app.subagent import ScopedReadFileTool
    from app.tools.builtin.read_file import ReadFileTool
    from app.tools.registry import ToolRegistry

    workspace = _workspace(tmp_path)
    big = "x" * (MAX_SCOPED_READ_CHARS + 500)
    (workspace / "big.txt").write_text(big, encoding="utf-8")

    registry = ToolRegistry()
    registry.register(ReadFileTool(workspace))
    tool = ScopedReadFileTool(
        registry.get("read_file"),
        workspace_root=workspace,
        prefixes=None,
    )
    result = await tool.execute({"path": "big.txt"})
    assert result.endswith(f"limit {MAX_SCOPED_READ_CHARS} chars reached]")
    assert len(result) < len(big)


def _git(workspace: Path, *args: str) -> str:
    completed = subprocess.run(
        [
            GIT,
            "-c",
            "user.name=offline-test",
            "-c",
            "user.email=offline@example.com",
            *args,
        ],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _git(workspace, "init", "-q")
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text(
        "def login():\n    return 1\n", encoding="utf-8"
    )
    (workspace / "docs").mkdir()
    (workspace / "docs" / "readme.md").write_text(
        "login docs\n", encoding="utf-8"
    )
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "initial")
    return workspace


async def test_git_status_and_diff_respect_scope(tmp_path: Path) -> None:
    workspace = _make_repo(tmp_path)
    (workspace / "src" / "app.py").write_text(
        "def login():\n    return 2\n", encoding="utf-8"
    )
    (workspace / "docs" / "readme.md").write_text(
        "login docs v2\n", encoding="utf-8"
    )

    scoped = GitStatusTool(workspace_root=workspace, prefixes=("src",))
    payload = await scoped.execute({})
    assert "src/app.py" in payload["output"]
    assert "docs/readme.md" not in payload["output"]

    unscoped = GitStatusTool(workspace_root=workspace, prefixes=None)
    payload = await unscoped.execute({})
    assert "src/app.py" in payload["output"]
    assert "docs/readme.md" in payload["output"]

    diff = GitDiffTool(workspace_root=workspace, prefixes=("src",))
    payload = await diff.execute({})
    assert "src/app.py" in payload["output"]
    assert "docs/readme.md" not in payload["output"]

    # staged 参数与越界 path。
    await diff.execute({"staged": True})
    with pytest.raises(ValueError):
        await diff.execute({"path": "docs"})
    with pytest.raises(ValueError):
        await diff.execute({"staged": "yes"})


async def test_git_log_and_show_follow_ancestor_chain(tmp_path: Path) -> None:
    workspace = _make_repo(tmp_path)
    (workspace / "src" / "app.py").write_text(
        "def login():\n    return 3\n", encoding="utf-8"
    )
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "second")

    log = GitLogTool(workspace_root=workspace, prefixes=("src",))
    payload = await log.execute({"max_count": 5})
    assert "second" in payload["output"]
    assert "initial" in payload["output"]

    show = GitShowTool(workspace_root=workspace, prefixes=("src",))
    payload = await show.execute({"rev": "HEAD"})
    assert "second" in payload["output"]
    assert "src/app.py" in payload["output"]

    payload = await show.execute({"rev": "HEAD~1"})
    assert "initial" in payload["output"]

    # rev 形态与祖先约束。
    for bad_rev in ("--help", "HEAD:src/app.py", "HEAD^{tree}", "zzzz"):
        with pytest.raises(ValueError):
            await show.execute({"rev": bad_rev})

    branch = _git(workspace, "branch", "--show-current")
    _git(workspace, "checkout", "-q", "-b", "feature")
    (workspace / "src" / "feature.py").write_text("x = 1\n", encoding="utf-8")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "feature")
    feature_sha = _git(workspace, "rev-parse", "HEAD")
    _git(workspace, "checkout", "-q", branch)

    with pytest.raises(ValueError, match="ancestor"):
        await show.execute({"rev": feature_sha})


async def test_git_tools_reject_non_repo_and_out_of_scope_path(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "src").mkdir()
    (workspace / "src" / "a.py").write_text("a", encoding="utf-8")
    (workspace / "b.txt").write_text("b", encoding="utf-8")

    status = GitStatusTool(workspace_root=workspace, prefixes=("src",))
    with pytest.raises(ValueError):
        await status.execute({})
    with pytest.raises(ValueError):
        await status.execute({"path": "b.txt"})

    log = GitLogTool(workspace_root=workspace, prefixes=("src",))
    with pytest.raises(ValueError):
        await log.execute({"path": "../outside"})
    with pytest.raises(ValueError):
        await log.execute({"max_count": "many"})


async def test_grep_and_glob_skip_sensitive_paths(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text(
        "TOKEN_LOOKUP = 1\n", encoding="utf-8"
    )
    (workspace / ".env").write_text("TOKEN_LOOKUP=secret\n", encoding="utf-8")
    (workspace / "keys").mkdir()
    (workspace / "keys" / "server.pem").write_text(
        "TOKEN_LOOKUP=secret\n", encoding="utf-8"
    )

    grep = GrepTool(workspace_root=workspace, prefixes=None)
    payload = await grep.execute({"pattern": "TOKEN_LOOKUP"})
    assert [item["path"] for item in payload["results"]] == ["src/app.py"]

    # 显式指向敏感文件被拒绝并带越权标记。
    for bad in (".env", "keys/server.pem"):
        with pytest.raises(ValueError, match="scope_violation"):
            await grep.execute({"pattern": "TOKEN_LOOKUP", "path": bad})

    # 敏感检查先于存在性检查：不存在的敏感路径同样拒绝并计数。
    with pytest.raises(ValueError, match="scope_violation"):
        await grep.execute({"pattern": "TOKEN_LOOKUP", "path": ".env.local"})

    glob = GlobTool(workspace_root=workspace, prefixes=None)
    payload = await glob.execute({"pattern": "**/*"})
    assert "src/app.py" in payload["files"]
    assert ".env" not in payload["files"]
    assert "keys/server.pem" not in payload["files"]


async def test_git_tools_filter_sensitive_paths_from_history(
    tmp_path: Path,
) -> None:
    workspace = _make_repo(tmp_path)
    (workspace / ".env").write_text(
        "API_KEY=sk-old-key-value-1234567890\n", encoding="utf-8"
    )
    (workspace / "src" / "app.py").write_text(
        "def login():\n    return 2\n", encoding="utf-8"
    )
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "add env and change app")

    show = GitShowTool(workspace_root=workspace, prefixes=None)
    payload = await show.execute({"rev": "HEAD"})
    assert "src/app.py" in payload["output"]
    assert ".env" not in payload["output"]
    assert "sk-old-key-value" not in payload["output"]
    assert "one file section was removed" in payload["output"]

    # 工作区状态同样隐去敏感路径条目（正向对照保留）。
    (workspace / ".env").write_text("API_KEY=sk-new-key-value-1234567890\n")
    (workspace / "src" / "app.py").write_text(
        "def login():\n    return 3\n", encoding="utf-8"
    )
    status = GitStatusTool(workspace_root=workspace, prefixes=None)
    payload = await status.execute({})
    assert "src/app.py" in payload["output"]
    assert ".env" not in payload["output"]

    # 显式指向敏感路径的 git 查询被拒绝并带越权标记。
    diff = GitDiffTool(workspace_root=workspace, prefixes=None)
    with pytest.raises(ValueError, match="scope_violation"):
        await diff.execute({"path": ".env"})
    with pytest.raises(ValueError, match="scope_violation"):
        await show.execute({"rev": "HEAD", "path": ".env"})


def test_filter_patch_and_status_handle_quoted_and_renamed_paths() -> None:
    from app.subagent.explore_tools import _filter_patch, _filter_status

    patch = (
        'diff --git "a/wei rd.txt" "b/wei rd.txt"\n'
        '--- "a/wei rd.txt"\n'
        '+++ "b/wei rd.txt"\n'
        "+ok\n"
        "diff --git a/.env b/.env\n"
        "--- a/.env\n"
        "+++ b/.env\n"
        "+SECRET=1\n"
        "diff --git a/src/a.py b/src/a.py\n"
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "+x\n"
    )
    filtered = _filter_patch(patch)
    assert "wei rd.txt" in filtered
    assert "SECRET=1" not in filtered
    assert ".env" not in filtered
    assert "src/a.py" in filtered
    assert "one file section was removed" in filtered

    status = (
        "## main\n"
        " M src/app.py\n"
        ' M ".env"\n'
        "?? keys/server.pem\n"
        "R  a.txt -> .ssh/config\n"
        "?? notes.txt\n"
    )
    filtered_status = _filter_status(status)
    assert "src/app.py" in filtered_status
    assert ".env" not in filtered_status
    assert "server.pem" not in filtered_status
    assert ".ssh/config" not in filtered_status
    assert "notes.txt" in filtered_status


def test_filter_patch_handles_combined_diff_sections() -> None:
    from app.subagent.explore_tools import _filter_patch

    patch = (
        "commit c188f54\n"
        "Merge: 6dc3749 26a0dc8\n"
        "\n"
        "    merge resolve\n"
        "\n"
        "diff --cc .env\n"
        "index eb9e66e,34bc6f8..671d374\n"
        "--- a/.env\n"
        "+++ b/.env\n"
        "@@@ -1,1 -1,1 +1,1 @@@\n"
        "- SECRET=main\n"
        " -SECRET=feature\n"
        "++SECRET=merged\n"
        "diff --cc wei rd.txt\n"
        "index 5799d3c,b9cf0e5..0000000\n"
        "--- a/wei rd.txt\n"
        "+++ b/wei rd.txt\n"
        "@@@ -1,1 -1,1 +1,1 @@@\n"
        "- hello main\n"
        " -hello feature\n"
        "++hello merged\n"
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1 +1 @@\n"
        "-x\n"
        "+y\n"
    )

    filtered = _filter_patch(patch)

    # 敏感文件的 combined diff 段整体丢弃。
    assert "SECRET" not in filtered
    assert "diff --cc .env" not in filtered
    # 非敏感文件的 combined / 常规段保留，敏感段处有策略说明。
    assert "wei rd.txt" in filtered
    assert "hello merged" in filtered
    assert "src/app.py" in filtered
    assert "one file section was removed" in filtered


async def test_git_show_filters_merge_combined_diff(tmp_path: Path) -> None:
    workspace = _make_repo(tmp_path)
    (workspace / ".env").write_text(
        "SECRET=base\nKEEP=shared\nMODE=base\n", encoding="utf-8"
    )
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-qm", "add env")
    base_branch = _git(workspace, "rev-parse", "--abbrev-ref", "HEAD")

    _git(workspace, "checkout", "-qb", "feature")
    (workspace / ".env").write_text(
        "SECRET=base\nKEEP=shared\nMODE=feature\n", encoding="utf-8"
    )
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-qm", "feature env")

    _git(workspace, "checkout", "-q", base_branch)
    (workspace / ".env").write_text(
        "SECRET=main\nKEEP=shared\nMODE=base\n", encoding="utf-8"
    )
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-qm", "main env")
    _git(workspace, "merge", "-q", "-m", "merge feature", "feature")

    # 先确认原始输出确实是 combined diff，保证用例覆盖到目标格式。
    raw = subprocess.run(
        [GIT, "show", "--no-color", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "diff --cc .env" in raw
    assert "MODE=feature" in raw

    show = GitShowTool(workspace_root=workspace, prefixes=None)
    payload = await show.execute({"rev": "HEAD"})
    assert "MODE=feature" not in payload["output"]
    assert "SECRET=main" not in payload["output"]
    assert "diff --cc" not in payload["output"]
    assert "one file section was removed" in payload["output"]


async def test_git_diff_filters_conflicted_combined_diff(
    tmp_path: Path,
) -> None:
    (tmp_path / "conflict").mkdir()
    workspace = _make_repo(tmp_path / "conflict")
    (workspace / ".env").write_text("SECRET=base\n", encoding="utf-8")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-qm", "add env")
    base_branch = _git(workspace, "rev-parse", "--abbrev-ref", "HEAD")

    _git(workspace, "checkout", "-qb", "feature")
    (workspace / ".env").write_text("SECRET=feature\n", encoding="utf-8")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-qm", "feature env")

    _git(workspace, "checkout", "-q", base_branch)
    (workspace / ".env").write_text("SECRET=main\n", encoding="utf-8")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-qm", "main env")

    conflicted = subprocess.run(
        [
            GIT,
            "-c",
            "user.name=offline-test",
            "-c",
            "user.email=offline@example.com",
            "merge",
            "feature",
        ],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    assert conflicted.returncode != 0

    # 冲突态的 git diff 同样走 combined diff。
    raw = subprocess.run(
        [GIT, "diff", "--no-color"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "diff --cc .env" in raw

    diff = GitDiffTool(workspace_root=workspace, prefixes=None)
    payload = await diff.execute({})
    assert "SECRET" not in payload["output"]
    assert "diff --cc" not in payload["output"]
    assert "one file section was removed" in payload["output"]


async def test_grep_and_glob_skip_symlinked_files(
    tmp_path: Path, symlink_support: None
) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "src").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(
        "LEAKED-TOKEN-12345\n", encoding="utf-8"
    )
    (workspace / "src" / "real.txt").write_text(
        "LEAKED-TOKEN-12345\n", encoding="utf-8"
    )
    (workspace / "src" / "linked.txt").symlink_to(outside / "secret.txt")
    (workspace / "src" / "inner.txt").symlink_to(
        workspace / "src" / "real.txt"
    )

    grep = GrepTool(workspace_root=workspace, prefixes=("src",))
    payload = await grep.execute({"pattern": "LEAKED-TOKEN"})
    # 链接一律不穿透：工作区外与工作区内的文件链接都跳过。
    assert [item["path"] for item in payload["results"]] == ["src/real.txt"]

    glob = GlobTool(workspace_root=workspace, prefixes=("src",))
    payload = await glob.execute({"pattern": "**/*.txt"})
    assert payload["files"] == ["src/real.txt"]


async def test_grep_and_glob_do_not_enter_directory_junctions(
    tmp_path: Path,
) -> None:
    if sys.platform != "win32":
        pytest.skip("目录 junction 是 Windows 特性")

    workspace = _workspace(tmp_path)
    (workspace / "src").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(
        "LEAKED-TOKEN-12345\n", encoding="utf-8"
    )
    junction = workspace / "src" / "linked_dir"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip(f"当前环境无法创建 junction：{created.stderr.strip()}")

    grep = GrepTool(workspace_root=workspace, prefixes=("src",))
    payload = await grep.execute({"pattern": "LEAKED-TOKEN"})
    assert payload["count"] == 0
    assert payload["scanned_files"] == 0

    glob = GlobTool(workspace_root=workspace, prefixes=("src",))
    payload = await glob.execute({"pattern": "**/*.txt"})
    assert payload["files"] == []
