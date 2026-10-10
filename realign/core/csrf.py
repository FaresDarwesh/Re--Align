"""CSRF protection (CWE-352) — two independent walls.

Defence 1 — Fetch-Metadata / Origin checking (primary).
    Every modern browser sends `Origin` on a cross-origin POST and
    `Sec-Fetch-Site` on *every* request.  When either proves the request came
    from our own origin there is nothing to forge, so no token is required.
    This is what makes the forms keep working inside a cross-site iframe,
    behind a preview proxy, or when third-party cookies are restricted —
    situations where a cookie-based token silently used to fail.

Defence 2 — Double-submit token bound to the session (fallback).
    For clients that send no Fetch-Metadata at all (curl, old embedded
    webviews, native clients) the classic token still applies: it is
    `HMAC(secret, exp|nonce|user|session_seed)`, so a token lifted from
    another session or another account will not validate, and it expires.

Verification is constant-time and never raises: callers get a bool.
"""

from __future__ import annotations

import hmac
import secrets
from datetime import timedelta
from hashlib import sha256

from realign.config import settings
from realign.core.utils import utcnow

FIELD = "_csrf"
HEADER = "X-CSRF-Token"

# Fetch-Metadata values that prove the request is not cross-site.
_SAFE_SITES = ("same-origin", "none")


def _sign(payload: str) -> str:
    return hmac.new(settings.app_secret_key.encode(), payload.encode(), sha256).hexdigest()


# ------------------------------------------------------------------ tokens --


def issue_token(*, user_id: str = "", session_id: str = "") -> str:
    """Mint `<expiry>.<nonce>.<owner>.<seed>.<signature>`."""
    expires_at = int((utcnow() + timedelta(minutes=settings.csrf_ttl_minutes)).timestamp())
    nonce = secrets.token_urlsafe(12)
    payload = f"{expires_at}.{nonce}.{user_id}.{session_id}"
    return f"{payload}.{_sign(payload)}"


def verify_token(token: str | None, *, user_id: str = "", session_id: str = "") -> bool:
    """Strict double-submit: signature, expiry, seed and owner must all match."""
    if not token or not settings.csrf_enabled:
        return not settings.csrf_enabled
    if not isinstance(token, str):
        return False
    try:
        expires_raw, nonce, owner, sess, signature = token.split(".", 4)
    except ValueError:
        return False
    payload = f"{expires_raw}.{nonce}.{owner}.{sess}"
    if not hmac.compare_digest(_sign(payload), signature):
        return False
    try:
        if int(expires_raw) < int(utcnow().timestamp()):
            return False
    except ValueError:
        return False
    # The token's seed must equal the cookie exactly. A seedless token must not
    # validate against a seeded browser (that would let an attacker pre-mint a
    # token and replay it on a victim).
    if not hmac.compare_digest(sess, session_id or ""):
        return False
    # Bound to the account: a token minted for another user must not work.
    if not hmac.compare_digest(owner, user_id or ""):
        return False
    return True


def csrf_input(*, user_id: str = "", session_id: str = "") -> str:
    token = issue_token(user_id=user_id, session_id=session_id)
    return f'<input type="hidden" name="{FIELD}" value="{token}">'


def extract(request) -> str | None:
    """Read the token from the header (form fields are read by the caller)."""
    return request.headers.get(HEADER) or None


# --------------------------------------------------------- same-origin proof --


def _host_of(value: str) -> str:
    """`https://Host:443/path` -> `host:443` (lower-cased, no scheme/port default)."""
    value = (value or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = "http://" + value
    rest = value.split("://", 1)[1]
    return rest.split("/", 1)[0].lower()


def _request_host(request) -> str:
    """The host the browser actually addressed, honouring the proxy headers."""
    for header in ("x-forwarded-host", "host"):
        raw = (request.headers.get(header) or "").split(",")[0].strip()
        if raw:
            return _host_of(raw)
    return _host_of(request.url.netloc)


def _request_scheme(request) -> str:
    raw = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    return raw or (request.url.scheme or "").lower()


def same_origin(request) -> bool | None:
    """Is this request provably first-party?

    Returns True/False when the browser gave us a usable signal, and `None`
    when it gave us nothing (non-browser clients such as curl).
    """
    origin = (request.headers.get("origin") or "").strip()
    if origin and origin.lower() != "null":
        return _host_of(origin) == _request_host(request)

    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if site:
        return site in _SAFE_SITES

    # A cross-site iframe sends the *parent* page as Referer while Host is ours.
    referer = (request.headers.get("referer") or "").strip()
    if referer:
        try:
            from urllib.parse import urlparse

            path = urlparse(referer).path or "/"
        except Exception:  # noqa: BLE001
            return None
        # Same app, different page -> first-party navigation.
        if _host_of(referer) == _request_host(request):
            return True
        # A different host could be a legitimate first-party link (email, chat)
        # or an attack, so it stays inconclusive rather than trusted.
        del path
        return None

    return None


def verify_request(request, *, token: str | None, user_id: str = "", session_id: str = "") -> bool:
    """Accept when *either* defence holds.

    * Fetch-Metadata/Origin says the request is first-party, or
    * the bound token is valid, or
    * the client sent no browser signal at all and holds no session
      (an API client that has nothing to hijack).
    """
    if not settings.csrf_enabled:
        return True

    origin_verdict = same_origin(request)
    if origin_verdict is True:
        return True
    if origin_verdict is False:
        # Provably cross-site: only a valid bound token can rescue it, and it
        # never can, because a cross-site attacker cannot read our cookie.
        return False

    if verify_token(token, user_id=user_id, session_id=session_id):
        return True

    # No usable signal and no session cookie: a non-browser client (curl, a
    # health check, a native app using the JSON API). Nothing to hijack.
    return not bool(session_id) and not _has_session_cookie(request)


def _has_session_cookie(request) -> bool:
    return bool(request.cookies.get("realign_session"))


__all__ = [
    "FIELD",
    "HEADER",
    "issue_token",
    "verify_token",
    "verify_request",
    "same_origin",
    "csrf_input",
    "extract",
]
