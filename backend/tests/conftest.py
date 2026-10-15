"""跨套件共享的测试夹具：环境隔离与平台能力探测。"""

from __future__ import annotations

from pathlib import Path

import pytest

_AMBIENT_ENVIRONMENT_KEYS = (
    "OPENAI_API_KEY",
    "DASHSCOPE_API_KEY",
    "QWEN_API_KEY",
    "DEEPSEEK_API_KEY",
    "ANTHROPIC_API_KEY",
    "TAVILY_API_KEY",
    "EVOLYN_SHELL",
    "EVOLYN_SANDBOX_LOW_IL",
)


@pytest.fixture(autouse=True)
def _clean_ambient_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """清除宿主机环境里的模型/搜索密钥与 Evolyn 开关。

    离线测试必须与开发者机器上的真实密钥、环境开关无关；需要特定取值时
    由测试自身用 ``monkeypatch.setenv`` 显式注入。
    """

    for key in _AMBIENT_ENVIRONMENT_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def symlink_support(tmp_path: Path) -> None:
    """探测符号链接能力；无权限（Windows 未启用开发者模式）时跳过测试。"""

    probe_dir = tmp_path / "symlink-probe"
    probe_dir.mkdir()
    target = probe_dir / "target.txt"
    target.write_text("probe", encoding="utf-8")
    link = probe_dir / "link.txt"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"当前环境无法创建符号链接：{exc}")
    link.unlink()
