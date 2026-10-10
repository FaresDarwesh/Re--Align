"""The execution engine: enqueue a run, execute it, settle the bill.

Everything that can go wrong is anticipated here — kill switch, suspension,
exhausted credits, daily limits, per-run budget, agent failure — so a run
either completes cleanly or fails with a readable reason, never silently.
"""

from __future__ import annotations

import time
import traceback
from datetime import timedelta
from typing import Any

from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from realign.agents.context import AgentContext
from realign.agents.registry import get_agent
from realign.config import settings
from realign.core.errors import InsufficientCredits, KillSwitchEngaged, NotFound
from realign.core.logging import get_logger
from realign.core.utils import utcnow
from realign.db import SessionLocal
from realign.llm.router import persist_usage
from realign.models import Agent as AgentModel
from realign.models import AuditLog, BrandProfile, Organization, Run
from realign.models.operations import RunStatus

log = get_logger("realign.engine")


# ---------------------------------------------------------------- enqueue ---


async def enqueue_run(
    session: AsyncSession,
    *,
    org_id: str,
    agent_key: str,
    params: dict[str, Any] | None = None,
    user_id: str | None = None,
    trigger: str = "manual",
    parent_run_id: str | None = None,
    commit: bool = True,
) -> Run:
    agent = get_agent(agent_key)
    if agent is None:
        raise NotFound(f"الوكيل '{agent_key}' غير موجود")

    run = Run(
        org_id=org_id,
        agent_key=agent_key,
        department=agent.department,
        status=RunStatus.QUEUED,
        trigger=trigger,
        parent_run_id=parent_run_id,
        requested_by=user_id,
        input=dict(params or {}),
        max_attempts=settings.worker_max_attempts,
        queued_at=utcnow(),
    )
    session.add(run)
    if commit:
        await session.commit()
        await session.refresh(run)

    # If the host runs no worker process, run it now, in this
    # request. `execute_run` opens its own session and is fully idempotent —
    # if it fails, the row stays `queued` and the cron endpoint retries it.
    if settings.serverless:
        try:
            await execute_run(run.id)
        except Exception as exc:  # noqa: BLE001 - never break the request
            log.warning("inline_run_failed", run=run.id, error=str(exc)[:200])
    else:
        await session.flush()
    return run


# ---------------------------------------------------------------- guards ----


async def _guard(session: AsyncSession, run: Run, org: Organization) -> str | None:
    """Return a skip/fail reason, or None when it is safe to execute."""
    if settings.kill_switch_global:
        return "تم إيقاف كل التنفيذات (Kill switch عام)"
    if org.kill_switch:
        return "الاشتراك موقوف مؤقتاً (Kill switch)"
    if org.status not in ("active", "trial"):
        return f"حالة الاشتراك لا تسمح بالتنفيذ: {org.status}"
    if int(org.credits_remaining or 0) <= 0:
        return "الرصيد المستهلك انتهى لهذه الدورة"

    since = utcnow().replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    today = (
        await session.execute(
            select(sa_func.count(Run.id)).where(Run.org_id == org.id, Run.created_at >= since)
        )
    ).scalar_one()
    if int(today or 0) >= int(org.daily_run_limit or 500):
        return f"تم الوصول للحد اليومي للتشغيل ({org.daily_run_limit})"
    return None


# --------------------------------------------------------------- execute ----


