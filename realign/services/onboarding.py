"""Tenant provisioning: plans, agents, brand scaffold, connector cards, trial."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from realign.agents.registry import list_agents
from realign.config import settings
from realign.connectors.registry import list_connectors
from realign.core.utils import add_months, slugify, utcnow
from realign.models import (
    Agent as AgentModel,
)
from realign.models import (
    BrandProfile,
    Integration,
    Organization,
    OrgStatus,
    Plan,
    Subscription,
    SubscriptionStatus,
    User,
    UserRole,
)
from realign.models.system import IntegrationStatus

DEFAULT_PLANS: list[dict] = [
    {
        "code": "starter",
        "name": "Starter",
        "name_ar": "المبتدئ",
        "description_ar": "لشخص واحد أو فريق صغير يبدأ بأتمتة قسم واحد.",
        "price_cents": 0,
        "credits_monthly": 500,
        "seats_limit": 2,
        "agent_limit": 3,
        "features": {"agents": 3, "departments": 2, "approvals": True, "audit_log_days": 7, "api": False},
        "sort_order": 1,
    },
    {
        "code": "growth",
        "name": "Growth",
        "name_ar": "النمو",
        "description_ar": "لفريق تسويق يعمل على كل القنوات مع بوابة موافقة كاملة.",
        "price_cents": 49900,  # 499.00 EGP
        "credits_monthly": 5000,
        "seats_limit": 10,
        "agent_limit": 11,
        "features": {"agents": 11, "departments": 6, "approvals": True, "audit_log_days": 90, "api": True, "scheduling": True},
        "sort_order": 2,
    },
    {
        "code": "business",
        "name": "Business",
        "name_ar": "الأعمال",
        "description_ar": "للشركات التي تشغّل عدة أقسام وتحتاج تقارير وصلاحيات متقدمة.",
        "price_cents": 149900,
        "credits_monthly": 20000,
        "seats_limit": 30,
        "agent_limit": 11,
        "features": {"agents": 11, "departments": 11, "approvals": True, "audit_log_days": 365, "api": True, "scheduling": True, "byok": True, "sso": False},
        "sort_order": 3,
    },
    {
        "code": "enterprise",
        "name": "Enterprise",
        "name_ar": "المؤسسات",
        "description_ar": "نشر خاص أو داخل الشركة، عقد مستقل وحدود مخصصة.",
        "price_cents": 0,
        "credits_monthly": 100000,
        "seats_limit": 500,
        "agent_limit": 11,
        "features": {"agents": 11, "departments": 11, "on_premise": True, "sso": True, "sla": True, "byok": True},
        "sort_order": 4,
        "is_public": False,
    },
]


async def seed_plans(session: AsyncSession) -> int:
    created = 0
    existing = set((await session.execute(select(Plan.code))).scalars().all())
    for spec in DEFAULT_PLANS:
        if spec["code"] in existing:
            continue
        session.add(Plan(**spec))
        created += 1
    if created:
        await session.commit()
    return created


async def ensure_agents(session: AsyncSession, org: Organization) -> int:
    existing = set(
        (
            await session.execute(select(AgentModel.key).where(AgentModel.org_id == org.id))
        ).scalars().all()
    )
    count = 0
    for agent in list_agents():
        if agent.key in existing:
            continue
        session.add(
            AgentModel(
                org_id=org.id,
                key=agent.key,
                name=agent.name,
                name_ar=agent.name_ar,
                department=agent.department,
                description_ar=agent.description_ar,
                enabled=True,
                requires_approval=agent.requires_approval,
                tools=list(agent.default_tools),
            )
        )
        count += 1
    if count:
        await session.commit()
    return count


async def ensure_integrations(session: AsyncSession, org: Organization) -> int:
    existing = set(
        (
            await session.execute(select(Integration.connector_key).where(Integration.org_id == org.id))
        ).scalars().all()
    )
    count = 0
    for connector in list_connectors():
        if connector.key in existing:
            continue
        env_creds = settings.integration_credentials(connector.key)
        session.add(
            Integration(
                org_id=org.id,
                connector_key=connector.key,
                display_name=connector.name_ar,
                status=IntegrationStatus.CONNECTED if connector.is_configured(env_creds) else IntegrationStatus.NOT_CONFIGURED,
            )
        )
        count += 1
    if count:
        await session.commit()
    return count


async def ensure_brand(session: AsyncSession, org: Organization) -> BrandProfile:
    result = await session.execute(select(BrandProfile).where(BrandProfile.org_id == org.id))
    brand = result.scalar_one_or_none()
    if brand is None:
        brand = BrandProfile(org_id=org.id, company_name=org.name)
        session.add(brand)
        await session.commit()
        await session.refresh(brand)
    return brand


async def ensure_subscription(session: AsyncSession, org: Organization, plan_code: str | None = None) -> Subscription:
    result = await session.execute(select(Subscription).where(Subscription.org_id == org.id))
    sub = result.scalars().first()
    if sub is not None:
        return sub
    now = utcnow().replace(tzinfo=None)
    sub = Subscription(
        org_id=org.id,
        plan_code=plan_code or org.plan_code or "starter",
        status=SubscriptionStatus.TRIALING,
        current_period_start=now,
        current_period_end=add_months(now, 1),
        trial_ends_at=add_months(now, 1),
        provider=settings.billing_provider or "manual",
    )
    session.add(sub)
    await session.commit()
    return sub


async def provision_org(session: AsyncSession, org: Organization) -> None:
    await ensure_agents(session, org)
    await ensure_integrations(session, org)
    await ensure_brand(session, org)
    await ensure_subscription(session, org)


async def create_organization(
    session: AsyncSession,
    *,
    name: str,
    owner_email: str,
    owner_password: str,
    owner_name: str = "",
    plan_code: str = "starter",
    locale: str | None = None,
) -> tuple[Organization, User]:
    from realign.core.security import hash_password

    base = slugify(name)
    slug = base
    n = 1
    while (await session.execute(select(Organization).where(Organization.slug == slug))).scalar_one_or_none():
        n += 1
        slug = f"{base}-{n}"

    plan = (
        await session.execute(select(Plan).where(Plan.code == plan_code))
    ).scalar_one_or_none()
    credits = int(plan.credits_monthly) if plan else int(settings.default_credits_per_month)

    org = Organization(
        name=name,
        slug=slug,
        status=OrgStatus.TRIAL,
        plan_code=plan_code,
        seats_limit=int(plan.seats_limit) if plan else 3,
        credits_monthly=credits,
        credits_used=0,
        locale=locale or settings.default_locale,
        timezone=settings.default_timezone,
        billing_email=owner_email,
        monthly_budget_cents=settings.default_monthly_budget_cents,
        per_run_budget_cents=settings.default_per_run_budget_cents,
        daily_run_limit=settings.default_daily_run_limit,
    )
    session.add(org)
    await session.flush()

    user = User(
        org_id=org.id,
        email=owner_email.strip().lower(),
        hashed_password=hash_password(owner_password),
        full_name=owner_name or owner_email.split("@")[0],
        role=UserRole.OWNER,
        is_active=True,
    )
    session.add(user)
    await session.flush()

    await provision_org(session, org)
    await session.commit()
    await session.refresh(org)
    await session.refresh(user)
    return org, user
