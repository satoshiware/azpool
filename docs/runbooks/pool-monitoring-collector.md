# Pool monitoring collector runbook (v0.1)

Support-node telemetry collector for `pool_sv2` monitoring APIs. Stores channel snapshots and accepted-work deltas in Postgres. **Does not create payouts or call wallet RPC.**

See also: [ADR-support-node-pool-telemetry-collector.md](../adr/ADR-support-node-pool-telemetry-collector.md)

## Pool endpoints (WireGuard)

| Pool | Monitoring base URL |
|------|---------------------|
| pool01 | `http://10.10.70.131:9090` |
| pool02 | `http://10.10.70.43:9090` |

### Health check

```bash
curl -sS http://10.10.70.131:9090/api/v1/health
curl -sS http://10.10.70.43:9090/api/v1/health
```

### Clients list

```bash
curl -sS 'http://10.10.70.131:9090/api/v1/clients?offset=0&limit=100'
curl -sS 'http://10.10.70.43:9090/api/v1/clients?offset=0&limit=100'
```

### Channels for a client

Replace `{client_id}` with a value from the clients response:

```bash
curl -sS 'http://10.10.70.131:9090/api/v1/clients/{client_id}/channels?offset=0&limit=100'
```

Channel payloads include `extended_channels` and `standard_channels` with fields such as `channel_id`, `user_identity`, `shares_accepted`, `share_work_sum`, `last_share_sequence_number`, and `blocks_found`.

Observed identities (v0.1 telemetry only, not payout mapping):

- `baveetstudy.miner1`
- `baveetstudy.miner2`
- `baveetstudy.miner3`

These remain **unmapped** (`sc_node_id = NULL`) until an explicit mapping exists in `sc_node_identity_mappings`.

**SC-node-first rules:**

- Support node pays/credits **SC nodes**, not individual users/workers.
- `user_identity` is collector/audit telemetry only — not a payout principal.
- Payout/credit math groups by `sc_node_id` only; do not use `user_identity` in payout reports by default.
- Unknown identities remain unmapped and unpaid.
- SC node operators handle downstream worker/customer splits.
- Native long-term identity should be `az/scnode/<sc_node_id>`.
- Historical rows with `sc_node_id IS NULL` are not auto-backfilled in v0.1.

### Temporary prefix mapping example (telemetry only)

After migration `002_sc_node_identity_mapping.sql`:

```sql
INSERT INTO sc_nodes (id, display_name, status, payout_enabled)
VALUES ('sc-3', 'SC Node 3', 'active', false)
ON CONFLICT (id) DO UPDATE
SET display_name = EXCLUDED.display_name,
    status = EXCLUDED.status,
    updated_at = now();

INSERT INTO sc_node_identity_mappings (sc_node_id, match_type, match_value, status)
VALUES ('sc-3', 'prefix', 'baveetstudy.', 'active')
ON CONFLICT (match_type, match_value) DO UPDATE
SET sc_node_id = EXCLUDED.sc_node_id,
    status = EXCLUDED.status;
```

`payout_enabled` remains `false` for this temporary mapping. New collector runs map `baveetstudy.miner*` to `sc-3`; existing historical NULL rows stay NULL unless a separate explicit backfill is approved later.

### SC-node work summary (payout/credit grouping)

```sql
SELECT
  sc_node_id,
  SUM(accepted_delta) AS accepted_delta_total,
  SUM(work_delta) AS work_delta_total
FROM pool_share_work_deltas
WHERE sc_node_id IS NOT NULL
GROUP BY sc_node_id
ORDER BY work_delta_total DESC;
```

## Pool instance registry (DB-backed)

Pool monitoring targets are loaded from the **`pool_instances`** Postgres table on every one-shot collector run. No service restart is required when adding, updating, or disabling pools.

Active pool criteria:

- `status = 'active'`
- `monitoring_enabled = true`
- `monitoring_base_url` is non-empty

The collector maps `monitoring_base_url` → internal `base_url` for HTTP polling only.

**`DATABASE_URL`** remains required in `/etc/azcoin-super/pool-ledger/collector.env`.

**`POOL_INSTANCES`** env JSON is a **temporary fallback only** when the DB registry is unavailable or returns zero active pools.

### Add or update a pool

```sql
INSERT INTO pool_instances (id, display_name, monitoring_base_url, status, monitoring_enabled)
VALUES ('pool03', 'Pool 03', 'http://10.10.70.99:9090', 'active', true)
ON CONFLICT (id) DO UPDATE
SET display_name = EXCLUDED.display_name,
    monitoring_base_url = EXCLUDED.monitoring_base_url,
    status = EXCLUDED.status,
    monitoring_enabled = EXCLUDED.monitoring_enabled,
    updated_at = now();
```

### Disable a pool

```sql
UPDATE pool_instances
SET status = 'inactive',
    monitoring_enabled = false,
    updated_at = now()
WHERE id = 'pool03';
```

