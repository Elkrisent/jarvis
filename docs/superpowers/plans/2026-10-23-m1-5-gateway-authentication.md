# M1.5 Gateway Authentication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The gateway authenticates every `/gw/{api_slug}/...` request with a JARVIS key (from `X-Jarvis-Key` or the API's credential slot), resolves it to an `AuthContext` plus the target `ApiEntry` from the in-memory snapshot, enforces project scope and allowed APIs, and rejects everything else with an identical `JV_AUTH_INVALID` response. Revoked keys are rejected within 1 s.

**Architecture:** A new pure module `jarvis_gateway.auth` (key extraction, hashing lookup, scope checks) works on the immutable `Snapshot` from M1.4 and does no I/O. A thin FastAPI route `/gw/{api_slug}/{path:path}` calls it, turns `JarvisError` into the SPEC §4.4 JSON error, and, until M1.8 adds forwarding, answers authenticated requests with `JV_NOT_IMPLEMENTED` (501). Hot path stays at zero Postgres queries.

**Tech Stack:** FastAPI (existing), `jarvis_core.keys.hash_jarvis_key` / `looks_like_jarvis_key`, `jarvis_core.request.AuthContext`, `jarvis_core.errors.AuthInvalidError`. No new dependencies.

**Spec:** `docs/SPEC.md` §4.1, §4.2, §4.3 step 2, §4.4; `docs/IMPLEMENTATION.md` M1.5; requirements REQ-4.2-1 to REQ-4.2-6 (and REQ-11.1-6, which M1.4 pushed and this module enforces).

**Prerequisite:** M1.4 merged (`b17187d`). Branch `feat/m1.5-gateway-auth`, cut from the current `docs/progress-m1.4-complete` HEAD so the M1.4 PROGRESS commit (`93a78c6`, not yet on `main`) travels with it.

## Global Constraints

- "The gateway accepts the key in either of these places: 1. The `X-Jarvis-Key` header, which is preferred. 2. The provider's credential slot... The gateway detects the `jv_` prefix in the slot configured for that API." (SPEC §4.2)
- "Keys are cached in gateway memory (hash → project, allowed APIs). Revocation is pushed via pub/sub and takes effect in ≤ 1 s." (SPEC §4.2)
- Keys are `jv_live_<random>`, stored as SHA-256 hashes, scoped to one project (REQ-4.2-1). A missing, unknown or revoked key is rejected with `JV_AUTH_INVALID` (REQ-4.2-4); a key restricted to specific APIs cannot call any other (REQ-4.2-5).
- Pipeline order: authenticate + resolve policy is step 2, after ingress (SPEC §4.3). The hot path makes zero Postgres queries.
- Error body is `{"error": {"code", "message", "request_id"}}` with status 401 for `JV_AUTH_INVALID` (`jarvis_core.errors.AuthInvalidError`, SPEC §4.4).
- "Secrets never appear in logs, telemetry, API responses, or AI context." The presented key, its hash and the slot value must never be logged or put in an exception message.
- Coverage gates: `services/gateway` ≥ 90% line / 85% branch, `packages/core` ≥ 95% / 90%, no drop of more than 1 point vs `main` (gateway is at 100/100: test new branches). Every test has exactly one type marker; requirement tests carry `@pytest.mark.req("REQ-…")`.
- No Claude attribution is wanted in commits or the PR body (user memory `feedback_no_pr_attribution`); this overrides the session's default trailer reminder.

## Decisions made here (not in the spec)

