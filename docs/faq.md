# Frequently asked questions

> **In short:** Short answers to the questions people most often ask about the platform.
> Each answer links to the document with the full explanation.

## Contents

- [About the project](#about-the-project)
- [Data and models](#data-and-models)
- [Lifecycle and governance](#lifecycle-and-governance)
- [Serving and monitoring](#serving-and-monitoring)
- [Running and extending](#running-and-extending)

---

## About the project

**Why is the model so simple?**
Because the project is about the lifecycle around a model, not about squeezing out the last
percent of accuracy. Standard scikit-learn models keep attention on data versioning,
governance, serving, monitoring and retraining. See [project-overview.md](project-overview.md).

**Why not use Kubeflow, Airflow or Kubernetes?**
They solve problems (scheduling at scale, many teams, many machines) that a one-machine
platform does not have, and each needs its own servers to operate. DVC gives reproducible,
cached pipelines without a server, and Docker Compose runs the services. The pipeline stages
are plain functions, so moving them to an orchestrator later does not require rewriting them.
See [design-decisions.md](design-decisions.md#1-dvc-pipelines-as-the-orchestrator).

**Does it need a cloud account or internet access at runtime?**
No. After images and Python packages are downloaded, everything runs locally.

**Is it production-ready?**
It is production-*style*: the lifecycle rules, failure handling and tests are what a
production system needs, but it runs on one machine without authentication, high
availability or backups. See [production-considerations.md](production-considerations.md).

---

## Data and models

**Why synthetic data?**
To control *when* and *how* the world changes, so that drift, delayed outcomes and
retraining can be shown reproducibly, and to avoid privacy issues.
See [synthetic-world.md](synthetic-world.md).

**What exactly changes in the simulated world?**
From 1 January 2026 (simulated time) many more customers get larger price increases, payment
problems rise, and customers react to price much more strongly. Both the inputs *and* the
churn logic change. See [synthetic-world.md](synthetic-world.md#the-timeline-when-the-world-changes).

**Why is the data split by time instead of randomly?**
Churn prediction is forecasting. A random split would let the model learn from customers
observed *after* the ones it is tested on and overstate its quality.
See [leakage-controls.md](leakage-controls.md).

**Why F1 and not accuracy?**
Only about 13% of customers churn. A model that never predicts churn would be 87% accurate
and useless. F1 balances finding churners (recall) and not raising false alarms (precision).
See [training-and-evaluation.md](training-and-evaluation.md#metrics).

**Why does each model have its own decision threshold?**
Different algorithms produce differently scaled probabilities. Tuning the threshold on the
validation period, storing it inside the model and using it everywhere keeps training,
gate, monitoring and serving consistent.

---

## Lifecycle and governance

**What is the difference between candidate, challenger and champion?**
*Candidate*: newest training winner. *Challenger*: a candidate that passed the quality gate.
*Champion*: the model in production. See [model-lifecycle.md](model-lifecycle.md).

**Can a bad model reach production by accident?**
Not through the platform's tools: promotion requires the `challenger` alias, a passed gate,
a comparison against the *current* champion and a verified model file. Note that MLflow
itself has no authentication in this local setup, so someone with access to this machine
could change aliases directly. See [security.md](security.md#trust-boundaries).

**Why does the gate compare against the champion on the candidate's test data?**
Because that is the newest data. After the world changes, the old champion must prove itself
on the new reality, not on the numbers it achieved a year ago.
See [quality-gate.md](quality-gate.md#fair-comparison).

**How fast is a rollback?**
The rollback command takes a few seconds: it verifies the restored model file against its
fingerprint and then moves the `champion` alias. The API serves the restored model after
`churnctl serving reload`. No model files are copied.

---

## Serving and monitoring

**Why does the API not switch to a new champion automatically?**
A running service that swaps its model on the fly can end up in a half-switched state and
makes incidents harder to reason about. An explicit reload is simple and observable.
See [design-decisions.md](design-decisions.md#11-load-once-retry-only-until-loaded-reload-explicitly).

**What does "alive but not ready" mean?**
The process is running (`/health` = 200) but it has no verified model, so `/ready` and
`/predict` answer 503. It never serves predictions from an unverified or missing model.
See [serving.md](serving.md#health-endpoints).

**Does drift mean the model is worse?**
Not necessarily. Drift means the inputs or outputs changed. Only true outcomes show whether
predictions got worse. That is why the retraining policy asks for severe drift *plus*
changed predictions, or a measured accuracy drop. See [ml-monitoring.md](ml-monitoring.md).

**Why did production F1 go up after the drift in the demo?**
Churn became more common (13% -> 28%), and F1 rises with the share of positives. ROC-AUC,
which ignores that share, fell from 0.848 to 0.796 and shows the real degradation.

**How does the platform know the true outcomes?**
The traffic simulator decides each customer's outcome when it creates them and hides it in a
separate table. `churnctl labels resolve --as-of <date>` reveals outcomes whose 30-day window
has closed. See [delayed-ground-truth.md](delayed-ground-truth.md).

---

## Running and extending

**Do the tests change my demo model?**
Unit, component, integration and end-to-end tests do not: the stack-based ones create
temporary sandboxes with their own model names and clean up afterwards. Operational tests
restart real containers on purpose and restore them. See [testing-strategy.md](testing-strategy.md).

**How do I add a new algorithm or feature?**
See the step-by-step recipes in [codebase-guide.md](codebase-guide.md#common-changes).

**Can I use my own data?**
Yes, if it follows the same schema (the 16 features, `customer_id`, `snapshot_date` and the
`churned_30d` label). Replace the `generate` stage with a stage that exports your data to
`data/raw/snapshots.parquet`; validation, splitting, training and everything after it stay
the same. The traffic simulator and outcome resolution would be replaced by your real
clients and your real billing data.

**Where are the logs?**
`docker compose logs <service>` for containers; the CLI prints to the terminal. See
[operations.md](operations.md#look-at-logs).
