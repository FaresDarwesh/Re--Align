#!/usr/bin/env python3
"""Seed a demo tenant so the platform is explorable the moment it boots.

Creates:
  * plans (if missing)
  * demo organization + owner
  * a filled-in Arabic brand file
  * one run of every department agent
  * pending approvals in the queue

Safe to run repeatedly: it is idempotent by organization slug.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from realign.agents.engine import enqueue_run, execute_run  # noqa: E402
from realign.config import settings  # noqa: E402
from realign.core.logging import setup_logging  # noqa: E402
from realign.core.utils import utcnow  # noqa: E402
from realign.db import SessionLocal, init_db  # noqa: E402
from realign.models import BrandProfile, Organization, Run, User  # noqa: E402
from realign.services.onboarding import create_organization, seed_plans  # noqa: E402

DEMO_EMAIL = "admin@re-align.app"
DEMO_PASSWORD = "Re-Align#Admin2026"
DEMO_ORG = "شركة رايزن للتقنية"

BRAND = dict(
    company_name="رايزن للحلول الرقمية",
    industry="برمجيات وخدمات رقمية",
    audience=(
        "أصحاب الشركات الصغيرة والمتوسطة في مصر والسعودية (٢٥-٤٥ سنة) الذين يديرون التسويق "
        "بأنفسهم ويعانون من تعدد الأدوات وعدم وضوح العائد."
    ),
    offer=(
        "نظام وكلاء أذكياء يشغّل التسويق والمبيعات وخدمة العملاء من مكان واحد، "
        "بموافقة بشرية على كل ما يُنشر. يبدأ من ٤٩٩ جنيه شهرياً."
    ),
    voice=(
        "عربي مصري خفيف، مباشر وودود. جُمل قصيرة. أرقام واضحة بدل العبارات العامة. "
        "بدون مبالغة تسويقية وبدون وعود بنتائج مضمونة."
    ),
    tone_keywords=["واضح", "عملي", "ودود", "مباشر", "بأرقام"],
    rules=(
        "١) لا نذكر أسماء عملاء بدون إذن. "
        "٢) لا نسب محددة للنتائج — نقول «في المتوسط» أو «في حالات دراسية». "
        "٣) كل منشور ينتهي بدعوة واحدة واضحة. "
        "٤) لا سياسة ولا دين ولا محتوى حساس."
    ),
    never_say="مضمون ١٠٠٪ · أرباح مؤكدة · الأفضل في مصر · استثمار بدون مخاطرة",
    competitors=["أدوات أتمتة أجنبية", "وكالات تسويق تقليدية", "أنظمة CRM منفصلة"],
    keywords=["أتمتة التسويق", "وكلاء ذكاء اصطناعي", "توفير الوقت", "إدارة المحتوى", "CRM عربي"],
    hashtags=["#أتمتة", "#ذكاء_اصطناعي", "#تسويق_رقمي", "#شركات_صغيرة"],
    locales=["ar", "en"],
)


async def main() -> None:
    setup_logging()
    await init_db()

    async with SessionLocal() as session:
        await seed_plans(session)

        # Look the demo tenant up by e-mail — the slug is derived from the
        # company name and does not survive a rename.
        user = (
            await session.execute(select(User).where(User.email == DEMO_EMAIL))
        ).scalars().first()

        if user is None:
            org, user = await create_organization(
                session,
                name=DEMO_ORG,
                owner_email=DEMO_EMAIL,
                owner_password=DEMO_PASSWORD,
                owner_name="مدير التجربة",
                plan_code="growth",
            )
            print(f"✓ created organization: {org.name} ({org.slug})")
        else:
            org = await session.get(Organization, user.org_id)
            org.name = DEMO_ORG
            print(f"• demo tenant already exists: {org.name}")

        org.status = "active"
        org.credits_monthly = 100_000

        brand = (
            await session.execute(select(BrandProfile).where(BrandProfile.org_id == org.id))
        ).scalar_one_or_none()
        for key, value in BRAND.items():
            setattr(brand, key, value)
        await session.commit()
        print("✓ brand file filled")

        # ---- run one agent per department -----------------------------------
        demo_jobs = [
            ("research", {"topic": "السوق المصري للشركات الصغيرة", "auto_chain": True}),
            ("content", {"angle": "توفير ١٥ ساعة أسبوعياً في التسويق", "channel": "instagram", "kind": "post"}),
            ("creative", {"channel": "multi"}),
            ("sales", {"message": "عايز أعرف أسعاركم وهل فيه تجربة مجانية؟", "lead_name": "أحمد", "channel": "whatsapp"}),
            ("support", {"message": "الاشتراك خصم مني مرتين!! عايز استرجاع فوري", "customer_name": "محمد", "channel": "whatsapp"}),
            ("audience", {"topic": "٣ خطوات لأتمتة التسويق", "emails": 3}),
            ("distribution", {"limit": 5}),
            ("analytics", {"days": 7}),
            ("finance", {"days": 30}),
            ("cio", {"objective": "زيادة الإيراد مع الحفاظ على هامش ٣٠٪", "days": 30}),
            ("orchestrator", {"goal": "أطلق حملة تسويقية متكاملة هذا الأسبوع", "execute": False}),
        ]

        for agent_key, params in demo_jobs:
            run = await enqueue_run(
                session, org_id=org.id, agent_key=agent_key, params=params, trigger="seed", commit=True
            )
            await execute_run(run.id)
            await session.refresh(run)
            done = run
            status = done.status
            cost = (done.cost_micros or 0) / 1_000_000
            summary = (done.output or {}).get("summary", "") or (done.error or "")
            print(f"  → {agent_key:<14} {status:<18} ${cost:.4f}  {summary[:70]}")

        # ---- some history for the finance/analytics reports -----------------
        print("\n✓ demo data ready")
        print(f"\n  الدخول:  {DEMO_EMAIL}  /  {DEMO_PASSWORD}")
        print(f"  الرابط:  {settings.app_base_url}")
        print(f"  قاعدة البيانات: {settings.database_url}")


if __name__ == "__main__":
    asyncio.run(main())