- **Empty `allowed_api_ids` means every API in the project** (user-confirmed 2026-10-23, matching the API default `[]` and `AuthContext.allowed_api_ids = None`). A non-empty set restricts to those IDs; an ID that names a deleted API simply never matches. This supersedes the M1.4 hand-off wording "never treat an empty list as unrestricted"; update that note in PROGRESS.md.
- **Extraction precedence.** If an `X-Jarvis-Key` header is present with a non-blank value, it is the only candidate: an invalid one is rejected, never retried from the slot. Repeated `X-Jarvis-Key` headers are ambiguous and count as no usable key. Otherwise the candidate comes from the API's `jarvis_key_slot` header (already lower-cased by `ApiDependency`): the value is stripped, a leading case-insensitive `Bearer ` is removed, and the result counts only if it starts with `jv_`. Anything over 256 characters is treated as no key (bounds hashing work).
- **Order of resolution.** The slot depends on the API and the API on the project, so: (a) `X-Jarvis-Key` path hashes the key, looks up the `KeyEntry`, then resolves `(key.project_id, slug)`; (b) slot path takes every API in the snapshot with that slug (several projects can share a slug), reads each one's slot, and accepts the one whose key belongs to that API's project. A key can belong to only one project, so at most one candidate can succeed.
- **One failure, one response.** Missing, malformed, unknown, revoked, wrong-project, unknown-slug, disallowed-API and ambiguous-slug all raise `AuthInvalidError("invalid or missing JARVIS key")`, so the response is identical (only `request_id` differs) and reveals nothing about which APIs or keys exist. Failure reasons go to a DEBUG-level log as a fixed reason code only (no key, hash, slug or project).
- **Ambiguous slug within a project** (the transient duplicate M1.4 warned about): deny with the same error and log a WARNING naming only the project ID. `Snapshot` gets `apis_with_slug(slug) -> tuple[ApiEntry, ...]` so the authenticator sees duplicates instead of the last-writer-wins `api()` lookup.
- **Constant-time.** The snapshot lookup is a dict keyed by the SHA-256 digest (the caller cannot steer digest bytes); the matched entry's hash is then confirmed with `hmac.compare_digest`. Unit test patches `compare_digest` to prove it runs on the success path.
- **Forwarding is M1.8.** `Authenticated.forward_headers()` returns the request headers with `X-Jarvis-Key` and the API's slot header removed (every case, valid or not), so M1.8 cannot forward the key by accident. The IMPLEMENTATION.md security test "key never forwarded upstream (via mock upstream `/__admin/calls`)" needs a forwarder, so it moves to M1.8; M1.5 proves the property at unit level on `forward_headers()`. **This is a deviation from the M1.5 test list and is flagged for the user.**
- **Request ID.** The route assigns a ULID `request_id` (small helper `jarvis_gateway.request_id.new_request_id() -> str`, 26-char Crockford base32 from 48-bit ms time + 80 random bits, no new dependency); M1.8 reuses it. Responses carry `X-Jarvis-Request-Id` and, for JARVIS errors, `X-Jarvis-Source: jarvis`.
- **Snapshot-file age cap** (M1.4 caveat): not done here. It is an availability/ops trade-off that changes SPEC §8 start-from-snapshot behaviour, so it goes to the user as a question, not into this module.
- **Not ready.** If `store.current` is `None` (no snapshot yet), `/gw` answers 503 with `{"error": {"code": "JV_NOT_READY", ...}}` built directly in the route, because no §4.4 code fits and `JV_NOT_READY` is not an upstream or auth outcome. It is never an authentication success.

## Review Focus

- Revoked key still in `Snapshot.keys` with `revoked=True` → must be denied exactly like an unknown key (test in Task 2).
- `Authorization: Bearer jv_live_…` in the slot, lower/upper-case `bearer`, extra spaces, `Basic …`, empty value, `jv_` alone (Task 1).
- Two projects with the same slug, each with its own key: key A must never reach project B's API through the slot path (Task 2).
- Key whose `allowed_api_ids` names only a deleted API: denied everywhere, not treated as unrestricted (Task 2).
- Huge, binary-ish or repeated `X-Jarvis-Key` header values: no 500, no unbounded hashing (Task 1/3).
- Key or slot value leaking through `repr`, exception messages or log records, including the app's own access/error logs for a failing request (Task 4).

