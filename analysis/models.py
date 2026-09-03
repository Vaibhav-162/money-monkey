"""Per-board LightGBM + logistic. Never fit across boards in the delivered scorer.

WHAT THIS FILE DOES
--------------------
This is the training and inference core for Strategy 1 (listing-day flip) and
Strategy 2 (research regressor on 6-month excess return). Every `FittedBundle`
is tagged with a single `board` (`mainboard` or `sme`); there is no pooled
"all IPOs" path in the delivered pickle that `analysis/score.py` loads.

`analysis/backtest.py` is the walk-forward caller: it `fit_s1` / `fit_s2` on
each expanding train slice, `predict_*` on the held-out listing year, and
`calibrate_threshold` on S1 test-fold probabilities. `run_analysis.py` then
*re-fits* on all 2020+ rows for that board and `save_bundle`s the delivered
`{board}_s1.pkl` / `{board}_s2.pkl`, copying the last walk-forward S1
threshold onto the final S1 bundle. Live scoring never trains: `analysis/score.py`
only `load_bundle`s those pickles and calls `predict_s1_proba`,
`predict_s1_open_return`, and `predict_s2`.

This file pulls rectangular `X` from `analysis.features.feature_matrix` using
`S1_FEATURE_COLS` / `S2_FEATURE_COLS`, and asks `analysis.tuning` for a small
inner-CV hyperparameter pick before each LightGBM fit.

KEY TERMS USED HERE
--------------------
- Mainboard vs SME: India's two exchange tiers for IPOs. This codebase's
  rule is never to share fitted weights across them — a mainboard model
  must not see SME rows and vice versa (the "pooled" string in
  `pooled_ablation_s1` is a backtest-only comparison, not a live board).
- LightGBM: gradient-boosted decision trees. S1 uses an `LGBMClassifier`
  for clean-pop probability plus, when enough labels exist, an
  `LGBMRegressor` on continuous open-day return % for EV sizing. S2 uses
  an `LGBMRegressor` on 6-month Nifty-excess return.
- Logistic regression fallback: a StandardScaler + LogisticRegression
  pipeline trained on *filled* features. `predict_s1_proba` uses LightGBM
  when the bundle has one, else this logistic — so scoring still works if
  LightGBM never fitted (too few rows / library missing).
- Clean pop (`is_clean_pop`): the S1 classification label — a strong,
  held first-day jump. The classifier's P(pop) is `p_pop`.
- Open-day return (`open_return_pct`): listing open vs issue price, in
  percent. The S1 regressor head predicts this so `score_features()` has
  a rupee-sized E[gain], not just a probability.
- EV / Expected Value (`realized_ev`): historical rupee profit per lot
  after allotment odds and the liquidity haircut. `calibrate_threshold`
  picks the S1 probability cutoff that maximizes mean EV on the rows it
  is given (in walk-forward, that is the *test* year).
- `exret_126`: 126-session (~6 month) return minus Nifty over the same
  window — the S2 regression target. Live `apply_s2` does *not* use this
  model; see `analysis/score.py`.
- Monotonic constraints: LightGBM is told that higher `roe` / `roce` /
  `pat_cagr` must not predict a *lower* S2 score, and higher `debt_equity`
  / `ofs_ratio` / `peer_rel_pe` must not predict a *higher* one. Noisy
  samples cannot reverse those real-world directions.
- SHAP / `shap_top`: which features moved the prediction most (mean
  |SHAP|, else LightGBM `feature_importances_`). Stored on the bundle
  and copied into walk-forward JSON; live cards do not ship a SHAP plot.
- Threshold selection (`calibrate_threshold`): a 0.2–0.8 grid search for
  the cutoff that maximizes mean `realized_ev` of picked rows. Despite
  the name this is *not* probability calibration (no Platt/isotonic, no
  Brier here — Brier is computed in `analysis/backtest.py`).
- Walk-forward: the outer time split lives in `backtest.py`; this file
  just fits one slice. Tuning's inner year-out CV is nested *inside*
  that train slice so the outer test year is never used to pick depth.

FUNCTIONS / CLASSES IN THIS FILE
---------------------------------
- `FittedBundle`: pickleable container — board, strategy, feature list,
  optional LightGBM classifier/regressor, optional logistic, apply
  threshold, metrics, `shap_top`, tuned params. S1's `lgbm_reg` is the
  open-return head used to size EV.
- `fit_s1(train, board)`: train the S1 classifier (and open-return
  regressor when ≥40 labeled returns exist) plus logistic. Needs ≥40
  rows and both classes for trees; otherwise those heads stay None.
- `fit_s2(train, board)`: train the monotone S2 excess-return regressor.
  Returns an empty-headed bundle if LightGBM is missing or <40 labeled
  `exret_126` rows (live S2 then falls back to the quality checklist).
- `predict_s1_proba(bundle, df)`: P(clean pop). LightGBM first, else
  logistic on filled features, else all-NaN.
- `predict_s1_open_return(bundle, df)`: predicted open-day return %, or
  None if no regressor was fit — `score.py` then uses a p_pop heuristic.
- `predict_s2(bundle, df)`: S2 model score, or the row's `quality_score`
  if the bundle has no LightGBM (same fallback the scorer relies on).
- `calibrate_threshold(df, proba)`: EV-maximizing apply cutoff + fold
  metrics. Called only from `backtest.py`, not at live-score time.
- `save_bundle` / `load_bundle`: pickle to/from `data/analysis/models/`.
- `_classifier_params` / `_shap_top`: class-imbalance weights for the
  S1 classifier, and the SHAP/importance top-k helper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from analysis.features import S1_FEATURE_COLS, S2_FEATURE_COLS, feature_matrix
from analysis.tuning import select_classifier_params, select_regressor_params

try:
    import lightgbm as lgb
except ImportError:  # pragma: no cover
    lgb = None

try:
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    from sklearn.pipeline import Pipeline
    from sklearn.metrics import brier_score_loss
except ImportError:  # pragma: no cover
    LogisticRegression = None
    StandardScaler = None
    Pipeline = None
    brier_score_loss = None


@dataclass
class FittedBundle:
    board: str
    strategy: str
    feature_cols: list[str]
    lgbm: Any = None
    logistic: Any = None
    lgbm_reg: Any = None  # S1 only: regressor on open_return_pct, used for EV sizing
    apply_threshold: float = 0.5
    metrics: dict[str, Any] = field(default_factory=dict)
    shap_top: list[tuple[str, float]] = field(default_factory=list)
    tuned_params: dict[str, Any] = field(default_factory=dict)


def _classifier_params(pos: int, neg: int, tuned: dict[str, Any]) -> dict[str, Any]:
    weight = (neg / pos) if pos else 1.0
    return {
        "objective": "binary",
        "metric": "binary_logloss",
        "verbosity": -1,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "scale_pos_weight": min(max(weight, 1.0), 8.0),
        "n_estimators": 200,
        **tuned,
    }


def fit_s1(train: pd.DataFrame, board: str) -> FittedBundle:
    y = pd.to_numeric(train["is_clean_pop"], errors="coerce")
    mask = y.notna()
    x_raw = feature_matrix(train.loc[mask], S1_FEATURE_COLS, filled=False)
    x_fill = feature_matrix(train.loc[mask], S1_FEATURE_COLS, filled=True)
    yv = y.loc[mask].astype(int)
    pos = int((yv == 1).sum())
    neg = int((yv == 0).sum())
    bundle = FittedBundle(board=board, strategy="s1", feature_cols=list(S1_FEATURE_COLS))
    if lgb is not None and len(yv) >= 40 and pos > 3 and neg > 3:
        tuned = select_classifier_params(train, S1_FEATURE_COLS, "is_clean_pop")
        bundle.tuned_params = tuned
        model = lgb.LGBMClassifier(**_classifier_params(pos, neg, tuned))
        model.fit(x_raw, yv)
        bundle.lgbm = model
        bundle.shap_top = _shap_top(model, x_raw)
    # Regression head on the continuous open-day return. score_features() needs an
    # actual E[gain] to size EV -- without this it has only the classifier's
    # probability to lean on, which is not a rupee amount.
    ret = pd.to_numeric(train.get("open_return_pct"), errors="coerce")
    ret_mask = ret.notna()
    if lgb is not None and ret_mask.sum() >= 40:
        reg_tuned = select_regressor_params(train, S1_FEATURE_COLS, "open_return_pct")
        x_reg = feature_matrix(train.loc[ret_mask], S1_FEATURE_COLS, filled=False)
        reg = lgb.LGBMRegressor(
            objective="regression", verbosity=-1, n_estimators=200,
            feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1,
            **reg_tuned,
        )
        reg.fit(x_reg, ret.loc[ret_mask])
        bundle.lgbm_reg = reg
    if Pipeline is not None and len(yv) >= 40:
        pipe = Pipeline([
            ("scaler", StandardScaler()),
            ("clf", LogisticRegression(
                solver="saga",
                l1_ratio=0.5,
                C=0.5,
                max_iter=4000,
                class_weight="balanced",
            )),
        ])
        x_log = x_fill.fillna(x_fill.median()).fillna(0)
        pipe.fit(x_log, yv)
        bundle.logistic = pipe
    return bundle


def fit_s2(train: pd.DataFrame, board: str) -> FittedBundle:
    if "exret_126" in train.columns:
        y = pd.to_numeric(train["exret_126"], errors="coerce")
    else:
        y = pd.Series(np.nan, index=train.index)
    mask = y.notna()
    bundle = FittedBundle(board=board, strategy="s2", feature_cols=list(S2_FEATURE_COLS))
    if mask.sum() < 40 or lgb is None:
        return bundle
    x = feature_matrix(train.loc[mask], S2_FEATURE_COLS, filled=False)
    mono = []
    for col in S2_FEATURE_COLS:
        if col in ("roe", "roce", "pat_cagr"):
            mono.append(1)
        elif col in ("debt_equity", "ofs_ratio", "peer_rel_pe"):
            mono.append(-1)
        else:
            mono.append(0)
    tuned = select_regressor_params(train, S2_FEATURE_COLS, "exret_126")
    bundle.tuned_params = tuned
    model = lgb.LGBMRegressor(
        objective="regression",
        verbosity=-1,
        feature_fraction=0.8,
        bagging_fraction=0.8,
        bagging_freq=1,
        n_estimators=200,
        monotone_constraints=mono,
        **tuned,
    )
    model.fit(x, y.loc[mask])
    bundle.lgbm = model
    bundle.shap_top = _shap_top(model, x)
    return bundle


def predict_s1_proba(bundle: FittedBundle, df: pd.DataFrame) -> np.ndarray:
    x = feature_matrix(df, bundle.feature_cols, filled=False)
    if bundle.lgbm is not None:
        return bundle.lgbm.predict_proba(x)[:, 1]
    if bundle.logistic is not None:
        xf = feature_matrix(df, bundle.feature_cols, filled=True).fillna(0)
        return bundle.logistic.predict_proba(xf)[:, 1]
    return np.full(len(df), np.nan)


def predict_s1_open_return(bundle: FittedBundle, df: pd.DataFrame) -> Optional[np.ndarray]:
    """Predicted continuous open-day return %, used to size EV. None if no regressor fit."""
    if bundle.lgbm_reg is None:
        return None
    x = feature_matrix(df, bundle.feature_cols, filled=False)
    return bundle.lgbm_reg.predict(x)


def predict_s2(bundle: FittedBundle, df: pd.DataFrame) -> np.ndarray:
    if bundle.lgbm is None:
        return pd.to_numeric(df.get("quality_score"), errors="coerce").to_numpy()
    x = feature_matrix(df, bundle.feature_cols, filled=False)
    return bundle.lgbm.predict(x)


def calibrate_threshold(df: pd.DataFrame, proba: np.ndarray) -> tuple[float, dict[str, float]]:
    ev = pd.to_numeric(df.get("realized_ev"), errors="coerce")
    best_t, best_ev = 0.5, -1e18
    grid = np.linspace(0.2, 0.8, 13)
    for t in grid:
        pick = proba >= t
        if pick.sum() < 3:
            continue
        mean_ev = float(ev[pick].mean())
        if mean_ev > best_ev:
            best_ev, best_t = mean_ev, float(t)
    picked = proba >= best_t
    metrics = {
        "threshold": best_t,
        "n_apply": int(picked.sum()),
        "mean_ev": float(ev[picked].mean()) if picked.any() else None,
        "pop_rate": float(pd.to_numeric(df.loc[picked, "is_clean_pop"], errors="coerce").mean()) if picked.any() else None,
    }
    return best_t, metrics


def _shap_top(model: Any, x: pd.DataFrame, k: int = 5) -> list[tuple[str, float]]:
    try:
        import shap
    except ImportError:
        if hasattr(model, "feature_importances_"):
            imp = model.feature_importances_
            pairs = sorted(zip(x.columns, imp), key=lambda t: -t[1])[:k]
            return [(str(a), float(b)) for a, b in pairs]
        return []
    try:
        expl = shap.TreeExplainer(model)
        vals = expl.shap_values(x)
        if isinstance(vals, list):
            vals = vals[1] if len(vals) > 1 else vals[0]
        mean_abs = np.abs(vals).mean(axis=0)
        pairs = sorted(zip(x.columns, mean_abs), key=lambda t: -t[1])[:k]
        return [(str(a), float(b)) for a, b in pairs]
    except Exception:
        if hasattr(model, "feature_importances_"):
            imp = model.feature_importances_
            pairs = sorted(zip(x.columns, imp), key=lambda t: -t[1])[:k]
            return [(str(a), float(b)) for a, b in pairs]
        return []


def save_bundle(bundle: FittedBundle, path: Path) -> None:
    import pickle

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pickle.dumps(bundle))


def load_bundle(path: Path) -> FittedBundle:
    import pickle

    return pickle.loads(path.read_bytes())
