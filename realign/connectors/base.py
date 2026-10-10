"""Connector contract. Every external system plugs in through this."""

from __future__ import annotations

from abc import ABC
from typing import Any, ClassVar

from pydantic import BaseModel


class PublishResult(BaseModel):
    ok: bool = False
    external_id: str = ""
    url: str = ""
    message: str = ""
    raw: dict[str, Any] = {}


class SendResult(BaseModel):
    ok: bool = False
    external_id: str = ""
    message: str = ""
    raw: dict[str, Any] = {}


class ValidationResult(BaseModel):
    ok: bool = False
    message: str = ""
    details: dict[str, Any] = {}


class Connector(ABC):
    """A channel or system the platform can talk to."""

    key: ClassVar[str] = "base"
    name: ClassVar[str] = "Base"
    name_ar: ClassVar[str] = "أساسي"
    category: ClassVar[str] = "general"          # social | messaging | email | data | commerce | automation
    icon: ClassVar[str] = "◻"
    description_ar: ClassVar[str] = ""
    required_credentials: ClassVar[list[str]] = []
    optional_credentials: ClassVar[list[str]] = []
    capabilities: ClassVar[list[str]] = []        # publish | message | read | schedule
    docs_url: ClassVar[str] = ""

    # ------------------------------------------------------------ generic ---

    @classmethod
    def description_en(cls) -> str:
        """English description, sourced from the catalogue when not on the class."""
        from realign.connectors.i18n_meta import english

        own = getattr(cls, "_description_en", "")
        return own or english(cls.key) or cls.description_ar

    @classmethod
    def meta(cls, *, status: str = "not_configured", configured_fields: list[str] | None = None) -> dict[str, Any]:
        return {
            "key": cls.key,
            "name": cls.name,
            "name_ar": cls.name_ar,
            "category": cls.category,
            "icon": cls.icon,
            "description_ar": cls.description_ar,
            "description_en": cls.description_en(),
            "required_credentials": list(cls.required_credentials),
            "optional_credentials": list(cls.optional_credentials),
            "capabilities": list(cls.capabilities),
            "status": status,
            "configured_fields": configured_fields or [],
            "docs_url": cls.docs_url,
            "missing": [
                f
                for f in cls.required_credentials
                if f not in (configured_fields or [])
            ],
        }

    @classmethod
    def is_configured(cls, credentials: dict[str, Any]) -> bool:
        return all(bool(credentials.get(f)) for f in cls.required_credentials)

    @classmethod
    def configured_fields(cls, credentials: dict[str, Any]) -> list[str]:
        return [f for f in (*cls.required_credentials, *cls.optional_credentials) if credentials.get(f)]

    # ------------------------------------------------------------ actions ---

    async def validate(self, credentials: dict[str, Any], config: dict[str, Any] | None = None) -> ValidationResult:  # noqa: ARG002
        if not self.is_configured(credentials):
            return ValidationResult(
                ok=False,
                message="بيانات الاعتماد غير مكتملة",
                details={"missing": [f for f in self.required_credentials if not credentials.get(f)]},
            )
        return ValidationResult(ok=True, message="الإعداد مكتمل")

    async def publish(self, credentials: dict[str, Any], asset: Any, config: dict[str, Any] | None = None) -> PublishResult:
        raise NotImplementedError(f"النشر غير مدعوم في {self.key}")

    async def send(
        self,
        credentials: dict[str, Any],
        *,
        to: str = "",
        subject: str = "",
        body: str = "",
        config: dict[str, Any] | None = None,
    ) -> SendResult:
        raise NotImplementedError(f"الإرسال غير مدعوم في {self.key}")

    async def read(self, credentials: dict[str, Any], config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError(f"القراءة غير مدعومة في {self.key}")
