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
仓库是**确定性的**：没有随机种子、没有外部网络，任何一次生成的结果一致。

## 目录地图

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

## 本地运行

    pytest tests/

## 约定

- 领域异常统一从 `src/core/errors.py` 抛出，由 `src/api/errors.py` 映射；
- 环境变量在 `docs/env_reference.md` 登记后使用；
- `src/legacy/` 处于迁移中：不要删除，也不要新增引用。
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
    "src/auth/hashing.py": '''"""密码哈希：PBKDF2-HMAC-SHA256。

迭代次数与盐长度是安全参数；调大前先评估登录延迟（见 runbook 的容量
章节）。哈希输出为 ``salt + digest``，salt 固定前置，便于整体入库。
"""

import hashlib
import hmac
import os

_ITERATIONS = 200_000
_SALT_BYTES = 16
_DIGEST_BYTES = 32


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


def needs_rehash(stored: bytes) -> bool:
    """存量哈希长度与当前参数不符时提示重算（迁移期辅助）。"""

    return len(stored) != _SALT_BYTES + _DIGEST_BYTES
''',
    "src/auth/session.py": '''"""会话创建、校验与过期管理。

会话保存在进程内（SessionStore），重启即失效；持久化迁移见 changelog。
注意两套读取语义：`validate_session` 会检查过期并清理；`SessionStore.
get_session` 只做字典取回——路由层有一处直接用了后者（见 api/routes 的
handle_profile 说明）。
"""

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

    def is_expired(self, now: float | None = None) -> bool:
        """是否已过期（now 参数便于测试注入时钟）。"""

        return self.expires_at <= (now if now is not None else time())


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

    def purge_expired(self, now: float | None = None) -> int:
        """清理过期会话，返回清理条数（由定时任务调用）。"""

        moment = now if now is not None else time()
        expired = [
            key
            for key, item in self._sessions.items()
            if item.is_expired(moment)
        ]
        for key in expired:
            self._sessions.pop(key, None)
        return len(expired)

    def count(self) -> int:
        """当前会话数（监控用）。"""

        return len(self._sessions)


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
    "src/auth/tokens.py": '''"""短期访问令牌：签名签发与校验。

令牌格式 ``user_id.signature``；TTL 常量仅作文档用途——校验函数**不检查
过期**（历史遗留，迁移清单 TOK-07）。密钥直接读环境变量，未经过集中配置。
"""

import hmac
import os
from hashlib import sha256

TOKEN_TTL_SECONDS = 900
_SIGNATURE_LENGTH = 64


def _secret() -> bytes:
    """直接读取环境变量，未经过集中配置。"""

    return os.environ.get("AUTH_TOKEN_SECRET", "").encode("utf-8")


def _sign(user_id: str) -> str:
    return hmac.new(_secret(), user_id.encode("utf-8"), sha256).hexdigest()


def _split(token: str) -> tuple[str, str]:
    """把令牌拆成 (user_id, signature)；形状不对时返回空串对。"""

    user_id, _, signature = str(token or "").partition(".")
    if not user_id or not signature:
        return "", ""
    return user_id, signature


def issue_token(user_id: str) -> str:
    """签发令牌：``user_id.signature``。"""

    return f"{user_id}.{_sign(user_id)}"


def token_ttl_note() -> str:
    """返回给调用方的 TTL 说明（当前仅文案用途）。"""

    return f"ttl={TOKEN_TTL_SECONDS}s"


def verify_token(token: str) -> str | None:
    """校验签名并返回 user_id；不包含过期校验。"""

    user_id, signature = _split(token)
    if not user_id or not signature:
        return None
    if hmac.compare_digest(signature, _sign(user_id)):
        return user_id
    return None
''',
    "src/auth/permissions.py": '''"""角色权限策略与检查。

策略以 (role, action) 白名单维护；未知角色按最小权限处理（空集合），
不抛异常——调用方用 require_permission 决定是否升级为 403。
"""

from src.core.errors import PermissionDeniedError

ROLE_POLICY: dict[str, frozenset[str]] = {
    "admin": frozenset({"read", "write", "export", "manage_users"}),
    "user": frozenset({"read"}),
}

KNOWN_ACTIONS: tuple[str, ...] = ("read", "write", "export", "manage_users")


def check_permission(role: str, action: str) -> bool:
    """角色是否允许执行动作；未知角色按最小权限处理。"""

    return action in ROLE_POLICY.get(role, frozenset())


def require_permission(user: object, action: str) -> None:
    """不满足权限时抛出 PermissionDeniedError。"""

    role = getattr(user, "role", "")
    if not check_permission(role, action):
        raise PermissionDeniedError(f"role {role!r} cannot {action}")


def actions_for(role: str) -> tuple[str, ...]:
    """列出角色可执行的动作（后台配置页展示用）。"""

    return tuple(sorted(ROLE_POLICY.get(role, frozenset())))
''',
    "src/auth/login.py": '''"""登录流程与失败锁定。

失败次数按用户名计数，达到上限后在锁定时窗内拒绝登录。窗口本身尚未
生效（只判断次数，见 T-103 待办）：_FAILED_ATTEMPTS 不会随时间清零。
"""

from src.auth.hashing import verify_password
from src.auth.session import Session, SessionStore, create_session
from src.config.settings import load_settings
from src.services import audit_service, user_service

_FAILED_ATTEMPTS: dict[str, int] = {}
_LOCKOUT_EVENT = "login_locked"


def is_locked(username: str) -> bool:
    """失败次数达到上限即锁定。"""

    return _FAILED_ATTEMPTS.get(username, 0) >= load_settings().max_login_attempts


def record_failure(username: str) -> None:
    """记录一次失败尝试。"""

    _FAILED_ATTEMPTS[username] = _FAILED_ATTEMPTS.get(username, 0) + 1


def reset_failures(username: str) -> None:
    """清零失败计数（登录成功或运维手动解锁时调用）。"""

    _FAILED_ATTEMPTS.pop(username, None)


def failure_count(username: str) -> int:
    """当前失败次数（测试与监控用）。"""

    return _FAILED_ATTEMPTS.get(username, 0)


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
    "src/config/defaults.py": '''"""配置默认值。

未显式设置环境变量时使用；改默认值等于改行为，需要走变更评审。
"""

DEFAULT_SESSION_TTL_SECONDS = 3600
DEFAULT_MAX_LOGIN_ATTEMPTS = 5
DEFAULT_LOCKOUT_WINDOW_SECONDS = 300
DEFAULT_AUDIT_ENABLED = True
''',
    "src/config/settings.py": '''"""集中配置：环境变量 -> Settings。

所有配置读取都要经过这里；`tokens.py` 的历史直读是已知例外（迁移清单
CFG-12）。Settings 是冻结 dataclass，加载后不随环境变化。
"""

import os
from dataclasses import dataclass

from src.config.defaults import (
    DEFAULT_AUDIT_ENABLED,
    DEFAULT_LOCKOUT_WINDOW_SECONDS,
    DEFAULT_MAX_LOGIN_ATTEMPTS,
    DEFAULT_SESSION_TTL_SECONDS,
)


@dataclass(frozen=True)
class Settings:
    session_ttl_seconds: int
    max_login_attempts: int
    lockout_window_seconds: int
    audit_enabled: bool

    def as_dict(self) -> dict:
        """展开为普通字典（日志与诊断用）。"""

        return {
            "session_ttl_seconds": self.session_ttl_seconds,
            "max_login_attempts": self.max_login_attempts,
            "lockout_window_seconds": self.lockout_window_seconds,
            "audit_enabled": self.audit_enabled,
        }


def load_settings() -> Settings:
    """从环境变量读取配置；未设置时使用默认值。"""

    return Settings(
        session_ttl_seconds=int(
            os.environ.get("AUTH_SESSION_TTL", DEFAULT_SESSION_TTL_SECONDS)
        ),
        max_login_attempts=int(
            os.environ.get("AUTH_MAX_LOGIN_ATTEMPTS", DEFAULT_MAX_LOGIN_ATTEMPTS)
        ),
        lockout_window_seconds=int(
            os.environ.get("AUTH_LOCKOUT_WINDOW", DEFAULT_LOCKOUT_WINDOW_SECONDS)
        ),
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
    "src/core/errors.py": '''"""领域异常层级。

约定：异常只承载语义，不承载 HTTP 状态码；映射在 src/api/errors.py。
新增异常类型时必须同步补齐映射，否则会落到 500 兜底分支。
"""


