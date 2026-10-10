"""Audit helper — every meaningful action leaves a trace."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from realign.models import AuditLog


async def record(
    session: AsyncSession,
    *,
    org_id: str = "",
    action: str,
    actor_type: str = "user",
    actor_id: str | None = None,
    actor_label: str = "",
    resource: str = "",
    resource_id: str | None = None,
    message: str = "",
    severity: str = "info",
    meta: dict[str, Any] | None = None,
    ip: str = "",
    request_id: str = "",
    commit: bool = False,
) -> AuditLog:
    entry = AuditLog(
        org_id=org_id or "",
        actor_type=actor_type,
        actor_id=actor_id,
        actor_label=actor_label,
        action=action,
        resource=resource,
        resource_id=resource_id,
        message=message,
        severity=severity,
        meta=meta or {},
        ip=ip,
        request_id=request_id,
    )
    session.add(entry)
    if commit:
        await session.commit()
    else:
        await session.flush()
    return entry
