# JARVIS Requirements

Every testable requirement in `SPEC.md`, with a stable ID (`REQ-<spec-section>-<n>`).
Functional tests link to these with `@pytest.mark.req("REQ-…")`; `make traceability` builds
`docs/traceability.md` from this file. The **Phase** column is the phase whose checkpoint
requires the requirement to be covered by a test (IMPLEMENTATION §B.4).

Rules: IDs are never reused or renumbered. If the spec changes, add a new ID and mark the old
row `(withdrawn)`. Keep the table format: `| REQ-x.y-n | text | Pn |`, the traceability
script parses it.

## §1 Goals

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-1.1-1 | `git clone` → `cp .env.example .env` → `docker compose up -d` yields a working system with no external accounts (G1). | P7 |
| REQ-1.1-2 | An integration normally needs only a base-URL change and a key swap (G2). | P1 |
| REQ-1.1-3 | The AI provider can be switched between Claude and Gemini at runtime from settings (G6). | P6 |

## §2 Scope decisions

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-2-1 | One install is one organization containing multiple projects; roles are `admin` and `member` (D1). | P4 |
| REQ-2-2 | The gateway is not publicly exposed by default (D2). | P0 |
| REQ-2-3 | When Redis is unavailable the gateway fails open: in-memory per-instance rate limiter, cache and dedup skipped, circuit state in process memory (D3). | P2 |
| REQ-2-4 | Fail-open behaviour is controlled by one global setting (D3). | P2 |
| REQ-2-5 | Non-2xx responses are never cached (D4). | P3 |
| REQ-2-6 | Responses with `Set-Cookie` are never cached (D4). | P3 |
| REQ-2-7 | Cache TTL is configured per endpoint, with an optional "honor upstream `Cache-Control`" toggle (D4). | P3 |
| REQ-2-8 | Request/response bodies are not logged by default; body sampling is opt-in per API and always redacted (D5). | P4 |
| REQ-2-9 | Config changes are versioned in Postgres, pushed via Redis pub/sub and applied in ≤ 1 s; rollback is supported (D7). | P5 |
| REQ-2-10 | Dashboard auth is local username/password with server-side sessions (D8). | P4 |
| REQ-2-11 | Path templating uses user-defined patterns with automatic numeric/UUID fallback (D9). | P1 |
| REQ-2-12 | Credential injection supports bearer, custom header or query parameter, and the client's credential is always stripped (D10). | P1 |
| REQ-2-13 | Streaming requests are rejected with a clear error when detected (D11). | P1 |
| REQ-2-14 | Only Claude or Gemini are supported as LLM; the key is bring-your-own and optional; an optional spend cap is off by default (D12). | P6 |
| REQ-2-15 | The AI layer never makes configuration changes autonomously (§1.2). | P6 |

## §3 System overview

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-3-1 | The gateway is reachable at `/gw/{api_slug}/{upstream_path}` on the private network (port 8080). | P1 |
| REQ-3-2 | The gateway emits telemetry to a Redis Stream and never blocks on it. | P1 |
| REQ-3-3 | Redis holds only ephemeral state; losing it loses no durable data. | P2 |
| REQ-3-4 | Upstream calls use pooled connections, injected credentials and an SSRF-checked destination. | P1 |

## §4 Gateway

### §4.1 Endpoint

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-4.1-1 | `{METHOD} /gw/{api_slug}/{upstream_path}?{query}` is forwarded to `{base_url}/{upstream_path}?{query}`. | P1 |
| REQ-4.1-2 | An unknown `api_slug` is rejected without calling any upstream. | P1 |

### §4.2 JARVIS authentication

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-4.2-1 | JARVIS keys have the form `jv_live_<random>` and are stored as a SHA-256 hash, scoped to one project. | P1 |
| REQ-4.2-2 | The gateway accepts the key in the `X-Jarvis-Key` header. | P1 |
| REQ-4.2-3 | The gateway accepts a `jv_`-prefixed key in the provider's credential slot configured for that API and replaces it with the real credential. | P1 |
| REQ-4.2-4 | A missing, unknown or revoked key is rejected with `JV_AUTH_INVALID`. | P1 |
| REQ-4.2-5 | A key restricted to specific APIs cannot call any other API. | P1 |
| REQ-4.2-6 | Keys are cached in gateway memory, and revocation takes effect in ≤ 1 s. | P1 |

