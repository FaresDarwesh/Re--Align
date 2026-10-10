"""Native Anthropic Messages API backend."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from realign.core.errors import LLMError
from realign.llm.base import LLMProvider, LLMRequest, LLMResponse
from realign.llm.pricing import compute_cost_micros

ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://api.anthropic.com",
        thinking_model: str = "claude-sonnet-4-5",
        decision_model: str = "claude-haiku-4-5",
        timeout: int = 180,
        max_retries: int = 3,
    ) -> None:
        super().__init__(timeout=timeout, max_retries=max_retries)
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.thinking_model = thinking_model
        self.decision_model = decision_model

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if not self.configured:
            raise LLMError("موفّر Anthropic غير مُعد: أضف ANTHROPIC_API_KEY.")

        model = request.model or self.thinking_model
        messages = [{"role": m.role, "content": m.content} for m in request.messages if m.content]
        if not messages:
            raise LLMError("لا توجد رسائل لإرسالها للنموذج")

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.system:
            payload["system"] = request.system
        if request.json_mode:
            payload["system"] = (
                (payload.get("system", "") + "\n\nأجب بصيغة JSON صالحة فقط بدون أي نص إضافي.").strip()
            )
            messages[-1]["content"] += "\n\nJSON:"
            payload["messages"] = messages

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    started = asyncio.get_event_loop().time()
                    resp = await client.post(f"{self.base_url}/v1/messages", headers=headers, json=payload)
                    latency_ms = int((asyncio.get_event_loop().time() - started) * 1000)
                if resp.status_code >= 400:
                    detail = resp.text[:400]
                    if resp.status_code in (429, 500, 502, 503, 529):
                        raise LLMError(f"HTTP {resp.status_code}: {detail}")
                    raise LLMError(f"خطأ من Anthropic ({resp.status_code}): {detail}")
                data = resp.json()

                parts = data.get("content") or []
                text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
                usage = data.get("usage") or {}
                tokens_in = int(usage.get("input_tokens") or 0)
                tokens_out = int(usage.get("output_tokens") or 0)
                json_data = None
                if request.json_mode and text:
                    try:
                        json_data = json.loads(text[text.find("{") : text.rfind("}") + 1])
                    except Exception:  # noqa: BLE001
                        json_data = None
                return LLMResponse(
                    text=text,
                    json_data=json_data,
                    model=data.get("model") or model,
                    provider=self.name,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_micros=compute_cost_micros(model, tokens_in, tokens_out, self.name),
                    latency_ms=latency_ms,
                    finish_reason=data.get("stop_reason") or "",
                    raw={"usage": usage},
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
            except LLMError as exc:
                last_error = exc
                if "HTTP 4" in str(exc) and "429" not in str(exc):
                    raise
            await asyncio.sleep(min(2**attempt, 15))

        raise LLMError(f"فشل الاتصال بـ Anthropic بعد {self.max_retries} محاولات: {last_error}")

    async def health(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "configured": self.configured,
            "thinking_model": self.thinking_model,
            "decision_model": self.decision_model,
        }
