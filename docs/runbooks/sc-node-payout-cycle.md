# SC-node payout cycle — operator runbook (v0)

End-to-end **manual** checklist for one SC-node payout cycle on the support node: reward observation → draft credits → payout plan → regtest rehearsal → production preflight → real send → confirm → reconciliation.

**This runbook does not execute anything.** It links to existing CLIs documented in per-PR runbooks. Work as `azledger` (or equivalent ops user) with collector env loaded.

## Architecture reminders

- Support node pays **SC nodes** (`sc_node_id`), not pool `user_identity`.
- `support_wallet_reward_events` is **gross reward history**, not spendable wallet balance.
- Draft credits, approved plans, preflights, and reconciliations are **accounting/audit** — only `execute-real` moves coins (and only via guarded `sendtoaddress`).
- Production source wallet name is **`wallet`** (use explicit `--azc-bin`, not a shell alias, for Python scripts).

## Per-cycle variable template

Copy and fill at the start of each cycle. **Do not reuse values from cycle #1** (see [Known successful cycle #1](#known-successful-cycle-1-history-only)).

| Placeholder | Your value this cycle |
|-------------|------------------------|
| `CREDIT_COVERAGE_START` | |
| `CREDIT_COVERAGE_END` | |
| `CREDIT_RUN_ID` | |
| `PAYOUT_PLAN_ID` | |
| `PRODUCTION_PREFLIGHT_ID` | |
| `PRODUCTION_EXECUTION_ID` | |
| `RECONCILIATION_ID` | |
| `TXID` | |
| `CONFIRM_PHRASE` | `SEND <amount> FROM wallet FOR PLAN <PAYOUT_PLAN_ID>` |
| `IDEMPOTENCY_KEY` | e.g. `production-real-v0-plan-<PAYOUT_PLAN_ID>` |

Deep links:

| Stage | Doc |
|-------|-----|
| Addresses | [sc-node-payout-addresses.md](sc-node-payout-addresses.md) |
| Rewards | [support-wallet-reward-listener.md](support-wallet-reward-listener.md) |
| Credits | [sc-node-credit-ledger.md](sc-node-credit-ledger.md) |
| Plans / approval | [sc-node-payout-plan-review.md](../payouts/docs/sc-node-payout-plan-review.md) |
| Regtest executor | [sc-node-payout-test-executor.md](../payouts/docs/sc-node-payout-test-executor.md) |
| Production preflight | [sc-node-production-payout-preflight.md](../payouts/docs/sc-node-production-payout-preflight.md) |
| Production execute | [sc-node-production-payout-executor.md](../payouts/docs/sc-node-production-payout-executor.md) |
| Chunked production execute | [sc-node-production-payout-chunked-executor.md](../payouts/docs/sc-node-production-payout-chunked-executor.md) |
| Reconciliation | [sc-node-payout-reconciliation.md](../payouts/docs/sc-node-payout-reconciliation.md) |
| Admin JSON | [pool-ledger-admin.md](pool-ledger-admin.md) |

## Common environment

```bash
cd /opt/azcoin-super/src/azpool
set -a
source /etc/azcoin-super/pool-ledger/collector.env
set +a
export PYTHONPATH=/opt/azcoin-super/src/azpool
```

Never echo `DATABASE_URL` or wallet secrets to logs.

---

## 1. Pre-cycle safety checks

Complete **before** any wallet or write operation.

- [ ] **Git / tests:** `main` (or release branch) is current; `pytest payouts/collector/tests` is green on the support node checkout.
- [ ] **Wallet wrappers exist:**
  - `/usr/local/bin/azc-payout` (guarded send path — production `execute-real` only)
  - `/usr/local/bin/azc-payout-readonly` (read-only RPC: `getbalances`, `gettransaction`, etc.)
- [ ] **Sudoers guards:** Non-interactive checks succeed (no unknown password prompt). Example:
  ```bash
  sudo -n /usr/local/bin/azc-payout-readonly -rpcwallet=wallet getbalances
  ```
  If sudo asks for a password or denies the command, **stop** — fix sudoers/wrappers before continuing.