### §4.3 Request pipeline

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-4.3-1 | Each request gets a ULID `request_id` and its deadline clock starts at ingress. | P1 |
| REQ-4.3-2 | A body larger than `max_body_bytes` (default 10 MB) is rejected with `JV_BODY_TOO_LARGE`. | P1 |
| REQ-4.3-3 | The pipeline runs in the normative order: ingress, authenticate and resolve, inbound limit, cache, dedup, circuit, upstream quota, upstream call, fallback, finalize. | P3 |
| REQ-4.3-4 | The inbound rate limit rejects with 429 and `Retry-After`. | P2 |
| REQ-4.3-5 | The pre-upstream Redis steps (3–7) take at most two Redis round trips. | P3 |
| REQ-4.3-6 | The hot path makes zero Postgres queries. | P1 |
| REQ-4.3-7 | The upstream quota bucket counts only calls that actually leave the gateway. | P2 |

### §4.4 Response headers and errors

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-4.4-1 | Every response carries `X-Jarvis-Request-Id`. | P1 |
| REQ-4.4-2 | `X-Jarvis-Cache` is one of `HIT`, `MISS`, `STALE`, `BYPASS`. | P3 |
| REQ-4.4-3 | `X-Jarvis-Attempts` reports the number of upstream attempts and `X-Jarvis-Circuit` the circuit state when served. | P2 |
| REQ-4.4-4 | `X-Jarvis-Source` is one of `upstream`, `cache`, `dedup`, `fallback`, `jarvis`. | P3 |
| REQ-4.4-5 | JARVIS-generated errors have the JSON body `{"error": {"code", "message", "request_id"}}`. | P0 |
| REQ-4.4-6 | The JARVIS error codes are exactly `JV_AUTH_INVALID`, `JV_RATE_LIMITED`, `JV_QUOTA_EXCEEDED`, `JV_CIRCUIT_OPEN`, `JV_UPSTREAM_TIMEOUT`, `JV_UPSTREAM_UNREACHABLE`, `JV_BODY_TOO_LARGE`, `JV_STREAMING_UNSUPPORTED`, `JV_DESTINATION_BLOCKED`. | P0 |
| REQ-4.4-7 | Errors returned by the upstream are passed through unchanged. | P1 |

### §4.5 Credential injection

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-4.5-1 | Credential modes are `bearer`, `header` (needs `header_name`), `query` (needs `param_name`) and `none`; invalid field combinations are rejected. | P0 |
| REQ-4.5-2 | `bearer` sets `Authorization: Bearer <secret>`. | P1 |
| REQ-4.5-3 | `header` sets `<header_name>: <secret>`. | P1 |
| REQ-4.5-4 | `query` sets `?<param_name>=<secret>`. | P1 |
| REQ-4.5-5 | `none` injects nothing. | P1 |
| REQ-4.5-6 | Any client-supplied value in the injected slot is always removed before injection. | P1 |
| REQ-4.5-7 | Secrets are stored encrypted and decrypted into gateway memory only at config load. | P1 |

### §4.6 Timeouts and deadline

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-4.6-1 | Defaults: `deadline_ms` 5000, `attempt_timeout_ms` 2000, `min_attempt_ms` 300. | P0 |
| REQ-4.6-2 | `attempt_timeout_ms` covers connect and read of one upstream attempt. | P2 |
| REQ-4.6-3 | A new attempt starts only if the remaining deadline is at least `min_attempt_ms`. | P2 |
| REQ-4.6-4 | Dedup followers wait at most until their own deadline. | P3 |
| REQ-4.6-5 | The total time for a request, across all attempts, never exceeds `deadline_ms`. | P2 |

