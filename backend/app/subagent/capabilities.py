"""子代理能力目录：解析、校验与 PLAN 降级。

能力只收窄不放开：授予的能力 = 主代理请求（含降级）∩ 主 Run 模式白名单
∩ Active Skill allowed-tools（后两步在 runner 装配时落实）。目录结构为纯
数据，新增能力（如 ``write_repo``、``run_shell``）只需在此登记并给出工具
映射与作用域校验。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

_HOST_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*$"
)
_MAX_HOSTS_PER_GRANT = 8

_SUPPORTED_HINT = (
    "支持的能力：read_repo[:工作区路径前缀]、web_search、"
    "web_fetch:域名[,域名]"
)


class CapabilityError(ValueError):
    """能力声明非法或不可满足（确定性拒绝，不产生 SUBAGENT_* 事件）。"""


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    """目录项：能力名 → 映射工具与作用域约束。"""

    name: str
    summary: str
    tools: tuple[str, ...]
    requires_argument: bool
    argument_kind: str | None
    plan_allowed: bool


CAPABILITY_CATALOG: dict[str, CapabilitySpec] = {
    "read_repo": CapabilitySpec(
        name="read_repo",
        summary="只读工作区文件（可限定路径前缀）",
        tools=("read_file", "list_files"),
        requires_argument=False,
        argument_kind="path_prefix",
        plan_allowed=True,
    ),
    "web_search": CapabilitySpec(
        name="web_search",
        summary="只读网页搜索",
        tools=("web_search",),
        requires_argument=False,
        argument_kind=None,
        plan_allowed=True,
    ),
    "web_fetch": CapabilitySpec(
        name="web_fetch",
        summary="抓取指定域名的网页（仅 GET/HEAD）",
        tools=("http_request",),
        requires_argument=True,
        argument_kind="hosts",
        plan_allowed=False,
    ),
}


@dataclass(frozen=True, slots=True)
class CapabilityGrant:
    """一条已规范化的能力授予。"""

    name: str
    argument: str | None
    requested: str

    @property
    def spec(self) -> str:
        """规范化后的能力声明文本（轨迹与结果里的生效值）。"""

        return self.name if self.argument is None else f"{self.name}:{self.argument}"


@dataclass(frozen=True, slots=True)
class GrantDowngrade:
    """一条被收窄丢弃的授予（请求值、原因进轨迹）。

    原因取值：``plan_mode``（PLAN 模式降级）、``skill_scope``（Active Skill
    allowed-tools 收窄）、``mode_not_allowed``（模式白名单收窄）。
    """

    requested: str
    effective: str | None
    reason: str


def parse_grants(raw_grants: Sequence[str]) -> tuple[CapabilityGrant, ...]:
    """解析并校验授予清单；非法声明直接拒绝。"""

    if not raw_grants:
        raise CapabilityError(f"grants 不能为空；{_SUPPORTED_HINT}")
    grants: list[CapabilityGrant] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_grants):
        if not isinstance(raw, str) or not raw.strip():
            raise CapabilityError(
                f"grants[{index}] 必须是非空字符串；{_SUPPORTED_HINT}"
            )
        text = raw.strip()
        name, separator, argument_text = text.partition(":")
        name = name.strip().casefold()
        # 显式写了冒号却没给参数（如 "read_repo:"）按非法声明拒绝：静默放宽成
        # 不限作用域是 fail-open，宁可让调用方改写声明。
        if separator and not argument_text.strip():
            raise CapabilityError(
                f"能力 {name!r} 的参数为空（不给参数时不要写冒号）；"
                f"{_SUPPORTED_HINT}"
            )
        raw_argument = argument_text.strip() if separator else None
        spec = CAPABILITY_CATALOG.get(name)
        if spec is None:
            raise CapabilityError(f"未知能力 {name!r}；{_SUPPORTED_HINT}")
        argument = _normalize_argument(spec, raw_argument)
        grant = CapabilityGrant(name=name, argument=argument, requested=text)
        if grant.spec in seen:
            continue
        seen.add(grant.spec)
        grants.append(grant)
    return tuple(grants)


def apply_plan_downgrade(
    grants: Sequence[CapabilityGrant],
) -> tuple[tuple[CapabilityGrant, ...], tuple[GrantDowngrade, ...]]:
    """PLAN 模式降级：只读能力保留，其余丢弃并记录原因。"""

    effective: list[CapabilityGrant] = []
    downgrades: list[GrantDowngrade] = []
    for grant in grants:
        if CAPABILITY_CATALOG[grant.name].plan_allowed:
            effective.append(grant)
        else:
            downgrades.append(
                GrantDowngrade(
                    requested=grant.spec,
                    effective=None,
                    reason="plan_mode",
                )
            )
    return tuple(effective), tuple(downgrades)


def _normalize_argument(spec: CapabilitySpec, argument: str | None) -> str | None:
    if spec.requires_argument:
        if not argument:
            raise CapabilityError(
                f"能力 {spec.name} 需要参数；{_SUPPORTED_HINT}"
            )
        if spec.argument_kind == "hosts":
            return _normalize_hosts(argument)
        if spec.argument_kind == "path_prefix":
            return normalize_path_prefix(argument)
        return argument
    if argument:
        if spec.argument_kind == "path_prefix":
            # 可选取参数：合法前缀原样收下，非法前缀（绝对路径 / '..'）拒绝。
            return normalize_path_prefix(argument)
        raise CapabilityError(
            f"能力 {spec.name} 不接受参数（收到 {argument!r}）"
        )
    return None


def _normalize_hosts(argument: str) -> str:
    hosts: list[str] = []
    for raw_host in argument.split(","):
        host = raw_host.strip().casefold()
        if not host:
            continue
        if not _HOST_RE.fullmatch(host):
            raise CapabilityError(
                f"web_fetch 域名非法：{raw_host.strip()!r}（只接受主机名，"
                "不带协议、端口或路径）"
            )
        if host not in hosts:
            hosts.append(host)
    if not hosts:
        raise CapabilityError(f"web_fetch 需要至少一个域名；{_SUPPORTED_HINT}")
    if len(hosts) > _MAX_HOSTS_PER_GRANT:
        raise CapabilityError(
            f"web_fetch 最多授予 {_MAX_HOSTS_PER_GRANT} 个域名"
        )
    return ",".join(hosts)


def normalize_path_prefix(argument: str | None) -> str | None:
    """规范化 read_repo 路径前缀；None 表示不限制前缀。"""

    if argument is None:
        return None
    text = argument.replace("\\", "/").strip()
    if not text or text in (".", "./"):
        return None
    if text.startswith("/") or (len(text) >= 2 and text[1] == ":"):
        raise CapabilityError("read_repo 路径前缀必须是工作区内的相对路径")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts:
        return None
    if any(part == ".." for part in parts):
        raise CapabilityError("read_repo 路径前缀不能包含 '..'")
    return "/".join(parts)


__all__ = [
    "CAPABILITY_CATALOG",
    "CapabilityError",
    "CapabilityGrant",
    "CapabilitySpec",
    "GrantDowngrade",
    "apply_plan_downgrade",
    "normalize_path_prefix",
    "parse_grants",
]
