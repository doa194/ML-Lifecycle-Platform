"""The quality gate decision: may this candidate become a challenger?

Pure logic, no I/O: it receives already computed evaluations of the candidate (and of the
current champion, measured on the same data) and returns a decision with one entry per
check, so every "why was this model rejected?" question has a recorded answer.

Checks, all of which must pass:
  * the candidate loads from the registry and matches its fingerprint
  * absolute F1, recall and ROC-AUC floors
  * single-row p95 latency limit
  * recall floor for every reliable critical segment
  * against the champion: overall F1 and ROC-AUC may not regress beyond tolerance, and no
    reliable segment may lose more than the allowed F1 (an overall improvement must not
    hide a regression for one customer group)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from churn_platform.config import QualityGatePolicy


@dataclass(frozen=True)
class ModelEvaluation:
    version: str
    overall: dict = field(default_factory=dict)
    segments: list[dict] = field(default_factory=list)
    latency_p95_ms: float | None = None
    load_error: str | None = None


@dataclass(frozen=True)
class GateCheck:
    name: str
    passed: bool
    observed: float | str | None
    threshold: float | str | None
    detail: str = ""


@dataclass(frozen=True)
class GateDecision:
    candidate_version: str
    champion_version: str | None
    checks: list[GateCheck]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

    @property
    def failed_checks(self) -> list[GateCheck]:
        return [check for check in self.checks if not check.passed]

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "candidate_version": self.candidate_version,
            "champion_version": self.champion_version,
            "checks": [asdict(check) for check in self.checks],
        }


def _minimum(name: str, value: float | None, threshold: float) -> GateCheck:
    if value is None:
        return GateCheck(name, False, None, threshold, "metric could not be computed")
    return GateCheck(name, value >= threshold, round(value, 4), threshold)


def _reliable_segments(evaluation: ModelEvaluation, columns: list[str]) -> dict[tuple[str, str], dict]:
    return {
        (s["segment"], s["value"]): s
        for s in evaluation.segments
        if s["segment"] in columns and s.get("reliable")
    }


def evaluate_gate(candidate: ModelEvaluation, champion: ModelEvaluation | None, policy: QualityGatePolicy) -> GateDecision:
    champion_version = champion.version if champion else None
    if candidate.load_error:
        # Nothing else can be judged for a model that cannot be loaded safely.
        check = GateCheck("loadable_and_intact", False, "error", "loads and matches fingerprint", candidate.load_error)
        return GateDecision(candidate.version, champion_version, [check])

    checks = [GateCheck("loadable_and_intact", True, "ok", "loads and matches fingerprint")]
    overall = candidate.overall
    checks.append(_minimum("f1_min", overall.get("f1"), policy.absolute.f1_min))
    checks.append(_minimum("recall_min", overall.get("recall"), policy.absolute.recall_min))
    checks.append(_minimum("roc_auc_min", overall.get("roc_auc"), policy.absolute.roc_auc_min))

    latency = candidate.latency_p95_ms
    checks.append(
        GateCheck(
            "latency_p95_ms",
            latency is not None and latency <= policy.operational.max_p95_latency_ms,
            None if latency is None else round(latency, 2),
            policy.operational.max_p95_latency_ms,
        )
    )

    segments = _reliable_segments(candidate, policy.segments.columns)
    weak = {f"{col}={val}": round(s["recall"], 4) for (col, val), s in segments.items() if s["recall"] < policy.segments.recall_min}
    worst = min((s["recall"] for s in segments.values()), default=None)
    checks.append(
        GateCheck(
            "segment_recall_min",
            not weak,
            None if worst is None else round(worst, 4),
            policy.segments.recall_min,
            f"segments below floor: {weak}" if weak else f"{len(segments)} reliable segments checked",
        )
    )

    if champion is None:
        checks.append(GateCheck("champion_comparison", True, None, None, "no current champion: absolute checks only"))
        return GateDecision(candidate.version, None, checks)

    comparison = policy.champion_comparison
    for metric, min_delta in (("f1", comparison.min_f1_delta), ("roc_auc", comparison.min_roc_auc_delta)):
        new, old = overall.get(metric), champion.overall.get(metric)
        if new is None or old is None:
            checks.append(GateCheck(f"{metric}_vs_champion", False, None, min_delta, "metric could not be computed"))
            continue
        delta = new - old
        checks.append(
            GateCheck(
                f"{metric}_vs_champion",
                delta >= min_delta,
                round(delta, 4),
                min_delta,
                f"candidate {new:.4f} vs champion {old:.4f}",
            )
        )

    champion_segments = _reliable_segments(champion, policy.segments.columns)
    shared = sorted(segments.keys() & champion_segments.keys())
    max_drop = policy.segments.max_f1_drop_vs_champion
    drops = {f"{col}={val}": round(champion_segments[(col, val)]["f1"] - segments[(col, val)]["f1"], 4) for col, val in shared}
    regressed = {name: drop for name, drop in drops.items() if drop > max_drop}
    checks.append(
        GateCheck(
            "segment_f1_vs_champion",
            not regressed,
            max(drops.values(), default=None),
            max_drop,
            f"segments regressed beyond limit: {regressed}" if regressed else f"{len(shared)} shared segments checked",
        )
    )
    return GateDecision(candidate.version, champion_version, checks)