async def execute_run(run_id: str) -> Run:
    started_perf = time.perf_counter()
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise NotFound("التشغيل غير موجود")

        org = await session.get(Organization, run.org_id)
        if org is None:
            run.status = RunStatus.FAILED
            run.error = "المؤسسة غير موجودة"
            await session.commit()
            return run

        agent = get_agent(run.agent_key)
        if agent is None:
            run.status = RunStatus.FAILED
            run.error = f"الوكيل '{run.agent_key}' غير مسجّل"
            await session.commit()
            return run

        blocked = await _guard(session, run, org)
        if blocked:
            run.status = RunStatus.SKIPPED
            run.error = blocked
            run.finished_at = utcnow()
            await session.commit()
            log.info("run_skipped", run_id=run_id, org=org.id, reason=blocked)
            return run

        brand = (
            await session.execute(
                select(BrandProfile).where(BrandProfile.org_id == org.id)
            )
        ).scalar_one_or_none()

        run.status = RunStatus.RUNNING
        run.started_at = utcnow()
        run.attempts = int(run.attempts or 0) + 1
        await session.commit()

        ctx = AgentContext(
            session=session,
            org=org,
            run=run,
            brand=brand,
            params=dict(run.input or {}),
            user_id=run.requested_by,
        )

        try:
            result = await agent.run(ctx)

            run.output = {
                "summary": result.summary,
                "assets": result.assets,
                "approvals": result.approvals,
                "next_actions": result.next_actions,
                "data": result.data,
            }
            run.artifacts = result.assets
            run.steps = list(ctx.steps)
            run.metrics = result.metrics

            # settle the bill
            await persist_usage(
                session,
                org_id=org.id,
                tracker=ctx.tracker,
                run_id=run.id,
                user_id=run.requested_by,
                agent_key=run.agent_key,
            )
            run.tokens_in = ctx.tracker.tokens_in
            run.tokens_out = ctx.tracker.tokens_out
            run.cost_micros = ctx.tracker.cost_micros
            run.credits = ctx.tracker.credits
            run.llm_calls = ctx.tracker.calls

            if ctx.approvals:
                run.status = RunStatus.WAITING_APPROVAL
            else:
                run.status = RunStatus.SUCCEEDED
                run.finished_at = utcnow()

            log.info(
                "run_finished",
                run_id=run.id,
                org=org.id,
                agent=run.agent_key,
                status=run.status,
                cost_micros=run.cost_micros,
                credits=run.credits,
            )

        except InsufficientCredits as exc:
            run.status = RunStatus.FAILED
            run.error = str(exc)
            run.error_type = "InsufficientCredits"
            log.warning("run_no_credits", run_id=run.id, org=org.id)
        except KillSwitchEngaged as exc:
            run.status = RunStatus.SKIPPED
            run.error = str(exc)
            run.error_type = "KillSwitchEngaged"
        except Exception as exc:  # noqa: BLE001 - agents must never crash the worker
            run.status = RunStatus.FAILED
            run.error = f"{type(exc).__name__}: {exc}"
            run.error_type = type(exc).__name__
            run.steps = list(ctx.steps)
            log.exception("run_failed", run_id=run.id, org=org.id, agent=run.agent_key)
            run.output = {"traceback": traceback.format_exc()[-3000:]}
        finally:
            if run.status not in (RunStatus.WAITING_APPROVAL,):
                run.finished_at = run.finished_at or utcnow()
            run.duration_ms = int((time.perf_counter() - started_perf) * 1000)
            try:
                await _settle_agent_stats(session, org.id, run)
                session.add(
                    AuditLog(
                        org_id=org.id,
                        actor_type="agent",
                        actor_id=run.id,
                        actor_label=run.agent_key,
                        action="agent.run",
                        resource="run",
                        resource_id=run.id,
                        severity="warn" if run.status == RunStatus.FAILED else "info",
                        message=run.error or run.status,
                        meta={"agent": run.agent_key, "cost_micros": run.cost_micros},
                    )
                )
                await session.commit()
            except Exception:  # noqa: BLE001
                await session.rollback()
                log.exception("run_settle_failed", run_id=run.id)

        return run


async def _settle_agent_stats(session: AsyncSession, org_id: str, run: Run) -> None:
    result = await session.execute(
        select(AgentModel).where(AgentModel.org_id == org_id, AgentModel.key == run.agent_key)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return
    row.last_run_at = utcnow()
    row.total_runs = int(row.total_runs or 0) + 1
    row.total_cost_micros = int(row.total_cost_micros or 0) + int(run.cost_micros or 0)


# --------------------------------------------------------------- resume -----


async def resume_approved_run(run_id: str) -> None:
    """After the last pending approval of a run is decided, finish it cleanly."""
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is None:
            return
        from realign.models import Approval

        pending = (
            await session.execute(
                select(sa_func.count(Approval.id)).where(
                    Approval.run_id == run_id, Approval.status == "pending"
                )
            )
        ).scalar_one()
        if int(pending or 0) == 0 and run.status == RunStatus.WAITING_APPROVAL:
            run.status = RunStatus.SUCCEEDED
            run.finished_at = utcnow()
            await session.commit()


async def recent_runs(session: AsyncSession, org_id: str, limit: int = 20) -> list[Run]:
    result = await session.execute(
        select(Run).where(Run.org_id == org_id).order_by(Run.created_at.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def runs_today(session: AsyncSession, org_id: str) -> int:
    since = utcnow().replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    value = (
        await session.execute(
            select(sa_func.count(Run.id)).where(Run.org_id == org_id, Run.created_at >= since)
        )
    ).scalar_one()
    return int(value or 0)


async def stalled_runs(session: AsyncSession, older_than_minutes: int = 15) -> list[Run]:
    """Runs stuck in RUNNING (worker crash) — picked up and retried by the janitor."""
    cutoff = utcnow().replace(tzinfo=None) - timedelta(minutes=older_than_minutes)
    result = await session.execute(
        select(Run).where(Run.status == RunStatus.RUNNING, Run.started_at.is_not(None), Run.started_at < cutoff)
    )
    return list(result.scalars().all())
