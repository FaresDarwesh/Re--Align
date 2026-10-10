"""Secret redaction — nothing sensitive ever reaches a log line, an error
message, an API response or the browser.

Covers: passwords, tokens, API keys, bearer headers, private keys, e-mails,
phone numbers, card numbers, Egyptian national IDs, connection strings,
webhook secrets and any configured value the operator marked as secret.
"""

from __future__ import annotations

import re
from typing import Any

from realign.config import settings

MASK = "***REDACTED***"

# Keys whose *value* must never be logged, whatever the source dict is.
SENSITIVE_KEYS = frozenset(
    {
        "password",
        "new_password",
        "current_password",
        "old_password",
        "hashed_password",
        "passwd",
        "pwd",
        "secret",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "token",
        "id_token",
        "authorization",
        "auth",
        "cookie",
        "set_cookie",
        "session",
        "csrf",
        "private_key",
        "llm_api_key",
        "llm_api_key_enc",
        "credentials",
        "client_secret",
        "webhook_secret",
        "hmac_secret",
        "encryption_key",
        "app_secret_key",
        "smtp_password",
        "card",
        "card_number",
        "cvv",
        "iban",
        "otp",
        "mfa",
        "seed",
        "salt",
    }
)

# Substrings that mark a key as sensitive even if not listed exactly.
SENSITIVE_KEY_PARTS = ("password", "secret", "token", "api_key", "apikey", "credential", "private")

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # JWT (three base64url segments separated by dots)
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), MASK),
    # Bearer / Basic auth headers
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + MASK),
    # Our own API keys
    (re.compile(r"\bnxk_[A-Za-z0-9_-]{8,}"), MASK),
    # Provider keys
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), MASK),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}"), MASK),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), MASK),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), MASK),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), MASK),
    # PEM private keys
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), MASK),
    # Fernet keys (urlsafe base64, 44 chars ending with '=')
    (re.compile(r"\b[A-Za-z0-9_-]{43}=\b"), MASK),
    # key=value / "key": "value" pairs in free text
    (
        re.compile(
            r"(?i)\b(pass(?:word|wd)?|secret|token|api[_-]?key|auth|credential)\b\s*[:=]\s*"
            r"(\"[^\"]{1,120}\"|'[^']{1,120}'|[^\s,;&]{1,120})"
        ),
        r"\1=" + MASK,
    ),
    # Database URLs with an embedded password
    (re.compile(r"(?i)\b([a-z+]+:\/\/)[^\s:@\/]+:[^\s@\/]+@"), r"\1" + MASK + "@"),
    # Payment cards (13-19 digits, optionally separated)
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), MASK),
]

# E-mail: keep the first character so the log stays debuggable but not identifying.
_EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
_PHONE_RE = re.compile(r"(?<![\d])(?:\+?\d{1,3}[\s-]?)?(?:01[0125]\d{8}|0\d{9,10})(?![\d])")
_NID_RE = re.compile(r"\b\d{14}\b")


def _extra_secrets() -> list[str]:
    """Live secret values pulled from settings, so a leaked config value is
    masked even if it appears inside an unrelated string."""
    values = [
        settings.app_secret_key,
        settings.credentials_encryption_key,
        settings.billing_webhook_secret,
        getattr(settings, "openai_api_key", "") or "",
        getattr(settings, "anthropic_api_key", "") or "",
        getattr(settings, "smtp_password", "") or "",
    ]
    return [v for v in values if isinstance(v, str) and len(v) >= 12]


def _redact_text(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    text = _EMAIL_RE.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", text)
    text = _NID_RE.sub(MASK, text)
    for secret in _extra_secrets():
        if secret and secret in text:
            text = text.replace(secret, MASK)
    return text


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively redact any structure (dict / list / str / anything)."""
    if _depth > 6:
        return MASK
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, val in value.items():
            k = str(key)
            lowered = k.lower()
            if lowered in SENSITIVE_KEYS or any(part in lowered for part in SENSITIVE_KEY_PARTS):
                out[key] = MASK
            else:
                out[key] = redact(val, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return type(value)(redact(v, _depth + 1) for v in value)
    if isinstance(value, bytes):
        try:
            return _redact_text(value.decode("utf-8", "replace")).encode()  # type: ignore[return-value]
        except Exception:  # noqa: BLE001
            return MASK
    return value


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    out = {}
    for key, value in headers.items():
        if key.lower() in {"authorization", "cookie", "set-cookie", "x-api-key", "x-csrf-token"}:
            out[key] = MASK
        else:
            out[key] = redact(value)
    return out


# ------------------------------------------------------------- structlog ----


def redacting_processor(_logger, _method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor: scrub every field before it is rendered."""
    out: dict[str, Any] = {}
    for key, value in event_dict.items():
        if key == "event" and isinstance(value, str):
            out[key] = _redact_text(value)
        else:
            out[key] = redact(value)
    return out


__all__ = [
    "MASK",
    "redact",
    "redact_headers",
    "redacting_processor",
    "SENSITIVE_KEYS",
]
