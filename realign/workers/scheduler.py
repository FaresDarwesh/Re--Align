"""Cron scheduler for recurring agent runs + the janitor that recovers stuck work."""

from __future__ import annotations

import asyncio
from datetime import datetime

from croniter import croniter
from sqlalchemy import select

from realign.agents.engine import enqueue_run, execute_run, stalled_runs
from realign.config import settings
from realign.core.logging import get_logger
from realign.core.utils import utcnow
from realign.db import SessionLocal
from realign.models import Organization, Schedule
from realign.models.operations import OutboxStatus, RunStatus

log = get_logger("realign.scheduler")

try:  # zoneinfo needs tzdata on some slim images; fall back to UTC
    from zoneinfo import ZoneInfo
except Exception:  # noqa: BLE001
    ZoneInfo = None  # type: ignore


def _tz(name: str):
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return None


def compute_next(cron: str, timezone_name: str, after: datetime | None = None) -> datetime | None:
    try:
        tz = _tz(timezone_name)
        base = (after or utcnow()).replace(tzinfo=None)
        if tz is not None:
            base = base.replace(tzinfo=None)
        it = croniter(cron, base)
        nxt = it.get_next(datetime)
        return nxt.replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        log.warning("bad_cron", cron=cron)
        return None


async def due_schedules() -> list[Schedule]:
    now = utcnow().replace(tzinfo=None)
    async with SessionLocal() as session:
        result = await session.execute(
            select(Schedule).where(
                Schedule.enabled.is_(True),
                (Schedule.next_run_at.is_(None)) | (Schedule.next_run_at <= now),
            )
        )
        rows = list(result.scalars().all())
        for row in rows:
            nxt = compute_next(row.cron, row.timezone, after=now)
            row.next_run_at = nxt
            row.last_run_at = now
        if rows:
            await session.commit()
        return rows


async def tick_schedules() -> int:
    launched = 0
    for sched in await due_schedules():
        async with SessionLocal() as session:
            org = await session.get(Organization, sched.org_id)
            if org is None or not org.is_operational:
                continue
            try:
                run = await enqueue_run(
                    session,
                    org_id=sched.org_id,
                    agent_key=sched.agent_key,
                    params=sched.payload or {},
                    trigger="schedule",
                    commit=True,
                )
            except Exception:  # noqa: BLE001
                log.exception("schedule_enqueue_failed", schedule=sched.id)
                continue
        asyncio.create_task(_safe_execute(run.id))
        launched += 1
        log.info("schedule_fired", schedule=sched.id, agent=sched.agent_key, org=sched.org_id)
    return launched


async def _safe_execute(run_id: str) -> None:
    try:
        await execute_run(run_id)
    except Exception:  # noqa: BLE001
        log.exception("scheduled_run_failed", run_id=run_id)


async def janitor() -> dict[str, int]:
    """Recover work orphaned by a crash or a restart."""
    stats = {"stalled_runs": 0, "orphan_approvals": 0}
    async with SessionLocal() as session:
        for run in await stalled_runs(session, older_than_minutes=15):
            if int(run.attempts or 0) < int(run.max_attempts or 3):
                run.status = RunStatus.QUEUED
                run.attempts = int(run.attempts or 0) + 1
            else:
                run.status = RunStatus.FAILED
                run.error = run.error or "توقف التنفيذ (timeout)"
                run.finished_at = utcnow()
            stats["stalled_runs"] += 1
        await session.commit()
    return stats


class Scheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def _loop(self) -> None:
        log.info("scheduler_started", enabled=settings.scheduler_enabled)
        while not self._stop.is_set():
            try:
                if settings.scheduler_enabled:
                    await tick_schedules()
                    await janitor()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("scheduler_loop_error")
            await asyncio.sleep(30)
        log.info("scheduler_stopped")

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="realign-scheduler")

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


scheduler = Scheduler()
