# ADR 0011: Credential injection

Status: Accepted (locked, SPEC §2 D10)

## Decision
Per-API mode: bearer header, custom header, or query parameter. The client's credential is always stripped.

## Consequences
See SPEC.md for the behavior this implies. Changing this decision requires a superseding ADR and user approval.