- [ ] **Support wallet balance snapshot:** Record trusted and immature from read-only `getbalances` (via `azc-payout-readonly`). Compare to operator reserve policy (default **50%** of trusted retained; see credit-ledger runbook).
- [ ] **Payout address registry:** Active/default addresses match intent:
  ```bash
  .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py payout-addresses
  ```
- [ ] **Unmapped work:** Review top unmapped identities; resolve or accept exclusion before crediting:
  ```bash
  .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py unmapped-identities --limit 50
  ```
- [ ] **Previous cycle closed:** Last production execution is `confirmed` and reconciliation is `matched` (if a prior cycle exists):
  ```bash
  .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py production-executions
  .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py payout-reconciliations
  ```

---

## 2. Reward listener — import / update

**Reminder:** `support_wallet_reward_events` records historical `generate` / `immature` / `orphan` rows. It is **not** wallet balance.

- [ ] Dry-run scan (no writes):
  ```bash
  .venv/bin/python payouts/scripts/support_wallet_reward_events.py scan \
    --wallet SUPPORT --count 100 --dry-run
  ```
- [ ] After review, persist:
  ```bash
  .venv/bin/python payouts/scripts/support_wallet_reward_events.py scan \
    --wallet SUPPORT --count 100 --write
  ```
- [ ] Verify counts by maturity:
  ```bash
  .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py reward-events
  .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py reward-events --maturity-status mature
  .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py reward-events --maturity-status immature
  ```
- [ ] Note mature vs immature vs orphaned totals in the cycle log. Do **not** treat mature event sum as funds available to send.

See [support-wallet-reward-listener.md](support-wallet-reward-listener.md).

---

## 3. Credit ledger cycle

- [ ] **Preview** with explicit coverage (required before write unless using documented default with eyes open):
  ```bash
  .venv/bin/python payouts/scripts/sc_node_credit_ledger.py preview \
    --wallet SUPPORT \
    --coverage-start CREDIT_COVERAGE_START \
    --coverage-end CREDIT_COVERAGE_END
  ```
  Review: `reward_amount_total`, `mapped_work_total`, `unmapped_work_total`, per-`sc_node_id` draft credits.
- [ ] **Write-draft** only after preview matches intent:
  ```bash
  .venv/bin/python payouts/scripts/sc_node_credit_ledger.py write-draft \
    --wallet SUPPORT \
    --coverage-start CREDIT_COVERAGE_START \
    --coverage-end CREDIT_COVERAGE_END
  ```
  Record `CREDIT_RUN_ID` from output.
- [ ] **Admin verify:**
  ```bash
  .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py \
    credit-run-details --credit-run-id CREDIT_RUN_ID
  ```

See [sc-node-credit-ledger.md](sc-node-credit-ledger.md).

---

## 4. Payout plan generation

- [ ] **Planner preview** (trusted balance snapshot from step 1; default reserve fraction 0.5):
  ```bash
  .venv/bin/python payouts/scripts/sc_node_payout_planner.py preview \
    --credit-run-id CREDIT_RUN_ID \
    --trusted-balance-snapshot <TRUSTED_FROM_GETBALANCES>
  ```
- [ ] **Write draft plan** after preview; record `PAYOUT_PLAN_ID`.
- [ ] **Inspect plan:**
  ```bash
  .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py \
    payout-plan-details --payout-plan-id PAYOUT_PLAN_ID
  ```
- [ ] **Approval + accounting preflight** (exact phrase — no send):
  ```bash
  .venv/bin/python payouts/scripts/sc_node_payout_plan_review.py approve \
    --payout-plan-id PAYOUT_PLAN_ID \
    --confirmation "APPROVE PAYOUT PLAN <PAYOUT_PLAN_ID> NO SEND" \
    --approved-by <operator>
  ```
  Run plan-review preflight commands per [sc-node-payout-plan-review.md](../payouts/docs/sc-node-payout-plan-review.md).

---

## 5. Fake / regtest execution (rehearsal)

