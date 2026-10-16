"""任务单（ticket）：解析校验、范围收窄与执行层强制。"""

from __future__ import annotations

import json

import pytest

from app.models.types import AgentMode, MessageRole
from app.subagent import (
    EVIDENCE_TYPES,
    CapabilityError,
    SubagentSettings,
    Ticket,
    TicketError,
    intersect_prefixes,
    minimize_paths,
    parse_ticket,
    render_task_brief,
)
from tests.offline.subagent.harness import (
    make_ticket,
    model_response,
    ticket_args,
    tool_call,
    usage,
)


def test_parse_ticket_normalizes_and_applies_defaults() -> None:
    ticket = parse_ticket(
        {
            "goal": "  调查认证流程  ",
            "questions": [" 哪些入口绕过会话校验？ "],
            "scope_paths": ["src\\auth\\", "src"],
            "done_when": "每个问题都有结论与文件定位",
        }
    )

    assert isinstance(ticket, Ticket)
    assert ticket.goal == "调查认证流程"
    assert ticket.questions == ("哪些入口绕过会话校验？",)
    # 反斜杠归一化 + 被父路径包含的子路径折叠。
    assert ticket.scope_paths == ("src",)
    assert ticket.known_facts == ()
    assert ticket.done_when == "每个问题都有结论与文件定位"
    assert ticket.tier == "medium"
    assert ticket.evidence_types == EVIDENCE_TYPES
    assert ticket.scope_is_repo_root is False
    assert ticket.scope_text() == "src/"


def test_parse_ticket_missing_required_fields() -> None:
    with pytest.raises(TicketError, match="goal"):
        parse_ticket(
            {
                "questions": ["q"],
                "scope_paths": ["src"],
                "done_when": "d",
            }
        )
    with pytest.raises(TicketError, match="questions"):
        parse_ticket(
            {"goal": "g", "scope_paths": ["src"], "done_when": "d"}
        )
    with pytest.raises(TicketError, match="scope_paths"):
        parse_ticket({"goal": "g", "questions": ["q"], "done_when": "d"})
    with pytest.raises(TicketError, match="done_when"):
        parse_ticket({"goal": "g", "questions": ["q"], "scope_paths": ["s"]})
    with pytest.raises(TicketError, match="ticket"):
        parse_ticket(["not", "a", "dict"])


@pytest.mark.parametrize(
    "overrides",
    [
        {"goal": ""},
        {"goal": None},
        {"questions": []},
        {"questions": ["ok", ""]},
        {"questions": "not-a-list"},
        {"scope_paths": []},
        {"scope_paths": [".."]},
        {"scope_paths": ["src/../lib"]},
        {"scope_paths": ["/abs/path"]},
        {"scope_paths": ["C:/abs"]},
        {"scope_paths": ["."]},
        {"scope_paths": [""]},
        {"done_when": "  "},
        {"known_facts": ["ok", 1]},
        {"tier": "huge"},
        {"tier": 3},
        {"evidence_types": ["exists", "nope"]},
        {"evidence_types": []},
    ],
)
def test_parse_ticket_rejects_invalid_fields(overrides: dict) -> None:
    with pytest.raises(TicketError):
        parse_ticket(ticket_args(**overrides))


def test_parse_ticket_enforces_length_limits() -> None:
    with pytest.raises(TicketError, match="goal"):
        parse_ticket(ticket_args(goal="x" * 301))
    with pytest.raises(TicketError, match="questions"):
        parse_ticket(ticket_args(questions=["x" * 501]))
    with pytest.raises(TicketError, match="questions"):
        parse_ticket(ticket_args(questions=[f"q{index}" for index in range(13)]))
    with pytest.raises(TicketError, match="scope_paths"):
        parse_ticket(
            ticket_args(scope_paths=[f"dir-{index}" for index in range(17)])
        )


def test_repo_root_scope_requires_explicit_opt_in() -> None:
    with pytest.raises(TicketError, match="仓库根"):
        parse_ticket(ticket_args(scope_paths=["."]))

    ticket = parse_ticket(ticket_args(scope_paths=["."]), allow_repo_root=True)
    assert ticket.scope_is_repo_root is True
    assert ticket.scope_text() == "整个仓库（显式声明）"

    # 与仓库根混排时折叠为根（根已覆盖其他路径）。
    mixed = parse_ticket(
        ticket_args(scope_paths=["src", "."]), allow_repo_root=True
    )
    assert mixed.scope_paths == (".",)


def test_evidence_types_and_tier_subset_validation() -> None:
    ticket = parse_ticket(
        ticket_args(
            scope_paths=["src"],
            evidence_types=["absent", "exists", "absent"],
        )
    )
    assert ticket.evidence_types == ("absent", "exists")
    ticket = parse_ticket(ticket_args(scope_paths=["src"], tier="thorough"))
    assert ticket.tier == "thorough"


def test_intersect_prefixes_semantics() -> None:
    assert intersect_prefixes(None, None) is None
    assert intersect_prefixes(("src",), None) == ("src",)
    assert intersect_prefixes(None, ("src",)) == ("src",)
    # 相互包含：结果取更窄的一方。
    assert intersect_prefixes(("src",), ("src/auth",)) == ("src/auth",)
    assert intersect_prefixes(("src/auth",), ("src",)) == ("src/auth",)
    # 仅有公共祖先（互不包含）不构成授权。
    assert intersect_prefixes(("src/auth",), ("src/db",)) == ()
    assert intersect_prefixes(("lib",), ("src",)) == ()


