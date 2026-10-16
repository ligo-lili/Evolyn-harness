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
