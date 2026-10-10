"""End-to-end smoke test: log in, walk every page in both languages, exercise
every form, and fail loudly on anything that is not a clean 2xx/3xx.

Run with the server already listening:   python scripts/smoke.py [base_url]
"""

from __future__ import annotations

import re
import sys
import uuid
from urllib.parse import urljoin

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
ADMIN = ("admin@re-align.app", "Re-Align#Admin2026")
DEMO = ("demo@re-align.app", "Re-Align#Demo2026")

PAGES = [
    "/app", "/app/agents", "/app/approvals", "/app/assets", "/app/runs",
    "/app/integrations", "/app/brand", "/app/billing", "/app/settings", "/app/admin",
    "/app/assets?status=draft", "/app/assets?status=published",
]
PUBLIC = [
    "/", "/login", "/register", "/cookies",
    "/forgot-password", "/reset-password",
    "/legal/privacy", "/legal/terms", "/legal/cookies", "/legal/security",
    "/legal/acceptable-use", "/legal/dpa",
]
ASSETS = [
    "/static/app.css", "/static/marketing.css", "/static/app.js",
    "/static/logo.svg", "/static/favicon.svg", "/static/favicon.png",
    "/static/apple-touch-icon.png", "/static/og-image.png", "/static/logo.png",
    "/static/manifest.webmanifest", "/static/fonts/inter-400.woff2",
    "/static/fonts/cairo-600.woff2", "/static/fonts/sora-700.woff2",
]

# Anything that looks like an untranslated key or a Jinja failure.
sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
from realign.web import i18n  # noqa: E402

_KEYS = sorted(set(i18n.AR) | set(i18n.EN), key=len, reverse=True)
_KEY_RE = re.compile(r"(?<![\w./:-])(" + "|".join(re.escape(k) for k in _KEYS) + r")(?![\w-])")
BAD_PATTERNS = [
    _KEY_RE,
    re.compile(r"jinja2\.exceptions|UndefinedError|TemplateSyntaxError|TemplateAssertionError"),
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"&(?:#34|quot;)?\{\{|\{\%"),
]

problems: list[str] = []
checks = 0


def note(kind: str, url: str, detail: str) -> None:
    problems.append(f"[{kind}] {url} — {detail}")
    print(f"  ✗ {kind}: {url} — {detail}")


def scan(url: str, resp: httpx.Response) -> None:
    global checks
    checks += 1
    if resp.status_code >= 400:
        note("status", url, str(resp.status_code))
        return
    # Binary assets (fonts, PNGs) are byte noise to these patterns.
    if not (resp.headers.get("content-type", "").startswith("text/")
            or "html" in resp.headers.get("content-type", "")):
        return
    body = resp.text
    for pattern in BAD_PATTERNS:
        m = pattern.search(body)
        if m:
            note("content", url, f"matched {m.group(0)[:80]!r}")
            return


def expect(condition: bool, kind: str, url: str, detail: str = "") -> None:
    """Assert-style check that records a problem instead of raising."""
    global checks
    checks += 1
    if not condition:
        note(kind, url, detail or "unexpected result")
    else:
        print(f"\u2713 {url}")


def login(client: httpx.Client, creds: tuple[str, str]) -> bool:
    email, password = creds
    client.get(f"{BASE}/login")
    token = ""
    page = client.get(f"{BASE}/login").text
    m = re.search(r'name="_csrf" value="([^"]+)"', page)
    if m:
        token = m.group(1)
    with httpx.Client(follow_redirects=False, timeout=30) as short:
        short.cookies.update(client.cookies)
        resp = short.post(
            f"{BASE}/login",
            data={"_csrf": token, "email": email, "password": password},
            headers={"Origin": BASE},
        )
    client.cookies.update(resp.cookies)
    ok = resp.status_code in (302, 303) and resp.headers.get("location", "").endswith("/app")
    print(f"{'✓' if ok else '✗'} login {email} -> {resp.status_code} {resp.headers.get('location','')}")
    if not ok:
        problems.append(f"[login] {email} -> {resp.status_code} {resp.headers.get('location','')}")
    return ok


def csrf(client: httpx.Client, path: str) -> str:
    page = client.get(urljoin(BASE, path)).text
    m = re.search(r'name="_csrf" value="([^"]+)"', page)
    return m.group(1) if m else ""


