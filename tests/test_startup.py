"""Regression tests for the production startup crash (Vercel dpl_Fv25nUuF…).

Two defects caused every request to 500 with "exit status: 1":

1. ``realign.main._bootstrap_owner`` referenced ``SessionLocal`` which was
   only imported *locally inside* ``lifespan`` — a guaranteed ``NameError``
   as soon as ADMIN_EMAIL/ADMIN_PASSWORD were configured.
2. Any failing startup stage crashed the process without a structured log
   identifying where it died.

These tests fail against the pre-fix code (NameError / missing event).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from realign import main as main_mod
from realign.db import Base
from realign.models import User


class _LogRecorder:
    """Minimal stand-in for the structlog bound logger."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def _rec(self, level: str):
        def _log(event: str, **kw: object) -> None:
            self.events.append((level, event, kw))
        return _log

    debug = property(lambda self: self._rec("debug"))
    info = property(lambda self: self._rec("info"))
    warning = property(lambda self: self._rec("warning"))
    error = property(lambda self: self._rec("error"))
    exception = property(lambda self: self._rec("exception"))


# ------------------------------------------------- SessionLocal scope defect


async def test_bootstrap_owner_resolves_session_local(monkeypatch):
    """Pre-fix this raises NameError: name 'SessionLocal' is not defined.

    The session-scoped test DB already has users, so on fixed code the
    function simply counts them and returns — proving the name resolves.
    """
    monkeypatch.setattr(main_mod.settings, "admin_email", "root@example.test")
    monkeypatch.setattr(main_mod.settings, "admin_password", "Str0ng!Pass#2026")

    await main_mod._bootstrap_owner()  # must not raise


async def test_bootstrap_owner_creates_first_admin_on_empty_db(tmp_path, monkeypatch):
    """Full bootstrap path: empty DB + ADMIN_* configured → owner created."""
    from realign.services.onboarding import seed_plans

    engine2 = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}")
    async with engine2.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session2 = async_sessionmaker(engine2, class_=AsyncSession, expire_on_commit=False)
    async with Session2() as session:  # plans must exist for plan_code='growth'
        await seed_plans(session)

    monkeypatch.setattr(main_mod, "SessionLocal", Session2)
    monkeypatch.setattr(main_mod.settings, "admin_email", "Root@Example.test")
    monkeypatch.setattr(main_mod.settings, "admin_password", "Str0ng!Pass#2026")
    monkeypatch.setattr(main_mod.settings, "admin_org_name", "شركة الاختبار")

    await main_mod._bootstrap_owner()

    async with Session2() as session:
        users = (await session.execute(select(User))).scalars().all()
    assert len(users) == 1
    assert users[0].email == "root@example.test"
    assert users[0].is_platform_admin is True
    assert users[0].role == "owner"
    await engine2.dispose()


# -------------------------------------------------- startup error reporting


async def test_startup_failed_identifies_database_stage(monkeypatch):
    """init_db crash → startup_failed{stage=database_init} AND still raises."""
    rec = _LogRecorder()
    monkeypatch.setattr(main_mod, "log", rec)

    async def boom() -> None:
        raise RuntimeError("synthetic db failure")

    monkeypatch.setattr(main_mod, "init_db", boom)

    with pytest.raises(RuntimeError):
        async with main_mod.lifespan(main_mod.app):
            pass  # pragma: no cover - unreachable: startup must fail

    failures = [(e, kw) for lvl, e, kw in rec.events if e == "startup_failed"]
    assert failures, "startup_failed event was not logged"
    _, kw = failures[0]
    assert kw["stage"] == "database_init"
    assert kw["error_class"] == "RuntimeError"
    # Diagnostics must stay free of secrets.
    assert set(kw) == {"stage", "error_class"}


async def test_startup_failed_identifies_later_stage(monkeypatch):
    """seed_plans crash → the reported stage advances to plan_seeding."""
    import realign.services.onboarding as onboarding

    rec = _LogRecorder()
    monkeypatch.setattr(main_mod, "log", rec)

    async def ok_init() -> None:
        return None

    async def boom(session) -> int:  # noqa: ANN001
        raise ValueError("synthetic seeding failure")

    monkeypatch.setattr(main_mod, "init_db", ok_init)
    monkeypatch.setattr(onboarding, "seed_plans", boom)

    with pytest.raises(ValueError):
        async with main_mod.lifespan(main_mod.app):
            pass  # pragma: no cover

    failures = [(e, kw) for lvl, e, kw in rec.events if e == "startup_failed"]
    assert failures
    assert failures[0][1]["stage"] == "plan_seeding"
    assert failures[0][1]["error_class"] == "ValueError"


async def test_startup_sequence_order(monkeypatch):
    """preflight → init_db → seed_plans → _bootstrap_owner → serverless init."""
    import realign.services.onboarding as onboarding

    calls: list[str] = []
    rec = _LogRecorder()
    monkeypatch.setattr(main_mod, "log", rec)

    async def fake_init() -> None:
        calls.append("database_init")

    async def fake_seed(session) -> int:  # noqa: ANN001
        calls.append("plan_seeding")
        return 0

    async def fake_bootstrap() -> None:
        calls.append("admin_bootstrap")

    monkeypatch.setattr(main_mod, "init_db", fake_init)
    monkeypatch.setattr(onboarding, "seed_plans", fake_seed)
    monkeypatch.setattr(main_mod, "_bootstrap_owner", fake_bootstrap)
    monkeypatch.setattr(main_mod.settings, "serverless", True)

    async with main_mod.lifespan(main_mod.app):
        pass

    assert calls == ["database_init", "plan_seeding", "admin_bootstrap"]
    assert any(e == "serverless_mode" for _, e, _ in rec.events)
