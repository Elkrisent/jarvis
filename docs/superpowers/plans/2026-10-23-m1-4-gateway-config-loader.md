# M1.4 Gateway Config Loader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The gateway holds every API policy and every JARVIS key hash in memory, loaded from Postgres at startup (or from a local snapshot file when Postgres is down), kept current by Redis pub/sub within 1 s, and corrected by a full resync every 60 s. The API publishes the events that make that true.

**Architecture:** In `services/gateway`, a new `jarvis_gateway.config` package: immutable `Snapshot` objects swapped atomically in a `ConfigStore`; a read-only asyncpg `PostgresLoader`; a `SnapshotFile` for the fallback; a `ConfigManager` that runs startup, targeted refreshes, the 60 s resync and the Redis subscriber. The gateway never imports `jarvis_api`. In `services/api`, the existing publisher gains a second event, `config:invalidated`, published after commit on key create/revoke, API delete and project delete.

**Tech Stack:** FastAPI/uvicorn (existing), `asyncpg==0.32.0` and `redis==8.0.1` (both already pinned for `services/api`; new for `services/gateway`), `jarvis_core.config.ApiDependency`, `jarvis_core.clock.Clock`.

**Spec:** `docs/SPEC.md` §5.2, §8, §4.2 (keys cached, revocation ≤ 1 s), §11.1, §13 (config propagation ≤ 1 s p99); `docs/IMPLEMENTATION.md` M1.4; requirements REQ-5.2-3, REQ-5.2-4, REQ-8-2, REQ-8-8, REQ-8-9, REQ-4.2-6, REQ-11.1-6 (revocation part), REQ-13-5.

**Prerequisite:** M1.3 merged (`7d8acec`). Branch `feat/m1.4-config-loader` from `main`.

## Global Constraints

- "Every save creates an immutable `ApiPolicyVersion` row. Once the save commits, the API publishes `config:changed {api_id, version}`. Gateways reload the policy and swap it atomically in memory. Gateways also do a full resync every 60 s and on startup." (SPEC §5.2)
- "Gateway startup requires Postgres once, to load config. After that it can run without Postgres indefinitely. The last loaded config is also written to a local snapshot file, so a gateway can start while Postgres is down." (SPEC §8)
- Postgres down at runtime: gateway unaffected. Redis down: gateway fails open, "config pub/sub replaced by the 60 s resync" (SPEC §8, ADR 0004).
- Keys: "cached in gateway memory (hash → project, allowed APIs). Revocation is pushed via pub/sub and takes effect in ≤ 1 s." (SPEC §4.2)
- Hot path: "0 Postgres queries"; config and keys held in memory (SPEC §13). Config propagation ≤ 1 s p99.
- "Secrets never appear in logs, telemetry, API responses, or AI context." The snapshot holds `secret_ref` ids and key hashes only, never plaintext secrets or full keys.
- Dependencies pinned exactly. Coverage gates: `packages/core` ≥ 95% line / 90% branch, `services/gateway` ≥ 90% / 85%, `services/api` ≥ 85% / 75%, no drop of more than 1 point vs `main` (the api is at 100% / 100%: keep it there by testing new branches).
- Every test carries exactly one type marker; requirement-linked tests carry `@pytest.mark.req("REQ-…")`.
- No Claude attribution anywhere: no `Co-Authored-By` trailer in commits, no footer in the PR (user memory).

## Decisions made here (not in the spec)

