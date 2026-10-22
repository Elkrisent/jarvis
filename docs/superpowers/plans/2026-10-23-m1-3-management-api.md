# M1.3 Minimal Management API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A usable `/api/v1` management API, authenticated by an admin token, with CRUD for projects, JARVIS keys and API dependencies; provider secrets sealed through `SecretBox`; every API save creating a policy version and publishing `config:changed`.

**Architecture:** `jarvis_core.keys` holds pure key generation and hashing (the gateway reuses it in M1.5). In `jarvis_api`, `settings.py` loads and validates the environment (fail-fast), `db/` gains repositories for projects, keys and APIs plus a Redis `ConfigPublisher`, and a new `http/` package holds the routers, request/response models and the admin-token dependency. `create_app` is given a `Services` bundle so unit tests can inject fakes. Redis publication is best-effort (ADR 0004: Redis is optional; gateways also resync every 60 s, SPEC §5.2).

**Tech Stack:** FastAPI 0.156.5, SQLAlchemy 2.1.6 / asyncpg 0.32.0 (pinned already), `redis==8.0.1` (new, pinned, `services/api`), `jarvis_core.secretbox` from M1.2.

**Spec:** `docs/SPEC.md` §10, §4.2, §5.1, §5.2, §11.2, §12; `docs/IMPLEMENTATION.md` M1.3 and B.5.3; requirements REQ-10-1, REQ-10-2, REQ-10-4, REQ-11.1-6 (key storage part), REQ-5.2-1, REQ-5.2-3 (publish part), REQ-11.2-4 (no secret in responses).

**Prerequisite:** M1.2 merged (`7b9e3c6`). Branch `feat/m1.3-management-api` from `main`.

## Global Constraints

- REST + JSON under `/api/v1`; OpenAPI generated automatically (SPEC §10).
- JARVIS keys: `jv_live_<32 bytes base62>`, stored as SHA-256 hashes, scoped to one project, optionally restricted to APIs, "the full key is shown once at creation" (SPEC §4.2, §10, IMPLEMENTATION M1.3).
- Admin token comes from the environment; it is "kept only for automation" later (IMPLEMENTATION line 744).
- "Secrets never appear in logs, telemetry, API responses, or AI context" (SPEC §11.2).
- "Every save creates an immutable `ApiPolicyVersion` row" and then "the API publishes `config:changed {api_id, version}`" after the save commits (SPEC §5.2).
- Validation errors return 422 with field paths (IMPLEMENTATION M1.3).
- Dependencies pinned exactly. Coverage gates: `packages/core` ≥ 95% line / 90% branch, `services/api` ≥ 85% / 75%, no drop of more than 1 point vs `main`.
- Every test carries exactly one type marker; requirement-linked tests carry `@pytest.mark.req("REQ-…")`.
- No Claude attribution in commits or the PR (user memory).

## Decisions made here (not in the spec)