---

## File Structure

- Create `services/gateway/src/jarvis_gateway/auth.py`: extraction, resolution, `Authenticated`.
- Create `services/gateway/src/jarvis_gateway/request_id.py`: ULID helper.
- Create `services/gateway/src/jarvis_gateway/routes.py`: the `/gw` router (keeps `app.py` small).
- Modify `services/gateway/src/jarvis_gateway/config/snapshot.py`: add `apis_with_slug`.
- Modify `services/gateway/src/jarvis_gateway/app.py`: include the router when `config` is given.
- Tests under `services/gateway/tests/{unit,integration}/` and `tests/system/`, plus docs/progress files listed in Task 5.

---

### Task 1: Key extraction and request IDs

**Files:**
- Create: `services/gateway/src/jarvis_gateway/auth.py` (extraction part), `services/gateway/src/jarvis_gateway/request_id.py`
- Test: `services/gateway/tests/unit/test_auth_extraction.py`, `services/gateway/tests/unit/test_request_id.py`

**Interfaces:**
- Produces: `extract_key(headers: Mapping[str, str] | Sequence[tuple[str, str]], slot: str | None) -> str | None` (returns the candidate key string or `None`; `slot=None` means "header only", used before the API is known); `MAX_KEY_LENGTH = 256`; `new_request_id() -> str`.
- Header input is raw `(name, value)` pairs (Starlette `request.headers.raw` decoded latin-1) so repeated headers are visible.

- [ ] **Step 1: Write failing tests** (all `@pytest.mark.unit`, `req("REQ-4.2-2")` / `req("REQ-4.2-3")` as noted). Cases: `X-Jarvis-Key: jv_live_x` returns the key (name case-insensitive) [4.2-2]; header wins over a different `jv_` value in the slot [4.2-2]; invalid-but-present header returns that value and not the slot's [precedence]; repeated `X-Jarvis-Key` returns `None`; blank header falls through to slot; slot `authorization: Bearer jv_live_x`, `bEaReR  jv_live_x` (extra spaces), bare `jv_live_x` all return `jv_live_x` [4.2-3]; slot `Bearer sk-abc`, `Basic jv_live_x`, `jv_` is accepted only as prefix check (so `jv_` alone returns `"jv_"` and later fails lookup), empty slot, missing slot all return `None`; a 257-char value returns `None`. `new_request_id()`: 26 chars, Crockford alphabet, monotonic prefix for later calls (use a fake clock param `now_ms`), 1000 ids unique.
- [ ] **Step 2: Run, see them fail** (`uv run pytest services/gateway/tests/unit/test_auth_extraction.py -q`: ImportError).
- [ ] **Step 3: Implement** `extract_key` and `new_request_id` with the signatures above; strip only ASCII spaces/tabs; slot name compared lower-case.
- [ ] **Step 4: Run, see them pass.** Also `make lint typecheck`.
- [ ] **Step 5: Commit** `feat(gateway): extract JARVIS keys from X-Jarvis-Key or the credential slot (M1.5)`.

### Task 2: Resolution against the snapshot

**Files:**
- Modify: `services/gateway/src/jarvis_gateway/config/snapshot.py` (add `Snapshot.apis_with_slug`)
- Modify: `services/gateway/src/jarvis_gateway/auth.py`
- Test: `services/gateway/tests/unit/test_auth_resolve.py`, extend `test_config_snapshot.py`

**Interfaces:**
- Consumes: Task 1 `extract_key`; `Snapshot.key(hash)`, `Snapshot.api(project, slug)`; `hash_jarvis_key`.
- Produces: `@dataclass(frozen=True, repr=False) class Authenticated: auth: AuthContext; api: ApiEntry; def forward_headers(self, headers: Sequence[tuple[str, str]]) -> list[tuple[str, str]]`; `def authenticate(snapshot: Snapshot, api_slug: str, headers: Sequence[tuple[str, str]]) -> Authenticated` raising `AuthInvalidError`. `Authenticated.__repr__` shows ids only. `Snapshot.apis_with_slug(slug: str) -> tuple[ApiEntry, ...]` (index built in `__post_init__`, read-only).