class DomainError(Exception):
    """业务异常基类。"""


class NotFoundError(DomainError):
    """资源不存在。"""


class PermissionDeniedError(DomainError):
    """权限不足。"""


class ValidationError(DomainError):
    """请求参数非法。"""


class ConflictError(DomainError):
    """资源状态冲突（重复创建、并发修改等）。"""
''',
    "src/core/logging.py": '''"""统一日志与审计通道。

审计走独立的 logger（evolyn.audit），便于日志采集侧按名字分流；事件体
统一序列化为单行 JSON，禁止多行输出（采集侧按行切分）。
"""

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


def audit_channel_active() -> bool:
    """审计通道是否已启用（测试辅助）。"""

    return _AUDIT_LOGGER.isEnabledFor(logging.INFO)
''',
    "src/services/__init__.py": '"""业务服务包。"""\n',
    "src/services/audit_service.py": '''"""审计事件记录。

事件先进内存缓冲，再视配置写入日志通道；缓冲不落盘，仅用于当次进程的
管理查询（/admin/audit）。
"""

from src.config.settings import load_settings
from src.core.logging import audit_log

_EVENTS: list[dict] = []
_BUFFER_LIMIT = 1000


def record(event: dict) -> None:
    """记录审计事件；按配置决定是否写入日志通道。"""

    _EVENTS.append(event)
    if len(_EVENTS) > _BUFFER_LIMIT:
        del _EVENTS[: len(_EVENTS) - _BUFFER_LIMIT]
    if load_settings().audit_enabled:
        audit_log(event)


def recent(limit: int = 20) -> list[dict]:
    """最近的事件（最多 limit 条）。"""

    return _EVENTS[-limit:]


def by_event_name(name: str, limit: int = 20) -> list[dict]:
    """按事件名过滤最近事件（排查用）。"""

    matches = [event for event in _EVENTS if event.get("event") == name]
    return matches[-limit:]


def count() -> int:
    """当前缓冲中的事件总数。"""

    return len(_EVENTS)
''',
    "src/services/user_service.py": '''"""用户读写服务（进程内存储+初始化 schema）。

用户在内存字典里维护，`_bootstrap` 只负责让内存 schema 可执行（迁移到
真实存储前的过渡）。写操作的审计事件统一经 audit_service 记录。
"""

from src.core.errors import NotFoundError
from src.storage import db
from src.storage.models import User

_BOOTSTRAPPED = False
_USERS: dict[str, User] = {}
_DEFAULT_ROLE = "user"


def _bootstrap() -> None:
    """首次使用时初始化表结构。"""

    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    executor = db.QueryExecutor(db.connect(":memory:"))
    for statement in db.SCHEMA_STATEMENTS:
        executor.execute(statement)
    _BOOTSTRAPPED = True


def _users_by_id() -> dict[str, User]:
    """按 ID 建索引（查找是 O(n)，用户量大后再做反向索引）。"""

    return {user.id: user for user in _USERS.values()}


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
    "src/storage/db.py": '''"""SQLite 连接与查询执行器。

本地开发用内存库；测试不落盘。生产接入前先看 runbook 的容量评估——
当前连接没有池化，每次 connect 都是新连接。
"""

import sqlite3

SCHEMA_STATEMENTS: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, username TEXT UNIQUE)",
    "CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, user_id TEXT)",
)


def connect(dsn: str) -> sqlite3.Connection:
    """打开连接；内存库用于本地开发。"""

    return sqlite3.connect(dsn)


def connect_readonly(dsn: str) -> sqlite3.Connection:
    """只读连接（导出与抽查用），写操作会直接报错。"""

    return sqlite3.connect(f"file:{dsn}?mode=ro", uri=True)


class QueryExecutor:
    """执行语句并返回结果行。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def execute(self, sql: str, params: tuple = ()) -> list[tuple]:
        cursor = self._connection.execute(sql, params)
        self._connection.commit()
        return cursor.fetchall()

    def execute_many(self, statements: list[str]) -> int:
        """按顺序执行多条语句，返回执行条数。"""

        for statement in statements:
            self._connection.execute(statement)
        self._connection.commit()
        return len(statements)
''',
    "src/storage/models.py": '''"""持久化模型。

模型是纯数据类，不带行为；后续接入 ORM 时保持字段名不变，便于迁移脚本
对照。所有时间字段约定为 Unix 时间戳（float）。
"""

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


@dataclass
class SessionRecord:
    """会话落库后的形状（当前未启用，见 changelog）。"""

    session_id: str
    user_id: str
    expires_at: float
''',
    "src/api/__init__.py": '"""HTTP 接口包。"""\n',
    "src/api/errors.py": '''"""Domain 异常到 HTTP 响应的映射。

映射按异常类型做全量判断；未知 DomainError 一律 500 兜底。错误体保持
最小形状：{"error": "<code>"}，避免内部信息泄漏。
"""

from collections.abc import Callable
from functools import wraps

from src.core.errors import (
    DomainError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)

_STATUS_BY_EXCEPTION: tuple[tuple[type[DomainError], int, str], ...] = (
    (NotFoundError, 404, "not found"),
    (PermissionDeniedError, 403, "forbidden"),
    (ValidationError, 400, "invalid request"),
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
    "src/api/middleware.py": '''"""请求级认证中间件。

会话 ID 从 X-Session-Id 头读取；这里走完整的 validate_session（含过期
检查），与 handle_profile 的直读形成对照。
"""

from src.auth.session import Session, SessionStore, validate_session

SESSION_HEADER = "X-Session-Id"


def extract_session_id(request) -> str:
    """从请求头提取会话 ID；缺失返回空串。"""

    headers = getattr(request, "headers", None) or {}
    return str(headers.get(SESSION_HEADER, "")).strip()


def auth_middleware(request, store: SessionStore) -> Session | None:
    """从 X-Session-Id 校验会话；失败返回 None。"""

    session_id = extract_session_id(request)
    if not session_id:
        return None
    return validate_session(session_id, store)
