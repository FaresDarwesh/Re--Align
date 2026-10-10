"""OpenAI-compatible Chat Completions backend.

Works with: OpenAI, OpenRouter, Groq, Together, Fireworks, vLLM, LM Studio,
Ollama (/v1), LiteLLM, and anything else speaking the same schema.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from realign.core.errors import LLMError
from realign.core.logging import get_logger
from realign.llm.base import LLMProvider, LLMRequest, LLMResponse
from realign.llm.pricing import compute_cost_micros

log = get_logger("realign.llm.openai")


class OpenAICompatProvider(LLMProvider):
    name = "openai"

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://api.openai.com/v1",
        thinking_model: str = "gpt-4o",
        decision_model: str = "gpt-4o-mini",
        timeout: int = 120,
        max_retries: int = 3,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(timeout=timeout, max_retries=max_retries)
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.thinking_model = thinking_model
        self.decision_model = decision_model
        self.extra_headers = extra_headers or {}

    @property
    def configured(self) -> bool:
        return bool(self.api_key) or "localhost" in self.base_url or "127.0.0.1" in self.base_url

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _payload(self, request: LLMRequest, model: str) -> dict[str, Any]:
        messages: list[dict[str, str]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        for m in request.messages:
            if m.content:
                messages.append({"role": m.role, "content": m.content})
        if not messages:
            raise LLMError("لا توجد رسائل لإرسالها للنموذج")

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": False,
        }
        if request.stop:
            payload["stop"] = request.stop
        if request.json_mode and self.supports_json_mode:
            payload["response_format"] = {"type": "json_object"}
            # Guarantee the word "json" appears (OpenAI requirement)
            if "json" not in messages[-1]["content"].lower():
                messages[-1]["content"] += "\n\nأجب بصيغة JSON صالحة فقط."
            payload["messages"] = messages
        return payload

    def _parse(self, data: dict[str, Any], request: LLMRequest, model: str, latency_ms: int) -> LLMResponse:
        choices = data.get("choices") or []
        text = ""
        finish = ""
        if choices:
            msg = choices[0].get("message", {})
            text = msg.get("content") or ""
            finish = choices[0].get("finish_reason") or ""
        usage = data.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or 0)
        if not tokens_in and not tokens_out:
            tokens_in = max(len(request.system or "") // 4, 1)
            for m in request.messages:
                tokens_in += max(len(m.content) // 4, 1)
            tokens_out = max(len(text) // 4, 1)

        json_data = None
        if request.json_mode and text:
            json_data = _try_parse_json(text)

        return LLMResponse(
            text=text,
            json_data=json_data,
            model=data.get("model") or model,
            provider=self.name,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_micros=compute_cost_micros(model, tokens_in, tokens_out, self.name),
            latency_ms=latency_ms,
            finish_reason=finish,
            raw={"usage": usage},
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if not self.configured:
            raise LLMError("موفّر النموذج غير مُعد: أضف مفتاح API من الإعدادات.")
        model = request.model or self.thinking_model
        payload = self._payload(request, model)
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    started = asyncio.get_event_loop().time()
                    resp = await client.post(
                        f"{self.base_url}/chat/completions", headers=self._headers(), json=payload
                    )
                    latency_ms = int((asyncio.get_event_loop().time() - started) * 1000)
                if resp.status_code >= 400:
                    detail = resp.text[:500]
                    if resp.status_code in (408, 409, 429) or resp.status_code >= 500:
                        raise LLMError(f"HTTP {resp.status_code}: {detail}")
                    raise LLMError(f"خطأ من مزوّد النموذج ({resp.status_code}): {detail}")
                return self._parse(resp.json(), request, model, latency_ms)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                log.warning("llm_transport_retry", attempt=attempt, error=str(exc))
            except LLMError as exc:
                last_error = exc
                if "HTTP 4" in str(exc) and "429" not in str(exc) and "408" not in str(exc):
                    raise
                log.warning("llm_retry", attempt=attempt, error=str(exc))
            await asyncio.sleep(min(2**attempt, 15))

        raise LLMError(f"فشل الاتصال بمزوّد النموذج بعد {self.max_retries} محاولات: {last_error}")

    async def health(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "configured": self.configured,
            "base_url": self.base_url,
            "thinking_model": self.thinking_model,
            "decision_model": self.decision_model,
        }


def _try_parse_json(text: str) -> Any | None:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1:
        start, end = cleaned.find("["), cleaned.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(cleaned[start : end + 1])
    except Exception:  # noqa: BLE001
        return None
