# M1.9 Telemetry Emitter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every request that reaches `/gw` produces one schema-v1 event on the Redis Stream `telemetry`, emitted without ever blocking the request path. With Redis down or the queue full, events are dropped and counted (`telemetry_dropped_total`) and requests are unaffected. An A/B micro-benchmark proves telemetry adds under 0.5 ms p99.

**Architecture:** `jarvis_core.telemetry` owns the event schema (`SCHEMA_VERSION = 1`, `RequestEvent`, JSON encoding) and its snapshot. `jarvis_gateway.telemetry.RedisTelemetrySink` implements the existing `TelemetrySink` protocol: `emit()` is synchronous and O(1) (append to a bounded deque, set an asyncio event); one background task drains the deque in batches and sends them with a single pipelined `XADD ... MAXLEN ~ 1000000` round trip under a hard timeout. The route builds a `RequestRecord` at ingress, the forwarder fills in upstream facts, and the record is turned into an event exactly once when the response is finished (after the last body chunk for streamed responses, immediately for errors).

**Tech Stack:** `redis==8.0.1` (already pinned for api; add to `services/gateway` deps), `jarvis_core`, pytest-benchmark. No new infrastructure.

**Spec:** `docs/SPEC.md` §6.1, §6.3, §4.3 step 11 (finalize), §8 (Redis row), §11.2; `docs/IMPLEMENTATION.md` M1.9, B.5.4; requirements REQ-3-2, REQ-6.1-1, REQ-6.1-2, REQ-6.1-5, REQ-6.3-1, REQ-6.3-2, REQ-6.3-4, REQ-8-1 (telemetry half), REQ-11.2-4.

