"""离线自检共享的 Fake Adapter 与响应构造。"""

from __future__ import annotations

from pydantic import SecretStr

from app.models.adapter import ModelAdapter
from app.models.config import ProviderConfig
from app.models.types import (
    ApiStyle,
    Message,
    MessageRole,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ToolCall,
)

FAKE_PROVIDER = "fake"
FAKE_MODEL = "fake-model"


def fake_provider_config() -> ProviderConfig:
    return ProviderConfig(
        provider=FAKE_PROVIDER,
        model=FAKE_MODEL,
        api_key=SecretStr("offline-test-key"),
        api_style=ApiStyle.CHAT_COMPLETIONS,
    )


class FakeModelAdapter(ModelAdapter):
    """按脚本顺序返回响应；同一实例同时服务父循环与子循环。"""

    def __init__(
        self,
        config: ProviderConfig,
        responses: list[ModelResponse],
    ) -> None:
        super().__init__(config)
        self.responses = responses
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.responses.pop(0)

    async def close(self) -> None:
        pass


def response(
    *,
    content: str | None = None,
    tool_calls: tuple[ToolCall, ...] = (),
    input_tokens: int = 100,
    output_tokens: int = 10,
) -> ModelResponse:
    return ModelResponse(
        id="fake-response",
        provider=FAKE_PROVIDER,
        model=FAKE_MODEL,
        message=Message(
            role=MessageRole.ASSISTANT,
            content=content,
            tool_calls=tool_calls,
        ),
        usage=ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            model_calls=1,
        ),
    )


__all__ = [
    "FAKE_MODEL",
    "FAKE_PROVIDER",
    "FakeModelAdapter",
    "fake_provider_config",
    "response",
]
