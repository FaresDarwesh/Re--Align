"""Plans, subscriptions, invoices and payment-gateway checkout.

Supported gateways: manual (default), Paymob, Kashier.
Stripe is intentionally *not* available for Egyptian entities (see README); the
adapter is wired but returns a clear message instead of failing silently.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from typing import Any

import httpx
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from realign.config import settings
from realign.core.logging import get_logger
from realign.core.utils import add_months, utcnow
from realign.models import Invoice, Organization, Plan, Subscription, UsageRecord
from realign.models.billing import InvoiceStatus, SubscriptionStatus
from realign.services import audit

log = get_logger("realign.billing")


# -------------------------------------------------------------------- plans --


async def list_plans(session: AsyncSession, *, public_only: bool = True) -> list[Plan]:
    query = select(Plan).where(Plan.is_active.is_(True))
    if public_only:
        query = query.where(Plan.is_public.is_(True))
    result = await session.execute(query.order_by(Plan.sort_order.asc()))
    return list(result.scalars().all())


async def get_plan(session: AsyncSession, code: str) -> Plan | None:
    return (
        await session.execute(select(Plan).where(Plan.code == code))
    ).scalar_one_or_none()


# ------------------------------------------------------------ subscription --


async def current_subscription(session: AsyncSession, org: Organization) -> Subscription:
    result = await session.execute(
        select(Subscription).where(Subscription.org_id == org.id).order_by(Subscription.created_at.desc())
    )
    sub = result.scalars().first()
    if sub is None:
        from realign.services.onboarding import ensure_subscription

        sub = await ensure_subscription(session, org)
    return sub


async def change_plan(
    session: AsyncSession,
    *,
    org: Organization,
    plan_code: str,
    actor_label: str = "",
    actor_id: str | None = None,
) -> Subscription:
    plan = await get_plan(session, plan_code)
    if plan is None:
        raise ValueError("الخطة غير موجودة")

    sub = await current_subscription(session, org)
    now = utcnow().replace(tzinfo=None)
    sub.plan_code = plan.code
    sub.current_period_start = now
    sub.current_period_end = add_months(now, 12 if plan.interval == "year" else 1)
    sub.cancel_at_period_end = False

    org.plan_code = plan.code
    org.credits_monthly = int(plan.credits_monthly)
    org.seats_limit = int(plan.seats_limit)
    if plan.price_cents > 0:
        org.status = "active"

    await audit.record(
        session,
        org_id=org.id,
        action="billing.plan_changed",
        actor_id=actor_id,
        actor_label=actor_label,
        resource="subscription",
        resource_id=sub.id,
        message=f"الخطة → {plan.code}",
        meta={"plan": plan.code, "credits": plan.credits_monthly},
    )
    await session.commit()
    await session.refresh(sub)
    return sub


# ----------------------------------------------------------------- invoices --


def _invoice_number() -> str:
    stamp = utcnow().strftime("%Y%m")
    return f"INV-{stamp}-{secrets.token_hex(3).upper()}"


async def create_invoice(
    session: AsyncSession,
    *,
    org: Organization,
    plan: Plan,
    period_start=None,
    period_end=None,
) -> Invoice:
    now = utcnow().replace(tzinfo=None)
    period_start = period_start or now
    period_end = period_end or add_months(now, 12 if plan.interval == "year" else 1)
    tax = int(round(plan.price_cents * 0.14)) if plan.price_cents else 0  # 14% VAT (EG)

    invoice = Invoice(
        org_id=org.id,
        number=_invoice_number(),
        status=InvoiceStatus.OPEN,
        amount_cents=int(plan.price_cents),
        tax_cents=tax,
        currency=plan.currency or settings.billing_currency,
        period_start=period_start,
        period_end=period_end,
        issued_at=now,
        due_at=add_months(now, 0),
        provider=settings.billing_provider or "manual",
        lines=[
            {
                "description": f"اشتراك {plan.name_ar or plan.name} ({plan.interval})",
                "quantity": 1,
                "unit_cents": int(plan.price_cents),
                "total_cents": int(plan.price_cents),
            }
        ],
    )
    session.add(invoice)
    await session.commit()
    await session.refresh(invoice)
    return invoice


async def mark_paid(
    session: AsyncSession,
    *,
    invoice: Invoice,
    external_ref: str | None = None,
    provider: str | None = None,
) -> Invoice:
    invoice.status = InvoiceStatus.PAID
    invoice.paid_at = utcnow()
    if external_ref:
        invoice.external_ref = external_ref
    if provider:
        invoice.provider = provider

    org = await session.get(Organization, invoice.org_id)
    if org is not None:
        org.status = "active"
        org.credits_used = 0
        sub = await current_subscription(session, org)
        sub.status = SubscriptionStatus.ACTIVE
        sub.external_ref = external_ref or sub.external_ref
        now = utcnow().replace(tzinfo=None)
        sub.current_period_start = now
        sub.current_period_end = add_months(now, 1)
    await session.commit()
    log.info("invoice_paid", invoice=invoice.number, org=invoice.org_id)
    return invoice


# ---------------------------------------------------------------- checkout --


async def checkout(
    session: AsyncSession, *, org: Organization, plan_code: str
) -> dict[str, Any]:
    """Return everything the client needs to pay (URL or manual instructions)."""
    plan = await get_plan(session, plan_code)
    if plan is None:
        raise ValueError("الخطة غير موجودة")

    if int(plan.price_cents or 0) == 0:
        await change_plan(session, org=org, plan_code=plan.code, actor_label="system")
        return {"provider": "free", "status": "activated", "message": "تم تفعيل الخطة المجانية."}

    invoice = await create_invoice(session, org=org, plan=plan)
    provider = (settings.billing_provider or "manual").lower()

    if provider == "paymob":
        return await _paymob_checkout(org, plan, invoice)
    if provider == "kashier":
        return await _kashier_checkout(org, plan, invoice)
    if provider == "stripe":
        return {
            "provider": "stripe",
            "status": "manual",
            "invoice": invoice.number,
            "message": (
                "Stripe غير متاح للكيانات المصرية مباشرة. استخدم Paymob أو Kashier، "
                "أو سجّل كياناً في دولة مدعومة ثم فعّل Stripe من الإعدادات."
            ),
        }
    return {
        "provider": "manual",
        "status": "pending",
        "invoice": invoice.number,
        "amount_cents": invoice.amount_cents + invoice.tax_cents,
        "currency": invoice.currency,
        "message": "حوّل المبلغ ثم أرسل مرجع التحويل للإدارة لتفعيل الاشتراك.",
    }


async def _paymob_checkout(org: Organization, plan: Plan, invoice: Invoice) -> dict[str, Any]:
    amount_cents = (invoice.amount_cents + invoice.tax_cents) / 100
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            auth = await client.post(
                "https://accept.paymob.com/api/auth/tokens",
                json={"api_key": settings.paymob_api_key},
            )
            token = auth.json().get("token")
            order = await client.post(
                "https://accept.paymob.com/api/ecommerce/orders",
                json={
                    "auth_token": token,
                    "delivery_needed": False,
                    "amount_cents": str(int(amount_cents * 100)),
                    "currency": invoice.currency,
                    "items": invoice.lines,
                },
            )
            order_id = order.json().get("id")
            key_resp = await client.post(
                "https://accept.paymob.com/api/acceptance/payment_keys",
                json={
                    "auth_token": token,
                    "amount_cents": str(int(amount_cents * 100)),
                    "expiration": 3600,
                    "order_id": order_id,
                    "billing_data": {
                        "email": org.billing_email or "",
                        "first_name": org.name[:30],
                        "last_name": "Org",
                        "phone_number": "",
                        "apartment": "NA",
                        "floor": "NA",
                        "street": "NA",
                        "building": "NA",
                        "city": "Cairo",
                        "country": "EG",
                    },
                    "currency": invoice.currency,
                    "integration_id": int(settings.paymob_integration_id or 0),
                },
            )
            payment_token = key_resp.json().get("token")
        return {
            "provider": "paymob",
            "status": "redirect",
            "invoice": invoice.number,
            "url": f"https://accept.paymob.com/api/acceptance/iframes/{settings.paymob_iframe_id}?payment_token={payment_token}",
        }
    except Exception as exc:  # noqa: BLE001
        log.exception("paymob_checkout_failed")
        return {"provider": "paymob", "status": "error", "invoice": invoice.number, "message": str(exc)}


async def _kashier_checkout(org: Organization, plan: Plan, invoice: Invoice) -> dict[str, Any]:
    amount = (invoice.amount_cents + invoice.tax_cents) / 100
    path = f"/?payment={settings.kashier_merchant_id}.{invoice.number}.{amount:.2f}.{invoice.currency}"
    signature = hmac.new(
        (settings.kashier_webhook_secret or settings.kashier_api_key).encode(),
        path.encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        "provider": "kashier",
        "status": "redirect",
        "invoice": invoice.number,
        "url": f"https://checkout.kashier.io{path}&merchantRedirect={settings.app_base_url}/billing&signature={signature}",
    }


# ---------------------------------------------------------------- webhooks --


async def handle_webhook(session: AsyncSession, provider: str, payload: dict[str, Any]) -> dict[str, Any]:
    provider = (provider or "").lower()
    number = ""
    paid = False
    external_ref = ""

    if provider == "paymob":
        obj = payload.get("obj") or payload
        number = str(obj.get("order", {}).get("merchant_order_id") or "")
        paid = bool(obj.get("success")) and not bool(obj.get("is_refund"))
        external_ref = str(obj.get("id") or "")
    elif provider == "kashier":
        data = payload.get("data") or payload
        number = str(data.get("merchantOrderId") or data.get("orderId") or "")
        paid = str(data.get("paymentStatus", "")).upper() in ("SUCCESS", "PAID", "CAPTURED")
        external_ref = str(data.get("transactionId") or "")
    elif provider == "manual":
        number = str(payload.get("invoice") or "")
        paid = bool(payload.get("paid"))
        external_ref = str(payload.get("reference") or "")
    else:
        return {"ok": False, "message": f"مزوّد غير مدعوم: {provider}"}

    if not number:
        return {"ok": False, "message": "رقم الفاتورة مفقود"}

    invoice = (
        await session.execute(select(Invoice).where(Invoice.number == number))
    ).scalar_one_or_none()
    if invoice is None:
        return {"ok": False, "message": "الفاتورة غير موجودة", "invoice": number}
    if not paid:
        invoice.status = InvoiceStatus.OPEN
        await session.commit()
        return {"ok": True, "paid": False, "invoice": number}

    if invoice.status != InvoiceStatus.PAID:
        await mark_paid(session, invoice=invoice, external_ref=external_ref, provider=provider)
    return {"ok": True, "paid": True, "invoice": number}


# ------------------------------------------------------------------ usage ---


async def usage_summary(session: AsyncSession, org: Organization) -> dict[str, Any]:
    since = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    row = (
        await session.execute(
            select(
                sa_func.coalesce(sa_func.sum(UsageRecord.cost_micros), 0),
                sa_func.coalesce(sa_func.sum(UsageRecord.credits), 0),
                sa_func.coalesce(sa_func.count(UsageRecord.id), 0),
            ).where(UsageRecord.org_id == org.id, UsageRecord.created_at >= since)
        )
    ).one()

    by_agent = (
        await session.execute(
            select(
                UsageRecord.agent_key,
                sa_func.coalesce(sa_func.sum(UsageRecord.cost_micros), 0).label("c"),
                sa_func.count(UsageRecord.id).label("n"),
            )
            .where(UsageRecord.org_id == org.id, UsageRecord.created_at >= since)
            .group_by(UsageRecord.agent_key)
            .order_by(sa_func.sum(UsageRecord.cost_micros).desc())
        )
    ).all()
    return {
        "period_start": since.isoformat(),
        "cost_micros": int(row[0]),
        "cost_usd": round(int(row[0]) / 1_000_000, 4),
        "credits_used_period": int(row[1]),
        "records": int(row[2]),
        "credits_used": int(org.credits_used or 0),
        "credits_monthly": int(org.credits_monthly or 0),
        "credits_remaining": int(org.credits_remaining or 0),
        "by_agent": [{"agent": a or "-", "usd": round(int(c) / 1_000_000, 4), "calls": int(n)} for a, c, n in by_agent],
    }
