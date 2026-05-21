-- SC-node payout address registry (v0.1).
-- Stores payout destination addresses per SC node. Registry only — no payout execution.

CREATE TABLE IF NOT EXISTS sc_node_payout_addresses (
  id BIGSERIAL PRIMARY KEY,
  sc_node_id TEXT NOT NULL REFERENCES sc_nodes(id) ON UPDATE CASCADE ON DELETE RESTRICT,
  payout_address TEXT NOT NULL,
  label TEXT,
  address_source TEXT NOT NULL DEFAULT 'manual',
  status TEXT NOT NULL DEFAULT 'pending_verification',
  is_default BOOLEAN NOT NULL DEFAULT false,
  verified_at TIMESTAMPTZ,
  retired_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT sc_node_payout_addresses_payout_address_not_empty
    CHECK (length(trim(payout_address)) > 0),
  CONSTRAINT sc_node_payout_addresses_status_check
    CHECK (status IN ('pending_verification', 'active', 'inactive', 'revoked')),
  CONSTRAINT sc_node_payout_addresses_address_source_check
    CHECK (address_source IN ('manual', 'imported', 'wallet', 'api')),
  CONSTRAINT sc_node_payout_addresses_payout_address_unique UNIQUE (payout_address)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sc_node_payout_addresses_one_active_default
ON sc_node_payout_addresses (sc_node_id)
WHERE is_default = true AND status = 'active';

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_addresses_sc_node_id
ON sc_node_payout_addresses (sc_node_id);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_addresses_status
ON sc_node_payout_addresses (status);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_addresses_sc_node_status
ON sc_node_payout_addresses (sc_node_id, status);

CREATE INDEX IF NOT EXISTS idx_sc_node_payout_addresses_default_active
ON sc_node_payout_addresses (sc_node_id, is_default)
WHERE status = 'active';
