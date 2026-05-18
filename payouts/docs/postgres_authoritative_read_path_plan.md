# Postgres Authoritative Read Path Plan

## Goal

Define the next implementation phase for a feature-flagged Postgres-authoritative read path that can be enabled only for selected read-only ledger/reporting endpoints and rolled back immediately to SQLite.

This phase is specifically about read-path planning. SQLite remains the source of truth for settlement execution and all money-moving behavior.

## Non-goals

- making Postgres authoritative for settlement execution
- changing payout math, reward attribution math, or settlement calculations
- changing payout creation, payout submission, or wallet/RPC money movement
- changing idempotency source of truth
- changing balance mutation or credit mutation source of truth
- introducing migrations in this planning step
- changing deployment config, systemd units, or service startup behavior in this planning step
- changing `master-api/*`

## Current verified state

The current verified production state for this plan is:

- PR `#9` was merged into `payouts`
- SC-2 ledger pulled merged `payouts` successfully
- `azcoin-ledger` service restarted cleanly
- Postgres password rotation/fix is complete
- historical backfill for settlements `40` through `48` completed successfully
- settlement `49` was already present from live shadow-write
- latest audit for settlements `40` through `49` is clean:
  - `comparison_status: matched`
  - `matched_count: 10`
  - `mismatched_count: 0`
  - `not_found_count: 0`
  - `error_count: 0`
- SQLite remains authoritative
- Postgres remains shadow-only
- settlement `50+` live observation is still required before enabling any authoritative Postgres behavior

## Required observation gates before implementation

Implementation of a feature-flagged Postgres-authoritative read path should not begin until the following gates are satisfied:

1. At least one additional live settlement after `49` is observed end-to-end in both SQLite and Postgres shadow data.
2. Preferably at least several consecutive live settlements after `49` are observed, not just one isolated interval.
3. Bulk audit over the historical backfill range plus new live settlements remains clean with:
   - `comparison_status: matched`
   - `mismatched_count: 0`
   - `not_found_count: 0`
   - `error_count: 0`
4. Read-path candidates are explicitly enumerated and limited before any flag is enabled.
5. SQLite fallback behavior is specified and tested before any flag can be turned on.
6. A clear operator rollback procedure is documented before any production rollout.

If any shadow mismatch appears during the gate period, authoritative Postgres read flags must remain disabled.

## Proposed feature flags

The future implementation should use explicit flags, all defaulting to disabled or safe fallback behavior.

### `POSTGRES_LEDGER_READS_ENABLED`

Master gate for allowing selected ledger read paths to prefer Postgres.

- default: disabled
- when disabled: all reads continue to use SQLite

### `POSTGRES_LEDGER_READ_FALLBACK_TO_SQLITE`

Controls whether a Postgres read failure immediately falls back to SQLite.

- default: enabled
- expected behavior: if Postgres read fails, timeout occurs, or payload validation fails, serve the SQLite result instead

### `POSTGRES_LEDGER_READ_REQUIRE_SHADOW_MATCH`

Requires clean shadow-audit state before any Postgres authoritative read path can be used.

- default: enabled
- if shadow audit is not clean: force SQLite path

### `POSTGRES_LEDGER_READ_ALLOWED_ENDPOINTS`

Explicit allowlist of endpoints or read-path identifiers that may use Postgres.

- default: empty allowlist
- purpose: prevent accidental broad cutover
- expected behavior: only allow specifically approved read-only paths

## Read paths that may move to Postgres first

The first candidates should be read-only/reporting/query paths where rollback to SQLite is straightforward and no payout or credit mutation depends on the result.

- Postgres shadow audit and read-only diagnostics
- settlement history read APIs
- settlement detail read APIs
- credit/payout reporting read APIs only where SQLite comparison is already proven and the response is informational
- internal reconciliation or audit views that summarize already-written data

Selection criteria for first-wave migration:

- no writes
- no settlement side effects
- no wallet interaction
- no idempotency ownership
- easy one-request fallback to SQLite
- existing SQLite response shape can remain the compatibility baseline

## Read paths that must remain SQLite for now

The following paths or behaviors must remain SQLite-backed in this phase:

- settlement execution
- payout creation
- payout transaction submission
- idempotency source of truth
- balance mutation
- credit mutation
- any wallet or RPC money movement
- any code path that decides whether money is owed, posted, sent, retried, or finalized
- any path where a Postgres read result would directly change operator-visible payout behavior

