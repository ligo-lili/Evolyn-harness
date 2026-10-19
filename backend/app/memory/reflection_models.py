"""Post-Run Memory Reflection 的配置、输入、决策与结果模型。"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.models.types import ModelUsage

_BACKEND_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
_MEMORY_ID_RE = re.compile(r"^M[0-9]{3,}$")
# append 增量的防御性硬上限；应用层还会按 max_append_chars 配置再收紧。
_MAX_APPEND_CHARS = 4_000


class ReflectionAction(StrEnum):
    """一次 Reflection 允许产生的唯一普通 Memory 动作。"""

    NONE = "none"
    CREATE = "create"
    UPDATE = "update"
    # 只向已读记忆追加一段增量内容，避免为"补充事实"回显完整正文。
    APPEND = "append"


class MemoryReflectionConfig(BaseSettings):
    """独立于主 Agent 的 Reflection 模型配置。"""

    model_config = SettingsConfigDict(
        env_file=_BACKEND_ENV_FILE,
        env_file_encoding="utf-8",
        env_prefix="MEMORY_REFLECTION_",
        extra="ignore",
    )

    enabled: bool = True
    provider: str | None = None
    model: str | None = None
    max_output_tokens: int = Field(default=2_000, gt=0)
    max_attempts: int = Field(default=2, ge=1, le=2)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    timeout_seconds: float = Field(default=30.0, gt=0.0)
    # Reflector 输入瘦身：用户消息 / 最终答案 / 工具摘要各有独立预算，
    # 避免一次长 Run 把整段对话灌进反思模型。
    max_user_chars: int = Field(default=4_000, ge=1, le=20_000)
    max_answer_chars: int = Field(default=8_000, ge=1, le=40_000)
    max_tool_context_chars: int = Field(default=4_000, ge=0, le=20_000)
    # append 动作单段增量内容的字符上限：必须落在 max_output_tokens 能完整
    # 生成的范围内，否则预算护栏会直接拒绝该决策。
    max_append_chars: int = Field(default=1_500, gt=0)
    # CREATE 前的近重复探测：强重复自动合并（append），中相似只观测。
    # 阈值按 bge-m3 实测分布校准：应合并样本 0.76~0.93，无关记忆 ≤0.55，
    # 因此合并阈值取 0.72（留出余量），0.6~0.72 记录 suspect 事件后放行。
    dedup_enabled: bool = True
    dedup_threshold: float = Field(default=0.72, ge=0.0, lt=1.0)
    dedup_suspect_threshold: float = Field(default=0.6, ge=0.0, lt=1.0)
    capture_raw_io: bool = False
    gate_enabled: bool = True

    @field_validator("provider", "model", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("reflection provider and model must be strings")
        return value.strip() or None

    @model_validator(mode="after")
    def validate_dedup_thresholds(self) -> MemoryReflectionConfig:
        if self.dedup_suspect_threshold > self.dedup_threshold:
            raise ValueError(
                "dedup_suspect_threshold cannot exceed dedup_threshold"
            )
        return self


class MemoryReflectionInput(BaseModel):
    """Reflector 可见的本轮有界上下文。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    conversation_id: str | None = None
    user_input: str
    final_answer: str
    tool_context: tuple[str, ...] = ()
    recalled_memory_ids: tuple[str, ...] = ()
    core_memory: str = ""
    memory_index: str = ""
    task_context: str = ""

    @field_validator("recalled_memory_ids")
    @classmethod
    def normalize_recalled_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for value in values:
            memory_id = value.strip().upper()
            if not _MEMORY_ID_RE.fullmatch(memory_id):
                raise ValueError("recalled memory IDs must use the Mxxx format")
            if memory_id not in normalized:
                normalized.append(memory_id)
        return tuple(normalized)


class ReflectionDecision(BaseModel):
    """严格单动作输出；自由文本不能直接写入 Memory Store。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: ReflectionAction
    memory_id: str | None = None
    title: str | None = None
    summary: str | None = None
    content: str | None = None
    # 仅 append 使用：追加到已读记忆正文末尾的增量内容。
    addition: str | None = None
    reason: str

    @field_validator(
        "memory_id",
        "title",
        "summary",
        "content",
        "addition",
        mode="before",
    )
    @classmethod
    def normalize_optional_text(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("reflection decision text fields must be strings")
        return value.strip() or None

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_reason(cls, value: object) -> str:
        if not isinstance(value, str):
            raise TypeError("reflection reason must be a string")
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("reflection reason cannot be empty")
        return normalized

    @model_validator(mode="after")
    def validate_action_fields(self) -> ReflectionDecision:
        if self.action is ReflectionAction.NONE:
            if any(
                value is not None
                for value in (
                    self.memory_id,
                    self.title,
                    self.summary,
                    self.content,
                    self.addition,
                )
            ):
                raise ValueError("none decision cannot contain mutation fields")
            return self
        if self.action is ReflectionAction.CREATE:
            if self.memory_id is not None:
                raise ValueError("create decision cannot contain memory_id")
            if self.addition is not None:
                raise ValueError("create decision cannot contain addition")
            if not self.title or not self.summary or not self.content:
                raise ValueError("create decision requires title, summary, and content")
            return self
        if not self.memory_id or not _MEMORY_ID_RE.fullmatch(self.memory_id.upper()):
            raise ValueError(
                f"{self.action.value} decision requires a valid memory_id"
            )
        if self.action is ReflectionAction.UPDATE:
            if self.addition is not None:
                raise ValueError("update decision cannot contain addition")
            if not self.title or not self.summary or not self.content:
                raise ValueError(
                    "update decision requires title, summary, and complete content"
                )
            return self.model_copy(update={"memory_id": self.memory_id.upper()})
        # APPEND：只追加一段增量内容，标题、摘要与完整正文必须保持为 null。
        if any(
            value is not None for value in (self.title, self.summary, self.content)
        ):
            raise ValueError(
                "append decision cannot contain title, summary, or content"
            )
        if not self.addition:
            raise ValueError("append decision requires addition")
        if len(self.addition) > _MAX_APPEND_CHARS:
            raise ValueError(
                f"append addition exceeds {_MAX_APPEND_CHARS} characters"
            )
        return self.model_copy(update={"memory_id": self.memory_id.upper()})


class MemoryReflectionProposal(BaseModel):
    """Reflection 模型调用结果；是否真正写入由 Runtime 协调。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: ReflectionDecision | None = None
    provider: str | None = None
    model: str | None = None
    duration_ms: float = Field(default=0.0, ge=0.0)
    usage: ModelUsage = Field(default_factory=ModelUsage)
    attempts: int = Field(default=0, ge=0)
    finish_reason: str | None = None
    error: str | None = None
    input_json: str | None = None
    raw_output: str | None = None


__all__ = [
    "MemoryReflectionConfig",
    "MemoryReflectionInput",
    "MemoryReflectionProposal",
    "ReflectionAction",
    "ReflectionDecision",
]
