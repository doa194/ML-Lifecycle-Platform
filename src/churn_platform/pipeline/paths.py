"""File locations shared by the DVC stages (dvc.yaml declares the same paths)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PipelinePaths:
    root: Path

    @property
    def raw(self) -> Path:
        return self.root / "data" / "raw" / "snapshots.parquet"

    @property
    def prepared(self) -> Path:
        return self.root / "data" / "prepared" / "snapshots.parquet"

    def split(self, name: str) -> Path:
        return self.root / "data" / "splits" / f"{name}.parquet"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def dataset_report(self) -> Path:
        return self.reports / "dataset.json"

    @property
    def validation_report(self) -> Path:
        return self.reports / "data_validation.json"

    @property
    def split_report(self) -> Path:
        return self.reports / "split.json"

    @property
    def training_summary(self) -> Path:
        return self.reports / "training_summary.json"

    @property
    def evaluation_report(self) -> Path:
        return self.reports / "evaluation.json"