- **Events** (user-confirmed): the API publishes on every change. `config:changed {"api_id","version"}` is unchanged (API saves). New channel `config:invalidated`, payload `{"kind": "api"|"key"|"project", "id": "<id>"}` published after commit on: key create, key revoke (every call), API delete (`kind: api`), project delete (`kind: project`; the gateway reloads the project, and a missing project removes all its APIs and keys). Events are hints: the gateway always re-reads the rows from Postgres, and a row that is gone means "remove".
- **Snapshot shape.** `ApiEntry(api_id, project_id, slug, version, config: ApiDependency)` indexed by `(project_id, slug)` and by `api_id`; `KeyEntry(key_id, project_id, allowed_api_ids: frozenset[str], revoked: bool)` indexed by key hash. Revoked keys stay in the snapshot flagged `revoked` (M1.5 distinguishes revoked from unknown); deleted keys are absent. `allowed_api_ids` is copied as stored, never pruned: M1.5 must deny when an id no longer exists.
- **Atomic swap:** `ConfigStore.current` is a single reference replaced in one assignment; snapshots are frozen dataclasses with read-only mappings (`types.MappingProxyType`). A targeted update builds a new snapshot from the current one.
- **Stale guard:** a targeted API refresh never replaces an entry with a lower `version` than the one held.
- **Bad rows:** an API whose `current_version_id` is NULL or whose `config_json` fails `ApiDependency` validation is skipped with an error log naming the `api_id` (never the config); the previous entry for it, if any, is kept. One bad row never fails a load.
- **Startup:** one Postgres attempt (connect timeout 5 s) → else snapshot file → else the app still starts, `/readyz` answers 503 with `config.source: "none"`, a clear ERROR is logged, and loading is retried every 5 s until it succeeds.
- **Snapshot file:** JSON `{"format": 1, "written_at": <iso>, "apis": [...], "keys": [...]}`, written atomically (temp file in the same directory, `fsync`, `os.replace`), mode 0600, off the event loop, coalesced to at most one write per second, written after every swap (full or targeted). A missing, unreadable, corrupt, oversized (> 64 MiB) or foreign-`format` file is treated as absent with a WARNING (path and reason, no contents). Path from `JARVIS_SNAPSHOT_PATH`, default `/var/lib/jarvis/gateway/snapshot.json`.
- **Settings:** `JARVIS_RESYNC_INTERVAL_S` (default `60`, minimum `1`), `JARVIS_SNAPSHOT_PATH`; `DATABASE_URL` and `REDIS_URL` as today. Missing `DATABASE_URL` stops startup with a message naming the variable.
- **Redis subscriber:** subscribes to both channels; on any connection loss retries with exponential backoff capped at 5 s (`health_check_interval=5`); every (re)subscribe triggers one immediate full resync (covers messages missed while disconnected). Malformed payloads are ignored with a WARNING that names the channel only.
- **Postgres failure at runtime:** a failed resync keeps the current snapshot, logs once per failure streak, and counts as "stale".
- **Readiness:** `/readyz` keeps its Redis check (optional). It adds `config`: `{"source": "postgres"|"file"|"none", "apis": N, "keys": N, "synced_at": <epoch seconds or null>}`. Status: 503 `unavailable` when `source` is `none`; `degraded` (HTTP 200) when `source` is `file`, or no successful sync for more than 3 × the resync interval; otherwise the existing logic.
- **Compose:** gateway gets `depends_on: postgres: service_healthy` and a named volume `gateway-snapshot` at `/var/lib/jarvis/gateway`; the Dockerfile creates that directory owned by uid 10001 so the volume is writable under `read_only: true`. Replicas sharing the volume is acceptable: writes are atomic and the content is equivalent.

## Review Focus

1. **Out-of-order or hostile events.** An event for version 3 arriving after version 4 was loaded must not downgrade; an event for an unknown `api_id`/`key_id`; garbage JSON; an event naming a row that was just deleted. Expected: ignored or removed, never an exception, never an older policy. (Tasks 2, 5)
2. **Bad rows.** An API with NULL `current_version_id`, one with invalid `config_json`, a key with an empty `allowed_api_ids`. Expected: the bad API is skipped and logged without its config; everything else loads. (Task 4)
3. **Revocation and cascade.** Revoke, then resync: key present and `revoked`. Project delete: all its APIs and keys disappear from the gateway within 1 s. (Tasks 1, 5, 7)
4. **Corrupt or hostile snapshot file.** Truncated, empty, invalid JSON, wrong `format`, valid JSON with an invalid API config, a symlink target. Expected: treated as absent with a WARNING, never a crash, never partial state. (Task 3)
5. **Redis and Postgres flapping.** Redis down at startup and mid-run; Postgres down mid-run; both back later. Expected: no crash loop, stale-but-serving config, the resync heals, readiness reflects staleness. (Tasks 5, 6)

---

## File Structure