No wallet RPC. Proves state machine before production.

- [ ] `preview` → `execute-fake` with **new** `IDEMPOTENCY_KEY` for this cycle
- [ ] `mark-confirmed` on test execution id
- [ ] `details` + admin `payout-test-execution-details`

See [sc-node-payout-test-executor.md](../payouts/docs/sc-node-payout-test-executor.md).

---

## 6. Production preflight

Fresh wallet read; **no sends**.

- [ ] `preview` with `--source-wallet-name wallet` and `--azc-bin /usr/local/bin/azc-payout-readonly`
- [ ] Confirm **50% reserve** math: `planned_amount_total` ≤ `spendable_after_reserve` (unless explicit `--override-reserve` with documented reason)
- [ ] Confirm payout addresses match registry (no drift)
- [ ] `record` with idempotency key; record `PRODUCTION_PREFLIGHT_ID`
- [ ] `details` / admin `production-preflight-details` — `execution_allowed` must be **true**

See [sc-node-production-payout-preflight.md](../payouts/docs/sc-node-production-payout-preflight.md).

---

## 7. Production `execute-real`

**Gate:** All prior sections green. Preflight passed. Fresh `getbalances` still satisfies reserve.

- [ ] `preview` again immediately before send (same plan + preflight ids)
- [ ] Build exact `CONFIRM_PHRASE` from preview (12 decimal places):
  ```text
  SEND <planned_amount_total> FROM wallet FOR PLAN <PAYOUT_PLAN_ID>
  ```
- [ ] **One** `execute-real` per plan/idempotency key — use new `IDEMPOTENCY_KEY` for this cycle
- [ ] **Never** rerun `execute-real` after status `sent` or `confirmed` for the same plan
- [ ] Record `PRODUCTION_EXECUTION_ID` and `TXID` from output
- [ ] Verify tx on source wallet:
  ```bash
  /usr/local/bin/azc-payout-readonly -rpcwallet=wallet gettransaction TXID
  ```

Production send uses `/usr/local/bin/azc-payout` (or documented `--azc-bin`) — **`sendtoaddress` only**.

See [sc-node-production-payout-executor.md](../payouts/docs/sc-node-production-payout-executor.md).

### 7b. Chunked `execute-real` (UTXO fragmentation)

Use when single-send execution is **`refused`** (e.g. `Transaction too large`) and wallet UTXOs are highly fragmented. **Does not modify** the refused execution row.

