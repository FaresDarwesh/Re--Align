"""Always-available connectors: dry-run channel, Telegram, generic webhook,
email (SMTP) and RSS reader. None of them require a paid third-party plan."""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage
from typing import Any
from xml.etree import ElementTree

import httpx

from realign.connectors.base import Connector, PublishResult, SendResult, ValidationResult


class MockChannelConnector(Connector):
    """Dry-run publisher. Records the payload and returns a synthetic URL.

    This is what keeps the platform demonstrable with zero credentials: every
    'publish' is simulated, logged and marked clearly in the UI."""

    key = "mock_channel"
    name = "Dry-run channel"
    name_ar = "قناة تجريبية (بدون نشر حقيقي)"
    category = "automation"
    icon = "◌"
    description_ar = "يحاكي النشر ويحفظ المخرجات محلياً — ممتاز للتجربة قبل ربط أي حساب حقيقي."
    capabilities = ["publish", "message"]
    required_credentials: list[str] = []

    @classmethod
    def is_configured(cls, credentials: dict[str, Any]) -> bool:
        return True

    async def publish(self, credentials, asset, config=None) -> PublishResult:
        body = getattr(asset, "body", "") or ""
        external_id = f"dryrun_{asset.id[:8]}"
        return PublishResult(
            ok=True,
            external_id=external_id,
            url=f"/mock/{external_id}",
            message="نشر تجريبي — لم يُرسل لأي قناة حقيقية",
            raw={"channel": getattr(asset, "channel", ""), "length": len(body)},
        )

    async def send(self, credentials, *, to="", subject="", body="", config=None) -> SendResult:
        return SendResult(ok=True, external_id=f"dryrun_msg_{abs(hash((to, subject)))%10**8}", message="إرسال تجريبي")


