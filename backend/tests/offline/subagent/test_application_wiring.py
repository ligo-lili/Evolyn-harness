"""Application 装配：subagent_run 延迟注册，经 tool_search 激活后可用。"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import SecretStr

from app.agent.events import AgentEventType, InMemoryEventHandler
from app.application import Application
from app.memory import MemoryMaintenanceConfig, MemoryReflectionConfig
from app.models.config import ModelSettings, ProviderConfig
from app.models.registry import ModelAdapterRegistry
from app.models.types import ApiStyle
from app.skill_learning import SkillLearningSettings
from tests.offline.subagent.harness import (
    FakeModelAdapter,
    model_response,
    tool_call,
    usage,
)


def _application(tmp_path: Path, adapter: FakeModelAdapter) -> Application:
    registry = ModelAdapterRegistry(ModelSettings(_env_file=None))
    registry.register("main", lambda _: adapter, config=adapter.config)
    return Application(
        provider="main",
        model="main-model",
        database=tmp_path / "evolyn.db",
        tasks_dir=tmp_path / "tasks",
        mcp_config=tmp_path / "mcp.json",
        memory_dir=tmp_path / "memory",
        skills_user_dir=tmp_path / "skills-user",
        skills_project_dir=tmp_path / "skills-project",
        workspace_root=tmp_path / "workspace",
        registry=registry,
        memory_reflection_config=MemoryReflectionConfig(
            _env_file=None, enabled=False
        ),
        memory_maintenance_config=MemoryMaintenanceConfig(
            _env_file=None, enabled=False
        ),
        skill_learning_settings=SkillLearningSettings(
            _env_file=None,
            skill_learning_enabled=False,
            skill_learning_data_dir=tmp_path / "skill-learning",
        ),
    )


async def test_application_wires_deferred_subagent_delegation(
    tmp_path: Path,
) -> None:
    config = ProviderConfig(
        provider="main",
        model="main-model",
        api_key=SecretStr("offline-test-key"),
        api_style=ApiStyle.CHAT_COMPLETIONS,
    )
    adapter = FakeModelAdapter(
        config,
        [
            # 1) 父：tool_search 激活 subagent_run
            model_response(
                usage=usage(10),
                tool_calls=(
                    tool_call(
                        "tool_search",
                        {"query": "subagent 子代理 委派"},
                        call_id="t1",
                    ),
                ),
            ),
            # 2) 父：委派
            model_response(
                usage=usage(10),
                tool_calls=(
                    tool_call(
                        "subagent_run",
                        {
                            "ticket": {
                                "goal": "读取 notes.txt 并给出结论",
                                "questions": ["notes.txt 的内容是什么"],
                                "scope_paths": ["notes.txt"],
                                "done_when": "给出结论与文件定位",
                            },
                            "grants": ["read_repo"],
                            "memory": "none",
                        },
                        call_id="p1",
                    ),
                ),
            ),
            # 3) 子：读文件
            model_response(
                usage=usage(20),
                tool_calls=(
                    tool_call("read_file", {"path": "notes.txt"}, call_id="s1"),
                ),
            ),
            # 4) 子：记录发现
            model_response(
                usage=usage(20),
                tool_calls=(
                    tool_call(
                        "record_finding",
                        {
                            "type": "exists",
                            "claim": "notes.txt 存在且内容已核对。",
                            "locations": [
                                {
                                    "path": "notes.txt",
                                    "lines": "1",
                                    "symbol": "hello",
                                }
                            ],
                        },
                        call_id="s2",
                    ),
                ),
            ),
            # 5) 子：提交
            model_response(
                usage=usage(20),
                tool_calls=(
                    tool_call(
                        "submit_result",
                        {"summary": "notes.txt 存在。"},
                        call_id="s3",
                    ),
                ),
            ),
            # 6) 父：终结
            model_response(usage=usage(10), content="调查完成。"),
        ],
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("hello", encoding="utf-8")

    app = _application(tmp_path, adapter)
    await app.start()
    try:
        assert app.subagent_runner is not None
        assert app.tool_registry is not None
        assert app.tool_registry.is_deferred("subagent_run")
        assert "subagent_run" in app.system_prompt

        events = InMemoryEventHandler()
        result = await app.runtime.run(
            "调查 notes.txt",
            conversation_id="conv-wiring",
            event_handler=events,
        )

        assert result.ok is True
        assert result.content == "调查完成。"
        # 延迟工具：激活前不可见，tool_search 命中后下一步可见完整定义。
        first_tools = {tool.name for tool in adapter.requests[0].tools}
        assert "tool_search" in first_tools
        assert "subagent_run" not in first_tools
        second_tools = {tool.name for tool in adapter.requests[1].tools}
        assert "subagent_run" in second_tools

        payload = json.loads(result.tool_calls[1].result.output)
        assert payload["submitted"] is True
        assert payload["status"] == "complete"
        assert payload["summary"] == "notes.txt 存在。"
        assert [item["finding_id"] for item in payload["findings"]] == ["f1"]
        assert payload["grants"] == ["read_repo"]

        types = [event.type for event in events.events]
        assert AgentEventType.SUBAGENT_STARTED in types
        assert AgentEventType.SUBAGENT_COMPLETED in types
    finally:
        await app.close()
