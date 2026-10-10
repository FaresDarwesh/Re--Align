"""re-align — Multi-tenant AI Agent Operating System.

Application factory: logging, schema bootstrap, routers, worker + scheduler
lifecycle, and safety guards that refuse to run insecurely in production.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from realign.api.v1.router import api_router
from realign.config import settings
from realign.core.errors import AuthenticationError, ReAlignError, register_exception_handlers
from realign.core.headers import (
    HTTPSRedirectMiddleware,
    RequestLimitMiddleware,
    SecurityHeadersMiddleware,
    api_rate_scope,
    rate_limit_headers,
)
from realign.core.logging import get_logger, setup_logging
from realign.core.ratelimit import client_ip, limiter
from realign.db import close_db, init_db

log = get_logger("realign.main")

DESCRIPTION = """
منصة **re-align** لتشغيل وكلاء الذكاء الاصطناعي داخل الشركات.

* Multi-tenant: كل مؤسسة معزولة تماماً عن غيرها.
* ١١ وكيلاً في ٩ أقسام: بحث، محتوى، إبداع، توزيع، جمهور، تحليلات، مبيعات، دعم، مالية.
* بوابة موافقة بشرية على كل ما يُنشر أو يُصرف.
* قياس دقيق للتكلفة وحدود إنفاق صارمة.
* ربطات جاهزة (تيليجرام، ويب هوك، SMTP، RSS) وأخرى عند توفير المفاتيح.

