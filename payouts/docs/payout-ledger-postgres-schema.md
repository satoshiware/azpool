# Payout Ledger Postgres Schema

## Why Postgres exists

The current SQLite code in `ledger/app` is still a prototype for payout runtime flow. This Postgres schema is the first durable accounting layer for SC-node payout settlement. It exists to support:

- auditable settlement windows
- actual block reward attribution by `blockhash`
- durable internal user credits and balances
- lean retention for raw worker snapshots
- safe retry behavior when rewards are delayed or settlement must be blocked

This schema is intentionally Postgres-first because the payout ledger needs stronger concurrency guarantees, explicit constraints, JSONB support for audit/raw payload capture, and predictable migration tooling as the accounting system grows.

## Why money is stored as integer sats/base units

All monetary values are stored as integer sat/base-unit values in `BIGINT` columns such as `reward_sats`, `amount_sats`, and `balance_sats`.

This avoids floating point rounding drift, keeps accounting exact, and makes ledger reconciliation deterministic. The runtime can render BTC/AZCOIN decimal display values at the API or UI boundary later, but settlement and ledger storage stay in integer base units.

## Why work is `NUMERIC(38,16)`

Difficulty-weighted miner work is not money. It is stored separately as `NUMERIC(38,16)` so the system can preserve high precision when:

- accumulating difficulty-weighted work from translator snapshots
- summing user work over a settlement window
- computing payout fractions from actual work contribution

`NUMERIC(38,16)` is intentionally wider than the current SQLite prototype so work accounting can remain precise over long-running nodes without collapsing into float behavior.

## Settlement window model

The settlement ledger is built around a shifted historical half-open window:

- `work_window_start = now - interval - maturity_offset`
- `work_window_end = now - maturity_offset`
- interval is expected to be 8 hours in production
- interval may be 10 minutes in testing
- maturity offset is expected to be 200 minutes

The runtime should use the same half-open window for both miner work and found blocks:

- `start <= timestamp < end`

Example production run:

- settlement run time: `2026-05-04T16:00:00Z`
- interval: 8 hours
- maturity offset: 200 minutes
- work window: `[2026-05-04T04:40:00Z, 2026-05-04T12:40:00Z)`

That exact shifted window supports the intended flow:

1. Read matured miner work from `miner_work_deltas`.
2. Read blocks found in that same window from `blocks_found`.
3. Require actual rewards for those `blockhash` values from `block_rewards`.
4. Sum reward totals and user work totals into `settlement_windows`.
5. Persist per-user work attribution in `settlement_user_work`.
6. Persist internal user settlement credits in `settlement_user_credits`.
7. Post durable account movements in `account_ledger_entries` and `account_balances`.

## Table roles

### Permanent tables

These tables are intended to be retained indefinitely for accounting, reconciliation, and audit:

- `users`
- `miner_identities`
- `settlement_windows`
- `settlement_blocks`
- `settlement_user_work`
- `settlement_user_credits`
- `account_ledger_entries`
- `account_balances`
- `audit_events`
- `service_cursors`

### Short-retention tables

These tables are raw/derived intake data and should not be kept forever by default:

- `raw_miner_snapshots`
- optional future raw upstream/downstream API payload capture, if added later

`miner_work_deltas` is a middle layer between raw snapshots and permanent settlement summaries. It can usually be retained for 90 days or longer, then compacted if storage pressure requires it.

## Translator and reward source mapping

The schema separates source-of-truth responsibilities by stage:

- `translator/miner-workers/snapshot`
  - lands in `raw_miner_snapshots`
  - normalized interval deltas land in `miner_work_deltas`
  - user mapping is resolved through `miner_identities`

- `translator/blocks-found`
  - lands in `blocks_found`
  - each block remains uniquely identifiable by `blockhash`

- `az/block/rewards`
  - lands in `block_rewards`
  - reward lookup is keyed by `blockhash`
  - missing reward data must block settlement retry rather than being interpreted as zero

## Audit and settlement safety properties

This schema is designed to support the payout rules without changing runtime behavior yet:

- settlement windows are unique by `(work_window_start, work_window_end)`
- each `blockhash` can fund only one settlement through `settlement_blocks.blockhash`
- reward data is separate from block detection so missing rewards remain visible
- user settlement credits are idempotent through `idempotency_key`
- account ledger entries are append-only style records tied to user balances

## What is intentionally not implemented yet

This migration does not change payout runtime logic. It does not implement:

- direct on-chain payout execution per miner
- settlement retry orchestration
- quarantining/blocking unmapped miner identities in runtime
- standalone/background compaction workers (inline post-settlement compaction is implemented)
- backfill from the existing SQLite prototype
- replacement of the current SQLite prototype API/runtime models

The Postgres schema is the storage foundation only.
