# Support wallet reward listener — v0 groundwork

Manual registry and read-only visibility for **SC-node payout / reward listener addresses**. No automated sends, no wallet RPC from the support ledger, and **no private keys or seed phrases stored**.

## Ownership model

- Each **SC node** owns and operates the receiving wallet (listener address) on its side.
- The support ledger **only records** the payout/listener address metadata (`sc_node_payout_addresses`) for later payout planning.
- Future support payouts will pay **to** the registered address; they do not custody the SC-node wallet.

## v0 scope (manual only)

| In scope | Out of scope |
|----------|----------------|
| Postgres registry + migrations | Automated `azc send*` or spend |
| Manual SQL insert/activate/revoke | Reward listener daemon |
| Read-only admin JSON (`payout-addresses`) | Credit ledger or batch execution |
| Validation helpers for manual registration | Storing keys, passphrases, or RPC secrets |

Operators register addresses by hand (SQL). Use read-only admin to audit the registry.

## Verify address on the SC node (operator, out of band)

The ledger does not call `azc`. On the **SC node host**, operators may inspect the wallet that will receive rewards (examples only — not invoked by support-node services):

```bash
# On the SC node — confirm the listener/payout address exists in that wallet
azc getaddressesbylabel "payout-listener"
azc validateaddress "<PAYOUT_ADDRESS>"
```

Record the verified address in the support ledger via manual SQL (see [sc-node-payout-addresses.md](../../docs/runbooks/sc-node-payout-addresses.md)).

## Security

- **Never** insert private keys, wallet passphrases, RPC passwords, or bearer tokens into `sc_node_payout_addresses` or collector logs.
- Registry rows are **public payout destinations** only.

## Related artifacts

- Migration: `payouts/migrations/004_sc_node_payout_addresses.sql` (+ `005` for `retired_at` if 004 was applied earlier)
- Read-only CLI: `payouts/scripts/pool_ledger_admin_readonly.py payout-addresses`
- ADR: [ADR-sc-node-payout-address-registry.md](../../docs/adr/ADR-sc-node-payout-address-registry.md)