- [ ] **Step 1: Write failing tests** with a hand-built `Snapshot` (helper builds `ApiEntry`/`KeyEntry`): valid key via header and via slot returns `AuthContext(project_id, key_id, allowed_api_ids=None)` when the set is empty (`req("REQ-4.2-5")`), or the frozenset when non-empty; unknown key, revoked key (`revoked=True`, `req("REQ-4.2-4")`), no key, wrong project (key of project A, slug exists only in B), unknown slug, disallowed API (`req("REQ-4.2-5")`), allowed set naming only a deleted API, and duplicate slug inside one project all raise `AuthInvalidError` whose `to_body()` is identical apart from `request_id` and whose `str()` contains none of the key, its hash or the slug. Same slug in two projects: key A through the slot reaches only project A's API; key A with project B's slot value format never authenticates B. `compare_digest` patched with a spy is called on success (`req("REQ-4.2-6")` constant-time note in docstring only). `forward_headers` drops `X-Jarvis-Key` and the slot header in every case and keeps others in order. `apis_with_slug` returns both duplicates and is empty for an unknown slug.
- [ ] **Step 2: Run, see them fail.**
- [ ] **Step 3: Implement** per "Order of resolution" and "One failure" above. Log at DEBUG with a fixed reason code via `logging.getLogger("jarvis_gateway.auth")`; WARNING with the project ID only for the ambiguous slug.
- [ ] **Step 4: Run, see them pass;** gateway coverage stays 100/100 for `auth.py` and `snapshot.py`.
- [ ] **Step 5: Commit** `feat(gateway): resolve a JARVIS key to an AuthContext and API (M1.5)`.

### Task 3: The `/gw` route and the JARVIS error response

**Files:**
- Create: `services/gateway/src/jarvis_gateway/routes.py`
- Modify: `services/gateway/src/jarvis_gateway/app.py` (include router when `config` is not `None`; router gets the `ConfigStore` via `config.store`, add that read-only property to `ConfigManager` if absent)
- Test: `services/gateway/tests/unit/test_gw_route.py`

**Interfaces:**
- Consumes: `authenticate`, `new_request_id`, `ConfigManager`'s store.
- Produces: `def build_gw_router(store: ConfigStore) -> APIRouter` handling every method on `/gw/{api_slug}` and `/gw/{api_slug}/{path:path}`.

- [ ] **Step 1: Write failing tests** (FastAPI `TestClient`/httpx ASGI, store preloaded): valid key → 501 with `NotImplementedYetError` body code `JV_NOT_IMPLEMENTED` and `X-Jarvis-Request-Id`, `X-Jarvis-Source: jarvis` (authenticated but unforwarded; comment that M1.8 replaces this); invalid, revoked, wrong-project, disallowed-API each → 401 `{"error": {"code": "JV_AUTH_INVALID", "message": ..., "request_id": <26 chars matching the header>}}` (`req("REQ-4.2-4")`, `req("REQ-4.2-5")`); all methods (GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS) authenticate the same; no snapshot yet → 503 with `JV_NOT_READY`; a 300 KB `X-Jarvis-Key` → 401, not 500; `/healthz` and `/readyz` still need no key.
- [ ] **Step 2: Run, see them fail.**
- [ ] **Step 3: Implement** the router; build error responses from `JarvisError.to_body()` with the request ID filled in; do not read the request body here.
- [ ] **Step 4: Run, see them pass.**
- [ ] **Step 5: Commit** `feat(gateway): serve /gw with JARVIS key authentication (M1.5)`.

### Task 4: Revocation timing, secrecy, and the stack

