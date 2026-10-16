"""结构化任务单（ticket）：解析、校验、规范化与渲染。

主 Agent 提交结构化任务单替代自由文本任务说明；runner 把它渲染成子代理
的任务说明，并把 ``scope_paths`` 与 grants 的 ``read_repo`` 前缀求交
（二次收窄）后作为执行层的访问白名单基线。校验失败是确定性拒绝：不消耗
调用配额、不产生 SUBAGENT_* 事件，只以工具错误返回给主循环。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

EVIDENCE_TYPES: tuple[str, ...] = (
    "exists",
    "calls",
    "reads_config",
    "absent",
    "behavior",
)
TIERS: tuple[str, ...] = ("quick", "medium", "thorough")
DEFAULT_TIER = "medium"
REPO_ROOT = "."

_MAX_GOAL_CHARS = 300
_MAX_QUESTIONS = 12
_MAX_QUESTION_CHARS = 500
_MAX_SCOPE_PATHS = 16
_MAX_KNOWN_FACTS = 20
_MAX_FACT_CHARS = 500
_MAX_DONE_WHEN_CHARS = 300
_MAX_CONTINUATION_CHARS = 80


class TicketError(ValueError):
    """任务单非法（确定性拒绝，不消耗调用配额）。"""


@dataclass(frozen=True, slots=True)
class Ticket:
    """一份已规范化的委派任务单。"""

    goal: str
    questions: tuple[str, ...]
    scope_paths: tuple[str, ...]
    known_facts: tuple[str, ...]
    done_when: str
    evidence_types: tuple[str, ...]
    tier: str
    # 续跑关系：上一次 partial 委派的 subagent_id（可空）。
    continuation_of: str | None = None

    @property
    def scope_is_repo_root(self) -> bool:
        return self.scope_paths == (REPO_ROOT,)

    def scope_text(self) -> str:
        if self.scope_is_repo_root:
            return "整个仓库（显式声明）"
        return "、".join(f"{path}/" for path in self.scope_paths)


def parse_ticket(raw: Any, *, allow_repo_root: bool = False) -> Ticket:
    """解析并校验任务单；必填 goal / questions / scope_paths / done_when。"""

    if not isinstance(raw, dict):
        raise TicketError(
            "'ticket' 必须是对象（必填：goal、questions、scope_paths、"
            "done_when）"
        )
    goal = _required_text(raw.get("goal"), field="goal", limit=_MAX_GOAL_CHARS)
    questions = _string_list(
        raw.get("questions"),
        field="questions",
        item_limit=_MAX_QUESTION_CHARS,
        max_items=_MAX_QUESTIONS,
        required=True,
    )
    done_when = _required_text(
        raw.get("done_when"), field="done_when", limit=_MAX_DONE_WHEN_CHARS
    )
    scope_paths = _scope_paths(
        raw.get("scope_paths"),
        allow_repo_root=allow_repo_root,
    )
    known_facts = _string_list(
        raw.get("known_facts"),
        field="known_facts",
        item_limit=_MAX_FACT_CHARS,
        max_items=_MAX_KNOWN_FACTS,
        required=False,
    )
    evidence_types = _evidence_types(raw.get("evidence_types"))
    tier_raw = raw.get("tier", DEFAULT_TIER)
    if tier_raw is None:
        tier_raw = DEFAULT_TIER
    if not isinstance(tier_raw, str) or tier_raw not in TIERS:
        raise TicketError(f"'tier' 必须是 {' / '.join(TIERS)} 之一")
    continuation_of = raw.get("continuation_of")
    if continuation_of is not None:
        if not isinstance(continuation_of, str) or not continuation_of.strip():
            raise TicketError(
                "'continuation_of' 必须是非空字符串（上一次委派的 "
                "subagent_id）"
            )
        continuation_of = continuation_of.strip()
        if len(continuation_of) > _MAX_CONTINUATION_CHARS:
            raise TicketError(
                f"'continuation_of' 最多 {_MAX_CONTINUATION_CHARS} 字符"
            )
    return Ticket(
        goal=goal,
        questions=tuple(questions),
        scope_paths=scope_paths,
        known_facts=tuple(known_facts),
        done_when=done_when,
        evidence_types=evidence_types,
        tier=tier_raw,
        continuation_of=continuation_of,
    )


def normalize_scope_path(text: str) -> str:
    """规范化 scope 路径：反斜杠转正斜杠、去尾斜杠、拒绝绝对路径与 '..'。

    空字符串、``.``、``./`` 归一为仓库根标记 ``.``。
    """

    value = text.replace("\\", "/").strip()
    if value in ("", REPO_ROOT, "./"):
        return REPO_ROOT
    if value.startswith("/") or (len(value) >= 2 and value[1] == ":"):
        raise TicketError(f"scope_paths 必须是工作区内的相对路径：{text!r}")
    parts = [part for part in value.split("/") if part not in ("", ".")]
    if not parts:
        return REPO_ROOT
    if any(part == ".." for part in parts):
        raise TicketError(f"scope_paths 不能包含 '..'：{text!r}")
    return "/".join(parts)


def minimize_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    """去重并折叠被其他路径包含的子路径（保持字典序，确定性）。"""

    ordered = sorted(dict.fromkeys(paths))
    kept: list[str] = []
    for path in ordered:
        if any(
            path == other or path.startswith(f"{other}/") for other in kept
        ):
            continue
        kept.append(path)
    return tuple(kept)


def intersect_prefixes(
    grant_prefixes: tuple[str, ...] | None,
    scope_paths: tuple[str, ...] | None,
) -> tuple[str, ...] | None:
    """两个访问前缀集合求交；``None`` 表示不限制（整仓）。

    - 任一为 ``None``：结果为另一方（都 None 则 None）。
    - 路径集合交集按前缀包含关系计算：两个条目前缀若互不包含，其路径
      集合无交集（共享祖先不构成授权）。
    - 结果可能为空元组（调用方负责拒绝）；最小化去重后返回。
    """

    if grant_prefixes is None:
        return scope_paths
    if scope_paths is None:
        return grant_prefixes
    kept: list[str] = []
    for scope in scope_paths:
        for prefix in grant_prefixes:
            if scope == prefix or scope.startswith(f"{prefix}/"):
                kept.append(scope)
                break
            if prefix.startswith(f"{scope}/"):
                kept.append(prefix)
                break
    return minimize_paths(tuple(kept))


def render_task_brief(ticket: Ticket, access_prefixes: tuple[str, ...] | None) -> str:
    """把任务单渲染为子代理的任务说明（子循环的 user 消息）。"""

    access_text = (
        "整个仓库（已显式声明）"
        if access_prefixes is None
        else "、".join(f"{path}/" for path in access_prefixes)
    )
    lines = [
        "# 委派任务单",
        "",
        f"目标：{ticket.goal}",
        "",
        "需要回答的问题：",
    ]
    lines.extend(
        f"{index}. {question}"
        for index, question in enumerate(ticket.questions, 1)
    )
    lines.extend(
        [
            "",
            f"允许访问的路径范围（执行层强制，越界调用会被拒绝）：{access_text}",
            f"完成条件（done_when）：{ticket.done_when}",
            f"期望的证据类型：{'、'.join(ticket.evidence_types)}",
            f"预算档位（tier）：{ticket.tier}",
            "",
            "交付方式：发现逐条用 record_finding 记录（Harness 即时读取原文并"
            "确定性校验）；结束时用 submit_result 提交 summary 与 not_covered。",
        ]
    )
    if ticket.continuation_of:
        lines.extend(
            [
                "",
                f"续跑关系：这是对上一次委派（id={ticket.continuation_of}）的"
                "续跑；已确认的结论见已知事实，只调查未覆盖范围，不要重复扫描"
                "已覆盖区域。",
            ]
        )
    if ticket.known_facts:
        lines.extend(["", "已知事实（主代理提供，可能不完整，需要时自行核实）："])
        lines.extend(f"- {fact}" for fact in ticket.known_facts)
    return "\n".join(lines)


def _required_text(value: Any, *, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TicketError(f"'{field}' 必须是非空字符串（必填）")
    text = value.strip()
    if len(text) > limit:
        raise TicketError(f"'{field}' 最多 {limit} 字符")
    return text


def _string_list(
    value: Any,
    *,
    field: str,
    item_limit: int,
    max_items: int,
    required: bool,
) -> list[str]:
    if value is None:
        if required:
            raise TicketError(f"'{field}' 必填：至少一条非空字符串")
        return []
    if not isinstance(value, list):
        raise TicketError(f"'{field}' 必须是字符串数组")
    if required and not value:
        raise TicketError(f"'{field}' 必填：至少一条非空字符串")
    if len(value) > max_items:
        raise TicketError(f"'{field}' 最多 {max_items} 条")
    normalized: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise TicketError(f"'{field}[{index}]' 必须是非空字符串")
        text = item.strip()
        if len(text) > item_limit:
            raise TicketError(f"'{field}[{index}]' 最多 {item_limit} 字符")
        normalized.append(text)
    return normalized


def _scope_paths(value: Any, *, allow_repo_root: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise TicketError("'scope_paths' 必填：至少一个工作区内相对路径")
    if len(value) > _MAX_SCOPE_PATHS:
        raise TicketError(f"'scope_paths' 最多 {_MAX_SCOPE_PATHS} 条")
    normalized: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise TicketError(f"'scope_paths[{index}]' 必须是非空字符串")
        normalized.append(normalize_scope_path(item))
    if REPO_ROOT in normalized:
        if not allow_repo_root:
            raise TicketError(
                "scope_paths 不能是整个仓库根（需要显式声明 allow_repo_root "
                "才能整体扫描；请给出具体目录）"
            )
        return (REPO_ROOT,)
    return minimize_paths(tuple(normalized))


def _evidence_types(value: Any) -> tuple[str, ...]:
    if value is None:
        return EVIDENCE_TYPES
    if not isinstance(value, list) or not value:
        raise TicketError(
            f"'evidence_types' 必须是 {' / '.join(EVIDENCE_TYPES)} 的非空子集"
        )
    normalized: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or item not in EVIDENCE_TYPES:
            raise TicketError(
                f"'evidence_types[{index}]' 必须是 "
                f"{' / '.join(EVIDENCE_TYPES)} 之一"
            )
        if item not in normalized:
            normalized.append(item)
    return tuple(normalized)


__all__ = [
    "DEFAULT_TIER",
    "EVIDENCE_TYPES",
    "REPO_ROOT",
    "TIERS",
    "Ticket",
    "TicketError",
    "intersect_prefixes",
    "minimize_paths",
    "normalize_scope_path",
    "parse_ticket",
    "render_task_brief",
]
