"""子代理只读探索工具：grep / glob / git 系列的作用域与输出边界。"""

from __future__ import annotations

import shutil
import subprocess
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
