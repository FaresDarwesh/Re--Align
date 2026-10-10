"""Shared helpers used by several agents."""

from __future__ import annotations

from datetime import datetime

from realign.core.utils import utcnow
from realign.models import OutboxEvent
from realign.models.operations import AssetStatus, OutboxStatus

CHANNELS_AR = {
    "instagram": "إنستجرام",
    "facebook": "فيسبوك",
    "linkedin": "لينكدإن",
    "x": "إكس",
    "tiktok": "تيك توك",
    "youtube": "يوتيوب",
    "email": "البريد الإلكتروني",
    "whatsapp": "واتساب",
    "telegram": "تيليجرام",
    "blog": "المدوّنة",
    "general": "عام",
    "sms": "رسائل نصية",
}


async def enqueue_event(
    ctx,
    *,
    type: str,
    payload: dict | None = None,
    delay_seconds: int = 0,
    max_attempts: int = 5,
) -> OutboxEvent:
    """Durable background job: committed with the current transaction."""
    event = OutboxEvent(
        org_id=ctx.org.id,
        type=type,
        payload=payload or {},
        status=OutboxStatus.PENDING,
        run_after=utcnow().replace(tzinfo=None),
        max_attempts=max_attempts,
    )
    if delay_seconds:
        event.run_after = datetime.fromtimestamp(
            utcnow().timestamp() + delay_seconds
        ).replace(tzinfo=None)
    ctx.session.add(event)
    await ctx.session.flush()
    return event


async def schedule_publish(
    ctx,
    *,
    asset,
    channel: str,
    when: datetime | None = None,
    connector_key: str | None = None,
) -> tuple[object, OutboxEvent]:
    """Mark an asset scheduled and queue the actual publish work."""
    asset.channel = channel
    asset.status = AssetStatus.SCHEDULED
    asset.scheduled_for = when or utcnow().replace(tzinfo=None)
    event = await enqueue_event(
        ctx,
        type="publish_asset",
        payload={"asset_id": asset.id, "channel": channel, "connector": connector_key or channel},
    )
    ctx.step("publish_scheduled", f"{channel} @ {asset.scheduled_for.isoformat(timespec='minutes')}")
    return asset, event


def channel_label(channel: str) -> str:
    return CHANNELS_AR.get(channel, channel)
