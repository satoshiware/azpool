# Payout app dependency audit (PR F)

**Branch:** `feature/payout-app-dependency-audit-v0`  
**Purpose:** Static audit of `payouts/app/*` dependencies and runtime boundaries. **No moves, deletions, or runtime changes.**

Related: [payout-ledger-file-inventory.md](payout-ledger-file-inventory.md), [ADR-pool-ledger-legacy-cleanup-plan.md](../adr/ADR-pool-ledger-legacy-cleanup-plan.md)  
Helper: `payouts/scripts/audit_payout_app_dependencies.py` (read-only JSON)

**Audit scope note:** Inbound reference counts include **source, docs, config, and script files only**. Generated/cache paths are **excluded**: `__pycache__/`, `.pytest_cache/`, `.pyc`, `.pyo`, `.so`, and paths under `.git/` or `.venv/`.

## Executive summary

`payouts/app/` is the **legacy FastAPI payout service** — user/worker-level settlement, SQLite ledger, Postgres shadow ledger, and wallet sender paths. It is **not** part of the active support-node SC-node telemetry, read-only admin, or collector systemd path.

**PR F** adds this audit, `payouts/app/README.md` boundary markers, and a read-only audit script. **`payouts/app` Python modules are unchanged and not quarantined** pending replacement SC-node payout design and verified runtime retirement.

Static scan (2026-05-19):

| Finding | Result |
|---------|--------|
| `deploy/` / systemd references to `payouts/app` | **None** (collector uses `payouts.collector.app.main` only) |
| `payouts/collector/` imports of `app.*` | **None** in source (inventory tests mention paths as strings only) |
| `payouts/tests/` imports of `app.*` | **~32 test modules** |
| `payouts/legacy/scripts/` imports of `app.*` | **3 quarantined backfill/demo scripts** |
| `payouts/scripts/run_translator_sv1_capture_proxy.py` | **Active-script dependency** — imports `app.postgres_db`, `app.postgres_repositories`, `app.translator_sv1_capture_proxy` |

Future payout work must be **SC-node-level** (`sc_node_id`), not worker/user-level `UserPayout` settlement.

## Current active path (unchanged)

- `payouts/collector/app/*` + collector tests
- `payouts/scripts/sc_node_work_summary.py`, `pool_ledger_admin_readonly.py`, `inventory_payout_ledger_files.py`
- `payouts/migrations/001–003`
- Support-node runbooks and inventory docs
- Quarantined standalone scripts: `payouts/legacy/scripts/*`

## Why `payouts/app` is legacy-candidate

| Reason | Detail |
|--------|--------|
| Architecture mismatch | Pays/credits **users/workers** via `UserPayout`, not SC nodes |
| FastAPI service surface | `main.py` exposes settlement/payout HTTP API |
| Money movement risk | `sender.py`, `postgres_sender.py` — wallet interaction |
| SQLite + shadow Postgres | Parallel legacy ledger; not telemetry `pool_share_work_deltas` |
| Superseded identity model | `mapping.py` / miner identity vs collector `sc_node_id` mapping |

**Explicit:** `payouts/app` is **not** part of the active collector/admin/reporting path. It **is** still referenced by legacy tests and quarantined scripts and may represent historical deployed behavior elsewhere.

**Do not quarantine `payouts/app`** until runtime/service references are verified retired and replacement SC-node payout-credit design exists.

## Module groups

### A) LEGACY-CANDIDATE / HIGH RISK

| Module | Role | Inbound refs (approx.) |
|--------|------|------------------------|
| `main.py` | FastAPI entry, scheduler hooks | tests, docs, README |
| `settlement.py` | User-level settlement | tests, legacy scripts |
| `postgres_settlement.py` | Postgres settlement | tests |
| `sender.py` | Wallet send | tests |
| `postgres_sender.py` | Postgres sender | tests |
| `reward_contract.py` | Reward contract | tests, pool_client |
| `models.py` | ORM (`UserPayout`, `Settlement`, …) | tests, app internals |
| `db.py` | SQLite engine/session | tests, app internals |
| `init_db.py` | SQLite schema init | tests |

### B) LEGACY-CANDIDATE / SUPPORTING

| Module | Role |
|--------|------|
| `audit.py` | Audit payloads for API |
| `config.py` | Legacy service settings |
| `delta.py` | User contribution deltas |
| `hooks.py` | Lifecycle hooks |
| `mapping.py` | Miner/user identity mapping |
| `metrics_parser.py` | Metrics parsing |
| `poller.py` | Channel/metrics polling |
| `pool_client.py` | Pool API client (legacy) |
| `postgres_db.py` | Postgres session |
| `postgres_delta.py` | Postgres delta path |
| `postgres_read_payloads.py` | Read payload builders |
| `postgres_repositories.py` | Postgres repositories |
| `postgres_schema.py` | Postgres schema metadata |
| `postgres_shadow_compare.py` | SQLite ↔ Postgres shadow compare |
| `runtime_cutover.py` | Read cutover flags |
| `scheduler.py` | APScheduler integration |
| `translator_candidate_reconstruction.py` | Translator candidate logic |
| `translator_sv1_capture_proxy.py` | Translator capture proxy |