- **Scope:** only the endpoints named in M1.3: projects, keys, APIs CRUD, and the `POST /apis/{id}/test` placeholder (returns 501 `JV_NOT_IMPLEMENTED` with a body naming the milestone that delivers it). Users, auth/session, versions/rollback, cache, metrics, etc. arrive later.
- **Paths** (SPEC §10): `GET/POST /projects`, `GET/PATCH/DELETE /projects/{id}`; `GET/POST /projects/{id}/keys`, `DELETE /keys/{id}` (revoke, sets `revoked_at`; the row stays); `GET/POST /projects/{id}/apis`, `GET/PATCH/DELETE /apis/{id}`, `POST /apis/{id}/test`. Everything under `/api/v1` requires the token; `/healthz` and `/readyz` stay outside.
- **Key format:** `"jv_live_" + base62(32 random bytes)` (about 43 characters, 256 bits). `prefix` stored for display is the first 12 characters (`jv_live_` + 4). `hash` is `sha256(full key).hexdigest()`.
- **Admin token:** `JARVIS_ADMIN_TOKEN`, at least 32 characters, sent as `Authorization: Bearer <token>`, compared with `hmac.compare_digest`. Missing or short token stops api startup with a clear message naming the variable, like `JARVIS_MASTER_KEY` (the fail-fast deferred from M1.2). Auth failures return 401 with the standard error body and never say which part was wrong.
- **Provider secrets:** an API request carries `credential.secret` (write-only plaintext). The server seals it with `PostgresSecretStore.add` and stores the resulting `secret_ref` in the policy. Responses never include `secret` or `secret_ref`; they include `credential.has_secret: bool`. On PATCH, omitting `secret` keeps the existing one; sending a new one replaces it (old row deleted in the same transaction); changing `mode` to `"none"` deletes it. Deleting an API deletes its secret.
- **PATCH** is a JSON merge patch (RFC 7386) over the stored config, validated as a whole by `ApiDependency`. `slug` can be changed; a duplicate slug within a project is 409.
- **Policy version fields:** `author_id` is `NULL` for the admin token (no user yet), `reason` is accepted as optional body field `reason`, `diff_json` is `{"changed": {<top-level key>: {"from":…,"to":…}}}` over the config with secret refs excluded (first version: `{"created": true}`).
- **Publishing:** channel `config:changed`, payload JSON `{"api_id": "...", "version": N}`, published after the transaction commits. If Redis is unreachable, log a warning (no payload) and still return success.
- **Errors:** `{"error": {"code": "JV_...", "message": "..."}}` using the `JarvisError` hierarchy in `jarvis_core.errors` where a fitting class exists; 422 bodies keep FastAPI's `detail` list with `loc` field paths (`["body","retry","max_attempts"]`).
- **Snapshot:** `tests/contract/openapi.snapshot.json`, regenerated by `make openapi-snapshot`; a unit test fails when it is stale (B.5.3).
- **Compose:** `JARVIS_MASTER_KEY` and `JARVIS_ADMIN_TOKEN` pass through to `api` as `${VAR:-}` (blank when unset, so the stack parses; the api then refuses to start with a clear message). `deploy/docker-compose.test.yml` sets throwaway values so the stack tests run.

## Review Focus

1. **Secret leakage through every door:** secret plaintext in create/patch/get/list responses, in 422 error bodies (FastAPI echoes `input`), in logs, and in the OpenAPI examples. Expected: absent everywhere; 422 bodies for a bad request containing a secret omit the input value. (Tasks 5, 6)
2. **Token handling:** missing header, `Basic` scheme, empty bearer, token with trailing whitespace, token as the right prefix only. Expected: 401, same body for each. (Task 4)
3. **Duplicate and dangling references:** duplicate project slug, duplicate API slug in a project, a key restricted to an API id from another project, unknown project/api ids. Expected: 409 / 422 / 404, never 500. (Tasks 3, 5)
4. **Concurrent saves of one API:** two PATCHes at once. Expected: distinct consecutive version numbers and one `config:changed` per committed version. (Task 3)
5. **Redis down on save:** Expected: 2xx, version committed, a warning logged, no exception text with the Redis URL. (Task 2)

---

## File Structure

- Create `packages/core/src/jarvis_core/keys.py`: `generate_jarvis_key`, `hash_jarvis_key`, `GeneratedKey`.
- Create `services/api/src/jarvis_api/settings.py`: `Settings`, `load_settings`, `SettingsError`.
- Create `services/api/src/jarvis_api/db/repositories.py`: `ProjectRepository`, `KeyRepository`.
- Modify `services/api/src/jarvis_api/db/config_source.py`: add `ApiRepository`-style methods (see Task 3) next to `PostgresConfigSource`, sharing its version-insert code.
- Create `services/api/src/jarvis_api/events.py`: `ConfigPublisher` protocol, `RedisConfigPublisher`, `CONFIG_CHANGED_CHANNEL`.
- Create `services/api/src/jarvis_api/http/` : `deps.py` (token auth, `Services`), `errors.py`, `models.py` (request/response models), `projects.py`, `keys.py`, `apis.py`, `router.py` (mounts under `/api/v1`).
- Modify `services/api/src/jarvis_api/app.py`: build `Services` from settings, include the router; keep `/healthz`, `/readyz`, migrations.
- Create `tests/contract/openapi.snapshot.json`, `tests/contract/test_openapi_snapshot.py`, `scripts/openapi_snapshot.py`.
- Create `docs/quickstart-dev.md`. Modify `README.md`, `deploy/.env.example`, `deploy/docker-compose.yml`, `deploy/docker-compose.test.yml`, `Makefile`, `services/api/pyproject.toml` (+ `uv lock`), `docs/PROGRESS.md`, `docs/traceability.md`, `docs/requirements.md` only if a REQ-ID needs adding (check first).

