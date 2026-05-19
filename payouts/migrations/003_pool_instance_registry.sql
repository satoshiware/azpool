-- Extend pool_instances for DB-backed collector registry (v0.1).
-- Uses existing monitoring_base_url column; does not seed operational pool rows.

ALTER TABLE pool_instances ADD COLUMN IF NOT EXISTS monitoring_enabled BOOLEAN NOT NULL DEFAULT true;

ALTER TABLE pool_instances DROP CONSTRAINT IF EXISTS pool_instances_status_check;
ALTER TABLE pool_instances ADD CONSTRAINT pool_instances_status_check
  CHECK (status IN ('active', 'inactive'));

CREATE INDEX IF NOT EXISTS idx_pool_instances_active_monitoring
ON pool_instances (status, monitoring_enabled)
WHERE status = 'active' AND monitoring_enabled = true;
