-- Monitoring runs remember which data they analysed (observation and outcome counts plus
-- the newest timestamps), so the worker can skip cycles when nothing changed instead of
-- storing identical reports over and over.
ALTER TABLE ops.monitoring_runs ADD COLUMN IF NOT EXISTS input_fingerprint TEXT;
