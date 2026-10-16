"""夹具仓库：shop-auth-sandbox（确定性、自包含、无外部依赖）。

仓库服务于 20 个评测任务：函数定位、跨文件调用链、配置追踪、异常处理、
测试覆盖、跨模块依赖，以及安全子集（认证 / 权限 / 数据隔离）。

设计约束：

- 纯文本落地，不执行；代码只要求结构真实（导入关系、调用关系、配置读取）；
- 每个任务的标准答案都能在文件内容里精确核对（tasks.py 的常见疑问）；
- 有意保留的「缺口」（权限缺失、会话绕过、数据隔离缺失、未使用配置）
  是安全任务要发现的确定事实，修改夹具内容时必须同步核对任务答案。
"""

from __future__ import annotations

from pathlib import Path

FIXTURE_NAME = "shop-auth-sandbox"

FIXTURE_FILES: dict[str, str] = {
    "README.md": '''# shop-auth-sandbox

一个用于本地探索的最小认证服务夹具仓库。

- `src/api/`：路由装配、请求中间件与错误映射
- `src/auth/`：登录、会话、令牌与权限
- `src/config/`：集中配置与环境变量读取
- `src/services/`：用户与审计业务
- `src/storage/`：持久化访问
- `src/core/`：基础异常与日志
- `tests/`：pytest 测试

入口装配：`src/api/app.py` 的 `dispatch()`。
''',
    "src/__init__.py": '"""shop-auth-sandbox 顶层包。"""\n',
    "src/auth/__init__.py": '''"""认证包：登录、会话、令牌与权限。"""

from src.auth.login import login
from src.auth.permissions import check_permission, require_permission
from src.auth.session import Session, SessionStore, create_session, validate_session
from src.auth.tokens import issue_token, verify_token

__all__ = [
    "Session",
    "SessionStore",
    "check_permission",
    "create_session",
    "issue_token",
    "login",
    "require_permission",
    "validate_session",
    "verify_token",
]
''',
    "src/auth/hashing.py": '''"""密码哈希：PBKDF2-HMAC-SHA256。"""

import hashlib
import hmac
import os

_ITERATIONS = 200_000
_SALT_BYTES = 16


def hash_password(password: str, *, salt: bytes | None = None) -> bytes:
    """加盐哈希；返回 salt + digest。"""

    if salt is None:
        salt = os.urandom(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        _ITERATIONS,
    )
    return salt + digest


def verify_password(password: str, stored: bytes) -> bool:
    """恒定时间校验密码。"""

    salt, expected = stored[:_SALT_BYTES], stored[_SALT_BYTES:]
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        _ITERATIONS,
    )
    return hmac.compare_digest(digest, expected)
''',
    "src/auth/session.py": '''"""会话创建、校验与过期管理。"""

from dataclasses import dataclass
from time import time
from uuid import uuid4

from src.config.settings import load_settings


@dataclass
class Session:
    """一次登录产生的会话。"""

    session_id: str
    user_id: str
    expires_at: float


class SessionStore:
    """进程内会话存储。"""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def save(self, session: Session) -> None:
        self._sessions[session.session_id] = session

    def get_session(self, session_id: str) -> Session | None:
        """按 ID 直接取回会话，不做任何有效期判断。"""

        return self._sessions.get(session_id)

    def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


def create_session(user_id: str, store: SessionStore) -> Session:
    """创建会话并写入存储；有效期来自集中配置。"""

    settings = load_settings()
    session = Session(
        session_id=uuid4().hex,
        user_id=user_id,
        expires_at=time() + settings.session_ttl_seconds,
    )
    store.save(session)
    return session


def validate_session(session_id: str, store: SessionStore) -> Session | None:
    """校验会话存在且未过期；过期会话会被删除。"""

    session = store.get_session(session_id)
    if session is None:
        return None
    if session.expires_at <= time():
        store.delete(session_id)
        return None
    return session
''',
    "src/auth/tokens.py": '''"""短期访问令牌：签名签发与校验。"""

import hmac
import os
from hashlib import sha256

TOKEN_TTL_SECONDS = 900


def _secret() -> bytes:
    """直接读取环境变量，未经过集中配置。"""

    return os.environ.get("AUTH_TOKEN_SECRET", "").encode("utf-8")


def _sign(user_id: str) -> str:
    return hmac.new(_secret(), user_id.encode("utf-8"), sha256).hexdigest()


def issue_token(user_id: str) -> str:
    """签发令牌：``user_id.signature``。"""

    return f"{user_id}.{_sign(user_id)}"


def verify_token(token: str) -> str | None:
    """校验签名并返回 user_id；不包含过期校验。"""

    user_id, _, signature = token.partition(".")
    if not user_id or not signature:
        return None
    if hmac.compare_digest(signature, _sign(user_id)):
        return user_id
    return None
''',
    "src/auth/permissions.py": '''"""角色权限策略与检查。"""

from src.core.errors import PermissionDeniedError

ROLE_POLICY: dict[str, frozenset[str]] = {
    "admin": frozenset({"read", "write", "export", "manage_users"}),
    "user": frozenset({"read"}),
}


def check_permission(role: str, action: str) -> bool:
    """角色是否允许执行动作；未知角色按最小权限处理。"""

    return action in ROLE_POLICY.get(role, frozenset())


def require_permission(user: object, action: str) -> None:
    """不满足权限时抛出 PermissionDeniedError。"""

    role = getattr(user, "role", "")
    if not check_permission(role, action):
        raise PermissionDeniedError(f"role {role!r} cannot {action}")
''',
    "src/auth/login.py": '''"""登录流程与失败锁定。"""

from src.auth.hashing import verify_password
from src.auth.session import Session, SessionStore, create_session
from src.config.settings import load_settings
from src.services import audit_service, user_service

_FAILED_ATTEMPTS: dict[str, int] = {}


def is_locked(username: str) -> bool:
    """失败次数达到上限即锁定。"""

    return _FAILED_ATTEMPTS.get(username, 0) >= load_settings().max_login_attempts


def record_failure(username: str) -> None:
    """记录一次失败尝试。"""

    _FAILED_ATTEMPTS[username] = _FAILED_ATTEMPTS.get(username, 0) + 1


def login(
    username: str,
    password: str,
    store: SessionStore,
) -> Session | None:
    """用户名密码登录；失败返回 None。"""

    if is_locked(username):
        audit_service.record({"event": "login_locked", "username": username})
        return None
    user = user_service.find_user(username)
    if user is None or not verify_password(password, user.password_hash):
        record_failure(username)
        return None
    audit_service.record({"event": "login_success", "username": username})
    return create_session(user.id, store)
''',
    "src/config/__init__.py": '''"""配置包。"""

from src.config.settings import Settings, load_settings

__all__ = ["Settings", "load_settings"]
''',
    "src/config/defaults.py": '''"""配置默认值。"""

DEFAULT_SESSION_TTL_SECONDS = 3600
DEFAULT_MAX_LOGIN_ATTEMPTS = 5
''',
    "src/config/settings.py": '''"""集中配置：环境变量 -> Settings。"""

import os
from dataclasses import dataclass

from src.config.defaults import (
    DEFAULT_MAX_LOGIN_ATTEMPTS,
    DEFAULT_SESSION_TTL_SECONDS,
)


@dataclass(frozen=True)
class Settings:
    session_ttl_seconds: int
    max_login_attempts: int
    lockout_window_seconds: int
    audit_enabled: bool


def load_settings() -> Settings:
    """从环境变量读取配置；未设置时使用默认值。"""

    return Settings(
        session_ttl_seconds=int(
            os.environ.get("AUTH_SESSION_TTL", DEFAULT_SESSION_TTL_SECONDS)
        ),
        max_login_attempts=int(
            os.environ.get("AUTH_MAX_LOGIN_ATTEMPTS", DEFAULT_MAX_LOGIN_ATTEMPTS)
        ),
        lockout_window_seconds=int(os.environ.get("AUTH_LOCKOUT_WINDOW", "300")),
        audit_enabled=os.environ.get("AUTH_AUDIT_ENABLED", "1") == "1",
    )
''',
    "src/core/__init__.py": '''"""基础异常与日志。"""

from src.core.errors import (
    DomainError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)

__all__ = [
    "DomainError",
    "NotFoundError",
    "PermissionDeniedError",
    "ValidationError",
]
''',
    "src/core/errors.py": '''"""领域异常层级。"""


class DomainError(Exception):
    """业务异常基类。"""


class NotFoundError(DomainError):
    """资源不存在。"""


class PermissionDeniedError(DomainError):
    """权限不足。"""


class ValidationError(DomainError):
    """请求参数非法。"""
''',
    "src/core/logging.py": '''"""统一日志与审计通道。"""

import json
import logging

_AUDIT_LOGGER = logging.getLogger("evolyn.audit")


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(level=level)


def audit_log(event: dict) -> None:
    """以结构化 JSON 写入审计通道。"""

    _AUDIT_LOGGER.info(
        json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    )
''',
    "src/services/__init__.py": '"""业务服务包。"""\n',
    "src/services/audit_service.py": '''"""审计事件记录。"""

from src.config.settings import load_settings
from src.core.logging import audit_log

_EVENTS: list[dict] = []


def record(event: dict) -> None:
    """记录审计事件；按配置决定是否写入日志通道。"""

    _EVENTS.append(event)
    if load_settings().audit_enabled:
        audit_log(event)


def recent(limit: int = 20) -> list[dict]:
    """最近的事件（最多 limit 条）。"""

    return _EVENTS[-limit:]
''',
    "src/services/user_service.py": '''"""用户读写服务（进程内存储+初始化 schema）。"""

from src.core.errors import NotFoundError
from src.storage import db
from src.storage.models import User

_BOOTSTRAPPED = False
_USERS: dict[str, User] = {}


def _bootstrap() -> None:
    """首次使用时初始化表结构。"""

    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    executor = db.QueryExecutor(db.connect(":memory:"))
    for statement in db.SCHEMA_STATEMENTS:
        executor.execute(statement)
    _BOOTSTRAPPED = True


def find_user(username: str) -> User | None:
    """按用户名查找。"""

    _bootstrap()
    return _USERS.get(username)


def find_by_id(user_id: str) -> User | None:
    """按用户 ID 查找。"""

    _bootstrap()
    for user in _USERS.values():
        if user.id == user_id:
            return user
    return None


def create_user(username: str, password: str, role: str = "user") -> User:
    """创建用户（内部调用哈希）。"""

    # 延迟导入：避免 auth <-> services 的模块级循环。
    from src.auth.hashing import hash_password

    _bootstrap()
    user = User(
        id=str(len(_USERS) + 1),
        username=username,
        role=role,
        password_hash=hash_password(password),
    )
    _USERS[username] = user
    return user


def update_password(user_id: str, password: str) -> None:
    """更新密码并记录审计。"""

    from src.auth.hashing import hash_password
    from src.services import audit_service

    _bootstrap()
    for user in _USERS.values():
        if user.id == user_id:
            user.password_hash = hash_password(password)
            audit_service.record(
                {"event": "password_updated", "user_id": user_id}
            )
            return
    raise NotFoundError(f"user {user_id} not found")


def list_users() -> list[User]:
    """列出全部用户。"""

    _bootstrap()
    return list(_USERS.values())


def export_user_data(user_id: str) -> dict:
    """导出指定用户的个人数据；不校验调用方与目标的关系。"""

    user = find_by_id(user_id)
    if user is None:
        raise NotFoundError(f"user {user_id} not found")
    return {
        "user_id": user.id,
        "username": user.username,
        "role": user.role,
    }


def delete_user(user_id: str) -> None:
    """删除用户。"""

    _bootstrap()
    for username, user in list(_USERS.items()):
        if user.id == user_id:
            del _USERS[username]
            return
    raise NotFoundError(f"user {user_id} not found")
''',
    "src/storage/__init__.py": '"""持久化包。"""\n',
    "src/storage/db.py": '''"""SQLite 连接与查询执行器。"""

import sqlite3

SCHEMA_STATEMENTS: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, username TEXT UNIQUE)",
    "CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, user_id TEXT)",
)


def connect(dsn: str) -> sqlite3.Connection:
    """打开连接；内存库用于本地开发。"""

    return sqlite3.connect(dsn)


class QueryExecutor:
    """执行语句并返回结果行。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def execute(self, sql: str, params: tuple = ()) -> list[tuple]:
        cursor = self._connection.execute(sql, params)
        self._connection.commit()
        return cursor.fetchall()
''',
    "src/storage/models.py": '''"""持久化模型。"""

from dataclasses import dataclass


@dataclass
class User:
    """用户记录。"""

    id: str
    username: str
    role: str
    password_hash: bytes


@dataclass
class ApiKey:
    """API Key 记录。"""

    key_id: str
    user_id: str
''',
    "src/api/__init__.py": '"""HTTP 接口包。"""\n',
    "src/api/errors.py": '''"""Domain 异常到 HTTP 响应的映射。"""

from collections.abc import Callable
from functools import wraps

from src.core.errors import (
    DomainError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)


def error_response(error: DomainError) -> tuple[int, dict]:
    """把领域异常映射为 (status_code, body)。"""

    if isinstance(error, NotFoundError):
        return 404, {"error": "not found"}
    if isinstance(error, PermissionDeniedError):
        return 403, {"error": "forbidden"}
    if isinstance(error, ValidationError):
        return 400, {"error": "invalid request"}
    return 500, {"error": "internal error"}


def wrap_domain_errors(handler: Callable) -> Callable:
    """装饰器：捕获 DomainError 并按映射返回响应。"""

    @wraps(handler)
    def wrapped(*args, **kwargs):
        try:
            return handler(*args, **kwargs)
        except DomainError as error:
            return error_response(error)

    return wrapped
''',
    "src/api/middleware.py": '''"""请求级认证中间件。"""

from src.auth.session import Session, SessionStore, validate_session


def auth_middleware(request, store: SessionStore) -> Session | None:
    """从 X-Session-Id 校验会话；失败返回 None。"""

    session_id = request.headers.get("X-Session-Id", "")
    if not session_id:
        return None
    return validate_session(session_id, store)
''',
    "src/api/routes.py": '''"""HTTP 路由处理函数。"""

from src.api.errors import wrap_domain_errors
from src.auth import permissions
from src.auth.login import login
from src.auth.session import SessionStore
from src.services import audit_service, user_service


@wrap_domain_errors
def handle_login(request, store: SessionStore):
    """POST /login：用户名密码换会话。"""

    session = login(
        request.body.get("username", ""),
        request.body.get("password", ""),
        store,
    )
    if session is None:
        return 401, {"error": "invalid credentials"}
    return 200, {"session_id": session.session_id}


@wrap_domain_errors
def handle_profile(request, store: SessionStore):
    """GET /profile：按会话直接取回当前用户。

    为省一次过期判断，这里直接调用 SessionStore.get_session。
    """

    session = store.get_session(request.headers.get("X-Session-Id", ""))
    if session is None:
        return 401, {"error": "unauthorized"}
    user = user_service.find_by_id(session.user_id)
    return 200, {"username": user.username, "role": user.role}


@wrap_domain_errors
def handle_admin_users(request, store: SessionStore):
    """GET /admin/users：列出全部用户。"""

    return 200, {"users": [user.username for user in user_service.list_users()]}


@wrap_domain_errors
def handle_user_export(request, store: SessionStore):
    """GET /export?user_id=...：导出用户数据。"""

    user_id = request.query.get("user_id", "")
    data = user_service.export_user_data(user_id)
    audit_service.record({"event": "user_exported", "user_id": user_id})
    return 200, data


@wrap_domain_errors
def handle_admin_audit(request, store: SessionStore):
    """GET /admin/audit：查看最近审计事件（需要 manage_users 权限）。"""

    user = user_service.find_by_id(request.session.user_id)
    permissions.require_permission(user, "manage_users")
    return 200, {"events": audit_service.recent(50)}
''',
    "src/api/app.py": '''"""最小应用装配：路由表与分发。"""

from src.api import middleware, routes
from src.auth.session import SessionStore

ROUTES = {
    ("POST", "/login"): routes.handle_login,
    ("GET", "/profile"): routes.handle_profile,
    ("GET", "/export"): routes.handle_user_export,
    ("GET", "/admin/users"): routes.handle_admin_users,
    ("GET", "/admin/audit"): routes.handle_admin_audit,
}

_AUTHENTICATED_PREFIXES = ("/admin/",)


def dispatch(method: str, path: str, request, store: SessionStore):
    """按方法+路径分发；/admin/* 先过认证中间件。"""

    if path.startswith(_AUTHENTICATED_PREFIXES):
        session = middleware.auth_middleware(request, store)
        if session is None:
            return 401, {"error": "unauthorized"}
        request.session = session
    handler = ROUTES.get((method, path))
    if handler is None:
        return 404, {"error": "not found"}
    return handler(request, store)


def main() -> None:
    """本地开发入口（真实 HTTP Server 未实现，保留占位）。"""

    raise SystemExit("this fixture needs a real HTTP server around dispatch()")


if __name__ == "__main__":
    main()
''',
    "tests/__init__.py": '"""夹具仓库测试包。"""\n',
    "tests/test_login.py": '''"""登录流程测试。"""

from src.auth.login import login
from src.auth.session import SessionStore
from src.services import user_service


def _make_user(username: str = "alice") -> None:
    user_service.create_user(username, "secret-123", role="user")


def test_login_success() -> None:
    _make_user("alice")
    store = SessionStore()
    session = login("alice", "secret-123", store)
    assert session is not None
    assert store.get_session(session.session_id) is not None


def test_login_wrong_password() -> None:
    _make_user("bob")
    store = SessionStore()
    assert login("bob", "wrong", store) is None
''',
    "tests/test_session.py": '''"""会话创建与校验测试。"""

from src.auth.session import SessionStore, create_session, validate_session


def test_create_session() -> None:
    store = SessionStore()
    session = create_session("user-1", store)
    assert session.session_id
    assert validate_session(session.session_id, store) is not None


def test_validate_unknown_session() -> None:
    store = SessionStore()
    assert validate_session("missing", store) is None
''',
    "tests/test_permissions.py": '''"""权限策略测试。"""

from src.auth.permissions import check_permission


def test_admin_can_manage_users() -> None:
    assert check_permission("admin", "manage_users")


def test_user_cannot_export() -> None:
    assert not check_permission("user", "export")


def test_user_can_read() -> None:
    assert check_permission("user", "read")
''',
}


def fixture_paths() -> tuple[str, ...]:
    """返回夹具仓库的全部相对路径（排序稳定）。"""

    return tuple(sorted(FIXTURE_FILES))


def write_fixture_repo(root: Path) -> tuple[str, ...]:
    """把夹具仓库写入 ``root``；返回写入的路径列表。"""

    root.mkdir(parents=True, exist_ok=True)
    for relative, content in FIXTURE_FILES.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content.lstrip("\n"), encoding="utf-8")
    return fixture_paths()


__all__ = [
    "FIXTURE_FILES",
    "FIXTURE_NAME",
    "fixture_paths",
    "write_fixture_repo",
]
