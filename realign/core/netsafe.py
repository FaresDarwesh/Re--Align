"""SSRF guard — outbound HTTP may never touch internal networks.

CWE-918. The platform fetches user-supplied URLs (RSS feeds, outbound webhooks,
generic inbox). Without this module any tenant could make the server request
  http://169.254.169.254/latest/meta-data/     (cloud metadata / credentials)
  http://127.0.0.1:8000/api/v1/admin/stats    (loopback services)
  http://10.0.0.5/                            (private network)
and read the response back.

Defence in depth:
  1. scheme allowlist (http/https only)
  2. DNS resolution **before** connecting, every address checked
  3. block loopback / link-local / private / reserved / multicast / metadata
  4. re-validate on every redirect hop (`follow_redirects=False` + manual loop)
  5. response size + timeout caps so a slow/huge body cannot exhaust memory
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx

from realign.core.sanitize import strip_control

BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
    "instance-data.ec2.internal",
}

MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MB
MAX_REDIRECTS = 3
DEFAULT_TIMEOUT = 8.0


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(
        ip.is_unspecified
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_reserved
        or ip.is_multicast
        or not ip.is_global
    )


def host_is_public(hostname: str) -> bool:
    """Resolve `hostname` and verify **every** returned address is public."""
    host = (hostname or "").strip().strip("[]").lower().rstrip(".")
    if not host:
        return False
    if host in BLOCKED_HOSTNAMES or host.endswith(".internal") or host.endswith(".local"):
        return False
    # A literal IP needs no DNS.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return not _is_blocked_ip(literal)
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:  # noqa: BLE001 - unresolvable == untrusted
        return False
    addresses = [info[4][0] for info in infos]
    if not addresses:
        return False
    for raw in addresses:
        try:
            parsed = ipaddress.ip_address(raw.split("%")[0])
        except ValueError:
            return False
        if _is_blocked_ip(parsed):
            return False
    return True


class SSRFBlocked(Exception):
    """Raised when a URL points somewhere the server must not talk to."""


def assert_public_url(url: str) -> str:
    """Validate `url`; return it unchanged or raise `SSRFBlocked`."""
    candidate = strip_control(str(url or "")).strip()
    if len(candidate) > 2048:
        raise SSRFBlocked("الرابط طويل جداً")
    try:
        parsed = urlparse(candidate)
    except Exception as exc:  # noqa: BLE001
        raise SSRFBlocked("رابط غير صالح") from exc
    if parsed.scheme not in ("http", "https"):
        raise SSRFBlocked("المسموح فقط http و https")
    if parsed.username or parsed.password:
        raise SSRFBlocked("لا يُسمح ببيانات اعتماد داخل الرابط")
    if not parsed.hostname:
        raise SSRFBlocked("رابط بدون نطاق")
    if not host_is_public(parsed.hostname):
        raise SSRFBlocked("الرابط يشير إلى شبكة داخلية أو عنوان محجوز")
    return candidate


def url_is_public(url: str) -> bool:
    try:
        assert_public_url(url)
    except SSRFBlocked:
        return False
    return True


async def safe_get(url: str, *, timeout: float = DEFAULT_TIMEOUT, **kwargs) -> httpx.Response:
    """GET a URL, refusing private targets and refusing to follow a redirect
    that would land inside the internal network."""
    assert_public_url(url)
    current = url
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, **kwargs) as client:
        for _ in range(MAX_REDIRECTS + 1):
            response = await client.get(current)
            if response.is_redirect:
                location = response.headers.get("location", "")
                if not location:
                    return response
                nxt = str(response.url.join(location)) if location.startswith("/") else location
                assert_public_url(nxt)      # SSRF check on the hop target too
                current = nxt
                continue
            # Cap the body so a hostile server cannot exhaust memory (CWE-400).
            if int(response.headers.get("content-length") or 0) > MAX_RESPONSE_BYTES:
                raise SSRFBlocked("حجم الاستجابة أكبر من المسموح")
            return response
        raise SSRFBlocked("عدد كبير من التحويلات")


async def safe_post(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    client_kwargs: dict | None = None,
    **request_kwargs,
) -> httpx.Response:
    """Same guarantees as `safe_get`, and a redirect is never followed on POST."""
    assert_public_url(url)
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, **(client_kwargs or {})
    ) as client:
        return await client.post(url, **request_kwargs)


__all__ = [
    "SSRFBlocked",
    "assert_public_url",
    "url_is_public",
    "host_is_public",
    "safe_get",
    "safe_post",
]
