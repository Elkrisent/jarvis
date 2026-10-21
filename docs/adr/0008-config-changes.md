# ADR 0008: Config changes

Status: Accepted (locked, SPEC §2 D7)

## Decision
Versioned in Postgres, pushed to gateways via Redis pub/sub, applied in ≤ 1 s. Rollback supported.

## Consequences
See SPEC.md for the behavior this implies. Changing this decision requires a superseding ADR and user approval.