def main() -> int:
    print(f"── base: {BASE}\n── static assets")
    anon = httpx.Client(follow_redirects=True, timeout=30)
    for a in ASSETS:
        r = anon.get(urljoin(BASE, a))
        scan(a, r)
    print(f"   checked {len(ASSETS)} assets")

    print("── public pages (anonymous)")
    for p in PUBLIC:
        r = anon.get(urljoin(BASE, p))
        scan(p, r)
        if r.status_code == 200 and p == "/":
            if "re-align" not in r.text:
                note("content", p, "brand name missing from the landing page")

    for creds in (ADMIN, DEMO):
        print(f"── authenticated walk: {creds[0]}")
        with httpx.Client(follow_redirects=True, timeout=30) as client:
            if not login(client, creds):
                continue
            for p in PAGES:
                r = client.get(urljoin(BASE, p))
                scan(p, r)
            # detail pages
            dash = client.get(f"{BASE}/app").text
            for m in re.finditer(r'href="/app/(?:assets|runs)/([0-9a-f-]{8,})"', dash):
                pass
            ids = re.findall(r'href="/app/runs/([0-9a-f\-]{36})"', client.get(f"{BASE}/app/runs").text)
            for rid in ids[:3]:
                scan(f"/app/runs/{rid}", client.get(f"{BASE}/app/runs/{rid}"))
            aids = re.findall(r'href="/app/assets/([0-9a-f\-]{36})"', client.get(f"{BASE}/app/assets").text)
            for aid in aids[:3]:
                scan(f"/app/assets/{aid}", client.get(f"{BASE}/app/assets/{aid}"))

            # ---- language switch
            r = client.get(f"{BASE}/lang/en", follow_redirects=False)
            if r.status_code not in (302, 303) or "realign_lang=en" not in r.headers.get("set-cookie", ""):
                note("i18n", "/lang/en", f"{r.status_code} {r.headers.get('set-cookie','')}")
            print(f"{'✓' if not problems else '·'} /lang/en -> {r.status_code}")
            client.cookies.set("realign_lang", "en", domain="127.0.0.1")
            for p in ("/app", "/app/agents", "/app/settings", "/app/billing", "/", "/login"):
                r = client.get(urljoin(BASE, p))
                scan(f"{p} [en]", r)
                if r.status_code == 200 and 'dir="ltr"' not in r.text:
                    note("i18n", f"{p} [en]", "dir is not ltr")
            client.cookies.set("realign_lang", "ar", domain="127.0.0.1")
            r = client.get(urljoin(BASE, "/app"))
            if 'dir="rtl"' not in r.text:
                note("i18n", "/app [ar]", "dir is not rtl")

    # ------------------------------------------------------------- forms ---
    print("── forms (admin)")
    with httpx.Client(follow_redirects=False, timeout=60) as client:
        login(client, ADMIN)

        # run an agent
        r = client.post(f"{BASE}/app/agents/research/run",
                        data={"_csrf": csrf(client, "/app/agents"), "topic": "smoke test",
                              "channel": "instagram"}, headers={"Origin": BASE})
        if r.status_code not in (302, 303):
            note("form", "/app/agents/research/run", str(r.status_code))
        else:
            print(f"✓ agent run -> {r.status_code}")

        # approve / reject the first pending approval
        page = client.get(f"{BASE}/app/approvals").text
        m = re.search(r'action="/app/approvals/([0-9a-f\-]{36})/decide"', page)
        if m:
            aid = m.group(1)
            r = client.post(f"{BASE}/app/approvals/{aid}/decide",
                            data={"_csrf": csrf(client, "/app/approvals"), "decision": "approve"},
                            headers={"Origin": BASE})
            if r.status_code not in (302, 303):
                note("form", f"/app/approvals/{aid}/decide", str(r.status_code))
            else:
                print(f"✓ approval decide -> {r.status_code}")

        # brand file
        r = client.post(f"{BASE}/app/brand",
                        data={"_csrf": csrf(client, "/app/brand"), "company_name": "رايزن smoketest",
                              "industry": "برمجيات", "audience": "", "offer": "", "voice": "",
                              "rules": "", "never_say": "", "keywords": "", "tone_keywords": "",
                              "hashtags": "", "competitors": "", "locales": "ar", "sample_posts": ""},
                        headers={"Origin": BASE})
        if r.status_code not in (302, 303):
            note("form", "/app/brand", str(r.status_code))
        else:
            print(f"✓ brand save -> {r.status_code}")

        # settings
        r = client.post(f"{BASE}/app/settings/org",
                        data={"_csrf": csrf(client, "/app/settings"), "name": "شركة رايزن للتقنية",
                              "monthly_budget_cents": "50000", "per_run_budget_cents": "200",
                              "daily_run_limit": "500"}, headers={"Origin": BASE})
        if r.status_code not in (302, 303):
            note("form", "/app/settings/org", str(r.status_code))
        else:
            print(f"✓ settings save -> {r.status_code}")

        # LLM settings
        r = client.post(f"{BASE}/app/settings/llm",
                        data={"_csrf": csrf(client, "/app/settings"), "provider": "mock",
                              "base_url": "", "api_key": "", "thinking_model": "", "decision_model": ""},
                        headers={"Origin": BASE})
        if r.status_code not in (302, 303):
            note("form", "/app/settings/llm", str(r.status_code))
        else:
            print(f"✓ llm save -> {r.status_code}")

        # integrations
        r = client.post(f"{BASE}/app/integrations/webhook",
                        data={"_csrf": csrf(client, "/app/integrations"), "cred_url": "https://example.com/hook"},
                        headers={"Origin": BASE})
        if r.status_code not in (302, 303):
            note("form", "/app/integrations/webhook", str(r.status_code))
        else:
            print(f"✓ integration save -> {r.status_code}")

        # billing: switch plan and back
        r = client.post(f"{BASE}/app/billing/subscribe",
                        data={"_csrf": csrf(client, "/app/billing"), "plan_code": "business"},
                        headers={"Origin": BASE})
        if r.status_code not in (302, 303):
            note("form", "/app/billing/subscribe", str(r.status_code))
        else:
            print(f"✓ subscribe -> {r.status_code}")
        client.post(f"{BASE}/app/billing/subscribe",
                    data={"_csrf": csrf(client, "/app/billing"), "plan_code": "growth"},
                    headers={"Origin": BASE})

        # cookie preferences
        r = client.post(f"{BASE}/cookies", data={"_csrf": csrf(client, "/cookies"), "choice": "accept"},
                        headers={"Origin": BASE})
        if r.status_code not in (302, 303):
            note("form", "/cookies", str(r.status_code))

        # registration of a brand-new workspace
        with httpx.Client(follow_redirects=False, timeout=60) as fresh:
            tok = csrf(fresh, "/register")
            r = fresh.post(f"{BASE}/register",
                           data={"_csrf": tok, "organization": "شركة الاختبار الآلي",
                                 "name": "اختبار",
                                 "email": f"smoke-{uuid.uuid4().hex[:10]}@example.com",
                                 "password": "SmokeTest#2026x"}, headers={"Origin": BASE})
            if r.status_code not in (302, 303):
                note("form", "/register", str(r.status_code))
            elif "ratelimit" in (r.headers.get("location") or ""):
                print(f"~ register -> {r.headers.get('location')} (rate limited, skipped)")
                return 0 if not problems else 1
            else:
                print(f"✓ register -> {r.status_code}")
            fresh.get(f"{BASE}/app")
            for p in ("/app", "/app/agents", "/app/billing"):
                scan(f"new tenant {p}", fresh.get(urljoin(BASE, p)))

        # logout
        r = client.get(f"{BASE}/logout", follow_redirects=False)
        if r.status_code not in (302, 303):
            note("form", "/logout", str(r.status_code))

    with httpx.Client(follow_redirects=True, timeout=60) as anon:
        print("\n── forgot / reset password")
        # Unknown addresses must produce the exact same answer as known ones.
        r = anon.post(f"{BASE}/forgot-password",
                        data={"_csrf": csrf(anon, "/forgot-password"),
                              "email": "stranger-xyz@example-abc.com"},
                        headers={"Origin": BASE})
        # The client follows redirects, so check where we landed: a stranger's
        # address must land exactly where a real one would.
        expect(r.status_code == 200 and "sent=1" in str(r.url),
               "form", "/forgot-password (unknown address)",
               f"{r.status_code} {r.url}")
        # A garbage token must not reach the database or render a form.
        r = anon.get(f"{BASE}/reset-password?token=short")
        body = r.text if r.status_code == 200 else ""
        expect(r.status_code == 200 and 'name="password"' not in body,
               "status", "/reset-password (bad token shows no form)", str(r.status_code))

        print("\n── language switch keeps the current page")
        for path in ("/app/runs", "/legal/dpa"):
            r = anon.get(f"{BASE}/lang/en", params={"next": path}, follow_redirects=False)
            loc = r.headers.get("location") or ""
            expect(r.status_code == 303 and loc.rstrip("/").endswith(path),
                   "lang", f"/lang/en?next={path}", f"{r.status_code} -> {loc}")

    # ------------------------------------------------------------- CSRF ----
    print("── CSRF must still block cross-site posts")
    with httpx.Client(follow_redirects=False, timeout=30) as evil:
        login(evil, ADMIN)
        r = evil.post(f"{BASE}/app/settings/org",
                      data={"name": "hacked"},
                      headers={"Origin": "https://evil.example.com", "Sec-Fetch-Site": "cross-site"})
        # Either the browser redirect (with a csrf hint) or a hard 403 is a block.
        blocked = r.status_code == 403 or (
            r.status_code in (302, 303) and "csrf" in r.headers.get("location", "")
        )
        print(f"{'✓' if blocked else '✗'} cross-site post blocked -> {r.status_code} {r.headers.get('location','')}")
        if not blocked:
            problems.append("[csrf] a provably cross-site POST was accepted")

    print()
    print("=" * 62)
    print(f"checks run: {checks}")
    if problems:
        print(f"❌ {len(problems)} problem(s):")
        for item in problems:
            print("   -", item)
        return 1
    print("✅ everything green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
