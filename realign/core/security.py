"""Passwords, JWT tokens, API keys, credential encryption."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import bcrypt
import jwt

from realign.config import settings
from realign.core.errors import AuthenticationError
from realign.core.utils import utcnow

# --------------------------------------------------------------- passwords --


def hash_password(password: str) -> str:
    if len(password.encode("utf-8")) > 72:
        # bcrypt silently truncates at 72 bytes; pre-hash to avoid surprises.
        password = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        if len(password.encode("utf-8")) > 72:
            password = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:  # noqa: BLE001 - any bcrypt error means "wrong password"
        return False


def validate_password_strength(password: str) -> tuple[bool, str]:
    """Delegates to the full policy (length + breached-password blocklist)."""
    from realign.core.passwords import validate_password

    return validate_password(password)


# ---------------------------------------------------------------- sessions --


def new_session_id() -> str:
    """Opaque session identifier (CWE-384: rotated on every privilege change)."""
    return secrets.token_urlsafe(32)


def session_fingerprint(request) -> str:
    """Loose, privacy-friendly binding of a session to a client.

    Deliberately *not* strict: IP + a coarse user-agent hash. It raises the cost
    of token theft without logging anyone out when they switch Wi-Fi.
    """
    import hashlib

    ip = (getattr(getattr(request, "client", None), "host", "") or "") if request else ""
    agent = ""
    if request is not None:
        agent = (getattr(request, "headers", {}) or {}).get("user-agent", "")[:200]
    return hashlib.sha256(f"{ip}|{agent}".encode("utf-8", "replace")).hexdigest()[:32]


def cookie_kwargs(*, secure: bool | None = None, request: Any = None) -> dict[str, Any]:
    """Cookie flags hardened by default: HttpOnly, SameSite, Secure in prod.

    Embedded / proxied contexts (the live preview, an admin portal, a partner
    dashboard) put the app inside a cross-site iframe. Browsers refuse
    `SameSite=Lax` cookies there, which silently breaks login. When such a
    context is detected over HTTPS the cookie is downgraded to
    `SameSite=None; Secure` — the only combination browsers accept in a
    third-party context, and it is still safe because `Secure` pins it to TLS.
    """
    samesite = settings.session_cookie_samesite
    is_secure = settings.effective_cookie_secure if secure is None else secure

    if request is not None and settings.auto_cookie_samesite:
        dest = (request.headers.get("sec-fetch-dest") or "").lower()
        site = (request.headers.get("sec-fetch-site") or "").lower()
        scheme = (request.headers.get("x-forwarded-proto") or request.url.scheme or "").split(",")[0].strip()

        embedded = dest in ("iframe", "frame", "embed", "object") or site == "cross-site"

        # Fallback: some proxies strip `Sec-Fetch-*`. A page framed inside a
        # different origin still sends that parent page as `Referer`, so a
        # cross-origin Referer is just as conclusive as `Sec-Fetch-Dest`.
        if not embedded:
            referer = (request.headers.get("referer") or "").strip()
            if referer:
                try:
                    from urllib.parse import urlparse

                    ref_host = (urlparse(referer).hostname or "").lower()
                    own_host = (
                        (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
                        or (request.headers.get("host") or "").split(",")[0].strip()
                        or request.url.hostname
                        or ""
                    ).lower()
                    if ref_host and own_host and ref_host != own_host:
                        embedded = True
                except Exception:  # noqa: BLE001 - a bad header must not 500
                    embedded = False

        if embedded and scheme == "https":
            # Inside a cross-site frame the *only* combination browsers accept
            # is SameSite=None; Secure — so there is no alternative to pick.
            samesite = "none"
            is_secure = True
        elif embedded:
            # Framed over plain HTTP: `Secure` cookies would be dropped, so the
            # best available option is to stay first-party-friendly.
            samesite = "lax"

    if samesite == "none":
        is_secure = True  # browsers reject SameSite=None without Secure

    return {
        "httponly": True,
        "secure": is_secure,
        "samesite": samesite,
        "path": "/",
    }


# ------------------------------------------------------------------- jwt ----

ALGORITHM = "HS256"


def _encode(payload: dict[str, Any], ttl: timedelta) -> tuple[str, datetime]:
    expires_at = utcnow() + ttl
    body = {**payload, "exp": expires_at, "iat": utcnow(), "iss": settings.app_name}
    token = jwt.encode(body, settings.app_secret_key, algorithm=ALGORITHM)
    return token, expires_at


def create_access_token(
    *,
    sub: str,
    org_id: str,
    role: str,
    email: str = "",
    is_platform_admin: bool = False,
    sid: str = "",
    fp: str = "",
) -> tuple[str, datetime]:
    return _encode(
        {
            "sub": sub,
            "org_id": org_id,
            "role": role,
            "email": email,
            "type": "access",
            "adm": is_platform_admin,
            "sid": sid,
            "fp": fp,
        },
        timedelta(minutes=settings.access_token_ttl_minutes),
    )


def create_refresh_token(*, sub: str, org_id: str, sid: str = "") -> tuple[str, datetime]:
    return _encode(
        {"sub": sub, "org_id": org_id, "type": "refresh", "sid": sid},
        timedelta(days=settings.refresh_token_ttl_days),
    )


def decode_token(token: str, *, expected_type: str | None = None) -> dict[str, Any]:
    """Decode a JWT.

    **Every** failure path maps to a 401. A malformed or forged token must
    never reach the generic 500 handler: that would turn a token-error into an
    unhandled exception (CWE-248 / CWE-703) and leak a stack trace.
    """
    if not token or not isinstance(token, str) or token.count(".") != 2:
        raise AuthenticationError("رمز غير صالح")
    try:
        payload = jwt.decode(
            token,
            settings.app_secret_key,
            algorithms=[ALGORITHM],
            options={"verify_signature": True, "verify_exp": True, "require": ["exp"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("انتهت صلاحية الجلسة") from exc
    except jwt.PyJWTError as exc:
        raise AuthenticationError("رمز غير صالح") from exc
    except Exception as exc:  # noqa: BLE001 - any decode error is "bad token"
        raise AuthenticationError("رمز غير صالح") from exc
    if expected_type and payload.get("type") != expected_type:
        raise AuthenticationError("نوع الرمز غير صحيح")
    if not payload.get("sub"):
        raise AuthenticationError("رمز غير صالح")
    return payload


# --------------------------------------------------------------- api keys ---


def generate_api_key() -> tuple[str, str, str]:
    """Return (plaintext, prefix, sha256_hash). The plaintext is shown once."""
    plaintext = "nxk_" + secrets.token_urlsafe(32)
    prefix = plaintext[:12]
    return plaintext, prefix, hashlib.sha256(plaintext.encode()).hexdigest()


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


# --------------------------------------------------- credential encryption --


def _fernet():
    from cryptography.fernet import Fernet

    key = settings.credentials_encryption_key
    if not key:
        # Deterministic dev key derived from the app secret: never leaks to prod
        # because production refuses to boot without an explicit key (see main.py).
        key = base64.urlsafe_b64encode(
            hashlib.sha256(f"{settings.app_secret_key}::creds".encode()).digest()
        ).decode()
    return Fernet(key)


def encrypt_dict(values: dict[str, Any] | None) -> dict[str, Any]:
    """Encrypt every value of a flat dict. Non-string values are JSON-serialised."""
    if not values:
        return {}
    import json

    f = _fernet()
    out: dict[str, Any] = {}
    for k, v in values.items():
        raw = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        out[k] = f.encrypt(raw.encode("utf-8")).decode()
    return out


def decrypt_dict(values: dict[str, Any] | None) -> dict[str, Any]:
    if not values:
        return {}
    import json

    f = _fernet()
    out: dict[str, Any] = {}
    for k, v in values.items():
        if not isinstance(v, str) or not v:
            out[k] = v
            continue
        try:
            raw = f.decrypt(v.encode("utf-8")).decode("utf-8")
        except Exception:  # noqa: BLE001 - value stored in cleartext (legacy)
            out[k] = v
            continue
        try:
            out[k] = json.loads(raw)
        except Exception:  # noqa: BLE001
            out[k] = raw
    return out


# ------------------------------------------------------------ signatures ----


def sign_webhook(secret: str, payload: bytes) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def verify_webhook(secret: str, payload: bytes, signature: str) -> bool:
    if not secret:
        return True  # no secret configured -> accept (dev convenience, logged)
    return hmac.compare_digest(sign_webhook(secret, payload), signature or "")


Role = Literal["owner", "admin", "manager", "approver", "editor", "viewer"]
ROLE_HIERARCHY: dict[str, int] = {
    "viewer": 10,
    "editor": 20,
    "approver": 30,
    "manager": 40,
    "admin": 50,
    "owner": 60,
}


def role_at_least(role: str, minimum: str) -> bool:
    return ROLE_HIERARCHY.get(role, 0) >= ROLE_HIERARCHY.get(minimum, 99)
