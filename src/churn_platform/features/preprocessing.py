"""Builds the single scikit-learn pipeline that is trained, registered and served.

    raw feature frame -> FeatureEngineer -> ColumnTransformer -> classifier

The whole object is one MLflow model artifact. Training, the quality gate, monitoring and
the inference API all call `predict_proba` on this same object, so preprocessing can
never differ between training and serving.
"""

from __future__ import annotations

from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from churn_platform.data.schema import CATEGORY_LEVELS
from churn_platform.features.engineering import (
    MODEL_CATEGORICAL_INPUTS,
    MODEL_NUMERIC_INPUTS,
    FeatureEngineer,
)

# Fully qualified class names that skops may instantiate when loading a model. Anything
# else found in a model file is refused, so a tampered artifact cannot run arbitrary code.
# The list is exactly what skops reports for the three supported algorithms; a unit test
# fails if a new algorithm or library version needs more.
TRUSTED_MODEL_TYPES = [
    "churn_platform.features.engineering.FeatureEngineer",
    "numpy.dtype",  # dtype settings stored by the one-hot encoder
    "sklearn.tree._tree.Tree",  # random forest trees
    "sklearn.ensemble._hist_gradient_boosting.predictor.TreePredictor",
]


def build_preprocessor() -> ColumnTransformer:
    numeric = Pipeline(
        [
            # Median imputation plus a "was missing" indicator: a missing resolution time
            # means "no tickets", which is information the model should keep.
            ("impute", SimpleImputer(strategy="median", add_indicator=True)),
            ("scale", StandardScaler()),
        ]
    )
    categorical = OneHotEncoder(
        # Fixed category lists keep the model's input layout stable across datasets.
        categories=[list(CATEGORY_LEVELS[c]) for c in MODEL_CATEGORICAL_INPUTS],
        handle_unknown="error",
        sparse_output=False,
    )
    return ColumnTransformer(
        [
            ("numeric", numeric, list(MODEL_NUMERIC_INPUTS)),
            ("categorical", categorical, list(MODEL_CATEGORICAL_INPUTS)),
        ],
        remainder="drop",
    )


def build_model_pipeline(classifier, new_customer_months: int) -> Pipeline:
    return Pipeline(
        [
            ("features", FeatureEngineer(new_customer_months=new_customer_months)),
            ("preprocess", build_preprocessor()),
            ("classifier", classifier),
        ]
    )
