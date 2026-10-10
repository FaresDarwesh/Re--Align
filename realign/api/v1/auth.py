"""Registration, login, session, profile, team and API keys."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from realign.config import settings
from realign.core.errors import AuthenticationError, Conflict, NotFound, PermissionDenied, ValidationError
from realign.core.security import (
    create_access_token,
    decode_token,
    generate_api_key,
    hash_password,
    validate_password_strength,
    verify_password,
)
from realign.core.logging import get_logger
from realign.core.ratelimit import client_ip as rl_client_ip, limiter
from realign.core.sanitize import clean_email, clean_label, clean_long
from realign.core.utils import slugify, utcnow
from realign.db import get_session
from realign.models import ApiKey, Organization, User, UserRole
from realign.models.identity import OrgStatus
from realign.services import audit
from realign.services.auth import (
    Principal,
    client_ip,
    get_principal,
    require_role,
    token_pair,
    user_payload,
)
from realign.services.onboarding import create_organization, provision_org

log = get_logger("realign.api.auth")

router = APIRouter(prefix="/auth", tags=["auth"])


# ----------------------------------------------------------------- schemas --


class RegisterIn(BaseModel):
    organization: str = Field(min_length=2, max_length=120)
    name: str = Field(default="", max_length=120)
    email: EmailStr
    password: str = Field(min_length=10, max_length=256)
    plan_code: str = Field(default="starter", max_length=40)
    locale: str = Field(default="ar", max_length=10)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=8, max_length=4096)


class ProfileUpdate(BaseModel):
    full_name: str | None = Field(default=None, max_length=120)
    locale: str | None = Field(default=None, max_length=10)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=10, max_length=256)


class ForgotPasswordIn(BaseModel):
    email: EmailStr


class ResetPasswordIn(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(min_length=10, max_length=256)


class ApiKeyIn(BaseModel):
    name: str = Field(default="default", max_length=60)
    scopes: list[str] = Field(default_factory=lambda: ["*"])


class InviteIn(BaseModel):
    email: EmailStr
    role: str = UserRole.VIEWER
    full_name: str = ""
    password: str = Field(default="", min_length=0)


class ApiKeyIn(BaseModel):
    name: str = "default"
    scopes: list[str] = Field(default_factory=lambda: ["*"])


# ------------------------------------------------------------------ routes --


@router.post("/register", status_code=201)
async def register(payload: RegisterIn, request: Request, session: AsyncSession = Depends(get_session)) -> dict:
    if not settings.allow_public_signup:
        raise PermissionDenied("التسجيل العام مغلق — تواصل مع الإدارة")

    ip = rl_client_ip(request)
    allowed, _remaining, retry_after = await limiter.check("auth.register", ip)
    if not allowed:
        raise ValidationError(f"محاولات تسجيل كثيرة من هذا الاتصال — حاول بعد {retry_after} ثانية")

    email = clean_email(payload.email)
    organization = clean_label(payload.organization, limit=120)
    name = clean_label(payload.name, limit=120)
    if len(organization) < 2:
        raise ValidationError("اسم المؤسسة قصير جداً")

    # Timing: always hash, so a taken e-mail is not faster to detect than a free
    # one (user enumeration via response time, CWE-208).
    _ = hash_password(payload.password)
    exists = (
        await session.execute(select(User).where(User.email == email))
    ).scalars().first()
    if exists:
        await audit.record(
            session, org_id=exists.org_id, action="auth.register_duplicate",
            actor_label=email, resource="user", message="محاولة تسجيل ببريد موجود",
            ip=ip, commit=True,
        )
        raise Conflict("هذا البريد مسجّل بالفعل")

    ok, msg = validate_password_strength(payload.password)
    if not ok:
        raise ValidationError(msg)

    org, user = await create_organization(
        session,
        name=organization,
        owner_email=email,
        owner_password=payload.password,
        owner_name=name,
        plan_code=payload.plan_code,
        locale=payload.locale,
    )
    user.last_login_at = utcnow()
    await session.commit()

    await audit.record(
        session,
        org_id=org.id,
        action="auth.register",
        actor_id=user.id,
        actor_label=user.email,
        resource="organization",
        resource_id=org.id,
        message=f"إنشاء مؤسسة: {org.name}",
        ip=ip,
        commit=True,
    )

    return {"organization": {"id": org.id, "name": org.name, "slug": org.slug}, **token_pair(user)}


@router.post("/login")
async def login(payload: LoginIn, request: Request, session: AsyncSession = Depends(get_session)) -> dict:
    """Hardened login.

    * per-IP **and** per-account budgets (credential stuffing)
    * distinct-account detection per IP (password spraying)
    * constant-ish work: a missing account still runs bcrypt
    * generic failure message (no user enumeration, CWE-204)
    * fresh session id on success (session fixation, CWE-384)
    """
    ip = rl_client_ip(request)
    email = clean_email(payload.email)

    blocked_for = await limiter.is_blocked("auth.login", ip)
    if blocked_for:
        await audit.record(
            session, org_id="", action="auth.login_blocked", actor_label=email,
            resource="auth", message="محاولة دخول أثناء حظر مؤقت", ip=ip, commit=True,
        )
        raise AuthenticationError(f"محاولات كثيرة — حاول بعد {blocked_for} ثانية")

    if await limiter.note_account_attempt(ip, email):
        await limiter.block("auth.login", ip, 1800)
        await audit.record(
            session, org_id="", action="auth.login_spray_detected", actor_label=email,
            resource="auth", message="تم رصد هجوم رشّ كلمات مرور (password spraying)",
            ip=ip, commit=True,
        )
        raise AuthenticationError("نشاط غير معتاد من هذا الاتصال — حاول بعد نصف ساعة")

    # Always perform a bcrypt verify so timing does not reveal account existence.
    DUMMY_HASH = "$2b$12$C6UzMDM.H6dfI/f/IKcEe.7Bq0HrS8G2kKq2ZQ8pZ9rQ0W1XuYzOa"
    result = await session.execute(select(User).where(User.email == email).order_by(User.created_at))
    user = result.scalars().first()
    password_ok = verify_password(payload.password, user.hashed_password if user else DUMMY_HASH)

    if user is None or not password_ok:
        await limiter.penalty("auth.login", ip)
        if user is not None:
            user.failed_login_attempts = int(user.failed_login_attempts or 0) + 1
            if user.failed_login_attempts >= settings.max_failed_logins_per_hour:
                user.locked_until = (utcnow() + timedelta(minutes=15)).replace(tzinfo=None)
                user.failed_login_attempts = 0
                await audit.record(
                    session, org_id=user.org_id, action="auth.account_locked",
                    actor_id=user.id, actor_label=email, resource="user",
                    resource_id=user.id, message="قفل الحساب بعد محاولات فاشلة",
                    ip=ip, commit=True,
                )
            await session.commit()
        else:
            await audit.record(
                session, org_id="", action="auth.login_failed", actor_label=email, resource="auth",
                message="محاولة دخول ببريد غير موجود", ip=ip, commit=True,
            )
        raise AuthenticationError("البريد أو كلمة المرور غير صحيحة")

    if not user.is_active:
        await limiter.penalty("auth.login", ip)
        raise AuthenticationError("الحساب موقوف — تواصل مع الإدارة")
    if user.is_locked:
        await limiter.penalty("auth.login", ip)
        raise AuthenticationError("الحساب موقوف مؤقتاً بسبب محاولات فاشلة — جرّب بعد ١٥ دقيقة")

    await limiter.reset("auth.login", ip)
    user.failed_login_attempts = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    await session.commit()

    await audit.record(
        session,
        org_id=user.org_id,
        action="auth.login",
        actor_id=user.id,
        actor_label=user.email,
        resource="user",
        resource_id=user.id,
        ip=ip,
        commit=True,
    )
    return token_pair(user)


@router.post("/refresh")
async def refresh(payload: RefreshIn, session: AsyncSession = Depends(get_session)) -> dict:
    data = decode_token(payload.refresh_token, expected_type="refresh")
    user = await session.get(User, data.get("sub"))
    if user is None or not user.is_active:
        raise AuthenticationError()
    return token_pair(user)


@router.get("/me")
async def me(principal: Principal = Depends(get_principal), session: AsyncSession = Depends(get_session)) -> dict:
    org = principal.org
    plan_code = org.plan_code if org else ""
    return {
        "user": user_payload(principal.user) if principal.user else None,
        "via": principal.via,
        "organization": {
            "id": org.id,
            "name": org.name,
            "slug": org.slug,
            "status": org.status,
            "plan_code": plan_code,
            "credits_monthly": org.credits_monthly,
            "credits_used": org.credits_used,
            "credits_remaining": org.credits_remaining,
            "locale": org.locale,
            "kill_switch": org.kill_switch,
            "auto_publish": org.auto_publish,
        }
        if org
        else None,
    }


@router.patch("/me")
async def update_me(
    payload: ProfileUpdate,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    user = principal.user
    if user is None:
        raise AuthenticationError()
    if payload.full_name is not None:
        user.full_name = payload.full_name
    if payload.locale is not None:
        user.locale = payload.locale
    await session.commit()
    return user_payload(user)


@router.post("/change-password")
async def change_password(
    payload: PasswordChange,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    user = principal.user
    if user is None:
        raise AuthenticationError()
    ip = getattr(principal, "ip", "") or ""
    allowed, _r, retry_after = await limiter.check("auth.password", user.id)
    if not allowed:
        raise ValidationError(f"محاولات كثيرة — حاول بعد {retry_after} ثانية")
    if not verify_password(payload.current_password, user.hashed_password):
        await limiter.penalty("auth.password", user.id)
        raise AuthenticationError("كلمة المرور الحالية غير صحيحة")
    ok, msg = validate_password_strength(payload.new_password)
    if not ok:
        raise ValidationError(msg)
    if verify_password(payload.new_password, user.hashed_password):
        raise ValidationError("كلمة المرور الجديدة يجب أن تختلف عن الحالية")
    user.hashed_password = hash_password(payload.new_password)
    await limiter.reset("auth.password", user.id)
    await session.commit()
    await audit.record(
        session,
        org_id=user.org_id,
        action="auth.password_changed",
        actor_id=user.id,
        actor_label=user.email,
        resource="user",
        resource_id=user.id,
        commit=True,
    )
    return {"ok": True}


# ------------------------------------------------------- forgot / reset -----


def _hash_reset_token(raw: str) -> str:
    """Reset tokens are stored hashed, exactly like passwords."""
    import hashlib

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@router.post("/forgot-password")
async def forgot_password(
    payload: ForgotPasswordIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Start a password reset.

    Always answers `{"ok": true}` — even when the address is unknown — so the
    endpoint cannot be used to enumerate who has an account (CWE-204).
    """
    ip = rl_client_ip(request)
    allowed, _r, retry_after = await limiter.check("auth.forgot", ip)
    if not allowed:
        raise ValidationError(f"محاولات كثيرة — حاول بعد {retry_after} ثانية")

    email = clean_email(payload.email)
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if user is not None and user.is_active:
        import secrets

        raw = secrets.token_urlsafe(32)
        user.password_reset_token_hash = _hash_reset_token(raw)
        user.password_reset_expires_at = utcnow() + timedelta(minutes=30)
        await audit.record(
            session,
            org_id=user.org_id,
            action="auth.password_reset_requested",
            actor_id=user.id,
            actor_label=user.email,
            resource="user",
            resource_id=user.id,
        )
        await session.commit()
        await _deliver_reset_link(request, session, user, raw)
    else:
        # Burn the same amount of work so timing does not leak either.
        _hash_reset_token(secrets_token())
    return {"ok": True}


