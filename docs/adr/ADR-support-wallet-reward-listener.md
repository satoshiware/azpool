# ADR: Support-wallet reward listener (read-only v0)

**Status:** Accepted (PR H — observe and record only)

**Date:** 2026-05-21

## Decision

The support node records **support-wallet reward-related transactions** (`generate`, `immature`, `orphan` from `azc listtransactions`) in Postgres table `support_wallet_reward_events`. PR H adds schema, normalization helpers, a manual scan script (dry-run by default), and read-only admin visibility.

**This does not send coins, create/sign/broadcast transactions, generate payout plans, or maintain an SC-node credit ledger.**

## Context

Pool telemetry (PR A–F) tracks accepted work per `sc_node_id`. The support wallet receives coinbase/staking-style rewards that must be observed before future SC-node crediting and payout planning.

Operators need a safe path to:

1. Scan the support wallet via read-only `azc listtransactions`
2. Normalize reward rows with maturity metadata
3. Optionally upsert into Postgres for audit
4. List stored events via read-only admin JSON

## Scope (this PR)

| In scope | Out of scope |
|----------|----------------|
| Migration `005_support_wallet_reward_events.sql` | `sendtoaddress`, `sendmany`, `sendrawtransaction` |
| `reward_events.py` normalization + SQL builders | `createrawtransaction`, `signrawtransaction`, `walletpassphrase` |
| `support_wallet_reward_events.py` (`print`, `scan`) | Payout execution or broadcast |
| Admin `reward-events` command (DB read-only) | SC-node credit ledger |
| Unit tests (no Postgres, no `azc` in CI) | Payout plan generator |
| | Collector timer/runtime changes |
| | systemd changes |

## Table shape

`support_wallet_reward_events` stores normalized reward rows with `wallet_name`, `txid`, `vout`, `category`, `amount`, `confirmations`, block metadata, boolean flags, `maturity_status`, and `raw_wallet_event` JSONB. Unique key: `(wallet_name, txid, vout)`.

## Maturity rules (v0)

- Categories `receive`, `send`, `move` are ignored.
- `immature` → `immature`; `orphan` → `orphaned`
- `confirmations < 0` → `conflicted`; `abandoned` → `abandoned`
- `generate` with confirmations ≥ threshold → `mature`, else `immature`

Default mature threshold: **100 confirmations** (configurable in scan CLI).

## Security

- **No private keys, seed phrases, or RPC passwords** are stored in the reward-events table or printed to stdout.
- `raw_wallet_event` is stored for audit but **hidden from default JSON output**.
- `azc` is used only for **`listtransactions`** via `subprocess` with explicit argv (`shell=False`).

## Future PRs

| PR | Scope |
|----|--------|
| **PR I** | SC-node credit ledger (no wallet sends) |
| **PR J** | Payout plan generator (no wallet sends) |
| **PR K** | Guarded dry-run wallet payout execution |
| Later | Guarded live wallet execution (separate approval) |

## Consequences

- Operators can observe and persist support-wallet rewards without enabling money movement.
- Downstream crediting remains blocked until PR I+.
- Collector timer behavior is unchanged; scanning is manual/on-demand in v0.
