"""Model routing + spend accounting.

Two ideas do the heavy lifting here:

1. **Routing by role.** `thinking` (writing/planning) goes to the big, expensive
   model. `decision` (tagging, scoring, routing) goes to the small cheap one.
   This is what keeps the cost of a 700-ad sweep at fractions of a cent.
2. **Hard budget enforcement.** Every call is metered through a `SpendTracker`
   with a ceiling; exceeding it raises before money is spent, not after.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from realign.config import Settings, settings
from realign.core.errors import BudgetExceeded, LLMError
from realign.core.logging import get_logger
from realign.core.security import decrypt_dict
from realign.core.utils import micros_to_credits, utcnow
from realign.llm.base import LLMMessage, LLMProvider, LLMRequest, LLMResponse
from realign.llm.pricing import compute_cost_micros
from realign.llm.providers import AnthropicProvider, MockProvider, OpenAICompatProvider
from realign.models import Organization, UsageRecord

log = get_logger("realign.llm.router")

Role = Literal["thinking", "decision"]


class SpendTracker:
    """Accumulates tokens/cost for a unit of work and enforces a ceiling."""

    def __init__(self, *, limit_micros: int | None = None, credit_value_cents: int = 1) -> None:
        self.limit_micros = limit_micros
        self.credit_value_cents = credit_value_cents
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_micros = 0
        self.calls = 0
        self.models: set[str] = set()

    @property
    def credits(self) -> int:
        return micros_to_credits(self.cost_micros, self.credit_value_cents)

    def check(self, estimated_micros: int = 0) -> None:
        if self.limit_micros is not None and self.cost_micros + estimated_micros > self.limit_micros:
            raise BudgetExceeded(
                "تم تجاوز حد الإنفصال المسموح لهذه المهمة",
                details={
                    "limit_micros": self.limit_micros,
                    "spent_micros": self.cost_micros,
                    "estimated_micros": estimated_micros,
                },
            )

    def record(self, response: LLMResponse) -> LLMResponse:
        self.cost_micros += response.cost_micros
        self.tokens_in += response.tokens_in
        self.tokens_out += response.tokens_out
        self.calls += 1
        if response.model:
            self.models.add(response.model)
        return response

    def snapshot(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_micros": self.cost_micros,
            "credits": self.credits,
            "models": sorted(self.models),
        }


class LLMRouter:
    def __init__(self, cfg: Settings | None = None) -> None:
        self.cfg = cfg or settings
        self._cache: dict[tuple[str, str | None], LLMProvider] = {}

    # ------------------------------------------------------------ providers --

    def _build_global(self, role: Role) -> LLMProvider:
        provider = (self.cfg.llm_thinking_provider if role == "thinking" else self.cfg.llm_decision_provider)
        provider = provider or self.cfg.llm_provider or "mock"
        if provider == "mock":
            return MockProvider()
        if provider == "anthropic":
            return AnthropicProvider(
                api_key=self.cfg.anthropic_api_key,
                thinking_model=self.cfg.anthropic_thinking_model,
                decision_model=self.cfg.anthropic_decision_model,
                timeout=self.cfg.llm_timeout_seconds,
                max_retries=self.cfg.llm_max_retries,
            )
        if provider == "ollama":
            return OpenAICompatProvider(
                api_key="ollama",
                base_url=f"{self.cfg.ollama_base_url.rstrip('/')}/v1",
                thinking_model=self.cfg.ollama_thinking_model,
                decision_model=self.cfg.ollama_decision_model,
                timeout=self.cfg.llm_timeout_seconds,
                max_retries=self.cfg.llm_max_retries,
            )
        if provider == "custom":
            return OpenAICompatProvider(
                api_key=self.cfg.custom_api_key,
                base_url=self.cfg.custom_base_url,
                thinking_model=self.cfg.custom_thinking_model or "custom-thinking",
                decision_model=self.cfg.custom_decision_model or "custom-decision",
                timeout=self.cfg.llm_timeout_seconds,
                max_retries=self.cfg.llm_max_retries,
            )
        # default: openai-compatible
        return OpenAICompatProvider(
            api_key=self.cfg.openai_api_key,
            base_url=self.cfg.openai_base_url,
            thinking_model=self.cfg.openai_thinking_model,
            decision_model=self.cfg.openai_decision_model,
            timeout=self.cfg.llm_timeout_seconds,
            max_retries=self.cfg.llm_max_retries,
        )

    def _build_for_org(self, org: Organization, role: Role) -> LLMProvider:
        """Bring-Your-Own-Key: the tenant pays its own provider bill."""
        if not self.cfg.allow_org_llm_keys or not org.llm_provider or not org.llm_api_key_enc:
            return self._build_global(role)
        from realign.core.security import decrypt_dict as _dec

        key = _dec({"k": org.llm_api_key_enc}).get("k") if org.llm_api_key_enc else ""
        provider = (org.llm_provider or "openai").lower()
        base = org.llm_base_url or ""
        model = (
            org.llm_thinking_model or self.cfg.openai_thinking_model
            if role == "thinking"
            else org.llm_decision_model or self.cfg.openai_decision_model
        )
        if provider == "anthropic":
            return AnthropicProvider(
                api_key=key,
                base_url=base or "https://api.anthropic.com",
                thinking_model=model,
                decision_model=model,
                timeout=self.cfg.llm_timeout_seconds,
                max_retries=self.cfg.llm_max_retries,
            )
        return OpenAICompatProvider(
            api_key=key,
            base_url=base or self.cfg.openai_base_url,
            thinking_model=model,
            decision_model=model,
            timeout=self.cfg.llm_timeout_seconds,
            max_retries=self.cfg.llm_max_retries,
        )

    def provider_for(self, role: Role, org: Organization | None = None) -> LLMProvider:
        cache_key = (role, org.id if org else None)
        if cache_key not in self._cache:
            self._cache[cache_key] = (
                self._build_for_org(org, role) if org is not None else self._build_global(role)
            )
        return self._cache[cache_key]

    def invalidate(self, org_id: str | None = None) -> None:
        for key in [k for k in self._cache if org_id is None or k[1] == org_id]:
            self._cache.pop(key, None)

    def model_for(self, role: Role, org: Organization | None = None) -> str:
        provider = self.provider_for(role, org)
        if hasattr(provider, "thinking_model"):
            return provider.thinking_model if role == "thinking" else provider.decision_model  # type: ignore[attr-defined]
        return f"{provider.name}-{role}"

    # ---------------------------------------------------------------- calls --

    @staticmethod
    def _request(
        *,
        messages: list[LLMMessage] | str,
        system: str,
        role: Role,
        model: str,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
        schema: dict | None,
        task: str,
    ) -> LLMRequest:
        if isinstance(messages, str):
            messages = [LLMMessage(role="user", content=messages)]
        return LLMRequest(
            messages=messages,
            system=system,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
            json_schema=schema,
            metadata={"task": task, "role": role},
        )

    async def call(
        self,
        *,
        role: Role,
        messages: list[LLMMessage] | str,
        system: str = "",
        org: Organization | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
        schema: dict | None = None,
        task: str = "generic",
        tracker: SpendTracker | None = None,
    ) -> LLMResponse:
        provider = self.provider_for(role, org)
        request = self._request(
            messages=messages,
            system=system,
            role=role,
            model=model or self.model_for(role, org),
            temperature=0.7 if role == "thinking" else 0.0 if temperature is None else temperature,
            max_tokens=max_tokens or self.cfg.llm_max_output_tokens,
            json_mode=json_mode,
            schema=schema,
            task=task,
        )
        if tracker:
            tracker.check()
        try:
            response = await provider.complete(request)
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"فشل استدعاء النموذج: {exc}") from exc

        if response.cost_micros == 0 and (response.tokens_in or response.tokens_out):
            response.cost_micros = compute_cost_micros(
                response.model, response.tokens_in, response.tokens_out, provider.name
            )
        if tracker:
            tracker.record(response)
        return response

    async def thinking(self, **kwargs: Any) -> LLMResponse:
        return await self.call(role="thinking", **kwargs)

    async def decision(self, **kwargs: Any) -> LLMResponse:
        return await self.call(role="decision", **kwargs)

    async def decide(
        self,
        *,
        task: str,
        prompt: str,
        schema: dict,
        system: str = "",
        org: Organization | None = None,
        tracker: SpendTracker | None = None,
        fallback: dict | None = None,
    ) -> dict:
        """Typed judgment from the cheap model. Always returns a dict.

        On any failure it degrades to `fallback` (or a conservative default)
        and — critically — the caller must treat a failed judgment as
        "send to a human", never as "go ahead".
        """
        response = await self.decision(
            messages=prompt,
            system=system or "أنت مصنّف دقيق. أجب بصيغة JSON فقط مطابق للمخطط المطلوب.",
            task=task,
            json_mode=True,
            schema=schema,
            temperature=0.0,
            max_tokens=800,
            org=org,
            tracker=tracker,
        )
        data = response.json_data
        if isinstance(data, dict):
            return data
        try:
            parsed = json.loads((response.text or "").strip())
            if isinstance(parsed, dict):
                return parsed
        except Exception:  # noqa: BLE001
            pass
        log.warning("decision_parse_failed", task=task)
        return dict(fallback or {})

    async def health(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for role in ("thinking", "decision"):
            provider = self.provider_for(role)
            try:
                out[role] = await provider.health()
            except Exception as exc:  # noqa: BLE001
                out[role] = {"provider": provider.name, "ok": False, "error": str(exc)}
            out[role]["model"] = self.model_for(role)
        return out


router = LLMRouter()


# --------------------------------------------------------------- metering ---


async def persist_usage(
    session: AsyncSession,
    *,
    org_id: str,
    tracker: SpendTracker,
    run_id: str | None = None,
    user_id: str | None = None,
    agent_key: str | None = None,
    kind: str = "llm",
) -> None:
    """Write the ledger row + decrement the org balance for consumed credits."""
    if tracker.calls == 0 and tracker.cost_micros == 0:
        return
    record = UsageRecord(
        org_id=org_id,
        user_id=user_id,
        run_id=run_id,
        agent_key=agent_key,
        kind=kind,
        provider="router",
        model=",".join(sorted(tracker.models)) or "-",
        tokens_in=tracker.tokens_in,
        tokens_out=tracker.tokens_out,
        cost_micros=tracker.cost_micros,
        credits=tracker.credits,
        meta=tracker.snapshot(),
    )
    session.add(record)

    result = await session.execute(select(Organization).where(Organization.id == org_id))
    org = result.scalar_one_or_none()
    if org is not None and tracker.credits:
        org.credits_used = int(org.credits_used or 0) + tracker.credits
    log.info(
        "usage_recorded",
        org_id=org_id,
        run_id=run_id,
        agent=agent_key,
        cost_micros=tracker.cost_micros,
        credits=tracker.credits,
    )


async def monthly_usage(session: AsyncSession, org_id: str, since=None) -> dict[str, Any]:
    from sqlalchemy import func as sa_func

    since = since or utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    row = (
        await session.execute(
            select(
                sa_func.coalesce(sa_func.sum(UsageRecord.cost_micros), 0),
                sa_func.coalesce(sa_func.sum(UsageRecord.credits), 0),
                sa_func.coalesce(sa_func.count(UsageRecord.id), 0),
            ).where(UsageRecord.org_id == org_id, UsageRecord.created_at >= since)
        )
    ).one()
    return {"cost_micros": int(row[0]), "credits": int(row[1]), "records": int(row[2])}