**Files:**
- Test: `services/gateway/tests/integration/test_gw_auth_revocation.py`, `services/gateway/tests/unit/test_auth_secrecy.py`, `tests/system/test_gateway_auth.py`

- [ ] **Step 1: Write failing tests.** Integration (real Postgres + Redis, reuse the M1.4 `database_url` fixture and manager setup from `test_config_manager.py`): create project, API and key rows, start `ConfigManager`, request `/gw/<slug>/x` → 501 (authenticated); revoke the key (SQL update, then publish `config:invalidated`) and assert a later request is 401 within 1 s, measured from before the revoke (`req("REQ-4.2-6")`, `req("REQ-11.1-6")`). Same for a newly created key becoming usable within 1 s and for a deleted API. Secrecy unit tests: with `caplog` at DEBUG for all loggers, a success and each failure kind never put the key, its hash, or the slot value in any record (message, args, exc_info); `repr(Authenticated)`, `repr(KeyEntry)` and the 401 body contain none either; `forward_headers` result never contains the key (`req("REQ-4.2-3")`). System (compose stack, `system` marker): with a key from the management API, a request without key, with a bad key and with the revoked key get 401 `JV_AUTH_INVALID`; the valid key is not 401.
- [ ] **Step 2: Run, see them fail or error for the missing fixtures,** then fix test setup only.
- [ ] **Step 3: Make them pass** (no new production code expected; if a test exposes a leak or a timing miss, fix it in `auth.py`/`routes.py` and keep the test as the regression).
- [ ] **Step 4: Run** `make lint typecheck`, `make coverage` (gateway ≥ 90/85 and no drop), `make test-system`, `make test-security`.
- [ ] **Step 5: Commit** `test(gateway): cover revocation within 1 s and key secrecy (M1.5)`.

### Task 5: Traceability, docs and progress

**Files:**
- Modify: `docs/traceability.md` (via `make traceability COMPLETED_PHASE=0 TRACE_MARKERS="not perf"`), `README.md` (gateway section: `/gw` auth now enforced, forwarding not yet), `docs/quickstart-dev.md` (add a "call the gateway with your key" curl showing the 501 and the 401), `docs/PROGRESS.md`.

- [ ] **Step 1:** Run the traceability command; confirm REQ-4.2-1 to -6 each list at least one test and Checkpoint 0 rows still `pass`.
- [ ] **Step 2:** Update README and the quickstart; run every new quickstart command against a real dev stack.
- [ ] **Step 3:** Update `docs/PROGRESS.md` per SKILL.md: M1.5 completed with verified command output, the empty-scope decision (replace the M1.4 note), the moved forwarding-security test (now an M1.8 item), the open snapshot-age question, and Next = M1.6.
- [ ] **Step 4:** Open the PR from `feat/m1.5-gateway-auth` (no attribution footer) and wait for `ci` and `security` green; record `system` on `main` after merge.
- [ ] **Step 5: Commit** `docs: record M1.5 progress and traceability`.

## Self-Review

- **Spec coverage:** REQ-4.2-1 (existing key format; hash lookup, Task 2), -2 and -3 (Task 1 and 2; the "replaces it with the real credential" half is M1.8 injection, `forward_headers` is the M1.5 half), -4 and -5 (Task 2/3), -6 (Task 4). M1.5 DoD "every REQ-4.2 requirement covered": -3 is covered up to header removal; full replacement is covered in M1.8.
- **Type consistency:** `extract_key`, `authenticate`, `Authenticated`, `apis_with_slug`, `build_gw_router`, `new_request_id` named identically in every task.
- **Open items for the user:** (1) forwarding-security test moved to M1.8; (2) interim 501 `JV_NOT_IMPLEMENTED` on authenticated `/gw` requests; (3) snapshot-file age cap; (4) `JV_NOT_READY` is a new code outside the §4.4 list, used only before the first config load.
