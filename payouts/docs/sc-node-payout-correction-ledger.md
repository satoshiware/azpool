# SC-node payout correction ledger (PR AB)

Audited **offset/correction** records that reduce payable payout plan amounts. **Does not send coins** or alter wallet balances directly.

See also: [sc-node-payout-planner.md](sc-node-payout-planner.md), [sc-node-payout-cycle.md](../docs/runbooks/sc-node-payout-cycle.md)

## Why corrections exist

Payout plans must not rely on manual amount fudges. When gross credits overstate net payable (for example a prior boundary overpayment or stale reward listener catch-up), operators create an explicit draft correction and attach it during payout plan creation.

## Cycle #3 example

| Item | Amount (AZC) |
|------|----------------|
| Catch-up gross credit (33 stale mature rewards) | 61.875 |
| Offset for `reward_event_id=2282` boundary overpayment | 1.875 |
| **Net payable** | **60.000** |

Create a draft correction for the 1.875 AZC offset, then pass `--payout-correction-id` to the payout planner preview/write-draft.

## Apply migration

```bash
cd /opt/azcoin-super/src/azpool/payouts
set -a && source /etc/azcoin-super/pool-ledger/collector.env && set +a
psql "$DATABASE_URL" -f migrations/016_sc_node_payout_corrections.sql
```

## Create draft correction

```bash
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/sc_node_payout_correction.py create-draft \
  --sc-node-id sc-2 \
  --wallet wallet \
  --amount 1.875000000000 \
  --reason-code boundary_overpayment \
  --related-credit-run-id 5 \
  --related-reward-event-id 2282 \
  --notes "Cycle #3 offset for pre-PR-AA boundary double-count"
```

## List / details / cancel

```bash
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/sc_node_payout_correction.py list

PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/sc_node_payout_correction.py details --correction-id 1

PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/sc_node_payout_correction.py cancel --correction-id 1
```

## Payout planner with correction

```bash
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/sc_node_payout_planner.py preview \
  --credit-run-id 5 \
  --wallet wallet \
  --trusted-balance-snapshot TRUSTED_BALANCE \
  --payout-correction-id 1
```

Preview rows include `gross_credit_amount`, `correction_amount`, and `net_payout_amount`. Reserve checks use **net** `planned_amount_total`.

`write-draft` applies the correction atomically (status `draft` → `applied`, links `related_payout_plan_id`).

## Safety

- Corrections are accounting offsets only — not spend authorization.
- No wallet RPC, no sends, no manual plan amount edits.
- A correction can be applied **once**; already-applied or cancelled corrections are refused.
- Correction must match plan `wallet_name`, target `sc_node_id`, and optional `related_credit_run_id`.