- Modify `services/api/src/jarvis_api/events.py`, `http/keys.py`, `http/apis.py`, `http/projects.py`, `services/api/tests/fakes.py`: Task 1.
- Create `services/gateway/src/jarvis_gateway/config/__init__.py`, `snapshot.py` (Task 2), `store.py` (Task 2), `snapshot_file.py` (Task 3), `postgres.py` (Task 4), `manager.py` and `events.py` (Task 5).
- Create `services/gateway/src/jarvis_gateway/settings.py`; modify `app.py` (Task 6).
- Modify `services/gateway/pyproject.toml` + `uv.lock` (Task 4), `deploy/docker-compose.yml`, `deploy/docker/service.Dockerfile`, `deploy/.env.example`, `README.md`, `docs/contributing.md` if commands change (Task 6).
- Create `services/gateway/tests/unit/…`, `services/gateway/tests/integration/…`, `tests/chaos/test_gateway_config.py`, `services/gateway/tests/bench/test_config_propagation.py` (Tasks 2–8). Modify `Makefile`, `.github/workflows/bench.yml`, `BENCHMARKS.md`, `docs/PROGRESS.md`, `docs/traceability.md` (Task 8).

---

### Task 1: The API publishes key and delete events

**Files:**
- Modify: `services/api/src/jarvis_api/events.py`, `services/api/src/jarvis_api/http/keys.py`, `http/apis.py`, `http/projects.py`, `services/api/tests/fakes.py`
- Test: `services/api/tests/unit/test_events.py`, `test_http_keys.py`, `test_http_apis.py`, `test_http_projects.py`, `services/api/tests/integration/test_events_redis.py`, `test_management_api.py`
- Modify: `docs/quickstart-dev.md` (replace the "does not publish yet" sentence)

**Interfaces:**
- Produces: `CONFIG_INVALIDATED_CHANNEL = "config:invalidated"`; `ConfigPublisher.invalidated(self, kind: Literal["api", "key", "project"], id: str) -> None` (same never-raises, WARNING-without-payload contract as `config_changed`; log text `could not publish config:invalidated (<ExceptionClassName>)`); the same method on `RedisConfigPublisher` and on `RecordingPublisher` (records `(kind, id)` tuples in `invalidations`).

- [ ] **Step 1: Write the failing tests** (`unit` unless noted; `REQ-11.1-6`, `REQ-5.2-3`).
  - `test_events.py`: `invalidated("key", "key_1")` publishes `{"kind":"key","id":"key_1"}` (compact JSON) on `config:invalidated` using the existing stubbed client; a `redis.ConnectionError("redis://:pw@host")` is swallowed and logged with `ConnectionError` but not `pw` or the URL; the timeouts are unchanged.
  - Router unit tests with `RecordingPublisher`: creating a key records `("key", key_id)`; revoking records `("key", key_id)` (also on a second revoke); deleting an API records `("api", api_id)`; deleting a project records `("project", project_id)`; none is recorded when the operation returns 404/409/422; publishing happens after the repository call succeeded.
  - Integration (`integration`, real Redis): subscribe to `config:invalidated`; create key, revoke it, delete API, delete project via the real app; the four messages arrive with the expected payloads; with a closed Redis port each operation still returns its normal 2xx/204.
- [ ] **Step 2: Run to verify failure:** `uv run pytest services/api/tests -k "invalidat" -q`. Expected: AttributeError / assertion failures.
- [ ] **Step 3: Implement** `invalidated` on both publishers (sharing the publish/except code with `config_changed`), and call it from the four routes after the repository call returns. Update the quickstart sentence to say these events are now published.
- [ ] **Step 4: Run to verify pass:** the whole `services/api` suite, `make coverage` (api must stay 100/100), ruff, mypy.
- [ ] **Step 5: Commit** `feat(api): publish config:invalidated for key, API and project changes (M1.4)`.

### Task 2: Snapshot model and atomic store

**Files:**
- Create: `services/gateway/src/jarvis_gateway/config/__init__.py`, `snapshot.py`, `store.py`
- Test: `services/gateway/tests/unit/test_config_snapshot.py`, `test_config_store.py`