### §4.7 Retries

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-4.7-1 | Retryable conditions are connection error, attempt timeout, 502, 503, 504 and 429. | P2 |
| REQ-4.7-2 | For 429 and 503 the gateway honors `Retry-After` when it fits in the remaining deadline, otherwise it does not retry. | P2 |
| REQ-4.7-3 | `POST` and `PATCH` are retried only when the API policy sets `idempotency.header`. | P2 |
| REQ-4.7-4 | `GET`, `HEAD`, `OPTIONS`, `PUT` and `DELETE` are retryable methods. | P2 |
| REQ-4.7-5 | With an idempotency header, JARVIS generates the key once per request and sends the same key on every attempt. | P2 |
| REQ-4.7-6 | Backoff is `random(0, min(cap, base × 2^attempt))` (exponential, full jitter), defaults base 100 ms and cap 2000 ms. | P2 |
| REQ-4.7-7 | Retries may not exceed `retry_budget_ratio` (default 0.2) of requests in a rolling 60 s window per API. | P2 |
| REQ-4.7-8 | When the retry budget is exhausted, requests fail without retrying. | P2 |
| REQ-4.7-9 | `max_attempts` bounds the number of attempts. | P2 |

### §4.8 Circuit breaker

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-4.8-1 | Circuit state is shared across gateway instances through Redis; each instance keeps a local copy refreshed every 1 s and on pub/sub events. | P2 |
| REQ-4.8-2 | 5xx responses, timeouts and connection errors count as failures. | P2 |
| REQ-4.8-3 | 4xx responses (including 429) are not counted as failures. | P2 |
| REQ-4.8-4 | CLOSED → OPEN when the failure rate ≥ `failure_rate_threshold` (default 50%) over `window_s` (default 30) with at least `min_calls` (default 20) calls. | P2 |
| REQ-4.8-5 | OPEN → HALF_OPEN after `open_duration_s` (default 30). | P2 |
| REQ-4.8-6 | In HALF_OPEN exactly one probe is allowed through (Redis lock). | P2 |
| REQ-4.8-7 | A successful probe moves HALF_OPEN → CLOSED. | P2 |
| REQ-4.8-8 | A failed probe moves HALF_OPEN → OPEN with the open duration doubled up to `max_open_duration_s` (default 300). | P2 |
| REQ-4.8-9 | Every state transition emits a telemetry event. | P2 |
| REQ-4.8-10 | While OPEN, requests fail fast without calling the upstream. | P2 |
| REQ-4.8-11 | `max_open_duration_s` must be ≥ `open_duration_s`. | P0 |

### §4.9 Cache

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-4.9-1 | A response is cache-eligible only if the method is GET or HEAD, the status is 2xx, there is no `Set-Cookie`, the body is ≤ `max_object_bytes` (default 1 MB) and the route has caching enabled. | P3 |
| REQ-4.9-2 | Responses with `Set-Cookie` are never cached. | P3 |
| REQ-4.9-3 | The cache key is `cache:{project}:{api}:{sha256(method\|route_path\|sorted_query\|vary_headers)}`. | P3 |
| REQ-4.9-4 | `vary_headers` is configurable per API and defaults to `Accept` and `Accept-Language`. | P3 |
| REQ-4.9-5 | TTL is set per route with a per-API default. | P3 |
| REQ-4.9-6 | With `honor_cache_control` on, upstream `max-age` and `no-store` override the configured TTL. | P3 |
| REQ-4.9-7 | Entries are kept `stale_ttl_s` past expiry and served only as a fallback with `X-Jarvis-Cache: STALE`. | P3 |
| REQ-4.9-8 | Cache entries can be invalidated manually by API, by route, or entirely. | P3 |
| REQ-4.9-9 | Query parameter order does not affect the cache key. | P3 |

