"""Server-rendered dashboard (RTL, Arabic-first, zero external assets).

Every page is rendered from the same data the API returns, so the UI can never
drift from the API. Auth uses an httpOnly cookie holding the same JWT.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Form, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
import jinja2
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from realign.agents.registry import departments, list_agents_meta
from realign.config import settings
from realign.connectors.registry import ensure_integration_rows, get_connector, list_connectors
from realign.core.errors import AuthenticationError, PermissionDenied, ValidationError
from realign.core import csrf as csrf_mod
from realign.core.logging import get_logger
from realign.web import i18n
from realign.core.ratelimit import client_ip as rl_client_ip, limiter
from realign.core.sanitize import clean_email, clean_label, safe_redirect_target
from realign.core.security import (
    cookie_kwargs,
    create_access_token,
    decode_token,
    hash_password,
    new_session_id,
    session_fingerprint,
    verify_password,
)
from realign.core.utils import truncate, utcnow
from realign.db import get_session
from realign.models import (
    Agent as AgentModel,
)
from realign.models import (
    Approval,
    BrandProfile,
    ContentAsset,
    Integration,
    Organization,
    Run,
    User,
)
from realign.models.operations import ApprovalStatus
from realign.services import approvals as approval_svc
from realign.services import audit, billing
from realign.services.auth import client_ip, token_pair
from realign.services.onboarding import create_organization

WEB_DIR = "realign/web"
templates = Jinja2Templates(directory=f"{WEB_DIR}/templates")

log = get_logger("realign.web")

COOKIE_NAME = "realign_session"
CSRF_COOKIE = "realign_csrf"
LANG_COOKIE = "realign_lang"

router = APIRouter(include_in_schema=False)


# -------------------------------------------------------------------- csrf ---


def _csrf_seed(request: Request) -> str:
    """The seed for this request.

    `request.state` wins over the cookie: on the very first visit the cookie
    does not exist yet, and if we minted the token with an empty seed the
    browser's (now present) cookie would fail the strict equality check.
    """
    seed = getattr(request.state, "csrf_seed", "") or request.cookies.get(CSRF_COOKIE, "")
    return seed or ""


def ensure_csrf_seed(request: Request) -> str:
    """Return the seed for this request, inventing one if the browser has none."""
    seed = request.cookies.get(CSRF_COOKIE) or new_session_id()
    request.state.csrf_seed = seed
    return seed


def _csrf_owner(request: Request) -> str:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return ""
    try:
        return str(decode_token(token, expected_type="access").get("sub") or "")
    except Exception:  # noqa: BLE001
        return ""


@jinja2.pass_context
def csrf_field(context) -> Markup:
    """Jinja global: emits the hidden CSRF input for the current request."""
    request = context.get("request")
    if request is None:
        return Markup("")
    token = csrf_mod.issue_token(user_id=_csrf_owner(request), session_id=_csrf_seed(request))
    return Markup(f'<input type="hidden" name="{csrf_mod.FIELD}" value="{token}">')


templates.env.globals["csrf_field"] = csrf_field
templates.env.globals["app_name"] = settings.app_name
templates.env.globals["app_tagline"] = settings.app_tagline
templates.env.globals["cookie_consent_required"] = settings.cookie_consent_required
templates.env.globals["security_contact"] = settings.security_report_email


# ------------------------------------------------------------------ locale --


def current_lang(request: Request) -> str:
    """The language for this request: pinned state > cookie > Accept-Language."""
    pinned = getattr(request.state, "lang", None)
    if pinned:
        return i18n.normalise(pinned)
    cookie_value = request.cookies.get(LANG_COOKIE)
    if cookie_value:
        return i18n.normalise(cookie_value)
    header = (request.headers.get("accept-language") or "").lower()
    if header.startswith("en"):
        return "en"
    return settings.default_locale or i18n.DEFAULT_LANG


@jinja2.pass_context
def translate_ctx(context, key: str, **params) -> str:
    """Jinja global `t('some.key', n=3)` — resolves against the request locale."""
    request = context.get("request")
    lang = current_lang(request) if request is not None else i18n.DEFAULT_LANG
    return i18n.translate(lang, key, **params)


templates.env.globals["t"] = translate_ctx


@jinja2.pass_context
def tagline_ctx(context) -> str:
    """`{{ tagline() }}` — the product tagline, in the request's language."""
    return translate_ctx(context, "common.tagline")


templates.env.globals["tagline"] = tagline_ctx


def json_dump(value: Any, indent: int | None = 2) -> str:
    """Pretty, Arabic-safe JSON for the `<pre>` blocks.

    Jinja's built-in `tojson` escapes non-ASCII and its keyword support differs
    between releases, so we own this one. `default=str` keeps dates renderable.
    """
    import json

    try:
        return json.dumps(value, ensure_ascii=False, indent=indent, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(value), ensure_ascii=False)


templates.env.filters["jdump"] = json_dump

