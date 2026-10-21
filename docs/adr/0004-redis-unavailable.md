# ADR 0004: Redis unavailable

Status: Accepted (locked, SPEC §2 D3)

## Decision
Fail open with degraded protection: in-memory per-instance rate limiter, cache and dedup skipped, circuit state held in process memory. One global setting.

## Consequences
See SPEC.md for the behavior this implies. Changing this decision requires a superseding ADR and user approval.