**Interfaces:**
- Produces in `snapshot.py`:
  - `@dataclass(frozen=True) class ApiEntry: api_id: str; project_id: str; slug: str; version: int; config: ApiDependency`
  - `@dataclass(frozen=True) class KeyEntry: key_id: str; project_id: str; allowed_api_ids: frozenset[str]; revoked: bool`
  - `@dataclass(frozen=True) class Snapshot` with `apis: Mapping[str, ApiEntry]` (by `api_id`), `keys: Mapping[str, KeyEntry]` (by key hash), `source: Literal["postgres", "file"]`, `synced_at: float`; methods `api(project_id: str, slug: str) -> ApiEntry | None`, `api_by_id(api_id: str) -> ApiEntry | None`, `key(key_hash: str) -> KeyEntry | None`, `with_changes(*, apis: Mapping[str, ApiEntry | None] = {}, keys: Mapping[str, KeyEntry | None] = {}, remove_project: str | None = None, source=..., synced_at=...) -> Snapshot` (a `None` value removes; a lower-`version` `ApiEntry` than the held one is ignored), `to_json_dict() -> dict[str, object]`, `@classmethod from_json_dict(data) -> Snapshot` (raises `ValueError` with a message free of config contents on any invalid input). `Snapshot.empty(source, synced_at)`.
  - Mappings are exposed read-only (`MappingProxyType`); `repr` shows counts only.
- Produces in `store.py`: `class ConfigStore` with `current: Snapshot | None` (property), `replace(snapshot: Snapshot) -> None`, `update(fn: Callable[[Snapshot], Snapshot]) -> Snapshot` (read-modify-replace without awaiting in between).

- [ ] **Step 1: Write the failing tests** (`unit`; `REQ-5.2-3`, `REQ-4.2-6`).
  - Round trip: `Snapshot.from_json_dict(s.to_json_dict()) == s` for a snapshot with two projects, a header-credential API, a revoked key; JSON-dumpable with `json.dumps`.
  - `from_json_dict` raises `ValueError` for: wrong `format`, missing keys, an API whose `config` fails `ApiDependency` (message contains the `api_id` but not the config), non-string ids.
  - `with_changes`: add/replace/remove an API and a key; a lower `version` is ignored, an equal or higher one replaces; `remove_project` removes all that project's APIs and keys; the original snapshot is untouched; lookups by `(project_id, slug)` and by id stay consistent after changes (slug change moves the entry).
  - Atomic swap: one task replaces snapshots A↔B (A: 200 APIs all at version 1, B: all at version 2) in a tight loop while 20 reader tasks read `store.current` and assert every API in it has the same version (never a mix); mutating a returned mapping raises `TypeError`.
  - `repr(snapshot)` contains counts and no slug, secret_ref or hash.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.** Keep `Snapshot` construction O(n) once and derive the `(project_id, slug)` index at construction.
- [ ] **Step 4: Run to verify pass,** then ruff, mypy (strict applies to the gateway).
- [ ] **Step 5: Commit** `feat(gateway): add the immutable config snapshot and atomic store (M1.4)`.

### Task 3: Snapshot file

**Files:**
- Create: `services/gateway/src/jarvis_gateway/config/snapshot_file.py`
- Test: `services/gateway/tests/unit/test_snapshot_file.py`

**Interfaces:**
- Consumes: `Snapshot` (Task 2).
- Produces: `MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024`; `class SnapshotFile(path: Path)` with `write(snapshot: Snapshot, *, written_at: float) -> None` (blocking; atomic as in Decisions), `read() -> Snapshot | None` (never raises for bad content; logs a WARNING with path and reason only; the returned snapshot has `source="file"`), `async def write_async(snapshot, written_at)` running `write` in a thread; `class CoalescingWriter(file: SnapshotFile, clock: Clock)` with `submit(snapshot: Snapshot) -> None` (non-blocking; at most one write per second, always writing the latest submitted snapshot) and `async def aclose() -> None` (flushes the pending write).

