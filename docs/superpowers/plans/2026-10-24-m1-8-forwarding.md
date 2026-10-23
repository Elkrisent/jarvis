# M1.8 Forwarding, Credential Injection, Header Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/gw/{api_slug}/{path}` forwards the request to the API's `base_url` through the SSRF-pinned transport, with the JARVIS key removed, the real provider credential injected by mode, hop-by-hop headers stripped, the body size limit enforced, streaming requests rejected, and `X-Jarvis-*` response headers added. The upstream's response (including 4xx/5xx) is returned unchanged. The "change one base URL" story works end to end.

**Architecture:** Pure helpers in `jarvis_gateway.forward` (headers, target URL, credential injection). A `CredentialVault` decrypts sealed provider secrets into memory whenever a config snapshot is installed. A `Forwarder` keeps one pooled `httpx.AsyncClient` (on `PinnedTransport`) per upstream origin and streams the response back. The route calls it after authentication. Provider secrets reach the gateway as sealed (encrypted) records inside the config snapshot, so a gateway started from the snapshot file still works while Postgres is down.

**Tech Stack:** FastAPI/Starlette, `httpx==0.28.1` + `httpcore==1.0.9` (already pinned for the gateway), `jarvis_core.secretbox`. New dev pin: `requests==2.34.2` (already in `uv.lock`) for the SDK-style system test.

**Spec:** `docs/SPEC.md` §4.1, §4.3 steps 1, 2, 8 (single attempt), 10, §4.4, §4.5, §8, §11.2, D10/D11; `docs/IMPLEMENTATION.md` M1.8; requirements REQ-3-4, REQ-4.1-1/-2, REQ-4.3-1/-2/-6, REQ-4.4-1/-5/-7, REQ-4.5-2 to -7.

**Prerequisite:** M1.7 merged (`a3d3942`). Branch `feat/m1.8-forwarding` from `main`.

## Global Constraints

- `{METHOD} /gw/{api_slug}/{upstream_path}?{query}` is forwarded to `{base_url}/{upstream_path}?{query}`; an unknown slug is rejected without any upstream call (SPEC §4.1, REQ-4.1-2).
- Pipeline order: ingress (request ID, body limit, streaming rejection) → authenticate → upstream call → finalize (SPEC §4.3). Hot path: zero Postgres queries.
- Credential modes `bearer` / `header` / `query` / `none`; "the gateway **always removes** any client-supplied value in that slot" before injection (SPEC §4.5). "Secrets are stored encrypted (§11.2) and decrypted into gateway memory at config load."
- "Secrets never appear in logs, telemetry, API responses, or AI context." The JARVIS key is never forwarded upstream.
- Error body `{"error": {"code","message","request_id"}}`, codes from `jarvis_core.errors` (`JV_BODY_TOO_LARGE` 413, `JV_STREAMING_UNSUPPORTED` 400, `JV_UPSTREAM_TIMEOUT` 504, `JV_UPSTREAM_UNREACHABLE` 502, `JV_DESTINATION_BLOCKED` 403). Upstream errors pass through unchanged (REQ-4.4-7). Streaming (SSE) is rejected in v1 (D11).
- Dependencies pinned exactly. Coverage gates: core ≥ 95/90, gateway ≥ 90/85, api ≥ 85/75, no drop over 1 point vs `main`. Core and gateway are mypy-strict. One type marker per test; requirement tests carry `@pytest.mark.req(...)`.
- No Claude attribution in commits or the PR body (user memory).

## Decisions made here (not in the spec)

