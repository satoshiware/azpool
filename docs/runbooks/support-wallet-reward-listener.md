# Support-wallet reward listener (read-only v0)

Observe **support-wallet** `generate` / `immature` / `orphan` transactions via read-only `azc listtransactions`, optionally persist normalized rows to Postgres, and list them with read-only admin JSON.

**This only observes support-wallet transactions. It does not send coins, create/sign/broadcast transactions, or generate payout plans.**

See also: [ADR-support-wallet-reward-listener.md](../adr/ADR-support-wallet-reward-listener.md), [pool-ledger-admin.md](pool-ledger-admin.md)

## Ownership and boundaries

| Topic | v0 behavior |
|-------|-------------|
| Observation | Read-only `azc listtransactions` on the support wallet |
| Storage | `support_wallet_reward_events` — destination metadata and wallet row snapshot |
| Secrets | **Never** store private keys, seed phrases, wallet passphrases, or RPC tokens |
| Payouts | **No** payout execution, credit ledger, or payout plan generation |
| Collector | Unchanged — no timer/systemd changes in this PR |

**Next PRs:** SC-node credit ledger (PR I), payout plan generator (PR J), guarded dry-run wallet execution (PR K).

## Apply migration

```bash
cd /opt/azcoin-super/src/azpool/payouts
set -a
source /etc/azcoin-super/pool-ledger/collector.env
set +a
psql "$DATABASE_URL" -f migrations/005_support_wallet_reward_events.sql
psql "$DATABASE_URL" -c "\d support_wallet_reward_events"
```

## Scan wallet (dry-run default)

Dry-run prints normalized reward events and **writes nothing**:

```bash
cd /opt/azcoin-super/src/azpool
set -a
source /etc/azcoin-super/pool-ledger/collector.env
set +a
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/support_wallet_reward_events.py scan \
  --wallet SUPPORT --count 100 --maturity-confirmations 100 --dry-run
```

(`--dry-run` is optional — scan is dry-run unless `--write` is passed.)

Persist after review:

```bash
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/support_wallet_reward_events.py scan \
  --wallet SUPPORT --count 100 --maturity-confirmations 100 --write
```

## Print stored events

```bash
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/support_wallet_reward_events.py print

PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/support_wallet_reward_events.py print --maturity-status mature

PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/support_wallet_reward_events.py print --include-raw
```

Default output **omits** `raw_wallet_event`. Use `--include-raw` only when debugging normalization.

## Read-only admin (DB only, no azc)

```bash
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/pool_ledger_admin_readonly.py reward-events

PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/pool_ledger_admin_readonly.py reward-events --maturity-status immature
```

Admin output never includes `raw_wallet_event`.

## Operator verification on support node (optional)

The scan script does not log RPC passwords. If you need to confirm wallet RPC health before scanning:

```bash
azc -rpcwallet=SUPPORT getwalletinfo
```

## Troubleshooting

- **`DATABASE_URL is required`** — source `collector.env` before `print`, `--write`, or admin commands.
- **Empty `events` in dry-run** — recent rows may be `receive`/`send` (ignored) or below the `--count` window.
- **Unique violation** — same `(wallet_name, txid, vout)` already stored; upsert should update — check migration applied.
- **Forbidden keyword error in code** — implementation must stay read-only; report if a false positive blocks startup.

## Safety reminder

Do **not** use `sendtoaddress`, `sendmany`, `sendrawtransaction`, `createrawtransaction`, `signrawtransaction`, or `walletpassphrase` in this tooling. Those operations are explicitly out of scope for PR H.