### §4.10 Deduplication

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-4.10-1 | Deduplication applies to cacheable requests only. | P3 |
| REQ-4.10-2 | Concurrent identical requests in the same process await a single in-flight call (singleflight keyed by cache key). | P3 |
| REQ-4.10-3 | Across instances, a leader takes `SET inflight:{key} NX PX {deadline}` and followers wait on `done:{key}` then read from the cache. | P3 |
| REQ-4.10-4 | If the leader dies, the lock expires at the deadline and followers call the upstream themselves. | P3 |
| REQ-4.10-5 | Each `dedup_follower` is counted as an avoided upstream call. | P3 |
| REQ-4.10-6 | 1,000 identical concurrent requests produce one upstream call. | P3 |

### §4.11 Rate limiting

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-4.11-1 | Both limiters are atomic token buckets (refill and take in one Lua script). | P2 |
| REQ-4.11-2 | The inbound limiter is scoped per (project, key, api) and is off by default. | P2 |
| REQ-4.11-3 | The upstream quota limiter is scoped per API and is off by default. | P2 |
| REQ-4.11-4 | If Redis is down each instance uses an in-memory bucket of `limit ÷ expected_instances` (configurable); requests keep flowing. | P2 |
| REQ-4.11-5 | A token bucket starts full and never holds more than its capacity. | P0 |
| REQ-4.11-6 | A rejected take does not consume tokens. | P0 |
| REQ-4.11-7 | The bucket parameters (`capacity`, `refill_per_s`) must be positive. | P0 |

### §4.12 Fallback

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-4.12-1 | With `serve_stale` on, a stale cache entry is returned first if one exists. | P3 |
| REQ-4.12-2 | Otherwise `fallback_api` repeats the pipeline from step 6 against the other API with the same path. | P2 |
| REQ-4.12-3 | A fallback API's own fallback is never followed (no chains). | P2 |
| REQ-4.12-4 | Otherwise a controlled error with the appropriate `JV_*` code is returned. | P2 |
| REQ-4.12-5 | An API cannot name itself as its fallback. | P0 |

### §4.13 SSRF protection

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-4.13-1 | Upstream hosts are resolved at config save and re-validated at connect time. | P1 |
| REQ-4.13-2 | The connection is pinned to the validated IP (no DNS rebinding). | P1 |
| REQ-4.13-3 | Loopback, RFC 1918, link-local (incl. 169.254.169.254), CGNAT, IPv6 ULA and link-local, multicast and the JARVIS containers are blocked by default. | P1 |
| REQ-4.13-4 | An admin-managed allowlist (CIDRs or hosts) permits internal upstreams. | P1 |
| REQ-4.13-5 | Only `https` upstream URLs are allowed by default; `http` requires the host on the allowlist. | P1 |
| REQ-4.13-6 | A blocked destination is rejected with `JV_DESTINATION_BLOCKED`. | P1 |
| REQ-4.13-7 | Upstream redirects are not followed by default; when enabled, every hop is SSRF-checked (§17). | P1 |

### §4.14 Path templating

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-4.14-1 | Metrics are grouped by route template, not raw path. | P1 |
| REQ-4.14-2 | Explicit patterns configured per API are matched first. | P1 |
| REQ-4.14-3 | Otherwise segments that are purely numeric, UUIDs, or longer than 24 characters of hex/base64-like text become `{id}`. | P1 |
| REQ-4.14-4 | Templates per API are capped at 500 by default; overflow goes to an `{other}` bucket. | P1 |

## §5 Configuration

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-5.1-1 | The canonical `ApiDependency` JSON of §5.1 is accepted, and unknown fields are rejected. | P0 |
| REQ-5.1-2 | `attempt_timeout_ms` must be ≤ `deadline_ms`. | P0 |
| REQ-5.1-3 | Retry `cap_ms` must be ≥ `base_ms`. | P0 |
| REQ-5.1-4 | Retry `budget_ratio` must satisfy 0 < ratio ≤ 1. | P0 |
| REQ-5.1-5 | Config can be imported and exported as JSON. | P4 |
| REQ-5.2-1 | Every save creates an immutable `ApiPolicyVersion` with author, diff and reason. | P5 |
| REQ-5.2-2 | The reason is required when the change comes from an AI recommendation. | P6 |
| REQ-5.2-3 | After a save commits, the API publishes `config:changed {api_id, version}` and gateways swap the policy atomically. | P5 |
| REQ-5.2-4 | Gateways do a full resync every 60 s and at startup. | P5 |
| REQ-5.2-5 | Rollback creates a new version copied from an old one; history is never rewritten. | P5 |

