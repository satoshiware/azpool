# Mirror sc-2 support-node software layout

Phased installer playbook to reproduce an **AZPool support node** (azcoind + Template Provider + pool-ledger timers) without local Bitcoin Core or local pool-sv2.

Script: [`deploy/scripts/mirror-sc2-support-node.sh`](../../deploy/scripts/mirror-sc2-support-node.sh)

## Decisions (locked)

| Topic | Decision |
|-------|----------|
| Bitcoin Core / `bitcoind` | **Not installed** by this playbook |
| Local `pool-sv2` | **Not built or installed** — remote pool instances handle SV2 |
| `azcoind` | Via `satoshiware/sc-node` `azcoin-install.sh` |
| Templar + ledger | Via azpool `deploy/scripts/*-support-node.sh` and pool-ledger installers |
| Payout scheduler timer | **Disabled** by default (service may be installed for manual use) |

## Canonical azpool trees

| Role | Path | Pin |
|------|------|-----|
| **Production runtime** | `/opt/azcoin-super/src/azpool` | `origin/main` (validated on sc-2 at `344d060`) |
| **Git / PR clone** | `~/repos/azpool` | Track `origin/main`; deploy into `/opt` after merge |

**Verdict:** `/opt/azcoin-super/src/azpool` is the **canonical production checkout**. Evidence:

- All ledger systemd units use `WorkingDirectory=/opt/azcoin-super/src/azpool` and `/opt/.../.venv/bin/python`.
- Template Provider binary installs to `/opt/azcoin-super/bin/azcoin-template-provider` ([ADR-azpool-templar-canonical-source](../adr/ADR-azpool-templar-canonical-source.md)).
- Runbooks and payout docs consistently `cd /opt/azcoin-super/src/azpool`.
- On sc-2, `/opt` was clean and matched `origin/main` at `344d060`; `~/repos/azpool` was only a stale working clone (behind remote).

`~/repos/azpool` is the **PR source**, not the live WorkingDirectory. Keep it ff-synced to `origin/main`, then update `/opt` deliberately after review.

## What gets enabled

| Unit | Default |
|------|---------|
| `azcoind.service` | enable (start only if `START_SERVICES=1`) |
| `azcoin-template-provider.service` | enable |
| `azcoin-pool-collector.timer` | enable |
| `azcoin-sc-node-fresh-cycle-automation.timer` | enable |
| `azcoin-support-wallet-reward-scan.timer` | enable |
| `azcoin-sc-node-payout-scheduler.timer` | **disable** unless `ENABLE_PAYOUT_SCHEDULER_TIMER=1` |
| `bitcoind` / `pool-sv2` | omitted |

## Dry-run / run

From a synced checkout (prefer the production tree once seeded):

```bash
cd /opt/azcoin-super/src/azpool   # or ~/repos/azpool before first seed
chmod +x deploy/scripts/mirror-sc2-support-node.sh

# Print planned actions only
DRY_RUN=1 ./deploy/scripts/mirror-sc2-support-node.sh --phase all

# Read-only status (no root required for systemctl queries)
./deploy/scripts/mirror-sc2-support-node.sh --phase status

# Install (does not start services by default)
sudo START_SERVICES=0 ./deploy/scripts/mirror-sc2-support-node.sh --phase all

# Single phase
sudo ./deploy/scripts/mirror-sc2-support-node.sh --phase templar
```

After configs exist under `/etc/azcoin-super/`, start selectively:

```bash
sudo START_SERVICES=1 ./deploy/scripts/mirror-sc2-support-node.sh --phase timers
# or: sudo systemctl start azcoind azcoin-template-provider azcoin-pool-collector.timer ...
```

## Portability note: sc-node `AZCOIN_BIN_PARENT`

`sc-node/azcoin-install.sh` currently hardcodes:

```text
AZCOIN_BIN_PARENT="/home/benc/repos/sc-node"
```

The playbook does **not** rewrite sc-node. If `SC_NODE_REPO` differs, it attempts to symlink the hardcoded path to the real clone so the tarball download/find still works. On non-`benc` hosts, either:

1. Keep the clone at `/home/benc/repos/sc-node`, or
2. Let the playbook create the symlink, or
3. Patch `AZCOIN_BIN_PARENT` upstream in sc-node in a separate PR.

## Related runbooks

- [template-provider-build-install.md](template-provider-build-install.md)
- [pool-monitoring-collector.md](pool-monitoring-collector.md)
- [support-wallet-reward-listener.md](support-wallet-reward-listener.md)
- [sc-node-current-state-discovery.md](sc-node-current-state-discovery.md)
- [sc-node-payout-cycle.md](sc-node-payout-cycle.md)
