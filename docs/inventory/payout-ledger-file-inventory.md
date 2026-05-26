# Payout ledger file inventory

**Branch:** `feature/payout-app-dependency-audit-v0` (PR F audit updates)
**Purpose:** Classify `payouts/` and related docs for safe legacy cleanup. Standalone scripts quarantined under `payouts/legacy/scripts/` (PR E). **`payouts/app/*` audited and marked legacy-candidate but not quarantined** (PR F) — see [payout-app-dependency-audit.md](payout-app-dependency-audit.md).

Related: [ADR-pool-ledger-legacy-cleanup-plan.md](../adr/ADR-pool-ledger-legacy-cleanup-plan.md)  
Helper scripts: `payouts/scripts/inventory_payout_ledger_files.py`, `payouts/scripts/audit_payout_app_dependencies.py` (read-only JSON)

## Executive summary

The support-node **active path** is pool telemetry collection, DB-backed pool registry, SC-node identity mapping, and read-only reporting/admin scripts under `payouts/collector/` plus `payouts/scripts/sc_node_work_summary.py` and `payouts/scripts/pool_ledger_admin_readonly.py`. Systemd on the support node runs only `payouts.collector.app.main` (`deploy/systemd/azcoin-pool-collector.service`).

A large **legacy-candidate** tree remains from the original worker/user-level payout service: FastAPI app (`payouts/app/`), SQLite/Postgres settlement and sender modules, and extensive `payouts/tests/`. **PR E quarantined** three standalone scripts to `payouts/legacy/scripts/` (demo interval runner, Postgres shadow backfill, SQLite settlement mapping backfill) — moved, not deleted. These are **not** imported by the collector or read-only admin scripts; **no** `deploy/` or systemd references to the old `payouts/scripts/` paths (preflight grep, 2026-05-19).

**Historical Alembic** and **telemetry migrations** (`payouts/migrations/001–003`) are **DO-NOT-REMOVE-YET** until a deliberate DB retirement plan exists.

Future payouts must be **SC-node-level** (`sc_node_id`), not worker `user_identity`. Historical unmapped `user_identity` rows stay unpaid unless a separate approved backfill occurs.

---

## Classification matrix

Columns: **Path** | **Classification** | **Reason** | **Evidence** | **Proposed action** | **Removal risk** | **Next verification**

### ACTIVE — current support-node path

