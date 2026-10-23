"""The /api/v1 management API end to end against a real Postgres and Redis."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fakes import ADMIN_TOKEN, AUTH
from jarvis_api.app import create_app
from jarvis_api.db.config_source import PostgresConfigSource
from jarvis_api.db.migrate import to_async_url, upgrade
from jarvis_api.db.repositories import KeyRepository, ProjectRepository
from jarvis_api.db.secrets import PostgresSecretStore
from jarvis_api.events import CONFIG_CHANGED_CHANNEL, RedisConfigPublisher
from jarvis_api.http.deps import Services
from jarvis_core.keys import hash_jarvis_key
from jarvis_core.secretbox import AesGcmSecretBox
from redis import asyncio as redis_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

pytestmark = pytest.mark.integration

SECRET = "SENTINEL-SECRET-1"
CLOSED_REDIS = "redis://127.0.0.1:1/0"


def _redis_url() -> str:
    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.fail("REDIS_URL must point at a Redis server to run integration tests")
    return url


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    await upgrade(database_url)
    engine = create_async_engine(to_async_url(database_url))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def box() -> AesGcmSecretBox:
    return AesGcmSecretBox(bytes(range(32)))


async def _client(
    engine: AsyncEngine, box: AesGcmSecretBox, redis_url: str
) -> tuple[httpx.AsyncClient, RedisConfigPublisher]:
    publisher = RedisConfigPublisher(redis_url)
    services = Services(
        projects=ProjectRepository(engine),
        keys=KeyRepository(engine),
        apis=PostgresConfigSource(engine),
        secrets=PostgresSecretStore(engine),
        secret_box=box,
        publisher=publisher,
    )
    app = create_app(services=services, admin_token=ADMIN_TOKEN)
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test", headers=AUTH)
    return client, publisher


@pytest.fixture
async def client(engine: AsyncEngine, box: AesGcmSecretBox) -> AsyncIterator[httpx.AsyncClient]:
    client, publisher = await _client(engine, box, _redis_url())
    try:
        yield client
    finally:
        await client.aclose()
        await publisher.aclose()


@pytest.fixture
async def subscriber() -> AsyncIterator[Any]:
    redis = redis_asyncio.from_url(_redis_url())
    pubsub = redis.pubsub()
    await pubsub.subscribe(CONFIG_CHANGED_CHANNEL)
    confirmation = await pubsub.get_message(timeout=5)
    assert confirmation is not None
    try:
        yield pubsub
    finally:
        await pubsub.aclose()
        await redis.aclose()


async def _next_event(pubsub: Any) -> dict[str, Any]:
    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=5)
    assert message is not None
    event: dict[str, Any] = json.loads(message["data"])
    return event


async def _count(engine: AsyncEngine, table: str) -> int:
    async with engine.connect() as conn:
        count: int = (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
    return count


async def _secret_ids(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        return list((await conn.execute(text("SELECT id FROM secrets"))).scalars())


async def _project(client: httpx.AsyncClient, slug: str = "alpha") -> str:
    response = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert response.status_code == 201, response.text
    pid: str = response.json()["id"]
    return pid


def _api_body(slug: str = "weather", secret: str | None = SECRET) -> dict[str, Any]:
    body: dict[str, Any] = {
        "slug": slug,
        "name": slug.title(),
        "base_url": "https://api.weather.example",
    }
    if secret is not None:
        body["credential"] = {"mode": "bearer", "secret": secret}
    return body


async def _api(client: httpx.AsyncClient, pid: str, **kwargs: Any) -> dict[str, Any]:
    response = await client.post(f"/api/v1/projects/{pid}/apis", json=_api_body(**kwargs))
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


@pytest.mark.req("REQ-5.2-1")
async def test_api_lifecycle_versions_secrets_and_events(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    box: AesGcmSecretBox,
    subscriber: Any,
) -> None:
    pid = await _project(client)
    created = await _api(client, pid)
    api_id = created["id"]
    assert await _next_event(subscriber) == {"api_id": api_id, "version": 1}

    got = await client.get(f"/api/v1/apis/{api_id}")
    assert got.json()["credential"]["has_secret"] is True
    assert SECRET not in got.text
    assert "secret_ref" not in got.text
    [secret_id] = await _secret_ids(engine)
    assert await PostgresSecretStore(engine).reveal(box, secret_id) == SECRET

    patched = await client.patch(
        f"/api/v1/apis/{api_id}", json={"deadline_ms": 9000, "reason": "slow"}
    )
    assert patched.status_code == 200
    assert patched.json()["version"] == 2
    assert await _next_event(subscriber) == {"api_id": api_id, "version": 2}
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT diff_json, author_id, reason FROM api_policy_versions "
                    "WHERE api_id = :id AND version = 2"
                ),
                {"id": api_id},
            )
        ).one()
    assert row.diff_json == {"changed": {"deadline_ms": {"from": 5000, "to": 9000}}}
    assert (row.author_id, row.reason) == (None, "slow")
    assert await _secret_ids(engine) == [secret_id]

    replaced = await client.patch(
        f"/api/v1/apis/{api_id}", json={"credential": {"secret": "SENTINEL-SECRET-2"}}
    )
    assert replaced.status_code == 200
    [new_id] = await _secret_ids(engine)
    assert new_id != secret_id
    assert await PostgresSecretStore(engine).reveal(box, new_id) == "SENTINEL-SECRET-2"
    assert await _next_event(subscriber) == {"api_id": api_id, "version": 3}

    cleared = await client.patch(f"/api/v1/apis/{api_id}", json={"credential": {"mode": "none"}})
    assert cleared.status_code == 200
    assert cleared.json()["credential"]["has_secret"] is False
    assert await _secret_ids(engine) == []

    await client.patch(
        f"/api/v1/apis/{api_id}", json={"credential": {"mode": "bearer", "secret": SECRET}}
    )
    assert len(await _secret_ids(engine)) == 1
    deleted = await client.delete(f"/api/v1/apis/{api_id}")
    assert deleted.status_code == 204
    assert await _secret_ids(engine) == []
    assert await _count(engine, "api_policy_versions") == 0
    assert (await client.get(f"/api/v1/apis/{api_id}")).status_code == 404


@pytest.mark.req("REQ-11.2-4")
async def test_a_failed_save_leaves_no_orphan_secret(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    pid = await _project(client)
    await _api(client, pid, slug="weather")
    maps = await _api(client, pid, slug="maps")
    before = sorted(await _secret_ids(engine))
    clash = await client.patch(
        f"/api/v1/apis/{maps['id']}", json={"slug": "weather", "credential": {"secret": "x"}}
    )
    assert clash.status_code == 409
    assert sorted(await _secret_ids(engine)) == before
    duplicate = await client.post(f"/api/v1/projects/{pid}/apis", json=_api_body("weather"))
    assert duplicate.status_code == 409
    assert sorted(await _secret_ids(engine)) == before


@pytest.mark.req("REQ-10-1")
async def test_deleting_a_project_removes_everything(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    pid = await _project(client)
    api = await _api(client, pid)
    await _api(client, pid, slug="maps")
    key = await client.post(
        f"/api/v1/projects/{pid}/keys", json={"name": "ci", "allowed_api_ids": [api["id"]]}
    )
    assert key.status_code == 201
    response = await client.delete(f"/api/v1/projects/{pid}")
    assert response.status_code == 204
    for table in ("projects", "jarvis_keys", "api_dependencies", "api_policy_versions", "secrets"):
        assert await _count(engine, table) == 0, table
    assert (await client.get(f"/api/v1/projects/{pid}")).status_code == 404


@pytest.mark.req("REQ-10-1", "REQ-10-4")
async def test_keys_are_hashed_scoped_and_revoked_in_place(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    pid = await _project(client, "alpha")
    other_api = await _api(client, await _project(client, "beta"))
    foreign = await client.post(
        f"/api/v1/projects/{pid}/keys", json={"name": "k", "allowed_api_ids": [other_api["id"]]}
    )
    assert foreign.status_code == 422
    assert foreign.json()["detail"][0]["loc"] == ["body", "allowed_api_ids"]

    created = await client.post(f"/api/v1/projects/{pid}/keys", json={"name": "k"})
    assert created.status_code == 201
    body = created.json()
    async with engine.connect() as conn:
        stored_hash = (
            await conn.execute(
                text("SELECT hash FROM jarvis_keys WHERE id = :id"), {"id": body["id"]}
            )
        ).scalar_one()
    assert stored_hash == hash_jarvis_key(body["key"])

    assert (await client.delete(f"/api/v1/keys/{body['id']}")).status_code == 204
    [listed] = (await client.get(f"/api/v1/projects/{pid}/keys")).json()
    assert listed["revoked_at"] is not None
    assert "key" not in listed
    assert await _count(engine, "jarvis_keys") == 1


@pytest.mark.req("REQ-10-1")
async def test_unknown_ids_and_duplicates_never_500(client: httpx.AsyncClient) -> None:
    pid = await _project(client)
    assert (
        await client.post("/api/v1/projects", json={"name": "a", "slug": "alpha"})
    ).status_code == 409
    assert (await client.get("/api/v1/projects/prj_missing")).status_code == 404
    assert (await client.patch("/api/v1/projects/prj_missing", json={})).status_code == 404
    assert (await client.delete("/api/v1/projects/prj_missing")).status_code == 404
    assert (await client.get("/api/v1/projects/prj_missing/keys")).status_code == 404
    missing_key = await client.post("/api/v1/projects/prj_missing/keys", json={"name": "k"})
    assert missing_key.status_code == 404
    assert (await client.delete("/api/v1/keys/key_missing")).status_code == 404
    missing_api = await client.post("/api/v1/projects/prj_missing/apis", json=_api_body())
    assert missing_api.status_code == 404
    assert (await client.get("/api/v1/apis/api_missing")).status_code == 404
    assert (await client.delete("/api/v1/apis/api_missing")).status_code == 404
    assert (await client.post(f"/api/v1/projects/{pid}/apis", json=_api_body())).status_code == 201


@pytest.mark.req("REQ-5.2-3")
async def test_a_save_succeeds_when_redis_is_down(
    engine: AsyncEngine, box: AesGcmSecretBox, caplog: pytest.LogCaptureFixture
) -> None:
    client, publisher = await _client(engine, box, CLOSED_REDIS)
    try:
        pid = await _project(client)
        created = await _api(client, pid)
        with caplog.at_level(logging.WARNING, logger="jarvis_api.events"):
            patched = await client.patch(
                f"/api/v1/apis/{created['id']}", json={"deadline_ms": 7000}
            )
        assert patched.status_code == 200
        assert patched.json()["version"] == 2
        stored = await PostgresConfigSource(engine).get_by_id(created["id"])
        assert stored is not None
        assert (stored.version, stored.config.deadline_ms) == (2, 7000)
        assert "config:changed" in caplog.text
        assert "127.0.0.1:1" not in caplog.text
    finally:
        await client.aclose()
        await publisher.aclose()


async def _current_refs(engine: AsyncEngine) -> set[str | None]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT v.config_json -> 'credential' ->> 'secret_ref' FROM api_dependencies a "
                "JOIN api_policy_versions v ON v.id = a.current_version_id"
            )
        )
        return set(rows.scalars())


@pytest.mark.req("REQ-10-1")
async def test_concurrent_creates_with_one_slug_give_one_201_and_one_409(
    client: httpx.AsyncClient, engine: AsyncEngine, box: AesGcmSecretBox
) -> None:
    pid = await _project(client)
    for round_ in range(3):
        slug = f"race-{round_}"
        first, second = await asyncio.gather(
            client.post(f"/api/v1/projects/{pid}/apis", json=_api_body(slug, secret="one")),
            client.post(f"/api/v1/projects/{pid}/apis", json=_api_body(slug, secret="two")),
        )
        assert sorted([first.status_code, second.status_code]) == [201, 409]
        winner = first if first.status_code == 201 else second
        assert winner.json()["version"] == 1
    assert await _count(engine, "api_dependencies") == 3
    assert await _count(engine, "api_policy_versions") == 3
    # The losing request's sealed secret was deleted again: one secret per API, all in use.
    assert set(await _secret_ids(engine)) == await _current_refs(engine)


@pytest.mark.req("REQ-11.2-4")
@pytest.mark.parametrize(
    "secret_change",
    [
        pytest.param({"secret": "SENTINEL-SECRET-NEW"}, id="replace"),
        pytest.param({"mode": "none"}, id="mode-none"),
    ],
)
async def test_interleaved_patches_lose_no_update_and_leave_no_dangling_secret(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    box: AesGcmSecretBox,
    secret_change: dict[str, str],
) -> None:
    pid = await _project(client)
    store = PostgresSecretStore(engine)
    for round_ in range(3):
        api_id = (await _api(client, pid, slug=f"api-{round_}"))["id"]
        changed_secret, changed_deadline = await asyncio.gather(
            client.patch(f"/api/v1/apis/{api_id}", json={"credential": secret_change}),
            client.patch(f"/api/v1/apis/{api_id}", json={"deadline_ms": 9000}),
        )
        statuses = {changed_secret.status_code, changed_deadline.status_code}
        assert statuses <= {200, 409}
        assert 200 in statuses
        stored = await PostgresConfigSource(engine).get_by_id(api_id)
        assert stored is not None
        ref = stored.config.credential.secret_ref
        if changed_secret.status_code == 200:
            if "secret" in secret_change:
                assert ref is not None
                assert await store.reveal(box, ref) == secret_change["secret"]
            else:
                assert ref is None
        else:
            assert ref is not None
            assert await store.reveal(box, ref) == SECRET
        assert (stored.config.deadline_ms == 9000) == (changed_deadline.status_code == 200)
        assert stored.version == 1 + sum(
            r.status_code == 200 for r in (changed_secret, changed_deadline)
        )
    # Every live config's secret exists, and no sealed secret is left unreferenced.
    assert set(await _secret_ids(engine)) == await _current_refs(engine) - {None}
