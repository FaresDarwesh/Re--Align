"""Inbound webhooks: payment gateways + a generic channel inbox (Telegram, etc.)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from realign.config import settings
from realign.core.logging import get_logger
from realign.core.security import verify_webhook
from realign.db import get_session
from realign.services import billing
from realign.workers.queue import enqueue

log = get_logger("realign.webhooks")

router = APIRouter(tags=["webhooks"])


async def _raw_body(request: Request) -> bytes:
    return await request.body()


# ------------------------------------------------------------- payments -----


@router.post("/webhooks/billing/{provider}")
async def billing_webhook(
    provider: str,
    request: Request,
    x_signature: str | None = Header(default=None, alias="X-Signature"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    body = await _raw_body(request)
    secret = {
        "paymob": settings.paymob_hmac_secret,
        "kashier": settings.kashier_webhook_secret,
        "stripe": settings.stripe_webhook_secret,
        "manual": settings.billing_webhook_secret,
    }.get(provider.lower(), settings.billing_webhook_secret)

    if secret and not verify_webhook(secret, body, x_signature or ""):
        log.warning("billing_webhook_bad_signature", provider=provider)
        return {"ok": False, "message": "توقيع غير صالح"}

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    return await billing.handle_webhook(session, provider, payload)


# --------------------------------------------------------------- inbox ------


@router.post("/webhooks/inbox/telegram")
async def telegram_inbox(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Telegram → support agent. Set the webhook secret header when you register
    the webhook with Telegram; if none is configured we accept and still verify
    the payload shape."""
    if settings.telegram_bot_token and x_telegram_bot_api_secret_token:
        import hashlib

        expected = hashlib.sha256(settings.telegram_bot_token.encode()).hexdigest()
        if expected != x_telegram_bot_api_secret_token:
            return {"ok": False, "message": "توقيع غير صالح"}

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "message": "حمولة غير صالحة"}

    message = (payload.get("message") or payload.get("edited_message") or {})
    text = (message.get("text") or "").strip()
    if not text:
        return {"ok": True, "skipped": True}

    chat_id = str((message.get("chat") or {}).get("id", ""))
    user = message.get("from") or {}
    name = " ".join(x for x in [user.get("first_name"), user.get("last_name")] if x) or str(user.get("username") or "عميل")

    # Resolve which tenant owns this bot, then hand the message to support.
    from sqlalchemy import select

    from realign.models import Integration

    rows = (
        await session.execute(
            select(Integration).where(Integration.connector_key == "telegram", Integration.status == "connected")
        )
    ).scalars().all()
    if not rows:
        return {"ok": False, "message": "لا يوجد مستأجر يملك هذا البوت"}

    target = None
    for row in rows:
        creds = await _creds(session, row.org_id, "telegram")
        if str(creds.get("chat_id", "")) == chat_id or len(rows) == 1:
            target = row
            break
    if target is None:
        return {"ok": False, "message": "معرّف المحادثة غير مطابق"}

    event_id = await enqueue(
        org_id=target.org_id,
        type="run_agent",
        payload={
            "agent_key": "support",
            "params": {"message": text, "channel": "telegram", "customer_name": name, "chat_id": chat_id},
        },
    )
    return {"ok": True, "queued": event_id}


async def _creds(session: AsyncSession, org_id: str, connector_key: str) -> dict:
    from realign.connectors.registry import credentials_for

    return await credentials_for(session, org_id=org_id, connector_key=connector_key)


@router.post("/webhooks/inbox/generic/{org_slug}")
async def generic_inbox(org_slug: str, request: Request, session: AsyncSession = Depends(get_session)) -> dict:
    """Any system can push a customer message here and get an agent reply draft."""
    from sqlalchemy import select

    from realign.models import Organization

    org = (
        await session.execute(select(Organization).where(Organization.slug == org_slug))
    ).scalar_one_or_none()
    if org is None:
        return {"ok": False, "message": "مؤسسة غير معروفة"}

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "message": "حمولة غير صالحة"}

    text = str(payload.get("message") or payload.get("text") or "").strip()
    if not text:
        return {"ok": False, "message": "الرسالة فارغة"}

    agent = "sales" if str(payload.get("intent", "")).lower() in ("sales", "lead", "buy") else "support"
    event_id = await enqueue(
        org_id=org.id,
        type="run_agent",
        payload={
            "agent_key": agent,
            "params": {
                "message": text,
                "channel": str(payload.get("channel") or "web"),
                "customer_name": str(payload.get("customer_name") or "عميل"),
                "lead_name": str(payload.get("customer_name") or "عميل"),
            },
        },
    )
    return {"ok": True, "queued": event_id, "agent": agent}
