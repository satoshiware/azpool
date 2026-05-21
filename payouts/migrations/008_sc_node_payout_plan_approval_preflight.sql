-- SC-node payout plan approval + no-send preflight (PR K).
-- Review workflow only — not payout execution.

ALTER TABLE sc_node_payout_plans
  ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS approved_by TEXT,
  ADD COLUMN IF NOT EXISTS approval_note TEXT,
  ADD COLUMN IF NOT EXISTS approval_confirmation_hash TEXT,
  ADD COLUMN IF NOT EXISTS preflight_checked_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS preflight_status TEXT,
  ADD COLUMN IF NOT EXISTS preflight_note TEXT,
  ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS cancelled_by TEXT,
  ADD COLUMN IF NOT EXISTS cancellation_note TEXT;

ALTER TABLE sc_node_payout_plans
  DROP CONSTRAINT IF EXISTS sc_node_payout_plans_status_check;

ALTER TABLE sc_node_payout_plans
  ADD CONSTRAINT sc_node_payout_plans_status_check
  CHECK (status IN ('draft', 'approved', 'cancelled', 'reviewed', 'void', 'executed'));

ALTER TABLE sc_node_payout_plans
  DROP CONSTRAINT IF EXISTS sc_node_payout_plans_preflight_status_check;

ALTER TABLE sc_node_payout_plans
  ADD CONSTRAINT sc_node_payout_plans_preflight_status_check
  CHECK (
    preflight_status IS NULL
    OR preflight_status IN ('allowed', 'refused')
  );

ALTER TABLE sc_node_payout_plan_rows
  DROP CONSTRAINT IF EXISTS sc_node_payout_plan_rows_row_status_check;

ALTER TABLE sc_node_payout_plan_rows
  ADD CONSTRAINT sc_node_payout_plan_rows_row_status_check
  CHECK (row_status IN ('draft', 'approved', 'cancelled', 'reviewed', 'void', 'executed'));
