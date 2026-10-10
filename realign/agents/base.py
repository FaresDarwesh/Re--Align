"""Agent contract + department metadata."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from realign.agents.context import AgentContext


class AgentResult(BaseModel):
    summary: str = ""
    assets: list[str] = Field(default_factory=list)      # ContentAsset ids
    approvals: list[str] = Field(default_factory=list)   # Approval ids
    metrics: dict[str, Any] = Field(default_factory=dict)
    next_actions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class BaseAgent(ABC):
    key: ClassVar[str] = "base"
    name: ClassVar[str] = "Base"
    name_ar: ClassVar[str] = "أساسي"
    name_en: ClassVar[str] = "Base"
    department: ClassVar[str] = "general"
    description_ar: ClassVar[str] = ""
    description_en: ClassVar[str] = ""
    icon: ClassVar[str] = "◆"
    requires_approval: ClassVar[bool] = True
    default_tools: ClassVar[list[str]] = []

    @abstractmethod
    async def run(self, ctx: AgentContext) -> AgentResult:
        ...

    # ------------------------------------------------------------ metadata --

    @classmethod
    def meta(cls) -> dict[str, Any]:
        from realign.agents.i18n_meta import department_en, english

        en = english(cls.key)
        return {
            "key": cls.key,
            "name": cls.name,
            "name_ar": cls.name_ar,
            "name_en": en["name"],
            "department": cls.department,
            "department_ar": DEPARTMENTS_AR.get(cls.department, cls.department),
            "department_en": department_en(cls.department),
            "description_ar": cls.description_ar,
            "description_en": en["description"] or cls.description_ar,
            "icon": cls.icon,
            "requires_approval": cls.requires_approval,
            "tools": list(cls.default_tools),
        }


DEPARTMENTS_AR: dict[str, str] = {
    "research": "أبحاث السوق",
    "content": "المحتوى",
    "creative": "الإبداع",
    "distribution": "التوزيع",
    "audience": "الجمهور والبريد",
    "analytics": "التحليلات",
    "sales": "المبيعات",
    "support": "خدمة العملاء",
    "finance": "المالية",
    "core": "القيادة والتنسيق",
    "general": "عام",
}
