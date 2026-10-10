"""Regression tests for the Vercel `database_init` failure (dpl_fbsnfWWU…).

`OSError: [Errno 16] Device or resource busy` inside the SSL connect is how
the AWS Lambda network layer reports a socket it cannot establish — most
commonly an unresolvable host (projectdiscovery/httpx#2273). A PGHOST pasted
*with surrounding quotes* produces exactly such a host.

Covered here:
- PG* component vars are cleaned of pasted quotes (config).
- Safe endpoint diagnostics expose type/suffix/port only.
- Bounded retry for transient socket errors; persistent errors surface at once.
- Serverless pool sizing.
- requirements.txt must not drag uvloop back in (the stdlib asyncio loop is
  the proven path on the serverless runtime; the app never imports uvloop).
"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from realign import db as db_mod
from realign.config import Settings

NEON_POOLED = "ep-bold-star-b4bg51sx-pooler.c-6.us-east-2.aws.neon.tech"


# ------------------------------------------------------- config: quote cleanup


def test_pg_host_with_pasted_quotes_is_cleaned():
    s = Settings(
        pghost=f'"{NEON_POOLED}"',
        pguser="'neondb_owner'",
        pgpassword='"s3cr#t/p@ss"',
        pgdatabase='"neondb"',
    )
    url = s.database_url
    assert url.startswith("postgresql+asyncpg://")
    assert f"@{NEON_POOLED}:5432/neondb?" in url
    for quote_char in ('"', "'"):
        assert quote_char not in url.split("://", 1)[1].split("?")[0]
    assert "sslmode=require" in url  # TLS preserved


def test_pg_host_plain_is_untouched():
    s = Settings(pghost=NEON_POOLED, pguser="neondb_owner",
                 pgpassword="p@ss#1", pgdatabase="neondb")
    assert f"@{NEON_POOLED}:5432/neondb?" in s.database_url


# ------------------------------------------------------------- endpoint facts


def test_endpoint_facts_pooled_neon(monkeypatch):
    monkeypatch.setattr(
        db_mod.settings, "database_url",
        f"postgresql+asyncpg://neondb_owner:secret@{NEON_POOLED}:5432/neondb",
    )
    facts = db_mod._endpoint_facts()
    assert facts["endpoint"] == "neon-pooled"
    assert facts["host_tail"] == "c-6.us-east-2.aws.neon.tech"
    assert facts["port"] == 5432
    # Never credentials, never the full URL:
    assert "secret" not in repr(facts)
    assert "neondb_owner" not in repr(facts)
    assert NEON_POOLED not in repr(facts)


def test_endpoint_facts_direct_neon(monkeypatch):
    monkeypatch.setattr(
        db_mod.settings, "database_url",
        "postgresql+asyncpg://u:p@ep-x-y.c-6.us-east-2.aws.neon.tech:5432/neondb",
    )
    assert db_mod._endpoint_facts()["endpoint"] == "neon-direct"


# -------------------------------------------------------------- bounded retry


async def _nosleep(delay: float) -> None:
    return None


async def test_attempt_retries_transient_then_succeeds():
    calls: list[int] = []
    slept: list[float] = []

    async def flaky() -> None:
        calls.append(1)
        if len(calls) < 3:
            raise OSError(errno.EBUSY, "Device or resource busy")

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    await db_mod._attempt("op", flaky, _sleep=fake_sleep)
    assert len(calls) == 3
    assert slept == [0.5, 1.0]  # linear backoff, bounded


async def test_attempt_raises_persistent_error_without_retry():
    calls: list[int] = []

    async def broken() -> None:
        calls.append(1)
        raise PermissionError(errno.EACCES, "permission denied")

    with pytest.raises(PermissionError):
        await db_mod._attempt("op", broken, _sleep=_nosleep)
    assert len(calls) == 1  # no pointless retries


async def test_attempt_transient_failure_is_bounded():
    calls: list[int] = []

    async def always_busy() -> None:
        calls.append(1)
        raise OSError(errno.EBUSY, "Device or resource busy")

    with pytest.raises(OSError):
        await db_mod._attempt("op", always_busy, _sleep=_nosleep)
    assert len(calls) == 3  # bounded: 3 attempts, then the real error surfaces


def test_transient_detection_walks_wrapped_chain():
    inner = OSError(errno.EBUSY, "Device or resource busy")
    wrapper = RuntimeError("sqlalchemy wrapped it")
    wrapper.__cause__ = inner
    assert db_mod._transient_errno(wrapper) == errno.EBUSY

    class _SAError(Exception):  # mimics SQLAlchemyError(.orig)
        def __init__(self, orig: Exception) -> None:
            self.orig = orig

    assert db_mod._transient_errno(_SAError(inner)) == errno.EBUSY
    assert db_mod._transient_errno(ValueError("nope")) is None


# -------------------------------------------------------------- pool sizing


def test_engine_kwargs_serverless_pool_is_small(monkeypatch):
    monkeypatch.setattr(db_mod.settings, "database_url",
                        f"postgresql+asyncpg://u:p@{NEON_POOLED}/neondb")
    monkeypatch.setattr(db_mod.settings, "serverless", True)
    kwargs = db_mod._engine_kwargs()
    assert kwargs["pool_size"] <= 5
    assert kwargs["max_overflow"] <= 5
    assert kwargs["pool_recycle"] == 300
    assert kwargs["pool_pre_ping"] is True


def test_engine_kwargs_non_serverless_keeps_defaults(monkeypatch):
    monkeypatch.setattr(db_mod.settings, "database_url",
                        f"postgresql+asyncpg://u:p@{NEON_POOLED}/neondb")
    monkeypatch.setattr(db_mod.settings, "serverless", False)
    kwargs = db_mod._engine_kwargs()
    assert kwargs["pool_size"] == db_mod.settings.db_pool_size
    assert kwargs["max_overflow"] == db_mod.settings.db_max_overflow


# ------------------------------------------------------- dependency discipline


def test_requirements_do_not_install_uvloop():
    text = Path(db_mod.__file__).resolve().parent.parent.joinpath("requirements.txt").read_text()
    # Only real requirement lines count — comments may mention uvloop by name.
    reqs = [ln.split("#", 1)[0].strip() for ln in text.splitlines()]
    reqs = [r for r in reqs if r]
    assert not any("uvloop" in r for r in reqs)
    assert not any(r.startswith("uvicorn[") for r in reqs)
    assert any(r.startswith("uvicorn==") for r in reqs)
