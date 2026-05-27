# SC-node receiver evidence export (PR V)

Read-only export of **SC-node listener wallet** receive-side transaction evidence for payout reconciliation. Uses an explicit wallet name, an operator-configured allowlist, and only allowlisted RPC methods (`listtransactions`, optional `gettransaction`).

## Non-goals

- No sends, signing, passphrase unlock, key import/export, or raw transaction creation.
- No HTTP/bearer calls to SC-node APIs from this script.
- No writes to Postgres or mutation of reconciliation/execution rows.
- No use of production source wallet (`wallet`) or support wallet (`SUPPORT`).

## Prerequisites

- `/usr/local/bin/azc-payout-readonly` installed on the SC node (or pass `--azc-bin`).
- Wallet allowlist configured on the operator shell:

```bash
export PAYOUT_RECEIVER_EVIDENCE_WALLET_ALLOWLIST=SC2TESTWALLETLISTENER
```

Add comma-separated listener wallet names for additional SC nodes. **`wallet` and `SUPPORT` are always denied.**

## Script

`payouts/scripts/sc_node_receiver_evidence_export.py`

### Export receive-side evidence (recommended)

```bash
cd /opt/azcoin-super/src/azpool
export PYTHONPATH=/opt/azcoin-super/src/azpool
export PAYOUT_RECEIVER_EVIDENCE_WALLET_ALLOWLIST=SC2TESTWALLETLISTENER

.venv/bin/python payouts/scripts/sc_node_receiver_evidence_export.py \
  --wallet SC2TESTWALLETLISTENER \
  --count 500 \
  --receive-only \
  --azc-bin /usr/local/bin/azc-payout-readonly \
  --output /tmp/sc2-wallet-transactions.json
```

Output JSON includes:

- `export_kind`: `sc_node_receiver_evidence`
- `wallet`: explicit listener wallet name
- `transactions`: sanitized `listtransactions` rows (secret-like fields omitted)
- optional `txid_details` when `--txid` is repeated

The file is accepted by reconciliation scripts as `--receiver-transactions-json` (top-level list **or** object with `transactions` array).

### Stdout export

Omit `--output` to print JSON to stdout (pipe to a file if needed).

## Safety rules

| Rule | Enforcement |
|------|-------------|
| Explicit wallet | Required `--wallet` |
| Allowlist | `PAYOUT_RECEIVER_EVIDENCE_WALLET_ALLOWLIST` |
| Deny production wallets | `wallet`, `SUPPORT` rejected |
| RPC allowlist | `listtransactions`, `gettransaction` only |
| Dangerous RPC keywords | Rejected in argv and `--azc-bin` |
| Secrets in output | `hex`, script/key material stripped |
| Subprocess | `shell=False`, explicit argv |

## Do not

- Call raw `azcoin-cli` against listener wallets without the read-only wrapper.
- Export from `wallet` (support-node source) and pass it as receiver evidence.
- Send coins manually on SC nodes to “fix” reconciliation mismatches.

See [sc-node-payout-cycle.md](../../docs/runbooks/sc-node-payout-cycle.md) for cycle closeout usage.

## Manual validation checklist

- [ ] `PAYOUT_RECEIVER_EVIDENCE_WALLET_ALLOWLIST` includes only SC-node listener wallets.
- [ ] `--wallet wallet` is refused.
- [ ] `--azc-bin` pointing at `azc-payout` (send-capable) is not used for export.
- [ ] Output file parses as JSON; `transactions` is an array.
- [ ] Expected chunk txids appear in `--receive-only` export before reconciliation `preview`.
- [ ] Reconciliation `preview` returns `matched: true` with exported file.
