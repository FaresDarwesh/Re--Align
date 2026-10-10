#!/usr/bin/env python3
"""Create (or promote) a platform administrator, and optionally rebuild demo data.

Interactive, safe to re-run:

  python scripts/create_admin.py
  ADMIN_EMAIL=you@company.com ADMIN_PASSWORD=... python scripts/create_admin.py --yes
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from realign.core.security import hash_password  # noqa: E402
from realign.core.utils import new_uuid  # noqa: E402
from realign.db import SessionLocal, init_db  # noqa: E402
from realign.models import Organization, User  # noqa: E402


async def main(email: str, password: str, name: str, org_name: str) -> int:
    await init_db()
    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()

        if user is not None:
            user.hashed_password = hash_password(password)
            user.role = "owner"
            user.is_active = True
            await session.commit()
            print(f"✓ حدّثنا المستخدم {email} (صلاحية: مالك/مدير)")
            return 0

        org = (await session.execute(select(Organization).order_by(Organization.created_at))).scalars().first()
        if org is None:
            org = Organization(
                id=new_uuid(), name=org_name, slug="admin-org", status="active", plan_code="business",
                credits_monthly=20000, credits_used=0,
            )
            session.add(org)
            await session.flush()
            print(f"✓ أنشأنا مؤسسة: {org.name}")

        session.add(
            User(
                id=new_uuid(), org_id=org.id, email=email,
                hashed_password=hash_password(password), full_name=name,
                role="owner", is_active=True,
            )
        )
        await session.commit()
        print(f"✓ أنشأنا المدير {email} داخل «{org.name}»")
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true", help="لا تسأل، استخدم متغيرات البيئة")
    args = parser.parse_args()

    email = os.getenv("ADMIN_EMAIL", "").strip()
    password = os.getenv("ADMIN_PASSWORD", "")
    name = os.getenv("ADMIN_NAME", "مدير النظام")
    org_name = os.getenv("ADMIN_ORG", "مؤسستي")

    if not args.yes:
        email = input("إيميل المدير: ").strip() or email
        password = password or getpass.getpass("كلمة السر (٨ أحرف على الأقل): ")

    if not email or "@" not in email:
        print("✗ إيميل غير صحيح", file=sys.stderr)
        raise SystemExit(1)
    if len(password) < 8:
        print("✗ كلمة السر قصيرة جداً", file=sys.stderr)
        raise SystemExit(1)

    raise SystemExit(asyncio.run(main(email, password, name, org_name)))