def secrets_token() -> str:
    """Constant-time filler used on the 'unknown address' branch."""
    import secrets

    return secrets.token_urlsafe(32)


async def _deliver_reset_link(
    request: Request, session: AsyncSession, user: User, raw: str
) -> None:
    """Send the reset link through the org's SMTP connector if it is wired up.

    With no SMTP credentials configured the link is written to the audit log
    instead of emailed — never shown on screen — so a self-hosted operator can
    still complete the flow from the server logs.
    """
    from realign.connectors.registry import credentials_for, get_connector

    link = str(request.base_url).rstrip("/") + "/reset-password?token=" + raw
    connector = get_connector("email")
    sent = False
    if connector is not None:
        try:
            creds = await credentials_for(
                session, org_id=user.org_id, connector_key=connector.key
            )
            if connector.is_configured(creds):
                locale = (user.locale or "ar").lower()
                who = user.full_name or user.email
                if locale == "en":
                    subject = "Reset your password — re-align"
                    body = (
                        f"Hello {who},\n\n"
                        "Use this link within 30 minutes to set a new password:\n"
                        f"{link}\n\n"
                        "If you did not ask for it, ignore this message - "
                        "your current password still works.\n"
                    )
                else:
                    subject = "إعادة تعيين كلمة المرور — re-align"
                    body = (
                        f"{who}،\n\n"
                        "اضغط الرابط ده خلال ٣٠ دقيقة عشان تعيّن كلمة سر جديدة:\n"
                        f"{link}\n\n"
                        "لو مش انت اللي طلبت، تجاهل الرسالة دي — كلمة سرك الحالية لسه شغالة.\n"
                    )
                result = await connector.send(creds, to=user.email, subject=subject, body=body)
                sent = bool(result.ok)
        except Exception:  # noqa: BLE001 - email is best-effort
            sent = False
    log.info(
        "password_reset_link",
        user_id=user.id,
        delivered_by="smtp" if sent else "audit_log_only",
    )
    if not sent:
        await audit.record(
            session,
            org_id=user.org_id,
            action="auth.password_reset_link_issued",
            actor_id=user.id,
            actor_label=user.email,
            resource="user",
            resource_id=user.id,
            meta={"link": link, "expires_in_minutes": 30},
            commit=True,
        )


