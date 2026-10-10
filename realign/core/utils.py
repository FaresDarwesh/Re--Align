"""Small shared helpers: ids, time, money, text."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

# --------------------------------------------------------------------- ids --


def new_uuid() -> str:
    return str(uuid.uuid4())


def short_id(prefix: str = "", length: int = 12) -> str:
    alphabet = string.ascii_lowercase + string.digits
    body = "".join(secrets.choice(alphabet) for _ in range(length))
    return f"{prefix}_{body}" if prefix else body


def slugify(value: str, max_len: int = 60) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE)
    value = re.sub(r"[\s_-]+", "-", value, flags=re.UNICODE)
    value = re.sub(r"^-+|-+$", "", value)
    return value[:max_len] or "item"


# -------------------------------------------------------------------- time --


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def naive_utc(dt: datetime | None = None) -> datetime:
    """SQLite has no timezone type; we always store naive UTC."""
    dt = dt or utcnow()
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def add_months(dt: datetime, months: int) -> datetime:
    year = dt.year + (dt.month - 1 + months) // 12
    month = (dt.month - 1 + months) % 12 + 1
    day = min(dt.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                       31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return dt.replace(year=year, month=month, day=day)


# ------------------------------------------------------------------- money --
# All internal money/math on AI cost is done in "micros" (1 USD = 1_000_000 micros)
# to keep everything in integers and avoid float drift.


def usd_to_micros(usd: float | Decimal) -> int:
    return int((Decimal(str(usd)) * 1_000_000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def micros_to_usd(micros: int) -> Decimal:
    return (Decimal(micros) / Decimal(1_000_000)).quantize(Decimal("0.0001"))


def micros_to_credits(micros: int, credit_usd_value_cents: int = 1) -> int:
    """Convert AI cost (micros) to platform credits.

    1 credit == ``credit_usd_value_cents`` US cents by default ($0.01).
    Never rounds down to zero for non-zero cost: a run always costs >= 1 credit.
    """
    if micros <= 0:
        return 0
    cents = Decimal(micros) / Decimal(10_000)          # micros -> cents
    credits = cents / Decimal(max(credit_usd_value_cents, 1))
    out = int(credits.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return max(out, 1)


def format_money(cents: int, currency: str = "EGP") -> str:
    value = (Decimal(cents) / Decimal(100)).quantize(Decimal("0.01"))
    return f"{value:,.2f} {currency}"


# -------------------------------------------------------------------- text --


ARABIC_DIACRITICS = re.compile(r"[\u064B-\u0652\u0670\u0640]")


def normalize_arabic(text: str) -> str:
    text = ARABIC_DIACRITICS.sub("", text or "")
    replacements = {
        "أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه",
        "ؤ": "و", "ئ": "ي",
    }
    return "".join(replacements.get(ch, ch) for ch in text)


def truncate(text: str, limit: int = 200, suffix: str = "…") -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - len(suffix)].rstrip() + suffix


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "").strip()


# ------------------------------------------------------------------ crypto --


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(str(a or ""), str(b or ""))


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hmac_sha256_hex(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def mask_secret(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - keep) + value[-2:]
