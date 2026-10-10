"""Agents, runs (the execution API), brand files and schedules."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from realign.agents.engine import enqueue_run, execute_run, runs_today
from realign.agents.registry import departments, get_agent, list_agents_meta
from realign.core.errors import NotFound, ValidationError
from realign.db import get_session
from realign.models import Agent as AgentModel
from realign.models import BrandProfile, ContentAsset, Run, Schedule
from realign.models.operations import AssetStatus, RunStatus
from realign.services import audit
from realign.services.auth import Principal, client_ip, get_principal, require_role
from realign.workers.scheduler import compute_next

router = APIRouter(tags=["agents"])


# ----------------------------------------------------------------- schemas --


class RunIn(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict, max_length=40)
    async_execution: bool = True


class AgentUpdate(BaseModel):
    enabled: bool | None = None
    requires_approval: bool | None = None
    schedule_cron: str | None = Field(default=None, max_length=80)
    per_run_budget_cents: int | None = Field(default=None, ge=0, le=1_000_000_00)
    config: dict[str, Any] | None = Field(default=None, max_length=40)


class BrandIn(BaseModel):
    company_name: str | None = Field(default=None, max_length=120)
    industry: str | None = Field(default=None, max_length=120)
    audience: str | None = Field(default=None, max_length=1000)
    offer: str | None = Field(default=None, max_length=1000)
    voice: str | None = Field(default=None, max_length=1000)
    tone_keywords: list[str] | None = Field(default=None, max_length=30)
    rules: str | None = Field(default=None, max_length=4000)
    never_say: str | None = Field(default=None, max_length=2000)
    competitors: list[str] | None = Field(default=None, max_length=30)
    keywords: list[str] | None = Field(default=None, max_length=30)
    hashtags: list[str] | None = Field(default=None, max_length=30)
    locales: list[str] | None = Field(default=None, max_length=10)
    sample_posts: list[str] | None = Field(default=None, max_length=10)


class ScheduleIn(BaseModel):
    agent_key: str = Field(max_length=40)
    cron: str = Field(max_length=80)
    timezone: str = Field(default="Africa/Cairo", max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict, max_length=40)
    enabled: bool = True


# -------------------------------------------------------------- catalogue ---


@router.get("/agents")
async def catalogue(principal: Principal = Depends(get_principal)) -> dict:
    return {"agents": list_agents_meta(), "departments": departments()}


@router.get("/agents/installed")
async def installed(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    result = await session.execute(
        select(AgentModel).where(AgentModel.org_id == principal.org_id).order_by(AgentModel.department, AgentModel.key)
    )
    rows = result.scalars().all()
    meta_by_key = {m["key"]: m for m in list_agents_meta()}
    return {
        "agents": [
            {
                **meta_by_key.get(r.key, {"key": r.key}),
                "id": r.id,
                "enabled": r.enabled,
                "requires_approval": r.requires_approval,
                "schedule_cron": r.schedule_cron,
                "per_run_budget_cents": r.per_run_budget_cents,
                "config": r.config,
                "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
                "total_runs": r.total_runs,
                "total_cost_usd": round(int(r.total_cost_micros or 0) / 1_000_000, 4),
            }
            for r in rows
        ]
    }


@router.patch("/agents/{key}")
async def update_agent(
    key: str,
    payload: AgentUpdate,
    request: Request,
    principal: Principal = Depends(require_role("manager")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = (
        await session.execute(
            select(AgentModel).where(AgentModel.org_id == principal.org_id, AgentModel.key == key)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("الوكيل غير مثبّت لهذه المؤسسة")

    for field in ("enabled", "requires_approval", "schedule_cron", "per_run_budget_cents", "config"):
        value = getattr(payload, field)
        if value is not None:
            setattr(row, field, value)
    await session.commit()

    await audit.record(
        session,
        org_id=principal.org_id,
        action="agent.updated",
        actor_id=principal.user_id,
        actor_label=principal.user.email if principal.user else "",
        resource="agent",
        resource_id=row.id,
        message=f"{key}",
        meta=payload.model_dump(exclude_none=True),
        ip=client_ip(request),
        commit=True,
    )
    return {"ok": True, "key": key}


# -------------------------------------------------------------------- runs --


@router.post("/agents/{key}/run", status_code=202)
async def run_agent(
    key: str,
    payload: RunIn,
    background: BackgroundTasks,
    request: Request,
    principal: Principal = Depends(require_role("editor")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if get_agent(key) is None:
        raise NotFound("الوكيل غير موجود")

    from realign.core.sanitize import sanitize_params

    params = sanitize_params(payload.params)

    row = (
        await session.execute(
            select(AgentModel).where(AgentModel.org_id == principal.org_id, AgentModel.key == key)
        )
    ).scalar_one_or_none()
    if row is not None and not row.enabled:
        raise ValidationError("هذا الوكيل موقوف — فعّله أولاً")

    run = await enqueue_run(
        session,
        org_id=principal.org_id,
        agent_key=key,
        params=params,
        user_id=principal.user_id,
        trigger="api",
        commit=True,
    )

    if payload.async_execution:
        background.add_task(execute_run, run.id)
    else:
        await execute_run(run.id)
        await session.refresh(run)

    await audit.record(
        session,
        org_id=principal.org_id,
        action="agent.run_queued",
        actor_id=principal.user_id,
        actor_label=principal.user.email if principal.user else "",
        resource="run",
        resource_id=run.id,
        message=f"{key}",
        ip=client_ip(request),
        commit=True,
    )
    return {"run_id": run.id, "status": run.status, "agent": key}


@router.get("/runs")
async def list_runs(
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
    agent: str | None = None,
    status: str | None = None,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    query = select(Run).where(Run.org_id == principal.org_id)
    if agent:
        query = query.where(Run.agent_key == agent)
    if status:
        query = query.where(Run.status == status)
    total = (
        await session.execute(select(sa_func.count()).select_from(query.subquery()))
    ).scalar_one()
    rows = (
        await session.execute(query.order_by(Run.created_at.desc()).offset(offset).limit(limit))
    ).scalars().all()
    return {
        "total": int(total),
        "runs": [run_payload(r) for r in rows],
        "runs_today": await runs_today(session, principal.org_id),
        "daily_limit": principal.org.daily_run_limit,
    }


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    run = await session.get(Run, run_id)
    if run is None or run.org_id != principal.org_id:
        raise NotFound("التشغيل غير موجود")
    data = run_payload(run)
    data["steps"] = run.steps
    data["output"] = run.output
    data["metrics"] = run.metrics
    data["input"] = run.input
    return data


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    principal: Principal = Depends(require_role("editor")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    run = await session.get(Run, run_id)
    if run is None or run.org_id != principal.org_id:
        raise NotFound("التشغيل غير موجود")
    if run.status in (RunStatus.QUEUED, RunStatus.RUNNING):
        run.status = RunStatus.CANCELLED
        run.finished_at = run.finished_at
        await session.commit()
    return {"ok": True, "status": run.status}


def run_payload(run: Run) -> dict[str, Any]:
    return {
        "id": run.id,
        "agent": run.agent_key,
        "department": run.department,
        "status": run.status,
        "trigger": run.trigger,
        "created_at": run.created_at.isoformat(),
        "duration_ms": run.duration_ms,
        "tokens_in": run.tokens_in,
        "tokens_out": run.tokens_out,
        "cost_usd": round(int(run.cost_micros or 0) / 1_000_000, 6),
        "credits": run.credits,
        "llm_calls": run.llm_calls,
        "error": run.error,
        "summary": (run.output or {}).get("summary", ""),
    }


# ------------------------------------------------------------------ brand --


@router.get("/brand")
async def get_brand(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    brand = (
        await session.execute(select(BrandProfile).where(BrandProfile.org_id == principal.org_id))
    ).scalar_one_or_none()
    if brand is None:
        return {"brand": None}
    return {"brand": {c.name: getattr(brand, c.name) for c in brand.__table__.columns}}


@router.put("/brand")
async def update_brand(
    payload: BrandIn,
    request: Request,
    principal: Principal = Depends(require_role("manager")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    brand = (
        await session.execute(select(BrandProfile).where(BrandProfile.org_id == principal.org_id))
    ).scalar_one_or_none()
    if brand is None:
        brand = BrandProfile(org_id=principal.org_id, company_name=principal.org.name)
        session.add(brand)
        await session.flush()

    from realign.core.sanitize import clean_long, clean_tags

    LIST_FIELDS = {
        "tone_keywords", "competitors", "keywords", "hashtags", "locales", "sample_posts",
    }
    LONG_FIELDS = {"audience", "offer", "voice", "rules", "never_say", "sample_posts"}

    for field, value in payload.model_dump(exclude_none=True).items():
        if field in LIST_FIELDS:
            setattr(brand, field, clean_tags(value))
        elif field in LONG_FIELDS and isinstance(value, str):
            setattr(brand, field, clean_long(value))
        elif isinstance(value, str):
            setattr(brand, field, clean_long(value, limit=2000))
        else:
            setattr(brand, field, value)
    await session.commit()
    await audit.record(
        session,
        org_id=principal.org_id,
        action="brand.updated",
        actor_id=principal.user_id,
        actor_label=principal.user.email if principal.user else "",
        resource="brand_profile",
        resource_id=brand.id,
        ip=client_ip(request),
        commit=True,
    )
    return {"ok": True, "id": brand.id}


# -------------------------------------------------------------- schedules --


@router.get("/schedules")
async def list_schedules(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = (
        await session.execute(select(Schedule).where(Schedule.org_id == principal.org_id))
    ).scalars().all()
    return {
        "schedules": [
            {
                "id": s.id,
                "agent_key": s.agent_key,
                "cron": s.cron,
                "timezone": s.timezone,
                "enabled": s.enabled,
                "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
                "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
                "payload": s.payload,
            }
            for s in rows
        ]
    }


@router.post("/schedules", status_code=201)
async def create_schedule(
    payload: ScheduleIn,
    principal: Principal = Depends(require_role("manager")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if get_agent(payload.agent_key) is None:
        raise NotFound("الوكيل غير موجود")
    next_run = compute_next(payload.cron, payload.timezone)
    if next_run is None:
        raise ValidationError("تعبير cron غير صالح")
    row = Schedule(
        org_id=principal.org_id,
        agent_key=payload.agent_key,
        cron=payload.cron,
        timezone=payload.timezone,
        payload=payload.payload,
        enabled=payload.enabled,
        next_run_at=next_run,
    )
    session.add(row)
    await session.commit()
    return {"id": row.id, "next_run_at": next_run.isoformat()}


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(
    schedule_id: str,
    principal: Principal = Depends(require_role("manager")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await session.get(Schedule, schedule_id)
    if row is None or row.org_id != principal.org_id:
        raise NotFound("الجدولة غير موجودة")
    await session.delete(row)
    await session.commit()
    return {"ok": True}
