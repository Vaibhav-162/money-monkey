"""Small hyperparameter search via inner expanding-window CV, per board.

The plan is explicit that any depth/leaves difference between mainboard and SME
models must come from what the data rewards inside each board's own training
window, not from a hardcoded "SME gets deeper trees" rule. This module runs a
small grid (matching the originally proposed search space: max_depth,
learning_rate, min_child_weight, num_leaves) through leave-one-future-year-out
folds *inside* the outer walk-forward train slice, so the outer test year is
never touched by tuning.

With only a handful of listing years per board there usually are not enough
inner folds for a statistically strong pick -- when that happens we fall back
to one documented, conservative default rather than pretending the search was
decisive.
"""

from __future__ import annotations

import itertools
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

try:
    import lightgbm as lgb
except ImportError:  # pragma: no cover
    lgb = None

from analysis.features import feature_matrix

CLASSIFIER_GRID: dict[str, list[Any]] = {
    "max_depth": [3, 5, 7],
    "learning_rate": [0.03, 0.1],
    "min_child_weight": [5, 20, 50],
    "num_leaves": [8, 31],
}

REGRESSOR_GRID: dict[str, list[Any]] = dict(CLASSIFIER_GRID)

DEFAULT_PARAMS: dict[str, Any] = {
    "max_depth": 4, "learning_rate": 0.05, "min_child_weight": 20, "num_leaves": 8,
}


def _grid_combos(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    keys = list(grid.keys())
    return [dict(zip(keys, vals)) for vals in itertools.product(*(grid[k] for k in keys))]


def _inner_year_folds(train: pd.DataFrame, min_train: int = 30, min_test: int = 6) -> list[tuple[pd.DataFrame, pd.DataFrame]]:
    if "listing_year" not in train.columns:
        return []
    years = sorted(int(y) for y in train["listing_year"].dropna().unique())
    folds = []
    for i in range(1, len(years)):
        ty = years[i]
        inner_train = train[train["listing_year"] < ty]
        inner_test = train[train["listing_year"] == ty]
        if len(inner_train) >= min_train and len(inner_test) >= min_test:
            folds.append((inner_train, inner_test))
    return folds


def select_classifier_params(
    train: pd.DataFrame,
    feature_cols: list[str],
    label_col: str,
    pos_weight_cap: float = 8.0,
    grid: Optional[dict[str, list[Any]]] = None,
) -> dict[str, Any]:
    """Pick max_depth/learning_rate/min_child_weight/num_leaves by inner-fold mean EV
    of the top-20% predicted rows. Falls back to DEFAULT_PARAMS if there is not
    enough inner data (< 1 usable fold) to trust the search."""
    if lgb is None:
        return dict(DEFAULT_PARAMS)
    folds = _inner_year_folds(train)
    if not folds:
        return dict(DEFAULT_PARAMS)
    grid = grid or CLASSIFIER_GRID
    best_combo, best_score = dict(DEFAULT_PARAMS), -np.inf
    for combo in _grid_combos(grid):
        fold_scores = []
        for inner_train, inner_test in folds:
            yv = pd.to_numeric(inner_train[label_col], errors="coerce")
            mask = yv.notna()
            if mask.sum() < 20 or yv[mask].nunique() < 2:
                continue
            x = feature_matrix(inner_train.loc[mask], feature_cols, filled=False)
            pos = int((yv[mask] == 1).sum())
            neg = int((yv[mask] == 0).sum())
            weight = min(max((neg / pos) if pos else 1.0, 1.0), pos_weight_cap)
            model = lgb.LGBMClassifier(
                objective="binary", verbosity=-1, n_estimators=150,
                scale_pos_weight=weight, feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1,
                **combo,
            )
            model.fit(x, yv[mask].astype(int))
            xt = feature_matrix(inner_test, feature_cols, filled=False)
            proba = model.predict_proba(xt)[:, 1]
            ev = pd.to_numeric(inner_test.get("realized_ev"), errors="coerce").to_numpy()
            if np.isfinite(ev).sum() < 3:
                continue
            order = np.argsort(-proba)
            topn = max(1, int(round(len(inner_test) * 0.2)))
            top_ev = ev[order][:topn]
            top_ev = top_ev[np.isfinite(top_ev)]
            if len(top_ev):
                fold_scores.append(float(np.mean(top_ev)))
        if fold_scores:
            mean_score = float(np.mean(fold_scores))
            if mean_score > best_score:
                best_score, best_combo = mean_score, combo
    return best_combo


def select_regressor_params(
    train: pd.DataFrame,
    feature_cols: list[str],
    label_col: str,
    grid: Optional[dict[str, list[Any]]] = None,
) -> dict[str, Any]:
    """Pick hyperparameters by inner-fold RMSE (lower is better)."""
    if lgb is None:
        return dict(DEFAULT_PARAMS)
    folds = _inner_year_folds(train)
    if not folds:
        return dict(DEFAULT_PARAMS)
    grid = grid or REGRESSOR_GRID
    best_combo, best_score = dict(DEFAULT_PARAMS), np.inf
    for combo in _grid_combos(grid):
        fold_scores = []
        for inner_train, inner_test in folds:
            yv = pd.to_numeric(inner_train[label_col], errors="coerce")
            mask = yv.notna()
            if mask.sum() < 20:
                continue
            x = feature_matrix(inner_train.loc[mask], feature_cols, filled=False)
            model = lgb.LGBMRegressor(
                objective="regression", verbosity=-1, n_estimators=150,
                feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1,
                **combo,
            )
            model.fit(x, yv[mask])
            xt = feature_matrix(inner_test, feature_cols, filled=False)
            pred = model.predict(xt)
            actual = pd.to_numeric(inner_test[label_col], errors="coerce").to_numpy()
            ok = np.isfinite(pred) & np.isfinite(actual)
            if ok.sum() < 3:
                continue
            rmse = float(np.sqrt(np.mean((pred[ok] - actual[ok]) ** 2)))
            fold_scores.append(rmse)
        if fold_scores:
            mean_score = float(np.mean(fold_scores))
            if mean_score < best_score:
                best_score, best_combo = mean_score, combo
    return best_combo
