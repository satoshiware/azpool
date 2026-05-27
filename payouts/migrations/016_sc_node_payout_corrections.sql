-- SC-node payout corrections / offsets (PR AB).
-- Audited accounting adjustments only — not wallet transactions.

CREATE TABLE IF NOT EXISTS sc_node_payout_corrections (
  id BIGSERIAL PRIMARY KEY,
  sc_node_id TEXT NOT NULL REFERENCES sc_nodes(id),
  wallet_name TEXT NOT NULL,
  amount NUMERIC(24, 12) NOT NULL,
  direction TEXT NOT NULL DEFAULT 'offset_debit',
  reason_code TEXT NOT NULL,
  notes TEXT,
  related_credit_run_id BIGINT REFERENCES sc_node_reward_credit_runs(id),
  related_payout_plan_id BIGINT REFERENCES sc_node_payout_plans(id),
  related_reward_event_id BIGINT,
  related_txid TEXT,
  status TEXT NOT NULL DEFAULT 'draft',
  created_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  applied_at TIMESTAMPTZ,
  cancelled_at TIMESTAMPTZ,
  CONSTRAINT sc_node_payout_corrections_amount_positive
    CHECK (amount > 0),
  CONSTRAINT sc_node_payout_corrections_direction_check
    CHECK (direction IN ('offset_debit')),
  CONSTRAINT sc_node_payout_corrections_status_check
    CHECK (status IN ('draft', 'applied', 'cancelled')),
  CONSTRAINT sc_node_payout_corrections_reason_code_not_empty
    CHECK (length(trim(reason_code)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_corrections_sc_node_id
ON sc_node_payout_corrections (sc_node_id);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_corrections_wallet_name
ON sc_node_payout_corrections (wallet_name);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_corrections_status
ON sc_node_payout_corrections (status);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_corrections_related_credit_run_id
ON sc_node_payout_corrections (related_credit_run_id)
WHERE related_credit_run_id IS NOT NULL;

ALTER TABLE sc_node_payout_plans
  ADD COLUMN IF NOT EXISTS payout_correction_id BIGINT
    REFERENCES sc_node_payout_corrections(id);

ALTER TABLE sc_node_payout_plan_rows
  ADD COLUMN IF NOT EXISTS gross_credit_amount NUMERIC(24, 12),
  ADD COLUMN IF NOT EXISTS correction_amount NUMERIC(24, 12) NOT NULL DEFAULT 0;

UPDATE sc_node_payout_plan_rows
SET gross_credit_amount = payout_amount
WHERE gross_credit_amount IS NULL;

ALTER TABLE sc_node_payout_plan_rows
  ALTER COLUMN gross_credit_amount SET NOT NULL;

ALTER TABLE sc_node_payout_plan_rows
  ADD CONSTRAINT sc_node_payout_plan_rows_correction_amount_non_negative
    CHECK (correction_amount >= 0);

ALTER TABLE sc_node_payout_plan_rows
  ADD CONSTRAINT sc_node_payout_plan_rows_net_amount_consistent
    CHECK (payout_amount = gross_credit_amount - correction_amount);