- [ ] **Step 1: Write the failing tests** (`unit`, `tmp_path`; `REQ-8-9`).
  - write then read round-trips; the file mode is `0o600`; no `*.tmp` file is left after a write or after a failed write (monkeypatch `os.replace` to raise: the old file is intact and the error is logged, not raised from `CoalescingWriter`).
  - `read()` returns `None` and logs a WARNING (without contents) for: missing file, empty file, truncated JSON, invalid UTF-8, `format: 2`, a file larger than `MAX_SNAPSHOT_BYTES` (use a sparse file / monkeypatched limit), valid JSON with an invalid API config, a directory at the path, an unreadable file (mode 000, skipped when running as root). A symlink at the path is read only if it points to a regular file inside the same directory; otherwise `None`.
  - `CoalescingWriter` with a `FakeClock`: ten `submit` calls within one second cause one write of the last snapshot; one more after `advance(1)` causes a second; `aclose` flushes a pending submission.
  - Concurrent writers (two `SnapshotFile` objects, 50 interleaved writes from threads): every `read()` during the run returns a complete snapshot, never a partial one.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** with `tempfile.mkstemp(dir=path.parent)`, `os.fchmod`, `fsync` of file and directory, `os.replace`. The writer runs a single background task started lazily on first `submit`.
- [ ] **Step 4: Run to verify pass,** ruff, mypy.
- [ ] **Step 5: Commit** `feat(gateway): persist the config snapshot to a local file (M1.4)`.

### Task 4: Postgres loader

**Files:**
- Create: `services/gateway/src/jarvis_gateway/config/postgres.py`
- Modify: `services/gateway/pyproject.toml` (add `"asyncpg==0.32.0"`, `"redis==8.0.1"`), then `uv lock`
- Test: `services/gateway/tests/integration/test_postgres_loader.py`, `services/gateway/tests/integration/conftest.py` (fresh migrated database per test, same pattern as `services/api/tests/integration`; the schema comes from `jarvis_api.db.migrate`, which is test-only here)

**Interfaces:**
- Produces: `class PostgresLoader(dsn: str, *, connect_timeout_s: float = 5.0)` with `async def load_all(now: float) -> Snapshot` (source `"postgres"`, `synced_at=now`), `async def load_api(api_id: str) -> ApiEntry | None`, `async def load_key(key_id: str) -> tuple[str, KeyEntry] | None` (hash, entry), `async def load_project(project_id: str) -> tuple[list[ApiEntry], list[tuple[str, KeyEntry]]]` (empty lists when the project is gone), `async def close() -> None`. Raises `ConfigLoadError` (new, message without DSN) when Postgres is unreachable or a query fails. Read-only SQL over `api_dependencies`/`api_policy_versions`/`jarvis_keys` through a pool of at most 2 connections; the pool is created lazily and recreated after a failure. - Bad rows per Decisions: skipped and logged with the `api_id` only.

- [ ] **Step 1: Write the failing tests** (`integration`, real Postgres via `DATABASE_URL`; `REQ-5.2-4`, `REQ-4.2-6`).
  - Seed through the API's `PostgresConfigSource`, `ProjectRepository`, `KeyRepository` (test-only imports): two projects, three APIs (one with two versions: the loader returns the current one), two keys (one revoked, one restricted to an API), then `load_all(now=…)` returns exactly those entries, `config.credential.secret_ref` preserved, hashes equal `hash_jarvis_key(full)`, the revoked flag set.
  - `load_api` returns the current version after a second `put_version`; unknown id → `None`; `load_key` for a revoked key → entry with `revoked=True`; `load_project` for a deleted project → `([], [])`.
  - Review Focus 2: an API row with `current_version_id` NULL and one whose `config_json` is replaced by `{"slug": 1}` via raw SQL are skipped (log contains the api_id and not the JSON); the good APIs still load; a key with `allowed_api_ids = '{}'` loads with an empty frozenset.
  - Unreachable Postgres (closed port) → `ConfigLoadError` within about `connect_timeout_s`, message contains neither the DSN nor the password; a later call after Postgres comes back succeeds (pool recreated).
  - The loader issues only `SELECT` statements (assert by connecting as a role that has only `SELECT` on the three tables, created in the fixture).
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** with `asyncpg.create_pool(dsn, min_size=0, max_size=2, timeout=connect_timeout_s)`; map `asyncpg.PostgresError`/`OSError`/`TimeoutError` to `ConfigLoadError(from None)`. `load_all` runs both queries in one `REPEATABLE READ` read-only transaction so the snapshot is consistent. Validate each `config_json` with `ApiDependency.model_validate`.
- [ ] **Step 4: Run to verify pass,** `uv lock --check`, ruff, mypy, pip-audit clean (`make security-scan`).
- [ ] **Step 5: Commit** `feat(gateway): load policies and key hashes from Postgres (M1.4)`.

