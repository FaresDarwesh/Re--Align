"""Central configuration. Every value has a safe default so the platform
boots with zero external services (SQLite + mock LLM + mock connectors)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from urllib.parse import quote, unquote

import pydantic
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


def _data_dir() -> Path:
    """A writable directory for SQLite, uploads and media.

    Some hosts hand you a read-only bundle and one writable scratch
    directory, so `BASE_DIR/data` simply does not exist there.
    Falling back keeps the app importable instead of exploding at cold start.
    """
    for candidate in (BASE_DIR / "data", Path("/tmp") / "realign-data"):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return candidate
        except Exception:  # noqa: BLE001 - read-only FS, wrong owner, …
            continue
    raise RuntimeError("no writable data directory available")


DATA_DIR = _data_dir()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BASE_DIR / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- core --
    app_env: Literal["development", "staging", "production"] = "development"
    app_name: str = "re-align"
    app_tagline: str = "نظام تشغيل الوكلاء الأذكياء"
    app_secret_key: str = "dev-insecure-secret-change-me-please-change-me"
    app_base_url: str = "http://localhost:8000"
    default_locale: str = "ar"
    default_timezone: str = "Africa/Cairo"
    debug: bool = True

    credentials_encryption_key: str = ""

    # ------------------------------------------------------------ database --
    # Many managed-Postgres providers hand you `postgres://…`, which
    # SQLAlchemy 2 refuses; and asyncpg wants `postgresql+asyncpg://`. The
    # validator below normalises whatever it is given.
    database_url: str = f"sqlite+aiosqlite:///{DATA_DIR / 'realign.db'}"
    serverless: bool = False
    cron_secret: str = ""
    cron_batch_size: int = 10

    # Fool-proof alternative to DATABASE_URL: set the four components as
    # separate env vars and the URL is composed here, with the password
    # percent-encoded correctly - special characters cannot break it.
    pghost: str = ""
    pgport: int = 5432
    pguser: str = ""
    pgpassword: str = ""
    pgdatabase: str = "neondb"
    sql_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # ---------------------------------------------------------------- auth --
    access_token_ttl_minutes: int = 60
    refresh_token_ttl_days: int = 30
    allow_public_signup: bool = True
    # First-run bootstrap: on an empty database this owner account is created
    # so a fresh deploy is usable immediately. Ignored once any user exists.
    admin_email: str = "admin@re-align.app"
    admin_password: str = ""
    admin_org_name: str = "شركتي"

    # ----------------------------------------------------------------- llm --
    llm_provider: str = "mock"
    llm_thinking_provider: str = "mock"
    llm_decision_provider: str = "mock"
    llm_embedding_provider: str = "mock"

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_thinking_model: str = "gpt-4o"
    openai_decision_model: str = "gpt-4o-mini"

    anthropic_api_key: str = ""
    anthropic_thinking_model: str = "claude-sonnet-4-5"
    anthropic_decision_model: str = "claude-haiku-4-5"

    ollama_base_url: str = "http://localhost:11434"
    ollama_thinking_model: str = "qwen2.5:32b"
    ollama_decision_model: str = "qwen2.5:7b"

    custom_base_url: str = ""
    custom_api_key: str = ""
    custom_thinking_model: str = ""
    custom_decision_model: str = ""

    llm_timeout_seconds: int = 120
    llm_max_retries: int = 3
    llm_max_output_tokens: int = 4096
    allow_org_llm_keys: bool = True

    # ------------------------------------------------------------- billing --
    billing_provider: str = "none"
    billing_currency: str = "EGP"
    billing_webhook_secret: str = ""

    paymob_api_key: str = ""
    paymob_hmac_secret: str = ""
    paymob_integration_id: str = ""
    paymob_iframe_id: str = ""

    kashier_api_key: str = ""
    kashier_merchant_id: str = ""
    kashier_webhook_secret: str = ""

    stripe_api_key: str = ""
    stripe_webhook_secret: str = ""

    default_credits_per_month: int = 5000
    credit_usd_value_cents: int = 1

    # ----------------------------------------------------------- execution --
    worker_enabled: bool = True
    worker_poll_seconds: float = 2.0
    worker_concurrency: int = 4
    worker_max_attempts: int = 3
    worker_backoff_seconds: int = 5
    scheduler_enabled: bool = True
    kill_switch_global: bool = False

    default_monthly_budget_cents: int = 50000
    default_per_run_budget_cents: int = 200
    default_daily_run_limit: int = 500

    # -------------------------------------------------------- integrations --
    telegram_bot_token: str = ""
    meta_app_id: str = ""
    meta_app_secret: str = ""
    meta_access_token: str = ""
    meta_ig_business_account_id: str = ""
    linkedin_client_id: str = ""
    linkedin_client_secret: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    google_analytics_property_id: str = ""
    whatsapp_cloud_token: str = ""
    whatsapp_phone_number_id: str = ""
    shopify_store_domain: str = ""
    shopify_access_token: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "re-align <no-reply@example.com>"

    # --------------------------------------------------------------- misc --
    log_level: str = "INFO"
    log_json: bool = False
    rate_limit_per_minute: int = 120
    telemetry_enabled: bool = False

    # -------------------------------------------------------------- security --
    # Transport / proxy
    trust_proxy: bool = False          # honoured automatically in production
    force_https: bool = True           # 308 + HSTS when app_env=production
    hsts_max_age: int = 31_536_000    # one year
    allowed_hosts: str = ""            # comma separated; empty = same-origin only

    # Sessions
    session_cookie_secure: bool = True  # auto: True in production
    session_cookie_samesite: str = "lax"
    auto_cookie_samesite: bool = True  # SameSite=None inside a cross-site iframe
    session_idle_minutes: int = 720
    session_absolute_minutes: int = 14_400
    rotate_session_on_login: bool = True   # session-fixation defence (CWE-384)

    # CSRF
    csrf_enabled: bool = True
    csrf_ttl_minutes: int = 240

    # Password policy (credential stuffing / spraying)
    password_min_length: int = 10
    # NIST SP 800-63B: length + blocklist beat forced composition rules,
    # so uppercase/symbol are optional and can be switched on per-tenant need.
    password_require_upper: bool = False
    password_require_lower: bool = False
    password_require_digit: bool = True
    password_require_symbol: bool = False
    password_block_common: bool = True
    password_max_length: int = 256
    max_failed_logins_per_hour: int = 10

    # SSRF / outbound network
    ssrf_protection: bool = True
    ssrf_allow_private: bool = False   # dev-only escape hatch; forced off in prod

    # Rate limiting / abuse
    rate_limit_enabled: bool = True
    redis_url: str = ""                # optional: shared counters across workers

    # Uploads
    upload_max_bytes: int = 2 * 1024 * 1024
    upload_allowed_types: str = "image/png,image/jpeg,image/webp"
    media_dir: str = str(DATA_DIR / "media")

    # Front-end / content
    cookie_consent_required: bool = True
    security_report_email: str = "security@yourdomain.com"
    expose_docs: bool = False          # /docs hidden unless explicitly enabled

    @pydantic.model_validator(mode="after")
    def _detect_serverless(self) -> "Settings":
        """Vercel sets VERCEL=1; honour it unless explicitly overridden."""
        if "serverless" not in self.model_fields_set:
            if str(os.getenv("VERCEL", "")).strip().lower() in {"1", "true"}:
                self.serverless = True
        return self

    @pydantic.model_validator(mode="after")
    def _compose_pg_url(self) -> "Settings":
        if not (self.pghost or "").strip().strip("\"'").strip():
            return self

        def _clean(v: str) -> str:
            # Consoles/docs often get copy-pasted *with* surrounding quotes
            # (e.g. PGHOST="ep-….neon.tech"). Literal quotes inside a DNS
            # name make it unresolvable — and on AWS Lambda an unresolvable
            # host surfaces as the cryptic OSError EBUSY, not a DNS error.
            return (v or "").strip().strip("\"'").strip()

        user = quote(_clean(self.pguser) or "neondb_owner", safe="")
        # Tolerate a password pasted straight out of a connection string
        # (percent-encoded) as well as a raw one: normalise then encode.
        # Quotes are stripped, but whitespace inside the password is kept.
        raw = unquote((self.pgpassword or "").strip().strip("\"'"))
        pwd = quote(raw, safe="")
        self.database_url = (
            f"postgresql+asyncpg://{user}:{pwd}"
            f"@{_clean(self.pghost)}:{self.pgport}/{_clean(self.pgdatabase) or 'neondb'}"
            "?sslmode=require"
        )
        return self

    # ---------------------------------------------------------- validators -
    @pydantic.field_validator("database_url", mode="before")
    @classmethod
    def _normalise_database_url(cls, v: object) -> object:
        if not isinstance(v, str) or not v.strip():
            return v
        url = v.strip()
        if url.startswith("postgres://"):
            url = "postgresql+asyncpg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+asyncpg://" + url[len("postgresql://"):]
        # Managed Postgres (Neon, Supabase, …) requires TLS.
        if url.startswith("postgresql+asyncpg://") and "sslmode" not in url:
            url += ("&" if "?" in url else "?") + "sslmode=require"
        return url

    # ----------------------------------------------------------- helpers ---
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def integration_credentials(self, key: str) -> dict[str, str]:
        """Global (platform-level) credentials for a connector, from env."""
        mapping: dict[str, dict[str, str]] = {
            "telegram": {"bot_token": self.telegram_bot_token},
            "instagram": {
                "app_id": self.meta_app_id,
                "app_secret": self.meta_app_secret,
                "access_token": self.meta_access_token,
                "ig_business_account_id": self.meta_ig_business_account_id,
            },
            "facebook": {
                "app_id": self.meta_app_id,
                "app_secret": self.meta_app_secret,
                "access_token": self.meta_access_token,
            },
            "linkedin": {
                "client_id": self.linkedin_client_id,
                "client_secret": self.linkedin_client_secret,
            },
            "google_analytics": {
                "client_id": self.google_client_id,
                "client_secret": self.google_client_secret,
                "property_id": self.google_analytics_property_id,
            },
            "whatsapp": {
                "token": self.whatsapp_cloud_token,
                "phone_number_id": self.whatsapp_phone_number_id,
            },
            "shopify": {
                "store_domain": self.shopify_store_domain,
                "access_token": self.shopify_access_token,
            },
            "email": {
                "host": self.smtp_host,
                "port": str(self.smtp_port),
                "user": self.smtp_user,
                "password": self.smtp_password,
                "from": self.smtp_from,
            },
        }
        return {k: v for k, v in mapping.get(key, {}).items() if v}

    # ------------------------------------------------------------- helpers --

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def allowed_host_list(self) -> list[str]:
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]

    @property
    def upload_allowed_type_list(self) -> list[str]:
        return [t.strip() for t in self.upload_allowed_types.split(",") if t.strip()]

    @property
    def effective_trust_proxy(self) -> bool:
        return self.trust_proxy or self.is_production

    @property
    def effective_ssrf_strict(self) -> bool:
        """Private/internal targets are always refused in production."""
        return self.ssrf_protection and (self.is_production or not self.ssrf_allow_private)

    @property
    def effective_cookie_secure(self) -> bool:
        """`Secure` breaks plain-http local development, so it is auto-relaxed
        there and always on for staging/production."""
        if self.app_env == "development":
            return False
        return self.session_cookie_secure


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

# Keep os.environ in sync for libs that read it directly (Alembic, APScheduler).
os.environ.setdefault("RE_ALIGN_DATABASE_URL", settings.database_url)
