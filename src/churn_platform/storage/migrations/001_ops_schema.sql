-- Operations database (churn_ops). MLflow keeps its own database; this one holds only data
-- that MLflow does not own: prediction observations, monitoring results, retraining requests
-- and an audit trail of lifecycle decisions. Model versions and aliases are NOT stored here:
-- the MLflow registry is their single source of truth.

CREATE SCHEMA IF NOT EXISTS ops;
-- Stand-in for the external billing/CRM system that reports real cancellations later.
CREATE SCHEMA IF NOT EXISTS simulation;

-- One row per served prediction. The feature snapshot is stored exactly as received so
-- monitoring and delayed evaluation see what the model saw. Outcome columns stay NULL until
-- the churn outcome becomes known (delayed ground truth).
CREATE TABLE IF NOT EXISTS ops.prediction_observations (
    prediction_id       UUID PRIMARY KEY,
    predicted_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    customer_id         TEXT NOT NULL,
    snapshot_date       DATE NOT NULL,
    features            JSONB NOT NULL,
    churn_probability   DOUBLE PRECISION NOT NULL CHECK (churn_probability BETWEEN 0 AND 1),
    churn_prediction    SMALLINT NOT NULL CHECK (churn_prediction IN (0, 1)),
    risk_level          TEXT NOT NULL,
    model_name          TEXT NOT NULL,
    model_version       TEXT NOT NULL,
    model_run_id        TEXT NOT NULL,
    actual_churn        SMALLINT CHECK (actual_churn IN (0, 1)),
    outcome_resolved_at TIMESTAMPTZ,
    -- An outcome and its resolution time always arrive together.
    CONSTRAINT outcome_complete CHECK ((actual_churn IS NULL) = (outcome_resolved_at IS NULL))
);
CREATE INDEX IF NOT EXISTS prediction_observations_model_time
    ON ops.prediction_observations (model_name, model_version, predicted_at);
CREATE INDEX IF NOT EXISTS prediction_observations_unresolved
    ON ops.prediction_observations (snapshot_date) WHERE actual_churn IS NULL;

-- Summary of each monitoring run (the full Evidently reports are MLflow artifacts).
CREATE TABLE IF NOT EXISTS ops.monitoring_runs (
    monitoring_run_id       UUID PRIMARY KEY,
    started_at              TIMESTAMPTZ NOT NULL,
    finished_at             TIMESTAMPTZ NOT NULL,
    model_name              TEXT NOT NULL,
    model_version           TEXT NOT NULL,
    reference_run_id        TEXT NOT NULL,
    reference_dataset       TEXT NOT NULL,
    observation_count       INTEGER NOT NULL,
    window_start            TIMESTAMPTZ,
    window_end              TIMESTAMPTZ,
    data_quality            JSONB NOT NULL,
    dataset_drift           BOOLEAN,
    drift_share             DOUBLE PRECISION,
    drifted_features        JSONB NOT NULL DEFAULT '[]',
    feature_drift_scores    JSONB NOT NULL DEFAULT '{}',
    prediction_drift        BOOLEAN,
    prediction_drift_score  DOUBLE PRECISION,
    labeled_count           INTEGER NOT NULL DEFAULT 0,
    performance             JSONB,
    reference_performance   JSONB,
    policy_decision         TEXT NOT NULL,
    policy_reasons          JSONB NOT NULL DEFAULT '[]',
    retraining_request_id   UUID,
    report_run_id           TEXT
);
CREATE INDEX IF NOT EXISTS monitoring_runs_model_time ON ops.monitoring_runs (model_name, finished_at);

-- Retraining requests created by the monitoring policy (or an operator) and processed by
-- the retraining controller. Monitoring only ever inserts; it never trains or deploys.
CREATE TABLE IF NOT EXISTS ops.retraining_requests (
    request_id              UUID PRIMARY KEY,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    model_name              TEXT NOT NULL,
    trigger                 TEXT NOT NULL CHECK (trigger IN ('policy', 'manual')),
    reasons                 JSONB NOT NULL DEFAULT '[]',
    monitoring_run_id       UUID,
    data_as_of              DATE NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    attempts                INTEGER NOT NULL DEFAULT 0,
    started_at              TIMESTAMPTZ,
    finished_at             TIMESTAMPTZ,
    outcome                 TEXT,
    candidate_version       TEXT,
    champion_version_before TEXT,
    champion_version_after  TEXT,
    error                   TEXT
);
-- At most one open request per model: repeated drift signals cannot pile up retraining work.
CREATE UNIQUE INDEX IF NOT EXISTS retraining_requests_one_open
    ON ops.retraining_requests (model_name) WHERE status IN ('pending', 'running');

-- Append-only audit trail of registry decisions (who changed which alias, when and why).
-- Never read to make decisions: current state always comes from the MLflow registry.
CREATE TABLE IF NOT EXISTS ops.lifecycle_events (
    event_id      BIGSERIAL PRIMARY KEY,
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    model_name    TEXT NOT NULL,
    model_version TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    actor         TEXT NOT NULL,
    details       JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS lifecycle_events_model ON ops.lifecycle_events (model_name, occurred_at);

-- Hidden true outcomes of simulated production customers, written by the traffic simulator
-- and revealed only by the delayed ground-truth workflow.
CREATE TABLE IF NOT EXISTS simulation.customer_outcomes (
    customer_id   TEXT NOT NULL,
    snapshot_date DATE NOT NULL,
    churned       SMALLINT NOT NULL CHECK (churned IN (0, 1)),
    profile       TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (customer_id, snapshot_date)
);

-- Least privilege for the other database roles (created by the PostgreSQL init script):
-- the inference service may only insert predictions; Grafana may only read ops tables.
GRANT USAGE ON SCHEMA ops TO churn_inference, grafana_reader;
GRANT INSERT ON ops.prediction_observations TO churn_inference;
GRANT SELECT ON ALL TABLES IN SCHEMA ops TO grafana_reader;
