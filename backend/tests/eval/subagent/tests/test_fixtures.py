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


# ---------------------------------------------------------------------------
# digest 套件（specs/ 语料）护栏：植入事实与任务答案一一对应
# ---------------------------------------------------------------------------


def _specs_files() -> dict[str, str]:
    return {
        relative: content
        for relative, content in FIXTURE_FILES.items()
        if relative.startswith("specs/")
    }


def test_digest_specs_present_and_scaled() -> None:
    """digest 语料必须成规模：通读全部文档才有真实摄入压力。"""

    specs = _specs_files()
    assert len(specs) >= 30, len(specs)
    total = sum(len(content) for content in specs.values())
    assert total >= 25_000, total


def test_digest_specs_index_lists_every_doc() -> None:
    """specs/README.md 的索引必须覆盖全部文档（可检索性要求）。"""

    index = FIXTURE_FILES["specs/README.md"]
    for relative in _specs_files():
        if relative == "specs/README.md":
            continue
        name = relative.removeprefix("specs/")
        assert name in index, name


def test_digest_d01_spec_claims_exist() -> None:
    """D01 的 8 处规格侧声明必须在语料里原文可查。"""

    expected = (
        ("specs/modules/checkout-order-contract.md", "必须自动重试，最多 3 次"),
        ("specs/platform/tax-calculation.md", "四舍五入到分（half-up）"),
        ("specs/modules/notifications-delivery.md", "抛出领域异常"),
        ("specs/modules/auth-session-policy.md", "300 秒为窗口"),
        ("specs/platform/legacy-migration-plan.md", "均已切换至事件总线"),
        ("specs/modules/notifications-delivery.md", "最多处理 500 个收件人"),
        ("specs/platform/legacy-migration-plan.md", "缺省值为 3"),
        ("specs/modules/promotions-rules.md", "必须返回 409"),
    )
    for relative, phrase in expected:
        assert phrase in FIXTURE_FILES[relative], (relative, phrase)


def test_digest_d01_code_counter_facts_hold() -> None:
    """D01 的 8 处代码侧事实（与规格声明相反）必须保持不变。"""

    checkout = FIXTURE_FILES["src/modules/checkout/service.py"]
    assert "charge_card(" in checkout
    assert "retry" not in checkout.lower()

    payments = FIXTURE_FILES["src/integrations/payments.py"]
    assert "_RETRYABLE_CODES" in payments

    tax = FIXTURE_FILES["src/integrations/tax.py"]
    assert 'flag_enabled("ENABLE_TAX_V2")' in tax
    assert "return int(raw)" in tax

    notifications = FIXTURE_FILES["src/modules/notifications/service.py"]
    assert "raise ValueError" in notifications
    assert "ValidationError" not in notifications

    login = FIXTURE_FILES["src/auth/login.py"]
    assert "窗口本身尚未" in login
    assert "T-103" in login

    audit_module = FIXTURE_FILES["src/modules/audit/service.py"]
    assert "record_audit_v1" in audit_module
    assert "v1 通道" in audit_module

    email = FIXTURE_FILES["src/integrations/email.py"]
    assert "_MAX_BULK = 200" in email
    assert "recipients[:_MAX_BULK]" in email

    config_v1 = FIXTURE_FILES["src/legacy/config_v1.py"]
    assert '"retries": 0' in config_v1

    promotions = FIXTURE_FILES["src/modules/promotions/service.py"]
    assert "409" not in promotions
    assert "_RECORDS[key] = record" in promotions


def test_digest_d02_conflict_pairs_exist() -> None:
    """D02 的 4 对矛盾双方文本必须各自可查（矛盾由内容语意成立）。"""

    pairs = (
        ("specs/platform/api-pagination.md", "统一为 100"),
        ("specs/platform/audit-events.md", "200 条（全仓唯一例外）"),
        ("specs/platform/error-mapping-policy.md", "禁止附加任何额外字段"),
        ("specs/platform/release-checklist.md", "必须包含 `fields` 数组"),
        ("specs/platform/release-checklist.md", "禁止在周五"),
        ("specs/platform/legacy-migration-plan.md", "周五夜间 02:00-04:00"),
        ("specs/platform/observability-standards.md", "90 天"),
        ("specs/platform/security-baseline.md", "365 天"),
    )
    for relative, phrase in pairs:
        assert phrase in FIXTURE_FILES[relative], (relative, phrase)


def test_digest_d03_event_list_and_code_reality() -> None:
    """D03：规范清单 8 项只出现在 specs/（不得混入代码）；实现侧事实与
    矩阵一致（3 处命名不符 + 5 处缺失）。"""

    audit_spec = FIXTURE_FILES["specs/platform/audit-events.md"]
    for name in (
        "user.login",
        "user.logout",
        "user.export",
        "account.password_change",
        "order.confirm",
        "refund.request",
        "review.publish",
        "notification.dispatch",
    ):
        occurrences = _occurrences(name)
        assert occurrences, name
        assert all(
            relative.startswith("specs/") for relative in occurrences
        ), (name, occurrences)
        assert name in audit_spec, name

    login = FIXTURE_FILES["src/auth/login.py"]
    assert "login_success" in login
    routes = FIXTURE_FILES["src/api/routes.py"]
    assert "user_exported" in routes
    assert routes.count("audit_service.record") == 1
    assert "handle_logout" in routes
    user_service = FIXTURE_FILES["src/services/user_service.py"]
    assert "password_updated" in user_service
    for relative in (
        "src/modules/checkout/service.py",
        "src/modules/refunds/service.py",
        "src/modules/reviews/service.py",
        "src/modules/notifications/service.py",
    ):
        assert "audit_service" not in FIXTURE_FILES[relative], relative
