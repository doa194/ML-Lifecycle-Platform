"""Critical end-to-end workflows, driven through the real CLI and a real inference server.

The three workflows build on each other, so each is a module-scoped fixture (run once, in
order, even when a single test is selected) and each test asserts one workflow's outcome:

  1. initial lifecycle: data -> DVC -> train -> MLflow -> register -> gate -> promote -> serve
  2. drift lifecycle:   traffic -> drift -> monitoring -> retraining request -> controller
                        -> candidate -> gate -> promotion, while serving is never disturbed
  3. rollback:          champion alias back to the previous version -> reload -> it serves

Lower-level rules (gate checks, policy thresholds, API validation) are covered by unit,
component and integration tests and are deliberately not repeated here.
"""

from __future__ import annotations

import pytest
import yaml

from churn_platform.lifecycle.states import Alias
from tests.support.lifecycle import LifecycleSandbox
from tests.support.processes import InferenceServer, churnctl, json_output
from tests.support.serving import valid_payload

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def box(tmp_path_factory, stack):
    sandbox = LifecycleSandbox(tmp_path_factory.mktemp("e2e")).create()
    yield sandbox
    sandbox.cleanup()


@pytest.fixture(scope="module")
def server(box):
    serving = InferenceServer(box)
    yield serving
    serving.stop()


@pytest.fixture(scope="module")
def initial_lifecycle(box, server):
    churnctl(box, "pipeline", "run")
    churnctl(box, "model", "register")
    churnctl(box, "model", "gate")
    churnctl(box, "model", "promote")
    server.start()
    return box.registry().aliases()[Alias.CHAMPION]


@pytest.fixture(scope="module")
def drift_lifecycle(box, server, initial_lifecycle):
    traffic = {"INFERENCE_URL": server.url}
    churnctl(box, "traffic", "send", "--start", "2025-11-01", "--end", "2025-12-31", "--count", "600", **traffic)
    before_drift = json_output(churnctl(box, "monitor", "run"))
    churnctl(box, "traffic", "send", "--start", "2026-01-01", "--end", "2026-12-31", "--count", "1000", **traffic)
    after_drift = json_output(churnctl(box, "monitor", "run"))
    # Only a request created by monitoring gives the controller something to do.
    run = churnctl(box, "retrain", "run")
    retraining = json_output(run) if after_drift["decision"] == "request_retraining" else {"stdout": run.stdout}
    return {
        "before_drift": before_drift,
        "after_drift": after_drift,
        "retraining": retraining,
        "served_during_retraining": server.get("/model").json()["model_version"],
        "ready_after_retraining": server.get("/ready").status_code,
    }


@pytest.fixture(scope="module")
def rollback_lifecycle(box, server, drift_lifecycle):
    new_champion = box.registry().aliases()[Alias.CHAMPION]
    served_after_reload = server.restart().get("/model").json()["model_version"]
    churnctl(box, "model", "rollback", "--reason", "e2e rollback drill")
    server.restart()
    return {
        "new_champion": new_champion,
        "served_after_reload": served_after_reload,
        "served_after_rollback": server.get("/model").json()["model_version"],
        "prediction": server.post("/predict", valid_payload()),
    }


def test_initial_lifecycle_serves_the_promoted_champion(box, server, initial_lifecycle):
    response = server.post("/predict", valid_payload())
    lineage = server.get("/model").json()
    params = yaml.safe_load((box.root / "params.yaml").read_text(encoding="utf-8"))

    assert response.status_code == 200
    assert response.json()["model_version"] == initial_lifecycle
    assert lineage["dataset"]["as_of"] == params["dataset"]["as_of"]
    assert lineage["quality_gate"]["status"] == "passed"
    assert box.workspace.remote_objects(), "the dataset version was pushed to the DVC remote"


def test_drift_triggers_governed_retraining_without_disturbing_serving(box, initial_lifecycle, drift_lifecycle):
    result = drift_lifecycle
    retraining = result["retraining"]
    aliases = box.registry().aliases()

    assert result["before_drift"]["decision"] in {"no_action", "watch"}, result["before_drift"]
    assert result["after_drift"]["dataset_drift"] and result["after_drift"]["prediction_drift"]
    assert result["after_drift"]["decision"] == "request_retraining", result["after_drift"]
    assert retraining["champion_before"] == initial_lifecycle
    # The scenario is built so that a model trained on the new regime clearly wins.
    assert retraining["outcome"] == "promoted"
    assert aliases[Alias.CHAMPION] == retraining["candidate_version"] == retraining["champion_after"]
    assert box.registry().version_state(initial_lifecycle).status == "retired"
    # Serving keeps its loaded model (and stays ready) until it is explicitly reloaded.
    assert result["served_during_retraining"] == initial_lifecycle
    assert result["ready_after_retraining"] == 200


def test_rollback_restores_the_previous_champion_after_reload(initial_lifecycle, rollback_lifecycle):
    result = rollback_lifecycle

    assert result["served_after_reload"] == result["new_champion"] != initial_lifecycle
    assert result["served_after_rollback"] == initial_lifecycle
    assert result["prediction"].status_code == 200
    assert result["prediction"].json()["model_version"] == initial_lifecycle
