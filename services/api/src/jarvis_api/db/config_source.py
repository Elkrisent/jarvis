"""``ConfigSource`` backed by Postgres (SPEC §12)."""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from jarvis_core.config import ApiDependency
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from jarvis_api.db.ids import new_id
from jarvis_api.db.models import ApiDependencyRow, ApiPolicyVersion, Project
from jarvis_api.db.repositories import Conflict, NotFound, violated_constraint

API_SLUG_CONSTRAINT = "uq_api_dependencies_project_slug"


def api_from_config_json(config_json: dict[str, Any]) -> ApiDependency:
    return ApiDependency.model_validate(config_json)


def config_json_from_api(api: ApiDependency) -> dict[str, Any]:
    return api.model_dump(mode="json")


def _diffable(config: dict[str, Any]) -> dict[str, Any]:
    """The config with secret references removed, so diffs never mention them."""
    credential = {k: v for k, v in config.get("credential", {}).items() if k != "secret_ref"}
    return {**config, "credential": credential}


def diff_configs(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """Top-level changes between two stored configs; ``{"created": True}`` for the first."""
    if old is None:
        return {"created": True}
    before, after = _diffable(old), _diffable(new)
    changed = {
        key: {"from": before.get(key), "to": after.get(key)}
        for key in sorted(before.keys() | after.keys())
        if before.get(key) != after.get(key)
    }
    return {"changed": changed}


@dataclass(frozen=True)
class StoredApi:
    id: str
    project_id: str
    version: int
    config: ApiDependency
    created_at: datetime


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
        _, version = await self.put_version(project_id, api)
        return version

    async def put_version(
        self,
        project_id: str,
        api: ApiDependency,
        *,
        author_id: str | None = None,
        reason: str | None = None,
        api_id: str | None = None,
    ) -> tuple[str, int]:
        """Store ``api`` as the next immutable version; return ``(api_id, version)``.

        Without ``api_id`` the API is upserted by slug. With it, that API's slug and name are
        updated (``Conflict`` on a slug clash, ``NotFound`` for an unknown API). An unknown project
        also raises ``NotFound``, so routers map every missing parent to 404.
        """
        try:
            async with self._engine.begin() as conn:
                exists = await conn.execute(select(Project.id).where(Project.id == project_id))
                if exists.first() is None:
                    raise NotFound(f"unknown project: {project_id}")

                # The upsert/update takes a row lock held until commit, which serialises
                # concurrent writes for the same API so each reads a distinct next version.
                if api_id is None:
                    api_id = (
                        await conn.execute(
                            insert(ApiDependencyRow)
                            .values(
                                id=new_id("api"),
                                project_id=project_id,
                                slug=api.slug,
                                name=api.name,
                            )
                            .on_conflict_do_update(
                                constraint="uq_api_dependencies_project_slug",
                                set_={"name": api.name},
                            )
                            .returning(ApiDependencyRow.id)
                        )
                    ).scalar_one()
                else:
                    updated = (
                        await conn.execute(
                            update(ApiDependencyRow)
                            .where(
                                ApiDependencyRow.id == api_id,
                                ApiDependencyRow.project_id == project_id,
                            )
                            .values(slug=api.slug, name=api.name)
                            .returning(ApiDependencyRow.id)
                        )
                    ).first()
                    if updated is None:
                        raise NotFound(f"api not found: {api_id}")
                previous = (
                    await conn.execute(
                        select(ApiPolicyVersion.config_json)
                        .where(ApiPolicyVersion.api_id == api_id)
                        .order_by(ApiPolicyVersion.version.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                version = (
                    await conn.execute(
                        select(func.coalesce(func.max(ApiPolicyVersion.version), 0) + 1).where(
                            ApiPolicyVersion.api_id == api_id
                        )
                    )
                ).scalar_one()
                config_json = config_json_from_api(api)
                version_id = new_id("ver")
                await conn.execute(
                    insert(ApiPolicyVersion).values(
                        id=version_id,
                        api_id=api_id,
                        version=version,
                        config_json=config_json,
                        diff_json=diff_configs(previous, config_json),
                        author_id=author_id,
                        reason=reason,
                    )
                )
                await conn.execute(
                    update(ApiDependencyRow)
                    .where(ApiDependencyRow.id == api_id)
                    .values(current_version_id=version_id)
                )
        except IntegrityError as exc:
            if violated_constraint(exc) != API_SLUG_CONSTRAINT:
                raise
            raise Conflict(f"api slug already exists in project: {api.slug}") from None
        return api_id, version

    def _stored(self) -> Any:
        return select(
            ApiDependencyRow.id,
            ApiDependencyRow.project_id,
            ApiPolicyVersion.version,
            ApiPolicyVersion.config_json,
            ApiPolicyVersion.created_at,
        ).join(ApiPolicyVersion, ApiPolicyVersion.id == ApiDependencyRow.current_version_id)

    @staticmethod
    def _to_stored(row: Any) -> StoredApi:
        return StoredApi(
            id=row.id,
            project_id=row.project_id,
            version=row.version,
            config=api_from_config_json(row.config_json),
            created_at=row.created_at,
        )

    async def get_by_id(self, api_id: str) -> StoredApi | None:
        async with self._engine.connect() as conn:
            row = (await conn.execute(self._stored().where(ApiDependencyRow.id == api_id))).first()
        return self._to_stored(row) if row else None

    async def list_stored(self, project_id: str) -> builtins.list[StoredApi]:
        query = (
            self._stored()
            .where(ApiDependencyRow.project_id == project_id)
            .order_by(ApiDependencyRow.slug)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [self._to_stored(r) for r in rows]

    async def delete(self, api_id: str) -> None:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                delete(ApiDependencyRow).where(ApiDependencyRow.id == api_id)
            )
        if result.rowcount == 0:
            raise NotFound(f"api not found: {api_id}")
