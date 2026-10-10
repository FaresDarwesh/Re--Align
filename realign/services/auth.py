"""Authentication + authorization primitives for the API layer."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from realign.core.errors import AuthenticationError, PermissionDenied
from realign.core.security import (
    decode_token,
    hash_api_key,
    role_at_least,
)
from realign.db import get_session
from realign.models import ApiKey, Organization, User

bearer_scheme = HTTPBearer(auto_error=False)

MAX_FAILED_LOGINS = 6
LOCK_MINUTES = 15


class Principal:
    """Everything the request is allowed to touch."""

    def __init__(
        self,
        *,
        user: User | None = None,
        org: Organization | None = None,
        via: str = "session",
        scopes: list[str] | None = None,
    ) -> None:
        self.user = user
        self.org = org
        self.via = via
        self.scopes = scopes or []

    # ------------------------------------------------------------- helpers --
    @property
    def org_id(self) -> str:
        if self.org is None:
            raise AuthenticationError("لا توجد مؤسسة مرتبطة بهذا الحساب")
        return self.org.id

    @property
    def user_id(self) -> str | None:
        return self.user.id if self.user else None

    @property
    def role(self) -> str:
        return self.user.role if self.user else "viewer"

    @property
    def is_platform_admin(self) -> bool:
        return bool(self.user and self.user.is_platform_admin)

    def require(self, minimum_role: str) -> None:
        if self.user is None:
            raise AuthenticationError()
        if not self.is_platform_admin and not role_at_least(self.role, minimum_role):
            raise PermissionDenied("هذا الإجراء يحتاج صلاحية أعلى")

    def require_scope(self, scope: str) -> None:
        if self.via == "api_key" and scope not in self.scopes and "*" not in self.scopes:
            raise PermissionDenied(f"مفتاح API لا يملك الصلاحية: {scope}")


# --------------------------------------------------------------- resolution --


async def _principal_from_token(token: str, session: AsyncSession) -> Principal:
    payload = decode_token(token, expected_type="access")
    user = await session.get(User, payload.get("sub"))
    if user is None or not user.is_active:
        raise AuthenticationError("الحساب غير موجود أو موقوف")
    org = await session.get(Organization, user.org_id)
    if org is None:
        raise AuthenticationError("المؤسسة غير موجودة")
    return Principal(user=user, org=org, via="session")


async def _principal_from_api_key(token: str, session: AsyncSession) -> Principal:
    result = await session.execute(
        select(ApiKey).where(ApiKey.hashed == hash_api_key(token), ApiKey.revoked_at.is_(None))
    )
    key = result.scalar_one_or_none()
    if key is None:
        raise AuthenticationError("مفتاح API غير صالح")
    org = await session.get(Organization, key.org_id)
    if org is None:
        raise AuthenticationError("المؤسسة غير موجودة")
    from realign.core.utils import utcnow

    key.last_used_at = utcnow()
    await session.flush()
    return Principal(user=None, org=org, via="api_key", scopes=list(key.scopes or []))


async def resolve_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = None,
    x_api_key: str | None = None,
    session: AsyncSession = None,  # type: ignore[assignment]
) -> Principal:
    token = None
    if credentials and credentials.credentials:
        token = credentials.credentials
    elif x_api_key:
        token = x_api_key
    if not token:
        # allow ?token= for quick browser links and server-rendered downloads
        token = request.query_params.get("token")
    if not token:
        raise AuthenticationError()

    if token.startswith("nxk_"):
        return await _principal_from_api_key(token, session)
    return await _principal_from_token(token, session)


async def get_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    session: AsyncSession = Depends(get_session),
) -> Principal:
    principal = await resolve_principal(request, credentials, x_api_key, session)
    request.state.principal = principal
    return principal


def require_role(minimum: str):
    async def _dep(principal: Principal = Depends(get_principal)) -> Principal:
        principal.require(minimum)
        return principal

    return _dep


async def get_optional_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_session),
) -> Principal | None:
    try:
        return await resolve_principal(request, credentials, None, session)
    except Exception:  # noqa: BLE001
        return None


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def token_pair(user: User) -> dict[str, Any]:
    from realign.core.security import create_access_token, create_refresh_token

    access, expires = create_access_token(
        sub=user.id,
        org_id=user.org_id,
        role=user.role,
        email=user.email,
        is_platform_admin=bool(user.is_platform_admin),
    )
    refresh, refresh_expires = create_refresh_token(sub=user.id, org_id=user.org_id)
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "expires_at": expires.isoformat(),
        "refresh_expires_at": refresh_expires.isoformat(),
        "user": user_payload(user),
    }


def user_payload(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "org_id": user.org_id,
        "locale": user.locale,
        "is_platform_admin": bool(user.is_platform_admin),
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }
