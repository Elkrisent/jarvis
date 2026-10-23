"""The /api/v1 router. Every route requires the admin token."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from jarvis_core.config import Slug
from pydantic import BaseModel, ConfigDict, Field

from jarvis_api.db.repositories import ProjectRecord
from jarvis_api.http.deps import Services, get_services, require_admin

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_admin)])

ServicesDep = Annotated[Services, Depends(get_services)]


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=200)]
    slug: Slug


class ProjectOut(BaseModel):
    id: str
    name: str
    slug: str
    created_at: datetime

    @classmethod
    def of(cls, record: ProjectRecord) -> ProjectOut:
        return cls(id=record.id, name=record.name, slug=record.slug, created_at=record.created_at)


@router.get("/projects")
async def list_projects(services: ServicesDep) -> list[ProjectOut]:
    return [ProjectOut.of(r) for r in await services.projects.list()]


@router.post("/projects", status_code=201)
async def create_project(body: ProjectCreate, services: ServicesDep) -> ProjectOut:
    return ProjectOut.of(await services.projects.create(body.name, body.slug))
