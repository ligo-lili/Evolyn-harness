"""敏感路径策略与密钥脱敏（v3 方案 3.4 / 8 节）。

子代理读取到的外部内容在返回模型之前统一过两道过滤：

- **路径策略**：命中敏感路径（凭证、私钥、环境文件等）的读取被拒绝并计入
  越权（与 scope 越界共用同一计数上限）；列表、搜索与 git 输出中敏感路径
  同样不出现，git 历史里的内容也按同一策略过滤。
- **密钥脱敏**：按已知密钥模式与高熵串检测，命中替换为 ``[REDACTED:…]``
  占位符；发现摘录在归档为证据之前同样脱敏。

生效边界：只作用于子代理工具面与发现账本；主 Agent 的同类工具行为不变。
脱敏是概率性防线，漏报由主 Agent 复核与 Trace 审计兜底（v3 残余风险）。
"""

from __future__ import annotations

import math
import re
from typing import Any

from .path_scope import SCOPE_VIOLATION_MARKER

# ---------------------------------------------------------------------------
# 敏感路径策略
# ---------------------------------------------------------------------------

# .env 变体默认敏感；模板文件（占位符而非真实凭证）放行。
_ENV_ALLOWED_SUFFIXES = (".example", ".sample", ".template")
_SENSITIVE_DIR_SEGMENTS = frozenset(
    {".ssh", ".gnupg", ".aws", ".azure", ".kube", ".git"}
)
_SENSITIVE_NAMES = frozenset(
    {
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "credentials",
        "credentials.json",
        "service-account.json",
        "secring.gpg",
        "master.key",
        ".npmrc",
        ".pypirc",
        ".netrc",
        "_netrc",
        ".htpasswd",
    }
)
_SENSITIVE_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".ppk",
    ".kdbx",
)


def is_sensitive_path(relative: str) -> bool:
    """判断工作区相对路径或绝对路径是否命中敏感路径策略。

    对目录段与文件名段做统一检查：``.ssh`` / ``credentials`` / ``.env``
    形态的目录名同样按敏感处理（例如 ``.ssh/config``、``credentials/x.json``）。
    """

    normalized = relative.replace("\\", "/").strip().strip("/")
    if not normalized:
        return False
    segments = [segment for segment in normalized.split("/") if segment]
    for segment in segments:
        folded = segment.casefold()
        if folded in _SENSITIVE_DIR_SEGMENTS:
            return True
        if folded in _SENSITIVE_NAMES:
            return True
        if folded == ".env" or (
            folded.startswith(".env.")
            and not folded.endswith(_ENV_ALLOWED_SUFFIXES)
        ):
            return True
    return segments[-1].casefold().endswith(_SENSITIVE_SUFFIXES)


def sensitive_rejection_text(relative: str) -> str:
    """敏感路径的拒绝消息；带越权标记（与 scope 越界共用计数）。"""

    return (
        f"{SCOPE_VIOLATION_MARKER}: sensitive path {relative!r} is excluded "
        "by policy (credentials and keys are not readable)"
    )


# ---------------------------------------------------------------------------
# 密钥脱敏
# ---------------------------------------------------------------------------

# 已知密钥模式：前缀 + 足够长的随机主体，误报率低。
_KEY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
            r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    ("openai-key", re.compile(r"(?<![\w-])sk-[A-Za-z0-9_-]{20,}")),
    ("github-token", re.compile(r"(?<![\w-])gh[pousr]_[A-Za-z0-9]{20,}")),
    ("gitlab-token", re.compile(r"(?<![\w-])glpat-[A-Za-z0-9_-]{20,}")),
    ("aws-key", re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}")),
    ("google-api-key", re.compile(r"(?<![\w-])AIza[0-9A-Za-z_-]{35}")),
    ("slack-token", re.compile(r"(?<![\w-])xox[baprs]-[A-Za-z0-9-]{10,}")),
    (
        "jwt",
        re.compile(
            r"(?<![\w-])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."
            r"[A-Za-z0-9_-]{10,}"
        ),
    ),
    ("bearer", re.compile(r"(?<![A-Za-z0-9_])Bearer\s+[A-Za-z0-9._~+/=-]{24,}")),
)

# 高熵串：长 token 且字符类足够混杂才算密钥候选；十六进制摘要（sha 等）
# 熵上限低于阈值，天然不会被误伤。
_HIGH_ENTROPY_CANDIDATE = re.compile(
    r"(?<![A-Za-z0-9+/_=-])([A-Za-z0-9+/_=-]{40,})(?![A-Za-z0-9+/_=-])"
)
_HEX_ONLY = re.compile(r"\A[0-9a-fA-F]+\Z")
_ENTROPY_THRESHOLD = 4.2


def _shannon_entropy(text: str) -> float:
    counts: dict[str, int] = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    length = len(text)
    return -sum(
        (count / length) * math.log2(count / length)
        for count in counts.values()
    )


def _is_high_entropy(token: str) -> bool:
    if _HEX_ONLY.match(token):
        return False
    classes = 0
    if any(char.islower() for char in token):
        classes += 1
    if any(char.isupper() for char in token):
        classes += 1
    if any(char.isdigit() for char in token):
        classes += 1
    if any(char in "+/_=-" for char in token):
        classes += 1
    if classes < 3:
        return False
    return _shannon_entropy(token) >= _ENTROPY_THRESHOLD


def redact_secrets(text: str) -> str:
    """按已知密钥模式与高熵串替换占位符；占位符自身不再命中（幂等）。"""

    redacted = text
    for kind, pattern in _KEY_PATTERNS:
        redacted = pattern.sub(f"[REDACTED:{kind}]", redacted)

    def _replace(candidate: re.Match[str]) -> str:
        token = candidate.group(1)
        if _is_high_entropy(token):
            return "[REDACTED:high-entropy]"
        return token

    return _HIGH_ENTROPY_CANDIDATE.sub(_replace, redacted)


def redact_value(value: Any) -> Any:
    """递归脱敏（dict / list / tuple 保持结构），非字符串原样返回。"""

    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    return value


__all__ = [
    "is_sensitive_path",
    "redact_secrets",
    "redact_value",
    "sensitive_rejection_text",
]
