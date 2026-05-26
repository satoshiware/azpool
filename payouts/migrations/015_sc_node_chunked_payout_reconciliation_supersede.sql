-- Chunked payout reconciliation supersede support (PR U).
-- Preserves historical rows; allows one active reconciliation per production_execution_id.

ALTER TABLE sc_node_chunked_payout_reconciliations
  ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS superseded_by_reconciliation_id BIGINT
    REFERENCES sc_node_chunked_payout_reconciliations (id),
  ADD COLUMN IF NOT EXISTS superseded_reason TEXT;

ALTER TABLE sc_node_chunked_payout_reconciliations
  DROP CONSTRAINT IF EXISTS scn_cpr_exec_uniq;

CREATE UNIQUE INDEX IF NOT EXISTS idx_scn_cpr_exec_active_uniq
ON sc_node_chunked_payout_reconciliations (production_execution_id)
WHERE superseded_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_scn_cpr_superseded_at
ON sc_node_chunked_payout_reconciliations (superseded_at)
WHERE superseded_at IS NOT NULL;
