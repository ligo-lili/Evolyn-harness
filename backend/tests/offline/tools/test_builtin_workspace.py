"""`app/tools/builtin/_workspace.py` 的 Windows 路径边界单测。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.tools.builtin._workspace import resolve_workspace_path


@pytest.mark.parametrize(
    "candidate",
    [
        r"\\srv\share\payload.txt",  # UNC
        "//srv/share/payload.txt",  # UNC（正斜杠写法）
        r"\\?\C:\Windows",  # verbatim 前缀
        r"\\?\UNC\srv\share",  # verbatim UNC
        "/abs/payload.txt",  # 悬挂根（无驱动器）
        r"\abs\payload.txt",  # 同上（反斜杠形式）
    ],
)
def test_rejects_out_of_workspace_spellings(tmp_path: Path, candidate: str) -> None:
    """UNC、verbatim、悬挂根等写法一律不能逃出工作区。"""

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError, match="workspace"):
        resolve_workspace_path(workspace, candidate)


def test_rejects_absolute_path_with_drive(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError, match="relative"):
        resolve_workspace_path(workspace, str(outside))


def test_same_drive_drive_relative_path_stays_inside(tmp_path: Path) -> None:
    """`C:foo` 盘符与工作区一致时由 pathlib 折叠为工作区相对路径，不逃逸。"""

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    resolved = resolve_workspace_path(workspace, f"{workspace.drive}folded.txt")

    assert resolved == workspace / "folded.txt"
    assert resolved.is_relative_to(workspace)


def test_other_drive_drive_relative_path_is_rejected(tmp_path: Path) -> None:
    """`X:foo` 盘符与工作区不同（含不存在的盘符）时一律拒绝。"""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    other_drive = "Z:" if workspace.drive.upper() != "Z:" else "Y:"

    with pytest.raises(ValueError, match="escapes"):
        resolve_workspace_path(workspace, f"{other_drive}escape.txt")
