-- SC-node identity mapping for pool telemetry collector (v0.1).
-- Maps observed pool_sv2 user_identity values to sc_node_id for telemetry grouping only.

CREATE TABLE IF NOT EXISTS sc_nodes (
  id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  payout_enabled BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sc_node_identity_mappings (
  id BIGSERIAL PRIMARY KEY,
  sc_node_id TEXT NOT NULL REFERENCES sc_nodes(id),
  match_type TEXT NOT NULL CHECK (match_type IN ('exact', 'prefix', 'glob')),
  match_value TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (match_type, match_value)
);

CREATE INDEX IF NOT EXISTS idx_sc_node_identity_mappings_node
ON sc_node_identity_mappings (sc_node_id);

CREATE INDEX IF NOT EXISTS idx_sc_node_identity_mappings_active_lookup
ON sc_node_identity_mappings (status, match_type, match_value)
WHERE status = 'active';
