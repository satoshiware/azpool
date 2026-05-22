-- SC-node production payout execution (PR N).
-- First real wallet send path (sendtoaddress only); manual operator-triggered.

CREATE TABLE IF NOT EXISTS sc_node_payout_production_executions (
  id BIGSERIAL PRIMARY KEY,
  payout_plan_id BIGINT NOT NULL REFERENCES sc_node_payout_plans(id),
  production_preflight_id BIGINT NOT NULL
    REFERENCES sc_node_payout_production_preflights(id),
  source_wallet_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  planned_amount_total NUMERIC(24, 12) NOT NULL DEFAULT 0,
  trusted_balance_before NUMERIC(24, 12) NOT NULL DEFAULT 0,
  immature_balance_before NUMERIC(24, 12) NOT NULL DEFAULT 0,
  reserve_amount NUMERIC(24, 12) NOT NULL DEFAULT 0,
  spendable_after_reserve NUMERIC(24, 12) NOT NULL DEFAULT 0,
  execution_attempt_count INTEGER NOT NULL DEFAULT 0,
  idempotency_key TEXT NOT NULL,
  confirmation_phrase TEXT NOT NULL,
  txid TEXT,
  refusal_reason TEXT,
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT scn_ppe_status_check
    CHECK (status IN ('draft', 'sent', 'confirmed', 'refused', 'void')),
  CONSTRAINT scn_ppe_planned_amount_nonneg
    CHECK (planned_amount_total >= 0),
  CONSTRAINT scn_ppe_trusted_balance_before_nonneg
    CHECK (trusted_balance_before >= 0),
  CONSTRAINT scn_ppe_immature_balance_before_nonneg
    CHECK (immature_balance_before >= 0),
  CONSTRAINT scn_ppe_reserve_amount_nonneg
    CHECK (reserve_amount >= 0),
  CONSTRAINT scn_ppe_spendable_after_reserve_nonneg
    CHECK (spendable_after_reserve >= 0),
  CONSTRAINT scn_ppe_execution_attempt_count_nonneg
    CHECK (execution_attempt_count >= 0),
  CONSTRAINT scn_ppe_plan_idempotency_unique
    UNIQUE (payout_plan_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_production_executions_payout_plan_id
ON sc_node_payout_production_executions (payout_plan_id);

CREATE INDEX IF NOT EXISTS idx_scn_ppe_preflight_id
ON sc_node_payout_production_executions (production_preflight_id);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_production_executions_status
ON sc_node_payout_production_executions (status);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_production_executions_source_wallet_name
ON sc_node_payout_production_executions (source_wallet_name);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_production_executions_created_at
ON sc_node_payout_production_executions (created_at);

CREATE TABLE IF NOT EXISTS sc_node_payout_production_execution_rows (
  id BIGSERIAL PRIMARY KEY,
  production_execution_id BIGINT NOT NULL
    REFERENCES sc_node_payout_production_executions(id),
  payout_plan_row_id BIGINT NOT NULL REFERENCES sc_node_payout_plan_rows(id),
  sc_node_id TEXT NOT NULL REFERENCES sc_nodes(id),
  payout_address TEXT NOT NULL,
  payout_amount NUMERIC(24, 12) NOT NULL DEFAULT 0,
  row_status TEXT NOT NULL DEFAULT 'draft',
  txid TEXT,
  refusal_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT scn_pper_payout_amount_nonneg
    CHECK (payout_amount >= 0),
  CONSTRAINT scn_pper_row_status_check
    CHECK (row_status IN ('draft', 'sent', 'confirmed', 'refused', 'void')),
  CONSTRAINT scn_pper_execution_plan_row_unique
    UNIQUE (production_execution_id, payout_plan_row_id)
);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_production_execution_rows_execution_id
ON sc_node_payout_production_execution_rows (production_execution_id);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_production_execution_rows_plan_row_id
ON sc_node_payout_production_execution_rows (payout_plan_row_id);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_production_execution_rows_sc_node_id
ON sc_node_payout_production_execution_rows (sc_node_id);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_production_execution_rows_row_status
ON sc_node_payout_production_execution_rows (row_status);
