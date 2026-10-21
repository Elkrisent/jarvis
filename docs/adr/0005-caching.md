# ADR 0005: Caching

Status: Accepted (locked, SPEC §2 D4)

## Decision
Configured TTL per endpoint, plus an optional "honor upstream `Cache-Control`" toggle. Never cache non-2xx responses or responses with `Set-Cookie`.

## Consequences
See SPEC.md for the behavior this implies. Changing this decision requires a superseding ADR and user approval.
