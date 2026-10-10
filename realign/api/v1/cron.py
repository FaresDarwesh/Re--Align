"""Scheduled work for hosts that run no background worker.

On a normal server the background worker and the scheduler run as asyncio
tasks inside the app. A serverless function is frozen the instant it returns a
response, so those tasks never get to finish. Instead:

* agent runs are executed inline by `realign.agents.engine.enqueue_run`, and
* this endpoint drains anything still queued, plus the durable outbox.

An external cron service may call it; it is protected by a shared secret so
it is not a public "make my server busy" button.
"""

from __future__ import annotations

import hmac
import time

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel
from sqlalchemy import select

from realign.config import settings
from realign.core.errors import PermissionDenied
from realign.core.logging import get_logger
from realign.db import SessionLocal
from realign.models import OutboxEvent, Run
from realign.models.operations import OutboxStatus, RunStatus

log = get_logger("realign.cron")
router = APIRouter(prefix="/cron", tags=["cron"])


class TickOut(BaseModel):
    ok: bool
    runs: int = 0
    outbox: int = 0
    milliseconds: int = 0
    mode: str


def _authorised(provided: str | None) -> bool:
    """Constant-time compare against the cron secret.

    No secret configured ⇒ the endpoint is disabled rather than open. That is
    the safe default: a missing environment variable must never mean "allow".
    """
    secret = (settings.cron_secret or "").strip()
    if not secret:
        return False
    if not provided:
        return False
    return hmac.compare_digest(secret, provided.strip())


@router.get("/tick", response_model=TickOut)
async def tick(
    request: Request,
    x_cron_secret: str | None = Header(default=None, alias="X-Cron-Secret"),
    authorization: str | None = Header(default=None),
) -> TickOut:
    """Drain queued runs and pending outbox events. Idempotent by design."""
    token = x_cron_secret or (
        authorization.split(" ", 1)[1].strip()
        if authorization and authorization.lower().startswith("bearer ")
        else ""
    )
    if not _authorised(token):
        raise PermissionDenied("غير مصرّح")

    started = time.perf_counter()
    runs_done = 0
    outbox_done = 0

    from realign.agents.engine import execute_run
    from realign.workers.queue import process_once

    # 1. Anything the inline path could not finish (DB down, cold start, …).
    async with SessionLocal() as session:
        stale = (
            await session.execute(
                select(Run)
                .where(Run.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]))
                .order_by(Run.queued_at.asc())
                .limit(settings.cron_batch_size)
            )
        ).scalars().all()
        run_ids = [r.id for r in stale]

    for run_id in run_ids:
        try:
            await execute_run(run_id)
            runs_done += 1
        except Exception as exc:  # noqa: BLE001 - one bad run must not stop the rest
            log.warning("cron_run_failed", run=run_id, error=str(exc)[:200])

    # 2. Durable outbox (publishes, follow-ups, webhooks).
    for _ in range(settings.cron_batch_size):
        try:
            if await process_once() == 0:
                break
            outbox_done += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("cron_outbox_failed", error=str(exc)[:200])
            break

    # 3. Schedules (recurring agent runs).
    try:
        from realign.workers.scheduler import tick_schedules

        await tick_schedules()
    except Exception as exc:  # noqa: BLE001
        log.warning("cron_schedules_failed", error=str(exc)[:200])

    elapsed = int((time.perf_counter() - started) * 1000)
    log.info("cron_tick", runs=runs_done, outbox=outbox_done, ms=elapsed)
    return TickOut(ok=True, runs=runs_done, outbox=outbox_done, milliseconds=elapsed,
                   mode="serverless" if settings.serverless else "worker")


@router.get("/stats")
async def stats(
    x_cron_secret: str | None = Header(default=None, alias="X-Cron-Secret"),
) -> dict:
    """Queue depth — handy to confirm the cron is actually needed."""
    if not _authorised(x_cron_secret):
        raise PermissionDenied("غير مصرّح")
    async with SessionLocal() as session:
        queued = (
            await session.execute(
                select(Run).where(Run.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]))
            )
        ).scalars().all()
        pending = (
            await session.execute(
                select(OutboxEvent).where(
                    OutboxEvent.status.in_([OutboxStatus.PENDING, OutboxStatus.FAILED])
                )
            )
        ).scalars().all()
    return {"queued_runs": len(queued), "pending_outbox": len(pending),
            "mode": "serverless" if settings.serverless else "worker"}
