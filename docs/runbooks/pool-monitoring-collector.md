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

These remain **unmapped** (`sc_node_id = NULL`) until an explicit mapping rule or table exists.

## Configuration

Create `/etc/azcoin-super/pool-ledger/collector.env` with placeholders only (no real credentials in Git):

```bash
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DATABASE
POOL_INSTANCES=[{"id":"pool01","base_url":"http://10.10.70.131:9090"},{"id":"pool02","base_url":"http://10.10.70.43:9090"}]
COLLECTOR_REQUEST_TIMEOUT_SECONDS=10
```

Permissions: root-owned, readable by `azledger` service user only.

## Apply migration

From the azpool checkout on the support node:

```bash
cd /opt/azcoin-super/src/azpool/payouts
psql "$DATABASE_URL" -f migrations/001_pool_telemetry_collector.sql
```

Verify tables:

```bash
psql "$DATABASE_URL" -c "\dt pool_*"
```

## Manual collector run

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

`user_identity` values like `baveetstudy.miner1` are stored with `sc_node_id = NULL`. This is expected in v0.1. Future payout logic must not treat unmapped rows as payable SC-node identities until explicitly mapped.

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