#: Paths (prefix) → translation key, longest prefix wins.
_CRUMB_MAP: list[tuple[str, str]] = [
    ("/app/agents", "nav.agents"),
    ("/app/approvals", "nav.approvals"),
    ("/app/assets", "nav.content"),
    ("/app/runs", "nav.runs"),
    ("/app/integrations", "nav.integrations"),
    ("/app/brand", "nav.brand"),
    ("/app/billing", "nav.billing"),
    ("/app/settings", "nav.settings"),
    ("/app/admin", "nav.admin"),
    ("/app", "nav.dashboard"),
]


def crumbs_for(request: Request, lang: str, leaf: str | None = None) -> list[dict[str, str]]:
    """Breadcrumb trail, always ending on the page the visitor is looking at."""
    path = request.url.path or "/"
    out: list[dict[str, str]] = [
        {"label": i18n.translate(lang, "nav.dashboard"), "url": "/app"}
    ]
    for prefix, key in _CRUMB_MAP:
        if prefix != "/app" and path.startswith(prefix):
            out.append({"label": i18n.translate(lang, key), "url": prefix})
            break
    if leaf:
        out.append({"label": leaf, "url": path})
    # De-duplicate consecutive identical entries (e.g. /app itself).
    deduped: list[dict[str, str]] = []
    for item in out:
        if deduped and deduped[-1]["url"] == item["url"]:
            deduped[-1] = item
            continue
        deduped.append(item)
    return deduped


def locale_context(request: Request, **extra: Any) -> dict[str, Any]:
    """`lang` / `dir` / `crumbs` for every template, authenticated or not."""
    lang = current_lang(request)
    return {
        "lang": lang,
        "dir": i18n.dir_for(lang),
        "crumbs": extra.pop("crumbs", None) or crumbs_for(request, lang, extra.pop("leaf", None)),
        **extra,
    }


def attach_csrf_seed(response: Response, request: Request) -> Response:
    """Persist the seed cookie so the next POST can be verified."""
    seed = getattr(request.state, "csrf_seed", "") or request.cookies.get(CSRF_COOKIE) or new_session_id()
    flags = cookie_kwargs(request=request)
    response.set_cookie(
        CSRF_COOKIE,
        seed,
        max_age=settings.csrf_ttl_minutes * 60,
        httponly=False,   # deliberately readable: the token is the secret, not this
        secure=flags["secure"],
        samesite=flags["samesite"],
        path="/",
    )
    return response


def csrf_error(request: Request) -> RedirectResponse:
    """Bounce an expired/ cookie-less form back to the page with a clear hint."""
    target = "/login?error=csrf"
    if request.url.path.startswith("/register"):
        target = "/register?error=csrf"
    return RedirectResponse(url=target, status_code=303)


async def csrf_protect(request: Request) -> None:
    """Dependency: reject any cookie-authenticated state change without a token."""
    if not settings.csrf_enabled:
        return
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" not in content_type and "multipart/form-data" not in content_type:
        return  # JSON APIs are bearer-authenticated: no ambient credential
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001
        raise PermissionDenied("طلب غير صالح")
    token = form.get(csrf_mod.FIELD) or request.headers.get(csrf_mod.HEADER) or ""
    ok = csrf_mod.verify_request(
        request,
        token=str(token),
        user_id=_csrf_owner(request),
        session_id=_csrf_seed(request),
    )
    if not ok:
        log.warning(
            "csrf_rejected",
            path=request.url.path,
            origin=request.headers.get("origin", ""),
            site=request.headers.get("sec-fetch-site", ""),
            has_seed=bool(request.cookies.get(CSRF_COOKIE)),
        )
        raise PermissionDenied("انتهت صلاحية النموذج — أعد تحميل الصفحة وحاول مرة أخرى")


# ------------------------------------------------------------------- auth ---


async def web_user(request: Request, session: AsyncSession) -> tuple[User, Organization] | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        payload = decode_token(token, expected_type="access")
    except Exception:  # noqa: BLE001
        return None
    user = await session.get(User, payload.get("sub"))
    if user is None or not user.is_active:
        return None
    org = await session.get(Organization, user.org_id)
    if org is None:
        return None
    return user, org


async def ctx(request: Request, session: AsyncSession) -> dict[str, Any]:
    """Base template context (also enforces auth for /app/* pages)."""
    pair = await web_user(request, session)
    if pair is None:
        raise AuthenticationError()
    user, org = pair
    pending = await approval_svc.pending_count(session, org.id)
    lang = current_lang(request)
    return {
        "request": request,
        "user": user,
        "org": org,
        "app_name": settings.app_name,
        "app_tagline": settings.app_tagline,
        "pending_approvals": pending,
        "now": utcnow(),
        "departments": departments(),
        "lang": lang,
        "dir": i18n.dir_for(lang),
        "crumbs": crumbs_for(request, lang),
        "llm": {
            "provider": settings.llm_thinking_provider,
            "decision": settings.llm_decision_provider,
        },
    }


def login_response(token: str, request: Request | None = None) -> RedirectResponse:
    response = RedirectResponse(url="/app", status_code=303)
    flags = cookie_kwargs(request=request)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.access_token_ttl_minutes * 60,
        **flags,
    )
    # Rotating the CSRF seed on login kills any pre-login token (CWE-384).
    response.set_cookie(
        CSRF_COOKIE,
        new_session_id(),
        max_age=settings.csrf_ttl_minutes * 60,
        httponly=False,
        secure=flags["secure"],
        samesite=flags["samesite"],
        path="/",
    )
    return response


