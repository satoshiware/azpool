# SC-node payout planner (PR J, no-send)

Generate **draft payout plans** from draft credit runs. **Does not send coins** or build/sign/broadcast transactions.

See also: [ADR-sc-node-payout-planner.md](../../docs/adr/ADR-sc-node-payout-planner.md), [sc-node-credit-ledger.md](../../docs/runbooks/sc-node-credit-ledger.md)

## Safety

- Payout plans are **proposals only**, not spend authorization.
- `support_wallet_reward_events` is **gross reward-event history**, not spendable wallet balance.
- Supply **`--trusted-balance-snapshot`** from current trusted wallet balance (may be lower than historical mature reward totals if funds were moved manually).
- Default **`--reserve-fraction 0.50`** keeps 50% reserved for manual/future testing — not wallet-enforced in PR J (no `azc`).

## Apply migration

```bash
cd /opt/azcoin-super/src/azpool/payouts
set -a && source /etc/azcoin-super/pool-ledger/collector.env && set +a
psql "$DATABASE_URL" -f migrations/007_sc_node_payout_plans.sql
```

## Preview (credit_run_id=1 example)

Replace `TRUSTED_BALANCE` with the operator's current trusted wallet balance (not gross reward-event sum).

```bash
cd /opt/azcoin-super/src/azpool
set -a && source /etc/azcoin-super/pool-ledger/collector.env && set +a
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/sc_node_payout_planner.py preview \
  --credit-run-id 1 \
  --wallet wallet \
  --trusted-balance-snapshot TRUSTED_BALANCE \
  --reserve-fraction 0.50
```

Refuses when `planned_amount_total` (e.g. 121.875 for sc-2) exceeds `max_spendable_amount`.

## Write draft

```bash
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/sc_node_payout_planner.py write-draft \
  --credit-run-id 1 \
  --wallet wallet \
  --trusted-balance-snapshot TRUSTED_BALANCE \
  --reserve-fraction 0.50 \
  --notes "manual plan for credit run 1"
```

## Admin (read-only)

```bash
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/pool_ledger_admin_readonly.py payout-plans

PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/pool_ledger_admin_readonly.py payout-plan-details --payout-plan-id 1
```

## Next PR

Guarded payout execution (separate approval) must re-check trusted balance and reserves before sending.
