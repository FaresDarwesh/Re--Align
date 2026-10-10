"""Tests for the new interface layer: bilingual strings, theme, locale routing
and the hardened CSRF verifier.

These live beside the security suite because a broken translation or a CSRF
check that silently stops working is a defect, not a cosmetic issue.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from realign.core import csrf as csrf_mod
from realign.main import app as realign_app
from realign.db import SessionLocal
from realign.models import Organization, User
from realign.services.onboarding import provision_org, seed_plans
from realign.web import i18n
from realign.web.routes import crumbs_for

def _make_org(name: str, email: str, plan: str = "starter"):
    from conftest import _make_org as _factory

    return _factory(name, email, plan)


@pytest.fixture
def app():
    return realign_app


# =========================================================== i18n catalogue ==


def test_every_key_exists_in_both_languages():
    gaps = i18n.missing_keys()
    assert not gaps["only_ar"], f"missing English: {gaps['only_ar']}"
    assert not gaps["only_en"], f"missing Arabic: {gaps['only_en']}"


def test_no_key_renders_as_itself():
    """A missing key would be printed verbatim into the page."""
    for key in set(i18n.AR) | set(i18n.EN):
        assert i18n.translate("ar", key) != key
        assert i18n.translate("en", key) != key


def test_translate_formats_and_falls_back_safely():
    assert i18n.translate("ar", "dash.used_of", used="5", total="10") == "5 من 10 مستخدم"
    assert i18n.translate("en", "dash.used_of", used="5", total="10") == "5 of 10 used"
    # Unknown keys degrade to the key instead of raising.
    assert i18n.translate("ar", "does.not.exist") == "does.not.exist"
    # Unknown languages fall back instead of crashing.
    assert i18n.translate("klingon", "nav.agents") == i18n.AR["nav.agents"]
    # A bad format string must never blow up a template.
    assert i18n.translate("ar", "nav.agents", unexpected=1) == i18n.AR["nav.agents"]


def test_dir_and_normalisation():
    assert i18n.dir_for("ar") == "rtl"
    assert i18n.dir_for("en") == "ltr"
    assert i18n.normalise("EN-US") == "en"
    assert i18n.normalise("ar_EG") == "ar"
    assert i18n.normalise(None) == i18n.DEFAULT_LANG
    assert i18n.normalise("zz") == i18n.DEFAULT_LANG


def test_every_t_key_used_in_templates_is_defined():
    """Walk the templates and assert each `t('…')` key exists in both tables."""
    tpl_dir = Path(__file__).resolve().parent.parent / "realign" / "web" / "templates"
    known = set(i18n.AR) | set(i18n.EN)
    used: set[str] = set()
    for path in tpl_dir.glob("*.html"):
        text = path.read_text(encoding="utf-8")
        # Literal keys only: `t('a.b')`. Keys assembled at runtime with `~`
        # or `+` are expanded explicitly below so they are still checked.
        for m in re.finditer(r"""(?<![\w.])t\(\s*['"]([a-z0-9_.]+)['"]\s*\)""", text):
            used.add(m.group(1))
        for fam, count, suffixes in (
            ("land.ben", 6, ("t", "d")),
            ("land.hiw", 3, ("t", "d")),
            ("land.sec", 8, ("t", "d")),
            ("land.faq", 6, ("q", "a")),
            ("land.sc", 5, ("tab", "title", "desc", "f1", "f2", "f3", "pv")),
        ):
            if f"t('{fam}' ~" in text or f"('{fam}1'" in text:
                for i in range(1, count + 1):
                    for suffix in suffixes:
                        used.add(f"{fam}{i}_{suffix}")
        # dynamic keys: t('plan.' + p.code + '.bN') for the four fixed plans
        if "plan.' + p.code" in text:
            for code in ("starter", "growth", "business", "enterprise"):
                for suffix in ("desc", "b1", "b2", "b3"):
                    used.add(f"plan.{code}.{suffix}")
    # `t('plan.' + p.code + …)` is captured as the bare prefix "plan."
    used.discard("plan.")
    missing = sorted(k for k in used if k not in known)
    assert not missing, f"templates use undefined keys: {missing}"
    assert len(used) > 80, f"only {len(used)} keys detected — did the walk break?"


def test_templates_do_not_hardcode_arabic_sentences():
    """Every user-facing string must go through `t()` so English works."""
    tpl_dir = Path(__file__).resolve().parent.parent / "realign" / "web" / "templates"
    offenders: list[str] = []
    for path in tpl_dir.glob("*.html"):
        if path.name in {"legal.html", "cookies.html"}:
            continue  # long-form policy documents are authored per language
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith(("{#", "{%-", "{%", "<!")):
                continue
            # Arabic words outside of a {{ … }} expression on a text line.
            visible = re.sub(r"\{\{.*?\}\}", "", stripped)
            if re.search(r"[\u0600-\u06FF]{4,}", visible) and "العربية" not in visible:
                offenders.append(f"{path.name}:{lineno}: {stripped[:70]}")
    assert not offenders, "hard-coded Arabic found:\n" + "\n".join(offenders)


# ============================================================ CSRF (CWE-352) ==


class _Url:
    scheme = "https"
    netloc = "app.example.com"
    path = "/app/settings/org"
    host = "app.example.com"


def _req(headers=None, cookies=None):
    return type(
        "Req",
        (),
        {"headers": headers or {}, "cookies": cookies or {}, "url": _Url()},
    )()


def test_cross_site_request_without_origin_is_refused():
    """No Origin, and Sec-Fetch-Site says cross-site: nothing may pass."""
    request = _req({"sec-fetch-site": "cross-site"}, {"realign_session": "a-token"})
    assert csrf_mod.same_origin(request) is False
    assert not csrf_mod.verify_request(request, token=csrf_mod.issue_token(user_id="u", session_id="s"))


def test_same_origin_request_is_accepted():
    request = _req({"origin": "https://app.example.com", "sec-fetch-site": "same-origin"})
    assert csrf_mod.same_origin(request) is True
    # Even with no token at all: the browser proved it is first-party.
    assert csrf_mod.verify_request(request, token="")


def test_origin_mismatch_is_refused():
    request = _req({"origin": "https://evil.example.com"}, {"realign_session": "x"})
    assert csrf_mod.same_origin(request) is False
    assert not csrf_mod.verify_request(request, token="whatever")


def test_non_browser_client_without_session_is_allowed():
    """curl / health checks send no Fetch-Metadata and hold no session."""

    class _LocalUrl:
        scheme = "http"
        netloc = "127.0.0.1:8000"
        host = "127.0.0.1"

    request = type("Req", (), {"headers": {}, "cookies": {}, "url": _LocalUrl()})()
    assert csrf_mod.same_origin(request) is None
    assert csrf_mod.verify_request(request, token="")


def test_token_still_bound_to_user_and_session():
    token = csrf_mod.issue_token(user_id="u1", session_id="s1")
    assert csrf_mod.verify_token(token, user_id="u1", session_id="s1")
    assert not csrf_mod.verify_token(token, user_id="u2", session_id="s1")
    assert not csrf_mod.verify_token(token, user_id="u1", session_id="s2")
    assert not csrf_mod.verify_token("", user_id="u1", session_id="s1")


# ================================================================ breadcrumbs ==


def test_crumbs_follow_the_path():
    class _RunsUrl:
        path = "/app/runs/abc"

    class _Req:
        url = _RunsUrl()
        cookies = {}
        state = type("S", (), {"lang": "ar"})()
        headers = {}

    crumbs = crumbs_for(_Req(), "ar", leaf="research")
    assert [c["url"] for c in crumbs] == ["/app", "/app/runs", "/app/runs/abc"]
    assert crumbs[-1]["label"] == "research"

    crumbs = crumbs_for(_Req(), "ar")
    assert [c["url"] for c in crumbs] == ["/app", "/app/runs"]


# ============================================================= live HTTP pass ==


@pytest_asyncio.fixture
async def fresh_org() -> tuple[Organization, User]:
    return await _make_org("شركة الواجهة", "ui@alpha-test.io")


@pytest.mark.asyncio
async def test_language_switch_sets_cookie_and_flips_direction(app, fresh_org):
    org, user = fresh_org
    from realign.core.security import create_access_token

    token, _ = create_access_token(sub=user.id, org_id=user.org_id, role=user.role, email=user.email)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.cookies.set("realign_session", token)
        arabic = await ac.get("/app")
        assert arabic.status_code == 200
        assert 'dir="rtl"' in arabic.text

        switch = await ac.get("/lang/en", follow_redirects=False)
        assert switch.status_code in (302, 303)
        assert "realign_lang=en" in switch.headers.get("set-cookie", "")

        ac.cookies.set("realign_lang", "en")
        english = await ac.get("/app")
        assert english.status_code == 200
        assert 'dir="ltr"' in english.text
        assert 'lang="en"' in english.text
        # The two renderings must actually differ.
        assert english.text != arabic.text


@pytest.mark.asyncio
async def test_every_page_renders_in_both_languages(app, fresh_org):
    org, user = fresh_org
    from realign.core.security import create_access_token

    token, _ = create_access_token(sub=user.id, org_id=user.org_id, role=user.role, email=user.email)
    paths = ["/app", "/app/agents", "/app/approvals", "/app/assets", "/app/runs",
             "/app/integrations", "/app/brand", "/app/billing", "/app/settings"]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.cookies.set("realign_session", token)
        for lang, direction in (("ar", "rtl"), ("en", "ltr")):
            ac.cookies.set("realign_lang", lang)
            for path in paths:
                resp = await ac.get(path)
                assert resp.status_code == 200, f"{lang} {path} -> {resp.status_code}"
                assert f'dir="{direction}"' in resp.text, f"{lang} {path} wrong direction"
                assert "{{" not in resp.text, f"{lang} {path} leaked a Jinja tag"


@pytest.mark.asyncio
async def test_public_pages_render_in_both_languages(app):
    paths = ["/", "/login", "/register", "/cookies", "/legal/privacy", "/legal/dpa"]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        for lang, direction in (("ar", "rtl"), ("en", "ltr")):
            ac.cookies.set("realign_lang", lang)
            for path in paths:
                resp = await ac.get(path)
                assert resp.status_code == 200, f"{lang} {path} -> {resp.status_code}"
                assert f'dir="{direction}"' in resp.text
                body = resp.text
                assert "Traceback" not in body
                # no untranslated key may reach the browser
                for key in ("nav.dashboard", "auth.login_title", "land.hero_title"):
                    assert key not in body, f"{lang} {path} leaked key {key}"


@pytest.mark.asyncio
async def test_brand_assets_are_served(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        for asset in ("/static/logo.svg", "/static/favicon.svg", "/static/og-image.png",
                      "/static/app.css", "/static/marketing.css", "/static/app.js",
                      "/static/fonts/inter-400.woff2", "/static/fonts/cairo-700.woff2",
                      "/static/manifest.webmanifest"):
            resp = await ac.get(asset)
            assert resp.status_code == 200, f"{asset} -> {resp.status_code}"
            assert len(resp.content) > 100, f"{asset} looks empty"


@pytest.mark.asyncio
async def test_landing_page_mentions_the_product_and_no_old_name(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        body = (await ac.get("/")).text
        assert "re-align" in body
        for stale in ("nexus", "NEXUS"):
            assert stale not in body, f"the old product name still leaks: {stale}"


# ======================================================= forgot / reset flow ==


def _reset_columns_present(sync_conn) -> bool:
    from sqlalchemy import inspect as sa_inspect

    cols = {c["name"] for c in sa_inspect(sync_conn).get_columns("users")}
    return {"password_reset_token_hash", "password_reset_expires_at"} <= cols


@pytest.mark.asyncio
async def test_password_reset_columns_were_migrated(app):
    """`create_all` never alters a table; the idempotent migration must have."""
    from realign.db import engine, init_db

    await init_db()
    async with engine.connect() as conn:
        assert await conn.run_sync(_reset_columns_present)


@pytest.mark.asyncio
async def test_forgot_password_page_renders(app):
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        for lang in ("ar", "en"):
            ac.cookies.set("realign_lang", lang)
            resp = await ac.get("/forgot-password")
            assert resp.status_code == 200
            assert "csrf" in resp.text
            assert "{{" not in resp.text
        resp = await ac.get("/reset-password")
        assert resp.status_code == 200
        assert "invalid" in resp.text or "auth.reset_bad_token" not in resp.text


@pytest.mark.asyncio
async def test_forgot_password_does_not_leak_account_existence(app, fresh_org):
    """The same answer for a real address and a stranger's (CWE-204)."""
    from httpx import ASGITransport, AsyncClient

    org, user = fresh_org
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        token = re.search(r'name="_csrf" value="([^"]+)"', (await ac.get("/forgot-password")).text)
        assert token
        known = await ac.post(
            "/forgot-password", data={"_csrf": token.group(1), "email": user.email}
        )
        assert known.status_code in (302, 303)
        token = re.search(r'name="_csrf" value="([^"]+)"', (await ac.get("/forgot-password")).text)
        unknown = await ac.post(
            "/forgot-password",
            data={"_csrf": token.group(1), "email": "stranger-xyz@example-abc.com"},
        )
        assert unknown.status_code in (302, 303)
        assert known.headers.get("location") == unknown.headers.get("location")


@pytest.mark.asyncio
async def test_reset_token_is_single_use_and_hashed(app, fresh_org):
    from sqlalchemy import select

    from realign.db import SessionLocal
    from realign.models import User

    org, user = fresh_org
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        page = await ac.get("/forgot-password")
        csrf_token = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
        await ac.post("/forgot-password", data={"_csrf": csrf_token, "email": user.email})

    # The token is written to the audit log as a link when SMTP is not set up.
    from realign.models.system import AuditLog

    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.action == "auth.password_reset_link_issued")
                .order_by(AuditLog.created_at.desc())
            )
        ).scalars().first()
        assert row is not None, "the reset link must be recorded"
        link = (row.meta or {}).get("link", "")
        raw = link.split("token=")[-1]
        stored = (
            await session.execute(select(User).where(User.id == user.id))
        ).scalar_one()
        assert stored.password_reset_token_hash
        assert raw not in stored.password_reset_token_hash, "token must be hashed at rest"

        # First use succeeds …
        page2 = await AsyncClient(transport=transport, base_url="http://test").__aenter__()
        try:
            p = await page2.get(f"/reset-password?token={raw}")
            csrf2 = re.search(r'name="_csrf" value="([^"]+)"', p.text).group(1)
            r = await page2.post(
                "/reset-password",
                data={"_csrf": csrf2, "token": raw, "password": "N3w-Password-2026"},
            )
            assert r.status_code in (302, 303)
            assert "reset=1" in (r.headers.get("location") or "")
            # … the second must not.
            p = await page2.get("/reset-password?token=" + raw)
            csrf3 = re.search(r'name="_csrf" value="([^"]+)"', p.text).group(1)
            r2 = await page2.post(
                "/reset-password",
                data={"_csrf": csrf3, "token": raw, "password": "An0ther-Password-2026"},
            )
            assert r2.status_code in (302, 303)
            assert "error=token" in (r2.headers.get("location") or "")
        finally:
            await page2.__aexit__(None, None, None)