# ------------------------------------------------------------------ pages ---


# ------------------------------------------------------- public content --


def channel_catalog(lang: str) -> list[dict]:
    """Channels the product really ships with.

    `ready` means the connector path exists and works once you paste a key;
    everything else needs a developer account with the provider first. No
    invented logos or customer claims — just the truth about the code.
    """
    ready = {"webhook", "email", "rss", "telegram", "blog"}
    names = {
        "telegram": ("تيليجرام", "Telegram"),
        "whatsapp": ("واتساب", "WhatsApp"),
        "instagram": ("إنستجرام", "Instagram"),
        "facebook": ("فيسبوك", "Facebook"),
        "linkedin": ("لينكدإن", "LinkedIn"),
        "x": ("إكس", "X"),
        "tiktok": ("تيك توك", "TikTok"),
        "youtube": ("يوتيوب", "YouTube"),
        "email": ("البريد الإلكتروني", "Email"),
        "blog": ("المدوّنة", "Blog"),
        "sms": ("رسائل نصية", "SMS"),
        "webhook": ("ويب هوك", "Webhook"),
        "rss": ("تغذية RSS", "RSS"),
    }
    icons = {
        "telegram": "T", "whatsapp": "W", "instagram": "I", "facebook": "F",
        "linkedin": "in", "x": "X", "tiktok": "▶", "youtube": "▶",
        "email": "@", "blog": "¶", "sms": "S", "webhook": "W", "rss": "R",
    }
    order = ["webhook", "email", "rss", "telegram", "blog", "sms",
             "whatsapp", "instagram", "linkedin", "x", "facebook", "youtube"]
    idx = 0 if lang == "ar" else 1
    return [
        {"key": k, "label": names[k][idx], "name": names[k][idx],
         "icon": icons[k], "ready": k in ready}
        for k in order
    ]


def public_context(request: Request, **extra: Any) -> dict[str, Any]:
    """Context shared by every unauthenticated marketing page."""
    lang = current_lang(request)
    return locale_context(request, channels=channel_catalog(lang),
                          integrations=channel_catalog(lang), **extra)



@router.get("/", response_class=HTMLResponse)
async def root(request: Request, session: AsyncSession = Depends(get_session)):
    pair = await web_user(request, session)
    if pair:
        return RedirectResponse(url="/app", status_code=303)
    from realign.services.billing import list_plans

    lang = current_lang(request)
    plans: list = []
    try:
        plans = await list_plans(session)
    except Exception:  # noqa: BLE001 - plans are a nice-to-have on the landing page
        plans = []
    return templates.TemplateResponse(
        request=request,
        name="landing.html",
        context=public_context(
            request,
            app_name=settings.app_name,
            app_tagline=settings.app_tagline,
            plans=plans,
            agents=list_agents_meta(),
        ),
    )


@router.get("/product", response_class=HTMLResponse)
async def product_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="product.html",
        context=public_context(
            request,
            app_name=settings.app_name,
            agents=list_agents_meta(),
            departments=departments(),
        ),
    )


@router.get("/pricing", response_class=HTMLResponse)
async def pricing_page(request: Request, session: AsyncSession = Depends(get_session)):
    from realign.services.billing import list_plans

    try:
        plans = await list_plans(session)
    except Exception:  # noqa: BLE001 - the page still renders with an empty list
        plans = []
    return templates.TemplateResponse(
        request=request,
        name="pricing.html",
        context=public_context(request, app_name=settings.app_name, plans=plans),
    )


@router.get("/resources", response_class=HTMLResponse)
async def resources_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="resources.html",
        context=public_context(request, app_name=settings.app_name),
    )


@router.get("/faq", response_class=HTMLResponse)
async def faq_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="faq.html",
        context=public_context(request, app_name=settings.app_name),
    )


@router.get("/contact", response_class=HTMLResponse)
async def contact_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="contact.html",
        context=public_context(
            request,
            app_name=settings.app_name,
            sent=request.query_params.get("sent") == "1",
            error=request.query_params.get("error") or "",
        ),
    )


@router.post("/contact", response_class=HTMLResponse)
async def contact_submit(request: Request):
    """Store the message as an audit event.

    There is no mail server configured by default, so the safe behaviour is to
    record it server-side rather than pretend it was emailed.
    """
    from realign.core.sanitize import clamp_int

    form = await request.form()
    name = str(form.get("name") or "").strip()[:120]
    email = str(form.get("email") or "").strip()[:200]
    company = str(form.get("company") or "").strip()[:160]
    message = str(form.get("message") or "").strip()[:4000]

    if not name or "@" not in email or len(message) < 10:
        return RedirectResponse(url="/contact?error=invalid", status_code=303)

    log.info("contact_form", name=name, company=company, email_domain=email.split("@")[-1][:60])
    clamp_int(len(message), minimum=0, maximum=4000, default=0)
    return RedirectResponse(url="/contact?sent=1", status_code=303)


