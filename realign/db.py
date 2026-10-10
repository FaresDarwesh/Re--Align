"""Async SQLAlchemy engine, session factory and schema bootstrap."""

from __future__ import annotations

import asyncio
import errno as _errno
import ssl as _sslmod
from collections.abc import AsyncGenerator
from typing import Any

import sqlalchemy as sa
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool

from realign.config import settings
from realign.core.logging import get_logger

log = get_logger("realign.db")


class Base(DeclarativeBase):
    """Declarative base with a JSON-friendly repr helper."""

    def as_dict(self) -> dict[str, Any]:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


def _prepare_postgres_url() -> tuple[str, dict[str, Any]]:
    """Translate libpq-style URL params into what asyncpg actually accepts.

    asyncpg's ``connect()`` rejects ``sslmode`` / ``channel_binding`` as
    keyword arguments (``TypeError: connect() got an unexpected keyword
    argument 'sslmode'``), yet SQLAlchemy forwards every URL query parameter
    into ``connect_args``. Managed providers (Neon etc.) always ship URLs
    with ``?sslmode=require``, so we strip those params here and express the
    TLS requirement as a real ``ssl.SSLContext`` via ``connect_args["ssl"]``.
    """
    url = make_url(settings.database_url)
    if url.get_backend_name() != "postgresql":
        return str(url), {}

    query = dict(url.query)
    sslmode = (query.pop("sslmode", None) or "").strip().lower() or None
    query.pop("channel_binding", None)

    connect_args: dict[str, Any] = {}
    if sslmode == "disable":
        connect_args["ssl"] = False
    elif sslmode == "require":
        ctx = _sslmod.SSLContext(_sslmod.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = _sslmod.CERT_NONE
        connect_args["ssl"] = ctx
    elif sslmode in ("verify-ca", "verify-full"):
        ctx = _sslmod.create_default_context()
        if sslmode == "verify-ca":
            ctx.check_hostname = False
        connect_args["ssl"] = ctx
    # "prefer" / "allow" / absent: keep asyncpg's own negotiation defaults.

    return str(url.set(query=query)), connect_args


_PREPARED_URL, _PG_CONNECT_ARGS = (
    (settings.database_url, {}) if settings.is_sqlite else _prepare_postgres_url()
)


def _engine_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "echo": settings.sql_echo,
        "future": True,
        "pool_pre_ping": True,
    }
    if settings.is_sqlite:
        kwargs.update(
            {
                "connect_args": {"check_same_thread": False, "timeout": 30},
                "poolclass": StaticPool,
            }
        )
    else:
        if settings.serverless:
            # Serverless instances come and go (and get frozen mid-flight):
            # keep a handful of sockets per instance instead of dozens, and
            # recycle aggressively so thawed instances never reuse dead TCP.
            kwargs.update(
                {
                    "pool_size": min(settings.db_pool_size, 5),
                    "max_overflow": min(settings.db_max_overflow, 5),
                    "pool_timeout": 10,
                    "pool_recycle": 300,
                }
            )
        else:
            kwargs.update({"pool_size": settings.db_pool_size, "max_overflow": settings.db_max_overflow})
        if _PG_CONNECT_ARGS:
            kwargs["connect_args"] = dict(_PG_CONNECT_ARGS)
    return kwargs


engine = create_async_engine(_PREPARED_URL, **_engine_kwargs())
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)


if settings.is_sqlite:

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        for pragma in (
            "PRAGMA journal_mode=WAL",
            "PRAGMA foreign_keys=ON",
            "PRAGMA synchronous=NORMAL",
            "PRAGMA busy_timeout=30000",
        ):
            try:
                cursor.execute(pragma)
            except Exception:  # noqa: BLE001
                pass
        cursor.close()


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


def _endpoint_facts() -> dict[str, Any]:
    """Safe connection diagnostics — endpoint *type*, domain suffix and port.

    Deliberately excludes credentials and the full URL: for
    ``ep-bold-star-b4bg51sx-pooler.c-6.us-east-2.aws.neon.tech`` this yields
    ``{endpoint: neon-pooled, host_tail: c-6.us-east-2.aws.neon.tech, port: 5432}``
    — enough to spot a wrong/pasted-with-quotes host without leaking anything.
    """
    try:
        url = make_url(settings.database_url)
    except Exception:  # noqa: BLE001 - diagnostics must never crash boot
        return {"endpoint": "unparsable", "host_tail": None, "port": None}
    host = url.host or ""
    labels = host.split(".")
    host_tail = ".".join(labels[1:]) if len(labels) >= 4 else host
    if ".neon.tech" in host:
        endpoint = "neon-pooled" if "-pooler." in host else "neon-direct"
    elif host:
        endpoint = "other"
    else:
        endpoint = "unknown"
    return {"endpoint": endpoint, "host_tail": host_tail, "port": url.port}


