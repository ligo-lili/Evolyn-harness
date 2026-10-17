"""夹具仓库：shop-auth-sandbox（确定性、自包含、无外部依赖）。

仓库服务于核心 20 个评测任务（函数定位、跨文件调用链、配置追踪、异常处理、
测试覆盖、跨模块依赖 + 安全子集）与「大范围探索」扩展任务（全仓盘点、
负向证明、跨模块链；见 tasks.py 的 large suite）。

设计约束：

- 纯文本落地，不执行；代码只要求结构真实（导入关系、调用关系、配置读取）；
- 每个任务的标准答案都能在文件内容里精确核对（tasks.py 的常见疑问）；
- 有意保留的「缺口」（权限缺失、会话绕过、数据隔离缺失、未使用配置、
  Deprecated 无调用方函数、未读开关、文档中从未读取的变量）是任务要发现的
  确定事实，修改夹具内容时必须同步核对任务答案。
"""

from __future__ import annotations

from pathlib import Path

FIXTURE_NAME = "shop-auth-sandbox"

FIXTURE_FILES: dict[str, str] = {
    "README.md": '''# shop-auth-sandbox

一个用于本地探索的认证服务夹具仓库（附电商后台模块，供大范围检索任务使用）。

- `src/api/`：路由装配、请求中间件与错误映射
- `src/auth/`：登录、会话、令牌与权限
- `src/config/`：集中配置、功能开关与限额
- `src/services/`：用户与审计业务
- `src/storage/`：持久化访问
- `src/core/`：基础异常与日志
- `src/modules/`：电商业务模块（结算、退款、订单等）
- `src/integrations/`：支付、税务、通知等外部适配
- `src/legacy/`：迁移中的历史代码（勿新增引用）
- `docs/`：环境变量参考与运维文档
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


# ---------------------------------------------------------------------------
# 大仓库扩展包（大范围探索任务专用）
#
# 在最小认证服务之外再挂一个「模块化电商后台」：12 个业务模块、外部集成
# 适配、legacy 遗留层、文档与测试，让「全仓检索 / 盘点 / 负向证明」类任务
# 有真实的规模与噪音。确定性约束（tests/test_fixtures.py 有对应断言）：
#
# - 「未使用」事实在全仓库唯一：Deprecated 且无调用方的 4 个 legacy 函数、
#   从未被读取的开关 ENABLE_LEGACY_EXPORT、只出现在文档里的 2 个环境变量；
# - 导入期连接（模块顶层 connect）只出现在 3 个集成适配文件；
# - 模块测试只覆盖部分模块（覆盖清单见 tasks.py 的 L08）。
# ---------------------------------------------------------------------------

_GENERATED_STORE_MODULES: tuple[tuple[str, str, str, str, str], ...] = (
    # (模块名, 中文名, 处理函数, 服务函数, 说明)：写入型模板。
    ("accounts", "账户", "handle_accounts_update", "update_account", "更新账户资料"),
    ("cart", "购物车", "handle_cart_add", "add_to_cart", "把商品加入购物车"),
    ("inventory", "库存", "handle_inventory_reserve", "reserve_stock", "预占库存"),
    ("promotions", "促销", "handle_promotions_apply", "apply_promotion", "应用促销码"),
)

_GENERATED_READ_MODULES: tuple[tuple[str, str, str, str, str], ...] = (
    # (模块名, 中文名, 处理函数, 服务函数, 说明)：读取型模板。
    (
        "catalog", "商品目录", "handle_catalog_list",
        "list_catalog_items", "列出商品目录",
    ),
    ("orders", "订单", "handle_orders_list", "list_user_orders", "列出用户订单"),
    ("shipping", "配送", "handle_shipping_quote", "quote_shipping", "计算配送费用"),
)

_GENERATED_SERVICE_STORE = '''"""%(zh)s模块业务逻辑。"""

from src.core.errors import ValidationError

_RECORDS: dict[str, dict] = {}


def %(fn)s(payload: dict) -> tuple[int, dict]:
    """%(desc)s。"""

    key = str(payload.get("id", "")).strip()
    if not key:
        raise ValidationError("id is required")
    _RECORDS[key] = payload
    return 200, {"id": key, "saved": True}
'''

_GENERATED_SERVICE_READ = '''"""%(zh)s模块业务逻辑。"""

_RECORDS: dict[str, dict] = {}


def %(fn)s(payload: dict) -> tuple[int, dict]:
    """%(desc)s。"""

    items = sorted(_RECORDS.values(), key=lambda item: str(item.get("id", "")))
    return 200, {"items": items[:50]}
'''

_GENERATED_HANDLER = '''"""%(zh)s模块请求处理函数。"""

from src.api.errors import wrap_domain_errors
from src.modules.%(name)s.service import %(fn)s


@wrap_domain_errors
def %(handler)s(request):
    """%(desc)s。"""

    return %(fn)s(request.body)
'''

_GENERATED_MODULE_INIT = '''"""%(zh)s模块。"""

from src.modules.%(name)s.handler import %(handler)s

__all__ = ["%(handler)s"]
'''

_CHECKOUT_SERVICE = '''"""结算模块业务逻辑：算税与扣款编排。"""

from src.config.feature_flags import flag_enabled
from src.config.limits import MAX_ORDER_ITEMS
from src.core.errors import ValidationError
from src.integrations.payments import charge_card
from src.integrations.tax import calculate_tax_for_order


def confirm_order(payload: dict) -> tuple[int, dict]:
    """校验订单并依次算税、扣款；新结算开关关闭时直接拒绝。"""

    if not flag_enabled("ENABLE_NEW_CHECKOUT"):
        return 503, {"error": "checkout disabled"}
    items = payload.get("items", [])
    if not items or len(items) > MAX_ORDER_ITEMS:
        raise ValidationError("invalid item count")
    amount_cents = int(payload.get("amount_cents", 0))
    tax_cents = calculate_tax_for_order(amount_cents)
    receipt = charge_card(
        str(payload.get("order_id", "")),
        amount_cents + tax_cents,
    )
    return 200, {"receipt": receipt, "tax_cents": tax_cents}
'''

_REFUNDS_SERVICE = '''"""退款模块业务逻辑。"""

from src.config.limits import MAX_REFUND_DAYS
from src.core.errors import NotFoundError, ValidationError

_ORDERS: dict[str, dict] = {}


def request_refund(payload: dict) -> tuple[int, dict]:
    """校验订单存在且未超期，然后登记退款。"""

    order_id = str(payload.get("order_id", ""))
    if order_id not in _ORDERS:
        raise NotFoundError(f"order {order_id} not found")
    days = int(payload.get("days_since_purchase", 0))
    if days > MAX_REFUND_DAYS:
        raise ValidationError("refund window expired")
    return 200, {"order_id": order_id, "status": "refund_requested"}
'''

_REVIEWS_SERVICE = '''"""评价模块业务逻辑。"""

from src.config.feature_flags import flag_enabled
from src.core.errors import ValidationError

_REVIEWS: dict[str, dict] = {}


def publish_review(payload: dict) -> tuple[int, dict]:
    """发布评价；V2 开关关闭时拒绝带图评价。"""

    if payload.get("has_image") and not flag_enabled("ENABLE_REVIEWS_V2"):
        raise ValidationError("images disabled")
    _REVIEWS[str(payload.get("id", ""))] = payload
    return 200, {"saved": True}
'''

_NOTIFICATIONS_SERVICE = '''"""通知模块业务逻辑。"""

from src.config.feature_flags import flag_enabled
from src.integrations import email, sms


def dispatch_notification(payload: dict) -> tuple[int, dict]:
    """按渠道派发通知；聊天通知由开关控制。"""

    channel = str(payload.get("channel", "email"))
    to = str(payload.get("to", ""))
    text = str(payload.get("text", ""))
    if channel == "email":
        email.send(to, text)
    elif channel == "sms":
        sms.send(to, text)
    elif channel != "chat" or not flag_enabled("ENABLE_SUPPORT_CHAT"):
        raise ValueError(f"unknown channel: {channel}")
    return 200, {"channel": channel, "sent": True}
'''

_AUDIT_SERVICE = '''"""审计模块业务逻辑。"""

from src.legacy.audit_v1 import record_audit_v1


def list_audit_events(payload: dict) -> tuple[int, dict]:
    """列出审计事件；旁路写入仍走 v1 通道（迁移中）。"""

    record_audit_v1({"action": "audit_list", "actor": payload.get("actor", "")})
    return 200, {"events": []}
'''

_HAND_WRITTEN_MODULES: tuple[tuple[str, str, str, str, str, str], ...] = (
    # (模块名, 中文名, 处理函数, 服务函数, 说明, 服务源码)
    (
        "checkout", "结算", "handle_checkout_confirm", "confirm_order",
        "确认订单", _CHECKOUT_SERVICE,
    ),
    (
        "refunds", "退款", "handle_refunds_request", "request_refund",
        "登记退款", _REFUNDS_SERVICE,
    ),
    (
        "reviews", "评价", "handle_reviews_publish", "publish_review",
        "发布商品评价", _REVIEWS_SERVICE,
    ),
    (
        "notifications", "通知", "handle_notifications_send",
        "dispatch_notification", "派发用户通知", _NOTIFICATIONS_SERVICE,
    ),
    (
        "audit", "审计", "handle_audit_list", "list_audit_events",
        "列出审计事件", _AUDIT_SERVICE,
    ),
)

_INTEGRATION_FILES: dict[str, str] = {
    "src/integrations/payments.py": '''"""支付渠道适配。"""

import os

from src.legacy.config_v1 import load_legacy_config

_API_KEY = os.environ.get("PAYMENTS_API_KEY", "")
_LEGACY = load_legacy_config()


def charge_card(order_id: str, amount_cents: int) -> dict:
    """向支付网关发起扣款（夹具不真正发请求）。"""

    return {
        "order_id": order_id,
        "amount_cents": amount_cents,
        "gateway": _LEGACY.get("base_url", "sandbox"),
        "has_key": bool(_API_KEY),
    }
''',
    "src/integrations/tax.py": '''"""税费计算适配。"""

import os

from src.config.feature_flags import flag_enabled

_TAX_RATE_PERCENT = float(os.environ.get("TAX_RATE_PERCENT", "0") or 0)


def calculate_tax_for_order(amount_cents: int) -> int:
    """按配置税率计算税额；V2 开关启用时使用四舍五入。"""

    if amount_cents <= 0:
        return 0
    raw = amount_cents * _TAX_RATE_PERCENT / 100
    if flag_enabled("ENABLE_TAX_V2"):
        return round(raw)
    return int(raw)
''',
    "src/integrations/email.py": '''"""邮件通道适配。"""

import os

_RELAY_URL = os.environ.get("EMAIL_RELAY_URL", "")


def send(to: str, text: str) -> dict:
    """发送邮件（夹具不真正发请求）。"""

    return {"to": to, "chars": len(text), "relay": bool(_RELAY_URL)}
''',
    "src/integrations/sms.py": '''"""短信通道适配。"""

import os

_PROVIDER_URL = os.environ.get("SMS_PROVIDER_URL", "")


def send(to: str, text: str) -> dict:
    """发送短信（夹具不真正发请求）。"""

    return {"to": to, "chars": len(text), "provider": bool(_PROVIDER_URL)}
''',
    "src/integrations/analytics.py": '''"""分析事件写入（导入期建立连接）。"""

import os

from src.storage.db import connect

_CONNECTION = connect("analytics.db")
_WRITE_KEY = os.environ.get("ANALYTICS_WRITE_KEY", "")


def track(name: str, payload: dict) -> None:
    """记录分析事件。"""

    _CONNECTION.execute("SELECT 1")
''',
    "src/integrations/storage.py": '''"""对象存储适配（导入期建立连接）。"""

from src.storage.db import connect

_CONNECTION = connect("objects.db")


def put_object(bucket: str, key: str) -> str:
    """登记对象路径。"""

    _CONNECTION.execute("SELECT 1")
    return f"{bucket}/{key}"
''',
    "src/integrations/search.py": '''"""搜索索引适配（导入期建立连接）。"""

from src.storage.db import connect

_CONNECTION = connect("search_index.db")


def index_document(doc_id: str, text: str) -> None:
    """写入索引；重建由离线任务负责。"""

    _CONNECTION.execute("SELECT 1")
''',
    "src/integrations/shipping_carrier.py": '''"""承运商适配。"""

import os

_CARRIER_CODE = os.environ.get("SHIPPING_CARRIER_CODE", "demo")


def quote_shipping_weight(weight_grams: int) -> int:
    """按重量估算运费（分）。"""

    return max(500, weight_grams // 10)
''',
}

_LEGACY_FILES: dict[str, str] = {
    "src/legacy/orders_v1.py": '''"""旧版订单金额计算（v1）。"""


def calculate_total_v1(items: list[dict]) -> int:
    """Deprecated：旧版合计计算，已被结算模块取代，没有调用方。"""

    total = 0
    for item in items:
        total += int(item.get("price_cents", 0)) * int(item.get("qty", 1))
    return total
''',
    "src/legacy/permissions_v1.py": '''"""旧版权限检查（v1）。"""


def check_permission_v1(role: str, action: str) -> bool:
    """Deprecated：旧版权限矩阵，已被 src/auth/permissions.py 取代。"""

    return role == "admin" or action == "read"
''',
    "src/legacy/pricing_v1.py": '''"""旧版折扣计算（v1）。"""


def apply_discount_v1(amount_cents: int, percent: float) -> int:
    """Deprecated：旧版折扣逻辑没有调用方，等待删除。"""

    return int(amount_cents * (1 - percent / 100))
''',
    "src/legacy/tokens_v1.py": '''"""旧版令牌（v1）。"""

import os


def issue_token_v1(user_id: str) -> str:
    """Deprecated：旧版静态前缀令牌没有调用方。"""

    prefix = os.environ.get("LEGACY_TOKEN_PREFIX", "")
    return f"v1.{user_id}.{prefix}"
''',
    "src/legacy/audit_v1.py": '''"""旧版审计写入（v1）。"""

_EVENTS: list[dict] = []


def record_audit_v1(event: dict) -> None:
    """Deprecated：仍被审计模块暂时复用，迁移完成后删除。"""

    _EVENTS.append(event)
''',
    "src/legacy/config_v1.py": '''"""旧版配置加载（v1）。"""

import os


def load_legacy_config() -> dict:
    """Deprecated：仍被支付适配复用；从环境变量读取旧配置。"""

    return {
        "base_url": os.environ.get("LEGACY_BASE_URL", ""),
        "retries": int(os.environ.get("LEGACY_RETRIES", "0") or 0),
    }
''',
}

_EXTRA_CONFIG_FILES: dict[str, str] = {
    "src/config/feature_flags.py": '''"""功能开关：从环境变量按名称读取。"""

import os

_FLAGS = (
    "ENABLE_NEW_CHECKOUT",
    "ENABLE_TAX_V2",
    "ENABLE_REVIEWS_V2",
    "ENABLE_SUPPORT_CHAT",
    "ENABLE_LEGACY_EXPORT",
)


def flag_enabled(name: str) -> bool:
    """开关仅在显式开启时生效（默认关闭）。"""

    return os.environ.get(f"FLAG_{name}", "0") == "1"
''',
    "src/config/limits.py": '''"""业务限额。"""

MAX_ORDER_ITEMS = 50
MAX_REFUND_DAYS = 30
RATE_LIMIT_PER_MINUTE = 120
''',
}

_DOC_FILES: dict[str, str] = {
    "docs/env_reference.md": '''# 环境变量参考

以下变量由部署环境注入（本地开发可省略，走默认值）。

| 变量 | 说明 |
| --- | --- |
| AUTH_TOKEN_SECRET | 令牌签名密钥 |
| AUTH_SESSION_TTL | 会话有效期（秒） |
| AUTH_MAX_LOGIN_ATTEMPTS | 登录失败上限 |
| AUTH_LOCKOUT_WINDOW | 锁定时窗（秒） |
| AUTH_AUDIT_ENABLED | 审计通道开关 |
| PAYMENTS_API_KEY | 支付网关密钥 |
| TAX_RATE_PERCENT | 税率（百分比） |
| EMAIL_RELAY_URL | 邮件中继地址 |
| SMS_PROVIDER_URL | 短信通道地址 |
| ANALYTICS_WRITE_KEY | 分析写入密钥 |
| SHIPPING_CARRIER_CODE | 承运商代码 |
| LEGACY_BASE_URL | 旧版配置地址（迁移中） |
| LEGACY_RETRIES | 旧版重试次数（迁移中） |
| VOICE_ORDERS_ENABLED | 语音下单开关（规划中，未接入） |
| LEGACY_EXPORT_BUCKET | 旧导出桶（已停用，未接入） |
''',
    "docs/architecture.md": '''# 架构概览

- `src/api/`：路由装配与请求中间件（最小实现）。
- `src/auth/`：认证、会话与权限。
- `src/config/`：集中配置、功能开关与限额。
- `src/services/`：用户与审计服务。
- `src/storage/`：SQLite 访问。
- `src/modules/`：电商业务模块（结算、订单、库存等）。
- `src/integrations/`：支付、税务、通知等外部适配。
- `src/legacy/`：迁移中的历史代码，不允许新增引用。

业务模块统一由 `handler.py`（请求边界）与 `service.py`（业务逻辑）组成；
外部依赖一律经 `integrations` 适配层。
''',
    "docs/runbook.md": '''# 运维手册（节选）

- 发布前运行 `pytest tests/`；
- 支付与税务适配依赖环境变量，见 `env_reference.md`；
- `src/legacy/` 在迁移完成前不要删除；
- 搜索索引由离线任务重建（见 search 适配）。
''',
    "docs/onboarding.md": '''# 新人上手

1. 阅读 `architecture.md` 了解目录结构；
2. 本地运行 `pytest tests/`；
3. 新代码不得直接引用 `src/legacy/`；
4. 业务模块沿用 `handler.py` + `service.py` 双层结构。
''',
    "docs/changelog.md": '''# 变更记录

## 2026-09

- 结算确认链路接入税务与支付适配；
- 退款模块加入超期校验；
- 旧导出通道停用（相关配置见 `env_reference.md`）。

## 2026-08

- 权限 v1 标记弃用，等待迁移到 `src/auth/`。
''',
}

_GENERATED_TEST_FILES: dict[str, str] = {
    "tests/test_accounts.py": '''"""账户模块测试。"""

from src.modules.accounts.service import update_account


def test_update_account_is_importable() -> None:
    assert callable(update_account)
''',
    "tests/test_catalog.py": '''"""商品目录模块测试。"""

from src.modules.catalog.service import list_catalog_items


def test_list_catalog_is_importable() -> None:
    assert callable(list_catalog_items)
''',
    "tests/test_checkout.py": '''"""结算模块测试。"""

from src.modules.checkout.service import confirm_order


def test_confirm_order_is_importable() -> None:
    assert callable(confirm_order)
''',
    "tests/test_orders.py": '''"""订单模块测试。"""

from src.modules.orders.service import list_user_orders


def test_list_orders_is_importable() -> None:
    assert callable(list_user_orders)
''',
    "tests/test_refunds.py": '''"""退款模块测试。"""

from src.modules.refunds.service import request_refund


def test_request_refund_is_importable() -> None:
    assert callable(request_refund)
''',
    "tests/test_tax.py": '''"""税费适配测试。"""

from src.integrations.tax import calculate_tax_for_order


def test_tax_is_importable() -> None:
    assert callable(calculate_tax_for_order)
''',
}


def _module_pack(
    name: str,
    zh: str,
    handler: str,
    fn: str,
    desc: str,
    *,
    store: bool = True,
    service: str | None = None,
) -> dict[str, str]:
    """一个模块的三个文件：__init__ / handler / service。"""

    if service is None:
        template = _GENERATED_SERVICE_STORE if store else _GENERATED_SERVICE_READ
        service = template % {"zh": zh, "fn": fn, "desc": desc}
    values = {"name": name, "zh": zh, "handler": handler, "fn": fn, "desc": desc}
    return {
        f"src/modules/{name}/__init__.py": _GENERATED_MODULE_INIT % values,
        f"src/modules/{name}/handler.py": _GENERATED_HANDLER % values,
        f"src/modules/{name}/service.py": service,
    }


def _generated_files() -> dict[str, str]:
    """大仓库扩展包的完整文件集合（确定性：无随机、无外部读取）。"""

    files: dict[str, str] = {
        "src/modules/__init__.py": '"""业务模块包（按领域拆分）。"""\n',
        "src/integrations/__init__.py": '"""外部集成适配包。"""\n',
        "src/legacy/__init__.py": '"""历史遗留代码（迁移中，勿新增引用）。"""\n',
    }
    for name, zh, handler, fn, desc in _GENERATED_STORE_MODULES:
        files.update(_module_pack(name, zh, handler, fn, desc, store=True))
    for name, zh, handler, fn, desc in _GENERATED_READ_MODULES:
        files.update(_module_pack(name, zh, handler, fn, desc, store=False))
    for name, zh, handler, fn, desc, service in _HAND_WRITTEN_MODULES:
        files.update(
            _module_pack(name, zh, handler, fn, desc, service=service)
        )
    files.update(_INTEGRATION_FILES)
    files.update(_LEGACY_FILES)
    files.update(_EXTRA_CONFIG_FILES)
    files.update(_DOC_FILES)
    files.update(_GENERATED_TEST_FILES)
    return files


FIXTURE_FILES.update(_generated_files())


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