''',
    "src/api/routes.py": '''"""HTTP 路由处理函数。

处理函数只做参数提取与响应拼装；权限与业务规则下沉到 auth/services。
注意 handle_profile 的历史实现：直接调用 SessionStore.get_session，绕过
了过期校验（见 T-101 修复单）。
"""

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
def handle_logout(request, store: SessionStore):
    """POST /logout：删除会话（幂等）。"""

    store.delete(request.headers.get("X-Session-Id", ""))
    return 200, {"ok": True}


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
    "src/api/app.py": '''"""最小应用装配：路由表与分发。

路由表是 (method, path) → handler 的平铺字典；路径参数暂不支持（需要时
先扩 dispatch）。/admin/* 的认证在 dispatch 里统一处理。
"""

from src.api import middleware, routes
from src.auth.session import SessionStore

ROUTES = {
    ("POST", "/login"): routes.handle_login,
    ("POST", "/logout"): routes.handle_logout,
    ("GET", "/profile"): routes.handle_profile,
    ("GET", "/export"): routes.handle_user_export,
    ("GET", "/admin/users"): routes.handle_admin_users,
    ("GET", "/admin/audit"): routes.handle_admin_audit,
}

_AUTHENTICATED_PREFIXES = ("/admin/",)
_HEALTH_PATHS = ("/healthz", "/readyz")


def dispatch(method: str, path: str, request, store: SessionStore):
    """按方法+路径分发；/admin/* 先过认证中间件。"""

    if path in _HEALTH_PATHS:
        return 200, {"status": "ok"}
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

from src.auth.login import failure_count, login
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


def test_login_records_failure() -> None:
    _make_user("carol")
    store = SessionStore()
    login("carol", "wrong", store)
    assert failure_count("carol") >= 1
''',
    "tests/test_session.py": '''"""会话创建与校验测试。"""

from src.auth.session import Session, SessionStore, create_session, validate_session


def test_create_session() -> None:
    store = SessionStore()
    session = create_session("user-1", store)
    assert session.session_id
    assert validate_session(session.session_id, store) is not None


def test_validate_unknown_session() -> None:
    store = SessionStore()
    assert validate_session("missing", store) is None


def test_purge_expired_removes_stale_sessions() -> None:
    store = SessionStore()
    store.save(Session(session_id="old", user_id="user-2", expires_at=0.0))
    assert store.purge_expired() == 1
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

_GENERATED_SERVICE_STORE = '''"""%(zh)s模块业务逻辑（进程内存储，持久化迁移中）。

%(desc)s。当前版本把记录保存在进程内字典里，重启即丢；持久化迁移由
platform 任务跟进，接口形状保持不变。
"""

from __future__ import annotations

import logging

from src.core.errors import NotFoundError, ValidationError

logger = logging.getLogger("evolyn.modules.%(name)s")

_RECORDS: dict[str, dict] = {}
_STORE_LIMIT = 500
_DEFAULT_PAGE_SIZE = 20
_MAX_PAGE_SIZE = 50
_ALLOWED_SOURCES: tuple[str, ...] = ("web", "app", "partner")

# 字段约定（字段名, 类型提示, 是否必填）：迁移到 ORM 时按此生成迁移脚本。
_FIELD_SPECS: tuple[tuple[str, str, bool], ...] = (
    ("id", "str", True),
    ("source", "str", False),
    ("note", "str", False),
)


def _require_id(value: object) -> str:
    """校验并规整 ID：空值与超长值直接拒绝，避免脏键进入存储。"""

    text = str(value or "").strip()
    if not text:
        raise ValidationError("id is required")
    if len(text) > 64:
        raise ValidationError("id is too long")
    return text


def _normalize_source(payload: dict) -> str:
    """把来源渠道归一到白名单；未知渠道直接拒绝。"""

    source = str(payload.get("source") or "web").strip().lower()
    if source not in _ALLOWED_SOURCES:
        raise ValidationError("unknown source: " + source)
    return source


def _snapshot(key: str) -> dict:
    """读取记录快照副本，避免调用方原地修改内部存储。"""

    return dict(_RECORDS.get(key) or {})


def _coerce_int(value: object, default: int) -> int:
    """把请求里的数字参数规整成 int；非法值回落默认值。"""

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _audit(action: str, key: str) -> None:
    """记录轻量审计日志（正式审计走 audit 模块）。"""

    logger.info("event=%%s key=%%s", action, key)


def %(fn)s(payload: dict) -> tuple[int, dict]:
    """%(desc)s；返回 (status, body) 二元组。"""

    key = _require_id(payload.get("id"))
    if len(_RECORDS) >= _STORE_LIMIT:
        raise ValidationError("store limit reached")
    record = dict(payload)
    record["source"] = _normalize_source(payload)
    _RECORDS[key] = record
    _audit("save", key)
    logger.info("%(name)s.%(fn)s: saved %%s", key)
    return 200, {"id": key, "saved": True}


def get_%(name)s_record(payload: dict) -> tuple[int, dict]:
    """按 ID 读取单条记录；缺失时抛出领域 NotFoundError。"""

    key = _require_id(payload.get("id"))
    record = _snapshot(key)
    if not record:
        raise NotFoundError("%(name)s record not found")
    return 200, {"record": record}


def list_%(name)s_records(payload: dict) -> tuple[int, dict]:
    """按录入顺序分页列出记录（默认 20 条/页，上限 50）。"""

    page = max(1, _coerce_int(payload.get("page"), 1))
    size = _coerce_int(payload.get("page_size"), _DEFAULT_PAGE_SIZE)
    size = max(1, min(size, _MAX_PAGE_SIZE))
    start = (page - 1) * size
    items = [_snapshot(key) for key in list(_RECORDS)[start : start + size]]
    return 200, {"items": items, "page": page, "page_size": size}


def remove_%(name)s_record(payload: dict) -> tuple[int, dict]:
    """软删记录：先标记 removed，再由离线任务物理清理。"""

    key = _require_id(payload.get("id"))
    record = _RECORDS.get(key)
    if record is None:
        raise NotFoundError("%(name)s record not found")
    record["removed"] = True
    _audit("remove", key)
    return 200, {"id": key, "removed": True}
'''

_GENERATED_SERVICE_READ = '''"""%(zh)s模块业务逻辑（只读查询，数据源迁移中）。

%(desc)s。当前从进程内快照读取；数据接入数据仓库后替换 `_load_items`
的实现即可，页面结构不变。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("evolyn.modules.%(name)s")

_RECORDS: dict[str, dict] = {}
_DEFAULT_PAGE_SIZE = 20
_MAX_PAGE_SIZE = 100
_SORT_KEYS: tuple[str, ...] = ("id", "updated_at", "created_at")


def _pagination(payload: dict) -> tuple[int, int]:
    """解析分页参数并收缩到安全范围。"""

    page = max(1, int(payload.get("page") or 1))
    size = int(payload.get("page_size") or _DEFAULT_PAGE_SIZE)
    size = max(1, min(size, _MAX_PAGE_SIZE))
    return page, size


def _load_items() -> list[dict]:
    """读取当前可见条目（后续替换为数据仓库查询）。"""

    return [dict(item) for item in _RECORDS.values()]


def _matches(item: dict, filters: dict) -> bool:
    """按等值条件过滤条目；未提供的字段不参与比较。"""

    for field, expected in filters.items():
        if str(item.get(field, "")) != str(expected):
            return False
    return True


def _sorted_items(items: list[dict], sort_by: str) -> list[dict]:
    """按白名单字段排序；未登记的排序键回退到 id。"""

    if sort_by not in _SORT_KEYS:
        sort_by = "id"
    return sorted(items, key=lambda item: str(item.get(sort_by, "")))


def %(fn)s(payload: dict) -> tuple[int, dict]:
    """%(desc)s；返回 (status, body) 二元组。"""

    page, size = _pagination(payload)
    filters = payload.get("filters")
    if not isinstance(filters, dict):
        filters = {}
    items = [item for item in _load_items() if _matches(item, filters)]
    items = _sorted_items(items, str(payload.get("sort_by") or "id"))
    start = (page - 1) * size
    window = items[start : start + size]
    logger.info("%(name)s.%(fn)s: page %%s size %%s", page, size)
    return 200, {
        "items": window,
        "page": page,
        "page_size": size,
        "total": len(items),
    }


def count_%(name)s_records(payload: dict) -> tuple[int, dict]:
    """统计条目总数（支持与列表相同的 filters）。"""

    filters = payload.get("filters")
    if not isinstance(filters, dict):
        filters = {}
    total = len([item for item in _load_items() if _matches(item, filters)])
    return 200, {"total": total}
'''

_GENERATED_HANDLER = '''"""%(zh)s模块请求处理函数（%(desc)s）。

