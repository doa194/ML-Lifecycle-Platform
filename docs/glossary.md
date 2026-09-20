# Glossary

> **In short:** Plain-language definitions of the terms used throughout the documentation,
> grouped by topic. Where it helps, each entry says where the concept appears in this
> platform.

## Contents

- [Business and data](#business-and-data)
- [Machine learning and metrics](#machine-learning-and-metrics)
- [Model lifecycle](#model-lifecycle)
- [Monitoring and drift](#monitoring-and-drift)
- [Infrastructure and tools](#infrastructure-and-tools)
- [Engineering terms](#engineering-terms)

---

## Business and data

**Churn** - A customer cancelling their subscription. Here: *voluntary* churn, where the
customer decides to leave, within **30 days** after the snapshot date. Stored as the label
`churned_30d` (1 = churned, 0 = stayed).

**Snapshot** - Everything known about one customer on one specific date (the *snapshot
date*). One training row and one prediction request are both snapshots.

**Feature** - One input fact the model uses, for example `tenure_months` or
`payment_failures_90d`. The platform uses 16 features plus 4 derived ones.

**Label (target)** - The answer the model learns to predict: `churned_30d`.

**Outcome window** - The 30 days after a snapshot that the label describes. The true
outcome is only known once this window has passed.

**As-of date** - The date a dataset is extracted (`dataset.as_of` in `params.yaml`). A
dataset as of date D contains only snapshots whose outcome window closed by D.

**Profile** - A named set of rules for generating customers (`baseline`, `pricing_shift`,
`engagement_shift`). See [synthetic-world.md](synthetic-world.md).

**Timeline** - The configuration that says which profile applies from which date. It is how
the simulated world "changes".

**Segment** - A group of customers evaluated separately: by plan tier, by contract type,
and new (tenure below 6 months) vs. established customers.

---

## Machine learning and metrics

**Training / validation / test period** - Three slices of a dataset ordered by time. The
model learns from the training period, its settings are tuned on the validation period, and
its final quality is measured once on the untouched test period (the newest data).

**Purge** - Removing rows at the boundary between two periods whose outcome window would
reach into the next period. Prevents the training labels from describing the validation
period. See [leakage-controls.md](leakage-controls.md).

**Leakage** - When a model gets information during training that it will not have in
production (for example a "cancellation date" column, or data from the future). It makes the
model look better than it is.

**Probability / decision threshold** - The model outputs a churn probability between 0 and
1. The *decision threshold* is the value at or above which a customer counts as "will
churn". Each model gets its own threshold, chosen to maximise F1 on the validation period.

**Risk level** - A business-friendly label: `high` exactly when the model predicts churn,
`medium` above 60% of the threshold, otherwise `low`.

**Precision** - Of the customers predicted to churn, the share that really churned. "How
often is a churn warning right?"

**Recall** - Of the customers who really churned, the share the model caught. "How many
churners do we find?"

**F1 score** - A single number combining precision and recall (their harmonic mean). The
primary metric in this project. F1 depends on how common churn is: when churn becomes more
frequent, F1 tends to rise even for the same model.

**ROC-AUC** - The probability that the model ranks a randomly chosen churner above a
randomly chosen non-churner. 0.5 = guessing, 1.0 = perfect ranking. Independent of the
threshold and of how common churn is.

**Accuracy** - The share of all predictions that are correct. Misleading here: a model that
never predicts churn would score about 87%. Recorded but never used for decisions.

**No-skill baseline** - The score of a model that ignores the input (predicts "churn" for
everyone). Used to prove that a trained model learned something.

**Latency (p50, p95)** - Time to answer one prediction. p95 means 95% of predictions were
faster than this value.

**Pipeline (scikit-learn)** - One object chaining feature engineering, preprocessing and the
model, so that all steps are saved and applied together.

**Training cycle** - One run of the `train` stage: all configured algorithms are trained and
one winner is chosen.

---

## Model lifecycle

**Experiment / run** - In MLflow, an *experiment* groups related *runs*; a run is one
recorded training (parameters, metrics, files, tags).

**Lineage** - The recorded chain from a model version back to its run, dataset hashes,
dataset date, configuration and Git commit.

**Model registry** - The catalogue of approved model versions (MLflow Model Registry). Each
registered model has numbered versions.

**Alias** - A movable name pointing at one model version. This platform uses three:
`candidate`, `challenger`, `champion`. Moving an alias changes a model's role instantly
without copying files.

**Candidate** - The winner of the latest training cycle, registered but not yet checked.

**Challenger** - A candidate that passed the quality gate and may replace the champion.

**Champion** - The approved production model. The inference API loads it.

**Quality gate** - The set of checks a candidate must pass before it may become challenger.
See [quality-gate.md](quality-gate.md).

**Promotion** - Moving the `champion` alias to a challenger.

**Rollback** - Moving the `champion` alias back to the previous approved champion.

**Lifecycle status** - A tag on each version recording where it is in its life:
`candidate`, `challenger`, `champion`, `rejected`, `retired`, `rolled_back`.

**Stale challenger** - A challenger whose gate comparison was made against a champion that
is no longer the champion. It must be re-checked before promotion.

**Audit trail** - The append-only log of lifecycle decisions (`ops.lifecycle_events`): who
registered, gated, promoted or rolled back which version, when and why.

**Fingerprint** - A SHA-256 checksum over all files of a model. Recorded when the model is
trained and checked on every load; any change to the files is detected.

**skops** - A safe file format for scikit-learn models that only recreates explicitly
trusted object types (unlike Python's pickle format, which can run arbitrary code).

---

## Monitoring and drift

**Operational monitoring** - Watching whether the service works: up, fast, few errors, right
model loaded. Done with Prometheus and Grafana.

**ML monitoring** - Watching whether the data and the model's quality are still what they
were. Done by the monitoring worker with Evidently.

**Reference data** - What production data is compared against: the model's own test-period
rows together with the model's own predictions on them.

**Current window** - The newest production predictions (1,000 by default) of the model
version being monitored.

**Drift** - A change in a distribution over time.

**Feature (covariate) drift** - The distribution of an input changes, for example many more
customers get a price increase. Can be detected immediately, without outcomes.

**Dataset drift** - Enough individual features drifted (here: at least 25% of the 16) to say
the input data as a whole changed.

**Prediction drift** - The distribution of the model's predicted probabilities changed.

**Concept drift** - The *relationship* between inputs and outcome changes: the same kind of
customer now churns more (or less) often. It can only be confirmed with true outcomes, and
usually requires retraining.

**Wasserstein distance / Jensen-Shannon distance** - The two measures used to quantify how
far a current distribution is from the reference (Wasserstein for numbers, Jensen-Shannon
for categories). 0 = identical; the drift threshold here is 0.1.

**Delayed ground truth** - True outcomes that only become known after the outcome window.
See [delayed-ground-truth.md](delayed-ground-truth.md).

**Retraining policy** - The rules that decide whether monitoring evidence justifies
retraining.

**Retraining request** - A row asking the retraining controller to retrain. At most one can
be open per model.

**Cooldown** - The minimum time between two retraining requests (12 hours), to avoid
reacting again and again to the same change.

---

## Infrastructure and tools

**Docker Compose** - Runs several containers together from one file (`compose.yaml`).

**Container / image / volume** - An *image* is a packaged application; a *container* is a
running instance of it; a *volume* is persistent storage that survives container restarts.

**Health check** - A small command Docker runs regularly to decide whether a container is
healthy.

**DVC (Data Version Control)** - Versions large files by their content hash and runs
pipeline stages whose inputs changed. See [dataset-versioning.md](dataset-versioning.md).

**MinIO** - A local object store with the same interface as Amazon S3. Stores dataset files
and model files.

**MLflow** - Records experiments and runs and manages the model registry.

**PostgreSQL** - The relational database. Holds MLflow's metadata and the platform's
operations data.

**FastAPI / Pydantic** - The web framework of the prediction API / the library that
validates request data against typed models.

**Prometheus** - Collects numeric metrics from services at regular intervals and evaluates
alert rules.

**Grafana** - Dashboards on top of Prometheus and PostgreSQL.

**Evidently** - A library for data drift and data quality analysis that also renders HTML
reports.

**uv** - A fast Python package and environment manager; `uv.lock` pins every dependency.

**`churnctl`** - The platform's command-line tool. See [cli-reference.md](cli-reference.md).

---

## Engineering terms

**Idempotent** - Doing something twice has the same effect as doing it once. Example:
registering the same run twice returns the same version.

**Deterministic / seeded** - Randomness comes from a fixed starting value (seed), so the
same inputs always produce the same outputs.

**Liveness vs. readiness** - *Liveness* (`/health`): the process is running. *Readiness*
(`/ready`): the service can actually do its job (a verified model is loaded).

**Least privilege** - Every component gets only the permissions it needs. Example: the
inference service's database user may only insert predictions.

**Trust boundary** - The line between what is trusted and what is not. Here: the local
machine is trusted; other machines cannot reach the platform.

**Training-serving skew** - Differences between how features are computed during training
and in production. Prevented here by shipping the preprocessing inside the model.

**Label cardinality** - The number of distinct values a metric label can take. Kept low in
Prometheus because every distinct value creates a separate time series.

**Migration** - A versioned change to a database structure, applied in order and only once.
