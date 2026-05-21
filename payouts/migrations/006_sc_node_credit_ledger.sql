-- SC-node reward credit ledger (PR I).
-- Allocates mature support-wallet rewards to SC nodes by mapped pool work — no payout execution.

CREATE TABLE IF NOT EXISTS sc_node_reward_credit_runs (
  id BIGSERIAL PRIMARY KEY,
  run_label TEXT,
  wallet_name TEXT NOT NULL,
  maturity_status TEXT NOT NULL DEFAULT 'mature',
  coverage_start TIMESTAMPTZ NOT NULL,
  coverage_end TIMESTAMPTZ NOT NULL,
  reward_event_count INTEGER NOT NULL DEFAULT 0,
  reward_amount_total NUMERIC(24, 12) NOT NULL DEFAULT 0,
  mapped_work_total NUMERIC(38, 18) NOT NULL DEFAULT 0,
  unmapped_work_total NUMERIC(38, 18) NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'draft',
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT sc_node_reward_credit_runs_maturity_status_mature
    CHECK (maturity_status = 'mature'),
  CONSTRAINT sc_node_reward_credit_runs_reward_event_count_non_negative
    CHECK (reward_event_count >= 0),
  CONSTRAINT sc_node_reward_credit_runs_reward_amount_total_non_negative
    CHECK (reward_amount_total >= 0),
  CONSTRAINT sc_node_reward_credit_runs_mapped_work_total_non_negative
    CHECK (mapped_work_total >= 0),
  CONSTRAINT sc_node_reward_credit_runs_unmapped_work_total_non_negative
    CHECK (unmapped_work_total >= 0),
  CONSTRAINT sc_node_reward_credit_runs_status_check
    CHECK (status IN ('draft', 'reviewed', 'void'))
);

CREATE TABLE IF NOT EXISTS sc_node_reward_credits (
  id BIGSERIAL PRIMARY KEY,
  credit_run_id BIGINT NOT NULL REFERENCES sc_node_reward_credit_runs(id),
  sc_node_id TEXT NOT NULL REFERENCES sc_nodes(id),
  reward_amount_total NUMERIC(24, 12) NOT NULL DEFAULT 0,
  work_delta_total NUMERIC(38, 18) NOT NULL DEFAULT 0,
  work_share NUMERIC(20, 18) NOT NULL DEFAULT 0,
  credit_amount NUMERIC(24, 12) NOT NULL DEFAULT 0,
  credit_status TEXT NOT NULL DEFAULT 'draft',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT sc_node_reward_credits_reward_amount_total_non_negative
    CHECK (reward_amount_total >= 0),
  CONSTRAINT sc_node_reward_credits_work_delta_total_non_negative
    CHECK (work_delta_total >= 0),
  CONSTRAINT sc_node_reward_credits_work_share_range
    CHECK (work_share >= 0 AND work_share <= 1),
  CONSTRAINT sc_node_reward_credits_credit_amount_non_negative
    CHECK (credit_amount >= 0),
  CONSTRAINT sc_node_reward_credits_credit_status_check
    CHECK (credit_status IN ('draft', 'reviewed', 'void')),
  CONSTRAINT sc_node_reward_credits_run_node_unique
    UNIQUE (credit_run_id, sc_node_id)
);

CREATE TABLE IF NOT EXISTS sc_node_reward_credit_run_events (
  id BIGSERIAL PRIMARY KEY,
  credit_run_id BIGINT NOT NULL REFERENCES sc_node_reward_credit_runs(id),
  reward_event_id BIGINT NOT NULL REFERENCES support_wallet_reward_events(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT sc_node_reward_credit_run_events_run_event_unique
    UNIQUE (credit_run_id, reward_event_id)
);

CREATE INDEX IF NOT EXISTS idx_sc_node_reward_credit_runs_wallet_name
ON sc_node_reward_credit_runs (wallet_name);

CREATE INDEX IF NOT EXISTS idx_sc_node_reward_credit_runs_status
ON sc_node_reward_credit_runs (status);

CREATE INDEX IF NOT EXISTS idx_sc_node_reward_credit_runs_coverage
ON sc_node_reward_credit_runs (coverage_start, coverage_end);

CREATE INDEX IF NOT EXISTS idx_sc_node_reward_credits_credit_run_id
ON sc_node_reward_credits (credit_run_id);

CREATE INDEX IF NOT EXISTS idx_sc_node_reward_credits_sc_node_id
ON sc_node_reward_credits (sc_node_id);

CREATE INDEX IF NOT EXISTS idx_sc_node_reward_credits_credit_status
ON sc_node_reward_credits (credit_status);

CREATE INDEX IF NOT EXISTS idx_sc_node_reward_credit_run_events_credit_run_id
ON sc_node_reward_credit_run_events (credit_run_id);

CREATE INDEX IF NOT EXISTS idx_sc_node_reward_credit_run_events_reward_event_id
ON sc_node_reward_credit_run_events (reward_event_id);