---

### Task 1: Key generation, settings and the Redis dependency

**Files:**
- Create: `packages/core/src/jarvis_core/keys.py`, `packages/core/tests/unit/test_keys.py`
- Create: `services/api/src/jarvis_api/settings.py`, `services/api/tests/unit/test_settings.py`
- Modify: `services/api/pyproject.toml` (add `"redis==8.0.1"`), then `uv lock`

**Interfaces:**
- Produces in `jarvis_core.keys`:
  - `KEY_PREFIX = "jv_live_"`, `DISPLAY_PREFIX_LEN = 12`
  - `@dataclass(frozen=True) class GeneratedKey: full: str; prefix: str; hash: str` with `__repr__` that shows only `prefix`
  - `def generate_jarvis_key() -> GeneratedKey`
  - `def hash_jarvis_key(key: str) -> str` → `sha256(key.encode()).hexdigest()`
  - `def looks_like_jarvis_key(value: str) -> bool` (starts with `jv_`; M1.5 uses it)
- Produces in `jarvis_api.settings`:
  - `class SettingsError(Exception)` (messages name the variable, never the value)
  - `@dataclass(frozen=True) class Settings: database_url: str; redis_url: str; admin_token: str; secret_box: AesGcmSecretBox` with a `__repr__` that hides token and box
  - `def load_settings(env: Mapping[str, str] | None = None) -> Settings` reading `DATABASE_URL`, `REDIS_URL`, `JARVIS_ADMIN_TOKEN` (≥ 32 chars), `JARVIS_MASTER_KEY` (via `secret_box_from_env`; map `MasterKeyError` to `SettingsError`)

- [ ] **Step 1: Write the failing tests** (marker `unit`).
  - `test_keys.py`, `REQ-11.1-6`: format matches `^jv_live_[0-9A-Za-z]{40,44}$`; `prefix == full[:12]`; `hash == hashlib.sha256(full.encode()).hexdigest()`; 1,000 generated keys are all distinct; `repr(generated)` contains the prefix and not `full[12:]`; `hash_jarvis_key` is deterministic; `looks_like_jarvis_key("jv_live_x")` true, `"sk-abc"` and `""` false. A statistical check that every base62 character class (digit, upper, lower) appears across 200 keys.
  - `test_settings.py`: a valid env loads; each of the four variables missing gives `SettingsError` whose message names that variable; token of 31 characters is rejected, 32 accepted; a bad master key is rejected without echoing it; `repr(settings)` and `str` contain neither the token nor the key.
- [ ] **Step 2: Run to verify failure:** `uv run pytest packages/core/tests/unit/test_keys.py services/api/tests/unit/test_settings.py -q`. Expected: `ModuleNotFoundError`.
- [ ] **Step 3: Implement** both modules. Base62 encoding of `secrets.token_bytes(32)` via `int.from_bytes`, no leading-zero loss concerns (length may vary by one or two characters, hence `{40,44}`). Add the dependency and `uv lock`.
- [ ] **Step 4: Run to verify pass**, then `uv run ruff check . && uv run ruff format --check . && uv run mypy`.
- [ ] **Step 5: Commit** `feat(core): add JARVIS key generation and hashing (M1.3)` and `feat(api): load and validate settings at startup (M1.3)`.

### Task 2: Config-changed publisher

**Files:**
- Create: `services/api/src/jarvis_api/events.py`
- Test: `services/api/tests/unit/test_events.py`, `services/api/tests/integration/test_events_redis.py`

**Interfaces:**
- Produces: `CONFIG_CHANGED_CHANNEL = "config:changed"`; `class ConfigPublisher(Protocol): async def config_changed(self, api_id: str, version: int) -> None`; `class RedisConfigPublisher` with `__init__(self, redis_url: str)`, the protocol method, and `async def aclose(self) -> None`; A `RecordingPublisher` test double lives in `services/api/tests/fakes.py`, not in production code.
- The method never raises: any `redis.RedisError`/`OSError` is caught and logged as `"could not publish config:changed (<ExceptionClassName>)"` at WARNING; the message contains no URL or payload.

