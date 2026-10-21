# ADR 0001: Record architecture decisions

Status: Accepted

## Context
JARVIS has locked scope decisions (SPEC §2, D1–D12). Changes to them must be deliberate and traceable.

## Decision
Architecture decisions are recorded as ADRs in `docs/adr/`, one per file, numbered sequentially. ADRs 0002–0013 record locked decisions D1–D12. Any agreed change to a locked decision is a new ADR that supersedes the old one, and is also reflected in `docs/SPEC.md`.

## Consequences
Decisions are discoverable and reviewable in PRs.
