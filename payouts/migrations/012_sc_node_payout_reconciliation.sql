-- SC-node payout post-execution reconciliation (PR O).
-- Read-only audit layer after confirmed production execution; no sends.

CREATE TABLE IF NOT EXISTS sc_node_payout_reconciliations (
  id BIGSERIAL PRIMARY KEY,
  production_execution_id BIGINT NOT NULL,
  payout_plan_id BIGINT NOT NULL,
  source_wallet_name TEXT NOT NULL,
  txid TEXT NOT NULL,
  reconciliation_status TEXT NOT NULL DEFAULT 'draft',
  expected_amount NUMERIC(24, 12) NOT NULL DEFAULT 0,
  expected_address TEXT NOT NULL,
  source_confirmations INTEGER,
  source_fee NUMERIC(24, 12),
  source_amount NUMERIC(24, 12),
  receiver_confirmations INTEGER,
  receiver_amount NUMERIC(24, 12),
  receiver_category TEXT,
  receiver_address TEXT,
  matched BOOLEAN NOT NULL DEFAULT false,
  mismatch_reason TEXT,
  source_wallet_evidence JSONB,
  receiver_wallet_evidence JSONB,
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT scn_ppr_reconciliation_status_check
    CHECK (reconciliation_status IN ('draft', 'matched', 'mismatch', 'void')),
  CONSTRAINT scn_ppr_expected_amount_nonneg
    CHECK (expected_amount >= 0),
  CONSTRAINT scn_ppr_source_confirmations_nonneg
    CHECK (source_confirmations IS NULL OR source_confirmations >= 0),
  CONSTRAINT scn_ppr_receiver_confirmations_nonneg
    CHECK (receiver_confirmations IS NULL OR receiver_confirmations >= 0),
  CONSTRAINT scn_ppr_execution_txid_unique
    UNIQUE (production_execution_id, txid)
);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_reconciliations_production_execution_id
ON sc_node_payout_reconciliations (production_execution_id);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_reconciliations_payout_plan_id
ON sc_node_payout_reconciliations (payout_plan_id);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_reconciliations_txid
ON sc_node_payout_reconciliations (txid);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_reconciliations_reconciliation_status
ON sc_node_payout_reconciliations (reconciliation_status);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_reconciliations_matched
ON sc_node_payout_reconciliations (matched);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_reconciliations_created_at
ON sc_node_payout_reconciliations (created_at);

CREATE TABLE IF NOT EXISTS sc_node_payout_reconciliation_rows (
  id BIGSERIAL PRIMARY KEY,
  reconciliation_id BIGINT NOT NULL
    REFERENCES sc_node_payout_reconciliations(id),
  production_execution_row_id BIGINT NOT NULL,
  sc_node_id TEXT NOT NULL,
  expected_address TEXT NOT NULL,
  expected_amount NUMERIC(24, 12) NOT NULL DEFAULT 0,
  receiver_address TEXT,
  receiver_amount NUMERIC(24, 12),
  receiver_category TEXT,
  receiver_confirmations INTEGER,
  row_status TEXT NOT NULL DEFAULT 'draft',
  mismatch_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT scn_pprr_row_status_check
    CHECK (row_status IN ('draft', 'matched', 'mismatch', 'void')),
  CONSTRAINT scn_pprr_expected_amount_nonneg
    CHECK (expected_amount >= 0),
  CONSTRAINT scn_pprr_receiver_confirmations_nonneg
    CHECK (receiver_confirmations IS NULL OR receiver_confirmations >= 0),
  CONSTRAINT scn_pprr_reconciliation_execution_row_unique
    UNIQUE (reconciliation_id, production_execution_row_id)
);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_reconciliation_rows_reconciliation_id
ON sc_node_payout_reconciliation_rows (reconciliation_id);

CREATE INDEX IF NOT EXISTS idx_scn_pprr_prod_exec_row_id
ON sc_node_payout_reconciliation_rows (production_execution_row_id);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_reconciliation_rows_sc_node_id
ON sc_node_payout_reconciliation_rows (sc_node_id);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_reconciliation_rows_row_status
ON sc_node_payout_reconciliation_rows (row_status);