- [ ] **Step 1: Write the failing tests.**
  - Unit (`REQ-5.2-3`): with `redis.asyncio.Redis` replaced by a stub whose `publish` raises `redis.ConnectionError("redis://:pw@host")`, `config_changed("api_1", 3)` returns normally and `caplog` contains `ConnectionError` but not `pw` or the URL.
  - Integration (`REQ-5.2-3`, real Redis from `REDIS_URL`): subscribe to `config:changed`, call `config_changed("api_1", 3)`, assert the received message decodes to `{"api_id": "api_1", "version": 3}`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** with `redis.asyncio.from_url(url)` and `json.dumps(..., separators=(",", ":"))`.
- [ ] **Step 4: Run to verify pass** (`REDIS_URL` set for the integration test) plus lint and mypy.
- [ ] **Step 5: Commit** `feat(api): publish config:changed to Redis (M1.3)`.

### Task 3: Repositories

**Files:**
- Create: `services/api/src/jarvis_api/db/repositories.py`
- Modify: `services/api/src/jarvis_api/db/config_source.py`
- Test: `services/api/tests/integration/test_repositories.py`

**Interfaces:**
- Consumes: `new_id`, models, `PostgresSecretStore`, `GeneratedKey` (Task 1).
- Produces in `repositories.py`:
  - `class Conflict(Exception)`, `class NotFound(Exception)` (carry a short message, no SQL)
  - `@dataclass(frozen=True) class ProjectRecord: id, name, slug, created_at`
  - `class ProjectRepository(engine)`: `create(name: str, slug: str) -> ProjectRecord` (raises `Conflict` on duplicate slug), `list() -> list[ProjectRecord]`, `get(id) -> ProjectRecord | None`, `update(id, *, name: str | None, slug: str | None) -> ProjectRecord` (`NotFound`/`Conflict`), `delete(id) -> None` (`NotFound`; cascades; also deletes the secrets of its APIs in the same transaction)
  - `@dataclass(frozen=True) class KeyRecord: id, project_id, name, prefix, allowed_api_ids: tuple[str, ...], created_at?` (the table has no `created_at`; use only existing columns plus `revoked_at: datetime | None`)
  - `class KeyRepository(engine)`: `create(project_id, name, allowed_api_ids, key: GeneratedKey) -> KeyRecord` (`NotFound` for an unknown project; `ValueError("allowed_api_ids must belong to the project")` mapped by the caller to 422 when any id is not an API of that project), `list(project_id) -> list[KeyRecord]`, `revoke(key_id) -> KeyRecord` (`NotFound`; idempotent: revoking again keeps the first `revoked_at`)
- Produces in `config_source.py`:
  - `@dataclass(frozen=True) class StoredApi: id: str; project_id: str; version: int; config: ApiDependency; created_at: datetime`
  - `PostgresConfigSource.put_version(project_id, api, *, author_id: str | None = None, reason: str | None = None) -> tuple[str, int]` returning `(api_id, version)`; the existing `put` delegates to it. It also writes `diff_json` per the Decisions section.
  - `PostgresConfigSource.get_by_id(api_id) -> StoredApi | None`, `list_stored(project_id) -> list[StoredApi]`, slug changes go through `put_version` with `api_id` provided via a new keyword `api_id: str | None = None` (when given, update that row's `slug`/`name` instead of upserting by slug; `Conflict` on a slug clash), `delete(api_id) -> None` (`NotFound`; returns nothing, secret cleanup is Task 5's job via `PostgresSecretStore.delete(secret_id)` added here: `async def delete(self, secret_id: str) -> None`).

- [ ] **Step 1: Write the failing tests** (marker `integration`, fresh database via `database_url` + `upgrade`):
  - Projects: create/list/get/update/delete round trip; duplicate slug → `Conflict`; delete of an unknown id → `NotFound`.
  - Keys: `create` stores `prefix` and `hash` and no full key anywhere in the table (query every text column for `generated.full`); `allowed_api_ids` containing an API from another project → `ValueError`; `revoke` sets `revoked_at` once, second call leaves it unchanged.
  - APIs (`REQ-5.2-1`): `put_version` creates version 1 then 2 with `diff_json` `{"created": true}` then `{"changed": {"deadline_ms": {"from": 5000, "to": 4000}}}`; `author_id`/`reason` persist; **concurrency** — 10 simultaneous `put_version` calls for one API yield versions `1..10` exactly once each; updating with `api_id=` and a slug already used by another API in the project → `Conflict`.
  - Secrets: `PostgresSecretStore.delete` removes the row; deleting an unknown id is a no-op.
