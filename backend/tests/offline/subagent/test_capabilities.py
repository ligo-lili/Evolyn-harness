"""能力目录：解析、规范化、校验与 PLAN 降级。"""

from __future__ import annotations

import pytest

from app.subagent import (
    CapabilityError,
    apply_plan_downgrade,
    parse_grants,
)


def test_parse_grants_normalizes_and_dedupes() -> None:
    grants = parse_grants(
        [
            " read_repo:src/utils/ ",
            "read_repo:src/utils",
            "WEB_SEARCH",
            "web_search",
            "web_fetch:Docs.Python.ORG, docs.python.org",
        ]
    )

    assert [grant.spec for grant in grants] == [
        "read_repo:src/utils",
        "web_search",
        "web_fetch:docs.python.org",
    ]
    assert grants[0].requested == "read_repo:src/utils/"
    assert grants[2].argument == "docs.python.org"


def test_read_repo_argument_is_optional_and_normalized() -> None:
    assert parse_grants(["read_repo"])[0].spec == "read_repo"
    assert parse_grants(["read_repo:."])[0].spec == "read_repo"
    assert parse_grants(["read_repo:src\\utils"])[0].spec == "read_repo:src/utils"


@pytest.mark.parametrize(
    "raw_grants",
    [
        [],
        ["run_shell"],
        ["web_fetch"],
        ["web_fetch:"],
        ["read_repo:"],
        ["web_search: "],
        ["web_fetch:http://docs.python.org"],
        ["web_fetch:docs.python.org:8080"],
        ["read_repo:../secrets"],
        ["read_repo:/etc"],
        ["read_repo:C:/Windows"],
        ["web_search:extra"],
        [""],
        [42],
    ],
)
def test_parse_grants_rejects_invalid_declarations(raw_grants) -> None:
    with pytest.raises(CapabilityError):
        parse_grants(raw_grants)


def test_web_fetch_caps_host_count() -> None:
    hosts = ",".join(f"a{index}.example.com" for index in range(9))
    with pytest.raises(CapabilityError):
        parse_grants([f"web_fetch:{hosts}"])


def test_plan_downgrade_keeps_readonly_and_drops_web_fetch() -> None:
    requested = parse_grants(
        ["read_repo:src", "web_search", "web_fetch:docs.python.org"]
    )

    effective, downgrades = apply_plan_downgrade(requested)

    assert [grant.spec for grant in effective] == ["read_repo:src", "web_search"]
    assert len(downgrades) == 1
    downgrade = downgrades[0]
    assert downgrade.requested == "web_fetch:docs.python.org"
    assert downgrade.effective is None
    assert downgrade.reason == "plan_mode"


def test_plan_downgrade_without_dropped_grants_is_empty() -> None:
    effective, downgrades = apply_plan_downgrade(parse_grants(["read_repo"]))

    assert [grant.spec for grant in effective] == ["read_repo"]
    assert downgrades == ()
