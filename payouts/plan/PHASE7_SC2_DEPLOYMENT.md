# Phase 7 - SC-2 Staged Deployment Checklist

> Dated: May 13, 2026  
> Target: SC-2 production environment  
> All phases 1-6 complete. Logic is in place for staged Postgres migration.

## Pre-Deployment Verification

- [ ] All Phase 1-6 tests passing locally (54/54 minimum)
- [ ] Code reviewed for Postgres primary paths in:
  - [ ] `app/main.py` — settlement cycle, sender, audit endpoints
  - [ ] `app/audit.py` — snapshot alignment, payout rows, user contributions  
  - [ ] `app/poller.py` — block polling and reward upsert
  - [ ] `app/postgres_shadow_compare.py` — shadow audit paths
- [ ] Postgres environment prepared:
  - [ ] Database accessible from SC-2
  - [ ] Migrations applied (alembic up)
  - [ ] Connection pooling configured
  - [ ] Backups verified
- [ ] SQLite backup created before deployment

---

## Deployment Steps

### Step 8 — Primary Session Cutover

**Goal**: Route main app session to Postgres while retaining SQLite fallback.  
**Success Criteria**: Settlement cycles run uninterrupted; parity matches expected.  
**Rollback**: Revert `POSTGRES_PRIMARY_SESSION_ENABLED` to false; restart app.

**Required Configuration**:
```bash
POSTGRES_PRIMARY_SESSION_ENABLED=true
POSTGRES_PRIMARY_SESSION_FALLBACK_TO_SQLITE=true
POSTGRES_LEDGER_DATABASE_URL=postgresql://...
```

**Validation Checklist**:
- [ ] App starts without errors
- [ ] `/health` endpoint returns success
- [ ] Next settlement cycle completes normally
- [ ] `/settlement/latest` returns correct data
- [ ] Payout audit log generated (`logs/payout_audit.jsonl`)
- [ ] Shadow compare shows `comparison_status: "matched"` (if SQLite still available)
- [ ] No errors in app logs related to Postgres connection
- [ ] Monitor for 3-5 settlement cycles (~30-60 minutes)

**Monitoring Queries**:
```sql
-- Verify settlement windows created in Postgres
SELECT COUNT(*) FROM settlement_windows WHERE settlement_run_at > NOW() - INTERVAL '1 hour';

-- Check payout records
SELECT COUNT(*) FROM settlement_user_credits WHERE settlement_id IN (
  SELECT id FROM settlement_windows WHERE settlement_run_at > NOW() - INTERVAL '1 hour'
);

-- Verify block rewards
SELECT COUNT(*) FROM settlement_blocks WHERE settlement_id IN (
  SELECT id FROM settlement_windows WHERE settlement_run_at > NOW() - INTERVAL '1 hour'
);
```

**If Issues Found**:
1. Check app logs for Postgres errors: `grep -i "postgres\|error" app.log`
2. Verify Postgres connection: `psql $POSTGRES_LEDGER_DATABASE_URL -c "SELECT 1"`
3. If parity mismatch, use `/postgres-shadow/settlements/{id}/compare` endpoint to inspect
4. Revert to Step 7 (SQLite-only): set `POSTGRES_PRIMARY_SESSION_ENABLED=false`, restart

---

### Step 9 — SQLite Retirement Mode (Postgres-Only)

**Goal**: Disable SQLite runtime writes and fallbacks entirely.  
**Success Criteria**: App operates on Postgres exclusively; parity proven over ≥10 cycles.  
**Rollback**: Set `SQLITE_RETIREMENT_MODE_ENABLED=false`, restart (falls back to Step 8).

**Required Configuration**:
```bash
SQLITE_RETIREMENT_MODE_ENABLED=true
SQLITE_RUNTIME_WRITES_ENABLED=false
POSTGRES_PRIMARY_SESSION_ENABLED=true
POSTGRES_PRIMARY_SESSION_FALLBACK_TO_SQLITE=false
POSTGRES_SETTLEMENT_ENGINE_ENABLED=true
POSTGRES_SENDER_ENABLED=true
POSTGRES_LEDGER_READ_FALLBACK_TO_SQLITE=false
```

**Pre-Step 9 Gate**:
- [ ] Step 8 stable for ≥10 cycles (4-8 hours minimum)
- [ ] Zero parity mismatches in that window
- [ ] All settlement cycles completed successfully
- [ ] Payout sends all delivered (confirmed on-chain if applicable)
- [ ] Audit log complete and validated

