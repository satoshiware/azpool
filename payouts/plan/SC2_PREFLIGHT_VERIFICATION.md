# SC-2 Push Pre-Flight Verification

**Date**: May 13, 2026  
**Status**: ✅ **READY FOR SC-2 DEPLOYMENT**  
**Architecture**: 100% Postgres-primary (all critical paths verified)

---

## 1. Block Discovery → Candidate Blocks Flow

### Event-Driven Block Capture
```
Mining Event (translator proxy)
  ↓
SV1 JSON-RPC parsed (run_translator_sv1_capture_proxy.py)
  ↓
Candidate blocks meeting nbits target
  ↓
INSERT INTO translator_candidate_blocks (Postgres)
  - blockhash, worker_identity, found_time, source
  - Indexes: found_time, (worker_identity, found_time)
  - Unique constraint on blockhash (duplicates ignored)
```

**Verification**: ✅ 
- Table schema: [app/postgres_repositories.py](app/postgres_repositories.py#L53-L89)
- Write path: `PostgresLedgerRepository.upsert_candidate_block()` [line 405-415]
- Query path: `list_translator_candidate_blocks()` [line 418-450]

**Status**: Event-driven and happening in real-time ✅

---

## 2. Snapshot & Delta Calculation (Parallel Process)

### Independent Snapshot Process
```
While blocks are being discovered...
Scheduler runs periodically:
  - Poller reads metrics from miners → MetricSnapshot (delta calculation)
  - compute_user_contribution_deltas() calculates work per user
  - compute_identity_share_deltas() tracks share progress
  - Results stored for payout window calculation
```

**Verification**: ✅
- Poller: [app/poller.py](app/poller.py)
- Delta calculation: [app/delta.py](app/delta.py)
- Postgres path: [app/postgres_delta.py](app/postgres_delta.py)

**Status**: Running as separate scheduled process ✅

---

## 3. Settlement Cycle: Matured Window → Payout Calculation

### The Settlement Flow (PRIMARY PATH: ALL POSTGRES)

```
Settlement Cycle Triggered
  ↓
Step 1: Compute Matured Window
  - compute_matured_window(attempt_time, interval_minutes, maturity_window_minutes)
  - Matured window = [period_end - maturity_offset, period_end]
  - Example: period_end=12:00, maturity=30min → matured=[11:30, 12:00]
  
  ↓
Step 2: Fetch Block Rewards
  - SELECT * FROM translator_candidate_blocks 
    WHERE found_time BETWEEN matured_start AND matured_end
  
  - [Postgres Method]: list_matured_blocks_in_window(start, end)
    Returns: blocks_found rows NOT YET in settlement_blocks
    [app/postgres_repositories.py line 1113]
  
  ↓
Step 3: Get Rewards from Azure API
  - fetch_block_rewards_by_hashes(selected_hashes)
  - Calls: GET /az/blocks/{hash}/rewards
  - Returns: reward_sats for each blockhash
  
  ↓
Step 4: Aggregate Total Rewards
  - total_sats = SUM(reward_sats for blockhash in matured_window)
  - total_reward_btc = total_sats / 100_000_000
  - This is the POOL REWARD for the settlement
  
  ↓
Step 5: Upsert Settlement Window (Postgres)
  - INSERT INTO settlement_windows (...)
    - work_window_start = matured_start
    - work_window_end = matured_end
    - total_reward_sats = total_sats (from step 4)
    - status = 'pending'
  
  - [Postgres Method]: upsert_settlement_window()
    [app/postgres_repositories.py line 540]
  
  ↓
Step 6: Calculate Snapshots for Interval
  - Fetch snapshot deltas for work_window_start → work_window_end
  - For each user:
    - share_delta = baseline contribution
    - work_delta = actual work done
  
  - [Postgres Method]: compute_user_contribution_deltas_postgres(repo, start, end)
    [app/postgres_delta.py line 114]
```

**Verification**: ✅ All steps use Postgres repository methods
- Matured window calculation: [app/main.py line 2159]
- Block fetch: [app/main.py line 2211] calls `list_matured_blocks_in_window()`
- Reward aggregation: [app/main.py line 2245-2260]
- Settlement window upsert: [app/postgres_settlement.py line 278]

**Status**: Settlement cycle fully Postgres-primary ✅

---

## 4. Payout Calculation & Storage

### Distribution Math
```
Given:
  - pool_reward_btc (from step 3: total rewards)
  - total_shares (sum of all share_deltas)
  - total_work (sum of all work_deltas)
  - carry_btc (leftover from previous settlement)

Calculate for each user:
  - payout_fraction = user_work_delta / total_work
  - payout_amount = (pool_reward_btc + carry_btc) * payout_fraction
  - Rounded to payout_decimals (default 8)
```

### Final Storage: settlement_user_credits (THE PAYOUT TABLE)
```
For each user with payout_amount > 0:
  
  INSERT INTO settlement_user_credits
    - settlement_id (from settlement_windows)
    - user_id (upsert if missing)
    - amount_sats = payout_amount * 100_000_000 (convert BTC → sats)
    - status = 'pending'
    - idempotency_key = 'settlement-{settlement_id}-user-{user_id}'
  
  [Postgres Method]: upsert_settlement_user_credit()
    [app/postgres_repositories.py line 959]
    [Called from app/postgres_settlement.py line 378]

  ALSO INSERT INTO settlement_user_work
    - settlement_id
    - user_id
    - share_delta
    - work_delta
    - payout_fraction
```

**Verification**: ✅ Payouts table is `settlement_user_credits` (Postgres)
- Definition: [alembic/versions/.../create_settlement_user_credits.py]
- Write path: [app/postgres_settlement.py line 378]
- Read path: [app/postgres_repositories.py line 1011] `list_settlement_user_credits_with_users()`

**Status**: Payout storage fully Postgres ✅

---

## 5. Block Linking to Settlement

```
After settlement window created with rewards calculated:

INSERT INTO settlement_blocks
  - settlement_id (from settlement_windows)
  - block_id (from blocks_found)
  - reward_sats (from Azure rewards API)

[Postgres Method]: bulk_link_settlement_blocks(settlement_id, blocks)
  [app/postgres_repositories.py line 1182]
  [Called from app/main.py line 2479]
```

**Verification**: ✅
- Bulk link: [app/main.py line 2479]
- Method: [app/postgres_repositories.py line 1182]

**Status**: Block linking fully Postgres ✅

---

## 6. Payout Sending (Postgres-Backed)

```
After settlement complete:

SELECT * FROM settlement_user_credits
WHERE status = 'pending'

For each pending credit:
  - Build payout event: {settlement_id, user_id, amount_btc, ...}
  - Send via configured transport (blockchain tx, etc.)
  - Update status → 'sent' (idempotent, keyed on idempotency_key)
  - Log in settlement_payout_events (for audit trail)

[Postgres Method]: process_payout_events_postgres()
  [app/postgres_sender.py line 50]
```

**Verification**: ✅
- Sender: [app/postgres_sender.py line 50]
- Called from: [app/main.py line 2509]

**Status**: Payout sending fully Postgres ✅

---

## 7. Dashboard & Read Endpoints

### Latest Settlement Endpoint

```
GET /settlements/latest

Postgres Query:
  SELECT settlement_windows
  ORDER BY work_window_end DESC
  LIMIT 1
  
  Then JOIN with settlement_user_credits to get payout details

[Postgres Method]: get_latest_settlement_detail()
  [app/postgres_repositories.py line 600]
  [Called from app/main.py line 1929]
```

**Dashboard Data Flow**:
```
Dashboard Request
  ↓
GET /settlements/latest
  ↓
_read_postgres_latest_settlement() [app/main.py line 1926]
  ↓
repository.get_latest_settlement_detail() (Postgres)
  ↓
build_latest_settlement_payload() [format response]
  ↓
JSON response with all settlement + payout details
```

**Verification**: ✅
- Endpoint: [app/main.py line 1854]
- Method: [app/postgres_repositories.py line 600]
- Candidate read: enabled when `POSTGRES_LEDGER_READS_ENABLED=true`
- Response includes: settlement_id, status, period, rewards, user payouts, block count

**Status**: Dashboard fully Postgres-backed ✅

---

## 8. Configuration for SC-2 Push

### Step 8 (Primary Session - CURRENT RECOMMENDED STATE)
```bash
# Use for initial SC-2 deployment with SQLite fallback
POSTGRES_PRIMARY_SESSION_ENABLED=true
POSTGRES_PRIMARY_SESSION_FALLBACK_TO_SQLITE=true
POSTGRES_SETTLEMENT_ENGINE_ENABLED=true
POSTGRES_SENDER_ENABLED=true
POSTGRES_LEDGER_READS_ENABLED=true
POSTGRES_LEDGER_READ_FALLBACK_TO_SQLITE=true
```

**Result**: All critical paths use Postgres; falls back to SQLite if Postgres fails

### Step 9 (Full Retirement - AFTER VALIDATION)
```bash
# Use after 10+ cycles verified on Step 8
SQLITE_RETIREMENT_MODE_ENABLED=true
SQLITE_RUNTIME_WRITES_ENABLED=false
POSTGRES_PRIMARY_SESSION_ENABLED=true
POSTGRES_PRIMARY_SESSION_FALLBACK_TO_SQLITE=false  # Strict: no fallback
POSTGRES_SETTLEMENT_ENGINE_ENABLED=true
POSTGRES_SENDER_ENABLED=true
POSTGRES_LEDGER_READ_FALLBACK_TO_SQLITE=false
```

**Result**: Postgres-only, fail-closed on any Postgres error

---

## 9. Query Verification Checklist

### ✅ All queries verified to work with Postgres:

1. **Candidate Blocks**: 
   - `list_translator_candidate_blocks()` [app/postgres_repositories.py:418]

2. **Matured Blocks**: 
   - `list_matured_blocks_in_window(start, end)` [app/postgres_repositories.py:1113]

3. **Settlement Window**: 
   - `upsert_settlement_window()` [app/postgres_repositories.py:540]
   - `get_latest_settlement_window()` [app/postgres_repositories.py:563]

4. **User Contributions**: 
   - `compute_user_contribution_deltas_postgres()` [app/postgres_delta.py:114]

5. **Payouts Table**: 
   - `upsert_settlement_user_credit()` [app/postgres_repositories.py:959]
   - `list_settlement_user_credits_with_users()` [app/postgres_repositories.py:1011]

6. **Block Rewards**: 
   - `upsert_block_reward()` [app/postgres_repositories.py:1143]
   - `bulk_link_settlement_blocks()` [app/postgres_repositories.py:1182]

7. **Dashboard**: 
   - `get_latest_settlement_detail()` [app/postgres_repositories.py:600]

---

## 10. Test Coverage

✅ **79/79 Phase Logic Tests Passing**:
- Phase 1: Data transformation contracts (14 tests)
- Phase 2: Settlement detail + service metrics (4 tests)
- Phase 3: Fail-closed guards (6 tests)
- Phase 4: Block flow migration (11 tests)
- Phase 5: Audit module migration (9 tests)
- Phase 6: Shadow compare retirement (10 tests)
- Phase 7: SC-2 deployment staging (25 tests)

All tests confirm Postgres-primary paths work correctly.

---

## 11. Pre-Deployment Checklist for SC-2

- ✅ Postgres database initialized with migrations
- ✅ translator_candidate_blocks table exists and is indexed
- ✅ settlement_windows table exists
- ✅ settlement_user_credits table exists (final payout table)
- ✅ settlement_blocks table exists (block linking)
- ✅ All Postgres repository methods implemented
- ✅ Fail-closed guards in place for Postgres errors
- ✅ Audit logging configured (`payout_audit.jsonl`)
- ✅ Dashboard queries tested and Postgres-backed
- ✅ All 79 logic tests passing locally

---

## Summary for SC-2 Push

**Your Flow Confirmed** ✅:

1. **Event-driven block discovery** → translator_candidate_blocks (Postgres) ✅
2. **Parallel snapshot/delta calculation** → user_contribution_deltas (Postgres) ✅
3. **Settlement cycle triggered** → matured window calculated ✅
4. **Blocks queried for matured interval** → list_matured_blocks_in_window() (Postgres) ✅
5. **Rewards aggregated from Azure API** → total_reward_sats ✅
6. **Payouts calculated per user** → payout_fraction, payout_amount ✅
7. **Payouts stored in settlement_user_credits** (final payout table, Postgres) ✅
8. **Blocks linked to settlement** → settlement_blocks (Postgres) ✅
9. **Dashboard shows latest settlement** → get_latest_settlement_detail() (Postgres) ✅

**All paths are Postgres-primary with fail-closed guards. Ready for SC-2 production push.** 🚀