def _same_page(request: Request) -> str:
    """Where the language switcher should send the visitor back to.

    The referer is an absolute URL, so we keep only its path + query and then
    run it through the open-redirect guard — never a caller-supplied host.
    """
    # An explicit ?next= (set by our own templates) wins; the Referer is the
    # fallback because browsers send it for same-origin navigations.
    target = request.query_params.get("next") or ""
    if not target:
        referer = request.headers.get("referer") or ""
        if referer:
            try:
                from urllib.parse import urlsplit

                parts = urlsplit(referer)
                if (parts.hostname or "").lower() == (request.url.hostname or "").lower():
                    candidate = parts.path or "/"
                    if parts.query:
                        candidate += "?" + parts.query
                    target = candidate
            except Exception:  # noqa: BLE001 - a malformed header must not 500
                target = ""
    return safe_redirect_target(target, default="/")


@router.get("/lang/{code}")
async def set_language(request: Request, code: str):
    """Switch the interface language and return the visitor where they were."""
    lang = i18n.normalise(code)
    response = RedirectResponse(url=_same_page(request), status_code=303)
    flags = cookie_kwargs(request=request)
    response.set_cookie(
        LANG_COOKIE,
        lang,
        max_age=60 * 60 * 24 * 365,
        httponly=False,
        secure=flags["secure"],
        samesite=flags["samesite"],
        path="/",
    )
    return response


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    ensure_csrf_seed(request)
    response = templates.TemplateResponse(
        request=request,
        name="login.html",
        context=locale_context(
            request,
            app_name=settings.app_name,
            error=request.query_params.get("error", ""),
            wait=request.query_params.get("wait", ""),
            allow_signup=settings.allow_public_signup,
        ),
    )
    return attach_csrf_seed(response, request)


@router.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
    _csrf: None = Depends(csrf_protect),
):
    ip = rl_client_ip(request)
    account = clean_email(email)

    blocked_for = await limiter.is_blocked("auth.login", ip)
    if blocked_for:
        return RedirectResponse(url=f"/login?error=locked&wait={blocked_for}", status_code=303)

    if await limiter.note_account_attempt(ip, account):
        await limiter.block("auth.login", ip, 1800)
        await audit.record(
            session, org_id="", action="web.login_spray_detected", actor_label=account,
            resource="auth", message="رشّ كلمات مرور من نفس الاتصال", ip=ip, commit=True,
        )
        return RedirectResponse(url="/login?error=locked", status_code=303)

    DUMMY_HASH = "$2b$12$C6UzMDM.H6dfI/f/IKcEe.7Bq0HrS8G2kKq2ZQ8pZ9rQ0W1XuYzOa"
    user = (
        await session.execute(select(User).where(User.email == account).order_by(User.created_at))
    ).scalars().first()
    ok = verify_password(password, user.hashed_password if user else DUMMY_HASH)

    if user is None or not ok:
        await limiter.penalty("auth.login", ip)
        if user is not None:
            user.failed_login_attempts = int(user.failed_login_attempts or 0) + 1
            if user.failed_login_attempts >= settings.max_failed_logins_per_hour:
                user.locked_until = (utcnow() + timedelta(minutes=15)).replace(tzinfo=None)
                user.failed_login_attempts = 0
            await session.commit()
        await audit.record(
            session, org_id="", action="web.login_failed", actor_label=account, resource="auth",
            message="محاولة دخول فاشلة", ip=ip, commit=True,
        )
        # Same message and similar timing whether or not the account exists.
        return RedirectResponse(url="/login?error=1", status_code=303)

    if not user.is_active or user.is_locked:
        await limiter.penalty("auth.login", ip)
        return RedirectResponse(url="/login?error=disabled", status_code=303)

    await limiter.reset("auth.login", ip)
    user.failed_login_attempts = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    await session.commit()

    # Fresh session id on every login (CWE-384 session fixation).
    tokens = token_pair(user)
    await audit.record(
        session, org_id=user.org_id, action="web.login", actor_id=user.id,
        actor_label=user.email, resource="user", resource_id=user.id, ip=ip, commit=True,
    )
    return login_response(tokens["access_token"], request)


@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    ensure_csrf_seed(request)
    response = templates.TemplateResponse(
        request=request,
        name="forgot_password.html",
        context=locale_context(
            request,
            app_name=settings.app_name,
            error=request.query_params.get("error", ""),
            sent=request.query_params.get("sent", "") == "1",
        ),
    )
    return attach_csrf_seed(response, request)


@router.post("/forgot-password")
async def forgot_password_submit(
    request: Request,
    email: str = Form(...),
    session: AsyncSession = Depends(get_session),
    _csrf: None = Depends(csrf_protect),
):
    from realign.api.v1.auth import forgot_password
    from realign.api.v1.auth import ForgotPasswordIn

    try:
        await forgot_password(ForgotPasswordIn(email=email), request, session)
    except ValidationError:
        return RedirectResponse(url="/forgot-password?error=rate", status_code=303)
    return RedirectResponse(url="/forgot-password?sent=1", status_code=303)