**Prerequisite:** M1.8 merged (`0ea3df7`). Branch `feat/m1.9-telemetry` from `main` (`dd5d40d`). The `bench` micro alert from the M1.8 merge is resolved: the next `bench` run on `main` (after PR #26) passed.

## Global Constraints

- `emit()` never awaits, never does I/O, never raises, and returns `False` (counted) when it drops (`TelemetrySink` in `jarvis_core.interfaces`).
- Defaults from the spec: queue 10,000; stream `telemetry`; `MAXLEN ~ 1,000,000`. Batched, pipelined `XADD`.
- Events never contain secrets, query strings, header values, request or response bodies, the JARVIS key or its hash, provider credentials, or upstream URLs (SPEC §11.2, REQ-11.2-4). Only ids, method, templated route, numbers and codes.
- Hot path adds zero Redis round trips (telemetry is off the request path), zero Postgres queries.
- Changing the schema requires bumping `v` and updating `tests/contract/telemetry.snapshot.json` in the same change (B.5.4).
- Dependencies pinned exactly. Coverage gates: core ≥ 95/90, gateway ≥ 90/85, no drop over 1 point vs `main`. Core and gateway are mypy-strict. One type marker per test; requirement tests carry `@pytest.mark.req(...)`.
- No Claude attribution in commits or the PR body (user memory).

## Decisions made here (not in the spec)

- **Stream entry shape:** one field, `event`, holding the compact JSON of the §6.1 object (`{"event": "<json>"}`). The worker (P4) parses one field; adding event kinds later (`kind: "event"` records, P2) needs no stream-layout change. Request records carry `"kind": "request"` as an addition to the §6.1 example so the two kinds are distinguishable; the snapshot pins it. *Cost if wrong: one extra key in `v: 1`, no consumer exists yet.*
- **What is emitted:** every request that gets a `X-Jarvis-Request-Id`, including ones rejected before authentication (ids that are not known are `null`): unknown or invalid key (`JV_AUTH_INVALID`), 413, 400 streaming, 503 not ready, 404/unknown API. This lets the P4 dashboard show rejected traffic. Rejected-before-authentication events carry `project_id`/`key_id`/`api_id` only when authentication succeeded. Not emitted: `/healthz`, `/readyz`.
- **Field values until later modules exist:** `source` is `"upstream"` or `"jarvis"` (errors/rejections), `cache`, `cache_key_hash`, `dedup_role`, `circuit` are `null` (M2/M3 fill them), `attempts` is 1 after an upstream attempt was made and 0 otherwise, `upstream_latency_ms` is time to response headers (`null` if no attempt), `latency_ms` is ingress to last body byte, `req_bytes` the bytes read from the client body, `resp_bytes` the bytes sent to the client. `status` is the status sent to the client; a client that disconnects mid-body is recorded with the status already sent and the `resp_bytes` actually delivered.
- **Route:** `RouteTemplater` (M1.6) is wired here, one instance per `(api_id, version)` held in a small registry that follows the snapshot (built lazily, dropped by a `ConfigStore` listener, like `Forwarder.retain`). Requests that fail before an API is resolved use route `null`.
- **Timestamps:** `ts` is UTC, millisecond precision, `Z` suffix, taken from the injected `Clock` at ingress.
- **Flushing:** the flusher wakes when events arrive, takes up to `batch_size` (default 500) events, waits at most `flush_interval_s` (default 0.05) to let a batch fill, then sends one pipeline with a 1 s command timeout. A failed or timed-out batch is dropped and counted (not retried: a retry would only grow the backlog while Redis is down); the connection is retried with capped backoff (0.1 s to 5 s). On shutdown the sink flushes what is queued within a 2 s limit, then counts the rest as dropped.
- **Counter and visibility:** `dropped_total` on the sink; `/readyz` gains `telemetry: {"queue": n, "dropped_total": n}` (numbers only; status is unchanged, SPEC §8 says Redis down means telemetry dropped, not degraded readiness). A WARNING is logged at most once per 30 s with the new drop count, never per event. Prometheus `/metrics` is P4 (REQ-6.4-1), not here.
- **Settings:** `JARVIS_TELEMETRY_QUEUE_SIZE` (default 10000, min 1), `JARVIS_TELEMETRY_BATCH_SIZE` (default 500, min 1), `JARVIS_TELEMETRY_STREAM_MAXLEN` (default 1000000, min 1). The stream name is a constant. `JARVIS_TELEMETRY_ENABLED=false` swaps in a no-op sink (used as the "off" arm of the benchmark and for debugging; documented in `deploy/.env.example`).
- **Same Redis, own connection:** the sink uses its own `redis.asyncio` client (socket and connect timeouts 1 s, no retries on commands), separate from the config manager's pub/sub client so a telemetry stall can never delay `config:invalidated`.

## Review Focus

- `emit()` cannot block or raise under any input, including an unserialisable event, a closed sink, a full queue and a flusher that has died (Task 2).
- Redis down at startup, Redis dying mid-batch, Redis hanging (accepts the connection, never answers): requests keep their latency and succeed; drops are counted exactly once (Task 2, 4, 6).
- Exactly one event per request: errors, 204/HEAD with no body, client disconnect mid-stream, an exception inside the body stream (Task 3).
- No secret or query string ever reaches an event or a log line, tested with a sentinel credential and a sentinel in the query (Task 1, 3).
- Event construction is pure and total over every outcome in the table in Task 1 (Task 1).
- The flusher task is started and stopped by the app lifespan and cannot leak across tests (Task 2, 4).

---

### Task 1: Event schema and snapshot (core)

**Files:** Create `packages/core/src/jarvis_core/telemetry.py`, `packages/core/tests/unit/test_telemetry_events.py`, `tests/contract/telemetry.snapshot.json`, `tests/contract/test_telemetry_schema.py`.

**Interfaces:** `SCHEMA_VERSION = 1`; `RequestEvent` (frozen dataclass) with the §6.1 fields plus `kind="request"`; `RequestEvent.to_dict()` (stable key order) and `encode(event) -> str` (compact JSON, `ensure_ascii=True`); `event_from_outcome(...)` builds an event from ids, method, route, status, source, error code, latencies, byte counts and `ts`, validating types (non-negative ints, `source` in `{"upstream","jarvis"}`, `error_code` is `None` or starts with `JV_`).

- [ ] **Step 1: Write failing tests.** One case per outcome: upstream 200, upstream 5xx passthrough (`error_code` null, source `upstream`), upstream timeout (`JV_UPSTREAM_TIMEOUT`, attempts 1, `upstream_latency_ms` null), unreachable, auth invalid (ids null, attempts 0, source `jarvis`), body too large, streaming rejected, not ready, unknown API, blocked destination. Every event has exactly the §6.1 keys plus `kind`, in the documented order, with `v == 1`. Negative or non-int latency and bytes raise `ValueError`. `ts` format `YYYY-MM-DDTHH:MM:SS.mmmZ`. Property test (hypothesis): any combination of valid inputs encodes to JSON that round-trips and contains none of the input path/query text (route is templated, never raw). The contract test compares `RequestEvent` field names, order and types with `tests/contract/telemetry.snapshot.json` and fails with a message naming "bump `v` and update the snapshot" when they differ; a second test fails if the snapshot changed but `SCHEMA_VERSION` did not (snapshot stores the version).
- [ ] **Step 2: Run, see them fail.**
- [ ] **Step 3: Implement** and generate the snapshot with a `make telemetry-snapshot` target (mirrors `make openapi-snapshot`).
- [ ] **Step 4: Run, lint, mypy; commit** `feat(core): telemetry event schema and snapshot (M1.9)`.

### Task 2: Bounded, non-blocking Redis sink (gateway)

**Files:** Create `services/gateway/src/jarvis_gateway/telemetry.py`, `services/gateway/tests/unit/test_telemetry_sink.py`, `services/gateway/tests/integration/test_telemetry_redis.py`; modify `services/gateway/pyproject.toml` (pin `redis==8.0.1`), `uv.lock`.

**Interfaces:** `RedisTelemetrySink(redis_url, *, queue_size=10_000, batch_size=500, flush_interval_s=0.05, maxlen=1_000_000, stream="telemetry", clock, client=None)` implementing `TelemetrySink`; `async start()`, `async aclose()`; `queue_depth`, `dropped_total`; `NoopTelemetrySink`. The Redis client is injectable so unit tests use a fake with controllable failures and delays.

- [ ] **Step 1: Write failing tests (unit, fake Redis client):**
  - `emit` on a full queue returns `False` and increments `dropped_total` by exactly 1; the queue never exceeds `queue_size`; the oldest events are kept (newest dropped), so order and count are predictable.
  - `emit` before `start()`, after `aclose()`, with a non-serialisable value, and with the flusher cancelled returns `False`/`True` as documented and never raises or awaits (asserted by calling it from a plain sync function).
  - Batching: 1,200 events with batch size 500 produce 3 pipeline calls of ≤ 500 `XADD`, each with `MAXLEN ~ 1000000`, `fields == {"event": <json>}`.
  - Redis error on a batch: that batch is counted dropped, later events still flow once the client recovers, and the loop applies capped backoff (fake clock/sleep).
  - Redis hang: a pipeline that never returns is cut at the 1 s command timeout, its batch dropped, `emit` latency unaffected (a loop of 10,000 emits while the flusher is stuck completes without yielding).
  - Shutdown flushes within 2 s, then counts leftovers as dropped; `aclose` is idempotent.
  - The drop WARNING is rate limited (many drops, one log line per 30 s of fake time) and contains counts only.
- [ ] **Step 2: Write failing integration tests (real Redis):** events appear in stream `telemetry` with the exact encoded JSON; `XLEN` stays near `MAXLEN` when more are written (approximate trim); with a bad `REDIS_URL` or Redis stopped mid-run, `emit` keeps returning quickly and `dropped_total` rises; after Redis returns, new events land.
- [ ] **Step 3: Run, see them fail.**
- [ ] **Step 4: Implement.** `emit` = `if len(deque) >= queue_size: drop` else `append` and `event.set()`; encoding happens in the flusher, not in `emit` (keeps the hot path to a dict reference; the route hands over an already-built immutable `RequestEvent`, whose `to_dict()` is called in the flusher).
- [ ] **Step 5: Run unit + integration, lint, mypy; commit** `feat(gateway): bounded non-blocking telemetry sink (M1.9)`.

### Task 3: Record, route templating and one-event-per-request wiring

**Files:** Create `services/gateway/src/jarvis_gateway/record.py`, `services/gateway/src/jarvis_gateway/routing.py` (templater registry), tests `services/gateway/tests/unit/test_record.py`, `services/gateway/tests/unit/test_routing.py`, `services/gateway/tests/functional/test_telemetry_events.py`; modify `routes.py`, `forwarder.py`, `app.py`.

**Interfaces:** `RequestRecord` (mutable, created at ingress: request id, ingress monotonic time, wall `ts`, method, ids, route; `mark_attempt()`, `mark_headers_received()`, `add_response_bytes(n)`, `add_request_bytes(n)`, `finish(status, source, error_code)` which builds and emits the event once and ignores a second call). `TemplaterRegistry.route_for(entry, raw_path) -> str` (cap and `{other}` bucket from M1.6; follows snapshot installs).

- [ ] **Step 1: Write failing tests:**
  - Unit: `finish` emits exactly once however often it is called; calls after `finish` are ignored; byte and latency arithmetic with a fake clock; a sink that returns `False` or raises does not change the response (guarded with a narrow `except` that logs once and counts nothing extra).
  - Unit: registry builds one templater per `(api_id, version)`, discards the ones for gone APIs on snapshot install, and maps `/v1/forecast/paris` with an explicit pattern `/v1/forecast/{city}` to that template, and `/v1/users/42` to `/v1/users/{id}`.
  - Functional (real mock upstream, `InMemoryTelemetrySink`): the table of outcomes from Task 1 each produces exactly one event with the expected `status`, `source`, `error_code`, `attempts`, ids; a streamed 5 MB response is finished after the last chunk (`resp_bytes` exact, `latency_ms` ≥ body time); client disconnect mid-stream still yields one event; HEAD and 204 give `resp_bytes == 0`; a 4xx/5xx passthrough has `error_code` null.
  - Security/functional: with a sentinel provider credential, a sentinel JARVIS key and a sentinel query value, the serialised events and all captured log lines contain none of them, and the route is templated (no raw ids).
  - Requests rejected before authentication are emitted with null ids and route; `/healthz` and `/readyz` emit nothing.
- [ ] **Step 2: Run, see them fail.**
- [ ] **Step 3: Implement.** `build_gw_router(store, forwarder, sink, templates)`; the route creates the record first, passes it to `forwarder.forward(...)`, and wraps the streaming body so `finish` runs in `finally` after the last chunk or on cancel; error responses call `finish` before returning.
- [ ] **Step 4: Run gateway unit + functional, lint, mypy; commit** `feat(gateway): one telemetry event per request (M1.9)`.

### Task 4: Settings, lifespan, readiness, compose

**Files:** Modify `services/gateway/src/jarvis_gateway/settings.py`, `app.py`, tests `test_settings.py`, `test_app.py`; `deploy/.env.example`, `README.md`, `docs/quickstart-dev.md`, `docs/contributing.md` if commands change.

- [ ] **Step 1: Failing tests:** each new setting parses, rejects values below the minimum with a message naming the variable (not the value of any URL); `JARVIS_TELEMETRY_ENABLED=false` gives the no-op sink; lifespan starts the sink before serving and stops it (flushing) on shutdown; `/readyz` includes `telemetry.queue` and `telemetry.dropped_total` as integers and its overall status is unchanged when Redis is down.
- [ ] **Step 2: Implement; run; commit** `feat(gateway): telemetry settings, lifecycle and readiness counts (M1.9)`.

### Task 5: Stack tests (system, chaos)

**Files:** `tests/system/test_telemetry_stream.py`, `tests/chaos/test_redis_down_telemetry.py`.

- [ ] **Step 1: Failing tests (compose test stack):** after N proxied requests the stream `telemetry` holds N events with the exact schema (compared with the snapshot keys), `request_id` equal to the `X-Jarvis-Request-Id` header, `key_id`/`api_id`/`project_id` matching the created objects. Chaos: stop the Redis container, send 50 requests, all succeed with the same status and body as before, `/readyz` `telemetry.dropped_total` grows, the gateway never crashes; start Redis, new events arrive. Tag `REQ-6.1-1`, `REQ-6.1-2`, `REQ-6.3-2`, `REQ-8-1`, `REQ-3-2`.
- [ ] **Step 2: Run on a Docker host (`make test-system test-chaos`); fix; commit** `test: telemetry stream and Redis-down behaviour (M1.9)`.

### Task 6: A/B benchmark (the DoD)

**Files:** Create `services/gateway/tests/bench/test_telemetry_overhead.py`; modify `benchmarks/budgets.yaml` (`telemetry_overhead_p99`, ms, lower, budget 0.5, spec "M1.9 DoD"), `Makefile` (`bench-telemetry`), `.github/workflows/bench.yml` (job chained like `propagation`, Redis service), `BENCHMARKS.md`.

- [ ] **Step 1: Write the benchmark** (`perf` marker): drive the real app in process (httpx `ASGITransport`) against a stub upstream transport with a fixed 1 ms response, 5,000 timed requests per arm after warm-up, arms interleaved in blocks to cancel drift: (A) `NoopTelemetrySink`, (B) `RedisTelemetrySink` against a real Redis, and (C) the same sink with Redis unreachable (worst case: drops). Report p50/p99 per arm and `telemetry_overhead_p99 = p99(B) - p99(A)`; fail if it is ≥ 0.5 ms for B or C. Also assert `emit()` itself stays under 20 µs at p99.
- [ ] **Step 2: Run locally 3 times** (variance), record numbers in `BENCHMARKS.md` as measured, not invented. If B's difference is near the budget, investigate (batching interval, event construction) before relaxing anything: the budget comes from IMPLEMENTATION.md and is not changed without asking.
- [ ] **Step 3: Commit** `bench: telemetry on/off A/B (M1.9)`.

### Task 7: Traceability, docs, progress, PR

- [ ] `make traceability COMPLETED_PHASE=0 TRACE_MARKERS="not perf"` regenerates `docs/traceability.md`; confirm REQ-3-2, REQ-6.1-1/-2/-5, REQ-6.3-1/-2/-4, REQ-8-1 (telemetry half), REQ-11.2-4 gain tests and Checkpoint 0 rows stay `pass`.
- [ ] README (telemetry section: stream name, schema pointer, drop behaviour, settings), `deploy/.env.example`, `docs/quickstart-dev.md` (`redis-cli XRANGE telemetry - + COUNT 3` after a call).
- [ ] Full run: `make lint typecheck`, `make coverage`, `make test-functional`, `make security-scan`, `make test-system test-e2e test-chaos test-security` on a Docker host, `make bench-telemetry`.
- [ ] Mutation spot checks, each must make a named test fail: `emit` awaits or raises on full queue; drop not counted; `MAXLEN` removed; query string or key put into an event; second `finish` emits again; flush timeout removed (hang test).
- [ ] Update `docs/PROGRESS.md` (M1.9 entry, Next = Checkpoint 1), open the PR, wait for CI, merge, check `main` CI.
- [ ] **Stop.** Checkpoint 1 is run only after the user says so.

## Open points for the user (defaults chosen above)

1. Emit events for requests rejected before authentication (default: yes, null ids).
2. Stream entry as a single `event` JSON field plus `kind` (default: yes).
3. Drop-newest when the queue is full (default) versus drop-oldest.
4. `JARVIS_TELEMETRY_ENABLED` switch kept as a user-visible setting (default: yes).
