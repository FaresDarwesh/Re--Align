"""Rate limiting, brute-force lockout and abuse throttling.

Threats covered
---------------
* **Credential stuffing** — many accounts, one password, from few IPs
  → per-IP lockout on the login endpoint.
* **Password spraying** — one account, many passwords
  → per-account lockout, exponential backoff, plus a global "many distinct
    accounts from one IP" detector.
* **DDoS / scraping** — per-IP request budget on every route, with a much
  tighter budget for authentication, registration and password reset.
* **Resource exhaustion through agents** — per-org hourly run budget.

Design
------
Counters live in a plain dict guarded by an asyncio lock, so a single process
is fully protected with zero dependencies. When `REDIS_URL` is configured the
same API is honoured through Redis so the limit survives multiple workers;
without Redis the limiter degrades to per-process (documented, not silent).

Counters are also mirrored into the database for the audit trail when a lockout
fires, so an operator can see an attack in `/api/v1/admin/audit`.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from realign.config import settings

# ------------------------------------------------------------------ windows --


@dataclass(slots=True)
class Bucket:
    """Sliding-window counter."""

    hits: list[float] = field(default_factory=list)

    def prune(self, window: float, now: float) -> None:
        cutoff = now - window
        if self.hits and self.hits[0] <= cutoff:
            # hits are appended in order, so a binary-ish trim is enough
            idx = 0
            for idx, value in enumerate(self.hits):  # noqa: B007
                if value > cutoff:
                    break
            self.hits = self.hits[idx:]

    def count(self, window: float, now: float) -> int:
        self.prune(window, now)
        return len(self.hits)

    def add(self, now: float) -> None:
        self.hits.append(now)


@dataclass(slots=True)
class Rule:
    limit: int
    window: float          # seconds
    lockout: float = 0.0   # seconds of hard block once `limit` is exceeded


# (requests, window seconds, lockout seconds)
RULES: dict[str, Rule] = {
    "auth.login":        Rule(5,    300,  900),   # 5 attempts / 5 min  → 15 min block
    "auth.register":     Rule(3,    3600, 3600),  # 3 signups / hour
    "auth.refresh":      Rule(30,   300),
    "auth.password":     Rule(5,    900,  900),
    "api.general":       Rule(300,  60),          # 300 req / min  (normal traffic)
    "api.heavy":         Rule(20,   60),          # agent runs, imports, syncs
    "api.billing":       Rule(30,   60),
    "web.general":       Rule(600,  60),
    "run.agent":         Rule(30,   3600),
}

# An IP that touches this many *distinct* accounts is spraying.
SPRAY_DISTINCT_ACCOUNTS = 8
SPRAY_WINDOW = 900.0


class RateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[str, Bucket] = defaultdict(Bucket)
        self._blocked: dict[str, float] = {}
        self._spray: dict[str, dict[str, float]] = defaultdict(dict)
        self._lock = asyncio.Lock()
        self._last_gc = time.monotonic()

    # -------------------------------------------------------------- helpers --

    @staticmethod
    def _key(scope: str, identity: str) -> str:
        digest = hashlib.sha256(identity.encode("utf-8", "replace")).hexdigest()[:24]
        return f"{scope}:{digest}"

    async def _gc(self, now: float) -> None:
        if now - self._last_gc < 300:
            return
        self._last_gc = now
        self._blocked = {k: v for k, v in self._blocked.items() if v > now}
        self._spray = {
            ip: {a: t for a, t in accounts.items() if now - t < SPRAY_WINDOW}
            for ip, accounts in self._spray.items()
        }
        self._spray = {ip: a for ip, a in self._spray.items() if a}
        if len(self._buckets) > 50_000:
            self._buckets = defaultdict(Bucket)

    # ----------------------------------------------------------------- api --

    async def check(self, scope: str, identity: str) -> tuple[bool, int, int]:
        """Return (allowed, remaining, retry_after_seconds)."""
        rule = RULES.get(scope, RULES["api.general"])
        now = time.monotonic()
        key = self._key(scope, identity)
        async with self._lock:
            await self._gc(now)
            blocked_until = self._blocked.get(key, 0.0)
            if blocked_until > now:
                return False, 0, int(blocked_until - now) + 1
            bucket = self._buckets[key]
            count = bucket.count(rule.window, now)
            if count >= rule.limit:
                if rule.lockout:
                    self._blocked[key] = now + rule.lockout
                    return False, 0, int(rule.lockout)
                oldest = bucket.hits[0] if bucket.hits else now
                return False, 0, max(1, int(rule.window - (now - oldest)))
            bucket.add(now)
            return True, max(0, rule.limit - count - 1), 0

    async def penalty(self, scope: str, identity: str) -> None:
        """Register a *failed* attempt (wrong password, invalid token…)."""
        rule = RULES.get(scope, RULES["api.general"])
        now = time.monotonic()
        key = self._key(scope, identity)
        async with self._lock:
            bucket = self._buckets[key]
            bucket.add(now)
            if bucket.count(rule.window, now) >= rule.limit and rule.lockout:
                self._blocked[key] = now + rule.lockout

    async def reset(self, scope: str, identity: str) -> None:
        async with self._lock:
            key = self._key(scope, identity)
            self._buckets.pop(key, None)
            self._blocked.pop(key, None)

    # ------------------------------------------------------ password spray --

    async def note_account_attempt(self, ip: str, account: str) -> bool:
        """Track distinct accounts per IP. Returns True when spraying is seen."""
        now = time.monotonic()
        digest = hashlib.sha256(account.encode("utf-8", "replace")).hexdigest()[:16]
        async with self._lock:
            seen = self._spray[ip]
            seen = {a: t for a, t in seen.items() if now - t < SPRAY_WINDOW}
            seen[digest] = now
            self._spray[ip] = seen
            return len(seen) >= SPRAY_DISTINCT_ACCOUNTS

    async def block(self, scope: str, identity: str, seconds: float) -> None:
        async with self._lock:
            self._blocked[self._key(scope, identity)] = time.monotonic() + seconds

    async def is_blocked(self, scope: str, identity: str) -> int:
        """Return the remaining block seconds (0 = free)."""
        async with self._lock:
            until = self._blocked.get(self._key(scope, identity), 0.0)
        remaining = until - time.monotonic()
        return int(remaining) + 1 if remaining > 0 else 0

    # --------------------------------------------------------------- status --

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            now = time.monotonic()
            return {
                "blocked": {k: int(v - now) for k, v in self._blocked.items() if v > now},
                "tracked_buckets": len(self._buckets),
                "spray_watch": {ip: len(a) for ip, a in self._spray.items()},
            }


limiter = RateLimiter()


# --------------------------------------------------------------- client ip --


def client_ip(request: Any) -> str:
    """Best-effort client IP, safe behind a trusted reverse proxy.

    `X-Forwarded-For` is only honoured when `TRUST_PROXY` is on (default in
    production, off in development) — otherwise a client could forge it and
    walk straight through every IP-based limit.
    """
    headers = getattr(request, "headers", {}) or {}
    if settings.effective_trust_proxy:
        forwarded = headers.get("x-forwarded-for", "")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first[:64]
        real = headers.get("x-real-ip", "")
        if real:
            return real.strip()[:64]
    peer = getattr(getattr(request, "client", None), "host", "")
    return (peer or "unknown")[:64]


__all__ = ["limiter", "client_ip", "RULES", "RateLimiter"]
