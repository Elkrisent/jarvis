"""``ConfigSource`` backed by Postgres (SPEC §12)."""

from __future__ import annotations

from typing import Any

from jarvis_core.config import ApiDependency
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from jarvis_api.db.ids import new_id
from jarvis_api.db.models import ApiDependencyRow, ApiPolicyVersion, Project


def api_from_config_json(config_json: dict[str, Any]) -> ApiDependency:
    return ApiDependency.model_validate(config_json)


def config_json_from_api(api: ApiDependency) -> dict[str, Any]:
    return api.model_dump(mode="json")


class PostgresConfigSource:
    """Stores each saved ``ApiDependency`` as a new immutable ``api_policy_versions`` row."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    def _current(self) -> Any:
        return (
            select(ApiDependencyRow.slug, ApiPolicyVersion.config_json)
            .join(ApiPolicyVersion, ApiPolicyVersion.id == ApiDependencyRow.current_version_id)
            .order_by(ApiDependencyRow.slug)
        )

    async def get(self, project_id: str, slug: str) -> ApiDependency | None:
        query = self._current().where(
            ApiDependencyRow.project_id == project_id, ApiDependencyRow.slug == slug
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(query)).first()
        return api_from_config_json(row.config_json) if row else None

    async def list(self, project_id: str) -> list[ApiDependency]:
        query = self._current().where(ApiDependencyRow.project_id == project_id)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [api_from_config_json(row.config_json) for row in rows]

    async def put(self, project_id: str, api: ApiDependency) -> int:
        async with self._engine.begin() as conn:
            exists = await conn.execute(select(Project.id).where(Project.id == project_id))
            if exists.first() is None:
                raise LookupError(f"unknown project: {project_id}")

            # The upsert takes a row lock held until commit, which serialises concurrent puts
            # for the same API so each one reads a distinct next version number.
            api_id = (
                await conn.execute(
                    insert(ApiDependencyRow)
                    .values(id=new_id("api"), project_id=project_id, slug=api.slug, name=api.name)
                    .on_conflict_do_update(
                        constraint="uq_api_dependencies_project_slug", set_={"name": api.name}
                    )
                    .returning(ApiDependencyRow.id)
                )
            ).scalar_one()
            version = (
                await conn.execute(
                    select(func.coalesce(func.max(ApiPolicyVersion.version), 0) + 1).where(
                        ApiPolicyVersion.api_id == api_id
                    )
                )
            ).scalar_one()
            version_id = new_id("ver")
            await conn.execute(
                insert(ApiPolicyVersion).values(
                    id=version_id,
                    api_id=api_id,
                    version=version,
                    config_json=config_json_from_api(api),
                )
            )
            await conn.execute(
                update(ApiDependencyRow)
                .where(ApiDependencyRow.id == api_id)
                .values(current_version_id=version_id)
            )
        return version
