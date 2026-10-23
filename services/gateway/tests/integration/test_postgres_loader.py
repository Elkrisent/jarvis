"""PostgresLoader against a real, migrated Postgres (the schema comes from the API's migrations)."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import asyncpg
import pytest
from jarvis_api.db.config_source import PostgresConfigSource
from jarvis_api.db.migrate import to_async_url
from jarvis_api.db.repositories import KeyRepository, ProjectRepository
from jarvis_api.db.secrets import PostgresSecretStore
from jarvis_core.config import ApiDependency
from jarvis_core.keys import generate_jarvis_key, hash_jarvis_key
from jarvis_core.secretbox import AesGcmSecretBox
from jarvis_gateway.config import postgres
from jarvis_gateway.config.postgres import (
    ConfigLoadError,
    InvalidApi,
    PostgresLoader,
    ProjectRows,
)
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

pytestmark = pytest.mark.integration

LOGGER = "jarvis_gateway.config.postgres"


def _api(slug: str, name: str | None = None, secret_ref: str | None = None) -> ApiDependency:
    cred: dict[str, str] = (
        {"mode": "bearer", "secret_ref": secret_ref} if secret_ref else {"mode": "none"}
    )
    return ApiDependency.model_validate(
        {
            "slug": slug,
            "name": name or slug.title(),
            "base_url": f"https://{slug}.example.com",
            "credential": cred,
        }
    )


@dataclass
class Seed:
    p1: str
    p2: str
    weather: str  # p1, two versions
    maps: str  # p1
    news: str  # p2
    live_full: str  # restricted to weather
    revoked_full: str  # p2, revoked
    live_id: str
    revoked_id: str


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(to_async_url(database_url))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def seed(engine: AsyncEngine) -> Seed:
    projects, source, keys = (
        ProjectRepository(engine),
        PostgresConfigSource(engine),
        KeyRepository(engine),
    )
    p1 = await projects.create("One", "one")
    p2 = await projects.create("Two", "two")
    weather, _ = await source.put_version(p1.id, _api("weather", "Weather v1", "ref-1"))
    weather, _ = await source.put_version(p1.id, _api("weather", "Weather v2", "ref-1"))
    maps, _ = await source.put_version(p1.id, _api("maps"))
    news, _ = await source.put_version(p2.id, _api("news"))
    live, revoked = generate_jarvis_key(), generate_jarvis_key()
    live_rec = await keys.create(p1.id, "live", [weather], live)
    revoked_rec = await keys.create(p2.id, "gone", [], revoked)
    await keys.revoke(revoked_rec.id)
    return Seed(
        p1.id, p2.id, weather, maps, news, live.full, revoked.full, live_rec.id, revoked_rec.id
    )


@pytest.fixture
async def loader(database_url: str) -> AsyncIterator[PostgresLoader]:
    loader = PostgresLoader(database_url, connect_timeout_s=2.0)
    try:
        yield loader
    finally:
        await loader.close()


@pytest.mark.req("REQ-5.2-4")
async def test_load_all_returns_exactly_the_seeded_entries(
    loader: PostgresLoader, seed: Seed
) -> None:
    snap, invalid = await loader.load_all(now=123.5)
    assert invalid == frozenset()

    assert snap.source == "postgres"
    assert snap.synced_at == 123.5
    assert set(snap.apis) == {seed.weather, seed.maps, seed.news}
    weather = snap.apis[seed.weather]
    assert (weather.project_id, weather.slug, weather.version) == (seed.p1, "weather", 2)
    assert weather.config.name == "Weather v2"
    assert weather.config.credential.secret_ref == "ref-1"
    assert snap.api(seed.p2, "news") is not None
    assert set(snap.keys) == {hash_jarvis_key(seed.live_full), hash_jarvis_key(seed.revoked_full)}
    live = snap.keys[hash_jarvis_key(seed.live_full)]
    assert (live.key_id, live.project_id, live.revoked) == (seed.live_id, seed.p1, False)
    assert live.allowed_api_ids == frozenset({seed.weather})
    revoked = snap.keys[hash_jarvis_key(seed.revoked_full)]
    assert revoked.revoked is True
    assert revoked.allowed_api_ids == frozenset()


@pytest.mark.req("REQ-5.2-4")
async def test_load_api_returns_the_current_version(
    loader: PostgresLoader, seed: Seed, engine: AsyncEngine
) -> None:
    first = await loader.load_api(seed.weather)
    assert first is not None
    assert first.version == 2

    await PostgresConfigSource(engine).put_version(
        seed.p1, _api("weather", "Weather v3"), api_id=seed.weather
    )
    again = await loader.load_api(seed.weather)
    assert again is not None
    assert (again.version, again.config.name) == (3, "Weather v3")


@pytest.mark.req("REQ-5.2-4")
async def test_load_api_unknown_id_is_none(loader: PostgresLoader, seed: Seed) -> None:
    assert await loader.load_api("api_nope") is None


@pytest.mark.req("REQ-4.2-6")
async def test_load_key_of_a_revoked_key_is_flagged_not_dropped(
    loader: PostgresLoader, seed: Seed
) -> None:
    got = await loader.load_key(seed.revoked_id)
    assert got is not None
    key_hash, entry = got
    assert key_hash == hash_jarvis_key(seed.revoked_full)
    assert entry.revoked is True
    live = await loader.load_key(seed.live_id)
    assert live is not None
    assert live[1].revoked is False
    assert await loader.load_key("key_nope") is None


@pytest.mark.req("REQ-5.2-4")
async def test_load_project_returns_its_rows_and_empty_when_gone(
    loader: PostgresLoader, seed: Seed, engine: AsyncEngine
) -> None:
    apis, keys, invalid = await loader.load_project(seed.p1)
    assert invalid == frozenset()
    assert {a.api_id for a in apis} == {seed.weather, seed.maps}
    assert [k[1].key_id for k in keys] == [seed.live_id]

    await ProjectRepository(engine).delete(seed.p1)
    assert await loader.load_project(seed.p1) == ProjectRows([], [], frozenset())


async def _raw(database_url: str, sql: str, *args: object) -> None:
    dsn = make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(sql, *args)
    finally:
        await conn.close()


@pytest.mark.req("REQ-5.2-4")
async def test_bad_api_rows_are_skipped_and_logged_without_their_config(
    loader: PostgresLoader,
    seed: Seed,
    database_url: str,
    engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    null_api, _ = await PostgresConfigSource(engine).put_version(seed.p2, _api("nullver"))
    bad_cfg, _ = await PostgresConfigSource(engine).put_version(seed.p2, _api("badcfg"))
    await _raw(
        database_url,
        "UPDATE api_dependencies SET current_version_id = NULL WHERE id = $1",
        null_api,
    )
    await _raw(
        database_url,
        'UPDATE api_policy_versions SET config_json = \'{"slug": 1, "name": "LEAKME"}\''
        " WHERE api_id = $1",
        bad_cfg,
    )

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        snap, invalid = await loader.load_all(now=1.0)
        single = await loader.load_api(bad_cfg)
        null_single = await loader.load_api(null_api)
        project = await loader.load_project(seed.p2)

    assert set(snap.apis) == {seed.weather, seed.maps, seed.news}
    assert invalid == {null_api, bad_cfg}
    assert single == InvalidApi(bad_cfg)
    assert null_single == InvalidApi(null_api)
    assert {a.api_id for a in project.apis} == {seed.news}
    assert project.invalid_api_ids == {null_api, bad_cfg}
    # Invalid is not gone: a missing row is still None, and a deleted project is empty.
    assert await loader.load_api("api_nope") is None
    text = caplog.text
    assert bad_cfg in text
    assert null_api in text
    assert "LEAKME" not in text
    assert all(r.levelno >= logging.ERROR for r in caplog.records if bad_cfg in r.getMessage())


@pytest.mark.req("REQ-4.2-6")
async def test_key_with_empty_allowed_api_ids_loads_with_empty_frozenset(
    loader: PostgresLoader, seed: Seed, database_url: str
) -> None:
    await _raw(
        database_url, "UPDATE jarvis_keys SET allowed_api_ids = '{}' WHERE id = $1", seed.live_id
    )
    got = await loader.load_key(seed.live_id)
    assert got is not None
    assert got[1].allowed_api_ids == frozenset()


@pytest.mark.req("REQ-5.2-4")
async def test_allowed_api_ids_are_copied_as_stored_even_if_dangling(
    loader: PostgresLoader, seed: Seed, database_url: str
) -> None:
    await _raw(
        database_url,
        "UPDATE jarvis_keys SET allowed_api_ids = '{api_gone}' WHERE id = $1",
        seed.live_id,
    )
    got = await loader.load_key(seed.live_id)
    assert got is not None
    assert got[1].allowed_api_ids == frozenset({"api_gone"})


class _Proxy:
    """A TCP forwarder in front of Postgres that the test can stop and restart."""

    def __init__(self, target_host: str, target_port: int) -> None:
        self._target = (target_host, target_port)
        self.port = 0
        self._server: asyncio.Server | None = None
        self._conns: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        for w in list(self._conns):
            w.close()
        self._conns.clear()

    async def _handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            tr, tw = await asyncio.open_connection(*self._target)
        except OSError:
            w.close()
            return
        self._conns.update((w, tw))

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
            except (OSError, asyncio.CancelledError):
                pass
            finally:
                dst.close()

        await asyncio.gather(pipe(r, tw), pipe(tr, w))


@pytest.mark.req("REQ-5.2-4")
async def test_unreachable_postgres_raises_a_dsn_free_error_and_the_pool_recovers(
    seed: Seed, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = make_url(database_url)
    proxy = _Proxy(url.host or "127.0.0.1", url.port or 5432)
    await proxy.start()
    dsn = url.set(host="127.0.0.1", port=proxy.port).render_as_string(hide_password=False)
    loader = PostgresLoader(dsn, connect_timeout_s=1.0)
    try:
        created = 0
        real_create_pool = postgres.asyncpg.create_pool

        def counting(*args: object, **kwargs: object) -> object:
            nonlocal created
            created += 1
            return real_create_pool(*args, **kwargs)

        monkeypatch.setattr(postgres.asyncpg, "create_pool", counting)
        assert (await loader.load_all(now=1.0))[0].apis  # healthy first
        assert created == 1
        first_pool = loader._pool
        await proxy.stop()  # existing connections are cut and the port is closed

        started = time.monotonic()
        with pytest.raises(ConfigLoadError) as err:
            await loader.load_all(now=2.0)
        assert time.monotonic() - started < 5.0
        message = str(err.value) + repr(err.value)
        assert dsn not in message
        assert (url.password or "") not in message
        assert f"{proxy.port}" not in message

        await proxy.start()  # same port again: the loader must recreate its pool
        snap, _ = await loader.load_all(now=3.0)
        assert set(snap.apis) == {seed.weather, seed.maps, seed.news}
        assert created == 2  # the failed pool was dropped and a new one created
        assert loader._pool is not None
        assert loader._pool is not first_pool
    finally:
        await loader.close()
        await proxy.stop()


@pytest.mark.req("REQ-5.2-4")
async def test_closed_port_fails_within_the_connect_timeout_without_leaking_the_dsn() -> None:
    dsn = "postgresql://someone:s3cr3t-pw@127.0.0.1:1/jarvis"
    loader = PostgresLoader(dsn, connect_timeout_s=1.0)
    started = time.monotonic()
    try:
        with pytest.raises(ConfigLoadError) as err:
            await loader.load_all(now=1.0)
        with pytest.raises(ConfigLoadError):
            await loader.load_api("api_x")
    finally:
        await loader.close()
    assert time.monotonic() - started < 6.0
    shown = str(err.value) + repr(err.value) + repr(err.value.__cause__)
    assert "s3cr3t-pw" not in shown
    assert "someone" not in shown
    assert dsn not in shown
    assert err.value.__suppress_context__ is True


@pytest.mark.req("REQ-5.2-4")
async def test_invalid_dsn_raises_config_load_error_without_the_dsn() -> None:
    loader = PostgresLoader("not a dsn pw=hunter2", connect_timeout_s=1.0)
    try:
        with pytest.raises(ConfigLoadError) as err:
            await loader.load_all(now=1.0)
    finally:
        await loader.close()
    assert "hunter2" not in str(err.value) + repr(err.value)


@dataclass
class ReadOnlyAccess:
    dsn: str
    own_role: bool  # False: the connecting role owns the tables, so only they were restricted


_TABLES = "api_dependencies, api_policy_versions, jarvis_keys, secrets"


@pytest.fixture
async def select_only(database_url: str) -> AsyncIterator[ReadOnlyAccess]:
    """Access whose only privilege on the three tables the loader reads is SELECT.

    Preferably a dedicated role. A server role that may not create roles (a plain CREATEDB test
    role) falls back to revoking the write privileges the owner holds on its own tables.
    """
    url = make_url(database_url)
    admin = await asyncpg.connect(
        url.set(drivername="postgresql").render_as_string(hide_password=False)
    )
    role = f"gw_ro_{uuid.uuid4().hex[:8]}"
    password = uuid.uuid4().hex
    created = False
    try:
        try:
            await admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}'")
            created = True
        except asyncpg.InsufficientPrivilegeError:
            await admin.execute(f"REVOKE ALL ON {_TABLES} FROM CURRENT_USER")
            await admin.execute(f"GRANT SELECT ON {_TABLES} TO CURRENT_USER")
            yield ReadOnlyAccess(database_url, own_role=False)
            return
        await admin.execute(f'GRANT CONNECT ON DATABASE "{url.database}" TO "{role}"')
        await admin.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
        await admin.execute(f'GRANT SELECT ON {_TABLES} TO "{role}"')
        dsn = url.set(username=role, password=password).render_as_string(hide_password=False)
        yield ReadOnlyAccess(dsn, own_role=True)
    finally:
        if created:
            await admin.execute(f'DROP OWNED BY "{role}"')
            await admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        await admin.close()


@pytest.mark.req("REQ-5.2-4")
async def test_the_loader_works_with_access_that_can_only_select(
    seed: Seed, select_only: ReadOnlyAccess
) -> None:
    # `seed` is requested first: it must write before the write privileges are taken away.
    # The access really is read-only: a write is refused ...
    conn = await asyncpg.connect(
        make_url(select_only.dsn).set(drivername="postgresql").render_as_string(hide_password=False)
    )
    try:
        for write in (
            "UPDATE jarvis_keys SET name = 'x'",
            "DELETE FROM api_dependencies",
            "INSERT INTO api_policy_versions (id, api_id, version, config_json)"
            " VALUES ('v', 'a', 1, '{}')",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(write)
        if select_only.own_role:  # ... and nothing beyond the four tables is readable.
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.fetch("SELECT 1 FROM projects")
    finally:
        await conn.close()

    # ... yet every loader entry point succeeds, so it issues nothing but SELECTs.
    loader = PostgresLoader(select_only.dsn, connect_timeout_s=2.0)
    try:
        snap, _ = await loader.load_all(now=1.0)
        assert set(snap.apis) == {seed.weather, seed.maps, seed.news}
        assert await loader.load_api(seed.weather) is not None
        assert await loader.load_key(seed.live_id) is not None
        apis, keys, _ = await loader.load_project(seed.p1)
        assert apis
        assert keys
    finally:
        await loader.close()


@pytest.mark.req("REQ-5.2-4")
async def test_full_and_project_loads_run_in_a_read_only_repeatable_read_transaction(
    loader: PostgresLoader, seed: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, str, str]] = []
    real_run = loader._run

    async def spying_run(fn):  # type: ignore[no-untyped-def]
        async def wrapped(conn):  # type: ignore[no-untyped-def]
            class Proxy:
                def transaction(self, **kwargs: object) -> object:
                    return conn.transaction(**kwargs)

                async def fetch(self, query: str, *args: object) -> object:
                    seen.append(
                        (
                            query.split(" FROM ")[1].split()[0],
                            await conn.fetchval("SHOW transaction_read_only"),
                            await conn.fetchval("SHOW transaction_isolation"),
                        )
                    )
                    return await conn.fetch(query, *args)

            return await fn(Proxy())

        return await real_run(wrapped)

    monkeypatch.setattr(loader, "_run", spying_run)
    await loader.load_all(now=1.0)
    await loader.load_project(seed.p1)

    assert len(seen) == 4  # two queries each, all inside the transaction
    assert {(ro, iso) for _, ro, iso in seen} == {("on", "repeatable read")}
    assert {t for t, _, _ in seen} == {"api_dependencies", "jarvis_keys"}


@pytest.mark.req("REQ-5.2-4")
@pytest.mark.parametrize(
    "option", ["options", "sslpassword", "server_settings", "statement_timeout"]
)
async def test_unsupported_dsn_options_are_rejected_by_name_only(option: str) -> None:
    loader = PostgresLoader(f"postgresql://u:pw-secret@127.0.0.1:1/db?{option}=VALUE-secret")
    try:
        with pytest.raises(ConfigLoadError) as err:
            await loader.load_all(now=1.0)
    finally:
        await loader.close()
    assert str(err.value) == f"unsupported DSN option: {option}"
    assert "secret" not in str(err.value)


@pytest.mark.req("REQ-5.2-4")
@pytest.mark.parametrize(
    "query",
    ["connect_timeout=5", "sslmode=prefer&connect_timeout=5", "application_name=jarvis-gw"],
)
async def test_libpq_style_dsn_options_shared_with_the_api_are_accepted(
    seed: Seed, database_url: str, query: str
) -> None:
    loader = PostgresLoader(f"{database_url}?{query}", connect_timeout_s=2.0)
    try:
        assert set((await loader.load_all(now=1.0))[0].apis) == {seed.weather, seed.maps, seed.news}
    finally:
        await loader.close()


# --- sealed provider secrets (M1.8) ---------------------------------------------------------------


@pytest.mark.req("REQ-4.5-7")
async def test_an_apis_sealed_secret_is_loaded_with_it_and_follows_a_replacement(
    engine: AsyncEngine, loader: PostgresLoader, seed: Seed
) -> None:
    box, store = AesGcmSecretBox(bytes(range(32))), PostgresSecretStore(engine)
    source = PostgresConfigSource(engine)
    first = await store.add(box, "provider-secret-1")
    api_id, _ = await source.put_version(seed.p1, _api("billing", secret_ref=first))
    entry = await loader.load_api(api_id)
    assert not isinstance(entry, InvalidApi)
    assert entry is not None
    assert entry.sealed == await store.get(first)
    assert box.open(entry.sealed, context=first) == "provider-secret-1"

    second = await store.add(box, "provider-secret-2")
    await source.put_version(seed.p1, _api("billing", secret_ref=second), api_id=api_id)
    replaced = await loader.load_api(api_id)
    assert not isinstance(replaced, InvalidApi)
    assert replaced is not None
    assert replaced.sealed == await store.get(second)
    assert replaced.sealed != entry.sealed


@pytest.mark.req("REQ-4.5-7")
async def test_apis_without_a_stored_secret_have_no_sealed_record(
    loader: PostgresLoader, seed: Seed
) -> None:
    snap, _ = await loader.load_all(now=1.0)
    assert snap.apis[seed.maps].sealed is None  # mode none
    assert snap.apis[seed.weather].sealed is None  # secret_ref points at no stored row


@pytest.fixture
async def sealed_api(engine: AsyncEngine, seed: Seed) -> str:
    """An API with a stored secret, created before the read-only access takes writes away."""
    secret_id = await PostgresSecretStore(engine).add(AesGcmSecretBox(bytes(range(32))), "s3cret")
    api_id, _ = await PostgresConfigSource(engine).put_version(
        seed.p1, _api("sealed", secret_ref=secret_id)
    )
    return api_id


@pytest.mark.req("REQ-4.5-7")
async def test_sealed_secrets_load_with_select_only_access(
    sealed_api: str, select_only: ReadOnlyAccess
) -> None:
    loader = PostgresLoader(select_only.dsn, connect_timeout_s=2.0)
    try:
        entry = await loader.load_api(sealed_api)
        assert not isinstance(entry, InvalidApi)
        assert entry is not None
        assert entry.sealed is not None
    finally:
        await loader.close()