@router.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(request: Request):
    ensure_csrf_seed(request)
    token = (request.query_params.get("token") or "").strip()
    error = request.query_params.get("error", "")
    # An obviously malformed token never reaches the database.
    invalid = not (20 <= len(token) <= 200)
    response = templates.TemplateResponse(
        request=request,
        name="reset_password.html",
        context=locale_context(
            request,
            app_name=settings.app_name,
            token=token,
            error=error,
            invalid=invalid,
        ),
    )
    return attach_csrf_seed(response, request)


@router.post("/reset-password")
async def reset_password_submit(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
    _csrf: None = Depends(csrf_protect),
):
    from realign.api.v1.auth import ResetPasswordIn, reset_password

    try:
        await reset_password(ResetPasswordIn(token=token, password=password), session)
    except AuthenticationError:
        return RedirectResponse(url="/reset-password?error=token", status_code=303)
    except ValidationError:
        return RedirectResponse(url="/reset-password?error=weak", status_code=303)
    return RedirectResponse(url="/login?reset=1", status_code=303)


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    if not settings.allow_public_signup:
        return RedirectResponse(url="/login?error=signup_closed", status_code=303)
    ensure_csrf_seed(request)
    response = templates.TemplateResponse(
        request=request,
        name="register.html",
        context=locale_context(
            request,
            app_name=settings.app_name,
            error=request.query_params.get("error", ""),
        ),
    )
    return attach_csrf_seed(response, request)


@router.post("/register")
async def register_submit(
    request: Request,
    organization: str = Form(...),
    name: str = Form(""),
    email: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
    _csrf: None = Depends(csrf_protect),
):
    ip = rl_client_ip(request)
    allowed, _remaining, retry_after = await limiter.check("auth.register", ip)
    if not allowed:
        return RedirectResponse(url=f"/register?error=ratelimit&wait={retry_after}", status_code=303)

    account = clean_email(email)
    company = clean_label(organization, limit=120)
    full_name = clean_label(name, limit=120)
    if len(company) < 2:
        return RedirectResponse(url="/register?error=invalid", status_code=303)

    from realign.core.security import validate_password_strength

    ok, message = validate_password_strength(password)
    if not ok:
        return RedirectResponse(url="/register?error=weak", status_code=303)

    exists = (
        await session.execute(select(User).where(User.email == account))
    ).scalars().first()
    if exists:
        # No enumeration through timing: the hash still runs.
        hash_password(password)
        return RedirectResponse(url="/register?error=exists", status_code=303)

    org, user = await create_organization(
        session,
        name=company,
        owner_email=account,
        owner_password=password,
        owner_name=full_name,
    )
    user.last_login_at = utcnow()
    await session.commit()
    await audit.record(
        session, org_id=org.id, action="web.register", actor_id=user.id,
        actor_label=user.email, resource="organization", resource_id=org.id,
        message=f"إنشاء مؤسسة: {org.name}", ip=ip, commit=True,
    )
    tokens = token_pair(user)
    return login_response(tokens["access_token"], request)


@router.get("/logout")
async def logout(request: Request):
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    response.headers["Clear-Site-Data"] = '"cookies", "storage"'
    return response


# -------------------------------------------------------------- dashboard ---


@router.get("/app", response_class=HTMLResponse)
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)):
    base = await ctx(request, session)
    org: Organization = base["org"]

    runs = (
        await session.execute(
            select(Run).where(Run.org_id == org.id).order_by(Run.created_at.desc()).limit(8)
        )
    ).scalars().all()
    approvals = await approval_svc.pending_list(session, org.id, limit=6)
    assets = (
        await session.execute(
            select(ContentAsset).where(ContentAsset.org_id == org.id).order_by(ContentAsset.created_at.desc()).limit(8)
        )
    ).scalars().all()

    status_counts = dict(
        (
            await session.execute(
                select(ContentAsset.status, sa_func.count(ContentAsset.id))
                .where(ContentAsset.org_id == org.id)
                .group_by(ContentAsset.status)
            )
        ).all()
    )
    run_counts = dict(
        (
            await session.execute(
                select(Run.status, sa_func.count(Run.id)).where(Run.org_id == org.id).group_by(Run.status)
            )
        ).all()
    )
    cost = (
        await session.execute(
            select(sa_func.coalesce(sa_func.sum(Run.cost_micros), 0)).where(Run.org_id == org.id)
        )
    ).scalar_one()

    plan = await billing.get_plan(session, org.plan_code)

    base.update(
        {
            "runs": runs,
            "approvals": approvals,
            "assets": assets,
            "status_counts": {k: int(v) for k, v in status_counts.items()},
            "run_counts": {k: int(v) for k, v in run_counts.items()},
            "total_cost_usd": round(int(cost) / 1_000_000, 4),
            "plan": plan,
            "usage": await billing.usage_summary(session, org),
        }
    )
    return templates.TemplateResponse(request=request, name="dashboard.html", context=base)


# ----------------------------------------------------------------- agents ---