def test_minimize_paths_folds_nested_entries() -> None:
    assert minimize_paths(("src/auth", "src", "lib")) == ("lib", "src")
    assert minimize_paths(("src", "src")) == ("src",)
    assert minimize_paths(("src/auth", "src/auth/deep")) == ("src/auth",)


def test_render_task_brief_carries_mission_and_scope() -> None:
    ticket = make_ticket(
        goal="审计会话校验",
        questions=["哪些入口绕过校验？"],
        scope_paths=["src"],
        known_facts=["handle_profile 可疑"],
        evidence_types=["exists", "absent"],
        tier="quick",
    )

    brief = render_task_brief(ticket, ("src/auth",))

    assert "委派任务单" in brief
    assert "审计会话校验" in brief
    assert "1. 哪些入口绕过校验？" in brief
    assert "src/auth/" in brief
    assert "handle_profile 可疑" in brief
    assert "exists、absent" in brief
    assert "quick" in brief
    assert "整个仓库" not in brief


def test_continuation_of_is_validated_and_rendered() -> None:
    ticket = make_ticket(continuation_of="  sub-abc  ")
    assert ticket.continuation_of == "sub-abc"

    brief = render_task_brief(ticket, (".",))
    assert "续跑" in brief
    assert "sub-abc" in brief

    plain = make_ticket()
    assert plain.continuation_of is None
    assert "续跑" not in render_task_brief(plain, (".",))

    for bad in ("", "   ", 3, "x" * 81):
        with pytest.raises(TicketError, match="continuation_of"):
            parse_ticket(
                ticket_args(continuation_of=bad), allow_repo_root=True
            )


async def test_scope_outside_grants_is_rejected_before_start(
    build_harness,
) -> None:
    harness = build_harness([])

    with pytest.raises(CapabilityError, match="没有交集"):
        await harness.runner.run(
            parent_run_id="run-scope",
            conversation_id="conv",
            step=1,
            ticket=make_ticket(scope_paths=["src"]),
            grants=["read_repo:lib"],
            memory="none",
            mode=AgentMode.NORMAL,
        )

    # 拒绝发生在任何子模型调用与配额消耗之前。
    assert harness.adapter.requests == []
    assert harness.runner._invocations == {}


async def test_ticket_scope_is_enforced_in_sub_loop(build_harness) -> None:
    harness = build_harness(
        [
            model_response(
                usage=usage(10),
                tool_calls=(
                    tool_call(
                        "subagent_run",
                        {
                            "ticket": ticket_args(
                                goal="只调查 src",
                                scope_paths=["src"],
                            ),
                            "grants": ["read_repo"],
                            "memory": "none",
                        },
                        call_id="p1",
                    ),
                ),
            ),
            model_response(
                usage=usage(20),
                tool_calls=(
                    tool_call(
                        "read_file", {"path": "notes.txt"}, call_id="s1"
                    ),
                ),
            ),
            model_response(
                usage=usage(20),
                tool_calls=(
                    tool_call(
                        "read_file", {"path": "src/app.py"}, call_id="s2"
                    ),
                ),
            ),
            model_response(
                usage=usage(20),
                tool_calls=(
                    tool_call(
                        "submit_result",
                        {"summary": "src/app.py 已核对"},
                        call_id="s3",
                    ),
                ),
            ),
            model_response(usage=usage(10), content="调查完成。"),
        ],
        workspace_files={
            "notes.txt": "root-secret",
            "src/app.py": "print('app')",
        },
    )

    result = await harness.runtime.run("q")

    assert result.ok is True
    # 任务书里能看到执行层范围声明。
    first_user = [
        message.content or ""
        for message in harness.sub_requests()[0].messages
        if message.role is MessageRole.USER
    ]
    assert any("src/" in content for content in first_user)

    contents = "\n".join(
        message.content or ""
        for request in harness.sub_requests()
        for message in request.messages
    )
    # 越界读取被执行层拒绝；文件内容从未进入子循环。
    assert "outside the granted read_repo scope" in contents
    assert "root-secret" not in contents
    assert "print('app')" in contents

    payload = json.loads(result.tool_calls[0].result.output)
    assert payload["submitted"] is True


async def test_tool_rejects_repo_root_scope_by_default(build_harness) -> None:
    harness = build_harness(
        [
            model_response(
                usage=usage(10),
                tool_calls=(
                    tool_call(
                        "subagent_run",
                        {"ticket": ticket_args(), "grants": ["read_repo"]},
                        call_id="p1",
                    ),
                ),
            ),
            model_response(usage=usage(10), content="改为直接处理。"),
        ],
        settings=SubagentSettings(_env_file=None),  # 产品默认：拒绝仓库根
    )

    result = await harness.runtime.run("q")

    record = result.tool_calls[0]
    assert record.result.success is False
    assert "仓库根" in (record.result.error or "")
    # 确定性拒绝：没有子模型请求，也不消耗配额。
    assert len(harness.adapter.requests) == 2
    assert len(harness.sub_requests()) == 0
    assert harness.runner._invocations == {}
