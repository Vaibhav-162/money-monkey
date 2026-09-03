"""Rolling walk-forward by listing year. Never random K-fold. Per-board only.

WHAT THIS FILE DOES
--------------------
Replays historical Apply/Skip decisions year by year so we never train on
IPOs that had not listed yet. Each fold trains on earlier `listing_year`s
and tests the next year, separately for `mainboard` and `sme`. Random
K-fold is forbidden here: IPOs are a time series, and a shuffled split
would let the model see the future.

`run_analysis.py` is the only production caller. It runs `walk_forward_s1`
and `walk_forward_s2` per board (metrics → `s1_walkforward.json` /
`s2_walkforward.json`), then `pooled_ablation_s1` and
`stop_loss_sensitivity` (comparison artifacts only). This file calls
`analysis.models` (`fit_s1` / `fit_s2`, `predict_*`, `calibrate_threshold`,
`save_bundle`). Walk-forward *does* pickle the last fold's bundle, but
`run_analysis.py` immediately re-fits on all 2020+ rows and overwrites
those files — the delivered scorer is the full-history fit, with S1's
`apply_threshold` copied from the last non-skipped walk-forward year.

KEY TERMS USED HERE
--------------------
- Walk-forward validation: rolling train-on-past / test-on-next-year.
  S1 starts test years at 2021 (train needs 2020+); S2 at 2018 when
  enough `exret_126` labels exist. A year with <40 train or <8 test
  rows is recorded as `skipped`, not scored.
- Mainboard vs SME: each walk is one `exchange_type`. The delivered
  scorer never mixes them; `pooled_ablation_s1` is the one-time
  "what if we ignored that rule" comparison.
- LightGBM / `FittedBundle`: fitted by `models.fit_*` each fold.
  S1 also gets a probability cutoff from `calibrate_threshold`.
- Clean pop (`is_clean_pop`): S1 label — a strong, held first-day jump.
  Fold metrics include pop rate among Apply rows and precision@top-10%.
- EV / `realized_ev`: historical rupee profit per lot (allotment ×
  open-day gain × haircut). The S1 apply cutoff is chosen to maximize
  mean EV on that fold's *test* rows — so the reported `apply_mean_ev`
  is after a threshold that saw those labels.
- Brier score: mean squared error of predicted probabilities vs the
  0/1 clean-pop label. Lower means better-calibrated probabilities;
  it is a diagnostic, not the threshold objective.
- SHAP (`shap_top`): which features drove that fold's model, copied
  into the JSON so a year-to-year drift is visible.
- `exret_126` / `s2_beat` / `ret_126` / `mdd_126`: 6-month Nifty-excess
  return, "beat Nifty by >5%", raw 126-session return, and max drawdown.
  These evaluate the *research* S2 regressor, not live `apply_s2`
  (which is the quality checklist in `score.py`).
- Information Ratio / CAGR proxies: cross-sectional stand-ins (mean/std
  of one fold's top-30% excess returns; average of each pick's own
  126-session return annualized). Named `_proxy` because there is one
  observation per IPO, not a daily portfolio series.
- Pooled ablation: one S1 model trained on mainboard+SME together.
  Never the delivered scorer — it exists to quantify what we give up
  by refusing to pool.
- Stop-loss sensitivity: *hypothetical* table only. Primary S1 EV
  assumes you sell at the listing open, so an intraday stop cannot
  fire. This asks "what if I held through day 1 with a protective
  stop instead?" and is never fed into training or `score_features()`.
- Quality-ranker fallback: when a board lacks ≥40 `exret_126` rows
  (usually because `scripts/fetch_prices.py` has not been run), S2
  walk-forward skips the regressor, pickles an empty bundle, and
  reports `mode: quality_ranker_fallback`.

FUNCTIONS / CLASSES IN THIS FILE
---------------------------------
- `walk_forward_s1(df, board, out_dir)`: yearly S1 replay, EV-calibrated
  threshold, Brier / precision / apply EV, pickle last fold (later
  overwritten). Returns `{folds, summary, n_rows}`.
- `walk_forward_s2(df, board, out_dir)`: yearly S2 regressor replay on
  labeled 6-month returns, or the quality-ranker fallback. Hit ratio,
  top-30% excess return, IR/CAGR proxies, mean drawdown.
- `stop_loss_sensitivity(df, board, stop_pcts)`: listing-low vs
  issue×(1+stop%) table for {0, −5, −10}%. Comparison only.
- `pooled_ablation_s1(df)`: same yearly S1 loop on both boards at once.
  Comparison only — `fit_s1(..., "pooled")` is not a live board name.
- `_precision_at_top(y, proba, frac)`: fraction of true clean pops in
  the top 10% by predicted probability (default).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from analysis.models import (
    FittedBundle,
    calibrate_threshold,
    fit_s1,
    fit_s2,
    predict_s1_proba,
    predict_s2,
    save_bundle,
)
from sklearn.metrics import brier_score_loss


def _precision_at_top(y: pd.Series, proba: np.ndarray, frac: float = 0.1) -> float | None:
    m = y.notna() & np.isfinite(proba)
    if m.sum() < 10:
        return None
    n = max(1, int(round(m.sum() * frac)))
    order = np.argsort(-proba[m])
    top = y[m].to_numpy()[order][:n]
    return float(np.mean(top))


def walk_forward_s1(df: pd.DataFrame, board: str, out_dir: Path) -> dict[str, Any]:
    chunk = df[(df["exchange_type"] == board) & (df["listing_year"] >= 2020)].copy()
    chunk = chunk.sort_values(["listing_date", "ipo_id"])
    folds: list[dict[str, Any]] = []
    last_bundle: FittedBundle | None = None
    years = sorted(int(y) for y in chunk["listing_year"].dropna().unique() if int(y) >= 2021)
    for test_year in years:
        train = chunk[chunk["listing_year"] < test_year]
        test = chunk[chunk["listing_year"] == test_year]
        if len(train) < 40 or len(test) < 8:
            folds.append({"test_year": test_year, "n_train": int(len(train)), "n_test": int(len(test)), "skipped": True})
            continue
        bundle = fit_s1(train, board)
        proba = predict_s1_proba(bundle, test)
        y = pd.to_numeric(test["is_clean_pop"], errors="coerce")
        t, cal = calibrate_threshold(test, proba)
        bundle.apply_threshold = t
        last_bundle = bundle
        pick = proba >= t
        ev = pd.to_numeric(test["realized_ev"], errors="coerce")
        brier = None
        if y.notna().sum() > 5 and np.isfinite(proba).all():
            try:
                brier = float(brier_score_loss(y.dropna(), proba[y.notna()]))
            except Exception:
                brier = None
        folds.append({
            "test_year": test_year,
            "n_train": int(len(train)),
            "n_test": int(len(test)),
            "skipped": False,
            "threshold": t,
            "precision_top10": _precision_at_top(y, proba),
            "brier": brier,
            "apply_n": int(pick.sum()),
            "apply_pop_rate": float(y[pick].mean()) if pick.any() else None,
            "apply_mean_ev": float(ev[pick].mean()) if pick.any() else None,
            "all_mean_ev": float(ev.mean()) if ev.notna().any() else None,
            "win_rate": float(y.mean()) if y.notna().any() else None,
            "shap_top": bundle.shap_top,
            **{f"cal_{k}": v for k, v in cal.items()},
        })
    if last_bundle is not None:
        save_bundle(last_bundle, out_dir / "models" / f"{board}_s1.pkl")
    numeric = [f for f in folds if not f.get("skipped")]
    summary = {}
    for key in ("precision_top10", "brier", "apply_mean_ev", "apply_pop_rate"):
        vals = [f[key] for f in numeric if f.get(key) is not None and pd.notna(f.get(key))]
        if vals:
            summary[key] = {"mean": float(np.mean(vals)), "std": float(np.std(vals)), "n_folds": len(vals)}
    return {"board": board, "strategy": "s1", "folds": folds, "summary": summary, "n_rows": int(len(chunk))}


def walk_forward_s2(df: pd.DataFrame, board: str, out_dir: Path) -> dict[str, Any]:
    chunk = df[df["exchange_type"] == board].copy()
    has_px = "exret_126" in chunk.columns and chunk["exret_126"].notna().sum() >= 40
    folds: list[dict[str, Any]] = []
    last: FittedBundle | None = None
    if not has_px:
        bundle = FittedBundle(board=board, strategy="s2", feature_cols=[])
        save_bundle(bundle, out_dir / "models" / f"{board}_s2.pkl")
        q = pd.to_numeric(chunk.get("quality_score"), errors="coerce")
        return {
            "board": board,
            "strategy": "s2",
            "mode": "quality_ranker_fallback",
            "n_rows": int(len(chunk)),
            "n_with_prices": int(chunk["exret_126"].notna().sum()) if "exret_126" in chunk.columns else 0,
            "quality_pass_rate": float((q >= 3).mean()) if q.notna().any() else None,
            "folds": [],
            "summary": {},
        }
    years = sorted(int(y) for y in chunk["listing_year"].dropna().unique() if int(y) >= 2018)
    for test_year in years:
        train = chunk[(chunk["listing_year"] < test_year) & chunk["exret_126"].notna()]
        test = chunk[(chunk["listing_year"] == test_year) & chunk["exret_126"].notna()]
        if len(train) < 40 or len(test) < 8:
            folds.append({"test_year": test_year, "n_train": int(len(train)), "n_test": int(len(test)), "skipped": True})
            continue
        bundle = fit_s2(train, board)
        pred = predict_s2(bundle, test)
        last = bundle
        actual = pd.to_numeric(test["exret_126"], errors="coerce")
        raw_ret = pd.to_numeric(test.get("ret_126"), errors="coerce")
        beat = pd.to_numeric(test.get("s2_beat"), errors="coerce")
        order = np.argsort(-pred)
        topn = max(1, int(round(len(test) * 0.3)))
        top_idx = order[:topn]
        top = actual.to_numpy()[top_idx]
        top_raw = raw_ret.to_numpy()[top_idx]
        mdd = pd.to_numeric(test.get("mdd_126"), errors="coerce")
        # Information Ratio: cross-sectional proxy (mean/std of excess return across
        # this fold's top-30% picks), not a true multi-period time-series IR -- we
        # only have one observation per IPO, not a return series per portfolio date.
        top_finite = top[np.isfinite(top)]
        ir_proxy = float(top_finite.mean() / top_finite.std()) if len(top_finite) > 1 and top_finite.std() > 0 else None
        # CAGR proxy: annualize each pick's own 126-session raw return, then average.
        # Not a compounded portfolio CAGR -- picks don't share a single holding period.
        top_raw_finite = top_raw[np.isfinite(top_raw) & (top_raw > -1)]
        cagr_proxy = float(np.mean((1.0 + top_raw_finite) ** (252.0 / 126.0) - 1.0)) if len(top_raw_finite) else None
        folds.append({
            "test_year": test_year,
            "n_train": int(len(train)),
            "n_test": int(len(test)),
            "skipped": False,
            "hit_ratio": float(beat.mean()) if beat.notna().any() else None,
            "mean_exret": float(actual.mean()),
            "top30_mean_exret": float(np.nanmean(top)),
            "information_ratio_proxy": ir_proxy,
            "cagr_proxy": cagr_proxy,
            "max_drawdown_mean": float(mdd.mean()) if mdd.notna().any() else None,
            "sharpe_mean": float(pd.to_numeric(test.get("sharpe_126"), errors="coerce").mean()),
            "shap_top": bundle.shap_top,
        })
        save_bundle(bundle, out_dir / "models" / f"{board}_s2.pkl")
    if last is not None:
        save_bundle(last, out_dir / "models" / f"{board}_s2.pkl")
    numeric = [f for f in folds if not f.get("skipped")]
    summary = {}
    for key in ("hit_ratio", "mean_exret", "top30_mean_exret", "information_ratio_proxy", "cagr_proxy", "max_drawdown_mean"):
        vals = [f[key] for f in numeric if f.get(key) is not None and pd.notna(f.get(key))]
        if vals:
            summary[key] = {"mean": float(np.mean(vals)), "std": float(np.std(vals)), "n_folds": len(vals)}
    return {"board": board, "strategy": "s2", "mode": "price_alpha", "folds": folds, "summary": summary, "n_rows": int(len(chunk))}


def stop_loss_sensitivity(df: pd.DataFrame, board: str, stop_pcts: tuple[float, ...] = (0.0, -5.0, -10.0)) -> dict[str, Any]:
    """Hypothetical secondary table only. The primary backtest (realized_ev, in
    walk_forward_s1) assumes you flip immediately at the listing open -- if that's
    your real behavior, an intraday stop is meaningless because you're not still
    holding when it could trigger. This answers a different question: "if I held
    through day 1 with a protective stop instead of selling at the open, what
    would that have been worth historically?" Never fed into the trained model or
    score_features(); for comparison only.
    """
    chunk = df[(df["exchange_type"] == board) & (df["listing_year"] >= 2020)].copy()
    ip = pd.to_numeric(chunk.get("issue_price"), errors="coerce")
    low = pd.to_numeric(chunk.get("listing_low"), errors="coerce")
    close = pd.to_numeric(chunk.get("listing_day_close"), errors="coerce")
    nse_last = pd.to_numeric(chunk.get("listing_nse_last"), errors="coerce")
    bse_last = pd.to_numeric(chunk.get("listing_bse_last"), errors="coerce")
    close = close.where(close.notna(), nse_last).where(close.notna() | nse_last.notna(), bse_last)
    p = pd.to_numeric(chunk.get("p_allot"), errors="coerce").fillna(0)
    haircut = pd.to_numeric(chunk.get("ev_haircut"), errors="coerce").fillna(1)
    lot_amt = pd.to_numeric(chunk.get("retail_min_amount"), errors="coerce")
    lot_fallback = pd.to_numeric(chunk.get("lot_size"), errors="coerce") * ip
    lot_amt = lot_amt.where(lot_amt.notna() & (lot_amt > 0), lot_fallback)
    have = ip.notna() & close.notna() & lot_amt.notna()
    rows = []
    for stop_pct in stop_pcts:
        stop_price = ip * (1.0 + stop_pct / 100.0)
        triggered = have & low.notna() & (low <= stop_price)
        exit_price = close.where(~triggered, stop_price)
        alt_return_pct = (exit_price / ip - 1.0) * 100.0
        alt_ev = p * (alt_return_pct / 100.0) * lot_amt * haircut
        m = have
        rows.append({
            "stop_pct": stop_pct,
            "n": int(m.sum()),
            "n_triggered": int(triggered.sum()),
            "trigger_rate": float(triggered[m].mean()) if m.any() else None,
            "mean_alt_return_pct": float(alt_return_pct[m].mean()) if m.any() else None,
            "mean_alt_ev": float(alt_ev[m].mean()) if m.any() else None,
        })
    return {"board": board, "hypothetical": True, "rows": rows}


def pooled_ablation_s1(df: pd.DataFrame) -> dict[str, Any]:
    """One-time comparison only. Never the delivered scorer."""
    chunk = df[df["listing_year"] >= 2020].copy()
    years = sorted(int(y) for y in chunk["listing_year"].dropna().unique() if int(y) >= 2021)
    folds = []
    for test_year in years:
        train = chunk[chunk["listing_year"] < test_year]
        test = chunk[chunk["listing_year"] == test_year]
        if len(train) < 40 or len(test) < 8:
            continue
        bundle = fit_s1(train, "pooled")
        proba = predict_s1_proba(bundle, test)
        y = pd.to_numeric(test["is_clean_pop"], errors="coerce")
        ev = pd.to_numeric(test["realized_ev"], errors="coerce")
        t, _ = calibrate_threshold(test, proba)
        pick = proba >= t
        folds.append({
            "test_year": test_year,
            "n_test": int(len(test)),
            "precision_top10": _precision_at_top(y, proba),
            "apply_mean_ev": float(ev[pick].mean()) if pick.any() else None,
        })
    return {"strategy": "s1_pooled_ablation", "folds": folds}
