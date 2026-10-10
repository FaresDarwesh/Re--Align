"""Liveness, readiness and system diagnostics."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from realign.agents.registry import list_agents_meta
from realign.config import settings
from realign.connectors.registry import connector_keys
from realign.core.utils import utcnow
from realign.db import get_session
from realign.llm.router import router as llm_router

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "app": settings.app_name,
        "env": settings.app_env,
        "time": utcnow().isoformat(),
        "kill_switch_global": settings.kill_switch_global,
    }


@router.get("/health/ready")
async def readiness(session: AsyncSession = Depends(get_session)) -> dict:
    db_ok = True
    db_error = ""
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        db_error = str(exc)

    llm = await llm_router.health()
    return {
        "status": "ok" if db_ok else "degraded",
        "database": {"ok": db_ok, "dialect": settings.database_url.split(":")[0], "error": db_error},
        "llm": llm,
        "agents": len(list_agents_meta()),
        "connectors": len(connector_keys()),
        "worker_enabled": settings.worker_enabled,
        "scheduler_enabled": settings.scheduler_enabled,
    }


@router.get("/meta")
async def meta() -> dict:
    return {
        "app_name": settings.app_name,
        "tagline": settings.app_tagline,
        "locale": settings.default_locale,
        "timezone": settings.default_timezone,
        "currency": settings.billing_currency,
        "billing_provider": settings.billing_provider,
        "allow_public_signup": settings.allow_public_signup,
        "agents": list_agents_meta(),
        "connectors": connector_keys(),
    }
