"""Project, key and API repositories against a real Postgres."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime

import pytest
from jarvis_api.db.config_source import PostgresConfigSource
from jarvis_api.db.ids import new_id
from jarvis_api.db.migrate import to_async_url, upgrade
from jarvis_api.db.repositories import (
    Conflict,
    KeyRepository,
    NotFound,
    ProjectRepository,
)
from jarvis_api.db.secrets import PostgresSecretStore
from jarvis_core.config import ApiDependency
from jarvis_core.keys import generate_jarvis_key
from jarvis_core.secretbox import AesGcmSecretBox
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

pytestmark = pytest.mark.integration


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    await upgrade(database_url)
    engine = create_async_engine(to_async_url(database_url), pool_size=15)
    try:
        yield engine
    finally:
        await engine.dispose()


def _api(slug: str = "weather", deadline_ms: int = 5000, name: str = "Weather") -> ApiDependency:
    return ApiDependency(
        slug=slug, name=name, base_url="https://api.weather.example", deadline_ms=deadline_ms
    )


class TestProjects:
    async def test_round_trip(self, engine: AsyncEngine) -> None:
        repo = ProjectRepository(engine)
        created = await repo.create("Alpha", "alpha")
        assert created.id.startswith("prj_")
        assert isinstance(created.created_at, datetime)
        assert await repo.get(created.id) == created
        assert await repo.list() == [created]
        assert await repo.update(created.id, name=None, slug=None) == created
        updated = await repo.update(created.id, name="Beta", slug=None)
        assert (updated.name, updated.slug) == ("Beta", "alpha")
        updated = await repo.update(created.id, name=None, slug="beta")
        assert (updated.name, updated.slug) == ("Beta", "beta")
        await repo.delete(created.id)
        assert await repo.get(created.id) is None
        assert await repo.list() == []

    async def test_duplicate_slug_conflicts(self, engine: AsyncEngine) -> None:
        repo = ProjectRepository(engine)
        await repo.create("A", "a")
        other = await repo.create("B", "b")
        with pytest.raises(Conflict):
            await repo.create("A2", "a")
        with pytest.raises(Conflict):
            await repo.update(other.id, name=None, slug="a")

    async def test_unknown_id(self, engine: AsyncEngine) -> None:
        repo = ProjectRepository(engine)
        with pytest.raises(NotFound):
            await repo.delete("prj_missing")
        with pytest.raises(NotFound):
            await repo.update("prj_missing", name="x", slug=None)

    async def test_delete_cascades_and_removes_api_secrets(self, engine: AsyncEngine) -> None:
        repo = ProjectRepository(engine)
        project = await repo.create("A", "a")
        secret_id = await PostgresSecretStore(engine).add(AesGcmSecretBox(bytes(32)), "s3cret")
        api = ApiDependency.model_validate(
            {
                **_api().model_dump(mode="json"),
                "credential": {"mode": "bearer", "secret_ref": secret_id},
            }
        )
        await PostgresConfigSource(engine).put_version(project.id, api)
        await KeyRepository(engine).create(project.id, "k", [], generate_jarvis_key())
        await repo.delete(project.id)
        async with engine.connect() as conn:
            for table in ("api_dependencies", "api_policy_versions", "jarvis_keys", "secrets"):
                count = (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
                assert count == 0, table


class TestKeys:
    async def test_stores_prefix_and_hash_but_never_the_full_key(self, engine: AsyncEngine) -> None:
        project = await ProjectRepository(engine).create("A", "a")
        generated = generate_jarvis_key()
        record = await KeyRepository(engine).create(project.id, "ci", [], generated)
        assert record.prefix == generated.prefix
        assert record.revoked_at is None
        assert record.allowed_api_ids == ()
        async with engine.connect() as conn:
            row = (await conn.execute(text("SELECT * FROM jarvis_keys"))).mappings().one()
            assert row["hash"] == generated.hash
            assert row["prefix"] == generated.prefix
            assert all(generated.full not in str(v) for v in row.values())
            dump = await conn.execute(text("SELECT row_to_json(k)::text FROM jarvis_keys k"))
            assert generated.full not in dump.scalar_one()

    async def test_allowed_apis_must_belong_to_the_project(self, engine: AsyncEngine) -> None:
        projects = ProjectRepository(engine)
        one = await projects.create("One", "one")
        two = await projects.create("Two", "two")
        source = PostgresConfigSource(engine)
        own_api, _ = await source.put_version(one.id, _api())
        foreign_api, _ = await source.put_version(two.id, _api())
        keys = KeyRepository(engine)
        ok = await keys.create(one.id, "k", [own_api], generate_jarvis_key())
        assert ok.allowed_api_ids == (own_api,)
        with pytest.raises(ValueError, match="allowed_api_ids must belong to the project"):
            await keys.create(one.id, "k2", [foreign_api], generate_jarvis_key())
        with pytest.raises(ValueError, match="allowed_api_ids must belong to the project"):
            await keys.create(one.id, "k3", ["api_missing"], generate_jarvis_key())

    async def test_unknown_project(self, engine: AsyncEngine) -> None:
        with pytest.raises(NotFound):
            await KeyRepository(engine).create("prj_missing", "k", [], generate_jarvis_key())

    async def test_list_is_scoped_to_project(self, engine: AsyncEngine) -> None:
        projects = ProjectRepository(engine)
        one = await projects.create("One", "one")
        two = await projects.create("Two", "two")
        keys = KeyRepository(engine)
        k1 = await keys.create(one.id, "k1", [], generate_jarvis_key())
        await keys.create(two.id, "k2", [], generate_jarvis_key())
        assert await keys.list(one.id) == [k1]

    async def test_revoke_is_idempotent(self, engine: AsyncEngine) -> None:
        project = await ProjectRepository(engine).create("A", "a")
        keys = KeyRepository(engine)
        record = await keys.create(project.id, "k", [], generate_jarvis_key())
        first = await keys.revoke(record.id)
        assert first.revoked_at is not None
        second = await keys.revoke(record.id)
        assert second.revoked_at == first.revoked_at
        with pytest.raises(NotFound):
            await keys.revoke("key_missing")


class TestApis:
    @pytest.fixture
    async def project_id(self, engine: AsyncEngine) -> str:
        return (await ProjectRepository(engine).create("A", "a")).id

    @pytest.mark.req("REQ-5.2-1")
    async def test_versions_and_diffs(self, engine: AsyncEngine, project_id: str) -> None:
        source = PostgresConfigSource(engine)
        api_id, v1 = await source.put_version(project_id, _api(), author_id=None, reason="first")
        _, v2 = await source.put_version(project_id, _api(deadline_ms=4000), reason="tighter")
        assert (v1, v2) == (1, 2)
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT version, diff_json, author_id, reason FROM api_policy_versions"
                        " WHERE api_id = :a ORDER BY version"
                    ),
                    {"a": api_id},
                )
            ).all()
        assert [r.diff_json for r in rows] == [
            {"created": True},
            {"changed": {"deadline_ms": {"from": 5000, "to": 4000}}},
        ]
        assert [r.reason for r in rows] == ["first", "tighter"]

    async def test_author_persists(self, engine: AsyncEngine, project_id: str) -> None:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role)"
                    " VALUES ('usr_1', 'a@b.c', 'x', 'admin')"
                )
            )
        api_id, _ = await PostgresConfigSource(engine).put_version(
            project_id, _api(), author_id="usr_1"
        )
        async with engine.connect() as conn:
            author = (
                await conn.execute(
                    text("SELECT author_id FROM api_policy_versions WHERE api_id = :a"),
                    {"a": api_id},
                )
            ).scalar_one()
        assert author == "usr_1"

    async def test_diff_excludes_secret_refs(self, engine: AsyncEngine, project_id: str) -> None:
        source = PostgresConfigSource(engine)

        def with_secret(ref: str) -> ApiDependency:
            return ApiDependency.model_validate(
                {
                    **_api().model_dump(mode="json"),
                    "credential": {"mode": "bearer", "secret_ref": ref},
                }
            )

        api_id, _ = await source.put_version(project_id, with_secret("sec_1"))
        await source.put_version(project_id, with_secret("sec_2"))
        async with engine.connect() as conn:
            diff = (
                await conn.execute(
                    text("SELECT diff_json FROM api_policy_versions WHERE api_id=:a AND version=2"),
                    {"a": api_id},
                )
            ).scalar_one()
        assert diff == {"changed": {}}

    @pytest.mark.req("REQ-5.2-1")
    async def test_ten_concurrent_put_versions_get_distinct_versions(
        self, engine: AsyncEngine, project_id: str
    ) -> None:
        source = PostgresConfigSource(engine)
        results = await asyncio.gather(
            *(
                source.put_version(project_id, _api(deadline_ms=3000 + i), reason=str(i))
                for i in range(10)
            )
        )
        assert len({api_id for api_id, _ in results}) == 1
        assert sorted(v for _, v in results) == list(range(1, 11))

    async def test_get_by_id_and_list_stored(self, engine: AsyncEngine, project_id: str) -> None:
        source = PostgresConfigSource(engine)
        assert await source.get_by_id("api_missing") is None
        api_id, _ = await source.put_version(project_id, _api())
        await source.put_version(project_id, _api(deadline_ms=4000))
        await source.put_version(project_id, _api("zeta"))
        stored = await source.get_by_id(api_id)
        assert stored is not None
        assert (stored.id, stored.project_id, stored.version) == (api_id, project_id, 2)
        assert stored.config.deadline_ms == 4000
        assert isinstance(stored.created_at, datetime)
        assert [s.config.slug for s in await source.list_stored(project_id)] == ["weather", "zeta"]

    async def test_update_by_id_can_change_slug_and_conflicts(
        self, engine: AsyncEngine, project_id: str
    ) -> None:
        source = PostgresConfigSource(engine)
        api_id, _ = await source.put_version(project_id, _api())
        await source.put_version(project_id, _api("other"))
        renamed, version = await source.put_version(
            project_id, _api("renamed", name="Renamed"), api_id=api_id
        )
        assert (renamed, version) == (api_id, 2)
        assert await source.get(project_id, "weather") is None
        got = await source.get(project_id, "renamed")
        assert got is not None
        assert got.name == "Renamed"
        with pytest.raises(Conflict):
            await source.put_version(project_id, _api("other"), api_id=api_id)
        with pytest.raises(NotFound):
            await source.put_version(project_id, _api(), api_id="api_missing")
        # a failed update leaves no new version behind
        stored = await source.get_by_id(api_id)
        assert stored is not None
        assert stored.version == 2

    async def test_unknown_author_is_not_reported_as_a_slug_conflict(
        self, engine: AsyncEngine, project_id: str
    ) -> None:
        with pytest.raises(IntegrityError) as caught:
            await PostgresConfigSource(engine).put_version(
                project_id, _api(), author_id="usr_missing"
            )
        assert not isinstance(caught.value, Conflict)

    async def test_real_slug_clash_is_a_conflict_on_rename(
        self, engine: AsyncEngine, project_id: str
    ) -> None:
        source = PostgresConfigSource(engine)
        await source.put_version(project_id, _api("one"))
        other, _ = await source.put_version(project_id, _api("two"))
        with pytest.raises(Conflict, match="slug"):
            await source.put_version(project_id, _api("one"), api_id=other)

    async def test_put_version_unknown_project(self, engine: AsyncEngine) -> None:
        with pytest.raises(LookupError):
            await PostgresConfigSource(engine).put_version("prj_missing", _api())

    async def test_delete(self, engine: AsyncEngine, project_id: str) -> None:
        source = PostgresConfigSource(engine)
        api_id, _ = await source.put_version(project_id, _api())
        await source.delete(api_id)
        assert await source.get_by_id(api_id) is None
        async with engine.connect() as conn:
            left = (
                await conn.execute(text("SELECT count(*) FROM api_policy_versions"))
            ).scalar_one()
        assert left == 0
        with pytest.raises(NotFound):
            await source.delete(api_id)


class TestSecretDelete:
    async def test_removes_row_and_unknown_is_noop(self, engine: AsyncEngine) -> None:
        store = PostgresSecretStore(engine)
        secret_id = await store.add(AesGcmSecretBox(bytes(32)), "x")
        await store.delete(secret_id)
        assert await store.get(secret_id) is None
        await store.delete(secret_id)
        await store.delete(new_id("sec"))


async def test_put_version_unknown_project_is_not_found(engine: AsyncEngine) -> None:
    # Routers rely on the port raising NotFound (not a bare LookupError) for a missing project.
    with pytest.raises(NotFound, match="prj_missing"):
        await PostgresConfigSource(engine).put_version("prj_missing", _api())