### List pools

```sql
SELECT id, display_name, monitoring_base_url, status, monitoring_enabled, updated_at
FROM pool_instances
ORDER BY id;
```

Known production rows (examples — verify in DB):

| id | monitoring_base_url |
|----|---------------------|
| pool01 | `http://10.10.70.131:9090` |
| pool02 | `http://10.10.70.43:9090` |

## Configuration

Create `/etc/azcoin-super/pool-ledger/collector.env` with placeholders only (no real credentials in Git):

```bash
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DATABASE
# Optional fallback only — omit when pool_instances DB registry is populated:
# POOL_INSTANCES=[{"id":"pool01","base_url":"http://10.10.70.131:9090"},{"id":"pool02","base_url":"http://10.10.70.43:9090"}]
COLLECTOR_REQUEST_TIMEOUT_SECONDS=10
```

Permissions: root-owned, readable by `azledger` service user only.

## Apply migration

From the azpool checkout on the support node:

```bash
cd /opt/azcoin-super/src/azpool/payouts
psql "$DATABASE_URL" -f migrations/001_pool_telemetry_collector.sql
psql "$DATABASE_URL" -f migrations/002_sc_node_identity_mapping.sql
psql "$DATABASE_URL" -f migrations/003_pool_instance_registry.sql
```

Verify tables:

```bash
psql "$DATABASE_URL" -c "\dt pool_*"
psql "$DATABASE_URL" -c "\dt sc_*"
```

## Manual collector run

The collector acquires a Postgres session advisory lock (`pg_try_advisory_lock(20260519, 3001)`) so only one run executes at a time. Overlapping timer and manual runs log `collector already running; skipping this run` and exit successfully without writing collector run rows.

For manual testing, prefer stopping the timer first:

```bash
sudo systemctl stop azcoin-pool-collector.timer
```

```bash
cd /opt/azcoin-super/src/azpool
set -a
source /etc/azcoin-super/pool-ledger/collector.env
set +a
.venv/bin/python -m payouts.collector.app.main
```

## Install and start systemd timer

The collector is a **one-shot service** triggered every 30 seconds by `azcoin-pool-collector.timer`.

```bash
sudo install -m 0644 -o root -g root \
  /opt/azcoin-super/src/azpool/deploy/systemd/azcoin-pool-collector.service \
  /etc/systemd/system/azcoin-pool-collector.service

sudo install -m 0644 -o root -g root \
  /opt/azcoin-super/src/azpool/deploy/systemd/azcoin-pool-collector.timer \
  /etc/systemd/system/azcoin-pool-collector.timer

sudo systemctl daemon-reload
sudo systemctl enable --now azcoin-pool-collector.timer
```

Check timer and recent collector runs:

```bash
systemctl status azcoin-pool-collector.timer --no-pager
journalctl -u azcoin-pool-collector.service -n 50 --no-pager
```

Manual one-shot run (without waiting for timer):

```bash
sudo systemctl start azcoin-pool-collector.service
```

## Rollback

1. Stop the timer and disable it:

```bash
sudo systemctl stop azcoin-pool-collector.timer
sudo systemctl disable azcoin-pool-collector.timer
```

2. Remove units (optional):

```bash
sudo rm -f /etc/systemd/system/azcoin-pool-collector.service \
  /etc/systemd/system/azcoin-pool-collector.timer
sudo systemctl daemon-reload
```

3. Postgres schema rollback (destructive — only if telemetry tables are unused):

```bash
psql "$DATABASE_URL" -c "DROP TABLE IF EXISTS pool_share_work_deltas, pool_channel_snapshots, pool_collector_runs, pool_instances CASCADE;"
```

## Troubleshooting

### Pool unreachable

- Confirm WireGuard tunnel and routing from support node to `10.10.70.131` / `10.10.70.43`.
- Run health `curl` examples above.
- Check `pool_collector_runs.error_message` for the latest failed run.

### Counter reset detected

Monitoring counters may decrease after `pool_sv2` restart. The collector logs `counter reset detected`, increments `resets_detected`, stores the new snapshot, and **does not** insert a negative delta.

### Identity unmapped

`user_identity` values like `baveetstudy.miner1` are stored with `sc_node_id = NULL` until mapped. This is expected in v0.1. Payout logic must group by `sc_node_id` only and must not treat unmapped rows as payable.

Query recent unmapped deltas:

```bash
psql "$DATABASE_URL" -c "SELECT user_identity, COUNT(*) FROM pool_share_work_deltas WHERE sc_node_id IS NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 20;"
```

### No deltas, only snapshots

First observation for each channel produces a snapshot only. Deltas appear on the next run when counters increase.

## Accepted-work formula (v0.1)

```
work_delta = current.share_work_sum - previous.share_work_sum
accepted_delta = current.shares_accepted - previous.shares_accepted
```

Applied only when both current counters are greater than or equal to previous values.