| Path | Classification | Reason | Evidence | Proposed action | Removal risk | Next verification |
|------|----------------|--------|----------|-----------------|--------------|-------------------|
| `payouts/collector/app/main.py` | ACTIVE | Collector orchestration | systemd `ExecStart=...payouts.collector.app.main` | Keep | High if removed | Timer + manual run |
| `payouts/collector/app/db.py` | ACTIVE | Postgres telemetry writes + advisory lock | Imported by `main.py` | Keep | High | Collector integration tests |
| `payouts/collector/app/config.py` | ACTIVE | `DATABASE_URL`, pool registry resolution | Imported by `main.py` | Keep | High | Config unit tests |
| `payouts/collector/app/identity.py` | ACTIVE | `user_identity` → `sc_node_id` mapping | Imported by `main.py` | Keep | High | Identity tests |
| `payouts/collector/app/pool_client.py` | ACTIVE | pool_sv2 monitoring HTTP client | Imported by `main.py` | Keep | High | Pool client tests |
| `payouts/collector/app/delta.py` | ACTIVE | Accepted-work delta math | Imported by `main.py` | Keep | High | Delta tests |
| `payouts/collector/app/sc_node_summary.py` | ACTIVE | Read-only SC-node summary SQL | Used by `sc_node_work_summary.py` | Keep | Medium | Summary tests |
| `payouts/collector/app/admin_readonly.py` | ACTIVE | Read-only admin SQL builders | Used by `pool_ledger_admin_readonly.py` | Keep | Medium | Admin readonly tests |
| `payouts/collector/app/payout_addresses.py` | ACTIVE | SC-node payout address registry SQL/validation (PR G) | Used by admin `payout-addresses` | Keep | Medium | Payout address tests |
| `payouts/collector/app/reward_events.py` | ACTIVE | Support-wallet reward event normalization/SQL (PR H) | Used by scan + admin `reward-events` | Keep | Medium | Reward event tests |
| `payouts/collector/app/sc_node_credit_ledger.py` | ACTIVE | SC-node credit ledger SQL/allocation (PR I) | Used by credit CLI + admin | Keep | Medium | Credit ledger tests |
| `payouts/collector/tests/*` | ACTIVE | Collector/reporting unit tests | No legacy payout imports | Keep | Medium | `pytest payouts/collector/tests` |
| `payouts/scripts/sc_node_work_summary.py` | ACTIVE | Read-only SC-node JSON report | Documented in runbooks | Keep | Low | Manual JSON smoke |
| `payouts/scripts/pool_ledger_admin_readonly.py` | ACTIVE | Read-only admin CLI | Documented in runbooks | Keep | Low | Admin CLI smoke |
| `payouts/scripts/support_wallet_reward_events.py` | ACTIVE | Support-wallet reward scan/print (PR H) | `azc listtransactions` only | Keep | Medium | Dry-run + admin smoke |
| `payouts/scripts/sc_node_credit_ledger.py` | ACTIVE | SC-node credit preview/write/print (PR I) | No `azc`; explicit coverage for writes | Keep | Medium | Preview + write-draft smoke |
| `payouts/scripts/inventory_payout_ledger_files.py` | ACTIVE | PR D inventory helper (read-only scan) | This PR | Keep | Low | Inventory tests |
| `payouts/migrations/001_pool_telemetry_collector.sql` | ACTIVE | Telemetry schema | Applied on support node | Keep | High | `\dt pool_*` |
| `payouts/migrations/002_sc_node_identity_mapping.sql` | ACTIVE | SC nodes + mappings | Applied on support node | Keep | High | `\dt sc_*` |
| `payouts/migrations/003_pool_instance_registry.sql` | ACTIVE | DB pool registry | Applied on support node | Keep | High | `pool_instances` query |
| `payouts/migrations/004_sc_node_payout_addresses.sql` | ACTIVE | SC-node payout address registry (PR G) | Manual apply; no payout execution | Keep | High | `\d sc_node_payout_addresses` |
| `payouts/migrations/005_support_wallet_reward_events.sql` | ACTIVE | Support-wallet reward events (PR H) | Manual apply; observe only | Keep | High | `\d support_wallet_reward_events` |
| `payouts/migrations/006_sc_node_credit_ledger.sql` | ACTIVE | SC-node draft credit ledger (PR I) | Manual apply; no sends | Keep | High | `\d sc_node_reward_credit_runs` |
| `payouts/migrations/007–012_sc_node_payout_*.sql` | ACTIVE | Plans, approval, test/prod execution, reconciliation (PR J–O) | Manual apply per PR | Keep | **Critical** | Per-migration `\d` smoke |
| `payouts/migrations/013_sc_node_payout_production_execution_chunks.sql` | ACTIVE | Chunked production execution chunks (PR S) | Manual apply; UTXO fragmentation path | Keep | **Critical** | `\d sc_node_payout_production_execution_chunks` |
| `payouts/scripts/sc_node_payout_*.py` | ACTIVE | Planner, review, test/prod executor, reconciliation CLIs | Manual operator-triggered; prod send only in executor | Keep | **Critical** | `pytest payouts/collector/tests` |
| `payouts/scripts/sc_node_payout_production_chunked_executor.py` | ACTIVE | Chunked production sendtoaddress sequence (PR S) | After single-send refused (tx too large) | Keep | **Critical** | Preview + dry-run argv tests |
| `payouts/collector/app/sc_node_payout_production_chunked_executor.py` | ACTIVE | Chunk split/SQL/guardrails (PR S) | Chunked CLI + admin chunks | Keep | Medium | Chunked executor tests |
| `docs/runbooks/sc-node-payout-cycle.md` | ACTIVE | End-to-end payout cycle checklist (PR R) | Linked from README | Keep | Low | Ops walkthrough |
| `docs/adr/ADR-sc-node-production-payout-chunked-executor.md` | ACTIVE | Chunked executor ADR (PR S) | PR S | Keep | Low | ADR review |
| `payouts/docs/sc-node-production-payout-chunked-executor.md` | ACTIVE | Chunked executor ops doc (PR S) | PR S | Keep | Low | Ops review |
| `docs/runbooks/sc-node-payout-cycle.md` | ACTIVE | End-to-end payout cycle operator checklist (PR R) | Linked from README | Keep | Low | Ops walkthrough |
| `docs/runbooks/pool-monitoring-collector.md` | ACTIVE | Collector operations | Linked from README | Keep | Low | Ops review |
| `docs/runbooks/pool-ledger-admin.md` | ACTIVE | Read-only admin ops | Linked from README | Keep | Low | Ops review |
| `docs/runbooks/sc-node-payout-addresses.md` | ACTIVE | Payout address registry ops (PR G) | Linked from README/ADR | Keep | Low | Ops review |
| `docs/runbooks/support-wallet-reward-listener.md` | ACTIVE | Support-wallet reward listener ops (PR H) | Linked from README/ADR | Keep | Low | Ops review |
| `docs/runbooks/sc-node-credit-ledger.md` | ACTIVE | SC-node credit ledger ops (PR I) | Linked from README/ADR | Keep | Low | Ops review |
| `docs/adr/ADR-sc-node-credit-ledger.md` | ACTIVE | Credit ledger architecture ADR (PR I) | PR I | Keep | Low | ADR review |
| `docs/adr/ADR-support-wallet-reward-listener.md` | ACTIVE | Reward listener architecture ADR (PR H) | PR H | Keep | Low | ADR review |
| `docs/adr/ADR-support-node-pool-telemetry-collector.md` | ACTIVE | Collector architecture ADR | ADR index | Keep | Low | ADR review |
| `docs/adr/ADR-sc-node-payout-address-registry.md` | ACTIVE | Payout address registry ADR (PR G) | PR G | Keep | Low | ADR review |
| `docs/adr/ADR-pool-ledger-legacy-cleanup-plan.md` | ACTIVE | Cleanup decision record | This inventory PR | Keep | Low | Update with PR refs |
| `docs/inventory/payout-ledger-file-inventory.md` | ACTIVE | File matrix (this document) | PR D | Keep | Low | Re-run inventory script |