请求边界只做三件事：取载荷、最小校验、调用 service；业务规则一律不要
写在这一层（见 onboarding 的开发约定）。
"""

from __future__ import annotations

import logging

from src.api.errors import wrap_domain_errors
from src.modules.%(name)s.service import %(fn)s

logger = logging.getLogger("evolyn.modules.%(name)s.handler")

_REQUIRED_FIELDS: tuple[str, ...] = ("id",)


def _missing_fields(payload: dict) -> tuple[str, ...]:
    """列出缺失的必填字段（仅做存在性检查，不校验类型）。"""

    return tuple(
        field for field in _REQUIRED_FIELDS if not str(payload.get(field, "")).strip()
    )


def parse_%(name)s_body(request) -> dict:
    """提取请求载荷：优先请求体，兼容部分网关回落到查询参数。"""

    body = getattr(request, "body", None)
    if isinstance(body, dict) and body:
        return dict(body)
    query = getattr(request, "query", None) or {}
    if isinstance(query, dict):
        return dict(query)
    return {}


@wrap_domain_errors
def %(handler)s(request):
    """%(desc)s；异常交给错误映射层统一转 HTTP 错误。"""

    payload = parse_%(name)s_body(request)
    if not payload:
        return 400, {"error": "empty payload"}
    missing = _missing_fields(payload)
    if missing:
        logger.debug("%(name)s payload missing fields: %%s", ",".join(missing))
    logger.info("%(name)s request accepted")
    return %(fn)s(payload)
'''

_GENERATED_MODULE_INIT = '''"""%(zh)s模块。

对外只暴露请求处理入口；服务层实现不要跨模块 import。模块内文件职责：

- ``handler.py``：请求边界（载荷提取与最小校验）；
- ``service.py``：业务逻辑（当前为进程内实现，持久化迁移中）。
"""

from src.modules.%(name)s.handler import %(handler)s, parse_%(name)s_body

MODULE_NAME = "%(name)s"
MODULE_TITLE = "%(zh)s"
MODULE_DESCRIPTION = "%(desc)s"

__all__ = [
    "MODULE_NAME",
    "MODULE_TITLE",
    "MODULE_DESCRIPTION",
    "%(handler)s",
    "parse_%(name)s_body",
]
'''

_GENERATED_VALIDATORS = '''"""%(zh)s模块请求校验（%(desc)s）。

校验与业务规则的边界：这里只做形状与取值范围检查；需要查库或查状态的
规则放 service 层。错误信息保持「字段名 + 期望形状」的格式，便于
``src/api/errors.py`` 的映射层统一处理。
"""

from __future__ import annotations

import logging

from src.core.errors import ValidationError

logger = logging.getLogger("evolyn.modules.%(name)s.validators")

_ID_MAX_LEN = 64
_NOTE_MAX_LEN = 500

# 可疑字符仅记录日志，不拦截（清洗由上游网关负责）。
_SUSPICIOUS_CHARS: tuple[str, ...] = ("<", ">", "&")

_REQUIRED_FIELDS: tuple[str, ...] = ("id",)


def _as_mapping(payload: object) -> dict:
    """把载荷规整为 dict；非 mapping 直接拒绝。"""

    if isinstance(payload, dict):
        return dict(payload)
    raise ValidationError("payload must be a json object")


def check_required(payload: dict) -> dict:
    """检查必填字段是否齐全（空字符串按缺失处理）。"""

    mapping = _as_mapping(payload)
    missing = [
        field
        for field in _REQUIRED_FIELDS
        if not str(mapping.get(field, "")).strip()
    ]
    if missing:
        raise ValidationError("missing fields: " + ", ".join(missing))
    return mapping


def check_lengths(payload: dict) -> dict:
    """检查字段长度上限（超长直接拒绝，避免存储被撑爆）。"""

    mapping = _as_mapping(payload)
    if len(str(mapping.get("id", ""))) > _ID_MAX_LEN:
        raise ValidationError("id is too long")
    if len(str(mapping.get("note", ""))) > _NOTE_MAX_LEN:
        raise ValidationError("note is too long")
    return mapping


def scan_suspicious(payload: dict) -> dict:
    """扫描可疑字符；只记日志，不改变载荷。"""

    text = str(payload.get("note", ""))
    for char in _SUSPICIOUS_CHARS:
        if char in text:
            logger.warning("suspicious char in note: %%r", char)
    return payload


def validate_%(name)s_payload(payload: dict) -> dict:
    """串起全部校验步骤；任一步失败即抛领域异常。"""

    mapping = check_required(payload)
    check_lengths(mapping)
    scan_suspicious(mapping)
    return mapping
'''

_CHECKOUT_SERVICE = '''"""结算模块业务逻辑：算税与扣款编排。

结算确认把校验、算税、扣款串成一条链：任何一步失败都直接冒出领域异常，
由 handler 的 @wrap_domain_errors 统一映射为 HTTP 错误后再返回。
"""

from __future__ import annotations

import logging

from src.config.feature_flags import flag_enabled
from src.config.limits import MAX_ORDER_ITEMS
from src.core.errors import ValidationError
from src.integrations.payments import charge_card
from src.integrations.tax import calculate_tax_for_order

logger = logging.getLogger("evolyn.modules.checkout")

_RECEIPTS: dict[str, dict] = {}


def _order_items(payload: dict) -> list:
    """提取条目列表并做基础形状校验。"""

    items = payload.get("items", [])
    if not isinstance(items, list):
        raise ValidationError("items must be a list")
    return items


def _require_amount(payload: dict) -> int:
    """解析订单金额（分）；非正数直接拒绝。"""

    amount = int(payload.get("amount_cents", 0))
    if amount <= 0:
        raise ValidationError("amount_cents must be positive")
    return amount


def preview_tax(payload: dict) -> tuple[int, dict]:
    """只算税不扣款（下单页预览用）。"""

    amount_cents = _require_amount(payload)
    return 200, {"tax_cents": calculate_tax_for_order(amount_cents)}


def confirm_order(payload: dict) -> tuple[int, dict]:
    """校验订单并依次算税、扣款；新结算开关关闭时直接拒绝。"""

    if not flag_enabled("ENABLE_NEW_CHECKOUT"):
        logger.info("checkout disabled by flag")
        return 503, {"error": "checkout disabled"}
    items = _order_items(payload)
    if not items or len(items) > MAX_ORDER_ITEMS:
        raise ValidationError("invalid item count")
    amount_cents = _require_amount(payload)
    tax_cents = calculate_tax_for_order(amount_cents)
    receipt = charge_card(
        str(payload.get("order_id", "")),
        amount_cents + tax_cents,
    )
    _RECEIPTS[str(payload.get("order_id", ""))] = dict(receipt)
    logger.info("checkout confirmed for %s", payload.get("order_id", ""))
    return 200, {"receipt": receipt, "tax_cents": tax_cents}


def get_receipt(payload: dict) -> tuple[int, dict]:
    """回读最近的扣款回执（对账排查用）。"""

    order_id = str(payload.get("order_id", ""))
    return 200, {"receipt": _RECEIPTS.get(order_id, {})}
'''

_REFUNDS_SERVICE = '''"""退款模块业务逻辑。

