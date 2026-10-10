"""Agents, runs, approvals, content assets, brand files, schedules, outbox."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from realign.models.base import Base, StrEnum, TimestampMixin, uuid_pk


class BrandProfile(TimestampMixin, Base):
    """The 'Brand Files' — plain structured text every agent reads before it
    writes a single word: voice, offer, audience, hard rules."""

    __tablename__ = "brand_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    company_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    industry: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    audience: Mapped[str] = mapped_column(Text, nullable=False, default="")
    offer: Mapped[str] = mapped_column(Text, nullable=False, default="")
    voice: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tone_keywords: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    rules: Mapped[str] = mapped_column(Text, nullable=False, default="")
    never_say: Mapped[str] = mapped_column(Text, nullable=False, default="")
    competitors: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    keywords: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    hashtags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    locales: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    sample_posts: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    extra: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class Agent(TimestampMixin, Base):
    """A configured agent instance inside one tenant."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(60), nullable=False)          # research | content | ...
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    name_ar: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    department: Mapped[str] = mapped_column(String(60), nullable=False, default="general")
    description_ar: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    schedule_cron: Mapped[str | None] = mapped_column(String(80), nullable=True)
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    tools: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    per_run_budget_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cost_micros: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = ({"sqlite_autoincrement": False},)


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class Run(TimestampMixin, Base):
    """One execution of one agent. The unit of cost, audit and observability."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    agent_key: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    department: Mapped[str] = mapped_column(String(60), nullable=False, default="general")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=RunStatus.QUEUED, index=True)
    trigger: Mapped[str] = mapped_column(String(30), nullable=False, default="manual")  # manual|schedule|api|event|parent
    parent_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    requested_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    input: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    output: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    steps: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    artifacts: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_micros: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    credits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(120), nullable=True)

    queued_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"
    EXPIRED = "expired"


class Approval(TimestampMixin, Base):
    """The human gate. Nothing reaches a real customer or spends real money
    without a row here moving to `approved`."""

    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    agent_key: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(40), nullable=False, default="publish")  # publish|budget|send|edge_case
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    channel: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=ApprovalStatus.PENDING, index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5)  # 1 = highest
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    asset_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    requested_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decision_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    risk_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    risk_flags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)


class AssetStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    REJECTED = "rejected"
    FAILED = "failed"


class ContentAsset(TimestampMixin, Base):
    """Anything an agent produces: post, article, script, ad copy, email..."""

    __tablename__ = "content_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    agent_key: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(40), nullable=False, default="post")  # post|article|script|ad|email|image_prompt|report
    title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    channel: Mapped[str] = mapped_column(String(40), nullable=False, default="general")
    locale: Mapped[str] = mapped_column(String(8), nullable=False, default="ar")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=AssetStatus.DRAFT, index=True)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    external_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    external_id: Mapped[str | None] = mapped_column(String(190), nullable=True)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    seo_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    brand_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    meta: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class Schedule(TimestampMixin, Base):
    __tablename__ = "schedules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    agent_key: Mapped[str] = mapped_column(String(60), nullable=False)
    cron: Mapped[str] = mapped_column(String(80), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Africa/Cairo")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class OutboxStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    DEAD = "dead"


class OutboxEvent(TimestampMixin, Base):
    """Durable background work queue (publishes, follow-ups, webhooks).

    Written in the same DB transaction as the business change, so a crash can
    never lose work. Retried with exponential backoff."""

    __tablename__ = "outbox_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(60), nullable=False, index=True)  # publish_asset|send_message|...
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=OutboxStatus.PENDING, index=True)
    run_after: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
