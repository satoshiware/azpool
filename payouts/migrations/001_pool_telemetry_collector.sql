-- v0.1 support-node pool telemetry collector schema.
-- Stores monitoring API snapshots and accepted-work deltas only (no payout ledger).

CREATE TABLE IF NOT EXISTS pool_instances (
  id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  monitoring_base_url TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pool_channel_snapshots (
  id BIGSERIAL PRIMARY KEY,
  pool_instance_id TEXT NOT NULL REFERENCES pool_instances(id),
  client_id BIGINT NOT NULL,
  channel_type TEXT NOT NULL CHECK (channel_type IN ('extended', 'standard')),
  channel_id BIGINT NOT NULL,
  user_identity TEXT NOT NULL,
  sc_node_id TEXT,
  shares_accepted NUMERIC NOT NULL,
  share_work_sum NUMERIC NOT NULL,
  last_share_sequence_number BIGINT,
  blocks_found NUMERIC NOT NULL DEFAULT 0,
  observed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pool_channel_snapshots_lookup
ON pool_channel_snapshots (pool_instance_id, client_id, channel_type, channel_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS pool_share_work_deltas (
  id BIGSERIAL PRIMARY KEY,
  pool_instance_id TEXT NOT NULL REFERENCES pool_instances(id),
  client_id BIGINT NOT NULL,
  channel_type TEXT NOT NULL CHECK (channel_type IN ('extended', 'standard')),
  channel_id BIGINT NOT NULL,
  user_identity TEXT NOT NULL,
  sc_node_id TEXT,
  accepted_delta NUMERIC NOT NULL,
  work_delta NUMERIC NOT NULL,
  from_sequence_number BIGINT,
  to_sequence_number BIGINT,
  observed_from TIMESTAMPTZ NOT NULL,
  observed_to TIMESTAMPTZ NOT NULL,
  reset_detected BOOLEAN NOT NULL DEFAULT false,
  idempotency_key TEXT NOT NULL UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pool_share_work_deltas_sc_node_time
ON pool_share_work_deltas (sc_node_id, observed_to);

CREATE INDEX IF NOT EXISTS idx_pool_share_work_deltas_identity_time
ON pool_share_work_deltas (user_identity, observed_to);

CREATE TABLE IF NOT EXISTS pool_collector_runs (
  id BIGSERIAL PRIMARY KEY,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  status TEXT NOT NULL,
  pools_checked INTEGER NOT NULL DEFAULT 0,
  snapshots_written INTEGER NOT NULL DEFAULT 0,
  deltas_written INTEGER NOT NULL DEFAULT 0,
  resets_detected INTEGER NOT NULL DEFAULT 0,
  error_message TEXT
);
