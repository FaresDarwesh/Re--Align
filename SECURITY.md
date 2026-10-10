# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| latest `main` | ✅ |
| older tags | ❌ |

## Reporting a vulnerability

Email **security@yourdomain.com** (set `SECURITY_REPORT_EMAIL`). Please include:

1. What you observed and where,
2. Steps to reproduce,
3. The impact as you judge it,
4. Anything you think fixes it.

**Please do not open a public issue for a security problem.** Give us 90 days
before disclosing; we will acknowledge within 72 hours and keep you updated.

## Scope

In scope: the web app, the JSON API, tenant isolation, authentication/session
handling, the approval gate, the outbox, and billing webhook verification.

Out of scope: denial-of-service by sheer volume, issues in third-party services
you connect yourself, and reports that require social engineering of our staff.

## Built-in protections

Multi-tenant isolation at the query layer · CSRF (double-submit + Fetch-Metadata)
· rate limiting with spray detection · bcrypt password hashing · JWT with `sid`,
`typ` and `fp` claims · AES-GCM/Fernet encryption of connector credentials ·
SSRF guards on outbound requests · hardened security headers · full audit log ·
per-task and per-month spend caps · an instant kill switch.

Run the suite with `python -m pytest -q` (86 tests) and the end-to-end harness
with `python scripts/smoke.py` (80 checks).
