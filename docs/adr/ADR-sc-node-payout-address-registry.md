# ADR: SC-node payout address registry

**Status:** Accepted (PR G — registry only, no payout execution)

**Date:** 2026-05-19

## Decision

The support node stores **payout destination addresses per SC node** in Postgres table `sc_node_payout_addresses`. This PR adds the schema, read-only admin visibility, validation helpers, and documentation only.

**No wallet sends from the support ledger, no transaction creation or broadcast.** SC-node operators may use `azc` locally to verify listener addresses; the registry stores destination addresses only (no private keys or seed phrases).

## Context

Support-node architecture pays/credits **SC nodes** (`sc_node_id`) only — not individual pool worker `user_identity` values. Worker/customer splits happen below the SC node.

Before reward credits or payout automation, the support node needs a **verified registry** mapping each SC node to one or more payout addresses, with at most one **active default** address per SC node.

Legacy user-level payout code under `payouts/app/` remains documented and untouched. This registry is the first **replacement SC-node payout foundation** artifact.

## Scope (this PR)

| In scope | Out of scope |
|----------|----------------|
| Migration `004_sc_node_payout_addresses.sql` | Wallet RPC / `azc` |
| Read-only SQL helpers + admin `payout-addresses` command | Reward listener |
| Field validation (status, source, non-empty address) | Credit ledger |
| Manual operator runbook for insert/activate/revoke | Payout plan generator |
| Unit tests | Payout execution |

## Table shape

`sc_node_payout_addresses`:

- `id` BIGSERIAL PRIMARY KEY
- `sc_node_id` → `sc_nodes(id)` ON UPDATE CASCADE ON DELETE RESTRICT
- `payout_address` TEXT NOT NULL, UNIQUE, non-empty
- `label` TEXT optional
- `address_source` ∈ `manual`, `imported`, `wallet`, `api`
- `status` ∈ `pending_verification`, `active`, `inactive`, `revoked`
- `is_default` BOOLEAN
- `verified_at`, `retired_at` (nullable), `created_at`, `updated_at` TIMESTAMPTZ

Partial unique index: one row per `sc_node_id` where `is_default = true AND status = 'active'`.

## Status lifecycle

1. **`pending_verification`** — address recorded; ownership not yet confirmed.
2. **`active`** — operator verified ownership; eligible as payout target.
3. **`inactive`** — temporarily disabled; not used for new payouts.
4. **`revoked`** — permanently retired; retained for audit.

## Default address rule

At most **one active default** payout address per SC node (enforced by partial unique index). Setting a new default requires deactivating or clearing the previous default in manual SQL until a dedicated admin mutation tool exists.

## Manual verification requirement

Registry insert **does not prove wallet ownership**. Operators must verify control of the address through an out-of-band process before setting `status = 'active'` and `verified_at`.

SQL and Python helpers **do not** validate AZCoin address format on-chain or via wallet RPC in this PR.

## Future PRs

| PR | Scope |
|----|--------|
| **PR H** | Read-only support-wallet reward listener |
| **PR I** | Reward event table + SC-node credit ledger |
| **PR J** | Payout plan generator (no wallet sends) |
| **PR K** | Guarded dry-run wallet payout execution |
| Later | Guarded live wallet execution (separate approval) |

## Consequences

- Operators can register and review SC-node payout addresses without enabling money movement.
- Payout automation remains blocked until credit ledger and guarded execution PRs land.
- `user_identity` telemetry remains separate; payout targets are always `sc_node_id` + registered address.
