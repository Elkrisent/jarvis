# Quickstart: run the stack and use the management API

This walks through starting JARVIS locally and managing a project, an API and a JARVIS key
through `/api/v1`.

> Note: the gateway does not proxy requests yet (M1.5), so this covers configuration only.

## Prerequisites

Docker with Compose v2, `openssl`, `curl` and `jq`.

## 1. Generate the two secrets

```bash
cp deploy/.env.example deploy/.env
openssl rand -base64 32     # JARVIS_MASTER_KEY: encrypts stored provider credentials
openssl rand -hex 32        # JARVIS_ADMIN_TOKEN: the bearer token for the management API
```

Edit `deploy/.env`: set `POSTGRES_PASSWORD`, `JARVIS_MASTER_KEY` and `JARVIS_ADMIN_TOKEN` to the
values above. The api refuses to start if either secret is missing or malformed (the token must be
at least 32 characters). Keep the master key: without it stored credentials cannot be read.
`deploy/.env` is ignored by git; never commit it.

## 2. Start the stack

The dev override publishes the gateway on `127.0.0.1:8090`, the api on `127.0.0.1:8081` and the worker on `127.0.0.1:8082` (the default stack publishes no host ports).

```bash
cd deploy
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build --wait
set -a; . ./.env; set +a      # exports JARVIS_ADMIN_TOKEN into this shell
```

If another program already holds host port 8090, 8081 or 8082 (on Windows with WSL some ports are reserved), `up --wait` reports a bind error; change the published ports in `deploy/docker-compose.dev.yml`. Port 8080 is avoided on purpose: WSL often reserves it.

## 3. Use the API

Every call carries `Authorization: Bearer $JARVIS_ADMIN_TOKEN`; without it the answer is 401.

```bash
API=http://127.0.0.1:8081/api/v1
AUTH="Authorization: Bearer $JARVIS_ADMIN_TOKEN"

# Create a project
PROJECT_ID=$(curl -sS -X POST "$API/projects" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name": "Demo", "slug": "demo"}' | jq -r .id)

# Create an API with a bearer credential
API_ID=$(curl -sS -X POST "$API/projects/$PROJECT_ID/apis" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"slug": "weather", "name": "Weather", "base_url": "https://api.weather.example",
       "jarvis_key_slot": "authorization",
       "credential": {"mode": "bearer", "secret": "replace-with-the-provider-secret"}}' | jq -r .id)

# Create a JARVIS key. The full key is in this response only; copy it now.
KEY_JSON=$(curl -sS -X POST "$API/projects/$PROJECT_ID/keys" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name": "local dev"}')
echo "$KEY_JSON" | jq .
KEY_ID=$(echo "$KEY_JSON" | jq -r .id)

# List what exists (keys show a prefix only; APIs show has_secret, never the secret)
curl -sS "$API/projects/$PROJECT_ID/keys" -H "$AUTH" | jq .
curl -sS "$API/projects/$PROJECT_ID/apis" -H "$AUTH" | jq .

# Call the gateway with the key (the gateway is on 127.0.0.1:8090 in the dev stack). It checks the
# key and forwards the request to the API's base_url with the real provider credential added.
# The example upstream api.weather.example does not exist, so a valid key gets 502
# JV_UPSTREAM_UNREACHABLE; a missing, unknown or revoked key gets 401 JV_AUTH_INVALID.
JARVIS_KEY=$(echo "$KEY_JSON" | jq -r .key)
GW=http://127.0.0.1:8090
sleep 1   # the gateway learns of a new key within 1 s
curl -sS -i "$GW/gw/weather/v1/current" -H "X-Jarvis-Key: $JARVIS_KEY" | sed -n '1p;/^{/p'
curl -sS -i "$GW/gw/weather/v1/current" -H "Authorization: Bearer $JARVIS_KEY" | sed -n '1p'
curl -sS -i "$GW/gw/weather/v1/current" | sed -n '1p;/^{/p'

# Change a field: PATCH is a JSON merge patch, validated as a whole, and creates a new version
curl -sS -X PATCH "$API/apis/$API_ID" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"deadline_ms": 4000}' | jq '{version, deadline_ms}'

# Revoke the key (it stays listed with revoked_at set)
curl -sS -X DELETE "$API/keys/$KEY_ID" -H "$AUTH" -w '%{http_code}\n'
sleep 1; curl -sS -o /dev/null -w 'gateway after revoke: %{http_code}\n' "$GW/gw/weather/v1/current" \
  -H "X-Jarvis-Key: $JARVIS_KEY"

# Delete the API, then the project
curl -sS -X DELETE "$API/apis/$API_ID" -H "$AUTH" -w '%{http_code}\n'
curl -sS -X DELETE "$API/projects/$PROJECT_ID" -H "$AUTH" -w '%{http_code}\n'

unset KEY_JSON JARVIS_KEY
```

### Things to know

* `credential.secret` is write-only. It is encrypted with the master key before it is stored and is
  never returned. `credential.has_secret` says whether one is stored.