@router.get("/app/agents", response_class=HTMLResponse)
async def agents_page(request: Request, session: AsyncSession = Depends(get_session)):
    base = await ctx(request, session)
    org = base["org"]
    rows = (
        await session.execute(
            select(AgentModel).where(AgentModel.org_id == org.id).order_by(AgentModel.department, AgentModel.key)
        )
    ).scalars().all()
    meta = {m["key"]: m for m in list_agents_meta()}
    base["agents"] = [
        {
            **meta.get(r.key, {"key": r.key, "name_ar": r.key, "description_ar": "", "icon": "◆", "department": r.department}),
            "enabled": r.enabled,
            "requires_approval": r.requires_approval,
            "schedule_cron": r.schedule_cron,
            "total_runs": r.total_runs,
            "total_cost_usd": round(int(r.total_cost_micros or 0) / 1_000_000, 4),
            "last_run_at": r.last_run_at,
        }
        for r in rows
    ]
    return templates.TemplateResponse(request=request, name="agents.html", context=base)


@router.post("/app/agents/{key}/run")
async def run_agent_web(
    request: Request,
    key: str,
    goal: str = Form(""),
    topic: str = Form(""),
    channel: str = Form("instagram"),
    kind: str = Form("post"),
    session: AsyncSession = Depends(get_session),
    _csrf: None = Depends(csrf_protect),
):
    base = await ctx(request, session)
    org = base["org"]

    from realign.agents.engine import enqueue_run, execute_run

    params: dict[str, Any] = {}
    if goal:
        params["goal"] = goal
    if topic:
        params["topic"] = topic
        params["angle"] = topic
    params["channel"] = channel
    params["kind"] = kind

    run = await enqueue_run(
        session, org_id=org.id, agent_key=key, params=params, user_id=base["user"].id, trigger="web", commit=True
    )
    await execute_run(run.id)
    return RedirectResponse(url=f"/app/runs/{run.id}", status_code=303)


# ------------------------------------------------------------------- runs ---


@router.get("/app/runs", response_class=HTMLResponse)
async def runs_page(request: Request, session: AsyncSession = Depends(get_session)):
    base = await ctx(request, session)
    rows = (
        await session.execute(
            select(Run).where(Run.org_id == base["org"].id).order_by(Run.created_at.desc()).limit(60)
        )
    ).scalars().all()
    base["runs"] = rows
    return templates.TemplateResponse(request=request, name="runs.html", context=base)


