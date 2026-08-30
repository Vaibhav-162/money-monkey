"""Rolling walk-forward by listing year. Never random K-fold. Per-board only."""

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