### LEGACY-CANDIDATE — user/worker payout & settlement

| Path | Classification | Reason | Evidence | Proposed action | Removal risk | Next verification |
|------|----------------|--------|----------|-----------------|--------------|-------------------|
| `payouts/app/*` | LEGACY-CANDIDATE | Legacy FastAPI user-level payout service | PR F audit: no collector/deploy refs; heavy `payouts/tests` imports | **Not quarantined** — see [payout-app-dependency-audit.md](payout-app-dependency-audit.md) | **Critical** | PR G+ SC-node design |
| `payouts/app/README.md` | DO-NOT-REMOVE-YET | Legacy boundary marker (PR F) | Documents do-not-run rules | Keep until app quarantined | Low | Ops review |
| `payouts/app/main.py` | LEGACY-CANDIDATE | FastAPI payout service entry | README `uvicorn app.main:app`; not in collector systemd | Quarantine blocked until PR F audit reviewed | High | `audit_payout_app_dependencies.py` |
| `payouts/app/settlement.py` | LEGACY-CANDIDATE | User-level settlement | Keywords: settlement, payout | Remove with main.py path | High | Settlement tests audit |
| `payouts/app/postgres_settlement.py` | LEGACY-CANDIDATE | Postgres settlement | Keywords: settlement, payout | Remove with SC-node design | High | Shadow compare deps |
| `payouts/app/sender.py` | LEGACY-CANDIDATE | Wallet send path | Keywords: sender, wallet | Remove after cutover proof | **Critical** | Wallet RPC grep |
| `payouts/app/postgres_sender.py` | LEGACY-CANDIDATE | Postgres-backed sender | Keywords: sender, payout | Remove with sender.py | **Critical** | Sender tests |
| `payouts/app/reward_contract.py` | LEGACY-CANDIDATE | Reward contract (user-level) | Keywords: payout | Remove or rewrite SC-node | High | reward_contract tests |
| `payouts/app/db.py` | LEGACY-CANDIDATE | SQLite legacy DB | Keywords: sqlite, settlement | Remove when SQLite retired | High | init_db / schema tests |
| `payouts/app/init_db.py` | LEGACY-CANDIDATE | SQLite init | Keywords: sqlite | Remove with db.py | Medium | Demo scripts |
| `payouts/app/models.py` | LEGACY-CANDIDATE | ORM models (legacy ledger) | Used by app/ tests | Remove with app/ | High | Import graph |
| `payouts/app/config.py` | LEGACY-CANDIDATE | Legacy service config | Not used by collector | Quarantine | Medium | Env template refs |
| `payouts/app/scheduler.py` | LEGACY-CANDIDATE | In-process payout scheduler | README scheduler section | Disable before delete | High | SCHEDULER_ENABLED grep |
| `payouts/app/poller.py` | LEGACY-CANDIDATE | Legacy poller | test_poller.py | Quarantine | Medium | Poller tests |
| `payouts/app/pool_client.py` | LEGACY-CANDIDATE | Duplicate of collector client pattern | Separate from collector copy | Merge or delete in PR F | Low | Diff vs collector |
| `payouts/app/delta.py` | LEGACY-CANDIDATE | Legacy delta (app) | Parallel to collector delta | Delete after confirm unused | Medium | Import grep |
| `payouts/app/mapping.py` | LEGACY-CANDIDATE | Legacy identity mapping | Pre-dates SC-node table | Replace with collector identity | Medium | test_mapping.py |
| `payouts/app/postgres_*.py` (remaining) | LEGACY-CANDIDATE | Shadow/read/settlement postgres helpers | payouts/tests postgres_* | Quarantine per module | High | Per-file import grep |
| `payouts/app/translator_*.py` | LEGACY-CANDIDATE | Translator capture/reconstruction | Specialized tests | Keep until translator ops confirm | Medium | Ops runbook |
| `payouts/app/hooks.py`, `audit.py`, `metrics_parser.py`, `runtime_cutover.py` | LEGACY-CANDIDATE | Legacy app support | Imported by main/tests | Quarantine with app/ | Medium | Import grep |
| `payouts/legacy/scripts/demo_interval_run.py` | LEGACY-CANDIDATE | Demo payout interval runner (quarantined PR E) | Was `payouts/scripts/`; no systemd ref | Keep in legacy/ until SC-node design | High | `rg demo_interval` |
| `payouts/legacy/scripts/backfill_postgres_shadow.py` | LEGACY-CANDIDATE | Shadow backfill (quarantined PR E) | Was `payouts/scripts/`; `test_postgres_shadow_backfill.py` updated in PR E | Keep in legacy/ | Medium | Ops usage log |
| `payouts/legacy/scripts/backfill_sqlite_settlement_mapping.py` | LEGACY-CANDIDATE | SQLite settlement mapping backfill (quarantined PR E) | Was `payouts/scripts/` | Keep in legacy/ | Medium | Confirm no cron |
| `payouts/legacy/README.md` | DO-NOT-REMOVE-YET | Quarantine index and safety rules | PR E | Keep | Low | Link from payouts/README |
| `payouts/tests/test_settlement.py` | LEGACY-CANDIDATE | Legacy settlement tests | settlement keyword | Remove with settlement.py | Low | pytest collection |
| `payouts/tests/test_sender.py` | LEGACY-CANDIDATE | Sender tests | sender, wallet keywords | Remove with sender.py | Low | Wallet safety grep |
| `payouts/tests/test_postgres_settlement*.py` | LEGACY-CANDIDATE | Postgres settlement tests | settlement keyword | Remove with postgres_settlement | Low | CI job scope |
| `payouts/tests/test_phase*.py` | LEGACY-CANDIDATE | Phased legacy logic tests | phase + payout paths | Archive after phase retirement | Low | Owner sign-off |
| `payouts/tests/test_postgres_shadow*.py` | LEGACY-CANDIDATE | Shadow compare/backfill tests | shadow + settlement | Remove with shadow modules | Medium | DB env required |
| `payouts/tests/*` (other under `payouts/tests/`) | LEGACY-CANDIDATE | Old payout/poller/schema tests | Not under `collector/tests` | Classify per file in script JSON | Medium | `inventory_payout_ledger_files.py` |

