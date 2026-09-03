"""Per-board EDA: correlations, VIF, distributions, cohorts.

WHAT THIS FILE DOES
--------------------
Descriptive statistics *before* modeling: how listing-day gain lines up
with close-day features, whether those features are collinear, and how
pops cluster by subscription and issue-size buckets. No model is fit
here and nothing is written to disk — `run_analysis.py` is the only
caller (`board_eda` once per `mainboard` and `sme`) and it dumps the
dict to `data/analysis/eda.json` via `analysis.report.dump_json`.

This file does not import the rest of the analysis package. It expects
`exchange_type` plus the feature/label columns that `add_features` /
`add_targets` already put on the frame.

KEY TERMS USED HERE
--------------------
- EDA (Exploratory Data Analysis): look at the data before trusting a
  model — correlations, shape of the gain distribution, missing GMP.
- Mainboard vs SME: `board_eda` slices on `exchange_type`. Mixing boards
  in one table would hide that SME pops and mainboard pops are different
  animals.
- Listing-day gain (`listing_day_gain_pct`): first-day % vs issue price.
  Used here as a descriptive target (Pearson/Spearman, discount vs
  premium rate), not as a model feature.
- Clean pop (`is_clean_pop`): in subscription-tier cohorts, the fraction
  of names that had a strong, held first-day jump.
- Subscription multiple (`sub_total_x`): times-oversubscribed. Cut into
  ≤1x / 1–5x / 5–20x / 20–50x / >50x to see whether hotter books listed
  better.
- Issue size (`issue_size_cr`): rupees raised in crore (1 crore = 10
  million). Size cohorts ask whether tiny books behave differently.
- GMP % vs issue / `gmp_at_close` / `gmp_anchor`: unofficial pre-listing
  premium as a % of issue price, and how that quote was dated. `gmp_fill_2020`
  is the fraction of 2020+ rows with a close-day GMP; `gmp_anchor_counts`
  flags listing-date-leaky vs ipo_close vs none.
- OFS ratio: fraction of the issue that is existing holders selling.
- ROE / ROCE / debt-equity / PE (`pe_pre`): profitability, leverage, and
  pre-issue valuation — the same fundamentals the models see.
- Allotment probability (`p_allot`) / promoter pre %: chance of getting
  shares, and founder stake before the IPO.
- Pearson / Spearman: linear vs rank correlation of each feature with
  listing gain. Spearman is more robust to a few insane GMP outliers.
- VIF (Variance Inflation Factor): how much a column is a linear combo
  of the others. High VIF means "this number is redundant"; it does not
  by itself drop a feature from LightGBM.

FUNCTIONS / CLASSES IN THIS FILE
---------------------------------
- `board_eda(df, board)`: the whole report for one exchange tier —
  correlations, VIF, gain distribution (mean/median/skew/VaR-5/p95),
  discount vs premium rate, subscription and size cohorts, GMP coverage.
- `_vif(frame)`: per-column VIF via least squares; empty if too few
  complete rows. Caps a near-perfect collinear column at 999.
- `EDA_COLS`: the feature list actually correlated and VIFed.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

EDA_COLS = [
    "sub_total_x", "issue_size_cr", "ofs_ratio", "gmp_pct_vs_issue",
    "pe_pre", "roe", "roce", "debt_equity", "p_allot", "promoter_pre_pct",
]


def _vif(frame: pd.DataFrame) -> dict[str, float]:
    cols = [c for c in frame.columns if frame[c].notna().sum() > 20]
    x = frame[cols].apply(pd.to_numeric, errors="coerce")
    x = x.dropna()
    if len(x) < 30 or x.shape[1] < 2:
        return {}
    out: dict[str, float] = {}
    arr = x.to_numpy(dtype=float)
    for i, col in enumerate(x.columns):
        y = arr[:, i]
        z = np.delete(arr, i, axis=1)
        z = np.column_stack([np.ones(len(z)), z])
        try:
            beta, *_ = np.linalg.lstsq(z, y, rcond=None)
            pred = z @ beta
            ss_res = float(np.sum((y - pred) ** 2))
            ss_tot = float(np.sum((y - y.mean()) ** 2))
            r2 = 1 - ss_res / ss_tot if ss_tot else 0
            out[col] = float(1 / (1 - r2)) if r2 < 0.999 else 999.0
        except np.linalg.LinAlgError:
            out[col] = float("nan")
    return out


def board_eda(df: pd.DataFrame, board: str) -> dict[str, Any]:
    chunk = df[df["exchange_type"] == board].copy()
    gain = pd.to_numeric(chunk.get("listing_day_gain_pct"), errors="coerce")
    feats = chunk.reindex(columns=EDA_COLS)
    for c in feats.columns:
        feats[c] = pd.to_numeric(feats[c], errors="coerce")
    target = gain
    pearson = {}
    spearman = {}
    for c in feats.columns:
        a = feats[c]
        m = a.notna() & target.notna()
        if m.sum() < 20:
            continue
        pearson[c] = float(a[m].corr(target[m], method="pearson"))
        spearman[c] = float(a[m].corr(target[m], method="spearman"))
    discount = float((gain < 0).mean()) if gain.notna().any() else None
    premium = float((gain > 0).mean()) if gain.notna().any() else None
    desc = gain.dropna()
    dist = {}
    if len(desc):
        dist = {
            "n": int(len(desc)),
            "mean": float(desc.mean()),
            "median": float(desc.median()),
            "std": float(desc.std()),
            "skew": float(desc.skew()),
            "kurtosis": float(desc.kurtosis()),
            "var_5": float(desc.quantile(0.05)),
            "p95": float(desc.quantile(0.95)),
        }
    sub = pd.to_numeric(chunk.get("sub_total_x"), errors="coerce")
    tiers = pd.cut(sub, bins=[-np.inf, 1, 5, 20, 50, np.inf], labels=["<=1x", "1-5x", "5-20x", "20-50x", ">50x"])
    cohort = (
        pd.DataFrame({"tier": tiers, "gain": gain, "pop": chunk.get("is_clean_pop")})
        .groupby("tier", observed=False)
        .agg(n=("gain", "count"), median_gain=("gain", "median"), pop_rate=("pop", "mean"))
        .reset_index()
        .to_dict(orient="records")
    )
    size = pd.to_numeric(chunk.get("issue_size_cr"), errors="coerce")
    size_tiers = pd.cut(size, bins=[-np.inf, 50, 200, 600, np.inf], labels=["<50", "50-200", "200-600", ">=600"])
    size_cohort = (
        pd.DataFrame({"tier": size_tiers, "gain": gain})
        .groupby("tier", observed=False)
        .agg(n=("gain", "count"), median_gain=("gain", "median"))
        .reset_index()
        .to_dict(orient="records")
    )
    return {
        "board": board,
        "n": int(len(chunk)),
        "n_2020": int((chunk["listing_year"] >= 2020).sum()),
        "pearson": pearson,
        "spearman": spearman,
        "vif": _vif(feats),
        "listing_gain": dist,
        "discount_rate": discount,
        "premium_rate": premium,
        "sub_cohorts": cohort,
        "size_cohorts": size_cohort,
        "gmp_fill_2020": float(
            chunk.loc[chunk["listing_year"] >= 2020, "gmp_at_close"].notna().mean()
        ) if "gmp_at_close" in chunk.columns else None,
        "gmp_anchor_counts": chunk["gmp_anchor"].value_counts(dropna=False).to_dict()
        if "gmp_anchor" in chunk.columns else {},
    }
