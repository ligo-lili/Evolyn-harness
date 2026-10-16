"""能力对应的受限工具包装：只收紧不放开。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models.types import ToolPermission
from app.subagent import (
    CapabilityError,
    ScopedListFilesTool,
    ScopedReadFileTool,
    ScopedWebFetchTool,
    build_scoped_tools,
    parse_grants,
)
from app.tools.builtin.list_files import ListFilesTool
from app.tools.builtin.read_file import ReadFileTool
from app.tools.registry import ToolRegistry
from tests.offline.subagent.harness import FakeHttpTool


def _registry(tmp_path: Path, *, http_tool: FakeHttpTool | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ReadFileTool(tmp_path))
    registry.register(ListFilesTool(tmp_path))
    if http_tool is not None:
        registry.register(http_tool)
    return registry


def _tool(tools, name: str):
    return next(tool for tool in tools if tool.definition.name == name)


async def test_scoped_read_file_enforces_prefix(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hello')", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "readme.md").write_text("readme", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")

    tools = build_scoped_tools(
        parse_grants(["read_repo:src"]),
        _registry(tmp_path),
        tmp_path,
    )
    read_tool = _tool(tools, "read_file")
    assert isinstance(read_tool, ScopedReadFileTool)

    assert "hello" in await read_tool.execute({"path": "src/app.py"})
    with pytest.raises(ValueError):
        await read_tool.execute({"path": "docs/readme.md"})
    with pytest.raises(ValueError):
        await read_tool.execute({"path": "secret.txt"})
    with pytest.raises(ValueError):
        await read_tool.execute({"path": "src/../secret.txt"})
    with pytest.raises(ValueError):
        await read_tool.execute({"path": "../outside.txt"})
    assert "src/" in read_tool.definition.description


async def test_scoped_read_file_without_argument_covers_workspace(
    tmp_path: Path,
) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "readme.md").write_text("readme", encoding="utf-8")

    tools = build_scoped_tools(
        parse_grants(["read_repo"]),
        _registry(tmp_path),
        tmp_path,
    )
    read_tool = _tool(tools, "read_file")

    assert "readme" in await read_tool.execute({"path": "docs/readme.md"})
    with pytest.raises(ValueError):
        await read_tool.execute({"path": "../outside.txt"})


async def test_scoped_list_files_defaults_to_granted_prefixes(tmp_path: Path) -> None:
    (tmp_path / "src" / "sub").mkdir(parents=True)
    (tmp_path / "src" / "a.py").write_text("a", encoding="utf-8")
    (tmp_path / "src" / "sub" / "b.py").write_text("b", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "c.md").write_text("c", encoding="utf-8")

    tools = build_scoped_tools(
        parse_grants(["read_repo:src"]),
        _registry(tmp_path),
        tmp_path,
    )
    list_tool = _tool(tools, "list_files")
    assert isinstance(list_tool, ScopedListFilesTool)

    payload = await list_tool.execute({})
    assert payload["files"] == ["src/a.py", "src/sub/b.py"]
    assert payload["count"] == 2

    inside = await list_tool.execute({"directory": "src/sub"})
    assert inside["files"] == ["src/sub/b.py"]

    with pytest.raises(ValueError):
        await list_tool.execute({"directory": "docs"})


async def test_scoped_list_files_merges_multiple_prefixes(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("a", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "c.md").write_text("c", encoding="utf-8")

    tools = build_scoped_tools(
        parse_grants(["read_repo:src", "read_repo:docs"]),
        _registry(tmp_path),
        tmp_path,
    )
    list_tool = _tool(tools, "list_files")

    payload = await list_tool.execute({})
    assert payload["files"] == ["docs/c.md", "src/a.py"]


async def test_scoped_web_fetch_enforces_host_scope_and_methods() -> None:
    delegate = FakeHttpTool()
    tool = ScopedWebFetchTool(delegate, hosts=("docs.python.org",))

    properties = tool.definition.parameters["properties"]
    assert properties["method"]["enum"] == ["GET", "HEAD"]
    assert "body" not in properties
    assert tool.definition.permission is ToolPermission.ALLOWED

    result = await tool.execute({"url": "https://docs.python.org/3/library/asyncio.html"})
    assert result["status"] == 200
    assert delegate.payloads[-1]["method"] == "GET"
    assert "body" not in delegate.payloads[-1]

    await tool.execute(
        {
            "url": "https://docs.python.org/3/",
            "method": "head",
            "headers": {"X-Test": "1"},
            "body": "ignored",
        }
    )
    assert delegate.payloads[-1]["method"] == "HEAD"
    assert delegate.payloads[-1]["headers"] == {"X-Test": "1"}
    assert "body" not in delegate.payloads[-1]

    # 子域名允许、相似域名拒绝。
    await tool.execute({"url": "https://www.docs.python.org/3/"})
    with pytest.raises(ValueError):
        await tool.execute({"url": "https://evil.com/docs.python.org"})
    with pytest.raises(ValueError):
        await tool.execute({"url": "https://docs.python.org.evil.com/"})
    with pytest.raises(ValueError):
        await tool.execute({"url": "https://evil-docs.python.org/"})

    with pytest.raises(ValueError):
        await tool.execute({"url": "https://docs.python.org/3/", "method": "POST"})
    with pytest.raises(ValueError):
        await tool.execute({"url": "ftp://docs.python.org/3/"})


async def test_build_scoped_tools_merges_prefixes_and_hosts(tmp_path: Path) -> None:
    http_tool = FakeHttpTool()
    registry = _registry(tmp_path, http_tool=http_tool)

    tools = build_scoped_tools(
        parse_grants(
            [
                "read_repo:src",
                "read_repo:docs",
                "web_fetch:a.example.com",
                "web_fetch:b.example.com",
            ]
        ),
        registry,
        tmp_path,
    )

    by_name = {tool.definition.name: tool for tool in tools}
    assert set(by_name) == {
        "read_file",
        "list_files",
        "grep",
        "glob",
        "git_status",
        "git_diff",
        "git_log",
        "git_show",
        "http_request",
    }
    # read_repo 未授予 web_search → 不装配搜索工具。
    assert "web_search" not in by_name

    fetch = by_name["http_request"]
    assert isinstance(fetch, ScopedWebFetchTool)
    await fetch.execute({"url": "https://a.example.com/x"})
    await fetch.execute({"url": "https://b.example.com/x"})
    with pytest.raises(ValueError):
        await fetch.execute({"url": "https://c.example.com/x"})


def test_build_scoped_tools_reports_missing_registry_tool(tmp_path: Path) -> None:
    with pytest.raises(CapabilityError):
        build_scoped_tools(parse_grants(["read_repo"]), ToolRegistry(), tmp_path)
    with pytest.raises(CapabilityError):
        build_scoped_tools(parse_grants(["web_search"]), ToolRegistry(), tmp_path)


async def test_scoped_read_file_rejects_sensitive_paths_with_marker(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys" / "server.pem").write_text("pem", encoding="utf-8")

    tools = build_scoped_tools(
        parse_grants(["read_repo"]),
        _registry(tmp_path),
        tmp_path,
    )
    read_tool = _tool(tools, "read_file")

    assert "hi" in await read_tool.execute({"path": "src/app.py"})
    # 敏感路径即使落在授权范围内也拒绝，消息带越权标记供计数。
    for bad in (".env", "keys/server.pem"):
        with pytest.raises(ValueError, match="scope_violation"):
            await read_tool.execute({"path": bad})


async def test_scoped_read_file_redacts_secrets_before_returning(
    tmp_path: Path,
) -> None:
    token = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
    (tmp_path / "config.py").write_text(
        f"API_KEY = '{token}'\n", encoding="utf-8"
    )

    tools = build_scoped_tools(
        parse_grants(["read_repo"]),
        _registry(tmp_path),
        tmp_path,
    )
    read_tool = _tool(tools, "read_file")

    result = await read_tool.execute({"path": "config.py"})

    assert token not in result
    assert "[REDACTED:openai-key]" in result


async def test_scoped_list_files_hides_sensitive_entries(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("a", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys" / "server.pem").write_text("pem", encoding="utf-8")

    tools = build_scoped_tools(
        parse_grants(["read_repo"]),
        _registry(tmp_path),
        tmp_path,
    )
    list_tool = _tool(tools, "list_files")

    payload = await list_tool.execute({})
    assert payload["files"] == ["src/app.py"]
    assert payload["count"] == 1

    # 直接列出敏感目录同样被拒绝并计数。
    with pytest.raises(ValueError, match="scope_violation"):
        await list_tool.execute({"directory": ".env"})

