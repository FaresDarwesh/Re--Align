"""Connector management: list, configure (encrypted), test, sync, remove."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from realign.config import settings
from realign.connectors.registry import (
    credentials_for,
    ensure_integration_rows,
    get_connector,
    list_connectors,
)
from realign.core.errors import NotFound, ValidationError
from realign.core.security import decrypt_dict, encrypt_dict
from realign.core.utils import mask_secret
from realign.core.utils import utcnow
from realign.db import get_session
from realign.models import Integration
from realign.models.system import IntegrationStatus
from realign.services import audit
from realign.services.auth import Principal, client_ip, get_principal, require_role

router = APIRouter(tags=["integrations"])


class CredentialsIn(BaseModel):
    credentials: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    display_name: str | None = None


class TestIn(BaseModel):
    to: str = ""
    subject: str = "اختبار من النظام"
    body: str = "هذه رسالة اختبار."


@router.get("/integrations")
async def list_all(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await ensure_integration_rows(session, principal.org_id)
    await session.commit()

    rows = (
        await session.execute(select(Integration).where(Integration.org_id == principal.org_id))
    ).scalars().all()
    by_key = {r.connector_key: r for r in rows}

    out = []
    for connector in list_connectors():
        row = by_key.get(connector.key)
        stored = decrypt_dict(row.credentials_enc) if row else {}
        effective = {**settings.integration_credentials(connector.key), **stored}
        status = row.status if row else IntegrationStatus.NOT_CONFIGURED
        if connector.is_configured(effective) and status == IntegrationStatus.NOT_CONFIGURED:
            status = IntegrationStatus.CONNECTED
        meta = connector.meta(status=status, configured_fields=connector.configured_fields(effective))
        meta.update(
            {
                "id": row.id if row else None,
                "display_name": (row.display_name if row else None) or connector.name_ar,
                "last_used_at": row.last_used_at.isoformat() if row and row.last_used_at else None,
                "last_sync_at": row.last_sync_at.isoformat() if row and row.last_sync_at else None,
                "last_error": row.last_error if row else None,
                "usage_count": row.usage_count if row else 0,
                "masked": {k: mask_secret(str(v)) for k, v in stored.items() if k in connector.required_credentials},
            }
        )
        out.append(meta)

    return {"integrations": out}


@router.put("/integrations/{key}")
async def configure(
    key: str,
    payload: CredentialsIn,
    request: Request,
    principal: Principal = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    connector = get_connector(key)
    if connector is None:
        raise NotFound("الربط غير موجود")

    row = (
        await session.execute(
            select(Integration).where(
                Integration.org_id == principal.org_id, Integration.connector_key == key
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = Integration(org_id=principal.org_id, connector_key=key, display_name=connector.name_ar)
        session.add(row)

    merged = {**(decrypt_dict(row.credentials_enc) or {}), **(payload.credentials or {})}
    row.credentials_enc = encrypt_dict(merged)
    row.config = {**(row.config or {}), **(payload.config or {})}
    if payload.display_name:
        row.display_name = payload.display_name
    row.status = IntegrationStatus.CONNECTED if connector.is_configured(merged) else IntegrationStatus.NOT_CONFIGURED
    row.last_error = None
    await session.commit()

    await audit.record(
        session,
        org_id=principal.org_id,
        action="integration.configured",
        actor_id=principal.user_id,
        actor_label=principal.user.email if principal.user else "",
        resource="integration",
        resource_id=row.id,
        message=key,
        meta={"fields": sorted((payload.credentials or {}).keys())},
        ip=client_ip(request),
        commit=True,
    )
    return {"ok": True, "status": row.status, "configured_fields": connector.configured_fields(merged)}


@router.post("/integrations/{key}/validate")
async def validate(
    key: str,
    principal: Principal = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    connector = get_connector(key)
    if connector is None:
        raise NotFound("الربط غير موجود")
    creds = await credentials_for(session, org_id=principal.org_id, connector_key=key)
    if not connector.is_configured(creds):
        return {"ok": False, "message": "بيانات الاعتماد غير مكتملة", "missing": [f for f in connector.required_credentials if not creds.get(f)]}
    try:
        result = await connector.validate(creds)
    except NotImplementedError:
        return {"ok": True, "message": "لا يوجد تحقق تلقائي لهذا الربط"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc)}

    row = (
        await session.execute(
            select(Integration).where(
                Integration.org_id == principal.org_id, Integration.connector_key == key
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        row.status = IntegrationStatus.CONNECTED if result.ok else IntegrationStatus.ERROR
        row.last_error = None if result.ok else result.message
        await session.commit()
    return {"ok": result.ok, "message": result.message, "details": result.details}


@router.post("/integrations/{key}/test")
async def test_send(
    key: str,
    payload: TestIn,
    principal: Principal = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    connector = get_connector(key)
    if connector is None:
        raise NotFound("الربط غير موجود")
    if "message" not in connector.capabilities and "publish" not in connector.capabilities:
        raise ValidationError("هذا الربط لا يدعم الإرسال")
    creds = await credentials_for(session, org_id=principal.org_id, connector_key=key)
    if not connector.is_configured(creds):
        return {"ok": False, "message": "بيانات الاعتماد غير مكتملة"}
    try:
        result = await connector.send(creds, to=payload.to, subject=payload.subject, body=payload.body)
    except NotImplementedError:
        return {"ok": False, "message": "الإرسال غير مدعوم"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc)}
    return {"ok": result.ok, "message": result.message, "external_id": result.external_id}


@router.delete("/integrations/{key}")
async def disconnect(
    key: str,
    request: Request,
    principal: Principal = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = (
        await session.execute(
            select(Integration).where(
                Integration.org_id == principal.org_id, Integration.connector_key == key
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("الربط غير موجود")
    row.credentials_enc = {}
    row.status = IntegrationStatus.DISCONNECTED
    row.last_used_at = None
    await session.commit()
    await audit.record(
        session,
        org_id=principal.org_id,
        action="integration.disconnected",
        actor_id=principal.user_id,
        actor_label=principal.user.email if principal.user else "",
        resource="integration",
        resource_id=row.id,
        message=key,
        severity="warn",
        ip=client_ip(request),
        commit=True,
    )
    return {"ok": True}


@router.post("/integrations/{key}/sync")
async def sync(
    key: str,
    principal: Principal = Depends(require_role("manager")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from realign.workers.queue import enqueue

    connector = get_connector(key)
    if connector is None:
        raise NotFound("الربط غير موجود")
    if "read" not in connector.capabilities:
        raise ValidationError("هذا الربط لا يدعم القراءة")
    event_id = await enqueue(org_id=principal.org_id, type="sync_integration", payload={"connector": key})
    return {"ok": True, "event_id": event_id}
