# SC-node payout addresses (registry v0.1)

Register and review **payout / reward listener destination addresses per SC node**. **Does not send coins or invoke wallet RPC from the support ledger.**

See also: [ADR-sc-node-payout-address-registry.md](../adr/ADR-sc-node-payout-address-registry.md), [pool-ledger-admin.md](pool-ledger-admin.md), [support-wallet-reward-listener-v0.md](../../payouts/docs/support-wallet-reward-listener-v0.md)

## Purpose

The support node pays/credits **SC nodes only** (`sc_node_id`). Each **SC node owns** the receiving/listener wallet on its host; the support ledger **only stores** the payout address and will pay to it in a later phase.

**v0 is manual only** — operators insert and activate rows via SQL; no automated payout or listener daemon.

**No secrets in the registry** — do not store private keys, seed phrases, wallet passphrases, or RPC credentials in `sc_node_payout_addresses`.

Before payout automation, each SC node needs one or more registered payout addresses with optional default selection.

This runbook covers:

- Applying migration `004_sc_node_payout_addresses.sql`
- Manual SQL to add, activate, or revoke addresses
- Read-only admin JSON via `pool_ledger_admin_readonly.py payout-addresses`

## Safety boundaries

| Allowed | Not allowed (this PR) |
|---------|------------------------|
| Registry table + manual SQL | Support-ledger wallet RPC or broadcast |
| Read-only admin JSON | Automatic payout execution |
| Status workflow | On-chain address proof in SQL |

**Warnings:**

- Inserting a row **does not send coins**.
- Support-node services **do not call `azc`**; operators may use `azc` on the SC node to verify ownership out of band (see [support-wallet-reward-listener-v0.md](../../payouts/docs/support-wallet-reward-listener-v0.md)).
- Registry presence **does not prove wallet ownership**.
- Verify address ownership **separately** before `status = 'active'`.

## Apply migration

From the azpool checkout on the support node:

```bash
cd /opt/azcoin-super/src/azpool/payouts
psql "$DATABASE_URL" -f migrations/004_sc_node_payout_addresses.sql
psql "$DATABASE_URL" -f migrations/005_sc_node_payout_addresses_retired_at.sql
```

Verify:

```bash
psql "$DATABASE_URL" -c "\d sc_node_payout_addresses"
```

## Manual SQL: insert pending address

Example for **sc-2** (current `baveetstudy.` mapping target). Use a **placeholder** until a real verified address is approved:

```sql
INSERT INTO sc_node_payout_addresses (
  sc_node_id,
  payout_address,
  label,
  address_source,
  status,
  is_default
)
VALUES (
  'sc-2',
  '<SC2_PAYOUT_ADDRESS_PLACEHOLDER>',
  'SC Node 2 primary (pending verification)',
  'manual',
  'pending_verification',
  false
);
```

## Manual SQL: activate and set default (after verification)

Only after **out-of-band ownership verification**:

```sql
UPDATE sc_node_payout_addresses
SET status = 'inactive',
    is_default = false,
    updated_at = now()
WHERE sc_node_id = 'sc-2'
  AND is_default = true
  AND status = 'active';

UPDATE sc_node_payout_addresses
SET status = 'active',
    is_default = true,
    verified_at = now(),
    updated_at = now()
WHERE sc_node_id = 'sc-2'
  AND payout_address = '<SC2_PAYOUT_ADDRESS_PLACEHOLDER>';
```

Partial unique index allows only one active default per SC node.

## Manual SQL: revoke or disable

```sql
UPDATE sc_node_payout_addresses
SET status = 'revoked',
    is_default = false,
    retired_at = now(),
    updated_at = now()
WHERE sc_node_id = 'sc-2'
  AND payout_address = '<SC2_PAYOUT_ADDRESS_PLACEHOLDER>';
```

Or temporarily disable:

```sql
UPDATE sc_node_payout_addresses
SET status = 'inactive',
    is_default = false,
    updated_at = now()
WHERE id = 1;
```

## Read-only admin command

```bash
cd /opt/azcoin-super/src/azpool
set -a
source /etc/azcoin-super/pool-ledger/collector.env
set +a
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py payout-addresses
```

Example output:

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
      "retired_at": null,
      "created_at": "2026-05-19T12:00:00+00:00",
      "updated_at": "2026-05-19T12:00:00+00:00"
    }
  ]
}
```

## Status reference

| Status | Meaning |
|--------|---------|
| `pending_verification` | Recorded; not verified for payout use |
| `active` | Verified; may be used as payout target |
| `inactive` | Temporarily disabled |
| `revoked` | Permanently retired (audit retained) |

## Troubleshooting

- **Unique violation on `payout_address`** — address already registered (global unique).
- **Unique violation on active default** — clear existing active default for that SC node first.
- **FK violation on `sc_node_id`** — ensure row exists in `sc_nodes`.
