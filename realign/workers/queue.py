"""Durable outbox worker.

Events are written by agents inside their own transaction, then processed here.
Every handler is idempotent-ish and failure is retried with exponential backoff
before being moved to `dead` (never silently dropped).
"""

from __future__ import annotations

import asyncio
import json
import traceback
import uuid
from datetime import timedelta
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from realign.agents.engine import enqueue_run, execute_run
from realign.config import settings
from realign.core.logging import get_logger
from realign.core.security import decrypt_dict
from realign.core.utils import utcnow
from realign.db import SessionLocal
from realign.models import ContentAsset, Integration, OutboxEvent
from realign.models.operations import AssetStatus, OutboxStatus

log = get_logger("realign.worker")

WORKER_ID = uuid.uuid4().hex[:12]

Handler = Callable[[AsyncSession, OutboxEvent], Awaitable[None]]


# ------------------------------------------------------------------ claim ---


async def claim_batch(session: AsyncSession, limit: int | None = None) -> list[OutboxEvent]:
    """Atomically-ish take a batch of due events (SQLite/Postgres safe)."""
    limit = limit or max(settings.worker_concurrency, 1)
    now = utcnow().replace(tzinfo=None)
    result = await session.execute(
        select(OutboxEvent)
        .where(
            OutboxEvent.status.in_([OutboxStatus.PENDING, OutboxStatus.FAILED]),
            OutboxEvent.run_after <= now,
        )
        .order_by(OutboxEvent.run_after.asc())
        .limit(limit)
    )
    events = list(result.scalars().all())
    if not events:
        return []
    ids = [e.id for e in events]
    for event in events:
        event.status = OutboxStatus.PROCESSING
        event.locked_at = utcnow()
        event.locked_by = WORKER_ID
        event.attempts = int(event.attempts or 0) + 1
    await session.commit()
    # re-read to get committed objects bound to this session
    result = await session.execute(select(OutboxEvent).where(OutboxEvent.id.in_(ids)))
    return list(result.scalars().all())


async def finish(session: AsyncSession, event: OutboxEvent, *, ok: bool, error: str = "") -> None:
    if ok:
        event.status = OutboxStatus.DONE
        event.processed_at = utcnow()
        event.last_error = None
    else:
        event.last_error = error[:4000]
        if int(event.attempts or 0) >= int(event.max_attempts or 5):
            event.status = OutboxStatus.DEAD
            log.error("outbox_dead", event_id=event.id, type=event.type, error=error[:300])
        else:
            backoff = min(settings.worker_backoff_seconds * (2 ** int(event.attempts or 1)), 3600)
            event.status = OutboxStatus.FAILED
            event.run_after = (utcnow() + timedelta(seconds=backoff)).replace(tzinfo=None)
    await session.commit()


# --------------------------------------------------------------- handlers ---


async def handle_run_agent(session: AsyncSession, event: OutboxEvent) -> None:
    payload = event.payload or {}
    agent_key = str(payload.get("agent_key", ""))
    if not agent_key:
        raise ValueError("agent_key مفقود")
    run = await enqueue_run(
        session,
        org_id=event.org_id,
        agent_key=agent_key,
        params=payload.get("params") or {},
        trigger="event",
        parent_run_id=payload.get("parent_run_id"),
        commit=True,
    )
    await execute_run(run.id)


async def handle_publish_asset(session: AsyncSession, event: OutboxEvent) -> None:
    from realign.connectors.registry import credentials_for, resolve_channel_connector

    payload = event.payload or {}
    asset = await session.get(ContentAsset, payload.get("asset_id"))
    if asset is None:
        raise ValueError("الأصل غير موجود")
    if asset.status in (AssetStatus.PUBLISHED, AssetStatus.REJECTED):
        return  # idempotent

    channel = str(payload.get("channel") or asset.channel or "general")
    connector = resolve_channel_connector(channel)
    creds = await credentials_for(session, org_id=event.org_id, connector_key=connector.key)

    if not connector.is_configured(creds):
        # No credentials: record it clearly instead of pretending we published.
        asset.status = AssetStatus.FAILED
        asset.meta = {
            **(asset.meta or {}),
            "publish_error": f"الربط '{connector.name_ar}' غير مُعد — تم حفظ المنشور كمسوّدة.",
        }
        await session.commit()
        log.warning("publish_no_credentials", asset=asset.id, connector=connector.key)
        return

    result = await connector.publish(creds, asset, config=payload.get("config"))
    if result.ok:
        asset.status = AssetStatus.PUBLISHED
        asset.published_at = utcnow()
        asset.external_id = result.external_id
        asset.external_url = result.url
    else:
        asset.status = AssetStatus.FAILED
        asset.meta = {**(asset.meta or {}), "publish_error": result.message}
        raise RuntimeError(result.message or "فشل النشر")
    await session.commit()
    log.info("asset_published", asset=asset.id, connector=connector.key, channel=channel)


async def handle_handoff(session: AsyncSession, event: OutboxEvent) -> None:
    """Agent-to-agent pass (the 'agent bus'). Only runs when explicitly auto."""
    payload = event.payload or {}
    if not payload.get("auto"):
        return
    target = str(payload.get("to", ""))
    if not target:
        return
    params: dict[str, Any] = {}
    asset_id = payload.get("asset_id")
    if asset_id:
        asset = await session.get(ContentAsset, asset_id)
        if asset is not None:
            params = {
                "asset_id": asset.id,
                "source_brief": asset.body[:3000],
                "angle": payload.get("angle") or asset.title,
                "topic": asset.title,
                "kind": "post",
            }
    await enqueue_run(
        session,
        org_id=event.org_id,
        agent_key=target,
        params=params,
        trigger="handoff",
        parent_run_id=payload.get("parent_run_id"),
        commit=True,
    )