### C) DO-NOT-REMOVE-YET

- All `payouts/app/*.py` while **`payouts/tests/*`** imports `app.*` (~32 modules)
- FastAPI tests importing `app.main` (`test_health.py`, shadow tests, read-flag tests, …)
- Alembic/schema tests (`test_alembic_env_database_url.py`, `test_schema.py`)
- Historical Postgres shadow/settlement concepts until SC-node ledger replacement
- `payouts/app/README.md` — boundary documentation (PR F)

## Static dependency findings

**Inbound to `payouts/app` (outside `payouts/app/`):**

| Source | Pattern | Notes |
|--------|---------|-------|
| `payouts/tests/*` | `from app.<module>` | Primary dependency; blocks removal |
| `payouts/legacy/scripts/*` | `from app.*` | Quarantined; still import app |
| `payouts/scripts/run_translator_sv1_capture_proxy.py` | `from app.postgres_*`, `from app.translator_sv1_capture_proxy` | **Blocks wholesale `payouts/app` quarantine** until translator runtime ownership is decided (move script, extract shared lib, or retire) |
| `payouts/README.md` | `uvicorn app.main:app` | Legacy API docs |
| `docs/inventory/*`, ADR | Path references | Documentation only |

**Internal `payouts/app` graph:** `main.py` imports settlement, sender, postgres_*, poller, scheduler, audit, hooks, config, models, db — tightly coupled.

**No inbound from:** `payouts/collector/app/*`, collector scripts, `deploy/systemd/azcoin-pool-collector.service`.

## Runtime / deploy reference findings

```text
deploy/systemd/azcoin-pool-collector.service
  ExecStart=... python -m payouts.collector.app.main   # NOT payouts.app
```

Grep across `deploy/` finds **no** `uvicorn app.main`, `payouts/app/`, or `payouts.app` references.

Legacy API may still be documented for local/dev in `payouts/README.md` — not support-node production path per current architecture.

## Test dependency findings

| Test area | Example modules | App deps |
|-----------|-----------------|----------|
| Settlement | `test_settlement.py`, `test_postgres_settlement*.py` | settlement, models, db |
| Sender | `test_sender.py` | sender, models |
| FastAPI / health | `test_health.py`, `test_postgres_shadow_*.py` | main, app |
| Shadow compare | `test_postgres_shadow_compare.py`, backfill test | postgres_shadow_compare |
| Phases 1–7 | `test_phase*.py`, `test_phase1_slices.py` | mixed postgres/runtime |
| Translator | `test_translator_*.py` | translator_* modules |
| Schema | `test_schema.py`, alembic tests | init_db, postgres_schema |

Removing `payouts/app` without rewriting or dropping these tests will break CI for the legacy suite.

## Risk areas

| Risk | Severity | Mitigation |
|------|----------|------------|
| Wallet RPC via `sender.py` | **Critical** | No quarantine until SC-node design + ops sign-off |
| User-level payout rows | **High** | Future ledger must use `sc_node_id` only |
| SQLite + Postgres shadow drift | **High** | Do not conflate with pool telemetry tables |
| Accidental API startup (`uvicorn app.main`) | **Medium** | README warnings; not in support-node systemd |
| Translator proxy script | **Medium** | `run_translator_sv1_capture_proxy.py` imports `payouts/app` Postgres + translator modules — blocks app quarantine |

## Recommended next PRs

| PR | Scope |
|----|--------|
| **PR F** (this) | Dependency audit + `payouts/app/README.md` — no moves |
| **PR G** | SC-node payout address registry spec/schema |
| **PR H** | Read-only support-wallet reward listener |
| **PR I** | SC-node credit ledger (no wallet sends) |
| **PR J** | Payout plan generator (no wallet sends) |
| **PR K** | Guarded dry-run wallet payout execution (separate approval) |

Later quarantine/removal of `payouts/app` requires PR G–I foundation and verified no production dependency.

## Verification commands

```bash
cd /opt/azcoin-super/src/azpool
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python payouts/scripts/audit_payout_app_dependencies.py | less

rg -n "from app\.|import app\." payouts/collector payouts/scripts/sc_node_work_summary.py payouts/scripts/pool_ledger_admin_readonly.py
rg -n "uvicorn app\.main|payouts/app" deploy docs/runbooks
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python -m pytest payouts/collector/tests -q
```
