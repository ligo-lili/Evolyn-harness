"""发现账本：record_finding 的归一化、确定性校验、幂等、上限与证据归档。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models.types import ToolCall
from app.subagent import EVIDENCE_TYPES
from app.subagent.findings import (
    FINDING_LIMITS,
    FindingLedger,
    RecordFindingTool,
)
from app.tools.hooks import ToolExecutionContext


def _context() -> ToolExecutionContext:
    return ToolExecutionContext(
        tool_call=ToolCall(id="call-finding", name="record_finding"),
        run_id="run-1",
        conversation_id="conv-1",
    )


def _make_ledger(
    tmp_path: Path,
    *,
    tier: str = "medium",
    prefixes: tuple[str, ...] | None = ("src",),
    files: dict[str, str] | None = None,
) -> FindingLedger:
    workspace = tmp_path / "ws"
    for name, content in (
        files or {"src/app.py": "def login():\n    pass\n"}
    ).items():
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return FindingLedger(
        workspace_root=workspace,
        access_prefixes=prefixes,
        tier=tier,
    )


def _exists_args(
    path: str = "src/app.py",
    *,
    symbol: str | None = None,
    claim: str = "app 模块存在。",
    lines: str = "1-2",
) -> dict:
    location: dict[str, str] = {"path": path, "lines": lines}
    if symbol:
        location["symbol"] = symbol
    return {"type": "exists", "claim": claim, "locations": [location]}


async def test_exists_finding_verified_and_duplicate_is_idempotent(
    tmp_path: Path,
) -> None:
    ledger = _make_ledger(tmp_path)
    ctx = _context()

    first = await ledger.record(_exists_args(symbol="def login"), ctx)
    assert first["finding_id"] == "f1"
    assert first["status"] == "accepted"
    assert first["verified"] is True
    assert first["reason"] == "verified"
    assert first["verified_count"] == 1
    assert first["limit"] == FINDING_LIMITS["medium"]

    # 重复提交（同参）命中 canonical key：返回既有 finding_id，不再入账。
    again = await ledger.record(_exists_args(symbol="def login"), ctx)
    assert again["status"] == "duplicate"
    assert again["finding_id"] == "f1"
    assert again["verified_count"] == 1
    assert len(ledger.entries) == 1
    assert [entry.claim for entry in ledger.listed()] == ["app 模块存在。"]


async def test_invalid_findings_are_recorded_but_not_listed(
    tmp_path: Path,
) -> None:
    ledger = _make_ledger(tmp_path)
    ctx = _context()

    bad = await ledger.record(_exists_args(symbol="def logout"), ctx)
    assert bad["status"] == "invalid"
    assert bad["verified"] is False
    assert bad["reason"] == "symbol_not_found"
    assert "校验未通过" in bad["message"]
    assert ledger.listed() == ()
    # 无效发现保留轨迹（供 Trace / 统计），但不进入清单。
    assert len(ledger.entries) == 1

    good = await ledger.record(
        _exists_args(symbol="def login", claim="login 存在。"), ctx
    )
    assert good["status"] == "accepted"
    assert [entry.finding_id for entry in ledger.listed()] == ["f2"]


async def test_scope_violation_is_counted_and_never_listed(tmp_path: Path) -> None:
    ledger = _make_ledger(tmp_path)
    ctx = _context()

    outside = await ledger.record(
        _exists_args(path="notes.txt", lines="1"), ctx
    )
    assert outside["status"] == "invalid"
    assert outside["reason"] == "path_outside_scope"
    assert ledger.scope_attempts == 1

    # 范围内但文件不存在：不算越权。
    missing = await ledger.record(_exists_args(path="src/missing.py"), ctx)
    assert missing["reason"] == "path_not_found"
    assert ledger.scope_attempts == 1
    assert ledger.listed() == ()


async def test_escape_paths_are_rejected_with_scope_violation_marker(
    tmp_path: Path,
) -> None:
    """绝对路径 / ``..`` 摆渡在参数规范化阶段就被拒绝，消息带统一标记。

    标记让子循环观察者把这类被拒调用计入 ``scope_violation``（与工具层
    路径越界共用同一上限），而不是当成普通参数错误。
    """

    ledger = _make_ledger(tmp_path)
    ctx = _context()

    with pytest.raises(ValueError, match="scope_violation"):
        await ledger.record(_exists_args(path="../outside.txt"), ctx)
    assert ledger.scope_attempts == 0

    with pytest.raises(ValueError, match="scope_violation"):
        await ledger.record(
            {
                "type": "absent",
                "claim": "越权范围没有 TODO。",
                "search": {"pattern": "TODO", "scope": ["../outside"]},
            },
            ctx,
        )
    assert ledger.scope_attempts == 0


async def test_absent_finding_reruns_search_deterministically(
    tmp_path: Path,
) -> None:
    ledger = _make_ledger(tmp_path, files={"src/app.py": "print('app')\n"})
    ctx = _context()

    verified = await ledger.record(
        {
            "type": "absent",
            "claim": "授权范围内没有 secret_token。",
            "search": {"pattern": "secret_token", "scope": ["src"]},
        },
        ctx,
    )
    assert verified["status"] == "accepted"
    entry = ledger.entries[0]
    assert entry.search == {"pattern": "secret_token", "scope": ["src"]}
    assert entry.as_payload()["search"] == {
        "pattern": "secret_token",
        "scope": ["src"],
    }

    contradicted = await ledger.record(
        {
            "type": "absent",
            "claim": "授权范围内没有 print 调用。",
            "search": {"pattern": "print", "scope": ["src"]},
        },
        ctx,
    )
    assert contradicted["status"] == "invalid"
    assert contradicted["reason"] == "absent_contradicted"

    outside = await ledger.record(
        {
            "type": "absent",
            "claim": "notes 目录没有 TODO。",
            "search": {"pattern": "TODO", "scope": ["notes"]},
        },
        ctx,
    )
    assert outside["reason"] == "scope_outside_access"
    assert ledger.scope_attempts == 1


async def test_tier_limit_keeps_trace_but_trims_listed_findings(
    tmp_path: Path,
) -> None:
    ledger = _make_ledger(tmp_path, tier="quick")
    ctx = _context()

    results = []
    for index in range(FINDING_LIMITS["quick"] + 1):
        results.append(
            await ledger.record(_exists_args(claim=f"第 {index} 条发现。"), ctx)
        )

    assert [item["status"] for item in results] == [
        "accepted"
    ] * FINDING_LIMITS["quick"] + ["over_limit"]
    assert results[-1]["verified"] is True
    assert "已满" in results[-1]["message"]
    # 超限发现仍保留轨迹与校验结果，只是不进入返回清单。
    assert len(ledger.verified()) == FINDING_LIMITS["quick"] + 1
    assert len(ledger.listed()) == FINDING_LIMITS["quick"]


async def test_argument_validation_and_line_ranges(tmp_path: Path) -> None:
    ledger = _make_ledger(tmp_path)
    ctx = _context()

    with pytest.raises(ValueError):
        await ledger.record({"type": "guess", "claim": "x"}, ctx)
    with pytest.raises(ValueError):
        await ledger.record({"type": "exists", "claim": "   "}, ctx)
    with pytest.raises(ValueError):
        await ledger.record({"type": "exists", "claim": "x"}, ctx)
    with pytest.raises(ValueError):
        await ledger.record({"type": "absent", "claim": "x"}, ctx)

    bad_lines = await ledger.record(_exists_args(lines="abc"), ctx)
    assert bad_lines["status"] == "invalid"
    assert bad_lines["reason"] == "invalid_lines"

    out_of_file = await ledger.record(_exists_args(lines="100-120"), ctx)
    assert out_of_file["reason"] == "lines_out_of_range"


async def test_reads_config_requires_adjacent_read_call(tmp_path: Path) -> None:
    ledger = _make_ledger(
        tmp_path,
        files={
            "src/config.py": (
                "MAX_TOKENS = 100\n"
                'TOKEN = os.getenv("MAX_TOKENS")\n'
            )
        },
    )
    ctx = _context()

    def reads_config(claim: str, lines: str, symbol: str) -> dict:
        return {
            "type": "reads_config",
            "claim": claim,
            "locations": [
                {"path": "src/config.py", "lines": lines, "symbol": symbol}
            ],
        }

    no_read = await ledger.record(
        reads_config("MAX_TOKENS 来自常量文件。", "1", "MAX_TOKENS"), ctx
    )
    assert no_read["reason"] == "no_adjacent_read"

    with_read = await ledger.record(
        reads_config("MAX_TOKENS 由环境变量注入。", "2", "MAX_TOKENS"), ctx
    )
    assert with_read["status"] == "accepted"

    missing_key = await ledger.record(
        reads_config("MAX_CONNECTIONS 由环境变量注入。", "2", "MAX_CONNECTIONS"),
        ctx,
    )
    assert missing_key["reason"] == "config_key_not_found"


async def test_verified_finding_archives_excerpt_via_callback(
    tmp_path: Path,
) -> None:
    archived: list[str] = []

    async def record_evidence(
        content: str, context: ToolExecutionContext
    ) -> str | None:
        archived.append(content)
        return "ev-1"

    workspace = tmp_path / "ws"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "app.py").write_text(
        "def login():\n    pass\n", encoding="utf-8"
    )
    ledger = FindingLedger(
        workspace_root=workspace,
        access_prefixes=("src",),
        tier="medium",
        record_evidence=record_evidence,
    )
    ctx = _context()

    receipt = await ledger.record(_exists_args(symbol="def login"), ctx)
    assert receipt["status"] == "accepted"
    entry = ledger.entries[0]
    assert entry.evidence_id == "ev-1"
    assert "src/app.py:1-2" in archived[0]
    assert "def login" in archived[0]

    # 无效发现不归档证据。
    invalid = await ledger.record(
        _exists_args(symbol="def logout", claim="logout 存在。"), ctx
    )
    assert invalid["status"] == "invalid"
    assert len(archived) == 1


async def test_record_finding_tool_definition_and_context_requirement(
    tmp_path: Path,
) -> None:
    ledger = _make_ledger(tmp_path)
    tool = RecordFindingTool(ledger)
    definition = tool.definition

    assert definition.name == "record_finding"
    assert definition.parameters["required"] == ["type", "claim"]
    assert set(definition.parameters["properties"]["type"]["enum"]) == set(
        EVIDENCE_TYPES
    )

    # 无 run 上下文直接调用（普通 execute）必须失败：协议工具只存在于子循环。
    with pytest.raises(ValueError):
        await tool.execute({})

    receipt = await tool.execute_with_context(
        _exists_args(symbol="def login"), _context()
    )
    assert receipt["status"] == "accepted"
    assert receipt["finding_id"] == "f1"


async def test_sensitive_path_finding_is_rejected_and_counted(
    tmp_path: Path,
) -> None:
    ledger = _make_ledger(
        tmp_path,
        prefixes=None,
        files={
            "src/app.py": "def login():\n    pass\n",
            ".env": "SECRET=1\n",
            ".ssh/config": "Host *\n",
        },
    )
    ctx = _context()

    env = await ledger.record(_exists_args(path=".env", lines="1"), ctx)
    assert env["status"] == "invalid"
    assert env["reason"] == "sensitive_path"
    assert ledger.scope_attempts == 1

    # absent 的 search.scope 指向敏感目录同样被拒并计数。
    absent = await ledger.record(
        {
            "type": "absent",
            "claim": "配置目录没有 TODO。",
            "search": {"pattern": "TODO", "scope": [".ssh"]},
        },
        ctx,
    )
    assert absent["status"] == "invalid"
    assert absent["reason"] == "sensitive_path"
    assert ledger.scope_attempts == 2
    assert ledger.listed() == ()


async def test_excerpt_is_redacted_before_evidence_archive(
    tmp_path: Path,
) -> None:
    token = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "config.py").write_text(
        f"API_KEY = '{token}'\n", encoding="utf-8"
    )
    archived: list[str] = []

    async def record_evidence(
        content: str, context: ToolExecutionContext
    ) -> str | None:
        archived.append(content)
        return "ev-1"

    ledger = FindingLedger(
        workspace_root=workspace,
        access_prefixes=None,
        tier="medium",
        record_evidence=record_evidence,
    )

    receipt = await ledger.record(
        {
            "type": "exists",
            "claim": "API_KEY 常量存在。",
            "locations": [
                {"path": "config.py", "lines": "1", "symbol": "API_KEY"}
            ],
        },
        _context(),
    )

    assert receipt["status"] == "accepted"
    assert ledger.entries[0].evidence_id == "ev-1"
    assert archived and token not in archived[0]
    assert "[REDACTED:openai-key]" in archived[0]
