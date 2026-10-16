"""Sub-Agent 配置（环境变量前缀 ``SUBAGENT_``）。

预算模型（v3 方案 5 节）：

- tier 三档（quick / medium / thorough）给出一组初始预算（Token / 步数 /
  墙钟 / finding 上限）；
- settings 里的对应字段是**部署级上限**，实际预算逐项取 ``min``，部署方
  可以整体调低而不能超过 tier 表；
- 阈值 80% 预警、90% 收口（只留交付工具）、100% 硬停；
- 同一父 Run 内全部子代理的 Token 跨委派累计，达到 ``run_token_budget``
  后拒绝新的委派。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.agent.budget import RunBudgetConfig

from .ticket import DEFAULT_TIER

_BACKEND_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


@dataclass(frozen=True, slots=True)
class TierBudget:
    """一个 tier 的初始预算（token / 步数 / 墙钟）。"""

    token_budget: int
    max_steps: int
    wall_clock_seconds: float


# v3 方案 5 节的初始值：上线后按成功任务的 P80-P90 消耗校准。
TIER_BUDGETS: dict[str, TierBudget] = {
    "quick": TierBudget(
        token_budget=40_000, max_steps=12, wall_clock_seconds=60.0
    ),
    "medium": TierBudget(
        token_budget=100_000, max_steps=25, wall_clock_seconds=240.0
    ),
    "thorough": TierBudget(
        token_budget=200_000, max_steps=45, wall_clock_seconds=600.0
    ),
}


class SubagentSettings(BaseSettings):
    """子代理的独立子预算、并发与结果边界。"""

    model_config = SettingsConfigDict(
        env_file=_BACKEND_ENV_FILE,
        env_file_encoding="utf-8",
        env_prefix="SUBAGENT_",
        extra="ignore",
    )

    enabled: bool = True
    # 以下四项是部署级上限（默认与 thorough 档持平）：本次委派的实际预算
    # 取 min(部署上限, tier 表)。
    max_steps: int = Field(default=45, ge=1)
    max_tool_rounds: int = Field(default=45, ge=1)
    wall_clock_seconds: float = Field(default=600.0, gt=0, le=900)
    token_budget: int = Field(default=200_000, ge=1_000)
    # 同一父 Run 内全部子代理的累计 Token 上限（跨委派，防并发绕过）。
    run_token_budget: int = Field(default=400_000, ge=1_000)
    max_concurrency: int = Field(default=3, ge=1, le=8)
    max_invocations_per_run: int = Field(default=4, ge=1)
    max_result_chars: int = Field(default=8_000, ge=500)
    # 任务单 scope_paths 默认不允许整个仓库根；部署方显式开启后才接受
    # scope_paths=["."]（整体扫描场景）。
    allow_repo_root: bool = False

    def budget_for(self, tier: str) -> TierBudget:
        """按 tier 取本次委派的实际预算：部署上限与 tier 表逐项取小。"""

        preset = TIER_BUDGETS.get(tier, TIER_BUDGETS[DEFAULT_TIER])
        return TierBudget(
            token_budget=min(self.token_budget, preset.token_budget),
            max_steps=min(self.max_steps, preset.max_steps),
            wall_clock_seconds=min(
                self.wall_clock_seconds, preset.wall_clock_seconds
            ),
        )

    def run_budget_config(self, tier: str = DEFAULT_TIER) -> RunBudgetConfig:
        """子预算三档：80% 预警、90% 收口（只留交付工具）、100% 硬停。

        子代理的 token 与 model calls 由父 Run 统一累计（结果回流），这里只
        决定子循环自身的收口节奏；调用上限按步数 + 收尾余量推导。
        """

        budget = self.budget_for(tier)
        warning = max(1, int(budget.token_budget * 0.8))
        finalization = max(2, int(budget.token_budget * 0.9))
        if finalization <= warning:
            finalization = warning + 1
        return RunBudgetConfig(
            enabled=True,
            warning_tokens=warning,
            finalization_tokens=finalization,
            hard_tokens=budget.token_budget,
            warning_model_calls=None,
            finalization_model_calls=None,
            hard_model_calls=budget.max_steps + 2,
            finalization_max_output_tokens=1_200,
        )


__all__ = ["TIER_BUDGETS", "SubagentSettings", "TierBudget"]