退款窗口以订单下单时间为基准：超窗直接拒绝，订单不存在给出 404 语义的
领域异常；审核通过后由结算侧发起原路退回（本期先登记状态）。
"""

from __future__ import annotations

import logging

from src.config.limits import MAX_REFUND_DAYS
from src.core.errors import NotFoundError, ValidationError

logger = logging.getLogger("evolyn.modules.refunds")

_ORDERS: dict[str, dict] = {}
_REFUNDS: dict[str, dict] = {}
_REFUND_STATES: tuple[str, ...] = ("requested", "approved", "rejected", "settled")


def _order_or_raise(order_id: str) -> dict:
    """读取订单；缺失时抛出领域 NotFoundError。"""

    order = _ORDERS.get(order_id)
    if order is None:
        raise NotFoundError("order " + order_id + " not found")
    return order


def refund_window_left(payload: dict) -> tuple[int, dict]:
    """查询订单剩余可退天数（负数表示已超窗）。"""

    order_id = str(payload.get("order_id", ""))
    _order_or_raise(order_id)
    days = int(payload.get("days_since_purchase", 0))
    return 200, {"days_left": MAX_REFUND_DAYS - days}


def request_refund(payload: dict) -> tuple[int, dict]:
    """校验订单存在且未超期，然后登记退款。"""

    order_id = str(payload.get("order_id", ""))
    if order_id not in _ORDERS:
        raise NotFoundError(f"order {order_id} not found")
    days = int(payload.get("days_since_purchase", 0))
    if days > MAX_REFUND_DAYS:
        raise ValidationError("refund window expired")
    _REFUNDS[order_id] = {"order_id": order_id, "state": "requested"}
    logger.info("refund requested for %s", order_id)
    return 200, {"order_id": order_id, "status": "refund_requested"}
'''

_REVIEWS_SERVICE = '''"""评价模块业务逻辑。

评价发布前经过一层内容筛查；带图评价受 V2 开关控制（关闭时只允许纯文字，
避免旧版审核通道过载）。
"""

from __future__ import annotations

import logging

from src.config.feature_flags import flag_enabled
from src.core.errors import ValidationError

logger = logging.getLogger("evolyn.modules.reviews")

_REVIEWS: dict[str, dict] = {}
_BLOCKED_WORDS: tuple[str, ...] = ("spam", "scam", "advertisement")
_MAX_TEXT_LEN = 2000


def _screen_text(text: str) -> str:
    """基础内容筛查：命中屏蔽词与超长文本直接拒绝。"""

    normalized = str(text or "").strip()
    if len(normalized) > _MAX_TEXT_LEN:
        raise ValidationError("review text is too long")
    lowered = normalized.lower()
    for word in _BLOCKED_WORDS:
        if word in lowered:
            raise ValidationError("review text blocked")
    return normalized


def hide_review(payload: dict) -> tuple[int, dict]:
    """下架评价（运营后台调用；仅改状态不删除原始记录）。"""

    review_id = str(payload.get("id", ""))
    record = _REVIEWS.get(review_id)
    if record is None:
        raise ValidationError("review not found")
    record["hidden"] = True
    return 200, {"id": review_id, "hidden": True}


def publish_review(payload: dict) -> tuple[int, dict]:
    """发布评价；V2 开关关闭时拒绝带图评价。"""

    if payload.get("has_image") and not flag_enabled("ENABLE_REVIEWS_V2"):
        raise ValidationError("images disabled")
    record = dict(payload)
    record["text"] = _screen_text(payload.get("text", ""))
    _REVIEWS[str(payload.get("id", ""))] = record
    logger.info("review published: %s", payload.get("id", ""))
    return 200, {"saved": True}
'''

_NOTIFICATIONS_SERVICE = '''"""通知模块业务逻辑。

通知按渠道路由到集成适配层：邮件与短信直接发送，站内聊天通知受开关
控制（灰度中）。发送结果只回执状态，不保存正文。
"""

from __future__ import annotations

import logging

from src.config.feature_flags import flag_enabled
from src.integrations import email, sms

logger = logging.getLogger("evolyn.modules.notifications")

_CHANNELS: tuple[str, ...] = ("email", "sms", "chat")
_MAX_TEXT_LEN = 500


def _channel_of(payload: dict) -> str:
    """解析并校验渠道参数。"""

    channel = str(payload.get("channel") or "email").strip().lower()
    if channel not in _CHANNELS:
        raise ValueError("unknown channel: " + channel)
    return channel


def _message_text(payload: dict) -> str:
    """提取通知正文并做长度保护。"""

    text = str(payload.get("text", ""))
    if len(text) > _MAX_TEXT_LEN:
        raise ValueError("text is too long")
    return text


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
    logger.info("notification dispatched: %s", channel)
    return 200, {"channel": channel, "sent": True}


def broadcast(payload: dict) -> tuple[int, dict]:
    """向一组收件人复制发送（运营公告用）。"""

    channel = _channel_of(payload)
    text = _message_text(payload)
    recipients = [str(item) for item in payload.get("recipients", [])]
    for recipient in recipients:
        if channel == "sms":
            sms.send(recipient, text)
        else:
            email.send(recipient, text)
    return 200, {"channel": channel, "sent": len(recipients)}
'''

_AUDIT_SERVICE = '''"""审计模块业务逻辑。

审计事件读取与写入分离：读取走本模块的查询接口，写入仍走 v1 通道
（`record_audit_v1`），待事件总线迁移完成后再切换。
"""

from __future__ import annotations

import hashlib
import logging

from src.legacy.audit_v1 import record_audit_v1

logger = logging.getLogger("evolyn.modules.audit")

_MAX_PAGE_SIZE = 200


def _fingerprint(event: dict) -> str:
    """为事件生成稳定指纹（去重与回溯定位用）。"""

    payload = repr(sorted(event.items()))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _page_size(payload: dict) -> int:
    """解析分页大小并收缩到安全范围。"""

    size = int(payload.get("page_size") or 50)
    return max(1, min(size, _MAX_PAGE_SIZE))


def list_audit_events(payload: dict) -> tuple[int, dict]:
    """列出审计事件；旁路写入仍走 v1 通道（迁移中）。"""

    record_audit_v1({"action": "audit_list", "actor": payload.get("actor", "")})
    size = _page_size(payload)
    logger.info("audit list requested, page_size=%s", size)
    return 200, {"events": []}


def describe_event(payload: dict) -> tuple[int, dict]:
    """返回事件指纹（排查重复事件用）。"""

    event = {"actor": payload.get("actor", ""), "action": payload.get("action", "")}
    return 200, {"fingerprint": _fingerprint(event)}
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
    "src/integrations/payments.py": '''"""支付渠道适配。

对上游结算模块暴露稳定接口 `charge_card`；网关地址等旧配置仍从 legacy
v1 读取（迁移清单 P-102）。夹具不真正发起网络请求，只回执请求形状。
"""

from __future__ import annotations

import logging
import os

from src.legacy.config_v1 import load_legacy_config

logger = logging.getLogger("evolyn.integrations.payments")

_API_KEY = os.environ.get("PAYMENTS_API_KEY", "")
_LEGACY = load_legacy_config()

_RETRYABLE_CODES: tuple[str, ...] = ("timeout", "gateway_busy", "rate_limited")
_RECEIPTS: dict[str, dict] = {}


def _gateway_name() -> str:
    """网关名从 legacy 配置读取，缺省回落到 sandbox。"""

    return str(_LEGACY.get("base_url") or "sandbox")


def _retryable(error_code: str) -> bool:
    """判断错误码是否值得重试（网络抖动类）。"""

    return error_code in _RETRYABLE_CODES


def charge_card(order_id: str, amount_cents: int) -> dict:
    """向支付网关发起扣款（夹具不真正发请求）。"""

    receipt = {
        "order_id": order_id,
        "amount_cents": amount_cents,
        "gateway": _LEGACY.get("base_url", "sandbox"),
        "has_key": bool(_API_KEY),
    }
    _RECEIPTS[order_id] = receipt
    logger.info("charge_card issued for %s", order_id)
    return receipt


def refund_charge(order_id: str, amount_cents: int) -> dict:
    """发起原路退回（退款审核通过后调用）。"""

    known = order_id in _RECEIPTS
    return {
        "order_id": order_id,
        "amount_cents": amount_cents,
        "gateway": _gateway_name(),
        "original_known": known,
    }
''',
    "src/integrations/tax.py": '''"""税费计算适配。