- [ ] **Step 2: Run to verify failure:** `uv run pytest services/api/tests/integration/test_repositories.py -q` (needs `DATABASE_URL`).
- [ ] **Step 3: Implement.** Map `sqlalchemy.exc.IntegrityError` on the unique constraints (`projects.slug`, `uq_api_dependencies_project_slug`) to `Conflict`; reuse the version-number locking already in `put`. Keep `PostgresConfigSource.put` behaviour and its existing tests unchanged.
- [ ] **Step 4: Run to verify pass**, then the existing `services/api` suites, lint and mypy.
- [ ] **Step 5: Commit** `feat(api): add project, key and API repositories (M1.3)`.

### Task 4: HTTP skeleton, admin-token auth and error format

**Files:**
- Create: `services/api/src/jarvis_api/http/__init__.py`, `deps.py`, `errors.py`, `router.py`
- Modify: `services/api/src/jarvis_api/app.py`
- Test: `services/api/tests/unit/test_http_auth.py`, update `services/api/tests/unit/test_app.py`

**Interfaces:**
- Produces:
  - `@dataclass class Services: projects: ProjectRepository; keys: KeyRepository; apis: PostgresConfigSource; secrets: PostgresSecretStore; secret_box: AesGcmSecretBox; publisher: ConfigPublisher` (fields typed as Protocols where the unit tests inject fakes: define `ProjectsPort`, `KeysPort`, `ApisPort`, `SecretsPort` Protocols in `http/deps.py` with exactly the methods used by the routers)
  - `def create_app(dependencies=(), probe=tcp_reachable, migrate=None, *, services: Services | None = None, admin_token: str | None = None) -> FastAPI`: when `services` is given the `/api/v1` router is mounted; existing calls without it behave as before.
  - `require_admin` FastAPI dependency: reads `Authorization`, accepts only `Bearer <token>`, compares with `hmac.compare_digest` on UTF-8 bytes, otherwise raises `JarvisError`-style 401.
  - Error handler producing `{"error": {"code": "JV_AUTH_REQUIRED" | "JV_NOT_FOUND" | "JV_CONFLICT" | "JV_NOT_IMPLEMENTED", "message": str}}`; if `jarvis_core.errors` already defines suitable classes use them, otherwise add the missing codes there with unit tests (check `JarvisError` first).
  - A request-validation handler that keeps FastAPI's `detail` list (field paths in `loc`) but strips every `input` and `ctx` value (secrets must not be echoed).
- Consumes: `Settings` (Task 1).

- [ ] **Step 1: Write the failing tests** (`unit`, `REQ-10-2`): with fake services, `GET /api/v1/projects` returns 401 with identical body for: no header, `Basic abc`, `Bearer ` (empty), `Bearer <token> ` (trailing space), `Bearer <token[:-1]>`, `bearer <token>` lowercase scheme is accepted (RFC 7235 schemes are case-insensitive) → 200. `/healthz` and `/readyz` need no token. A 422 produced by a bad body contains `loc` paths and does not contain the submitted `SENTINEL-SECRET` anywhere in the response text (`REQ-10-1` for the `/api/v1` prefix and `/openapi.json` being served with the token-less docs disabled: set `docs_url=None, redoc_url=None`, keep `/openapi.json` behind the token).
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** the modules; `create_app_from_env` builds `Settings` first (so a missing key or token stops startup with its message), then engine, repositories, publisher and the `Services` bundle, and disposes engine and publisher at shutdown in the lifespan.
- [ ] **Step 4: Run to verify pass**, plus the existing `test_app*.py` unchanged, lint and mypy.
- [ ] **Step 5: Commit** `feat(api): mount /api/v1 with admin-token auth and a uniform error format (M1.3)`.

### Task 5: Project, key and API endpoints

**Files:**
- Create: `services/api/src/jarvis_api/http/models.py`, `projects.py`, `keys.py`, `apis.py`
- Test: `services/api/tests/unit/test_http_projects.py`, `test_http_keys.py`, `test_http_apis.py` (fake ports from `services/api/tests/fakes.py`), plus `services/api/tests/integration/test_management_api.py` (real Postgres and Redis through `TestClient`/`httpx.ASGITransport`)

