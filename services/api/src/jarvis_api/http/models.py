"""Request and response models of /api/v1. Every input model forbids unknown fields."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from jarvis_core.config import Slug
from pydantic import BaseModel, ConfigDict, Field

from jarvis_api.db.repositories import KeyRecord, ProjectRecord

Name = Annotated[str, Field(min_length=1, max_length=200)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectIn(_In):
    name: Name
    slug: Slug


class ProjectPatch(_In):
    name: Name | None = None
    slug: Slug | None = None


class ProjectOut(BaseModel):
    id: str
    name: str
    slug: str
    created_at: datetime

    @classmethod
    def of(cls, record: ProjectRecord) -> ProjectOut:
        return cls(id=record.id, name=record.name, slug=record.slug, created_at=record.created_at)


class KeyIn(_In):
    name: Name
    allowed_api_ids: list[str] = []


class KeyOut(BaseModel):
    """A JARVIS key as listed: the display prefix only, never the key or its hash."""

    id: str
    project_id: str
    name: str
    prefix: str
    allowed_api_ids: list[str]
    revoked_at: datetime | None

    @classmethod
    def fields_of(cls, record: KeyRecord) -> dict[str, object]:
        return {
            "id": record.id,
            "project_id": record.project_id,
            "name": record.name,
            "prefix": record.prefix,
            "allowed_api_ids": list(record.allowed_api_ids),
            "revoked_at": record.revoked_at,
        }

    @classmethod
    def of(cls, record: KeyRecord) -> KeyOut:
        return cls.model_validate(cls.fields_of(record))


class KeyCreated(KeyOut):
    """The creation response: the only place the full key is ever returned."""

    key: str
