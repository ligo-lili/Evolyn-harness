"""Sub-Agent 离线测试夹具：暴露 ``build_harness`` 工厂。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.offline.subagent.harness import build_harness as _build_harness


@pytest.fixture()
def build_harness(tmp_path: Path):
    """返回 ``build_harness(responses, **options)`` 工厂（工作区在 tmp_path）。"""

    def build(responses, **options):
        return _build_harness(tmp_path, responses, **options)

    return build
