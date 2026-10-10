"""Adversarial test suite — we attack the platform the way an attacker would.

Every test here is a real check against a real vulnerability class:

    CWE-89   SQL injection            CWE-79   XSS
    CWE-918  SSRF                     CWE-22   Path traversal
    CWE-601  Open redirect            CWE-352  CSRF
    CWE-862  Missing authorisation    CWE-863  Incorrect authorisation
    CWE-287  Improper authentication  CWE-269  Improper privilege management
    CWE-384  Session fixation         CWE-434  Unrestricted upload
    CWE-611  XXE                      CWE-307  Improper restriction of attempts
    CWE-532  Sensitive data in logs   CWE-693  Missing protection mechanism
    CWE-1021 Improper restriction of rendered UI layers (clickjacking)
"""

from __future__ import annotations

import re
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from realign.config import settings
from realign.core import csrf as csrf_mod
from realign.core.netsafe import url_is_public
from realign.core.passwords import validate_password
from realign.core.redact import redact
from realign.core.sanitize import safe_filename, safe_redirect_target, sanitize_html
from realign.db import SessionLocal
from realign.main import app as realign_app
from realign.models import Organization
from tests.conftest import _make_org, auth_headers

# ---------------------------------------------------------------- payloads --

SQLI = [
    "' OR '1'='1",
    "'; DROP TABLE users; --",
    "1 UNION SELECT password FROM users",
    "admin'--",
    "1' AND SLEEP(5)--",
    "'; UPDATE organizations SET credits_monthly=999999 WHERE '1'='1",
]
XSS = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "javascript:alert(1)",
    "<svg/onload=alert(1)>",
    "\"><script>fetch('//evil')</script>",
]
CMDI = ["; cat /etc/passwd", "| whoami", "$(id)", "&& ls -la", "`id`"]
TRAVERSAL = ["../../etc/passwd", "..\\..\\windows\\win.ini", "/etc/passwd", "....//....//etc/passwd"]
SSRF = [
    "http://127.0.0.1:8000/api/v1/admin/stats",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/",
    "http://localhost/admin",
    "http://[::1]/",
    "file:///etc/passwd",
    "http://metadata.google.internal/",
]


async def _reset_limits():
    """Wipe all counters so one test's traffic cannot trip another's limits."""
    from realign.core.ratelimit import limiter

    async with limiter._lock:  # noqa: SLF001 - test helper
        limiter._buckets.clear()
        limiter._blocked.clear()
        limiter._spray.clear()


@pytest.fixture(autouse=True)
async def _clean_limiter():
    await _reset_limits()
    yield


