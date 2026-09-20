# Operations runbook

> **In short:** Step-by-step procedures for everyday work with the platform: starting and
> checking it, putting a model into production, handling retraining, rolling back, changing
> policies, reading logs, backing up and starting over. Each procedure says what to run, what
> you should see and what to do if you do not.

## Contents

- [Start of day: start and check](#start-of-day-start-and-check)
- [Put a newly trained model into production](#put-a-newly-trained-model-into-production)
- [Promote with manual approval](#promote-with-manual-approval)
- [Handle a retraining request](#handle-a-retraining-request)
- [Roll back the production model](#roll-back-the-production-model)
- [Run monitoring now](#run-monitoring-now)
- [Change a policy](#change-a-policy)
- [Update the operations database schema](#update-the-operations-database-schema)
- [Look at logs](#look-at-logs)
- [Back up and restore](#back-up-and-restore)
- [Stop, or start over](#stop-or-start-over)

---

## Start of day: start and check

1. Start the platform (safe if it is already running):

   ```bash
   uv run churnctl stack up
   ```

2. Check that the API serves the expected model:

   ```bash
   uv run churnctl serving status
   ```

   Expected: `ready: True  {'status': 'ready', 'model_name': 'customer-churn-classifier', 'model_version': '<N>'}`.
   If not ready, the reason is printed - see [troubleshooting.md](troubleshooting.md#serving).

3. Glance at the dashboards:
   - Grafana -> *Churn Inference Operations*: champion `LOADED`, error rate near 0, p95 latency low;
   - Prometheus -> *Alerts*: nothing firing;
   - Grafana -> *Churn Model Monitoring*: the latest decision and any open retraining request.

---

## Put a newly trained model into production

1. Build data and train:

   ```bash
   uv run churnctl pipeline run
   ```

2. Register the winner:

   ```bash
   uv run churnctl model register
   ```

3. Run the quality gate:

   ```bash
   uv run churnctl model gate
   ```

   - Exit code 0 / `PASSED -> challenger`: continue.
   - Exit code 3 / `FAILED -> rejected`: stop here. The champion is untouched. The table shows
     which checks failed; the full report is on the version's MLflow run
     (`gate/report-...json`).

4. Promote:

   ```bash
   uv run churnctl model promote
   ```

   If it is refused, every reason is listed (for example "re-run the gate" when the champion
   changed in the meantime).

5. Load the new champion into the API:

   ```bash
   uv run churnctl serving reload
   ```

   Expected: `inference is ready and serving customer-churn-classifier version <N>`.

---

## Promote with manual approval

Set in `config/quality_gate.yaml`:

```yaml
approval:
  manual_approval_required: true
```

From now on promotion requires a named approver (retraining stops at `awaiting_promotion`):

```bash
uv run churnctl model promote --approved-by alice
```

The approver's name is stored on the version (`lifecycle.approved_by`) and in the audit trail.

---

## Handle a retraining request

1. See what was requested and why:

   ```bash
   uv run churnctl retrain status
   ```

2. Optionally inspect the evidence: MLflow -> experiment `customer-churn-monitoring` -> the
   latest run -> `monitoring/evidently_report.html`.
3. Process the request (and load a newly promoted champion right away):

   ```bash
   uv run churnctl retrain run --reload-serving
   ```

4. Read the outcome: `promoted`, `awaiting_promotion`, `rejected`, `unchanged` or `failed`
   ([what each means](continuous-training.md#outcomes)).
5. Commit the new dataset version, which the controller wrote to the working tree:

   ```bash
   git add params.yaml dvc.lock reports
   ```

   ```bash
   git commit -m "Dataset version as of <date>"
   ```

**If the request failed:** the champion is unchanged. Read the error in `retrain status`,
fix the cause, then retry:

```bash
uv run churnctl retrain run --request-id <request-id>
```

**To request retraining yourself:**

```bash
uv run churnctl retrain request --reason "explain why"
```

---

## Roll back the production model

1. Move the `champion` alias back:

   ```bash
   uv run churnctl model rollback --reason "short explanation"
   ```

   Without `--to-version`, the most recently retired champion returns. With
   `--to-version N`, only a retired, gate-approved former champion is accepted.

2. Load it:

   ```bash
   uv run churnctl serving reload
   ```

3. Confirm:

   ```bash
   uv run churnctl model status
   ```

   The restored version holds `champion`; the rolled-back version shows `rolled_back` and can
   never become champion again.

---

## Run monitoring now

The monitoring worker analyses new data every five minutes by itself. To see a result
immediately:

```bash
uv run churnctl monitor run
```

```bash
uv run churnctl monitor status
```

To reveal outcomes of old predictions (simulated time):

```bash
uv run churnctl labels resolve --as-of 2027-01-31
```

---

## Change a policy

Edit the file in `config/` and apply it:

| File | How the change is applied |
|---|---|
| `quality_gate.yaml` | nothing to do - read by the next gate, promotion or retraining command |
| `monitoring.yaml`, `retraining.yaml` | nothing to do - re-read by every monitoring cycle and command; only `worker.interval_seconds` needs `uv run churnctl stack restart monitoring-worker` |
| `serving.yaml` | `uv run churnctl stack restart inference` (read once at startup) |
| `data.yaml`, `training.yaml`, `params.yaml` | `uv run churnctl pipeline run` re-runs the affected stages |

Invalid files are rejected with a clear message when loaded
([configuration-reference.md](configuration-reference.md#validation-examples)). After changing
the gate policy, a previously rejected version can be re-evaluated with
`uv run churnctl model gate --version N`.

---

## Update the operations database schema

Add a new numbered SQL file to `src/churn_platform/storage/migrations/` (never edit an applied
one) and run:

```bash
uv run churnctl db migrate
```

The `db-migrate` container also applies pending migrations on every `stack up`. Applied
migrations are listed in the table `ops.schema_migrations`.

---

## Look at logs

```bash
docker compose logs --tail 100 inference
```

Replace `inference` with any service: `monitoring-worker`, `mlflow`, `postgres`, `minio`,
`prometheus`, `grafana`. Add `--follow` to keep watching. What the important lines mean:
[observability.md](observability.md#logs).

---

## Back up and restore

All platform state lives in four Docker volumes:

| Volume | Contains |
|---|---|
| `churn-platform_postgres-data` | MLflow metadata, predictions, monitoring results, retraining requests, audit trail |
| `churn-platform_minio-data` | model files, reports, dataset versions |
| `churn-platform_prometheus-data` | 7 days of metrics |
| `churn-platform_grafana-data` | Grafana's own settings |

**Back up** (stop the stack first so files are consistent):

```bash
uv run churnctl stack down
```

```bash
docker run --rm -v churn-platform_postgres-data:/data -v "${PWD}:/backup" alpine tar czf /backup/postgres-data.tgz -C /data .
```

Repeat for the other volumes. Keep `.env` together with the backup - the data only works with
the passwords it was created with.

**Restore** into an empty volume:

```bash
docker run --rm -v churn-platform_postgres-data:/data -v "${PWD}:/backup" alpine tar xzf /backup/postgres-data.tgz -C /data
```

There is no automated backup ([production-considerations.md](production-considerations.md)).

---

## Stop, or start over

| Goal | Steps |
|---|---|
| stop, keep everything | `uv run churnctl stack down` |
| start again | `uv run churnctl stack up` |
| wipe everything | `uv run churnctl stack reset --yes`; set `dataset.as_of` in `params.yaml` back to `"2026-01-01"`; `uv run churnctl bootstrap` |
