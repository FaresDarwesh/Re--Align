"""Integrations (connectors) and the audit log."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from realign.models.base import Base, StrEnum, TimestampMixin, uuid_pk


class IntegrationStatus(StrEnum):
    NOT_CONFIGURED = "not_configured"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ERROR = "error"
    PENDING_OAUTH = "pending_oauth"


class Integration(TimestampMixin, Base):
    """One connector instance for one tenant. Credentials are encrypted at rest."""

    __tablename__ = "integrations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    connector_key: Mapped[str] = mapped_column(String(60), nullable=False)   # telegram | instagram | ...
    display_name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=IntegrationStatus.NOT_CONFIGURED
    )
    credentials_enc: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    scopes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AuditLog(TimestampMixin, Base):
    """Append-only record of every meaningful action (who / what / when / how)."""

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False, default="user")  # user|agent|system|api
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actor_label: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    resource: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    resource_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="info")  # info|warn|critical
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    meta: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