## §6 Telemetry

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-6.1-1 | Events go to the Redis Stream `telemetry` with `MAXLEN ~ 1,000,000`. | P1 |
| REQ-6.1-2 | Each request event follows the §6.1 schema (`v`, `request_id`, `ts`, ids, `method`, `route`, `status`, `source`, `cache`, `cache_key_hash`, `attempts`, latencies, bytes, `circuit`, `error_code`, `dedup_role`). | P1 |
| REQ-6.1-3 | Circuit transitions, retry-budget exhaustion and fallback use are emitted as separate `kind: "event"` records. | P2 |
| REQ-6.1-4 | `cache_key_hash` is a one-way hash and no parameters are stored. | P3 |
| REQ-6.1-5 | Changing the event schema requires bumping `v` and updating the snapshot (B.5). | P1 |
| REQ-6.2-1 | Retention defaults: `request_events` 7 days, `metrics_1m` 30 days, `metrics_1h` 365 days, `gateway_events` 90 days. | P4 |
| REQ-6.2-2 | Retention periods are configurable via environment variables. | P4 |
| REQ-6.2-3 | Rollups store counts, error counts, cache hits, dedup followers, attempts, bytes and latency percentile sketches. | P4 |
| REQ-6.3-1 | Emitting telemetry is non-blocking, through a bounded queue (default 10,000) flushed in batches. | P1 |
| REQ-6.3-2 | If the queue is full or Redis is down, events are dropped and counted (`telemetry_dropped_total`). | P1 |
| REQ-6.3-3 | The dashboard shows a warning when telemetry is being dropped. | P4 |
| REQ-6.3-4 | The request path never waits on telemetry. | P1 |
| REQ-6.4-1 | Each service exposes `/metrics` (request counts, latency histograms, circuit state gauges, dropped telemetry). | P4 |
| REQ-6.4-2 | Prometheus and Grafana are an optional compose profile; the dashboard does not depend on them. | P4 |

## §7 Analytics and incidents

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-7.1-1 | Every 60 s the worker evaluates each API over the last 5 minutes against its baseline. | P5 |
| REQ-7.1-2 | The baseline is the median over the prior 24 h excluding incident windows; with less history, fixed absolute thresholds apply. | P5 |
| REQ-7.1-3 | A rule fires only with at least `min_samples` requests (default 50) in the window. | P5 |
| REQ-7.1-4 | A rule must fire in `sustain` consecutive evaluations (default 3) before an incident opens. | P5 |
| REQ-7.2-1 | `error_rate_spike`: upstream error rate ≥ max(3 × baseline, 5%). | P5 |
| REQ-7.2-2 | `latency_spike`: upstream p95 ≥ max(3 × baseline, baseline + 500 ms). | P5 |
| REQ-7.2-3 | `circuit_opened`: any CLOSED → OPEN transition, firing immediately without sustain. | P5 |
| REQ-7.2-4 | `quota_pressure`: upstream quota bucket empty for ≥ 20% of the window. | P5 |
| REQ-7.2-5 | `cache_effectiveness_drop`: hit ratio drops ≥ 50% relative to baseline with stable traffic. | P5 |
| REQ-7.2-6 | `telemetry_loss`: dropped telemetry > 1%. | P5 |
| REQ-7.3-1 | The first sustained rule for an API opens an incident with a timeline entry. | P5 |
| REQ-7.3-2 | Further rules and transitions are appended to the open incident's timeline, and severity is the max of active rules. | P5 |
| REQ-7.3-3 | An incident resolves after no rules fire for `resolve_after` (default 5 min). | P5 |
| REQ-7.3-4 | Only one incident per API can be open at a time. | P5 |
| REQ-7.3-5 | Opening an incident queues an AI explanation job and resolving queues a summary job, only if AI is enabled. | P6 |

