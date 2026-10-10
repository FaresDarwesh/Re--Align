"""Test harness: isolated SQLite file per session, workers off."""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio

_TMP = tempfile.mkdtemp(prefix="realign-test-")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{Path(_TMP) / 'test.db'}"
os.environ["APP_ENV"] = "development"
os.environ["WORKER_ENABLED"] = "false"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["APP_SECRET_KEY"] = "test-secret-key-not-for-production-use-0123456789"
os.environ["LOG_LEVEL"] = "WARNING"

from httpx import ASGITransport, AsyncClient  # noqa: E402

from realign.core.security import hash_password  # noqa: E402
from realign.db import SessionLocal, close_db, init_db  # noqa: E402
from realign.main import app as realign_app  # noqa: E402
from realign.models import Organization, User  # noqa: E402
from realign.services.onboarding import provision_org, seed_plans  # noqa: E402


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest_asyncio.fixture(scope="session", autouse=True)
async def database() -> AsyncGenerator[None, None]:
    await init_db()
    async with SessionLocal() as session:
        await seed_plans(session)
        await session.commit()
    yield
    await close_db()


async def _make_org(name: str, email: str, plan: str = "starter") -> tuple[Organization, User]:
    """Create a tenant. E-mail and slug are always unique (both are UNIQUE in
    the schema), so the fixtures can be re-created on every test."""
    import uuid as _uuid

    from realign.core.utils import new_uuid, slugify

    local, _, domain = email.partition("@")
    email = f"{local}+{_uuid.uuid4().hex[:10]}@{domain}"

    async with SessionLocal() as session:
        org = Organization(
            name=name,
            slug=slugify(name) + "-" + new_uuid()[:8],
            status="active",
            plan_code=plan,
            credits_monthly=10000,
            credits_used=0,
        )
        session.add(org)
        await session.flush()
        user = User(
            org_id=org.id,
            email=email,
            hashed_password=hash_password("password12345"),
            full_name="Test Owner",
            role="owner",
            is_active=True,
        )
        session.add(user)
        await session.flush()
        await provision_org(session, org)
        await session.commit()
        await session.refresh(org)
        await session.refresh(user)
        return org, user


@pytest_asyncio.fixture
async def org_a() -> tuple[Organization, User]:
    return await _make_org("شركة ألف", "a@alpha-test.io")


@pytest_asyncio.fixture
async def org_b() -> tuple[Organization, User]:
    return await _make_org("شركة باء", "b@beta-test.io")


def auth_headers(user: User) -> dict[str, str]:
    from realign.core.security import create_access_token

    token, _ = create_access_token(
        sub=user.id, org_id=user.org_id, role=user.role, email=user.email
    )
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=realign_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
