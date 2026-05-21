# ADR: SC-node payout test/regtest execution harness (v0)

**Status:** Accepted (PR L — fake execution only)

**Date:** 2026-05-21

## Decision

Add a **test-only** payout execution harness that records fake/regtest execution state in dedicated tables (`sc_node_payout_test_executions`, `sc_node_payout_test_execution_rows`). PR L models execution transitions without moving real coins.

**This harness is not production payout execution and does not spend from the support wallet or any live AZCoin node.**

## Context

- PR K approves payout plans and records no-send preflight (`preflight_status=allowed`).
- Operators need to rehearse idempotency and status transitions (`sent` → `confirmed`) before a future guarded real executor PR.
- Live plan `payout_plan_id=1` may exist in production DB; PR L must never broadcast or call wallet RPC against it.

## Scope (v0)

| In scope | Out of scope |
|----------|----------------|
| `preview`, `execute-fake`, `mark-confirmed`, `details` CLI | Real `sendtoaddress` / `sendmany` |
| `fake_regtest` mode with deterministic fake txids | `azc` / `azcoin-cli` |
| Idempotent `execute-fake` per `(payout_plan_id, idempotency_key)` | Production wallet `wallet` execution |
| Read-only admin: `payout-test-executions`, `payout-test-execution-details` | Mutating payout plan to “executed on-chain” |

## Safety boundaries

- **No AZCoin Core RPC** — no wallet load, passphrase, or send APIs.
- **No `azc` or subprocess** — scripts use PostgreSQL only.
- **No private keys, mnemonics, RPC secrets, or passphrases.**
- **Test wallet names** must use `fake-` prefix or contain `regtest`; blocklist includes `wallet`, `support`, `production`.
- Fake txids use prefix `fake-regtest-` derived from plan id, row ids, and idempotency key (SHA-256 digest).

## State machine (test tables only)

| Header status | Meaning |
|---------------|---------|
| `draft` | Reserved for future flows |
| `executing` | Reserved |
| `sent` | Fake send recorded (`execute-fake`) |
| `confirmed` | Operator `mark-confirmed` after fake send |
| `failed` | Terminal failure (no confirm) |

Row statuses: `pending` → `sent` → `confirmed` (or `failed`).

## Idempotency (v0)

- Re-run `execute-fake` with same `payout_plan_id` + `idempotency_key` returns existing execution.
- Re-run with different `idempotency_key` while an active execution (`executing`/`sent`/`confirmed`) exists → refuse.

## Future production executor

A separate PR will introduce guarded real execution with reserve enforcement, current trusted balance checks, and explicit spend authorization. PR L does not implement that path.

## Consequences

- Operators can dry-run payout execution bookkeeping without coin movement.
- Test tables are clearly named to avoid confusion with future production execution tables.
- Safety tests grep implementation files for wallet-send keywords (guard regex excluded).
