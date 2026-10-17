"""run_live CLI 的离线端到端自检（Fake Adapter + 替换 Registry）。

验证：参数解析后的完整跑批流程、报告与 JSON 落盘、联合判定与退出码。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.models.config import ModelSettings
from app.models.registry import ModelAdapterRegistry
from app.models.types import ToolCall
from tests.eval.subagent import run_live
from tests.eval.subagent.tests.fakes import (
    FAKE_MODEL,
    FAKE_PROVIDER,
    FakeModelAdapter,
    fake_provider_config,
    response,
)
from tests.eval.subagent.tests.test_harness import (
    DELEGATE_FINAL,
    DIRECT_FINAL,
    SUB_SUMMARY,
)

_TASK_ID = "T01-locate-validate-session"


def _args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        arm="both",
        task=[_TASK_ID],
        category=None,
        suite=None,
        runs=1,
        provider=FAKE_PROVIDER,
        model=FAKE_MODEL,
        root=tmp_path / "root",
        out=tmp_path / "report.md",
        print=False,
        allow_no_go=False,
        price_input=None,
        price_output=None,
        currency="USD",
        max_steps=40,
        max_tool_rounds=40,
        max_context_reduction=0.5,
        max_correctness_drop=0.05,
        max_cost_ratio=1.3,
    )


def _responses() -> list:
    return [
        # A 臂：直接探索。
        response(
            tool_calls=(
                ToolCall(
                    id="a-1",
                    name="read_file",
                    arguments={"path": "src/auth/session.py"},
                ),
            ),
            input_tokens=1000,
            output_tokens=10,
        ),
        response(content=DIRECT_FINAL, input_tokens=2000, output_tokens=20),
        # B 臂：tool_search → 委派 → 子代理提交 → 主 Agent 汇总。
        response(
            tool_calls=(
                ToolCall(
                    id="b-1",
                    name="tool_search",
                    arguments={"query": "subagent"},
                ),
            ),
            input_tokens=500,
            output_tokens=5,
        ),
        response(
            tool_calls=(
                ToolCall(
                    id="b-2",
                    name="subagent_run",
                    arguments={
                        "ticket": {
                            "goal": "定位 validate_session 并说明校验行为",
                            "questions": [
                                "validate_session 定义在哪个文件？"
                            ],
                            "scope_paths": ["src"],
                            "done_when": "给出文件定位与校验行为结论",
                        },
                        "grants": ["read_repo"],
                    },
                ),
            ),
            input_tokens=800,
            output_tokens=8,
        ),
        response(
            tool_calls=(
                ToolCall(
                    id="b-3",
                    name="record_finding",
                    arguments={
                        "type": "exists",
                        "claim": (
                            "validate_session 定义在 src/auth/session.py。"
                        ),
                        "locations": [
                            {
                                "path": "src/auth/session.py",
                                "symbol": "validate_session",
                            }
                        ],
                    },
                ),
            ),
            input_tokens=300,
            output_tokens=30,
        ),
        response(
            tool_calls=(
                ToolCall(
                    id="b-4",
                    name="submit_result",
                    arguments={"summary": SUB_SUMMARY},
                ),
            ),
            input_tokens=700,
            output_tokens=70,
        ),
        response(content=DELEGATE_FINAL, input_tokens=900, output_tokens=9),
    ]


async def test_run_live_end_to_end_offline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    adapter = FakeModelAdapter(fake_provider_config(), _responses())
    registry = ModelAdapterRegistry(ModelSettings(_env_file=None))
    registry.register(FAKE_PROVIDER, lambda _: adapter, config=fake_provider_config())
    monkeypatch.setattr(
        run_live,
        "ModelAdapterRegistry",
        lambda _settings: registry,
    )

    args = _args(tmp_path)
    exit_code = await run_live.main(args)
    assert exit_code == 0

    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "判定：**Go**" in report
    assert "安全子集明细" in report

    payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert payload["verdict"]["result"] == "Go"
    task = payload["tasks"][0]
    assert task["task_id"] == _TASK_ID
    assert task["delegate"][0]["subagent_calls"] == 1
    assert task["direct"][0]["subagent_calls"] == 0
    # 峰值降幅 55%：2000 → 900。
    assert payload["summary"]["context_reduction"] > 0.5