### Task 5: Config manager (startup, targeted refresh, resync, subscriber)

**Files:**
- Create: `services/gateway/src/jarvis_gateway/config/events.py`, `manager.py`
- Test: `services/gateway/tests/unit/test_config_manager.py`, `test_config_events.py`, `services/gateway/tests/integration/test_config_manager.py`

**Interfaces:**
- Consumes: `PostgresLoader`, `SnapshotFile`/`CoalescingWriter`, `ConfigStore`, `Clock`.
- Produces in `events.py`: `CHANNELS = ("config:changed", "config:invalidated")`; `ConfigEvent = ApiChanged(api_id: str, version: int) | Invalidated(kind: Literal["api","key","project"], id: str)`; `parse_event(channel: str, payload: bytes | str) -> ConfigEvent | None` (None on any malformed input); `async def subscribe(redis_url: str, on_event: Callable[[ConfigEvent], Awaitable[None]], on_connected: Callable[[], Awaitable[None]], clock: Clock, stop: asyncio.Event) -> None` (the reconnect loop of Decisions).
- Produces in `manager.py`: `ConfigStatus` (`source: Literal["postgres","file","none"]`, `apis: int`, `keys: int`, `synced_at: float | None`, `last_sync_ok: float | None`); `class ConfigManager(loader, snapshot_file, writer, store, clock, *, resync_interval_s: float, redis_url: str | None)` with `async def start() -> None` (startup per Decisions; returns once the first attempt finished, loading continues in the background if it failed), `async def stop() -> None`, `async def resync() -> bool`, `async def handle_event(event: ConfigEvent) -> None`, `status() -> ConfigStatus`, `store` property.
- `handle_event` rules: `ApiChanged` → `load_api`, apply with the stale guard; `Invalidated("api", id)` → `load_api` (None → remove); `Invalidated("key", id)` → `load_key` (None → remove the entries whose `key_id` matches); `Invalidated("project", id)` → `load_project` (empty → `remove_project`); any `ConfigLoadError` → log once per streak and leave the snapshot; events are applied serially.