**Interfaces:**
- Consumes: Task 3 repositories, Task 2 publisher, Task 4 deps.
- Produces request/response models in `models.py` (all `extra="forbid"` on input):
  - `ProjectIn(name: str 1..200, slug: Slug)`, `ProjectPatch` (both optional), `ProjectOut(id, name, slug, created_at)`
  - `KeyIn(name: str 1..200, allowed_api_ids: list[str] = [])`, `KeyOut(id, project_id, name, prefix, allowed_api_ids, revoked_at)`, `KeyCreated(KeyOut + key: str)` (the only model with the full key)
  - `CredentialIn(mode, header_name, param_name, secret: SecretStr | None)` and `ApiIn`: every `ApiDependency` field except `credential`, which is `CredentialIn`, plus `reason: str | None`. `ApiOut`: `id, project_id, version, created_at`, the `ApiDependency` fields with `credential` replaced by `{mode, header_name, param_name, has_secret}`.
  - `ApiPatch`: a plain `dict` body merged per the Decisions section (declare as `dict[str, Any]` with the OpenAPI schema documenting `ApiIn` via `openapi_extra`).
- Endpoints: as listed in "Decisions". Status codes: create → 201; delete → 204; unknown id → 404; `Conflict` → 409; invalid `allowed_api_ids` → 422 with `loc` `["body","allowed_api_ids"]`; `POST /apis/{id}/test` → 501 `JV_NOT_IMPLEMENTED`.
- API save flow: validate shape → (if a secret is supplied) seal and store → `put_version` → commit → `publisher.config_changed(api_id, version)`. If `put_version` fails after the secret was stored, delete that secret row (no orphan).

- [ ] **Step 1: Write the failing tests.**
  - Unit with fakes (`REQ-10-1`): happy-path CRUD for each resource; 404 for unknown ids; 409 for duplicate slugs; 422 field paths for `retry.max_attempts=0` (`loc == ["body","retry","max_attempts"]`), `attempt_timeout_ms > deadline_ms`, `credential.mode="bearer"` without a secret on create, a bad slug; `POST` of a key returns `key` once and `GET` of the keys list never contains `key` or `hash`.
  - Unit secrets (`REQ-11.2-4`, `REQ-10-4`): `SENTINEL-SECRET-1` sent as `credential.secret` appears in no response body (create, get, list, patch, 422) and in no `caplog` text.
  - Integration (real Postgres + Redis, `REQ-5.2-1`, `REQ-5.2-3`): create project → create API with a secret → GET shows `has_secret: true` and no secret; the `secrets` table holds exactly one row and it opens to the plaintext with the box; PATCH `deadline_ms` creates version 2 and a subscriber receives `{"api_id": …, "version": 2}` for each of the two saves; PATCH replacing the secret leaves exactly one secret row; PATCH `credential.mode` to `"none"` deletes it; DELETE API removes its secret and versions; DELETE project removes everything; a key created with `allowed_api_ids=[other project's api]` → 422; revoking a key sets `revoked_at` and keeps the row.
  - Integration, Review Focus 5: with the publisher pointed at a closed Redis port, a save returns 2xx and version 2 is committed.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** the routers. Build the merged config for PATCH from the current stored config (with `credential.secret_ref` kept internally), apply `merge_patch`, validate with `ApiDependency`, and compute the diff in a small pure function `config_diff(old: dict, new: dict) -> dict` in `http/apis.py` (unit-tested: nested changes are reported at the top-level key, `secret_ref` is excluded).
- [ ] **Step 4: Run to verify pass**, the whole `services/api` suite, lint, mypy.
- [ ] **Step 5: Commit** per endpoint group: `feat(api): add project endpoints`, `feat(api): add JARVIS key endpoints`, `feat(api): add API dependency endpoints with sealed provider secrets` (each with `(M1.3)`).

### Task 6: OpenAPI contract snapshot

**Files:**
- Create: `scripts/openapi_snapshot.py`, `tests/contract/openapi.snapshot.json`, `tests/contract/test_openapi_snapshot.py`
- Modify: `Makefile` (target `openapi-snapshot`), `docs/contributing.md` (one line), `tests/meta` only if a marker rule needs the new directory.

**Interfaces:**
- `scripts/openapi_snapshot.py [--write]` builds the app with fake services (no database), dumps `app.openapi()` as sorted, 2-space-indented JSON with a trailing newline, and either prints a unified diff / exits 1 when it differs from the file, or rewrites the file with `--write`.

