"""Platform administration (cross-tenant). Requires a platform admin account."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from realign.config import settings
from realign.core.errors import PermissionDenied
from realign.core.utils import utcnow
from realign.db import get_session
from realign.models import Invoice, Organization, Run, Subscription, User, UsageRecord
from realign.models.identity import OrgStatus
from realign.services.auth import Principal, get_principal

router = APIRouter(prefix="/admin", tags=["admin"])


async def require_admin(
    principal: Principal = Depends(get_principal),
) -> Principal:
    if not principal.is_platform_admin:
        raise PermissionDenied("هذه المنطقة للإدارة العامة فقط")
    return principal


class OrgStatusIn(BaseModel):
    status: str
    reason: str = ""


@router.get("/stats")
async def stats(
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    orgs = (
        await session.execute(
            select(Organization.status, sa_func.count(Organization.id)).group_by(Organization.status)
        )
    ).all()
    users = (await session.execute(select(sa_func.count(User.id)))).scalar_one()
    runs = (
        await session.execute(select(Run.status, sa_func.count(Run.id)).group_by(Run.status))
    ).all()
    cost = (await session.execute(select(sa_func.coalesce(sa_func.sum(UsageRecord.cost_micros), 0)))).scalar_one()
    revenue = (
        await session.execute(
            select(sa_func.coalesce(sa_func.sum(Invoice.amount_cents), 0)).where(Invoice.status == "paid")
        )
    ).scalar_one()
    return {
        "organizations": {k: int(v) for k, v in orgs},
        "users": int(users),
        "runs": {k: int(v) for k, v in runs},
        "total_ai_cost_usd": round(int(cost) / 1_000_000, 4),
        "revenue_cents": int(revenue),
        "generated_at": utcnow().isoformat(),
    }


@router.get("/organizations")
async def list_orgs(
    limit: int = Query(100, ge=1, le=500),
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = (
        await session.execute(select(Organization).order_by(Organization.created_at.desc()).limit(limit))
    ).scalars().all()
    out = []
    for org in rows:
        members = (
            await session.execute(select(sa_func.count(User.id)).where(User.org_id == org.id))
        ).scalar_one()
        out.append(
            {
                "id": org.id,
                "name": org.name,
                "slug": org.slug,
                "status": org.status,
                "plan_code": org.plan_code,
                "members": int(members),
                "credits_used": org.credits_used,
                "credits_monthly": org.credits_monthly,
                "created_at": org.created_at.isoformat(),
            }
        )
    return {"organizations": out}


@router.patch("/organizations/{org_id}/status")
async def set_status(
    org_id: str,
    payload: OrgStatusIn,
    request: Request,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    org = await session.get(Organization, org_id)
    if org is None:
        raise PermissionDenied("المؤسسة غير موجودة")
    if payload.status not in OrgStatus.values():
        raise PermissionDenied("حالة غير صالحة")
    org.status = payload.status
    await session.commit()
    return {"ok": True, "status": org.status}


@router.post("/organizations/{org_id}/add-credits")
async def add_credits(
    org_id: str,
    credits: int = Query(..., ge=1, le=1_000_000),
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    org = await session.get(Organization, org_id)
    if org is None:
        raise PermissionDenied("المؤسسة غير موجودة")
    org.credits_monthly = int(org.credits_monthly or 0) + credits
    await session.commit()
    return {"ok": True, "credits_monthly": org.credits_monthly}


@router.get("/config")
async def platform_config(principal: Principal = Depends(require_admin)) -> dict:
    """Non-secret view of the running configuration."""
    return {
        "app_env": settings.app_env,
        "app_name": settings.app_name,
        "database": settings.database_url.split("://")[0],
        "billing_provider": settings.billing_provider,
        "currency": settings.billing_currency,
        "kill_switch_global": settings.kill_switch_global,
        "worker_enabled": settings.worker_enabled,
        "scheduler_enabled": settings.scheduler_enabled,
        "allow_public_signup": settings.allow_public_signup,
        "llm": {
            "thinking_provider": settings.llm_thinking_provider,
            "decision_provider": settings.llm_decision_provider,
        },
    }