**Validation Checklist**:
- [ ] App starts without errors (no SQLite session opened)
- [ ] `/health` endpoint returns success
- [ ] Next settlement cycle completes normally
- [ ] Payout audit log generated with expected shape
- [ ] No "fallback to SQLite" messages in logs (should not exist at all)
- [ ] Monitor for ≥10 settlement cycles before final commit

**Monitoring Queries** (same as Step 8, plus):
```sql
-- Verify all settlements in Postgres (no SQLite query)
SELECT COUNT(*) FROM settlement_windows;

-- Verify settlement credit distribution
SELECT status, COUNT(*) as count FROM settlement_user_credits 
GROUP BY status;
```

**If Issues Found**:
1. Check retirement mode constraints in app logs
2. Verify all config prerequisites are set (see required config above)
3. Check Postgres connectivity and query performance
4. If critical issue: set `SQLITE_RETIREMENT_MODE_ENABLED=false`, restart immediately

---

## Rollback Procedure

### Quick Rollback to Step 8
```bash
SQLITE_RETIREMENT_MODE_ENABLED=false
# Restart app
# App will use primary session with SQLite fallback (Step 8 mode)
```

### Full Rollback to SQLite-Only
```bash
POSTGRES_PRIMARY_SESSION_ENABLED=false
# Restart app
# App will use SQLite exclusively
```

---

## Post-Deployment Validation

After Step 9 is stable for ≥24 hours:

- [ ] Confirm ≥50 settlement cycles completed successfully
- [ ] Verify payout sends all delivered
- [ ] Run shadow audit: `GET /postgres-shadow/settlements/audit?limit=50`
- [ ] Check audit logs are complete and queryable
- [ ] Performance metrics stable (response times, query duration)
- [ ] No orphaned SQLite locks or connections
- [ ] Document any custom patches or workarounds applied

---

## Known Gotchas & Troubleshooting

### "Postgres primary is enabled but Postgres query failed"
**Cause**: Postgres unavailable when `POSTGRES_PRIMARY_SESSION_ENABLED=true` and `POSTGRES_PRIMARY_SESSION_FALLBACK_TO_SQLITE=false`.  
**Fix**: Either enable fallback or restore Postgres connectivity.

### Parity mismatch in shadow compare
**Cause**: Data divergence between SQLite and Postgres (usually during backfill).  
**Investigation**: 
```bash
# Get mismatch details
curl "http://localhost:8000/postgres-shadow/settlements/audit?status_filter=mismatched&include_details=true"
```
**Fix**: Depends on mismatch type; may require data sync or migration retry.

### Audit log not generated
**Cause**: Audit module failed silently or Postgres path has an error.  
**Investigation**: Check `logs/payout_audit.jsonl` exists and has recent entries.  
**Fix**: Examine app logs for `_build_snapshot_alignment`, `_build_payout_rows`, `_build_user_contributions` errors.

### Performance degradation
**Cause**: Postgres connection pool exhausted or query slow.  
**Fix**: 
- [ ] Tune `POSTGRES_POOL_SIZE` and `POSTGRES_MAX_OVERFLOW`
- [ ] Check Postgres slow query log
- [ ] Verify indexes on settlement tables
- [ ] Consider read-only replicas if read load is high

---

## Verification Scripts

### Check Current Deployment Stage
```bash
# Shows which mode is active
curl http://localhost:8000/postgres-shadow/read-mode
```

### Validate Configuration
```bash
# Verify required environment variables
env | grep -E "POSTGRES_PRIMARY|SQLITE_RETIREMENT|POSTGRES_SETTLEMENT|POSTGRES_SENDER"
```

### Run Health Check
```bash
# Basic health
curl http://localhost:8000/health

# Settlement detail (Postgres path when primary enabled)
curl http://localhost:8000/latest-settlement

# Service metrics
curl http://localhost:8000/metrics
```

### Audit Bulk Comparison (Step 8)
```bash
# Verify shadow compare works before Step 9
curl "http://localhost:8000/postgres-shadow/settlements/audit?limit=10"
```

---

## Document Updates Needed Post-Deployment

- [ ] Update production runbooks with Step 8/9 procedures
- [ ] Document any custom configuration applied to SC-2
- [ ] Record deployment timing and cycle counts for future reference
- [ ] Update disaster recovery plan with Postgres-first procedures

---

## Contact & Escalation

**Primary**: [SC-2 on-call engineer]  
**Backup**: [Ledger team lead]  
**Escalation**: If parity broken or rollback needed, contact immediately.
