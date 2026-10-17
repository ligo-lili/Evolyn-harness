"""敏感路径策略与密钥脱敏：模式命中、高熵判定与结构保持。"""

from __future__ import annotations

from app.subagent.redaction import (
    is_sensitive_path,
    redact_secrets,
    redact_value,
    sensitive_rejection_text,
)

# 48 个字符、lc/uc/digit 三类混杂，熵高于阈值。
_HIGH_ENTROPY = "aB3xK9mQ2wE7rT5yU8iO1pA4sD6fG0hJ3kL6zX9cV2bN5m"
_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_is_sensitive_path_matches_credentials_and_env_variants() -> None:
    for path in (
        ".env",
        ".env.local",
        "config/.env.production",
        ".ENV",
        "Credentials.JSON",
        "config\\.env.production",
        ".ssh/config",
        ".ssh/id_rsa",
        "deploy/id_ed25519",
        "keys/server.pem",
        "certs/client.p12",
        "credentials.json",
        ".aws/credentials",
        ".npmrc",
        ".git/config",
        "sub/credentials/token.json",
    ):
        assert is_sensitive_path(path), path


def test_is_sensitive_path_allows_templates_and_normal_files() -> None:
    for path in (
        ".env.example",
        ".env.sample",
        ".env.template",
        "src/app.py",
        "docs/readme.md",
        "keys/public.pub",
        "tests/test_env.py",
        ".",
        "",
    ):
        assert not is_sensitive_path(path), path


def test_redact_secrets_masks_known_key_patterns() -> None:
    text = (
        "OPENAI=sk-abcdefghijklmnopqrstuvwxyz012345\n"
        "GH=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\n"
        "AWS=AKIAIOSFODNN7EXAMPLE\n"
        "AUTH=Bearer abcdefghijklmnopqrstuvwxyz0123456789.ABCDEF\n"
    )

    redacted = redact_secrets(text)

    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in redacted
    assert "[REDACTED:openai-key]" in redacted
    assert "[REDACTED:github-token]" in redacted
    assert "[REDACTED:aws-key]" in redacted
    assert "[REDACTED:bearer]" in redacted


def test_redact_secrets_masks_private_key_blocks_and_jwts() -> None:
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAv1v1v1v1v1v1v1v1v1v1v1\n"
        "-----END RSA PRIVATE KEY-----"
    )
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )

    redacted = redact_secrets(f"{pem}\n{jwt}")

    assert "MIIEowIBAAKCAQEA" not in redacted
    assert "-----BEGIN" not in redacted
    assert "[REDACTED:private-key]" in redacted
    assert jwt not in redacted
    assert "[REDACTED:jwt]" in redacted


def test_redact_secrets_masks_high_entropy_but_keeps_hashes() -> None:
    text = f"{_HIGH_ENTROPY}\n{_SHA256}\nprint('hello-world')\n"

    redacted = redact_secrets(text)

    assert _HIGH_ENTROPY not in redacted
    assert "[REDACTED:high-entropy]" in redacted
    # 十六进制摘要与普通代码不是密钥，保持原样。
    assert _SHA256 in redacted
    assert "print('hello-world')" in redacted


def test_redact_value_preserves_structure_and_is_idempotent() -> None:
    payload = {
        "path": "src/app.py",
        "matches": ["sk-abcdefghijklmnopqrstuvwxyz012345", ("a", "b")],
        "count": 2,
    }

    redacted = redact_value(payload)

    assert redacted["path"] == "src/app.py"
    assert redacted["count"] == 2
    assert redacted["matches"][1] == ("a", "b")
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in redacted["matches"][0]
    assert redact_value(redacted) == redacted


def test_sensitive_rejection_text_carries_scope_marker() -> None:
    text = sensitive_rejection_text("config/.env")

    assert text.startswith("scope_violation:")
    assert "config/.env" in text
