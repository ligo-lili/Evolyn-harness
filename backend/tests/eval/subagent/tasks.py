"""阶段 0 评测任务集：20 个真实代码探索任务 + 标准答案。

任务分类对应 v3 §9 的覆盖面：函数定位、跨文件调用链、配置追踪、异常处理、
测试覆盖、跨模块依赖，外加安全子集（认证 / 权限 / 数据隔离）。

标准答案形式为「关键结论」（KeyFact）：

- ``patterns`` 是大小写不敏感的正则；``match="any"`` 表示任一条命中即算
  该结论被给出，``match="all"`` 表示全部命中；
- ``critical=True`` 的结论只在安全子集使用：任何一条缺失即计一次关键错误
  （Go/No-go 的门槛口径：关键错误 0 新增）；
- 字符串匹配是刻意保守的确定性口径：它对「结论缺失」判罚可靠，对「表述
  含混」会有误报，阶段 0 报告同时给出每任务的关键结论明细供人工抽样核对。
"""

from __future__ import annotations

from dataclasses import dataclass

SECURITY_CATEGORY = "security"

CATEGORIES: tuple[str, ...] = (
    "function_locate",
    "call_chain",
    "config_trace",
    "error_handling",
    "test_coverage",
    "module_dependency",
    SECURITY_CATEGORY,
)


@dataclass(frozen=True, slots=True)
class KeyFact:
    """一条关键结论及其可判定的匹配模式。"""

    id: str
    description: str
    patterns: tuple[str, ...]
    match: str = "any"
    critical: bool = False

    def __post_init__(self) -> None:
        if self.match not in ("any", "all"):
            raise ValueError(f"match must be 'any' or 'all': {self.match!r}")
        if not self.patterns:
            raise ValueError(f"KeyFact {self.id!r} needs at least one pattern")


@dataclass(frozen=True, slots=True)
class EvalTask:
    """一个自包含的代码探索任务。"""

    id: str
    category: str
    name: str
    user_input: str
    criteria: str
    expected_files: int
    scope_hint: tuple[str, ...]
    key_facts: tuple[KeyFact, ...]

    @property
    def is_security(self) -> bool:
        return self.category == SECURITY_CATEGORY

    @property
    def critical_fact_count(self) -> int:
        return sum(1 for fact in self.key_facts if fact.critical)