### DO-NOT-REMOVE-YET — migrations & schema history

| Path | Classification | Reason | Evidence | Proposed action | Removal risk | Next verification |
|------|----------------|--------|----------|-----------------|--------------|-------------------|
| `payouts/alembic/versions/*.py` | DO-NOT-REMOVE-YET | Historical Postgres payout schema | Alembic revision chain | Keep until DB retirement ADR | **Critical** | `alembic history` on prod |
| `payouts/alembic/env.py` | DO-NOT-REMOVE-YET | Alembic runtime | alembic.ini reference | Keep | High | alembic upgrade dry-run |
| `payouts/alembic.ini` | DO-NOT-REMOVE-YET | Alembic config | May be used in ops | Keep | High | Deploy docs |
| `payouts/migrations/001–003` | DO-NOT-REMOVE-YET | Active telemetry DDL | Collector depends on tables | Keep (also ACTIVE) | **Critical** | Already applied — do not drop |

### UNKNOWN — needs owner review

| Path | Classification | Reason | Evidence | Proposed action | Removal risk | Next verification |
|------|----------------|--------|----------|-----------------|--------------|-------------------|
| `payouts/README.md` | UNKNOWN | Mixed legacy + collector docs | References uvicorn + demo scripts | Split or trim in PR E | Low | Doc review |
| `payouts/scripts/check_candidate_blocks.sh` | UNKNOWN | Ops shell helper | Not collector; not settlement demo | Classify with translator ops | Medium | Runbook reference |
| `payouts/scripts/run_translator_sv1_capture_proxy.py` | DO-NOT-REMOVE-YET | Translator proxy runner | **Active import** of `app.postgres_db`, `app.postgres_repositories`, `app.translator_sv1_capture_proxy` — blocks `payouts/app` quarantine | Keep until translator ownership decided | Medium | See [payout-app-dependency-audit.md](payout-app-dependency-audit.md) |
| `payouts/docs/*.md` | UNKNOWN | Legacy postgres/translator plans | Historical design | Archive or link from ADR | Low | Stale date check |
| `payouts/plan/*.md` | UNKNOWN | Phase deployment notes | PHASE7, SC2 preflight | Archive when phases close | Low | Owner review |
| `payouts/requirements.txt` | UNKNOWN | Shared deps for app + collector | Both trees install | Pin split requirements later | Medium | pip compile diff |
| `payouts/.env.example` | UNKNOWN | Legacy API env template | May contain payout vars | Keep until API retired | Low | Secret scan |
| `payouts/docker-compose.postgres.yml` | UNKNOWN | Local dev postgres | Legacy tests | Keep for dev until legacy tests gone | Low | Compose usage |
| `deploy/systemd/azcoin-pool-collector.*` | UNKNOWN (ops) | Active collector deploy | Outside `payouts/` edit scope | DO-NOT-REMOVE-YET for collector | High | systemctl status |

