# ADR: Support-node pool telemetry collector

## Status

Accepted (v0.1 scaffolding)

## Context

Pool instances (`pool_sv2`) expose monitoring HTTP APIs over WireGuard from the support/super node. Accepted-work counters (`shares_accepted`, `share_work_sum`, sequence numbers) are needed for future payout settlement but must not be conflated with wallet operations during ingestion.

The standalone template-provider repository migration established that support-node services own operational telemetry adjacent to AZCoin Core. Pool telemetry ingestion belongs on the support node, not on individual pool VMs.

## Decision

1. **Support node owns pool telemetry ingestion** via `payouts/collector/` (systemd: `azcoin-pool-collector.service`).
2. **Pool instances expose monitoring APIs** reachable from the support node (for example `http://10.10.70.131:9090`, `http://10.10.70.43:9090`).
3. **Collector stores snapshots and deltas** in Postgres tables:
   - `pool_channel_snapshots`
   - `pool_share_work_deltas`
   - `pool_collector_runs`
4. **Ledger/payout service reads deltas later**; the collector does not accrue credits or trigger settlement.
5. **Collector never moves money** — no wallet RPC, no payout batches, no transaction broadcast.
6. **Monitoring counters are v0.1 telemetry**, not final immutable share events. Counter resets (service restart) are detected and skipped rather than producing negative deltas.
7. **Future production** may patch `pool_sv2` to push immutable accepted-share events; this collector remains compatible as a polling baseline.

## Identity mapping (v0.1)

- Store observed `user_identity` verbatim.
- Derive `sc_node_id` only when a safe, documented parser rule matches (`az/scnode/<id>`, `scnode.<id>`, `scnode-<id>`).
- Unknown identities (for example `baveetstudy.miner1`) remain **unmapped** (`sc_node_id = NULL`).

## Non-goals

- Creating payout credits, payout batches, or wallet transactions.
- Calling AZCoin Core wallet RPC (`sendmany`, `sendtoaddress`, etc.).
- Treating monitoring counters as immutable share proofs.

## Consequences

- Apply `payouts/migrations/001_pool_telemetry_collector.sql` before starting the collector.
- Configure `DATABASE_URL` and `POOL_INSTANCES` in `/etc/azcoin-super/pool-ledger/collector.env`.
- Operate via `docs/runbooks/pool-monitoring-collector.md`.
