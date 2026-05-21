# SC-node payout plan approval and preflight (PR K)

Approve or cancel draft payout plans and run **no-send preflight** on approved plans. **Does not send coins** or build/sign/broadcast transactions.

See also: [ADR-sc-node-payout-plan-approval-preflight.md](../../docs/adr/ADR-sc-node-payout-plan-approval-preflight.md), [sc-node-payout-planner.md](sc-node-payout-planner.md)

## Safety

- **Approval is not execution** and **preflight is not execution**.
- Payout plans remain **proposals only**, not spend authorization.
- `support_wallet_reward_events` is gross history, not spendable wallet balance.
- Preflight uses operator-supplied **current** trusted balance (may differ from plan snapshot if funds were moved).

## Apply migration

```bash
cd /opt/azcoin-super/src/azpool/payouts
set -a && source /etc/azcoin-super/pool-ledger/collector.env && set +a
psql "$DATABASE_URL" -f migrations/008_sc_node_payout_plan_approval_preflight.sql
```

## Approve payout_plan_id=1

Exact confirmation phrase (required):

```text
APPROVE PAYOUT PLAN 1 NO SEND
```

```bash
cd /opt/azcoin-super/src/azpool
set -a && source /etc/azcoin-super/pool-ledger/collector.env && set +a
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/sc_node_payout_plan_review.py approve \
  --payout-plan-id 1 \
  --approved-by azledger \
  --confirmation "APPROVE PAYOUT PLAN 1 NO SEND" \
  --approval-note "reviewed plan 1"
```

## Preflight payout_plan_id=1

Use current trusted wallet balance (not gross reward-event totals). Plan 1 snapshot was `660.624813450000` with 50% reserve; planned `121.875` fits if current balance is similar.

```bash
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/sc_node_payout_plan_review.py preflight \
  --payout-plan-id 1 \
  --trusted-balance-current 660.624813450000
```

Optional reserve override:

```bash
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/sc_node_payout_plan_review.py preflight \
  --payout-plan-id 1 \
  --trusted-balance-current 660.624813450000 \
  --reserve-fraction-current 0.50
```

## Cancel (draft or approved)

```bash
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/sc_node_payout_plan_review.py cancel \
  --payout-plan-id 1 \
  --cancelled-by azledger \
  --reason "hold payout plan"
```

## Admin (read-only)

```bash
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/pool_ledger_admin_readonly.py payout-plan-details --payout-plan-id 1
```

Includes `approved_at`, `preflight_status`, `cancellation_note`, and related metadata.

## Next PR

Guarded payout execution (separate approval) must re-check trusted balance and reserve rules before any send.
