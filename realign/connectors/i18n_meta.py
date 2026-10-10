"""English descriptions for the connector catalogue (Arabic lives on the class)."""

from __future__ import annotations

CONNECTORS_EN: dict[str, str] = {
    "mock_channel": "Simulates publishing and stores the output locally — perfect for trying the platform before connecting any real account.",
    "telegram": "Publishes messages and replies to customers through a Telegram bot.",
    "webhook": "Sends any output to any external URL — n8n, Make, Zapier or your own server.",
    "email": "Sends messages and newsletters through any SMTP server.",
    "rss": "Reads the latest articles from any RSS feed and uses them as research signals.",
    "instagram": "Publishes photos and reels to an Instagram business account via the Graph API.",
    "facebook": "Publishes to a Facebook page.",
    "linkedin": "Publishes text posts to a LinkedIn page or profile.",
    "x": "Posts a tweet through the X v2 API.",
    "whatsapp": "Sends WhatsApp messages through the official Meta Cloud API.",
    "shopify": "Reads products and orders from a Shopify store to feed the sales and analytics agents.",
    "google_analytics": "Pulls performance metrics for your site to feed the analytics agent.",
}


def english(key: str) -> str:
    return CONNECTORS_EN.get(key, "")


__all__ = ["CONNECTORS_EN", "english"]
