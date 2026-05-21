-- Add retired_at for SC-node payout address lifecycle (idempotent).
-- Safe when 004 was applied before retired_at existed.

ALTER TABLE sc_node_payout_addresses
  ADD COLUMN IF NOT EXISTS retired_at TIMESTAMPTZ;
