"""内置只读探索工具（grep / glob）：主 Agent 装配与工作区边界。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.tools import build_builtin_tool_registry
from app.tools.builtin.explore import GlobTool, GrepTool


def test_builtin_registry_includes_explore_tools(tmp_path: Path) -> None:
    registry = build_builtin_tool_registry(tmp_path)
    names = set(registry.names())
    assert {"grep", "glob"} <= names


async def test_grep_searches_whole_workspace(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "def login():\n    return 1\n", encoding="utf-8"
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "readme.md").write_text(
        "login docs\n", encoding="utf-8"
    )

    tool = GrepTool(workspace_root=tmp_path)
    payload = await tool.execute({"pattern": "def login"})
    assert [item["path"] for item in payload["results"]] == ["src/app.py"]


async def test_glob_matches_whole_workspace(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / "src" / "b.md").write_text("x", encoding="utf-8")

    tool = GlobTool(workspace_root=tmp_path)
    payload = await tool.execute({"pattern": "**/*.py"})
    assert payload["files"] == ["src/a.py"]


async def test_grep_rejects_path_escape(tmp_path: Path) -> None:
    tool = GrepTool(workspace_root=tmp_path)
    with pytest.raises(ValueError):
        await tool.execute({"pattern": "x", "path": "../outside"})
