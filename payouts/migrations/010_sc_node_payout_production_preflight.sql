-- SC-node production payout preflight audit (PR M).
-- Read-only wallet balance checks; no sends or production execution.

CREATE TABLE IF NOT EXISTS sc_node_payout_production_preflights (
  id BIGSERIAL PRIMARY KEY,
  payout_plan_id BIGINT NOT NULL REFERENCES sc_node_payout_plans(id),
  source_wallet_name TEXT NOT NULL,
  preflight_status TEXT NOT NULL DEFAULT 'draft',
  execution_allowed BOOLEAN NOT NULL DEFAULT false,
  refusal_reason TEXT,
  trusted_balance NUMERIC(24, 12) NOT NULL DEFAULT 0,
  immature_balance NUMERIC(24, 12) NOT NULL DEFAULT 0,
  planned_amount_total NUMERIC(24, 12) NOT NULL DEFAULT 0,
  reserve_mode TEXT NOT NULL DEFAULT 'percent',
  reserve_percent NUMERIC(8, 6) NOT NULL DEFAULT 0.500000,
  reserve_amount NUMERIC(24, 12) NOT NULL DEFAULT 0,
  spendable_after_reserve NUMERIC(24, 12) NOT NULL DEFAULT 0,
  max_spend_percent NUMERIC(8, 6) NOT NULL DEFAULT 0.500000,
  operator_override BOOLEAN NOT NULL DEFAULT false,
  wallet_balance_source TEXT NOT NULL DEFAULT 'azc_getbalances',
  idempotency_key TEXT,
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT scn_pppf_status_check
    CHECK (preflight_status IN ('draft', 'passed', 'refused', 'void')),
  CONSTRAINT scn_pppf_reserve_mode_check
    CHECK (reserve_mode IN ('percent', 'amount')),
  CONSTRAINT scn_pppf_trusted_balance_nonneg
    CHECK (trusted_balance >= 0),
  CONSTRAINT scn_pppf_immature_balance_nonneg
    CHECK (immature_balance >= 0),
  CONSTRAINT scn_pppf_planned_amount_nonneg
    CHECK (planned_amount_total >= 0),
  CONSTRAINT scn_pppf_reserve_percent_range
    CHECK (reserve_percent >= 0 AND reserve_percent <= 1),
  CONSTRAINT scn_pppf_reserve_amount_nonneg
    CHECK (reserve_amount >= 0),
  CONSTRAINT scn_pppf_spendable_nonneg
    CHECK (spendable_after_reserve >= 0),
  CONSTRAINT scn_pppf_max_spend_percent_range
    CHECK (max_spend_percent >= 0 AND max_spend_percent <= 1),
  CONSTRAINT scn_pppf_plan_idempotency_unique
    UNIQUE (payout_plan_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_production_preflights_payout_plan_id
ON sc_node_payout_production_preflights (payout_plan_id);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_production_preflights_preflight_status
ON sc_node_payout_production_preflights (preflight_status);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_production_preflights_execution_allowed
ON sc_node_payout_production_preflights (execution_allowed);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_production_preflights_source_wallet_name
ON sc_node_payout_production_preflights (source_wallet_name);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_production_preflights_created_at
ON sc_node_payout_production_preflights (created_at);

CREATE TABLE IF NOT EXISTS sc_node_payout_production_preflight_rows (
  id BIGSERIAL PRIMARY KEY,
  production_preflight_id BIGINT NOT NULL
    REFERENCES sc_node_payout_production_preflights(id),
  payout_plan_row_id BIGINT NOT NULL REFERENCES sc_node_payout_plan_rows(id),
  sc_node_id TEXT NOT NULL REFERENCES sc_nodes(id),
  payout_address TEXT NOT NULL,
  payout_amount NUMERIC(24, 12) NOT NULL DEFAULT 0,
  row_status TEXT NOT NULL DEFAULT 'checked',
  refusal_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT scn_pppf_rows_payout_amount_nonneg
    CHECK (payout_amount >= 0),
  CONSTRAINT scn_pppf_rows_row_status_check
    CHECK (row_status IN ('checked', 'refused')),
  CONSTRAINT scn_pppf_rows_preflight_plan_unique
    UNIQUE (production_preflight_id, payout_plan_row_id)
);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_production_preflight_rows_preflight_id
ON sc_node_payout_production_preflight_rows (production_preflight_id);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_production_preflight_rows_plan_row_id
ON sc_node_payout_production_preflight_rows (payout_plan_row_id);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_production_preflight_rows_sc_node_id
ON sc_node_payout_production_preflight_rows (sc_node_id);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_production_preflight_rows_row_status
ON sc_node_payout_production_preflight_rows (row_status);