## §8 Failure modes

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-8-1 | Redis down: the gateway fails open (in-memory limiter, no cache or dedup, local circuit state); telemetry is dropped and counted. | P2 |
| REQ-8-2 | Postgres down: the gateway is unaffected and serves from in-memory config. | P2 |
| REQ-8-3 | Postgres down: dashboard and API return 503, and the worker pauses consumption while the stream buffers. | P4 |
| REQ-8-4 | Worker down: the gateway is unaffected, and the worker catches up from the stream after restart. | P4 |
| REQ-8-5 | Management API down: the gateway is unaffected. | P4 |
| REQ-8-6 | LLM provider down: AI features show "unavailable" and jobs retry with backoff, then are marked failed. | P6 |
| REQ-8-7 | Upstream down: retry → circuit → fallback. | P2 |
| REQ-8-8 | A restarted gateway loads config from Postgres at startup while other replicas keep serving. | P2 |
| REQ-8-9 | The last loaded config is written to a local snapshot so a gateway can start while Postgres is down. | P2 |

## §9 AI layer

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-9.1-1 | With no provider key configured, AI surfaces show "Configure AI" and every other feature works. | P6 |
| REQ-9.1-2 | The request path never depends on the AI layer. | P6 |
| REQ-9.1-3 | No AI tool changes configuration; the only write tool creates a pending recommendation. | P6 |
| REQ-9.1-4 | Every answer lists the data it used, and the UI renders that evidence. | P6 |
| REQ-9.1-5 | Chat streams tokens as they are generated, with no artificial delays or batching windows. | P6 |
| REQ-9.2-1 | `LLMProvider` exposes `chat(messages, tools, stream)` yielding normalized events, plus `capabilities` (tools, max_context, json_mode). | P0 |
| REQ-9.2-2 | `AnthropicProvider` and `GeminiProvider` map the internal message and tool schema to their APIs. | P6 |
| REQ-9.2-3 | `ProviderRegistry` hot-swaps provider, model or key on settings change without restart. | P6 |
| REQ-9.2-4 | The AI API key is stored encrypted. | P6 |
| REQ-9.2-5 | `ai.monthly_token_cap` is off by default; when set, AI jobs stop at the cap until the next month. | P6 |
| REQ-9.2-6 | With `ai.send_route_templates` off, routes are replaced with opaque IDs. | P6 |
| REQ-9.2-7 | "Test connection" makes one minimal call and reports success or the provider's error. | P6 |
| REQ-9.3-1 | An incident explanation (markdown, evidence list, confidence of `high`/`medium`/`low`/`insufficient`) is generated when an incident opens. | P6 |
| REQ-9.3-2 | A postmortem-style summary is generated when an incident resolves. | P6 |
| REQ-9.3-3 | Explanations and summaries are generated once and cached; viewing an incident never triggers an LLM call, and "Regenerate" runs it again. | P6 |
| REQ-9.3-4 | Chat answers over SSE with tool calls shown. | P6 |
| REQ-9.4-1 | The worker builds the evidence bundle deterministically and includes it in the first prompt. | P6 |
| REQ-9.4-2 | The model has the tools of §9.4, and chat uses the same tools. | P6 |
| REQ-9.4-3 | Per interaction: at most 8 tool calls, each result truncated to about 8 KB, and a 90 s wall-clock timeout; at a limit the model answers with the data gathered. | P6 |
| REQ-9.5-1 | A recommendation is validated against the Pydantic schema and rejected if it does not match. | P6 |
| REQ-9.5-2 | `simulate_cache_policy` simulates TTL changes only; other changes show "not simulated". | P6 |
| REQ-9.5-3 | `Apply` creates a new config version linked to the recommendation, and can be rolled back. | P6 |
| REQ-9.5-4 | `Dismiss` requires a reason, which feeds future prompts. | P6 |
| REQ-9.5-5 | Only admins can apply recommendations. | P6 |
| REQ-9.6-1 | Provider credentials, JARVIS keys, raw headers, raw query strings, raw bodies, IPs and user identifiers are never sent to the provider. | P6 |
| REQ-9.6-2 | The redaction layer enforces an allowlist of fields, not a blocklist. | P6 |
| REQ-9.7-1 | Tool results are wrapped as data blocks, and the system prompt tells the model never to follow instructions inside them. | P6 |
| REQ-9.8-1 | The eval runner scores category match, required facts and no false certainty, and answers `insufficient` on ambiguous fixtures. | P6 |
| REQ-9.8-2 | The eval suite runs nightly against both providers and posts scores to the job summary. | P6 |