- **Secrets reach the gateway sealed.** `ApiEntry.sealed: SealedSecret | None` is loaded with the API (`LEFT JOIN secrets` on `credential.secret_ref`), stored in the snapshot file as base64 ciphertext, and decrypted by the `CredentialVault` with `JARVIS_MASTER_KEY` each time a snapshot is installed (config load, targeted refresh, file fallback). Plaintext lives only in the vault's memory, never in `Snapshot.to_json_dict`, a log, or a `repr`. The loader's read-only role now also needs `SELECT` on `secrets`. *Cost if wrong: the gateway holds the master key (the spec requires it to decrypt at load).*
- **The gateway requires `JARVIS_MASTER_KEY`** at startup (like the api). After `jarvis rotate-master-key` the gateway must be restarted with the new key; until then APIs with a stored credential answer `JV_UPSTREAM_UNREACHABLE` ("credential unavailable", logged with the API id only). `docs/secrets-rotation.md` is updated.
- **Settings:** `JARVIS_MAX_BODY_BYTES` (default 10485760, minimum 1); the SSRF policy comes from `SsrfPolicy.from_env` (M1.7).
- **Ingress checks before authentication** (SPEC order): `Accept` containing `text/event-stream` → `JV_STREAMING_UNSUPPORTED`; a declared `Content-Length` over the limit → `JV_BODY_TOO_LARGE`. A body without a (truthful) length is capped while it is read, after authentication.
- **Target URL.** The path and query are taken from the raw request target (`scope["raw_path"]`, `scope["query_string"]`) so percent-encoding is preserved (`%2F` stays encoded). Dot segments (`.`, `..`, `%2e`) are resolved and can never climb above `base_url`'s path; bytes outside the URL-safe set are percent-encoded. For a `query` credential, existing query segments whose decoded key equals `param_name` are dropped and `param_name=<encoded secret>` is appended; all other segments are kept byte for byte.
- **Headers.** Request: drop hop-by-hop (`Connection`, `Keep-Alive`, `Proxy-*`, `TE`, `Trailer`, `Transfer-Encoding`, `Upgrade`, plus any header named in `Connection`), `Host`, `Content-Length` (recomputed), `Expect`, the JARVIS key header and slot (`Authenticated.forward_headers`). `Accept-Encoding` is forwarded and the response is returned undecoded with its `Content-Encoding`, so bodies are byte-for-byte. Response: drop hop-by-hop headers only; repeated headers (`Set-Cookie`) are kept in order. No `X-Forwarded-*` or `Via` is added.
- **Response is streamed** from the upstream (`aiter_raw`) and the upstream response is closed when the client goes away. Redirects from the upstream are returned as they are (`follow_redirects=False`).
- **Time limits (single attempt, refined in M2.1):** the upstream must send response headers within `deadline_ms`; connect and each read use `attempt_timeout_ms`. Timeout → `JV_UPSTREAM_TIMEOUT`; connect, protocol or credential-header errors → `JV_UPSTREAM_UNREACHABLE` with a fixed message (no address, no httpx text).
- **Response headers added:** `X-Jarvis-Request-Id`, `X-Jarvis-Source: upstream` (or `jarvis` for errors), `X-Jarvis-Attempts: 1`. `X-Jarvis-Cache` and `X-Jarvis-Circuit` arrive with M3 and M2.
- **Clients:** one `httpx.AsyncClient` per `(scheme, host, port)`, created lazily on `PinnedTransport(policy, ...)`, never given `proxy=` or `mounts=`; clients for origins no API uses any more are closed when a snapshot is installed. The key for the SDK-style test is `requests`, a dev-only pin.
- **Moved here from M1.5:** the security test "the JARVIS key is never forwarded upstream" (asserted through the mock upstream's `/__admin/calls`).

## Review Focus

- A client-supplied `Authorization` / custom header / query parameter in the injected slot is removed in any letter case, including repeated headers and `?key=a&key=b` (Task 3, 5).
- The JARVIS key never reaches the upstream (header, slot, any case, repeated) nor any log (Task 5).
- Paths: `..`, `%2e%2e`, `%2F`, `//`, empty path, trailing slash, spaces and non-ASCII, a base URL with its own path prefix (Task 3, 5).
- Request bodies: empty POST, JSON, binary, chunked without length, a lying `Content-Length`, exactly the limit, one byte over (Task 4, 5).
- Responses: 204/304/HEAD without body, gzip passthrough with its `Content-Encoding`, repeated `Set-Cookie`, upstream 3xx not followed, upstream 5xx unchanged, a hung upstream → 504, a refused connection → 502 (Task 5).
- A rotated master key (credential cannot be decrypted) gives a clear 502, never a 500 or a leaked secret (Task 2, 5).
- Snapshot file contains ciphertext only; a gateway started from it can still inject credentials (Task 1, 2).

---

### Task 1: Sealed credentials in the config pipeline

**Files:** Modify `services/gateway/src/jarvis_gateway/config/snapshot.py` (`ApiEntry.sealed`, JSON), `services/gateway/src/jarvis_gateway/config/postgres.py` (SQL join), tests `services/gateway/tests/unit/test_config_snapshot.py`, `services/gateway/tests/integration/test_postgres_loader.py` (grant `SELECT` on `secrets` to the read-only role; new cases).

**Interfaces:** `ApiEntry(api_id, project_id, slug, version, config, sealed: SealedSecret | None = None)` (`repr` unchanged: ids and version only); snapshot JSON gains per API `"sealed": null | {"key_id": str, "nonce": b64, "ciphertext": b64}`; `format` stays 1 and a missing `sealed` key reads as `None`.

- [ ] **Step 1: Write failing tests:** snapshot round trip keeps `sealed`; JSON contains base64 ciphertext and no plaintext; bad `sealed` (wrong types, bad base64) raises `ValueError` without echoing contents; an API with `credential.mode == "none"` has `sealed is None`; loader integration: an API created through the management API with a secret loads with `sealed` equal to the stored row (`key_id`, `nonce`, `ciphertext`), an API without a secret has `None`, a PATCH that replaces the secret reloads the new record, the loader still works with a role that has only `SELECT` on the four tables.
- [ ] **Step 2: Run, see them fail.**
- [ ] **Step 3: Implement** the field, parsing/serialising, and `LEFT JOIN secrets s ON s.id = v.config_json->'credential'->>'secret_ref'` selecting `s.key_id, s.nonce, s.ciphertext` in `_API_SELECT`.
- [ ] **Step 4: Run (unit + integration with Postgres/Redis), lint, mypy; gateway coverage within gates.**
- [ ] **Step 5: Commit** `feat(gateway): carry sealed provider secrets in the config snapshot (M1.8)`.

### Task 2: Credential vault and gateway settings

**Files:** Create `services/gateway/src/jarvis_gateway/credentials.py`; modify `config/store.py` (listeners), `settings.py`, tests `unit/test_credentials.py`, `unit/test_config_store.py`, `unit/test_settings.py`.

**Interfaces (produces):** `ConfigStore.add_listener(fn: Callable[[Snapshot], None]) -> None` (called after every `replace`/`update`; a listener error is logged with its type name only and never breaks the swap). `class CredentialUnavailable(Exception)`. `class CredentialVault: __init__(self, box: AesGcmSecretBox)`; `refresh(self, snapshot: Snapshot) -> None` (decrypts every `sealed` record, binding the context to `secret_ref`; drops plaintext for APIs no longer present; a record that fails to open is skipped and logged once per `(api_id, key_id)` with those ids only); `secret_for(self, api: ApiEntry) -> str | None` (`None` when the mode needs no secret; raises `CredentialUnavailable` when a secret is needed and none is held); `__repr__` hides contents. `Settings` gains `master_key: bytes` (required, via `parse_master_key`), `max_body_bytes: int`, `ssrf_policy: SsrfPolicy`; `__repr__` shows neither the key nor the allowlist.

- [ ] **Step 1: Write failing tests:** a sealed secret for `secret_ref` opens and is returned for its API; a record sealed for another row id (wrong context) is unavailable; a wrong master key is unavailable and logged once without the key, secret or ciphertext; mode `none` returns `None`; mode `bearer` with no sealed record raises `CredentialUnavailable`; `refresh` after a PATCH replaces the plaintext and after a delete forgets it; `repr(vault)` has no plaintext; the store calls listeners on `replace` and `update`, and a raising listener does not stop the swap; settings: key required and malformed key named by variable only, `JARVIS_MAX_BODY_BYTES` default, override, invalid values rejected without echoing, `repr` hides secrets.
- [ ] **Step 2: Run, see them fail.** **Step 3: Implement.** **Step 4: Run, lint, mypy, coverage.**
- [ ] **Step 5: Commit** `feat(gateway): decrypt provider secrets into memory when a snapshot is installed (M1.8)`.

### Task 3: Pure forwarding helpers

**Files:** Create `services/gateway/src/jarvis_gateway/forward.py`; test `services/gateway/tests/unit/test_forward.py`.

**Interfaces (produces):**
- `filter_request_headers(headers: Sequence[tuple[str, str]]) -> list[tuple[str, str]]` (hop-by-hop, `Host`, `Content-Length`, `Expect`, names listed in `Connection`).
- `inject_credential_header(headers, credential: Credential, secret: str | None) -> list[tuple[str, str]]` (removes the slot header case-insensitively, then adds `Authorization: Bearer <secret>` or `<header_name>: <secret>`; other modes return the headers unchanged).
- `build_target_url(base_url: str, raw_path: str, raw_query: str, credential: Credential, secret: str | None) -> str`.
- `filter_response_headers(headers) -> list[tuple[str, str]]` (hop-by-hop only, order and repeats kept).
- `wants_stream(accept: str | None) -> bool`.

- [ ] **Step 1: Write failing tests (table-driven):** request header filtering incl. `Connection: X-Foo, Keep-Alive` removing `X-Foo`, `Proxy-Authorization`, `proxy-x`, mixed case, repeated headers; injection for each mode (REQ-4.5-2 to -5) including removal of a client `authorization`/`AUTHORIZATION`/custom header with several values (REQ-4.5-6); `none` keeps the client's `Authorization`; `build_target_url`: `https://api.x.com` + `/v1/a` + `city=B` → `https://api.x.com/v1/a?city=B`; base with prefix `https://api.x.com/v1/`; empty path; trailing slash kept; `%2F` kept encoded; `..`, `%2e%2e`, `a/../../b` clamped under the base prefix; `//` collapsed; space, non-ASCII and `\x00` percent-encoded; raw query preserved byte for byte (`a=1&&b=%20&c`); query credential: existing `key=` segments (any encoding of the key) removed, others kept in order, secret URL-encoded (`a b&c=d` → `a%20b%26c%3Dd`), `?` omitted when the query becomes empty; response header filtering keeps repeated `Set-Cookie`, `Content-Encoding`, `Content-Length`; `wants_stream`: `text/event-stream`, `TEXT/EVENT-STREAM;q=0.9, */*`, `application/json` (false), `None`.
- [ ] **Step 2: Run, see them fail.** **Step 3: Implement.** **Step 4: Run, lint, mypy, coverage 100%.**
- [ ] **Step 5: Commit** `feat(gateway): header filtering, credential injection and target URL building (M1.8)`.

### Task 4: Forwarder and route

**Files:** Create `services/gateway/src/jarvis_gateway/forwarder.py`; modify `routes.py`, `app.py`; tests `unit/test_forwarder.py`, update `unit/test_gw_route.py`, `unit/test_app.py`.

**Interfaces:** `class Forwarder: __init__(self, policy: SsrfPolicy, vault: CredentialVault, *, max_body_bytes: int, transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None)`; `async def forward(self, request: Request, authenticated: Authenticated, request_id: str) -> Response`; `def retain(self, snapshot: Snapshot) -> None` (closes clients for origins no API uses); `async def aclose(self) -> None`. `build_gw_router(store: ConfigStore, forwarder: Forwarder | None = None)`: with no forwarder an authenticated request still answers 501 (the M1.5 behaviour, so old unit tests keep their meaning). `create_app(..., forwarder=...)`; `create_app_from_env` builds the vault, forwarder and store listeners, closes the forwarder on shutdown.

- [ ] **Step 1: Write failing tests** with `httpx.MockTransport` as the transport factory: success returns upstream status, body, repeated `Set-Cookie`, `Content-Encoding` untouched and adds `X-Jarvis-Request-Id`/`Source: upstream`/`Attempts: 1`; the upstream request has the right method, URL, query, injected credential, no JARVIS key or slot header, no hop-by-hop headers, `Host` equal to the upstream host; 4xx and 5xx pass through unchanged (REQ-4.4-7); redirects not followed; `Accept: text/event-stream` → 400 `JV_STREAMING_UNSUPPORTED` before auth (even with no key); `Content-Length` over the limit → 413 before auth; a chunked body over the limit → 413 and the upstream never called; exactly the limit passes; a lying `Content-Length` smaller than the body does not bypass the cap; `httpx.ConnectTimeout`/read timeout/handler sleeping past `deadline_ms` → 504 `JV_UPSTREAM_TIMEOUT`; `ConnectError` → 502; `DestinationBlockedError` from the transport → 403; `CredentialUnavailable` → 502 with the fixed message; error responses carry `X-Jarvis-Source: jarvis` and the request ID in header and body; one client per origin and reuse (counted via the factory); `retain` closes unused clients; HEAD and 204 responses have no body; the upstream response is closed when the body is fully sent.
- [ ] **Step 2: Run, see them fail.** **Step 3: Implement** (raw target from `request.scope`; body read via `request.stream()` with a running cap; response through `StreamingResponse` with a generator that closes the upstream response in `finally`).
- [ ] **Step 4: Run, lint, mypy, coverage within gates.**
- [ ] **Step 5: Commit** `feat(gateway): forward authenticated requests to the upstream (M1.8)`.

### Task 5: Functional suite against the mock upstream

**Files:** Create `services/gateway/tests/functional/conftest.py`, `test_forwarding_matrix.py`, `test_forwarding_security.py` (marker `functional`; the Makefile's `test-functional` placeholder is replaced by `$(PYTEST) -m functional`).

Setup: the real `mock_upstream` app on an ephemeral loopback port (uvicorn, as in `tools/mock_upstream` integration tests), the real gateway app (`create_app`) wired to a `ConfigStore` holding a hand-built snapshot (project, API per credential mode with a sealed secret from a test `AesGcmSecretBox`, keys), `SsrfPolicy.from_env({"JARVIS_SSRF_ALLOWLIST": "127.0.0.1/32, upstream.test"})` with a resolver stub mapping `upstream.test` to `127.0.0.1`, driven through `httpx.AsyncClient(transport=ASGITransport(app))`. Assertions read what the upstream received from `/__admin/calls`.

- [ ] **Step 1: Write the tests (RED against Task 4 if any gap shows):** table-driven matrix methods (GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS) × credential modes (`bearer`, `header`, `query`, `none`) × bodies (none, empty, JSON, 1 MiB binary, chunked) asserting exact method, path, query, injected credential, absence of the JARVIS key, body size and `Host` (REQ-4.1-1, REQ-4.5-2 to -6); client-supplied credential in the slot removed incl. repeated and mixed-case forms; JARVIS key (header, slot, repeated) never in `/__admin/calls` headers or query; 4xx/5xx and `Retry-After` passed through byte for byte (REQ-4.4-7); `body_bytes` filler response streamed unchanged; gzip response keeps its `Content-Encoding` and bytes; upstream 302 returned, not followed; path cases (`..`, `%2e%2e`, `%2F`, base prefix, trailing slash); 413 for declared and undeclared oversize with `JV_BODY_TOO_LARGE` and no upstream call; SSE `Accept` → 400; hung upstream → 504 within `deadline_ms`; refused port → 502; unknown slug and wrong key → 401 with no upstream call (REQ-4.1-2); `caplog` at DEBUG for every case: no secret, key, key hash or ciphertext in any record or response body; rotated key (vault built with another master key) → 502 with fixed message.
- [ ] **Step 2–4:** run, fix, `make test-functional`, lint, mypy.
- [ ] **Step 5: Commit** `test(gateway): functional forwarding matrix and key/secret secrecy (M1.8)`.

### Task 6: Compose wiring, SDK-style system test, docs

**Files:** Modify `deploy/docker-compose.yml` (gateway gets `JARVIS_MASTER_KEY`, `JARVIS_MAX_BODY_BYTES`), `deploy/docker-compose.test.yml` (placeholder master key identical to the api's), `deploy/.env.example`, root `pyproject.toml` (+ `uv.lock`: dev pin `requests==2.34.2`), `Makefile` (`test-functional`), `docs/secrets-rotation.md`, `docs/quickstart-dev.md`, `README.md`, `docs/traceability.md`, `docs/PROGRESS.md`; create `tests/system/test_change_one_base_url.py`.

- [ ] **Step 1: System test (the DoD story):** create project, API (`http://mock-upstream:8080`, bearer secret, allowlisted in the test stack) and key through the management API; a small `requests`-based client class `WeatherClient(base_url, api_key)` that calls `GET {base_url}/v1/current?city=Bangalore` with `Authorization: Bearer {api_key}` is pointed at the gateway (`http://127.0.0.1:18080/gw/mock`) with the JARVIS key as its API key; it works unchanged (200, JSON echo); the mock upstream's call log shows `Authorization: Bearer <the provider secret>`, no `jv_` value anywhere, path `/v1/current`, query `city=Bangalore`; revoking the key makes the same client get 401; a PATCH that rotates the provider secret is used by the next call within 1 s. API created with `jarvis_key_slot: "authorization"`.
- [ ] **Step 2:** wire compose, docs and the rotation note (restart the gateway after rotating); quickstart run against a dev stack updated (a real call through the gateway to a public example is not possible offline: the quickstart shows the 501→200 change using the test-profile mock upstream is not available in dev, so document the forwarding call and run it against a local `python -m http.server` upstream allowlisted with `JARVIS_SSRF_ALLOWLIST`).
- [ ] **Step 3:** `make lint typecheck`, `make coverage`, `make test-functional`, `make test-system`, `make test-security`, `make traceability COMPLETED_PHASE=0 TRACE_MARKERS="not perf"`; confirm REQ-3-4, REQ-4.1-1/-2, REQ-4.3-1/-2, REQ-4.4-1/-5/-7 and REQ-4.5-2 to -7 are covered and Checkpoint 0 rows still pass.
- [ ] **Step 4:** update `docs/PROGRESS.md`; commit `feat: change-one-base-URL end to end, compose and docs (M1.8)`.

## Self-Review

- **Coverage:** REQ-4.1-1/-2 (Tasks 4, 5), REQ-4.3-1 (route, existing + Task 4), -2 (Task 4, 5), -6 (no Postgres on the hot path: Task 4 uses only the in-memory snapshot), REQ-4.4-1/-5/-7 (Task 4, 5), REQ-4.5-2 to -7 (Tasks 2, 3, 5), REQ-3-4 (Tasks 4, 5), D11 (Task 4, 5), M1.5's moved test (Task 5).
- **Types:** `ApiEntry.sealed`, `CredentialVault.secret_for`, `CredentialUnavailable`, `Forwarder.forward/retain/aclose`, `build_gw_router(store, forwarder)` named identically across tasks.
- **Open for the user:** the gateway holds `JARVIS_MASTER_KEY` and needs a restart after key rotation; sealed secrets travel in the snapshot file; a dev-only `requests` pin; the time-limit semantics until M2.1.
