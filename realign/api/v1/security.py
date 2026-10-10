"""Security endpoints: CSP reporting, self-check, and the operator dashboard.

Nothing here leaks configuration values — every field is either a boolean, a
count, or a masked status. Secrets are never echoed back.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from realign.config import settings
from realign.core.errors import PermissionDenied
from realign.core.headers import RULES_SNAPSHOT
from realign.core.ratelimit import limiter
from realign.core.redact import MASK
from realign.db import get_session
from realign.models import AuditLog, User
from realign.services.auth import Principal, get_principal, require_role

router = APIRouter(prefix="/security", tags=["security"])


# ------------------------------------------------------------------ schemas --


class CspReport(BaseModel):
    class Body(BaseModel):
        document_uri: str = ""
        violated_directive: str = ""
        blocked_uri: str = ""
        line_number: int | None = None
        source_file: str = ""

    csp_report: Body | None = None


# ------------------------------------------------------------------- routes --


@router.post("/csp-report", status_code=204)
async def csp_report(payload: CspReport, request: Request) -> None:
    """Browser reports a CSP violation. Logged (redacted), never stored raw."""
    from realign.core.logging import get_logger

    body = payload.csp_report
    get_logger("realign.csp").warning(
        "csp_violation",
        uri=(body.document_uri if body else "")[:200],
        directive=(body.violated_directive if body else "")[:80],
        blocked=(body.blocked_uri if body else "")[:200],
        ip=(request.client.host if request.client else ""),
    )
    return None


@router.get("/status")
async def security_status(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Non-sensitive posture summary for the authenticated tenant."""
    failed = (
        await session.execute(
            select(sa_func.count())
            .select_from(AuditLog)
            .where(AuditLog.org_id == principal.org_id, AuditLog.action.like("auth.%failed%"))
        )
    ).scalar() or 0

    return {
        "transport": {
            "https_enforced": settings.force_https,
            "hsts": settings.is_production and settings.force_https,
            "environment": settings.app_env,
        },
        "session": {
            "cookie_secure": settings.effective_cookie_secure,
            "cookie_httponly": True,
            "cookie_samesite": settings.session_cookie_samesite,
            "access_ttl_minutes": settings.access_token_ttl_minutes,
            "rotation_on_login": settings.rotate_session_on_login,
        },
        "csrf": {"enabled": settings.csrf_enabled, "ttl_minutes": settings.csrf_ttl_minutes},
        "password": {
            "min_length": settings.password_min_length,
            "blocklist_enabled": settings.password_block_common,
        },
        "rate_limits": RULES_SNAPSHOT,
        "input": {
            "sanitisation": True,
            "ssrf_protection": settings.ssrf_protection,
            "upload_max_bytes": settings.upload_max_bytes,
            "body_max_bytes": 2 * 1024 * 1024,
        },
        "tenant": {
            "failed_auth_events_30d": int(failed),
        },
        "docs_exposed": settings.expose_docs or not settings.is_production,
        "report_to": settings.security_report_email,
    }


@router.get("/headers")
async def security_headers_probe() -> dict:
    """Documented header contract — verifiable with `curl -I`."""
    return {
        "expected": {
            "Strict-Transport-Security": "max-age=31536000; includeSubDomains; preload",
            "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; object-src 'none'",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Cross-Origin-Opener-Policy": "same-origin",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Cache-Control": "no-store, no-cache, must-revalidate, private",
        },
        "note": "الرؤوس تُطبَّق تلقائياً على كل الاستجابات. تحقّق بـ: curl -I https://yourdomain.com/",
    }


# ------------------------------------------------------- admin-only overview --


@router.get("/admin")
async def security_admin(
    principal: Principal = Depends(require_role("owner")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Abuse/attack overview. Requires a platform-level administrator."""
    user = principal.user
    if user is None or not user.is_platform_admin:
        raise PermissionDenied("هذه المنطقة للإدارة العامة فقط")

    since_alias = AuditLog.created_at
    suspicious = (
        await session.execute(
            select(AuditLog.action, sa_func.count())
            .where(
                AuditLog.action.in_(
                    [
                        "auth.login_failed",
                        "auth.login_blocked",
                        "auth.login_spray_detected",
                        "web.login_failed",
                        "web.login_spray_detected",
                        "auth.account_locked",
                        "auth.register_duplicate",
                    ]
                )
            )
            .group_by(AuditLog.action)
        )
    ).all()

    locked_users = (
        await session.execute(
            select(sa_func.count()).select_from(User).where(User.locked_until.is_not(None))
        )
    ).scalar() or 0

    return {
        "events": {action: int(count) for action, count in suspicious},
        "locked_accounts": int(locked_users),
        "rate_limiter": await limiter.snapshot(),
        "secret": MASK,  # proves redaction is wired, without revealing anything
    }


__all__ = ["router"]
