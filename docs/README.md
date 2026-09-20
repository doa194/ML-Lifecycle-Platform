# Documentation

Welcome to the documentation of the **Production ML Lifecycle Platform**. The documents are
organised by the question they answer, not by technology. Each one starts with a short
"In short" summary, explains the ideas in plain language first and then gives the exact
details (commands, configuration keys, tables).

If a word is unfamiliar, look it up in the [glossary](glossary.md).

---

## Suggested reading paths

**"I want to understand what this is." (15 minutes)**

1. [Project overview](project-overview.md) - the problem, the idea and the lifecycle in plain words
2. [How it works end to end](how-it-works.md) - the life of a model and the life of a prediction
3. [Architecture](architecture.md) - the components and how they fit together

**"I want to run it." (30 minutes)**

1. [Getting started](getting-started.md) - install, start, verify, first prediction, clean up
2. [Demo walkthrough](demo-walkthrough.md) - the full lifecycle scenario with real output
3. [Troubleshooting](troubleshooting.md) - if something does not work

**"I want to evaluate the engineering." (reviewer path)**

1. [Design decisions](design-decisions.md) - what was chosen, what else was considered, trade-offs
2. [Quality gate](quality-gate.md) and [model lifecycle](model-lifecycle.md) - governance
3. [Testing strategy](testing-strategy.md) - what is tested, at which layer and why
4. [Security](security.md) and [production considerations](production-considerations.md) - honest limits

**"I want to change or extend it." (developer path)**

1. [Codebase guide](codebase-guide.md) - where everything lives and how to extend it
2. [Configuration reference](configuration-reference.md) - every setting and environment variable
3. [Data pipeline](data-pipeline.md) and [training and evaluation](training-and-evaluation.md)

---

## All documents

### Orientation

| Document | Answers |
|---|---|
| [project-overview.md](project-overview.md) | What problem does the platform solve, and what does "ML lifecycle" mean here? |
| [how-it-works.md](how-it-works.md) | What happens, step by step, from raw data to a served, monitored and retrained model? |
| [getting-started.md](getting-started.md) | How do I install, start, verify, use and remove the platform? |
| [demo-walkthrough.md](demo-walkthrough.md) | What does the complete lifecycle look like with real commands and results? |
| [faq.md](faq.md) | Short answers to common questions, with links to the details |
| [glossary.md](glossary.md) | What do the technical terms mean? |

### Architecture and code

| Document | Answers |
|---|---|
| [architecture.md](architecture.md) | Which components exist, what does each own, and how do they communicate? |
| [design-decisions.md](design-decisions.md) | Why was each important choice made, and what are the trade-offs? |
| [codebase-guide.md](codebase-guide.md) | Where is the code for each responsibility, and how do I extend it safely? |

### Data

| Document | Answers |
|---|---|
| [synthetic-world.md](synthetic-world.md) | How is the customer data generated, and how does the simulated world change over time? |
| [data-pipeline.md](data-pipeline.md) | What does each pipeline stage do, check and produce? |
| [dataset-versioning.md](dataset-versioning.md) | How are dataset versions stored, restored and linked to models? |
| [leakage-controls.md](leakage-controls.md) | How does the platform stop models from "cheating" with future information? |

### Models

| Document | Answers |
|---|---|
| [training-and-evaluation.md](training-and-evaluation.md) | How are models built, compared and measured? |
| [experiment-tracking.md](experiment-tracking.md) | What is recorded in MLflow, and how do I trace a model back to its data and code? |
| [model-lifecycle.md](model-lifecycle.md) | How do registration, promotion and rollback work? |
| [quality-gate.md](quality-gate.md) | What must a model pass before it may replace the production model? |

### Serving, monitoring and retraining

| Document | Answers |
|---|---|
| [serving.md](serving.md) | How does the prediction service load models, answer requests and record predictions? |
| [api-reference.md](api-reference.md) | What exactly does the API accept and return? |
| [observability.md](observability.md) | Which operational metrics, alerts and dashboards exist? |
| [ml-monitoring.md](ml-monitoring.md) | How is drift detected, and how should the results be interpreted? |
| [delayed-ground-truth.md](delayed-ground-truth.md) | When do true outcomes arrive, and what do they make possible? |
| [continuous-training.md](continuous-training.md) | When and how does the platform retrain itself, and what keeps that safe? |

### Operating the platform

| Document | Answers |
|---|---|
| [configuration-reference.md](configuration-reference.md) | Which settings, files and environment variables exist, with their defaults? |
| [cli-reference.md](cli-reference.md) | What does every `churnctl` command do? |
| [operations.md](operations.md) | How do I perform everyday tasks (deploy, retrain, roll back, back up, reset)? |
| [failure-recovery.md](failure-recovery.md) | What happens when a component fails, and how does the platform recover? |
| [troubleshooting.md](troubleshooting.md) | Something is wrong - what is the likely cause and fix? |

### Quality, security and limits

| Document | Answers |
|---|---|
| [testing-strategy.md](testing-strategy.md) | How is the platform tested, and what does each test layer protect? |
| [security.md](security.md) | What is trusted, what is protected, and which risks are accepted? |
| [production-considerations.md](production-considerations.md) | What is simplified for local use, and what would change in production? |

---

## Conventions used in these documents

- Commands are run from the repository root and start with `uv run` so they use the
  project's Python environment. They work in PowerShell, Command Prompt, bash and zsh unless
  a section says otherwise.
- Numbers such as F1 scores come from a real run of the demo scenario. Data generation and
  training are seeded, so you get the same values; IDs and timestamps differ.
- *Simulated dates* (for example "2026-06-30") refer to the timeline of the synthetic world,
  not to the clock on your computer.
