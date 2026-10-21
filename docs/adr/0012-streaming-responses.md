# ADR 0012: Streaming responses

Status: Accepted (locked, SPEC §2 D11)

## Decision
Rejected in v1 with a clear error when detected (`Accept: text/event-stream` or a streaming flag in the body, if configured).

## Consequences
See SPEC.md for the behavior this implies. Changing this decision requires a superseding ADR and user approval.
