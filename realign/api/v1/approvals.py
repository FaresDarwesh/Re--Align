"""The human gate API — plus assets (content) and the kill switch."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from realign.core.errors import NotFound
from realign.core.utils import utcnow
from realign.db import get_session
from realign.models import Approval, ContentAsset
from realign.models.operations import ApprovalStatus, AssetStatus
from realign.services import approvals as svc
from realign.services.auth import Principal, client_ip, get_principal, require_role

router = APIRouter(tags=["approvals"])


class DecisionIn(BaseModel):
    decision: str = Field(pattern="^(approve|reject|changes_requested)$")
    note: str = ""


class BulkDecisionIn(BaseModel):
    ids: list[str]
    decision: str = Field(pattern="^(approve|reject|changes_requested)$")
    note: str = ""


@router.get("/approvals")
async def list_approvals(
    status: str = Query("pending", pattern="^(pending|approved|rejected|changes_requested|all)$"),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    query = select(Approval).where(Approval.org_id == principal.org_id)
    if status != "all":
        query = query.where(Approval.status == status)
    rows = (
        await session.execute(
            query.order_by(Approval.priority.asc(), Approval.created_at.desc()).limit(limit)
        )
    ).scalars().all()
    return {
        "pending_count": await svc.pending_count(session, principal.org_id),
        "approvals": [approval_payload(a) for a in rows],
    }


@router.post("/approvals/{approval_id}/decide")
async def decide(
    approval_id: str,
    payload: DecisionIn,
    request: Request,
    principal: Principal = Depends(require_role("approver")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    approval = await svc.decide(
        session,
        approval_id=approval_id,
        org_id=principal.org_id,
        decision=payload.decision,
        user_id=principal.user_id,
        note=payload.note,
        actor_label=principal.user.email if principal.user else "",
        ip=client_ip(request),
    )
    return {"ok": True, "approval": approval_payload(approval)}


@router.post("/approvals/bulk")
async def bulk_decide(
    payload: BulkDecisionIn,
    request: Request,
    principal: Principal = Depends(require_role("approver")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await svc.bulk_decide(
        session,
        org_id=principal.org_id,
        approval_ids=payload.ids,
        decision=payload.decision,
        user_id=principal.user_id,
        note=payload.note,
        actor_label=principal.user.email if principal.user else "",
        ip=client_ip(request),
    )


def approval_payload(a: Approval) -> dict[str, Any]:
    return {
        "id": a.id,
        "kind": a.kind,
        "title": a.title,
        "body": a.body,
        "channel": a.channel,
        "agent_key": a.agent_key,
        "status": a.status,
        "priority": a.priority,
        "risk_score": a.risk_score,
        "risk_flags": a.risk_flags,
        "asset_id": a.asset_id,
        "run_id": a.run_id,
        "decision_note": a.decision_note,
        "created_at": a.created_at.isoformat(),
        "decided_at": a.decided_at.isoformat() if a.decided_at else None,
    }


# ------------------------------------------------------------------ assets --


@router.get("/assets")
async def list_assets(
    kind: str | None = None,
    status: str | None = None,
    channel: str | None = None,
    limit: int = Query(40, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    query = select(ContentAsset).where(ContentAsset.org_id == principal.org_id)
    if kind:
        query = query.where(ContentAsset.kind == kind)
    if status:
        query = query.where(ContentAsset.status == status)
    if channel:
        query = query.where(ContentAsset.channel == channel)
    rows = (
        await session.execute(query.order_by(ContentAsset.created_at.desc()).limit(limit))
    ).scalars().all()
    return {"assets": [asset_payload(a) for a in rows]}


@router.get("/assets/{asset_id}")
async def get_asset(
    asset_id: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    asset = await session.get(ContentAsset, asset_id)
    if asset is None or asset.org_id != principal.org_id:
        raise NotFound("الأصل غير موجود")
    return {"asset": asset_payload(asset, full=True)}


@router.post("/assets/{asset_id}/requeue")
async def requeue_asset(
    asset_id: str,
    principal: Principal = Depends(require_role("approver")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from realign.workers.queue import enqueue

    asset = await session.get(ContentAsset, asset_id)
    if asset is None or asset.org_id != principal.org_id:
        raise NotFound("الأصل غير موجود")
    if asset.status not in (AssetStatus.APPROVED, AssetStatus.FAILED):
        raise NotFound("لا يمكن إعادة النشر في هذه الحالة")
    event_id = await enqueue(
        org_id=principal.org_id,
        type="publish_asset",
        payload={"asset_id": asset.id, "channel": asset.channel},
    )
    return {"ok": True, "event_id": event_id}


def asset_payload(a: ContentAsset, full: bool = False) -> dict[str, Any]:
    data = {
        "id": a.id,
        "kind": a.kind,
        "title": a.title,
        "channel": a.channel,
        "locale": a.locale,
        "status": a.status,
        "summary": a.summary,
        "score": a.score,
        "seo_score": a.seo_score,
        "brand_score": a.brand_score,
        "scheduled_for": a.scheduled_for.isoformat() if a.scheduled_for else None,
        "published_at": a.published_at.isoformat() if a.published_at else None,
        "external_url": a.external_url,
        "created_at": a.created_at.isoformat(),
        "agent_key": a.agent_key,
    }
    if full:
        data["body"] = a.body
        data["meta"] = a.meta
        data["metrics"] = a.metrics
        data["publish_error"] = (a.meta or {}).get("publish_error")
    return data


# ------------------------------------------------------------- kill switch --


@router.post("/kill-switch")
async def toggle_kill_switch(
    enabled: bool,
    request: Request,
    principal: Principal = Depends(require_role("owner")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    org = principal.org
    org.kill_switch = bool(enabled)
    await session.commit()
    from realign.services import audit

    await audit.record(
        session,
        org_id=org.id,
        action="org.kill_switch",
        actor_id=principal.user_id,
        actor_label=principal.user.email if principal.user else "",
        resource="organization",
        resource_id=org.id,
        severity="critical" if enabled else "warn",
        message="تشغيل" if not enabled else "إيقاف",
        ip=client_ip(request),
        commit=True,
    )
    return {"ok": True, "kill_switch": org.kill_switch}


@router.get("/overview")
async def overview(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    org = principal.org
    pending = await svc.pending_count(session, org.id)
    assets_by_status = dict(
        (
            await session.execute(
                select(ContentAsset.status, sa_func.count(ContentAsset.id))
                .where(ContentAsset.org_id == org.id)
                .group_by(ContentAsset.status)
            )
        ).all()
    )
    return {
        "organization": {
            "name": org.name,
            "status": org.status,
            "plan": org.plan_code,
            "kill_switch": org.kill_switch,
            "credits_remaining": org.credits_remaining,
            "credits_monthly": org.credits_monthly,
        },
        "pending_approvals": pending,
        "assets_by_status": {k: int(v) for k, v in assets_by_status.items()},
        "runs_today": None,
        "now": utcnow().isoformat(),
    }
