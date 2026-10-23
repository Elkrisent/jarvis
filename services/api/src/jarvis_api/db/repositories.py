"""Repositories for projects and JARVIS keys (SPEC §10, §12)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from jarvis_core.keys import GeneratedKey
from sqlalchemy import Row, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from jarvis_api.db.ids import new_id
from jarvis_api.db.models import ApiDependencyRow, ApiPolicyVersion, JarvisKey, Project, Secret


def violated_constraint(exc: IntegrityError) -> str | None:
    """Name of the constraint behind an ``IntegrityError`` (asyncpg exposes it), if known."""
    orig = exc.orig
    for candidate in (orig, getattr(orig, "__cause__", None)):
        name = getattr(candidate, "constraint_name", None)
        if name:
            return str(name)
    return None


PROJECT_SLUG_CONSTRAINT = "projects_slug_key"


class Conflict(Exception):
    """A unique constraint was violated (for example a duplicate slug)."""


class NotFound(LookupError):
    """The addressed row does not exist (a ``LookupError``, so older callers still match)."""


@dataclass(frozen=True)
class ProjectRecord:
    id: str
    name: str
    slug: str
    created_at: datetime


@dataclass(frozen=True)
class KeyRecord:
    id: str
    project_id: str
    name: str
    prefix: str
    allowed_api_ids: tuple[str, ...]
    revoked_at: datetime | None


class ProjectRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create(self, name: str, slug: str) -> ProjectRecord:
        try:
            async with self._engine.begin() as conn:
                row = (
                    await conn.execute(
                        insert(Project)
                        .values(id=new_id("prj"), name=name, slug=slug)
                        .returning(*Project.__table__.c)
                    )
                ).one()
        except IntegrityError as exc:
            if violated_constraint(exc) != PROJECT_SLUG_CONSTRAINT:
                raise
            raise Conflict(f"project slug already exists: {slug}") from None
        return _record(row)

    async def list(self) -> list[ProjectRecord]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(*Project.__table__.c).order_by(Project.created_at, Project.id)
                )
            ).all()
        return [_record(r) for r in rows]

    async def get(self, id: str) -> ProjectRecord | None:
        async with self._engine.connect() as conn:
            row = (await conn.execute(select(*Project.__table__.c).where(Project.id == id))).first()
        return _record(row) if row else None

    async def update(self, id: str, *, name: str | None, slug: str | None) -> ProjectRecord:
        values = {k: v for k, v in (("name", name), ("slug", slug)) if v is not None}
        try:
            async with self._engine.begin() as conn:
                stmt: Any
                if values:
                    stmt = (
                        update(Project)
                        .where(Project.id == id)
                        .values(**values)
                        .returning(*Project.__table__.c)
                    )
                else:
                    stmt = select(*Project.__table__.c).where(Project.id == id)
                row = (await conn.execute(stmt)).first()
        except IntegrityError as exc:
            if violated_constraint(exc) != PROJECT_SLUG_CONSTRAINT:
                raise
            raise Conflict(f"project slug already exists: {slug}") from None
        if row is None:
            raise NotFound(f"project not found: {id}")
        return _record(row)

    async def delete(self, id: str) -> None:
        """Delete a project, its dependent rows (cascade) and its APIs' secrets."""
        async with self._engine.begin() as conn:
            # Unlocked read: a secret_ref added by a concurrent put_version in the narrow window
            # before the delete is not collected and is left orphaned.
            refs: Any = (
                await conn.execute(
                    select(ApiPolicyVersion.config_json["credential"]["secret_ref"].astext)
                    .join(ApiDependencyRow, ApiDependencyRow.id == ApiPolicyVersion.api_id)
                    .where(ApiDependencyRow.project_id == id)
                )
            ).scalars()
            secret_ids = {ref for ref in refs if ref}
            deleted = await conn.execute(delete(Project).where(Project.id == id))
            if deleted.rowcount == 0:
                raise NotFound(f"project not found: {id}")
            if secret_ids:
                await conn.execute(delete(Secret).where(Secret.id.in_(secret_ids)))


def _record(row: Row[Any]) -> ProjectRecord:
    return ProjectRecord(row.id, row.name, row.slug, row.created_at)


class KeyRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @staticmethod
    def _key(row: Row[Any]) -> KeyRecord:
        return KeyRecord(
            id=row.id,
            project_id=row.project_id,
            name=row.name,
            prefix=row.prefix,
            allowed_api_ids=tuple(row.allowed_api_ids),
            revoked_at=row.revoked_at,
        )

    @staticmethod
    def _columns() -> tuple[Any, ...]:
        t = JarvisKey
        return (t.id, t.project_id, t.name, t.prefix, t.allowed_api_ids, t.revoked_at)

    async def create(
        self, project_id: str, name: str, allowed_api_ids: list[str], key: GeneratedKey
    ) -> KeyRecord:
        async with self._engine.begin() as conn:
            if (
                await conn.execute(select(Project.id).where(Project.id == project_id))
            ).first() is None:
                raise NotFound(f"project not found: {project_id}")
            wanted = set(allowed_api_ids)
            if wanted:
                found = (
                    await conn.execute(
                        select(func.count()).where(
                            ApiDependencyRow.project_id == project_id,
                            ApiDependencyRow.id.in_(wanted),
                        )
                    )
                ).scalar_one()
                if found != len(wanted):
                    raise ValueError("allowed_api_ids must belong to the project")
            row = (
                await conn.execute(
                    insert(JarvisKey)
                    .values(
                        id=new_id("key"),
                        project_id=project_id,
                        name=name,
                        prefix=key.prefix,
                        hash=key.hash,
                        allowed_api_ids=allowed_api_ids,
                    )
                    .returning(*self._columns())
                )
            ).one()
        return self._key(row)

    async def list(self, project_id: str) -> list[KeyRecord]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(*self._columns())
                    .where(JarvisKey.project_id == project_id)
                    .order_by(JarvisKey.id)
                )
            ).all()
        return [self._key(r) for r in rows]

    async def revoke(self, key_id: str) -> KeyRecord:
        async with self._engine.begin() as conn:
            await conn.execute(
                update(JarvisKey)
                .where(JarvisKey.id == key_id, JarvisKey.revoked_at.is_(None))
                .values(revoked_at=func.now())
            )
            row = (
                await conn.execute(select(*self._columns()).where(JarvisKey.id == key_id))
            ).first()
        if row is None:
            raise NotFound(f"key not found: {key_id}")
        return self._key(row)
