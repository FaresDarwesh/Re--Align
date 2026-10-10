"""Connector registry + credential resolution (env fallback, then tenant row)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from realign.config import settings
from realign.connectors.base import Connector
from realign.connectors.channels import (
    EmailConnector,
    MockChannelConnector,
    RSSConnector,
    TelegramConnector,
    WebhookConnector,
)
from realign.connectors.social import (
    FacebookConnector,
    GoogleAnalyticsConnector,
    InstagramConnector,
    LinkedInConnector,
    ShopifyConnector,
    WhatsAppConnector,
    XConnector,
)

CONNECTOR_CLASSES: list[type[Connector]] = [
    MockChannelConnector,
    TelegramConnector,
    WebhookConnector,
    EmailConnector,
    RSSConnector,
    InstagramConnector,
    FacebookConnector,
    LinkedInConnector,
    XConnector,
    WhatsAppConnector,
    ShopifyConnector,
    GoogleAnalyticsConnector,
]

_CONNECTORS: dict[str, Connector] = {cls.key: cls() for cls in CONNECTOR_CLASSES}


def get_connector(key: str) -> Connector | None:
    return _CONNECTORS.get(key)


def list_connectors() -> list[Connector]:
    return list(_CONNECTORS.values())


def connector_keys() -> list[str]:
    return list(_CONNECTORS.keys())


def resolve_channel_connector(channel: str) -> Connector:
    """Map a content channel name to a connector key."""
    mapping = {
        "instagram": "instagram",
        "facebook": "facebook",
        "linkedin": "linkedin",
        "x": "x",
        "whatsapp": "whatsapp",
        "telegram": "telegram",
        "email": "email",
        "rss": "rss",
        "shopify": "shopify",
        "google_analytics": "google_analytics",
    }
    return get_connector(mapping.get(channel, "mock_channel")) or _CONNECTORS["mock_channel"]


async def credentials_for(
    session: AsyncSession,
    *,
    org_id: str,
    connector_key: str,
) -> dict[str, Any]:
    """Tenant credentials, falling back to platform-level env credentials."""
    from realign.core.security import decrypt_dict
    from realign.models import Integration

    result = await session.execute(
        select(Integration).where(
            Integration.org_id == org_id, Integration.connector_key == connector_key
        )
    )
    row = result.scalar_one_or_none()

    creds: dict[str, Any] = {}
    if row is not None:
        creds.update(decrypt_dict(row.credentials_enc))
        creds.update({k: v for k, v in (row.config or {}).items() if k in ("chat_id", "page_id", "author_urn", "default_image_url")})

    env_creds = settings.integration_credentials(connector_key)
    for key, value in env_creds.items():
        creds.setdefault(key, value)

    return creds


async def connector_overview(session: AsyncSession, org_id: str) -> list[dict[str, Any]]:
    from realign.core.security import decrypt_dict
    from realign.models import Integration

    rows = (
        await session.execute(select(Integration).where(Integration.org_id == org_id))
    ).scalars().all()
    by_key = {r.connector_key: r for r in rows}

    out: list[dict[str, Any]] = []
    for connector in list_connectors():
        row = by_key.get(connector.key)
        creds = decrypt_dict(row.credentials_enc) if row else {}
        creds = {**settings.integration_credentials(connector.key), **creds}
        status = row.status if row else ("connected" if connector.is_configured(creds) else "not_configured")
        out.append(connector.meta(status=status, configured_fields=connector.configured_fields(creds)))
    return out


async def ensure_integration_rows(session: AsyncSession, org_id: str) -> None:
    """Create a row for every connector so the UI can show one card each."""
    from realign.models import Integration
    from realign.models.system import IntegrationStatus

    existing = {
        r.connector_key
        for r in (
            await session.execute(select(Integration).where(Integration.org_id == org_id))
        ).scalars().all()
    }
    added = False
    for connector in list_connectors():
        if connector.key in existing:
            continue
        env_creds = settings.integration_credentials(connector.key)
        status = IntegrationStatus.CONNECTED if connector.is_configured(env_creds) else IntegrationStatus.NOT_CONFIGURED
        session.add(
            Integration(org_id=org_id, connector_key=connector.key, display_name=connector.name_ar, status=status)
        )
        added = True
    if added:
        await session.flush()


__all__ = [
    "get_connector",
    "list_connectors",
    "connector_keys",
    "resolve_channel_connector",
    "credentials_for",
    "connector_overview",
    "ensure_integration_rows",
]
