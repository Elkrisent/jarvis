# M1.6 Path Templating Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A pure, fast `RouteTemplater` that turns a raw request path into a route template for metrics: explicit per-API patterns first, then an automatic `{id}` fallback, with a per-API cap of 500 distinct fallback templates and an `{other}` overflow bucket.

**Architecture:** One module in `jarvis_core` (shared by the gateway now, telemetry in M1.9 and the worker later), no I/O, no dependencies. One instance per API holds the compiled patterns and the set of fallback templates seen so far. Wiring into the request pipeline is M1.8/M1.9; this module only provides the function and its benchmark.

**Tech Stack:** Python stdlib only; tests use pytest and `hypothesis==6.131.15` (already pinned); benchmark uses pytest-benchmark (already pinned).

**Spec:** `docs/SPEC.md` §4.14 and §5.1 (`routes[].pattern`); `docs/IMPLEMENTATION.md` M1.6; requirements REQ-4.14-1 to REQ-4.14-4.

**Prerequisite:** M1.5 merged (`d756c4c`). Branch `feat/m1.6-path-templating` from `main`.

## Global Constraints

- "Metrics are grouped by route template, not raw path." (SPEC §4.14)
- "1. Explicit patterns configured per API, e.g. `/v1/users/{id}/orders`, matched first. 2. Automatic fallback: replace segments that are purely numeric, UUIDs, or longer than 24 characters of hex or base64-like text with `{id}`. 3. Templates per API are capped at 500 by default, and the overflow goes into an `{other}` bucket."
- DoD: the `route_template_match` benchmark is logged and below 20 µs per call.
- `packages/core` gates: ≥ 95% line / 90% branch, no drop of more than 1 point vs `main`. Core is mypy-strict. Every test has exactly one type marker; requirement tests carry `@pytest.mark.req("REQ-…")`.
- No Claude attribution in commits or the PR body (user memory).

## Decisions made here (not in the spec)

- **Pattern syntax.** A segment that is exactly `{name}` matches any one non-empty segment; every other segment must match exactly (case-sensitive, no percent-decoding). Segment counts must be equal. `Route.pattern` already guarantees a leading `/`.
- **Several patterns match.** The one with the most literal segments wins; ties go to the configured order. The result is the pattern string as configured (trailing slash removed).
- **Normalisation.** Anything from the first `?` or `#` on is dropped, empty segments are ignored, a trailing slash is ignored, the root is `/`.
- **Fallback rules.** A segment becomes `{id}` if it is all ASCII digits, a UUID (8-4-4-4-12 hex, any case), or longer than 24 characters made only of `A-Za-z0-9_-+=`. Other segments stay literal.
- **Cap.** The cap counts distinct fallback templates only. Explicit pattern results are bounded by the API's own config, are always returned, and are not counted. When a new fallback template arrives with 500 already seen, the result is `{other}`. The cap is a constructor argument (`max_templates=500`), not a config field: SPEC §5.1 has no such field and adding one changes the config schema.
- **Hostile input.** A path longer than 2048 characters or with more than 32 segments is `{other}` without being stored, so memory stays bounded and the function never raises.
- **Budget.** `route_template_match` in `benchmarks/budgets.yaml` changes from `null` to `20` (µs/op) to match the DoD.
- **Not wired yet.** The gateway pipeline (M1.8) will keep one `RouteTemplater` per API entry and rebuild it on a config change.

## Review Focus

- Pattern vs fallback precedence: `/v1/users/42` with pattern `/v1/users/{id}` returns the pattern, and with pattern `/v1/users/me` plus `/v1/users/{id}` the literal one wins for `/v1/users/me`.
- Case and trailing-slash variants (`/V1/Users/`, `/v1/users`) do not multiply templates beyond what they are.
- A path made only of unique literal words (`/a1`, `/a2` … thousands of them) hits the cap and returns `{other}`.
- Empty path, `//`, `?x=1`, unicode digits (`/v1/١٢٣`), 25-char alphabetic words, a 100 KB path: no exception, bounded output.
- Same input, same output on a fresh instance (stable), and an already-seen template is still returned after the cap is full.

---

### Task 1: RouteTemplater

**Files:**
- Create: `packages/core/src/jarvis_core/route_template.py`
- Test: `packages/core/tests/unit/test_route_template.py`, `packages/core/tests/unit/test_route_template_properties.py`

**Interfaces:**
- Produces: `OTHER = "{other}"`, `DEFAULT_MAX_TEMPLATES = 500`, `class RouteTemplater: def __init__(self, patterns: Sequence[str] = (), *, max_templates: int = DEFAULT_MAX_TEMPLATES) -> None; def template(self, path: str) -> str`.

