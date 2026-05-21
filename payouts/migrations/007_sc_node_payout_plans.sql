-- SC-node payout plans (PR J).
-- No-send payout proposals from draft credits — not wallet transactions.

CREATE TABLE IF NOT EXISTS sc_node_payout_plans (
  id BIGSERIAL PRIMARY KEY,
  credit_run_id BIGINT NOT NULL REFERENCES sc_node_reward_credit_runs(id),
  wallet_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  reserve_fraction NUMERIC(5, 4) NOT NULL DEFAULT 0.5000,
  trusted_balance_snapshot NUMERIC(24, 12) NOT NULL,
  reserve_amount NUMERIC(24, 12) NOT NULL DEFAULT 0,
  max_spendable_amount NUMERIC(24, 12) NOT NULL DEFAULT 0,
  planned_amount_total NUMERIC(24, 12) NOT NULL DEFAULT 0,
  row_count INTEGER NOT NULL DEFAULT 0,
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT sc_node_payout_plans_status_check
    CHECK (status IN ('draft', 'reviewed', 'void', 'executed')),
  CONSTRAINT sc_node_payout_plans_reserve_fraction_range
    CHECK (reserve_fraction >= 0 AND reserve_fraction <= 1),
  CONSTRAINT sc_node_payout_plans_trusted_balance_non_negative
    CHECK (trusted_balance_snapshot >= 0),
  CONSTRAINT sc_node_payout_plans_reserve_amount_non_negative
    CHECK (reserve_amount >= 0),
  CONSTRAINT sc_node_payout_plans_max_spendable_non_negative
    CHECK (max_spendable_amount >= 0),
  CONSTRAINT sc_node_payout_plans_planned_amount_non_negative
    CHECK (planned_amount_total >= 0),
  CONSTRAINT sc_node_payout_plans_row_count_non_negative
    CHECK (row_count >= 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sc_node_payout_plans_one_draft_per_credit_run
ON sc_node_payout_plans (credit_run_id)
WHERE status = 'draft';

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_plans_credit_run_id
ON sc_node_payout_plans (credit_run_id);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_plans_wallet_name
ON sc_node_payout_plans (wallet_name);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_plans_status
ON sc_node_payout_plans (status);

CREATE TABLE IF NOT EXISTS sc_node_payout_plan_rows (
  id BIGSERIAL PRIMARY KEY,
  payout_plan_id BIGINT NOT NULL REFERENCES sc_node_payout_plans(id),
  credit_id BIGINT NOT NULL REFERENCES sc_node_reward_credits(id),
  sc_node_id TEXT NOT NULL REFERENCES sc_nodes(id),
  sc_node_display_name TEXT,
  payout_address TEXT NOT NULL,
  payout_amount NUMERIC(24, 12) NOT NULL DEFAULT 0,
  row_status TEXT NOT NULL DEFAULT 'draft',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT sc_node_payout_plan_rows_payout_address_not_empty
    CHECK (length(trim(payout_address)) > 0),
  CONSTRAINT sc_node_payout_plan_rows_payout_amount_non_negative
    CHECK (payout_amount >= 0),
  CONSTRAINT sc_node_payout_plan_rows_row_status_check
    CHECK (row_status IN ('draft', 'reviewed', 'void', 'executed')),
  CONSTRAINT sc_node_payout_plan_rows_plan_credit_unique
    UNIQUE (payout_plan_id, credit_id)
);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_plan_rows_payout_plan_id
ON sc_node_payout_plan_rows (payout_plan_id);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_plan_rows_sc_node_id
ON sc_node_payout_plan_rows (sc_node_id);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_plan_rows_row_status
ON sc_node_payout_plan_rows (row_status);
