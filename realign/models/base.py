"""Shared declarative base + mixins."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from realign.db import Base
from realign.core.utils import new_uuid


def uuid_pk() -> str:
    return new_uuid()


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SoftDeleteMixin:
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)


class StrEnum(enum.StrEnum):
    """Base for portable string enums."""

    @classmethod
    def values(cls) -> list[str]:
        return [m.value for m in cls]


__all__ = ["Base", "TimestampMixin", "SoftDeleteMixin", "StrEnum", "uuid_pk", "ForeignKey", "String", "Mapped", "mapped_column"]
