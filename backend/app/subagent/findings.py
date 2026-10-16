"""发现账本与 ``record_finding`` 工具。

模型只给定位（path/lines/symbol）与 claim，不写 snippet；Harness 读取原文
并按类型做确定性校验（exists / calls / reads_config / absent / behavior）。
通过的发现进入返回清单（受 tier 上限约束）；校验失败的标记 invalid，留在
账本里供 Trace，但不进入返回清单。``canonical_key`` 保证重试与重复提交幂等：
同一条发现再次提交返回既有 finding_id。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.models.types import ToolDefinition, ToolPermission
from app.tools.base import BaseTool
from app.tools.builtin._workspace import workspace_root_path
from app.tools.hooks import ToolExecutionContext

from .explore_tools import GrepTool
from .path_scope import SCOPE_VIOLATION_MARKER, assert_relative_within_scope
from .ticket import EVIDENCE_TYPES, TicketError, normalize_scope_path

RECORD_FINDING_TOOL_NAME = "record_finding"

# tier → 返回清单的 finding 上限（阶段 4 的预算档位共用这张表）。
FINDING_LIMITS: dict[str, int] = {"quick": 4, "medium": 6, "thorough": 8}

# 同一任务累计的越权调用上限（v3 方案 7 节）：达到即终止，状态为 failed。
MAX_SCOPE_VIOLATIONS = 3

_MAX_CLAIM_CHARS = 500
_MAX_LOCATIONS = 8
_MAX_SYMBOL_CHARS = 200
_MAX_LINES_CHARS = 40
_MAX_EXCERPT_LINES = 80
_MAX_PLAIN_EXCERPT_LINES = 200
_MAX_EXCERPT_CHARS = 8_000
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_SEARCH_SCOPE = 16

_LINE_RANGE = re.compile(r"^(\d+)(?:\s*-\s*(\d+))?$")
# reads_config 的“读取调用相邻”检查：片段里出现任一读取迹象才算通过。
_READ_HINTS = ("getenv", "environ", "settings", "config", "get(")

RecordEvidence = Callable[[str, ToolExecutionContext], Awaitable[str | None]]


@dataclass(slots=True)
class FindingEntry:
    """一条已入账的发现（``verified`` 表示通过确定性校验）。"""

    finding_id: str
    type: str
    claim: str
    locations: tuple[dict[str, str], ...]
    search: dict[str, Any] | None
    verified: bool
    reason: str
    evidence_id: str | None
    listed: bool

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "finding_id": self.finding_id,
            "type": self.type,
            "claim": self.claim,
            "status": "verified" if self.verified else "invalid",
            "reason": self.reason,
        }
        if self.locations:
            payload["locations"] = [dict(item) for item in self.locations]
        if self.search is not None:
            payload["search"] = {
                "pattern": self.search["pattern"],
                "scope": list(self.search.get("scope", [])),
            }
        if self.evidence_id:
            payload["evidence_id"] = self.evidence_id
        return payload


class FindingLedger:
    """一次委派的发现账本：入账、确定性校验、幂等与证据归档。"""

    def __init__(
        self,
        *,
        workspace_root: str | Path | None,
        access_prefixes: tuple[str, ...] | None,
        tier: str,
        record_evidence: RecordEvidence | None = None,
    ) -> None:
        self._workspace_root = workspace_root_path(workspace_root)
        self._access_prefixes = access_prefixes
        self.limit = FINDING_LIMITS.get(tier, FINDING_LIMITS["medium"])
        self._record_evidence = record_evidence
        self._entries: list[FindingEntry] = []
        self._by_key: dict[str, FindingEntry] = {}
        # 越权尝试计数（步骤 5 的 scope_violation / injection_suspected 用）。
        self.scope_attempts = 0

    @property
    def entries(self) -> tuple[FindingEntry, ...]:
        return tuple(self._entries)

    def verified(self) -> tuple[FindingEntry, ...]:
        return tuple(entry for entry in self._entries if entry.verified)

    def listed(self) -> tuple[FindingEntry, ...]:
        return tuple(
            entry for entry in self._entries if entry.verified and entry.listed
        )

    async def record(
        self,
        arguments: Any,
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        payload = _normalize_arguments(arguments)
        key = _canonical_key(payload)
        existing = self._by_key.get(key)
        if existing is not None:
            return self._receipt(existing, status="duplicate")

        finding_id = f"f{len(self._entries) + 1}"
        verified, reason, excerpt, violation = await self._verify(payload)
        if violation is not None:
            self.scope_attempts += 1
        evidence_id: str | None = None
        if verified and excerpt and self._record_evidence is not None:
            content = (
                f"[finding {finding_id} · {payload['type']}] "
                f"{payload['claim']}\n\n{excerpt}"
            )
            try:
                evidence_id = await self._record_evidence(content, context)
            except Exception:  # pragma: no cover - 归档失败不影响入账
                evidence_id = None
        listed = verified and len(self.listed()) < self.limit
        entry = FindingEntry(
            finding_id=finding_id,
            type=payload["type"],
            claim=payload["claim"],
            locations=payload["locations"],
            search=payload["search"],
            verified=verified,
            reason=reason,
            evidence_id=evidence_id,
            listed=listed,
        )
        self._entries.append(entry)
        self._by_key[key] = entry
        if not verified:
            status = "invalid"
        elif listed:
            status = "accepted"
        else:
            status = "over_limit"
        return self._receipt(entry, status=status)

    def _receipt(self, entry: FindingEntry, *, status: str) -> dict[str, Any]:
        receipt: dict[str, Any] = {
            "finding_id": entry.finding_id,
            "status": status,
            "verified": entry.verified,
            "reason": entry.reason,
            "verified_count": len(self.verified()),
            "limit": self.limit,
        }
        if status == "duplicate":
            receipt["message"] = "同一条发现此前已提交，返回既有 finding_id。"
        elif status == "invalid":
            receipt["message"] = (
                "校验未通过，本条不会进入返回清单；请修正定位或改写为可验证的结论。"
            )
        elif status == "over_limit":
            receipt["message"] = (
                f"finding 清单已满（{self.limit} 条）：本条已保留在轨迹中，"
                "但不进入返回清单；请优先用 submit_result 收尾。"
            )
        return receipt

    async def _verify(
        self, payload: dict[str, Any]
    ) -> tuple[bool, str, str, str | None]:
        """返回（是否通过, 原因, 证据原文, 越权标记）。"""

        if payload["type"] == "absent":
            return await self._verify_absent(payload["search"])
        return await asyncio.to_thread(self._verify_locations, payload)

    def _verify_locations(
        self, payload: dict[str, Any]
    ) -> tuple[bool, str, str, str | None]:
        excerpts: list[str] = []
        for location in payload["locations"]:
            path = location["path"]
            try:
                target = assert_relative_within_scope(
                    self._workspace_root,
                    path,
                    self._access_prefixes,
                )
            except ValueError:
                return False, "path_outside_scope", "", path
            if not target.is_file():
                return False, "path_not_found", "", None
            try:
                if target.stat().st_size > _MAX_FILE_BYTES:
                    return False, "file_too_large", "", None
                text = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return False, "read_error", "", None
            file_lines = text.splitlines()
            lines = location.get("lines", "")
            if lines:
                parsed = _parse_line_range(lines)
                if parsed is None:
                    return False, "invalid_lines", "", None
                start, end = parsed
                if start > len(file_lines):
                    return False, "lines_out_of_range", "", None
                end = min(end, len(file_lines))
                if end - start + 1 > _MAX_EXCERPT_LINES:
                    end = start + _MAX_EXCERPT_LINES - 1
                excerpt_lines = file_lines[start - 1 : end]
                label = f"{start}-{end}"
            else:
                excerpt_lines = file_lines[:_MAX_PLAIN_EXCERPT_LINES]
                label = f"1-{min(len(file_lines), _MAX_PLAIN_EXCERPT_LINES)}"
            excerpt_text = "\n".join(excerpt_lines)
            ok, reason = _check_semantics(
                payload["type"],
                location.get("symbol", ""),
                excerpt_text,
            )
            if not ok:
                return False, reason, "", None
            excerpts.append(f"{path}:{label}\n{excerpt_text}")
        excerpt = "\n\n".join(excerpts)
        if len(excerpt) > _MAX_EXCERPT_CHARS:
            excerpt = f"{excerpt[:_MAX_EXCERPT_CHARS]}\n…[excerpt truncated]"
        return True, "verified", excerpt, None

    async def _verify_absent(
        self, search: dict[str, Any] | None
    ) -> tuple[bool, str, str, str | None]:
        assert search is not None  # noqa: S101 - _normalize 已保证
        pattern = search["pattern"]
        scope_entries = search.get("scope") or []
        prefixes = self._access_prefixes
        if scope_entries:
            checked: list[str] = []
            for raw in scope_entries:
                try:
                    item = normalize_scope_path(raw)
                    target = assert_relative_within_scope(
                        self._workspace_root,
                        item,
                        self._access_prefixes,
                        allow_root=True,
                    )
                except ValueError:
                    return False, "scope_outside_access", "", raw
                if not target.is_dir():
                    return False, "scope_not_directory", "", raw
                if item not in checked:
                    checked.append(item)
            prefixes = tuple(checked)
        tool = GrepTool(workspace_root=self._workspace_root, prefixes=prefixes)
        try:
            result = await tool.execute({"pattern": pattern, "max_results": 1})
        except ValueError as exc:
            return False, f"search_error: {exc}", "", None
        if result["count"] > 0:
            return False, "absent_contradicted", "", None
        if result.get("truncated"):
            return False, "search_truncated", "", None
        scope_text = ", ".join(prefixes) if prefixes else "整个工作区"
        excerpt = (
            f"[absent] pattern={pattern!r}\n"
            f"scope={scope_text}\n"
            "重新搜索 0 命中。"
        )
        return True, "verified", excerpt, None


class RecordFindingTool(BaseTool):
    """record_finding：入账一条发现（幂等 + 即时确定性校验）。"""

    def __init__(self, ledger: FindingLedger) -> None:
        self._ledger = ledger

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=RECORD_FINDING_TOOL_NAME,
            description=(
                "记录一条发现并立即获得校验结果：Harness 会读取你给出的定位"
                "原文、按 type 做确定性校验，通过的发现进入返回清单（本任务"
                f"上限 {self._ledger.limit} 条）。你只写定位，不要写代码片段。"
                "type 取值：exists（符号/内容存在）——给出 path 与 lines，"
                "symbol 填要核对的符号；calls（A 调用 B）——symbol 填被调方"
                "名称；reads_config——symbol 填配置键名；absent（某处不存在"
                "某事物）——必须给出 search.pattern（正则）与 search.scope"
                "（目录列表，缺省为整个授权范围），Harness 会重跑搜索确认；"
                "behavior（行为/逻辑结论）——给出支撑结论的 path/lines。"
                "定位越精确越容易通过；重复提交同一发现返回既有 finding_id。"
                "校验失败的发现不会进入返回清单，会给出失败原因。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": list(EVIDENCE_TYPES),
                    },
                    "claim": {
                        "type": "string",
                        "description": "一句话结论（≤500 字符）。",
                    },
                    "locations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "description": "工作区相对路径。",
                                },
                                "lines": {
                                    "type": "string",
                                    "description": (
                                        "行号或范围，如 \"120\" 或 "
                                        "\"120-128\"。"
                                    ),
                                },
                                "symbol": {
                                    "type": "string",
                                    "description": (
                                        "要核对的符号 / 被调方名称 / "
                                        "配置键名（按类型选择）。"
                                    ),
                                },
                            },
                            "required": ["path"],
                            "additionalProperties": False,
                        },
                        "description": (
                            "定位列表（1-8 条）；absent 类型不需要 locations。"
                        ),
                    },
                    "search": {
                        "type": "object",
                        "properties": {
                            "pattern": {
                                "type": "string",
                                "description": "重跑搜索的 Python 正则。",
                            },
                            "scope": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "搜索范围目录（工作区相对路径，"
                                    "最多 16 条；缺省为整个授权范围）。"
                                ),
                            },
                        },
                        "required": ["pattern"],
                        "additionalProperties": False,
                        "description": "仅 absent 必填。",
                    },
                },
                "required": ["type", "claim"],
                "additionalProperties": False,
            },
            strict=False,
            permission=ToolPermission.ALLOWED,
            record_output=False,
        )

    async def execute(self, arguments: dict[str, Any]) -> Any:
        raise ValueError("record_finding requires run context")

    async def execute_with_context(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        return await self._ledger.record(arguments, context)


def _normalize_arguments(arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("finding 必须是一个对象")
    ftype = arguments.get("type")
    if ftype not in EVIDENCE_TYPES:
        raise ValueError(f"'type' 必须是 {' / '.join(EVIDENCE_TYPES)} 之一")
    claim = arguments.get("claim")
    if not isinstance(claim, str) or not claim.strip():
        raise ValueError("'claim' 必须是非空字符串")
    claim = " ".join(claim.split())
    if len(claim) > _MAX_CLAIM_CHARS:
        raise ValueError(f"'claim' 最多 {_MAX_CLAIM_CHARS} 字符")

    locations: list[dict[str, str]] = []
    raw_locations = arguments.get("locations")
    if ftype != "absent":
        if not isinstance(raw_locations, list) or not raw_locations:
            raise ValueError("locations 必填：至少一条 path 定位")
        if len(raw_locations) > _MAX_LOCATIONS:
            raise ValueError(f"'locations' 最多 {_MAX_LOCATIONS} 条")
        for index, item in enumerate(raw_locations):
            locations.append(_normalize_location(item, index=index))

    search: dict[str, Any] | None = None
    if ftype == "absent":
        raw_search = arguments.get("search")
        if not isinstance(raw_search, dict):
            raise ValueError(
                "type=absent 时必须给出 search（pattern 与可选 scope）"
            )
        pattern = raw_search.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            raise ValueError("'search.pattern' 必须是非空字符串")
        scope = raw_search.get("scope") or []
        if not isinstance(scope, list) or len(scope) > _MAX_SEARCH_SCOPE:
            raise ValueError(f"'search.scope' 最多 {_MAX_SEARCH_SCOPE} 条")
        normalized_scope: list[str] = []
        for index, item in enumerate(scope):
            if not isinstance(item, str) or not item.strip():
                raise ValueError(
                    f"'search.scope[{index}]' 必须是非空字符串"
                )
            normalized = _normalize_scoped_path(item)
            if normalized not in normalized_scope:
                normalized_scope.append(normalized)
        search = {
            "pattern": pattern.strip(),
            "scope": normalized_scope,
        }

    return {
        "type": ftype,
        "claim": claim,
        "locations": locations,
        "search": search,
    }


def _normalize_scoped_path(text: str) -> str:
    """规范化工作区相对路径；绝对路径 / ``..`` 摆渡即越权，拒绝并带统一标记。

    标记让子循环观察者把这类被拒调用计入 ``scope_violation``（与工具层
    路径越界共用同一计数），而不是当成普通参数错误。
    """

    try:
        return normalize_scope_path(text)
    except TicketError as exc:
        raise ValueError(f"{SCOPE_VIOLATION_MARKER}: {exc}") from None


def _normalize_location(item: Any, *, index: int) -> dict[str, str]:
    if not isinstance(item, dict):
        raise ValueError(f"'locations[{index}]' 必须是对象")
    path = item.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ValueError(f"'locations[{index}].path' 必须是非空字符串")
    normalized_path = _normalize_scoped_path(path)
    lines = item.get("lines", "")
    symbol = item.get("symbol", "")
    for field_name, value, limit in (
        ("lines", lines, _MAX_LINES_CHARS),
        ("symbol", symbol, _MAX_SYMBOL_CHARS),
    ):
        if value in (None, ""):
            continue
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(
                f"'locations[{index}].{field_name}' 必须是不超过 "
                f"{limit} 字符的字符串"
            )
    return {
        "path": normalized_path,
        "lines": (lines or "").strip(),
        "symbol": (symbol or "").strip(),
    }


def _canonical_key(payload: dict[str, Any]) -> str:
    data = {
        "type": payload["type"],
        "claim": payload["claim"],
        "locations": sorted(
            (item["path"], item["lines"], item["symbol"])
            for item in payload["locations"]
        ),
        "search": (
            {
                "pattern": payload["search"]["pattern"],
                "scope": sorted(payload["search"]["scope"]),
            }
            if payload["search"] is not None
            else None
        ),
    }
    digest = hashlib.sha256(
        json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    )
    return digest.hexdigest()


def _parse_line_range(value: str) -> tuple[int, int] | None:
    match = _LINE_RANGE.match(value.strip())
    if match is None:
        return None
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else start
    if start < 1 or end < start:
        return None
    return start, end


def _check_semantics(
    ftype: str, symbol: str, excerpt: str
) -> tuple[bool, str]:
    text = excerpt.strip()
    if ftype == "exists":
        if symbol and symbol not in excerpt:
            return False, "symbol_not_found"
        if not text:
            return False, "empty_range"
        return True, "verified"
    if ftype == "calls":
        if not symbol:
            return False, "symbol_required"
        if symbol not in excerpt:
            return False, "callee_not_found"
        return True, "verified"
    if ftype == "reads_config":
        if not symbol:
            return False, "symbol_required"
        if symbol not in excerpt:
            return False, "config_key_not_found"
        lowered = excerpt.casefold()
        if not any(hint in lowered for hint in _READ_HINTS):
            return False, "no_adjacent_read"
        return True, "verified"
    if ftype == "behavior":
        if not text:
            return False, "empty_range"
        return True, "verified"
    return False, "unknown_type"  # pragma: no cover - 枚举已收窄


__all__ = [
    "FINDING_LIMITS",
    "FindingEntry",
    "FindingLedger",
    "RECORD_FINDING_TOOL_NAME",
    "RecordFindingTool",
]
