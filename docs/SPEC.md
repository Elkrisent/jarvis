# JARVIS — Technical Specification

**Just Another Reliability & Visibility Infrastructure Service**
Version 0.1 · Status: Draft · September 2026

---

## 0. Summary

JARVIS is a **self-hosted reliability gateway for outbound REST API calls**. An application sends its third-party REST calls through JARVIS instead of directly to the provider. JARVIS applies authentication, rate limiting, caching, deduplication, timeouts, retries, circuit breaking and fallbacks, and records telemetry for every call.

An **optional, asynchronous AI layer** (Claude or Gemini, using the installer's own API key) reads that telemetry to explain incidents, answer questions, and propose configuration changes. A human applies those changes.

> **Governing principle:** Deterministic code handles execution, safety and performance. The AI layer interprets, correlates, explains and recommends. The request path never depends on the AI layer, and JARVIS is fully functional with no LLM key configured.

---

## 1. Goals and Non-Goals

### 1.1 Goals

| # | Goal | Measure |
|---|------|---------|
| G1 | Self-hostable as a first-class target | `git clone` → `cp .env.example .env` → `docker compose up -d` yields a working system with no external accounts |
| G2 | Easy integration | Most REST integrations need only a base-URL change and a key swap |
| G3 | Low overhead | See §13 performance budgets |
| G4 | Standard reliability | Timeouts, retries, circuit breaking, rate limits, cache, dedup, fallback |
| G5 | Useful visibility | Per-API and per-endpoint metrics, incidents, dashboard |
| G6 | Replaceable AI provider | Switch between Claude and Gemini at runtime from settings |
| G7 | Polished, responsive UI | See §13 UI budgets |

### 1.2 Non-Goals (v1)

- Protocols other than REST over HTTP/1.1 (no gRPC, GraphQL, WebSockets, or SSE/streaming responses).
- Inbound traffic, including inbound webhooks. JARVIS handles outbound calls only.
- Browser-to-JARVIS calls. JARVIS is server-to-server only.
- Multi-tenant SaaS hosting. One install serves one organization.
- Local or self-hosted LLMs.
- Autonomous configuration changes by the AI.
- Kubernetes, multi-region deployment, billing, SSO, and SDKs in many languages.

---

## 2. Scope Decisions (Locked)

| # | Decision | Choice |
|---|----------|--------|
| D1 | Tenancy | One install = one organization, containing multiple **projects**. Roles: `admin`, `member`. |
| D2 | Topology | The gateway runs on the same host or private network as the calling application. Not publicly exposed by default. |
| D3 | Redis unavailable | Fail open with degraded protection: in-memory per-instance rate limiter, cache and dedup skipped, circuit state held in process memory. One global setting. |
| D4 | Caching | Configured TTL per endpoint, plus an optional "honor upstream `Cache-Control`" toggle. Never cache non-2xx responses or responses with `Set-Cookie`. |
| D5 | Body logging | Metadata only by default. Body sampling is opt-in per API and always redacted. |
| D6 | Webhooks | Out of scope. |
| D7 | Config changes | Versioned in Postgres, pushed to gateways via Redis pub/sub, applied in ≤ 1 s. Rollback supported. |
| D8 | Dashboard auth | Local username and password, server-side sessions. |
| D9 | Path templating | User-defined route patterns, with automatic fallback that templates numeric and UUID segments. |
| D10 | Credential injection | Per-API mode: bearer header, custom header, or query parameter. The client's credential is always stripped. |
| D11 | Streaming responses | Rejected in v1 with a clear error when detected (`Accept: text/event-stream` or a streaming flag in the body, if configured). |
| D12 | LLM | Claude or Gemini only, bring-your-own key, optional. Optional spend cap set by the installer, off by default. |

---

## 3. System Overview

```text
1. Caddy (public edge, optional)
   TLS termination + reverse proxy
   /       → web (static)
   /api/*  → management API

2. Gateway  (data plane, N instances)
   hot path: auth → policy → limits → cache → upstream
   config + keys cached in memory
   emits telemetry to Redis Streams, never blocks on it

3. Management API  (control plane)
   projects, APIs, policies, users, settings
   dashboard queries (aggregates only)
   AI chat endpoint (SSE to browser)

4. Worker
   consumes telemetry stream → Postgres/Timescale
   rollups, anomaly rules, incident lifecycle
   AI jobs: explanations, summaries, recommendations

5. Redis
   cache, rate limits, dedup locks, circuit state
   telemetry stream, config pub/sub
   ephemeral — safe to lose

6. Postgres + TimescaleDB
   durable config, users, incidents, audit
   raw events (short retention) + rollups
   source of truth
```

**Traffic paths**

```text
1. Application → Gateway (private network, :8080)
   X-Jarvis-Key or key in the provider's credential slot
   /gw/{api_slug}/{upstream_path}

2. Gateway → Upstream provider (HTTPS)
   pooled connections, injected credentials
   SSRF-checked destination

3. Browser → Caddy → Web / Management API
   session cookie auth
   live updates via SSE
```

The gateway listens on the private network only. Caddy exposes the dashboard. Admins can expose the gateway through Caddy if their apps run on other hosts, and the docs cover this as an opt-in.

---

## 4. Gateway Specification

### 4.1 Endpoint

```
{METHOD} /gw/{api_slug}/{upstream_path}?{query}
```

Example: the application calls `http://jarvis-gateway:8080/gw/weather/v1/current?city=Bangalore`, and JARVIS forwards it to `https://api.weather.example/v1/current?city=Bangalore`.

### 4.2 JARVIS Authentication

JARVIS keys look like `jv_live_<random>`, are stored hashed with SHA-256, and are scoped to one project.

The gateway accepts the key in either of these places:

1. The `X-Jarvis-Key` header, which is preferred.
2. **The provider's credential slot.** Many SDKs only let you change the base URL and the API key. In that case the developer puts the JARVIS key where the provider key would normally go (for example `Authorization: Bearer jv_live_...`). The gateway detects the `jv_` prefix in the slot configured for that API, authenticates with it, and replaces it with the real provider credential.

Keys are cached in gateway memory (hash → project, allowed APIs). Revocation is pushed via pub/sub and takes effect in ≤ 1 s.

### 4.3 Request Pipeline (normative order)

```text
1. Ingress
   assign request_id (ULID), start deadline clock
   reject if body > max_body_bytes (default 10 MB)
   reject streaming requests (D11)

2. Authenticate + resolve policy
   JARVIS key → AuthContext(project, key_id)
   api_slug → ApiDependency + policy (in-memory)
   path → route template (D9)

3. Inbound rate limit
   token bucket per (project, key, api)
   protects JARVIS and fair-shares between keys
   429 with Retry-After on reject

4. Cache lookup  (cacheable methods only)
   key = hash(project, api, method, path, sorted query, vary headers)
   fresh hit → respond; stale entry kept for fallback

5. Deduplication  (cacheable methods only)
   in-process singleflight first, then Redis lock
   followers await leader's result (bounded by deadline)

6. Circuit breaker check
   OPEN → skip upstream, go to fallback
   HALF_OPEN → only one probe request allowed (Redis lock)

7. Upstream quota limit
   token bucket per api (provider's quota)
   counts only calls that actually leave the gateway

8. Upstream call  (with retries)
   SSRF-validated host, credential injected
   per-attempt timeout inside overall deadline
   retries per §4.7, honoring retry budget

9. Fallback  (if upstream failed or circuit open)
   serve_stale → fallback_api → controlled error

10. Finalize
    cache write (if eligible), release dedup lock
    add X-Jarvis-* response headers
    emit telemetry (fire-and-forget)
```

Redis steps 3–7 run through **server-side Lua scripts**, so the pre-upstream checks take at most two Redis round trips. The hot path makes **zero Postgres queries**.

### 4.4 Response Headers

| Header | Meaning |
|--------|---------|
| `X-Jarvis-Request-Id` | Request ID, for correlating with the dashboard |
| `X-Jarvis-Cache` | `HIT`, `MISS`, `STALE`, or `BYPASS` |
| `X-Jarvis-Attempts` | Number of upstream attempts |
| `X-Jarvis-Circuit` | Circuit state when the request was served |
| `X-Jarvis-Source` | `upstream`, `cache`, `dedup`, `fallback`, or `jarvis` (error generated by JARVIS) |

JARVIS-generated errors use a JSON body `{ "error": { "code": "...", "message": "...", "request_id": "..." } }` and one of these codes: `JV_AUTH_INVALID`, `JV_RATE_LIMITED`, `JV_QUOTA_EXCEEDED`, `JV_CIRCUIT_OPEN`, `JV_UPSTREAM_TIMEOUT`, `JV_UPSTREAM_UNREACHABLE`, `JV_BODY_TOO_LARGE`, `JV_STREAMING_UNSUPPORTED`, `JV_DESTINATION_BLOCKED`. Errors returned by the upstream are passed through unchanged.

### 4.5 Credential Injection

| Mode | Config | Behavior |
|------|--------|----------|
| `bearer` | — | Set `Authorization: Bearer <secret>` |
| `header` | `header_name` | Set `<header_name>: <secret>` |
| `query` | `param_name` | Set `?<param_name>=<secret>` |
| `none` | — | Nothing injected |

Before injection, the gateway **always removes** any client-supplied value in that slot. Secrets are stored encrypted (§11.2) and decrypted into gateway memory at config load.

### 4.6 Timeouts and Deadline

- `deadline_ms`: total time budget per request (default 5000).
- `attempt_timeout_ms`: time limit for each upstream attempt (default 2000). Covers connect and read.
- A new attempt starts only if the remaining deadline is at least `min_attempt_ms` (default 300).
- Dedup followers wait at most until their own deadline.

### 4.7 Retries

**Retryable conditions:** connection error, attempt timeout, `502`, `503`, `504`, and `429`. For `429` and `503`, the gateway honors `Retry-After` if it fits inside the remaining deadline. Otherwise it does not retry.

**Retryable methods:** `GET`, `HEAD`, `OPTIONS`, `PUT`, `DELETE`. `POST` and `PATCH` are retried only when the API policy sets `idempotency.header` (for example `Idempotency-Key`). In that case JARVIS generates the key once per request and sends the same key on every attempt.

**Backoff:** exponential with full jitter. `delay = random(0, min(cap, base × 2^attempt))`, where `base` defaults to 100 ms and `cap` to 2000 ms.

**Retry budget:** per API, retries may not exceed `retry_budget_ratio` (default 0.2) of requests over a rolling 60 s window, tracked in Redis. When the budget is exhausted, requests fail without retrying.

### 4.8 Circuit Breaker

State lives in Redis and is shared across gateway instances. Each instance keeps a local copy refreshed every 1 s and on pub/sub state-change events.

- **Counted as failures:** 5xx, timeouts, connection errors. 4xx responses (including 429) are **not** counted.
- **CLOSED → OPEN:** the failure rate is at least `failure_rate_threshold` (default 50%) over a rolling window of `window_s` (default 30), with at least `min_calls` (default 20) calls in that window.
- **OPEN → HALF_OPEN:** after `open_duration_s` (default 30).
- **HALF_OPEN:** one probe is allowed through a Redis lock. On success → CLOSED. On failure → OPEN, with the open duration doubled up to `max_open_duration_s` (default 300).
- Every state transition emits a telemetry event.

### 4.9 Cache

- **Eligible:** `GET` or `HEAD`, a `2xx` response, no `Set-Cookie`, body ≤ `max_object_bytes` (default 1 MB), and the route has caching enabled.
- **Key:** `cache:{project}:{api}:{sha256(method|route_path|sorted_query|vary_headers)}`. `vary_headers` is configured per API and defaults to `Accept` and `Accept-Language`.
- **TTL:** set per route, with a per-API default. With `honor_cache_control` on, `max-age` and `no-store` from the upstream override the configured TTL.
- **Stale window:** entries are kept for `stale_ttl_s` beyond expiry, to be served as a fallback only (`X-Jarvis-Cache: STALE`).
- **Manual invalidation:** by API, by route, or everything, from the dashboard or the management API.

### 4.10 Deduplication

- Applies to cacheable requests only.
- **In-process:** an `asyncio.Future` keyed by the cache key. Concurrent identical requests in the same worker await the same future.
- **Cross-instance:** `SET inflight:{key} NX PX {deadline}`. The leader calls the upstream and writes to the cache. Followers subscribe to `done:{key}` on Redis pub/sub, then read the result from the cache. If the leader dies, the lock expires at the deadline and followers call the upstream themselves.
- Metrics count each `dedup_follower` as an avoided upstream call.

### 4.11 Rate Limiting

Both limiters use a token bucket implemented as a Redis Lua script (atomic refill and take).

| Limiter | Scope | Purpose | Default |
|---------|-------|---------|---------|
| Inbound | (project, key, api) | Fair use and self-protection | Off (unlimited) |
| Upstream quota | (api) | Stay within the provider's quota | Off, set by user from the provider's documented limits |

If Redis is down, each gateway instance falls back to an in-memory bucket set to `limit ÷ expected_instances` (configurable). Protection is looser in this mode, but requests keep flowing (D3).

### 4.12 Fallback

Fallback is configured per API and tried in this order:

1. `serve_stale: true` → return the stale cache entry, if one exists.
2. `fallback_api: <slug>` → repeat the pipeline from step 6 against another configured API with the same path. The fallback API may itself have no further fallback, so there are no chains.
3. Controlled error with the appropriate `JV_*` code.

### 4.13 SSRF Protection

- Upstream hosts are resolved at config save and **re-validated at connect time**. The connection is pinned to the validated IP, which prevents DNS-rebinding attacks.
- **Blocked by default:** loopback, RFC 1918, link-local (including `169.254.169.254`), CGNAT, IPv6 ULA and link-local, multicast, and the JARVIS containers themselves.
- An admin-managed allowlist (CIDRs or hosts) permits internal upstreams.
- Only `https` upstream URLs are allowed by default. Plain `http` requires the host to be on the allowlist.

### 4.14 Path Templating

Metrics are grouped by **route template**, not raw path.

1. Explicit patterns configured per API, e.g. `/v1/users/{id}/orders`, matched first.
2. Automatic fallback: replace segments that are purely numeric, UUIDs, or longer than 24 characters of hex or base64-like text with `{id}`.
3. Templates per API are capped at 500 by default, and the overflow goes into an `{other}` bucket.

---

## 5. Configuration Model

### 5.1 ApiDependency (canonical JSON)

```json
{
  "slug": "weather",
  "name": "Weather API",
  "base_url": "https://api.weather.example",
  "credential": { "mode": "query", "param_name": "appid", "secret_ref": "sec_01H..." },
  "jarvis_key_slot": "x-jarvis-key",
  "deadline_ms": 5000,
  "attempt_timeout_ms": 2000,
  "retry": {
    "enabled": true, "max_attempts": 3, "base_ms": 100, "cap_ms": 2000,
    "budget_ratio": 0.2, "idempotency_header": null
  },
  "circuit": {
    "failure_rate_threshold": 0.5, "window_s": 30, "min_calls": 20,
    "open_duration_s": 30, "max_open_duration_s": 300
  },
  "cache": {
    "enabled": true, "default_ttl_s": 60, "stale_ttl_s": 600,
    "honor_cache_control": false, "vary_headers": ["accept", "accept-language"],
    "max_object_bytes": 1048576
  },
  "rate_limits": {
    "inbound":  { "enabled": false, "capacity": 100, "refill_per_s": 10 },
    "upstream": { "enabled": true,  "capacity": 60,  "refill_per_s": 1 }
  },
  "fallback": { "serve_stale": true, "fallback_api": null },
  "routes": [
    { "pattern": "/v1/current", "cache_ttl_s": 120 },
    { "pattern": "/v1/forecast/{city}", "cache_ttl_s": 900 }
  ],
  "body_sampling": { "enabled": false, "rate": 0.0 }
}
```

The dashboard edits this through forms. Users never write JSON by hand, though an import/export of this JSON is available.

### 5.2 Versioning and Propagation

- Every save creates an immutable `ApiPolicyVersion` row with an author, a diff, and a reason. The reason is required when a change comes from an AI recommendation.
- Once the save commits, the API publishes `config:changed {api_id, version}`. Gateways reload the policy and swap it atomically in memory.
- Gateways also do a full resync every 60 s and on startup, so a missed pub/sub message is corrected within a minute.
- **Rollback** means creating a new version whose content is copied from an old one. History is never rewritten.

---

## 6. Telemetry

### 6.1 Event Schema

Events are emitted by the gateway to the Redis Stream `telemetry` with `MAXLEN ~ 1,000,000`. The stream is approximately trimmed, and dropping old events is acceptable.

```json
{
  "v": 1,
  "request_id": "01J8...",
  "ts": "2026-09-24T10:42:01.123Z",
  "project_id": "prj_...",
  "key_id": "key_...",
  "api_id": "api_...",
  "method": "GET",
  "route": "/v1/forecast/{city}",
  "status": 200,
  "source": "cache",
  "cache": "HIT",
  "cache_key_hash": "9f2c...",
  "attempts": 0,
  "latency_ms": 3,
  "upstream_latency_ms": null,
  "req_bytes": 0,
  "resp_bytes": 5120,
  "circuit": "CLOSED",
  "error_code": null,
  "dedup_role": null
}
```

Circuit transitions, retry-budget exhaustion and fallback use are emitted as separate `kind: "event"` records.

`cache_key_hash` is a one-way hash. It allows cache-policy simulation (§9.5) without storing any parameters.

### 6.2 Storage and Retention

| Table | Granularity | Retention |
|-------|-------------|-----------|
| `request_events` (hypertable) | Raw | 7 days |
| `metrics_1m` (continuous aggregate) | Per api × route × minute | 30 days |
| `metrics_1h` (continuous aggregate) | Per api × route × hour | 365 days |
| `gateway_events` | Raw | 90 days |

Retention periods are configurable via environment variables. Rollups store counts, error counts, cache hits, dedup followers, attempts, bytes, and latency percentiles as TimescaleDB percentile sketches.

### 6.3 Backpressure

- Emitting telemetry is non-blocking: events go into a bounded in-memory queue (default 10,000) and are flushed in batches with `XADD` pipelining.
- If the queue is full or Redis is unavailable, events are **dropped and counted** (`telemetry_dropped_total`), and the dashboard shows a warning banner. The request path never waits on telemetry.

### 6.4 Prometheus

Each service exposes `/metrics` (request counts, latency histograms, circuit state gauges, dropped telemetry). Prometheus and Grafana ship as an **optional** compose profile. The JARVIS dashboard does not depend on them.

---

## 7. Analytics and Incidents

### 7.1 Evaluation Loop

Every 60 s, the worker evaluates each API over the last 5 minutes against its baseline.

- **Baseline:** the median of the same metric over the prior 24 h, excluding incident windows. If there is less than 24 h of history, fixed absolute thresholds apply.
- **Minimum evidence:** a rule fires only if the window has at least `min_samples` requests (default 50).
- **Sustain:** a rule must fire in `sustain` consecutive evaluations (default 3) before an incident opens.

### 7.2 Rules (v1)

| Rule | Condition (defaults) |
|------|----------------------|
| `error_rate_spike` | Upstream error rate ≥ max(3 × baseline, 5%) |
| `latency_spike` | Upstream p95 ≥ max(3 × baseline, baseline + 500 ms) |
| `circuit_opened` | Any CLOSED → OPEN transition (fires immediately, no sustain) |
| `quota_pressure` | Upstream quota bucket empty for ≥ 20% of the window |
| `cache_effectiveness_drop` | Cache hit ratio drops ≥ 50% relative to baseline, with traffic stable |
| `telemetry_loss` | Dropped telemetry > 1% |

### 7.3 Incident Lifecycle

```text
1. Open
   first sustained rule for an API
   incident row + timeline entry
   AI explanation job queued (if enabled)

2. Update
   further rules/transitions appended to timeline
   severity = max of active rules

3. Resolve
   no rules firing for resolve_after (default 5 min)
   AI summary job queued (if enabled)
```

Only one incident per API can be open at a time. New rules on an API with an open incident are appended to that incident's timeline.

---

## 8. Failure Modes

| Component down | Gateway behavior | Everything else |
|----------------|------------------|-----------------|
| Redis | Fail open (D3): in-memory limiter, no cache or dedup, local circuit state | Telemetry dropped and counted; config pub/sub replaced by the 60 s resync (which needs Postgres) |
| Postgres | **Unaffected**: serves from in-memory config | Dashboard and API return 503; worker pauses consumption, and the stream buffers up to MAXLEN |
| Worker | Unaffected | Metrics and incidents lag; the worker catches up from the stream after restart |
| Management API | Unaffected | Dashboard unavailable |
| LLM provider | Unaffected | AI features show "unavailable"; jobs retry with backoff, then are marked failed |
| Upstream API | Retry → circuit → fallback | Incident detection runs as normal |
| Gateway restart | Other replicas keep serving; the restarted instance loads config from Postgres at startup | — |

Gateway startup requires Postgres once, to load config. After that it can run without Postgres indefinitely. The last loaded config is also written to a local snapshot file, so a gateway can start while Postgres is down.

---

## 9. AI Layer

### 9.1 Principles

1. **Optional.** With no provider key configured, every AI surface shows a "Configure AI" prompt and all other features work.
2. **Read-mostly.** Tools read data. The only write tool creates a *pending* recommendation. No tool changes configuration.
3. **Evidence first.** Every answer lists the data it used, and the UI renders that evidence.
4. **No secrets, no raw payloads.** Only redacted metadata leaves the install (§9.6).
5. **Not deliberately slow.** No artificial delays or batching windows. Chat streams tokens as they are generated.

### 9.2 Provider Abstraction

```text
1. LLMProvider (interface)
   chat(messages, tools, stream) → events
   capabilities: tools, max_context, json_mode
   normalized internal message + tool schema

2. AnthropicProvider
   Messages API, tool_use / tool_result blocks
   maps internal schema ↔ Anthropic format

3. GeminiProvider
   generateContent, functionCall / functionResponse
   maps internal schema ↔ Gemini format

4. ProviderRegistry
   reads active provider/model/key from settings
   hot-swaps on settings change, no restart
```

Settings are stored in Postgres, and the key is encrypted:

| Setting | Values |
|---------|--------|
| `ai.provider` | `none` \| `anthropic` \| `gemini` |
| `ai.model` | Free text, with suggested defaults per provider |
| `ai.api_key` | Encrypted |
| `ai.features` | Toggles: `explain`, `summarize`, `chat`, `recommend` |
| `ai.monthly_token_cap` | Optional, **off by default**. When set, AI jobs stop at the cap until the next month |
| `ai.send_route_templates` | On by default. If off, routes are replaced with opaque IDs |

A "Test connection" button makes one minimal call and reports success or the provider's error.

### 9.3 Features

| Feature | Trigger | Output | Mode |
|---------|---------|--------|------|
| Incident explanation | Incident opened | Markdown explanation + evidence list + confidence (`high` / `medium` / `low` / `insufficient`) | Background job; result stored on the incident |
| Incident summary | Incident resolved | Postmortem-style summary (timeline, impact, likely cause, follow-ups) | Background job |
| Ask JARVIS (chat) | User message | Streamed answer with tool calls shown | Interactive, SSE |
| Recommendations | Daily job + on demand + from chat | Structured recommendation (§9.5) | Background / interactive |

Explanations and summaries are generated **once** and cached. Viewing an incident never triggers a new LLM call. Users can press "Regenerate" to run it again.

### 9.4 Evidence Bundle and Tools

For incident jobs, the worker builds an **evidence bundle** deterministically and puts it in the first prompt, so the model reasons over data from the first turn:

- Metrics for the affected API, before and during the incident (rate, errors by status class, p50/p95/p99, cache, retries, attempts)
- Metrics for the gateway itself (its own latency, telemetry drops) and for other APIs over the same window, used as controls
- Circuit transitions and timeline
- Current config and recent config changes (the diff, and who made it)
- Similar past incidents (same API and rule)

The model may call further **tools** to get more detail. Chat uses the same tools:

| Tool | Returns |
|------|---------|
| `list_apis()` | APIs with current health |
| `get_metrics(api, route?, metric[], from, to, step)` | Time series from rollups |
| `get_top_routes(api, by, from, to, limit)` | Routes ranked by volume, errors, or latency |
| `get_status_breakdown(api, from, to)` | Counts by status code |
| `get_circuit_history(api, from, to)` | Transitions |
| `get_config(api)` / `get_config_history(api)` | Policy (secrets removed) and version diffs |
| `list_incidents(filter)` / `get_incident(id)` | Incidents with timelines |
| `simulate_cache_policy(api, route, ttl_s, window)` | Estimated hit ratio and upstream calls saved (§9.5) |
| `create_recommendation(...)` | Pending recommendation ID |

Limits per interaction: at most 8 tool calls, each tool result truncated to about 8 KB, and a 90 s wall-clock timeout. When the model hits a limit, it answers with the data gathered so far.

### 9.5 Recommendations

A recommendation is structured, validated with Pydantic, and rejected if it doesn't match the schema:

```json
{
  "api": "weather",
  "route": "/v1/forecast/{city}",
  "change": { "path": "cache.routes[/v1/forecast/{city}].cache_ttl_s", "from": 60, "to": 300 },
  "rationale": "Repeated identical requests within 5 minutes; low hit ratio at current TTL.",
  "evidence": [{ "tool": "simulate_cache_policy", "summary": "hit ratio 18% → 71%, −12.4k upstream calls/day" }],
  "risk": "Forecast data up to 5 minutes stale.",
  "confidence": "medium"
}
```

- **Simulation:** `simulate_cache_policy` replays `request_events` (timestamp and `cache_key_hash`) through an LRU-free TTL model. Only TTL changes are simulated in v1. Other change types show "not simulated".
- **UI actions:** `Apply` creates a new config version with the recommendation linked as the reason, and the change can be rolled back. `Dismiss` requires a reason, which feeds into future prompts to avoid repeats.
- Only admins can apply recommendations.

### 9.6 Data Sent to the Provider

**Allowed:** API slug and name, route templates (if enabled), aggregate metrics, status codes, circuit states, config values with secrets removed, incident timelines, and redacted body samples if the installer opted in.

**Never sent:** provider credentials, JARVIS keys, raw headers, raw query strings, raw bodies, IPs, user identifiers.

A redaction layer sits between the tools and the provider. It enforces this list with an allowlist of fields, rather than a blocklist, and is unit-tested. The settings page shows this list verbatim.

### 9.7 Prompt-Injection Posture

- Tool results are wrapped as data blocks. The system prompt instructs the model never to follow instructions found inside them.
- Because no tool can change configuration or read secrets, the worst outcome of a successful injection is a misleading answer or recommendation. Every recommendation still requires a human to apply it.

### 9.8 Evaluation

- `tests/ai_eval/` holds recorded incident fixtures: an evidence bundle, the expected diagnosis category (`upstream_degradation`, `quota_exhaustion`, `config_regression`, `client_traffic_surge`, `insufficient_evidence`), and must-mention facts.
- The eval runner scores category match, required facts, and **no false certainty** (it must answer `insufficient` on fixtures designed to be ambiguous).
- It runs nightly in CI against both providers using repository secrets, and posts the scores as a job summary.

---

## 10. Management API (summary)

REST + JSON under `/api/v1`, authenticated by session cookie (dashboard) or admin token (automation). An OpenAPI spec is generated automatically.

| Area | Endpoints |
|------|-----------|
| Auth | `POST /auth/login`, `POST /auth/logout`, `GET /auth/me` |
| Setup | `GET /setup/status`, `POST /setup` (first admin; allowed only while no users exist) |
| Users | `GET/POST /users`, `PATCH/DELETE /users/{id}` |
| Projects | `GET/POST /projects`, `GET/PATCH/DELETE /projects/{id}` |
| Keys | `GET/POST /projects/{id}/keys`, `DELETE /keys/{id}` (the full key is shown once at creation) |
| APIs | `GET/POST /projects/{id}/apis`, `GET/PATCH/DELETE /apis/{id}`, `POST /apis/{id}/test` |
| Config versions | `GET /apis/{id}/versions`, `POST /apis/{id}/versions/{v}/rollback` |
| Cache | `POST /apis/{id}/cache/invalidate` |
| Metrics | `GET /metrics/overview`, `GET /apis/{id}/metrics`, `GET /apis/{id}/routes` |
| Live | `GET /stream/live` (SSE: health changes, incident updates, summary counts every 2 s) |
| Incidents | `GET /incidents`, `GET /incidents/{id}`, `POST /incidents/{id}/regenerate` |
| AI | `POST /ai/chat` (SSE), `GET/POST /ai/settings`, `POST /ai/test` |
| Recommendations | `GET /recommendations`, `POST /recommendations/{id}/apply`, `POST /recommendations/{id}/dismiss` |
| Settings | `GET/PATCH /settings` (SSRF allowlist, retention, fail-open mode) |
| Audit | `GET /audit` |

`POST /apis/{id}/test` sends a test request through the full gateway pipeline and shows each step's result, which is useful for onboarding.

---

## 11. Security

### 11.1 Authentication and Authorization

- **Dashboard:** Argon2id password hashing, server-side sessions in Redis (with Postgres fallback), `HttpOnly` + `Secure` + `SameSite=Lax` cookies, CSRF token on mutating requests, and login rate limiting.
- **Roles:** `admin` (everything) and `member` (read everything, use chat, create keys for their projects, no config changes).
- **Gateway keys:** stored as hashes, scoped to a project, optionally restricted to specific APIs, revocable in ≤ 1 s.

### 11.2 Secrets

- A master key is supplied via `JARVIS_MASTER_KEY` (32 bytes, base64). The setup script generates it if it is missing.
- Provider credentials and the AI key are encrypted with AES-256-GCM using per-record nonces, with a key ID stored alongside each record for rotation.
- `jarvis rotate-master-key` re-encrypts all secrets.
- Secrets never appear in logs, telemetry, API responses, or AI context.

### 11.3 Other

- SSRF protection as described in §4.13.
- The audit log records every config change, key creation or revocation, recommendation apply or dismiss, settings change, and login.
- Containers run as non-root with read-only filesystems where possible. Only Caddy publishes host ports by default, and the gateway port is bound to the internal network.
- Upstream TLS verification is on and can't be disabled per API in v1.

---

## 12. Data Model (Postgres)

```text
users(id, email, password_hash, role, created_at, last_login_at)
projects(id, name, slug, created_at)
jarvis_keys(id, project_id, name, prefix, hash, allowed_api_ids[], created_by, revoked_at)
api_dependencies(id, project_id, slug, name, current_version_id, created_at)
api_policy_versions(id, api_id, version, config_json, diff_json, author_id,
                    reason, recommendation_id, created_at)
secrets(id, key_id, ciphertext, nonce, created_at)
incidents(id, api_id, status, severity, opened_at, resolved_at, rules[],
          timeline_json, ai_explanation_md, ai_confidence, ai_summary_md)
recommendations(id, api_id, payload_json, status, created_by('ai'|'user'),
                decided_by, decided_at, dismiss_reason)
settings(key, value_json, updated_by, updated_at)
audit_log(id, actor_id, action, target_type, target_id, detail_json, at)
ai_usage(id, feature, provider, model, input_tokens, output_tokens, at)

-- TimescaleDB
request_events (hypertable, §6.1)
gateway_events (hypertable)
metrics_1m, metrics_1h (continuous aggregates)
```

Migrations are managed with Alembic and applied automatically by the `api` service at startup, with an advisory lock so they run exactly once.

---

## 13. Performance Budgets

These are targets. Each is measured by a test in CI (`tests/load/`, k6) against a mock upstream on the same host.

| Area | Budget |
|------|--------|
| Gateway overhead, cache hit | p50 ≤ 2 ms, p99 ≤ 10 ms |
| Gateway overhead, upstream call (excluding upstream time) | p50 ≤ 3 ms, p99 ≤ 15 ms |
| Gateway throughput | ≥ 2,000 req/s on 2 vCPU with a cache-hit-heavy workload |
| Hot-path I/O | 0 Postgres queries; ≤ 2 Redis round trips before upstream, ≤ 1 after |
| Config propagation | ≤ 1 s p99 |
| Dashboard first load (LAN) | ≤ 1.5 s to interactive |
| Dashboard API | p95 ≤ 200 ms (aggregates only) |
| Live updates | ≤ 2 s from event to screen |
| Chat | First streamed token as fast as the provider allows; tool steps shown as they run |

Implementation rules that follow from these budgets:
- **Gateway runtime:** uvicorn + uvloop, one pooled `httpx.AsyncClient` per upstream host, orjson.
- **Hot-path state:** config and keys held in memory; no work on the hot path beyond what the budgets allow.
- **Dashboard queries:** read only from rollups, never `request_events`, except the request log view, which is paginated and indexed.

---

## 14. User Interface

**Stack:** React + TypeScript + Vite, Tailwind CSS, shadcn/ui, TanStack Query (server state), TanStack Table + virtualization (logs), uPlot (time series).

**Pages**

```text
1. Overview
   org-wide requests, upstream calls avoided, errors
   API health grid with sparklines
   open incidents strip

2. APIs → API detail
   status, p95, error rate, cache hit, circuit
   tabs: Metrics · Routes · Config · Versions · Incidents
   "Test request" pipeline trace

3. Incidents → Incident detail
   timeline, impacted metrics chart
   JARVIS explanation + evidence chips + confidence
   summary after resolution

4. Ask JARVIS
   streamed chat, collapsible tool-call cards
   evidence links jump to the charts they cite
   suggested questions per current page

5. Recommendations
   pending / applied / dismissed
   diff view + simulation result
   Apply · Dismiss with reason

6. Settings
   projects & keys, users, AI provider,
   SSRF allowlist, retention, audit log
```

**Design rules**
- **Look and feel:** dark and light themes, dark by default. Monospace for IDs and numbers.
- **Navigation:** a ⌘K command palette to jump to any API, incident or setting.
- **Loading states:** skeleton loaders only. No layout shift, and no full-page spinners after the first load.
- **Optimistic updates:** config saves apply optimistically in the UI, with rollback if the save fails.
- **AI availability:** when AI is disabled, AI surfaces show a single "Configure AI" card and take up no other space.

---

## 15. Deployment (Self-Hosted First)

### 15.1 Compose

`deploy/docker-compose.yml`:

| Service | Image | Profile | Notes |
|---------|-------|---------|-------|
| `caddy` | `ghcr.io/<org>/jarvis-web` (Caddy + built web assets) | default | Ports 80/443; `JARVIS_DOMAIN` enables automatic HTTPS |
| `gateway` | `ghcr.io/<org>/jarvis-gateway` | default | Internal port 8080; `GATEWAY_REPLICAS`; optional host bind via `.env` |
| `api` | `ghcr.io/<org>/jarvis-api` | default | Runs migrations at startup |
| `worker` | `ghcr.io/<org>/jarvis-worker` | default | Telemetry, analytics, AI jobs |
| `postgres` | `timescale/timescaledb` (pinned) | default | Named volume |
| `redis` | `redis:7` (pinned) | default | AOF off; ephemeral by design |
| `prometheus`, `grafana` | upstream images | `monitoring` | Optional, with a provisioned dashboard |

Every service has a health check. `depends_on` waits for services to be healthy. Target footprint for the default profile at idle: **≤ 1.5 GB RAM**, running comfortably on 2 vCPU / 4 GB.

A `docker-compose.dev.yml` override builds from source with hot reload.

### 15.2 Install and Operate

```bash
git clone https://github.com/<org>/jarvis && cd jarvis/deploy
./setup.sh              # writes .env, generates JARVIS_MASTER_KEY + DB passwords
docker compose up -d    # open https://<domain> → create first admin
```

The docs include:
- **Upgrade:** `docker compose pull && docker compose up -d`. Migrations run automatically, and release notes flag any breaking changes.
- **Backup:** `scripts/backup.sh` (pg_dump + `.env`). Redis needs no backup.
- **Restore:** `scripts/restore.sh`.
- **Exposing the gateway to other hosts:** optional Caddy route with an IP allowlist.

JARVIS makes no outbound calls of its own except to upstream APIs and, if configured, the chosen LLM provider. There is no telemetry phone-home.

---

## 16. Repository and CI/CD

### 16.1 Layout (monorepo)

```text
jarvis/
├── packages/core/        # shared: config models, schemas, interfaces, redaction
├── services/gateway/     # data plane
├── services/api/         # control plane + migrations (alembic/)
├── services/worker/      # telemetry, analytics, AI
├── web/                  # React app
├── deploy/               # compose files, Caddyfile, setup/backup scripts, .env.example
├── tests/
│   ├── e2e/              # full stack via compose + mock upstream
│   ├── load/             # k6 scripts + budgets
│   └── ai_eval/          # fixtures + runner
├── docs/                 # install, config reference, architecture, ADRs
└── .github/workflows/
```

Python code is managed with `uv` workspaces. Tooling: `ruff` (lint and format), `mypy --strict` on `packages/core` and the gateway, and `pytest` with `pytest-asyncio`.

### 16.2 Interfaces (in `packages/core`)

`CacheStore`, `RateLimiter`, `CircuitStore`, `Deduplicator`, `TelemetrySink`, `ConfigSource`, `SecretBox`, `LLMProvider`. Each has a Redis or Postgres implementation plus an in-memory implementation used both for tests and as the fail-open fallback.

### 16.3 Workflows

| Workflow | Trigger | Steps |
|----------|---------|-------|
| `ci.yml` | PR, push to main | Lint, type-check, unit tests, integration tests (Postgres and Redis service containers), web lint, typecheck and build |
| `e2e.yml` | PR (label) + main | Compose up, e2e suite against a mock upstream with fault injection |
| `load.yml` | Nightly + manual | k6 against §13 budgets; fails if any budget is exceeded |
| `ai-eval.yml` | Nightly + manual | Eval suite against both providers (secrets) |
| `release.yml` | Tag `v*.*.*` | Multi-arch (amd64, arm64) buildx → GHCR, GitHub Release with changelog, compose file pinned to the tag |

Repository rules: protected `main`, required checks, conventional commits, Dependabot for pip, npm, Docker and Actions, and ADRs in `docs/adr/` for each locked decision in §2.

---

## 17. Testing Strategy

| Level | Coverage |
|-------|----------|
| Unit | Token bucket math, backoff and jitter, deadline accounting, circuit state machine, cache key and eligibility, path templating, redaction allowlist, config diffing, recommendation schema |
| Integration | Lua scripts against real Redis; singleflight + Redis dedup across 2 gateway processes; pub/sub config propagation; Timescale rollups |
| Fault injection | Mock upstream controllable per test: latency, 5xx rates, timeouts, 429 + Retry-After, connection resets, oversized bodies; Redis killed mid-test; Postgres killed mid-test |
| E2E | Onboarding flow: create project → add API → test request → call via gateway → see metrics → incident opens under injected failure → resolves |
| Load | §13 budgets; upstream-calls-avoided ratio under a duplicate-heavy workload |
| Security | SSRF bypass attempts (DNS rebinding, redirects to private IPs, IPv6 forms), key-slot stripping, secrets absent from logs and AI context |
| AI | §9.8 |

Upstream redirects are not followed by default. When following is enabled, every hop is SSRF-checked.

---

## 18. Delivery Plan

Each phase ends with a tagged pre-release and a passing CI run.

| Phase | Deliverable | Exit criteria |
|-------|-------------|---------------|
| P0 Foundations | Monorepo, compose skeleton, CI, core interfaces | `docker compose up` runs healthy empty services; CI green |
| P1 Proxy | Gateway routing, config (DB + in-memory), JARVIS keys, credential injection, SSRF guard, request IDs, telemetry to stream | App calls a mock upstream through JARVIS with a base-URL change only |
| P2 Reliability | Deadlines, retries + budget, circuit breaker, both rate limiters, fallback | Fault-injection suite passes |
| P3 Performance | Cache, stale serving, singleflight + Redis dedup, Lua consolidation | §13 gateway budgets met in `load.yml` |
| P4 Visibility | Worker ingestion, Timescale rollups, management API, dashboard (Overview, APIs, Settings), live SSE | Metrics visible within 2 s; dashboard budgets met |
| P5 Incidents | Rules engine, incident lifecycle, incident UI, config versioning + rollback | Injected upstream failure produces exactly one incident that opens and resolves correctly |
| P6 AI | Provider abstraction (Claude + Gemini), explanations, summaries, chat + tools, recommendations + simulation, eval suite | Eval suite passes thresholds on both providers; AI-off mode fully functional |
| P7 Self-host polish | Setup wizard, setup/backup/restore scripts, multi-arch releases, docs, Caddy HTTPS | A fresh VM reaches a working install from the README in ≤ 10 minutes |

---

## 19. Future Work (explicitly deferred)

Streaming (SSE) passthrough · per-route circuit breakers · OIDC/SSO · multi-org tenancy · Python/JS SDKs (thin wrappers) · simulation of non-TTL recommendations · alerting integrations (email, Slack, webhooks out) · Go rewrite of the gateway behind the same pipeline contract, if Python budgets can't be met.

---

## 20. Glossary

**Upstream:** the third-party REST API being called. **Route template:** a normalized path used as a metrics key. **Deadline:** the total time budget for one request across all attempts. **Retry budget:** the maximum ratio of retries to requests per API. **Leader/follower:** in deduplication, the one request that calls the upstream and the concurrent identical requests that wait for its result. **Evidence bundle:** deterministic data assembled for an AI incident analysis.