- [ ] Apply migration `013` if not already applied
- [ ] Chunked `preview` with `--chunk-amount` (plan #2 example: `25` → 9 chunks):
  ```bash
  .venv/bin/python payouts/scripts/sc_node_payout_production_chunked_executor.py preview \
    --payout-plan-id PAYOUT_PLAN_ID \
    --production-preflight-id PRODUCTION_PREFLIGHT_ID \
    --source-wallet-name wallet \
    --chunk-amount 25 \
    --azc-bin /usr/local/bin/azc-payout-readonly
  ```
- [ ] Use exact chunked phrase from preview, e.g. `SEND CHUNKED 223.125000000000 FROM wallet FOR PLAN 2 IN 9 CHUNKS`
- [ ] Chunked `execute-real` with **new** `IDEMPOTENCY_KEY` (not the single-send key)
- [ ] On partial failure: status `partial_sent` — **stop**; do not rerun execute-real; investigate before resuming
- [ ] `details` / admin `production-chunked-execution-details` lists per-chunk txids

See [sc-node-production-payout-chunked-executor.md](../payouts/docs/sc-node-production-payout-chunked-executor.md).

---

## 8. Mark confirmed

Only after on-chain confirmations are visible to the operator.

- [ ] `gettransaction` shows `confirmations >= 1` (prefer higher before closing books)
- [ ] `mark-confirmed --production-execution-id PRODUCTION_EXECUTION_ID`
- [ ] `details` + admin `production-execution-details` — status `confirmed`, `txid` matches `TXID`

---

## 9. Reconciliation

After execution is **confirmed**.

- [ ] Export SC-node **receive-side** JSON manually (no bearer token / no HTTP from reconciliation script). Save path e.g. `/tmp/sc2-wallet-transactions.json`
- [ ] `preview` with `--receiver-transactions-json` and `--azc-bin /usr/local/bin/azc-payout-readonly`
- [ ] `record` — expect `reconciliation_status: matched` when evidence aligns
- [ ] **Idempotent replay:** Re-running `record` with the same evidence should return `idempotent_replay: true`, `recorded: false` (no unique constraint error)
- [ ] `details --reconciliation-id RECONCILIATION_ID`
- [ ] Admin (sanitized by default — no huge `hex`):
  ```bash
  .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py payout-reconciliations
  .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py \
    payout-reconciliation-details --reconciliation-id RECONCILIATION_ID
  ```
- [ ] Use `--include-raw-evidence` only when debugging raw `gettransaction` payload

See [sc-node-payout-reconciliation.md](../payouts/docs/sc-node-payout-reconciliation.md).

---

## 10. Stop conditions

**Stop the cycle** (do not send, do not mark confirmed, do not “fix” by rerunning execute-real) if any of the following occur:

| Condition | Action |
|-----------|--------|
| Reconciliation or preview **mismatch** | Investigate; do not record over conflicting evidence |
| **Unmapped work** material to this payout | Map identities or exclude from credit window |
| **Planned amount** exceeds reserve / trusted balance | Re-plan or wait for balance |
| **Address drift** vs registry | Update registry or plan before production |
| Wallet **wrapper refuses** RPC (sudoers / guard script) | Fix ops path; no raw `azcoin-cli` bypass |
| Source vs receiver **evidence mismatch** | Hold funds reconciliation; verify txid/address/amount |
| **Duplicate execution** for same plan (sent/confirmed) | Do not second `execute-real`; use details/admin |
| **Unexpected DB row counts** (duplicate plans, orphan executions) | SQL review; no ad-hoc deletes |
| Command prompts for **unknown sudo password** | Stop — sudoers misconfigured |
| Any tool attempts **`sendmany`**, raw tx create/sign, **`walletpassphrase`**, key export | Stop — out of v0 contract |

When stopped, document state in the cycle log (ids, statuses, refusal reasons) before resuming.

---

## Known successful cycle #1 (history only)

The following are **historical facts** from the first production SC-node payout. **Do not reuse** ids, keys, phrases, or txids for cycle #2+.

| Field | Cycle #1 value |
|-------|----------------|
| `PAYOUT_PLAN_ID` | 1 |
| `PRODUCTION_EXECUTION_ID` | 1 |
| `RECONCILIATION_ID` | 1 |
| `sc_node_id` | sc-2 |
| Amount | 121.875000000000 AZC |
| Destination | `az1qxgr54ykergmzp7h7fg37lgtc0ccdce355xppqv` |
| `TXID` | `838d4ac398cd3a570f0601389b55334099c14f6484571397f2be35d6df758b00` |
| Production execution status | confirmed |
| Reconciliation status | matched |
| `matched` | true |

---

## Cycle completion checklist

- [ ] `CREDIT_RUN_ID` written and reviewed
- [ ] `PAYOUT_PLAN_ID` approved
- [ ] Regtest execution confirmed (rehearsal)
- [ ] `PRODUCTION_PREFLIGHT_ID` passed
- [ ] `PRODUCTION_EXECUTION_ID` confirmed with `TXID`
- [ ] `RECONCILIATION_ID` matched (or documented mismatch hold)
- [ ] Admin reconciliations list shows expected row
- [ ] Cycle log archived for auditors

---

## Forbidden (entire cycle)

- `sendmany`, `sendrawtransaction`, `createrawtransaction`, `signrawtransaction`, `walletpassphrase`
- Private key / seed / mnemonic handling
- HTTP/bearer calls to SC-node wallets from ledger scripts
- Automatic timers/daemons for payout execution
- Rerunning production `execute-real` after send
- Using cycle #1 constants as templates for new cycles