税率是部署级配置：`TAX_RATE_PERCENT` 以百分比形式注入（如 "13"）。
V2 开关控制取整策略：开启后四舍五入到分，关闭时截断（历史行为）。
"""

from __future__ import annotations

import logging
import os

from src.config.feature_flags import flag_enabled

logger = logging.getLogger("evolyn.integrations.tax")

_TAX_RATE_PERCENT = float(os.environ.get("TAX_RATE_PERCENT", "0") or 0)
_MAX_COMBINED_RATE = 30.0


def _rate() -> float:
    """读取税率并收缩到安全范围（负值按 0 处理）。"""

    if _TAX_RATE_PERCENT < 0:
        return 0.0
    return min(_TAX_RATE_PERCENT, _MAX_COMBINED_RATE)


def tax_breakdown(amount_cents: int) -> dict:
    """返回税额明细（对账与客服查询用）。"""

    tax_cents = calculate_tax_for_order(amount_cents)
    return {
        "amount_cents": amount_cents,
        "tax_cents": tax_cents,
        "rate_percent": _rate(),
    }


def calculate_tax_for_order(amount_cents: int) -> int:
    """按配置税率计算税额；V2 开关启用时使用四舍五入。"""

    if amount_cents <= 0:
        return 0
    raw = amount_cents * _TAX_RATE_PERCENT / 100
    if flag_enabled("ENABLE_TAX_V2"):
        return round(raw)
    return int(raw)
''',
    "src/integrations/email.py": '''"""邮件通道适配。

夹具不真正发请求：只回执收件人与字数，便于主流程单测。
生产环境由 EMAIL_RELAY_URL 指向真实中继。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("evolyn.integrations.email")

_RELAY_URL = os.environ.get("EMAIL_RELAY_URL", "")
_MAX_BULK = 200
_TEMPLATES: dict[str, str] = {
    "welcome": "欢迎加入，{name}",
    "refund": "退款处理中：{order_id}",
    "shipping": "包裹已发出：{tracking}",
}


def render_template(name: str, values: dict) -> str:
    """渲染内置模板；未知模板名回落到纯文本提示。"""

    template = _TEMPLATES.get(name, "{name}")
    text = template
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def send(to: str, text: str) -> dict:
    """发送邮件（夹具不真正发请求）。"""

    logger.info("email queued to %s", to)
    return {"to": to, "chars": len(text), "relay": bool(_RELAY_URL)}


def send_bulk(recipients: list, text: str) -> dict:
    """批量发送（运营公告）；超过单批上限时只处理前 N 个。"""

    batch = [str(item) for item in recipients[:_MAX_BULK]]
    for recipient in batch:
        send(recipient, text)
    return {"queued": len(batch), "skipped": max(0, len(recipients) - _MAX_BULK)}
''',
    "src/integrations/sms.py": '''"""短信通道适配。

生产由 SMS_PROVIDER_URL 指向真实通道；夹具只回执请求形状。
号码统一按 E.164 规整，无法识别的直接拒绝。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("evolyn.integrations.sms")

_PROVIDER_URL = os.environ.get("SMS_PROVIDER_URL", "")
_COUNTRY_PREFIX = "+86"
_MAX_LEN = 480


def normalize_phone(raw: str) -> str:
    """把号码规整为 E.164；缺国家码时补默认前缀。"""

    digits = "".join(ch for ch in str(raw or "") if ch.isdigit() or ch == "+")
    if not digits:
        raise ValueError("phone is empty")
    if not digits.startswith("+"):
        digits = _COUNTRY_PREFIX + digits
    return digits


def send(to: str, text: str) -> dict:
    """发送短信（夹具不真正发请求）。"""

    if len(text) > _MAX_LEN:
        raise ValueError("sms text is too long")
    normalized = normalize_phone(to)
    return {"to": normalized, "chars": len(text), "provider": bool(_PROVIDER_URL)}
''',
    "src/integrations/analytics.py": '''"""分析事件写入（导入期建立连接）。

注意：连接在模块导入时就建立，测试与本地脚本引入本模块会连带打开
analytics.db；这是既有的偷懒写法，已在重构清单（REF-31）中登记。
"""

from __future__ import annotations

import logging
import os

from src.storage.db import connect

logger = logging.getLogger("evolyn.integrations.analytics")

_CONNECTION = connect("analytics.db")
_WRITE_KEY = os.environ.get("ANALYTICS_WRITE_KEY", "")

_MAX_BATCH = 100


def _redact(payload: dict) -> dict:
    """去除事件里可能携带的个人信息字段。"""

    redacted = dict(payload)
    for key in ("email", "phone", "address"):
        redacted.pop(key, None)
    return redacted


def track(name: str, payload: dict) -> None:
    """记录分析事件。"""

    _CONNECTION.execute("SELECT 1")
    logger.info("analytics event: %s", name)


def track_batch(events: list) -> int:
    """批量写入事件，返回写入条数（夹具中只计数）。"""

    count = 0
    for event in events[:_MAX_BATCH]:
        if isinstance(event, dict):
            track(str(event.get("name", "unknown")), _redact(event))
            count += 1
    return count
''',
    "src/integrations/storage.py": '''"""对象存储适配（导入期建立连接）。

连接同样在导入时建立（见 REF-31）；对象路径统一为 bucket/key。
"""

from __future__ import annotations

import logging

from src.storage.db import connect

logger = logging.getLogger("evolyn.integrations.storage")

_CONNECTION = connect("objects.db")

_ALLOWED_BUCKETS: tuple[str, ...] = ("exports", "attachments", "invoices")


def _check_bucket(bucket: str) -> str:
    """校验桶名在白名单内。"""

    if bucket not in _ALLOWED_BUCKETS:
        raise ValueError("unknown bucket: " + bucket)
    return bucket


def put_object(bucket: str, key: str) -> str:
    """登记对象路径。"""

    _CONNECTION.execute("SELECT 1")
    return _check_bucket(bucket) + "/" + key


def get_object_url(bucket: str, key: str) -> str:
    """生成对象的只读访问地址（有效期由网关控制）。"""

    return "https://objects.local/" + _check_bucket(bucket) + "/" + key
''',
    "src/integrations/search.py": '''"""搜索索引适配（导入期建立连接）。

索引写入是增量式的；全量重建由离线任务负责（见 ops 手册）。
"""

from __future__ import annotations

import logging

from src.storage.db import connect

logger = logging.getLogger("evolyn.integrations.search")

_CONNECTION = connect("search_index.db")

_MAX_TEXT_LEN = 10000


def _normalize(doc_id: str, text: str) -> tuple[str, str]:
    """规整文档 ID 与正文长度。"""

    clean_id = str(doc_id or "").strip()
    if not clean_id:
        raise ValueError("doc_id is empty")
    return clean_id, str(text or "")[:_MAX_TEXT_LEN]


def index_document(doc_id: str, text: str) -> None:
    """写入索引；重建由离线任务负责。"""

    clean_id, clean_text = _normalize(doc_id, text)
    _CONNECTION.execute("SELECT 1")
    logger.info("indexed document: %s (%s chars)", clean_id, len(clean_text))


def reindex_prefix(prefix: str) -> int:
    """按前缀触发增量重建（迁移脚本调用），夹具中只记日志。"""

    logger.info("reindex requested for prefix=%s", prefix)
    return 0
''',
    "src/integrations/shipping_carrier.py": '''"""承运商适配。