class TelegramConnector(Connector):
    key = "telegram"
    name = "Telegram"
    name_ar = "تيليجرام"
    category = "messaging"
    icon = "✈"
    description_ar = "ينشر الرسائل ويرد على العملاء عبر بوت تيليجرام."
    required_credentials = ["bot_token", "chat_id"]
    capabilities = ["publish", "message"]
    docs_url = "https://core.telegram.org/bots/api"

    def _api(self, token: str) -> str:
        return f"https://api.telegram.org/bot{token}"

    async def validate(self, credentials, config=None) -> ValidationResult:
        base = super().validate(credentials, config)
        if not base.ok:
            return base
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                resp = await client.get(f"{self._api(credentials['bot_token'])}/getMe")
            data = resp.json()
            if data.get("ok"):
                return ValidationResult(ok=True, message="البوت متصل", details=data.get("result", {}))
            return ValidationResult(ok=False, message=str(data.get("description", "فشل التحقق")))
        except Exception as exc:  # noqa: BLE001
            return ValidationResult(ok=False, message=f"تعذّر الاتصال: {exc}")

    async def send(self, credentials, *, to="", subject="", body="", config=None) -> SendResult:
        chat_id = to or credentials.get("chat_id")
        text = f"{subject}\n\n{body}" if subject else body
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                resp = await client.post(
                    f"{self._api(credentials['bot_token'])}/sendMessage",
                    json={"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True},
                )
            data = resp.json()
            if data.get("ok"):
                msg_id = str(data["result"]["message_id"])
                return SendResult(ok=True, external_id=msg_id, message="تم الإرسال", raw=data)
            return SendResult(ok=False, message=str(data.get("description", "فشل الإرسال")), raw=data)
        except Exception as exc:  # noqa: BLE001
            return SendResult(ok=False, message=f"تعذّر الإرسال: {exc}")

    async def publish(self, credentials, asset, config=None) -> PublishResult:
        result = await self.send(
            credentials,
            to=str(credentials.get("chat_id", "")),
            subject=getattr(asset, "title", "") or "",
            body=getattr(asset, "body", "") or "",
            config=config,
        )
        return PublishResult(ok=result.ok, external_id=result.external_id, message=result.message, raw=result.raw)


class WebhookConnector(Connector):
    """Post to any HTTP endpoint (n8n, Make, Zapier, your own service)."""

    key = "webhook"
    name = "Webhook"
    name_ar = "ويب هوك (ربط خارجي)"
    category = "automation"
    icon = "⇄"
    description_ar = "يرسل أي مخرجات لأي رابط خارجي — n8n أو Make أو Zapier أو سيرفرك الخاص."
    required_credentials = ["url"]
    optional_credentials = ["secret"]
    capabilities = ["publish", "message"]

    async def publish(self, credentials, asset, config=None) -> PublishResult:
        import hashlib
        import hmac
        import json

        payload = {
            "asset_id": getattr(asset, "id", ""),
            "kind": getattr(asset, "kind", ""),
            "channel": getattr(asset, "channel", ""),
            "title": getattr(asset, "title", ""),
            "body": getattr(asset, "body", ""),
            "meta": getattr(asset, "meta", {}) or {},
        }
        headers = {"Content-Type": "application/json"}
        secret = credentials.get("secret") or ""
        if secret:
            body = json.dumps(payload, ensure_ascii=False)
            headers["X-ReAlign-Signature"] = hmac.new(
                secret.encode(), body.encode(), hashlib.sha256
            ).hexdigest()
        url = str(credentials.get("url") or "")
        try:
            from realign.config import settings

            if settings.effective_ssrf_strict:
                from realign.core.netsafe import assert_public_url

                assert_public_url(url)   # blocks 127.0.0.1 / 10.x / 169.254.169.254
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                resp = await client.post(url, json=payload, headers=headers)
            ok = 200 <= resp.status_code < 300
            return PublishResult(
                ok=ok,
                external_id=f"wh_{resp.status_code}",
                message="تم الإرسال" if ok else f"استجابة غير ناجحة: {resp.status_code}",
                raw={"status": resp.status_code, "body": resp.text[:500]},
            )
        except Exception as exc:  # noqa: BLE001
            return PublishResult(ok=False, message=f"تعذّر الإرسال: {exc}")

    async def send(self, credentials, *, to="", subject="", body="", config=None) -> SendResult:
        class _A:
            id, kind, channel, title, body, meta = "", "message", to or "webhook", subject, body, {}

        result = await self.publish(credentials, _A(), config)
        return SendResult(ok=result.ok, external_id=result.external_id, message=result.message)


class EmailConnector(Connector):
    """SMTP sender — works with Gmail, Outlook, Zoho, Mailgun SMTP, etc."""

    key = "email"
    name = "Email (SMTP)"
    name_ar = "البريد الإلكتروني (SMTP)"
    category = "email"
    icon = "✉"
    description_ar = "يرسل الرسائل والنشرات عبر أي خادم SMTP."
    required_credentials = ["host", "user", "password"]
    optional_credentials = ["port", "from"]
    capabilities = ["publish", "message"]

    def _send_sync(self, credentials: dict[str, Any], to: str, subject: str, body: str) -> tuple[bool, str]:
        msg = EmailMessage()
        msg["Subject"] = subject or "رسالة من نظام الوكلاء"
        msg["From"] = credentials.get("from") or credentials.get("user")
        msg["To"] = to
        msg.set_content(body or "")
        host = credentials.get("host")
        port = int(credentials.get("port") or 587)
        try:
            with smtplib.SMTP(host, port, timeout=25) as server:
                server.starttls()
                server.login(credentials.get("user", ""), credentials.get("password", ""))
                server.send_message(msg)
            return True, "تم الإرسال"
        except Exception as exc:  # noqa: BLE001
            return False, f"تعذّر الإرسال: {exc}"

    async def send(self, credentials, *, to="", subject="", body="", config=None) -> SendResult:
        ok, message = await asyncio.to_thread(
            self._send_sync, credentials, to or credentials.get("user", ""), subject, body
        )
        return SendResult(ok=ok, message=message, external_id="smtp" if ok else "")

    async def validate(self, credentials, config=None) -> ValidationResult:
        base = super().validate(credentials, config)
        if not base.ok:
            return base
        ok, message = await asyncio.to_thread(
            self._send_sync, credentials, credentials.get("user", ""), "اختبار الاتصال", "رسالة اختبار من النظام."
        )
        return ValidationResult(ok=ok, message=message or "تم الاتصال")


class RSSConnector(Connector):
    """Read-only signal source for the research agent."""

    key = "rss"
    name = "RSS feeds"
    name_ar = "تغذيات RSS"
    category = "data"
    icon = "☰"
    description_ar = "يقرأ آخر المقالات من أي تغذية RSS لاستخدامها كإشارات بحثية."
    required_credentials = ["feed_url"]
    capabilities = ["read"]

    async def read(self, credentials, config=None) -> list[dict[str, Any]]:
        url = str(credentials.get("feed_url") or "")
        if not url:
            return []
        try:
            from realign.config import settings

            if settings.effective_ssrf_strict:
                from realign.core.netsafe import safe_get

                resp = await safe_get(url, headers={"User-Agent": "ReAlignBot/1.0"})
            else:
                async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                    resp = await client.get(url, headers={"User-Agent": "ReAlignBot/1.0"})
            if len(resp.content) > 2 * 1024 * 1024:
                return [{"error": "حجم التغذية أكبر من المسموح"}]
            # defusedxml: no external entity resolution, no billion-laughs.
            from defusedxml.ElementTree import fromstring as safe_fromstring

            root = safe_fromstring(resp.content)
        except Exception as exc:  # noqa: BLE001
            return [{"error": str(exc)[:200]}]

        items: list[dict[str, Any]] = []
        for node in root.iter():
            tag = node.tag.split("}")[-1]
            if tag != "item":
                continue
            entry: dict[str, Any] = {}
            for child in node:
                ctag = child.tag.split("}")[-1]
                if ctag in ("title", "link", "description", "pubDate"):
                    entry[ctag] = (child.text or "").strip()[:600]
            if entry:
                items.append(entry)
            if len(items) >= 20:
                break
        return items
