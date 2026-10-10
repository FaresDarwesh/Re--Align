"""The human gate: decide an approval and execute exactly what was approved.

Nothing else in the system publishes or spends. If it is not here, it does not
happen.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from realign.agents.engine import resume_approved_run
from realign.connectors.registry import resolve_channel_connector
from realign.core.errors import NotFound, PermissionDenied
from realign.core.logging import get_logger
from realign.core.utils import utcnow
from realign.models import Approval, ContentAsset, Integration
from realign.models.operations import ApprovalStatus, AssetStatus
from realign.services import audit
from realign.workers.queue import enqueue as outbox_enqueue

log = get_logger("realign.approvals")


async def decide(
    session: AsyncSession,
    *,
    approval_id: str,
    org_id: str,
    decision: str,                       # approve | reject | changes_requested
    user_id: str | None = None,
    note: str = "",
    actor_label: str = "",
    ip: str = "",
) -> Approval:
    approval = await session.get(Approval, approval_id)
    if approval is None or approval.org_id != org_id:
        raise NotFound("طلب الموافقة غير موجود")
    if approval.status != ApprovalStatus.PENDING:
        raise PermissionDenied("هذا الطلب تم البت فيه بالفعل")

    decision = (decision or "").strip().lower()
    if decision not in ("approve", "reject", "changes_requested"):
        raise PermissionDenied("قرار غير معروف")

    approval.status = {
        "approve": ApprovalStatus.APPROVED,
        "reject": ApprovalStatus.REJECTED,
        "changes_requested": ApprovalStatus.CHANGES_REQUESTED,
    }[decision]
    approval.decided_by = user_id
    approval.decided_at = utcnow()
    approval.decision_note = note or ""
    await session.flush()

    asset = None
    if approval.asset_id:
        asset = await session.get(ContentAsset, approval.asset_id)

    executed: dict[str, Any] = {}

    if decision == "approve":
        if asset is not None:
            asset.status = AssetStatus.APPROVED
        if approval.kind == "publish" and asset is not None:
            channel = approval.channel or asset.channel or "general"
            event_id = await outbox_enqueue(
                org_id=org_id,
                type="publish_asset",
                payload={"asset_id": asset.id, "channel": channel},
            )
            executed = {"queued": "publish_asset", "event_id": event_id, "channel": channel}
        elif approval.kind == "send":
            payload = approval.payload or {}
            asset_id = payload.get("asset_id")
            body = ""
            subject = ""
            if asset is not None:
                body = asset.body
                subject = asset.title
            channel = approval.channel or payload.get("channel") or "email"
            connector = resolve_channel_connector(channel)
            event_id = await outbox_enqueue(
                org_id=org_id,
                type="send_message",
                payload={
                    "connector": connector.key,
                    "to": payload.get("to", ""),
                    "subject": subject,
                    "body": body,
                    "asset_id": asset_id,
                },
            )
            executed = {"queued": "send_message", "event_id": event_id, "connector": connector.key}
        elif approval.kind == "budget":
            executed = {"note": "تم اعتماد تغيير الميزانية"}
        else:
            executed = {"note": "تم الاعتماد"}
    elif decision == "reject" and asset is not None:
        asset.status = AssetStatus.REJECTED
        executed = {"note": "تم رفض المسوّدة"}
    elif decision == "changes_requested" and asset is not None:
        asset.status = AssetStatus.DRAFT
        executed = {"note": "أُعيدت المسوّدة للتعديل"}

    await audit.record(
        session,
        org_id=org_id,
        action=f"approval.{approval.status}",
        actor_type="user",
        actor_id=user_id,
        actor_label=actor_label,
        resource="approval",
        resource_id=approval.id,
        message=note or approval.status,
        severity="info",
        meta={"kind": approval.kind, "executed": executed},
        ip=ip,
    )

    await session.commit()
    await session.refresh(approval)

    # close the parent run when nothing is pending anymore
    if approval.run_id:
        await resume_approved_run(approval.run_id)

    log.info("approval_decided", approval=approval.id, org=org_id, decision=approval.status)
    return approval


async def bulk_decide(
    session: AsyncSession,
    *,
    org_id: str,
    approval_ids: list[str],
    decision: str,
    user_id: str | None = None,
    note: str = "",
    actor_label: str = "",
    ip: str = "",
) -> dict[str, Any]:
    approved, failed = 0, []
    for aid in approval_ids:
        try:
            await decide(
                session,
                approval_id=aid,
                org_id=org_id,
                decision=decision,
                user_id=user_id,
                note=note,
                actor_label=actor_label,
                ip=ip,
            )
            approved += 1
        except Exception as exc:  # noqa: BLE001
            failed.append({"id": aid, "error": str(exc)})
    return {"processed": approved, "failed": failed}


async def pending_count(session: AsyncSession, org_id: str) -> int:
    value = (
        await session.execute(
            select(sa_func.count(Approval.id)).where(
                Approval.org_id == org_id, Approval.status == ApprovalStatus.PENDING
            )
        )
    ).scalar_one()
    return int(value or 0)


async def pending_list(session: AsyncSession, org_id: str, limit: int = 50) -> list[Approval]:
    result = await session.execute(
        select(Approval)
        .where(Approval.org_id == org_id, Approval.status == ApprovalStatus.PENDING)
        .order_by(Approval.priority.asc(), Approval.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def connector_ready(session: AsyncSession, org_id: str, channel: str) -> bool:
    from realign.connectors.registry import credentials_for

    connector = resolve_channel_connector(channel)
    creds = await credentials_for(session, org_id=org_id, connector_key=connector.key)
    return connector.is_configured(creds)


async def touch_integration(session: AsyncSession, org_id: str, connector_key: str) -> None:
    row = (
        await session.execute(
            select(Integration).where(
                Integration.org_id == org_id, Integration.connector_key == connector_key
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        row.last_used_at = utcnow()
        row.usage_count = int(row.usage_count or 0) + 1
