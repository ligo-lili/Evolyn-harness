"""Sub-Agent V1 配置（环境变量前缀 ``SUBAGENT_``）。"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.agent.budget import RunBudgetConfig

_BACKEND_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class SubagentSettings(BaseSettings):
    """子代理的独立子预算、并发与结果边界。"""

    model_config = SettingsConfigDict(
        env_file=_BACKEND_ENV_FILE,
        env_file_encoding="utf-8",
        env_prefix="SUBAGENT_",
        extra="ignore",
    )

    enabled: bool = True
    max_steps: int = Field(default=8, ge=1)
    max_tool_rounds: int = Field(default=8, ge=1)
    wall_clock_seconds: float = Field(default=180.0, gt=0, le=900)
    token_budget: int = Field(default=60_000, ge=1_000)
    max_concurrency: int = Field(default=3, ge=1, le=8)
    max_invocations_per_run: int = Field(default=4, ge=1)
    max_result_chars: int = Field(default=8_000, ge=500)
    conclusion_max_chars: int = Field(default=4_000, ge=200)

    def run_budget_config(self) -> RunBudgetConfig:
        """子预算三档：80% 预警、95% 收口、100% 硬停。

        子代理的 token 与 model calls 由父 Run 统一累计（结果回流），这里只
        决定子循环自身的收口节奏；调用上限按步数 + 收尾余量推导。
        """

        warning = max(1, int(self.token_budget * 0.8))
        finalization = max(2, int(self.token_budget * 0.95))
        if finalization <= warning:
            finalization = warning + 1
        return RunBudgetConfig(
            enabled=True,
            warning_tokens=warning,
            finalization_tokens=finalization,
            hard_tokens=self.token_budget,
            warning_model_calls=None,
            finalization_model_calls=None,
            hard_model_calls=self.max_steps + 2,
            finalization_max_output_tokens=1_200,
        )


__all__ = ["SubagentSettings"]