async def handle_send_message(session: AsyncSession, event: OutboxEvent) -> None:
    from realign.connectors.registry import credentials_for, get_connector

    payload = event.payload or {}
    connector = get_connector(str(payload.get("connector", "mock_channel"))) or get_connector("mock_channel")
    assert connector is not None
    creds = await credentials_for(session, org_id=event.org_id, connector_key=connector.key)
    if not connector.is_configured(creds):
        # Nothing is wired up yet (no WhatsApp token, no SMTP credentials…).
        # That is a customer setup step, not a transient failure: recording it
        # as "skipped" stops the outbox from burning five retries on a job that
        # can never succeed until someone fills in the credentials.
        log.warning(
            "send_skipped_not_configured",
            connector=connector.key,
            org_id=event.org_id,
            event_id=event.id,
        )
        event.status = OutboxStatus.DONE
        event.payload = {
            **payload,
            "skipped": True,
            "skip_reason": (
                f"موصل «{connector.label}» غير مضبوط — أضف بيانات الربط "
                "من صفحة الربطات ثم أعد المحاولة."
            ),
        }
        event.last_error = "skipped: connector not configured"
        return
    result = await connector.send(
        creds,
        to=str(payload.get("to", "")),
        subject=str(payload.get("subject", "")),
        body=str(payload.get("body", "")),
    )
    if not result.ok:
        raise RuntimeError(result.message or "فشل الإرسال")


async def handle_sync_integration(session: AsyncSession, event: OutboxEvent) -> None:
    from realign.connectors.registry import credentials_for, get_connector

    payload = event.payload or {}
    connector = get_connector(str(payload.get("connector", "")))
    if connector is None or "read" not in connector.capabilities:
        return
    creds = await credentials_for(session, org_id=event.org_id, connector_key=connector.key)
    if not connector.is_configured(creds):
        return
    data = await connector.read(creds, config=payload.get("config"))
    row = (
        await session.execute(
            select(Integration).where(
                Integration.org_id == event.org_id, Integration.connector_key == connector.key
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        row.last_sync_at = utcnow()
        row.usage_count = int(row.usage_count or 0) + 1
        row.status = "connected"
    await session.commit()
    log.info("integration_synced", connector=connector.key, items=len(data))


async def handle_reset_credits(session: AsyncSession, event: OutboxEvent) -> None:
    from realign.models import Organization

    org = await session.get(Organization, event.org_id)
    if org is None:
        return
    org.credits_used = 0
    await session.commit()
    log.info("credits_reset", org=org.id)


HANDLERS: dict[str, Handler] = {
    "run_agent": handle_run_agent,
    "publish_asset": handle_publish_asset,
    "handoff": handle_handoff,
    "send_message": handle_send_message,
    "sync_integration": handle_sync_integration,
    "reset_credits": handle_reset_credits,
}


# ------------------------------------------------------------------ loop ----


async def process_once() -> int:
    processed = 0
    async with SessionLocal() as session:
        events = await claim_batch(session)
        for event in events:
            handler = HANDLERS.get(event.type)
            if handler is None:
                await finish(session, event, ok=False, error=f"لا يوجد معالج للنوع {event.type}")
                continue
            try:
                await handler(session, event)
                await finish(session, event, ok=True)
                processed += 1
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "outbox_failed",
                    event_id=event.id,
                    type=event.type,
                    attempt=event.attempts,
                    error=str(exc)[:300],
                )
                await finish(session, event, ok=False, error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}")
    return processed


async def requeue_stalled() -> int:
    """Events locked by a crashed worker go back to pending."""
    cutoff = (utcnow() - timedelta(minutes=10)).replace(tzinfo=None)
    async with SessionLocal() as session:
        result = await session.execute(
            select(OutboxEvent).where(
                OutboxEvent.status == OutboxStatus.PROCESSING,
                OutboxEvent.locked_at.is_not(None),
                OutboxEvent.locked_at < cutoff,
            )
        )
        rows = list(result.scalars().all())
        for row in rows:
            row.status = OutboxStatus.FAILED
            row.locked_at = None
            row.locked_by = None
        if rows:
            await session.commit()
    return len(rows)


class OutboxWorker:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def _loop(self) -> None:
        log.info("worker_started", worker_id=WORKER_ID, concurrency=settings.worker_concurrency)
        tick = 0
        while not self._stop.is_set():
            try:
                processed = await process_once()
                tick += 1
                if tick % 30 == 0:  # every ~60s
                    recovered = await requeue_stalled()
                    if recovered:
                        log.info("worker_requeued_stalled", count=recovered)
                delay = settings.worker_poll_seconds if processed else min(settings.worker_poll_seconds * 3, 10)
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("worker_loop_error")
                await asyncio.sleep(settings.worker_poll_seconds * 2)
        log.info("worker_stopped")

    def start(self) -> None:
        if not settings.worker_enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="realign-outbox-worker")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._task = None


worker = OutboxWorker()


async def enqueue(*, org_id: str, type: str, payload: dict | None = None, delay_seconds: int = 0) -> str:
    """Public helper for API routes."""
    async with SessionLocal() as session:
        from realign.models import OutboxEvent as OE

        event = OE(
            org_id=org_id,
            type=type,
            payload=payload or {},
            status=OutboxStatus.PENDING,
            run_after=(utcnow() + timedelta(seconds=delay_seconds)).replace(tzinfo=None),
        )
        session.add(event)
        await session.commit()
        return event.id
