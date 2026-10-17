"""夹具仓库自检：可生成、可编译、确定、支撑标准答案。"""

from __future__ import annotations

from pathlib import Path

from tests.eval.subagent.fixtures import (
    FIXTURE_FILES,
    write_fixture_repo,
)
from tests.eval.subagent.tasks import TASKS


def test_write_fixture_repo(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    paths = write_fixture_repo(root)
    assert set(paths) == set(FIXTURE_FILES)
    for relative in paths:
        assert (root / relative).is_file(), relative


def test_fixture_python_files_compile(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    write_fixture_repo(root)
    for relative, content in FIXTURE_FILES.items():
        if not relative.endswith(".py"):
            continue
        compile(content, relative, "exec")  # 语法错误即失败


def test_fixture_is_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    write_fixture_repo(first)
    write_fixture_repo(second)
    for relative in FIXTURE_FILES:
        assert (first / relative).read_text(encoding="utf-8") == (
            second / relative
        ).read_text(encoding="utf-8")


def test_expected_symbols_present() -> None:
    """标准答案依赖的符号必须在夹具原文中精确定位。"""

    checks = {
        "src/auth/session.py": ("validate_session", "get_session", "expires_at"),
        "src/auth/login.py": ("record_failure", "is_locked", "max_login_attempts"),
        "src/auth/permissions.py": (
            "ROLE_POLICY",
            "check_permission",
            "require_permission",
        ),
        "src/auth/tokens.py": ("AUTH_TOKEN_SECRET", "verify_token"),
        "src/config/settings.py": ("lockout_window_seconds", "AUTH_SESSION_TTL"),
        "src/api/routes.py": (
            "handle_profile",
            "handle_admin_users",
            "handle_user_export",
        ),
        "src/api/middleware.py": ("auth_middleware", "validate_session"),
        "src/api/errors.py": ("error_response", "wrap_domain_errors", "500"),
        "src/services/user_service.py": ("export_user_data",),
        "src/services/audit_service.py": ("def record", "audit_log"),
        "src/core/logging.py": ("audit_log",),
        "src/storage/models.py": ("class User",),
        "tests/test_session.py": ("test_validate_unknown_session",),
    }
    for relative, symbols in checks.items():
        content = FIXTURE_FILES[relative]
        for symbol in symbols:
            assert symbol in content, f"{relative} missing {symbol!r}"


def test_no_usage_config_is_unused() -> None:
    """T09 的标准答案：lockout_window_seconds 只出现在 settings.py。"""

    occurrences = [
        relative
        for relative, content in FIXTURE_FILES.items()
        if "lockout_window" in content
    ]
    assert occurrences == ["src/config/settings.py"]


def test_task_scope_hints_match_fixture_paths() -> None:
    paths = tuple(FIXTURE_FILES)
    for task in TASKS:
        for hint in task.scope_hint:
            assert any(path.startswith(hint) for path in paths), (
                task.id,
                hint,
            )


def _occurrences(symbol: str) -> list[str]:
    return [
        relative
        for relative, content in FIXTURE_FILES.items()
        if symbol in content
    ]


def test_unused_deprecated_functions_are_unique() -> None:
    """L02 的标准答案：4 个 Deprecated 函数全仓库只有定义处一次出现。"""

    for name in (
        "calculate_total_v1",
        "check_permission_v1",
        "apply_discount_v1",
        "issue_token_v1",
    ):
        occurrences = _occurrences(name)
        assert len(occurrences) == 1, (name, occurrences)
        assert "Deprecated" in FIXTURE_FILES[occurrences[0]]


def test_still_used_deprecated_functions_have_callers() -> None:
    """L02 的反例：这两个 Deprecated 函数仍被引用，不算无调用方。"""

    for name in ("record_audit_v1", "load_legacy_config"):
        occurrences = _occurrences(name)
        assert len(occurrences) >= 2, (name, occurrences)


def test_unused_feature_flag_is_unique() -> None:
    """L03 的标准答案：ENABLE_LEGACY_EXPORT 只出现在开关定义处。"""

    assert _occurrences("ENABLE_LEGACY_EXPORT") == [
        "src/config/feature_flags.py"
    ]
    for used in (
        "ENABLE_NEW_CHECKOUT",
        "ENABLE_TAX_V2",
        "ENABLE_REVIEWS_V2",
        "ENABLE_SUPPORT_CHAT",
    ):
        occurrences = _occurrences(used)
        assert len(occurrences) >= 2, (used, occurrences)


def test_docs_only_env_vars() -> None:
    """L07 的标准答案：这两个变量只出现在文档里。"""

    for name in ("VOICE_ORDERS_ENABLED", "LEGACY_EXPORT_BUCKET"):
        assert _occurrences(name) == ["docs/env_reference.md"], name


def test_import_time_connections_only_expected() -> None:
    """L05 的标准答案：模块顶层 connect 只出现在三个集成适配。"""

    occurrences = sorted(
        relative
        for relative, content in FIXTURE_FILES.items()
        if "= connect(" in content
    )
    assert occurrences == [
        "src/integrations/analytics.py",
        "src/integrations/search.py",
        "src/integrations/storage.py",
    ]


def test_module_test_coverage_inventory() -> None:
    """L08 的标准答案：未覆盖模块清单与夹具测试文件一一对应。"""

    module_names = sorted(
        relative.split("/")[2]
        for relative in FIXTURE_FILES
        if relative.startswith("src/modules/") and relative.endswith("/service.py")
    )
    covered = {
        relative.removeprefix("tests/test_").removesuffix(".py")
        for relative in FIXTURE_FILES
        if relative.startswith("tests/test_")
    }
    uncovered = [name for name in module_names if name not in covered]
    assert uncovered == [
        "audit",
        "cart",
        "inventory",
        "notifications",
        "promotions",
        "reviews",
        "shipping",
    ]


def test_tax_rate_flow_files_exist() -> None:
    """L06 的链路：tax.py 读配置，checkout 消费。"""

    assert "TAX_RATE_PERCENT" in FIXTURE_FILES["src/integrations/tax.py"]
    assert "calculate_tax_for_order" in FIXTURE_FILES["src/integrations/tax.py"]
    checkout = FIXTURE_FILES["src/modules/checkout/service.py"]
    assert "calculate_tax_for_order" in checkout
    assert "charge_card" in checkout


def test_fixture_byte_scale_supports_context_pressure() -> None:
    """夹具规模必须支撑「大范围探索」：单文件字节接近真实仓库量级。

    回归护栏：曾出现「文件数多但总字节极小」（95 个文件合计约 3.2 万字符）
    的夹具，全仓阅读也不产生真实上下文压力，上下文指标失去区分度。
    """

    total_chars = sum(len(content) for content in FIXTURE_FILES.values())
    assert total_chars >= 100_000, total_chars
    py_sizes = [
        len(content)
        for relative, content in FIXTURE_FILES.items()
        if relative.endswith(".py")
    ]
    mean_py = sum(py_sizes) / len(py_sizes)
    assert mean_py >= 900, mean_py