- [ ] **Step 1: Write the failing tests.**
  - Unit with a fake loader and `FakeClock` (`unit`; `REQ-5.2-3`, `REQ-5.2-4`): startup loads from the loader and writes the file; startup with the loader raising and a valid file → store from the file, `source="file"`, background retry then switches to `postgres` and rewrites the file; loader raising and no file → `source="none"`, ERROR logged ("no configuration available…" naming the snapshot path), retried every 5 s of fake time; `resync` fires every `resync_interval_s` of fake time and replaces the snapshot; a failed resync keeps the old snapshot and logs once per streak; events: `ApiChanged` v3 after v4 loaded leaves v4; unknown `api_id` is a no-op; `Invalidated("api")` for a deleted API removes it; `Invalidated("key")` for a revoked key flips `revoked`; `Invalidated("project")` for a gone project removes its entries; malformed payloads (`parse_event` → `None`) never reach `handle_event`; events are applied one at a time.
  - `test_config_events.py` (`unit`): `parse_event` accepts both payload shapes (bytes and str), rejects bad JSON, wrong types, unknown kind, extra channel; `subscribe` with a fake Redis connection factory: `on_connected` runs after every (re)connect, backoff doubles to the 5 s cap on repeated failures (fake clock), `stop` ends the loop, a Redis error never escapes.
  - Integration (`integration`, real Postgres + Redis; `REQ-5.2-3`, `REQ-5.2-4`, `REQ-8-2`, `REQ-4.2-6`, `REQ-11.1-6`): build the real management app from Task-1 code against the same Postgres/Redis, run a `ConfigManager` against them with a tmp snapshot file. (a) `PATCH` an API through the app → the store shows the new version within 1 s (poll every 10 ms, assert elapsed ≤ 1 s); (b) key create → key hash present ≤ 1 s; key revoke → entry `revoked` ≤ 1 s; API delete and project delete → entries gone ≤ 1 s; (c) a missed message: stop the subscriber (or publish nothing, edit Postgres directly with `put_version`), advance to the next resync with `resync_interval_s=1` → the store catches up within about 2 s; (d) kill Redis connectivity (point the manager at a closed port) → startup still succeeds and the resync alone converges; (e) Postgres unreachable after startup (close the loader's pool/port): the store keeps serving the last snapshot, `status().last_sync_ok` ages, and recovers when Postgres returns.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.** One supervisor task owns the resync timer and the retry-until-loaded loop; one task runs `subscribe`; `stop()` cancels both and flushes the writer. No exception from a background task may end the process: log and continue.
- [ ] **Step 4: Run to verify pass,** the whole gateway suite, `make coverage` (gateway gates), ruff, mypy.
- [ ] **Step 5: Commit** `feat(gateway): add the config manager with pub/sub refresh and resync (M1.4)`.

### Task 6: App wiring, readiness, compose

**Files:**
- Create: `services/gateway/src/jarvis_gateway/settings.py`
- Modify: `services/gateway/src/jarvis_gateway/app.py`, `deploy/docker-compose.yml`, `deploy/docker/service.Dockerfile`, `deploy/.env.example`, `README.md`
- Test: `services/gateway/tests/unit/test_settings.py`, `services/gateway/tests/unit/test_app.py` (existing tests keep passing), `tests/system/test_stack_health.py` (extend), `tests/chaos/test_gateway_config.py`

**Interfaces:**
- Produces: `Settings(database_url, redis_url, snapshot_path: Path, resync_interval_s: float)` + `load_settings(env=None)` + `SettingsError` (names the variable, never values; `repr` hides passwords like the api's `redact_url`; reuse that behavior by re-implementing the small helper, do not import `jarvis_api`); `create_app(dependencies=(), probe=tcp_reachable, *, config: ConfigManager | None = None)`; the readiness body per Decisions (`config` object and status mapping). Without `config` the app behaves exactly as before.

- [ ] **Step 1: Write the failing tests.**
  - Settings (`unit`): defaults; each variable missing/invalid (`JARVIS_RESYNC_INTERVAL_S=0`, `abc`) → `SettingsError` naming it; repr hides the DB password.
  - App (`unit`, fake manager exposing `status()`): `/readyz` is 503 `unavailable` with `config.source == "none"`; 200 `degraded` with `source == "file"`; 200 `degraded` when `last_sync_ok` is older than 3 × interval (fake clock); `ok` with `postgres` and fresh sync; the body contains counts and no slug/hash/secret; the manager is started in the lifespan and stopped at shutdown; `/healthz` is always 200.
  - System (`system`, real stack): `/readyz` of the gateway returns `config.source == "postgres"`; create a project and an API through the management API and poll the gateway `/readyz` until `config.apis` increases, asserting it happens within 1 s of the API call returning (`REQ-13-5`, `REQ-5.2-3`); revoke a key and delete the project and see the counts drop within 1 s.
  - Chaos (`chaos`, real stack; `REQ-8-2`, `REQ-8-8`, `REQ-8-9`): with one API created, `docker compose stop postgres`; the running gateway stays `ok`-or-`degraded` with the same API count; `restart gateway` → `/readyz` 200 `degraded` with `source == "file"` and the API count preserved; start Postgres → within about the resync interval the source returns to `postgres`; then remove the snapshot volume contents and restart the gateway with Postgres stopped → `/readyz` 503 `unavailable`, `source == "none"`, container still running; start Postgres → becomes ready within ~10 s. (Use a shortened `JARVIS_RESYNC_INTERVAL_S` in the test override.)
- [ ] **Step 2: Run to verify failure** (unit first; the stack tests with `make test-system` / `make test-chaos`, Docker required).
- [ ] **Step 3: Implement** settings, lifespan wiring (`create_app_from_env` builds loader, snapshot file, writer, store and manager), compose volume + `depends_on postgres` + Dockerfile directory ownership, `.env.example` entries (`JARVIS_RESYNC_INTERVAL_S`, `JARVIS_SNAPSHOT_PATH` commented with defaults), `docker-compose.test.yml` override for the short interval, README paragraph.
- [ ] **Step 4: Run to verify pass:** unit, `make test-system`, `make test-chaos`, `make test-e2e` (Checkpoint 0 stories must still pass), `make lint typecheck`.
- [ ] **Step 5: Commit** `feat(gateway): load config at startup and report it in /readyz (M1.4)`.

### Task 7: End-to-end DoD tests and requirement traceability

**Files:**
- Create: `tests/e2e/test_m1_4_propagation.py` if the Task 5/6 tests do not already cover a case below; modify `docs/requirements.md` only if a REQ-ID is missing (check first); `docs/traceability.md` (regenerate).

- [ ] **Step 1:** List each M1.4 test bullet from `docs/IMPLEMENTATION.md` (atomic swap; snapshot round trip; edit reaches the gateway ≤ 1 s measured; missed message corrected by resync; Postgres down loads from snapshot; Postgres down and no snapshot → clear log + non-ready) against the tests written so far and write whichever is missing, naming the owning test in the commit body.
- [ ] **Step 2:** Run `make traceability COMPLETED_PHASE=0 TRACE_MARKERS="not perf"` with Docker: REQ-5.2-3, REQ-5.2-4, REQ-8-2, REQ-8-8, REQ-8-9, REQ-4.2-6, REQ-11.1-6, REQ-13-5 list tests and Checkpoint 0 rows stay `pass`.
- [ ] **Step 3: Commit** `test: cover the M1.4 definition of done (M1.4)`.

### Task 8: Propagation benchmark, CI and progress

**Files:**
- Create: `services/gateway/tests/bench/test_config_propagation.py`
- Modify: `Makefile` (`bench-propagation`), `.github/workflows/bench.yml`, `BENCHMARKS.md`, `docs/PROGRESS.md`, `docs/quickstart-dev.md` if events are mentioned

**Interfaces:** the test (marker `perf`) runs the Task 5 integration setup, performs 200 PATCHes spaced 20 ms apart, measures from the API response to the store showing the new version, and writes `{"config_propagation_p99": <ms>}` into `$BENCH_DIR/results.json` using `scripts/bench/benchlib.py` helpers (merge, do not overwrite other metrics). `make bench-propagation` runs it with `DATABASE_URL`/`REDIS_URL`. `bench.yml` gets a `propagation` job with Postgres and Redis service containers (copy the pattern of the `ci.yml` integration job and the `micro` job's publishing steps, data dir `bench/propagation`).

- [ ] **Step 1: Write the benchmark test;** assert p99 ≤ `benchmarks/budgets.yaml` `config_propagation_p99` budget (1000 ms) and record the number.
- [ ] **Step 2: Run** `make bench-propagation` locally; paste the real p50/p99 into `BENCHMARKS.md` (never invented), wire the workflow, and verify it with `act`-free YAML validation plus the first real run on the PR.
- [ ] **Step 3: Run the full verification:** `make lint typecheck`, `make security-scan`, `make coverage` (all gates, api back at 100/100 unless justified), `make test-system test-chaos test-e2e`, gitleaks over `main..HEAD`.
- [ ] **Step 4: Update `docs/PROGRESS.md`** (M1.4 verified on the branch with evidence; list anything not run; next = open the PR, then M1.5), commit `docs: record M1.4 in PROGRESS.md and BENCHMARKS.md (M1.4)`.

---

## Self-review notes

- **Spec coverage:** immutable snapshot + atomic swap (T2); Postgres first, file fallback, file written after every load (T3–T5); `config:changed` + key revocation subscriber (T1, T5); 60 s resync and at startup (T5); Postgres-down and Postgres-down-no-snapshot startup (T5, T6); ≤ 1 s propagation measured in an integration test, the system test and the benchmark (T5, T6, T8); `config_propagation_p99` recorded (T8).
- **Deferred on purpose:** authentication using the cached keys and denying unknown `allowed_api_ids` (M1.5); credential injection and secret decryption (later; the gateway holds `secret_ref` only); request routing by slug (M1.5/M1.6); the P5 rollback / old-version `secret_ref` question (recorded in PROGRESS; M1.4 reads only the current version).
- **Open assumption to confirm:** the readiness body shape (`config` object) and that unreadable config must make `/readyz` 503 rather than crash the container.