- [ ] **Step 1: Write failing tests** (unit marker). Table-driven `template()` cases, fresh instance each: `/v1/users/42` → `/v1/users/{id}` (4.14-3); `/v1/items/550e8400-e29b-41d4-a716-446655440000` and the upper-case form → `{id}`; a 25-char hex string and a 30-char base64url string → `{id}`; a 24-char string stays literal; `/v1/users/me` stays; `/v1/users/١٢٣` (unicode digits) stays literal; `/v1/a/` equals `/v1/a`; `//v1//a` equals `/v1/a`; `""` and `"/"` → `/`; `/v1/a?x=1#f` → `/v1/a`; mixed `/v1/users/42/orders/9f86d081884c7d659a2feaa0c55ad015` → `/v1/users/{id}/orders/{id}`. Explicit (4.14-2, 4.14-1): pattern `/v1/users/{id}/orders` returns itself for `/v1/users/7/orders` and `/v1/users/abc/orders` (grouping of different raw paths); explicit wins over fallback for `/v1/users/42` with `/v1/users/{id}`; literal pattern `/v1/users/me` beats `/v1/users/{user}` for `/v1/users/me` while `/v1/users/bob` gets `/v1/users/{user}`; tie goes to configured order; pattern not matching falls back; different segment counts never match. Cap (4.14-4): with `max_templates=3`, `/a`, `/b`, `/c` return themselves, `/d` returns `{other}`, `/a` still returns `/a`; explicit patterns are never replaced by `{other}` even when the cap is full; default cap is 500 (`/p0`…`/p499` pass, `/p500` is `{other}`). Hostile: 2049-char path and 33-segment path return `{other}` and do not grow the seen set.
- [ ] **Step 2: Property tests** (hypothesis, unit marker, `max_examples=200`): `template(text())` never raises for any text, including surrogate-free arbitrary unicode; two fresh instances give identical output for the same input; with `max_templates=20` and 500 random paths the set of distinct outputs has at most 21 members (20 + `{other}`), and with patterns added at most 21 + number of patterns.
- [ ] **Step 3: Run, see them fail** (`uv run pytest packages/core/tests/unit/test_route_template.py -q`: ImportError).
- [ ] **Step 4: Implement** `RouteTemplater` per the decisions above. Compile patterns once in `__init__` into a dict keyed by segment count, sorted by descending literal-segment count (stable). Precompiled regexes for UUID and long-token. No per-call allocation beyond the segment list.
- [ ] **Step 5: Run, see them pass;** `make lint typecheck`; core coverage stays ≥ gates.
- [ ] **Step 6: Commit** `feat(core): route templating with explicit patterns, {id} fallback and a per-API cap (M1.6)`.

### Task 2: Benchmark, requirements and docs

**Files:**
- Create: `packages/core/tests/bench/test_bench_route_template_match.py`
- Modify: `benchmarks/budgets.yaml` (budget 20), `docs/traceability.md` (regenerated), `docs/PROGRESS.md`, `BENCHMARKS.md` only if `make bench-micro` rewrites it.

- [ ] **Step 1: Write the benchmark** (`perf` marker, `benchmark` fixture): a templater with three explicit patterns, called on a realistic mixed path (`/v1/users/12345/orders/550e8400-e29b-41d4-a716-446655440000`) and an explicit-match path; the benchmarked callable does both. Metric name comes from the test name (`route_template_match`).
- [ ] **Step 2: Run** `make bench-micro`; read `build/bench/results.json`; confirm `route_template_match` is recorded and below 20 µs. If it is not, optimise the implementation (not the budget) and say so in the ledger.
- [ ] **Step 3:** Set the budget to 20 in `benchmarks/budgets.yaml`; run `make bench-micro` again; the budget checker must pass.
- [ ] **Step 4:** Run `make traceability COMPLETED_PHASE=0 TRACE_MARKERS="not perf"` (needs Docker, Postgres and Redis env); confirm REQ-4.14-1 to -4 are covered and Checkpoint 0 rows still `pass`.
- [ ] **Step 5:** Update `docs/PROGRESS.md` (M1.6 completed with real numbers, next M1.7), README only if it lists modules.
- [ ] **Step 6: Commit** `feat(core): benchmark route_template_match and record M1.6 (M1.6)`.

## Self-Review

- **Coverage:** REQ-4.14-1 (grouping tests), -2 (explicit first), -3 (fallback table), -4 (cap tests). DoD benchmark in Task 2.
- **Types:** `RouteTemplater`, `OTHER`, `DEFAULT_MAX_TEMPLATES`, `template` named identically in both tasks.
- **Open for the user:** the cap counts only fallback templates and is not configurable per API (SPEC §5.1 has no field); `{other}` also covers over-long or very deep paths.
