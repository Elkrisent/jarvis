"""Project endpoints (SPEC §10)."""

from __future__ import annotations

from fastapi import APIRouter, Response

from jarvis_api.db.repositories import NotFound
from jarvis_api.http.deps import ServicesDep
from jarvis_api.http.models import ProjectIn, ProjectOut, ProjectPatch

router = APIRouter(tags=["projects"])


@router.get("/projects")
async def list_projects(services: ServicesDep) -> list[ProjectOut]:
    return [ProjectOut.of(r) for r in await services.projects.list()]


@router.post("/projects", status_code=201)
async def create_project(body: ProjectIn, services: ServicesDep) -> ProjectOut:
    return ProjectOut.of(await services.projects.create(body.name, body.slug))


@router.get("/projects/{project_id}")
async def get_project(project_id: str, services: ServicesDep) -> ProjectOut:
    record = await services.projects.get(project_id)
    if record is None:
        raise NotFound(f"project not found: {project_id}")
    return ProjectOut.of(record)


@router.patch("/projects/{project_id}")
async def update_project(project_id: str, body: ProjectPatch, services: ServicesDep) -> ProjectOut:
    record = await services.projects.update(project_id, name=body.name, slug=body.slug)
    return ProjectOut.of(record)


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(project_id: str, services: ServicesDep) -> Response:
    """Delete a project with its keys, APIs, versions and the APIs' sealed secrets."""
    await services.projects.delete(project_id)
    return Response(status_code=204)
