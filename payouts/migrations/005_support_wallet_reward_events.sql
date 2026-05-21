-- Support-wallet reward event ledger (PR H).
-- Observes wallet listtransactions reward categories only — no payout execution.

CREATE TABLE IF NOT EXISTS support_wallet_reward_events (
  id BIGSERIAL PRIMARY KEY,
  wallet_name TEXT,
  txid TEXT NOT NULL,
  vout INTEGER,
  category TEXT,
  amount NUMERIC(24, 12) NOT NULL DEFAULT 0,
  confirmations INTEGER NOT NULL DEFAULT 0,
  blockhash TEXT,
  blockheight INTEGER,
  blockindex INTEGER,
  blocktime TIMESTAMPTZ,
  event_time TIMESTAMPTZ,
  trusted BOOLEAN,
  spendable BOOLEAN,
  generated BOOLEAN,
  immature BOOLEAN,
  abandoned BOOLEAN,
  maturity_status TEXT NOT NULL DEFAULT 'unknown',
  raw_wallet_event JSONB NOT NULL DEFAULT '{}'::jsonb,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT support_wallet_reward_events_txid_not_empty
    CHECK (length(trim(txid)) > 0),
  CONSTRAINT support_wallet_reward_events_amount_non_negative
    CHECK (amount >= 0),
  CONSTRAINT support_wallet_reward_events_confirmations_non_negative
    CHECK (confirmations >= 0),
  CONSTRAINT support_wallet_reward_events_maturity_status_check
    CHECK (maturity_status IN (
      'unknown',
      'immature',
      'mature',
      'orphaned',
      'conflicted',
      'abandoned'
    )),
  CONSTRAINT support_wallet_reward_events_wallet_tx_vout_unique
    UNIQUE (wallet_name, txid, vout)
);

CREATE INDEX IF NOT EXISTS idx_support_wallet_reward_events_txid
ON support_wallet_reward_events (txid);

CREATE INDEX IF NOT EXISTS idx_support_wallet_reward_events_wallet_name
ON support_wallet_reward_events (wallet_name);

CREATE INDEX IF NOT EXISTS idx_support_wallet_reward_events_maturity_status
ON support_wallet_reward_events (maturity_status);

CREATE INDEX IF NOT EXISTS idx_support_wallet_reward_events_confirmations
ON support_wallet_reward_events (confirmations);

CREATE INDEX IF NOT EXISTS idx_support_wallet_reward_events_blockheight
ON support_wallet_reward_events (blockheight);

CREATE INDEX IF NOT EXISTS idx_support_wallet_reward_events_last_seen_at
ON support_wallet_reward_events (last_seen_at);
