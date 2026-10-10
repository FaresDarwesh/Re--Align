"""Social + commerce connectors.

These require real developer credentials (Meta app review, LinkedIn partner
access, WhatsApp Cloud, Shopify admin token). The code paths are complete and
correct, but they stay `not_configured` until the tenant supplies keys — the UI
says so explicitly instead of pretending.
"""

from __future__ import annotations

from typing import Any

import httpx

from realign.connectors.base import Connector, PublishResult, SendResult, ValidationResult

GRAPH = "https://graph.facebook.com/v23.0"


class InstagramConnector(Connector):
    key = "instagram"
    name = "Instagram"
    name_ar = "إنستجرام"
    category = "social"
    icon = "◉"
    description_ar = "ينشر الصور والريلز على حساب أعمال إنستجرام عبر Graph API."
    required_credentials = ["access_token", "ig_business_account_id"]
    capabilities = ["publish"]
    docs_url = "https://developers.facebook.com/docs/instagram-platform"

    async def validate(self, credentials, config=None) -> ValidationResult:
        base = super().validate(credentials, config)
        if not base.ok:
            return base
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{GRAPH}/{credentials['ig_business_account_id']}",
                    params={"fields": "id,username", "access_token": credentials["access_token"]},
                )
            data = resp.json()
            if "error" in data:
                return ValidationResult(ok=False, message=data["error"].get("message", "خطأ من Meta"))
            return ValidationResult(ok=True, message=f"@{data.get('username', '?')}", details=data)
        except Exception as exc:  # noqa: BLE001
            return ValidationResult(ok=False, message=str(exc))

    async def publish(self, credentials, asset, config=None) -> PublishResult:
        token = credentials["access_token"]
        ig_id = credentials["ig_business_account_id"]
        body = getattr(asset, "body", "") or ""
        image_url = (getattr(asset, "meta", {}) or {}).get("image_url") or (credentials.get("default_image_url") or "")
        try:
            async with httpx.AsyncClient(timeout=40) as client:
                if not image_url:
                    return PublishResult(ok=False, message="إنستجرام يتطلب صورة: أضف image_url في بيانات الأصل.")
                container = await client.post(
                    f"{GRAPH}/{ig_id}/media",
                    data={"image_url": image_url, "caption": body[:2200], "access_token": token},
                )
                cdata = container.json()
                if "error" in cdata:
                    return PublishResult(ok=False, message=cdata["error"].get("message", "خطأ"), raw=cdata)
                creation_id = cdata.get("id")
                publish = await client.post(
                    f"{GRAPH}/{ig_id}/media_publish",
                    data={"creation_id": creation_id, "access_token": token},
                )
                pdata = publish.json()
                if "error" in pdata:
                    return PublishResult(ok=False, message=pdata["error"].get("message", "خطأ"), raw=pdata)
                return PublishResult(ok=True, external_id=str(pdata.get("id", "")), message="تم النشر", raw=pdata)
        except Exception as exc:  # noqa: BLE001
            return PublishResult(ok=False, message=str(exc))


class FacebookConnector(Connector):
    key = "facebook"
    name = "Facebook Pages"
    name_ar = "صفحات فيسبوك"
    category = "social"
    icon = "f"
    description_ar = "ينشر على صفحة فيسبوك."
    required_credentials = ["access_token", "page_id"]
    capabilities = ["publish"]

    async def publish(self, credentials, asset, config=None) -> PublishResult:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{GRAPH}/{credentials['page_id']}/feed",
                    data={
                        "message": getattr(asset, "body", ""),
                        "access_token": credentials["access_token"],
                    },
                )
            data = resp.json()
            if "error" in data:
                return PublishResult(ok=False, message=data["error"].get("message", "خطأ"), raw=data)
            return PublishResult(ok=True, external_id=str(data.get("id", "")), message="تم النشر", raw=data)
        except Exception as exc:  # noqa: BLE001
            return PublishResult(ok=False, message=str(exc))


class LinkedInConnector(Connector):
    key = "linkedin"
    name = "LinkedIn"
    name_ar = "لينكدإن"
    category = "social"
    icon = "in"
    description_ar = "ينشر منشورات نصية على صفحة أو حساب لينكدإن."
    required_credentials = ["access_token", "author_urn"]
    capabilities = ["publish"]
    docs_url = "https://learn.microsoft.com/en-us/linkedin/marketing/"

    async def publish(self, credentials, asset, config=None) -> PublishResult:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.linkedin.com/v2/ugcPosts",
                    headers={
                        "Authorization": f"Bearer {credentials['access_token']}",
                        "X-Restli-Protocol-Version": "2.0.0",
                        "Content-Type": "application/json",
                    },
                    json={
                        "author": credentials["author_urn"],
                        "lifecycleState": "PUBLISHED",
                        "specificContent": {
                            "com.linkedin.ugc.ShareContent": {
                                "shareCommentary": {"text": (getattr(asset, "body", "") or "")[:3000]},
                                "shareMediaCategory": "NONE",
                            }
                        },
                        "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
                    },
                )
            if resp.status_code >= 300:
                return PublishResult(ok=False, message=f"HTTP {resp.status_code}: {resp.text[:300]}")
            external_id = resp.headers.get("x-restli-id", "")
            return PublishResult(ok=True, external_id=external_id, message="تم النشر", raw=resp.json() if resp.content else {})
        except Exception as exc:  # noqa: BLE001
            return PublishResult(ok=False, message=str(exc))


