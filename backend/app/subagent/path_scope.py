"""任务单范围与授权前缀的执行层路径校验（read / list / grep / glob / git 共用）。

所有校验在本地确定性完成：绝对路径、``..`` 与符号链接逃逸一律拒绝；前缀
比对基于规范化后的真实路径（``Path.resolve``），不依赖模型的文字约定。
"""

from __future__ import annotations

from pathlib import Path

from app.tools.builtin._workspace import resolve_workspace_path

# 越权拒绝的统一标记：子循环观察者据此累计 scope_violation；同一任务
# 累计达上限（见 MAX_SCOPE_VIOLATIONS）直接终止并记 failed。
SCOPE_VIOLATION_MARKER = "scope_violation"


def prefix_scope_text(prefixes: tuple[str, ...] | None) -> str:
    if prefixes is None:
        return "the whole workspace"
    return ", ".join(f"{prefix}/" for prefix in prefixes)


def rel_within_prefixes(relative: str, prefixes: tuple[str, ...]) -> bool:
    return any(
        relative == prefix or relative.startswith(f"{prefix}/")
        for prefix in prefixes
    )


def assert_relative_within_scope(
    workspace_root: Path,
    relative_path: str,
    prefixes: tuple[str, ...] | None,
    *,
    allow_root: bool = False,
) -> Path:
    """把相对路径解析为真实路径，并校验落点在前缀集合内。

    返回解析后的绝对路径；越界（含 ``..`` 与符号链接逃逸）抛 ``ValueError``，
    消息带 ``scope_violation`` 标记供越权计数使用。
    """

    try:
        target = resolve_workspace_path(
            workspace_root,
            relative_path,
            allow_root=allow_root,
        )
    except ValueError as exc:
        raise ValueError(f"{SCOPE_VIOLATION_MARKER}: {exc}") from None
    if prefixes is None:
        return target
    relative = (
        "."
        if target == workspace_root
        else target.relative_to(workspace_root).as_posix()
    )
    if not rel_within_prefixes(relative, prefixes):
        raise ValueError(
            f"{SCOPE_VIOLATION_MARKER}: path {relative_path!r} is outside "
            f"the granted read_repo scope: {prefix_scope_text(prefixes)}"
        )
    return target


def resolve_scope_roots(
    workspace_root: Path,
    prefixes: tuple[str, ...] | None,
) -> tuple[Path, ...]:
    """允许访问的根目录（前缀 → 解析后的绝对路径；None → 工作区根）。

    只返回存在且仍在工作区内的目录；嵌套前缀先折叠，避免重复扫描。
    """

    if prefixes is None:
        return (workspace_root,)
    roots: list[Path] = []
    for prefix in prefixes:
        target = (workspace_root / prefix).resolve()
        try:
            target.relative_to(workspace_root)
        except ValueError:  # pragma: no cover - 构造期已拒绝逃逸前缀
            continue
        if not target.is_dir():
            continue
        if any(target == root or root in target.parents for root in roots):
            continue
        roots = [root for root in roots if target not in root.parents]
        roots.append(target)
    return tuple(roots)


__all__ = [
    "SCOPE_VIOLATION_MARKER",
    "assert_relative_within_scope",
    "prefix_scope_text",
    "rel_within_prefixes",
    "resolve_scope_roots",
]
