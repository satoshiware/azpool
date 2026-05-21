-- SC-node payout test/regtest execution harness (PR L).
-- Fake in-memory execution tracking only — not production wallet sends.

CREATE TABLE IF NOT EXISTS sc_node_payout_test_executions (
  id BIGSERIAL PRIMARY KEY,
  payout_plan_id BIGINT NOT NULL REFERENCES sc_node_payout_plans(id),
  mode TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  planned_amount_total NUMERIC(24, 12) NOT NULL DEFAULT 0,
  test_wallet_name TEXT NOT NULL,
  txid TEXT,
  execution_attempt_count INTEGER NOT NULL DEFAULT 0,
  idempotency_key TEXT NOT NULL,
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT sc_node_payout_test_executions_mode_check
    CHECK (mode IN ('regtest', 'fake_regtest')),
  CONSTRAINT sc_node_payout_test_executions_status_check
    CHECK (status IN ('draft', 'executing', 'sent', 'confirmed', 'failed')),
  CONSTRAINT sc_node_payout_test_executions_planned_amount_non_negative
    CHECK (planned_amount_total >= 0),
  CONSTRAINT sc_node_payout_test_executions_attempt_count_non_negative
    CHECK (execution_attempt_count >= 0),
  CONSTRAINT sc_node_payout_test_executions_plan_idempotency_unique
    UNIQUE (payout_plan_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_test_executions_payout_plan_id
ON sc_node_payout_test_executions (payout_plan_id);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_test_executions_status
ON sc_node_payout_test_executions (status);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_test_executions_test_wallet_name
ON sc_node_payout_test_executions (test_wallet_name);

CREATE TABLE IF NOT EXISTS sc_node_payout_test_execution_rows (
  id BIGSERIAL PRIMARY KEY,
  test_execution_id BIGINT NOT NULL REFERENCES sc_node_payout_test_executions(id),
  payout_plan_row_id BIGINT NOT NULL REFERENCES sc_node_payout_plan_rows(id),
  sc_node_id TEXT NOT NULL REFERENCES sc_nodes(id),
  payout_address TEXT NOT NULL,
  payout_amount NUMERIC(24, 12) NOT NULL DEFAULT 0,
  row_status TEXT NOT NULL DEFAULT 'pending',
  txid TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT sc_node_payout_test_execution_rows_payout_address_not_empty
    CHECK (length(trim(payout_address)) > 0),
  CONSTRAINT sc_node_payout_test_execution_rows_payout_amount_non_negative
    CHECK (payout_amount >= 0),
  CONSTRAINT sc_node_payout_test_execution_rows_row_status_check
    CHECK (row_status IN ('pending', 'sent', 'confirmed', 'failed')),
  CONSTRAINT sc_node_payout_test_execution_rows_execution_plan_row_unique
    UNIQUE (test_execution_id, payout_plan_row_id)
);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_test_execution_rows_test_execution_id
ON sc_node_payout_test_execution_rows (test_execution_id);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_test_execution_rows_sc_node_id
ON sc_node_payout_test_execution_rows (sc_node_id);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_test_execution_rows_row_status
ON sc_node_payout_test_execution_rows (row_status);
