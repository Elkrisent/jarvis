"""Request and response models of /api/v1. Every input model forbids unknown fields."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from jarvis_core.config import Slug
from pydantic import BaseModel, ConfigDict, Field

from jarvis_api.db.repositories import ProjectRecord

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