## Rollback plan

Rollback must be immediate and operationally simple.

Primary rollback:

- set `POSTGRES_LEDGER_READS_ENABLED` to disabled
- keep `POSTGRES_LEDGER_READ_FALLBACK_TO_SQLITE` enabled
- restart or reload only the ledger service behavior needed for config pickup
- confirm reads return through SQLite again

Safety rules:

- rollback must not require schema changes
- rollback must not require data rewriting
- rollback must not require replaying settlements
- rollback must not change existing SQLite data

Trigger conditions for rollback:

- any shadow audit mismatch
- any Postgres read timeout or repeated query failure
- any response-shape divergence from expected SQLite-compatible output
- any operator uncertainty about whether a read path is safe

## Validation plan

Future implementation validation should include:

1. Unit tests proving flag-off behavior remains SQLite-only.
2. Unit tests proving per-endpoint allowlist enforcement.
3. Unit tests proving Postgres failure falls back to SQLite when fallback is enabled.
4. Unit tests proving clean shadow-match is required when `POSTGRES_LEDGER_READ_REQUIRE_SHADOW_MATCH` is enabled.
5. Endpoint tests comparing SQLite and Postgres-backed responses for approved read paths.
6. Staging or canary verification with authoritative Postgres reads enabled only for a small approved endpoint set.
7. Production verification against fresh live settlements with repeated clean shadow audits.

Success criteria for rollout validation:

- approved read endpoints return expected data from Postgres
- SQLite fallback works on forced Postgres failure
- no change to settlement execution behavior
- no change to payout math
- no change to money-moving behavior

## Failure modes and mitigations

### Shadow mismatch appears

- mitigation: keep `POSTGRES_LEDGER_READS_ENABLED` disabled or disable it immediately
- mitigation: require clean shadow audit before authoritative reads

### Postgres query fails or times out

- mitigation: fallback to SQLite when `POSTGRES_LEDGER_READ_FALLBACK_TO_SQLITE` is enabled
- mitigation: log and alert on fallback frequency

### Response shape differs between SQLite and Postgres

- mitigation: keep SQLite response shape as the compatibility contract
- mitigation: block rollout until equivalence is proven for the allowed endpoint set

### Operator accidentally broadens cutover scope

- mitigation: require `POSTGRES_LEDGER_READ_ALLOWED_ENDPOINTS`
- mitigation: start with an empty allowlist by default

### Read-path drift causes hidden business-logic changes

- mitigation: restrict first-wave usage to reporting and diagnostics only
- mitigation: keep all write and execution paths on SQLite

## Deployment plan

The future deployment should be staged.

### Phase 1

- ship implementation code with all flags safe by default
- keep Postgres authoritative reads disabled
- verify no behavior change in production

### Phase 2

- enable Postgres authoritative reads only for one or more read-only diagnostic endpoints
- keep fallback to SQLite enabled
- require clean shadow match

### Phase 3

- expand to selected settlement history/detail/reporting endpoints only after repeated clean results
- continue to exclude execution and money-moving paths

### Phase 4

- reassess whether broader read usage is safe
- do not begin write-path or execution-path migration in the same PR or rollout

## Open questions

- How many post-`49` live settlements should be required before implementation begins: one minimum gate or a larger consecutive sample?
- Which exact endpoint names should be in the first allowlist?
- Should clean shadow-match be evaluated globally, per-settlement range, or per-endpoint query scope?
- What is the expected fallback behavior when Postgres returns partial data for a multi-row response?
- What operator-visible metrics or alerts should track Postgres read failures and SQLite fallback frequency?
- Should the future implementation expose whether a response was served from Postgres or SQLite for diagnostics?

## Definition of done for the future implementation PR

The future implementation PR is done only when all of the following are true:

1. Only approved read-only ledger endpoints can use Postgres.
2. All new Postgres authoritative read behavior is feature-flagged.
3. SQLite remains authoritative for settlement execution and all money-moving behavior.
4. SQLite fallback is implemented and tested.
5. Clean shadow-match gating is implemented and tested.
6. Approved Postgres-backed read responses are validated against SQLite-equivalent expectations.
7. Rollback instructions are documented and operationally simple.
8. No payout math, settlement execution behavior, wallet behavior, idempotency ownership, or balance mutation behavior changes.
9. Live validation after settlement `50+` confirms the planned gates before any production flag enablement.