- [ ] **Step 1: Write the failing test** (marker `contract` if it exists in `pyproject.toml` markers; otherwise `unit` and note it in the PR): generated OpenAPI equals the committed snapshot, and the failure message says to run `make openapi-snapshot` and explain the change in the PR description. A second test asserts no schema in the snapshot has a property named `secret_ref`, `hash` or `password` (secrets never appear in the contract).
- [ ] **Step 2: Run to verify failure** (snapshot missing).
- [ ] **Step 3: Implement** the script and Makefile target, run `make openapi-snapshot`, review the generated JSON for anything surprising, commit it.
- [ ] **Step 4: Run to verify pass**; also run it once after changing a field description to see the test fail, then revert.
- [ ] **Step 5: Commit** `test: add the OpenAPI contract snapshot (M1.3)`.

### Task 7: Compose, docs, system test and progress

**Files:**
- Modify: `deploy/docker-compose.yml` (api `environment`: `JARVIS_MASTER_KEY: ${JARVIS_MASTER_KEY:-}`, `JARVIS_ADMIN_TOKEN: ${JARVIS_ADMIN_TOKEN:-}`), `deploy/docker-compose.test.yml` (throwaway test values for the api; not real-looking secrets, gitleaks must pass: use `test-master-key-placeholder`-style values only if they pass validation, otherwise generate them in the test fixture and pass via env), `deploy/.env.example` (replace the "api does not read it yet" note; document `JARVIS_ADMIN_TOKEN` with `openssl rand -hex 32`), `README.md`, `docs/contributing.md` if commands changed
- Create: `docs/quickstart-dev.md`, `tests/system/test_management_api.py`
- Modify: `docs/PROGRESS.md`, `docs/traceability.md` (regenerate)

**Interfaces:** consumes the running compose `api` on `127.0.0.1:18081`.

- [ ] **Step 1: Write the failing system test** (marker `system`): with the test stack up, using `httpx` and the test token: create a project, create an API pointing at `http://mock-upstream:8080`, create a key, list them, confirm the full key was returned only at creation and `GET /api/v1/projects/{id}/keys` never shows it, confirm a request without the token is 401, and confirm the secret sent is not in any response.
- [ ] **Step 2: Run to verify failure** (`make test-system`, Docker required): the api refuses to start or the routes are missing.
- [ ] **Step 3: Write `docs/quickstart-dev.md`** (the DoD): prerequisites; generating the two secrets (`openssl rand -base64 32`, `openssl rand -hex 32`); `cp deploy/.env.example deploy/.env` and filling them; `docker compose … up -d --wait`; the exact `curl` commands (with `$JARVIS_ADMIN_TOKEN`) for: create project, create API with a bearer credential, create a key (note it is shown once), list, patch `deadline_ms`, revoke the key, delete; what `has_secret` means; where the OpenAPI lives (`/openapi.json` with the token). Run every command in the document against the stack and paste nothing you did not run.
- [ ] **Step 4: Run to verify pass:** `make test-system`; then `make lint typecheck`, `make coverage` (gates; no drop vs `main`), `make security-scan`, `make traceability COMPLETED_PHASE=0 TRACE_MARKERS="not perf"` and the whole non-perf suite. Expected: REQ-10-1, REQ-10-2, REQ-10-4, REQ-5.2-1, REQ-11.2-4 now list tests; Checkpoint 0 rows still `pass`.
- [ ] **Step 5: Update `docs/PROGRESS.md`** (M1.3 in progress with evidence, next = M1.4, resume point) in the existing format, then commit `docs: document the management API quickstart and record M1.3 (M1.3)`.

---

## Self-review notes

- **Spec coverage:** projects/keys/APIs CRUD (Task 5), hashed one-time keys (Tasks 1, 3, 5), sealed secrets never returned (Task 5), policy version + `config:changed` (Tasks 2, 3, 5), 422 field paths (Tasks 4, 5), OpenAPI snapshot (Task 6), quickstart DoD (Task 7), fail-fast master key deferred from M1.2 (Tasks 1, 4).
- **Deferred on purpose:** users/sessions (M4.3), versions list and rollback (P5), the real `/apis/{id}/test` (later), key restriction enforcement and revocation push (M1.4/M1.5).
- **Open assumption to confirm with the user:** key length reading of "32 bytes base62" (Decisions), and the `redis` client dependency.