运费按重量阶梯估算（分）；真实报价接口在网关侧，夹具只给本地估算。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("evolyn.integrations.shipping_carrier")

_CARRIER_CODE = os.environ.get("SHIPPING_CARRIER_CODE", "demo")

_BASE_FEE_CENTS = 500
_MAX_DAYS = 7


def quote_shipping_weight(weight_grams: int) -> int:
    """按重量估算运费（分）。"""

    return max(_BASE_FEE_CENTS, weight_grams // 10)


def estimate_days(weight_grams: int) -> int:
    """估算送达天数（越重越慢，封顶）。"""

    days = 1 + weight_grams // 5000
    return min(days, _MAX_DAYS)


def carrier_code() -> str:
    """当前启用的承运商代码。"""

    return _CARRIER_CODE
''',
}

_LEGACY_FILES: dict[str, str] = {
    "src/legacy/orders_v1.py": '''"""旧版订单金额计算（v1）。

迁移说明：结算模块已接手金额计算，本文件只做归档保留，等待删除审批
（REMOVE-204）。金额单位统一为分；历史版本曾用浮点元，回滚时注意换算。
"""

_CURRENCY_MINOR_UNITS = 100
_LEGACY_TAX_INCLUDED = True
_ROUNDING_POLICY = "half-up"


def calculate_total_v1(items: list[dict]) -> int:
    """Deprecated：旧版合计计算，已被结算模块取代，没有调用方。"""

    total = 0
    for item in items:
        total += int(item.get("price_cents", 0)) * int(item.get("qty", 1))
    return total
''',
    "src/legacy/permissions_v1.py": '''"""旧版权限检查（v1）。

迁移说明：权限矩阵已迁到 `src/auth/permissions.py` 的（role, action）表；
本实现只在灰度回滚期间被开关引用过，当前没有任何调用方。
"""

_LEGACY_ROLES = ("admin", "user", "guest")
_READ_ACTIONS = ("read", "list", "view")


def check_permission_v1(role: str, action: str) -> bool:
    """Deprecated：旧版权限矩阵，已被 src/auth/permissions.py 取代。"""

    return role == "admin" or action == "read"
''',
    "src/legacy/pricing_v1.py": '''"""旧版折扣计算（v1）。

迁移说明：折扣规则迁到促销模块；旧公式只支持整单百分比，没有叠加与
封顶逻辑，因此不再维护。文件保留用于历史订单重算的对照。
"""

_MAX_DISCOUNT_PERCENT = 90.0
_MIN_PRICE_CENTS = 1


def apply_discount_v1(amount_cents: int, percent: float) -> int:
    """Deprecated：旧版折扣逻辑没有调用方，等待删除。"""

    return int(amount_cents * (1 - percent / 100))
''',
    "src/legacy/tokens_v1.py": '''"""旧版令牌（v1）。

迁移说明：令牌签发已迁到 `src/auth/tokens.py`（带签名与过期）；旧版
静态前缀令牌仅用于历史数据抽查，没有任何调用方。
"""

import os

_TOKEN_PREFIX = "v1"
_MAX_SUBJECT_LEN = 64


def issue_token_v1(user_id: str) -> str:
    """Deprecated：旧版静态前缀令牌没有调用方。"""

    prefix = os.environ.get("LEGACY_TOKEN_PREFIX", "")
    return f"v1.{user_id}.{prefix}"
''',
    "src/legacy/audit_v1.py": '''"""旧版审计写入（v1）。

迁移说明：审计事件总线迁移中；读取侧已切到 audit 模块，写入侧仍在复用
本实现（唯一存活引用方：src/modules/audit/service.py）。
"""

_EVENTS: list[dict] = []
_MAX_BUFFER = 1000


def record_audit_v1(event: dict) -> None:
    """Deprecated：仍被审计模块暂时复用，迁移完成后删除。"""

    if len(_EVENTS) >= _MAX_BUFFER:
        _EVENTS.pop(0)
    _EVENTS.append(event)
''',
    "src/legacy/config_v1.py": '''"""旧版配置加载（v1）。

迁移说明：集中配置在 `src/config/settings.py`；本实现读取部署级旧变量，
仍被支付适配复用（load_legacy_config），其余引用已清理。
"""

import os

_DEFAULTS: dict = {
    "base_url": "sandbox",
    "retries": 0,
}


def load_legacy_config() -> dict:
    """Deprecated：仍被支付适配复用；从环境变量读取旧配置。"""

    return {
        "base_url": os.environ.get("LEGACY_BASE_URL", ""),
        "retries": int(os.environ.get("LEGACY_RETRIES", "0") or 0),
    }
''',
}

_EXTRA_CONFIG_FILES: dict[str, str] = {
    "src/config/feature_flags.py": '''"""功能开关：从环境变量按名称读取。

开关值以 `FLAG_<名称>` 注入（"1"/"true"/"yes"/"on" 视为开启，其余关闭）；
这里登记全部合法开关名，读取侧只用 flag_enabled，不允许直接 getenv。
"""

import os

_ENV_PREFIX = "FLAG_"
_TRUTHY = ("1", "true", "yes", "on")

_FLAGS = (
    "ENABLE_NEW_CHECKOUT",
    "ENABLE_TAX_V2",
    "ENABLE_REVIEWS_V2",
    "ENABLE_SUPPORT_CHAT",
    "ENABLE_LEGACY_EXPORT",
)


def flag_table() -> tuple[str, ...]:
    """全部已登记开关名（运营后台展示用）。"""

    return _FLAGS


def flag_enabled(name: str) -> bool:
    """开关仅在显式开启时生效（默认关闭）。"""

    raw = os.environ.get(f"{_ENV_PREFIX}{name}", "0")
    return raw.strip().lower() in _TRUTHY
''',
    "src/config/limits.py": '''"""业务限额与速率限制。

限额是兜底保护（防呆），不是业务规则本身；调整前先看 runbook 的容量
评估章节。单位均为「条 / 次 / 天」。
"""

MAX_ORDER_ITEMS = 50
MAX_REFUND_DAYS = 30
RATE_LIMIT_PER_MINUTE = 120
MAX_PAGE_SIZE = 100
DEFAULT_EXPORT_DAYS = 90
''',
}

_DOC_FILES: dict[str, str] = {
    "docs/env_reference.md": '''# 环境变量参考

以下变量由部署环境注入；本地开发可省略，走默认值。**新增变量前先在此登记**，
否则读取侧会静默拿到空值。

## 认证与安全

| 变量 | 说明 |
| --- | --- |
| AUTH_TOKEN_SECRET | 令牌签名密钥 |
| AUTH_SESSION_TTL | 会话有效期（秒） |
| AUTH_MAX_LOGIN_ATTEMPTS | 登录失败上限 |
| AUTH_LOCKOUT_WINDOW | 锁定时窗（秒） |
| AUTH_AUDIT_ENABLED | 审计通道开关 |

## 支付与税费

| 变量 | 说明 |
| --- | --- |
| PAYMENTS_API_KEY | 支付网关密钥 |
| TAX_RATE_PERCENT | 税率（百分比） |

## 通知与集成

| 变量 | 说明 |
| --- | --- |
| EMAIL_RELAY_URL | 邮件中继地址 |
| SMS_PROVIDER_URL | 短信通道地址 |
| ANALYTICS_WRITE_KEY | 分析写入密钥 |
| SHIPPING_CARRIER_CODE | 承运商代码 |

## 迁移与历史

| 变量 | 说明 |
| --- | --- |
| LEGACY_BASE_URL | 旧版配置地址（迁移中） |
| LEGACY_RETRIES | 旧版重试次数（迁移中） |
| VOICE_ORDERS_ENABLED | 语音下单开关（规划中，未接入） |
| LEGACY_EXPORT_BUCKET | 旧导出桶（已停用，未接入） |

## 注意事项

- 「迁移与历史」一节是历史包袱：部分变量代码尚未接入，登记仅为占位；
- 迁移完成后，相关变量随 legacy 代码一并删除；
- 密钥类变量只由部署平台注入，不要写入仓库或日志。
''',
    "docs/architecture.md": '''# 架构概览

服务按「入口 → 模块 → 适配」三层组织：

- `src/api/`：路由装配与请求中间件（最小实现）；
- `src/auth/`：认证、会话与权限；
- `src/config/`：集中配置、功能开关与限额；
- `src/services/`：用户与审计服务；
- `src/storage/`：SQLite 访问；
- `src/modules/`：电商业务模块（结算、订单、库存等）；
- `src/integrations/`：支付、税务、通知等外部适配；
- `src/legacy/`：迁移中的历史代码，不允许新增引用。

业务模块统一由 `handler.py`（请求边界）与 `service.py`（业务逻辑）组成；
外部依赖一律经 `integrations` 适配层，模块之间不允许 import 实现细节。

## 请求生命周期

1. 路由层解析请求并完成最小校验；
2. handler 调用同模块 service 函数；
3. service 编排本模块逻辑，必要时调用 integrations 适配层；
4. 领域异常由 `wrap_domain_errors` 统一映射为 HTTP 错误。

## 已知技术债

- 部分 integrations 在导入期建立数据库连接（重构清单 REF-31）；
- `src/legacy/` 靠人工约定限制引用，缺乏自动检查；
- 功能开关的取值方式不统一，历史写法散落在各模块。
''',
    "docs/runbook.md": '''# 运维手册（节选）

## 发布前检查

- 运行 `pytest tests/` 并确保全绿；
- 支付与税务适配依赖环境变量，逐项核对 `env_reference.md`；
- `src/legacy/` 在迁移完成前不要删除，也不要新增引用。

## 例行任务

- 搜索索引由离线任务重建（见 search 适配的 reindex_prefix）；
- 分析事件积压时先看 `analytics` 连接的写入延迟；
- 审计事件读取走 audit 模块，写入仍走 v1 通道（迁移中）。

## 故障处理

- 结算被拒且日志出现 "checkout disabled"：检查结算开关是否被误关；
- 短信报 "text is too long"：通知模板超出 480 字符上限；
- 对象存储报 "unknown bucket"：bucket 必须先加入白名单。
''',
    "docs/onboarding.md": '''# 新人上手

## 第一天

1. 阅读 `architecture.md` 了解目录结构；
2. 本地运行 `pytest tests/`，确保环境可用；
3. 从 `src/modules/checkout/` 开始读一个完整模块（handler → service）。

## 开发约定

- 新代码不得直接引用 `src/legacy/`；
- 业务模块沿用 `handler.py` + `service.py` 双层结构；
- 外部依赖只经 `integrations` 适配层；
- 开关与限额分别登记在 `config/feature_flags.py` 与 `config/limits.py`。

## 提交前

- `ruff check` 与 `pytest` 都通过；
- 涉及环境变量的改动同步更新 `env_reference.md`。
''',
    "docs/changelog.md": '''# 变更记录

## 2026-09

- 结算确认链路接入税务与支付适配；
- 退款模块加入超期校验；
- 旧导出通道停用（相关配置见 `env_reference.md`）。

## 2026-08

- 权限 v1 标记弃用，等待迁移到 `src/auth/`；
- 会话服务拆出 `src/services/session.py`；
- 限流常量集中到 `config/limits.py`。

## 2026-07

- 功能开关统一改名为 `flag_enabled`；
- 审计读取侧从 v1 迁到 audit 模块，写入侧暂保留；
- 邮件与短信适配抽出模板渲染。
''',
}

_GENERATED_TEST_FILES: dict[str, str] = {
    "tests/test_accounts.py": '''"""账户模块测试。"""

from src.modules.accounts.service import update_account


def test_update_account_is_importable() -> None:
    assert callable(update_account)


def test_update_account_keeps_id() -> None:
    status, body = update_account({"id": "acc-1", "name": "Ada"})
    assert status == 200
    assert body["id"] == "acc-1"


def test_update_account_requires_id() -> None:
    try:
        update_account({"name": "Ada"})
    except Exception as error:  # noqa: BLE001 - 只断言会抛错
        assert "id" in str(error)
    else:  # pragma: no cover - 走到这里说明校验被改坏
        raise AssertionError("update_account should require an id")
''',
    "tests/test_catalog.py": '''"""商品目录模块测试。"""

from src.modules.catalog.service import list_catalog_items


def test_list_catalog_is_importable() -> None:
    assert callable(list_catalog_items)


def test_list_catalog_returns_page() -> None:
    status, body = list_catalog_items({"page": 1, "page_size": 10})
    assert status == 200
    assert body["page"] == 1
    assert isinstance(body["items"], list)
''',
    "tests/test_checkout.py": '''"""结算模块测试。"""

from src.modules.checkout.service import confirm_order, preview_tax


def test_confirm_order_is_importable() -> None:
    assert callable(confirm_order)


def test_confirm_order_respects_feature_flag() -> None:
    payload = {"order_id": "o-1", "items": [1], "amount_cents": 100}
    status, body = confirm_order(payload)
    assert status in (200, 503)
    assert isinstance(body, dict)


def test_preview_tax_requires_amount() -> None:
    try:
        preview_tax({})
    except Exception as error:  # noqa: BLE001 - 只断言会抛错
        assert "amount" in str(error)
    else:  # pragma: no cover - 走到这里说明校验被改坏
        raise AssertionError("preview_tax should require amount_cents")
''',
    "tests/test_orders.py": '''"""订单模块测试。"""

from src.modules.orders.service import list_user_orders


def test_list_orders_is_importable() -> None:
    assert callable(list_user_orders)


def test_list_orders_accepts_paging() -> None:
    status, body = list_user_orders({"page": 2, "page_size": 5})
    assert status == 200
    assert "items" in body
''',
    "tests/test_refunds.py": '''"""退款模块测试。"""

from src.modules.refunds.service import request_refund


def test_request_refund_is_importable() -> None:
    assert callable(request_refund)


def test_request_refund_rejects_unknown_order() -> None:
    try:
        request_refund({"order_id": "missing", "days_since_purchase": 1})
    except Exception as error:  # noqa: BLE001 - 只断言会抛错
        assert "missing" in str(error)
    else:  # pragma: no cover - 走到这里说明校验被改坏
        raise AssertionError("request_refund should reject unknown orders")
''',
    "tests/test_tax.py": '''"""税费适配测试。"""

from src.integrations.tax import calculate_tax_for_order, tax_breakdown


def test_tax_is_importable() -> None:
    assert callable(calculate_tax_for_order)


def test_tax_zero_amount_stays_zero() -> None:
    assert calculate_tax_for_order(0) == 0


def test_tax_breakdown_has_rate() -> None:
    status, _ = 200, tax_breakdown(1000)
    assert status == 200
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
    """一个模块的四个文件：__init__ / handler / service / validators。"""

    if service is None:
        template = _GENERATED_SERVICE_STORE if store else _GENERATED_SERVICE_READ
        service = template % {"name": name, "zh": zh, "fn": fn, "desc": desc}
    values = {"name": name, "zh": zh, "handler": handler, "fn": fn, "desc": desc}
    return {
        f"src/modules/{name}/__init__.py": _GENERATED_MODULE_INIT % values,
        f"src/modules/{name}/handler.py": _GENERATED_HANDLER % values,
        f"src/modules/{name}/service.py": service,
        f"src/modules/{name}/validators.py": _GENERATED_VALIDATORS % values,
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