## §10 Management API

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-10-1 | The API is REST + JSON under `/api/v1`, with an OpenAPI spec generated automatically. | P4 |
| REQ-10-2 | It is authenticated by session cookie (dashboard) or admin token (automation). | P4 |
| REQ-10-3 | `POST /setup` is allowed only while no users exist. | P4 |
| REQ-10-4 | A full key is shown once at creation and never again. | P4 |
| REQ-10-5 | `POST /apis/{id}/test` sends a test request through the full gateway pipeline and reports each step. | P4 |
| REQ-10-6 | The endpoints of §10 exist: auth, setup, users, projects, keys, APIs, versions and rollback, cache invalidation, metrics, live SSE, incidents, AI, recommendations, settings and audit. | P5 |
| REQ-10-7 | `GET /stream/live` sends health changes, incident updates and summary counts every 2 s. | P4 |

## §11 Security

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-11.1-1 | Dashboard passwords use Argon2id. | P4 |
| REQ-11.1-2 | Sessions are server-side in Redis (with Postgres fallback), with `HttpOnly`, `Secure` and `SameSite=Lax` cookies. | P4 |
| REQ-11.1-3 | Mutating dashboard requests require a CSRF token. | P4 |
| REQ-11.1-4 | Login is rate limited. | P4 |
| REQ-11.1-5 | `admin` can do everything; `member` can read everything, use chat and create keys for their projects, but cannot change config. | P4 |
| REQ-11.1-6 | Gateway keys are stored hashed, scoped to a project, optionally restricted to APIs, and revocable in ≤ 1 s. | P1 |
| REQ-11.2-1 | The master key comes from `JARVIS_MASTER_KEY` (32 bytes, base64). | P1 |
| REQ-11.2-2 | Secrets are encrypted with AES-256-GCM with per-record nonces and a stored key ID. | P1 |
| REQ-11.2-3 | `jarvis rotate-master-key` re-encrypts all secrets. | P1 |
| REQ-11.2-4 | Secrets never appear in logs, telemetry, API responses or AI context. | P1 |
| REQ-11.3-1 | The audit log records config changes, key creation or revocation, recommendation apply or dismiss, settings changes and logins. | P4 |
| REQ-11.3-2 | Containers run as non-root, and only Caddy publishes host ports by default. | P0 |
| REQ-11.3-3 | Upstream TLS verification is on and cannot be disabled per API. | P1 |

## §12 Data model

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-12-1 | The Postgres tables of §12 exist with the listed columns. | P4 |
| REQ-12-2 | Migrations are managed with Alembic and applied by the `api` service at startup under an advisory lock so they run exactly once. | P1 |
| REQ-12-3 | `request_events` and `gateway_events` are hypertables, with `metrics_1m` and `metrics_1h` as continuous aggregates. | P4 |

