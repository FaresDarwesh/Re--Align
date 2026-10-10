"""Organizations (tenants), users, API keys, invitations."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from realign.models.base import Base, StrEnum, TimestampMixin, uuid_pk


class OrgStatus(StrEnum):
    ACTIVE = "active"
    TRIAL = "trial"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"


class UserRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MANAGER = "manager"
    APPROVER = "approver"
    EDITOR = "editor"
    VIEWER = "viewer"


class Organization(TimestampMixin, Base):
    """A tenant. Every business table carries `org_id`; nothing is ever queried
    across tenants without an explicit platform-admin path."""

    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=OrgStatus.TRIAL)

    # commercial
    plan_code: Mapped[str] = mapped_column(String(50), nullable=False, default="starter")
    seats_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    billing_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_customer_id: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # metering + safety rails
    credits_monthly: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    credits_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    credits_reset_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    monthly_budget_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=50000)
    per_run_budget_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    daily_run_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=500)

    # behaviour
    locale: Mapped[str] = mapped_column(String(8), nullable=False, default="ar")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Africa/Cairo")
    kill_switch: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    auto_publish: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    settings: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    branding: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # bring-your-own-key LLM
    llm_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    llm_api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_base_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    llm_thinking_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    llm_decision_model: Mapped[str | None] = mapped_column(String(120), nullable=True)

    users: Mapped[list["User"]] = relationship(back_populates="organization", cascade="all, delete-orphan")

    @property
    def credits_remaining(self) -> int:
        return max(self.credits_monthly - self.credits_used, 0)

    @property
    def is_operational(self) -> bool:
        return self.status in (OrgStatus.ACTIVE, OrgStatus.TRIAL) and not self.kill_switch


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = ({"sqlite_autoincrement": False},)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    org_id: Mapped[str] = mapped_column(String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=UserRole.VIEWER)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    locale: Mapped[str] = mapped_column(String(8), nullable=False, default="ar")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    failed_login_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Forgot-password flow: only ever stores a *hash* of the one-time token,
    # and the token is single-use + short lived.
    password_reset_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    password_reset_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    organization: Mapped["Organization"] = relationship(back_populates="users")

    @property
    def is_locked(self) -> bool:
        from realign.core.utils import utcnow

        return bool(self.locked_until and self.locked_until.replace(tzinfo=None) > utcnow().replace(tzinfo=None))


class ApiKey(TimestampMixin, Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    org_id: Mapped[str] = mapped_column(String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    prefix: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    hashed: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    scopes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Invitation(TimestampMixin, Base):
    __tablename__ = "invitations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    org_id: Mapped[str] = mapped_column(String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=UserRole.VIEWER)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
