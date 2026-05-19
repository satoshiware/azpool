# ADR: Support-node pool telemetry collector

## Status

Accepted (v0.1 scaffolding)

## Context

Pool instances (`pool_sv2`) expose monitoring HTTP APIs over WireGuard from the support/super node. Accepted-work counters (`shares_accepted`, `share_work_sum`, sequence numbers) are needed for future payout settlement but must not be conflated with wallet operations during ingestion.

The standalone template-provider repository migration established that support-node services own operational telemetry adjacent to AZCoin Core. Pool telemetry ingestion belongs on the support node, not on individual pool VMs.

## Decision

1. **Support node owns pool telemetry ingestion** via `payouts/collector/` (systemd: `azcoin-pool-collector.service`).
2. **Pool instances are DB-backed** via `pool_instances` (`monitoring_base_url`). The collector loads the active registry every one-shot timer run; `POOL_INSTANCES` env JSON is fallback only.
3. **Pool instances expose monitoring APIs** reachable from the support node over WireGuard.
4. **Collector stores snapshots and deltas** in Postgres tables:
   - `pool_channel_snapshots`
   - `pool_share_work_deltas`
   - `pool_collector_runs`
4. **Ledger/payout service reads deltas later**; the collector does not accrue credits or trigger settlement.
5. **Collector never moves money** — no wallet RPC, no payout batches, no transaction broadcast.
6. **Monitoring counters are v0.1 telemetry**, not final immutable share events. Counter resets (service restart) are detected and skipped rather than producing negative deltas.
7. **Future production** may patch `pool_sv2` to push immutable accepted-share events; this collector remains compatible as a polling baseline.

## Identity mapping (v0.1)

- Store observed `user_identity` verbatim for collector/audit telemetry only.
- **Support node pays/credits SC nodes, not individual users/workers.**
- Payout/credit math groups by `sc_node_id` only; `user_identity` must not appear in payout reports by default.
- Resolve `sc_node_id` in order:
  1. Native format (`az/scnode/<id>`, then documented legacy aliases `scnode.<id>`, `scnode-<id>`)
  2. Active rows in `sc_node_identity_mappings` (`exact` → longest `prefix` → deterministic `glob`)
- Unknown or unmapped identities remain **unmapped** (`sc_node_id = NULL`) and **unpaid**.
- `payout_enabled` on `sc_nodes` does not control telemetry mapping in v0.1.
- Temporary prefix example: `baveetstudy.` → `sc-3` with `payout_enabled = false`.
- Long-term native identity should be `az/scnode/<sc_node_id>`.
- Historical delta rows with `sc_node_id IS NULL` are not backfilled automatically in v0.1; optional backfill is a separate deliberate operation.
- SC node operators handle downstream worker/customer splits locally.

## Non-goals

- Creating payout credits, payout batches, or wallet transactions.
- Calling AZCoin Core wallet RPC (`sendmany`, `sendtoaddress`, etc.).
- Treating monitoring counters as immutable share proofs.

## Consequences

- Apply `payouts/migrations/001_pool_telemetry_collector.sql` before starting the collector.
- Apply `payouts/migrations/002_sc_node_identity_mapping.sql` before using database identity mappings.
- Apply `payouts/migrations/003_pool_instance_registry.sql` before using DB-backed pool instance registry.
- Configure `DATABASE_URL` in `/etc/azcoin-super/pool-ledger/collector.env`. `POOL_INSTANCES` is optional fallback only.
- Operate via `docs/runbooks/pool-monitoring-collector.md`.
