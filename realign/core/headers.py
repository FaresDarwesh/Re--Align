"""Transport security: HTTPS enforcement, security headers, request size caps.

Headers applied (CWE-1021 / 693 / 319 / 614 / 1004 / 1275 …)
    Strict-Transport-Security   → forces TLS for a year, incl. subdomains
    X-Content-Type-Options      → kills MIME sniffing
    X-Frame-Options: DENY       → clickjacking (UI redress)
    Content-Security-Policy     → XSS, data exfiltration, mixed content
    Referrer-Policy             → stops URL leakage to third parties
    Permissions-Policy          → disables camera/mic/geolocation/USB…
    Cross-Origin-Opener-Policy  → XS-Leaks / Spectre-style cross-window reads
    Cross-Origin-Resource-Policy→ stops cross-site embedding of responses
    Cache-Control: no-store     → keeps tokens/CSRF out of caches
"""

from __future__ import annotations

import os
import secrets
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp

from realign.config import settings
from realign.core.ratelimit import RULES, client_ip, limiter

# Paths that never get a `no-store` (they are static assets).
CACHEABLE_PREFIXES = ("/static/", "/favicon.ico", "/media/logo/")

# Paths that are exempt from the general request budget (health checks).
BUDGET_EXEMPT = ("/api/v1/health", "/static/")

MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MB — upload/JSON bomb protection


def _csp(nonce: str) -> str:
    base = (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self' 'unsafe-inline'; "  # inline <style> blocks are ours
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "object-src 'none'; "
        "frame-src 'none'; "
        "worker-src 'self'; "
        "manifest-src 'self'; "
        "media-src 'self' data:; "
        "upgrade-insecure-requests"
    )
    if not settings.is_production:
        base += "; report-uri /api/v1/security/csp-report"
    else:
        base += f"; report-uri {settings.app_base_url.rstrip('/')}/api/v1/security/csp-report"
    return base


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds the full security-header suite and tags every request with an id."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce

        response = await call_next(request)

        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
        headers.setdefault("Origin-Agent-Cluster", "?1")
        headers.setdefault(
            "Permissions-Policy",
            "accelerometer=(), autoplay=(), camera=(), display-capture=(), "
            "encrypted-media=(), fullscreen=(self), geolocation=(), gyroscope=(), "
            "magnetometer=(), microphone=(), midi=(), payment=(), "
            "picture-in-picture=(), publickey-credentials-get=(), usb=(), "
            "screen-wake-lock=(), serial=(), xr-spatial-tracking=()",
        )
        headers.setdefault("Content-Security-Policy", _csp(nonce))
        headers.setdefault("X-XSS-Protection", "0")  # CSP supersedes it; 0 disables the buggy auditor

        if settings.is_production and settings.force_https:
            headers.setdefault(
                "Strict-Transport-Security", f"max-age={settings.hsts_max_age}; includeSubDomains; preload"
            )

        if not path.startswith(CACHEABLE_PREFIXES):
            # Authenticated pages and JSON carry tokens — never store them.
            existing = headers.get("cache-control", "")
            if "no-store" not in existing:
                headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
            headers.setdefault("Pragma", "no-cache")

        headers.setdefault("X-Request-ID", getattr(request.state, "request_id", ""))
        return response


class HTTPSRedirectMiddleware(BaseHTTPMiddleware):
    """Force TLS everywhere once `FORCE_HTTPS` (default: production) is on."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not (settings.force_https and settings.is_production):
            return await call_next(request)

        # Behind nginx/Caddy the scheme arrives in X-Forwarded-Proto.
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        if scheme.split(",")[0].strip() == "https":
            return await call_next(request)

        if request.url.path.startswith("/api/"):
            return JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "code": "https_required",
                        "message": "يجب استخدام اتصال HTTPS",
                    }
                },
            )
        url = request.url.replace(scheme="https")
        return RedirectResponse(url=str(url), status_code=308)


class RequestLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP request budget (DDoS/scraping) + hard cap on body size."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if path.startswith(BUDGET_EXEMPT):
            return await call_next(request)

        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return JSONResponse(
                status_code=413,
                content={"error": {"code": "payload_too_large", "message": "حجم الطلب أكبر من المسموح"}},
            )

        ip = client_ip(request)
        allowed, _remaining, retry_after = await limiter.check("web.general", ip)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "rate_limited",
                        "message": "طلبات كثيرة جداً، حاول بعد قليل",
                        "retry_after": retry_after,
                    }
                },
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)


def api_rate_scope(path: str, method: str) -> str:
    """Pick the budget rule for an API path."""
    if path.startswith("/api/v1/auth/login"):
        return "auth.login"
    if path.startswith("/api/v1/auth/register"):
        return "auth.register"
    if path.startswith("/api/v1/auth/refresh"):
        return "auth.refresh"
    if path.startswith("/api/v1/auth/change-password"):
        return "auth.password"
    if path.startswith("/api/v1/billing") or path.startswith("/api/v1/plans"):
        return "api.billing"
    if method != "GET":
        return "api.heavy"
    return "api.general"


def rate_limit_headers(remaining: int, retry_after: int) -> dict[str, str]:
    return {
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Retry-After": str(retry_after),
    }


RULES_SNAPSHOT = {name: {"limit": r.limit, "window": r.window, "lockout": r.lockout} for name, r in RULES.items()}


def install(app: ASGIApp) -> None:  # pragma: no cover - wiring helper
    """Order matters: outermost first."""
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(HTTPSRedirectMiddleware)
    app.add_middleware(RequestLimitMiddleware)


__all__ = [
    "SecurityHeadersMiddleware",
    "HTTPSRedirectMiddleware",
    "RequestLimitMiddleware",
    "api_rate_scope",
    "rate_limit_headers",
    "install",
    "MAX_BODY_BYTES",
    "RULES_SNAPSHOT",
]