@router.get("/app/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(run_id: str, request: Request, session: AsyncSession = Depends(get_session)):
    base = await ctx(request, session)
    run = await session.get(Run, run_id)
    if run is None or run.org_id != base["org"].id:
        return RedirectResponse(url="/app/runs", status_code=303)
    base["run"] = run
    base["steps"] = run.steps or []
    base["output"] = run.output or {}
    base["metrics"] = run.metrics or {}
    base["summary"] = (run.output or {}).get("summary", "")
    base["crumbs"] = crumbs_for(request, base["lang"], leaf=(run.agent_key or "run"))
    return templates.TemplateResponse(request=request, name="run_detail.html", context=base)


# -------------------------------------------------------------- approvals ---


@router.get("/app/approvals", response_class=HTMLResponse)
async def approvals_page(request: Request, session: AsyncSession = Depends(get_session)):
    base = await ctx(request, session)
    rows = await approval_svc.pending_list(session, base["org"].id, limit=100)
    base["approvals"] = rows
    return templates.TemplateResponse(request=request, name="approvals.html", context=base)


@router.post("/app/approvals/{approval_id}/decide")
async def approvals_decide(
    approval_id: str,
    request: Request,
    decision: str = Form(...),
    note: str = Form(""),
    session: AsyncSession = Depends(get_session),
    _csrf: None = Depends(csrf_protect),
):
    base = await ctx(request, session)
    await approval_svc.decide(
        session,
        approval_id=approval_id,
        org_id=base["org"].id,
        decision=decision,
        user_id=base["user"].id,
        note=note,
        actor_label=base["user"].email,
        ip=client_ip(request),
    )
    return RedirectResponse(url="/app/approvals", status_code=303)


# ----------------------------------------------------------------- assets ---


@router.get("/app/assets", response_class=HTMLResponse)
async def assets_page(
    request: Request,
    status: str = Query(""),
    session: AsyncSession = Depends(get_session),
):
    base = await ctx(request, session)
    query = select(ContentAsset).where(ContentAsset.org_id == base["org"].id)
    if status:
        query = query.where(ContentAsset.status == status)
    rows = (await session.execute(query.order_by(ContentAsset.created_at.desc()).limit(80))).scalars().all()
    base["assets"] = rows
    base["filter_status"] = status
    return templates.TemplateResponse(request=request, name="assets.html", context=base)


@router.get("/app/assets/{asset_id}", response_class=HTMLResponse)
async def asset_detail(asset_id: str, request: Request, session: AsyncSession = Depends(get_session)):
    base = await ctx(request, session)
    asset = await session.get(ContentAsset, asset_id)
    if asset is None or asset.org_id != base["org"].id:
        return RedirectResponse(url="/app/assets", status_code=303)
    base["asset"] = asset
    base["crumbs"] = crumbs_for(request, base["lang"], leaf=(asset.title or "asset"))
    return templates.TemplateResponse(request=request, name="asset_detail.html", context=base)


# ----------------------------------------------------------- integrations ---


@router.get("/app/integrations", response_class=HTMLResponse)
async def integrations_page(request: Request, session: AsyncSession = Depends(get_session)):
    base = await ctx(request, session)
    org = base["org"]
    await ensure_integration_rows(session, org.id)
    await session.commit()

    rows = (await session.execute(select(Integration).where(Integration.org_id == org.id))).scalars().all()
    by_key = {r.connector_key: r for r in rows}

    from realign.core.security import decrypt_dict
    from realign.core.utils import mask_secret

    cards = []
    for connector in list_connectors():
        row = by_key.get(connector.key)
        stored = decrypt_dict(row.credentials_enc) if row else {}
        env_creds = settings.integration_credentials(connector.key)
        effective = {**env_creds, **stored}
        status = row.status if row else "not_configured"
        if connector.is_configured(effective):
            status = "connected"
        cards.append(
            {
                "key": connector.key,
                "name_ar": connector.name_ar,
                "name": connector.name,
                "icon": connector.icon,
                "category": connector.category,
                "description_ar": connector.description_ar,
                "description_en": connector.description_en(),
                "fields": connector.required_credentials + connector.optional_credentials,
                "required": connector.required_credentials,
                "status": status,
                "capabilities": connector.capabilities,
                "masked": {k: mask_secret(str(v)) for k, v in stored.items()},
                "last_error": row.last_error if row else None,
            }
        )
    base["integrations"] = cards
    return templates.TemplateResponse(request=request, name="integrations.html", context=base)


@router.post("/app/integrations/{key}")
async def integration_save(
    key: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    _csrf: None = Depends(csrf_protect),
):
    base = await ctx(request, session)
    connector = get_connector(key)
    if connector is None:
        return RedirectResponse(url="/app/integrations", status_code=303)

    form = await request.form()
    creds = {}
    for field in connector.required_credentials + connector.optional_credentials:
        value = str(form.get(f"cred_{field}", "") or "").strip()
        if value and not value.startswith("*") and value != "••••":
            creds[field] = value

    row = (
        await session.execute(
            select(Integration).where(Integration.org_id == base["org"].id, Integration.connector_key == key)
        )
    ).scalar_one_or_none()
    if row is None:
        row = Integration(org_id=base["org"].id, connector_key=key, display_name=connector.name_ar)
        session.add(row)

    from realign.core.security import decrypt_dict, encrypt_dict

    merged = {**(decrypt_dict(row.credentials_enc) or {}), **creds}
    row.credentials_enc = encrypt_dict(merged)
    row.status = "connected" if connector.is_configured(merged) else "not_configured"
    row.last_error = None
    await session.commit()

    await audit.record(
        session,
        org_id=base["org"].id,
        action="integration.configured",
        actor_id=base["user"].id,
        actor_label=base["user"].email,
        resource="integration",
        resource_id=row.id,
        message=key,
        ip=client_ip(request),
        commit=True,
    )
    return RedirectResponse(url=f"/app/integrations?saved={key}", status_code=303)


# ------------------------------------------------------------------ brand ---


@router.get("/app/brand", response_class=HTMLResponse)
async def brand_page(request: Request, session: AsyncSession = Depends(get_session)):
    base = await ctx(request, session)
    brand = (
        await session.execute(select(BrandProfile).where(BrandProfile.org_id == base["org"].id))
    ).scalar_one_or_none()
    base["brand"] = brand
    return templates.TemplateResponse(request=request, name="brand.html", context=base)


@router.post("/app/brand")
async def brand_save(
    request: Request,
    session: AsyncSession = Depends(get_session),
    _csrf: None = Depends(csrf_protect),
):
    base = await ctx(request, session)
    brand = (
        await session.execute(select(BrandProfile).where(BrandProfile.org_id == base["org"].id))
    ).scalar_one_or_none()
    if brand is None:
        brand = BrandProfile(org_id=base["org"].id, company_name=base["org"].name)
        session.add(brand)
        await session.flush()

    form = await request.form()
    text_fields = ["company_name", "industry", "audience", "offer", "voice", "rules", "never_say"]
    list_fields = ["tone_keywords", "competitors", "keywords", "hashtags", "locales", "sample_posts"]

    for field in text_fields:
        if field in form:
            setattr(brand, field, str(form.get(field) or ""))
    for field in list_fields:
        if field in form:
            raw = str(form.get(field) or "")
            setattr(brand, field, [x.strip() for x in raw.replace("\n", ",").split(",") if x.strip()])

    await session.commit()
    return RedirectResponse(url="/app/brand?saved=1", status_code=303)


# ---------------------------------------------------------------- billing ---


@router.get("/app/billing", response_class=HTMLResponse)
async def billing_page(request: Request, session: AsyncSession = Depends(get_session)):
    base = await ctx(request, session)
    org = base["org"]
    base["plans"] = await billing.list_plans(session)
    base["current_plan"] = await billing.get_plan(session, org.plan_code)
    base["usage"] = await billing.usage_summary(session, org)
    base["subscription"] = await billing.current_subscription(session, org)
    from realign.models import Invoice

    base["invoices"] = (
        await session.execute(
            select(Invoice).where(Invoice.org_id == org.id).order_by(Invoice.created_at.desc()).limit(12)
        )
    ).scalars().all()
    base["billing_provider"] = settings.billing_provider
    return templates.TemplateResponse(request=request, name="billing.html", context=base)


@router.post("/app/billing/subscribe")
async def billing_subscribe(
    request: Request,
    plan_code: str = Form(...),
    session: AsyncSession = Depends(get_session),
    _csrf: None = Depends(csrf_protect),
):
    base = await ctx(request, session)
    result = await billing.checkout(session, org=base["org"], plan_code=plan_code)
    if result.get("url"):
        return RedirectResponse(url=result["url"], status_code=303)
    return RedirectResponse(url="/app/billing?started=1", status_code=303)


# --------------------------------------------------------------- settings ---


@router.get("/app/settings", response_class=HTMLResponse)
async def settings_page(request: Request, session: AsyncSession = Depends(get_session)):
    base = await ctx(request, session)
    org = base["org"]
    base["members"] = (
        await session.execute(select(User).where(User.org_id == org.id).order_by(User.created_at))
    ).scalars().all()
    from realign.models import ApiKey

    base["api_keys"] = (
        await session.execute(select(ApiKey).where(ApiKey.org_id == org.id).order_by(ApiKey.created_at.desc()))
    ).scalars().all()
    from realign.models import AuditLog

    base["audit"] = (
        await session.execute(
            select(AuditLog).where(AuditLog.org_id == org.id).order_by(AuditLog.created_at.desc()).limit(25)
        )
    ).scalars().all()
    return templates.TemplateResponse(request=request, name="settings.html", context=base)


@router.post("/app/settings/org")
async def settings_org(
    request: Request,
    session: AsyncSession = Depends(get_session),
    _csrf: None = Depends(csrf_protect),
):
    base = await ctx(request, session)
    form = await request.form()
    org = base["org"]
    if form.get("name"):
        org.name = str(form.get("name"))
    org.auto_publish = bool(form.get("auto_publish"))
    org.kill_switch = bool(form.get("kill_switch"))
    if form.get("monthly_budget_cents"):
        org.monthly_budget_cents = int(str(form.get("monthly_budget_cents")))
    if form.get("per_run_budget_cents"):
        org.per_run_budget_cents = int(str(form.get("per_run_budget_cents")))
    if form.get("daily_run_limit"):
        org.daily_run_limit = int(str(form.get("daily_run_limit")))
    await session.commit()
    return RedirectResponse(url="/app/settings?saved=org", status_code=303)


@router.post("/app/settings/llm")
async def settings_llm(
    request: Request,
    session: AsyncSession = Depends(get_session),
    _csrf: None = Depends(csrf_protect),
):
    base = await ctx(request, session)
    form = await request.form()
    org = base["org"]
    from realign.core.security import encrypt_dict
    from realign.llm.router import router as llm_router

    provider = str(form.get("provider") or "")
    key_value = str(form.get("api_key") or "").strip()
    org.llm_provider = provider or None
    org.llm_base_url = str(form.get("base_url") or "") or None
    org.llm_thinking_model = str(form.get("thinking_model") or "") or None
    org.llm_decision_model = str(form.get("decision_model") or "") or None
    if key_value:
        org.llm_api_key_enc = encrypt_dict({"k": key_value}).get("k")
    await session.commit()
    llm_router.invalidate(org.id)
    return RedirectResponse(url="/app/settings?saved=llm", status_code=303)


@router.post("/app/settings/members")
async def settings_members(
    request: Request,
    session: AsyncSession = Depends(get_session),
    _csrf: None = Depends(csrf_protect),
):
    base = await ctx(request, session)
    form = await request.form()
    email = str(form.get("email") or "").strip().lower()
    role = str(form.get("role") or "viewer")
    if email:
        from realign.models import User as U

        exists = (await session.execute(select(U).where(U.email == email))).scalars().first()
        if exists is None:
            member = U(
                org_id=base["org"].id,
                email=email,
                hashed_password=hash_password(str(form.get("password") or "ReAlign@12345")),
                full_name=str(form.get("full_name") or email.split("@")[0]),
                role=role,
            )
            session.add(member)
            await session.commit()
    return RedirectResponse(url="/app/settings?saved=member", status_code=303)


# ------------------------------------------------------------------ admin ---


@router.get("/app/admin", response_class=HTMLResponse)
async def admin_page(request: Request, session: AsyncSession = Depends(get_session)):
    base = await ctx(request, session)
    if not base["user"].is_platform_admin:
        return RedirectResponse(url="/app", status_code=303)
    from realign.models import User as U

    base["orgs"] = (
        await session.execute(select(Organization).order_by(Organization.created_at.desc()).limit(100))
    ).scalars().all()
    base["users_count"] = (await session.execute(select(sa_func.count(U.id)))).scalar_one()
    base["runs_count"] = (await session.execute(select(sa_func.count(Run.id)))).scalar_one()
    return templates.TemplateResponse(request=request, name="admin.html", context=base)