TASKS: tuple[EvalTask, ...] = (
    # ------------------------------------------------------------------
    # 函数定位
    # ------------------------------------------------------------------
    EvalTask(
        id="T01-locate-validate-session",
        category="function_locate",
        name="定位会话校验函数",
        user_input=(
            "在这个仓库中定位会话校验函数 validate_session：它在哪个文件"
            "定义、内部做了哪些校验？请给出文件路径、函数名与校验行为。"
        ),
        criteria="给出 validate_session 的定义文件，并说明存在性/过期两类校验。",
        expected_files=2,
        scope_hint=("src/auth/",),
        key_facts=(
            KeyFact(
                "location",
                "定义在 src/auth/session.py",
                (r"session\.py", r"validate_session"),
                match="all",
            ),
            KeyFact(
                "expiry",
                "过期校验：expires_at 与当前时间比较，过期会话被删除",
                (r"expires_at", r"(删除|delete)"),
                match="all",
            ),
        ),
    ),
    EvalTask(
        id="T02-locate-login-handler",
        category="function_locate",
        name="定位登录处理函数",
        user_input=(
            "处理 POST /login 的函数叫什么、在哪个文件？它把用户名密码交给"
            "哪个函数完成登录？给出两者的定位。"
        ),
        criteria=(
            "给出 handle_login（src/api/routes.py）与 login（src/auth/login.py）。"
        ),
        expected_files=3,
        scope_hint=("src/api/", "src/auth/"),
        key_facts=(
            KeyFact(
                "handler",
                "handle_login 定义在 src/api/routes.py",
                (r"handle_login", r"routes\.py"),
                match="all",
            ),
            KeyFact(
                "entry",
                "调用 src/auth/login.py 的 login()",
                (r"login\.py", r"(?<!_)login\("),
                match="all",
            ),
        ),
    ),
    EvalTask(
        id="T03-locate-permission",
        category="function_locate",
        name="定位权限检查实现",
        user_input=(
            "权限检查的实现（检查函数与角色策略常量）在哪个文件？函数与常量的"
            "名字是什么？请给出定位与简要语义。"
        ),
        criteria="给出 check_permission 与 ROLE_POLICY（src/auth/permissions.py）。",
        expected_files=2,
        scope_hint=("src/auth/",),
        key_facts=(
            KeyFact(
                "function",
                "check_permission 定义在 src/auth/permissions.py",
                (r"check_permission", r"permissions\.py"),
                match="all",
            ),
            KeyFact(
                "policy",
                "ROLE_POLICY 定义角色到动作集合的映射（admin/user）",
                (r"ROLE_POLICY", r"admin"),
                match="all",
            ),
        ),
    ),
    EvalTask(
        id="T04-locate-error-mapping",
        category="function_locate",
        name="定位错误映射函数",
        user_input=(
            "DomainError 到 HTTP 状态码的映射函数在哪个文件？各子类分别映射到"
            "什么状态码？"
        ),
        criteria=(
            "给出 error_response（src/api/errors.py）；"
            "NotFoundError=404、PermissionDeniedError=403、ValidationError=400。"
        ),
        expected_files=3,
        scope_hint=("src/api/", "src/core/"),
        key_facts=(
            KeyFact(
                "function",
                "error_response 定义在 src/api/errors.py",
                (r"error_response", r"errors\.py"),
                match="all",
            ),
            KeyFact(
                "notfound",
                "NotFoundError 映射 404",
                (r"NotFound", r"404"),
                match="all",
            ),
            KeyFact(
                "denied",
                "PermissionDeniedError 映射 403",
                (r"PermissionDenied", r"403"),
                match="all",
            ),
            KeyFact(
                "validation",
                "ValidationError 映射 400",
                (r"Validation", r"400"),
                match="all",
            ),
        ),
    ),
    # ------------------------------------------------------------------
    # 跨文件调用链
    # ------------------------------------------------------------------
    EvalTask(
        id="T05-login-call-chain",
        category="call_chain",
        name="登录请求调用链",
        user_input=(
            "从 src/api/app.py 的 dispatch 开始，列出 POST /login 被处理到"
            "会话创建之间的完整调用链（函数级），标出每一步所在文件。"
        ),
        criteria=(
            "dispatch → handle_login → login → find_user / verify_password → "
            "create_session 的链路完整。"
        ),
        expected_files=6,
        scope_hint=("src/api/", "src/auth/", "src/services/"),
        key_facts=(
            KeyFact(
                "dispatch",
                "dispatch 分发到 handle_login",
                (r"dispatch", r"handle_login"),
                match="all",
            ),
            KeyFact(
                "handler",
                "handle_login 调用 login()",
                (r"(?<!_)login\(", r"routes\.py"),
                match="all",
            ),
            KeyFact(
                "verify",
                "login 内部经 find_user 与 verify_password 校验",
                (r"find_user", r"verify_password"),
                match="all",
            ),
            KeyFact("session", "成功路径调用 create_session", (r"create_session",)),
        ),
    ),
    EvalTask(
        id="T06-admin-call-chain",
        category="call_chain",
        name="管理员接口调用链与授权",
        user_input=(
            "GET /admin/users 从 dispatch 到返回用户列表经过哪些函数？"
            "其中包含哪些认证/授权检查？是否完整？"
        ),
        criteria=(
            "给出 middleware/validate_session 认证链路、handle_admin_users→"
            "list_users，并指出没有权限检查。"
        ),
        expected_files=5,
        scope_hint=("src/api/", "src/auth/", "src/services/"),
        key_facts=(
            KeyFact(
                "auth-chain",
                "/admin/* 先过 auth_middleware → validate_session",
                (r"auth_middleware", r"validate_session"),
                match="all",
            ),
            KeyFact(
                "data-chain",
                "handle_admin_users 调用 list_users",
                (r"handle_admin_users", r"list_users"),
                match="all",
            ),
            KeyFact(
                "gap",
                "handle_admin_users 没有任何权限检查",
                (r"handle_admin_users", r"(没有|未|缺少|缺失|无)", r"权限"),
                match="all",
            ),
        ),
    ),
    EvalTask(
        id="T07-audit-call-chain",
        category="call_chain",
        name="审计链路与调用方",
        user_input=(
            "审计事件 audit_service.record() 最终写到哪里？列出仓库中所有调用"
            "record() 的位置（文件+函数）以及各自触发场景。"
        ),
        criteria=(
            "record → core.logging.audit_log；调用方为 handle_user_export、"
            "login（成功/锁定）、update_password。"
        ),
        expected_files=5,
        scope_hint=("src/services/", "src/core/", "src/api/", "src/auth/"),
        key_facts=(
            KeyFact(
                "sink",
                "record 经 core/logging.py 的 audit_log 写日志",
                (r"audit_log", r"logging\.py"),
                match="all",
            ),
            KeyFact("export", "handle_user_export 调用", (r"handle_user_export",)),
            KeyFact(
                "login",
                "login 记录 login_success / login_locked",
                (r"login_success|login_locked",),
            ),
            KeyFact("password", "update_password 调用", (r"update_password",)),
        ),
    ),
    # ------------------------------------------------------------------
    # 配置追踪
    # ------------------------------------------------------------------
    EvalTask(
        id="T08-auth-env-vars",
        category="config_trace",
        name="认证配置环境变量",
        user_input=(
            "认证相关配置涉及哪些环境变量、各自默认值是什么？读取它们的函数"
            "在哪个文件？"
        ),
        criteria=(
            "load_settings（src/config/settings.py）读取 AUTH_SESSION_TTL=3600、"
            "AUTH_MAX_LOGIN_ATTEMPTS=5、AUTH_AUDIT_ENABLED 等。"
        ),
        expected_files=2,
        scope_hint=("src/config/",),
        key_facts=(
            KeyFact(
                "loader",
                "load_settings 定义在 src/config/settings.py",
                (r"load_settings", r"settings\.py"),
                match="all",
            ),
            KeyFact(
                "ttl",
                "AUTH_SESSION_TTL 默认 3600",
                (r"AUTH_SESSION_TTL", r"3600"),
                match="all",
            ),
            KeyFact(
                "attempts",
                "AUTH_MAX_LOGIN_ATTEMPTS 默认 5",
                (r"AUTH_MAX_LOGIN_ATTEMPTS", r"5"),
                match="all",
            ),
            KeyFact("audit", "AUTH_AUDIT_ENABLED", (r"AUTH_AUDIT_ENABLED",)),
        ),
    ),
    EvalTask(
        id="T09-unused-config",
        category="config_trace",
        name="未使用的配置项",
        user_input=(
            "src/config/settings.py 中定义了哪些配置项？其中哪一项没有被任何"
            "模块使用？给出定义位置与判断依据。"
        ),
        criteria="指出 lockout_window_seconds（AUTH_LOCKOUT_WINDOW）未被任何模块引用。",
        expected_files=2,
        scope_hint=("src/config/", "src/auth/", "src/services/"),
        key_facts=(
            KeyFact(
                "unused",
                "lockout_window_seconds 定义但未被使用",
                (
                    r"lockout_window",
                    r"(未使用|没有使用|未被|不被|未引用|"
                    r"没有任何模块使用|unused|not used)",
                ),
                match="all",
            ),
        ),
    ),
    EvalTask(
        id="T10-token-secret",
        category="config_trace",
        name="令牌密钥读取方式",
        user_input=(
            "src/auth/tokens.py 的签名密钥从哪里读取？它和 src/config/settings.py "
            "的集中配置方式有什么区别？"
        ),
        criteria="AUTH_TOKEN_SECRET 由 tokens.py 直接读 os.environ，未经过集中配置。",
        expected_files=3,
        scope_hint=("src/auth/", "src/config/"),
        key_facts=(
            KeyFact(
                "secret",
                "密钥来自 AUTH_TOKEN_SECRET 环境变量",
                (r"AUTH_TOKEN_SECRET", r"(os\.environ|environ|环境变量)"),
                match="all",
            ),
            KeyFact(
                "inconsistency",
                "tokens.py 直接读取，未经过集中配置（settings）",
                (r"tokens\.py", r"(直接在|绕过|未经过|没有经过|不走)"),
                match="all",
            ),
        ),
    ),
    # ------------------------------------------------------------------
    # 异常处理分析
    # ------------------------------------------------------------------
    EvalTask(
        id="T11-error-handling-mechanism",
        category="error_handling",
        name="异常映射机制",
        user_input=(
            "API 层是如何把业务异常转成 HTTP 响应的（装饰器与映射函数）？"
            "对于没有专门映射的 DomainError 子类会发生什么？"
        ),
        criteria=(
            "wrap_domain_errors 捕获 DomainError 调 error_response；"
            "未映射类型回退 500。"
        ),
        expected_files=3,
        scope_hint=("src/api/", "src/core/"),
        key_facts=(
            KeyFact(
                "decorator",
                "wrap_domain_errors 捕获 DomainError",
                (r"wrap_domain_errors", r"DomainError"),
                match="all",
            ),
            KeyFact(
                "fallback",
                "未映射的 DomainError 作为 500 兜底",
                (r"500", r"(其他|未知|默认|兜底|fallback)"),
                match="all",
            ),
        ),
    ),
    EvalTask(
        id="T12-login-failure-path",
        category="error_handling",
        name="登录失败与锁定路径",
        user_input=(
            "登录失败路径是怎么处理的：哪些分支记录失败次数？锁定条件是什么？"
            "成功登录时失败计数会被清零吗？"
        ),
        criteria=(
            "record_failure 仅在密码错误/用户不存在时调用；is_locked 用 "
            "max_login_attempts；成功登录不会清零 _FAILED_ATTEMPTS。"
        ),
        expected_files=3,
        scope_hint=("src/auth/",),
        key_facts=(
            KeyFact(
                "failure-branch",
                "密码错误或用户不存在时 record_failure",
                (
                    r"record_failure",
                    r"(密码错误|verify_password|user is None|校验失败)",
                ),
                match="all",
            ),
            KeyFact(
                "lock-condition",
                "is_locked 依据 max_login_attempts",
                (r"is_locked", r"max_login_attempts"),
                match="all",
            ),
            KeyFact(
                "no-reset",
                "成功登录不会清零失败计数",
                (r"(不会|没有|未).{0,6}(清零|重置)", r"失败|_FAILED_ATTEMPTS"),
                match="all",
            ),
        ),
    ),
    # ------------------------------------------------------------------
    # 测试覆盖分析
    # ------------------------------------------------------------------
    EvalTask(
        id="T13-session-test-coverage",
        category="test_coverage",
        name="会话测试覆盖缺口",
        user_input=(
            "tests/test_session.py 覆盖了 validate_session 的哪些分支？哪些"
            "分支没有被覆盖？请给出缺口与依据。"
        ),
        criteria="覆盖有效会话与未知会话；未覆盖过期分支（expires_at 判断）。",
        expected_files=3,
        scope_hint=("tests/", "src/auth/"),
        key_facts=(
            KeyFact(
                "covered",
                "覆盖了未知/不存在的会话分支",
                (r"test_session\.py", r"(未知|不存在|unknown|missing)"),
                match="all",
            ),
            KeyFact(
                "gap",
                "过期分支未覆盖",
                (r"(过期|expires_at)", r"(未覆盖|没有覆盖|没覆盖|缺少|缺失)"),
                match="all",
            ),
        ),
    ),
    EvalTask(
        id="T14-missing-test-files",
        category="test_coverage",
        name="完全缺少测试的模块",
        user_input=(
            "src/auth 与 src/api 包中，哪些模块没有任何对应的测试文件？另外"
            "登录锁定逻辑是否有测试？"
        ),
        criteria="tokens.py、middleware.py、routes.py 无测试；锁定逻辑无测试。",
        expected_files=8,
        scope_hint=("tests/", "src/auth/", "src/api/"),
        key_facts=(
            KeyFact("tokens", "tokens.py 无测试", (r"tokens\.py",)),
            KeyFact("middleware", "middleware.py 无测试", (r"middleware\.py",)),
            KeyFact("routes", "routes.py 无测试", (r"routes\.py",)),
            KeyFact(
                "lockout",
                "登录锁定/失败计数没有测试",
                (
                    r"(lockout|锁定|is_locked|record_failure)",
                    r"(未测试|没有测试|无测试|未覆盖|缺少测试|缺失)",
                ),
                match="all",
            ),
        ),
    ),
    # ------------------------------------------------------------------
    # 跨模块依赖分析
    # ------------------------------------------------------------------
    EvalTask(
        id="T15-api-dependencies",
        category="module_dependency",
        name="api 包内部依赖",
        user_input=(
            "src/api 包内各文件依赖哪些内部模块？给出文件级的主要依赖关系"
            "（谁 import 了谁）。"
        ),
        criteria=(
            "routes→auth/services、middleware→auth.session、app→routes/middleware、"
            "errors→core.errors。"
        ),
        expected_files=6,
        scope_hint=("src/api/", "src/auth/", "src/core/"),
        key_facts=(
            KeyFact(
                "routes",
                "routes.py 依赖 auth 与 services",
                (r"routes\.py", r"auth", r"service"),
                match="all",
            ),
            KeyFact(
                "middleware",
                "middleware.py 依赖 auth.session",
                (r"middleware\.py", r"session"),
                match="all",
            ),
            KeyFact(
                "app",
                "app.py 依赖 routes 与 middleware",
                (r"app\.py", r"(routes|middleware)"),
                match="all",
            ),
        ),
    ),
    EvalTask(
        id="T16-dependency-direction",
        category="module_dependency",
        name="层次依赖方向",
        user_input=(
            "auth、services、storage、core 之间的依赖方向是什么？是否存在"
            "反向依赖或循环导入风险？"
        ),
        criteria=(
            "auth→services/config、services→storage、core 无内部依赖；不存在"
            "反向依赖与循环导入。"
        ),
        expected_files=5,
        scope_hint=("src/",),
        key_facts=(
            KeyFact(
                "services-storage",
                "services 依赖 storage（models/db）",
                (r"services", r"storage"),
                match="all",
            ),
            KeyFact(
                "auth-down",
                "auth 依赖 config 与 services",
                (r"auth", r"(config|service)"),
                match="all",
            ),
            KeyFact(
                "core-base",
                "core 不依赖其他内部模块（基础层）",
                (r"core", r"(不依赖|无依赖|没有依赖|独立|基础层|最底层)"),
                match="all",
            ),
            KeyFact(
                "no-cycle",
                "不存在循环导入/反向依赖",
                (r"(不存在|没有|无)", r"(循环|反向)"),
                match="all",
            ),
        ),
    ),
    EvalTask(
        id="T17-audit-callers",
        category="module_dependency",
        name="审计调用方场景",
        user_input=(
            "谁调用了 audit_service.record？这些调用分别发生在什么业务场景？"
            "给出调用点定位。"
        ),
        criteria=(
            "handle_user_export（导出）、login（成功/锁定）、update_password"
            "（改密）三处。"
        ),
        expected_files=4,
        scope_hint=("src/",),
        key_facts=(
            KeyFact("export", "导出时记录", (r"handle_user_export",)),
            KeyFact(
                "login",
                "登录成功/锁定记录",
                (r"login_success|login_locked",),
            ),
            KeyFact("password", "更新密码记录", (r"update_password",)),
        ),
    ),
    # ------------------------------------------------------------------
    # 安全子集（认证 / 权限 / 数据隔离）
    # ------------------------------------------------------------------
    EvalTask(
        id="T18-admin-authorization-gap",
        category=SECURITY_CATEGORY,
        name="管理员接口授权缺口",
        user_input=(
            "GET /admin/users 有没有做权限校验？请给出证据定位。如果存在缺口，"
            "说明谁可以访问这个接口、后果是什么。与同类接口对比说明。"
        ),
        criteria=(
            "关键：handle_admin_users 没有任何 require_permission 调用；"
            "认证（middleware）存在但授权缺失；对比 handle_admin_audit。"
        ),
        expected_files=4,
        scope_hint=("src/api/", "src/auth/"),
        key_facts=(
            KeyFact(
                "gap",
                "handle_admin_users 没有权限检查（关键）",
                (
                    r"handle_admin_users",
                    r"(没有|缺少|缺失|未|无).{0,12}(权限|授权|permission|require_permission)",
                ),
                match="all",
                critical=True,
            ),
            KeyFact(
                "location",
                "缺口位于 src/api/routes.py（关键）",
                (r"routes\.py", r"handle_admin_users"),
                match="all",
                critical=True,
            ),
            KeyFact(
                "contrast",
                "对比：handle_admin_audit 有 require_permission",
                (r"require_permission", r"(admin_audit|审计)"),
                match="all",
            ),
            KeyFact(
                "authn-exists",
                "认证仍存在（middleware 校验会话）",
                (r"middleware|auth_middleware",),
            ),
        ),
    ),
    EvalTask(
        id="T19-session-validation-bypass",
        category=SECURITY_CATEGORY,
        name="会话校验绕过",
        user_input=(
            "GET /profile 的会话校验是否完整？它与 validate_session 的差异是"
            "什么？过期会话能否继续访问？给出证据定位。"
        ),
        criteria=(
            "关键：handle_profile 直接调用 store.get_session（不做过期检查），"
            "而非 validate_session；过期会话仍可通过。"
        ),
        expected_files=4,
        scope_hint=("src/api/", "src/auth/"),
        key_facts=(
            KeyFact(
                "bypass",
                "handle_profile 直接调用 get_session（关键）",
                (r"handle_profile", r"get_session"),
                match="all",
                critical=True,
            ),
            KeyFact(
                "no-expiry",
                "get_session 不检查过期（关键）",
                (r"get_session", r"(过期|expires_at)"),
                match="all",
                critical=True,
            ),
            KeyFact(
                "contrast",
                "对比 validate_session 才是完整校验",
                (r"validate_session", r"(而不是|绕过|跳过|差异|区别|没有调用|未调用)"),
                match="all",
            ),
        ),
    ),
    EvalTask(
        id="T20-data-isolation-gap",
        category=SECURITY_CATEGORY,
        name="用户数据隔离缺口",
        user_input=(
            "GET /export 能否导出其他用户的数据？数据出口在哪里、缺少什么"
            "检查？给出证据定位与后果。"
        ),
        criteria=(
            "关键：user_id 来自请求参数且无归属/权限校验；export_user_data "
            "接受任意 user_id。"
        ),
        expected_files=4,
        scope_hint=("src/api/", "src/services/"),
        key_facts=(
            KeyFact(
                "source",
                "user_id 来自请求参数（关键）",
                (r"export", r"user_id", r"(请求|query|参数)"),
                match="all",
                critical=True,
            ),
            KeyFact(
                "no-check",
                "没有归属/权限校验（关键）",
                (r"(没有|未|缺少|缺失|缺乏|无)", r"(归属|owner|所有权|身份|权限)"),
                match="all",
                critical=True,
            ),
            KeyFact(
                "export-fn",
                "export_user_data 接受任意 user_id",
                (r"export_user_data", r"user_id"),
                match="all",
            ),
        ),
    ),
)


