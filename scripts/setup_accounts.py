"""Reset the demo workspace's accounts.

Run:  python scripts/setup_accounts.py

What it does, idempotently:

1. Deletes the old `demo@nexusdemo.com` account (and everything tied to it).
2. Renames the demo workspace so no trace of the old product name remains.
3. Creates the platform administrator account.
4. Creates a second, cheaper account for hands-on exploration.

Passwords can be overridden with the env vars below; the defaults are the ones
handed to the operator.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from realign.config import settings  # noqa: E402
from realign.core.security import hash_password, validate_password_strength  # noqa: E402
from realign.db import SessionLocal, close_db, init_db  # noqa: E402
from realign.models import Organization, User  # noqa: E402
from realign.services.onboarding import provision_org, seed_plans  # noqa: E402

OLD_EMAIL = "demo@nexusdemo.com"
ADMIN_EMAIL = os.environ.get("REALIGN_ADMIN_EMAIL", "admin@re-align.app")
ADMIN_PASSWORD = os.environ.get("REALIGN_ADMIN_PASSWORD", "Re-Align#Admin2026")
ADMIN_NAME = os.environ.get("REALIGN_ADMIN_NAME", "مدير منصة re-align")

DEMO_EMAIL = os.environ.get("REALIGN_DEMO_EMAIL", "demo@re-align.app")
DEMO_PASSWORD = os.environ.get("REALIGN_DEMO_PASSWORD", "Re-Align#Demo2026")
DEMO_NAME = os.environ.get("REALIGN_DEMO_NAME", "حساب تجريبي")

ORG_NAME = "شركة رايزن للتقنية"
ORG_SLUG = "raizen-demo"


def _check(password: str, label: str) -> None:
    ok, message = validate_password_strength(password)
    if not ok:
        raise SystemExit(f"{label}: كلمة المرور ضعيفة — {message}")


async def main() -> None:
    _check(ADMIN_PASSWORD, ADMIN_EMAIL)
    _check(DEMO_PASSWORD, DEMO_EMAIL)

    await init_db()
    async with SessionLocal() as session:
        await seed_plans(session)

        # ---------------------------------------------------------- delete --
        old = (
            await session.execute(select(User).where(User.email == OLD_EMAIL))
        ).scalars().all()
        for user in old:
            await session.delete(user)
            print(f"🗑  deleted account: {user.email}")
        if not old:
            print(f"·  nothing to delete for {OLD_EMAIL}")

        # ------------------------------------------------- workspace rename --
        org = (
            await session.execute(select(Organization).order_by(Organization.created_at))
        ).scalars().first()
        if org is None:
            raise SystemExit("لا توجد مؤسسة — شغّل scripts/seed.py أولاً")
        org.name = ORG_NAME
        org.slug = ORG_SLUG
        org.billing_email = ADMIN_EMAIL
        org.locale = "ar"
        await session.flush()

        # ---------------------------------------------------------- upsert --
        for email, password, name, role, platform_admin in (
            (ADMIN_EMAIL, ADMIN_PASSWORD, ADMIN_NAME, "owner", True),
            (DEMO_EMAIL, DEMO_PASSWORD, DEMO_NAME, "admin", False),
        ):
            user = (
                await session.execute(select(User).where(User.email == email))
            ).scalars().first()
            if user is None:
                user = User(
                    org_id=org.id,
                    email=email,
                    hashed_password=hash_password(password),
                    full_name=name,
                    role=role,
                    is_active=True,
                    is_platform_admin=platform_admin,
                    locale="ar",
                )
                session.add(user)
                print(f"✅ created {email} ({role}{', platform admin' if platform_admin else ''})")
            else:
                user.hashed_password = hash_password(password)
                user.full_name = name
                user.role = role
                user.is_active = True
                user.is_platform_admin = platform_admin
                user.failed_login_attempts = 0
                user.locked_until = None
                print(f"🔄 updated {email} ({role}{', platform admin' if platform_admin else ''})")

        await session.flush()
        await provision_org(session, org)
        await session.commit()

        print()
        print("─" * 58)
        print(f"  workspace : {org.name}  ({org.slug})")
        print(f"  admin     : {ADMIN_EMAIL}  /  {ADMIN_PASSWORD}")
        print(f"  demo      : {DEMO_EMAIL}  /  {DEMO_PASSWORD}")
        print("─" * 58)

    await close_db()


if __name__ == "__main__":
    print(f"database: {settings.database_url}")
    asyncio.run(main())