المصادقة: `Authorization: Bearer <access_token>` أو `X-API-Key: nxk_...`
"""


def _preflight_checks() -> None:
    """Refuse to start insecurely instead of failing quietly later."""
    if not settings.is_production:
        return
    problems: list[str] = []
    if settings.app_secret_key.startswith("dev-insecure") or len(settings.app_secret_key) < 32:
        problems.append("APP_SECRET_KEY قصير أو افتراضي — ولّده بـ: openssl rand -hex 32")
    if not settings.credentials_encryption_key:
        problems.append("CREDENTIALS_ENCRYPTION_KEY غير مضبوط — ولّده بـ Fernet.generate_key()")
    if settings.database_url.startswith("sqlite"):
        problems.append("SQLite غير مناسب للإنتاج — استخدم PostgreSQL")
    if not settings.force_https:
        problems.append("FORCE_HTTPS معطّل في الإنتاج")
    if not settings.csrf_enabled:
        problems.append("CSRF معطّل في الإنتاج")
    if not settings.rate_limit_enabled:
        problems.append("تحديد المعدّل (rate limiting) معطّل في الإنتاج")
    if problems:
        for p in problems:
            log.error("preflight_failed", problem=p)
        raise SystemExit("فشل فحص ما قبل التشغيل:\n- " + "\n- ".join(problems))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    _preflight_checks()
    log.info("starting", app=settings.app_name, env=settings.app_env, db=settings.database_url.split(":")[0])

    from realign.services.onboarding import seed_plans

    await init_db()
    from realign.db import SessionLocal

    async with SessionLocal() as session:
        created = await seed_plans(session)
        if created:
            log.info("plans_seeded", count=created)

    await _bootstrap_owner()

    # On hosts where no background asyncio task can survive, runs are
    # executed inline (see `realign.agents.engine.enqueue_run`) and the
    # scheduler is replaced by an HTTP-triggered cron endpoint.
    if settings.serverless:
        log.info("serverless_mode", worker="inline", scheduler="cron endpoint /api/v1/cron/tick")
    else:
        from realign.workers.queue import worker
        from realign.workers.scheduler import scheduler

        worker.start()
        scheduler.start()

    yield

    if not settings.serverless:
        await scheduler.stop()
        await worker.stop()
    await close_db()
    log.info("shutdown_complete")


async def _bootstrap_owner() -> None:
    """Create the first owner account on an empty database.

    Without this, a fresh production deploy would have no way to sign in: public
    signup creates a *new* workspace, and there would be nobody to administer
    the platform. Runs once; the moment any user exists it does nothing.
    """
    from sqlalchemy import func, select

    from realign.models import User
    from realign.services.onboarding import create_organization

    password = (settings.admin_password or "").strip()
    email = (settings.admin_email or "").strip().lower()
    if not password or not email or "@" not in email:
        log.info("bootstrap_skipped", reason="no ADMIN_PASSWORD configured")
        return

    async with SessionLocal() as session:
        total = (await session.execute(select(func.count()).select_from(User))).scalar_one()
        if total:
            return
        try:
            org, user = await create_organization(
                session,
                name=settings.admin_org_name or "شركتي",
                owner_email=email,
                owner_password=password,
                owner_name="مدير المنصة",
                plan_code="growth",
            )
            user.is_platform_admin = True
            user.role = "owner"
            await session.commit()
            log.warning("bootstrap_owner_created", email=email, org=org.slug,
                        note="غيّر كلمة المرور فوراً من صفحة الإعدادات")
        except Exception as exc:  # noqa: BLE001 - a failed bootstrap must not stop boot
            await session.rollback()
            log.warning("bootstrap_owner_failed", error=str(exc)[:200])



def _register_not_found(app: FastAPI) -> None:
    """A branded 404 instead of a bare JSON blob.

    API paths keep their JSON contract; everything a browser might hit gets a
    real page in the site's own language.
    """

    @app.exception_handler(404)
    async def _not_found(request: Request, exc: Exception):  # noqa: ANN401
        if request.url.path.startswith("/api/") or request.url.path.startswith("/static/"):
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "not_found", "message": "المسار غير موجود"}},
            )
        from realign.web.routes import public_context, templates

        return templates.TemplateResponse(
            request=request,
            name="404.html",
            context=public_context(request, app_name=settings.app_name),
            status_code=404,
        )


def create_app() -> FastAPI:
    expose_docs = settings.expose_docs or not settings.is_production
    app = FastAPI(
        title=settings.app_name,
        description=DESCRIPTION,
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if expose_docs else None,
        redoc_url="/redoc" if expose_docs else None,
        openapi_url="/openapi.json" if expose_docs else None,
    )

    # CORS: an explicit allowlist. `*` is never used with credentials, and in
    # production only the configured origin (plus its www variant) is accepted.
    if settings.is_production:
        origins = settings.allowed_host_list or [settings.app_base_url]
        origins = list(dict.fromkeys([o.rstrip("/") for o in origins if o]))
    else:
        origins = ["http://localhost:8000", "http://127.0.0.1:8000"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_origin_regex=None,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-CSRF-Token", "X-Request-ID"],
        expose_headers=["X-Request-ID", "X-RateLimit-Remaining", "X-RateLimit-Retry-After"],
        max_age=600,
    )

    # Outermost → innermost: request budget, TLS enforcement, security headers.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(HTTPSRedirectMiddleware)
    app.add_middleware(RequestLimitMiddleware)

    register_exception_handlers(app)
    _register_not_found(app)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        import time
        import uuid

        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        started = time.perf_counter()

        # Per-IP + per-endpoint budget for the JSON API (DDoS / brute force).
        if settings.rate_limit_enabled and request.url.path.startswith("/api/v1/") \
                and not request.url.path.startswith("/api/v1/health"):
            scope = api_rate_scope(request.url.path, request.method)
            ip = client_ip(request)
            identity = ip
            if scope.startswith("auth.login"):
                body_marker = (request.headers.get("x-account-hint") or "").strip()
                identity = f"{ip}|{body_marker[:80]}" if body_marker else ip
            allowed, remaining, retry_after = await limiter.check(scope, identity)
            if not allowed:
                response = JSONResponse(
                    status_code=429,
                    content={
                        "error": {
                            "code": "rate_limited",
                            "message": "محاولات كثيرة جداً — حاول بعد قليل",
                            "retry_after": retry_after,
                        }
                    },
                    headers={"Retry-After": str(retry_after), **rate_limit_headers(0, retry_after)},
                )
                duration_ms = int((time.perf_counter() - started) * 1000)
                response.headers["X-Request-ID"] = request_id
                response.headers["X-Response-Time"] = f"{duration_ms}ms"
                return response
            request.state.rate_remaining = remaining

        try:
            response = await call_next(request)
        except AuthenticationError as exc:
            response = JSONResponse(status_code=401, content=exc.to_dict())
        except ReAlignError as exc:
            response = JSONResponse(status_code=exc.status_code, content=exc.to_dict())
        except Exception:
            log.exception("unhandled_request", path=request.url.path)
            response = JSONResponse(
                status_code=500,
                content={"error": {"code": "internal_error", "message": "حدث خطأ غير متوقع"}},
            )
        duration_ms = int((time.perf_counter() - started) * 1000)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time"] = f"{duration_ms}ms"
        remaining = getattr(request.state, "rate_remaining", None)
        if remaining is not None:
            response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response

    app.include_router(api_router)

    from realign.web.legal import router as legal_router
    from realign.web.routes import router as web_router

    app.include_router(web_router)
    app.include_router(legal_router)

    static_dir = os.path.join(os.path.dirname(__file__), "web", "static")
    os.makedirs(static_dir, exist_ok=True)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # Uploaded media lives outside the code tree and is served read-only.
    from realign.core.uploads import _media_root

    media_root = _media_root()
    app.mount("/media", StaticFiles(directory=str(media_root)), name="media")

    return app


app = create_app()