* In a PATCH, omit `credential.secret` to keep the stored secret, or send a new one to replace it.
* To remove the provider credential and its secret, send `{"credential": null}`. This works in every mode.
* Explicit alternative: `{"credential": {"mode": "none"}}` for a bearer API, and
  `{"credential": {"mode": "none", "header_name": null}}` (or `"param_name": null`) for a header
  (or query) API. Without the nulled field the stored one is kept and the request is a 422.
* `"secret": null` in `bearer`, `header` or `query` mode is a 422, because those modes require a secret.
* A JARVIS key looks like `jv_live_...`. Only its hash is stored, so it cannot be shown again; create
  a new key if you lose it.
* Every create or change of an API stores a new immutable policy version and publishes
  `config:changed` on Redis. Creating or revoking a key, deleting an API and deleting a project publish
  `config:invalidated` with `{"kind": "key"|"api"|"project", "id": "..."}`.
* Validation errors are 422 with the field path of each problem, in FastAPI's `{"detail": [...]}` shape. Other errors use `{"error": {"code", "message"}}`, so clients must handle both.
* The gateway takes the key in `X-Jarvis-Key`, or, for SDKs that only let you change the API key, in the
  API's `jarvis_key_slot` header (default `x-jarvis-key`; the example above sets `"jarvis_key_slot": "authorization"` so
  `Authorization: Bearer jv_live_...` works). A key with an empty `allowed_api_ids` may call every API in its
  project; otherwise only the listed ones. All failures return the same 401 body.
* Upstream hosts are checked when an API is saved (and on every gateway connection). By
  default JARVIS refuses loopback, private (RFC 1918), link-local (including `169.254.169.254`),
  CGNAT, IPv6 ULA/link-local and multicast addresses, the JARVIS containers, and plain `http`; the
  answer is a 422 on `base_url` with the message `destination is not allowed`. To use an internal
  upstream, list it in `JARVIS_SSRF_ALLOWLIST` (CIDRs and/or host names, see `deploy/.env.example`)
  and restart. A host name that does not resolve is accepted at save time; the gateway checks again
  when it connects.
* The gateway forwards `{METHOD} /gw/{slug}/{path}?{query}` to `{base_url}/{path}?{query}`. Before
  it does, it removes the JARVIS key and any hop-by-hop headers, replaces whatever the client sent in
  the credential's slot (an `Authorization` header, a custom header or a query parameter) with the
  stored provider secret, and refuses bodies over `JARVIS_MAX_BODY_BYTES` (default 10 MB, 413) and
  `Accept: text/event-stream` requests (400). The upstream's answer, including its errors, comes
  back unchanged with `X-Jarvis-Request-Id`, `X-Jarvis-Source` and `X-Jarvis-Attempts` added.
* `POST /apis/{id}/test` answers 501 until M4.10.

## 4. Forward to a real upstream

The calls above end in a 502 because the example upstream does not exist. To see a full round trip,
start a throwaway upstream on the stack's network, allow it, and point an API at it:

```bash
docker run -d --rm --name demo-upstream --network jarvis_default python:3.12-slim \
  python -m http.server 8000
echo 'JARVIS_SSRF_ALLOWLIST=demo-upstream' >> deploy/.env     # plain http to an internal host
(cd deploy && docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --wait api gateway)

PROJECT_ID=$(curl -sS -X POST "$API/projects" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name": "Round trip", "slug": "round-trip"}' | jq -r .id)
curl -sS -X POST "$API/projects/$PROJECT_ID/apis" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"slug": "demo", "name": "Demo", "base_url": "http://demo-upstream:8000",
       "jarvis_key_slot": "authorization",
       "credential": {"mode": "bearer", "secret": "the-real-provider-secret"}}' | jq '{slug, version}'
JARVIS_KEY=$(curl -sS -X POST "$API/projects/$PROJECT_ID/keys" -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{"name": "demo"}' | jq -r .key)
sleep 1

# An SDK that only lets you change its base URL and API key would do exactly this:
curl -sS -i "$GW/gw/demo/" -H "Authorization: Bearer $JARVIS_KEY" | sed -n '1p;/^[Xx]-[Jj]arvis/p'
docker logs demo-upstream 2>&1 | tail -2     # shows the request; the JARVIS key never arrives

# The call also left one telemetry event on the Redis Stream (ids, route, status, timings, bytes;
# never the key, the credential or the query string):
(cd deploy && docker compose exec redis redis-cli XREVRANGE telemetry + - COUNT 1)
curl -sS http://127.0.0.1:8090/readyz | jq .telemetry     # {"queue": 0, "dropped_total": 0}

docker rm -f demo-upstream
curl -sS -X DELETE "$API/projects/$PROJECT_ID" -H "$AUTH" -w '%{http_code}\n'
```

## OpenAPI

The schema is served at `/openapi.json`, behind the same token:

```bash
curl -sS http://127.0.0.1:8081/openapi.json -H "$AUTH" | jq '.paths | keys'
```

A committed snapshot (`tests/contract/openapi.snapshot.json`) fails the contract test when the API
changes; regenerate it with `make openapi-snapshot` and explain the change in the PR.

## Stop

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml down      # add -v to delete the database
```
