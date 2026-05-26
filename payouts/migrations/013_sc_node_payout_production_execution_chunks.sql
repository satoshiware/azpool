-- SC-node chunked production payout execution (PR S).
-- Multiple sendtoaddress calls per payout row for UTXO fragmentation.

ALTER TABLE sc_node_payout_production_executions
  DROP CONSTRAINT IF EXISTS scn_ppe_status_check;

ALTER TABLE sc_node_payout_production_executions
  ADD CONSTRAINT scn_ppe_status_check
    CHECK (status IN ('draft', 'sent', 'confirmed', 'refused', 'void', 'partial_sent'));

CREATE TABLE IF NOT EXISTS sc_node_payout_production_execution_chunks (
  id BIGSERIAL PRIMARY KEY,
  production_execution_id BIGINT NOT NULL
    REFERENCES sc_node_payout_production_executions(id),
  production_execution_row_id BIGINT NOT NULL
    REFERENCES sc_node_payout_production_execution_rows(id),
  payout_plan_id BIGINT NOT NULL,
  payout_plan_row_id BIGINT NOT NULL,
  sc_node_id TEXT NOT NULL,
  payout_address TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  chunk_amount NUMERIC(24, 12) NOT NULL,
  chunk_status TEXT NOT NULL DEFAULT 'draft',
  txid TEXT,
  refusal_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT scn_ppec_chunk_status_check
    CHECK (chunk_status IN ('draft', 'sent', 'confirmed', 'refused')),
  CONSTRAINT scn_ppec_chunk_amount_positive
    CHECK (chunk_amount > 0),
  CONSTRAINT scn_ppec_row_chunk_index_unique
    UNIQUE (production_execution_row_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_scn_ppec_production_execution_id
ON sc_node_payout_production_execution_chunks (production_execution_id);

CREATE INDEX IF NOT EXISTS idx_scn_ppec_production_execution_row_id
ON sc_node_payout_production_execution_chunks (production_execution_row_id);

CREATE INDEX IF NOT EXISTS idx_scn_ppec_payout_plan_id
ON sc_node_payout_production_execution_chunks (payout_plan_id);

CREATE INDEX IF NOT EXISTS idx_scn_ppec_chunk_status
ON sc_node_payout_production_execution_chunks (chunk_status);