Re-run `payouts/scripts/inventory_payout_ledger_files.py` for the full machine-readable file list and keyword hits.

---

## Deletion rules

A file may only be **deleted** or **quarantined** after **all** of the following are true:

1. **No systemd/deploy references** — grep `deploy/`, `/etc/systemd`, install scripts.
2. **No active imports** from `payouts/collector/`, read-only scripts, or support-node runbooks.
3. **Not required by runtime DB migrations** — Alembic chain and applied `payouts/migrations/*` remain until a DB retirement plan is approved.
4. **Not required by current tests** — collector tests and agreed CI subset still pass.
5. **Rollback plan exists** — restore branch, systemd units, or feature flags documented.
6. **Replacement exists for money/accounting** — SC-node-level payout-credit design (PR G+) before any sender/settlement removal.

---

## Verification commands before removal

```bash
# Branch and inventory helper
cd /opt/azcoin-super/src/azpool
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python payouts/scripts/inventory_payout_ledger_files.py | less

# Collector path must not import legacy app
rg -n "from payouts\.app|import payouts\.app" payouts/collector payouts/scripts/sc_node_work_summary.py payouts/scripts/pool_ledger_admin_readonly.py

# Systemd / deploy references
rg -n "payouts\.app|demo_interval|uvicorn app\.main" deploy docs /etc/systemd 2>/dev/null || true

# Wallet / broadcast keywords in candidate files only
rg -n "sendmany|sendtoaddress|sendrawtransaction|walletpassphrase" payouts/app/settlement.py payouts/app/sender.py payouts/app/postgres_sender.py || true

# Legacy test scope
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python -m pytest payouts/collector/tests -q
# Full legacy suite (optional, pre-removal baseline):
# PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python -m pytest payouts/tests -q
```

---

## Recommended PR sequence

| PR | Scope | Deletes code? |
|----|--------|----------------|
| **PR D** (`feature/payout-ledger-legacy-inventory-v0`) | Inventory matrix + script + ADR link | **No** |
| **PR E** (`feature/payout-ledger-legacy-quarantine-plan-v0`) | Quarantine standalone legacy scripts → `payouts/legacy/scripts/` | **No** (git mv only) |
| **PR F** (`feature/payout-app-dependency-audit-v0`) | `payouts/app` dependency audit + boundary README — no moves | **No** |
| **PR G** (`feature/sc-node-payout-address-registry-v0`) | SC-node payout address registry — schema + read-only admin | **No** |
| **PR H** | Read-only support-wallet reward listener | **Yes** (PR H) |
| **PR I** | SC-node credit ledger (no wallet sends) | **Yes** (PR I) |
| **PR J** | Payout plan generator (no wallet sends) | **No** |
| **PR K** | Guarded dry-run wallet payout execution (separate approval) | TBD |

**PR G** begins replacement SC-node payout foundation with address registry only — **not automated payouts**. **PR E/F** precede it; **`payouts/app/*` not quarantined**.
