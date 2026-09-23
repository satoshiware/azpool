# Register an SC-node (ledger)

Operator script to upsert an SC-node into the azpool ledger when a new SC comes online on sn01: `sc_nodes`, identity mapping, and payout address. **Does not send coins, call `azc`, or enable payout automation** (unless you pass `--payout-enabled`).

See also: [sc-node-payout-addresses.md](sc-node-payout-addresses.md), [pool-ledger-admin.md](pool-ledger-admin.md)

## Script location

| Checkout | Path |
|----------|------|
| Git (preferred edit) | `/home/benc/azpool/payouts/scripts/register_sc_node.sh` |
| Deployed on sn01 | `/opt/azcoin-super/src/azpool/payouts/scripts/register_sc_node.sh` |
| Optional symlink | `/home/benc/bin/register-sc-node.sh` |

If ProtectHome blocks `/home/benc` for `azledger`, copy the script (and keep migrations) under `/opt` before running:

```bash
sudo install -m 0755 \
  /home/benc/azpool/payouts/scripts/register_sc_node.sh \
  /opt/azcoin-super/src/azpool/payouts/scripts/register_sc_node.sh
```

## Prerequisites

- Root (`sudo`)
- Collector env present: `/etc/azcoin-super/pool-ledger/collector.env` (sourced as `azledger`; never printed)
- Migrations `001`–`003` already applied (`sc_nodes` exists). Migration `004` is applied automatically only if `sc_node_payout_addresses` is missing.

## Usage

```bash
sudo bash /opt/azcoin-super/src/azpool/payouts/scripts/register_sc_node.sh \
  --sc-node-id <id> \
  --payout-address az1q... \
  --match-value 'azc-XXXX.' \
  [--display-name <name>] \
  [--match-type prefix] \
  [--activate-address|--no-activate-address] \
  [--payout-enabled] \
  [--label '<id> primary']
```

| Flag | Default | Notes |
|------|---------|--------|
| `--sc-node-id` | required | Ledger id |
| `--display-name` | same as id | Human label |
| `--payout-address` | required | Operator `az1…` address (`az1q…` or `az1p…`) |
| `--match-type` | `prefix` | `prefix` \| `exact` \| `glob` |
| `--match-value` | required | For prefix: include trailing `.` (e.g. `azc-Y69004JG90205.`) so `…miner1` matches |
| `--activate-address` | **true** | Pending insert then activate + set active default (operator-provided address path) |
| `--no-activate-address` | — | Leave `pending_verification`, not default |
| `--payout-enabled` | off | Opt-in; default leaves `payout_enabled` false / unchanged on re-run |

Idempotent: safe to re-run. Re-registration does **not** clear `payout_enabled` if another process already enabled it.

## Example (next SC-node)

```bash
sudo bash /opt/azcoin-super/src/azpool/payouts/scripts/register_sc_node.sh \
  --sc-node-id '<NEW_SC_ID>' \
  --display-name '<NEW_SC_ID>' \
  --payout-address 'az1q...' \
  --match-type prefix \
  --match-value 'azc-<NODE_PREFIX>.'
```

Known pattern from pool: miner identities look like `azc-Y69004JG90205.miner1` → register with `--match-value 'azc-Y69004JG90205.'`.

## Several SC-nodes on one pool account

The collector resolves each observed `user_identity` to one SC-node in this order (`payouts/collector/app/identity.py`):

1. `exact` — whole identity must equal `match_value`
2. `prefix` — longest matching `match_value` wins
3. `glob`

Pool identities carry a miner suffix, so an SC-node that shares an account with another node (for example `azc-Y69004JG90205.Frontier.miner4` alongside `azc-Y69004JG90205.circle-01.miner851`) must be registered with a **prefix that includes its sub-name and trailing dot**:

```bash
sudo bash /opt/azcoin-super/src/azpool/payouts/scripts/register_sc_node.sh \
  --sc-node-id frontier \
  --display-name 'Frontier' \
  --payout-address 'az1p...' \
  --match-type prefix \
  --match-value 'azc-Y69004JG90205.Frontier.'
```

Do **not** use `--match-type exact --match-value 'azc-Y69004JG90205.Frontier'`: it never matches `…Frontier.miner4`, so that work falls through to the account-wide prefix (`azc-Y69004JG90205.`) and is credited and paid to the other node, with no unmapped work to flag it. The script now warns when `exact` is used.

Mappings apply from the next collector run (every 30 s). Deltas already stored keep their `sc_node_id`; re-attributing past work needs a separate ledger correction. After registering, confirm new deltas land on the intended node:

```bash
sudo -u azledger bash -c 'set -a; source /etc/azcoin-super/pool-ledger/collector.env; set +a; psql "$DATABASE_URL" -c "
SELECT user_identity, sc_node_id, count(*), max(observed_to)
FROM pool_share_work_deltas
WHERE observed_to > now() - interval '\''10 minutes'\''
GROUP BY 1, 2 ORDER BY 1, 2;"'
```

## What it does

1. Checks registry tables; applies `004_sc_node_payout_addresses.sql` only if needed
2. Upserts `sc_nodes`, `sc_node_identity_mappings`, `sc_node_payout_addresses`
3. Optionally activates the address as the sole active default for that node
4. Prints verification `SELECT`s and optional `pool_ledger_admin_readonly.py` JSON (no secrets)

## Safety

- Registry only — no wallet RPC / broadcast
- Ownership of the address must still be verified out-of-band before treating it as production-ready (default path activates for operator-supplied addresses; use `--no-activate-address` if verification is still pending)
- Do **not** pass `--payout-enabled` until credit/payout automation for that node is intentionally turned on
