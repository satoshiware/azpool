# Pool ledger admin (read-only v0)

Read-only visibility into the support-node pool telemetry ledger: pool instance registry, SC-node metadata, identity mappings, and unmapped worker identities. **Does not execute payouts, create transactions, call wallet RPC, or mutate Postgres data.**

See also: [pool-monitoring-collector.md](pool-monitoring-collector.md), [ADR-pool-ledger-legacy-cleanup-plan.md](../adr/ADR-pool-ledger-legacy-cleanup-plan.md)

## Purpose

Operators and `azledger` need safe JSON views of:

- Which pool instances are registered for monitoring
- Which SC nodes exist and whether payout is enabled
- How `user_identity` values map to `sc_node_id`
- Registered SC-node payout addresses (registry only — not execution)
- Which identities still have unmapped telemetry deltas (unpaid)

Architecture reminder:

- Support node credits/pays **SC nodes** only (`sc_node_id`).
- `user_identity` is telemetry/audit input only — not a payout principal.
- Worker/customer splits happen below the SC node.
- Rows with `sc_node_id IS NULL` are **unmapped and unpaid**.

## Safety boundaries

| Allowed | Not allowed (this tooling) |
|---------|----------------------------|
| `SELECT` via read-only psycopg connection | `INSERT` / `UPDATE` / `DELETE` / DDL |
| JSON to stdout | Wallet RPC, broadcast, transaction creation |
| `unmapped-identities` shows `user_identity` for mapping work | Default payout reports exposing `user_identity` |
| `payout-addresses` lists registry rows | Sending coins or proving on-chain ownership |

**Strong warning:** These commands are admin visibility only. They do **not** move money, create payout batches, or invoke AZCoin Core wallet RPC.

## Prerequisites

Run as `azledger` on the support node with collector env loaded:

```bash
cd /opt/azcoin-super/src/azpool
set -a
source /etc/azcoin-super/pool-ledger/collector.env
set +a
```

`DATABASE_URL` must be set (never print it to stdout).

## Commands

```bash
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py pool-instances
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py sc-nodes
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py mappings
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py payout-addresses
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py unmapped-identities --limit 50
```

### Example: pool-instances

```json
{
  "command": "pool-instances",
  "rows": [
    {
      "id": "pool01",
      "display_name": "Pool 01",
      "monitoring_base_url": "http://10.10.70.131:9090",
      "status": "active",
      "monitoring_enabled": true,
      "created_at": "2026-05-19T12:00:00+00:00",
      "updated_at": "2026-05-19T12:00:00+00:00"
    }
  ]
}
```

### Example: sc-nodes

```json
{
  "command": "sc-nodes",
  "rows": [
    {
      "id": "sc-2",
      "display_name": "SC Node 2",
      "status": "active",
      "payout_enabled": false,
      "created_at": "2026-05-19T12:00:00+00:00",
      "updated_at": "2026-05-19T12:00:00+00:00"
    }
  ]
}
```

### Example: mappings

```json
{
  "command": "mappings",
  "rows": [
    {
      "id": 1,
      "sc_node_id": "sc-2",
      "sc_node_display_name": "SC Node 2",
      "match_type": "prefix",
      "match_value": "baveetstudy.",
      "status": "active",
      "created_at": "2026-05-19T12:00:00+00:00"
    }
  ]
}
```

### Example: payout-addresses

Registry visibility only — **does not send coins** or call wallet RPC. See [sc-node-payout-addresses.md](sc-node-payout-addresses.md).

```json
{
  "command": "payout-addresses",
  "rows": [
    {
      "id": 1,
      "sc_node_id": "sc-2",
      "sc_node_display_name": "SC Node 2",
      "payout_address": "<SC2_PAYOUT_ADDRESS_PLACEHOLDER>",
      "label": "SC Node 2 primary (pending verification)",
      "address_source": "manual",
      "status": "pending_verification",
      "is_default": false,
      "verified_at": null,
      "created_at": "2026-05-19T12:00:00+00:00",
      "updated_at": "2026-05-19T12:00:00+00:00"
    }
  ]
}
```

### Example: unmapped-identities

**Intentionally includes `user_identity`** — this command is an admin mapping aid, not a payout report. Identities listed here have `sc_node_id IS NULL` in `pool_share_work_deltas` and are **unpaid** until mapped.

```json
{
  "command": "unmapped-identities",
  "limit": 50,
  "rows": [
    {
      "user_identity": "baveetstudy.miner1",
      "delta_rows": 10,
      "accepted_delta_total": "30",
      "work_delta_total": "900.0",
      "first_observed_at": "2026-05-19T12:00:00+00:00",
      "last_observed_at": "2026-05-19T13:00:00+00:00"
    }
  ]
}
```

Limit is clamped to 1–500.

## Manual admin SQL (mutating — not run by the script)

Use `psql` only when deliberately changing registry or mappings. Verify impact before applying.

### Add or update pool instance

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

### Disable pool instance

```sql
UPDATE pool_instances
SET status = 'inactive',
    monitoring_enabled = false,
    updated_at = now()
WHERE id = 'pool03';
```

### SC node + prefix mapping (current production example: baveetstudy. → sc-2)

```sql
INSERT INTO sc_nodes (id, display_name, status, payout_enabled)
VALUES ('sc-2', 'SC Node 2', 'active', false)
ON CONFLICT (id) DO UPDATE
SET display_name = EXCLUDED.display_name,
    status = EXCLUDED.status,
    updated_at = now();

INSERT INTO sc_node_identity_mappings (sc_node_id, match_type, match_value, status)
VALUES ('sc-2', 'prefix', 'baveetstudy.', 'active')
ON CONFLICT (match_type, match_value) DO UPDATE
SET sc_node_id = EXCLUDED.sc_node_id,
    status = EXCLUDED.status;
```

`sc-3` is inactive — do not use it as the active `baveetstudy.` mapping example.

### Disable a mapping

```sql
UPDATE sc_node_identity_mappings
SET status = 'inactive'
WHERE match_type = 'prefix' AND match_value = 'baveetstudy.';
```

## Troubleshooting

### `DATABASE_URL is required`

- Source `/etc/azcoin-super/pool-ledger/collector.env` as `azledger`.
- Confirm the file exists and defines `DATABASE_URL` (permissions: root-owned, readable by `azledger`).

### `ModuleNotFoundError: psycopg`

- Use the project venv: `.venv/bin/python` with `PYTHONPATH=/opt/azcoin-super/src/azpool`.
- Install deps: `pip install -r requirements.txt` in the azpool checkout.

### Empty `rows` arrays

- Collector may not have written deltas yet.
- Pool instances may be inactive or `monitoring_enabled = false`.
- Identities may already map to an SC node (check `mappings` and `sc-nodes`).

### Permission denied connecting to Postgres

- Confirm `DATABASE_URL` user can `SELECT` telemetry tables.
- Run as `azledger` if that is the configured DB role.
