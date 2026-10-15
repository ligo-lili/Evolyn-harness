"""Sub-Agent V1：主代理把有边界的调查任务委派给隔离的子代理。"""

from .capabilities import (
    CAPABILITY_CATALOG,
    CapabilityError,
    CapabilityGrant,
    GrantDowngrade,
    apply_plan_downgrade,
    parse_grants,
)
from .runner import SUBAGENT_BUDGET_MESSAGES, SubAgentOutcome, SubAgentRunner
from .scoped_tools import (
    ScopedListFilesTool,
    ScopedReadFileTool,
    ScopedWebFetchTool,
    build_scoped_tools,
)
from .settings import SubagentSettings
from .submit import SUBMIT_RESULT_TOOL_NAME, SubmitResultTool
from .tool import SUBAGENT_TOOL_NAME, SubAgentTool

__all__ = [
    "CAPABILITY_CATALOG",
    "SUBAGENT_BUDGET_MESSAGES",
    "SUBAGENT_TOOL_NAME",
    "SUBMIT_RESULT_TOOL_NAME",
    "CapabilityError",
    "CapabilityGrant",
    "GrantDowngrade",
    "ScopedListFilesTool",
    "ScopedReadFileTool",
    "ScopedWebFetchTool",
    "SubAgentOutcome",
    "SubAgentRunner",
    "SubAgentTool",
    "SubmitResultTool",
    "SubagentSettings",
    "apply_plan_downgrade",
    "build_scoped_tools",
    "parse_grants",
]
