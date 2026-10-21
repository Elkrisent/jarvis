# Mock upstream

A controllable fake REST API used by tests and benchmarks. Every request outside `/__admin/`
is answered according to the rules you set, and recorded so tests can assert what JARVIS sent.

Run it (listens on `:8080` in the container, `:19000` in the test compose override):

```bash
uv run uvicorn --factory mock_upstream.app:create_app_from_env --port 9000
MOCK_SEED=42 uv run uvicorn --factory mock_upstream.app:create_app_from_env --port 9000   # deterministic
```

With no matching rule, any method and path returns `200` and a JSON echo:
`{"method", "path", "query": [[k, v], ...], "headers": {...}, "body_size"}`.

## Admin API

| Endpoint | Purpose |
|----------|---------|
| `POST /__admin/rules` | Replace the rule set with one rule or a list. The first matching rule wins. |
| `GET /__admin/rules` | The current rules. |
| `GET /__admin/calls[?path_prefix=/x]` | `{"count", "calls": [{index, method, path, query, headers, body_size, rule, received_at}]}`, in arrival order. Recorded even for hung or dropped calls. |
| `POST /__admin/reset[?seed=N]` | Clear rules and calls, and restart the random sequence from the seed. |
| `GET /__admin/health` | Liveness (used by the compose health check). |

## Rule fields

All optional. Unknown fields are rejected.

| Field | Effect |
|-------|--------|
| `path_prefix` (default `/`), `methods` | Which requests the rule applies to. |
| `latency_ms` / `latency_range_ms: [min, max]` | Fixed or random delay before answering. |
| `status` | Response status (default 200). |
| `errors: [{status, rate}]` | Answer with `status` for a fraction `rate` of requests; rates sum to at most 1. |
| `retry_after_s` | Add `Retry-After` (use with `status: 429` or `503`). |
| `hang` | Never answer; the connection stays open until the client gives up. |
| `reset` | Send the headers and one byte of an announced body, then drop the connection. The client sees an incomplete response. (A TCP RST before any bytes cannot be produced from inside an ASGI app.) |
| `body_bytes` | Respond with that many bytes of filler, streamed (oversized bodies). |
| `set_cookie`, `cache_control`, `headers` | Add those response headers. |
| `redirect_to` | Respond with `Location`; the status is 302 unless a 3xx `status` is given. |

Example, a flaky API that is slow and sometimes rate-limits:

```bash
curl -X POST localhost:9000/__admin/rules -H 'content-type: application/json' -d '{
  "path_prefix": "/v1", "latency_range_ms": [40, 60],
  "errors": [{"status": 503, "rate": 0.3}], "retry_after_s": 1}'
```

## Determinism

Random latency and error draws use one seeded generator. Start with `MOCK_SEED=<n>` (or
`POST /__admin/reset?seed=<n>`); every `reset` restarts the sequence, so a test that resets
first gets the same answers on every run.

## Tests

`make test-unit` covers the rules with injected sleep and seed. `make test-integration` runs a
real server and checks the behaviours that need a socket (drop, hang, large bodies, redirects).
