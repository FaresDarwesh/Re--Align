"""Plans, subscription, invoices, usage and checkout."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from realign.core.errors import NotFound
from realign.db import get_session
from realign.models import Invoice, Organization
from realign.services import audit, billing
from realign.services.auth import Principal, client_ip, get_principal, require_role

router = APIRouter(tags=["billing"])


class PlanChange(BaseModel):
    plan_code: str


@router.get("/plans")
async def plans(session: AsyncSession = Depends(get_session)) -> dict:
    rows = await billing.list_plans(session)
    return {
        "currency": rows[0].currency if rows else "EGP",
        "plans": [
            {
                "code": p.code,
                "name": p.name,
                "name_ar": p.name_ar,
                "description_ar": p.description_ar,
                "price_cents": p.price_cents,
                "currency": p.currency,
                "interval": p.interval,
                "credits_monthly": p.credits_monthly,
                "seats_limit": p.seats_limit,
                "agent_limit": p.agent_limit,
                "features": p.features,
            }
            for p in rows
        ],
    }


@router.get("/billing")
async def overview(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    org = principal.org
    sub = await billing.current_subscription(session, org)
    plan = await billing.get_plan(session, sub.plan_code)
    invoices = (
        await session.execute(
            select(Invoice).where(Invoice.org_id == org.id).order_by(Invoice.created_at.desc()).limit(12)
        )
    ).scalars().all()
    return {
        "subscription": {
            "plan_code": sub.plan_code,
            "plan_name": plan.name_ar if plan else sub.plan_code,
            "status": sub.status,
            "current_period_end": sub.current_period_end.isoformat() if sub.current_period_end else None,
            "trial_ends_at": sub.trial_ends_at.isoformat() if sub.trial_ends_at else None,
            "cancel_at_period_end": sub.cancel_at_period_end,
        },
        "usage": await billing.usage_summary(session, org),
        "invoices": [
            {
                "id": i.id,
                "number": i.number,
                "status": i.status,
                "amount_cents": i.amount_cents,
                "tax_cents": i.tax_cents,
                "currency": i.currency,
                "issued_at": i.issued_at.isoformat() if i.issued_at else None,
                "paid_at": i.paid_at.isoformat() if i.paid_at else None,
                "period_end": i.period_end.isoformat() if i.period_end else None,
            }
            for i in invoices
        ],
    }


@router.post("/billing/subscribe")
async def subscribe(
    payload: PlanChange,
    request: Request,
    principal: Principal = Depends(require_role("owner")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    org = principal.org
    result = await billing.checkout(session, org=org, plan_code=payload.plan_code)
    await audit.record(
        session,
        org_id=org.id,
        action="billing.checkout",
        actor_id=principal.user_id,
        actor_label=principal.user.email if principal.user else "",
        resource="subscription",
        message=payload.plan_code,
        meta={k: v for k, v in result.items() if k in ("provider", "status", "invoice", "amount_cents")},
        ip=client_ip(request),
        commit=True,
    )
    return result


@router.post("/billing/plan")
async def change_plan(
    payload: PlanChange,
    principal: Principal = Depends(require_role("owner")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    sub = await billing.change_plan(
        session,
        org=principal.org,
        plan_code=payload.plan_code,
        actor_id=principal.user_id,
        actor_label=principal.user.email if principal.user else "",
    )
    return {"ok": True, "plan_code": sub.plan_code}


@router.get("/billing/invoices/{invoice_id}")
async def invoice_detail(
    invoice_id: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    invoice = await session.get(Invoice, invoice_id)
    if invoice is None or invoice.org_id != principal.org_id:
        raise NotFound("الفاتورة غير موجودة")
    return {
        "invoice": {
            "id": invoice.id,
            "number": invoice.number,
            "status": invoice.status,
            "amount_cents": invoice.amount_cents,
            "tax_cents": invoice.tax_cents,
            "total_cents": invoice.amount_cents + invoice.tax_cents,
            "currency": invoice.currency,
            "lines": invoice.lines,
            "issued_at": invoice.issued_at.isoformat() if invoice.issued_at else None,
            "paid_at": invoice.paid_at.isoformat() if invoice.paid_at else None,
        }
    }
