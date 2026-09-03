"""Close-day scorer. Dispatches to the board-specific model. Never mixes boards.

WHAT THIS FILE DOES
--------------------
This is the live Apply/Skip API: one IPO row of fields knowable at ~15:30
IST on close day in, a dict of S1/S2 decisions out. It never trains and
never sees listing OHLC. `scripts/live_scanner.py` is the production
caller (`score_features` after `to_score_row`); README examples and
`tests/test_live_scanner.py` / `tests/test_score_s2_hybrid.py` are the
other users. The historical trainer (`run_analysis.py`) does *not* import
this module — it writes the pickles this file later loads.

`Scorer` loads `{board}_s1.pkl` / `{board}_s2.pkl` from
`data/analysis/models/` via `analysis.models.load_bundle`, then calls
`predict_s1_proba`, `predict_s1_open_return`, and `predict_s2`. Before
that it runs `analysis.features.add_features` on a one-row frame (same
math as training) and `analysis.targets.quality_ranker` /
`quality_checklist_for_row` for Strategy 2. Unknown `exchange_type`
raises; `mainline`/`main` are coerced to `mainboard`.

KEY TERMS USED HERE
--------------------
- Close day: last IST day investors can apply. Predictions here must use
  only information available then (GMP, subscription, fundamentals) —
  listing prices are the thing we are trying to forecast, not an input.
- Mainboard vs SME: `score_row` routes to that board's pickles. Mixing
  boards would violate the "never fit across boards" rule in `models.py`.
- apply_s1: Strategy 1 decision — apply for a listing-day flip if
  `p_pop` ≥ the bundle's `apply_threshold` (copied from the last
  walk-forward year by `run_analysis.py`). False when no S1 bundle.
- apply_s2: Strategy 2 decision — *always* the 4-point quality checklist
  (`quality_score` ≥ `QUALITY_PASS_THRESHOLD`, currently 3), never the
  S2 LightGBM. A high `s2_model_exret_pred` cannot flip apply_s2.
- Quality score / checklist: 0–4 sanity card (subscription, OFS ratio,
  ROE, debt-equity). `quality_breakdown` is pass/fail/not_disclosed per
  check so a missing OFS is not shown as a genuine pass.
- p_pop: model P(clean pop) — a strong, held first-day price jump.
- p_allot: chance a retail applicant gets *any* shares when the book is
  oversubscribed. Already computed in `add_features`.
- EV / `ev_retail`: p_allot × expected rupee gain per lot. Gain is
  (predicted open-return % / 100) × lot rupees × `ev_haircut` when the
  S1 regressor exists (`ev_retail_source: lgbm_regressor`); otherwise a
  linear proxy in p_pop (`p_pop_heuristic_fallback`).
- Lot size / `retail_min_amount`: shares per application, or the rupee
  ticket for one retail lot. Percent returns are converted to rupees
  here because a 15% pop on a small lot is not the same as on a large one.
- GMP missing / liquidity_flag: `gmp_missing` tells the model there was
  no street quote; `liquidity_risk` / `ev_haircut` discount illiquid SME
  prints inside the EV math (set in features, consumed here).
- S2 model status (`experimental_unvalidated`): the walk-forward S2
  regressor is exposed as `s2_model_exret_pred` for monitoring only. It
  is not what apply_s2 uses.

FUNCTIONS / CLASSES IN THIS FILE
---------------------------------
- `score_features(new_ipo_row, model_dir)`: public entry. Caches one
  `Scorer` per process (rebuilt if `model_dir` changes) and returns
  Apply/Skip plus EV/quality fields for a single live IPO.
- `Scorer`: loads and caches per-board bundles; `score_row` is the
  actual feature → predict → EV assembly.
- `_board_of(row)`: maps `exchange_type`/`board` to `mainboard` or
  `sme`. Rejects anything else so a bad scrape cannot silently hit the
  wrong pickle.
- `_bundle(board, strategy)`: pickle load with in-memory cache; None if
  the file is missing (S1 then yields no apply, S2 still has the
  checklist).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from analysis.features import add_features
from analysis.models import (
    FittedBundle,
    load_bundle,
    predict_s1_open_return,
    predict_s1_proba,
    predict_s2,
)
from analysis.targets import (
    QUALITY_PASS_THRESHOLD,
    quality_checklist_for_row,
    quality_ranker,
)

ROOT_DEFAULT = Path(__file__).resolve().parents[1] / "data" / "analysis"


def _board_of(row: dict[str, Any] | pd.Series) -> str:
    raw = str(row.get("exchange_type") or row.get("board") or "").strip().lower()
    if raw in {"sme", "mainboard"}:
        return raw
    if raw in {"mainline", "main"}:
        return "mainboard"
    raise ValueError(f"unknown exchange_type={raw!r}; pass mainboard or sme")


class Scorer:
    def __init__(self, model_dir: Path | None = None):
        self.model_dir = Path(model_dir) if model_dir else ROOT_DEFAULT / "models"
        self._cache: dict[str, FittedBundle] = {}

    def _bundle(self, board: str, strategy: str) -> FittedBundle | None:
        key = f"{board}_{strategy}"
        if key in self._cache:
            return self._cache[key]
        path = self.model_dir / f"{key}.pkl"
        if not path.exists():
            return None
        bundle = load_bundle(path)
        self._cache[key] = bundle
        return bundle

    def score_row(self, row: dict[str, Any] | pd.Series) -> dict[str, Any]:
        board = _board_of(row)
        frame = pd.DataFrame([dict(row)])
        frame = add_features(frame)
        frame["quality_score"] = quality_ranker(frame)
        s1 = self._bundle(board, "s1")
        s2 = self._bundle(board, "s2")
        p_pop = None
        apply_s1 = False
        exp_open_return_pct = None
        if s1 is not None:
            p_pop = float(predict_s1_proba(s1, frame)[0])
            apply_s1 = bool(p_pop >= s1.apply_threshold)
            reg_pred = predict_s1_open_return(s1, frame)
            if reg_pred is not None:
                exp_open_return_pct = float(reg_pred[0])
        s2_score = float(frame["quality_score"].iloc[0])
        apply_s2 = bool(s2_score >= QUALITY_PASS_THRESHOLD)
        quality_breakdown = quality_checklist_for_row(frame.iloc[0])
        s2_model_exret_pred = None
        s2_model_status = None
        if s2 is not None and s2.lgbm is not None:
            s2_model_exret_pred = float(predict_s2(s2, frame)[0])
            s2_model_status = "experimental_unvalidated"
        p_allot = float(frame["p_allot"].iloc[0]) if pd.notna(frame["p_allot"].iloc[0]) else None
        if "retail_min_amount" in frame.columns:
            lot = pd.to_numeric(frame["retail_min_amount"], errors="coerce")
        else:
            lot = pd.Series(np.nan, index=frame.index)
        if lot.isna().all():
            ls = pd.to_numeric(frame["lot_size"], errors="coerce") if "lot_size" in frame.columns else 0
            ip = pd.to_numeric(frame["issue_price"], errors="coerce") if "issue_price" in frame.columns else 0
            lot = ls * ip
        expected_gain = None
        expected_gain_source = None
        lot0 = float(lot.iloc[0]) if hasattr(lot, "iloc") else float(lot)
        haircut = float(frame["ev_haircut"].iloc[0])
        if pd.notna(lot0) and lot0 > 0:
            if exp_open_return_pct is not None:
                expected_gain = (exp_open_return_pct / 100.0) * lot0 * haircut
                expected_gain_source = "lgbm_regressor"
            elif p_pop is not None:
                # Fallback only: no regressor was fit (e.g. too few rows with
                # open_return_pct in the training window). This linear proxy in
                # p_pop is a rough stand-in, not a fitted expected-return model.
                expected_gain = (2 * p_pop - 0.5) * 0.15 * lot0 * haircut
                expected_gain_source = "p_pop_heuristic_fallback"
        ev = None if p_allot is None or expected_gain is None else p_allot * expected_gain
        return {
            "board_model_used": board,
            "p_pop": p_pop,
            "exp_open_return_pct": exp_open_return_pct,
            "p_allot": p_allot,
            "ev_retail": ev,
            "ev_retail_source": expected_gain_source,
            "liquidity_flag": int(frame["liquidity_risk"].iloc[0]),
            "s2_score": s2_score,
            "quality_score": s2_score,
            "quality_breakdown": quality_breakdown,
            "apply_s1": apply_s1,
            "apply_s2": apply_s2,
            "s2_model_exret_pred": s2_model_exret_pred,
            "s2_model_status": s2_model_status,
            "gmp_missing": int(frame["gmp_missing"].iloc[0]),
            "apply_threshold_s1": None if s1 is None else s1.apply_threshold,
        }


_SCORER: Scorer | None = None


def score_features(new_ipo_row: dict[str, Any], model_dir: Path | None = None) -> dict[str, Any]:
    """Public API: variables knowable at 15:30 on IPO close day -> Apply/Skip."""
    global _SCORER
    if _SCORER is None or (model_dir and Path(model_dir) != _SCORER.model_dir):
        _SCORER = Scorer(model_dir)
    return _SCORER.score_row(new_ipo_row)
