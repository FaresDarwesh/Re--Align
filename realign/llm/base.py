"""Provider-agnostic LLM contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, Field


class LLMMessage(BaseModel):
    role: Literal["system", "user", "assistant"] = "user"
    content: str = ""


class LLMRequest(BaseModel):
    messages: list[LLMMessage]
    model: str = ""
    system: str = ""
    temperature: float = 0.3
    max_tokens: int = 2048
    json_mode: bool = False          # force valid JSON output
    json_schema: dict | None = None  # advisory hint (providers may ignore)
    stop: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    text: str = ""
    json_data: Any | None = None
    model: str = ""
    provider: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_micros: int = 0
    latency_ms: int = 0
    finish_reason: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out


class LLMProvider(ABC):
    """Minimal surface every backend must implement."""

    name: str = "base"
    supports_json_mode: bool = True

    def __init__(self, *, timeout: int = 120, max_retries: int = 3, **_: Any) -> None:
        self.timeout = timeout
        self.max_retries = max_retries

    @abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResponse:
        ...

    async def health(self) -> dict[str, Any]:
        return {"provider": self.name, "ok": True, "configured": True}