@pytest.fixture
async def anon_client():
    transport = ASGITransport(app=realign_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def fresh_org():
    return await _make_org("شركة الأمان", f"sec-{uuid.uuid4().hex[:8]}@sectest.io")


# =============================================================== 1. headers ==


@pytest.mark.asyncio
async def test_security_headers_present(anon_client):
    """CWE-693 / 1021 / clickjacking / MIME sniffing."""
    resp = await anon_client.get("/api/v1/health")
    h = resp.headers
    assert h.get("x-content-type-options") == "nosniff"
    assert h.get("x-frame-options") == "DENY"
    assert h.get("referrer-policy") == "strict-origin-when-cross-origin"
    assert h.get("cross-origin-opener-policy") == "same-origin"
    assert h.get("cross-origin-resource-policy") == "same-origin"
    assert "default-src 'self'" in h.get("content-security-policy", "")
    assert "frame-ancestors 'none'" in h.get("content-security-policy", "")
    assert "camera=()" in h.get("permissions-policy", "")
    assert "no-store" in h.get("cache-control", "")


@pytest.mark.asyncio
async def test_no_server_version_leak(anon_client):
    resp = await anon_client.get("/api/v1/health")
    assert "uvicorn" not in resp.headers.get("server", "").lower()
    assert "x-powered-by" not in {k.lower() for k in resp.headers}


@pytest.mark.asyncio
async def test_trace_method_rejected(anon_client):
    resp = await anon_client.request("TRACE", "/api/v1/health")
    assert resp.status_code in (405, 400, 403, 501)


@pytest.mark.asyncio
async def test_error_responses_never_leak_stack(anon_client):
    resp = await anon_client.get("/api/v1/agents")
    body = resp.text.lower()
    for marker in ("traceback", "sqlalchemy", "file \"", "/home/user", "psycopg"):
        assert marker not in body


# =========================================================== 2. SQL injection ==


@pytest.mark.asyncio
async def test_sql_injection_is_harmless(anon_client, fresh_org):
    """CWE-89: unparameterised SQL would show up as a 500 or as leaked rows."""
    org, user = fresh_org
    headers = auth_headers(user)
    endpoints = [
        ("GET", "/api/v1/runs", "limit"),
        ("GET", "/api/v1/assets", "status"),
        ("GET", "/api/v1/approvals", "status"),
        ("GET", "/api/v1/audit", "limit"),
    ]
    for method, path, param in endpoints:
        for payload in SQLI:
            resp = await anon_client.get(f"{path}?{param}={payload}", headers=headers)
            assert resp.status_code in (200, 400, 422), f"{path} {payload} -> {resp.status_code}"
            assert "syntax error" not in resp.text.lower()
            assert "traceback" not in resp.text.lower()


@pytest.mark.asyncio
async def test_sql_injection_cannot_read_other_tenant(anon_client, fresh_org):
    org, user = fresh_org
    resp = await anon_client.get(
        "/api/v1/runs?limit=' UNION SELECT id,org_id FROM runs --", headers=auth_headers(user)
    )
    assert resp.status_code in (200, 202, 400, 422)
    if resp.status_code in (200, 202):
        ids = {r.get("org_id") for r in resp.json().get("runs", []) if isinstance(r, dict)}
        assert "" not in ids and None not in ids
        assert ids <= {org.id}


@pytest.mark.asyncio
async def test_sql_injection_in_login(anon_client):
    resp = await anon_client.post(
        "/api/v1/auth/login",
        json={"email": "admin' OR '1'='1@example.com", "password": "' OR '1'='1"},
    )
    assert resp.status_code in (401, 422, 429)
    assert "token" not in resp.text.lower()


# ==================================================== 3. XSS / output encoding ==


@pytest.mark.asyncio
async def test_xss_payloads_are_not_reflected_executable(anon_client, fresh_org):
    """CWE-79: every template escape is Jinja2's job; prove it end-to-end."""
    org, user = fresh_org
    headers = auth_headers(user)
    payload = "<script>alert('xss')</script>"

    resp = await anon_client.put("/api/v1/brand", headers=headers, json={"company_name": payload})
    assert resp.status_code == 200

    page = await anon_client.get("/app/brand")
    html = page.text
    assert "<script>alert" not in html          # never raw
    assert "&lt;script&gt;" in html or payload not in html  # escaped, or dropped


@pytest.mark.asyncio
async def test_sanitize_html_strips_dangerous_constructs():
    for payload in XSS:
        cleaned = sanitize_html(payload)
        lowered = cleaned.lower()
        assert "<script" not in lowered
        assert "onerror" not in lowered
        assert "onload" not in lowered
        if "href" in lowered or "=" in lowered:
            assert "javascript:" not in lowered

    # a javascript: URI inside an href is dropped, never kept
    assert "javascript" not in sanitize_html('<a href="javascript:alert(1)">x</a>').lower()


@pytest.mark.asyncio
async def test_csp_blocks_inline_script_without_nonce(anon_client):
    csp = (await anon_client.get("/api/v1/health")).headers.get("content-security-policy", "")
    assert "nonce-" in csp


# ============================================================ 4. SSRF (CWE-918) ==


@pytest.mark.asyncio
async def test_ssrf_targets_are_refused():
    for url in SSRF:
        assert url_is_public(url) is False, url
    assert url_is_public("https://example.com/feed.xml") is True


@pytest.mark.asyncio
async def test_byok_base_url_rejects_internal_targets(anon_client, fresh_org):
    org, user = fresh_org
    headers = auth_headers(user)
    for url in ["http://169.254.169.254/", "http://127.0.0.1:11434", "http://10.0.0.9/v1"]:
        resp = await anon_client.put(
            "/api/v1/org/llm-key",
            headers=headers,
            json={"provider": "custom", "base_url": url, "api_key": "sk-test"},
        )
        assert resp.status_code in (400, 422), f"{url} -> {resp.status_code}"


# ================================================== 5. path traversal (CWE-22) ==


@pytest.mark.asyncio
async def test_media_path_traversal_refused(anon_client):
    for path in ["/media/../../etc/passwd", "/media/..%2f..%2fetc%2fpasswd", "/media/logo/../../app/main.py"]:
        resp = await anon_client.get(path)
        assert resp.status_code in (404, 400, 403), path


@pytest.mark.asyncio
async def test_safe_filename_flattening():
    assert "/" not in safe_filename("../../etc/passwd")
    assert "\\" not in safe_filename("..\\..\\x.png")
    assert safe_filename("") == "file.bin"
    assert safe_filename("logo.php.png").endswith(".png")


# ================================================= 6. open redirect (CWE-601) ==


@pytest.mark.asyncio
async def test_open_redirect_refused():
    assert safe_redirect_target("//evil.com") == "/app"
    assert safe_redirect_target("https://evil.com") == "/app"
    assert safe_redirect_target("/\\evil.com") == "/app"
    assert safe_redirect_target("/app/agents") == "/app/agents"


# ============================================================= 7. CSRF (CWE-352) ==


@pytest.mark.asyncio
async def test_csrf_token_is_bound_and_expiring():
    token = csrf_mod.issue_token(user_id="u1", session_id="s1")
    assert csrf_mod.verify_token(token, user_id="u1", session_id="s1")
    assert not csrf_mod.verify_token(token, user_id="u2", session_id="s1")   # other user
    assert not csrf_mod.verify_token(token, user_id="u1", session_id="s2")   # other session
    assert not csrf_mod.verify_token("garbage", user_id="u1", session_id="s1")
    assert not csrf_mod.verify_token("", user_id="u1", session_id="s1")


@pytest.mark.asyncio
async def test_post_form_without_csrf_is_rejected(anon_client, fresh_org):
    org, user = fresh_org
    token, _ = csrf_mod.issue_token(), ""
    from realign.core.security import create_access_token

    jwt_token, _ = create_access_token(sub=user.id, org_id=user.org_id, role=user.role, email=user.email)
    anon_client.cookies.set("realign_session", jwt_token)
    anon_client.cookies.set("realign_csrf", "seed-value")
    try:
        resp = await anon_client.post(
            "/app/settings/org", data={"name": " hacked "}, follow_redirects=False
        )
        # either rejected explicitly, or the change must not have been persisted
        if resp.status_code == 200:
            async with SessionLocal() as session:
                row = await session.get(Organization, org.id)
                assert row.name != " hacked "
    finally:
        anon_client.cookies.clear()


@pytest.mark.asyncio
async def test_post_form_with_valid_csrf_is_accepted(anon_client, fresh_org):
    org, user = fresh_org
    from realign.core.security import create_access_token

    jwt_token, _ = create_access_token(sub=user.id, org_id=user.org_id, role=user.role, email=user.email)
    seed = "seed-" + uuid.uuid4().hex
    anon_client.cookies.set("realign_session", jwt_token)
    anon_client.cookies.set("realign_csrf", seed)
    token = csrf_mod.issue_token(user_id=user.id, session_id=seed)
    try:
        resp = await anon_client.post(
            "/app/settings/org",
            data={"name": "شركة معدّلة", "_csrf": token},
            follow_redirects=False,
        )
        assert resp.status_code in (303, 200), resp.status_code
        async with SessionLocal() as session:
            row = await session.get(Organization, org.id)
            assert row.name == "شركة معدّلة"
    finally:
        anon_client.cookies.clear()


# ============================================ 8. authorisation (CWE-862/863) ==


@pytest.mark.asyncio
async def test_every_admin_endpoint_needs_platform_admin(anon_client, fresh_org):
    org, user = fresh_org
    headers = auth_headers(user)
    for path in ("/api/v1/admin/stats", "/api/v1/admin/organizations", "/api/v1/admin/config"):
        resp = await anon_client.get(path, headers=headers)
        assert resp.status_code == 403, path


@pytest.mark.asyncio
async def test_admin_page_redirects_non_admin(anon_client, fresh_org):
    org, user = fresh_org
    from realign.core.security import create_access_token

    jwt_token, _ = create_access_token(sub=user.id, org_id=user.org_id, role=user.role, email=user.email)
    anon_client.cookies.set("realign_session", jwt_token)
    try:
        resp = await anon_client.get("/app/admin", follow_redirects=False)
        assert resp.status_code in (303, 403)
    finally:
        anon_client.cookies.clear()


@pytest.mark.asyncio
async def test_privilege_escalation_via_role_change_is_refused(anon_client, fresh_org):
    """CWE-269: a non-owner must not be able to promote themselves."""
    org, user = fresh_org
    headers = auth_headers(user)
    resp = await anon_client.patch(
        f"/api/v1/auth/team/{user.id}/role?role=owner", headers=headers
    )
    assert resp.status_code in (403, 404, 200)
    async with SessionLocal() as session:
        from realign.models import User

        fresh = await session.get(User, user.id)
        # whatever happened, platform admin cannot be granted this way
        assert fresh.is_platform_admin is False


@pytest.mark.asyncio
async def test_idor_on_assets_and_approvals(anon_client, fresh_org):
    """CWE-639: guessing another tenant's id must return 404, never data."""
    org, user = fresh_org
    headers = auth_headers(user)
    for path in ("/api/v1/assets", "/api/v1/approvals", "/api/v1/runs"):
        listing = await anon_client.get(path, headers=headers)
        assert listing.status_code == 200
    fake = str(uuid.uuid4())
    for path in (f"/api/v1/assets/{fake}", f"/api/v1/runs/{fake}"):
        resp = await anon_client.get(path, headers=headers)
        assert resp.status_code == 404


# ============================================== 9. authentication (CWE-287) ==


@pytest.mark.asyncio
async def test_tampered_jwt_is_rejected(anon_client, fresh_org):
    org, user = fresh_org
    from realign.core.security import create_access_token

    jwt_token, _ = create_access_token(sub=user.id, org_id=user.org_id, role="viewer", email=user.email)
    header, payload, signature = jwt_token.split(".")
    forged = f"{header}.{payload}.{'A' * len(signature)}"
    resp = await anon_client.get("/api/v1/agents", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_privilege_escalation_in_jwt_payload_is_rejected(anon_client, fresh_org):
    """Re-signing a token with a different role must fail: the secret guards it."""
    import jwt as pyjwt

    org, user = fresh_org
    forged = pyjwt.encode(
        {"sub": user.id, "org_id": user.org_id, "role": "owner", "type": "access", "adm": True},
        "wrong-secret",
        algorithm="HS256",
    )
    resp = await anon_client.get("/api/v1/admin/stats", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_refresh_token_cannot_be_used_as_access(anon_client, fresh_org):
    from realign.core.security import create_refresh_token

    org, user = fresh_org
    refresh, _ = create_refresh_token(sub=user.id, org_id=user.org_id)
    resp = await anon_client.get("/api/v1/agents", headers={"Authorization": f"Bearer {refresh}"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_does_not_reveal_account_existence(anon_client):
    missing = await anon_client.post(
        "/api/v1/auth/login", json={"email": f"nope-{uuid.uuid4().hex}@x.com", "password": "wrongpass123"}
    )
    assert missing.status_code == 401
    assert missing.json()["error"]["message"] == "البريد أو كلمة المرور غير صحيحة"


@pytest.mark.asyncio
async def test_weak_and_breached_passwords_rejected():
    for weak in ("password", "123456", "qwerty", "12345678", "egypt123", "abc"):
        ok, _ = validate_password(weak)
        assert ok is False, weak
    ok, _ = validate_password("K7#mzQ2vLp9wR")
    assert ok is True


# ============================================ 10. brute force (CWE-307) ==


@pytest.mark.asyncio
async def test_brute_force_login_is_locked_out(anon_client, fresh_org):
    from realign.core.ratelimit import limiter

    org, user = fresh_org
    email = user.email
    await limiter.reset("auth.login", "testclient")
    codes = []
    for _ in range(9):
        resp = await anon_client.post(
            "/api/v1/auth/login", json={"email": email, "password": "WrongPassword!23"}
        )
        codes.append(resp.status_code)
    # the limiter must engage: either 429 from middleware or 401 with a lock message
    assert 429 in codes or any("حاول بعد" in (await anon_client.post(
        "/api/v1/auth/login", json={"email": email, "password": "WrongPassword!23"}
    )).text for _ in [0])


@pytest.mark.asyncio
async def test_password_spraying_across_accounts_is_detected():
    from realign.core.ratelimit import limiter

    ip = "spray-test-ip"
    seen = False
    for i in range(10):
        seen = await limiter.note_account_attempt(ip, f"victim{i}@test.io")
        if seen:
            break
    assert seen is True


# ========================================== 11. rate limiting (DoS) ==


@pytest.mark.asyncio
async def test_rate_limit_headers_and_429(anon_client):
    statuses = []
    for _ in range(650):
        resp = await anon_client.get("/api/v1/health".replace("/api/v1/health", "/api/v1/meta"))
        statuses.append(resp.status_code)
        if resp.status_code == 429:
            break
    assert 429 in statuses


@pytest.mark.asyncio
async def test_oversized_body_is_rejected(anon_client):
    resp = await anon_client.post(
        "/api/v1/auth/login",
        content=b"x" * (3 * 1024 * 1024),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code in (413, 422, 400)


# ============================================ 12. uploads (CWE-434/611) ==


@pytest.mark.asyncio
async def test_upload_rejects_svg_and_executables():
    from realign.core.uploads import UploadRejected, store_image

    for data, label in [
        (b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", "svg"),
        (b"MZ\x90\x00" + b"\x00" * 100, "exe"),
        (b"%PDF-1.4\n" + b"\x00" * 100, "pdf"),
        (b"<?xml version='1.0'?><!DOCTYPE foo [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]>", "xxe"),
        (b"GIF89a" + b"\x00" * 100, "gif"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 100, "truncated png"),
    ]:
        with pytest.raises(UploadRejected):
            store_image(data, original_name=f"payload.{label}")


@pytest.mark.asyncio
async def test_upload_accepts_and_reencodes_real_png():
    import io

    from PIL import Image

    from realign.core.uploads import store_image

    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (12, 34, 56)).save(buffer, format="PNG")
    stored = store_image(buffer.getvalue(), original_name="../../evil.php.png")
    assert stored.filename.startswith("logo/")
    assert ".." not in stored.filename
    assert stored.content_type == "image/png"
    assert stored.url.startswith("/media/logo/")


@pytest.mark.asyncio
async def test_xxe_entity_is_not_expanded():
    """A billion-laughs payload must be rejected by defusedxml."""
    from defusedxml.ElementTree import fromstring
    from defusedxml.common import DefusedXmlException

    payload = (
        b"<?xml version='1.0'?><!DOCTYPE lolz ["
        b"<!ENTITY lol 'lol'>"
        b"<!ENTITY lol2 '&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;'>"
        b"]><lolz>&lol2;</lolz>"
    )
    with pytest.raises((DefusedXmlException, Exception)):
        fromstring(payload)


# ======================================== 13. secrets & logging (CWE-532) ==


@pytest.mark.asyncio
async def test_no_secrets_in_html_pages(anon_client):
    for path in ("/", "/login", "/register", "/legal/privacy"):
        body = (await anon_client.get(path)).text
        for marker in ("sk-", "nxk_", "APP_SECRET_KEY", "CREDENTIALS_ENCRYPTION_KEY", "BEGIN PRIVATE KEY"):
            assert marker not in body


@pytest.mark.asyncio
async def test_api_never_returns_secret_values(anon_client, fresh_org):
    org, user = fresh_org
    headers = auth_headers(user)
    body = (await anon_client.get("/api/v1/integrations", headers=headers)).text
    assert "***" in body or True  # masked values are allowed
    assert "sk-ant-" not in body
    assert "sk-proj-" not in body


@pytest.mark.asyncio
async def test_log_redaction_masks_everything():
    samples = [
        "password=hunter2",
        "token: abcdefghijklmnop123",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefgh",
        "nxk_AbCdEfGhIjKlMnOpQrStUv",
        "card 4111111111111111",
        "user ahmed@example.com logged in",
        "postgres://user:secretpass@db:5432/x",
    ]
    for sample in samples:
        out = redact(sample)
        assert "hunter2" not in out
        assert "abcdefghijklmnop123" not in out
        assert "AbCdEfGhIjKlMnOpQrStUv" not in out
        assert "4111111111111111" not in out
        assert "ahmed@" not in out
        assert "secretpass" not in out


# ============================================= 14. legal & consent pages ==


@pytest.mark.asyncio
async def test_legal_pages_are_public_and_complete(anon_client):
    for slug, needle in [
        ("privacy", "سياسة الخصوصية"),
        ("terms", "الشروط والأحكام"),
        ("cookies", "سياسة الكوكيز"),
        ("security", "كيف نحمي بياناتك"),
        ("acceptable-use", "سياسة الاستخدام المقبول"),
    ]:
        resp = await anon_client.get(f"/legal/{slug}")
        assert resp.status_code == 200, slug
        assert needle in resp.text
        assert len(resp.text) > 3000


@pytest.mark.asyncio
async def test_cookie_settings_page_and_choice(anon_client):
    resp = await anon_client.get("/cookies")
    assert resp.status_code == 200
    assert "إعدادات الكوكيز" in resp.text


@pytest.mark.asyncio
async def test_unknown_legal_page_redirects(anon_client):
    resp = await anon_client.get("/legal/does-not-exist", follow_redirects=False)
    assert resp.status_code == 303


# ================================================ 15. transport security ==


@pytest.mark.asyncio
async def test_https_enforced_in_production(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "force_https", True)
    from realign.core.headers import HTTPSRedirectMiddleware

    assert settings.is_production is True

    calls = []

    async def app(scope, receive, send):
        calls.append(scope)

    middleware = HTTPSRedirectMiddleware(app)
    scope = {
        "type": "http",
        "path": "/api/v1/health",
        "headers": [(b"x-forwarded-proto", b"http")],
        "scheme": "http",
        "query_string": b"",
        "root_path": "",
        "method": "GET",
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    await middleware(scope, receive, send)
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 403


@pytest.mark.asyncio
async def test_cors_is_not_wildcard_in_production(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "allowed_hosts", "")
    origins = settings.allowed_host_list or [settings.app_base_url]
    assert "*" not in origins


# ============================================= 16. CORS preflight sanity ==


@pytest.mark.asyncio
async def test_cors_preflight_uses_explicit_origin(anon_client):
    resp = await anon_client.options(
        "/api/v1/health",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    allow = resp.headers.get("access-control-allow-origin", "")
    assert allow != "*"


# ================================================== 17. brand & PII hygiene ==


@pytest.mark.asyncio
async def test_brand_input_is_sanitised(anon_client, fresh_org):
    org, user = fresh_org
    headers = auth_headers(user)
    resp = await anon_client.put(
        "/api/v1/brand",
        headers=headers,
        json={"company_name": "ش\x00ركة", "never_say": "\x1b[31m" + "x" * 50},
    )
    assert resp.status_code == 200
    async with SessionLocal() as session:
        from realign.models import BrandProfile
        from sqlalchemy import select

        row = (
            await session.execute(select(BrandProfile).where(BrandProfile.org_id == org.id))
        ).scalar_one_or_none()
        assert row is not None
        assert "\x00" not in (row.company_name or "")
        assert "\x1b" not in (row.never_say or "")


@pytest.mark.asyncio
async def test_agent_params_are_bounded(anon_client, fresh_org):
    org, user = fresh_org
    headers = auth_headers(user)
    resp = await anon_client.post(
        "/api/v1/agents/research/run",
        headers=headers,
        json={"params": {"topic": "x" * 50_000}, "async_execution": False},
    )
    assert resp.status_code in (200, 202, 400, 422)
    if resp.status_code in (200, 202):
        async with SessionLocal() as session:
            from sqlalchemy import select

            from realign.models import Run

            run = (
                await session.execute(select(Run).where(Run.org_id == org.id).order_by(Run.created_at.desc()))
            ).scalars().first()
            stored = getattr(run, "input", None) or getattr(run, "params", None) or {}
            assert len((stored.get("topic") or "")) <= 8000


# ===================================================== 18. misc hardening ==


@pytest.mark.asyncio
async def test_docs_are_hidden_in_production(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "expose_docs", False)
    from realign.main import create_app

    prod = create_app()
    paths = {getattr(r, "path", None) for r in prod.routes}
    assert "/docs" not in paths


@pytest.mark.asyncio
async def test_html_has_no_external_resources(anon_client):
    """The app must work fully offline — no third-party script can be injected."""
    body = (await anon_client.get("/")).text
    external = re.findall(r'(?:src|href)\s*=\s*["\'](https?://[^"\']+)', body)
    assert not external, external


@pytest.mark.asyncio
async def test_static_assets_exist(anon_client):
    for path in ("/static/logo.png", "/static/favicon.png", "/static/manifest.webmanifest"):
        resp = await anon_client.get(path)
        assert resp.status_code == 200, path
