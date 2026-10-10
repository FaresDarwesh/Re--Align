"""Organization settings (tenant) + audit trail."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from realign.core.errors import ValidationError
from realign.db import get_session
from realign.models import AuditLog, Organization
from realign.models.identity import OrgStatus
from realign.services import audit
from realign.services.auth import Principal, client_ip, get_principal, require_role

router = APIRouter(tags=["organization"])


class OrgUpdate(BaseModel):
    name: str | None = None
    locale: str | None = None
    timezone: str | None = None
    auto_publish: bool | None = None
    monthly_budget_cents: int | None = None
    per_run_budget_cents: int | None = None
    daily_run_limit: int | None = None
    branding: dict[str, Any] | None = None


class LLMKeyIn(BaseModel):
    provider: str = Field(pattern="^(openai|anthropic|ollama|custom|mock)$")
    api_key: str = Field(default="", max_length=512)
    base_url: str = Field(default="", max_length=512)
    thinking_model: str = Field(default="", max_length=120)
    decision_model: str = Field(default="", max_length=120)


@router.get("/org")
async def get_org(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    org = principal.org
    return {
        "organization": {
            "id": org.id,
            "name": org.name,
            "slug": org.slug,
            "status": org.status,
            "plan_code": org.plan_code,
            "locale": org.locale,
            "timezone": org.timezone,
            "auto_publish": org.auto_publish,
            "kill_switch": org.kill_switch,
            "seats_limit": org.seats_limit,
            "credits_monthly": org.credits_monthly,
            "credits_used": org.credits_used,
            "credits_remaining": org.credits_remaining,
            "monthly_budget_cents": org.monthly_budget_cents,
            "per_run_budget_cents": org.per_run_budget_cents,
            "daily_run_limit": org.daily_run_limit,
            "branding": org.branding,
            "llm": {
                "provider": org.llm_provider,
                "base_url": org.llm_base_url,
                "thinking_model": org.llm_thinking_model,
                "decision_model": org.llm_decision_model,
                "has_key": bool(org.llm_api_key_enc),
            },
        }
    }


@router.patch("/org")
async def update_org(
    payload: OrgUpdate,
    request: Request,
    principal: Principal = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    org = principal.org
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(org, field, value)
    await session.commit()
    await audit.record(
        session,
        org_id=org.id,
        action="org.updated",
        actor_id=principal.user_id,
        actor_label=principal.user.email if principal.user else "",
        resource="organization",
        resource_id=org.id,
        meta=payload.model_dump(exclude_none=True),
        ip=client_ip(request),
        commit=True,
    )
    return {"ok": True}


@router.put("/org/llm-key")
async def set_llm_key(
    payload: LLMKeyIn,
    request: Request,
    principal: Principal = Depends(require_role("owner")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Bring-Your-Own-Key: the tenant's own provider key and models."""
    from realign.core.security import encrypt_dict
    from realign.llm.router import router as llm_router

    org = principal.org

    base_url = (payload.base_url or "").strip()
    if base_url:
        # A tenant-supplied endpoint is an SSRF vector: block internal targets.
        from realign.core.netsafe import assert_public_url
        from realign.core.sanitize import safe_external_url

        normalised = safe_external_url(base_url)
        if normalised is None:
            raise ValidationError("عنوان غير صالح — المسموح http أو https فقط")
        try:
            assert_public_url(normalised)
        except Exception as exc:  # noqa: BLE001
            raise ValidationError("لا يُسمح بعنوان يشير إلى شبكة داخلية") from exc
        base_url = normalised

    from realign.core.sanitize import clean_label

    org.llm_provider = payload.provider
    org.llm_api_key_enc = encrypt_dict({"k": payload.api_key}).get("k") if payload.api_key else None
    org.llm_base_url = base_url or None
    org.llm_thinking_model = clean_label(payload.thinking_model, limit=120) or None
    org.llm_decision_model = clean_label(payload.decision_model, limit=120) or None
    await session.commit()
    llm_router.invalidate(org.id)

    await audit.record(
        session,
        org_id=org.id,
        action="org.llm_key_updated",
        actor_id=principal.user_id,
        actor_label=principal.user.email if principal.user else "",
        resource="organization",
        resource_id=org.id,
        severity="warn",
        message=payload.provider,
        ip=client_ip(request),
        commit=True,
    )
    return {"ok": True, "provider": payload.provider}


@router.get("/audit")
async def audit_trail(
    limit: int = Query(50, ge=1, le=300),
    action: str | None = None,
    severity: str | None = None,
    principal: Principal = Depends(require_role("manager")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    query = select(AuditLog).where(AuditLog.org_id == principal.org_id)
    if action:
        query = query.where(AuditLog.action == action)
    if severity:
        query = query.where(AuditLog.severity == severity)
    rows = (
        await session.execute(query.order_by(AuditLog.created_at.desc()).limit(limit))
    ).scalars().all()
    return {
        "logs": [
            {
                "id": e.id,
                "action": e.action,
                "actor_type": e.actor_type,
                "actor_label": e.actor_label,
                "resource": e.resource,
                "resource_id": e.resource_id,
                "severity": e.severity,
                "message": e.message,
                "created_at": e.created_at.isoformat(),
                "meta": e.meta,
            }
            for e in rows
        ]
    }
