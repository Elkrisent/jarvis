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

The dev override publishes the api on `127.0.0.1:8081` (the default stack publishes no host ports).

```bash
cd deploy
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build --wait
set -a; . ./.env; set +a      # exports JARVIS_ADMIN_TOKEN into this shell
```

If another program already holds host port 8080 or 8082 (on Windows with WSL some ports are reserved), `up --wait` reports a bind error for the gateway or worker; the api on 8081 is still usable for everything below, or change the published ports in `deploy/docker-compose.dev.yml`.

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

# Call the gateway with the key (the gateway is on 127.0.0.1:8080 in the dev stack). It now
# authenticates the key; forwarding to the provider arrives in a later module, so a valid key
# gets 501 JV_NOT_IMPLEMENTED and a missing, unknown or revoked key gets 401 JV_AUTH_INVALID.
JARVIS_KEY=$(echo "$KEY_JSON" | jq -r .key)
GW=http://127.0.0.1:8080
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
* `POST /apis/{id}/test` answers 501 until M4.10.

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
