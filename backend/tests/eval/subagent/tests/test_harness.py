"""A/B 装配离线自检（Fake Adapter，不调用真实模型）。

覆盖点：

- 两臂都能在同一夹具仓库上跑完整闭环并采到指标；
- B 臂走生产的 deferred 委派路径（tool_search → subagent_run → 子代理
  record_finding → submit_result → 主 Agent 汇总）；
- 峰值上下文口径只统计主 Run（子代理 usage 不参与峰值，但计入全量消耗）。
"""

from __future__ import annotations

from pathlib import Path

from app.evidence import SQLiteEvidenceStore
from app.models.config import ModelSettings
from app.models.registry import ModelAdapterRegistry
from app.models.types import ToolCall
from app.trace import SQLiteTraceStore
from tests.eval.subagent.harness import (
    DELEGATE_ARM,
    DIRECT_ARM,
    HarnessConfig,
    run_task,
)
from tests.eval.subagent.metrics import build_run_metrics
from tests.eval.subagent.tasks import TASKS
from tests.eval.subagent.tests.fakes import (
    FAKE_MODEL,
    FAKE_PROVIDER,
    FakeModelAdapter,
    fake_provider_config,
    response,
)

_T01 = next(task for task in TASKS if task.id.startswith("T01"))

DIRECT_FINAL = (
    "validate_session 定义在 src/auth/session.py：先按 session_id 取会话，"
    "不存在返回 None；当 expires_at 已过期时删除会话并返回 None。"
)
DELEGATE_FINAL = (
    "子代理结论：validate_session 位于 src/auth/session.py；"
    "过期 expires_at 会删除会话。位置与行为均已核实。"
)
SUB_SUMMARY = (
    "validate_session 在 src/auth/session.py 中定义；"
    "检查 expires_at 过期并删除会话。"
)
SUB_RECORD_TOKENS = 300


def _make_registry(adapter: FakeModelAdapter) -> ModelAdapterRegistry:
    registry = ModelAdapterRegistry(ModelSettings(_env_file=None))
    registry.register(FAKE_PROVIDER, lambda _: adapter, config=fake_provider_config())
    return registry


def _harness_config() -> HarnessConfig:
    return HarnessConfig(provider=FAKE_PROVIDER, model=FAKE_MODEL)


async def test_direct_arm_smoke(tmp_path: Path) -> None:
    adapter = FakeModelAdapter(
        fake_provider_config(),
        [
            response(
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        name="read_file",
                        arguments={"path": "src/auth/session.py"},
                    ),
                ),
                input_tokens=1000,
            ),
            response(content=DIRECT_FINAL, input_tokens=2000),
        ],
    )
    outcome = await run_task(
        _T01,
        arm=DIRECT_ARM,
        root=tmp_path / "direct",
        config=_harness_config(),
        registry=_make_registry(adapter),
    )
    assert outcome.ok, outcome.error
    metrics = build_run_metrics(
        _T01,
        DIRECT_ARM,
        result=outcome.result,
        events=outcome.events,
        duration_s=outcome.duration_s,
        error=outcome.error,
    )
    assert metrics.answer.passed
    assert metrics.main_peak_prompt_tokens == 2000
    assert metrics.subagent_calls == 0
    assert metrics.result_payload_chars == 0
    assert metrics.files_read_main == 1
    assert (outcome.workspace / "src" / "auth" / "session.py").is_file()
    # Evidence 链路：A 臂工具输出原文落 Store（与生产同源）。
    store = SQLiteEvidenceStore(outcome.workspace.parent / "eval.db")
    await store.initialize()
    hits = await store.search(
        conversation_id=f"eval-{_T01.id}-{DIRECT_ARM}",
        query="validate_session",
        tool_name="read_file",
    )
    assert hits, "direct arm read_file output should be archived as evidence"


async def test_delegate_arm_smoke(tmp_path: Path) -> None:
    adapter = FakeModelAdapter(
        fake_provider_config(),
        [
            response(
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        name="tool_search",
                        arguments={"query": "subagent"},
                    ),
                ),
                input_tokens=1000,
            ),
            response(
                tool_calls=(
                    ToolCall(
                        id="call-2",
                        name="subagent_run",
                        arguments={
                            "ticket": {
                                "goal": (
                                    "在当前仓库定位 validate_session "
                                    "的定义与校验行为"
                                ),
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
                input_tokens=2000,
            ),
            response(
                tool_calls=(
                    ToolCall(
                        id="call-3",
                        name="record_finding",
                        arguments={
                            "type": "exists",
                            "claim": (
                                "validate_session 定义在 "
                                "src/auth/session.py。"
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
                input_tokens=SUB_RECORD_TOKENS,
            ),
            response(
                tool_calls=(
                    ToolCall(
                        id="call-4",
                        name="submit_result",
                        arguments={"summary": SUB_SUMMARY},
                    ),
                ),
                input_tokens=999_999,
            ),
            response(content=DELEGATE_FINAL, input_tokens=3000),
        ],
    )
    outcome = await run_task(
        _T01,
        arm=DELEGATE_ARM,
        root=tmp_path / "delegate",
        config=_harness_config(),
        registry=_make_registry(adapter),
    )
    assert outcome.ok, outcome.error
    metrics = build_run_metrics(
        _T01,
        DELEGATE_ARM,
        result=outcome.result,
        events=outcome.events,
        duration_s=outcome.duration_s,
        error=outcome.error,
    )
    assert metrics.answer.passed
    assert metrics.subagent_calls == 1
    assert metrics.result_payload_chars > 0
    # 峰值口径只含主 Run：子代理的 999_999 不参与。
    assert metrics.main_peak_prompt_tokens == 3000
    # 但子代理消耗计入全量账（record_finding + submit 两次调用）。
    assert metrics.sub_input_tokens == SUB_RECORD_TOKENS + 999_999
    assert metrics.all_chargeable_tokens > metrics.main_chargeable_tokens
    # Evidence 链路：子代理发现有原文可回读（主 Agent 可用 evidence_read）。
    store = SQLiteEvidenceStore(outcome.workspace.parent / "eval.db")
    await store.initialize()
    hits = await store.search(
        conversation_id=f"eval-{_T01.id}-{DELEGATE_ARM}",
        query="validate_session",
        tool_name="record_finding",
    )
    assert hits, "subagent finding should be archived as evidence"
    # 子代理 Trace：调查过程事件落库（供离线复盘）。
    trace_store = SQLiteTraceStore(outcome.workspace.parent / "eval.db")
    await trace_store.initialize()
    payloads = await trace_store.load_event_payloads_for_conversation(
        f"eval-{_T01.id}-{DELEGATE_ARM}"
    )
    assert payloads, "subagent trace events should be persisted"
