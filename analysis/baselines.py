"""Friend-rule baselines and a live-bot-feasible total-sub + GMP + size rule."""

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
