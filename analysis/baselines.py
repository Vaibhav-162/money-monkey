"""Friend-rule baselines and a live-bot-feasible total-sub + GMP + size rule.

WHAT THIS FILE DOES
--------------------
Scores simple hand-written "would we have applied?" rules on historical
IPOs so the ML walk-forward can be compared to something a human already
believes. `run_analysis.py` is the only production caller: it writes
`baselines.json` (three rules plus an apply-to-everyone 2020+ control) and
`s2_quality_checklist_eval.json` via `evaluate_quality_checklist`. Live
alerts do *not* import this file — `analysis/score.py` uses the fitted
models and `quality_ranker`, not these masks. The only import out is
`QUALITY_PASS_THRESHOLD` from `analysis/targets.py`.

KEY TERMS USED HERE
--------------------
- Baseline: a dumb-but-honest rule you measure the model against. If a
  three-line GMP+sub+size filter matches the model, the model is not adding
  much.
- GMP (Grey Market Premium): unofficial pre-listing premium, used here as
  `gmp_pct_vs_issue` or `gmp_pct_vs_cap` (percent of issue price / band cap).
- Subscription multiple (`sub_total_x`): times the offer was bid for.
- Issue size (crore): rupees raised (1 crore = 10 million). Friend rule 1
  and the live-feasible rule both require size < 600.
- QIB (Qualified Institutional Buyer): the institutional subscription
  split. `live_feasible_rule` refuses to use it — that breakdown is often
  paywalled, so a close-day bot cannot actually read it.
- Listing pop (`is_clean_pop`) / `listing_day_gain_pct` / `open_return_pct`:
  first-day outcome labels from `add_targets`, used only to *summarize* a
  rule, not to decide the mask.
- EV (`realized_ev`): allotment-adjusted expected rupee profit. Reported
  as `mean_ev` so a high-pop rule that never gets shares looks worse.
- ROE / debt-equity / peer-relative PE: friend rule 2's longer-hold
  fundamentals (healthy ROE, low leverage, not expensive vs same-year peers).
- `exret_126` / `s2_beat`: 6-month return minus Nifty, and "beat by >5%".
  The quality-checklist eval buckets these by `quality_score` 0–4.
- Mainboard vs SME: exchange tiers. Checklist eval is run separately per
  board so a liquid large-cap book is not mixed with a tiny SME.

FUNCTIONS / CLASSES IN THIS FILE
---------------------------------
- `rule1_listing_pop(df)`: friend flip rule — GMP ≥ 50%, sub ≥ 50x, size
  < 600. High-demand, smaller-book listing-day bet.
- `rule2_long_hold(df)`: friend hold rule — strong but not manic GMP
  (20–45%), sub ≥ 35x, ROE ≥ 12, D/E ≤ 0.5 or missing, peer PE at or below
  the industry-year median.
- `live_feasible_rule(df)`: what a close-day bot can actually compute:
  GMP ≥ 20%, total sub ≥ 20x, size < 600. No QIB split, no intraday GMP.
- `summarize_rule(df, mask, name)`: pop rate, median listing gain, mean EV,
  mean open return for the rows the mask picks.
- `evaluate_quality_checklist(df, board)`: buckets `quality_score` 0–4
  against realized `exret_126`, plus pass/fail rollup at threshold 3.
- `_exret_stats(chunk)`: n / hit ratio / mean excess return helper.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from analysis.targets import QUALITY_PASS_THRESHOLD


def rule1_listing_pop(df: pd.DataFrame) -> pd.Series:
    gmp = pd.to_numeric(df.get("gmp_pct_vs_cap"), errors="coerce")
    gmp = gmp.where(gmp.notna(), pd.to_numeric(df.get("gmp_pct_vs_issue"), errors="coerce"))
    sub = pd.to_numeric(df.get("sub_total_x"), errors="coerce")
    size = pd.to_numeric(df.get("issue_size_cr"), errors="coerce")
    return (gmp >= 50) & (sub >= 50) & (size < 600)


def rule2_long_hold(df: pd.DataFrame) -> pd.Series:
    sub = pd.to_numeric(df.get("sub_total_x"), errors="coerce")
    gmp = pd.to_numeric(df.get("gmp_pct_vs_issue"), errors="coerce")
    roe = pd.to_numeric(df.get("roe"), errors="coerce")
    de = pd.to_numeric(df.get("debt_equity"), errors="coerce")
    pe = pd.to_numeric(df.get("peer_rel_pe"), errors="coerce")
    return (
        (sub >= 35)
        & (gmp >= 20)
        & (gmp <= 45)
        & (roe >= 12)
        & (de.isna() | (de <= 0.5))
        & (pe.isna() | (pe <= 0))
    )


def live_feasible_rule(df: pd.DataFrame) -> pd.Series:
    """What a close-day bot can actually run: total sub + GMP + size. No QIB, no intraday GMP."""
    gmp = pd.to_numeric(df.get("gmp_pct_vs_issue"), errors="coerce")
    sub = pd.to_numeric(df.get("sub_total_x"), errors="coerce")
    size = pd.to_numeric(df.get("issue_size_cr"), errors="coerce")
    return (gmp >= 20) & (sub >= 20) & (size < 600)


def summarize_rule(df: pd.DataFrame, mask: pd.Series, name: str) -> dict:
    m = mask.fillna(False)
    n = int(m.sum())
    if n == 0:
        return {"rule": name, "n": 0, "pop_rate": None, "median_gain": None, "mean_ev": None}
    gain = pd.to_numeric(df.loc[m, "listing_day_gain_pct"], errors="coerce")
    pop = pd.to_numeric(df.loc[m, "is_clean_pop"], errors="coerce")
    ev = pd.to_numeric(df.loc[m, "realized_ev"], errors="coerce")
    return {
        "rule": name,
        "n": n,
        "pop_rate": float(pop.mean()) if pop.notna().any() else None,
        "median_gain": float(gain.median()) if gain.notna().any() else None,
        "mean_ev": float(ev.mean()) if ev.notna().any() else None,
        "mean_open_return": float(pd.to_numeric(df.loc[m, "open_return_pct"], errors="coerce").mean()),
    }


def _exret_stats(chunk: pd.DataFrame) -> dict[str, Any]:
    n = int(len(chunk))
    if n == 0:
        return {"n": 0, "hit_ratio": None, "mean_exret": None}
    beat = pd.to_numeric(chunk.get("s2_beat"), errors="coerce")
    ex = pd.to_numeric(chunk.get("exret_126"), errors="coerce")
    return {
        "n": n,
        "hit_ratio": float(beat.mean()) if beat.notna().any() else None,
        "mean_exret": float(ex.mean()) if ex.notna().any() else None,
    }


def evaluate_quality_checklist(df: pd.DataFrame, board: str) -> dict[str, Any]:
    """Score the 0-4 quality checklist against realized 6-month excess returns."""
    chunk = df[df["exchange_type"] == board].copy() if "exchange_type" in df.columns else df.copy()
    if "exret_126" not in chunk.columns:
        return {
            "board": board,
            "n_with_prices": 0,
            "threshold": QUALITY_PASS_THRESHOLD,
            "buckets": [],
            "rollup": {},
        }
    have = chunk[pd.to_numeric(chunk["exret_126"], errors="coerce").notna()].copy()
    score = pd.to_numeric(have.get("quality_score"), errors="coerce")
    buckets = []
    for pts in range(5):
        part = have[score == pts]
        buckets.append({"quality_score": pts, **_exret_stats(part)})
    passed = have[score >= QUALITY_PASS_THRESHOLD]
    failed = have[score < QUALITY_PASS_THRESHOLD]
    return {
        "board": board,
        "n_with_prices": int(len(have)),
        "threshold": QUALITY_PASS_THRESHOLD,
        "buckets": buckets,
        "rollup": {
            "quality_pass": _exret_stats(passed),
            "quality_fail": _exret_stats(failed),
        },
    }