def select_tasks(
    *,
    task_ids: tuple[str, ...] = (),
    categories: tuple[str, ...] = (),
) -> tuple[EvalTask, ...]:
    """按 ID / 分类筛选任务；两个条件都是可选的并集过滤。"""

    selected: list[EvalTask] = []
    wanted_ids = set(task_ids)
    wanted_categories = set(categories)
    for task in TASKS:
        if wanted_ids or wanted_categories:
            if task.id not in wanted_ids and task.category not in wanted_categories:
                continue
        selected.append(task)
    return tuple(selected)


def validate_task_set(tasks: tuple[EvalTask, ...] = TASKS) -> None:
    """离线自检：任务集的确定性问题在这里 fail fast。"""

    seen_ids: set[str] = set()
    for task in tasks:
        if task.id in seen_ids:
            raise ValueError(f"duplicate task id: {task.id}")
        seen_ids.add(task.id)
        if task.category not in CATEGORIES:
            raise ValueError(f"unknown category: {task.category!r}")
        if not task.user_input.strip() or not task.criteria.strip():
            raise ValueError(f"task {task.id} needs user_input and criteria")
        if not task.key_facts:
            raise ValueError(f"task {task.id} needs at least one key fact")
        fact_ids: set[str] = set()
        for fact in task.key_facts:
            if fact.id in fact_ids:
                raise ValueError(f"task {task.id}: duplicate fact id {fact.id}")
            fact_ids.add(fact.id)
        if task.is_security and task.critical_fact_count == 0:
            raise ValueError(f"security task {task.id} needs a critical fact")
        if not task.is_security and task.critical_fact_count > 0:
            raise ValueError(
                f"non-security task {task.id} must not mark facts critical"
            )


__all__ = [
    "CATEGORIES",
    "SECURITY_CATEGORY",
    "TASKS",
    "EvalTask",
    "KeyFact",
    "select_tasks",
    "validate_task_set",
]
