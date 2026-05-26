-- Chunked SC-node payout post-execution reconciliation (PR T).
-- Read-only audit; compares chunk txids to source gettransaction and receiver JSON.

CREATE TABLE IF NOT EXISTS sc_node_chunked_payout_reconciliations (
  id BIGSERIAL PRIMARY KEY,
  production_execution_id BIGINT NOT NULL
    REFERENCES sc_node_payout_production_executions(id),
  payout_plan_id BIGINT NOT NULL,
  sc_node_id TEXT NOT NULL,
  payout_address TEXT NOT NULL,
  expected_chunk_count INTEGER NOT NULL,
  source_chunk_count INTEGER NOT NULL,
  receiver_chunk_count INTEGER,
  expected_amount_total NUMERIC(24, 12) NOT NULL,
  source_amount_total NUMERIC(24, 12) NOT NULL,
  source_fee_total NUMERIC(24, 12),
  receiver_amount_total NUMERIC(24, 12),
  reconciliation_status TEXT NOT NULL,
  matched BOOLEAN NOT NULL DEFAULT false,
  refusal_reason TEXT,
  source_wallet_name TEXT NOT NULL,
  source_wallet_evidence JSONB,
  receiver_wallet_evidence JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT scn_cpr_stat_chk
    CHECK (reconciliation_status IN ('matched', 'mismatch', 'source_only')),
  CONSTRAINT scn_cpr_exp_chunk_cnt_nonneg
    CHECK (expected_chunk_count >= 0),
  CONSTRAINT scn_cpr_src_chunk_cnt_nonneg
    CHECK (source_chunk_count >= 0),
  CONSTRAINT scn_cpr_rcv_chunk_cnt_nonneg
    CHECK (receiver_chunk_count IS NULL OR receiver_chunk_count >= 0),
  CONSTRAINT scn_cpr_exp_amt_nonneg
    CHECK (expected_amount_total >= 0),
  CONSTRAINT scn_cpr_src_amt_nonneg
    CHECK (source_amount_total >= 0),
  CONSTRAINT scn_cpr_exec_uniq
    UNIQUE (production_execution_id)
);

CREATE INDEX IF NOT EXISTS idx_scn_cpr_prod_exec_id
ON sc_node_chunked_payout_reconciliations (production_execution_id);

CREATE INDEX IF NOT EXISTS idx_scn_cpr_payout_plan_id
ON sc_node_chunked_payout_reconciliations (payout_plan_id);

CREATE INDEX IF NOT EXISTS idx_scn_cpr_status
ON sc_node_chunked_payout_reconciliations (reconciliation_status);

CREATE INDEX IF NOT EXISTS idx_scn_cpr_matched
ON sc_node_chunked_payout_reconciliations (matched);

CREATE TABLE IF NOT EXISTS sc_node_chunked_payout_reconciliation_chunks (
  id BIGSERIAL PRIMARY KEY,
  reconciliation_id BIGINT NOT NULL
    REFERENCES sc_node_chunked_payout_reconciliations(id),
  production_execution_chunk_id BIGINT NOT NULL
    REFERENCES sc_node_payout_production_execution_chunks(id),
  chunk_index INTEGER NOT NULL,
  txid TEXT NOT NULL,
  expected_amount NUMERIC(24, 12) NOT NULL,
  source_amount NUMERIC(24, 12),
  source_fee NUMERIC(24, 12),
  source_confirmations INTEGER,
  source_blockhash TEXT,
  receiver_amount NUMERIC(24, 12),
  receiver_address TEXT,
  receiver_confirmations INTEGER,
  receiver_category TEXT,
  row_status TEXT NOT NULL,
  refusal_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT scn_cprc_stat_chk
    CHECK (row_status IN ('matched', 'mismatch', 'source_only')),
  CONSTRAINT scn_cprc_exp_amt_pos
    CHECK (expected_amount > 0),
  CONSTRAINT scn_cprc_src_conf_nonneg
    CHECK (source_confirmations IS NULL OR source_confirmations >= 0),
  CONSTRAINT scn_cprc_rcv_conf_nonneg
    CHECK (receiver_confirmations IS NULL OR receiver_confirmations >= 0),
  CONSTRAINT scn_cprc_rec_chunk_uniq
    UNIQUE (reconciliation_id, chunk_index),
  CONSTRAINT scn_cprc_rec_txid_uniq
    UNIQUE (reconciliation_id, txid)
);

CREATE INDEX IF NOT EXISTS idx_scn_cprc_reconciliation_id
ON sc_node_chunked_payout_reconciliation_chunks (reconciliation_id);

CREATE INDEX IF NOT EXISTS idx_scn_cprc_prod_exec_chunk_id
ON sc_node_chunked_payout_reconciliation_chunks (production_execution_chunk_id);

CREATE INDEX IF NOT EXISTS idx_scn_cprc_row_status
ON sc_node_chunked_payout_reconciliation_chunks (row_status);
