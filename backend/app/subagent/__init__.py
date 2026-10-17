"""Sub-Agent V1：主代理把有边界的调查任务委派给隔离的子代理。"""

from app.tools.builtin.explore import GlobTool, GrepTool

from .capabilities import (
    CAPABILITY_CATALOG,
    CapabilityError,
    CapabilityGrant,
    GrantDowngrade,
    apply_plan_downgrade,
    parse_grants,
)
from .explore_tools import (
    GitDiffTool,
    GitLogTool,
    GitShowTool,
    GitStatusTool,
)
from .findings import (
    FINDING_LIMITS,
    MAX_SCOPE_VIOLATIONS,
    RECORD_FINDING_TOOL_NAME,
    FindingEntry,
    FindingLedger,
    RecordFindingTool,
)
from .redaction import (
    is_sensitive_path,
    redact_secrets,
    sensitive_exclusion,
    sensitive_rejection_text,
)
from .runner import SUBAGENT_BUDGET_MESSAGES, SubAgentOutcome, SubAgentRunner
from .scoped_tools import (
    ScopedListFilesTool,
    ScopedReadFileTool,
    ScopedWebFetchTool,
    build_scoped_tools,
    read_grant_prefixes,
)
from .settings import TIER_BUDGETS, SubagentSettings, TierBudget
from .submit import SUBMIT_RESULT_TOOL_NAME, SubmitResultTool
from .ticket import (
    DEFAULT_TIER,
    EVIDENCE_TYPES,
    TIERS,
    Ticket,
    TicketError,
    intersect_prefixes,
    minimize_paths,
    normalize_scope_path,
    parse_ticket,
    render_task_brief,
)
from .tool import SUBAGENT_TOOL_NAME, SubAgentTool
from .untrusted import (
    UNTRUSTED_CLOSE,
    UNTRUSTED_OPEN,
    UntrustedContentTool,
    wrap_untrusted,
)

__all__ = [
    "CAPABILITY_CATALOG",
    "DEFAULT_TIER",
    "EVIDENCE_TYPES",
    "FINDING_LIMITS",
    "MAX_SCOPE_VIOLATIONS",
    "RECORD_FINDING_TOOL_NAME",
    "SUBAGENT_BUDGET_MESSAGES",
    "SUBAGENT_TOOL_NAME",
    "SUBMIT_RESULT_TOOL_NAME",
    "TIERS",
    "TIER_BUDGETS",
    "UNTRUSTED_CLOSE",
    "UNTRUSTED_OPEN",
    "CapabilityError",
    "CapabilityGrant",
    "FindingEntry",
    "FindingLedger",
    "GitDiffTool",
    "GitLogTool",
    "GitShowTool",
    "GitStatusTool",
    "GlobTool",
    "GrantDowngrade",
    "GrepTool",
    "RecordFindingTool",
    "ScopedListFilesTool",
    "ScopedReadFileTool",
    "ScopedWebFetchTool",
    "SubAgentOutcome",
    "SubAgentRunner",
    "SubAgentTool",
    "SubmitResultTool",
    "SubagentSettings",
    "Ticket",
    "TicketError",
    "TierBudget",
    "UntrustedContentTool",
    "apply_plan_downgrade",
    "build_scoped_tools",
    "intersect_prefixes",
    "is_sensitive_path",
    "minimize_paths",
    "normalize_scope_path",
    "parse_grants",
    "parse_ticket",
    "read_grant_prefixes",
    "redact_secrets",
    "render_task_brief",
    "sensitive_exclusion",
    "sensitive_rejection_text",
    "wrap_untrusted",
]