## §13 Performance budgets

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-13-1 | Gateway overhead on a cache hit: p50 ≤ 2 ms, p99 ≤ 10 ms. | P3 |
| REQ-13-2 | Gateway overhead on an upstream call: p50 ≤ 3 ms, p99 ≤ 15 ms. | P3 |
| REQ-13-3 | Gateway throughput ≥ 2,000 req/s on 2 vCPU with a cache-hit-heavy workload. | P3 |
| REQ-13-4 | Hot-path I/O: 0 Postgres queries, ≤ 2 Redis round trips before upstream and ≤ 1 after. | P3 |
| REQ-13-5 | Config propagation ≤ 1 s p99. | P3 |
| REQ-13-6 | Dashboard first load (LAN) ≤ 1.5 s to interactive. | P4 |
| REQ-13-7 | Dashboard API p95 ≤ 200 ms (aggregates only). | P4 |
| REQ-13-8 | Live updates reach the screen ≤ 2 s from the event. | P4 |
| REQ-13-9 | Dashboard queries read only from rollups, except the paginated, indexed request log. | P4 |

## §14 User interface

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-14-1 | The UI provides Overview, APIs and API detail, Incidents and detail, Ask JARVIS, Recommendations and Settings pages. | P4 |
| REQ-14-2 | Dark and light themes exist, dark by default. | P4 |
| REQ-14-3 | A ⌘K command palette jumps to any API, incident or setting. | P4 |
| REQ-14-4 | Loading uses skeletons only, with no layout shift. | P4 |
| REQ-14-5 | Config saves apply optimistically and roll back if the save fails. | P4 |
| REQ-14-6 | When AI is disabled, AI surfaces show a single "Configure AI" card. | P6 |

## §15 Deployment

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-15.1-1 | `deploy/docker-compose.yml` defines caddy, gateway, api, worker, postgres and redis in the default profile, and prometheus and grafana in `monitoring`. | P4 |
| REQ-15.1-2 | Every service has a health check, and `depends_on` waits for healthy services. | P0 |
| REQ-15.1-3 | The default profile idles at ≤ 1.5 GB RAM. | P7 |
| REQ-15.1-4 | `docker-compose.dev.yml` builds from source with hot reload. | P0 |
| REQ-15.2-1 | `setup.sh` writes `.env` and generates `JARVIS_MASTER_KEY` and the DB passwords. | P7 |
| REQ-15.2-2 | Upgrade (`pull` + `up -d`) applies migrations automatically. | P7 |
| REQ-15.2-3 | Backup and restore scripts exist; Redis needs no backup. | P7 |
| REQ-15.2-4 | JARVIS makes no outbound calls except to upstream APIs and the configured LLM provider. | P7 |

## §16 Repository and CI/CD

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-16.1-1 | The repo is a monorepo with `uv` workspaces, ruff, mypy strict on `packages/core` and the gateway, and pytest with pytest-asyncio. | P0 |
| REQ-16.2-1 | `packages/core` defines `CacheStore`, `RateLimiter`, `CircuitStore`, `Deduplicator`, `TelemetrySink`, `ConfigSource`, `SecretBox` and `LLMProvider`. | P0 |
| REQ-16.2-2 | Each interface has an in-memory implementation that passes the same contract suite as the Redis or Postgres one. | P0 |
| REQ-16.3-1 | `ci.yml` runs lint, type-check, unit tests and integration tests on PRs and pushes to `main`. | P0 |
| REQ-16.3-2 | `main` is protected with required checks and conventional commits. | P0 |
| REQ-16.3-3 | Load, AI-eval and release workflows run as listed in §16.3. | P7 |

## §17 Testing strategy (requirements on the code under test)

| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-17-1 | Upstream redirects are not followed by default. | P1 |
| REQ-17-2 | SSRF bypass attempts (DNS rebinding, redirects to private IPs, IPv6 forms) are blocked. | P1 |
| REQ-17-3 | Secrets are absent from logs and AI context. | P1 |

## Sections without their own requirements

| Section | Reason |
|---------|--------|
| §0 Summary | Restates later sections. |
| §1.2 Non-goals | Covered by REQ-2-13 and REQ-2-15; the rest are things not built. |
| §6.4 | Covered by REQ-6.4-1, REQ-6.4-2. |
| §18 Delivery plan | Phase exit criteria; each is verified by the checkpoint acceptance stories. |
| §19 Future work | Explicitly deferred. |
| §20 Glossary | Definitions only. |
