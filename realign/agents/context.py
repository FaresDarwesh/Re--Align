"""Execution context handed to every agent.

It bundles: DB session, tenant, brand files, the run row, the spend tracker,
and the two model calls (think = big/expensive, decide = small/cheap).
Agents never touch the router or the DB directly for policy — they use this.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from realign.core.logging import get_logger
from realign.core.utils import truncate, utcnow
from realign.llm.base import LLMMessage
from realign.llm.router import SpendTracker, router
from realign.models import (
    Approval,
    BrandProfile,
    ContentAsset,
    Organization,
    Run,
)
from realign.models.operations import ApprovalStatus, AssetStatus, RunStatus

log = get_logger("realign.agents.context")


class AgentContext:
    def __init__(
        self,
        *,
        session: AsyncSession,
        org: Organization,
        run: Run,
        brand: BrandProfile | None = None,
        params: dict[str, Any] | None = None,
        user_id: str | None = None,
        dry_run: bool = False,
    ) -> None:
        self.session = session
        self.org = org
        self.run = run
        self.brand = brand
        self.params: dict[str, Any] = dict(params or {})
        self.user_id = user_id
        self.dry_run = dry_run
        self.tracker = SpendTracker(limit_micros=int(org.per_run_budget_cents or 200) * 10_000)
        self.steps: list[dict[str, Any]] = []
        self.assets: list[ContentAsset] = []
        self.approvals: list[Approval] = []
        self.warnings: list[str] = []
        self.metrics: dict[str, Any] = {}

    # ------------------------------------------------------------ helpers ---

    def step(self, name: str, detail: str = "", **extra: Any) -> None:
        entry = {
            "name": name,
            "detail": detail,
            "at": utcnow().isoformat(),
            **extra,
        }
        self.steps.append(entry)
        self.run.steps = list(self.steps)
        log.debug("agent_step", org=self.org.id, run=self.run.id, step=name, detail=detail[:120])

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        log.warning("agent_warning", org=self.org.id, run=self.run.id, message=message)

    @property
    def per_run_limit_micros(self) -> int:
        return int(self.org.per_run_budget_cents or 200) * 10_000

    # ----------------------------------------------------------- brand -----

    def brand_brief(self) -> str:
        """The 'brand files' block injected into every generation prompt."""
        b = self.brand
        if not b:
            return "لا يوجد ملف براند محدّد. استخدم لغة عربية واضحة ومحايدة."
        parts = [
            f"الشركة: {b.company_name or self.org.name}",
            f"المجال: {b.industry}" if b.industry else "",
            f"الجمهور المستهدف: {b.audience}" if b.audience else "",
            f"العرض: {b.offer}" if b.offer else "",
            f"نبرة الصوت: {b.voice}" if b.voice else "",
            f"كلمات مفتاحية: {', '.join(b.keywords)}" if b.keywords else "",
            f"قواعد صارمة: {b.rules}" if b.rules else "",
            f"ممنوع قوله: {b.never_say}" if b.never_say else "",
            f"هاشتاقات مفضلة: {', '.join(b.hashtags)}" if b.hashtags else "",
        ]
        return "\n".join(p for p in parts if p)

    # -------------------------------------------------------------- llm ----

    async def think(
        self,
        prompt: str | list[LLMMessage],
        *,
        system: str = "",
        task: str = "generic",
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> str:
        response = await router.thinking(
            messages=prompt,
            system=system,
            task=task,
            org=self.org,
            temperature=temperature,
            max_tokens=max_tokens,
            tracker=self.tracker,
        )
        return response.text or ""

    async def decide(
        self,
        *,
        task: str,
        prompt: str,
        schema: dict,
        system: str = "",
        fallback: dict | None = None,
    ) -> dict:
        return await router.decide(
            task=task,
            prompt=prompt,
            schema=schema,
            system=system,
            org=self.org,
            tracker=self.tracker,
            fallback=fallback,
        )

    def spend(self) -> dict[str, Any]:
        return self.tracker.snapshot()

    # ----------------------------------------------------------- outputs ---

    async def create_asset(
        self,
        *,
        kind: str,
        title: str = "",
        body: str = "",
        channel: str = "general",
        locale: str | None = None,
        summary: str = "",
        meta: dict | None = None,
        score: float = 0.0,
        seo_score: float = 0.0,
        brand_score: float = 0.0,
        auto_status: str | None = None,
    ) -> ContentAsset:
        asset = ContentAsset(
            org_id=self.org.id,
            run_id=self.run.id,
            agent_key=self.run.agent_key,
            kind=kind,
            title=title or truncate(body, 80),
            body=body,
            summary=summary or truncate(body, 200),
            channel=channel,
            locale=locale or self.org.locale,
            status=auto_status or AssetStatus.DRAFT,
            score=score,
            seo_score=seo_score,
            brand_score=brand_score,
            meta=meta or {},
        )
        self.session.add(asset)
        await self.session.flush()
        self.assets.append(asset)
        return asset

    async def request_approval(
        self,
        *,
        kind: str = "publish",
        title: str,
        body: str = "",
        payload: dict | None = None,
        asset: ContentAsset | None = None,
        channel: str | None = None,
        priority: int = 5,
        risk_score: float = 0.0,
        risk_flags: list[str] | None = None,
    ) -> Approval:
        """The human gate. Creates the pending row; the worker/API resumes it."""
        approval = Approval(
            org_id=self.org.id,
            run_id=self.run.id,
            agent_key=self.run.agent_key,
            kind=kind,
            title=title,
            body=body,
            channel=channel,
            payload=payload or {},
            asset_id=asset.id if asset else None,
            requested_by=self.user_id,
            priority=priority,
            risk_score=risk_score,
            risk_flags=risk_flags or [],
            status=ApprovalStatus.PENDING,
        )
        self.session.add(approval)
        await self.session.flush()
        self.approvals.append(approval)
        self.run.status = RunStatus.WAITING_APPROVAL
        self.step("approval_requested", title[:120], approval_id=approval.id, kind=kind)
        return approval

    async def load_asset(self, asset_id: str) -> ContentAsset | None:
        result = await self.session.execute(
            select(ContentAsset).where(ContentAsset.id == asset_id, ContentAsset.org_id == self.org.id)
        )
        return result.scalar_one_or_none()