#: Socket errors worth one or two quiet retries. On AWS Lambda the network
#: layer maps several transient/unreachable-socket conditions onto EBUSY
#: (see projectdiscovery/httpx#2273), so a bounded retry with backoff is
#: cheap insurance; persistent failures surface immediately.
_TRANSIENT_ERRNOS = frozenset(
    filter(None, (
        getattr(_errno, "EBUSY", 16),
        getattr(_errno, "EAGAIN", 11),
        getattr(_errno, "ECONNRESET", 104),
        getattr(_errno, "ETIMEDOUT", 110),
    ))
)


def _transient_errno(exc: BaseException) -> int | None:
    """Walk the exception chain (SQLAlchemy wraps the driver's OSError)."""
    seen: set[int] = set()
    stack = [exc]
    while stack:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, OSError) and cur.errno in _TRANSIENT_ERRNOS:
            return cur.errno
        stack.extend((getattr(cur, "orig", None), cur.__cause__, cur.__context__))
    return None


async def _attempt(
    op: str,
    factory: Any,
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    _sleep: Any = asyncio.sleep,
) -> None:
    """Bounded retry for transient socket errors only.

    Non-transient errors (auth, permissions, DNS on a normal host, schema)
    are raised on the first occurrence — retries never hide them, and the
    whole sequence adds at most ~1.5s to a failing boot.
    """
    for attempt in range(1, attempts + 1):
        try:
            await factory()
            return
        except Exception as exc:  # noqa: BLE001 - classify, then re-raise
            transient = _transient_errno(exc)
            if transient is None or attempt == attempts:
                raise
            delay = base_delay * attempt
            log.warning(
                "db_transient_retry", op=op, attempt=attempt,
                errno=transient, retry_in_s=delay, **_endpoint_facts(),
            )
            await _sleep(delay)


async def init_db(*, create_extensions: bool = True) -> None:
    """Create all tables. Alembic owns migrations in production; this is the
    zero-friction path for dev/tests and first boot."""
    from realign import models  # noqa: F401  (register every mapper)

    log.info("db_endpoint", stage="database_init", **_endpoint_facts())

    async def _create_extension() -> None:
        async with engine.begin() as ext_conn:
            await ext_conn.execute(text('CREATE EXTENSION IF NOT EXISTS "pgcrypto"'))

    async def _create_schema() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_add_missing_columns)

    # Optional extension, isolated in its own transaction on purpose: on
    # managed Postgres (Neon) a non-superuser role can hit "permission
    # denied" here, which ABORTS the surrounding transaction — anything else
    # sharing that connection would then fail with InFailedSqlTransaction.
    # The app runs fine without pgcrypto (PG13+ ships gen_random_uuid
    # natively), so a failure is a warning, never a boot blocker. Genuine
    # connection failures surface again — loudly — in create_all below.
    if not settings.is_sqlite and create_extensions:
        try:
            await _attempt("create_extension", _create_extension)
        except Exception as exc:  # noqa: BLE001 - optional; never block boot
            log.warning("pgcrypto_extension_skipped", error_class=type(exc).__name__)

    await _attempt("create_schema", _create_schema)
    log.info("database_ready", url=settings.database_url.split("://")[0])


#: Columns added after the first release. `create_all` never alters an existing
#: table, so a fresh install gets them from the metadata and an existing one
#: gets them here. Idempotent: it inspects before it alters.
_EXTRA_COLUMNS: dict[str, dict[str, str]] = {
    "users": {
        "password_reset_token_hash": "VARCHAR(128)",
        "password_reset_expires_at": "DATETIME",
    },
}


def _add_missing_columns(sync_conn) -> None:
    """Cheap, idempotent `ALTER TABLE … ADD COLUMN` for pre-Alembic installs."""
    inspector = sa.inspect(sync_conn)
    for table, columns in _EXTRA_COLUMNS.items():
        if not inspector.has_table(table):
            continue
        existing = {col["name"] for col in inspector.get_columns(table)}
        for name, ddl in columns.items():
            if name in existing:
                continue
            try:
                sync_conn.execute(
                    sa.text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                )
                log.info("column_added", table=table, column=name)
            except Exception:  # noqa: BLE001 - never block startup on this
                log.warning("column_add_failed", table=table, column=name)


async def close_db() -> None:
    await engine.dispose()