class XConnector(Connector):
    key = "x"
    name = "X (Twitter)"
    name_ar = "إكس (تويتر)"
    category = "social"
    icon = "𝕏"
    description_ar = "ينشر تغريدة عبر واجهة X v2."
    required_credentials = ["bearer_token"]
    capabilities = ["publish"]

    async def publish(self, credentials, asset, config=None) -> PublishResult:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.x.com/2/tweets",
                    headers={"Authorization": f"Bearer {credentials['bearer_token']}"},
                    json={"text": (getattr(asset, "body", "") or "")[:280]},
                )
            data = resp.json()
            if "errors" in data or "detail" in data:
                return PublishResult(ok=False, message=str(data)[:300], raw=data)
            return PublishResult(ok=True, external_id=str(data.get("data", {}).get("id", "")), message="تم النشر", raw=data)
        except Exception as exc:  # noqa: BLE001
            return PublishResult(ok=False, message=str(exc))


class WhatsAppConnector(Connector):
    key = "whatsapp"
    name = "WhatsApp Cloud"
    name_ar = "واتساب (Cloud API)"
    category = "messaging"
    icon = "☎"
    description_ar = "يرسل رسائل واتساب عبر واجهة Meta Cloud الرسمية."
    required_credentials = ["token", "phone_number_id"]
    capabilities = ["message", "publish"]
    docs_url = "https://developers.facebook.com/docs/whatsapp/cloud-api"

    async def send(self, credentials, *, to="", subject="", body="", config=None) -> SendResult:
        text = f"{subject}\n\n{body}" if subject else body
        try:
            async with httpx.AsyncClient(timeout=25) as client:
                resp = await client.post(
                    f"{GRAPH}/{credentials['phone_number_id']}/messages",
                    headers={"Authorization": f"Bearer {credentials['token']}"},
                    json={
                        "messaging_product": "whatsapp",
                        "to": to,
                        "type": "text",
                        "text": {"preview_url": False, "body": text[:4000]},
                    },
                )
            data = resp.json()
            if "error" in data:
                return SendResult(ok=False, message=data["error"].get("message", "خطأ"), raw=data)
            return SendResult(ok=True, external_id=str(data.get("messages", [{}])[0].get("id", "")), message="تم الإرسال", raw=data)
        except Exception as exc:  # noqa: BLE001
            return SendResult(ok=False, message=str(exc))


class ShopifyConnector(Connector):
    key = "shopify"
    name = "Shopify"
    name_ar = "شوبيفاي"
    category = "commerce"
    icon = "🛍"
    description_ar = "يقرأ المنتجات والطلبات من متجر شوبيفاي لتغذية وكلاء المبيعات والتحليل."
    required_credentials = ["store_domain", "access_token"]
    capabilities = ["read"]

    async def read(self, credentials, config=None) -> list[dict[str, Any]]:
        domain = (credentials.get("store_domain") or "").replace("https://", "").strip("/")
        if not domain:
            return []
        url = f"https://{domain}/admin/api/2024-10/{config.get('resource', 'products') if config else 'products'}.json?limit=20"
        try:
            async with httpx.AsyncClient(timeout=25) as client:
                resp = await client.get(url, headers={"X-Shopify-Access-Token": credentials.get("access_token", "")})
            data = resp.json()
            key = (config.get("resource", "products") if config else "products")
            return list(data.get(key, []))
        except Exception as exc:  # noqa: BLE001
            return [{"error": str(exc)}]


class GoogleAnalyticsConnector(Connector):
    key = "google_analytics"
    name = "Google Analytics 4"
    name_ar = "جوجل أناليتكس 4"
    category = "data"
    icon = "◧"
    description_ar = "يجلب مقاييس الأداء لصفحتك لتغذية وكيل التحليلات."
    required_credentials = ["access_token", "property_id"]
    capabilities = ["read"]
    docs_url = "https://developers.google.com/analytics/devguides/reporting/data/v1"

    async def read(self, credentials, config=None) -> list[dict[str, Any]]:
        try:
            async with httpx.AsyncClient(timeout=25) as client:
                resp = await client.post(
                    f"https://analyticsdata.googleapis.com/v1beta/properties/{credentials['property_id']}:runReport",
                    headers={"Authorization": f"Bearer {credentials['access_token']}"},
                    json={
                        "dateRanges": [{"startDate": "28daysAgo", "endDate": "today"}],
                        "dimensions": [{"name": "pagePath"}],
                        "metrics": [{"name": "sessions"}, {"name": "conversions"}],
                        "limit": 20,
                    },
                )
            if resp.status_code >= 300:
                return [{"error": f"HTTP {resp.status_code}: {resp.text[:300]}"}]
            rows = resp.json().get("rows", [])
            out = []
            for r in rows:
                dims = [d.get("value", "") for d in r.get("dimensionValues", [])]
                mets = [m.get("value", "") for m in r.get("metricValues", [])]
                out.append({"page": dims[0] if dims else "", "sessions": mets[0] if mets else "0", "conversions": mets[1] if len(mets) > 1 else "0"})
            return out
        except Exception as exc:  # noqa: BLE001
            return [{"error": str(exc)}]