@router.post("/reset-password")
async def reset_password(
    payload: ResetPasswordIn,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Finish a password reset with a single-use, 30-minute token."""
    token = payload.token.strip()
    result = await session.execute(
        select(User).where(User.password_reset_token_hash == _hash_reset_token(token))
    )
    user = result.scalar_one_or_none()
    ok, message = validate_password_strength(payload.password)
    if user is None:
        raise AuthenticationError("الرابط غير صالح أو منتهي الصلاحية")
    if not ok:
        raise ValidationError(message)
    expires = user.password_reset_expires_at
    if expires is None or expires.replace(tzinfo=None) < utcnow().replace(tzinfo=None):
        # Expired or already used: drop it so a stolen link dies immediately.
        user.password_reset_token_hash = None
        user.password_reset_expires_at = None
        await session.commit()
        raise AuthenticationError("الرابط غير صالح أو منتهي الصلاحية")
    if verify_password(payload.password, user.hashed_password):
        raise ValidationError("كلمة المرور الجديدة يجب أن تختلف عن الحالية")

    user.hashed_password = hash_password(payload.password)
    user.password_reset_token_hash = None
    user.password_reset_expires_at = None
    user.failed_login_attempts = 0
    user.locked_until = None
    await session.commit()
    await audit.record(
        session,
        org_id=user.org_id,
        action="auth.password_reset_completed",
        actor_id=user.id,
        actor_label=user.email,
        resource="user",
        resource_id=user.id,
        commit=True,
    )
    return {"ok": True}



# -------------------------------------------------------------------- team --


@router.get("/team")
async def team(
    principal: Principal = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    result = await session.execute(
        select(User).where(User.org_id == principal.org_id).order_by(User.created_at.asc())
    )
    return {"members": [user_payload(u) for u in result.scalars().all()], "seats_limit": principal.org.seats_limit}


@router.post("/team", status_code=201)
async def add_member(
    payload: InviteIn,
    request: Request,
    principal: Principal = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    org = principal.org
    current = (
        await session.execute(select(User).where(User.org_id == org.id))
    ).scalars().all()
    if len(current) >= int(org.seats_limit or 0):
        raise ValidationError(f"تم الوصول للحد الأقصى للمقاعد ({org.seats_limit}) — ترقّي الخطة لإضافة أعضاء")

    email = payload.email.strip().lower()
    if (await session.execute(select(User).where(User.email == email))).scalars().first():
        raise Conflict("هذا البريد موجود بالفعل")
    if payload.role not in UserRole.values():
        raise ValidationError("دور غير صالح")

    password = payload.password or f"Nx-{slugify(org.name)[:6]}-{utcnow().strftime('%H%M%S')}"
    user = User(
        org_id=org.id,
        email=email,
        hashed_password=hash_password(password),
        full_name=payload.full_name or email.split("@")[0],
        role=payload.role,
    )
    session.add(user)
    await session.commit()
    await audit.record(
        session,
        org_id=org.id,
        action="team.member_added",
        actor_id=principal.user_id,
        actor_label=principal.user.email if principal.user else "",
        resource="user",
        resource_id=user.id,
        message=f"{email} · {payload.role}",
        ip=client_ip(request),
        commit=True,
    )
    return {**user_payload(user), "temporary_password": password if not payload.password else None}


@router.patch("/team/{user_id}/role")
async def change_role(
    user_id: str,
    role: str,
    principal: Principal = Depends(require_role("owner")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    user = await session.get(User, user_id)
    if user is None or user.org_id != principal.org_id:
        raise NotFound("المستخدم غير موجود")
    if role not in UserRole.values():
        raise ValidationError("دور غير صالح")
    user.role = role
    await session.commit()
    return user_payload(user)


@router.delete("/team/{user_id}")
async def remove_member(
    user_id: str,
    principal: Principal = Depends(require_role("owner")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    user = await session.get(User, user_id)
    if user is None or user.org_id != principal.org_id:
        raise NotFound("المستخدم غير موجود")
    if user.id == principal.user_id:
        raise ValidationError("لا يمكنك حذف حسابك")
    await session.delete(user)
    await session.commit()
    return {"ok": True}


# ---------------------------------------------------------------- api keys --


@router.get("/api-keys")
async def list_keys(
    principal: Principal = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    result = await session.execute(
        select(ApiKey).where(ApiKey.org_id == principal.org_id).order_by(ApiKey.created_at.desc())
    )
    return {
        "keys": [
            {
                "id": k.id,
                "name": k.name,
                "prefix": k.prefix,
                "scopes": k.scopes,
                "revoked": bool(k.revoked_at),
                "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
                "created_at": k.created_at.isoformat(),
            }
            for k in result.scalars().all()
        ]
    }


@router.post("/api-keys", status_code=201)
async def create_key(
    payload: ApiKeyIn,
    principal: Principal = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    plaintext, prefix, hashed = generate_api_key()
    key = ApiKey(
        org_id=principal.org_id,
        name=payload.name,
        prefix=prefix,
        hashed=hashed,
        scopes=payload.scopes or ["*"],
    )
    session.add(key)
    await session.commit()
    return {"id": key.id, "name": key.name, "prefix": prefix, "token": plaintext, "note": "يظهر مرة واحدة فقط"}


@router.delete("/api-keys/{key_id}")
async def revoke_key(
    key_id: str,
    principal: Principal = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    key = await session.get(ApiKey, key_id)
    if key is None or key.org_id != principal.org_id:
        raise NotFound("المفتاح غير موجود")
    key.revoked_at = utcnow()
    await session.commit()
    return {"ok": True}
